"""Sensors: the managed visits and when the last sync finished."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import GuestsConfigEntry
from .entity import GuestsEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GuestsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [VisitsSensor(coordinator, "visits"), LastSyncSensor(coordinator, "last_sync")]
    )


class VisitsSensor(GuestsEntity, SensorEntity):
    """Open and upcoming visits on the controller, with the last pass's errors."""

    _attr_native_unit_of_measurement = "visits"

    @property
    def native_value(self) -> int:
        """Number of visits currently on the controller."""
        return len(self.coordinator.data.visits)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The visits (never their PINs) and any per-visit errors."""
        data = self.coordinator.data
        return {
            "visits": [
                {
                    "name": visit.name,
                    "start": visit.start.isoformat(),
                    "end": visit.end.isoformat(),
                    "has_pin": visit.has_pin,
                }
                for visit in data.visits
            ],
            "errors": data.errors,
            "skipped_events": data.skipped_events,
        }


class LastSyncSensor(GuestsEntity, SensorEntity):
    """When the last successful pass finished."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        """Finish time of the last pass."""
        return self.coordinator.data.finished
