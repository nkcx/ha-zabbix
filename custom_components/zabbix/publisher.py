"""Publish Home Assistant states to Zabbix (HA → Zabbix).

Compatible with Home Assistant's built-in Zabbix integration: the same item keys
and low-level discovery, so existing Zabbix hosts and templates keep working.

- Numeric states (and states HA can express as numbers, e.g. on/off → 1/0) are
  sent to ``homeassistant.float[<entity_id>]``; numeric attributes to
  ``homeassistant.float[<entity_id>/<attribute>]``.
- Other states are sent to ``homeassistant.string[<entity_id>]`` when enabled.
- The keys are announced through the trapper discovery items
  ``homeassistant.floats_discovery`` and ``homeassistant.strings_discovery``.

Differences from the built-in integration, all compatible with its template:
the full discovery list is sent at startup and every hour (so Zabbix doesn't
consider items lost after a restart), values carry the time HA recorded them,
and this integration's own entities are never published back to Zabbix.
"""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
import json
import logging
import math
import time
from typing import Any

from homeassistant.const import (
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import entity_registry as er, state as state_helper
from homeassistant.helpers.entityfilter import EntityFilter
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from zabbix_utils import AsyncSender, ItemValue
from zabbix_utils.exceptions import ProcessingError

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

FLOAT = "float"
STRING = "string"

BATCH_SIZE = 100
BATCH_TIMEOUT = 1.0
MAX_TRIES = 3
RETRY_DELAY = 20.0
# Events older than this are dropped when catching up after an outage.
MAX_EVENT_AGE = 30 + MAX_TRIES * RETRY_DELAY
REDISCOVERY_INTERVAL = timedelta(hours=1)
# Zabbix creates discovered items asynchronously, so values sent together with
# the discovery data of a new key are rejected. The current states of entities
# with new keys are sent again after these delays (seconds); the template's
# "discard unchanged" preprocessing drops the duplicates.
RESEND_DELAYS = (30, 90, 300)

_SEND_ERRORS = (
    ProcessingError,
    OSError,
    TimeoutError,
    ValueError,
    asyncio.IncompleteReadError,
)


def discovery_key(item_type: str) -> str:
    """Return the trapper discovery item key for a value type."""
    return f"homeassistant.{item_type}s_discovery"


def value_key(item_type: str, key: str) -> str:
    """Return the item key for a published value."""
    if any(char in key for char in ',]"') or key.startswith(" "):
        key = '"' + key.replace('"', '\\"') + '"'
    return f"homeassistant.{item_type}[{key}]"


@dataclass(slots=True)
class StateValues:
    """The values published for one state."""

    floats: dict[str, float] = field(default_factory=dict)
    strings: dict[str, str] = field(default_factory=dict)


def state_values(state: State, publish_strings: bool) -> StateValues | None:
    """Convert a state like the built-in integration does; None to skip it."""
    if state.state in (STATE_UNKNOWN, "", STATE_UNAVAILABLE):
        return None
    values = StateValues()
    entity_id = state.entity_id
    try:
        values.floats[entity_id] = float(state.state)
    except ValueError:
        try:
            values.floats[entity_id] = float(state_helper.state_as_number(state))
        except ValueError:
            if publish_strings:
                values.strings[entity_id] = str(state.state)
    for name, value in state.attributes.items():
        try:
            number = float(value)
        except ValueError, TypeError:
            continue
        if math.isfinite(number):
            values.floats[f"{entity_id}/{name}"] = number
    return values


@dataclass(slots=True)
class PublisherStats:
    """Counters shown in diagnostics."""

    sent: int = 0
    processed: int = 0
    failed: int = 0
    dropped: int = 0
    lost: int = 0
    last_success: datetime | None = None
    last_error: str | None = None


class ZabbixPublisher:
    """Sends state changes to a Zabbix host through the trapper protocol."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        sender: AsyncSender,
        *,
        host: str,
        entity_filter: EntityFilter,
        publish_strings: bool,
    ) -> None:
        """Initialize the publisher."""
        self.hass = hass
        self.entry_id = entry_id
        self.sender = sender
        self.host = host
        self.entity_filter = entity_filter
        self.publish_strings = publish_strings
        self.stats = PublisherStats()
        self.keys: dict[str, set[str]] = {FLOAT: set(), STRING: set()}
        self._queue: asyncio.Queue[tuple[float, State | None] | None] = asyncio.Queue()
        self._unsubscribe: list[Callable[[], None]] = []
        self._task: asyncio.Task[None] | None = None
        self._rediscover = False
        self._failing = False
        self._rejecting = False
        self._new_entities: set[str] = set()
        self._resend_cancels: set[Callable[[], None]] = set()

    def should_publish(self, entity_id: str) -> bool:
        """Return True if the entity's state is published."""
        if not self.entity_filter(entity_id):
            return False
        entry = er.async_get(self.hass).async_get(entity_id)
        # Never send Zabbix's own data back to Zabbix.
        return entry is None or entry.platform != DOMAIN

    @callback
    def async_start(self) -> None:
        """Start publishing: current states first, then every change."""
        for state in self.hass.states.async_all():
            if self.should_publish(state.entity_id):
                self._queue.put_nowait((time.monotonic(), state))
        self._rediscover = True
        self._unsubscribe.append(
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._async_state_changed)
        )
        self._unsubscribe.append(
            async_track_time_interval(
                self.hass, self._async_request_rediscovery, REDISCOVERY_INTERVAL
            )
        )
        self._task = self.hass.async_create_background_task(
            self._async_run(), f"{DOMAIN} publisher {self.entry_id}"
        )

    async def async_stop(self) -> None:
        """Stop publishing and wait for the worker."""
        for unsubscribe in (*self._unsubscribe, *self._resend_cancels):
            unsubscribe()
        self._unsubscribe.clear()
        self._resend_cancels.clear()
        if self._task is not None:
            self._queue.put_nowait(None)
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except TimeoutError:
                self._task.cancel()
            self._task = None

    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        state = event.data["new_state"]
        if state is not None and self.should_publish(state.entity_id):
            self._queue.put_nowait((time.monotonic(), state))

    @callback
    def _async_request_rediscovery(self, _now: datetime) -> None:
        self._rediscover = True
        self._queue.put_nowait((time.monotonic(), None))

    async def _async_next_batch(self) -> tuple[list[ItemValue], bool]:
        """Collect up to BATCH_SIZE values, waiting BATCH_TIMEOUT for more."""
        metrics: list[ItemValue] = []
        new_keys = False
        first = True
        while len(metrics) < BATCH_SIZE:
            try:
                if first:
                    item = await self._queue.get()
                    first = False
                else:
                    item = await asyncio.wait_for(self._queue.get(), BATCH_TIMEOUT)
            except TimeoutError:
                break
            if item is None:
                return metrics, True
            queued, state = item
            if state is None:  # rediscovery tick
                continue
            if time.monotonic() - queued > MAX_EVENT_AGE:
                self.stats.dropped += 1
                continue
            values = state_values(state, self.publish_strings)
            if values is None:
                continue
            clock = state.last_updated.timestamp()
            for item_type, group in ((FLOAT, values.floats), (STRING, values.strings)):
                for key, value in group.items():
                    if key not in self.keys[item_type]:
                        self.keys[item_type].add(key)
                        self._new_entities.add(state.entity_id)
                        new_keys = True
                    metrics.append(
                        ItemValue(
                            self.host,
                            value_key(item_type, key),
                            str(value),
                            int(clock),
                            int((clock % 1) * 1_000_000_000),
                        )
                    )
        if new_keys:
            self._rediscover = True
        return metrics, False

    def _discovery(self) -> list[ItemValue]:
        """Return the discovery values for every known key."""
        types = (FLOAT, STRING) if self.publish_strings else (FLOAT,)
        return [
            ItemValue(
                self.host,
                discovery_key(item_type),
                json.dumps([{"{#KEY}": key} for key in sorted(self.keys[item_type])]),
            )
            for item_type in types
            if self.keys[item_type]
        ]

    async def _async_run(self) -> None:
        stopping = False
        while not stopping:
            metrics, stopping = await self._async_next_batch()
            if self._rediscover:
                self._rediscover = False
                metrics = self._discovery() + metrics
            if metrics:
                await self._async_send(metrics)
            if self._new_entities:
                self._schedule_resend(self._new_entities)
                self._new_entities = set()

    @callback
    def _schedule_resend(self, entity_ids: set[str]) -> None:
        """Send these entities' current states again once Zabbix has the items."""
        pending = frozenset(entity_ids)
        for delay in RESEND_DELAYS:
            self._call_later(delay, partial(self._async_resend, pending))

    @callback
    def _call_later(self, delay: float, action: Callable[[], None]) -> None:
        cancel: Callable[[], None]

        @callback
        def _fire(_now: datetime) -> None:
            self._resend_cancels.discard(cancel)
            action()

        cancel = async_call_later(self.hass, delay, _fire)
        self._resend_cancels.add(cancel)

    @callback
    def _async_resend(self, entity_ids: frozenset[str]) -> None:
        for entity_id in entity_ids:
            state = self.hass.states.get(entity_id)
            if state is not None and self.should_publish(entity_id):
                self._queue.put_nowait((time.monotonic(), state))

    async def _async_send(self, metrics: list[ItemValue]) -> None:
        """Send values, retrying like the built-in integration."""
        for attempt in range(MAX_TRIES + 1):
            try:
                response = await self.sender.send(metrics)
            except _SEND_ERRORS as err:
                if attempt < MAX_TRIES:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                if not self._failing:
                    _LOGGER.error("Error sending to Zabbix host %s: %s", self.host, err)
                self._failing = True
                self.stats.lost += len(metrics)
                self.stats.last_error = str(err)
                return
            if self._failing:
                _LOGGER.info(
                    "Sending to Zabbix resumed; %d values were lost", self.stats.lost
                )
                self._failing = False
            self.stats.sent += len(metrics)
            self.stats.processed += int(response.processed)
            self.stats.failed += int(response.failed)
            if int(response.processed) == 0 and int(response.failed) > 0:
                # The connection works but Zabbix rejected everything.
                self.stats.last_error = (
                    f"Zabbix rejected all {int(response.failed)} values; check the "
                    f"host {self.host!r}, its template and allowed hosts"
                )
                if not self._rejecting:
                    _LOGGER.warning("%s", self.stats.last_error)
                self._rejecting = True
            elif int(response.processed) > 0:
                # "Success" means Zabbix accepted values, not just the connection.
                self.stats.last_success = datetime.now().astimezone()
                self._rejecting = False
                self.stats.last_error = None
            _LOGGER.debug(
                "Sent %d values to Zabbix: %d processed, %d failed",
                len(metrics),
                response.processed,
                response.failed,
            )
            return

    def diagnostics(self) -> Mapping[str, Any]:
        """Return publisher details for diagnostics."""
        stats = self.stats
        return {
            "host": self.host,
            "publish_strings": self.publish_strings,
            "keys": {item_type: len(keys) for item_type, keys in self.keys.items()},
            "sent": stats.sent,
            "processed": stats.processed,
            "failed": stats.failed,
            "dropped": stats.dropped,
            "lost": stats.lost,
            "last_success": stats.last_success.isoformat()
            if stats.last_success
            else None,
            "last_error": stats.last_error,
        }
