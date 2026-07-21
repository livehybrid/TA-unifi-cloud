"""
Modular-input scheme execution smoke (network-free).

Runs each packaged input script under Splunk's own Python via `--scheme`. This
proves the vendored libraries (import_declare_test path bootstrap, requests,
solnlib, splunklib) all import inside the container and every script emits a
valid scheme XML, independent of splunkd having scheduled them.
"""
from __future__ import annotations

import re

import pytest

from conftest import docker_exec

APP = "TA-unifi-cloud"
# script -> the scheme name (smi.Scheme(...)) each entrypoint emits. ucc-gen
# names each generated entrypoint and its scheme after the input service.
SCRIPTS = {
    "unifi_collect_hosts.py": "unifi_collect_hosts",
    "unifi_collect_sites.py": "unifi_collect_sites",
    "unifi_collect_devices.py": "unifi_collect_devices",
}


@pytest.mark.parametrize("script,scheme_name", SCRIPTS.items())
def test_script_emits_scheme(splunk, script, scheme_name):
    rc, out, err = docker_exec(
        "/opt/splunk/bin/splunk",
        "cmd",
        "python",
        f"/opt/splunk/etc/apps/{APP}/bin/{script}",
        "--scheme",
    )
    assert rc == 0, f"{script} --scheme exited {rc}\nSTDOUT:\n{out}\nSTDERR:\n{err}"
    assert "<scheme>" in out, f"{script} did not emit a scheme:\n{out}\n{err}"
    assert f"<title>{scheme_name}</title>" in out, f"{script} scheme missing title '{scheme_name}':\n{out}"
    # index/interval/host/source/sourcetype/disabled are supplied by Splunk
    # natively. Declaring any as a scheme argument makes splunkd reject the whole
    # kind at startup ("Endpoint argument '<x>' is an internal argument") so the
    # input never registers. Catch it here in <1s instead of via a much later
    # ModularInputs init failure.
    declared = set(re.findall(r'<arg name="([^"]+)"', out))
    reserved = {"index", "interval", "host", "source", "sourcetype", "disabled", "name"}
    clash = sorted(declared & (reserved - {"name"}))
    assert not clash, (
        f"{script} scheme declares reserved arg(s) {clash}; Splunk supplies these "
        f"natively so splunkd will refuse to register the input"
    )
