"""Fixtures: a fake Access controller and a controllable calendar."""

from __future__ import annotations

from collections.abc import Callable, Generator
from datetime import datetime
import itertools
import re
from typing import Any

import pytest
from yarl import URL

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    setup_test_component_platform,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.unifi_access_guests.const import (
    CONF_API_TOKEN,
    CONF_CALENDAR,
    CONF_DOORS,
    CONF_HORIZON_DAYS,
    CONF_HOST,
    CONF_SCAN_MINUTES,
    CONF_SECRET,
    CONF_VERIFY_SSL,
    DOMAIN,
)

HOST = "192.0.2.10"
TOKEN = "test-token"
CALENDAR = "calendar.guests"
DOORS = [{"id": "door-front", "name": "Front Door"}, {"id": "door-gate", "name": "Gate"}]

_API = re.compile(r"^https://192\.0\.2\.10:12445/api/v1/developer/(?P<path>.*)$")
NIL_POINTER = "runtime error: invalid memory address or nil pointer dereference"
_CONSOLE = re.compile(r"^https://192\.0\.2\.10/proxy/(network|protect)/integration/")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load integrations from custom_components."""


class FakeAccess:
    """A stateful stand-in for the controller's developer API.

    Requests that should fail are queued in ``fail``: a key such as
    ``"PUT pin_codes"`` maps to the HTTP status (or ``"code"`` for a non-SUCCESS
    envelope) the next matching request gets.
    """

    def __init__(self) -> None:
        self.doors = list(DOORS)
        self.visitors: dict[str, dict[str, Any]] = {}
        self.pins: dict[str, str] = {}
        self.fail: dict[str, int | str] = {}
        # Refuse a PIN another visitor holds (the controller's assumed rule).
        self.unique_pins = True
        # Set to "UniFi Network" or "UniFi Protect" to make the token a UniFi
        # OS key: that app's integration API accepts it (Access still 401s).
        self.os_key_for: str | None = None
        self.calls: list[tuple[str, str, Any]] = []
        self._ids = (f"visitor-{n}" for n in itertools.count(1))
        # As on a real controller (LOC-304 probe, 2026-10-23), the doors sent
        # with a visitor become a door group made for it, and the visitor
        # reads back with that group. Like that controller (probe run 2),
        # neither an update nor a delete removes the old group; clear the
        # flags for a controller that cleans up after itself.
        self.door_groups: dict[str, dict[str, Any]] = {}
        self.sent_resources: dict[str, list[dict[str, Any]]] = {}
        self.leak_groups_on_update = True
        self.leak_groups_on_delete = True
        # An envelope error code for every door group lookup, when set.
        self.door_group_lookup_error: str | None = None
        self.regroup_without_resources = False
        # GET /door_groups leaves out visitor groups (probe run 3).
        self.list_hides_visitor_groups = True
        # Looking up a group that doesn't exist crashes the lookup instead
        # of answering not-found (probe run 4).
        self.missing_group_lookup_crashes = True
        self.refuse_door_group_delete = False
        # Accept a visitor update but keep the old start and end times.
        self.ignore_update_times = False
        # Refuse a visitor update when this returns True for its body.
        self.refuse_visitor_update: Callable[[dict[str, Any]], bool] | None = None
        # Answer a group DELETE with success but keep the group.
        self.door_group_delete_is_noop = False
        # Extra PIN rule: refuse when this returns True for (visitor id, pin).
        self.refuse_pin: Callable[[str, str], bool] | None = None
        self._group_ids = (f"group-{n}" for n in itertools.count(1))

    def _store_resources(self, visitor_id: str, data: dict[str, Any]) -> None:
        """Turn the doors in a create/update body into a fresh door group.

        A body without resources keeps the visitor's group (unless
        ``regroup_without_resources``), and one naming only existing door
        groups keeps those. How a real controller treats both is what probe
        ``--door-groups`` finds out.
        """
        visitor = self.visitors[visitor_id]
        old = [r["id"] for r in visitor.get("resources") or [] if r.get("type") == "door_group"]
        if "resources" not in data:
            if not self.regroup_without_resources:
                return
            data = {"resources": self.sent_resources.get(visitor_id, [])}
        named = [r for r in data["resources"] if r.get("type") == "door_group"]
        if named and len(named) == len(data["resources"]) and all(
            r.get("id") in self.door_groups for r in named
        ):
            visitor["resources"] = [
                {"id": r["id"], "name": self.door_groups[r["id"]]["name"], "type": "door_group"}
                for r in named
            ]
            return
        self.sent_resources[visitor_id] = data["resources"]
        group_id = next(self._group_ids)
        self.door_groups[group_id] = {
            "id": group_id,
            "name": f"visitor-{group_id}",
            "resources": data["resources"],
        }
        visitor["resources"] = [
            {"id": group_id, "name": f"visitor-{group_id}", "type": "door_group"}
        ]
        if not self.leak_groups_on_update:
            for group in old:
                self.door_groups.pop(group, None)

    def add_visitor(self, **fields: Any) -> str:
        visitor_id = next(self._ids)
        self.visitors[visitor_id] = {"id": visitor_id, "status": "UPCOMING", **fields}
        return visitor_id

    def install(self, aioclient_mock: AiohttpClientMocker) -> None:
        for method in ("get", "post", "put", "delete"):
            aioclient_mock.request(method, _API, side_effect=self._handle)
        aioclient_mock.get(_CONSOLE, side_effect=self._handle_console)

    async def _handle_console(self, method: str, url: URL, data: Any):
        """UniFi OS integration APIs: accept the token only if it is an OS key."""
        app = {"network": "UniFi Network", "protect": "UniFi Protect"}[url.parts[2]]
        ok = app == self.os_key_for
        return self._response(method, url, status=200 if ok else 401, ok=True)

    def _response(self, method: str, url: URL, status: int = 200, **body: Any):
        return AiohttpClientMockResponse(method, url, status=status, json=body or None)

    def _ok(self, method: str, url: URL, data: Any = None):
        return self._response(method, url, code="SUCCESS", msg="success", data=data)

    async def _handle(self, method: str, url: URL, data: Any):
        method = method.upper()
        path = _API.match(str(url.with_query(None)))["path"]
        self.calls.append((method, path, data))
        parts = path.split("/")
        fail = self.fail.pop(f"{method} {parts[-1]}", None) or self.fail.pop(
            f"{method} {parts[0]}", None
        )
        if fail == "code":
            return self._response(method, url, code="CODE_PARAMS_INVALID", msg="bad")
        if fail:
            return self._response(method, url, status=fail)

        if path == "doors" and method == "GET":
            return self._ok(method, url, self.doors)
        if path == "visitors" and method == "GET":
            page = int(url.query.get("page_num", 1))
            size = int(url.query.get("page_size", 25))
            items = list(self.visitors.values())[(page - 1) * size : page * size]
            return self._ok(method, url, items)
        if path == "visitors" and method == "POST":
            visitor_id = self.add_visitor(**{k: v for k, v in data.items() if k != "resources"})
            self._store_resources(visitor_id, data)
            return self._ok(method, url, self.visitors[visitor_id])
        if path == "door_groups" and method == "GET":
            return self._ok(method, url, [
                g for g in self.door_groups.values()
                if not (self.list_hides_visitor_groups and str(g.get("name", "")).startswith("visitor-"))
            ])
        if parts[0] == "door_groups" and len(parts) == 2 and method == "DELETE":
            if self.refuse_door_group_delete:
                return self._response(method, url, code="CODE_SYSTEM_ERROR", msg="Server system error.")
            if parts[1] not in self.door_groups:
                return self._response(method, url, status=404)
            if not self.door_group_delete_is_noop:
                del self.door_groups[parts[1]]
            return self._ok(method, url)
        if parts[0] == "door_groups" and len(parts) == 2 and method == "GET":
            if self.door_group_lookup_error:
                return self._response(method, url, code=self.door_group_lookup_error, msg="nope")
            group = self.door_groups.get(parts[1])
            if group:
                return self._ok(method, url, group)
            if self.missing_group_lookup_crashes:
                return self._response(method, url, code="CODE_SYSTEM_ERROR", msg=NIL_POINTER)
            return self._response(method, url, status=404)
        if parts[0] == "visitors" and len(parts) == 2:
            visitor_id = parts[1]
            if visitor_id not in self.visitors:
                return self._response(method, url, status=404)
            if method == "PUT":
                if self.refuse_visitor_update and self.refuse_visitor_update(data):
                    return self._response(method, url, code="CODE_PARAMS_INVALID", msg="bad times")
                kept = {"resources"} | ({"start_time", "end_time"} if self.ignore_update_times else set())
                self.visitors[visitor_id].update({k: v for k, v in data.items() if k not in kept})
                if self.visitors[visitor_id].get("status") == "CANCELLED":
                    # An update revives a cancelled visit (probe run 5).
                    self.visitors[visitor_id]["status"] = "UPCOMING"
                self._store_resources(visitor_id, data)
                return self._ok(method, url, self.visitors[visitor_id])
            if method == "DELETE":
                if url.query.get("is_force") == "false":
                    # Cancel: the record and its group stay.
                    self.visitors[visitor_id]["status"] = "CANCELLED"
                    return self._ok(method, url)
                assert url.query.get("is_force") == "true"
                visitor = self.visitors.pop(visitor_id)
                self.pins.pop(visitor_id, None)
                if not self.leak_groups_on_delete:
                    for r in visitor.get("resources") or []:
                        self.door_groups.pop(r["id"], None)
                return self._ok(method, url)
        if parts[0] == "visitors" and parts[2:] == ["pin_codes"]:
            visitor_id = parts[1]
            if method == "PUT":
                # A PIN belongs to one credential holder. A real controller
                # refuses a duplicate with this generic error (LOC-304 probe,
                # 2026-10-23), not a PIN-specific code.
                if (self.unique_pins and any(
                    p == data["pin_code"] for v, p in self.pins.items() if v != visitor_id
                )) or (self.refuse_pin and self.refuse_pin(visitor_id, data["pin_code"])):
                    return self._response(
                        method, url, code="CODE_SYSTEM_ERROR", msg="Server system error."
                    )
                self.pins[visitor_id] = data["pin_code"]
            else:
                self.pins.pop(visitor_id, None)
            return self._ok(method, url)
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def access(aioclient_mock: AiohttpClientMocker) -> FakeAccess:
    """The fake controller, wired into HA's aiohttp session."""
    fake = FakeAccess()
    fake.install(aioclient_mock)
    return fake


class FakeCalendar(CalendarEntity):
    """A calendar whose events a test sets directly."""

    _attr_name = "Guests"

    def __init__(self) -> None:
        self.events: list[CalendarEvent] = []
        self.error: Exception | None = None

    @property
    def event(self) -> CalendarEvent | None:
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        if self.error:
            raise self.error
        return [
            e
            for e in self.events
            if e.end_datetime_local > start_date and e.start_datetime_local < end_date
        ]


@pytest.fixture
async def calendar(hass: HomeAssistant) -> FakeCalendar:
    """A loaded calendar entity at calendar.guests."""
    entity = FakeCalendar()
    setup_test_component_platform(hass, "calendar", [entity])
    assert await async_setup_component(hass, "calendar", {"calendar": {"platform": "test"}})
    await hass.async_block_till_done()
    assert entity.entity_id == CALENDAR
    return entity


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """A config entry for the guests calendar, both doors."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Guests",
        unique_id=f"{HOST}|{CALENDAR}",
        data={
            CONF_HOST: HOST,
            CONF_API_TOKEN: TOKEN,
            CONF_VERIFY_SSL: False,
            CONF_CALENDAR: CALENDAR,
            CONF_SECRET: "0" * 32,
        },
        options={
            CONF_DOORS: ["door-front", "door-gate"],
            CONF_HORIZON_DAYS: 30,
            CONF_SCAN_MINUTES: 5,
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def mock_setup_entry() -> Generator[None]:
    """Keep config-flow tests from setting up the entry they create."""
    from unittest.mock import patch

    with patch(
        "custom_components.unifi_access_guests.async_setup_entry", return_value=True
    ):
        yield
