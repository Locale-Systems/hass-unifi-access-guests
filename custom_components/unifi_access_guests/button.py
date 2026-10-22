"""Button: sync now instead of waiting for the next interval."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
    """Add the button."""
    async_add_entities([SyncNowButton(entry.runtime_data, "sync_now")])


class SyncNowButton(GuestsEntity, ButtonEntity):
    """Run a sync pass now."""

    _attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        """Sync now."""
        await self.coordinator.async_request_refresh()
