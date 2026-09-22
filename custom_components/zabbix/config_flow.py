"""Config flow for the Zabbix integration."""

from collections.abc import Mapping
import logging
from typing import Any, override

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_SCAN_INTERVAL, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol
from yarl import URL

from .api import (
    HostGroup,
    ZabbixApiError,
    ZabbixAuthError,
    ZabbixClient,
    ZabbixConnectionError,
    ZabbixError,
    ZabbixInvalidResponseError,
    ZabbixPermissionError,
    ZabbixSSLError,
    parse_version,
)
from .const import (
    CONF_API_TOKEN,
    CONF_CONFIRM,
    CONF_GROUP_IDS,
    CONF_ITEM_MODE,
    CONF_TAG,
    DEFAULT_ITEM_MODE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TAG,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    MIN_ZABBIX_VERSION,
    ItemMode,
)

_LOGGER = logging.getLogger(__name__)

EXAMPLE_URL = "https://zabbix.example.com/zabbix/"

TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
URL_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.URL, autocomplete="url")
)


class FlowError(Exception):
    """A validation error shown in a flow form."""

    def __init__(self, error: str, placeholders: dict[str, str] | None = None) -> None:
        """Initialize the error."""
        super().__init__(error)
        self.error = error
        self.placeholders = placeholders or {}


async def async_validate(
    hass: HomeAssistant, url: str, token: str, verify_ssl: bool
) -> ZabbixClient:
    """Connect, check the version and the token. Raises FlowError."""
    try:
        client = ZabbixClient(
            url, token, async_get_clientsession(hass, verify_ssl=verify_ssl)
        )
    except ValueError as err:
        raise FlowError("invalid_url") from err
    try:
        version = await client.async_get_version()
        if parse_version(version) < MIN_ZABBIX_VERSION:
            raise FlowError("unsupported_version", {"version": version})
        await client.async_get_token_user()
        # Confirms the user's role grants API access.
        await client.async_get_host_groups()
    except ZabbixSSLError as err:
        raise FlowError("ssl_error") from err
    except ZabbixConnectionError as err:
        raise FlowError("cannot_connect") from err
    except ZabbixInvalidResponseError as err:
        raise FlowError("invalid_url") from err
    except ZabbixAuthError as err:
        raise FlowError("invalid_auth") from err
    except ZabbixPermissionError as err:
        raise FlowError("no_api_access") from err
    except ZabbixApiError as err:
        _LOGGER.debug("Zabbix API error during validation: %s", err)
        raise FlowError("api_error", {"error": err.data or err.message}) from err
    return client


def _title(client: ZabbixClient) -> str:
    return f"Zabbix ({URL(client.url).host})"


def _group_selector(groups: list[HostGroup]) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(
                    value=group.group_id, label=f"{group.name} ({group.host_count})"
                )
                for group in groups
            ],
            multiple=True,
            mode=SelectSelectorMode.LIST,
        )
    )


ITEM_MODE_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=[mode.value for mode in ItemMode],
        translation_key=CONF_ITEM_MODE,
        mode=SelectSelectorMode.LIST,
    )
)


def _item_schema(options: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_ITEM_MODE, default=options.get(CONF_ITEM_MODE, DEFAULT_ITEM_MODE)
            ): ITEM_MODE_SELECTOR,
            vol.Required(CONF_TAG, default=options.get(CONF_TAG, DEFAULT_TAG)): str,
        }
    )


async def async_all_items_placeholders(
    client: ZabbixClient, group_ids: list[str]
) -> dict[str, str]:
    """Return the numbers shown when confirming the All items mode."""
    hosts = await client.async_get_hosts(group_ids)
    counts = await client.async_get_item_counts(host.host_id for host in hosts)
    names = {host.host_id: host.name for host in hosts}
    largest_id = max(counts, key=lambda host_id: counts[host_id], default=None)
    return {
        "hosts": str(len(hosts)),
        "items": str(sum(counts.values())),
        "largest_host": names.get(largest_id, "-") if largest_id else "-",
        "largest_count": str(counts[largest_id]) if largest_id else "0",
    }


CONFIRM_SCHEMA = vol.Schema(
    {vol.Required(CONF_CONFIRM, default=False): BooleanSelector()}
)


class ZabbixConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Zabbix."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._client: ZabbixClient | None = None
        self._data: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._groups: list[HostGroup] = []

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> ZabbixOptionsFlow:
        """Return the options flow."""
        return ZabbixOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the server URL and API token."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            try:
                client = await async_validate(
                    self.hass,
                    user_input[CONF_URL],
                    user_input[CONF_API_TOKEN],
                    user_input[CONF_VERIFY_SSL],
                )
                self._groups = await client.async_get_host_groups()
            except FlowError as err:
                errors["base"] = err.error
                placeholders = err.placeholders
            except ZabbixError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error while setting up Zabbix")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(client.url)
                self._abort_if_unique_id_configured()
                self._client = client
                self._data = {
                    CONF_URL: client.url,
                    CONF_API_TOKEN: user_input[CONF_API_TOKEN],
                    CONF_VERIFY_SSL: user_input[CONF_VERIFY_SSL],
                }
                return await self.async_step_select_groups()

        schema = vol.Schema(
            {
                vol.Required(CONF_URL): URL_SELECTOR,
                vol.Required(CONF_API_TOKEN): TOKEN_SELECTOR,
                vol.Required(CONF_VERIFY_SSL, default=True): BooleanSelector(),
            }
        )
        suggested = {
            key: value
            for key, value in (user_input or {}).items()
            if key != CONF_API_TOKEN
        }
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
            description_placeholders={"example_url": EXAMPLE_URL, **placeholders},
        )

    async def async_step_select_groups(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which host groups become devices."""
        if user_input is not None:
            self._options[CONF_GROUP_IDS] = user_input[CONF_GROUP_IDS]
            return await self.async_step_select_items()
        return self.async_show_form(
            step_id="select_groups",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_GROUP_IDS,
                        default=[group.group_id for group in self._groups],
                    ): _group_selector(self._groups)
                }
            ),
        )

    async def async_step_select_items(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which items become entities."""
        if user_input is not None:
            self._options[CONF_ITEM_MODE] = user_input[CONF_ITEM_MODE]
            self._options[CONF_TAG] = user_input[CONF_TAG].strip() or DEFAULT_TAG
            if user_input[CONF_ITEM_MODE] == ItemMode.ALL:
                return await self.async_step_confirm_all()
            return self._create_entry()
        return self.async_show_form(
            step_id="select_items", data_schema=_item_schema(self._options)
        )

    async def async_step_confirm_all(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Make the user confirm the All items mode, with real numbers."""
        assert self._client is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_CONFIRM]:
                return self._create_entry()
            errors["base"] = "confirm_required"
        try:
            placeholders = await async_all_items_placeholders(
                self._client, self._options[CONF_GROUP_IDS]
            )
        except ZabbixError:
            return self.async_abort(reason="cannot_connect")
        return self.async_show_form(
            step_id="confirm_all",
            data_schema=CONFIRM_SCHEMA,
            description_placeholders=placeholders,
            errors=errors,
        )

    def _create_entry(self) -> ConfigFlowResult:
        assert self._client is not None
        return self.async_create_entry(
            title=_title(self._client),
            data=self._data,
            options={CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL, **self._options},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new API token."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"url": entry.data[CONF_URL]}
        if user_input is not None:
            try:
                await async_validate(
                    self.hass,
                    entry.data[CONF_URL],
                    user_input[CONF_API_TOKEN],
                    entry.data[CONF_VERIFY_SSL],
                )
            except FlowError as err:
                errors["base"] = err.error
                placeholders |= err.placeholders
            except Exception:
                _LOGGER.exception("Unexpected error during Zabbix reauthentication")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_TOKEN: user_input[CONF_API_TOKEN]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_TOKEN): TOKEN_SELECTOR}),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the server URL or TLS verification."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            try:
                client = await async_validate(
                    self.hass,
                    user_input[CONF_URL],
                    entry.data[CONF_API_TOKEN],
                    user_input[CONF_VERIFY_SSL],
                )
                group_ids = entry.options.get(CONF_GROUP_IDS, [])
                if group_ids and not await client.async_get_host_groups(group_ids):
                    raise FlowError("different_server")
            except FlowError as err:
                errors["base"] = err.error
                placeholders = err.placeholders
            except ZabbixError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error while reconfiguring Zabbix")
                errors["base"] = "unknown"
            else:
                if any(
                    other.unique_id == client.url and other.entry_id != entry.entry_id
                    for other in self._async_current_entries(include_ignore=False)
                ):
                    return self.async_abort(reason="already_configured")
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=client.url,
                    title=_title(client),
                    data_updates={
                        CONF_URL: client.url,
                        CONF_VERIFY_SSL: user_input[CONF_VERIFY_SSL],
                    },
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_URL): URL_SELECTOR,
                vol.Required(CONF_VERIFY_SSL): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                schema, user_input or entry.data
            ),
            errors=errors,
            description_placeholders=placeholders,
        )


class ZabbixOptionsFlow(OptionsFlowWithReload):
    """Change host groups, polling and item selection."""

    def __init__(self) -> None:
        """Initialize the options flow."""
        self._client: ZabbixClient | None = None
        self._options: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options."""
        entry = self.config_entry
        if self._client is None:
            self._client = ZabbixClient(
                entry.data[CONF_URL],
                entry.data[CONF_API_TOKEN],
                async_get_clientsession(
                    self.hass, verify_ssl=entry.data[CONF_VERIFY_SSL]
                ),
            )
        if user_input is not None:
            self._options = {
                **user_input,
                CONF_TAG: user_input[CONF_TAG].strip() or DEFAULT_TAG,
            }
            if (
                user_input[CONF_ITEM_MODE] == ItemMode.ALL
                and entry.options.get(CONF_ITEM_MODE) != ItemMode.ALL
            ):
                return await self.async_step_confirm_all()
            return self.async_create_entry(data=self._options)
        try:
            groups = await self._client.async_get_host_groups()
        except ZabbixError:
            return self.async_abort(reason="cannot_connect")
        options = entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_GROUP_IDS,
                    default=[
                        group_id
                        for group_id in options.get(CONF_GROUP_IDS, [])
                        if group_id in {group.group_id for group in groups}
                    ],
                ): _group_selector(groups),
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.All(
                    NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=MAX_SCAN_INTERVAL,
                            step=1,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Coerce(int),
                ),
            }
        ).extend(_item_schema(options).schema)
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_confirm_all(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Make the user confirm the All items mode."""
        assert self._client is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_CONFIRM]:
                return self.async_create_entry(data=self._options)
            errors["base"] = "confirm_required"
        try:
            placeholders = await async_all_items_placeholders(
                self._client, self._options[CONF_GROUP_IDS]
            )
        except ZabbixError:
            return self.async_abort(reason="cannot_connect")
        return self.async_show_form(
            step_id="confirm_all",
            data_schema=CONFIRM_SCHEMA,
            description_placeholders=placeholders,
            errors=errors,
        )
