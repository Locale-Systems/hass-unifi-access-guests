"""Sync coordinator: read the calendar, plan, apply."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.components.calendar.const import DATA_COMPONENT
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import AccessAuthError, AccessClient, AccessError
from .const import (
    CONF_CALENDAR,
    CONF_DOORS,
    CONF_HORIZON_DAYS,
    CONF_SCAN_MINUTES,
    CONF_SECRET,
    DEFAULT_HORIZON_DAYS,
    DEFAULT_SCAN_MINUTES,
    DOMAIN,
)
from .sync import CalendarItem, Plan, VisitSpec, build_spec, plan, visitor_body

_LOGGER = logging.getLogger(__name__)

type GuestsConfigEntry = ConfigEntry[GuestsCoordinator]


@dataclass(frozen=True, slots=True)
class VisitSummary:
    """One managed visit, as shown on the sensor. Never carries the PIN."""

    name: str
    start: datetime
    end: datetime
    has_pin: bool


@dataclass(slots=True)
class SyncResult:
    """Outcome of one sync pass."""

    finished: datetime
    visits: list[VisitSummary] = field(default_factory=list)
    created: int = 0
    updated: int = 0
    deleted: int = 0
    skipped_events: int = 0
    errors: list[str] = field(default_factory=list)


class CalendarUnavailable(Exception):
    """The configured calendar entity is missing or failed to list events."""


def to_item(event: CalendarEvent) -> CalendarItem:
    """Resolve a calendar event's times to Unix seconds."""
    return CalendarItem(
        summary=event.summary or "",
        description=event.description,
        start=int(event.start_datetime_local.timestamp()),
        end=int(event.end_datetime_local.timestamp()),
        uid=event.uid,
        recurrence_id=event.recurrence_id,
    )


async def fetch_events(
    hass: HomeAssistant, entity_id: str, start: datetime, end: datetime
) -> list[CalendarEvent]:
    """List events overlapping [start, end) from a calendar entity."""
    component = hass.data.get(DATA_COMPONENT)
    entity = component.get_entity(entity_id) if component else None
    if not isinstance(entity, CalendarEntity):
        raise CalendarUnavailable(f"{entity_id} is not a loaded calendar")
    try:
        return await entity.async_get_events(hass, start, end)
    except Exception as err:  # noqa: BLE001 - calendar sources raise anything
        raise CalendarUnavailable(f"{entity_id}: {err}") from err


class GuestsCoordinator(DataUpdateCoordinator[SyncResult]):
    """Each refresh is one full sync pass."""

    config_entry: GuestsConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: GuestsConfigEntry, client: AccessClient
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                minutes=entry.options.get(CONF_SCAN_MINUTES, DEFAULT_SCAN_MINUTES)
            ),
        )
        self.client = client
        self.last_plan: Plan | None = None

    @property
    def calendar_entity_id(self) -> str:
        """The calendar this entry reads."""
        return self.config_entry.data[CONF_CALENDAR]

    async def _async_update_data(self) -> SyncResult:
        entry = self.config_entry
        now = dt_util.now()
        horizon_end = now + timedelta(
            days=entry.options.get(CONF_HORIZON_DAYS, DEFAULT_HORIZON_DAYS)
        )

        # Read both sides before changing anything. If either read fails the
        # pass changes nothing: an unreadable calendar must never look like an
        # empty one, which would delete every upcoming visit.
        try:
            events = await fetch_events(self.hass, self.calendar_entity_id, now, horizon_end)
        except CalendarUnavailable as err:
            raise UpdateFailed(str(err)) from err
        try:
            visitors = await self.client.list_visitors()
        except AccessAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except AccessError as err:
            raise UpdateFailed(str(err)) from err

        door_ids: list[str] = entry.options.get(CONF_DOORS, [])
        desired: list[VisitSpec] = []
        skipped = 0
        for event in events:
            spec = build_spec(self.calendar_entity_id, to_item(event), door_ids)
            if spec is None:
                skipped += 1
            else:
                desired.append(spec)

        work = plan(
            entry_id=entry.entry_id,
            secret=entry.data[CONF_SECRET],
            desired=desired,
            visitors=visitors,
            now=int(now.timestamp()),
            horizon_end=int(horizon_end.timestamp()),
        )
        self.last_plan = work
        result = SyncResult(finished=now, skipped_events=skipped)
        try:
            await self._apply(work, result)
        except AccessAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err

        result.visits.extend(_summary(spec) for _, spec in work.unchanged)
        result.visits.sort(key=lambda v: v.start)
        result.finished = dt_util.now()
        for error in result.errors:
            _LOGGER.warning("Sync of %s: %s", self.calendar_entity_id, error)
        return result

    async def _apply(self, work: Plan, result: SyncResult) -> None:
        """Carry out a plan. One failed visitor never stops the others.

        Deletes go first, so a PIN freed by a removed visit is available to a
        visit created in the same pass.
        """
        entry_id = self.config_entry.entry_id
        secret = self.config_entry.data[CONF_SECRET]

        for visitor in work.deletes:
            try:
                await self.client.delete_visitor(visitor.id)
                result.deleted += 1
            except AccessAuthError:
                raise
            except AccessError as err:
                result.errors.append(f"delete {visitor.first_name} {visitor.last_name}: {err}")

        for update in work.updates:
            spec = update.spec
            try:
                # The PIN goes first: the remarks written next record the new
                # PIN, so if the PIN fails the old remarks make the next pass
                # retry it.
                if update.set_pin:
                    await self.client.set_visitor_pin(update.visitor.id, spec.pin)
                await self.client.update_visitor(
                    update.visitor.id, visitor_body(entry_id, spec, secret)
                )
                result.updated += 1
                result.visits.append(_summary(spec))
            except AccessAuthError:
                raise
            except AccessError as err:
                result.errors.append(f"update {spec.name}: {err}")

        for spec in work.creates:
            try:
                visitor_id = await self.client.create_visitor(
                    visitor_body(entry_id, spec, secret)
                )
            except AccessAuthError:
                raise
            except AccessError as err:
                result.errors.append(f"create {spec.name}: {err}")
                continue
            if spec.pin:
                try:
                    await self.client.set_visitor_pin(visitor_id, spec.pin)
                except AccessError as err:
                    # Its remarks already record the PIN, so leaving it would
                    # hide the failure from later passes. Remove it and let
                    # the next pass create it again.
                    result.errors.append(f"PIN for {spec.name}: {err}")
                    try:
                        await self.client.delete_visitor(visitor_id)
                    except AccessError as cleanup_err:
                        result.errors.append(
                            f"remove {spec.name} after PIN failure: {cleanup_err}"
                        )
                    if isinstance(err, AccessAuthError):
                        raise
                    continue
            result.created += 1
            result.visits.append(_summary(spec))


def _summary(spec: VisitSpec) -> VisitSummary:
    return VisitSummary(
        name=spec.name,
        start=dt_util.utc_from_timestamp(spec.start),
        end=dt_util.utc_from_timestamp(spec.end),
        has_pin=spec.pin is not None,
    )
