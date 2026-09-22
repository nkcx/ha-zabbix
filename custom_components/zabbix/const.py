"""Constants for the Zabbix integration."""

from datetime import timedelta
from enum import StrEnum
import logging
from typing import Final

DOMAIN: Final = "zabbix"
LOGGER = logging.getLogger(__package__)

MIN_ZABBIX_VERSION: Final = (7, 0, 0)

CONF_API_TOKEN: Final = "api_token"
CONF_GROUP_IDS: Final = "group_ids"
CONF_ITEM_MODE: Final = "item_mode"
CONF_TAG: Final = "tag"
CONF_CONFIRM: Final = "confirm"

DEFAULT_SCAN_INTERVAL: Final = 30
MIN_SCAN_INTERVAL: Final = 10
MAX_SCAN_INTERVAL: Final = 3600
DEFAULT_TAG: Final = "homeassistant"

# Item metadata (the list of items, their names and units) changes rarely; it is
# refreshed on this interval, and immediately when the set of hosts changes.
METADATA_REFRESH_INTERVAL: Final = timedelta(minutes=10)

# Home Assistant limits state strings to 255 characters.
MAX_STATE_LENGTH: Final = 255

# Integration-created maintenance periods are named with this prefix.
MAINTENANCE_PREFIX: Final = "Home Assistant: "

EVENT_PROBLEM: Final = "zabbix_problem"


class ItemMode(StrEnum):
    """Which Zabbix items become Home Assistant entities."""

    ALL = "all"
    SERVER_AND_TAGGED = "server_and_tagged"
    TAGGED = "tagged"


DEFAULT_ITEM_MODE: Final = ItemMode.SERVER_AND_TAGGED

# Zabbix server self-monitoring items enabled by default in the
# "Zabbix server + tagged" mode (glob patterns on the item key). The rest of the
# server's internal items (per-process utilization etc.) are created disabled.
SERVER_ITEMS_ENABLED_BY_DEFAULT: Final = (
    "zabbix[wcache,values]",
    "zabbix[queue]",
    "zabbix[queue,10m]",
    "zabbix[requiredperformance]",
    "zabbix[lld_queue]",
    "zabbix[preprocessing_queue]",
    "zabbix[connector_queue]",
    "zabbix[*,pused]",
    "zabbix[*,*,pused]",
)
# Server items represented by dedicated entities instead.
SERVER_ITEMS_SKIPPED: Final = ("zabbix[version]",)


class ProblemEventType(StrEnum):
    """Changes to a problem detected between two polls."""

    PROBLEM = "problem"
    RESOLVED = "resolved"
    ACKNOWLEDGED = "acknowledged"
    UNACKNOWLEDGED = "unacknowledged"
    SEVERITY_CHANGED = "severity_changed"
    SUPPRESSED = "suppressed"
    UNSUPPRESSED = "unsuppressed"


# Entity kinds, used in unique ids.
KIND_PROBLEM: Final = "problem"
KIND_MAINTENANCE: Final = "maintenance"
KIND_HIGHEST_SEVERITY: Final = "highest_severity"
KIND_PROBLEM_EVENTS: Final = "problem_events"
KIND_AVAILABILITY_PREFIX: Final = "availability_"
KIND_PROBLEMS: Final = "problems"
KIND_VERSION: Final = "version"
KIND_HOSTS: Final = "hosts"
KIND_ITEMS: Final = "items"
KIND_UNSUPPORTED_ITEMS: Final = "unsupported_items"
KIND_TRIGGERS: Final = "triggers"
SERVICE_COUNT_KINDS: Final = (
    KIND_HOSTS,
    KIND_ITEMS,
    KIND_UNSUPPORTED_ITEMS,
    KIND_TRIGGERS,
)

SEVERITY_NAMES: Final = (
    "not_classified",
    "information",
    "warning",
    "average",
    "high",
    "disaster",
)
