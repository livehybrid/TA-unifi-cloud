# TA-unifi-cloud

**Unifi Cloud Add-on for Splunk**: a Splunk add-on (built with the
[UCC framework](https://github.com/splunk/addonfactory-ucc-generator)) that
collects inventory from the Ubiquiti UniFi cloud API (Site Manager) through
three modular inputs. A UniFi Cloud API key is required for all inputs.

| Input | Collects |
|-------|----------|
| **Unifi - Collect hosts** | UniFi consoles / hosts on the account |
| **Unifi - Collect Sites** | Sites |
| **Unifi - Collect Devices** | Devices |

## Compatibility

| Attribute | Value |
|-----------|-------|
| **Add-on version** | 1.1.0R5386590 |
| **Tested against** | Splunk Enterprise 10.0 (Python 3.9) and 10.6 (Python 3.13), real Splunk in Docker, on every CI run |
| **Python runtime** | 3.13 on Splunk 10.2 and later, 3.9 on Splunk 10.1 and earlier |
| **Expected compatible** | Splunk Enterprise and Cloud 9.3+ and 10.x |
| **Deployment roles** | Standalone, Distributed, Search Head Clustering |
| **AppInspect** | Passes the `cloud`, `future` and `private_victoria` tag sets |

Splunk 10.2 and later ship Python 3.9 and 3.13 side by side; this add-on sets
`python.required = 3.13`, so it runs on 3.13 there. Splunk 10.1 and earlier
ignore `python.required` and run it on 3.9 (`python.version = python3`), so the
code and vendored libraries stay 3.9-compatible (`requests < 2.33`,
`splunk-sdk < 3`). CI runs the integration tests on Splunk 10.0 (Python 3.9)
and 10.6 (Python 3.13) to hold both.

## Testing

The add-on ships a real-Splunk integration harness under `docker/`
(`make build up test down`): it installs the packaged add-on into Splunk 10 in
Docker and asserts that the app installs and all three modular inputs register
and expose their expected arguments on both runtimes. This harness
caught a Python 3.9 incompatibility in a vendored `requests` release (PEP 604
`X | Y` type unions, which 3.9 rejects at import), now pinned out. The suite
runs on every push via GitHub Actions.

> **Coverage note:** all three inputs require a UniFi Cloud API key, which CI
> does not hold, so they are scheme- and registration-tested rather than run
> against the live UniFi API. See
> [deploy-splunk-app-action#22](https://github.com/livehybrid/deploy-splunk-app-action/issues/22).
