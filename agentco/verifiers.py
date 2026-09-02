"""The L3 side of a gate: routing a verdict to somebody entitled to give it.

A `deterministic` gate needs no routing — the process that completed the work
re-runs the check and submits an attestation with its report. A `judged` gate is
the opposite case by construction: it exists *because* the executor may not
grade its own work, so somebody else has to be reachable, and "reachable" for a
work queue means an item they can claim.

So a parked gate gets a **vehicle**: an ordinary work item carrying
`requires: ["verify"]`, invisible to any node that has not declared the
capability, whose whole content is "go decide this". The verdict itself still
travels through `Queue.attest` against the ORIGINAL item — the vehicle is how a
verifier finds the work, never where the outcome lives. Two records for one
decision would be two places for it to disagree with itself.

**Routing is a pass, not a side effect of reporting.** `report_result` parks the
item and creates nothing, for the same reason it records a retry decision
instead of acting on it: a queue that files work as a consequence of somebody
reporting theirs has started having opinions, and the first surprise is an item
nobody asked for holding a lease nobody expected. This runs on a cadence, is
idempotent, and can be read in a dry run before it is trusted.

**A vehicle is never itself gated.** That is not a detail — a gated vehicle
needs a vehicle, and the second one needs a third. The regress is silent,
produces a queue that grows without anybody doing anything, and the test that
catches it is worth more than the comment.

WHAT IS NOT HERE YET: a human gate gets a vehicle assigned to the named person,
and nothing tells that person it exists. The channel — updating the record the
work originated from, or a surface of AgentCo's own — is an open decision
(deferred by the principal, 2026-09-01), and picking one here to feel finished
would be worse than the gap: it would be an unnotified queue entry that looks
like a delivery. The stuck-gate digest is the honest floor until then.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from agentco import gates
from agentco.work import Queue, WorkItem, WorkStatus

# Re-exported from `gates` rather than defined twice. `work.attest` enforces it,
# and work.py cannot import this module without a cycle.
VERIFY_CAPABILITY = gates.VERIFY_CAPABILITY

# Gate kinds that need somebody other than the executor. `deterministic` is
# absent on purpose: the executor IS its intended attester.
ROUTED_KINDS = ("judged", "human")

VEHICLE_MARKER = "verifies"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def vehicle_key(item: WorkItem) -> str:
    """The natural key for one item's current verification attempt.

    Includes the failure count, so a re-verify after a rejected gate gets its
    own vehicle rather than colliding with the closed one. Without that, the
    second attempt would be silently suppressed as a duplicate and the item
    would sit in `verify_failed` with nothing routed to it — a stall that looks
    exactly like nobody caring.
    """
    return f"verify:{item.id}:{item.verify_failures}"


def is_vehicle(item: WorkItem) -> bool:
    return bool((item.metadata or {}).get(VEHICLE_MARKER))


def verifies(item: WorkItem) -> Optional[str]:
    return (item.metadata or {}).get(VEHICLE_MARKER)


def route_open_gates(queue: Queue, *, dry_run: bool = False) -> dict:
    """Give every parked judged or human gate a vehicle, and retire moot ones.

    Both directions in one pass, because they are the same bookkeeping: a
    verifier can only act on what is routed to it, and a vehicle whose verdict
    arrived elsewhere is a queue entry that will be claimed by somebody with
    nothing to do.

    Idempotent by inspection rather than by catching a duplicate: `create`
    announces a suppressed duplicate on stderr, which is right for a human
    filing work and wrong for a pass that runs every five minutes — the log
    would fill with reports of the routing working correctly. The natural key
    stays as the backstop for two passes racing.
    """
    items = queue.list()
    by_id = {i.id for i in items}
    routed_for = {verifies(i): i for i in items if is_vehicle(i)}

    created: list[dict] = []
    retired: list[str] = []

    for item in items:
        if is_vehicle(item):
            continue
        gate = item.verify or {}
        if gate.get("kind") not in ROUTED_KINDS:
            continue
        if item.status is not WorkStatus.AWAITING_VERIFY:
            continue
        existing = routed_for.get(item.id)
        if existing is not None and existing.status not in (WorkStatus.DONE, WorkStatus.FAILED):
            continue
        if existing is not None and existing.natural_key == vehicle_key(item):
            # Already answered for this attempt. A new one appears only when
            # the failure count moves, which is what a re-verify does.
            continue

        plan = {
            "verifies": item.id,
            "kind": gate["kind"],
            "requires": [VERIFY_CAPABILITY] if gate["kind"] == "judged" else [],
            "assigned_agent": gate.get("escalate_to") if gate["kind"] == "human" else None,
            "naturalKey": vehicle_key(item),
        }
        created.append(plan)
        if dry_run:
            continue
        queue.create(
            f"Verify: {item.title}",
            requires=plan["requires"],
            assigned_agent=plan["assigned_agent"],
            natural_key=plan["naturalKey"],
            # NO `verify=` here, ever. A gated vehicle needs a vehicle of its
            # own, and that one needs a third. See the module docstring.
            metadata={
                VEHICLE_MARKER: item.id,
                "gate": gate,
                "routed_at": _now().isoformat(),
                "criteria": gate.get("check"),
                # Carried so a verifier reading only this item knows what it is
                # judging. A vehicle that says "go decide this" without saying
                # what is being decided sends somebody to read another record
                # first, and the whole point of routing is that they do not
                # have to go looking.
                "subject_title": item.title,
            },
        )

    for vehicle in items:
        if not is_vehicle(vehicle) or vehicle.status in (WorkStatus.DONE, WorkStatus.FAILED):
            continue
        parent_id = verifies(vehicle)
        parent = next((i for i in items if i.id == parent_id), None)
        if parent_id not in by_id or (
            parent is not None and parent.status is not WorkStatus.AWAITING_VERIFY
        ):
            # The verdict arrived through `attest`, or the parent is gone. The
            # vehicle has nothing left to carry. Retiring it is not recording an
            # outcome — the outcome lives on the parent — so it is closed with a
            # result saying exactly that.
            retired.append(vehicle.id)
            if not dry_run:
                queue.report_result(
                    vehicle.id,
                    vehicle.lease_attempt,
                    WorkStatus.DONE,
                    result=(
                        f"retired by routing: {parent_id} is no longer awaiting a "
                        f"verdict. The outcome is on the item itself, never here."
                    ),
                )

    return {
        "state": "dry-run" if dry_run else "routed",
        "created": created,
        "retired": retired,
        "capability": VERIFY_CAPABILITY,
    }
