"""Diagnostics: settings and the last plan, without secrets or PINs."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_API_TOKEN, CONF_SECRET
from .coordinator import GuestsConfigEntry

TO_REDACT = {CONF_API_TOKEN, CONF_SECRET}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: GuestsConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    work = coordinator.last_plan
    result = coordinator.data

    def visitor(v) -> dict[str, Any]:
        return {
            "id": v.id,
            "start_time": v.start_time,
            "end_time": v.end_time,
            "status": v.status,
            "remarks": v.remarks,
        }

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "last_sync": {
            "finished": result.finished.isoformat() if result else None,
            "created": result.created if result else None,
            "updated": result.updated if result else None,
            "deleted": result.deleted if result else None,
            "skipped_events": result.skipped_events if result else None,
            "errors": [e.describe() for e in result.errors] if result else None,
        },
        "last_plan": None
        if work is None
        else {
            "creates": [s.key for s in work.creates],
            "updates": [{"visitor": visitor(v), "key": s.key} for v, s in work.updates],
            "deletes": [visitor(v) for v in work.deletes],
            "unchanged": [visitor(v) for v, _ in work.unchanged],
            "ended": [visitor(v) for v in work.ended],
        },
    }
