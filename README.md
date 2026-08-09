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
| **Tested against** | Splunk Enterprise 10.0, Python 3.9 (real Splunk in Docker, on every CI run) |
| **Python runtime** | 3.9, Splunk's long-term-support runtime |
| **Expected compatible** | Splunk Enterprise and Cloud 9.3+ and 10.x (any release on the Python 3.9 runtime) |
| **Deployment roles** | Standalone, Distributed, Search Head Clustering |
| **AppInspect** | Passes the `cloud`, `future` and `private_victoria` tag sets |

Splunk 9.3 through 10.1 default to Python 3.9, and 3.9 stays the LTS runtime on
10.2 and later, so an add-on that is clean on 3.9 runs unchanged across that
whole range. This add-on is validated on 3.9 and pins its vendored libraries to
versions that stay 3.9-clean. It is not yet validated on the opt-in Python 3.13
runtime introduced in Splunk 10.2.

## Testing

The add-on ships a real-Splunk integration harness under `docker/`
(`make build up test down`): it installs the packaged add-on into Splunk 10 in
Docker and asserts that the app installs and all three modular inputs register
and expose their expected arguments on the Python 3.9 runtime. This harness
caught a Python 3.9 incompatibility in a vendored `requests` release (PEP 604
`X | Y` type unions, which 3.9 rejects at import), now pinned out. The suite
runs on every push via GitHub Actions.

> **Coverage note:** all three inputs require a UniFi Cloud API key, which CI
> does not hold, so they are scheme- and registration-tested rather than run
> against the live UniFi API. See
> [deploy-splunk-app-action#22](https://github.com/livehybrid/deploy-splunk-app-action/issues/22).
