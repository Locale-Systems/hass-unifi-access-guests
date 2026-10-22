# UniFi Access Guests

A Home Assistant integration that turns events on a calendar into UniFi Access
**visitors**. Each event gives one guest access to the doors you choose, for
exactly the event's times. Any calendar Home Assistant can read works: Google,
Office 365, CalDAV, Local Calendar.

It talks to the UniFi Access controller directly and works with or without
either UniFi Access integration (the core one or `imhotep/hass-unifi-access`).

## How events map to visitors

Use a calendar for guests only: **every event on it becomes a visit.**

| Event | Visitor |
| --- | --- |
| Title `Jane Doe` | First name `Jane`, last name `Doe`. A one-word title gets the last name `Guest`. |
| Start and end | The visit window. An all-day event runs from local midnight to local midnight. |
| `PIN: 4821` in the description | The visitor's PIN. `pin=4821` and `PIN 4821` work too; 4 to 12 digits. Without one, the visitor has no PIN, and you can give them a credential in the UniFi Access app. |
| Each occurrence of a repeating event | A separate visitor. |

The controller enforces each visit's window. If Home Assistant is down when a
visit ends, the guest still loses access on time; new and changed events just
sync late.

Every sync pass (every 5 minutes by default, or when you press **Sync now**):

- creates a visitor for each new event within the sync horizon (30 days by default);
- updates the visitor when an event moves, is renamed, changes PIN, or when
  someone edits the visitor in UniFi Access (the calendar wins);
- deletes the visitor when its event is removed, if the visit hasn't ended;
- leaves ended visits on the controller, as history.

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

- **Visits**: the number of open and upcoming visits on the controller. Its
  attributes list each visit's name, times and whether it has a PIN, plus any
  errors from the last pass.
- **Last sync**: when the last pass finished.
- **Sync now**: a button that runs a pass immediately.

## Not yet verified on a real controller

This was built from the Access API reference and tested against a simulated
controller. Check these on a real one before relying on it:

- visit times are sent as Unix seconds;
- a visitor with a start and end time but no weekly schedule can open its doors
  for the whole window;
- PINs are set and removed at `/visitors/{id}/pin_codes`;
- what happens when a guest's PIN is already in use. Expect an error on the
  **Visits** sensor; the pass then removes that new visitor and retries next time.

## Development

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The tests run against Home Assistant 2026.10, pinned through
`pytest-homeassistant-custom-component` in `requirements-dev.txt`.
