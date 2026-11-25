"""UniFi API client shared by every input. Plain `requests`, no Splunk imports,
so the collector can be exercised outside Splunk (unit tests, live local check).

Two connection modes, one code path for the UniFi Network Integration API:

* cloud: Site Manager key against https://api.ui.com. Consoles come from
  /v1/hosts; each console's Network API is reached through the connector at
  /v1/connector/consoles/<id>/proxy/network/integration.
* local: Network API key against the console itself,
  https://<console>/proxy/network/integration.

Python 3.9 compatible (Splunk 10.1 and earlier run add-ons on 3.9).
"""
from __future__ import annotations

import time

import requests

CLOUD_DEFAULT_URL = "https://api.ui.com"
INTEGRATION_PATH = "/proxy/network/integration"
# The real API clamps limit to 200; asking for more is harmless.
PAGE_LIMIT = 200
MAX_PAGES = 10000
RETRY_STATUSES = (429, 502, 503, 504)
MAX_RETRIES = 3
MAX_RETRY_WAIT = 30.0

# 400 codes that mean "this console has no such feature", not "request broken".
NOT_CONFIGURED_CODES = ("api.firewall.zone-based-firewall-not-configured",)
OFFLINE_CODES = ("device_offline",)


class UnifiError(Exception):
    """An API failure. str() holds only status and API error code: never a URL,
    id or payload, so it is safe to log anywhere."""

    def __init__(self, status=None, code=None, kind="error"):
        self.status = status
        self.code = code
        self.kind = kind
        super().__init__("%s (HTTP %s, code %s)" % (kind, status if status is not None else "-", code or "-"))


class UnifiAuthError(UnifiError):
    def __init__(self, status=None, code=None):
        super().__init__(status, code, "auth_error")


class UnifiNotSupported(UnifiError):
    def __init__(self, status=None, code=None):
        super().__init__(status, code, "not_supported")


class UnifiConsoleUnavailable(UnifiError):
    def __init__(self, status=None, code=None):
        super().__init__(status, code, "console_unavailable")


def is_true(value, default=True):
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "t", "on")


def normalise_url(url):
    url = (url or "").strip().rstrip("/")
    if url.endswith(INTEGRATION_PATH):
        url = url[: -len(INTEGRATION_PATH)]
    return url


def local_integration_base(console_url):
    """Network Integration API base for a local console."""
    url = normalise_url(console_url)
    if not url:
        raise ValueError("local connection needs the console URL, for example https://192.168.1.1")
    if not url.lower().startswith("https://"):
        raise ValueError("console URL must start with https://")
    return url + INTEGRATION_PATH


def cloud_integration_base(cloud_url, console_id):
    """Network Integration API base for a console reached through api.ui.com."""
    return "%s/v1/connector/consoles/%s%s" % (
        normalise_url(cloud_url) or CLOUD_DEFAULT_URL, console_id, INTEGRATION_PATH)


class UnifiClient(object):
    """GET-only JSON client for one base URL."""

    def __init__(self, base_url, api_key, verify=True, timeout=30, page_limit=PAGE_LIMIT,
                 logger=None, session=None):
        self.base_url = base_url.rstrip("/")
        self.verify = verify
        self.timeout = timeout
        self.page_limit = page_limit
        self.logger = logger
        self.session = session or requests.Session()
        self.session.headers.update({"X-API-KEY": api_key, "Accept": "application/json"})
        if not verify:
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:  # pragma: no cover
                pass

    def get(self, path, params=None):
        url = self.base_url + path
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout, verify=self.verify)
            except requests.exceptions.SSLError:
                raise UnifiError(None, "tls_verify_failed", "connection_error")
            except requests.exceptions.Timeout:
                raise UnifiError(None, "timeout", "connection_error")
            except requests.exceptions.ConnectionError:
                raise UnifiError(None, "connection_failed", "connection_error")
            if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
                time.sleep(self._retry_after(resp, attempt))
                continue
            return self._decode(resp)
        raise UnifiError(None, "retries_exhausted")  # pragma: no cover

    @staticmethod
    def _retry_after(resp, attempt):
        try:
            wait = float(resp.headers.get("Retry-After", ""))
        except ValueError:
            wait = 2.0 ** attempt
        return max(0.0, min(wait, MAX_RETRY_WAIT))

    @staticmethod
    def _decode(resp):
        status = resp.status_code
        try:
            body = resp.json()
        except ValueError:
            body = None
        code = body.get("code") if isinstance(body, dict) else None
        if status == 200:
            if body is None:
                raise UnifiError(status, "invalid_json")
            return body
        if status in (401, 403):
            raise UnifiAuthError(status, code)
        if status == 404 and code in OFFLINE_CODES:
            raise UnifiConsoleUnavailable(status, code)
        if status == 404:
            raise UnifiNotSupported(status, code)
        if status == 400 and code in NOT_CONFIGURED_CODES:
            raise UnifiNotSupported(status, code)
        if status in (502, 503, 504):
            raise UnifiConsoleUnavailable(status, code)
        raise UnifiError(status, code)

    def paginate(self, path):
        """Every record of an offset/limit collection, following totalCount."""
        offset = 0
        for _ in range(MAX_PAGES):
            body = self.get(path, params={"offset": offset, "limit": self.page_limit})
            data = body.get("data") or []
            for item in data:
                yield item
            got = len(data)
            total = body.get("totalCount")
            offset += got
            if got == 0:
                return
            if total is not None and offset >= int(total):
                return
            if total is None and got < int(body.get("limit") or self.page_limit):
                return

    def site_manager(self, path, page_size=None):
        """Every record of a Site Manager list (pageSize/nextToken pagination)."""
        token = None
        for _ in range(MAX_PAGES):
            params = {}
            if page_size:
                params["pageSize"] = page_size
            if token:
                params["nextToken"] = token
            body = self.get(path, params=params or None)
            for item in body.get("data") or []:
                yield item
            token = body.get("nextToken")
            if not token:
                return


# ---- Network Integration API collection --------------------------------------

# resource name (input setting) -> (API path under /v1/sites/<id>/, sourcetype)
RESOURCES = {
    "sites": (None, "unifi:network:site"),
    "devices": ("devices", "unifi:network:device"),
    "device_statistics": ("devices", "unifi:network:device:statistics"),
    "clients": ("clients", "unifi:network:client"),
    "networks": ("networks", "unifi:network:network"),
    "wifi_broadcasts": ("wifi/broadcasts", "unifi:network:wifi"),
    "firewall_zones": ("firewall/zones", "unifi:network:firewall:zone"),
    "firewall_policies": ("firewall/policies", "unifi:network:firewall:policy"),
}
STATUS_SOURCETYPE = "unifi:network:status"
DEFAULT_RESOURCES = tuple(RESOURCES)


def parse_resources(value):
    if not value:
        return list(DEFAULT_RESOURCES)
    wanted = [v.strip() for v in str(value).replace("~", ",").split(",") if v.strip()]
    unknown = [v for v in wanted if v not in RESOURCES]
    if unknown:
        raise ValueError("unknown resource(s): %s" % ", ".join(unknown))
    return wanted


def network_targets(connection_type, api_key, url=None, verify=True, timeout=30,
                    page_limit=PAGE_LIMIT, logger=None):
    """[(console_id or None, UnifiClient)] for every console to collect from.

    Raises UnifiError if the console list itself cannot be read (cloud) or the
    local settings are invalid."""
    if (connection_type or "cloud") == "local":
        base = local_integration_base(url)
        return [(None, UnifiClient(base, api_key, verify, timeout, page_limit, logger))]
    cloud_url = normalise_url(url) or CLOUD_DEFAULT_URL
    hosts = UnifiClient(cloud_url, api_key, verify, timeout, page_limit, logger)
    out = []
    for host in hosts.site_manager("/v1/hosts"):
        cid = host.get("id")
        if not cid:
            continue
        out.append((cid, UnifiClient(cloud_integration_base(cloud_url, cid), api_key, verify,
                                     timeout, page_limit, logger)))
    return out


def collect_console(client, resources, console_id=None, connection_type="cloud"):
    """Yield (sourcetype, record) for one console, then ("status", status).

    One endpoint answering 404 (or 400 "not configured") is recorded as not
    supported and collection carries on. An unreachable console yields only
    its status record."""
    status = {"connection_type": connection_type, "state": "ok", "resources": {},
              "unsupported": [], "errors": {}, "sites": 0}
    if console_id:
        status["console_id"] = console_id
    try:
        sites = list(client.paginate("/v1/sites"))
    except UnifiError as exc:
        status["state"] = exc.kind
        status["http_status"] = exc.status
        status["error_code"] = exc.code
        yield "status", status
        return
    status["sites"] = len(sites)

    def tag(rec, site):
        rec = dict(rec)
        rec["unifi_connection"] = connection_type
        rec["unifi_site_id"] = site.get("id")
        rec["unifi_site_name"] = site.get("name")
        if console_id:
            rec["unifi_console_id"] = console_id
        return rec

    device_cache = {}

    def site_records(resource, site):
        path, sourcetype = RESOURCES[resource]
        if resource == "sites":
            yield sourcetype, tag(site, site)
            return
        sid = site.get("id")
        base = "/v1/sites/%s/" % sid
        if resource in ("devices", "device_statistics"):
            if sid not in device_cache:
                device_cache[sid] = list(client.paginate(base + "devices"))
            items = device_cache[sid]
        else:
            items = client.paginate(base + path)
        if resource != "device_statistics":
            for item in items:
                yield sourcetype, tag(item, site)
            return
        for dev in items:
            did = dev.get("id")
            try:
                st = client.get(base + "devices/%s/statistics/latest" % did)
            except UnifiNotSupported:
                continue  # device without statistics (not adopted, offline)
            rec = tag(st, site)
            rec["unifi_device_id"] = did
            rec["unifi_device_name"] = dev.get("name")
            rec["unifi_device_model"] = dev.get("model")
            yield sourcetype, rec

    for resource in resources:
        n = 0
        unsupported = 0
        for site in sites:
            try:
                for item in site_records(resource, site):
                    yield item
                    n += 1
            except UnifiNotSupported:
                unsupported += 1
            except UnifiAuthError as exc:
                status["errors"][resource] = str(exc)
                status["state"] = "auth_error"
            except UnifiError as exc:
                status["errors"][resource] = str(exc)
                if status["state"] == "ok":
                    status["state"] = "partial"
        if sites and unsupported == len(sites):
            status["unsupported"].append(resource)
        status["resources"][resource] = n
    yield "status", status
