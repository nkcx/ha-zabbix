"""Unique ids for devices and entities.

Item entities are keyed on the item **key**, not the item id, so they survive
low-level discovery deleting and re-creating an item.
"""

import hashlib

from .const import DOMAIN

_MAX_KEY_LENGTH = 180


def _key_part(key: str) -> str:
    if len(key) <= _MAX_KEY_LENGTH:
        return key
    return "sha1:" + hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()


def service_device_identifier(entry_id: str) -> tuple[str, str]:
    """Return the identifier of the Zabbix (service) device."""
    return (DOMAIN, entry_id)


def host_device_identifier(entry_id: str, host_id: str) -> tuple[str, str]:
    """Return the identifier of a host's device."""
    return (DOMAIN, f"{entry_id}_{host_id}")


def item_unique_id(entry_id: str, host_id: str, key: str) -> str:
    """Return the unique id of an item entity."""
    return f"{entry_id}_item_{host_id}_{_key_part(key)}"


def host_unique_id(entry_id: str, host_id: str, kind: str) -> str:
    """Return the unique id of a built-in host entity."""
    return f"{entry_id}_host_{host_id}_{kind}"


def service_unique_id(entry_id: str, kind: str) -> str:
    """Return the unique id of a Zabbix (service) entity."""
    return f"{entry_id}_server_{kind}"
