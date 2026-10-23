"""Fixtures: a fake Access controller and a controllable calendar."""

from __future__ import annotations

from collections.abc import Generator
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
            visitor_id = self.add_visitor(**data)
            return self._ok(method, url, self.visitors[visitor_id])
        if parts[0] == "visitors" and len(parts) == 2:
            visitor_id = parts[1]
            if visitor_id not in self.visitors:
                return self._response(method, url, status=404)
            if method == "PUT":
                self.visitors[visitor_id].update(data)
                return self._ok(method, url, self.visitors[visitor_id])
            if method == "DELETE":
                assert url.query.get("is_force") == "true"
                del self.visitors[visitor_id]
                self.pins.pop(visitor_id, None)
                return self._ok(method, url)
        if parts[0] == "visitors" and parts[2:] == ["pin_codes"]:
            visitor_id = parts[1]
            if method == "PUT":
                # The controller lets a PIN belong to one credential holder.
                # The real error code is unverified; this one is made up.
                if self.unique_pins and any(
                    p == data["pin_code"] for v, p in self.pins.items() if v != visitor_id
                ):
                    return self._response(
                        method, url, code="CODE_PIN_IN_USE", msg="PIN already in use"
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
