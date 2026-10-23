"""Sync coordinator: read the calendar, plan, apply, report."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging

from homeassistant.components import persistent_notification
from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.components.calendar.const import DATA_COMPONENT
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import AccessAuthError, AccessClient, AccessError, Visitor
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
from .sync import CalendarItem, Plan, VisitSpec, build_specs, parse_marker, plan, visitor_body

_LOGGER = logging.getLogger(__name__)

type GuestsConfigEntry = ConfigEntry[GuestsCoordinator]


@dataclass(frozen=True, slots=True)
class VisitSummary:
    """One managed visit, as shown on the sensor. Never carries the PIN."""

    name: str
    start: datetime
    end: datetime
    has_pin: bool
    later: int


@dataclass(frozen=True, slots=True)
class VisitError:
    """A guest the last pass could not bring in line."""

    key: str
    name: str
    start: datetime | None
    action: str
    reason: str

    def describe(self) -> str:
        """One line for the sensor and the log."""
        when = f" ({dt_util.as_local(self.start):%a %d %b %H:%M})" if self.start else ""
        return f"{self.name}{when}: could not {self.action}: {self.reason}"


@dataclass(slots=True)
class SyncResult:
    """Outcome of one sync pass."""

    finished: datetime
    visits: list[VisitSummary] = field(default_factory=list)
    created: int = 0
    updated: int = 0
    deleted: int = 0
    skipped_events: int = 0
    errors: list[VisitError] = field(default_factory=list)


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


def _ts(seconds: int) -> datetime:
    return dt_util.utc_from_timestamp(seconds)


def _summary(spec: VisitSpec) -> VisitSummary:
    return VisitSummary(
        name=spec.name,
        start=_ts(spec.start),
        end=_ts(spec.end),
        has_pin=spec.pin is not None,
        later=spec.later,
    )


def _spec_error(spec: VisitSpec, action: str, err: AccessError) -> VisitError:
    return VisitError(spec.key, spec.name, _ts(spec.start), action, err.reason)


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
        # Guest key -> text of the notification currently shown for it.
        self._notified: dict[str, str] = {}

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

        desired, skipped = build_specs(
            calendar_entity_id=self.calendar_entity_id,
            items=[to_item(event) for event in events],
            door_ids=entry.options.get(CONF_DOORS, []),
            secret=entry.data[CONF_SECRET],
            now=int(now.timestamp()),
        )
        work = plan(
            entry_id=entry.entry_id,
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
            _LOGGER.warning("Sync of %s: %s", self.calendar_entity_id, error.describe())
        self._notify(result.errors)
        return result

    async def _apply(self, work: Plan, result: SyncResult) -> None:
        """Carry out a plan. One failed visitor never stops the others.

        Deletes go first, so a PIN freed by a removed guest is available to a
        guest created in the same pass.
        """
        entry_id = self.config_entry.entry_id

        for visitor in work.deletes:
            try:
                await self.client.delete_visitor(visitor.id)
                result.deleted += 1
            except AccessAuthError:
                raise
            except AccessError as err:
                result.errors.append(_visitor_error(visitor, err))

        for visitor, spec in work.updates:
            try:
                await self.client.update_visitor(visitor.id, visitor_body(entry_id, spec))
                result.updated += 1
                result.visits.append(_summary(spec))
            except AccessAuthError:
                raise
            except AccessError as err:
                result.errors.append(_spec_error(spec, "update the visit", err))

        for spec in work.creates:
            try:
                visitor_id = await self.client.create_visitor(visitor_body(entry_id, spec))
            except AccessAuthError:
                raise
            except AccessError as err:
                result.errors.append(_spec_error(spec, "create the visit", err))
                continue
            if spec.pin:
                try:
                    await self.client.set_visitor_pin(visitor_id, spec.pin)
                except AccessError as err:
                    # The guest key stands for the PIN, so a visitor left
                    # without it would look correct to every later pass.
                    # Remove it and let the next pass create it again.
                    result.errors.append(_spec_error(spec, "set the PIN", err))
                    try:
                        await self.client.delete_visitor(visitor_id)
                    except AccessError as cleanup_err:
                        result.errors.append(
                            _spec_error(spec, "remove the visit after the PIN failed", cleanup_err)
                        )
                    if isinstance(err, AccessAuthError):
                        raise
                    continue
            result.created += 1
            result.visits.append(_summary(spec))

    @callback
    def _notify(self, errors: list[VisitError]) -> None:
        """One notification per guest with an error; cleared once it syncs.

        A notification is only re-raised when its text changes, so an error
        that persists does not reappear every pass.
        """
        current: dict[str, list[VisitError]] = {}
        for error in errors:
            current.setdefault(error.key, []).append(error)

        for key in list(self._notified):
            if key not in current:
                persistent_notification.async_dismiss(self.hass, self._notification_id(key))
                del self._notified[key]

        for key, guest_errors in current.items():
            message = "\n".join(f"- {e.describe()}" for e in guest_errors)
            if self._notified.get(key) == message:
                continue
            persistent_notification.async_create(
                self.hass,
                f"Calendar **{self.config_entry.title}** could not sync a visit to "
                f"UniFi Access:\n\n{message}\n\nFix the event (for example, give it a "
                "PIN no one else uses); this clears once the visit syncs.",
                title="UniFi Access guest not synced",
                notification_id=self._notification_id(key),
            )
            self._notified[key] = message

    @callback
    def dismiss_notifications(self) -> None:
        """Clear every notification this entry raised (on unload)."""
        for key in self._notified:
            persistent_notification.async_dismiss(self.hass, self._notification_id(key))
        self._notified.clear()

    def _notification_id(self, key: str) -> str:
        return f"{DOMAIN}_{self.config_entry.entry_id}_{key}"


def _visitor_error(visitor: Visitor, err: AccessError) -> VisitError:
    marker = parse_marker(visitor.remarks)
    start = _ts(visitor.start_time) if visitor.start_time is not None else None
    return VisitError(
        marker.key if marker else visitor.id,
        f"{visitor.first_name} {visitor.last_name}",
        start,
        "remove the visit",
        err.reason,
    )
