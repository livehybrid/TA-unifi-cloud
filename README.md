# TA-unifi-cloud

**Unifi Cloud Add-on for Splunk**: a Splunk add-on (built with the
[UCC framework](https://github.com/splunk/addonfactory-ucc-generator)) that
collects UniFi inventory and state, either from the cloud (api.ui.com) or
straight from a local UniFi console.

## Accounts

Each account has a connection type:

| Connection type | API key | Console URL | Reaches |
|-----------------|---------|-------------|---------|
| **Cloud** (default) | Site Manager key from unifi.ui.com | leave empty | every console on the UniFi account, through `https://api.ui.com/v1/connector/consoles/<id>/...` |
| **Local** | UniFi Network key (Settings, Control Plane, Integrations) | `https://<console>`, for example `https://192.168.1.1` | that one console, at `https://<console>/proxy/network/integration/...` |

**Verify TLS certificate** is on by default. Untick it for a local console
with its factory self-signed certificate. Accounts created before 1.2.0 have
no connection type and are treated as Cloud, so existing inputs carry on
unchanged.

## Inputs

| Input | Account | Sourcetype | Collects |
|-------|---------|------------|----------|
| **UniFi Site Manager - Collect hosts** | Cloud | `unifi:cloud:hosts` | consoles on the account |
| **UniFi Site Manager - Collect Sites** | Cloud | `unifi:cloud:sites` | sites |
| **UniFi Site Manager - Collect Devices** | Cloud | `unifi:cloud:devices` | devices of every console |
| **UniFi Network** | Cloud or Local | see below | the UniFi Network Integration API, per site |

The **UniFi Network** input has a *Resources* setting; pick any of:

| Resource | Sourcetype | API (under `/v1/sites/<siteId>/`) |
|----------|------------|-----------------------------------|
| Sites | `unifi:network:site` | `/v1/sites` |
| Devices | `unifi:network:device` | `devices` |
| Device statistics | `unifi:network:device:statistics` | `devices/<id>/statistics/latest` (one request per device) |
| Clients | `unifi:network:client` | `clients` |
| Networks | `unifi:network:network` | `networks` |
| Wi-Fi broadcasts | `unifi:network:wifi` | `wifi/broadcasts` |
| Firewall zones | `unifi:network:firewall:zone` | `firewall/zones` |
| Firewall policies | `unifi:network:firewall:policy` | `firewall/policies` |

Every run is a full snapshot (all pages, offset/limit pagination), indexed
at collection time, so there is no checkpoint. Each event carries
`unifi_site_id`, `unifi_site_name`, `unifi_connection` and, for Cloud,
`unifi_console_id`; statistics events also carry `unifi_device_id`,
`unifi_device_name` and `unifi_device_model`.

After each console the input writes one `unifi:network:status` event:
`state` (`ok`, `partial`, `console_unavailable`, `auth_error`,
`connection_error`), record counts per resource, `unsupported` (resources the
console does not offer, answered 404, or 400 "zone-based firewall not
configured") and `errors`. An offline console (Cloud) gets a status event and
the input carries on with the others. Rate limiting (429) is retried.

Example: `sourcetype=unifi:network:status | stats latest(state) by unifi_console_id console_id`

## Compatibility

| Attribute | Value |
|-----------|-------|
| **Add-on version** | 1.2.0 |
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

All CI runs on GitHub Actions (`.github/workflows/build-test.yaml`):

| Job | When | What |
|-----|------|------|
| `unit-test` | every push | the collector against a synthetic mock (`tests/mock`), Python 3.9 and 3.13: pagination, unsupported endpoints, an offline console, auth failure, TLS verification on and off, Site Manager paging |
| `integration-test` | every push | the packaged add-on in real Splunk 10.0 and 10.6.0 in Docker, against the same mock as a compose service: install, schemes, exact event counts per sourcetype for Cloud and Local accounts, status events, key never logged, input runs on the expected Python |
| `live-cloud` | main, weekly, manual | real api.ui.com end to end in Splunk 10.6.0 (repo secret `UNIFI_CLOUD_API_KEY`) |
| `live-local` | main, weekly, manual | the collector against a real local console from a self-hosted runner (label `unifi-local`; secrets `UNIFI_LOCAL_URL`, `UNIFI_LOCAL_API_KEY`), no Splunk |

The live jobs never run for pull requests and print counts and field names
only, never event data.

Locally: `make -C docker build up test down` (needs Docker, `ucc-gen`,
`pytest`, `openssl`), or `make -C docker unit` for the collector tests alone.
