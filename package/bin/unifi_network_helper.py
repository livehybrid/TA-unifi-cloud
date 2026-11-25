"""UniFi Network input: the Network Integration API, Cloud or Local account.

Every run is a snapshot: each selected resource is listed in full (offset/limit
pagination) and indexed with the current time, so there is no checkpoint.
After each console a status event (sourcetype unifi:network:status) records
per-resource counts, resources the console does not support, and errors. An
offline console gets a status event and the run carries on with the next one.
"""
import import_declare_test  # noqa: F401
from solnlib import log
from splunklib import modularinput as smi

import unifi_client
from unifi_input_helper import get_account, setup_logger, write


def validate_input(definition: smi.ValidationDefinition):
    unifi_client.parse_resources(definition.parameters.get("resources"))


def stream_events(inputs: smi.InputDefinition, event_writer: smi.EventWriter):
    for input_name, input_item in inputs.inputs.items():
        name = input_name.split("/")[-1]
        logger = setup_logger(inputs, name)
        try:
            log.modular_input_start(logger, name)
            account = get_account(inputs.metadata["session_key"], input_item.get("account"))
            resources = unifi_client.parse_resources(input_item.get("resources"))
            ctype = account["connection_type"]
            try:
                targets = unifi_client.network_targets(
                    ctype, account["api_key"], account["url"], account["verify"], logger=logger)
            except (unifi_client.UnifiError, ValueError) as exc:
                logger.error("input=%s connection_type=%s cannot list consoles: %s", name, ctype, exc)
                state = exc.kind if isinstance(exc, unifi_client.UnifiError) else "config_error"
                write(event_writer, input_item, unifi_client.STATUS_SOURCETYPE, {
                    "connection_type": ctype, "state": state, "stage": "consoles",
                    "http_status": getattr(exc, "status", None),
                    "error_code": getattr(exc, "code", None) or str(exc)})
                continue
            counts = {}
            for console_id, client in targets:
                for sourcetype, record in unifi_client.collect_console(client, resources, console_id, ctype):
                    if sourcetype == "status":
                        write(event_writer, input_item, unifi_client.STATUS_SOURCETYPE, record)
                        level = logger.info if record["state"] == "ok" else logger.warning
                        level("input=%s console=%s state=%s sites=%s resources=%s unsupported=%s errors=%s",
                              name, console_id or "local", record["state"], record.get("sites"),
                              record.get("resources"), record.get("unsupported"),
                              record.get("errors") or record.get("error_code"))
                        continue
                    write(event_writer, input_item, sourcetype, record)
                    counts[sourcetype] = counts.get(sourcetype, 0) + 1
            for sourcetype, n in sorted(counts.items()):
                log.events_ingested(logger, input_name, sourcetype, n, input_item.get("index"),
                                    account=input_item.get("account"))
            log.modular_input_end(logger, name)
        except Exception as e:
            log.log_exception(logger, e, "unifi_network_error",
                              msg_before="Exception raised while ingesting data for %s: " % name)
