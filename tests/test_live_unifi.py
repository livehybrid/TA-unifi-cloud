"""
Live end-to-end test for the unifi_collect_sites modular input.

This is the real ingestion proof. Unlike the keyless-collector pilots (e.g.
TA-stocks' Vanguard input), *every* UniFi collector is keyed: it calls
https://api.ui.com/ea/<type> with an `X-API-KEY` header taken from a stored
account. So this test cannot run without a real UniFi Site Manager API key and
is skipped unless `UNIFI_API_KEY` is present in the environment.

That skip IS the estate-wide keyed-collector coverage gap tracked in
deploy-splunk-app-action#22 (tracker row E3a): register `UNIFI_API_KEY` as an
Actions secret, wire it into the integration-test job, and this test un-skips to
give TA-unifi-cloud true end-to-end ingestion coverage. Until then, the add-on's
install, registration and scheme execution are proven by the network-free smokes.

When a key IS present, honest failure semantics (mirrors the keyless pattern):
  * events indexed with fields   -> PASS  (the whole pipeline works)
  * no events, but splunkd logged the input's own upstream fetch error
                                 -> SKIP  (api.ui.com unreachable/blocked or the
                                           key was rejected; not our packaging)
  * no events and no error logged -> FAIL  (the input never ran / is broken)
"""
from __future__ import annotations

import os
import time

import pytest

API_KEY = os.environ.get("UNIFI_API_KEY")

pytestmark = pytest.mark.skipif(
    not API_KEY,
    reason="no UNIFI_API_KEY in env — keyed live ingestion is deferred (deploy-splunk-app-action#22 / E3a)",
)

APP = "TA-unifi-cloud"
ACCOUNT = "probe_account"
STANZA = "unifi_sites_probe"
INDEX = "main"
INPUT_KIND = "unifi_collect_sites"
SOURCETYPE = "unifi:cloud:sites"

# UCC exposes accounts at /servicesNS/nobody/<app>/<restRoot>_account; the
# handler encrypts api_key under the __REST_CREDENTIAL__ realm the input reads.
ACCOUNT_PATH = f"/servicesNS/nobody/{APP}/ta_unifi_cloud_account"
# Create the input in the add-on namespace so it owns the stanza and resolves the
# account from its own conf; fall back to `search` if the app dir is read-only.
CREATE_PATHS = (
    f"/servicesNS/nobody/{APP}/data/inputs/{INPUT_KIND}",
    f"/servicesNS/nobody/search/data/inputs/{INPUT_KIND}",
)

POLL_SECONDS = 240
POLL_INTERVAL = 10


@pytest.fixture(scope="module")
def unifi_input(splunk):
    """Create an account + short-interval sites input; remove both on teardown."""
    # 1) Store the API key as an account (idempotent: 409 == already there).
    splunk.request("POST", ACCOUNT_PATH, data={"name": ACCOUNT, "api_key": API_KEY})

    # 2) Create the input pointing at that account.
    used_path = None
    status = body = None
    for path in CREATE_PATHS:
        status, body = splunk.request(
            "POST",
            path,
            data={
                "name": STANZA,
                "account": ACCOUNT,
                "index": INDEX,
                "interval": "60",
            },
        )
        if status in (200, 201) or status == 409:  # created, or already exists
            used_path = path
            break
    assert used_path, f"could not create {INPUT_KIND} input (last {status}: {str(body)[:400]})"
    splunk.request("POST", f"{used_path}/{STANZA}/enable")

    yield used_path

    splunk.request("DELETE", f"{used_path}/{STANZA}", params={"output_mode": "json"})
    splunk.request("DELETE", f"{ACCOUNT_PATH}/{ACCOUNT}", params={"output_mode": "json"})


def _upstream_error_logged(splunk):
    # solnlib names the per-input log after the STANZA, not the kind:
    # $SPLUNK_HOME/var/log/splunk/ta-unifi-cloud_<stanza>.log, which splunkd
    # monitors into _internal. The collector catches upstream failures and logs
    # them, so an error here means "the input ran but api.ui.com was unreachable
    # or rejected the key", not "our packaging is broken".
    spl = (
        f"search index=_internal source=*ta-unifi-cloud_{STANZA}* "
        '("Exception raised while ingesting" OR "ConnectionError" OR "Max retries" '
        'OR "Timeout" OR "Temporary failure in name resolution" '
        'OR "401" OR "403" OR "ERROR") '
        "earliest=-15m"
    )
    return bool(splunk.search(spl, earliest="-15m"))


def test_unifi_sites_events_indexed(splunk, unifi_input):
    deadline = time.time() + POLL_SECONDS
    results = []
    while time.time() < deadline:
        # `| spath` parses each event's JSON `_raw` directly, so field assertions
        # are independent of search-time auto-kv being in scope for this oneshot.
        results = splunk.search(
            f"search index={INDEX} sourcetype={SOURCETYPE} | head 5 | spath",
            earliest="-7d",
        )
        if results:
            break
        time.sleep(POLL_INTERVAL)

    if not results:
        if _upstream_error_logged(splunk):
            pytest.skip("unifi_collect_sites ran but api.ui.com was unreachable/rejected the key")
        pytest.fail(
            f"no {SOURCETYPE} events indexed within {POLL_SECONDS}s and no upstream "
            "error logged — the modular input did not run"
        )

    # Prove Splunk indexed the JSON site payload the collector emits. Site records
    # from /ea/sites carry an id/name; assert at least one recognisable field.
    row = results[0]
    assert any(k in row for k in ("id", "name", "siteId", "desc", "meta")), (
        f"indexed event missing expected unifi site fields: {sorted(row)}"
    )
