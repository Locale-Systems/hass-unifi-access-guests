"""Turn calendar events into Access visitors, and plan the changes.

Everything here is pure: no I/O, no Home Assistant state. The coordinator
fetches events and visitors, asks ``build_specs`` and ``plan`` what to do, and
carries it out.

One visitor stands for a *guest*, not an event. Events group by:

- PIN: every event with the same PIN is one guest, since the controller lets
  a PIN belong to only one visitor. This covers a recurring series and
  separate events alike.
- otherwise the recurring series (calendar + uid);
- otherwise (no uid) the single event.

Each guest's visitor holds a rolling window: the occurrence that is open now,
or the next one, merged with any occurrences that overlap or touch it. When it
ends, the next pass moves the same visitor on to the following occurrence.
The controller never holds more than one window, so it never grants time the
calendar doesn't show.

Each managed visitor carries a marker in its ``remarks`` field::

    ha-guests:v1:<entry id>:<guest key>:<spec digest>

The marker is the only link between a visitor and its guest, and it lives on
the controller, so a reinstalled Home Assistant (or lost storage) still finds
and tidies its own visitors. Visitors without a marker for this config entry
are never touched. The guest key for a PIN is a keyed hash, so the remarks
never reveal the PIN.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import hashlib
import hmac
import html
import re

from .api import Visitor
from .const import FALLBACK_LAST_NAME, VISIT_REASON

MARKER_PREFIX = "ha-guests:v1"
_MARKER_RE = re.compile(
    rf"{re.escape(MARKER_PREFIX)}:(?P<entry>[0-9A-Za-z]+)"
    r":(?P<key>[0-9a-f]{16}):(?P<spec>[0-9a-f]{8})"
)
REMARKS_TEXT = "Managed by Home Assistant from a calendar event; edits here are overwritten."

# "PIN: 4821", "pin=4821", "PIN 4821". Four to twelve digits; the controller
# enforces its own configured PIN length.
_PIN_RE = re.compile(r"\bPIN\s*[:=#]?\s*(\d{4,12})\b", re.IGNORECASE)
# "NAME: Jane Doe" to the end of its line, stopping short of a PIN on the
# same line. The colon (or =) is required: "name" is too common a word.
_NAME_RE = re.compile(
    r"\bNAME[ \t]*[:=][ \t]*(?P<name>[^\n]*?)\s*(?:\bPIN\b.*)?$",
    re.IGNORECASE | re.MULTILINE,
)
# Google returns HTML descriptions. Line-ending tags become newlines so a
# NAME stops at the end of its line; every other tag is dropped.
_BREAK_RE = re.compile(r"<\s*(?:br\s*/?|/\s*(?:p|div|li|h\d))\s*>", re.IGNORECASE)
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
    """What one guest's visitor should look like on the controller."""

    key: str
    first_name: str
    last_name: str
    start: int
    end: int
    door_ids: tuple[str, ...]
    pin: str | None
    # Occurrences still to come after this window, within the horizon.
    later: int = 0

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


@dataclass(slots=True)
class Plan:
    """Changes needed to make the controller match the calendar."""

    creates: list[VisitSpec] = field(default_factory=list)
    updates: list[tuple[Visitor, VisitSpec]] = field(default_factory=list)
    deletes: list[Visitor] = field(default_factory=list)
    unchanged: list[tuple[Visitor, VisitSpec]] = field(default_factory=list)
    # Visitors this entry owns that the plan leaves alone because their guest
    # has nothing left on the calendar and the visit is over. The controller
    # already refuses them; deleting would erase the visit from Access's
    # history.
    ended: list[Visitor] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when nothing needs to change."""
        return not (self.creates or self.updates or self.deletes)


def _plain_text(description: str) -> str:
    return html.unescape(_TAG_RE.sub(" ", _BREAK_RE.sub("\n", description)))


def parse_pin(description: str | None) -> str | None:
    """Find 'PIN: 1234' in an event description."""
    if not description:
        return None
    match = _PIN_RE.search(_plain_text(description))
    return match.group(1) if match else None


def parse_name(description: str | None) -> str | None:
    """Find 'NAME: Jane Doe' in an event description."""
    if not description:
        return None
    match = _NAME_RE.search(_plain_text(description))
    name = " ".join(match["name"].split()) if match else ""
    return name or None


def split_name(name: str) -> tuple[str, str]:
    """Split a full name into first and last name."""
    words = name.split()
    if not words:
        return "", ""
    if len(words) == 1:
        return words[0], FALLBACK_LAST_NAME
    return words[0], " ".join(words[1:])


def guest_key(
    calendar_entity_id: str, item: CalendarItem, pin: str | None, secret: str
) -> str:
    """Which guest an event belongs to; see the module docstring."""
    if pin:
        # Keyed: an unkeyed hash of a 4-digit PIN is trivially reversed.
        return hmac.new(
            secret.encode(), f"pin\x1f{pin}".encode(), hashlib.sha256
        ).hexdigest()[:16]
    if item.uid:
        ident = f"series\x1f{calendar_entity_id}\x1f{item.uid}"
    else:
        ident = f"event\x1f{calendar_entity_id}\x1f{item.summary}\x1f{item.start}"
    return hashlib.sha256(ident.encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class _Occurrence:
    first_name: str
    last_name: str
    start: int
    end: int


def build_specs(
    *,
    calendar_entity_id: str,
    items: list[CalendarItem],
    door_ids: list[str],
    secret: str,
    now: int,
) -> tuple[list[VisitSpec], int]:
    """Group events into guests and give each its current window.

    Returns the specs and the number of events skipped as unusable (no name,
    or an end not after the start).
    """
    groups: dict[str, list[_Occurrence]] = defaultdict(list)
    pins: dict[str, str | None] = {}
    skipped = 0
    for item in items:
        first, last = split_name(parse_name(item.description) or item.summary)
        if not first or item.end <= item.start:
            skipped += 1
            continue
        if item.end <= now:
            continue
        pin = parse_pin(item.description)
        key = guest_key(calendar_entity_id, item, pin, secret)
        groups[key].append(_Occurrence(first, last, item.start, item.end))
        pins[key] = pin

    doors = tuple(sorted(door_ids))
    specs: list[VisitSpec] = []
    for key, occurrences in groups.items():
        occurrences.sort(key=lambda o: (o.start, o.end))
        head = occurrences[0]
        end = head.end
        rest = 0
        for occurrence in occurrences[1:]:
            if occurrence.start <= end:
                end = max(end, occurrence.end)
            else:
                rest += 1
        specs.append(
            VisitSpec(
                key=key,
                first_name=head.first_name,
                last_name=head.last_name,
                start=head.start,
                end=end,
                door_ids=doors,
                pin=pins[key],
                later=rest,
            )
        )
    return specs, skipped


def make_remarks(entry_id: str, spec: VisitSpec) -> str:
    """Remarks text carrying the marker for ``spec``."""
    marker = f"{MARKER_PREFIX}:{entry_id}:{spec.key}:{spec.spec_digest()}"
    return f"{REMARKS_TEXT} [{marker}]"


def parse_marker(remarks: str) -> Marker | None:
    """Find this integration's marker in a visitor's remarks."""
    match = _MARKER_RE.search(remarks or "")
    if not match:
        return None
    return Marker(entry_id=match["entry"], key=match["key"], spec=match["spec"])


def visitor_body(entry_id: str, spec: VisitSpec) -> dict[str, object]:
    """Request body for creating or updating a visitor."""
    return {
        "first_name": spec.first_name,
        "last_name": spec.last_name,
        "remarks": make_remarks(entry_id, spec),
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
    desired: list[VisitSpec],
    visitors: list[Visitor],
    now: int,
    horizon_end: int,
) -> Plan:
    """Work out the creates, updates and deletes.

    ``desired`` holds every guest the calendar shows between ``now`` and
    ``horizon_end``. A guest's existing visitor is reused even after its
    window has ended: that is how the rolling window moves on. A visitor whose
    guest is gone is deleted only while its window is still open or upcoming
    within the horizon: an ended visit stays as history, and one starting past
    the horizon (left over from a longer horizon setting) is outside what
    ``desired`` can speak for.
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
        if marker.spec != spec.spec_digest() or _drifted(visitor, spec):
            result.updates.append((visitor, spec))
        else:
            result.unchanged.append((visitor, spec))

    result.creates = [spec for key, spec in wanted.items() if key not in seen]
    return result
