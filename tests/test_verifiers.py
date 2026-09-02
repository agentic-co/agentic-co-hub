"""Routing a judged gate to somebody entitled to answer it.

A `judged` gate exists because the executor may not grade its own work. That is
only true in practice if somebody else can FIND the work — a rule about who may
decide, with no way to reach a decider, is a rule that resolves by timeout every
time.

Two properties carry the weight here, and each has a mutation behind it: a
vehicle is claimable only by a node declaring the capability, and a vehicle is
never itself gated. The second sounds like housekeeping and is the one that
would take the queue down: a gated vehicle needs a vehicle, which needs a third,
and the growth is silent because every step of it looks like correct routing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentco import verifiers
from agentco.errors import Refusal
from agentco.work import CapabilityError, WorkStatus

JUDGED = {
    "kind": "judged",
    "check": "a reviewer confirms the rollback was exercised",
    "max_park_seconds": 3600,
    "on_timeout": "escalate",
    "escalate_to": "release-owner",
}
HUMAN = {
    "kind": "human",
    "check": "the release owner signs off on the customer-facing wording",
    "max_park_seconds": 604800,
    "on_timeout": "escalate",
    "escalate_to": "dana",
}
DETERMINISTIC = {
    "kind": "deterministic",
    "check": "pytest -q",
    "max_park_seconds": 900,
    "on_timeout": "fail",
}


def attestation(check: str, exit_status: int = 0) -> dict:
    return {
        "check": check,
        "exit_status": exit_status,
        "environment": "reviewer laptop",
        "at": "2026-09-01T12:00:00+00:00",
    }


def park(queue, gate=JUDGED, title="migrate the schema"):
    """File a gated item and get it into `awaiting_verify` the ordinary way."""
    item = queue.create(title, verify=gate)
    claimed = queue.claim(item.id, "executor")
    queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE)
    assert queue.get(item.id).status is WorkStatus.AWAITING_VERIFY
    return item


def vehicles(queue):
    return [i for i in queue.list() if verifiers.is_vehicle(i)]


# --------------------------------------------------------------------------- #
# What gets routed, and what deliberately does not
# --------------------------------------------------------------------------- #


def test_a_parked_judged_gate_gets_a_vehicle_only_a_verifier_can_claim(queue):
    item = park(queue)
    result = verifiers.route_open_gates(queue)

    assert [c["verifies"] for c in result["created"]] == [item.id]
    [vehicle] = vehicles(queue)
    assert vehicle.requires == [verifiers.VERIFY_CAPABILITY]
    assert verifiers.verifies(vehicle) == item.id
    assert item.title in vehicle.title

    with pytest.raises(CapabilityError):
        queue.claim(vehicle.id, "plain-worker", capabilities=[])
    claimed = queue.claim(vehicle.id, "reviewer", capabilities=["verify"])
    assert claimed is not None


def test_a_vehicle_is_never_itself_gated(queue):
    """The regress. A gated vehicle needs a vehicle, and that one needs a third.

    Nothing about it looks like a fault from the outside: every item created is
    a correctly-routed verification of the one before it, and the queue simply
    grows on its own. Dies if `route_open_gates` ever passes `verify=` through.
    """
    park(queue)
    verifiers.route_open_gates(queue)
    [vehicle] = vehicles(queue)
    assert vehicle.verify is None
    assert not vehicle.is_gated

    # And the proof that it terminates: routing again finds nothing new, where a
    # gated vehicle would have produced one more every pass.
    before = len(queue.list())
    verifiers.route_open_gates(queue)
    verifiers.route_open_gates(queue)
    assert len(queue.list()) == before


def test_a_deterministic_gate_is_not_routed_anywhere(queue):
    """Its executor is its intended attester. Routing it would invite a second
    party to answer a question that was never theirs."""
    item = queue.create("ship it", verify=DETERMINISTIC)
    claimed = queue.claim(item.id, "executor")
    queue.report_result(
        item.id, claimed.lease_attempt, WorkStatus.DONE,
        attestation=attestation("pytest -q"),
    )
    assert verifiers.route_open_gates(queue)["created"] == []
    assert vehicles(queue) == []


def test_an_ungated_item_and_an_unparked_one_are_not_routed(queue):
    queue.create("no gate at all")
    queue.create("gated but not yet finished", verify=JUDGED)
    assert verifiers.route_open_gates(queue)["created"] == []


def test_a_human_gate_gets_a_vehicle_assigned_to_the_named_person(queue):
    """And nothing notifies them, which is stated rather than papered over.

    The channel is an open decision. A vehicle assigned to a named person is the
    substrate any channel would read, and on its own it is a queue entry that
    looks like a delivery and is not one — so the digest is the honest floor
    until the decision lands.
    """
    item = park(queue, gate=HUMAN)
    verifiers.route_open_gates(queue)
    [vehicle] = vehicles(queue)
    assert vehicle.assigned_agent == "dana"
    assert vehicle.requires == [], "a person does not declare a machine capability"
    assert verifiers.verifies(vehicle) == item.id


def test_routing_is_idempotent(queue):
    """It runs on a cadence, so a second pass must be a no-op — and quietly.

    `create` announces a suppressed duplicate on stderr, which is right for a
    person filing work and wrong for a job that runs every five minutes: the log
    would fill with reports of the routing working correctly.
    """
    park(queue)
    first = verifiers.route_open_gates(queue)
    assert len(first["created"]) == 1
    for _ in range(3):
        assert verifiers.route_open_gates(queue)["created"] == []
    assert len(vehicles(queue)) == 1


def test_a_dry_run_touches_nothing(queue):
    park(queue)
    plan = verifiers.route_open_gates(queue, dry_run=True)
    assert plan["state"] == "dry-run"
    assert len(plan["created"]) == 1
    assert vehicles(queue) == []


# --------------------------------------------------------------------------- #
# The verdict travels through the item, never through the vehicle
# --------------------------------------------------------------------------- #


def test_the_verdict_closes_the_item_and_retires_the_vehicle(queue):
    item = park(queue)
    verifiers.route_open_gates(queue)
    [vehicle] = vehicles(queue)

    queue.attest(item.id, attestation(JUDGED["check"]), "reviewer", capabilities=["verify"])
    assert queue.get(item.id).status is WorkStatus.DONE

    result = verifiers.route_open_gates(queue)
    assert result["retired"] == [vehicle.id]
    retired = queue.get(vehicle.id)
    assert retired.status is WorkStatus.DONE
    assert "no longer awaiting a verdict" in retired.result
    assert retired.attestation is None, "the evidence belongs to the item, not the vehicle"


def test_a_rejected_gate_gets_a_fresh_vehicle_for_the_next_attempt(queue):
    """Dies if the natural key ignores the failure count.

    The second attempt would then be suppressed as a duplicate of the closed
    first one, and the item would sit in `verify_failed` with nothing routed to
    it — a stall indistinguishable from nobody caring.
    """
    item = park(queue)
    verifiers.route_open_gates(queue)
    [first] = vehicles(queue)

    queue.attest(item.id, attestation(JUDGED["check"], exit_status=1), "reviewer", capabilities=["verify"])
    assert queue.get(item.id).status is WorkStatus.VERIFY_FAILED

    # The failed item is not awaiting anything, so its vehicle is retired.
    verifiers.route_open_gates(queue)
    assert queue.get(first.id).status is WorkStatus.DONE

    # A re-verify parks it again, and THAT gets its own vehicle.
    queue.attest(item.id, attestation(JUDGED["check"]), "reviewer", capabilities=["verify"])
    assert queue.get(item.id).status is WorkStatus.DONE

    keys = {v.natural_key for v in vehicles(queue)}
    assert keys == {f"verify:{item.id}:0"}, keys


def test_a_vehicle_for_a_vanished_parent_is_retired(queue):
    item = park(queue)
    verifiers.route_open_gates(queue)
    [vehicle] = vehicles(queue)
    # Simulate a store where the parent is not readable by this version: the
    # vehicle must not become permanent work nobody can act on.
    queue.attest(item.id, attestation(JUDGED["check"]), "reviewer", capabilities=["verify"])
    assert verifiers.route_open_gates(queue)["retired"] == [vehicle.id]


# --------------------------------------------------------------------------- #
# The capability gates the verdict, not just the route
# --------------------------------------------------------------------------- #


def test_a_judged_verdict_from_a_node_that_declares_nothing_is_refused(queue):
    """Gating the route and leaving the outcome open is a queue that LOOKS routed.

    The vehicle requires `verify` to be claimed. Before this, `attest` checked
    nothing, so anyone holding the item id could answer the gate and the
    declaration was decorative — found by reading the L3 section of the adoption
    guide against the code, which is the cheapest review there is.
    """
    item = park(queue)
    with pytest.raises(Refusal) as caught:
        queue.attest(item.id, attestation(JUDGED["check"]), "reviewer")
    assert "declares (no capabilities)" in caught.value.message
    assert "AGENTCO_CAPABILITIES=verify" in caught.value.remediation
    assert queue.get(item.id).status is WorkStatus.AWAITING_VERIFY, "a refusal moves nothing"

    closed = queue.attest(item.id, attestation(JUDGED["check"]), "reviewer",
                          capabilities=["verify"])
    assert closed.status is WorkStatus.DONE


def test_a_human_gate_asks_no_machine_capability_of_a_person(queue):
    """Symmetry with the vehicle, which gives a human-gated item no `requires`."""
    item = park(queue, gate=HUMAN)
    closed = queue.attest(item.id, attestation(HUMAN["check"]), "dana")
    assert closed.status is WorkStatus.DONE


def test_a_deterministic_gate_asks_no_capability_either(queue):
    """Its executor is its intended attester, and an executor is not an L3 node."""
    item = queue.create("ship it", verify=DETERMINISTIC)
    claimed = queue.claim(item.id, "executor")
    queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE,
                        attestation=attestation("pytest -q", exit_status=1))
    assert queue.get(item.id).status is WorkStatus.VERIFY_FAILED
    cleared = queue.attest(item.id, attestation("pytest -q"), "executor")
    assert cleared.status is WorkStatus.DONE


# --------------------------------------------------------------------------- #
# The park clock — correctness without liveness is a deadlock with good intentions
# --------------------------------------------------------------------------- #

SHORT = {"max_park_seconds": 60}


def gate(kind="judged", on_timeout="fail", **over):
    base = {"kind": kind, "check": f"the {kind} criteria", "on_timeout": on_timeout, **SHORT}
    if on_timeout == "escalate":
        base["escalate_to"] = "dana"
    return {**base, **over}


def later(seconds=120):
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def test_a_gate_nobody_answers_resolves_by_its_declared_default(queue):
    """The ordinary case, not the exception: the reviewer is on holiday, the
    verifier node is misconfigured, the person left. Without a clock every one of
    those blocks its dependents forever and the queue's answer to "what is stuck"
    is silence."""
    passing = park(queue, gate=gate(on_timeout="pass"), title="defaults to pass")
    failing = park(queue, gate=gate(on_timeout="fail"), title="defaults to fail")

    assert verifiers.sweep_park_clocks(queue)["resolved"] == [], "not yet due"

    result = verifiers.sweep_park_clocks(queue, now=later())
    assert {r["item"] for r in result["resolved"]} == {passing.id, failing.id}
    assert queue.get(passing.id).status is WorkStatus.DONE
    assert queue.get(failing.id).status is WorkStatus.VERIFY_FAILED


def test_a_default_pass_is_never_recorded_as_a_verdict(queue):
    """**The lie this system must not tell about itself.** An item that completed
    because nobody looked must stay distinguishable from one that was checked —
    forever, in the store, to a reader six weeks later. Dies if the clock ever
    writes an attestation or omits the resolution record."""
    item = park(queue, gate=gate(on_timeout="pass"))
    verifiers.sweep_park_clocks(queue, now=later())

    closed = queue.get(item.id)
    assert closed.status is WorkStatus.DONE
    assert closed.attestation is None, "no evidence was produced, so none may be stored"
    resolution = closed.metadata["verify_resolution"]
    assert resolution["by"] == "park-clock"
    assert resolution["default"] == "pass"
    assert "no check was run" in resolution["note"]
    assert resolution["declared_seconds"] == 60 and resolution["waited_seconds"] >= 60


def test_a_default_failure_counts_against_the_retry_policy(queue):
    """A gate that timed out is a failure of this attempt, so the same policy
    applies — one fix, then a human, never a third autonomous try."""
    item = park(queue, gate=gate(on_timeout="fail"))
    verifiers.sweep_park_clocks(queue, now=later())
    failed = queue.get(item.id)
    assert failed.verify_failures == 1
    assert failed.metadata["verify_retry"]["decision"] == "fix"


def test_an_escalating_gate_is_handed_over_and_stays_parked(queue):
    """Escalation IS the declared outcome there. Closing it would answer a
    question the gate explicitly said a person should answer."""
    item = park(queue, gate=gate(kind="human", on_timeout="escalate"))
    result = verifiers.sweep_park_clocks(queue, now=later())

    assert result["resolved"] == []
    assert result["escalated"] == [{"item": item.id, "to": "dana", "waited": 120}]
    parked = queue.get(item.id)
    assert parked.status is WorkStatus.AWAITING_VERIFY
    assert parked.metadata["verify_escalated"]["to"] == "dana"


def test_the_sweep_is_idempotent_in_both_directions(queue):
    """It runs on a cadence. A second pass must resolve nothing and escalate
    nobody twice — an escalation re-sent every five minutes is how a person
    learns to filter the channel it arrives on."""
    park(queue, gate=gate(on_timeout="pass"), title="a")
    park(queue, gate=gate(kind="human", on_timeout="escalate"), title="b")
    first = verifiers.sweep_park_clocks(queue, now=later())
    assert len(first["resolved"]) == 1 and len(first["escalated"]) == 1
    for _ in range(3):
        again = verifiers.sweep_park_clocks(queue, now=later())
        assert again["resolved"] == [] and again["escalated"] == []


def test_the_clock_runs_on_parked_gates_only(queue):
    """A `verify_failed` item waits on a fix, not on a person. A second clock on
    top of the retry policy would close items whose repair was in progress."""
    item = park(queue, gate=gate(on_timeout="pass"))
    queue.attest(item.id, attestation(gate(on_timeout="pass")["check"], exit_status=1),
                 "reviewer", capabilities=["verify"])
    assert queue.get(item.id).status is WorkStatus.VERIFY_FAILED
    assert verifiers.sweep_park_clocks(queue, now=later(86400))["resolved"] == []
    assert queue.get(item.id).status is WorkStatus.VERIFY_FAILED


def test_a_default_may_not_overwrite_a_real_verdict(queue):
    """The race: a verifier answers between the sweep listing an item and writing
    to it. The write is refused inside the lock, so the verdict stands."""
    item = park(queue, gate=gate(on_timeout="pass"))
    queue.attest(item.id, attestation(gate(on_timeout="pass")["check"]), "reviewer",
                 capabilities=["verify"])
    with pytest.raises(Exception):
        queue.resolve_by_default(item.id, WorkStatus.VERIFY_FAILED, {"by": "park-clock"})
    assert queue.get(item.id).attestation is not None


def test_a_dry_run_reports_the_deadline_without_moving_anything(queue):
    item = park(queue, gate=gate(on_timeout="pass"))
    plan = verifiers.sweep_park_clocks(queue, now=later(), dry_run=True)
    assert plan["state"] == "dry-run" and len(plan["resolved"]) == 1
    assert queue.get(item.id).status is WorkStatus.AWAITING_VERIFY


def test_an_item_parked_before_the_clock_existed_still_gets_one(queue):
    """`verify_parked_at` is stamped by this version. An item parked by an earlier
    one has none, and refusing to start its clock would leave the OLDEST parked
    gates — the ones most likely to be stuck — as the only ones nothing resolves."""
    item = park(queue, gate=gate(on_timeout="pass"))
    queue.annotate(item.id, {"verify_parked_at": None})
    assert verifiers.due_at(queue.get(item.id)) is not None
    assert len(verifiers.sweep_park_clocks(queue, now=later())["resolved"]) == 1


# --------------------------------------------------------------------------- #
# "No verifier configured" is a state you can read, not a silence
# --------------------------------------------------------------------------- #


def test_nothing_routed_yet_reports_none_and_not_no(queue):
    """Opposite findings. "Nobody has answered a gate" and "no gate has ever
    needed answering" rendered the same way is how the wrong one gets believed —
    the rule the L1-conversion metric already follows."""
    status = verifiers.verifier_status(queue)
    assert status["configured"] is None
    assert "NOT 'no verifier configured'" in status["verdict"]
    assert status["warning"] is None


def test_gates_routed_and_never_claimed_reports_no_verifier(queue):
    park(queue, gate=gate(on_timeout="fail"), title="a")
    verifiers.route_open_gates(queue)
    status = verifiers.verifier_status(queue)
    assert status["configured"] is False
    assert status["routedGates"] == 1 and status["claimedEver"] == 0
    assert "none ever claimed" in status["verdict"]


def test_evidence_of_a_verifier_is_a_claim_and_not_a_declaration(queue):
    """A declaration proves somebody set an environment variable. A claim is a
    lease — fenced, recorded — and means a verifier actually turned up."""
    park(queue, gate=gate(on_timeout="fail"))
    verifiers.route_open_gates(queue)
    [vehicle] = vehicles(queue)
    assert verifiers.verifier_status(queue)["configured"] is False

    queue.claim(vehicle.id, "reviewer", capabilities=["verify"])
    status = verifiers.verifier_status(queue)
    assert status["configured"] is True
    assert status["claimedEver"] == 1


def test_a_queue_approving_itself_on_a_timer_says_so_loudly(queue):
    """**The failure the park clock creates.** With `on_timeout: pass` and nobody
    verifying, every gate resolves green on the clock and the system
    manufactures its own approval at scale. Each item carries a record saying no
    check was run — which is exactly the evidence nobody reads one row at a
    time, so it is reported in aggregate."""
    for n in range(3):
        park(queue, gate=gate(on_timeout="pass"), title=f"item-{n}")
    verifiers.route_open_gates(queue)
    verifiers.sweep_park_clocks(queue, now=later())

    status = verifiers.verifier_status(queue)
    assert status["resolvedByDefault"] == 3
    assert status["resolvedByVerdict"] == 0
    assert "approving its own work on a timer" in status["warning"]


def test_one_real_verdict_clears_the_warning(queue):
    """The warning is about a queue with NO verification happening, not about any
    single default. An org that verifies most gates and lets some lapse is doing
    the normal thing."""
    defaulted = park(queue, gate=gate(on_timeout="pass"), title="lapsed")
    verified = park(queue, gate=gate(on_timeout="pass"), title="checked")
    verifiers.sweep_park_clocks(queue, now=later())
    assert queue.get(defaulted.id).metadata["verify_resolution"]["by"] == "park-clock"
    del verified

    fresh = park(queue, gate=gate(on_timeout="pass"), title="answered")
    queue.attest(fresh.id, attestation(gate()["check"]), "reviewer", capabilities=["verify"])
    assert verifiers.verifier_status(queue)["warning"] is None


def test_the_oldest_outstanding_gate_is_reported(queue):
    park(queue, gate=gate(on_timeout="fail"))
    verifiers.route_open_gates(queue)
    status = verifiers.verifier_status(queue, now=later(3600))
    assert status["outstanding"] == 1
    assert status["oldestOutstandingSeconds"] >= 3600


# --------------------------------------------------------------------------- #
# The change feed carries work-queue events at all
# --------------------------------------------------------------------------- #


@pytest.fixture
def registry(tmp_path):
    from agentco import db

    return db.connect(tmp_path / "registry.sqlite3")


def feed(conn, kind=None):
    from agentco import events as events_mod

    rows = events_mod.read(conn)["events"]
    return [e for e in rows if kind is None or e["kind"] == kind]


def test_routing_a_parked_gate_puts_it_on_the_change_feed(queue, registry):
    """**The substrate that was missing.** The feed is how everything else here
    reaches a harness — the tier-1 splice, the session hook, the digest sender
    all read it — and a work item entering `awaiting_verify` emitted nothing, so
    no tier could surface it even in principle."""
    item = park(queue, gate=gate(kind="human", on_timeout="escalate"))
    verifiers.route_open_gates(queue, conn=registry)

    [event] = feed(registry, "WorkParked")
    assert event["payload"]["itemId"] == item.id
    assert event["payload"]["gateKind"] == "human"
    assert event["payload"]["assignedTo"] == "dana"
    assert event["payload"]["dueAt"] is not None
    assert event["actor"] == "agentco", "the plane observed this; no person caused it"


def test_the_event_carries_the_park_time_not_the_observation_time(queue, registry):
    """A cron looked at 09:05; the gate has been waiting since 08:00. A consumer
    deciding whether that is urgent needs the second number.

    The first version of this test parked and routed in the same second, so
    dropping `occurred_at` entirely changed nothing it could see — it passed
    against a mutation that removed the mechanism. The park time is now pushed
    an hour into the past, which is the only version that can fail.
    """
    item = park(queue)
    an_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    queue.annotate(item.id, {"verify_parked_at": an_hour_ago})

    verifiers.route_open_gates(queue, conn=registry)
    [event] = feed(registry, "WorkParked")
    assert event["occurredAt"] == an_hour_ago, (
        "the feed stamped when the cron looked, not when the gate started waiting"
    )


def test_an_escalation_reaches_the_feed_with_who_and_how_long(queue, registry):
    item = park(queue, gate=gate(kind="human", on_timeout="escalate"))
    verifiers.sweep_park_clocks(queue, now=later(), conn=registry)

    [event] = feed(registry, "GateEscalated")
    payload = event["payload"]
    assert payload["itemId"] == item.id and payload["to"] == "dana"
    assert payload["waitedSeconds"] == 120 and payload["declaredSeconds"] == 60
    assert payload["check"] == gate(kind="human", on_timeout="escalate")["check"]
    # Filed locally, so there is no external record to write back to. Absent is
    # the right answer here, not a gap.
    assert payload["sourceKey"] is None


def test_the_passes_work_without_a_registry_and_emit_nothing(queue):
    """Routing and the clock are about the QUEUE. A caller with no registry
    connection still gets both; it just gets no feed, which is the honest
    degradation — the alternative is a pass that refuses to run because a
    downstream consumer is not configured."""
    park(queue, gate=gate(kind="human", on_timeout="escalate"))
    assert len(verifiers.route_open_gates(queue)["created"]) == 1
    assert len(verifiers.sweep_park_clocks(queue, now=later())["escalated"]) == 1


def test_events_are_not_re_emitted_on_every_pass(queue, registry):
    """The pass runs every few minutes. A feed that repeats itself is a feed
    whose consumers learn to ignore it."""
    park(queue, gate=gate(kind="human", on_timeout="escalate"))
    for _ in range(3):
        verifiers.route_open_gates(queue, conn=registry)
        verifiers.sweep_park_clocks(queue, now=later(), conn=registry)
    assert len(feed(registry, "WorkParked")) == 1
    assert len(feed(registry, "GateEscalated")) == 1


def test_an_event_for_mirrored_work_carries_its_origin(queue, registry):
    """What makes the write-back connector possible at all.

    The origin is read off the natural key, which is where `keys.external_key`
    already puts it when a connector mirrors an ADO or Jira record. A parallel
    `source` column would be a second answer to one question.
    """
    item = queue.create(
        "fix the retry path",
        source="ado",
        source_id="acme/91060",
        verify=gate(kind="human", on_timeout="escalate"),
        metadata={"url": "https://dev.example.com/acme/_workitems/edit/91060"},
    )
    claimed = queue.claim(item.id, "executor")
    queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE)
    verifiers.route_open_gates(queue, conn=registry)

    [event] = feed(registry, "WorkParked")
    assert event["payload"]["source"] == "ado"
    assert event["payload"]["sourceId"] == "acme/91060"
    assert event["payload"]["sourceUrl"].endswith("/91060")


def test_locally_filed_work_reports_no_origin_rather_than_a_blank_one(queue):
    item = queue.create("filed here, belongs here")
    assert verifiers.origin_of(item) == {
        "sourceKey": None, "source": None, "sourceId": None, "sourceUrl": None
    }


# --------------------------------------------------------------------------- #
# Quarantine
# --------------------------------------------------------------------------- #


def abandoned(queue, days=8, title="nobody answered this"):
    """An escalated gate that has gone unanswered past the grace period."""
    item = park(queue, gate=gate(kind="human", on_timeout="escalate"), title=title)
    verifiers.route_open_gates(queue)
    verifiers.sweep_park_clocks(queue, now=later())
    return item, later(days * 86400)


def test_only_an_escalating_gate_can_be_abandoned(queue):
    """`pass` and `fail` resolve themselves on the clock. The sole way to stay
    parked forever is a gate whose declared answer was "ask a person" — and then
    the person did not. That is the case quarantine is for, and the only one.

    The park sweep is deliberately NOT run first. The earlier version of this
    test ran it, which resolved both gates out of `awaiting_verify` — so the
    status filter caught them and the escalation rule was never exercised, and
    the test passed against a mutation that let any parked gate be quarantined.
    Here both are still parked and long overdue, so only the escalation record
    can be the thing that excludes them.
    """
    park(queue, gate=gate(on_timeout="pass"), title="would close itself")
    park(queue, gate=gate(on_timeout="fail"), title="would reject itself")
    assert verifiers.sweep_quarantine(queue, now=later(30 * 86400))["quarantined"] == []
    assert [i.status for i in queue.list()].count(WorkStatus.AWAITING_VERIFY) == 2


def test_an_unanswered_escalation_is_quarantined_after_the_grace_period(queue):
    item, much_later = abandoned(queue)
    assert verifiers.sweep_quarantine(queue, now=later(3 * 86400))["quarantined"] == []
    result = verifiers.sweep_quarantine(queue, now=much_later)
    assert [q["item"] for q in result["quarantined"]] == [item.id]
    assert verifiers.is_quarantined(queue.get(item.id))


def test_quarantine_is_not_a_resolution(queue):
    """**The property that makes this safe.** Nothing has been decided, so the
    item stays parked and keeps blocking everything downstream. What changes is
    that it stops being OFFERED."""
    item, much_later = abandoned(queue)
    downstream = queue.create("depends on it", blocked_by=[item.id])
    verifiers.sweep_quarantine(queue, now=much_later)

    still = queue.get(item.id)
    assert still.status is WorkStatus.AWAITING_VERIFY
    assert still.attestation is None
    assert "verify_resolution" not in still.metadata, "the clock did not close this"
    assert downstream.id not in {i.id for i in queue.ready()}


def test_a_quarantined_gate_stops_being_offered_to_verifiers(queue):
    """A verifier polling the queue must not be handed work another person has
    ignored for a week. That is the difference between silence and noise."""
    item, much_later = abandoned(queue)
    [vehicle] = [v for v in vehicles(queue) if verifiers.verifies(v) == item.id]
    assert vehicle.status is not WorkStatus.DONE

    verifiers.sweep_quarantine(queue, now=much_later)
    assert queue.get(vehicle.id).status is WorkStatus.DONE
    assert "no longer offered" in queue.get(vehicle.id).result
    assert verifiers.verifier_status(queue, now=much_later)["outstanding"] == 0


def test_an_answer_after_quarantine_still_closes_the_item(queue):
    """Reversible on purpose: the flag governs what is offered, never what is
    permitted. A reviewer who comes back from leave is not locked out."""
    item, much_later = abandoned(queue)
    verifiers.sweep_quarantine(queue, now=much_later)
    closed = queue.attest(item.id, attestation(gate(kind="human", on_timeout="escalate")["check"]), "dana")
    assert closed.status is WorkStatus.DONE


def test_quarantine_is_idempotent(queue):
    item, much_later = abandoned(queue)
    assert len(verifiers.sweep_quarantine(queue, now=much_later)["quarantined"]) == 1
    for _ in range(3):
        assert verifiers.sweep_quarantine(queue, now=much_later)["quarantined"] == []
    del item


def test_the_digest_leads_with_the_count_and_sorts_by_neglect(queue):
    """The reader's question is "what has been ignored longest", so an item
    waiting a month must not sit below one waiting eight days because it was
    filed later."""
    assert "none" in verifiers.render_quarantine(verifiers.quarantine_digest(queue))

    old_item, _ = abandoned(queue, title="ignored for ages")
    new_item, _ = abandoned(queue, title="ignored recently")
    queue.annotate(old_item.id, {"verify_parked_at":
                                 (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()})
    much_later = later(9 * 86400)
    verifiers.sweep_quarantine(queue, now=much_later)

    digest = verifiers.quarantine_digest(queue, now=much_later)
    assert digest["count"] == 2
    assert [r["itemId"] for r in digest["stuckGates"]] == [old_item.id, new_item.id]
    text = verifiers.render_quarantine(digest)
    assert text.startswith("Stuck gates: 2")
    assert "still block their dependents" in text


def test_the_digest_carries_the_origin_so_it_can_be_chased(queue):
    item = queue.create("fix the retry path", source="ado", source_id="acme/91060",
                        verify=gate(kind="human", on_timeout="escalate"))
    claimed = queue.claim(item.id, "executor")
    queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE)
    verifiers.sweep_park_clocks(queue, now=later())
    much_later = later(9 * 86400)
    verifiers.sweep_quarantine(queue, now=much_later)
    [row] = verifiers.quarantine_digest(queue, now=much_later)["stuckGates"]
    assert row["sourceId"] == "acme/91060"
    assert "ado:acme/91060" in verifiers.render_quarantine(
        verifiers.quarantine_digest(queue, now=much_later))
