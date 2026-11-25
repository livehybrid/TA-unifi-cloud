"""Modular input glue shared by every input: account lookup, logging, events.

Site Manager inputs (hosts, sites, devices) call api.ui.com/ea/<type> and need a
Cloud account. The UniFi Network input (unifi_network_helper) works with Cloud
and Local accounts alike.
"""
import json
import logging

import import_declare_test  # noqa: F401
from solnlib import conf_manager, log
from splunklib import modularinput as smi

import unifi_client

ADDON_NAME = "TA-unifi-cloud"
CONF_PREFIX = "ta_unifi_cloud"


def logger_for_input(input_name):
    return log.Logs().get_logger("%s_%s" % (ADDON_NAME.lower(), input_name))


def get_account(session_key, account_name):
    """The account as a dict: connection_type (cloud|local), api_key, url, verify."""
    cfm = conf_manager.ConfManager(
        session_key,
        ADDON_NAME,
        realm="__REST_CREDENTIAL__#%s#configs/conf-%s_account" % (ADDON_NAME, CONF_PREFIX),
    )
    conf = cfm.get_conf("%s_account" % CONF_PREFIX)
    acct = conf.get(account_name) or {}
    # Accounts made before 1.2.0 have only api_key: they are Cloud accounts.
    return {
        "connection_type": (acct.get("connection_type") or "cloud").strip().lower(),
        "api_key": acct.get("api_key"),
        "url": acct.get("url") or "",
        "verify": unifi_client.is_true(acct.get("verify_ssl"), default=True),
    }


def setup_logger(inputs, normalized_input_name):
    logger = logger_for_input(normalized_input_name)
    try:
        log_level = conf_manager.get_log_level(
            logger=logger,
            session_key=inputs.metadata["session_key"],
            app_name=ADDON_NAME,
            conf_name="%s_settings" % CONF_PREFIX,
        )
        logger.setLevel(log_level)
    except Exception:
        logger.setLevel(logging.INFO)
    return logger


def write(event_writer, input_item, sourcetype, record):
    event_writer.write_event(
        smi.Event(
            data=json.dumps(record, ensure_ascii=False, default=str),
            index=input_item.get("index"),
            sourcetype=sourcetype,
        )
    )


class unifiInput(object):
    """Site Manager input (hosts, sites or devices)."""

    def __init__(self, input_type) -> None:
        self.input_type = input_type
        self.sourcetype = "unifi:cloud:%s" % input_type

    def get_records(self, account, logger):
        client = unifi_client.UnifiClient(
            unifi_client.normalise_url(account["url"]) or unifi_client.CLOUD_DEFAULT_URL,
            account["api_key"],
            verify=account["verify"],
            logger=logger,
        )
        for item in client.site_manager("/ea/%s" % self.input_type):
            if self.input_type == "devices":
                # One entry per console, each with its own device list.
                for dev in item.get("devices") or []:
                    yield dev
            else:
                yield item

    def validate_input(self, definition: smi.ValidationDefinition):
        return

    def stream_events(self, inputs: smi.InputDefinition, event_writer: smi.EventWriter):
        for input_name, input_item in inputs.inputs.items():
            normalized_input_name = input_name.split("/")[-1]
            logger = setup_logger(inputs, normalized_input_name)
            try:
                log.modular_input_start(logger, normalized_input_name)
                account = get_account(inputs.metadata["session_key"], input_item.get("account"))
                if account["connection_type"] != "cloud":
                    logger.error(
                        "input=%s Site Manager inputs need a Cloud account (api.ui.com); "
                        "account=%s is %s. Use the UniFi Network input for local consoles.",
                        normalized_input_name, input_item.get("account"), account["connection_type"])
                    continue
                n = 0
                for record in self.get_records(account, logger):
                    write(event_writer, input_item, self.sourcetype, record)
                    n += 1
                log.events_ingested(
                    logger,
                    input_name,
                    self.sourcetype,
                    n,
                    input_item.get("index"),
                    account=input_item.get("account"),
                )
                log.modular_input_end(logger, normalized_input_name)
            except unifi_client.UnifiError as e:
                logger.error("input=%s Site Manager request failed: %s", normalized_input_name, e)
            except Exception as e:
                log.log_exception(logger, e, "unifi_input_error",
                                  msg_before="Exception raised while ingesting data for %s: " % normalized_input_name)
