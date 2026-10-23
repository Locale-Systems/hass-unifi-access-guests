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

1. Create an API token **inside UniFi Access**: Settings → System → API Token,
   at `https://<console>/access/settings/system`. Give it every permission it
   offers. A UniFi OS API key (Settings → Control Plane → Integrations) does
   not work: the Access API rejects it, and setup says so. Store the token
   like a password. The integration itself only reads doors and changes the
   visitors it created.
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

## Checked on a real controller

The first probe run (2026-10-23, `tools/probe_controller.py` below) confirmed:

- visit times are Unix seconds, and read back unchanged;
- PINs are set and removed at `/visitors/{id}/pin_codes`;
- a visitor whose window has ended (status `NO_VISIT`) can be moved to a later
  window, and goes back to `UPCOMING`. This is how the rolling window works;
- the visitor list pages correctly;
- a PIN another visitor holds is refused, but only with the generic
  `CODE_SYSTEM_ERROR` / "Server system error.". The notification therefore
  says the PIN is *most likely* in use and quotes the controller.

It also showed that the controller turns the doors sent with a visitor into a
door group made for that visitor (`visitor-<timestamp>`), and that an update
gives the visitor a new group. The integration never compares the doors it
reads back, so this doesn't make every pass see a change.

Still to check (the probe now covers the first two):

- whether an update or a delete leaves the visitor's old door group behind.
  The rolling window updates once per occurrence, so leftovers would pile up;
- that the duplicate-PIN refusal really is about the duplicate: the probe now
  sets a different PIN on the same visitor as a control;
- that the PIN opens the chosen door for the whole window, and no other door
  (by hand at the door, while the probe pauses);
- a repeating event, and two events with the same PIN, as one visitor that
  moves on after each visit (in Home Assistant).

Known limit: two config entries (two calendars) on the same controller don't
know about each other's guests, so the same PIN on both still collides.

## Probing a real controller

`tools/probe_controller.py` checks the items above against your controller
with the integration's own client, before you install anything in Home
Assistant. It creates up to three visitors named "Probe Visitor" on one door,
then deletes them, along with any probe visitors an interrupted run left behind.
It never touches other visitors. Run it with the dev venv (see Development):

```bash
.venv/bin/python tools/probe_controller.py --host 192.168.1.1 --token-ref "op://Private/UniFi Access API token/credential"
```

Without `--door` it lists the doors and stops. Run it again with `--door <id>`.
In a terminal it pauses while the first visitor's window is open, so you can
try its PIN at that door. It takes about two minutes, because one check waits
for a short visit to end. It ends with a PASS/FAIL summary.

## Development

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The tests run against Home Assistant 2026.10, pinned through
`pytest-homeassistant-custom-component` in `requirements-dev.txt`.
