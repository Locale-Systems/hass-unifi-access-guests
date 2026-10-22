"""The Access client on its own: URLs, envelope, paging, errors."""

from __future__ import annotations

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.unifi_access_guests import api
from custom_components.unifi_access_guests.api import (
    AccessAuthError,
    AccessClient,
    AccessError,
    base_url,
)

from .conftest import HOST, TOKEN, FakeAccess


@pytest.fixture
def client(hass: HomeAssistant, access: FakeAccess) -> AccessClient:
    return AccessClient(HOST, TOKEN, async_get_clientsession(hass))


def test_base_url() -> None:
    assert base_url("192.0.2.10") == "https://192.0.2.10:12445"
    assert base_url("access.example.com:8443") == "https://access.example.com:8443"
    assert base_url("https://192.0.2.10") == "https://192.0.2.10:12445"
    assert base_url("fd00::5") == "https://[fd00::5]:12445"
    assert base_url("[fd00::5]:8443") == "https://[fd00::5]:8443"
    assert base_url(" 192.0.2.10 ") == "https://192.0.2.10:12445"
    with pytest.raises(ValueError):
        base_url("https://")


async def test_token_is_sent(client: AccessClient, aioclient_mock) -> None:
    await client.get_doors()
    _, _, _, headers = aioclient_mock.mock_calls[-1]
    assert headers["Authorization"] == f"Bearer {TOKEN}"


async def test_list_visitors_follows_pages(
    client: AccessClient, access: FakeAccess, monkeypatch
) -> None:
    monkeypatch.setattr(api, "PAGE_SIZE", 2)
    for n in range(5):
        access.add_visitor(first_name=f"V{n}", last_name="X", remarks="")
    visitors = await client.list_visitors()
    assert [v.first_name for v in visitors] == ["V0", "V1", "V2", "V3", "V4"]
    pages = [c for c in access.calls if c[:2] == ("GET", "visitors")]
    assert len(pages) == 3


async def test_list_visitors_stops_a_runaway_server(
    client: AccessClient, access: FakeAccess, monkeypatch
) -> None:
    monkeypatch.setattr(api, "PAGE_SIZE", 1)
    monkeypatch.setattr(api, "MAX_PAGES", 3)
    for n in range(5):
        access.add_visitor(first_name=f"V{n}", last_name="X", remarks="")
    with pytest.raises(AccessError, match="did not end"):
        await client.list_visitors()


async def test_non_success_envelope_raises(client: AccessClient, access: FakeAccess) -> None:
    access.fail["GET doors"] = "code"
    with pytest.raises(AccessError, match="bad"):
        await client.get_doors()


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_statuses(client: AccessClient, access: FakeAccess, status: int) -> None:
    access.fail["GET doors"] = status
    with pytest.raises(AccessAuthError):
        await client.get_doors()


async def test_pin_set_and_remove(client: AccessClient, access: FakeAccess) -> None:
    vid = access.add_visitor(first_name="A", last_name="B", remarks="")
    await client.set_visitor_pin(vid, "4821")
    assert access.pins == {vid: "4821"}
    await client.set_visitor_pin(vid, None)
    assert access.pins == {}
    assert [c[:2] for c in access.calls] == [
        ("PUT", f"visitors/{vid}/pin_codes"),
        ("DELETE", f"visitors/{vid}/pin_codes"),
    ]
