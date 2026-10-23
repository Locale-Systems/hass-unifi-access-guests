"""UniFi Access Guests: turn calendar events into UniFi Access visitors."""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .api import AccessClient, AccessError
from .const import CONF_API_TOKEN, CONF_HOST, CONF_VERIFY_SSL
from .coordinator import GuestsConfigEntry, GuestsCoordinator, retire
from .sync import CANCELLED, parse_marker

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.SENSOR]


def build_client(hass: HomeAssistant, data: dict) -> AccessClient:
    """Client for a config entry's (or a flow's) connection settings."""
    session = async_get_clientsession(hass, verify_ssl=data[CONF_VERIFY_SSL])
    return AccessClient(data[CONF_HOST], data[CONF_API_TOKEN], session)


async def async_setup_entry(hass: HomeAssistant, entry: GuestsConfigEntry) -> bool:
    """Set up from a config entry."""
    coordinator = GuestsCoordinator(hass, entry, build_client(hass, entry.data))
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GuestsConfigEntry) -> bool:
    """Unload a config entry."""
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        entry.runtime_data.dismiss_notifications()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: GuestsConfigEntry) -> None:
    """Retire this entry's open and upcoming visitors on the controller.

    A re-added entry gets a new id and never touches these again, so they
    must not stay live: each one's PIN is removed and its visit cancelled.
    They are kept rather than deleted, because deleting a visitor leaves
    its door group behind (LOC-304). Ended and already-cancelled visits
    stay as they are. Best effort: the controller may be unreachable.
    """
    client = build_client(hass, entry.data)
    now = int(dt_util.now().timestamp())
    try:
        visitors = await client.list_visitors()
    except AccessError as err:
        _LOGGER.warning(
            "Could not cancel the visits created by %s: %s", entry.title, err
        )
        return
    for visitor in visitors:
        marker = parse_marker(visitor.remarks)
        if marker is None or marker.entry_id != entry.entry_id:
            continue
        if visitor.end_time is not None and visitor.end_time <= now:
            continue
        if visitor.status == CANCELLED:
            continue
        try:
            await retire(client, visitor)
        except AccessError as err:
            _LOGGER.warning(
                "Could not cancel the visit of %s %s: %s",
                visitor.first_name,
                visitor.last_name,
                err,
            )
