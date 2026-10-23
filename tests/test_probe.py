"""The controller probe (tools/probe_controller.py), against the fake controller."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.unifi_access_guests.api import AccessClient

from .conftest import HOST, TOKEN, FakeAccess

_PATH = Path(__file__).resolve().parents[1] / "tools" / "probe_controller.py"
_SPEC = importlib.util.spec_from_file_location("probe_controller", _PATH)
probe = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = probe  # dataclasses look their module up here
_SPEC.loader.exec_module(probe)

PIN = "482190"


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


@pytest.fixture
def client(hass: HomeAssistant, access: FakeAccess) -> AccessClient:
    return AccessClient(HOST, TOKEN, async_get_clientsession(hass))


async def run(client, *, door_check=None, clock=None):
    clock = clock or Clock()
    return await probe.run(client, "door-gate", PIN, now=clock.now, sleep=clock.sleep,
                           door_check=door_check)


def verdicts(steps) -> list[str]:
    return [s.verdict for s in steps]


async def test_happy_path(client: AccessClient, access: FakeAccess) -> None:
    # A controller that removes replaced and deleted visitors' groups.
    access.leak_groups_on_update = access.leak_groups_on_delete = False
    checked: list[tuple[str, int]] = []

    async def door_check(pin: str, end: int) -> None:
        checked.append((pin, end))
        assert access.pins and PIN in access.pins.values()  # PIN live during the check

    clock = Clock()
    steps = await run(client, door_check=door_check, clock=clock)
    assert [(s.name.split(" ")[0], s.verdict) for s in steps] == [
        ("Create", "PASS"), ("Set,", "PASS"), ("Give", "PASS"), ("Visitor", "INFO"),
        ("Move", "PASS"), ("An", "PASS"), ("The", "PASS"), ("Delete", "PASS"),
        ("Deleting", "PASS"),
    ]
    assert "CODE_SYSTEM_ERROR" in steps[2].detail
    assert "a different PIN on the same visitor was accepted" in steps[2].detail
    assert access.door_groups == {}
    assert checked and checked[0][0] == PIN
    # It waited for visitor 1's window to open, then for visitor 3's to end.
    assert clock.slept[0] == 30 and clock.slept[1] > 60
    assert access.visitors == {} and access.pins == {}


async def test_cleans_up_only_probe_visitors(client: AccessClient, access: FakeAccess) -> None:
    hand_made = access.add_visitor(first_name="Plumber", last_name="Bob", remarks="Let in at 9")
    other_entry = access.add_visitor(
        first_name="Jane", last_name="Doe",
        remarks="[ha-guests:v1:01JREALENTRY000000000000AA:0123456789abcdef:01234567]",
    )
    leftover = access.add_visitor(
        first_name="Probe", last_name="Visitor 1",
        remarks="[ha-guests:v1:probe:0123456789abcdef:01234567]",
    )
    steps = await run(client)
    assert step_named(steps, "Delete every probe visitor").verdict == "PASS"
    assert set(access.visitors) == {hand_made, other_entry}
    assert leftover not in access.visitors


async def test_without_a_terminal_the_door_check_is_skipped(
    client: AccessClient, access: FakeAccess
) -> None:
    clock = Clock()
    steps = await run(client, clock=clock)
    assert steps[3].verdict == "SKIP"
    assert clock.slept[0] > 60  # straight to visitor 3's wait


async def test_duplicate_pin_accepted_is_a_failure(
    client: AccessClient, access: FakeAccess
) -> None:
    access.unique_pins = False
    steps = await run(client)
    assert steps[2].verdict == "FAIL"
    assert "ACCEPTED" in steps[2].detail
    assert access.visitors == {}


async def test_times_in_another_format_fail_step_one(
    client: AccessClient, access: FakeAccess, monkeypatch
) -> None:
    original = access.add_visitor

    def as_milliseconds(**fields):
        fields["start_time"] *= 1000
        fields["end_time"] *= 1000
        return original(**fields)

    monkeypatch.setattr(access, "add_visitor", as_milliseconds)
    steps = await run(client)
    assert steps[0].verdict == "FAIL"
    assert "read back" in steps[0].detail
    assert step_named(steps, "Delete every probe visitor").verdict == "PASS"
    assert access.visitors == {}


async def test_rejected_token_explained_as_os_key(
    client: AccessClient, access: FakeAccess, capsys
) -> None:
    access.os_key_for = "UniFi Network"
    await probe.explain_rejected_token(client, HOST, TOKEN)
    out = capsys.readouterr().out
    assert "UniFi OS API key" in out and "UniFi Network" in out
    assert f"https://{HOST}/access/settings/system" in out
    assert TOKEN not in out


async def test_rejected_token_checklist_flags_whitespace(
    client: AccessClient, access: FakeAccess, capsys
) -> None:
    await probe.explain_rejected_token(client, HOST, "user name\nsecret")
    out = capsys.readouterr().out
    assert "not a UniFi OS key" in out
    assert "CONTAINS WHITESPACE" in out
    assert "secret" not in out
    await probe.explain_rejected_token(client, HOST, "abcDEF123-_")
    assert "letters, digits" in capsys.readouterr().out


async def test_refused_create_still_cleans_up(client: AccessClient, access: FakeAccess) -> None:
    access.fail["POST visitors"] = "code"
    steps = await run(client)
    assert [s.verdict for s in steps] == ["FAIL", "PASS", "INFO"]
    assert access.visitors == {}


def leftovers(access: FakeAccess) -> tuple[str, str]:
    """An orphaned visitor group, a hand-made visitor with its own group, and
    a group that isn't a visitor group. Returns (orphan id, hand-made group id)."""
    access.door_groups["group-old"] = {"id": "group-old", "name": "visitor-1792776844541"}
    access.door_groups["group-main"] = {"id": "group-main", "name": "Main doors"}
    bob = access.add_visitor(first_name="Bob", last_name="Plumber", remarks="")
    access._store_resources(bob, {"resources": [{"id": "door-gate", "type": "door"}]})
    [bobs_group] = [r["id"] for r in access.visitors[bob]["resources"]]
    return "group-old", bobs_group


async def investigate(client, *, answer: bool | None = True, delete_ids=()):
    async def confirm(orphans):
        return answer

    return await probe.investigate_door_groups(
        client,
        "door-gate",
        now=Clock().now,
        confirm=None if answer is None else confirm,
        delete_ids=delete_ids,
    )


ALL_STEPS = [
    "A made-up door group id", "GET /door_groups lists", "A live visitor's group",
    "GET /door_groups includes", "An update without resources", "End an upcoming visit early",
    "Reuse the ended visitor", "Cancel a visit", "DELETE /door_groups", "Deleting the visitor, then",
    "Delete every probe visitor", "Delete the door groups this run made",
    "Delete leftover visitor door groups (by listing)",
]


async def test_door_groups_all_pass_and_delete_only_orphans(
    client: AccessClient, access: FakeAccess
) -> None:
    access.list_hides_visitor_groups = False  # a controller that lists them
    orphan, bobs_group = leftovers(access)
    steps = await investigate(client)
    assert [s.name for s in steps] == [
        next(s.name for s in steps if s.name.startswith(p)) for p in ALL_STEPS
    ]
    assert [s.verdict for s in steps] == [
        "INFO", "INFO", "PASS", "PASS", "PASS", "PASS",
        "PASS", "PASS", "PASS", "PASS",
        "PASS", "PASS", "PASS",
    ]
    assert "nil pointer" in steps[0].detail  # the fake answers like run 4
    assert "1 of those used by no visitor" in steps[1].detail
    assert "start a minute ago" in step_named(steps, "End an upcoming").detail
    assert "CANCELLED" in step_named(steps, "Cancel a visit").detail
    assert orphan not in access.door_groups
    assert bobs_group in access.door_groups  # a live visitor's group stays
    assert "group-main" in access.door_groups  # not a visitor group
    assert [v["first_name"] for v in access.visitors.values()] == ["Bob"]
    # Every group the run made is gone: only Bob's and the main group remain.
    assert set(access.door_groups) == {bobs_group, "group-main"}


async def test_a_lookup_crash_reads_as_gone_only_through_the_control(
    client: AccessClient, access: FakeAccess
) -> None:
    # The fake crashes on a missing group, as run 4's controller did; without
    # the control those deletes would read "unknown", never PASS.
    steps = await investigate(client, answer=None)
    assert step_named(steps, "DELETE /door_groups").verdict == "PASS"
    assert step_named(steps, "Delete the door groups this run made").verdict == "PASS"
    assert await probe.door_group_state(client, "nope") == (
        "unknown: GET /api/v1/developer/door_groups/nope: CODE_SYSTEM_ERROR "
        "runtime error: invalid memory address or nil pointer dereference"
    )


async def test_lookups_that_always_error_prove_nothing(
    client: AccessClient, access: FakeAccess
) -> None:
    # Every lookup errors alike, live group or not: the control would match
    # everything, so "gone" must not be believed.
    access.door_group_lookup_error = "CODE_SYSTEM_ERROR"
    steps = await investigate(client, answer=None)
    assert step_named(steps, "A live visitor's group").verdict == "FAIL"
    for prefix in ("DELETE /door_groups", "Deleting the visitor, then", "Delete the door groups this run made"):
        assert step_named(steps, prefix).verdict != "PASS", prefix


@pytest.mark.parametrize("answer", [None, False])
async def test_door_groups_left_alone_without_a_yes(
    client: AccessClient, access: FakeAccess, answer
) -> None:
    access.list_hides_visitor_groups = False
    orphan, _ = leftovers(access)
    steps = await investigate(client, answer=answer)
    assert steps[-1].verdict == "INFO" and "not deleted" in steps[-1].detail
    assert orphan in access.door_groups


async def test_named_leftovers_deleted_only_when_safe(
    client: AccessClient, access: FakeAccess
) -> None:
    orphan, bobs_group = leftovers(access)
    ids = [orphan, bobs_group, "group-main", "never-existed"]
    s = step_named(await investigate(client, delete_ids=ids), "Delete the leftover groups named")
    assert s.verdict == "PASS" and "deleted 1 of 1" in s.detail
    assert orphan not in access.door_groups
    assert bobs_group in access.door_groups and "group-main" in access.door_groups
    assert "a visitor still uses it" in s.detail
    assert "not a visitor group" in s.detail
    assert "never-existed: already gone" in s.detail


@pytest.mark.parametrize("answer", [None, False])
async def test_named_leftovers_need_a_yes(client: AccessClient, access: FakeAccess, answer) -> None:
    orphan, _ = leftovers(access)
    s = step_named(
        await investigate(client, answer=answer, delete_ids=[orphan]), "Delete the leftover groups named"
    )
    assert s.verdict == "INFO" and "not deleted" in s.detail
    assert orphan in access.door_groups


async def test_controller_that_cleans_up_itself_is_told_apart(
    client: AccessClient, access: FakeAccess
) -> None:
    # Neither replaced nor deleted visitors' groups linger: no DELETE needed,
    # and the probe must say so rather than report its own delete refused.
    access.leak_groups_on_update = access.leak_groups_on_delete = False
    steps = await investigate(client, answer=None)
    s = step_named(steps, "DELETE /door_groups")
    assert s.verdict == "INFO" and "already gone before the DELETE" in s.detail
    s = step_named(steps, "Deleting the visitor, then")
    assert s.verdict == "PASS" and "removed its group" in s.detail and "by itself" in s.detail


async def test_update_without_resources_that_regroups_fails(
    client: AccessClient, access: FakeAccess
) -> None:
    access.regroup_without_resources = True
    s = step_named(await investigate(client, answer=None), "An update without resources")
    assert s.verdict == "FAIL" and "group changed" in s.detail


async def test_group_delete_that_keeps_the_group_fails(
    client: AccessClient, access: FakeAccess
) -> None:
    access.door_group_delete_is_noop = True
    steps = await investigate(client, answer=None)
    for prefix in ("DELETE /door_groups", "Deleting the visitor, then"):
        s = step_named(steps, prefix)
        assert s.verdict == "FAIL" and "exists" in s.detail
    assert step_named(steps, "Delete the door groups this run made").verdict == "FAIL"


async def test_refused_group_delete_fails(client: AccessClient, access: FakeAccess) -> None:
    access.refuse_door_group_delete = True
    access.list_hides_visitor_groups = False
    steps = await investigate(client)
    assert step_named(steps, "DELETE /door_groups").verdict == "FAIL"
    assert step_named(steps, "Deleting the visitor, then").verdict == "FAIL"
    assert step_named(steps, "Delete the door groups this run made").verdict == "FAIL"
    assert steps[-1].verdict == "FAIL"  # the offered clean-up could not delete either


async def test_door_groups_survive_a_refused_create(client: AccessClient, access: FakeAccess) -> None:
    access.fail["POST visitors"] = "code"
    steps = await investigate(client, answer=None)
    assert step_named(steps, "Create and read back").verdict == "FAIL"
    assert step_named(steps, "Delete every probe visitor").verdict == "PASS"


async def test_hidden_visitor_groups_are_reported_not_passed(
    client: AccessClient, access: FakeAccess
) -> None:
    # As on a real controller: the list leaves visitor groups out.
    orphan, _ = leftovers(access)
    steps = await investigate(client)
    assert step_named(steps, "GET /door_groups includes").verdict == "INFO"
    last = steps[-1]
    assert last.verdict == "INFO" and "can't be found this way" in last.detail
    assert orphan in access.door_groups  # invisible, so untouched


async def test_every_early_end_refused_fails_and_skips_reuse(
    client: AccessClient, access: FakeAccess
) -> None:
    clock_start = 1_800_000_000
    # Refuse any update ending within two minutes of the clock: both tries.
    access.refuse_visitor_update = lambda body: body.get("end_time", 0) < clock_start + 120
    steps = await investigate(client, answer=None)
    s = step_named(steps, "End an upcoming visit early")
    assert s.verdict == "FAIL"
    assert s.detail.count("refused (CODE_PARAMS_INVALID") == 2
    assert step_named(steps, "Reuse the ended visitor").verdict == "SKIP"


async def test_early_end_falls_back_to_a_future_start(
    client: AccessClient, access: FakeAccess
) -> None:
    clock_start = 1_800_000_000
    # Refuse a start in the past, as one reading of run 4 would have it.
    access.refuse_visitor_update = lambda body: body.get("start_time", clock_start) < clock_start
    s = step_named(await investigate(client, answer=None), "End an upcoming visit early")
    assert s.verdict == "PASS"
    assert "start a minute ago, end in a minute: refused" in s.detail
    assert "start in 5 s, end in a minute: accepted" in s.detail


async def test_early_end_that_regroups_fails(client: AccessClient, access: FakeAccess) -> None:
    access.regroup_without_resources = True
    steps = await investigate(client, answer=None)
    s = step_named(steps, "End an upcoming visit early")
    assert s.verdict == "FAIL" and "group changed" in s.detail


async def test_updates_that_keep_old_times_fail(client: AccessClient, access: FakeAccess) -> None:
    access.ignore_update_times = True
    steps = await investigate(client, answer=None)
    for prefix in ("An update without resources", "End an upcoming visit early"):
        s = step_named(steps, prefix)
        assert s.verdict == "FAIL" and "window" in s.detail, (prefix, s.detail)


async def test_reuse_that_keeps_the_ended_times_fails(
    client: AccessClient, access: FakeAccess
) -> None:
    # Accept the early end, then ignore the times of the next update only.
    original = access._store_resources
    calls = []

    def watch(visitor_id, data):
        calls.append(data.get("end_time"))
        access.ignore_update_times = len(calls) >= 3  # create, move, end early
        return original(visitor_id, data)

    access._store_resources = watch
    s = step_named(await investigate(client, answer=None), "Reuse the ended visitor")
    assert s.verdict == "FAIL" and "did not move" in s.detail


async def test_listed_groups_and_no_leftovers_pass(client: AccessClient, access: FakeAccess) -> None:
    access.list_hides_visitor_groups = False
    access.leak_groups_on_update = access.leak_groups_on_delete = False
    steps = await investigate(client)
    assert steps[-1].verdict == "PASS" and steps[-1].detail == "none left"


def step_named(steps, prefix: str):
    [s] = [s for s in steps if s.name.startswith(prefix)]
    return s


async def test_refusing_any_pin_fails_the_duplicate_control(
    client: AccessClient, access: FakeAccess
) -> None:
    # Visitor 2 takes no PIN at all: the refusal is not about duplication.
    access.refuse_pin = lambda vid, pin: access.visitors[vid]["last_name"] == "Visitor 2"
    s = step_named(await run(client), "Give a second visitor")
    assert s.verdict == "FAIL"
    assert "not shown to be about the duplicate" in s.detail


async def test_door_group_left_by_an_update_fails(client: AccessClient, access: FakeAccess) -> None:
    access.leak_groups_on_update, access.leak_groups_on_delete = True, False
    steps = await run(client)
    s = step_named(steps, "An update leaves")
    assert s.verdict == "FAIL" and "left the old one" in s.detail
    assert step_named(steps, "Deleting a visitor").verdict == "FAIL"


async def test_door_group_left_by_a_delete_fails(client: AccessClient, access: FakeAccess) -> None:
    access.leak_groups_on_update, access.leak_groups_on_delete = False, True
    steps = await run(client)
    assert step_named(steps, "An update leaves").verdict == "PASS"
    s = step_named(steps, "Deleting a visitor")
    assert s.verdict == "FAIL" and "delete them in the Access app" in s.detail
    # Visitors 1, 2 and 3 (after its update) each left a group; visitor 3's
    # pre-update group went with the update.
    assert "3 of 4 still exist" in s.detail


async def test_unreadable_door_groups_are_unknown_not_gone(
    client: AccessClient, access: FakeAccess
) -> None:
    access.door_group_lookup_error = "CODE_SYSTEM_ERROR"
    steps = await run(client)
    for prefix in ("An update leaves", "Deleting a visitor"):
        s = step_named(steps, prefix)
        assert s.verdict == "INFO" and "could not tell" in s.detail
