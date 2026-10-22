"""Turn calendar events into Access visitors, and plan the changes.

Everything here is pure: no I/O, no Home Assistant state. The coordinator
fetches events and visitors, asks ``plan`` what to do, and carries it out.

Each managed visitor carries a marker in its ``remarks`` field::

    ha-guests:v1:<entry id>:<event key>:<spec digest>:<pin digest>

The marker is the only link between a visitor and the event it came from, and
it lives on the controller, so a reinstalled Home Assistant (or lost storage)
still finds and tidies its own visitors. Visitors without a marker for this
config entry are never touched.

- event key: hash of calendar + event uid + recurrence id, so a moved or
  renamed event updates its visitor instead of replacing it.
- spec digest: hash of everything sent to the controller except the PIN.
- pin digest: keyed hash of the PIN, so a changed PIN is noticed without
  writing the PIN into the remarks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import re

from .api import Visitor
from .const import FALLBACK_LAST_NAME, VISIT_REASON

MARKER_PREFIX = "ha-guests:v1"
_MARKER_RE = re.compile(
    rf"{re.escape(MARKER_PREFIX)}:(?P<entry>[0-9A-Za-z]+)"
    r":(?P<key>[0-9a-f]{16}):(?P<spec>[0-9a-f]{8}):(?P<pin>[0-9a-f]{8})"
)
REMARKS_TEXT = "Managed by Home Assistant from a calendar event; edits here are overwritten."

# "PIN: 4821", "pin=4821", "PIN 4821". Four to twelve digits; the controller
# enforces its own configured PIN length.
_PIN_RE = re.compile(r"\bPIN\s*[:=#]?\s*(\d{4,12})\b", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True, slots=True)
class CalendarItem:
    """The parts of a calendar event that matter here.

    Times are Unix seconds, already resolved from the event's local start and
    end (all-day events run from local midnight to local midnight).
    """

    summary: str
    description: str | None
    start: int
    end: int
    uid: str | None
    recurrence_id: str | None


@dataclass(frozen=True, slots=True)
class VisitSpec:
    """What one visitor should look like on the controller."""

    key: str
    first_name: str
    last_name: str
    start: int
    end: int
    door_ids: tuple[str, ...]
    pin: str | None

    @property
    def name(self) -> str:
        """Full display name."""
        return f"{self.first_name} {self.last_name}"

    def spec_digest(self) -> str:
        """Hash of every field sent in the visitor body."""
        parts = [
            self.first_name,
            self.last_name,
            str(self.start),
            str(self.end),
            ",".join(self.door_ids),
        ]
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:8]


@dataclass(frozen=True, slots=True)
class Marker:
    """A parsed remarks marker."""

    entry_id: str
    key: str
    spec: str
    pin: str


@dataclass(frozen=True, slots=True)
class Update:
    """Bring an existing visitor in line with its event."""

    visitor: Visitor
    spec: VisitSpec
    set_pin: bool


@dataclass(slots=True)
class Plan:
    """Changes needed to make the controller match the calendar."""

    creates: list[VisitSpec] = field(default_factory=list)
    updates: list[Update] = field(default_factory=list)
    deletes: list[Visitor] = field(default_factory=list)
    unchanged: list[tuple[Visitor, VisitSpec]] = field(default_factory=list)
    # Visitors this entry owns that the plan leaves alone because their visit
    # is over. The controller already refuses them; deleting would erase the
    # visit from Access's history.
    ended: list[Visitor] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when nothing needs to change."""
        return not (self.creates or self.updates or self.deletes)


def event_key(calendar_entity_id: str, item: CalendarItem) -> str:
    """Stable identity for one event occurrence.

    Without a uid (some calendar sources omit it) the title and start stand in,
    so moving such an event replaces its visitor rather than updating it.
    """
    if item.uid:
        ident = f"{calendar_entity_id}\x1f{item.uid}\x1f{item.recurrence_id or ''}"
    else:
        ident = f"{calendar_entity_id}\x1f\x1f{item.summary}\x1f{item.start}"
    return hashlib.sha256(ident.encode()).hexdigest()[:16]


def split_name(summary: str) -> tuple[str, str]:
    """Split an event title into first and last name."""
    words = summary.split()
    if not words:
        return "", ""
    if len(words) == 1:
        return words[0], FALLBACK_LAST_NAME
    return words[0], " ".join(words[1:])


def parse_pin(description: str | None) -> str | None:
    """Find 'PIN: 1234' in an event description, ignoring any HTML markup."""
    if not description:
        return None
    match = _PIN_RE.search(_TAG_RE.sub(" ", description))
    return match.group(1) if match else None


def build_spec(
    calendar_entity_id: str, item: CalendarItem, door_ids: list[str]
) -> VisitSpec | None:
    """Turn an event into a visit, or None if it cannot be one."""
    first, last = split_name(item.summary)
    if not first or item.end <= item.start:
        return None
    return VisitSpec(
        key=event_key(calendar_entity_id, item),
        first_name=first,
        last_name=last,
        start=item.start,
        end=item.end,
        door_ids=tuple(sorted(door_ids)),
        pin=parse_pin(item.description),
    )


def pin_digest(secret: str, pin: str | None) -> str:
    """Keyed hash of a PIN; the key keeps a 4-digit PIN from being guessable."""
    return hmac.new(secret.encode(), (pin or "").encode(), hashlib.sha256).hexdigest()[:8]


def make_remarks(entry_id: str, spec: VisitSpec, secret: str) -> str:
    """Remarks text carrying the marker for ``spec``."""
    marker = (
        f"{MARKER_PREFIX}:{entry_id}:{spec.key}:{spec.spec_digest()}"
        f":{pin_digest(secret, spec.pin)}"
    )
    return f"{REMARKS_TEXT} [{marker}]"


def parse_marker(remarks: str) -> Marker | None:
    """Find this integration's marker in a visitor's remarks."""
    match = _MARKER_RE.search(remarks or "")
    if not match:
        return None
    return Marker(
        entry_id=match["entry"], key=match["key"], spec=match["spec"], pin=match["pin"]
    )


def visitor_body(entry_id: str, spec: VisitSpec, secret: str) -> dict[str, object]:
    """Request body for creating or updating a visitor."""
    return {
        "first_name": spec.first_name,
        "last_name": spec.last_name,
        "remarks": make_remarks(entry_id, spec, secret),
        "start_time": spec.start,
        "end_time": spec.end,
        "visit_reason": VISIT_REASON,
        "resources": [{"id": door_id, "type": "door"} for door_id in spec.door_ids],
    }


def _drifted(visitor: Visitor, spec: VisitSpec) -> bool:
    """True when someone changed the visitor on the controller."""
    return (
        visitor.first_name != spec.first_name
        or visitor.last_name != spec.last_name
        or visitor.start_time != spec.start
        or visitor.end_time != spec.end
    )


def plan(
    *,
    entry_id: str,
    secret: str,
    desired: list[VisitSpec],
    visitors: list[Visitor],
    now: int,
    horizon_end: int,
) -> Plan:
    """Work out the creates, updates and deletes.

    ``desired`` holds every visit the calendar shows between ``now`` and
    ``horizon_end``. A visitor this entry owns whose event is gone is deleted
    only while its window is still open or upcoming within the horizon: an
    ended visit stays as history, and one starting past the horizon (left over
    from a longer horizon setting) is outside what ``desired`` can speak for.
    """
    result = Plan()
    wanted = {spec.key: spec for spec in desired if spec.end > now}
    seen: set[str] = set()

    for visitor in visitors:
        marker = parse_marker(visitor.remarks)
        if marker is None or marker.entry_id != entry_id:
            continue
        spec = wanted.get(marker.key)
        if spec is None or marker.key in seen:
            # Gone from the calendar, or a duplicate left by a create whose
            # response was lost.
            ended = visitor.end_time is not None and visitor.end_time <= now
            beyond = visitor.start_time is not None and visitor.start_time >= horizon_end
            if ended:
                result.ended.append(visitor)
            elif not beyond or marker.key in seen:
                result.deletes.append(visitor)
            continue
        seen.add(marker.key)
        set_pin = marker.pin != pin_digest(secret, spec.pin)
        if set_pin or marker.spec != spec.spec_digest() or _drifted(visitor, spec):
            result.updates.append(Update(visitor=visitor, spec=spec, set_pin=set_pin))
        else:
            result.unchanged.append((visitor, spec))

    result.creates = [spec for key, spec in wanted.items() if key not in seen]
    return result
