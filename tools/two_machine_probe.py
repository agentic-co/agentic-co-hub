#!/usr/bin/env python3
"""Two-machine functional probe against a shared AgentCo registry.

Run the SAME file on both hosts, each with its own actor identity, pointed at
one registry. Every step asserts on the host it runs on and prints a one-line
verdict — the caller reads verdicts, never payloads, because the point of a
two-machine test is what each side *concluded*, not what crossed the wire.

    AGENTCO_ACTOR=bigmac AGENTCO_SECRET=... AGENTCO_URL=http://host:8787 \
        python3 tools/two_machine_probe.py claim mabidoli/agentco-hub agentco/beads implement

Exit status is the result: 0 pass, 1 fail. Nothing here mutates a repo; the
registry holds claims and pointers, never document bodies.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentco.publish import Registry, RegistryError  # noqa: E402


def registry() -> Registry:
    missing = [v for v in ("AGENTCO_ACTOR", "AGENTCO_SECRET", "AGENTCO_URL") if not os.environ.get(v)]
    if missing:
        sys.exit(f"FAIL  missing env: {', '.join(missing)}")
    return Registry(os.environ["AGENTCO_ACTOR"], os.environ["AGENTCO_SECRET"], os.environ["AGENTCO_URL"])


def step_auth(reg: Registry, _argv: list[str]) -> int:
    """A successful authenticated read is the whole proof of registration.

    There is no /whoami over HTTP — that tool exists only on the MCP surface —
    so the honest check is that a signed request is accepted where an unsigned
    one is refused. The caller verifies the 401 separately.
    """
    reg.events(limit=1)
    print(f"PASS  {reg.actor} authenticated against {reg.base_url}")
    return 0


def step_claim(reg: Registry, argv: list[str]) -> int:
    repo, prefix, intent = argv[0], argv[1], argv[2]
    expect = argv[3] if len(argv) > 3 else None  # "conflict" | "clean"
    lease = reg.claim_scope(repo, [prefix], intent)
    conflicts = lease.get("conflicts", [])
    who = ", ".join(f"{c.get('withHolder')}({c.get('theirIntent')} vs {intent})" for c in conflicts) or "none"
    print(f"      lease={lease.get('leaseUid')} scope={repo}:{prefix} intent={intent} conflicts={who}")
    if expect == "conflict" and not conflicts:
        print("FAIL  expected a conflict and the registry reported none")
        return 1
    if expect == "clean" and conflicts:
        print("FAIL  expected no conflict — overlap rule is firing too wide")
        return 1
    print(f"PASS  claim {'saw the other holder' if conflicts else 'was clean'}")
    return 0


def step_release(reg: Registry, argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else "released"
    reg.release_scope(argv[0], action)
    print(f"PASS  released {argv[0]} action={action}")
    return 0


def step_snapshot(reg: Registry, argv: list[str]) -> int:
    """Record a pointer, and assert the SERVER could resolve it.

    `resolution` is the field that matters. A snapshot is accepted either way —
    that is deliberate, an unresolvable pointer is still a record of what you
    built against — but only a resolved one can ever report divergence. Asserting
    on acceptance alone passes while the feature is silently dead, which is the
    exact failure the Dockerfile's `git` note is about.
    """
    snap = reg.snapshot(argv[0], argv[1])
    freshness = snap.get("freshness") or {}
    resolution = freshness.get("externalResolution")
    digest = (snap.get("contentHash") or "")[:12] or "-"
    print(f"      uri={argv[0]} resolution={resolution} kind={snap.get('hashKind') or '-'} hash={digest}")
    if resolution != "resolved":
        print(f"FAIL  {reg.actor}: pointer recorded but unresolvable ({freshness.get('reason')}) "
              "— divergence can never fire for it")
        return 1
    print(f"PASS  {reg.actor} snapshot resolved server-side")
    return 0


def step_events(reg: Registry, argv: list[str]) -> int:
    """Read the feed and assert the OTHER machine's activity is visible here."""
    expect_actor = argv[0] if argv else None
    since = argv[1] if len(argv) > 1 else None
    feed = reg.events(since=since, limit=200)
    events = feed.get("events", [])
    actors = sorted({e.get("actor") for e in events if e.get("actor")})
    kinds = sorted({e.get("kind") or e.get("type") for e in events})
    cursor = feed.get("nextCursor")
    print(f"      {len(events)} events actors={actors} kinds={kinds}")
    print(f"      nextCursor={cursor}")
    if expect_actor and expect_actor not in actors:
        print(f"FAIL  {reg.actor} cannot see any activity from {expect_actor} — the feed is not shared")
        return 1
    # Resuming from the cursor must not replay what we just read.
    if cursor:
        again = reg.events(since=cursor, limit=200).get("events", [])
        if again:
            print(f"FAIL  cursor {cursor} replayed {len(again)} events — not resumable")
            return 1
        print("PASS  cursor is resumable (no replay)")
    if expect_actor:
        print(f"PASS  {reg.actor} sees {expect_actor}'s activity in the shared feed")
    return 0


# --- the work queue, across machines ------------------------------------


def step_work_drain(reg: Registry, _argv: list[str]) -> int:
    """Empty the queue so a run cannot inherit a leftover item from the last one.

    The registry's state is a docker volume that outlives any single run. A
    poller that pulls "the next ready item" would otherwise get last run's
    work, and the assertion that the item filed here is the item pulled there
    would pass or fail on history rather than on this run.
    """
    drained = 0
    for _ in range(50):
        pulled = reg.work_pull()
        if pulled.get("state") != "leased":
            break
        item = pulled["item"]
        reg.work_report(item["id"], pulled["attempt"], "failed", result="drained by the probe")
        drained += 1
    print(f"PASS  queue drained ({drained} leftover item{'s' if drained != 1 else ''})")
    return 0


def step_work_create(reg: Registry, argv: list[str]) -> int:
    title, assigned = argv[0], (argv[1] if len(argv) > 1 else None)
    fields = {"assignedAgent": assigned} if assigned else {}
    item = reg.work_create(title, **fields)["item"]
    print(f"      item={item['id']} title={item['title']!r} assigned={item.get('assigned_agent')}")
    print(f"PASS  {reg.actor} filed work")
    return 0


def step_work_pull(reg: Registry, argv: list[str]) -> int:
    """Claim the next item, and assert it is the one the OTHER machine filed."""
    expect_title = argv[0] if argv else None
    pulled = reg.work_pull()
    if pulled.get("state") != "leased":
        print(f"FAIL  {reg.actor} pulled nothing — work filed on the other machine never arrived")
        return 1
    item = pulled["item"]
    print(f"      item={item['id']} attempt={pulled['attempt']} leasedBy={item['leased_by']}")
    if expect_title and item["title"] != expect_title:
        print(f"FAIL  pulled {item['title']!r}, expected {expect_title!r}")
        return 1
    if item["leased_by"] != reg.actor:
        print(f"FAIL  lease recorded against {item['leased_by']!r}, not the authenticated actor")
        return 1
    print(f"PASS  {reg.actor} pulled the item filed on the other machine")
    return 0


def step_work_report(reg: Registry, argv: list[str]) -> int:
    item_id, attempt, status, result = argv[0], int(argv[1]), argv[2], argv[3]
    updated = reg.work_report(item_id, attempt, status, result=result)["item"]
    print(f"      item={item_id} status={updated['status']}")
    print(f"PASS  {reg.actor} reported {status}")
    return 0


def step_work_fence(reg: Registry, argv: list[str]) -> int:
    """A stale attempt must be refused — the fence has to survive the network."""
    item_id, attempt = argv[0], int(argv[1])
    try:
        reg.work_report(item_id, attempt - 1, "done", result="late report")
    except RegistryError as exc:
        print(f"      refused status={exc.status} code={exc.payload.get('code')}")
        if exc.status == 409:
            print("PASS  a stale fence is refused as superseded, across the wire")
            return 0
        print(f"FAIL  refused, but as {exc.status} rather than 409")
        return 1
    print("FAIL  a stale report was ACCEPTED — the fence did not survive HTTP")
    return 1


def step_work_check(reg: Registry, argv: list[str]) -> int:
    """Read back an item's outcome from the other machine."""
    item_id, expect_status, expect_result = argv[0], argv[1], argv[2]
    items = reg.work_list()["items"]
    found = next((i for i in items if i["id"] == item_id), None)
    if found is None:
        print(f"FAIL  {reg.actor} cannot see item {item_id} at all")
        return 1
    print(f"      status={found['status']} result={found.get('result')!r}")
    if found["status"] != expect_status or found.get("result") != expect_result:
        print(f"FAIL  expected {expect_status}/{expect_result!r}")
        return 1
    print(f"PASS  {reg.actor} sees the outcome reported by the other machine")
    return 0


# --- SOPs: a lesson learned on one machine, read on the other -------------


#: The one step this probe's procedure has. Held as a constant because a
#: revision replaces the whole sequence, so the lesson step has to be built
#: from the same body the create used — hand-copying it would make the probe
#: report a carry-forward failure it caused itself.
PROBE_STEP = {
    "name": "run the lane",
    "role": "runner",
    "purpose": "Keep the two-machine lane honest",
    "definition_of_done": "Both machines report the same outcome for the same item",
    "gate": {
        "kind": "deterministic",
        "check": "agentco work --status done",
        "max_park_seconds": 900,
        "on_timeout": "fail",
    },
}


def _step_of(sop: dict) -> dict:
    return (sop.get("steps") or [{}])[0]


def step_sop_create(reg: Registry, argv: list[str]) -> int:
    sop = reg.sop_create(
        argv[0],
        task_type="two-machine-lane",
        purpose="Keep the two-machine lane honest",
        trigger="A cross-machine run is starting",
        roles={"runner": {"kind": "agent"}},
        steps=[dict(PROBE_STEP)],
    )["sop"]
    print(f"      sop={sop['asop_id']} version={sop['version']} status={sop['status']}")
    reg.sop_activate(sop["asop_id"], sop["version"])
    print(f"PASS  {reg.actor} authored and activated the ASOP")
    return 0


def step_sop_lesson(reg: Registry, argv: list[str]) -> int:
    """Record a lesson learned as a new version, and make it the active one.

    The lesson lands on the STEP, which is where v3 keeps the lesson channel —
    so what crosses the machine boundary is a step's `common_mistakes`, not a
    procedure-level field.
    """
    sop_id, lesson = argv[0], argv[1]
    revised = reg.sop_revise(
        sop_id, steps=[{**PROBE_STEP, "common_mistakes": [lesson]}]
    )["sop"]
    reg.sop_activate(sop_id, revised["version"])
    print(f"      sop={sop_id} newVersion={revised['version']}")
    print(f"PASS  {reg.actor} published the lesson as version {revised['version']}")
    return 0


def step_sop_read(reg: Registry, argv: list[str]) -> int:
    """Assert this machine reads the version — and the lesson — the other wrote."""
    sop_id, expect_version = argv[0], int(argv[1])
    expect_lesson = argv[2] if len(argv) > 2 else None
    sop = reg.sop_get(sop_id)["sop"]
    if sop is None:
        print(f"FAIL  {reg.actor} sees no active ASOP {sop_id} — the library is not shared")
        return 1
    step = _step_of(sop)
    print(f"      version={sop['version']} mistakes={step.get('common_mistakes')}")
    if sop["version"] != expect_version:
        print(f"FAIL  read version {sop['version']}, expected {expect_version}")
        return 1
    if expect_lesson and expect_lesson not in (step.get("common_mistakes") or []):
        print("FAIL  the lesson written on the other machine is not on this step")
        return 1
    if sop.get("purpose") is None:
        print("FAIL  revision blanked a field it should have carried forward")
        return 1
    # The gate has to cross too. A shared procedure whose steps arrive
    # ungated is a procedure the other machine cannot be held to.
    if not (step.get("gate") or {}).get("kind"):
        print("FAIL  the step arrived with no gate — the version's check did not cross")
        return 1
    print(f"PASS  {reg.actor} reads v{expect_version}"
          f"{' including the lesson' if expect_lesson else ''}")
    return 0


def step_sop_list(reg: Registry, argv: list[str]) -> int:
    expect_id = argv[0]
    ids = [s["asop_id"] for s in reg.sop_list()["sops"]]
    print(f"      active sops={ids}")
    if expect_id not in ids:
        print(f"FAIL  {expect_id} is not discoverable from {reg.actor}")
        return 1
    print(f"PASS  {reg.actor} discovers the SOP without being told its id")
    return 0


STEPS = {
    "auth": step_auth,
    "claim": step_claim,
    "release": step_release,
    "snapshot": step_snapshot,
    "events": step_events,
    "work_drain": step_work_drain,
    "work_create": step_work_create,
    "work_pull": step_work_pull,
    "work_report": step_work_report,
    "work_fence": step_work_fence,
    "work_check": step_work_check,
    "sop_create": step_sop_create,
    "sop_lesson": step_sop_lesson,
    "sop_read": step_sop_read,
    "sop_list": step_sop_list,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in STEPS:
        sys.exit(f"usage: {sys.argv[0]} {{{'|'.join(STEPS)}}} [args...]")
    try:
        return STEPS[sys.argv[1]](registry(), sys.argv[2:])
    except RegistryError as exc:
        print(f"FAIL  registry refused: status={exc.status} {json.dumps(exc.payload)[:300]}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
