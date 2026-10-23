"""The pure event-to-visitor logic."""

from __future__ import annotations

from custom_components.unifi_access_guests.api import Visitor
from custom_components.unifi_access_guests.sync import (
    CalendarItem,
    VisitSpec,
    build_specs,
    guest_key,
    make_remarks,
    parse_marker,
    parse_name,
    parse_pin,
    plan,
    split_name,
)

ENTRY = "01JTESTENTRY0000000000000A"
SECRET = "s" * 32
CAL = "calendar.guests"
NOW = 1_800_000_000
HOUR = 3600
DAY = 24 * HOUR
HORIZON = NOW + 30 * DAY


def item(summary="Jane Doe", start=NOW + HOUR, end=None, *, uid="uid-1",
         recurrence_id=None, description=None) -> CalendarItem:
    return CalendarItem(summary=summary, description=description, start=start,
                        end=start + HOUR if end is None else end, uid=uid,
                        recurrence_id=recurrence_id)


def specs(*items: CalendarItem, now: int = NOW, doors=("door-b", "door-a")):
    return build_specs(calendar_entity_id=CAL, items=list(items), door_ids=list(doors),
                       secret=SECRET, now=now)


def spec(**kwargs) -> VisitSpec:
    [built], _ = specs(item(**kwargs))
    return built


def visitor_for(s: VisitSpec, *, vid="v1", entry=ENTRY, start=None, end=None,
                first=None, remarks=None) -> Visitor:
    return Visitor.from_api({
        "id": vid,
        "first_name": first or s.first_name,
        "last_name": s.last_name,
        "remarks": remarks if remarks is not None else make_remarks(entry, s),
        "start_time": s.start if start is None else start,
        "end_time": s.end if end is None else end,
        "status": "UPCOMING",
    })


def run(desired, visitors, now=NOW, horizon_end=HORIZON):
    return plan(entry_id=ENTRY, desired=desired, visitors=visitors, now=now,
                horizon_end=horizon_end)


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


def test_parse_name() -> None:
    assert parse_name("NAME: Jane Doe") == "Jane Doe"
    assert parse_name("Notes\nname = Sam  Lee \nmore") == "Sam Lee"
    # Stops before a PIN on the same line.
    assert parse_name("NAME: Ann Kim PIN: 4821") == "Ann Kim"
    # HTML: the name ends with its paragraph, and entities decode.
    assert parse_name("<p>NAME: Zo&euml; Hart</p><p>PIN: 4821</p>") == "Zoë Hart"
    assert parse_name("<b>Name:</b>&nbsp;Bo Ray<br>PIN: 1234") == "Bo Ray"
    assert parse_name("<p>NAME: Jane Doe</p><p>Arrives at 3</p>") == "Jane Doe"
    assert parse_name("NAME: Jane Doe<br/>Gate code is on the fridge") == "Jane Doe"
    # Needs the colon, and the whole word.
    assert parse_name("Name the dog") is None
    assert parse_name("USERNAME: jdoe") is None
    # Never reaches onto the next line.
    assert parse_name("NAME:\nJane Doe") is None
    assert parse_name(None) is None


def test_name_line_wins_over_title() -> None:
    s = spec(summary="Cleaning", description="NAME: Rosa Diaz\nPIN: 4821")
    assert (s.first_name, s.last_name, s.pin) == ("Rosa", "Diaz", "4821")
    s = spec(summary="Cleaning", description="NAME:   ")
    assert (s.first_name, s.last_name) == ("Cleaning", "Guest")


def test_unusable_events_are_skipped() -> None:
    result, skipped = specs(item(summary="  "), item(start=NOW + HOUR, end=NOW + HOUR, uid="u2"))
    assert result == [] and skipped == 2


def test_ended_events_are_dropped_not_skipped() -> None:
    result, skipped = specs(item(start=NOW - 2 * HOUR, end=NOW - HOUR))
    assert result == [] and skipped == 0


def test_doors_sorted() -> None:
    assert spec().door_ids == ("door-a", "door-b")


# --- grouping and the rolling window -------------------------------------


def weekly(n: int, *, description=None, start=NOW + HOUR, summary="Cleaner"):
    return [
        item(summary=summary, start=start + w * 7 * DAY, uid="series-1",
             recurrence_id=f"week{w}", description=description)
        for w in range(n)
    ]


def test_series_is_one_guest_holding_the_next_occurrence() -> None:
    [s], _ = specs(*weekly(4))
    assert (s.start, s.end) == (NOW + HOUR, NOW + 2 * HOUR)
    assert s.later == 3


def test_window_rolls_to_the_next_occurrence() -> None:
    occurrences = weekly(3)
    [first], _ = specs(*occurrences)
    [rolled], _ = specs(*occurrences, now=NOW + 3 * HOUR)
    assert rolled.key == first.key
    assert (rolled.start, rolled.later) == (NOW + HOUR + 7 * DAY, 1)


def test_open_occurrence_is_the_window() -> None:
    [s], _ = specs(*weekly(2), now=NOW + HOUR + 60)
    assert s.start == NOW + HOUR  # already started, still open


def test_same_pin_groups_separate_events() -> None:
    a = item(summary="Jane Doe", uid="a", description="PIN: 4821")
    b = item(summary="Jane Doe", start=NOW + 3 * DAY, uid="b", description="PIN: 4821")
    c = item(summary="Sam Lee", uid="c", description="PIN: 1111")
    result, _ = specs(a, b, c)
    assert sorted((s.name, s.later) for s in result) == [("Jane Doe", 1), ("Sam Lee", 0)]


def test_without_a_pin_separate_events_stay_separate() -> None:
    a = item(uid="a")
    b = item(start=NOW + 3 * DAY, uid="b")
    result, _ = specs(a, b)
    assert len(result) == 2


def test_overlapping_and_touching_occurrences_merge() -> None:
    pin = "PIN: 4821"
    a = item(start=NOW + HOUR, end=NOW + 3 * HOUR, uid="a", description=pin)
    b = item(start=NOW + 2 * HOUR, end=NOW + 4 * HOUR, uid="b", description=pin)
    c = item(start=NOW + 4 * HOUR, end=NOW + 5 * HOUR, uid="c", description=pin)
    d = item(start=NOW + 9 * HOUR, end=NOW + 10 * HOUR, uid="d", description=pin)
    [s], _ = specs(d, c, b, a)
    assert (s.start, s.end, s.later) == (NOW + HOUR, NOW + 5 * HOUR, 1)


def test_guest_named_by_its_current_occurrence() -> None:
    a = item(summary="Jane Doe", uid="a", description="PIN: 4821")
    b = item(summary="Jane Q Doe", start=NOW + 3 * DAY, uid="b", description="PIN: 4821")
    [s], _ = specs(b, a)
    assert s.name == "Jane Doe"


def test_guest_key() -> None:
    a = item(uid="a")
    # A series keeps its key whatever its title or time.
    assert guest_key(CAL, a, None, SECRET) == guest_key(
        CAL, item(uid="a", summary="X Y", start=NOW + DAY), None, SECRET
    )
    assert guest_key(CAL, a, None, SECRET) != guest_key("calendar.other", a, None, SECRET)
    # Without a uid, title and start stand in.
    no_uid = item(uid=None)
    assert guest_key(CAL, no_uid, None, SECRET) != guest_key(
        CAL, item(uid=None, start=NOW + 2), None, SECRET
    )
    # A PIN outranks the series, and its key is keyed by the entry secret.
    assert guest_key(CAL, a, "4821", SECRET) == guest_key(CAL, item(uid="z"), "4821", SECRET)
    assert guest_key(CAL, a, "4821", SECRET) != guest_key(CAL, a, "4821", "t" * 32)
    assert guest_key(CAL, a, "4821", SECRET) != guest_key(CAL, a, None, SECRET)


def test_marker_round_trip_and_hides_pin() -> None:
    s = spec(description="PIN: 4821")
    remarks = make_remarks(ENTRY, s)
    assert "4821" not in remarks
    marker = parse_marker(remarks)
    assert (marker.entry_id, marker.key, marker.spec) == (ENTRY, s.key, s.spec_digest())
    assert parse_marker("Visiting for the interview") is None
    assert parse_marker("") is None


# --- planning -------------------------------------------------------------


def test_new_guest_is_created() -> None:
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
    new = spec(start=NOW + 3 * HOUR)
    result = run([new], [visitor_for(old)])
    assert result.updates == [(visitor_for(old), new)]
    assert not result.creates and not result.deletes


def test_ended_visitor_rolls_to_the_next_occurrence() -> None:
    occurrences = weekly(2)
    [first], _ = specs(*occurrences)
    later = NOW + 3 * HOUR
    [rolled], _ = specs(*occurrences, now=later)
    result = run([rolled], [visitor_for(first)], now=later)
    assert [s for _, s in result.updates] == [rolled]
    assert not result.creates and not result.ended


def test_door_change_updates_via_spec_digest() -> None:
    s = spec()
    [narrowed], _ = specs(item(), doors=["door-a"])
    result = run([narrowed], [visitor_for(s)])
    assert [n for _, n in result.updates] == [narrowed]


def test_edit_on_the_controller_is_reverted() -> None:
    s = spec()
    assert [n for _, n in run([s], [visitor_for(s, end=s.end + HOUR)]).updates] == [s]
    assert [n for _, n in run([s], [visitor_for(s, first="Janet")]).updates] == [s]


def test_changed_pin_replaces_the_visitor() -> None:
    old = spec(description="PIN: 1111")
    new = spec(description="PIN: 2222")
    v = visitor_for(old)
    result = run([new], [v])
    assert result.deletes == [v]
    assert result.creates == [new]


def test_removed_upcoming_guest_is_deleted() -> None:
    v = visitor_for(spec())
    assert run([], [v]).deletes == [v]


def test_removed_guest_in_progress_is_deleted() -> None:
    v = visitor_for(spec(start=NOW - HOUR, end=NOW + HOUR))
    assert run([], [v]).deletes == [v]


def test_ended_visit_with_nothing_left_is_kept_as_history() -> None:
    s = VisitSpec("a" * 16, "Jane", "Doe", NOW - 3 * HOUR, NOW - HOUR, ("d",), None)
    v = visitor_for(s)
    result = run([], [v])
    assert result.is_empty
    assert result.ended == [v]


def test_visit_past_the_horizon_is_left_alone() -> None:
    v = visitor_for(spec(start=HORIZON + HOUR))
    assert run([], [v]).is_empty


def test_visitors_of_other_entries_or_people_are_ignored() -> None:
    s = spec()
    other_entry = visitor_for(s, vid="v2", entry="01JOTHERENTRY000000000000B")
    hand_made = visitor_for(s, vid="v3", remarks="Plumber, let in at 9")
    assert run([], [other_entry, hand_made]).is_empty
    assert run([s], [other_entry, hand_made]).creates == [s]


def test_duplicate_visitor_for_one_guest_is_deleted() -> None:
    s = spec()
    first, second = visitor_for(s, vid="v1"), visitor_for(s, vid="v2")
    result = run([s], [first, second])
    assert result.deletes == [second]
    assert len(result.unchanged) == 1
    assert not result.creates
