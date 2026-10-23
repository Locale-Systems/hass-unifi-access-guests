"""Minimal client for the UniFi Access developer API.

Only the calls this integration needs: list doors, and create, update, list,
delete visitors and set their PINs. The endpoints and the response envelope
(``{"code": "SUCCESS", "msg": ..., "data": ...}``) follow Ubiquiti's Access API
reference; the envelope handling mirrors py-unifi-access, which is exercised
against real controllers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import ip_address
import logging
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .const import API_PORT, DEFAULT_REQUEST_TIMEOUT

_LOGGER = logging.getLogger(__name__)

DOORS_PATH = "/api/v1/developer/doors"
VISITORS_PATH = "/api/v1/developer/visitors"
VISITOR_PATH = "/api/v1/developer/visitors/{visitor_id}"
VISITOR_PIN_PATH = "/api/v1/developer/visitors/{visitor_id}/pin_codes"

# Endpoints that accept a UniFi OS API key (X-API-KEY) on the console's own
# port, used only to explain a token the Access API rejected.
UNIFI_OS_KEY_PROBES = (
    ("UniFi Network", "/proxy/network/integration/v1/info"),
    ("UniFi Protect", "/proxy/protect/integration/v1/meta/info"),
)

# The visitor list is paged. 100 keeps a household's list to one request.
PAGE_SIZE = 100
# A guard against a server that ignores page_num and returns page 1 forever.
MAX_PAGES = 50


class AccessError(Exception):
    """The controller returned an error or an unexpected response.

    ``reason`` is the controller's own message when it sent one (for example
    why it refused a PIN), for showing to people; ``code`` is its error code.
    """

    def __init__(self, message: str, *, code: str | None = None, reason: str | None = None) -> None:
        """Initialize the error."""
        super().__init__(message)
        self.code = code
        self.reason = reason or message


class AccessAuthError(AccessError):
    """The API token was rejected (HTTP 401) or lacks a permission (403)."""


class AccessConnectionError(AccessError):
    """The controller could not be reached."""


@dataclass(frozen=True, slots=True)
class Door:
    """A door the controller manages."""

    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Visitor:
    """The fields of an Access visitor this integration reads back."""

    id: str
    first_name: str
    last_name: str
    remarks: str
    start_time: int | None
    end_time: int | None
    status: str
    raw: dict[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Visitor:
        """Build from one item of the API's visitor list."""
        return cls(
            id=str(data["id"]),
            first_name=data.get("first_name") or "",
            last_name=data.get("last_name") or "",
            remarks=data.get("remarks") or "",
            start_time=_as_int(data.get("start_time")),
            end_time=_as_int(data.get("end_time")),
            status=data.get("status") or "",
            raw=data,
        )


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def base_url(host: str) -> str:
    """Return the API base URL for a host, IP or URL, defaulting the port."""
    host = host.strip()
    try:
        # A bare IPv6 address would otherwise parse as host:port.
        if ip_address(host).version == 6:
            host = f"[{host}]"
    except ValueError:
        pass
    if "://" not in host:
        host = f"https://{host}"
    parsed = urlparse(host)
    if not parsed.hostname:
        raise ValueError(f"Invalid host: {host!r}")
    hostname = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"https://{hostname}:{parsed.port or API_PORT}"


class AccessClient:
    """Async client over an externally owned aiohttp session."""

    def __init__(self, host: str, api_token: str, session: aiohttp.ClientSession) -> None:
        """Initialize the client."""
        self._base = base_url(host)
        hostname = urlparse(self._base).hostname or ""
        self._console = f"https://[{hostname}]" if ":" in hostname else f"https://{hostname}"
        self._session = session
        self._token = api_token
        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_token}",
        }
        self._timeout = aiohttp.ClientTimeout(
            total=DEFAULT_REQUEST_TIMEOUT.total_seconds()
        )

    async def unifi_os_key_app(self) -> str | None:
        """Name the app a rejected token works for, if it is a UniFi OS key.

        The usual reason the Access API rejects a token is that it is a
        UniFi OS API key (Settings → Control Plane → Integrations), made for
        the Network and Protect integration APIs on the console's own port.
        Best effort: any failure means "no idea", never an error.
        """
        for app, path in UNIFI_OS_KEY_PROBES:
            try:
                async with self._session.get(
                    f"{self._console}{path}",
                    headers={"X-API-KEY": self._token, "Accept": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        return app
            except (TimeoutError, aiohttp.ClientError, OSError):
                continue
        return None

    async def get_doors(self) -> list[Door]:
        """List doors. Also the cheapest call to validate a token."""
        data = await self._request("GET", DOORS_PATH)
        return [Door(id=str(d["id"]), name=d.get("name") or str(d["id"])) for d in data]

    async def list_visitors(self) -> list[Visitor]:
        """List every visitor, following pages until a short page."""
        visitors: list[Visitor] = []
        for page in range(1, MAX_PAGES + 1):
            data = await self._request(
                "GET",
                VISITORS_PATH,
                params={"page_num": page, "page_size": PAGE_SIZE},
            )
            items = data or []
            visitors.extend(Visitor.from_api(item) for item in items)
            if len(items) < PAGE_SIZE:
                return visitors
        raise AccessError(f"Visitor list did not end after {MAX_PAGES} pages")

    async def create_visitor(self, body: dict[str, Any]) -> str:
        """Create a visitor and return its id."""
        data = await self._request("POST", VISITORS_PATH, json=body)
        try:
            return str(data["id"])
        except (TypeError, KeyError) as err:
            raise AccessError("Create visitor response has no id") from err

    async def update_visitor(self, visitor_id: str, body: dict[str, Any]) -> None:
        """Replace a visitor's details."""
        await self._request(
            "PUT", VISITOR_PATH.format(visitor_id=visitor_id), json=body
        )

    async def delete_visitor(self, visitor_id: str) -> None:
        """Delete a visitor outright (is_force=true), not just cancel it."""
        await self._request(
            "DELETE",
            VISITOR_PATH.format(visitor_id=visitor_id),
            params={"is_force": "true"},
        )

    async def set_visitor_pin(self, visitor_id: str, pin: str | None) -> None:
        """Assign a visitor's PIN, or remove it when ``pin`` is empty."""
        path = VISITOR_PIN_PATH.format(visitor_id=visitor_id)
        if pin:
            await self._request("PUT", path, json={"pin_code": pin})
        else:
            await self._request("DELETE", path)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base}{path}"
        _LOGGER.debug("%s %s", method, path)
        try:
            async with self._session.request(
                method,
                url,
                headers=self._headers,
                json=json,
                params=params,
                timeout=self._timeout,
            ) as resp:
                if resp.status in (401, 403):
                    raise AccessAuthError(f"{method} {path}: HTTP {resp.status}")
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    raise AccessError(
                        f"{method} {path}: HTTP {resp.status} {body}",
                        reason=f"HTTP {resp.status}",
                    )
                try:
                    payload = await resp.json(content_type=None)
                except ValueError as err:
                    raise AccessError(f"{method} {path}: invalid JSON") from err
        except AccessError:
            raise
        except TimeoutError as err:
            raise AccessConnectionError(f"{method} {path}: timed out") from err
        except (aiohttp.ClientError, OSError) as err:
            raise AccessConnectionError(f"{method} {path}: {err}") from err

        if not isinstance(payload, dict):
            raise AccessError(f"{method} {path}: unexpected response {payload!r}"[:300])
        if payload.get("code") != "SUCCESS":
            msg = payload.get("msg") or "no message"
            raise AccessError(
                f"{method} {path}: {payload.get('code')} {msg}",
                code=payload.get("code"),
                reason=str(msg),
            )
        return payload.get("data")
