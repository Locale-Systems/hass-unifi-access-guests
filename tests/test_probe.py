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
    checked: list[tuple[str, int]] = []

    async def door_check(pin: str, end: int) -> None:
        checked.append((pin, end))
        assert access.pins and PIN in access.pins.values()  # PIN live during the check

    clock = Clock()
    steps = await run(client, door_check=door_check, clock=clock)
    assert verdicts(steps) == ["PASS", "PASS", "PASS", "INFO", "PASS", "PASS", "PASS"]
    assert "PIN already in use" in steps[2].detail
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
    assert steps[-1].verdict == "PASS"
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
    assert steps[-1].verdict == "PASS"
    assert access.visitors == {}


async def test_refused_create_still_cleans_up(client: AccessClient, access: FakeAccess) -> None:
    access.fail["POST visitors"] = "code"
    steps = await run(client)
    assert [s.verdict for s in steps] == ["FAIL", "PASS"]
    assert access.visitors == {}
