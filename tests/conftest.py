"""
Shared fixtures for the TA-unifi-cloud integration suite.

These run against a real Splunk started by ../docker/docker-compose.yml with the
built add-on bind-mounted in. Everything talks to the management API over stdlib
urllib (no `requests`), so the suite needs only pytest.

Environment (all have working defaults for the docker harness):
  SPLUNK_MGMT_URL   management API base   (default https://127.0.0.1:8089)
  SPLUNK_USER       admin user            (default admin)
  SPLUNK_PASSWORD   admin password        (default Changeme1!)
  SPLUNK_CONTAINER  container name for     (default ta_unifi_cloud_splunk)
                    `docker exec` smokes
"""
from __future__ import annotations

import base64
import json
import os
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

MGMT = os.environ.get("SPLUNK_MGMT_URL", "https://127.0.0.1:8089").rstrip("/")
USER = os.environ.get("SPLUNK_USER", "admin")
PW = os.environ.get("SPLUNK_PASSWORD", "Changeme1!")
CONTAINER = os.environ.get("SPLUNK_CONTAINER", "ta_unifi_cloud_splunk")
# `docker exec` defaults to the image's build-time USER, which is not the account
# splunkd runs modular inputs as. Running `splunk cmd python <script> --scheme`
# as a non-owner of $SPLUNK_HOME/var/log makes splunk.mining.dcutils fail at
# import (PermissionError opening python.log), so the scheme never emits. splunkd
# runs the real inputs as `splunk`, so exec as that user to mirror production and
# land on the log owner. Override via SPLUNK_RUN_USER if an image differs.
RUN_USER = os.environ.get("SPLUNK_RUN_USER", "splunk")
APP = "TA-unifi-cloud"

# Modular-input kinds this add-on ships. splunkd opens the management port (so
# /services/server/info answers 200 and the compose healthcheck passes) before
# it has finished introspecting a freshly-installed app's modular inputs, so
# /services/data/modular-inputs can be briefly empty at that point. The `splunk`
# fixture waits for these kinds to register so every test asserts against a
# fully-initialised splunkd rather than a cold-start snapshot.
EXPECTED_MODULAR_INPUTS = (
    "unifi_collect_hosts",
    "unifi_collect_sites",
    "unifi_collect_devices",
)

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_AUTH = "Basic " + base64.b64encode(f"{USER}:{PW}".encode()).decode()


class Splunk:
    """Minimal management-API client (urllib, JSON output_mode)."""

    def request(self, method, path, data=None, params=None):
        url = MGMT + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", _AUTH)
        try:
            with urllib.request.urlopen(req, context=_CTX, timeout=90) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def get_json(self, path, **params):
        params.setdefault("output_mode", "json")
        status, body = self.request("GET", path, params=params)
        assert status == 200, f"GET {path} -> {status}: {body[:400]}"
        return json.loads(body)

    def entries(self, path, **params):
        """Return the .entry list of a collection endpoint (unpaginated).

        Splunk collection endpoints default to count=30; on a shared dev
        instance with many apps the entries under test can fall off the first
        page and vanish silently. count=0 returns everything.
        """
        params.setdefault("count", 0)
        return self.get_json(path, **params).get("entry", [])

    def search(self, spl, earliest="-7d", latest="now", count=100):
        """Blocking oneshot search -> list of result dicts."""
        if not spl.lstrip().startswith("|") and not spl.lstrip().lower().startswith("search"):
            spl = "search " + spl
        status, body = self.request(
            "POST",
            "/services/search/jobs/oneshot",
            data={
                "search": spl,
                "output_mode": "json",
                "earliest_time": earliest,
                "latest_time": latest,
                "count": count,
            },
        )
        assert status == 200, f"oneshot search -> {status}: {body[:400]}"
        return json.loads(body).get("results", [])


def docker_exec(*cmd, timeout=180):
    """Run a command inside the Splunk container as the splunk runtime user.

    Returns (rc, stdout, stderr). The `-u` is required: without it `docker exec`
    uses the image's build-time USER, which cannot write $SPLUNK_HOME/var/log and
    so trips a PermissionError inside splunk.mining.dcutils before any script code
    runs. splunkd runs modular inputs as `splunk`, so this matches production.
    """
    full = ["docker", "exec", "-u", RUN_USER, CONTAINER, *cmd]
    p = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


@pytest.fixture(scope="session")
def splunk():
    """A ready Splunk client; blocks until the management API answers."""
    c = Splunk()
    deadline = time.time() + 300
    last = "no attempt"
    ready = False
    while time.time() < deadline:
        try:
            status, body = c.request("GET", "/services/server/info", params={"output_mode": "json"})
            if status == 200:
                ready = True
                break
            last = f"{status}: {body[:200]}"
        except Exception as exc:  # connection refused while Splunk boots
            last = repr(exc)
        time.sleep(5)
    if not ready:
        pytest.fail(f"Splunk management API not ready at {MGMT} within 300s (last: {last})")

    # The management port answers before splunkd finishes introspecting a
    # freshly-installed app's modular inputs. Wait for this add-on's kinds to
    # register so the registration / scheme / live-create tests do not race a
    # still-initialising splunkd. On timeout, fall through and let the specific
    # assertion report exactly which kind is missing.
    mi_deadline = time.time() + 180
    while time.time() < mi_deadline:
        try:
            names = {e["name"] for e in c.entries("/services/data/modular-inputs")}
            if all(k in names for k in EXPECTED_MODULAR_INPUTS):
                break
        except Exception:  # transient during introspection settle
            pass
        time.sleep(3)
    return c
