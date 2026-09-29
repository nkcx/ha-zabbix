"""Contract tests against a real, stock Zabbix server.

Skipped unless ``ZABBIX_CONTRACT_URL`` and ``ZABBIX_CONTRACT_TOKEN`` are set. CI
runs them against the official Zabbix Docker images (see .github/workflows).
"""

import asyncio
from collections.abc import AsyncGenerator
import json
import os
from pathlib import Path
import time
from typing import Any

import aiohttp
import pytest
from zabbix_utils import AsyncSender, ItemValue

from custom_components.zabbix.api import ZabbixAuthError, ZabbixClient, parse_version
from custom_components.zabbix.availability import host_availability
from custom_components.zabbix.publisher import discovery_key, value_key

URL = os.environ.get("ZABBIX_CONTRACT_URL", "")
TOKEN = os.environ.get("ZABBIX_CONTRACT_TOKEN", "")

pytestmark = pytest.mark.skipif(
    not (URL and TOKEN), reason="no Zabbix contract server configured"
)


@pytest.fixture
async def client(socket_enabled: None) -> AsyncGenerator[ZabbixClient]:
    """Return a client for the contract server."""
    async with aiohttp.ClientSession() as session:
        yield ZabbixClient(URL, TOKEN, session)


async def test_identity(client: ZabbixClient) -> None:
    assert parse_version(await client.async_get_version()) >= (7, 0, 0)
    user = await client.async_get_token_user()
    assert user.username == "Admin"
    async with aiohttp.ClientSession() as session:
        with pytest.raises(ZabbixAuthError):
            await ZabbixClient(URL, "0" * 64, session).async_get_hosts(["1"])


async def test_stock_zabbix_server(client: ZabbixClient) -> None:
    groups = {group.name: group for group in await client.async_get_host_groups()}
    assert "Zabbix servers" in groups
    hosts = await client.async_get_hosts([groups["Zabbix servers"].group_id])
    server = next(host for host in hosts if host.host == "Zabbix server")
    items = await client.async_get_items(host_ids=[server.host_id])
    assert items
    assert all(item.name for item in items)
    assert (
        await client.async_get_items(host_ids=[server.host_id], tag="homeassistant")
        == []
    )

    server_items = await client.async_get_server_items()
    keys = {item.key for item in server_items}
    assert "zabbix[queue]" in keys
    assert "zabbix[wcache,values]" in keys
    assert not any(key.startswith("zabbix[host,") for key in keys)

    values = await client.async_get_item_values(item.item_id for item in items)
    assert set(values) <= {item.item_id for item in items}

    interface_counts = await client.async_get_interface_item_counts(
        interface.interface_id for interface in server.interfaces
    )
    active_counts = await client.async_get_active_item_counts([server.host_id])
    availability = host_availability(
        server, interface_counts, active_counts.get(server.host_id, 0)
    )
    assert availability
    assert (await client.async_get_item_counts([server.host_id]))[server.host_id] > 0

    counts = await client.async_get_server_counts()
    assert counts.hosts >= 1
    assert counts.items > 0
    assert counts.triggers > 0

    problems = await client.async_get_problems()
    trigger_hosts = await client.async_get_trigger_hosts(
        problem.trigger_id for problem in problems
    )
    assert set(trigger_hosts) <= {problem.trigger_id for problem in problems}


async def test_maintenance_round_trip(client: ZabbixClient) -> None:
    groups = {group.name: group for group in await client.async_get_host_groups()}
    hosts = await client.async_get_hosts([groups["Zabbix servers"].group_id])
    prefix = "Home Assistant contract test: "
    maintenance_id = await client.async_create_maintenance(
        f"{prefix}{time.time()}",
        [hosts[0].host_id],
        start=int(time.time()),
        duration=600,
    )
    found = await client.async_get_maintenances(prefix)
    assert [m.maintenance_id for m in found] == [maintenance_id]
    await client.async_set_maintenance_hosts(maintenance_id, [hosts[0].host_id])
    await client.async_delete_maintenances([maintenance_id])
    assert await client.async_get_maintenances(prefix) == []


async def _wait_for(check: Any) -> None:
    """Retry ``check`` until it returns True (Zabbix applies changes async)."""
    async with asyncio.timeout(180):
        while not await check():  # noqa: ASYNC110 - polling an external server
            await asyncio.sleep(3)


async def _import_template(client: ZabbixClient) -> str:
    """Import zabbix/template_home_assistant.yaml and return its template id."""
    template = Path(__file__).parents[1] / "zabbix" / "template_home_assistant.yaml"
    create = {"createMissing": True, "updateExisting": True}
    await client.call(
        "configuration.import",
        {
            "format": "yaml",
            "source": template.read_text(encoding="utf-8"),
            "rules": {
                "template_groups": create,
                "templates": create,
                "discoveryRules": {**create, "deleteMissing": False},
            },
        },
    )
    templates = await client.call(
        "template.get",
        {
            "output": ["templateid"],
            "filter": {"host": ["Home Assistant by Zabbix trapper"]},
        },
    )
    return str(templates[0]["templateid"])


async def _send_until_stored(
    client: ZabbixClient, host_name: str, host_id: str, values: dict[str, str]
) -> None:
    """Send discovery and values until Zabbix created the items and stored them."""
    sender = AsyncSender(server="127.0.0.1", port=10051)
    by_type: dict[str, list[str]] = {}
    for key in values:
        item_type, _, entity = key.partition(":")
        by_type.setdefault(item_type, []).append(entity)

    async def discovered() -> bool:
        await sender.send(
            [
                ItemValue(
                    host_name,
                    discovery_key(item_type),
                    json.dumps([{"{#KEY}": entity} for entity in entities]),
                )
                for item_type, entities in by_type.items()
            ]
        )
        items = await client.call(
            "item.get", {"output": ["key_"], "hostids": [host_id]}
        )
        wanted = {value_key(*key.split(":", 1)) for key in values}
        return wanted <= {item["key_"] for item in items}

    await _wait_for(discovered)

    async def stored() -> bool:
        await sender.send(
            [
                ItemValue(host_name, value_key(*key.split(":", 1)), value)
                for key, value in values.items()
            ]
        )
        items = await client.call(
            "item.get", {"output": ["key_", "lastvalue"], "hostids": [host_id]}
        )
        current = {item["key_"]: item["lastvalue"] for item in items}
        return all(
            current.get(value_key(*key.split(":", 1))) == value
            for key, value in values.items()
        )

    await _wait_for(stored)


ALLOW_ALL = [{"macro": "{$HOMEASSISTANT.ALLOWED_HOSTS}", "value": "0.0.0.0/0,::/0"}]


async def test_publish_round_trip(client: ZabbixClient) -> None:
    """Import the template, create a host and push values like the publisher."""
    template_id = await _import_template(client)
    groups = {group.name: group for group in await client.async_get_host_groups()}
    # The host name suggested in the README must work (#4).
    host_name = "Home Assistant"
    created = await client.call(
        "host.create",
        {
            "host": host_name,
            "groups": [{"groupid": groups["Zabbix servers"].group_id}],
            "templates": [{"templateid": template_id}],
            # Newer Zabbix only accepts trapper data from localhost by default.
            "macros": ALLOW_ALL,
        },
    )
    host_id = created["hostids"][0]
    try:
        exists, has_template = await client.async_check_publish_host(host_name)
        assert (exists, has_template) == (True, True)
        await _send_until_stored(
            client,
            host_name,
            host_id,
            {
                "float:sensor.contract/battery": "42.5",
                "string:climate.contract": "heat",
            },
        )
    finally:
        await client.call("host.delete", [host_id])


async def test_migrate_from_builtin_template(client: ZabbixClient) -> None:
    """Replacing the built-in integration's template keeps items and history."""
    groups = {group.name: group for group in await client.async_get_host_groups()}
    group_id = groups["Zabbix servers"].group_id
    # The built-in integration's template, as published in Home Assistant's docs.
    old = await client.call(
        "template.create",
        {"host": "Template Home Assistant", "groups": [{"groupid": "1"}]},
    )
    old_id = old["templateids"][0]
    rule = await client.call(
        "discoveryrule.create",
        {
            "name": "Floats Discovery",
            "key_": "homeassistant.floats_discovery",
            "hostid": old_id,
            "type": 2,
            "trapper_hosts": "{$HOMEASSISTANT.ALLOWED_HOSTS}",
        },
    )
    await client.call(
        "itemprototype.create",
        {
            "name": "{#KEY}",
            "key_": "homeassistant.float[{#KEY}]",
            "hostid": old_id,
            "ruleid": rule["itemids"][0],
            "type": 2,
            "value_type": 0,
            "history": "1095d",
            "trends": "0",
            "trapper_hosts": "{$HOMEASSISTANT.ALLOWED_HOSTS}",
        },
    )
    new_id = await _import_template(client)
    host_name = f"ha-migrate-{int(time.time())}"
    created = await client.call(
        "host.create",
        {
            "host": host_name,
            "groups": [{"groupid": group_id}],
            "templates": [{"templateid": old_id}],
            "macros": ALLOW_ALL,
        },
    )
    host_id = created["hostids"][0]
    key = value_key("float", "sensor.migrate")
    try:
        await _send_until_stored(
            client, host_name, host_id, {"float:sensor.migrate": "1.5"}
        )
        before = await client.call(
            "item.get",
            {"output": ["itemid"], "hostids": [host_id], "filter": {"key_": [key]}},
        )
        # Unlink (without clearing) the old template, link the new one.
        await client.call(
            "host.update",
            {
                "hostid": host_id,
                "templates": [{"templateid": new_id}],
                "macros": ALLOW_ALL,
            },
        )
        rules = await client.call(
            "discoveryrule.get",
            {"output": ["itemid", "templateid"], "hostids": [host_id]},
        )
        assert rules
        assert all(rule["templateid"] != "0" for rule in rules)
        after = await client.call(
            "item.get",
            {"output": ["itemid"], "hostids": [host_id], "filter": {"key_": [key]}},
        )
        # Same item (so its history is kept), now receiving new values.
        assert [item["itemid"] for item in after] == [item["itemid"] for item in before]
        await _send_until_stored(
            client, host_name, host_id, {"float:sensor.migrate": "2.5"}
        )
    finally:
        await client.call("host.delete", [host_id])
        await client.call("template.delete", [old_id])
