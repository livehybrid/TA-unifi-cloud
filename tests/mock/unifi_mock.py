"""Deterministic stand-in for the UniFi APIs (stdlib only, HTTPS).

Serves, with entirely synthetic data:

* Site Manager:   /ea/hosts, /ea/sites, /ea/devices, /v1/hosts (pageSize + nextToken)
* Cloud connector: /v1/connector/consoles/<id>/proxy/network/integration/v1/...
* Local console:   /proxy/network/integration/v1/...

The local console serves the same data as console "mock-console-a".

Behaviour the collectors must cope with, all taken from the real APIs:

* offset/limit pagination, with the page size clamped to MAX_LIMIT (the real
  API clamps at 200; the mock clamps at 25 so a collector that does not page
  loses data visibly);
* console "mock-console-b" has no zone-based firewall (400
  api.firewall.zone-based-firewall-not-configured) and no Wi-Fi endpoint
  (404 api.request.error, "not supported");
* console "mock-console-c" is offline (every proxied path: 404 device_offline);
* any key other than GOOD_KEY gets 401;
* the first request for console A's networks list on each site answers 429
  once with Retry-After: 0, so collectors must retry.

Control endpoints (never used by the add-on):
  GET /__mock/health     liveness
  GET /__mock/requests   JSON list of the paths requested so far
  POST /__mock/reset     clear the request log and the 429 state

Run:  python unifi_mock.py --port 8443 --cert cert.pem --key key.pem
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GOOD_KEY = "mock-good-key"
MAX_LIMIT = 25
HOSTS_PAGE_SIZE = 2

# Per-site record counts. Chosen so several resources need more than one page.
CONSOLES = {
    "mock-console-a": {
        "online": True,
        "sites": {
            "site-a1": {"devices": 7, "clients": 60, "networks": 4, "wifi/broadcasts": 3,
                        "firewall/zones": 6, "firewall/policies": 57},
            "site-a2": {"devices": 3, "clients": 5, "networks": 2, "wifi/broadcasts": 1,
                        "firewall/zones": 4, "firewall/policies": 9},
        },
        "unsupported": {},
    },
    "mock-console-b": {
        "online": True,
        "sites": {
            "site-b1": {"devices": 2, "clients": 30, "networks": 3, "wifi/broadcasts": 0,
                        "firewall/zones": 0, "firewall/policies": 0},
        },
        "unsupported": {
            "firewall/zones": (400, "api.firewall.zone-based-firewall-not-configured"),
            "firewall/policies": (400, "api.firewall.zone-based-firewall-not-configured"),
            "wifi/broadcasts": (404, "api.request.error"),
        },
    },
    "mock-console-c": {"online": False, "sites": {}, "unsupported": {}},
}
LOCAL_CONSOLE = "mock-console-a"
# Device statistics answer 404 for this one device (it is not adopted).
NO_STATS_DEVICE = "site-a1-device-6"

RESOURCES = ("devices", "clients", "networks", "wifi/broadcasts", "firewall/zones", "firewall/policies")


def _mac(n):
    return "02:00:00:%02x:%02x:%02x" % ((n >> 16) & 255, (n >> 8) & 255, n & 255)


def record(site, resource, i):
    """One synthetic record, shaped like the real API's list item."""
    rid = "%s-%s-%d" % (site, resource.replace("/", "-").rstrip("s"), i)
    if resource == "devices":
        rid = "%s-device-%d" % (site, i)
        return {"id": rid, "macAddress": _mac(i), "ipAddress": "198.51.100.%d" % (i + 1),
                "name": "Mock device %d" % i, "model": "MOCK-AP", "state": "ONLINE",
                "supported": True, "firmwareVersion": "1.0.%d" % i, "firmwareUpdatable": False,
                "features": ["accessPoint"], "interfaces": ["radios"]}
    if resource == "clients":
        return {"type": "WIRELESS", "id": rid, "name": "mock-client-%d" % i,
                "connectedAt": "2026-01-01T00:00:00Z", "ipAddress": "203.0.113.%d" % (i % 250 + 1),
                "macAddress": _mac(1000 + i), "uplinkDeviceId": "%s-device-0" % site,
                "access": {"type": "DEFAULT"}}
    if resource == "networks":
        return {"management": "GATEWAY", "id": rid, "name": "Mock network %d" % i, "enabled": True,
                "vlanId": i + 1, "metadata": {"origin": "USER_DEFINED"}, "default": i == 0}
    if resource == "wifi/broadcasts":
        return {"type": "STANDARD", "id": rid, "name": "mock-ssid-%d" % i, "enabled": True,
                "metadata": {"origin": "USER_DEFINED"}, "network": {"type": "NATIVE"},
                "securityConfiguration": {"type": "WPA2_PERSONAL"},
                "broadcastingFrequenciesGHz": [2.4, 5]}
    if resource == "firewall/zones":
        return {"id": rid, "name": "Mock zone %d" % i, "networkIds": [],
                "metadata": {"origin": "SYSTEM_DEFINED", "configurable": False}}
    if resource == "firewall/policies":
        return {"id": rid, "enabled": True, "name": "Mock policy %d" % i, "index": i,
                "action": {"type": "ALLOW", "allowReturnTraffic": True},
                "source": {"zoneId": "z1"}, "destination": {"zoneId": "z2"},
                "ipProtocolScope": {"ipVersion": "IPV4_AND_IPV6"}, "loggingEnabled": False,
                "metadata": {"origin": "USER_DEFINED"}}
    raise KeyError(resource)


def stats(device_id):
    return {"uptimeSec": 1000, "lastHeartbeatAt": "2026-01-01T00:00:00Z",
            "nextHeartbeatAt": "2026-01-01T00:00:30Z", "loadAverage1Min": 0.1,
            "loadAverage5Min": 0.1, "loadAverage15Min": 0.1, "cpuUtilizationPct": 5.0,
            "memoryUtilizationPct": 40.0, "uplink": {"txRateBps": 1, "rxRateBps": 2}}


def site_record(site_id):
    return {"id": site_id, "internalReference": "default", "name": "Mock " + site_id}


# ---- expected totals (what a correct collector indexes) ---------------------

def expected_counts(console_ids):
    """Events per Network-API resource a correct collector produces for these consoles."""
    out = {"sites": 0, "device_statistics": 0}
    for r in RESOURCES:
        out[r] = 0
    for cid in console_ids:
        c = CONSOLES[cid]
        if not c["online"]:
            continue
        out["sites"] += len(c["sites"])
        for site, counts in c["sites"].items():
            for r in RESOURCES:
                if r not in c["unsupported"]:
                    out[r] += counts[r]
            out["device_statistics"] += sum(
                1 for i in range(counts["devices"]) if "%s-device-%d" % (site, i) != NO_STATS_DEVICE)
    return out


# ---- HTTP -----------------------------------------------------------------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests = []
        self.throttled = set()

    def reset(self):
        with self.lock:
            self.requests = []
            self.throttled = set()


STATE = State()

_INTEGRATION = re.compile(r"^/v1(?P<rest>/.*)?$")


class Handler(BaseHTTPRequestHandler):
    server_version = "unifi-mock/1"
    # Keep-alive: a TLS handshake per request made the suite crawl.
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, status, body, headers=None):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _err(self, status, code, cloud):
        if cloud:
            return self._send(status, {"code": code, "httpStatusCode": status, "message": code,
                                       "traceId": "0"})
        return self._send(status, {"statusCode": status, "statusName": "ERROR", "code": code,
                                   "message": code})

    def do_POST(self):
        if self.path == "/__mock/reset":
            STATE.reset()
            return self._send(200, {"ok": True})
        return self._err(405, "method", False)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        q = dict(urllib.parse.parse_qsl(url.query))
        if path == "/__mock/health":
            return self._send(200, {"ok": True})
        if path == "/__mock/requests":
            with STATE.lock:
                return self._send(200, list(STATE.requests))
        with STATE.lock:
            STATE.requests.append(path)

        key = self.headers.get("X-API-KEY")
        local_prefix = "/proxy/network/integration"
        cloud = not path.startswith(local_prefix)
        if key != GOOD_KEY:
            return self._err(401, "unauthorized", cloud)

        if path.startswith(local_prefix):
            return self._integration(LOCAL_CONSOLE, path[len(local_prefix):], q)
        m = re.match(r"^/v1/connector/consoles/([^/]+)/proxy/network/integration(/.*)$", path)
        if m:
            cid = m.group(1)
            if cid not in CONSOLES:
                return self._err(404, "not_found", True)
            if not CONSOLES[cid]["online"]:
                return self._err(404, "device_offline", True)
            return self._integration(cid, m.group(2), q)
        if path in ("/ea/hosts", "/v1/hosts"):
            return self._hosts(q)
        if path == "/ea/sites":
            data = []
            for cid, c in CONSOLES.items():
                for s in c["sites"]:
                    data.append({"siteId": s, "hostId": cid, "meta": {"desc": "Mock " + s, "name": "default"}})
            return self._send(200, {"data": data, "httpStatusCode": 200, "traceId": "0"})
        if path == "/ea/devices":
            data = []
            for cid, c in CONSOLES.items():
                devs = []
                for s, counts in c["sites"].items():
                    devs += [record(s, "devices", i) for i in range(counts["devices"])]
                data.append({"hostId": cid, "hostName": "Mock " + cid, "devices": devs,
                             "updatedAt": "2026-01-01T00:00:00Z"})
            return self._send(200, {"data": data, "httpStatusCode": 200, "traceId": "0"})
        return self._err(404, "not_found", True)

    def _hosts(self, q):
        ids = sorted(CONSOLES)
        start = int(q.get("nextToken") or 0)
        size = min(int(q.get("pageSize") or HOSTS_PAGE_SIZE), HOSTS_PAGE_SIZE)
        page = ids[start:start + size]
        body = {"data": [{"id": cid, "hardwareId": "hw-" + cid, "type": "console",
                          "ipAddress": "192.0.2.1", "owner": True, "isBlocked": False,
                          "reportedState": {"state": "connected" if CONSOLES[cid]["online"] else "disconnected"}}
                         for cid in page],
                "httpStatusCode": 200, "traceId": "0"}
        if start + size < len(ids):
            body["nextToken"] = str(start + size)
        return self._send(200, body)

    def _page(self, items, q):
        try:
            offset = max(int(q.get("offset", 0)), 0)
            limit = int(q.get("limit", MAX_LIMIT))
        except ValueError:
            return self._err(400, "api.request.argument-type-mismatch", False)
        limit = max(0, min(limit, MAX_LIMIT))
        page = items[offset:offset + limit]
        return self._send(200, {"offset": offset, "limit": limit, "count": len(page),
                                "totalCount": len(items), "data": page})

    def _integration(self, cid, rest, q):
        c = CONSOLES[cid]
        m = _INTEGRATION.match(rest)
        if not m:
            return self._err(404, "api.request.error", False)
        rest = m.group("rest") or ""
        if rest == "/info":
            return self._send(200, {"applicationVersion": "10.0.0"})
        if rest == "/sites":
            return self._page([site_record(s) for s in c["sites"]], q)
        m = re.match(r"^/sites/([^/]+)/(.+)$", rest)
        if not m or m.group(1) not in c["sites"]:
            return self._err(404, "api.request.error", False)
        site, sub = m.group(1), m.group(2)
        counts = c["sites"][site]
        md = re.match(r"^devices/([^/]+)/statistics/latest$", sub)
        if md:
            did = md.group(1)
            ids = {"%s-device-%d" % (site, i) for i in range(counts["devices"])}
            if did not in ids or did == NO_STATS_DEVICE:
                return self._err(404, "api.request.error", False)
            return self._send(200, stats(did))
        if sub in c["unsupported"]:
            status, code = c["unsupported"][sub]
            return self._err(status, code, False)
        if sub not in RESOURCES:
            return self._err(404, "api.request.error", False)
        if sub == "networks" and cid == "mock-console-a":
            with STATE.lock:
                first = site not in STATE.throttled
                STATE.throttled.add(site)
            if first:
                return self._send(429, {"statusCode": 429, "code": "rate_limited"}, {"Retry-After": "0"})
        return self._page([record(site, sub, i) for i in range(counts[sub])], q)


def make_server(port, cert, key, host="0.0.0.0"):
    srv = ThreadingHTTPServer((host, port), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.daemon_threads = True
    return srv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--cert", required=True)
    ap.add_argument("--key", required=True)
    a = ap.parse_args()
    make_server(a.port, a.cert, a.key).serve_forever()


if __name__ == "__main__":
    main()
