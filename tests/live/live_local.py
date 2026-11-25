"""Live check of the add-on's collector against a real local console, no Splunk.

Runs the same code the unifi_network input runs (package/bin/unifi_client.py)
with a Local account: UNIFI_LOCAL_URL, UNIFI_LOCAL_API_KEY, and
UNIFI_LOCAL_VERIFY (default 0: consoles ship a self-signed certificate).

The console's data is private. This prints STRUCTURE AND COUNTS ONLY: records
per resource, the console's status (state, unsupported resource names) and
the sorted top-level field names per resource. Errors print as type, HTTP
status and API error code. Nothing else, including tracebacks, is printed.

Checks:
  1. every resource is either collected or reported as not supported;
  2. sites, devices, clients and networks are non-empty;
  3. pagination: collecting with a page size of 5 gives exactly the same
     record ids as the default page size (200) for every stable resource
     (clients and statistics churn), and at least one of them needs more
     than one page of 5.
Exit status 0 when all hold, 1 otherwise.
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "package", "bin"))

import unifi_client as uc  # noqa: E402

SAFE_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")
SMALL_PAGE = 5
REQUIRED = ("sites", "devices", "clients", "networks")
# Change between two runs a few seconds apart, so not compared id for id.
VOLATILE = ("clients", "device_statistics")


def run(page_limit):
    url = os.environ["UNIFI_LOCAL_URL"]
    key = os.environ["UNIFI_LOCAL_API_KEY"]
    verify = uc.is_true(os.environ.get("UNIFI_LOCAL_VERIFY"), default=False)
    ids, fields, status = {}, {}, None
    for cid, client in uc.network_targets("local", key, url, verify, page_limit=page_limit):
        for sourcetype, rec in uc.collect_console(client, list(uc.RESOURCES), cid, "local"):
            if sourcetype == "status":
                status = rec
                continue
            res = [r for r, (_, st) in uc.RESOURCES.items() if st == sourcetype][0]
            rid = rec.get("unifi_device_id") if res == "device_statistics" else rec.get("id")
            ids.setdefault(res, []).append("%s/%s" % (rec.get("unifi_site_id"), rid))
            fields.setdefault(res, set()).update(k for k in rec if SAFE_FIELD.match(k))
    return ids, fields, status


def main():
    failures = []
    try:
        ids, fields, status = run(uc.PAGE_LIMIT)
        small_ids, _, small_status = run(SMALL_PAGE)
    except uc.UnifiError as exc:
        print(json.dumps({"error": type(exc).__name__, "http_status": exc.status, "code": exc.code}))
        return 1
    except Exception as exc:  # never print a traceback: it can carry payloads
        print(json.dumps({"error": type(exc).__name__}))
        return 1

    summary = {
        "state": status.get("state"),
        "sites": status.get("sites"),
        "records": {r: len(ids.get(r, [])) for r in uc.RESOURCES},
        "unsupported": sorted(status.get("unsupported") or []),
        "errors": sorted((status.get("errors") or {}).keys()),
        "fields": {r: sorted(f) for r, f in sorted(fields.items())},
        "pagination": {},
    }
    if status.get("state") != "ok":
        failures.append("console state is %s" % status.get("state"))
    for r in uc.RESOURCES:
        if r not in ids and r not in summary["unsupported"]:
            if summary["records"][r] == 0 and r not in REQUIRED:
                continue  # an empty but supported list is fine
            failures.append("%s neither collected nor reported unsupported" % r)
    for r in REQUIRED:
        if not ids.get(r):
            failures.append("no %s records" % r)
    multi_page = 0
    for r in uc.RESOURCES:
        a, b = sorted(ids.get(r, [])), sorted(small_ids.get(r, []))
        same = a == b
        summary["pagination"][r] = {"page_200": len(a), "page_%d" % SMALL_PAGE: len(b), "same_ids": same}
        if r in VOLATILE:
            continue
        if not same:
            failures.append("%s differs between page sizes" % r)
        if len(a) > SMALL_PAGE:
            multi_page += 1
    if small_status.get("state") != "ok":
        failures.append("small-page run state is %s" % small_status.get("state"))
    if not multi_page:
        failures.append("no resource needed more than one page of %d" % SMALL_PAGE)
    summary["failures"] = failures
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
