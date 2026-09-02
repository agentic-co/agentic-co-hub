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
