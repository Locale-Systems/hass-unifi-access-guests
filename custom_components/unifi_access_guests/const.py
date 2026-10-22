"""Constants for the UniFi Access Guests integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "unifi_access_guests"

CONF_HOST: Final = "host"
CONF_API_TOKEN: Final = "api_token"
CONF_VERIFY_SSL: Final = "verify_ssl"
CONF_CALENDAR: Final = "calendar_entity_id"
# Random per-entry key for the PIN fingerprint in a visitor's remarks (see
# sync.py). Never shown in the UI or diagnostics.
CONF_SECRET: Final = "secret"

# Options
CONF_DOORS: Final = "doors"
CONF_HORIZON_DAYS: Final = "horizon_days"
CONF_SCAN_MINUTES: Final = "scan_minutes"

DEFAULT_HORIZON_DAYS: Final = 30
DEFAULT_SCAN_MINUTES: Final = 5
MIN_SCAN_MINUTES: Final = 1

# The developer API listens on its own port, not the console's 443.
API_PORT: Final = 12445

# Fallback last name when an event title is a single word. Access requires
# both names on a visitor.
FALLBACK_LAST_NAME: Final = "Guest"

VISIT_REASON: Final = "Others"

DEFAULT_REQUEST_TIMEOUT: Final = timedelta(seconds=15)
