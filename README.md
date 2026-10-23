# UniFi Access Guests

A Home Assistant integration that turns events on a calendar into UniFi Access
**visitors**. Each event gives one guest access to the doors you choose, for
exactly the event's times. Any calendar Home Assistant can read works: Google,
Office 365, CalDAV, Local Calendar.

It talks to the UniFi Access controller directly and works with or without
either UniFi Access integration (the core one or `imhotep/hass-unifi-access`).

## How events map to visitors

Use a calendar for guests only: **every event on it gives a guest access.**

| Event | Visitor |
| --- | --- |
| `NAME: Jane Doe` in the description | The guest's name. `name = Jane Doe` works too. It needs the colon (or `=`) and ends at the end of its line, or before a PIN on the same line. |
| Title `Jane Doe` | The guest's name when there's no `NAME:` line. A one-word name gets the last name `Guest`. |
| Start and end | The visit window. An all-day event runs from local midnight to local midnight. |
| `PIN: 4821` in the description | The visitor's PIN. `pin=4821` and `PIN 4821` work too; 4 to 12 digits. Without one, the visitor has no PIN, and you can give them a credential in the UniFi Access app. |

### One visitor per guest

A PIN can belong to only one visitor, so events are grouped into guests:

- **Events with the same PIN are one guest**, whether they're a repeating
  event or separate events (Jane this Friday and again next Friday).
- Otherwise, **each repeating event is one guest.**
- Otherwise each event is its own guest.

Each guest's visitor holds one window at a time: the occurrence that's open
now, or the next one (merged with any occurrences that overlap or touch it).
When it ends, the next pass moves the same visitor, and its PIN, on to the
following occurrence. The controller never holds time the calendar doesn't
show, so a biweekly or irregular series works as well as a weekly one.

The controller enforces each window. If Home Assistant is down when a visit
ends, the guest still loses access on time; the cost of downtime is that the
next occurrence, or a new or changed event, starts late.

### Each sync pass

Every 5 minutes by default, or when you press **Sync now**:

- creates a visitor for each new guest within the sync horizon (30 days by default);
- updates the visitor when its window moves on, an event moves or is renamed,
  or someone edits the visitor in UniFi Access (the calendar wins);
- replaces the visitor when its PIN changes;
- deletes the visitor when the guest's events are removed, if the visit hasn't ended;
- leaves ended visits with nothing left on the calendar on the controller, as history.

### When a visit can't sync

If the controller refuses a visit (for example, its PIN already belongs to
someone else), Home Assistant shows a notification naming the guest and the
controller's reason, without the PIN. It's raised once, not every pass, and
clears by itself once the visit syncs. The error is also listed on the
**Visits** sensor. Your calendar events are never edited.

It only touches visitors it created. It recognises them by a marker in the
visitor's remarks, so it never changes visitors you added by hand. If the
calendar can't be read, the pass changes nothing; an unreadable calendar is
never treated as an empty one.

Removing the integration deletes its open and upcoming visitors.

PINs are never shown in entity attributes or diagnostics. They do sit in the
calendar event as plain text, so share the calendar accordingly.

## Setup

1. In UniFi Access, create an API token (Settings → General → API Token) with
   permission to view doors and to view and edit visitors.
2. Copy `custom_components/unifi_access_guests` into your Home Assistant
   `config/custom_components/` (or add this repository to HACS), and restart.
3. Settings → Devices & services → Add integration → **UniFi Access Guests**.
   Enter the controller's address and the token, then choose the calendar and
   the doors its guests may open.

Add the integration once per calendar. For example, a "House guests" calendar
for the front door and a "Pool guests" calendar for the gate.

## Entities

- **Visits**: the number of guests with an open or upcoming visit on the
  controller. Its attributes list each guest's name, current window, whether
  they have a PIN, and how many later occurrences are queued behind it, plus
  any errors from the last pass.
- **Last sync**: when the last pass finished.
- **Sync now**: a button that runs a pass immediately.

## Not yet verified on a real controller

This was built from the Access API reference and tested against a simulated
controller. Check these on a real one before relying on it:

- visit times are sent as Unix seconds;
- a visitor with a start and end time but no weekly schedule can open its doors
  for the whole window;
- PINs are set and removed at `/visitors/{id}/pin_codes`;
- a visitor whose window has ended can be moved to a later window with an
  update (this is how the rolling window works);
- the controller refuses a PIN that another user or visitor already holds, and
  what its error code and message are. Expect a notification; the pass then
  removes the new visitor and retries next time.

Known limit: two config entries (two calendars) on the same controller don't
know about each other's guests, so the same PIN on both still collides.

## Development

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The tests run against Home Assistant 2026.10, pinned through
`pytest-homeassistant-custom-component` in `requirements-dev.txt`.
