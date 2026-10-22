"""The pure event-to-visitor logic."""

from __future__ import annotations

from custom_components.unifi_access_guests.api import Visitor
from custom_components.unifi_access_guests.sync import (
    CalendarItem,
    VisitSpec,
    build_spec,
    event_key,
    make_remarks,
    parse_marker,
    parse_pin,
    pin_digest,
    plan,
    split_name,
)

ENTRY = "01JTESTENTRY0000000000000A"
SECRET = "s" * 32
CAL = "calendar.guests"
NOW = 1_800_000_000
HOUR = 3600
HORIZON = NOW + 30 * 24 * HOUR


def item(summary="Jane Doe", start=NOW + HOUR, end=NOW + 2 * HOUR, *, uid="uid-1",
         recurrence_id=None, description=None) -> CalendarItem:
    return CalendarItem(summary=summary, description=description, start=start,
                        end=end, uid=uid, recurrence_id=recurrence_id)


def spec(**kwargs) -> VisitSpec:
    built = build_spec(CAL, item(**kwargs), ["door-b", "door-a"])
    assert built is not None
    return built


def visitor_for(s: VisitSpec, *, vid="v1", entry=ENTRY, start=None, end=None,
                first=None, remarks=None) -> Visitor:
    data = {
        "id": vid,
        "first_name": first or s.first_name,
        "last_name": s.last_name,
        "remarks": remarks if remarks is not None else make_remarks(entry, s, SECRET),
        "start_time": s.start if start is None else start,
        "end_time": s.end if end is None else end,
        "status": "UPCOMING",
    }
    return Visitor.from_api(data)


def run(desired, visitors, now=NOW, horizon_end=HORIZON):
    return plan(entry_id=ENTRY, secret=SECRET, desired=desired, visitors=visitors,
                now=now, horizon_end=horizon_end)


# --- parsing --------------------------------------------------------------


def test_split_name() -> None:
    assert split_name("Jane Doe") == ("Jane", "Doe")
    assert split_name("  Mary  Ann  Smith ") == ("Mary", "Ann Smith")
    assert split_name("Cher") == ("Cher", "Guest")
    assert split_name("   ") == ("", "")


def test_parse_pin() -> None:
    assert parse_pin("PIN: 4821") == "4821"
    assert parse_pin("Arrives late.\npin=123456\nThanks") == "123456"
    assert parse_pin("Gate PIN #9988") == "9988"
    # Google often returns HTML descriptions.
    assert parse_pin("<p>Hi</p><b>PIN:</b> 5150<br>") == "5150"
    assert parse_pin("PIN: 12") is None  # too short
    assert parse_pin("SPIN: 4821") is None  # not the word PIN
    assert parse_pin(None) is None
    assert parse_pin("no code here") is None


def test_build_spec_rejects_unusable_events() -> None:
    assert build_spec(CAL, item(summary="  "), ["d"]) is None
    assert build_spec(CAL, item(start=NOW, end=NOW), ["d"]) is None


def test_build_spec_sorts_doors_and_reads_pin() -> None:
    s = spec(description="PIN: 4821")
    assert s.door_ids == ("door-a", "door-b")
    assert s.pin == "4821"
    assert (s.first_name, s.last_name) == ("Jane", "Doe")


def test_event_key_follows_uid_not_time_or_title() -> None:
    moved = item(start=NOW + 5 * HOUR, end=NOW + 6 * HOUR, summary="Jane Q Doe")
    assert event_key(CAL, item()) == event_key(CAL, moved)
    assert event_key(CAL, item()) != event_key("calendar.other", item())
    # Each occurrence of a recurring event is its own visit.
    assert event_key(CAL, item(recurrence_id="20261101")) != event_key(
        CAL, item(recurrence_id="20261108")
    )


def test_event_key_without_uid_uses_title_and_start() -> None:
    a = item(uid=None)
    assert event_key(CAL, a) == event_key(CAL, item(uid=None))
    assert event_key(CAL, a) != event_key(CAL, item(uid=None, start=NOW + 2))


def test_marker_round_trip_and_hides_pin() -> None:
    s = spec(description="PIN: 4821")
    remarks = make_remarks(ENTRY, s, SECRET)
    assert "4821" not in remarks
    marker = parse_marker(remarks)
    assert marker is not None
    assert (marker.entry_id, marker.key, marker.spec) == (ENTRY, s.key, s.spec_digest())
    assert marker.pin == pin_digest(SECRET, "4821")
    assert parse_marker("Visiting for the interview") is None
    assert parse_marker("") is None


def test_pin_digest_is_keyed() -> None:
    assert pin_digest("a" * 32, "4821") != pin_digest("b" * 32, "4821")
    assert pin_digest(SECRET, None) == pin_digest(SECRET, "")


# --- planning -------------------------------------------------------------


def test_new_event_is_created() -> None:
    s = spec()
    result = run([s], [])
    assert result.creates == [s]
    assert not result.updates and not result.deletes


def test_matching_visitor_is_unchanged() -> None:
    s = spec()
    result = run([s], [visitor_for(s)])
    assert result.is_empty
    assert len(result.unchanged) == 1


def test_moved_event_updates_its_visitor() -> None:
    old = spec()
    new = spec(start=NOW + 3 * HOUR, end=NOW + 4 * HOUR)
    result = run([new], [visitor_for(old)])
    assert [u.spec for u in result.updates] == [new]
    assert not result.updates[0].set_pin
    assert not result.creates and not result.deletes


def test_door_change_updates_via_spec_digest() -> None:
    s = spec()
    narrowed = build_spec(CAL, item(), ["door-a"])
    result = run([narrowed], [visitor_for(s)])
    assert [u.spec for u in result.updates] == [narrowed]


def test_edit_on_the_controller_is_reverted() -> None:
    s = spec()
    result = run([s], [visitor_for(s, end=s.end + HOUR)])
    assert [u.spec for u in result.updates] == [s]
    result = run([s], [visitor_for(s, first="Janet")])
    assert [u.spec for u in result.updates] == [s]


def test_pin_change_sets_pin() -> None:
    old = spec(description="PIN: 1111")
    new = spec(description="PIN: 2222")
    result = run([new], [visitor_for(old)])
    assert result.updates[0].set_pin
    removed = spec()
    result = run([removed], [visitor_for(old)])
    assert result.updates[0].set_pin
    assert result.updates[0].spec.pin is None


def test_removed_upcoming_event_deletes_visitor() -> None:
    s = spec()
    v = visitor_for(s)
    result = run([], [v])
    assert result.deletes == [v]


def test_removed_event_in_progress_deletes_visitor() -> None:
    s = spec(start=NOW - HOUR, end=NOW + HOUR)
    v = visitor_for(s)
    assert run([], [v]).deletes == [v]


def test_ended_visit_is_kept_as_history() -> None:
    s = spec(start=NOW - 3 * HOUR, end=NOW - HOUR)
    v = visitor_for(s)
    # Still on the calendar or not, an ended visit is left alone.
    for desired in ([s], []):
        result = run(desired, [v])
        assert result.is_empty
        assert result.ended == [v]


def test_visit_past_the_horizon_is_left_alone() -> None:
    s = spec(start=HORIZON + HOUR, end=HORIZON + 2 * HOUR)
    v = visitor_for(s)
    result = run([], [v])
    assert result.is_empty


def test_visitors_of_other_entries_or_people_are_ignored() -> None:
    s = spec()
    other_entry = visitor_for(s, vid="v2", entry="01JOTHERENTRY000000000000B")
    hand_made = visitor_for(s, vid="v3", remarks="Plumber, let in at 9")
    result = run([], [other_entry, hand_made])
    assert result.is_empty
    result = run([s], [other_entry, hand_made])
    assert result.creates == [s]


def test_duplicate_visitor_for_one_event_is_deleted() -> None:
    s = spec()
    first, second = visitor_for(s, vid="v1"), visitor_for(s, vid="v2")
    result = run([s], [first, second])
    assert result.deletes == [second]
    assert len(result.unchanged) == 1
    assert not result.creates
