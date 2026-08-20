"""
Install / load smoke tests (network-free).

Prove the built add-on installs into a real Splunk cleanly: it is enabled, every
modular input is registered, their schemes introspect, and nothing in the add-on
failed to import at startup.
"""
from __future__ import annotations

import json

APP = "TA-unifi-cloud"
INPUTS = (
    "unifi_collect_hosts",
    "unifi_collect_sites",
    "unifi_collect_devices",
)


def test_app_installed_and_enabled(splunk):
    entries = splunk.entries(f"/services/apps/local/{APP}")
    assert entries, f"{APP} is not installed"
    content = entries[0]["content"]
    assert content.get("disabled") in (False, 0, "0"), f"{APP} is disabled: {content.get('disabled')}"


def test_all_modular_inputs_registered(splunk):
    names = {e["name"] for e in splunk.entries("/services/data/modular-inputs")}
    missing = [i for i in INPUTS if i not in names]
    assert not missing, f"modular inputs not registered: {missing} (have: {sorted(names)})"


def test_modinput_schemes_expose_expected_args(splunk):
    # If a script failed to import, Splunk cannot introspect its scheme, so the
    # input-specific argument would be absent. Shape-tolerant: check the JSON.
    # Every unifi collector keys off an `account` arg (the stored API-key handle),
    # so its presence proves the script imported and emitted a full scheme.
    for inp in INPUTS:
        data = splunk.get_json(f"/services/data/modular-inputs/{inp}")
        assert "account" in json.dumps(data), f"{inp} scheme missing expected arg 'account'"


def test_no_startup_import_or_init_errors(splunk):
    # Precise signatures: a failed modular-input init or an import error tied to
    # our scripts. Deliberately does NOT match runtime fetch errors (those are a
    # separate concern covered by the live test).
    input_clause = " OR ".join(f'"Unable to initialize modular input \\"{i}\\""' for i in INPUTS)
    script_clause = " OR ".join(INPUTS + ("unifi_input_helper", "import_declare_test"))
    spl = (
        "search index=_internal log_level=ERROR "
        f"({input_clause} "
        '   OR (("ImportError" OR "ModuleNotFoundError" OR "Traceback") '
        f"       AND ({script_clause}))) "
        # The live test's own probe input logs a teardown-window fetch error on
        # a shared instance (account deleted before its final scheduled run);
        # that is test noise, not an app startup failure.
        'NOT source=*_probe.log '
        "earliest=-1h"
    )
    hits = splunk.search(spl, earliest="-1h")
    assert not hits, f"startup import/init errors found: {[h.get('_raw', '')[:200] for h in hits[:3]]}"
