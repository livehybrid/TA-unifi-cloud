"""Collector tests against the synthetic mock (no Splunk, no network, no secrets).

Expected values come from the mock's own fixture tables (tests/mock), never
from the collector: the collector reaches the data over HTTP with pagination,
the tests count it straight from the table that generates it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.request
import ssl

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "package", "bin"))
sys.path.insert(0, os.path.join(ROOT, "tests", "mock"))

import unifi_client as uc  # noqa: E402
import unifi_mock as mock  # noqa: E402

ALL = list(uc.RESOURCES)
# resource name in the add-on -> path segment in the mock tables
PATHS = {"devices": "devices", "clients": "clients", "networks": "networks",
         "wifi_broadcasts": "wifi/broadcasts", "firewall_zones": "firewall/zones",
         "firewall_policies": "firewall/policies", "sites": "sites",
         "device_statistics": "device_statistics"}


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    d = tmp_path_factory.mktemp("certs")
    cert, key = str(d / "cert.pem"), str(d / "key.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                    "-subj", "/CN=localhost", "-keyout", key, "-out", cert],
                   check=True, capture_output=True)
    srv = mock.make_server(0, cert, key, host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "https://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()


@pytest.fixture(autouse=True)
def reset(server):
    _control(server, "/__mock/reset", "POST")


def _control(server, path, method="GET"):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(server + path, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
        return json.loads(r.read())


def requested(server):
    return _control(server, "/__mock/requests")


def collect(ctype, url, key=mock.GOOD_KEY, verify=False, resources=ALL):
    events, statuses = {}, []
    for cid, client in uc.network_targets(ctype, key, url, verify):
        for st, rec in uc.collect_console(client, resources, cid, ctype):
            if st == "status":
                statuses.append(rec)
            else:
                events.setdefault(st, []).append(rec)
    return events, statuses


def counts_by_resource(events):
    return {r: len(events.get(uc.RESOURCES[r][1], [])) for r in ALL}


def expected(console_ids):
    exp = mock.expected_counts(console_ids)
    return {r: exp[PATHS[r]] for r in ALL}


# ---- local mode -----------------------------------------------------------

def test_local_collects_every_record_of_every_resource(server):
    events, statuses = collect("local", server)
    assert counts_by_resource(events) == expected([mock.LOCAL_CONSOLE])
    assert [s["state"] for s in statuses] == ["ok"]


def test_local_requests_only_the_local_integration_path(server):
    collect("local", server, resources=["sites", "clients"])
    paths = requested(server)
    assert paths, "no request reached the mock"
    assert all(p.startswith("/proxy/network/integration/v1/") for p in paths), sorted(set(paths))[:5]


def test_local_url_forms_are_equivalent(server):
    for url in (server, server + "/", server + "/proxy/network/integration"):
        events, _ = collect("local", url, resources=["sites"])
        assert len(events["unifi:network:site"]) == len(mock.CONSOLES[mock.LOCAL_CONSOLE]["sites"]), url


def test_pagination_reaches_records_past_the_first_page(server):
    # 60 clients on site-a1 at 25 a page: the last page holds ids 50..59.
    events, _ = collect("local", server, resources=["clients"])
    ids = {e["id"] for e in events["unifi:network:client"]}
    n = mock.CONSOLES[mock.LOCAL_CONSOLE]["sites"]["site-a1"]["clients"]
    assert n > mock.MAX_LIMIT * 2
    assert "site-a1-client-%d" % (n - 1) in ids
    assert len(ids) == len(events["unifi:network:client"]), "duplicate records across pages"


def test_pagination_asks_for_each_offset(server):
    collect("local", server, resources=["clients"])
    # The request log holds paths only; count list calls for site-a1 clients.
    calls = [p for p in requested(server) if p.endswith("/sites/site-a1/clients")]
    n = mock.CONSOLES[mock.LOCAL_CONSOLE]["sites"]["site-a1"]["clients"]
    assert len(calls) == -(-n // mock.MAX_LIMIT)


def test_events_carry_site_context(server):
    events, _ = collect("local", server, resources=["networks"])
    sites = {e["unifi_site_id"] for e in events["unifi:network:network"]}
    assert sites == set(mock.CONSOLES[mock.LOCAL_CONSOLE]["sites"])
    assert all(e["unifi_connection"] == "local" and "unifi_console_id" not in e
               for e in events["unifi:network:network"])


def test_rate_limit_is_retried(server):
    # The mock answers 429 to the first networks call of each console-a site.
    events, statuses = collect("local", server, resources=["networks"])
    assert len(events["unifi:network:network"]) == expected([mock.LOCAL_CONSOLE])["networks"]
    assert statuses[0]["state"] == "ok"


def test_device_statistics_skip_device_without_stats(server):
    events, statuses = collect("local", server, resources=["device_statistics"])
    recs = events["unifi:network:device:statistics"]
    assert mock.NO_STATS_DEVICE not in {r["unifi_device_id"] for r in recs}
    assert len(recs) == expected([mock.LOCAL_CONSOLE])["device_statistics"]
    assert statuses[0]["state"] == "ok"


# ---- cloud mode -----------------------------------------------------------

def test_cloud_iterates_every_console_across_host_pages(server):
    _, statuses = collect("cloud", server, resources=["sites"])
    assert len(mock.CONSOLES) > mock.HOSTS_PAGE_SIZE
    assert sorted(s["console_id"] for s in statuses) == sorted(mock.CONSOLES)


def test_cloud_collects_online_consoles_through_the_connector(server):
    events, _ = collect("cloud", server)
    assert counts_by_resource(events) == expected(list(mock.CONSOLES))
    proxied = [p for p in requested(server) if "/proxy/network/integration/" in p]
    assert proxied and all(p.startswith("/v1/connector/consoles/") for p in proxied)


def test_dead_console_is_reported_and_others_continue(server):
    events, statuses = collect("cloud", server)
    by = {s["console_id"]: s for s in statuses}
    assert by["mock-console-c"]["state"] == "console_unavailable"
    assert by["mock-console-c"]["error_code"] == "device_offline"
    assert by["mock-console-a"]["state"] == "ok"
    assert by["mock-console-b"]["state"] == "ok"
    assert {e["unifi_console_id"] for e in events["unifi:network:site"]} == {"mock-console-a", "mock-console-b"}


def test_unsupported_endpoints_are_not_failures(server):
    _, statuses = collect("cloud", server)
    b = {s["console_id"]: s for s in statuses}["mock-console-b"]
    assert sorted(b["unsupported"]) == ["firewall_policies", "firewall_zones", "wifi_broadcasts"]
    assert b["errors"] == {}
    assert b["resources"]["clients"] == mock.CONSOLES["mock-console-b"]["sites"]["site-b1"]["clients"]


# ---- auth and TLS -----------------------------------------------------------

def test_local_bad_key_reports_auth_error(server):
    events, statuses = collect("local", server, key="wrong-key")
    assert events == {}
    assert statuses[0]["state"] == "auth_error" and statuses[0]["http_status"] == 401


def test_cloud_bad_key_fails_listing_consoles(server):
    with pytest.raises(uc.UnifiAuthError):
        uc.network_targets("cloud", "wrong-key", server, False)


def test_tls_verify_on_rejects_self_signed_console(server):
    events, statuses = collect("local", server, verify=True)
    assert events == {}
    assert statuses[0]["state"] == "connection_error"
    assert statuses[0]["error_code"] == "tls_verify_failed"


def test_tls_verify_off_accepts_self_signed_console(server):
    events, statuses = collect("local", server, verify=False, resources=["sites"])
    assert statuses[0]["state"] == "ok" and events["unifi:network:site"]


def test_errors_never_carry_the_key_or_url(server):
    _, statuses = collect("local", server, key="wrong-key-123")
    text = json.dumps(statuses)
    assert "wrong-key-123" not in text and server not in text


# ---- configuration ----------------------------------------------------------

def test_local_requires_https_console_url():
    with pytest.raises(ValueError):
        uc.network_targets("local", "k", "", True)
    with pytest.raises(ValueError):
        uc.network_targets("local", "k", "http://console", True)


def test_resource_parsing():
    assert uc.parse_resources("") == ALL
    assert uc.parse_resources("clients, sites") == ["clients", "sites"]
    with pytest.raises(ValueError):
        uc.parse_resources("clients,vouchers")


# ---- Site Manager (existing inputs) -----------------------------------------

def test_site_manager_devices_cover_every_console(server):
    client = uc.UnifiClient(server, mock.GOOD_KEY, verify=False)
    entries = list(client.site_manager("/ea/devices"))
    devices = [d for e in entries for d in e["devices"]]
    want = sum(c for con in mock.CONSOLES.values() for s in con["sites"].values() for c in [s["devices"]])
    assert len(devices) == want


def test_site_manager_hosts_follow_next_token(server):
    client = uc.UnifiClient(server, mock.GOOD_KEY, verify=False)
    hosts = list(client.site_manager("/ea/hosts"))
    assert sorted(h["id"] for h in hosts) == sorted(mock.CONSOLES)
