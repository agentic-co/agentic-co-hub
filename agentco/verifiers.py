"""The L3 side of a gate: routing a verdict to somebody entitled to give it.

A `deterministic` gate needs no routing — the process that completed the work
re-runs the check and submits an attestation with its report. A `judged` gate is
the opposite case by construction: it exists *because* the executor may not
grade its own work, so somebody else has to be reachable, and "reachable" for a
work queue means an item they can claim.

So a gate awaiting an answer gets a **vehicle**: an ordinary work item whose
whole content is "go decide this", reachable only by whoever is entitled to
answer — `requires: ["verify"]` for a judged gate, so no node that has not
declared the capability can claim it, and `assigned_agent` set to the gate's
named `verifier` for a human one, so the executor cannot. The verdict itself
still travels through `Queue.attest` against the ORIGINAL item — the vehicle is
how a verifier finds the work, never where the outcome lives. Two records for
one decision would be two places for it to disagree with itself.

**A rejected gate is still owed one.** Parking is not the only state that needs
an answer: the ASOP re-verify invariant keeps a failed unit blocking until its
own gate runs again and passes, so `verify_failed` gets a vehicle too, keyed on
the failure count and governed by the retry policy the failure recorded. One
attempt, one vehicle; the superseded one is retired in the same pass.

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

from datetime import datetime, timedelta, timezone
from typing import Optional

from agentco import events, gates
from agentco.work import RESOLUTION_KEY, LeaseError, Queue, WorkItem, WorkStatus

# Re-exported from `gates` rather than defined twice. `work.attest` enforces it,
# and work.py cannot import this module without a cycle.
VERIFY_CAPABILITY = gates.VERIFY_CAPABILITY

# Gate kinds that need somebody other than the executor. `deterministic` is
# absent on purpose: the executor IS its intended attester.
ROUTED_KINDS = ("judged", "human")

VEHICLE_MARKER = "verifies"

RETRY_KEY = "verify_retry"

# Retry decisions (`gates.retry_decision`) that still want somebody to look.
# `stop` is absent on purpose: the policy has already said there is no third
# autonomous attempt, and a pass that kept offering the item would be the queue
# overruling the decision it recorded one call earlier.
REROUTED_DECISIONS = ("fix", "escalate")


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


def needs_a_verifier(item: WorkItem) -> bool:
    """True while this item's own gate is still waiting for somebody to answer it.

    Two states qualify, and the second is the one L3 missed. `awaiting_verify`
    is the obvious case. `verify_failed` is the ASOP re-verify invariant: a
    failed unit keeps blocking everything downstream until ITS OWN gate runs
    again and passes, so it is still owed a route — and it was never getting
    one, because routing looked at `awaiting_verify` alone and nothing anywhere
    returns an item there. The result was zero live vehicles and a sweep that
    walked past the item forever.

    Which failed items qualify is the retry policy's call, read off the record
    the failure wrote rather than recomputed from the failure count. The
    decision is a fact this queue stored; deriving it again here would be a
    second opinion about it, and the two would eventually disagree. An item
    carrying no decision at all is not routed: nothing said to try again, and
    inventing that is how a `stop` becomes another attempt.
    """
    if is_vehicle(item) or (item.verify or {}).get("kind") not in ROUTED_KINDS:
        return False
    if item.status is WorkStatus.AWAITING_VERIFY:
        return True
    if item.status is not WorkStatus.VERIFY_FAILED:
        return False
    decision = ((item.metadata or {}).get(RETRY_KEY) or {}).get("decision")
    return decision in REROUTED_DECISIONS


def is_vehicle(item: WorkItem) -> bool:
    return bool((item.metadata or {}).get(VEHICLE_MARKER))


def verifies(item: WorkItem) -> Optional[str]:
    return (item.metadata or {}).get(VEHICLE_MARKER)



def origin_of(item: WorkItem) -> dict:
    """Where this work came from, if anywhere. `ext|<source>|<source_id>`.

    Read off the natural key rather than a field of its own, because that is
    where the external connectors already put it — `keys.external_key` is what
    mirrors an ADO or Jira record onto the queue, and inventing a parallel
    `source` column would give two answers to one question. An item filed
    locally has no external key and therefore no origin, which is the correct
    answer and not a missing value.
    """
    key = item.natural_key or ""
    parts = key.split("|")
    if len(parts) != 3 or parts[0] != "ext":
        return {"sourceKey": None, "source": None, "sourceId": None, "sourceUrl": None}
    return {
        "sourceKey": key,
        "source": parts[1],
        "sourceId": parts[2],
        "sourceUrl": (item.metadata or {}).get("url"),
    }


def route_open_gates(queue: Queue, *, conn=None, dry_run: bool = False) -> dict:
    """Give every judged or human gate still owed an answer a vehicle, and
    retire the moot ones.

    Both directions in one pass, because they are the same bookkeeping: a
    verifier can only act on what is routed to it, and a vehicle whose verdict
    arrived elsewhere is a queue entry that will be claimed by somebody with
    nothing to do.

    "Still owed an answer" is `needs_a_verifier`, and it covers a REJECTED gate
    as well as a parked one. That is the half this pass was missing: it routed
    `awaiting_verify` alone, nothing returns a rejected item there, so after a
    verdict-fail the item had no live vehicle and never got another one. Each
    attempt is keyed separately, so the superseded vehicle is retired in the
    same pass that files the new one — otherwise two verifiers hold two items
    for one question.

    `conn` is the registry connection. Given one, the pass emits `WorkParked`
    the first time it notices a parked gate — carrying the PARK time rather than
    the observation time, so a consumer reads when the gate actually started
    waiting and not when a cron happened to look. A re-verify emits nothing: the
    item is not parked, no clock is running on it, and a consumer told otherwise
    would start timing a wait that is not happening. Without a connection the
    pass still routes; making a gate claimable and telling anybody it exists are
    different jobs, and only the second needs a feed.

    Idempotent by inspection rather than by catching a duplicate: `create`
    announces a suppressed duplicate on stderr, which is right for a human
    filing work and wrong for a pass that runs every five minutes — the log
    would fill with reports of the routing working correctly. The natural key
    stays as the backstop for two passes racing.

    **Losing a race is an ordinary answer, not an error.** The queue moves
    between this pass's `list()` and each of its writes, and every such loss is
    per-item: a verifier claimed one vehicle, another router filed one first.
    Raising out of the loop abandoned every item after it, so the pass's own
    reliability depended on nothing happening concurrently — in a work queue.
    Each loss is caught, named in `skipped`, and the pass continues. Same rule
    `reap_expired_leases` has always followed.
    """
    items = queue.list()
    by_id = {i.id: i for i in items}
    routed_for = {verifies(i): i for i in items if is_vehicle(i)}

    created: list[dict] = []
    retired: list[str] = []
    skipped: list[dict] = []

    for item in items:
        if not needs_a_verifier(item):
            continue
        gate = item.verify or {}
        existing = routed_for.get(item.id)
        if existing is not None and existing.natural_key == vehicle_key(item):
            # This attempt is already routed — live, or closed because it was
            # answered or quarantined. One clause rather than two: a vehicle for
            # the CURRENT attempt is owed nothing further whatever state it is
            # in, and a live vehicle for a superseded attempt is exactly what
            # the retire loop below is for. Nothing new appears until the
            # failure count moves, which is what a re-verify does.
            continue

        plan = {
            "verifies": item.id,
            "kind": gate["kind"],
            "requires": [VERIFY_CAPABILITY] if gate["kind"] == "judged" else [],
            # The gate's own `verifier`, never its `escalate_to`. Those answer
            # different questions — who answers this, versus where it goes when
            # nobody does — and assigning from the second gave a human gate
            # declaring `pass` or `fail` a vehicle with no assignee at all,
            # because `escalate_to` cannot be declared on those. `gates` makes
            # `verifier` mandatory for a human gate, so this is never None
            # there; for a judged gate it is optional and narrows the route.
            "assigned_agent": gate.get("verifier"),
            "naturalKey": vehicle_key(item),
        }
        created.append(plan)
        if dry_run:
            continue
        vehicle = queue.create(
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
                # Which attempt this is. A re-verify vehicle is not a fresh
                # review — somebody already said no once — and a verifier who
                # cannot tell the difference from the item in front of them will
                # grade it as if nobody had.
                "attempt": item.verify_failures,
            },
        )
        if (vehicle.metadata or {}).get("natural_key_conflict"):
            # Another router filed this vehicle between our read and our write.
            # `create` returns the existing item rather than raising, which is
            # what every ingest path wants — and it left this pass reporting a
            # create it did not make and announcing a park the other router had
            # already announced. Two `WorkParked` events for one gate is a feed
            # whose consumers double-count.
            created.pop()
            skipped.append({
                "item": item.id,
                "reason": (
                    f"another router already filed {plan['naturalKey']!r} as "
                    f"{vehicle.id} — the vehicle exists and is not ours to announce"
                ),
            })
            continue
        if conn is not None and item.status is WorkStatus.AWAITING_VERIFY:
            # Only a genuinely parked gate. Routing a re-verify is not a park:
            # the item was answered and rejected, its clock is not running, and
            # a consumer told "WorkParked" about it would start counting a wait
            # that is not happening.
            deadline = due_at(item)
            events.append(
                conn,
                kind="WorkParked",
                actor=events.PLANE_ACTOR,
                occurred_at=(_parked_at(item) or _now()).isoformat(),
                payload={
                    "itemId": item.id,
                    "title": item.title,
                    "gateKind": gate["kind"],
                    "check": gate.get("check"),
                    "assignedTo": plan["assigned_agent"],
                    "requires": plan["requires"],
                    "dueAt": deadline.isoformat() if deadline else None,
                    **origin_of(item),
                },
            )

    for vehicle in items:
        if not is_vehicle(vehicle) or vehicle.status in (WorkStatus.DONE, WorkStatus.FAILED):
            continue
        parent_id = verifies(vehicle)
        parent = by_id.get(parent_id)
        if parent is None:
            # Not "the parent has an answer" — the parent is not in this store
            # at all: deleted, or a row this version cannot model, which
            # `_read_all` drops. Tested separately, because every status test
            # written against None is False and this arm is the only thing
            # standing between that vehicle and being permanent work nobody can
            # act on.
            why = f"{parent_id} is not in this store"
        elif not needs_a_verifier(parent):
            why = f"{parent_id} is no longer waiting on a verdict"
        elif vehicle.natural_key != vehicle_key(parent):
            # A live vehicle for an attempt that has been superseded. The gate
            # answered no, the failure count moved, and a fresh vehicle went out
            # above — this one would send a second verifier to re-answer a
            # question that already has an answer.
            why = f"{parent_id} has moved on to attempt {parent.verify_failures}"
        else:
            continue

        # Retiring is not recording an outcome — the outcome lives on the parent
        # — so it closes with a result saying exactly that.
        retired.append(vehicle.id)
        if dry_run:
            continue
        try:
            queue.retire(
                vehicle.id,
                f"retired by routing: {why}. The outcome is on the item "
                f"itself, never here.",
            )
        except LeaseError as exc:
            # A verifier claimed it after the read above. They are working it;
            # the next pass will find it moot again and close it then.
            retired.pop()
            skipped.append({"item": vehicle.id, "reason": str(exc)})

    return {
        "state": "dry-run" if dry_run else "routed",
        "created": created,
        "retired": retired,
        "skipped": skipped,
        "capability": VERIFY_CAPABILITY,
    }


# --------------------------------------------------------------------------- #
# The park clock
# --------------------------------------------------------------------------- #

# How a gate was resolved, recorded on every item the clock touches. A reader
# must be able to tell a VERIFIED pass from a pass nobody gave — otherwise the
# clock quietly manufactures verified work, which is the most expensive lie this
# system could tell about itself.
#
# Defined by `work` and re-exported here, not declared twice: `resolve_by_default`
# writes it and `attest` moves it into HISTORY_KEY when a real verdict lands, so
# the meaning of the key is owned there and read here.
ESCALATED_KEY = "verify_escalated"


def _parked_at(item: WorkItem) -> Optional[datetime]:
    """When the clock started. `updated_at` is the fallback, and it is honest.

    `report_result` stamps `verify_parked_at` when it parks a gated item, so
    anything parked by this version has an exact answer. An item parked by an
    EARLIER version has none, and the moment it last changed is the closest
    truthful thing available — a clock that refused to start on those would
    leave the oldest parked gates, the very ones most likely to be stuck, as the
    only ones nothing ever resolves.
    """
    raw = (item.metadata or {}).get("verify_parked_at") or item.updated_at
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def due_at(item: WorkItem) -> Optional[datetime]:
    """When this item's gate runs out of time, or None if it has no clock."""
    gate = item.verify or {}
    started = _parked_at(item)
    if not gate or started is None:
        return None
    return started + timedelta(seconds=int(gate["max_park_seconds"]))


def sweep_park_clocks(
    queue: Queue,
    *,
    now: Optional[datetime] = None,
    conn=None,
    dry_run: bool = False,
) -> dict:
    """Resolve every gate whose park clock has run out, by its declared default.

    **Correctness without liveness is a deadlock with good intentions.** A gate
    that nobody answers is the ordinary case, not the exception — the reviewer
    is on holiday, the verifier node is misconfigured, the person left. Without
    this, every one of those becomes an item that blocks its dependents forever,
    and the queue's own answer to "what is stuck" is silence.

    So a deadline resolves. `pass` completes the item, `fail` rejects it, and
    `escalate` hands it to the named party and keeps it parked — because
    escalating IS the declared outcome there, and closing it would be answering
    a question the gate explicitly said a person should answer.

    **A default resolution is never recorded as a verdict.** Nothing gets an
    attestation it did not earn: the item carries a resolution record naming the
    clock, the default that fired and how long it waited, so "verified" and
    "nobody looked and the default said pass" are distinguishable forever after.
    The alternative is a queue that manufactures its own green.

    Only `awaiting_verify` has a clock. A `verify_failed` item is not waiting on
    anybody — it is waiting on a fix, which the retry policy governs, and a
    second clock on top of that would close items whose repair was in progress.

    A verdict arriving between the read and a write is refused by
    `resolve_by_default` — correctly, the verdict wins — and that refusal is
    caught per item and reported in `skipped`. Uncaught it ended the pass, so
    one gate answered at the wrong moment left every later due gate parked for
    another cycle, on the mechanism whose entire job is that gated work
    terminates.
    """
    at = now or _now()
    resolved: list[dict] = []
    escalated: list[dict] = []
    skipped: list[dict] = []

    for item in queue.list():
        if item.status is not WorkStatus.AWAITING_VERIFY:
            continue
        deadline = due_at(item)
        if deadline is None or deadline > at:
            continue

        gate = item.verify or {}
        default = gate.get("on_timeout")
        waited = int((at - (_parked_at(item) or at)).total_seconds())
        record = {
            "by": "park-clock",
            "default": default,
            "waited_seconds": waited,
            "declared_seconds": int(gate["max_park_seconds"]),
            "resolved_at": at.isoformat(),
            # Said out loud in the record itself, because this is the field
            # somebody will read six weeks from now while deciding whether to
            # trust the outcome above it.
            "note": "resolved by the declared default, NOT by a verdict — no check was run",
        }

        if default == "escalate":
            if (item.metadata or {}).get(ESCALATED_KEY):
                continue  # already handed over; the digest keeps surfacing it
            escalated.append({"item": item.id, "to": gate.get("escalate_to"), "waited": waited})
            if not dry_run:
                queue.annotate(item.id, {ESCALATED_KEY: {**record, "to": gate.get("escalate_to")}})
                if conn is not None:
                    events.append(
                        conn,
                        kind="GateEscalated",
                        actor=events.PLANE_ACTOR,
                        occurred_at=at.isoformat(),
                        payload={
                            "itemId": item.id,
                            "title": item.title,
                            "to": gate.get("escalate_to"),
                            "waitedSeconds": waited,
                            "declaredSeconds": int(gate["max_park_seconds"]),
                            "check": gate.get("check"),
                            **origin_of(item),
                        },
                    )
            continue

        resolved.append({"item": item.id, "default": default, "waited": waited})
        if dry_run:
            continue
        landing = WorkStatus.DONE if default == "pass" else WorkStatus.VERIFY_FAILED
        try:
            queue.resolve_by_default(item.id, landing, record)
        except LeaseError as exc:
            resolved.pop()
            skipped.append({"item": item.id, "reason": str(exc)})

    return {
        "state": "dry-run" if dry_run else "swept",
        "resolved": resolved,
        "escalated": escalated,
        "skipped": skipped,
    }


# --------------------------------------------------------------------------- #
# "No verifier configured" — a state, not a silence
# --------------------------------------------------------------------------- #


def verifier_status(queue: Queue, *, now: Optional[datetime] = None) -> dict:
    """Is anybody actually answering the gates this queue routes?

    The park clock means gated work no longer parks forever with no verifier —
    an org that never sets up L3 can still use gates, which is the requirement.
    It also creates the failure this report exists to catch: with
    `on_timeout: pass` and nobody verifying, **every gate resolves green on the
    clock and the system manufactures its own approval at scale.** Each item
    carries a resolution record saying no check was run, which is exactly the
    evidence nobody reads one row at a time. So it is reported in aggregate, and
    loudly.

    `configured` is `None`, never `False`, until this queue has routed a gate at
    all. "Nobody has answered a gate" and "no gate has ever needed answering"
    are opposite findings, and a report that renders them the same way will get
    the wrong one believed — the same rule the L1-conversion metric follows.

    Evidence of a verifier is a vehicle that was CLAIMED, not a node that
    declared something. Capabilities are self-asserted and a declaration proves
    only that somebody set an environment variable; a claim is a lease, fenced
    and recorded, and it means a verifier turned up.
    """
    at = now or _now()
    items = queue.list()

    routed = [i for i in items if is_vehicle(i)]
    # `claim()` records every claim in `metadata.claims`, and that is the only
    # evidence that counts. Not `leased_by` (cleared by a report or a reap), not
    # `lease_report` (absent when the holder vanished and was reaped), and never
    # the fence — `lease_attempt` is advanced by reports and reaps that are not
    # claims, and counting it once turned a clock-only queue into "a verifier
    # turned up".
    claimed_ever = [i for i in routed if (i.metadata or {}).get("claims")]
    quarantined_parents = {i.id for i in items if is_quarantined(i)}
    outstanding = [
        i for i in routed
        if i.status not in (WorkStatus.DONE, WorkStatus.FAILED)
        and i.lease_attempt == 0
        and verifies(i) not in quarantined_parents
    ]

    by_verdict = 0
    by_default = 0
    for item in items:
        if is_vehicle(item) or not item.is_gated:
            continue
        # The top-level resolution record IS the final-transition test: `attest`
        # displaces it into HISTORY_KEY when somebody answers for real, so it is
        # present only when the clock had the last word. Before that, an item a
        # reviewer genuinely overturned counted as resolved-by-default, and a
        # queue doing real verification warned that it approved its own work on
        # a timer. One mechanism carries this; a second discriminator here would
        # be a check no test could prove necessary.
        if (item.metadata or {}).get(RESOLUTION_KEY):
            by_default += 1
        elif item.attestation is not None and item.status in (
            WorkStatus.DONE, WorkStatus.VERIFY_FAILED
        ):
            by_verdict += 1

    oldest = None
    for item in outstanding:
        started = _parked_at(item)
        if started is None:
            continue
        age = int((at - started).total_seconds())
        oldest = age if oldest is None or age > oldest else oldest

    if not routed:
        configured: Optional[bool] = None
        verdict = (
            "no gate has been routed to a verifier yet — nothing to measure. This "
            "is NOT 'no verifier configured'; the two are opposite findings."
        )
    elif claimed_ever:
        configured = True
        verdict = (
            f"{len(claimed_ever)} routed gate(s) have been claimed by a verifier; "
            f"{len(outstanding)} outstanding."
        )
    else:
        configured = False
        verdict = (
            f"{len(routed)} gate(s) routed and none ever claimed. Declaring "
            f"{VERIFY_CAPABILITY!r} on a node is what makes them claimable — until "
            f"then every one of them resolves on its park clock instead."
        )

    warning = None
    if by_default and not by_verdict:
        warning = (
            f"{by_default} gate(s) have been resolved by their park clock and NONE by a "
            f"verdict. If those gates declare on_timeout='pass', this queue is "
            f"approving its own work on a timer — the outcomes are real, the "
            f"verification is not."
        )

    return {
        "metric": "VERIFIER-PRESENCE",
        "configured": configured,
        "routedGates": len(routed),
        "claimedEver": len(claimed_ever),
        "outstanding": len(outstanding),
        "oldestOutstandingSeconds": oldest,
        "resolvedByVerdict": by_verdict,
        "resolvedByDefault": by_default,
        "capability": VERIFY_CAPABILITY,
        "verdict": verdict,
        "warning": warning,
    }


# --------------------------------------------------------------------------- #
# Quarantine — abandonment degrades to silence, not to queue noise
# --------------------------------------------------------------------------- #

QUARANTINE_KEY = "verify_quarantined"

# How long an ESCALATED gate may sit before it stops counting as live work.
# A week, because the thing being waited on is a person, and a person who has
# not answered in a week is not about to answer on the eighth day because the
# queue asked again.
QUARANTINE_GRACE_S = 7 * 24 * 3600


def sweep_quarantine(
    queue: Queue,
    *,
    now: Optional[datetime] = None,
    grace_seconds: int = QUARANTINE_GRACE_S,
    dry_run: bool = False,
) -> dict:
    """Move abandoned gates out of the live set and into a list somebody reads.

    Only an ESCALATING gate can reach here, and that is the whole point: `pass`
    and `fail` resolve themselves on the clock, so the sole way to stay parked
    forever is a gate whose declared answer was "ask a person" — and then the
    person did not answer. That is abandonment, and it is the one case the park
    clock deliberately does not close, because closing it would be answering the
    question the gate said a human should answer.

    **Quarantine is not a resolution.** The item stays `awaiting_verify` and
    keeps blocking everything downstream, because nothing about it has been
    decided. What changes is that it stops being offered: its vehicle is
    retired, so a verifier polling the queue is not handed work that has been
    ignored for a week by somebody else. Abandonment degrades to silence rather
    than to noise, and the silence has a list.

    Reversible on purpose. An answer arriving after quarantine closes the item
    exactly as it always would; the flag governs what is OFFERED, never what is
    permitted.

    A vehicle somebody is holding is left alone and named in `skipped`. A live
    lease is somebody finally looking; withdrawing the offer is moot, and this
    sweep does not get to end early over it either.
    """
    at = now or _now()
    quarantined: list[dict] = []
    skipped: list[dict] = []

    for item in queue.list():
        if item.status is not WorkStatus.AWAITING_VERIFY or is_vehicle(item):
            continue
        metadata = item.metadata or {}
        if metadata.get(QUARANTINE_KEY):
            continue
        escalated = metadata.get(ESCALATED_KEY)
        if not escalated:
            continue
        try:
            since = datetime.fromisoformat(escalated["resolved_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        waited = int((at - since).total_seconds())
        if waited < grace_seconds:
            continue

        quarantined.append({
            "item": item.id,
            "title": item.title,
            "to": escalated.get("to"),
            "abandonedSeconds": waited,
        })
        if dry_run:
            continue
        queue.annotate(item.id, {QUARANTINE_KEY: {
            "at": at.isoformat(),
            "escalated_to": escalated.get("to"),
            "unanswered_seconds": waited,
            "note": (
                "not a resolution — the item is still awaiting a verdict and still "
                "blocks its dependents. It has stopped being offered."
            ),
        }})
        for vehicle in queue.list():
            if verifies(vehicle) == item.id and vehicle.status not in (
                WorkStatus.DONE, WorkStatus.FAILED
            ):
                try:
                    queue.retire(
                        vehicle.id,
                        f"retired by quarantine: unanswered for {waited}s after "
                        f"escalation. The gate is still open; it is no longer offered.",
                    )
                except LeaseError as exc:
                    # Somebody is holding it, which means somebody is finally
                    # looking. Withdrawing the offer is moot and taking it out
                    # of their hands would be worse than leaving it.
                    skipped.append({"item": vehicle.id, "reason": str(exc)})

    return {
        "state": "dry-run" if dry_run else "quarantined",
        "quarantined": quarantined,
        "skipped": skipped,
    }


def is_quarantined(item: WorkItem) -> bool:
    return bool((item.metadata or {}).get(QUARANTINE_KEY))


def quarantine_digest(queue: Queue, *, now: Optional[datetime] = None) -> dict:
    """Every abandoned gate, oldest first — the list the silence turns into.

    Sorted by how long it has gone unanswered rather than by when it was filed:
    the reader's question is "what has been ignored longest", and an item that
    has been waiting a month should not be below one that waited eight days
    because it was created later.
    """
    at = now or _now()
    rows = []
    for item in queue.list():
        if not is_quarantined(item):
            continue
        record = (item.metadata or {})[QUARANTINE_KEY]
        started = _parked_at(item)
        rows.append({
            "itemId": item.id,
            "title": item.title,
            "escalatedTo": record.get("escalated_to"),
            "unansweredSeconds": int((at - started).total_seconds()) if started else None,
            "quarantinedAt": record.get("at"),
            "check": (item.verify or {}).get("check"),
            **origin_of(item),
        })
    rows.sort(key=lambda r: r["unansweredSeconds"] or 0, reverse=True)
    return {"stuckGates": rows, "count": len(rows)}


def render_quarantine(digest: dict) -> str:
    """Plain text, leading with the count. Nothing to report says so in one line."""
    rows = digest["stuckGates"]
    if not rows:
        return "Stuck gates: none."
    lines = [f"Stuck gates: {len(rows)} abandoned after escalation."]
    for row in rows:
        days = (row["unansweredSeconds"] or 0) // 86400
        origin = f"  [{row['source']}:{row['sourceId']}]" if row.get("sourceId") else ""
        lines.append(f"  - {row['itemId']} {row['title']}{origin}")
        lines.append(f"      waiting {days}d on {row['escalatedTo'] or '(nobody named)'} — {row['check']}")
    lines.append(
        "These still block their dependents. They are no longer offered to verifiers, "
        "which is why they are here instead of in the queue."
    )
    return "\n".join(lines)
