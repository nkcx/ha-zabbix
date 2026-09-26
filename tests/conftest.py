"""Fixtures for Zabbix tests."""

from collections.abc import AsyncGenerator
from typing import Any

from aiohttp.test_utils import TestServer
from homeassistant.const import CONF_SCAN_INTERVAL, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.syrupy import (
    HomeAssistantSnapshotExtension,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.zabbix.api import normalize_url
from custom_components.zabbix.const import (
    CONF_API_TOKEN,
    CONF_GROUP_IDS,
    CONF_ITEM_MODE,
    CONF_TAG,
    DOMAIN,
)

from .fake_zabbix import TOKEN, FakeZabbix, make_app


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> None:
    """Enable loading custom_components/zabbix."""


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Use the Home Assistant snapshot extension (snapshots/ directory).

    The test plugin overrides syrupy's fixture too, but which plugin's fixture
    wins depends on plugin load order; a conftest fixture always wins.
    """
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


@pytest.fixture
async def fake_zabbix(socket_enabled: None) -> AsyncGenerator[FakeZabbix]:
    """Run a fake Zabbix API on 127.0.0.1."""
    fake = FakeZabbix()
    server = TestServer(make_app(fake), host="127.0.0.1")
    await server.start_server()
    fake.url = str(server.make_url("/zabbix/"))
    yield fake
    await server.close()


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """Options of the test config entry."""
    return {
        CONF_GROUP_IDS: ["2", "7"],
        CONF_ITEM_MODE: "server_and_tagged",
        CONF_TAG: "homeassistant",
        CONF_SCAN_INTERVAL: 30,
    }


@pytest.fixture
def config_entry(
    fake_zabbix: FakeZabbix, entry_options: dict[str, Any]
) -> MockConfigEntry:
    """Return a config entry for the fake server."""
    url = normalize_url(fake_zabbix.url)
    return MockConfigEntry(
        domain=DOMAIN,
        entry_id="01K5ZABBIXTESTENTRY000000",
        title="Zabbix (127.0.0.1)",
        unique_id=url,
        data={CONF_URL: url, CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: True},
        options=entry_options,
    )


@pytest.fixture
async def init_integration(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> MockConfigEntry:
    """Set up the integration."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
