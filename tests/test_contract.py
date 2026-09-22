"""Contract tests against a real, stock Zabbix server.

Skipped unless ``ZABBIX_CONTRACT_URL`` and ``ZABBIX_CONTRACT_TOKEN`` are set. CI
runs them against the official Zabbix Docker images (see .github/workflows).
"""

from collections.abc import AsyncGenerator
import os
import time

import aiohttp
import pytest

from custom_components.zabbix.api import ZabbixAuthError, ZabbixClient, parse_version
from custom_components.zabbix.availability import host_availability

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
