"""Receive Zabbix alerts through a Home Assistant webhook.

Zabbix sends a notification (via a webhook media type and a trigger action) when
a problem starts, resolves or is updated. The integration then refreshes from the
Zabbix API right away, so problem events and entities update within seconds
instead of on the next poll. The data itself always comes from the API, so it's
exactly what polling would show.

The ha-zabbix-alerts companion integration reads ``DATA_ALERT_WEBHOOK_ID`` and
``DATA_ALERT_SECRET`` from this integration's config entry data to configure
Zabbix; keep those keys stable.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from hmac import compare_digest
from http import HTTPStatus
import secrets
from typing import Any

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LOGGER

DATA_ALERT_WEBHOOK_ID = "alert_webhook_id"
DATA_ALERT_SECRET = "alert_secret"
SECRET_HEADER = "X-Zabbix-Alert-Secret"


def new_alert_credentials() -> dict[str, str]:
    """Return a new webhook id and shared secret."""
    return {
        DATA_ALERT_WEBHOOK_ID: webhook.async_generate_id(),
        DATA_ALERT_SECRET: secrets.token_hex(32),
    }


@callback
def alert_url(hass: HomeAssistant, webhook_id: str) -> str:
    """Return the webhook URL Zabbix should call (the internal URL if set)."""
    return webhook.async_generate_url(hass, webhook_id, prefer_external=False)


@dataclass(slots=True)
class AlertStats:
    """What the receiver has seen, for the sensor and diagnostics."""

    received: int = 0
    rejected: int = 0
    last_received: datetime | None = None
    last_event: dict[str, Any] | None = None


class AlertReceiver:
    """Registers the webhook and triggers a refresh on every alert."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_title: str,
        webhook_id: str,
        secret: str,
        refresh: Callable[[], Awaitable[None]],
    ) -> None:
        """Initialize the receiver; ``refresh`` requests a coordinator refresh."""
        self.hass = hass
        self.webhook_id = webhook_id
        self._secret = secret
        self._entry_title = entry_title
        self._refresh = refresh
        self.stats = AlertStats()
        self._listeners: list[Callable[[], None]] = []

    @callback
    def async_register(self) -> None:
        """Register the webhook.

        Not ``local_only``: Zabbix often reaches Home Assistant over a global
        IPv6 address, which Home Assistant doesn't treat as local. Every request
        must carry the 64-character shared secret instead.
        """
        webhook.async_register(
            self.hass,
            DOMAIN,
            f"Zabbix alerts ({self._entry_title})",
            self.webhook_id,
            self._async_handle,
            allowed_methods=["POST"],
        )

    @callback
    def async_unregister(self) -> None:
        """Unregister the webhook."""
        webhook.async_unregister(self.hass, self.webhook_id)

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call ``listener`` after every accepted alert; return a remover."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    async def _async_handle(
        self, hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> web.Response:
        if not compare_digest(request.headers.get(SECRET_HEADER, ""), self._secret):
            self.stats.rejected += 1
            LOGGER.warning(
                "Rejected a Zabbix alert with a missing or wrong secret from %s",
                request.remote,
            )
            return web.Response(status=HTTPStatus.UNAUTHORIZED)
        try:
            payload = await request.json()
        except ValueError:
            payload = None
        event = (
            {
                key: str(payload[key])
                for key in ("event_id", "event_value", "event_update_status")
                if key in payload
            }
            if isinstance(payload, dict)
            else {}
        )
        self.stats.received += 1
        self.stats.last_received = dt_util.utcnow()
        self.stats.last_event = event
        LOGGER.debug("Zabbix alert received: %s", event)
        for listener in list(self._listeners):
            listener()
        await self._refresh()
        return web.json_response({"ok": True})

    def diagnostics(self) -> dict[str, Any]:
        """Return receiver details for diagnostics."""
        stats = self.stats
        return {
            "received": stats.received,
            "rejected": stats.rejected,
            "last_received": stats.last_received.isoformat()
            if stats.last_received
            else None,
            "last_event": stats.last_event,
        }
