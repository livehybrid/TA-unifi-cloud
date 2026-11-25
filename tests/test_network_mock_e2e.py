"""End to end on real Splunk against the synthetic mock (docker/docker-compose.yml).

Accounts are created through the add-on's own REST handler, inputs through
data/inputs, and every assertion is a search over what Splunk indexed. The
expected numbers are counted from the mock's fixture tables (tests/mock), not
from anything the add-on reports.

Every input uses interval 3600 so each runs exactly once during the suite and
event counts are exact.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock"))
import unifi_mock as mock  # noqa: E402

APP = "TA-unifi-cloud"
ACCOUNT_PATH = f"/servicesNS/nobody/{APP}/ta_unifi_cloud_account"
MOCK_URL = os.environ.get("UNIFI_MOCK_URL", "https://mock:8443")
INDEX = "main"
WAIT = 300

SOURCETYPES = {
    "sites": "unifi:network:site",
    "devices": "unifi:network:device",
    "device_statistics": "unifi:network:device:statistics",
    "clients": "unifi:network:client",
    "networks": "unifi:network:network",
    "wifi/broadcasts": "unifi:network:wifi",
    "firewall/zones": "unifi:network:firewall:zone",
    "firewall/policies": "unifi:network:firewall:policy",
}

ACCOUNTS = {
    "mock_cloud": {"connection_type": "cloud", "api_key": mock.GOOD_KEY, "url": MOCK_URL, "verify_ssl": "0"},
    "mock_local": {"connection_type": "local", "api_key": mock.GOOD_KEY, "url": MOCK_URL, "verify_ssl": "0"},
    "mock_local_badkey": {"connection_type": "local", "api_key": "not-the-key", "url": MOCK_URL, "verify_ssl": "0"},
    "mock_local_verify": {"connection_type": "local", "api_key": mock.GOOD_KEY, "url": MOCK_URL, "verify_ssl": "1"},
}

# stanza -> (kind, account)
INPUTS = {
    "net_local": ("unifi_network", "mock_local"),
    "net_cloud": ("unifi_network", "mock_cloud"),
    "net_badkey": ("unifi_network", "mock_local_badkey"),
    "net_verify": ("unifi_network", "mock_local_verify"),
    "sm_hosts": ("unifi_collect_hosts", "mock_cloud"),
    "sm_sites": ("unifi_collect_sites", "mock_cloud"),
    "sm_devices": ("unifi_collect_devices", "mock_cloud"),
    "sm_devices_local": ("unifi_collect_devices", "mock_local"),
}


def _input_path(kind):
    return f"/servicesNS/nobody/{APP}/data/inputs/{kind}"


@pytest.fixture(scope="module")
def mock_inputs(splunk):
    for name, fields in ACCOUNTS.items():
        status, body = splunk.request("POST", ACCOUNT_PATH, data={"name": name, **fields})
        assert status in (200, 201, 409), f"account {name}: {status} {body[:300]}"
    for stanza, (kind, account) in INPUTS.items():
        data = {"name": stanza, "account": account, "index": INDEX, "interval": "3600"}
        status, body = splunk.request("POST", _input_path(kind), data=data)
        assert status in (200, 201, 409), f"input {stanza}: {status} {body[:300]}"
    yield
    for stanza, (kind, _) in INPUTS.items():
        splunk.request("POST", f"{_input_path(kind)}/{stanza}/disable")
    for stanza, (kind, _) in INPUTS.items():
        splunk.request("DELETE", f"{_input_path(kind)}/{stanza}")
    for name in ACCOUNTS:
        splunk.request("DELETE", f"{ACCOUNT_PATH}/{name}")


def counts(splunk, stanza):
    rows = splunk.search(
        f'search index={INDEX} source="{_kind(stanza)}://{stanza}" | stats count by sourcetype',
        earliest="-1h")
    return {r["sourcetype"]: int(r["count"]) for r in rows}


def _kind(stanza):
    return INPUTS[stanza][0]


def statuses(splunk, stanza):
    rows = splunk.search(
        f'search index={INDEX} source="unifi_network://{stanza}" sourcetype=unifi:network:status '
        "| spath | table console_id state error_code http_status unsupported{}",
        earliest="-1h")
    return [_dedupe(r) for r in rows]


def _dedupe(row):
    """KV_MODE=json and `| spath` both extract each field, so values arrive as
    duplicated multivalues. Collapse them: a single distinct value becomes a
    scalar, several stay a list."""
    out = {}
    for k, v in row.items():
        if isinstance(v, list):
            uniq = list(dict.fromkeys(v))
            v = uniq[0] if len(uniq) == 1 and not k.endswith("{}") else uniq
        out[k] = v
    return out


def wait_for(fn, done, timeout=WAIT, settle=2):
    """Poll fn() until done(value) holds for `settle` consecutive reads."""
    deadline = time.time() + timeout
    value, good = None, 0
    while time.time() < deadline:
        value = fn()
        good = good + 1 if done(value) else 0
        if good >= settle:
            return value
        time.sleep(8)
    return value


def expected_by_sourcetype(console_ids):
    exp = mock.expected_counts(console_ids)
    return {SOURCETYPES[k]: v for k, v in exp.items() if v}


def data_only(c):
    return {k: v for k, v in c.items() if k != "unifi:network:status"}


def test_local_account_indexes_every_record(splunk, mock_inputs):
    want = expected_by_sourcetype([mock.LOCAL_CONSOLE])
    got = wait_for(lambda: counts(splunk, "net_local"), lambda c: data_only(c) == want)
    assert data_only(got) == want
    assert got.get("unifi:network:status") == 1


def test_cloud_account_indexes_every_online_console(splunk, mock_inputs):
    want = expected_by_sourcetype(list(mock.CONSOLES))
    got = wait_for(lambda: counts(splunk, "net_cloud"), lambda c: data_only(c) == want)
    assert data_only(got) == want
    assert got.get("unifi:network:status") == len(mock.CONSOLES)


def test_cloud_dead_console_and_unsupported_endpoints(splunk, mock_inputs):
    rows = wait_for(lambda: statuses(splunk, "net_cloud"), lambda r: len(r) >= len(mock.CONSOLES))
    by = {r.get("console_id"): r for r in rows}
    assert set(by) == set(mock.CONSOLES)
    assert by["mock-console-c"]["state"] == "console_unavailable"
    assert by["mock-console-c"]["error_code"] == "device_offline"
    assert by["mock-console-a"]["state"] == "ok"
    assert by["mock-console-b"]["state"] == "ok"
    unsupported = by["mock-console-b"].get("unsupported{}") or []
    if isinstance(unsupported, str):
        unsupported = [unsupported]
    assert sorted(unsupported) == ["firewall_policies", "firewall_zones", "wifi_broadcasts"]


def test_bad_key_indexes_only_an_auth_error_status(splunk, mock_inputs):
    rows = wait_for(lambda: statuses(splunk, "net_badkey"), lambda r: len(r) >= 1)
    assert [r["state"] for r in rows] == ["auth_error"]
    assert rows[0]["http_status"] == "401"
    assert data_only(counts(splunk, "net_badkey")) == {}


def test_tls_verify_on_rejects_self_signed_mock(splunk, mock_inputs):
    rows = wait_for(lambda: statuses(splunk, "net_verify"), lambda r: len(r) >= 1)
    assert [(r["state"], r["error_code"]) for r in rows] == [("connection_error", "tls_verify_failed")]
    assert data_only(counts(splunk, "net_verify")) == {}


def test_site_manager_inputs_through_cloud_account(splunk, mock_inputs):
    n_devices = sum(s["devices"] for c in mock.CONSOLES.values() for s in c["sites"].values())
    n_sites = sum(len(c["sites"]) for c in mock.CONSOLES.values())
    want = {"sm_hosts": {"unifi:cloud:hosts": len(mock.CONSOLES)},
            "sm_sites": {"unifi:cloud:sites": n_sites},
            # Devices from every console, not only the first entry.
            "sm_devices": {"unifi:cloud:devices": n_devices}}
    for stanza, w in want.items():
        got = wait_for(lambda: counts(splunk, stanza), lambda c: c == w)
        assert got == w, stanza


def test_site_manager_input_refuses_local_account(splunk, mock_inputs):
    hits = wait_for(
        lambda: splunk.search(
            "search index=_internal source=*ta-unifi-cloud_sm_devices_local* "
            '"need a Cloud account" | stats count', earliest="-1h"),
        lambda r: r and int(r[0]["count"]) >= 1)
    assert hits and int(hits[0]["count"]) >= 1
    assert counts(splunk, "sm_devices_local") == {}


def test_api_key_never_logged(splunk, mock_inputs):
    # Wait until the inputs have run, then look for the key anywhere in _internal.
    wait_for(lambda: counts(splunk, "net_local"), lambda c: c.get("unifi:network:status") == 1)
    # The search string itself lands in (remote_)searches.log: exclude those.
    rows = splunk.search(f'search index=_internal "{mock.GOOD_KEY}" NOT source=*searches.log | stats count', earliest="-1h")
    assert rows and int(rows[0]["count"]) == 0


def test_network_input_runs_on_expected_python(splunk, mock_inputs):
    info = splunk.get_json("/services/server/info")["entry"][0]["content"]
    major, minor = (int(x) for x in info["version"].split(".")[:2])
    want = "python3.13" if (major, minor) >= (10, 2) else "python3.9"
    rows = wait_for(
        lambda: splunk.search(
            'search index=_internal sourcetype=splunkd component=ExecProcessor '
            '"New scheduled exec process" "unifi_network.py" | head 1 | table _raw', earliest="-1h"),
        lambda r: bool(r))
    assert rows, "no ExecProcessor line for unifi_network.py"
    raw = rows[0]["_raw"]
    assert want in raw or (want == "python3.9" and "/bin/python3 " in raw), raw[:300]
