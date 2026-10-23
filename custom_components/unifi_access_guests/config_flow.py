"""Config flow: controller connection, then the calendar and its doors."""

from __future__ import annotations

from collections.abc import Mapping
import logging
import secrets
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import build_client
from .api import AccessAuthError, AccessConnectionError, AccessError, Door
from .const import (
    CONF_API_TOKEN,
    CONF_CALENDAR,
    CONF_DOORS,
    CONF_HORIZON_DAYS,
    CONF_HOST,
    CONF_SCAN_MINUTES,
    CONF_SECRET,
    CONF_VERIFY_SSL,
    DEFAULT_HORIZON_DAYS,
    DEFAULT_SCAN_MINUTES,
    DOMAIN,
    MIN_SCAN_MINUTES,
)


_LOGGER = logging.getLogger(__name__)


async def _fetch_doors(hass: HomeAssistant, data: Mapping[str, Any]) -> list[Door]:
    """List doors, raising ``UnifiOsKey`` for the commonest wrong token."""
    client = build_client(hass, dict(data))
    try:
        return await client.get_doors()
    except AccessAuthError:
        if await client.unifi_os_key_app():
            raise UnifiOsKey from None
        raise
    except AccessError as err:
        # The form can only say "could not reach" or "unexpected error"; the
        # log says why.
        _LOGGER.warning("Setting up UniFi Access Guests: %s", err)
        raise


class UnifiOsKey(AccessAuthError):
    """The token is a UniFi OS API key, not a UniFi Access one."""

    def __init__(self) -> None:
        """Initialize the error."""
        super().__init__("UniFi OS API key given for the Access API")


def _error_key(err: AccessError) -> str:
    if isinstance(err, UnifiOsKey):
        return "unifi_os_key"
    if isinstance(err, AccessAuthError):
        return "invalid_auth"
    if isinstance(err, AccessConnectionError):
        return "cannot_connect"
    return "unknown"


def _options_schema(doors: list[Door], current: Mapping[str, Any]) -> vol.Schema:
    known = {door.id for door in doors}
    return vol.Schema(
        {
            vol.Required(
                CONF_DOORS,
                default=[d for d in current.get(CONF_DOORS, []) if d in known],
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=door.id, label=door.name)
                        for door in sorted(doors, key=lambda d: d.name.lower())
                    ],
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            ),
            vol.Required(
                CONF_HORIZON_DAYS,
                default=current.get(CONF_HORIZON_DAYS, DEFAULT_HORIZON_DAYS),
            ): NumberSelector(
                NumberSelectorConfig(min=1, max=365, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(
                CONF_SCAN_MINUTES,
                default=current.get(CONF_SCAN_MINUTES, DEFAULT_SCAN_MINUTES),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_SCAN_MINUTES, max=1440, mode=NumberSelectorMode.BOX
                )
            ),
        }
    )


def _clean_options(user_input: Mapping[str, Any]) -> dict[str, Any]:
    return {
        CONF_DOORS: list(user_input[CONF_DOORS]),
        CONF_HORIZON_DAYS: int(user_input[CONF_HORIZON_DAYS]),
        CONF_SCAN_MINUTES: int(user_input[CONF_SCAN_MINUTES]),
    }


class GuestsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up one calendar against one Access controller."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._connection: dict[str, Any] = {}
        self._doors: list[Door] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> GuestsOptionsFlow:
        """Options: doors, horizon and sync interval."""
        return GuestsOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Controller address and API token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._doors = await _fetch_doors(self.hass, user_input)
            except ValueError:
                errors[CONF_HOST] = "invalid_host"
            except AccessError as err:
                errors["base"] = _error_key(err)
            else:
                if not self._doors:
                    errors["base"] = "no_doors"
                else:
                    self._connection = user_input
                    return await self.async_step_calendar()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_HOST): str,
                        vol.Required(CONF_API_TOKEN): str,
                        vol.Required(CONF_VERIFY_SSL, default=False): bool,
                    }
                ),
                user_input or {},
            ),
            errors=errors,
        )

    async def async_step_calendar(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Which calendar, and which doors its guests may open."""
        errors: dict[str, str] = {}
        if user_input is not None and CONF_CALENDAR in user_input:
            calendar = user_input[CONF_CALENDAR]
            if not user_input[CONF_DOORS]:
                errors[CONF_DOORS] = "no_doors_selected"
            else:
                await self.async_set_unique_id(f"{self._connection[CONF_HOST]}|{calendar}")
                self._abort_if_unique_id_configured()
                state = self.hass.states.get(calendar)
                title = state.name if state else calendar
                return self.async_create_entry(
                    title=title,
                    data={
                        **self._connection,
                        CONF_CALENDAR: calendar,
                        CONF_SECRET: secrets.token_hex(16),
                    },
                    options=_clean_options(user_input),
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_CALENDAR): EntitySelector(
                    EntitySelectorConfig(domain="calendar")
                ),
                **_options_schema(self._doors, {}).schema,
            }
        )
        return self.async_show_form(
            step_id="calendar",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The controller rejected the token."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new token."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_API_TOKEN: user_input[CONF_API_TOKEN]}
            try:
                await _fetch_doors(self.hass, data)
            except AccessError as err:
                errors["base"] = _error_key(err)
            else:
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_TOKEN): str}),
            description_placeholders={"host": entry.data[CONF_HOST]},
            errors=errors,
        )


class GuestsOptionsFlow(OptionsFlowWithReload):
    """Change doors, horizon or sync interval."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Single options form."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input[CONF_DOORS]:
                errors[CONF_DOORS] = "no_doors_selected"
            else:
                return self.async_create_entry(data=_clean_options(user_input))

        try:
            doors = await _fetch_doors(self.hass, self.config_entry.data)
        except AccessError as err:
            return self.async_abort(reason=_error_key(err))

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(doors, user_input or self.config_entry.options),
            errors=errors,
        )
