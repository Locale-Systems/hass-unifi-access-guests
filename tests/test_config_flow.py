"""Config, options and reauth flows."""

from __future__ import annotations

import pytest

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

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

from .conftest import CALENDAR, HOST, TOKEN, FakeAccess, FakeCalendar

CONNECTION = {CONF_HOST: HOST, CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: False}
CHOICES = {
    CONF_CALENDAR: CALENDAR,
    CONF_DOORS: ["door-gate"],
    CONF_HORIZON_DAYS: 14,
    CONF_SCAN_MINUTES: 10,
}


async def start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


@pytest.mark.usefixtures("mock_setup_entry")
async def test_full_flow(hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar) -> None:
    result = await start(hass)
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CONNECTION)
    assert result["step_id"] == "calendar"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CHOICES)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Guests"
    data = result["data"]
    assert {k: data[k] for k in CONNECTION} == CONNECTION
    assert data[CONF_CALENDAR] == CALENDAR
    assert len(data[CONF_SECRET]) == 32
    assert result["options"] == {
        CONF_DOORS: ["door-gate"],
        CONF_HORIZON_DAYS: 14,
        CONF_SCAN_MINUTES: 10,
    }
    assert result["result"].unique_id == f"{HOST}|{CALENDAR}"


@pytest.mark.parametrize(
    ("fail", "error"),
    [(401, "invalid_auth"), (403, "invalid_auth"), ("code", "unknown"), (500, "unknown")],
)
async def test_connection_errors(
    hass: HomeAssistant, access: FakeAccess, fail, error: str
) -> None:
    access.fail["GET doors"] = fail
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CONNECTION)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


@pytest.mark.parametrize("app", ["UniFi Network", "UniFi Protect"])
async def test_unifi_os_key_is_named(hass: HomeAssistant, access: FakeAccess, app: str) -> None:
    access.fail["GET doors"] = 401
    access.os_key_for = app
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CONNECTION)
    assert result["errors"] == {"base": "unifi_os_key"}


async def test_reauth_with_a_unifi_os_key(
    hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar, config_entry
) -> None:
    result = await config_entry.start_reauth_flow(hass)
    access.fail["GET doors"] = 401
    access.os_key_for = "UniFi Protect"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "an-os-key"}
    )
    assert result["errors"] == {"base": "unifi_os_key"}


async def test_unreachable_controller(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(
        "https://192.0.2.10:12445/api/v1/developer/doors", exc=TimeoutError()
    )
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CONNECTION)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_controller_with_no_doors(hass: HomeAssistant, access: FakeAccess) -> None:
    access.doors = []
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CONNECTION)
    assert result["errors"] == {"base": "no_doors"}


async def test_no_doors_selected(
    hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar
) -> None:
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CONNECTION)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**CHOICES, CONF_DOORS: []}
    )
    assert result["errors"] == {CONF_DOORS: "no_doors_selected"}


async def test_same_calendar_twice_aborts(
    hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar, config_entry
) -> None:
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CONNECTION)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CHOICES)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow_reloads(
    hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar, config_entry
) -> None:
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DOORS: ["door-front"], CONF_HORIZON_DAYS: 7, CONF_SCAN_MINUTES: 2},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {
        CONF_DOORS: ["door-front"],
        CONF_HORIZON_DAYS: 7,
        CONF_SCAN_MINUTES: 2,
    }
    assert config_entry.runtime_data.update_interval.total_seconds() == 120


async def test_reauth(
    hass: HomeAssistant, access: FakeAccess, calendar: FakeCalendar, config_entry
) -> None:
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    access.fail["GET doors"] = 401
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "still-bad"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "new-token"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_API_TOKEN] == "new-token"
