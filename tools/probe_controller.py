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
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid

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
DOOR_GROUPS_PATH = "/api/v1/developer/door_groups"
VISITOR_PATH = "/api/v1/developer/visitors/{visitor_id}"
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
    # How a lookup of a missing group fails; set once a live group has
    # been seen to read as found (see missing_group_signature).
    missing: str | None = None

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
            if after:
                control = await missing_group_signature(client)
                live = [await door_group_state(client, g, control) for g in after]
                missing = control if all(st == "exists" for st in live) else None
            replaced = before - after
            if not moved:
                s.detail = "skipped: the update did not go through"
            elif not before:
                s.verdict, s.detail = "INFO", "the visitor had no door group to compare"
            elif not replaced:
                s.verdict, s.detail = "PASS", "the update kept the same door group"
            else:
                states = {g: await door_group_state(client, g, missing) for g in replaced}
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
            states = {g: await door_group_state(client, g, missing) for g in sorted(groups)}
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


def _error_signature(err: AccessError) -> str:
    return f"{err.code or ''}|{err.reason}"


async def missing_group_signature(client: AccessClient) -> str | None:
    """How this controller answers a lookup of a group that cannot exist.

    Probe run 4 (2026-10-23): after a DELETE, looking the group up made the
    controller fail with CODE_SYSTEM_ERROR "runtime error: invalid memory
    address or nil pointer dereference" instead of a not-found. A random id
    is the control: an error that matches its answer means "no such group".
    None if even the random id was found, which would make lookups useless.
    """
    try:
        await client._request("GET", f"{DOOR_GROUPS_PATH}/{uuid.uuid4()}")  # noqa: SLF001
    except AccessError as err:
        return _error_signature(err)
    return None


async def door_group_state(
    client: AccessClient, group_id: str, missing: str | None = None
) -> str:
    """'exists', 'gone', or 'unknown: <error>' for GET /door_groups/{id}.

    Gone means a 404, a not-found code, or the same error a made-up id gets
    (``missing``, from ``missing_group_signature``). Anything else stays
    unknown: an error must never pass for a clean-up. Uses the client's
    private request helper because the integration has no reason to read
    door groups.
    """
    try:
        await client._request("GET", f"{DOOR_GROUPS_PATH}/{group_id}")  # noqa: SLF001
    except AccessError as err:
        code = (err.code or "").upper()
        if err.reason == "HTTP 404" or "NOT_FOUND" in code or "NOT_EXIST" in code:
            return "gone"
        if missing is not None and _error_signature(err) == missing:
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
    delete_ids: Sequence[str] = (),
) -> list[Step]:
    """Work out how to stop visitor door groups piling up (LOC-304).

    Every visitor create, and every update that sends doors, makes a door
    group; nothing removes the old one (probe runs 2-4). This finds out:
    - how the controller answers a lookup of a group that doesn't exist (the
      control every "is it gone" check below relies on);
    - whether an update without resources keeps the visitor's group;
    - how a visit can be retired without deleting the visitor: ending it
      early (both times in the past was refused in run 4, so it tries the
      variants) or cancelling it, and whether it can then be reused;
    - whether DELETE /door_groups/{id} removes a group.
    It deletes the groups this run made, and the leftovers named in
    ``delete_ids``. Those must be visitor-* groups no visitor uses, and it
    asks for a yes first.
    """
    steps: list[Step] = []
    made: set[str] = set()  # door groups this run's visitors held
    missing: str | None = None
    # Whether the list shows visitor groups at all. Until a run proves it
    # does, "no leftovers listed" proves nothing (probe run 3: it doesn't).
    visitor_groups_listed = False

    def step(name: str) -> Step:
        steps.append(Step(name))
        say(f"\n== {name}")
        return steps[-1]

    def done(s: Step) -> None:
        say(f"    {s.verdict}: {s.detail}")

    async def group_now(visitor_id: str) -> str | None:
        got = await find(client, visitor_id)
        ids = door_groups_of(got) if got else set()
        made.update(ids)
        return next(iter(ids)) if len(ids) == 1 else None

    async def state(group_id: str) -> str:
        return await door_group_state(client, group_id, missing)

    def no_resources(s: VisitSpec) -> dict:
        return {k: val for k, val in visitor_body(PROBE_ENTRY, s).items() if k != "resources"}

    try:
        s = step("A made-up door group id looks up as")
        missing = await missing_group_signature(client)
        if missing is None:
            s.verdict, s.detail = "FAIL", "FOUND: lookups can't tell a deleted group from a live one"
        else:
            s.verdict, s.detail = "INFO", f"an error: {missing}; the same error later means 'gone'"
        done(s)

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
        except AccessError as err:
            s.verdict, s.detail = "FAIL", f"could not list door groups: {err}"
        done(s)

        t = int(now())
        v = spec(4, door, t + 24 * HOUR, t + 25 * HOUR)
        v_id = await client.create_visitor(visitor_body(PROBE_ENTRY, v))
        first = await group_now(v_id)
        say(f"\n    created probe visitor {v_id} with door group {first}")

        # The control only means something if a live group reads differently.
        s = step("A live visitor's group looks up as found")
        live = await state(first) if first else "missing"
        if live == "exists":
            s.verdict, s.detail = "PASS", f"{first} found"
        else:
            missing = None  # lookups can't tell live from gone; trust no "gone"
            s.verdict, s.detail = "FAIL", f"{first} reads as {live}; no 'gone' below can be trusted"
        done(s)

        s = step("GET /door_groups includes a visitor's own group")
        try:
            listed = {str(g["id"]) for g in await list_door_groups(client)}
            if first and first in listed:
                visitor_groups_listed = True
                s.verdict, s.detail = "PASS", f"{first} is listed"
            else:
                s.verdict = "INFO"
                s.detail = f"{first} is not listed, so leftover visitor groups can't be found by listing"
        except AccessError as err:
            s.verdict, s.detail = "FAIL", f"could not list door groups: {err}"
        done(s)

        s = step("An update without resources keeps the visitor's group")
        moved = VisitSpec(v.key, v.first_name, v.last_name, v.start + HOUR, v.end + HOUR, v.door_ids, None)
        try:
            await client.update_visitor(v_id, no_resources(moved))
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

        # Probe run 4: start and end both in the past was refused with
        # CODE_PARAMS_INVALID. Try the end still (just) in the future, first
        # with the start in the past, then with both ahead.
        s = step("End an upcoming visit early (no resources sent)")
        t = int(now())
        tried: list[str] = []
        ended_ok = False
        for label, start, end in (
            ("start a minute ago, end in a minute", t - 60, t + 60),
            ("start in 5 s, end in a minute", t + 5, t + 60),
        ):
            early = VisitSpec(v.key, v.first_name, v.last_name, start, end, v.door_ids, None)
            try:
                current = await group_now(v_id)
                await client.update_visitor(v_id, no_resources(early))
            except AccessError as err:
                tried.append(f"{label}: refused ({err.code} {err.reason})")
                continue
            got = await find(client, v_id)
            if got is None or (got.start_time, got.end_time) != (early.start, early.end):
                tried.append(f"{label}: accepted, but the window read back differently")
            elif door_groups_of(got) != {current}:
                tried.append(f"{label}: accepted, but the group changed")
            else:
                tried.append(f"{label}: accepted, status {got.status!r}, group kept")
                ended_ok = True
                break
        s.verdict = "PASS" if ended_ok else "FAIL"
        s.detail = "; ".join(tried)
        done(s)

        s = step("Reuse the ended visitor for a later visit (no resources sent)")
        if not ended_ok:
            s.detail = "skipped: ending it early did not work"
        else:
            t = int(now())
            again = VisitSpec(v.key, v.first_name, v.last_name, t + 48 * HOUR, t + 49 * HOUR, v.door_ids, None)
            try:
                current = await group_now(v_id)
                await client.update_visitor(v_id, no_resources(again))
                got = await find(client, v_id)
                if got is None or (got.start_time, got.end_time) != (again.start, again.end):
                    s.verdict, s.detail = "FAIL", "accepted, but the window did not move"
                elif door_groups_of(got) != {current}:
                    s.verdict, s.detail = "FAIL", f"group changed {current} -> {door_groups_of(got)}"
                else:
                    s.verdict, s.detail = "PASS", f"status {got.status!r}; group kept"
            except AccessError as err:
                s.verdict, s.detail = "FAIL", f"refused: {err}"
        done(s)

        s = step("Cancel a visit (DELETE is_force=false), then reuse it")
        t = int(now())
        w = spec(5, door, t + 24 * HOUR, t + 25 * HOUR)
        try:
            w_id = await client.create_visitor(visitor_body(PROBE_ENTRY, w))
            w_group = await group_now(w_id)
            await client._request(  # noqa: SLF001
                "DELETE", VISITOR_PATH.format(visitor_id=w_id), params={"is_force": "false"}
            )
            cancelled = await find(client, w_id)
            if cancelled is None:
                s.verdict, s.detail = "INFO", "cancelling removed the visitor from the list"
            else:
                status = cancelled.status
                later = VisitSpec(w.key, w.first_name, w.last_name, t + 48 * HOUR, t + 49 * HOUR, w.door_ids, None)
                try:
                    await client.update_visitor(w_id, no_resources(later))
                    got = await find(client, w_id)
                    if got is None or (got.start_time, got.end_time) != (later.start, later.end):
                        s.verdict, s.detail = "INFO", f"cancelled ({status!r}); reuse accepted but did not move it"
                    elif door_groups_of(got) != {w_group}:
                        s.verdict, s.detail = "INFO", f"cancelled ({status!r}); reused, but the group changed"
                    else:
                        s.verdict = "PASS"
                        s.detail = f"cancelled ({status!r}), group kept; reused, status {got.status!r}"
                except AccessError as err:
                    s.verdict, s.detail = "INFO", f"cancelled ({status!r}); reuse refused: {err}"
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
            elif (before := await state(old)) != "exists":
                s.verdict, s.detail = "INFO", f"{old} was already {before} before the DELETE"
            else:
                await client._request("DELETE", f"{DOOR_GROUPS_PATH}/{old}")  # noqa: SLF001
                after = await state(old)
                still = await find(client, v_id)
                if after == "gone" and still is not None and door_groups_of(still) == {new}:
                    s.verdict, s.detail = "PASS", f"deleted {old}; the visitor keeps {new}"
                else:
                    s.verdict, s.detail = "FAIL", f"after DELETE, {old} is {after}"
        except AccessError as err:
            s.verdict, s.detail = "FAIL", f"refused: {err}"
        done(s)

        s = step("Deleting the visitor, then its group, leaves nothing")
        last = await group_now(v_id)
        try:
            await client.delete_visitor(v_id)
            if last and (after_visitor := await state(last)) == "gone":
                # Runs 2 and 3 say this doesn't happen; a controller that
                # starts doing it needs no group delete at all.
                s.verdict = "PASS"
                s.detail = f"deleting the visitor removed its group {last} by itself"
            elif last:
                await client._request("DELETE", f"{DOOR_GROUPS_PATH}/{last}")  # noqa: SLF001
                st = await state(last)
                s.verdict = "PASS" if st == "gone" else "FAIL"
                s.detail = f"visitor deleted, group was {after_visitor}; after DELETE it is {st}"
            else:
                s.verdict, s.detail = "INFO", "visitor deleted; it had no single group to delete"
        except AccessError as err:
            s.verdict, s.detail = "FAIL", f"refused: {err}"
        done(s)
    except AccessError as err:
        # Creating or reading a probe visitor failed; the experiments that
        # depend on it can't run.
        s = step("Create and read back a probe visitor")
        s.verdict, s.detail = "FAIL", str(err)
        done(s)
    finally:
        for visitor in await _probe_visitors(client):
            made.update(door_groups_of(visitor))
        await cleanup(client, steps)

    s = step("Delete the door groups this run made")
    states = {}
    for group_id in sorted(made):
        st = await state(group_id)
        if st == "exists":
            try:
                await client._request("DELETE", f"{DOOR_GROUPS_PATH}/{group_id}")  # noqa: SLF001
            except AccessError as err:
                st = f"refused: {err}"
            else:
                st = await state(group_id)
        states[group_id] = st
    left = {g: st for g, st in states.items() if st != "gone"}
    if not states:
        s.verdict, s.detail = "INFO", "this run saw no door groups"
    elif left:
        s.verdict, s.detail = "FAIL", f"{len(left)} of {len(states)} not gone: {left}"
    else:
        s.verdict, s.detail = "PASS", f"all {len(states)} gone"
    done(s)

    if delete_ids:
        s = step("Delete the leftover groups named on the command line")
        await _delete_named_groups(client, s, delete_ids, missing, confirm)
        done(s)

    s = step("Delete leftover visitor door groups (by listing)")
    try:
        orphans = await orphan_visitor_groups(client)
    except AccessError as err:
        s.verdict, s.detail = "FAIL", f"could not list them: {err}"
        done(s)
        return steps
    if not orphans and not visitor_groups_listed:
        s.verdict = "INFO"
        s.detail = "GET /door_groups doesn't list visitor groups, so leftovers can't be found this way"
    elif not orphans:
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


async def _delete_named_groups(
    client: AccessClient,
    s: Step,
    group_ids: Sequence[str],
    missing: str | None,
    confirm: Callable[[list[dict]], Awaitable[bool]] | None,
) -> None:
    """Delete leftover groups by id, if each is a visitor-* group no visitor uses.

    The ids come from earlier runs' output, since the list doesn't show
    visitor groups. Each one is looked up first. Anything that isn't a
    visitor group, or that a visitor still uses, is left alone.
    """
    used: set[str] = set()
    for visitor in await client.list_visitors():
        used |= door_groups_of(visitor)
    candidates: list[dict] = []
    notes: list[str] = []
    for group_id in group_ids:
        st = await door_group_state(client, group_id, missing)
        if st == "gone":
            notes.append(f"{group_id}: already gone")
            continue
        if st != "exists":
            notes.append(f"{group_id}: {st}; left alone")
            continue
        group = await client._request("GET", f"{DOOR_GROUPS_PATH}/{group_id}")  # noqa: SLF001
        name = str(group.get("name", "")) if isinstance(group, dict) else ""
        if not name.startswith("visitor-"):
            notes.append(f"{group_id}: named {name!r}, not a visitor group; left alone")
        elif group_id in used:
            notes.append(f"{group_id}: a visitor still uses it; left alone")
        else:
            candidates.append({"id": group_id, "name": name})
    for g in candidates:
        say(f"    {g['id']}  {g['name']}")
    if not candidates:
        s.verdict, s.detail = "PASS", "; ".join(notes) or "nothing to delete"
        return
    if confirm is None or not await confirm(candidates):
        s.verdict = "INFO"
        s.detail = "; ".join([*notes, f"{len(candidates)} deletable; not deleted"])
        return
    for g in candidates:
        try:
            await client._request("DELETE", f"{DOOR_GROUPS_PATH}/{g['id']}")  # noqa: SLF001
        except AccessError as err:
            notes.append(f"{g['id']}: delete refused: {err}")
            continue
        notes.append(f"{g['id']}: {await door_group_state(client, g['id'], missing)}")
    gone = sum(1 for n in notes if n.endswith(": gone"))
    s.verdict = "PASS" if gone == len(candidates) else "FAIL"
    s.detail = f"deleted {gone} of {len(candidates)}; " + "; ".join(notes)


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
    parser.add_argument("--delete-groups", nargs="+", default=(), metavar="ID",
                        help="with --door-groups: leftover visitor group ids to delete "
                             "(only unused visitor-* groups, after a yes)")
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
                client,
                args.door,
                confirm=terminal_confirm if sys.stdin.isatty() else None,
                delete_ids=args.delete_groups,
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
