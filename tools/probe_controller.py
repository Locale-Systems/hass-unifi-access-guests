"""Probe a real UniFi Access controller with the integration's own client.

Answers the questions the simulated controller can't (LOC-304): which time
format the controller takes, whether the PIN endpoint works, what refusing a
duplicate PIN looks like, and whether a visitor whose window has ended can be
moved to a later one (the rolling window depends on it).

It creates up to three visitors named "Probe Visitor", tagged with a probe
marker in their remarks, and deletes every visitor carrying that marker when
it finishes, including leftovers from an earlier run that was interrupted. It
never touches any other visitor.

Run from the repo root with the dev venv:

    .venv/bin/python tools/probe_controller.py --host 192.168.1.1 \\
        --token-ref "op://Private/UniFi Access API token/credential"

Without --door it lists the doors and stops; pick one and run again with
--door <id>. The token comes from 1Password (--token-ref, read with `op read`)
or the UNIFI_ACCESS_TOKEN environment variable, and is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp  # noqa: E402

from custom_components.unifi_access_guests import api  # noqa: E402
from custom_components.unifi_access_guests.api import (  # noqa: E402
    AccessAuthError,
    AccessClient,
    AccessError,
    Visitor,
)
from custom_components.unifi_access_guests.sync import (  # noqa: E402
    VisitSpec,
    parse_marker,
    visitor_body,
)

PROBE_ENTRY = "probe"
HOUR = 3600


@dataclass
class Step:
    """One check and its verdict."""

    name: str
    verdict: str = "SKIP"  # PASS, FAIL, INFO, SKIP
    detail: str = ""


def say(text: str = "") -> None:
    print(text, flush=True)


def when(ts: object) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).astimezone().strftime("%a %d %b %H:%M:%S %Z")
    except (TypeError, ValueError):
        return f"not a Unix time: {ts!r}"


def spec(n: int, door: str, start: int, end: int) -> VisitSpec:
    return VisitSpec(
        key=secrets.token_hex(8),
        first_name="Probe",
        last_name=f"Visitor {n}",
        start=start,
        end=end,
        door_ids=(door,),
        pin=None,
    )


def is_probe(visitor: Visitor) -> bool:
    marker = parse_marker(visitor.remarks)
    return marker is not None and marker.entry_id == PROBE_ENTRY


async def find(client: AccessClient, visitor_id: str) -> Visitor | None:
    return next((v for v in await client.list_visitors() if v.id == visitor_id), None)


def show(visitor: Visitor) -> None:
    say("    as read back:")
    for line in json.dumps(visitor.raw, indent=2, default=str).splitlines():
        say(f"      {line}")


async def run(
    client: AccessClient,
    door: str,
    pin: str,
    *,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    door_check: Callable[[str, int], Awaitable[None]] | None = None,
    short_window: int = 60,
) -> list[Step]:
    """Run every check, always cleaning up. Returns the verdicts."""
    steps: list[Step] = []
    created: list[str] = []
    # Every door group a probe visitor was seen holding (see step 5b).
    groups: set[str] = set()

    def step(name: str) -> Step:
        steps.append(Step(name))
        say(f"\n== {name}")
        return steps[-1]

    try:
        # 1. Times as Unix seconds, and a visitor with no weekly schedule.
        s = step("Create a visitor; times read back as sent (Unix seconds)")
        t = int(now())
        a = spec(1, door, t + 30, t + 30 * 60)
        say(f"    sending start {a.start} ({when(a.start)}), end {a.end} ({when(a.end)})")
        try:
            a_id = await client.create_visitor(visitor_body(PROBE_ENTRY, a))
        except AccessError as err:
            s.verdict, s.detail = "FAIL", f"create refused: {err}"
            say(f"    FAIL: {s.detail}")
            return steps
        created.append(a_id)
        got = await find(client, a_id)
        if got is None:
            s.verdict, s.detail = "FAIL", f"created {a_id} but it is not in the visitor list"
        else:
            show(got)
            groups.update(door_groups_of(got))
            if (got.start_time, got.end_time) == (a.start, a.end):
                s.verdict, s.detail = "PASS", "start_time and end_time came back unchanged"
            else:
                s.verdict = "FAIL"
                s.detail = (
                    f"sent {a.start}/{a.end}, read back "
                    f"{got.raw.get('start_time')!r}/{got.raw.get('end_time')!r}"
                )
        say(f"    {s.verdict}: {s.detail}")

        # 2. The PIN endpoint: set, remove, set again.
        s = step("Set, remove and re-set a PIN at /visitors/{id}/pin_codes")
        try:
            await client.set_visitor_pin(a_id, pin)
            await client.set_visitor_pin(a_id, None)
            await client.set_visitor_pin(a_id, pin)
            s.verdict, s.detail = "PASS", "all three calls accepted"
        except AccessError as err:
            s.verdict, s.detail = "FAIL", str(err)
        say(f"    {s.verdict}: {s.detail}")
        pin_on_a = s.verdict == "PASS"

        # 3. A PIN another visitor holds.
        s = step("Give a second visitor the same PIN (expect a refusal)")
        if not pin_on_a:
            s.detail = "skipped: the first visitor has no PIN"
            say(f"    SKIP: {s.detail}")
        else:
            t = int(now())
            b = spec(2, door, t + 24 * HOUR, t + 25 * HOUR)
            b_id = None
            try:
                b_id = await client.create_visitor(visitor_body(PROBE_ENTRY, b))
                created.append(b_id)
                await client.set_visitor_pin(b_id, pin)
                s.verdict = "FAIL"
                s.detail = (
                    "the controller ACCEPTED a duplicate PIN; grouping by PIN is "
                    "still safe, but the duplicate-PIN premise is wrong"
                )
            except AccessError as err:
                refusal = f"code={err.code!r} reason={err.reason!r} ({err})"
                if b_id is None:
                    s.verdict, s.detail = "FAIL", f"could not create the second visitor: {refusal}"
                else:
                    # Control: the same visitor takes a PIN nobody holds, so
                    # the refusal was about the duplicate, not the visitor.
                    fresh = f"{(int(pin) + 1) % 10 ** len(pin):0{len(pin)}d}"
                    try:
                        await client.set_visitor_pin(b_id, fresh)
                        s.verdict = "PASS"
                        s.detail = f"refused; a different PIN on the same visitor was accepted. {refusal}"
                    except AccessError as control_err:
                        s.verdict = "FAIL"
                        s.detail = (
                            f"refused, but a different PIN was refused too ({control_err}), "
                            f"so the refusal is not shown to be about the duplicate. {refusal}"
                        )
            say(f"    {s.verdict}: {s.detail}")

        # 4. The door, while visitor 1's window is open.
        s = step("Visitor 1's PIN opens the door during its window")
        if door_check is None:
            s.detail = "skipped: not run from a terminal (use --door-check)"
            say(f"    SKIP: {s.detail}")
        elif not pin_on_a:
            s.detail = "skipped: the first visitor has no PIN"
            say(f"    SKIP: {s.detail}")
        else:
            wait = a.start - int(now())
            if wait > 0:
                say(f"    waiting {wait}s for the window to open")
                await sleep(wait)
            await door_check(pin, a.end)
            s.verdict = "INFO"
            s.detail = "checked by hand: see what the door and the Access app did"

        # 5. Move a visitor whose window has ended (the rolling window).
        s = step("Move a visitor whose window has ended to a later window")
        t = int(now())
        c = spec(3, door, t + 10, t + 10 + short_window)
        try:
            c_id = await client.create_visitor(visitor_body(PROBE_ENTRY, c))
            created.append(c_id)
        except AccessError as err:
            s.verdict, s.detail = "FAIL", f"create refused: {err}"
            say(f"    FAIL: {s.detail}")
        else:
            wait = c.end - int(now()) + 15
            say(f"    window {when(c.start)} to {when(c.end)}; waiting {max(wait, 0)}s for it to end")
            if wait > 0:
                await sleep(wait)
            ended = await find(client, c_id)
            before: set[str] = set()
            if ended is not None:
                before = door_groups_of(ended)
                groups.update(before)
                say(f"    status after it ended: {ended.status!r}")
            t = int(now())
            rolled = VisitSpec(c.key, c.first_name, c.last_name, t + 24 * HOUR,
                               t + 25 * HOUR, c.door_ids, None)
            moved = False
            try:
                await client.update_visitor(c_id, visitor_body(PROBE_ENTRY, rolled))
                got = await find(client, c_id)
                if got is not None:
                    show(got)
                    groups.update(door_groups_of(got))
                if got is not None and (got.start_time, got.end_time) == (rolled.start, rolled.end):
                    s.verdict = "PASS"
                    s.detail = f"moved; status now {got.status!r}"
                    moved = True
                else:
                    s.verdict, s.detail = "FAIL", "update accepted but the window did not move"
            except AccessError as err:
                s.verdict, s.detail = "FAIL", f"update refused: {err}"
            say(f"    {s.verdict}: {s.detail}")

            # 5b. The controller turns the doors sent into a door group per
            # visitor. The rolling window updates once per occurrence, so a
            # group left behind by each update would pile up.
            s = step("An update leaves no door group behind")
            after = door_groups_of(got) if moved and got is not None else set()
            replaced = before - after
            if not moved:
                s.detail = "skipped: the update did not go through"
            elif not before:
                s.verdict, s.detail = "INFO", "the visitor had no door group to compare"
            elif not replaced:
                s.verdict, s.detail = "PASS", "the update kept the same door group"
            else:
                states = {g: await door_group_state(client, g) for g in replaced}
                s.verdict, s.detail = _group_verdict(states, "the update made a new group and left the old one")
                if s.verdict == "PASS":
                    s.detail = "the update replaced the group and removed the old one"
        say(f"    {s.verdict}: {s.detail}")

        # 6. Paging gives the same visitors as one big page.
        s = step("The visitor list pages correctly")
        everyone = {v.id for v in await client.list_visitors()}
        saved = api.PAGE_SIZE
        api.PAGE_SIZE = 1
        try:
            paged = [v.id for v in await client.list_visitors()]
        finally:
            api.PAGE_SIZE = saved
        if set(paged) == everyone and len(paged) == len(everyone):
            s.verdict = "PASS"
            s.detail = f"{len(paged)} visitors, one per page, same as unpaged"
        else:
            s.verdict = "FAIL"
            s.detail = f"unpaged {len(everyone)}, paged {len(paged)} ({len(set(paged))} distinct)"
        say(f"    {s.verdict}: {s.detail}")
    finally:
        for visitor in await _probe_visitors(client):
            groups.update(door_groups_of(visitor))
        await cleanup(client, steps)
        s = step("Deleting a visitor removes its door group")
        if not groups:
            s.verdict, s.detail = "INFO", "no door groups were seen"
        else:
            states = {g: await door_group_state(client, g) for g in sorted(groups)}
            s.verdict, s.detail = _group_verdict(states, "left after their visitors were deleted")
            if s.verdict == "FAIL":
                s.detail += "; delete them in the Access app"
        say(f"    {s.verdict}: {s.detail}")
    return steps


def door_groups_of(visitor: Visitor) -> set[str]:
    """Ids of the door groups a visitor's resources read back as."""
    return {
        str(r["id"])
        for r in visitor.raw.get("resources") or []
        if isinstance(r, dict) and r.get("type") == "door_group" and r.get("id")
    }


async def door_group_state(client: AccessClient, group_id: str) -> str:
    """'exists', 'gone', or 'unknown: <error>' for GET /door_groups/{id}.

    Only a 404 or a not-found code counts as gone: how this controller says
    "no such group" is itself unverified, and an error must never pass for
    a clean-up. Uses the client's private request helper because the
    integration has no reason to read door groups.
    """
    try:
        await client._request("GET", f"/api/v1/developer/door_groups/{group_id}")  # noqa: SLF001
    except AccessError as err:
        code = (err.code or "").upper()
        if err.reason == "HTTP 404" or "NOT_FOUND" in code or "NOT_EXIST" in code:
            return "gone"
        return f"unknown: {err}"
    return "exists"


def _group_verdict(states: dict[str, str], what: str) -> tuple[str, str]:
    still = [g for g, st in states.items() if st == "exists"]
    unknown = {g: st for g, st in states.items() if st.startswith("unknown")}
    if still:
        return "FAIL", f"{what}: {len(still)} of {len(states)} still exist: {still}"
    if unknown:
        return "INFO", f"could not tell for {len(unknown)}: {unknown}"
    return "PASS", f"all {len(states)} gone"


DOOR_GROUPS_PATH = "/api/v1/developer/door_groups"


async def list_door_groups(client: AccessClient) -> list[dict]:
    """GET /door_groups as a list of group dicts (private helper; see above)."""
    data = await client._request("GET", DOOR_GROUPS_PATH)  # noqa: SLF001
    if isinstance(data, dict):  # tolerate a wrapped list
        data = next((v for v in data.values() if isinstance(v, list)), [])
    return [g for g in data or [] if isinstance(g, dict) and g.get("id")]


async def orphan_visitor_groups(client: AccessClient) -> list[dict]:
    """visitor-* door groups that no existing visitor (anyone's) refers to."""
    used: set[str] = set()
    for visitor in await client.list_visitors():
        used |= door_groups_of(visitor)
    return [
        g for g in await list_door_groups(client)
        if str(g.get("name", "")).startswith("visitor-") and str(g["id"]) not in used
    ]


async def investigate_door_groups(
    client: AccessClient,
    door: str,
    *,
    now: Callable[[], float] = time.time,
    confirm: Callable[[list[dict]], Awaitable[bool]] | None = None,
) -> list[Step]:
    """Work out how to stop visitor door groups piling up (LOC-304).

    Probe run 2 showed every visitor create and update makes a door group
    and neither an update nor a delete removes the old one. This finds out
    what the controller allows: updating without resources, updating with
    the visitor's own group, and deleting a group. It then offers to delete
    the leftover visitor groups, but only ones no existing visitor uses, and
    only on a yes.
    """
    steps: list[Step] = []

    def step(name: str) -> Step:
        steps.append(Step(name))
        say(f"\n== {name}")
        return steps[-1]

    def done(s: Step) -> None:
        say(f"    {s.verdict}: {s.detail}")

    async def group_now(visitor_id: str) -> str | None:
        got = await find(client, visitor_id)
        ids = door_groups_of(got) if got else set()
        return next(iter(ids)) if len(ids) == 1 else None

    try:
        s = step("GET /door_groups lists the visitor groups")
        try:
            groups = await list_door_groups(client)
            orphans = await orphan_visitor_groups(client)
            visitor_groups = [g for g in groups if str(g.get("name", "")).startswith("visitor-")]
            s.verdict = "INFO"
            s.detail = (
                f"{len(groups)} groups, {len(visitor_groups)} named visitor-*, "
                f"{len(orphans)} of those used by no visitor"
            )
            if orphans:
                say("    one leftover, as GET /door_groups/{id} returns it:")
                sample = await client._request("GET", f"{DOOR_GROUPS_PATH}/{orphans[0]['id']}")  # noqa: SLF001
                for line in json.dumps(sample, indent=2, default=str).splitlines()[:30]:
                    say(f"      {line}")
        except AccessError as err:
            s.verdict, s.detail = "FAIL", f"could not list door groups: {err}"
        done(s)

        t = int(now())
        v = spec(4, door, t + 24 * HOUR, t + 25 * HOUR)
        v_id = await client.create_visitor(visitor_body(PROBE_ENTRY, v))
        first = await group_now(v_id)
        say(f"\n    created probe visitor {v_id} with door group {first}")

        s = step("An update without resources keeps the visitor's group")
        moved = VisitSpec(v.key, v.first_name, v.last_name, v.start + HOUR, v.end + HOUR, v.door_ids, None)
        body = {k: val for k, val in visitor_body(PROBE_ENTRY, moved).items() if k != "resources"}
        try:
            await client.update_visitor(v_id, body)
            after = await group_now(v_id)
            got = await find(client, v_id)
            if got is None or got.start_time != moved.start:
                s.verdict, s.detail = "FAIL", "accepted, but the window did not move"
            elif after == first:
                s.verdict, s.detail = "PASS", f"window moved, group kept ({first})"
            else:
                s.verdict, s.detail = "FAIL", f"window moved, but group changed {first} -> {after}"
        except AccessError as err:
            s.verdict, s.detail = "FAIL", f"refused: {err}"
        done(s)

        s = step("An update sending the visitor's own group keeps it")
        current = await group_now(v_id)
        body = visitor_body(PROBE_ENTRY, moved)
        body["resources"] = [{"id": current, "type": "door_group"}]
        try:
            await client.update_visitor(v_id, body)
            after = await group_now(v_id)
            if after == current:
                s.verdict, s.detail = "PASS", f"group kept ({current})"
            else:
                s.verdict, s.detail = "FAIL", f"group changed {current} -> {after}"
        except AccessError as err:
            s.verdict, s.detail = "FAIL", f"refused: {err}"
        done(s)

        s = step("DELETE /door_groups/{id} removes the group an update replaced")
        old = await group_now(v_id)
        try:
            await client.update_visitor(v_id, visitor_body(PROBE_ENTRY, moved))  # doors again
            new = await group_now(v_id)
            if not old or new == old:
                s.verdict, s.detail = "INFO", f"no replaced group to delete (group {old} -> {new})"
            else:
                await client._request("DELETE", f"{DOOR_GROUPS_PATH}/{old}")  # noqa: SLF001
                state = await door_group_state(client, old)
                still = await find(client, v_id)
                if state == "gone" and still is not None and door_groups_of(still) == {new}:
                    s.verdict, s.detail = "PASS", f"deleted {old}; the visitor keeps {new}"
                else:
                    s.verdict, s.detail = "FAIL", f"after DELETE, {old} is {state}"
        except AccessError as err:
            s.verdict, s.detail = "FAIL", f"refused: {err}"
        done(s)

        s = step("Deleting the visitor, then its group, leaves nothing")
        last = await group_now(v_id)
        try:
            await client.delete_visitor(v_id)
            if last:
                await client._request("DELETE", f"{DOOR_GROUPS_PATH}/{last}")  # noqa: SLF001
                state = await door_group_state(client, last)
                s.verdict = "PASS" if state == "gone" else "FAIL"
                s.detail = f"visitor deleted; its group {last} is {state}"
            else:
                s.verdict, s.detail = "INFO", "visitor deleted; it had no single group to delete"
        except AccessError as err:
            s.verdict, s.detail = "FAIL", f"refused: {err}"
        done(s)
    except AccessError as err:
        # Creating or reading the probe visitor failed; the experiments that
        # depend on it can't run.
        s = step("Create and read back a probe visitor")
        s.verdict, s.detail = "FAIL", str(err)
        done(s)
    finally:
        await cleanup(client, steps)

    s = step("Delete leftover visitor door groups")
    try:
        orphans = await orphan_visitor_groups(client)
    except AccessError as err:
        s.verdict, s.detail = "FAIL", f"could not list them: {err}"
        done(s)
        return steps
    if not orphans:
        s.verdict, s.detail = "PASS", "none left"
    else:
        for g in orphans:
            say(f"    {g['id']}  {g.get('name')}")
        if confirm is None or not await confirm(orphans):
            s.verdict, s.detail = "INFO", f"{len(orphans)} left; not deleted"
        else:
            failed = []
            for g in orphans:
                try:
                    await client._request("DELETE", f"{DOOR_GROUPS_PATH}/{g['id']}")  # noqa: SLF001
                except AccessError as err:
                    failed.append(f"{g['id']}: {err}")
            remaining = await orphan_visitor_groups(client)
            s.verdict = "FAIL" if failed or remaining else "PASS"
            s.detail = f"deleted {len(orphans) - len(failed)} of {len(orphans)}; {len(remaining)} left"
            if failed:
                s.detail += f"; errors: {failed}"
    done(s)
    return steps


async def terminal_confirm(orphans: list[dict]) -> bool:
    say(f"    These {len(orphans)} visitor-* door groups are used by no visitor.")
    answer = await asyncio.to_thread(input, "    Delete them? Type yes to delete: ")
    return answer.strip().lower() == "yes"


async def _probe_visitors(client: AccessClient) -> list[Visitor]:
    try:
        return [v for v in await client.list_visitors() if is_probe(v)]
    except AccessError:
        return []


async def cleanup(client: AccessClient, steps: list[Step]) -> None:
    s = Step("Delete every probe visitor (is_force=true)")
    steps.append(s)
    say(f"\n== {s.name}")
    try:
        probes = [v for v in await client.list_visitors() if is_probe(v)]
        for visitor in probes:
            await client.delete_visitor(visitor.id)
        left = [v.id for v in await client.list_visitors() if is_probe(v)]
    except AccessError as err:
        s.verdict, s.detail = "FAIL", f"{err}; delete 'Probe Visitor' entries by hand"
    else:
        if left:
            s.verdict, s.detail = "FAIL", f"still present after delete: {left}"
        else:
            s.verdict, s.detail = "PASS", f"deleted {len(probes)}; none left"
    say(f"    {s.verdict}: {s.detail}")


def read_token(ref: str | None) -> str:
    if ref:
        try:
            out = subprocess.run(["op", "read", ref], check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as err:
            sys.exit(f"Could not read the token from 1Password ({ref}): {err}")
        return out.stdout.strip()
    token = os.environ.get("UNIFI_ACCESS_TOKEN", "").strip()
    if not token:
        sys.exit("Give --token-ref (a 1Password op:// reference) or set UNIFI_ACCESS_TOKEN.")
    return token


async def explain_rejected_token(client: AccessClient, host: str, token: str) -> None:
    """Say why the Access API rejected the token, as far as can be told."""
    where = f"https://{host}/access/settings/system"
    app = await client.unifi_os_key_app()
    if app:
        say(f"\nThis is a UniFi OS API key: {app}'s integration API accepts it.")
        say("The Access developer API needs a token made inside UniFi Access:")
        say(f"  {where} -> API Token. Give it every permission it offers.")
        return
    shape = "letters, digits, - and _ only"
    if any(c.isspace() for c in token):
        shape = "CONTAINS WHITESPACE (wrong 1Password field?)"
    elif not all(c.isalnum() or c in "-_=+/." for c in token):
        shape = "contains unusual characters (wrong 1Password field?)"
    say("\nThe token is not a UniFi OS key either. Check that:")
    say(f"  - it was made in UniFi Access at {where} (not in UniFi OS settings);")
    say("  - it has not expired (Access tokens can be created with a validity period);")
    say(f"  - --token-ref names the right field: what it returned is {len(token)} characters, {shape}.")


async def terminal_door_check(pin: str, end: int) -> None:
    say(f"    The window is open until {when(end)}.")
    say(f"    Enter PIN {pin} at the door now: it should open.")
    say("    Then try it at a door you did NOT pick: it should be refused.")
    say("    Check the visit in the Access app too.")
    await asyncio.to_thread(input, "    Press Enter when done... ")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True, help="console IP or hostname")
    parser.add_argument("--token-ref", help="1Password reference, e.g. op://Vault/Item/credential")
    parser.add_argument("--door", help="door id to grant; omit to list doors")
    parser.add_argument("--pin", default=f"{secrets.randbelow(900000) + 100000}",
                        help="test PIN (default: random 6 digits)")
    parser.add_argument("--verify-ssl", action="store_true", help="check the console's certificate")
    parser.add_argument("--door-groups", action="store_true",
                        help="investigate door group clean-up instead of the main checks")
    parser.add_argument("--door-check", action=argparse.BooleanOptionalAction,
                        default=sys.stdin.isatty(),
                        help="pause to try the PIN at the door (default: on in a terminal)")
    args = parser.parse_args()

    token = read_token(args.token_ref)
    connector = aiohttp.TCPConnector(ssl=None if args.verify_ssl else False)
    async with aiohttp.ClientSession(connector=connector) as session:
        client = AccessClient(args.host, token, session)
        try:
            doors = await client.get_doors()
        except AccessAuthError as err:
            say(f"Could not list doors: {err}")
            await explain_rejected_token(client, args.host, token)
            return 1
        except AccessError as err:
            say(f"Could not list doors: {err}")
            return 1
        if not args.door:
            say("Doors:")
            for door in doors:
                say(f"  {door.id}  {door.name}")
            say("\nRun again with --door <id>.")
            return 0
        if args.door not in {d.id for d in doors}:
            say(f"No door {args.door!r}; run without --door to list them.")
            return 1

        if args.door_groups:
            steps = await investigate_door_groups(
                client, args.door, confirm=terminal_confirm if sys.stdin.isatty() else None
            )
        else:
            steps = await run(
                client,
                args.door,
                args.pin,
                door_check=terminal_door_check if args.door_check else None,
            )

    say("\nSummary")
    for s in steps:
        say(f"  {s.verdict:<4}  {s.name}: {s.detail}")
    return 1 if any(s.verdict == "FAIL" for s in steps) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
