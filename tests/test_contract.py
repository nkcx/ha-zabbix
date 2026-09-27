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


async def test_publish_round_trip(client: ZabbixClient) -> None:
    """Import the template, create a host and push values like the publisher."""
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
        {"output": ["templateid"], "filter": {"host": ["Home Assistant"]}},
    )
    groups = {group.name: group for group in await client.async_get_host_groups()}
    host_name = f"ha-contract-{int(time.time())}"
    created = await client.call(
        "host.create",
        {
            "host": host_name,
            "groups": [{"groupid": groups["Zabbix servers"].group_id}],
            "templates": [{"templateid": templates[0]["templateid"]}],
        },
    )
    host_id = created["hostids"][0]
    sender = AsyncSender(server="127.0.0.1", port=10051)
    try:
        exists, has_template = await client.async_check_publish_host(host_name)
        assert (exists, has_template) == (True, True)

        async def discovered() -> bool:
            await sender.send(
                [
                    ItemValue(
                        host_name,
                        discovery_key("float"),
                        json.dumps([{"{#KEY}": "sensor.contract/battery"}]),
                    ),
                    ItemValue(
                        host_name,
                        discovery_key("string"),
                        json.dumps([{"{#KEY}": "climate.contract"}]),
                    ),
                ]
            )
            items = await client.call(
                "item.get", {"output": ["key_"], "hostids": [host_id]}
            )
            return {
                value_key("float", "sensor.contract/battery"),
                value_key("string", "climate.contract"),
            } <= {item["key_"] for item in items}

        await _wait_for(discovered)

        async def stored() -> bool:
            response = await sender.send(
                [
                    ItemValue(
                        host_name, value_key("float", "sensor.contract/battery"), "42.5"
                    ),
                    ItemValue(
                        host_name, value_key("string", "climate.contract"), "heat"
                    ),
                ]
            )
            if response.processed < 2:
                return False
            items = await client.call(
                "item.get",
                {"output": ["key_", "lastvalue"], "hostids": [host_id]},
            )
            values = {item["key_"]: item["lastvalue"] for item in items}
            return (
                values.get(value_key("float", "sensor.contract/battery")) == "42.5"
                and values.get(value_key("string", "climate.contract")) == "heat"
            )

        await _wait_for(stored)
    finally:
        await client.call("host.delete", [host_id])
