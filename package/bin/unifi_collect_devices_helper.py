from unifi_input_helper import unifiInput
from splunklib import modularinput as smi

input_helper = unifiInput("devices")


def validate_input(definition: smi.ValidationDefinition):
    return input_helper.validate_input(definition)


def stream_events(inputs: smi.InputDefinition, event_writer: smi.EventWriter):
    return input_helper.stream_events(inputs, event_writer)
