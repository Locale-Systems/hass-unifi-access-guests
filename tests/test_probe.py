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


async def investigate(client, *, answer: bool | None = True):
    async def confirm(orphans):
        return answer

    return await probe.investigate_door_groups(
        client, "door-gate", now=Clock().now, confirm=None if answer is None else confirm
    )


async def test_door_groups_all_pass_and_delete_only_orphans(
    client: AccessClient, access: FakeAccess
) -> None:
    orphan, bobs_group = leftovers(access)
    steps = await investigate(client)
    assert [s.verdict for s in steps] == ["INFO", "PASS", "PASS", "PASS", "PASS", "PASS", "PASS"]
    assert "1 of those used by no visitor" in steps[0].detail
    assert orphan not in access.door_groups
    assert bobs_group in access.door_groups  # a live visitor's group stays
    assert "group-main" in access.door_groups  # not a visitor group
    assert [v["first_name"] for v in access.visitors.values()] == ["Bob"]


@pytest.mark.parametrize("answer", [None, False])
async def test_door_groups_left_alone_without_a_yes(
    client: AccessClient, access: FakeAccess, answer
) -> None:
    orphan, _ = leftovers(access)
    steps = await investigate(client, answer=answer)
    assert steps[-1].verdict == "INFO" and "not deleted" in steps[-1].detail
    assert orphan in access.door_groups


async def test_update_without_resources_that_regroups_fails(
    client: AccessClient, access: FakeAccess
) -> None:
    access.regroup_without_resources = True
    s = step_named(await investigate(client, answer=None), "An update without resources")
    assert s.verdict == "FAIL" and "group changed" in s.detail


async def test_update_naming_own_group_that_regroups_fails(
    client: AccessClient, access: FakeAccess
) -> None:
    access.ignore_group_references = True
    s = step_named(await investigate(client, answer=None), "An update sending the visitor's own group")
    assert s.verdict == "FAIL" and "group changed" in s.detail


async def test_group_delete_that_keeps_the_group_fails(
    client: AccessClient, access: FakeAccess
) -> None:
    access.door_group_delete_is_noop = True
    steps = await investigate(client, answer=None)
    for prefix in ("DELETE /door_groups", "Deleting the visitor, then"):
        s = step_named(steps, prefix)
        assert s.verdict == "FAIL" and "exists" in s.detail


async def test_refused_group_delete_fails(client: AccessClient, access: FakeAccess) -> None:
    access.refuse_door_group_delete = True
    steps = await investigate(client)
    assert step_named(steps, "DELETE /door_groups").verdict == "FAIL"
    assert step_named(steps, "Deleting the visitor, then").verdict == "FAIL"
    assert steps[-1].verdict == "FAIL"  # the offered clean-up could not delete either


async def test_door_groups_survive_a_refused_create(client: AccessClient, access: FakeAccess) -> None:
    access.fail["POST visitors"] = "code"
    steps = await investigate(client, answer=None)
    assert step_named(steps, "Create and read back").verdict == "FAIL"
    assert step_named(steps, "Delete every probe visitor").verdict == "PASS"


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
