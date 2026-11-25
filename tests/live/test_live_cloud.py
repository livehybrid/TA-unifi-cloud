"""Live end to end on real Splunk against the real api.ui.com (Cloud account).

Runs only when UNIFI_API_KEY is set (CI: push to main, schedule, manual
dispatch; never on pull requests). The account's data is private, so this
file asserts and reports STRUCTURE AND COUNTS ONLY: event counts per
sourcetype, status counts per state, and top-level field names. No assertion
message carries an event value, and the summary written to LIVE_SUMMARY holds
nothing else. CI runs it with --tb=no as a second line of defence.
"""
from __future__ import annotations

import json
import os
import re
import time

import pytest

API_KEY = os.environ.get("UNIFI_API_KEY")
SUMMARY = os.environ.get("LIVE_SUMMARY")

pytestmark = pytest.mark.skipif(not API_KEY, reason="no UNIFI_API_KEY: live cloud test skipped")

APP = "TA-unifi-cloud"
ACCOUNT = "live_cloud"
ACCOUNT_PATH = f"/servicesNS/nobody/{APP}/ta_unifi_cloud_account"
INDEX = "main"
WAIT = 600
INPUTS = {
    "live_network": "unifi_network",
    "live_hosts": "unifi_collect_hosts",
    "live_sites": "unifi_collect_sites",
    "live_devices": "unifi_collect_devices",
}
SAFE_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")
SUMMARY_DATA = {}


def _path(kind):
    return f"/servicesNS/nobody/{APP}/data/inputs/{kind}"


@pytest.fixture(scope="module")
def live(splunk):
    status, _ = splunk.request("POST", ACCOUNT_PATH, data={
        "name": ACCOUNT, "connection_type": "cloud", "api_key": API_KEY, "verify_ssl": "1"})
    assert status in (200, 201, 409), f"account create -> HTTP {status}"
    for stanza, kind in INPUTS.items():
        status, _ = splunk.request("POST", _path(kind), data={
            "name": stanza, "account": ACCOUNT, "index": INDEX, "interval": "3600"})
        assert status in (200, 201, 409), f"input {stanza} create -> HTTP {status}"
    yield
    for stanza, kind in INPUTS.items():
        splunk.request("POST", f"{_path(kind)}/{stanza}/disable")
        splunk.request("DELETE", f"{_path(kind)}/{stanza}")
    splunk.request("DELETE", f"{ACCOUNT_PATH}/{ACCOUNT}")
    if SUMMARY:
        with open(SUMMARY, "w") as f:
            json.dump(SUMMARY_DATA, f, indent=2, sort_keys=True)


def counts(splunk, stanza):
    rows = splunk.search(
        f'search index={INDEX} source="{INPUTS[stanza]}://{stanza}" | stats count by sourcetype',
        earliest="-2h")
    return {r["sourcetype"]: int(r["count"]) for r in rows}


def field_names(splunk, stanza, sourcetype):
    rows = splunk.search(
        f'search index={INDEX} source="{INPUTS[stanza]}://{stanza}" sourcetype="{sourcetype}" '
        "| head 200 | spath | fieldsummary | fields field", earliest="-2h", count=0)
    names = {r["field"] for r in rows}
    return sorted(n for n in names if SAFE_FIELD.match(n) and not n.startswith("date_"))


def statuses(splunk):
    return [_dedupe(r) for r in splunk.search(
        f'search index={INDEX} source="unifi_network://live_network" sourcetype=unifi:network:status '
        "| spath | table state sites unsupported{}", earliest="-2h", count=0)]


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


def wait_stable(fn, ready, timeout=WAIT):
    deadline = time.time() + timeout
    last, same = None, 0
    while time.time() < deadline:
        value = fn()
        if ready(value):
            same = same + 1 if value == last else 0
            if same >= 2:
                return value
        last = value
        time.sleep(15)
    return last


def test_live_cloud_end_to_end(splunk, live):
    hosts = wait_stable(lambda: counts(splunk, "live_hosts"), lambda c: c.get("unifi:cloud:hosts", 0) > 0)
    n_consoles = hosts.get("unifi:cloud:hosts", 0)
    assert n_consoles > 0, "no unifi:cloud:hosts events"

    rows = wait_stable(lambda: statuses(splunk), lambda r: len(r) >= n_consoles)
    net = counts(splunk, "live_network")
    states = {}
    unsupported = {}
    for r in rows:
        states[r.get("state")] = states.get(r.get("state"), 0) + 1
        u = r.get("unsupported{}") or []
        for name in [u] if isinstance(u, str) else u:
            unsupported[name] = unsupported.get(name, 0) + 1

    SUMMARY_DATA.update({
        "consoles": n_consoles,
        "status_by_state": states,
        "unsupported_by_resource": unsupported,
        "events": {s: counts(splunk, s) for s in INPUTS},
        "fields": {st: field_names(splunk, "live_network", st) for st in sorted(net)},
    })

    assert len(rows) == n_consoles, f"status events {len(rows)} != consoles {n_consoles}"
    assert states.get("ok", 0) >= 1, f"no console answered: states {states}"
    ok_sites = [int(r.get("sites") or 0) for r in rows if r.get("state") == "ok"]
    assert all(n >= 1 for n in ok_sites), "an ok console reported no sites"
    for st in ("unifi:network:site", "unifi:network:device", "unifi:network:client",
               "unifi:network:network", "unifi:network:device:statistics"):
        assert net.get(st, 0) >= 1, f"no {st} events"
    assert counts(splunk, "live_sites").get("unifi:cloud:sites", 0) >= 1
    assert counts(splunk, "live_devices").get("unifi:cloud:devices", 0) >= 1


def test_live_key_never_logged(splunk, live):
    # The search string itself lands in (remote_)searches.log: exclude those.
    rows = splunk.search(f'search index=_internal "{API_KEY}" NOT source=*searches.log | stats count', earliest="-2h")
    n = int(rows[0]["count"]) if rows else 0
    SUMMARY_DATA["key_in_internal"] = n
    assert n == 0, "API key found in _internal"
