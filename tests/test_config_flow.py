"""Tests for the Zabbix config and options flows."""

from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_SCAN_INTERVAL, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zabbix.api import ZabbixError, ZabbixSSLError, normalize_url
from custom_components.zabbix.const import (
    CONF_API_TOKEN,
    CONF_CONFIRM,
    CONF_GROUP_IDS,
    CONF_ITEM_MODE,
    CONF_TAG,
    DOMAIN,
)

from .fake_zabbix import TOKEN, ApiFailure, FakeZabbix


async def _start(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def _connect(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, token: str = TOKEN
) -> dict[str, Any]:
    result = await _start(hass)
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: fake_zabbix.url, CONF_API_TOKEN: token, CONF_VERIFY_SSL: True},
    )


async def test_user_flow(hass: HomeAssistant, fake_zabbix: FakeZabbix) -> None:
    result = await _connect(hass, fake_zabbix)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "select_groups"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GROUP_IDS: ["2"]}
    )
    assert result["step_id"] == "select_items"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ITEM_MODE: "server_and_tagged", CONF_TAG: "  "},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    url = normalize_url(fake_zabbix.url)
    assert result["title"] == "Zabbix (127.0.0.1)"
    assert result["data"] == {
        CONF_URL: url,
        CONF_API_TOKEN: TOKEN,
        CONF_VERIFY_SSL: True,
    }
    assert result["options"] == {
        CONF_SCAN_INTERVAL: 30,
        CONF_GROUP_IDS: ["2"],
        CONF_ITEM_MODE: "server_and_tagged",
        CONF_TAG: "homeassistant",
    }
    assert result["result"].unique_id == url


async def test_all_items_needs_confirmation(
    hass: HomeAssistant, fake_zabbix: FakeZabbix
) -> None:
    result = await _connect(hass, fake_zabbix)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GROUP_IDS: ["2", "7"]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ITEM_MODE: "all", CONF_TAG: "ha"}
    )
    assert result["step_id"] == "confirm_all"
    assert result["description_placeholders"] == {
        "hosts": "4",
        "items": "17",
        "largest_host": "Web server 01",
        "largest_count": "15",
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONFIRM: False}
    )
    assert result["step_id"] == "confirm_all"
    assert result["errors"] == {"base": "confirm_required"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONFIRM: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_ITEM_MODE] == "all"
    assert result["options"][CONF_TAG] == "ha"


async def test_confirm_all_without_hosts(
    hass: HomeAssistant, fake_zabbix: FakeZabbix
) -> None:
    result = await _connect(hass, fake_zabbix)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GROUP_IDS: []}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ITEM_MODE: "all", CONF_TAG: "ha"}
    )
    assert result["description_placeholders"] == {
        "hosts": "0",
        "items": "0",
        "largest_host": "-",
        "largest_count": "0",
    }


async def test_confirm_all_cannot_connect(
    hass: HomeAssistant, fake_zabbix: FakeZabbix
) -> None:
    result = await _connect(hass, fake_zabbix)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GROUP_IDS: ["2"]}
    )
    fake_zabbix.http_status = 503
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ITEM_MODE: "all", CONF_TAG: "ha"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


@pytest.mark.parametrize(
    ("setup", "error"),
    [
        (lambda fake: setattr(fake, "http_status", 503), "cannot_connect"),
        (lambda fake: setattr(fake, "raw_body", "<html></html>"), "invalid_url"),
        (lambda fake: setattr(fake, "version", "6.4.20"), "unsupported_version"),
        (
            lambda fake: fake.failures.update(
                {
                    "hostgroup.get": ApiFailure(
                        -32500,
                        "Application error.",
                        'No permissions to call "hostgroup.get".',
                    )
                }
            ),
            "no_api_access",
        ),
        (
            lambda fake: fake.failures.update(
                {"hostgroup.get": ApiFailure(-32500, "Application error.", "Boom")}
            ),
            "api_error",
        ),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, setup: Any, error: str
) -> None:
    setup(fake_zabbix)
    result = await _connect(hass, fake_zabbix)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    # The flow recovers once the problem is fixed.
    fake_zabbix.http_status = None
    fake_zabbix.raw_body = None
    fake_zabbix.version = "7.0.0"
    fake_zabbix.failures.clear()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: fake_zabbix.url, CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: True},
    )
    assert result["step_id"] == "select_groups"


async def test_user_flow_unsupported_version_placeholder(
    hass: HomeAssistant, fake_zabbix: FakeZabbix
) -> None:
    fake_zabbix.version = "6.0.30"
    result = await _connect(hass, fake_zabbix)
    assert result["description_placeholders"] == {"version": "6.0.30"}


async def test_user_flow_invalid_auth(
    hass: HomeAssistant, fake_zabbix: FakeZabbix
) -> None:
    result = await _connect(hass, fake_zabbix, token="wrong")
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_invalid_url(hass: HomeAssistant) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: "ftp://zabbix", CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: True},
    )
    assert result["errors"] == {"base": "invalid_url"}


async def test_user_flow_ssl_error(
    hass: HomeAssistant, fake_zabbix: FakeZabbix
) -> None:
    with patch(
        "custom_components.zabbix.api.ZabbixClient.async_get_version",
        side_effect=ZabbixSSLError("bad cert"),
    ):
        result = await _connect(hass, fake_zabbix)
    assert result["errors"] == {"base": "ssl_error"}


@pytest.mark.parametrize(
    ("exception", "error"),
    [(ZabbixError("x"), "cannot_connect"), (RuntimeError("x"), "unknown")],
)
async def test_user_flow_other_errors(
    hass: HomeAssistant,
    fake_zabbix: FakeZabbix,
    exception: Exception,
    error: str,
) -> None:
    with patch(
        "custom_components.zabbix.config_flow.async_validate", side_effect=exception
    ):
        result = await _connect(hass, fake_zabbix)
    assert result["errors"] == {"base": error}


async def test_already_configured(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await _connect(hass, fake_zabbix)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["url"] == config_entry.data[CONF_URL]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    fake_zabbix.token = "c" * 64
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "c" * 64}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_API_TOKEN] == "c" * 64


async def test_reauth_unexpected_error(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    with patch(
        "custom_components.zabbix.config_flow.async_validate",
        side_effect=RuntimeError,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_TOKEN: TOKEN}
        )
    assert result["errors"] == {"base": "unknown"}


async def test_reconfigure(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    new_url = fake_zabbix.url.replace("/zabbix/", "/monitoring/zabbix.php")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: new_url, CONF_VERIFY_SSL: False}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_URL] == normalize_url(new_url)
    assert config_entry.data[CONF_VERIFY_SSL] is False
    assert config_entry.unique_id == normalize_url(new_url)
    assert config_entry.data[CONF_URL].endswith("/monitoring/api_jsonrpc.php")
    assert config_entry.title == "Zabbix (127.0.0.1)"


async def test_reconfigure_different_server(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    fake_zabbix.data["groups"] = [{"groupid": "100", "name": "Else"}]
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: fake_zabbix.url, CONF_VERIFY_SSL: True}
    )
    assert result["errors"] == {"base": "different_server"}


async def test_reconfigure_errors(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    fake_zabbix.version = "6.0.0"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: fake_zabbix.url, CONF_VERIFY_SSL: True}
    )
    assert result["errors"] == {"base": "unsupported_version"}
    assert result["description_placeholders"] == {"version": "6.0.0"}
    fake_zabbix.version = "7.4.0"
    for exception, error in (
        (ZabbixError("x"), "cannot_connect"),
        (RuntimeError("x"), "unknown"),
    ):
        with patch(
            "custom_components.zabbix.api.ZabbixClient.async_get_host_groups",
            side_effect=[[], exception],
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_URL: fake_zabbix.url, CONF_VERIFY_SSL: True}
            )
        assert result["errors"] == {"base": error}


async def test_reconfigure_to_other_entry_url(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    other_url = normalize_url(fake_zabbix.url.replace("/zabbix/", "/other/"))
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=other_url,
        data={CONF_URL: other_url, CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: True},
    ).add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: other_url, CONF_VERIFY_SSL: True}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_GROUP_IDS: ["2"],
            CONF_SCAN_INTERVAL: 60,
            CONF_ITEM_MODE: "tagged",
            CONF_TAG: " monitor ",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert init_integration.options == {
        CONF_GROUP_IDS: ["2"],
        CONF_SCAN_INTERVAL: 60,
        CONF_ITEM_MODE: "tagged",
        CONF_TAG: "monitor",
    }
    assert init_integration.runtime_data.update_interval.total_seconds() == 60


async def test_options_flow_all_items(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    user_input = {
        CONF_GROUP_IDS: ["2"],
        CONF_SCAN_INTERVAL: 30,
        CONF_ITEM_MODE: "all",
        CONF_TAG: "homeassistant",
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input
    )
    assert result["step_id"] == "confirm_all"
    assert result["description_placeholders"]["hosts"] == "3"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CONFIRM: False}
    )
    assert result["errors"] == {"base": "confirm_required"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CONFIRM: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert init_integration.options[CONF_ITEM_MODE] == "all"

    # Staying in All items mode doesn't ask again.
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_options_flow_cannot_connect(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, init_integration: MockConfigEntry
) -> None:
    fake_zabbix.http_status = 503
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_options_flow_confirm_cannot_connect(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, init_integration: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    fake_zabbix.http_status = 503
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_GROUP_IDS: ["2"],
            CONF_SCAN_INTERVAL: 30,
            CONF_ITEM_MODE: "all",
            CONF_TAG: "homeassistant",
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"
    fake_zabbix.http_status = None
