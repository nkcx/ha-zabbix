"""Async client for the Zabbix JSON-RPC API (Zabbix 7.0+).

The client has no Home Assistant dependencies; it only needs an
``aiohttp.ClientSession``.
"""

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from enum import IntFlag
from itertools import count
import json
import logging
import ssl
import time
from typing import Any

import aiohttp
from yarl import URL

from .errors import (
    ZabbixConnectionError,
    ZabbixInvalidResponseError,
    ZabbixSSLError,
    classify_error,
)
from .models import (
    Host,
    HostGroup,
    Item,
    ItemType,
    ItemValue,
    Maintenance,
    Problem,
    ServerCounts,
    TokenUser,
    ids,
)

_LOGGER = logging.getLogger(__name__)

API_PATH = "api_jsonrpc.php"
DEFAULT_TIMEOUT = 30.0

_HOST_OUTPUT = [
    "hostid",
    "host",
    "name",
    "description",
    "maintenance_status",
    "maintenanceid",
    "active_available",
]
_INTERFACE_OUTPUT = [
    "interfaceid",
    "type",
    "main",
    "useip",
    "ip",
    "dns",
    "port",
    "available",
    "error",
]
_ITEM_OUTPUT = [
    "itemid",
    "hostid",
    "key_",
    "name",
    "name_resolved",
    "type",
    "value_type",
    "units",
    "master_itemid",
]
_VALUE_OUTPUT = ["itemid", "lastvalue", "lastclock", "state", "error"]
_PROBLEM_OUTPUT = [
    "eventid",
    "objectid",
    "name",
    "severity",
    "acknowledged",
    "suppressed",
    "clock",
    "opdata",
    "cause_eventid",
]
# Item types whose availability is tracked on an interface (frontend logic).
_INTERFACE_ITEM_TYPES = [
    ItemType.ZABBIX_AGENT,
    ItemType.IPMI,
    ItemType.JMX,
    ItemType.SNMP,
]
# Operator 4 = "Exists" in Zabbix tag filters.
_TAG_EXISTS = 4


class AcknowledgeAction(IntFlag):
    """Bitmask for ``event.acknowledge``."""

    CLOSE = 1
    ACKNOWLEDGE = 2
    MESSAGE = 4
    CHANGE_SEVERITY = 8
    UNACKNOWLEDGE = 16


def normalize_url(url: str) -> str:
    """Return the API endpoint URL for a frontend or API URL.

    Accepts ``host``, ``https://host/zabbix/``, a frontend page such as
    ``https://host/zabbix/zabbix.php?action=dashboard.view`` or the API URL itself.
    """
    url = url.strip()
    if "://" not in url:
        url = f"https://{url}"
    parsed = URL(url)
    if parsed.scheme not in ("http", "https") or not parsed.host:
        raise ValueError(f"Invalid URL: {url}")
    path = parsed.path
    if path.endswith(".php"):
        path = path.rsplit("/", 1)[0]
    path = path.rstrip("/")
    return str(
        URL.build(
            scheme=parsed.scheme.lower(),
            host=parsed.host.lower(),
            port=parsed.explicit_port,
            path=f"{path}/{API_PATH}",
        )
    )


def frontend_url(api_url: str) -> str:
    """Return the frontend base URL (with trailing slash) for an API URL."""
    return api_url.removesuffix(API_PATH)


class ZabbixClient:
    """Minimal typed client for the parts of the Zabbix API we use."""

    def __init__(
        self,
        url: str,
        token: str,
        session: aiohttp.ClientSession,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Initialize the client. ``url`` may be any form accepted by normalize_url."""
        self.url = normalize_url(url)
        self._token = token
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._request_ids = count(1)

    @property
    def frontend_url(self) -> str:
        """Return the frontend base URL."""
        return frontend_url(self.url)

    async def call(
        self,
        method: str,
        params: Mapping[str, Any] | Sequence[Any] | None = None,
        *,
        authenticated: bool = True,
    ) -> Any:
        """Call an API method and return its ``result``."""
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params if params is not None else {},
            "id": next(self._request_ids),
        }
        headers = {"Content-Type": "application/json-rpc"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._token}"
        started = time.monotonic()
        try:
            async with self._session.post(
                self.url, json=payload, headers=headers, timeout=self._timeout
            ) as response:
                if response.status >= 500:
                    raise ZabbixConnectionError(
                        f"{method}: HTTP {response.status} from {self.url}"
                    )
                if response.status != 200:
                    raise ZabbixInvalidResponseError(
                        f"{method}: HTTP {response.status} from {self.url}"
                    )
                body = await response.text()
        except (aiohttp.ClientSSLError, ssl.SSLError) as err:
            raise ZabbixSSLError(f"{method}: TLS error: {err}") from err
        except TimeoutError as err:
            raise ZabbixConnectionError(
                f"{method}: timeout talking to {self.url}"
            ) from err
        except aiohttp.ClientError as err:
            raise ZabbixConnectionError(f"{method}: {err}") from err
        _LOGGER.debug(
            "%s answered in %.3fs (%d bytes)",
            method,
            time.monotonic() - started,
            len(body),
        )
        try:
            data = json.loads(body)
        except ValueError as err:
            raise ZabbixInvalidResponseError(
                f"{method}: response is not JSON (is {self.url} a Zabbix server?)"
            ) from err
        if not isinstance(data, dict) or data.get("jsonrpc") != "2.0":
            raise ZabbixInvalidResponseError(f"{method}: not a JSON-RPC response")
        if "error" in data:
            error = data["error"]
            raise classify_error(method, error if isinstance(error, dict) else {})
        if "result" not in data:
            raise ZabbixInvalidResponseError(f"{method}: response has no result")
        return data["result"]

    # Identity -----------------------------------------------------------------

    async def async_get_version(self) -> str:
        """Return the API version (``apiinfo.version``; must be unauthenticated)."""
        result = await self.call("apiinfo.version", authenticated=False)
        if not isinstance(result, str):
            raise ZabbixInvalidResponseError("apiinfo.version: unexpected result")
        return result

    async def async_get_token_user(self) -> TokenUser:
        """Validate the token and return its user (must be unauthenticated)."""
        result = await self.call(
            "user.checkAuthentication", {"token": self._token}, authenticated=False
        )
        if not isinstance(result, dict):
            raise ZabbixInvalidResponseError(
                "user.checkAuthentication: unexpected result"
            )
        return TokenUser.from_api(result)

    # Read path ----------------------------------------------------------------

    async def async_get_host_groups(
        self, group_ids: Iterable[str] | None = None
    ) -> list[HostGroup]:
        """Return host groups that contain monitored hosts."""
        params: dict[str, Any] = {
            "output": ["groupid", "name"],
            "selectHosts": "count",
            "sortfield": "name",
        }
        if group_ids is not None:
            params["groupids"] = ids(set(group_ids))
        else:
            params["with_monitored_hosts"] = True
        result = await self.call("hostgroup.get", params)
        return [HostGroup.from_api(group) for group in result]

    async def async_get_hosts(self, group_ids: Iterable[str]) -> list[Host]:
        """Return monitored hosts in the given groups, with interfaces."""
        group_ids = ids(set(group_ids))
        if not group_ids:
            return []
        result = await self.call(
            "host.get",
            {
                "output": _HOST_OUTPUT,
                "groupids": group_ids,
                "monitored_hosts": True,
                "selectInterfaces": _INTERFACE_OUTPUT,
                "selectHostGroups": ["groupid"],
            },
        )
        return [Host.from_api(host) for host in result]

    async def async_get_host_names(self, host_ids: Iterable[str]) -> dict[str, str]:
        """Return the visible names of the given hosts."""
        host_ids = ids(set(host_ids))
        if not host_ids:
            return {}
        result = await self.call(
            "host.get", {"output": ["hostid", "host", "name"], "hostids": host_ids}
        )
        return {
            str(host["hostid"]): str(host.get("name") or host.get("host", ""))
            for host in result
        }

    async def async_get_items(
        self,
        *,
        host_ids: Iterable[str] | None = None,
        tag: str | None = None,
    ) -> list[Item]:
        """Return enabled items (metadata) on monitored hosts.

        ``tag`` restricts the result to items that carry a tag with that name.
        """
        params: dict[str, Any] = {
            "output": _ITEM_OUTPUT,
            "monitored": True,
            "webitems": True,
            "selectTags": ["tag", "value"],
            "selectValueMap": ["valuemapid", "name", "mappings"],
        }
        if host_ids is not None:
            host_ids = ids(set(host_ids))
            if not host_ids:
                return []
            params["hostids"] = host_ids
        if tag is not None:
            params["tags"] = [{"tag": tag, "operator": _TAG_EXISTS}]
        result = await self.call("item.get", params)
        return [Item.from_api(item) for item in result]

    async def async_get_server_items(self) -> list[Item]:
        """Return the Zabbix server's self-monitoring items, on any host.

        These are enabled internal items with ``zabbix[...]`` keys, excluding the
        per-host ``zabbix[host,...]`` keys, plus items that depend on them.
        """
        params: dict[str, Any] = {
            "output": _ITEM_OUTPUT,
            "monitored": True,
            "filter": {"type": ItemType.INTERNAL},
            "search": {"key_": "zabbix["},
            "startSearch": True,
            "selectTags": ["tag", "value"],
            "selectValueMap": ["valuemapid", "name", "mappings"],
        }
        result = await self.call("item.get", params)
        items = [
            item
            for item in (Item.from_api(raw) for raw in result)
            if not item.key.startswith("zabbix[host,")
        ]
        master_ids = {item.item_id for item in items}
        seen = set(master_ids)
        while master_ids:
            dependents = await self.call(
                "item.get",
                {
                    "output": _ITEM_OUTPUT,
                    "monitored": True,
                    "filter": {"master_itemid": ids(master_ids)},
                    "selectTags": ["tag", "value"],
                    "selectValueMap": ["valuemapid", "name", "mappings"],
                },
            )
            master_ids = set()
            for raw in dependents:
                item = Item.from_api(raw)
                if item.item_id not in seen:
                    seen.add(item.item_id)
                    master_ids.add(item.item_id)
                    items.append(item)
        return items

    async def async_get_item_values(
        self, item_ids: Iterable[str]
    ) -> dict[str, ItemValue]:
        """Return the latest values of the given items."""
        item_ids = ids(set(item_ids))
        if not item_ids:
            return {}
        result = await self.call(
            "item.get",
            {"output": _VALUE_OUTPUT, "itemids": item_ids, "webitems": True},
        )
        values = [ItemValue.from_api(value) for value in result]
        return {value.item_id: value for value in values}

    async def async_get_interface_item_counts(
        self, interface_ids: Iterable[str]
    ) -> dict[str, int]:
        """Return the number of enabled interface-bound items per interface."""
        interface_ids = ids(set(interface_ids))
        if not interface_ids:
            return {}
        result = await self.call(
            "item.get",
            {
                "countOutput": True,
                "groupCount": True,
                "interfaceids": interface_ids,
                "filter": {"type": _INTERFACE_ITEM_TYPES, "status": 0},
            },
        )
        return {str(row["interfaceid"]): int(row["rowscount"]) for row in result}

    async def async_get_active_item_counts(
        self, host_ids: Iterable[str]
    ) -> dict[str, int]:
        """Return the number of enabled active-agent items per host."""
        host_ids = ids(set(host_ids))
        if not host_ids:
            return {}
        result = await self.call(
            "item.get",
            {
                "countOutput": True,
                "groupCount": True,
                "hostids": host_ids,
                "filter": {"type": ItemType.ZABBIX_ACTIVE, "status": 0},
            },
        )
        return {str(row["hostid"]): int(row["rowscount"]) for row in result}

    async def async_get_item_counts(self, host_ids: Iterable[str]) -> dict[str, int]:
        """Return the number of enabled items per monitored host."""
        host_ids = ids(set(host_ids))
        if not host_ids:
            return {}
        result = await self.call(
            "item.get",
            {
                "countOutput": True,
                "groupCount": True,
                "hostids": host_ids,
                "monitored": True,
                "webitems": True,
            },
        )
        return {str(row["hostid"]): int(row["rowscount"]) for row in result}

    async def async_get_server_counts(self) -> ServerCounts:
        """Return host, item and trigger counts, like System information."""
        hosts, items, unsupported, triggers = await asyncio.gather(
            self.call("host.get", {"countOutput": True, "filter": {"status": 0}}),
            self.call(
                "item.get", {"countOutput": True, "monitored": True, "webitems": True}
            ),
            self.call(
                "item.get",
                {
                    "countOutput": True,
                    "monitored": True,
                    "webitems": True,
                    "filter": {"state": 1},
                },
            ),
            self.call("trigger.get", {"countOutput": True, "monitored": True}),
        )
        return ServerCounts(
            hosts=int(hosts),
            items=int(items),
            items_unsupported=int(unsupported),
            triggers=int(triggers),
        )

    async def async_get_problems(self) -> list[Problem]:
        """Return every open problem visible to the token's user."""
        result = await self.call(
            "problem.get",
            {
                "output": _PROBLEM_OUTPUT,
                "selectTags": ["tag", "value"],
                "sortfield": ["eventid"],
            },
        )
        return [Problem.from_api(problem) for problem in result]

    async def async_get_trigger_hosts(
        self, trigger_ids: Iterable[str]
    ) -> dict[str, tuple[str, ...]]:
        """Return the host ids of each trigger."""
        trigger_ids = ids(set(trigger_ids))
        if not trigger_ids:
            return {}
        result = await self.call(
            "trigger.get",
            {
                "output": ["triggerid"],
                "triggerids": trigger_ids,
                "selectHosts": ["hostid"],
            },
        )
        return {
            str(trigger["triggerid"]): tuple(
                str(host["hostid"]) for host in trigger.get("hosts") or ()
            )
            for trigger in result
        }

    async def async_get_maintenances(self, name_prefix: str) -> list[Maintenance]:
        """Return maintenance periods whose name starts with ``name_prefix``."""
        result = await self.call(
            "maintenance.get",
            {
                "output": ["maintenanceid", "name"],
                "selectHosts": ["hostid"],
                "search": {"name": name_prefix},
                "startSearch": True,
            },
        )
        return [Maintenance.from_api(maintenance) for maintenance in result]

    # Write path ---------------------------------------------------------------

    async def async_acknowledge(
        self,
        event_ids: Iterable[str],
        action: AcknowledgeAction,
        *,
        message: str | None = None,
        severity: int | None = None,
    ) -> None:
        """Update problems (acknowledge, close, comment, ...)."""
        params: dict[str, Any] = {"eventids": ids(set(event_ids))}
        if message:
            action |= AcknowledgeAction.MESSAGE
            params["message"] = message
        if severity is not None:
            action |= AcknowledgeAction.CHANGE_SEVERITY
            params["severity"] = severity
        params["action"] = int(action)
        await self.call("event.acknowledge", params)

    async def async_create_maintenance(
        self,
        name: str,
        host_ids: Iterable[str],
        *,
        start: int,
        duration: int,
        collect_data: bool = True,
        description: str = "",
    ) -> str:
        """Create a one-time maintenance period and return its id."""
        result = await self.call(
            "maintenance.create",
            {
                "name": name,
                "description": description,
                "maintenance_type": 0 if collect_data else 1,
                "active_since": start,
                "active_till": start + duration,
                "hosts": [{"hostid": host_id} for host_id in ids(set(host_ids))],
                "timeperiods": [
                    {"timeperiod_type": 0, "start_date": start, "period": duration}
                ],
            },
        )
        return str(result["maintenanceids"][0])

    async def async_set_maintenance_hosts(
        self, maintenance_id: str, host_ids: Iterable[str]
    ) -> None:
        """Replace the hosts of a maintenance period."""
        await self.call(
            "maintenance.update",
            {
                "maintenanceid": maintenance_id,
                "hosts": [{"hostid": host_id} for host_id in ids(set(host_ids))],
            },
        )

    async def async_delete_maintenances(self, maintenance_ids: Iterable[str]) -> None:
        """Delete maintenance periods."""
        maintenance_ids = ids(set(maintenance_ids))
        if maintenance_ids:
            await self.call("maintenance.delete", maintenance_ids)
