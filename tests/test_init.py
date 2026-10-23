"""End to end: a real calendar entity, the coordinator, a fake controller."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.calendar import CalendarEvent
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.unifi_access_guests.sync import parse_marker

from .conftest import FakeAccess, FakeCalendar

VISITS = "sensor.guests_visits"
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=dt_util.UTC)


def event(summary: str, start: datetime, hours: float = 2, *, uid: str,
          description: str | None = None, recurrence_id: str | None = None) -> CalendarEvent:
    return CalendarEvent(start=start, end=start + timedelta(hours=hours), summary=summary,
                         description=description, uid=uid, recurrence_id=recurrence_id)


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def resync(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


@pytest.fixture(autouse=True)
def frozen(freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    freezer.move_to(NOW)
    return freezer


async def test_event_becomes_visitor_with_pin(
    hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar, config_entry
) -> None:
    start = NOW + timedelta(days=1)
    calendar.events = [event("Jane Doe", start, uid="e1", description="<p>PIN: 4821</p>")]
    await setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    [(vid, visitor)] = access.visitors.items()
    assert visitor["first_name"] == "Jane"
    assert visitor["last_name"] == "Doe"
    assert visitor["start_time"] == int(start.timestamp())
    assert visitor["end_time"] == int((start + timedelta(hours=2)).timestamp())
    assert visitor["resources"] == [
        {"id": "door-front", "type": "door"},
        {"id": "door-gate", "type": "door"},
    ]
    assert parse_marker(visitor["remarks"]).entry_id == config_entry.entry_id
    assert access.pins == {vid: "4821"}

    state = hass.states.get(VISITS)
    assert state.state == "1"
    [visit] = state.attributes["visits"]
    assert visit["name"] == "Jane Doe"
    assert visit["has_pin"] is True
    assert "4821" not in str(state.attributes)


async def test_second_pass_changes_nothing(
    hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar, config_entry
) -> None:
    calendar.events = [event("Jane Doe", NOW + timedelta(days=1), uid="e1",
                             description="PIN: 4821")]
    await setup(hass, config_entry)
    access.calls.clear()
    await resync(hass, config_entry)
    assert [c[0] for c in access.calls] == ["GET"]  # just the visitor list
    assert hass.states.get(VISITS).state == "1"


async def test_moved_event_updates_and_removed_event_deletes(
    hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar, config_entry
) -> None:
    calendar.events = [
        event("Jane Doe", NOW + timedelta(days=1), uid="e1"),
        event("Sam Lee", NOW + timedelta(days=2), uid="e2"),
    ]
    await setup(hass, config_entry)
    assert len(access.visitors) == 2
    jane_id = next(k for k, v in access.visitors.items() if v["first_name"] == "Jane")

    moved = NOW + timedelta(days=3)
    calendar.events = [event("Jane Doe", moved, uid="e1")]
    await resync(hass, config_entry)

    assert list(access.visitors) == [jane_id]  # same visitor, updated in place
    assert access.visitors[jane_id]["start_time"] == int(moved.timestamp())
    assert hass.states.get(VISITS).state == "1"


async def test_ended_visit_stays_on_the_controller(
    hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar, config_entry,
    frozen: FrozenDateTimeFactory,
) -> None:
    calendar.events = [event("Jane Doe", NOW + timedelta(hours=1), uid="e1")]
    await setup(hass, config_entry)
    frozen.move_to(NOW + timedelta(hours=4))
    await resync(hass, config_entry)
    assert len(access.visitors) == 1
    assert hass.states.get(VISITS).state == "0"


async def test_all_day_event_runs_local_midnight_to_midnight(
    hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar, config_entry
) -> None:
    await hass.config.async_set_time_zone("America/Los_Angeles")
    calendar.events = [
        CalendarEvent(start=date(2026, 11, 5), end=date(2026, 11, 7), summary="Ann Kim", uid="e1")
    ]
    await setup(hass, config_entry)
    [visitor] = access.visitors.values()
    tz = dt_util.get_time_zone("America/Los_Angeles")
    assert visitor["start_time"] == int(datetime(2026, 11, 5, tzinfo=tz).timestamp())
    assert visitor["end_time"] == int(datetime(2026, 11, 7, tzinfo=tz).timestamp())


async def test_recurring_event_is_one_visitor_that_rolls(
    hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar, config_entry,
    frozen: FrozenDateTimeFactory,
) -> None:
    first, second = NOW + timedelta(days=1), NOW + timedelta(days=8)
    calendar.events = [
        event("Cleaner", start, uid="r1", recurrence_id=f"occ{n}",
              description="NAME: Rosa Diaz\nPIN: 4821")
        for n, start in enumerate((first, second))
    ]
    await setup(hass, config_entry)
    [(vid, visitor)] = access.visitors.items()
    assert (visitor["first_name"], visitor["last_name"]) == ("Rosa", "Diaz")
    assert visitor["start_time"] == int(first.timestamp())
    assert access.pins == {vid: "4821"}
    [visit] = hass.states.get(VISITS).attributes["visits"]
    assert visit["later_occurrences"] == 1

    # After the first occurrence ends, the same visitor moves to the second
    # and keeps its PIN: no new visitor, no PIN request.
    frozen.move_to(first + timedelta(hours=3))
    access.calls.clear()
    await resync(hass, config_entry)
    assert list(access.visitors) == [vid]
    assert access.visitors[vid]["start_time"] == int(second.timestamp())
    assert access.pins == {vid: "4821"}
    assert [c[:2] for c in access.calls] == [("GET", "visitors"), ("PUT", f"visitors/{vid}")]


async def test_same_pin_on_separate_events_shares_a_visitor(
    hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar, config_entry
) -> None:
    calendar.events = [
        event("Jane Doe", NOW + timedelta(days=d), uid=f"e{d}", description="PIN: 4821")
        for d in (1, 8)
    ]
    await setup(hass, config_entry)
    assert len(access.visitors) == 1
    assert list(access.pins.values()) == ["4821"]
    assert hass.states.get(VISITS).attributes["errors"] == []


def notifications(hass: HomeAssistant) -> dict:
    from homeassistant.components import persistent_notification

    return persistent_notification._async_get_or_create_notifications(hass)


async def test_pin_in_use_elsewhere_notifies_until_fixed(
    hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar, config_entry,
    frozen: FrozenDateTimeFactory,
) -> None:
    # A hand-made visitor (not ours) already holds the PIN.
    other = access.add_visitor(first_name="Plumber", last_name="Bob", remarks="")
    access.pins[other] = "4821"
    calendar.events = [event("Jane Doe", NOW + timedelta(days=1), uid="e1",
                             description="PIN: 4821")]
    await setup(hass, config_entry)

    assert list(access.visitors) == [other]  # the new visitor was removed
    [note] = notifications(hass).values()
    assert "Jane Doe" in note["message"]
    assert "PIN already in use" in note["message"]
    assert "4821" not in note["message"]
    assert any("set the PIN" in e for e in hass.states.get(VISITS).attributes["errors"])

    # Unchanged on the next pass: the notification is not raised again.
    raised = note["created_at"]
    frozen.tick(timedelta(minutes=1))  # a re-raise would carry a new time
    await resync(hass, config_entry)
    [note] = notifications(hass).values()
    assert note["created_at"] == raised

    # The event gets another PIN: the visit syncs and the notification clears.
    calendar.events = [event("Jane Doe", NOW + timedelta(days=1), uid="e1",
                             description="PIN: 5555")]
    await resync(hass, config_entry)
    assert notifications(hass) == {}
    assert sorted(access.pins.values()) == ["4821", "5555"]


async def test_unloading_clears_notifications(
    hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar, config_entry
) -> None:
    access.fail["POST visitors"] = "code"
    calendar.events = [event("Jane Doe", NOW + timedelta(days=1), uid="e1")]
    await setup(hass, config_entry)
    assert len(notifications(hass)) == 1
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert notifications(hass) == {}


async def test_unreadable_calendar_changes_nothing(
    hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar, config_entry
) -> None:
    calendar.events = [event("Jane Doe", NOW + timedelta(days=1), uid="e1")]
    await setup(hass, config_entry)
    calendar.error = RuntimeError("Google said no")
    access.calls.clear()
    await resync(hass, config_entry)
    assert len(access.visitors) == 1
    assert access.calls == []
    assert not config_entry.runtime_data.last_update_success


async def test_failed_pin_removes_the_new_visitor_and_retries(
    hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar, config_entry
) -> None:
    calendar.events = [event("Jane Doe", NOW + timedelta(days=1), uid="e1",
                             description="PIN: 4821")]
    access.fail["PUT pin_codes"] = "code"  # a one-off failure
    await setup(hass, config_entry)
    assert access.visitors == {}
    state = hass.states.get(VISITS)
    assert state.state == "0"
    assert any(e.startswith("Jane Doe") and "set the PIN" in e for e in state.attributes["errors"])
    assert len(notifications(hass)) == 1

    await resync(hass, config_entry)
    [vid] = access.visitors
    assert access.pins == {vid: "4821"}
    assert hass.states.get(VISITS).attributes["errors"] == []
    assert notifications(hass) == {}


async def test_one_failure_does_not_stop_the_rest(
    hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar, config_entry
) -> None:
    calendar.events = [
        event("Jane Doe", NOW + timedelta(days=1), uid="e1"),
        event("Sam Lee", NOW + timedelta(days=2), uid="e2"),
    ]
    access.fail["POST visitors"] = 500
    await setup(hass, config_entry)
    assert len(access.visitors) == 1
    assert hass.states.get(VISITS).state == "1"


async def test_rejected_token_starts_reauth(
    hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar, config_entry
) -> None:
    await setup(hass, config_entry)
    access.fail["GET visitors"] = 401
    await resync(hass, config_entry)
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_missing_calendar_retries_setup(
    hass: HomeAssistant, access: FakeAccess, config_entry
) -> None:
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_interval_triggers_sync(
    hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar, config_entry,
    frozen: FrozenDateTimeFactory,
) -> None:
    await setup(hass, config_entry)
    calendar.events = [event("Jane Doe", NOW + timedelta(days=1), uid="e1")]
    frozen.tick(timedelta(minutes=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(access.visitors) == 1


async def test_sync_now_button(
    hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar, config_entry
) -> None:
    await setup(hass, config_entry)
    calendar.events = [event("Jane Doe", NOW + timedelta(days=1), uid="e1")]
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.guests_sync_now"}, blocking=True
    )
    await hass.async_block_till_done()
    assert len(access.visitors) == 1


async def test_removing_entry_deletes_its_open_visitors_only(
    hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar, config_entry,
    frozen: FrozenDateTimeFactory,
) -> None:
    calendar.events = [
        event("Past Visit", NOW + timedelta(hours=1), uid="e0"),
        event("Jane Doe", NOW + timedelta(days=1), uid="e1"),
    ]
    await setup(hass, config_entry)
    frozen.move_to(NOW + timedelta(hours=4))  # Past Visit has ended
    hand_made = access.add_visitor(first_name="Plumber", last_name="Bob", remarks="")

    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert sorted(v["first_name"] for v in access.visitors.values()) == ["Past", "Plumber"]
    assert hand_made in access.visitors


async def test_diagnostics_redacts_secrets(
    hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar, config_entry
) -> None:
    from custom_components.unifi_access_guests.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    calendar.events = [event("Jane Doe", NOW + timedelta(days=1), uid="e1",
                             description="PIN: 4821")]
    await setup(hass, config_entry)
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    text = str(diag)
    assert "test-token" not in text
    assert "0" * 32 not in text
    assert "4821" not in text
    assert diag["last_sync"]["created"] == 1
