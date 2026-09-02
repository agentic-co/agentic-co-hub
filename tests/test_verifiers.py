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
from agentco.work import BlockedError, CapabilityError, WorkStatus

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
    # Deliberately DIFFERENT names. Who answers the gate and where it goes when
    # nobody does are different questions, and a fixture that answers both with
    # one name cannot tell a vehicle assigned from the right field from one
    # assigned from the wrong one — which is how the conflation survived.
    "escalate_to": "release-owner",
    "verifier": "dana",
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


def test_a_deterministic_gate_that_failed_its_check_is_not_routed_either(queue):
    """Routing follows the gate KIND, not the status alone. A deterministic
    gate's executor IS its attester — that is the whole of what makes it
    deterministic — so a rejected one is owed a re-run by the same process, not
    a reviewer.

    It is also the FIX-L3.4 hole arriving through a second door: a deterministic
    gate may name no verifier and requires no capability, so its vehicle would
    be assigned to nobody and claimable by anyone.
    """
    item = queue.create("ship it", verify=DETERMINISTIC)
    claimed = queue.claim(item.id, "executor")
    queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE,
                        attestation=attestation("pytest -q", exit_status=1))
    assert queue.get(item.id).status is WorkStatus.VERIFY_FAILED
    assert queue.get(item.id).metadata["verify_retry"]["decision"] == "fix"

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


def test_a_human_gate_is_offered_to_the_person_it_names_and_to_nobody_else(queue):
    """FIX-L3.4. A human vehicle used to be assigned to `escalate_to`, and
    `validate_gate` refuses `escalate_to` unless `on_timeout` is `escalate` — so
    every human gate that resolved on its own clock produced a vehicle with
    `assigned_agent=None` and `requires=[]`. `ready()` offered it to the
    executor, who claimed it with no capabilities and reported it done.

    Dies if the vehicle is assigned from anything but the gate's own `verifier`.
    """
    for on_timeout in ("pass", "fail", "escalate"):
        item = park(queue, gate=gate(kind="human", on_timeout=on_timeout),
                    title=f"a person answers this ({on_timeout})")
        verifiers.route_open_gates(queue)
        [vehicle] = [v for v in vehicles(queue) if verifiers.verifies(v) == item.id]

        assert vehicle.assigned_agent == "dana", (
            f"on_timeout={on_timeout!r} produced a vehicle nobody owns"
        )
        with pytest.raises(BlockedError):
            queue.claim(vehicle.id, "executor")
        assert vehicle.id not in {i.id for i in queue.ready("executor")}
        assert queue.claim(vehicle.id, "dana") is not None


def test_a_stored_human_gate_with_no_verifier_is_reported_not_routed(queue):
    """F3, the reviewer's second reproduction. `validate_gate` has refused an
    unnamed human gate at the write boundary since FIX-L3.4, but that boundary
    is younger than some of the rows a store can already hold — this pins the
    row that predates it, built by editing the gate directly rather than
    through `create`, which would refuse it.

    `needs_a_verifier` cannot tell: it reads the gate's KIND, and the kind is
    still `human`. Routed the way a well-formed one is, the vehicle would be
    assigned from `gate.get('verifier')` (`None`) — no assignee, no required
    capability, `ready()` offering it to the executor. That is the exact
    FIX-L3.4 hole, reopened by a row instead of a field mapping. Dies if
    `route_open_gates` stops checking `verifier` before routing a human gate.
    """
    item = park(queue, gate=HUMAN)
    queue._mutate(item.id, lambda i: {"verify": {**i.verify, "verifier": None}})
    assert queue.get(item.id).verify["verifier"] is None

    result = verifiers.route_open_gates(queue)
    assert result["created"] == []
    assert vehicles(queue) == []
    [entry] = result["malformed"]
    assert entry["item"] == item.id
    assert "no verifier" in entry["reason"]


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
    assert "no longer waiting on a verdict" in retired.result
    assert retired.attestation is None, "the evidence belongs to the item, not the vehicle"


def test_a_rejected_gate_gets_a_fresh_vehicle_for_the_next_attempt(queue):
    """FIX-L3.5. The title was always the property; the assertion was its
    opposite — it pinned exactly one key, the FIRST attempt's, and passed.

    `route_open_gates` routed `awaiting_verify` and nothing else, and nothing
    returns a rejected item there. So a verdict-fail retired the vehicle, no
    second one was ever created, and the item sat in `verify_failed` with
    nothing routed to it — while `vehicle_key`'s docstring described the
    re-verify vehicle keyed on the failure count that never happened. Per the
    ASOP re-verify invariant a failed unit keeps blocking until ITS OWN gate
    runs again and passes, and that needs a route.
    """
    item = park(queue)
    verifiers.route_open_gates(queue)
    [first] = vehicles(queue)
    assert first.natural_key == f"verify:{item.id}:0"

    queue.attest(item.id, attestation(JUDGED["check"], exit_status=1), "reviewer",
                 capabilities=["verify"])
    assert queue.get(item.id).status is WorkStatus.VERIFY_FAILED
    assert queue.get(item.id).metadata["verify_retry"]["decision"] == "fix"

    result = verifiers.route_open_gates(queue)
    assert [c["verifies"] for c in result["created"]] == [item.id]
    assert result["retired"] == [first.id], "the answered attempt's vehicle is moot"

    live = [v for v in vehicles(queue) if v.status is not WorkStatus.DONE]
    assert [v.natural_key for v in live] == [f"verify:{item.id}:1"]
    assert live[0].metadata["attempt"] == 1, "somebody already said no once, and it shows"
    assert queue.claim(live[0].id, "reviewer", capabilities=["verify"]) is not None

    # And it stays at one. The pass runs on a cadence.
    assert verifiers.route_open_gates(queue)["created"] == []


def test_the_retry_policy_decides_whether_a_failed_gate_is_routed_again(queue):
    """One fix, then a human, then never again autonomously — `gates.retry_decision`
    already says so, and re-routing a `stop` item would be the queue overruling
    the policy it recorded one line earlier. An agent regenerating a fix for a
    check it does not understand is the exact behaviour that policy halts."""
    item = park(queue)
    live_after = {}
    for decision in ("fix", "escalate", "stop"):
        queue.attest(item.id, attestation(JUDGED["check"], exit_status=1), "reviewer",
                     capabilities=["verify"])
        assert queue.get(item.id).metadata["verify_retry"]["decision"] == decision
        verifiers.route_open_gates(queue)
        live_after[decision] = sorted(
            v.natural_key for v in vehicles(queue)
            if v.status not in (WorkStatus.DONE, WorkStatus.FAILED)
        )

    assert live_after["fix"] == [f"verify:{item.id}:1"]
    assert live_after["escalate"] == [f"verify:{item.id}:2"]
    assert live_after["stop"] == [], "the policy said stop and the queue kept offering it"


def test_a_verify_failed_item_with_no_retry_decision_recorded_routes_nothing(queue):
    """9ac74ed's claim, never pinned. `needs_a_verifier` reads the retry
    decision off the record the failure wrote, rather than recomputing it —
    and an item that reached `verify_failed` with no such record has said
    nothing about trying again. Every path this store's own API can produce
    writes one (`attest`, `resolve_by_default`); this is the row from before
    `verify_retry` existed, or one edited by hand, built directly rather than
    through either.

    Inventing a decision here is how a `stop` becomes another attempt — the
    correct read of silence is not-routed, and nothing said so.
    """
    item = park(queue)
    queue._mutate(item.id, lambda i: {"status": WorkStatus.VERIFY_FAILED})
    assert queue.get(item.id).status is WorkStatus.VERIFY_FAILED
    assert queue.get(item.id).metadata.get(verifiers.RETRY_KEY) is None

    assert verifiers.route_open_gates(queue)["created"] == []
    assert vehicles(queue) == []


def test_a_gate_exactly_at_its_deadline_is_due(queue):
    """The clock boundary, pinned. `sweep_park_clocks` skips only when
    `due_at() > now`, so a gate exactly AT its deadline resolves — "the clock
    just ran out" and "the clock ran out a moment ago" are the same finding to
    anything reading this pass, and only one of the two ways to write the
    comparison treats them that way. Dies if the skip condition becomes
    `>= now`, which would leave the gate parked for one more cycle.
    """
    item = park(queue, gate=gate(on_timeout="fail"))
    deadline = verifiers.due_at(queue.get(item.id))
    result = verifiers.sweep_park_clocks(queue, now=deadline)
    assert [r["item"] for r in result["resolved"]] == [item.id]


def test_a_quarantined_gate_is_not_re_offered_by_the_next_routing_pass(queue):
    """Quarantine retires the vehicle while the item stays parked, so the item
    still looks like it needs a verifier — and it does. What it does not need is
    to be handed back to the queue five minutes later, which is the whole of
    what quarantine buys.

    Dies if the routing pass stops asking whether this attempt was already
    routed. The natural key would still suppress the duplicate item, so the
    damage is quieter than a second vehicle: a pass that reports work it did not
    do, every five minutes, forever.
    """
    item, much_later = abandoned(queue)
    verifiers.sweep_quarantine(queue, now=much_later)
    [vehicle] = [v for v in vehicles(queue) if verifiers.verifies(v) == item.id]
    assert vehicle.status is WorkStatus.DONE

    assert verifiers.route_open_gates(queue)["created"] == []
    assert len(vehicles(queue)) == 1


def test_a_vehicle_whose_parent_this_store_does_not_have_is_retired(queue):
    """Dies if the retire loop only tests the parent's STATUS. A parent that is
    not in the store at all — deleted, or a row this version cannot model, which
    `_read_all` drops — makes `parent` None, and every status test on None is
    False. The vehicle becomes permanent work nobody can act on and every pass
    walks past it.

    The previous version of this test ATTESTED the parent rather than removing
    it, so the parent was present throughout and the case its name describes was
    never exercised. Deleting the `parent is None` arm left it green.
    """
    ghost = queue.create(
        "Verify: an item this store does not have",
        natural_key="verify:w-00000000:0",
        by_plane=True,
        requires=[verifiers.VERIFY_CAPABILITY],
        metadata={verifiers.VEHICLE_MARKER: "w-00000000"},
    )
    assert verifiers.route_open_gates(queue)["retired"] == [ghost.id]
    assert queue.get(ghost.id).status is WorkStatus.DONE


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
    if kind == "human":
        base["verifier"] = "dana"
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


def test_a_verdict_displaces_the_clocks_record_but_does_not_erase_it(queue):
    """FIX-L3.3. **A done item must never carry a top-level record saying no
    check was run.** The clock failed this gate and wrote its resolution; a
    reviewer then answered it for real. `attest` popped `verify_retry` and left
    `verify_resolution` behind, so the item closed DONE with an attestation
    sitting next to a note reading "no check was run" — the store contradicting
    itself about the one thing it exists to be believed on.

    Deleting the resolution would be the other lie. The clock DID fire, and that
    is true and worth keeping, so it moves under a history key and the top level
    holds only the transition that actually settled the item.
    """
    criteria = gate(on_timeout="fail")
    item = park(queue, gate=criteria)
    verifiers.sweep_park_clocks(queue, now=later())
    assert queue.get(item.id).status is WorkStatus.VERIFY_FAILED
    assert queue.get(item.id).metadata["verify_resolution"]["by"] == "park-clock"

    queue.attest(item.id, attestation(criteria["check"]), "reviewer", capabilities=["verify"])

    closed = queue.get(item.id)
    assert closed.status is WorkStatus.DONE
    assert closed.attestation["exit_status"] == 0
    assert "verify_resolution" not in closed.metadata, (
        "the top-level record still says nobody checked, and somebody did"
    )
    [superseded] = closed.metadata["verify_history"]
    assert superseded["by"] == "park-clock" and superseded["default"] == "fail"
    assert "no check was run" in superseded["note"], "the clock firing is still a fact"


def test_a_clock_failure_a_verifier_later_overturns_counts_as_a_verdict(queue):
    """The same defect read through the presence report. `verifier_status`
    classified on `verify_resolution` first, so an item a reviewer genuinely
    answered was counted as resolved-by-default — and a queue with real
    verification happening warned that it was approving its own work on a timer.

    The top-level resolution key is the final-transition test precisely because
    a verdict displaces it. That is one mechanism, not two: a second
    discriminator here would be a check no test could prove necessary.
    """
    criteria = gate(on_timeout="fail")
    item = park(queue, gate=criteria)
    verifiers.sweep_park_clocks(queue, now=later())
    queue.attest(item.id, attestation(criteria["check"]), "reviewer", capabilities=["verify"])

    status = verifiers.verifier_status(queue)
    assert status["resolvedByVerdict"] == 1
    assert status["resolvedByDefault"] == 0
    assert status["warning"] is None, "a reviewer answered this one"


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


def test_the_clock_starts_when_the_gate_parked_not_when_the_row_last_changed(queue, monkeypatch):
    """`_gate_outcome` stamps `verify_parked_at`, and `_parked_at` prefers it to
    `updated_at`. Every test of the clock parked and swept inside the same
    second, so deleting the stamp changed nothing any of them could see — the
    fallback gave the same answer to the tenth of a second. The mechanism was
    unprotected and looked covered.

    Here the row is touched an hour after the gate parked, so the two candidate
    start times differ by an hour and only one of them makes the gate due.
    """
    from agentco import work as work_module

    an_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    monkeypatch.setattr(work_module, "_now", lambda: an_hour_ago)
    item = park(queue, gate=gate(on_timeout="pass"))
    monkeypatch.undo()

    queue.annotate(item.id, {"note": "something changed the row after it parked"})
    touched = queue.get(item.id)
    assert touched.updated_at > touched.metadata["verify_parked_at"]

    assert [r["item"] for r in verifiers.sweep_park_clocks(queue)["resolved"]] == [item.id], (
        "the deadline was measured from the last touch, not from the park"
    )


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


def test_outstanding_excludes_a_quarantined_parents_vehicle(queue):
    """c9c7b49's claim, never pinned. Annotating the parent quarantined
    directly, rather than going through `sweep_quarantine` — which also
    retires the vehicle, and would let the DONE/FAILED filter alone carry this
    test whether or not the quarantine clause exists — isolates the thing
    actually being tested: `verifies(i) not in quarantined_parents`. Dies if
    that clause is removed; the vehicle is still live, unclaimed, and would
    otherwise count.
    """
    item = park(queue, gate=gate(on_timeout="escalate"))
    verifiers.route_open_gates(queue)
    [vehicle] = vehicles(queue)
    assert vehicle.status is WorkStatus.PENDING and vehicle.lease_attempt == 0

    queue.annotate(item.id, {verifiers.QUARANTINE_KEY: {
        "at": later().isoformat(), "escalated_to": "dana", "unanswered_seconds": 999999,
    }})

    assert verifiers.verifier_status(queue)["outstanding"] == 0


def test_a_deterministic_self_attestation_is_not_counted_as_a_verdict(queue):
    """F6. The old discriminator was `item.attestation is not None` — but a
    deterministic gate's executor attaches its own attestation to its own
    report, with no separation and no call to `attest`, which is nothing a
    verifier did. Three judged gates the clock closed alone, plus one
    deterministic gate that passed its own re-run, used to report
    `resolvedByVerdict=1` and suppress the warning that nothing but the clock
    was resolving the judged ones.
    """
    for n in range(3):
        park(queue, gate=gate(on_timeout="pass"), title=f"judged-{n}")
    verifiers.sweep_park_clocks(queue, now=later())

    deterministic_item = queue.create("ship it", verify=DETERMINISTIC)
    claimed = queue.claim(deterministic_item.id, "executor")
    queue.report_result(
        deterministic_item.id, claimed.lease_attempt, WorkStatus.DONE,
        attestation=attestation(DETERMINISTIC["check"]),
    )

    status = verifiers.verifier_status(queue)
    assert status["resolvedByDefault"] == 3
    assert status["resolvedByVerdict"] == 0, (
        "a deterministic self-attestation is not a verdict from anybody "
        "entitled to grade somebody else's work"
    )
    assert "approving its own work on a timer" in status["warning"]


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


def test_routing_a_re_verify_announces_no_new_park(queue, registry):
    """A rejected gate is owed a route and is NOT parked. Its clock is not
    running — `sweep_park_clocks` skips `verify_failed` deliberately — so a
    consumer handed `WorkParked` would start timing a wait that is not
    happening, and every surface reading the feed would show the item as a gate
    nobody has looked at. Somebody has, and said no.
    """
    item = park(queue)
    verifiers.route_open_gates(queue, conn=registry)
    assert len(feed(registry, "WorkParked")) == 1

    queue.attest(item.id, attestation(JUDGED["check"], exit_status=1), "reviewer",
                 capabilities=["verify"])
    result = verifiers.route_open_gates(queue, conn=registry)
    assert [c["verifies"] for c in result["created"]] == [item.id], "the re-verify is routed"
    assert len(feed(registry, "WorkParked")) == 1, "and it is not a park"


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


# --------------------------------------------------------------------------- #
# FIX-L3.1 / FIX-L3.2 — the two criticals from the adversarial review
# --------------------------------------------------------------------------- #


def test_an_empty_submitter_cannot_close_a_gate(queue):
    """`submitted_by and submitted_by == executor` skipped the separation check
    for a falsy submitter, so a verdict from nobody passed. No transport produced
    one; the Queue API did."""
    item = park(queue, gate=gate(kind="human", on_timeout="escalate"))
    for nobody in ("", "   "):
        with pytest.raises(Refusal) as caught:
            queue.attest(item.id, attestation(gate(kind="human", on_timeout="escalate")["check"]), nobody)
        assert "verdict from nobody" in caught.value.remediation
    assert queue.get(item.id).status is WorkStatus.AWAITING_VERIFY


def test_a_clock_only_queue_never_reads_as_configured(queue):
    """**The critical the review reproduced.** Retiring a vehicle used to go
    through `report_result`, which advances the fence — and `lease_attempt > 0`
    was the evidence of a claim. So the exact condition the presence report
    exists to catch (the clock approving everything) flipped its headline to "a
    verifier turned up"."""
    park(queue, gate=gate(on_timeout="pass"))
    verifiers.route_open_gates(queue)
    assert verifiers.verifier_status(queue)["configured"] is False

    verifiers.sweep_park_clocks(queue, now=later())
    verifiers.route_open_gates(queue)  # retires the moot vehicle

    status = verifiers.verifier_status(queue)
    assert status["configured"] is False, "no verifier ever touched this queue"
    assert status["claimedEver"] == 0
    assert status["resolvedByDefault"] == 1
    assert status["warning"] is not None


def test_retiring_a_vehicle_does_not_advance_its_fence(queue):
    park(queue)
    verifiers.route_open_gates(queue)
    [vehicle] = vehicles(queue)
    assert vehicle.lease_attempt == 0
    queue.retire(vehicle.id, "moot")
    retired = queue.get(vehicle.id)
    assert retired.status is WorkStatus.DONE
    assert retired.lease_attempt == 0, "it was never handed out, and the count must say so"
    assert "lease_report" not in retired.metadata


def test_retire_refuses_to_take_work_out_of_a_verifiers_hands(queue):
    """The routing pass's view went stale between its read and its write, and a
    verifier claimed the vehicle in between. That verifier is working; the pass
    does not get to close the item under them."""
    park(queue)
    verifiers.route_open_gates(queue)
    [vehicle] = vehicles(queue)
    queue.claim(vehicle.id, "reviewer", capabilities=["verify"])
    with pytest.raises(Exception):
        queue.retire(vehicle.id, "moot")
    assert queue.get(vehicle.id).leased_by == "reviewer"


def test_a_report_on_an_unclaimed_vehicle_is_not_evidence_of_a_verifier(queue):
    """The definition, tested on its own. Reporting an item at attempt 0 without
    ever claiming it is exactly what the old routing did to retire a vehicle;
    it advances the fence and nobody ever held the item. A definition that reads
    the fence calls this a claim. Dies if `claimed_ever` counts `lease_attempt`."""
    park(queue)
    verifiers.route_open_gates(queue)
    [vehicle] = vehicles(queue)
    with pytest.raises(Exception):
        queue.report_result(vehicle.id, 0, WorkStatus.DONE, result="closed by nobody")
    # Since FIX-L3.10 a report needs a lease, so the fence cannot move without a
    # claim any more and the two definitions agree on every reachable store.
    # The claims record is still the one read, because it is the fact and the
    # fence is an inference — and inferences are what broke last time.
    assert queue.get(vehicle.id).lease_attempt == 0
    assert verifiers.verifier_status(queue)["claimedEver"] == 0


def test_a_verifier_that_claimed_and_was_reaped_still_counts(queue):
    """The other direction. A reap clears `leased_by` and leaves no report, but a
    verifier DID turn up — the question is whether one exists, not whether one
    finished. A definition built on `leased_by` or `lease_report` alone says
    nobody ever came."""
    import time as _t

    park(queue)
    verifiers.route_open_gates(queue)
    [vehicle] = vehicles(queue)
    queue.claim(vehicle.id, "reviewer", ttl_seconds=1, capabilities=["verify"])
    _t.sleep(1.1)
    assert [r.id for r in queue.reap_expired_leases()] == [vehicle.id]
    assert queue.get(vehicle.id).leased_by is None
    assert verifiers.verifier_status(queue)["claimedEver"] == 1


# --------------------------------------------------------------------------- #
# FIX-L3.6 — a pass survives losing a race, and reports what it skipped
# --------------------------------------------------------------------------- #


def test_a_retire_that_loses_a_race_does_not_abort_the_routing_pass(queue):
    """`reap_expired_leases` catches `LeaseError` per item and carries on; these
    passes did not. A verifier claiming a vehicle between the pass's `list()`
    and its write raised straight out of the loop, and every item after it in
    that run went untouched.

    The failure is nearly invisible: the pass runs on a cadence, the next run
    reads fresh, so the only symptom is one cycle in which half the queue did
    not get swept — until the race is with something that recurs, and then the
    tail of the queue is never swept at all.
    """
    first, second = park(queue, title="raced"), park(queue, title="later in the run")
    verifiers.route_open_gates(queue)
    [held] = [v for v in vehicles(queue) if verifiers.verifies(v) == first.id]
    [after] = [v for v in vehicles(queue) if verifiers.verifies(v) == second.id]

    queue.attest(first.id, attestation(JUDGED["check"]), "reviewer-1", capabilities=["verify"])
    queue.attest(second.id, attestation(JUDGED["check"]), "reviewer-2", capabilities=["verify"])
    queue.claim(held.id, "reviewer-1", capabilities=["verify"])

    result = verifiers.route_open_gates(queue)
    assert result["retired"] == [after.id], "the item after the race must still be swept"
    assert [s["item"] for s in result["skipped"]] == [held.id]
    assert "holds it" in result["skipped"][0]["reason"]
    assert queue.get(held.id).leased_by == "reviewer-1", "and the work stays theirs"


def test_a_verdict_landing_mid_sweep_does_not_abort_the_park_clock(queue, monkeypatch):
    """The same race on the other pass, and the one that matters more: the clock
    is what makes gated work terminate at all. One verdict arriving at the wrong
    moment left every later due gate parked for another cycle."""
    raced = park(queue, gate=gate(on_timeout="pass"), title="answered mid-pass")
    untouched = park(queue, gate=gate(on_timeout="pass"), title="later in the run")

    stale = queue.list()
    queue.attest(raced.id, attestation(gate()["check"]), "reviewer", capabilities=["verify"])
    monkeypatch.setattr(queue, "list", lambda *a, **kw: stale)
    result = verifiers.sweep_park_clocks(queue, now=later())
    monkeypatch.undo()

    assert [r["item"] for r in result["resolved"]] == [untouched.id]
    assert [s["item"] for s in result["skipped"]] == [raced.id]
    assert queue.get(untouched.id).status is WorkStatus.DONE
    assert queue.get(raced.id).attestation is not None, "the verdict stands"


def test_quarantine_leaves_a_vehicle_a_verifier_is_holding(queue):
    """Third pass, same rule. Somebody is working this item; quarantine does not
    close it under them, and it does not stop halfway through the sweep either."""
    first, _ = abandoned(queue, title="held by a verifier")
    second, much_later = abandoned(queue, title="nobody is holding this one")
    [held] = [v for v in vehicles(queue) if verifiers.verifies(v) == first.id]
    queue.claim(held.id, "dana")

    result = verifiers.sweep_quarantine(queue, now=much_later)
    assert {q["item"] for q in result["quarantined"]} == {first.id, second.id}
    assert [s["item"] for s in result["skipped"]] == [held.id]
    assert queue.get(held.id).leased_by == "dana"


def test_a_router_that_loses_the_create_race_announces_nothing(queue, registry, monkeypatch):
    """Two routers on a cadence overlap. `create` deduplicates on the natural key
    and returns the EXISTING item rather than raising, which is right — and it
    left this pass reporting a create it did not make and emitting a second
    `WorkParked` for a gate the other router had already announced. A feed that
    carries one park twice is a feed whose consumers double-count, and the whole
    point of the natural key is that the second router does nothing."""
    item = park(queue, gate=gate(kind="human", on_timeout="escalate"))
    stale = queue.list()
    queue.create(
        "Verify: the other router got there first",
        natural_key=verifiers.vehicle_key(item),
        by_plane=True,
        assigned_agent="dana",
        metadata={verifiers.VEHICLE_MARKER: item.id},
    )
    monkeypatch.setattr(queue, "list", lambda *a, **kw: stale)
    result = verifiers.route_open_gates(queue, conn=registry)
    monkeypatch.undo()

    assert result["created"] == [], "it did not create this; the other router did"
    assert [s["item"] for s in result["skipped"]] == [item.id]
    assert feed(registry, "WorkParked") == []
    assert len(vehicles(queue)) == 1


def test_a_human_gate_is_answered_by_the_person_it_names_and_nobody_else(queue):
    """FIX-L3.4 gave a human gate a `verifier`, and routing honoured it — the
    vehicle is assigned to that person. `attest` did not: anyone who was not the
    executor could close it, so the name governed who was OFFERED the work and
    not who could DECIDE it. Found by the docs pass, which could not truthfully
    write that the field was enforced."""
    item = park(queue, gate=gate(kind="human", on_timeout="escalate"))
    named = queue.get(item.id).verify["verifier"]
    check = gate(kind="human", on_timeout="escalate")["check"]

    with pytest.raises(Refusal) as caught:
        queue.attest(item.id, attestation(check), "some-other-reviewer")
    assert "is not that person" in caught.value.message
    assert queue.get(item.id).status is WorkStatus.AWAITING_VERIFY

    assert queue.attest(item.id, attestation(check), named).status is WorkStatus.DONE


# --------------------------------------------------------------------------- #
# FIX-L3.16 — the executor cannot suppress verification of its own gate
# --------------------------------------------------------------------------- #


def test_a_caller_cannot_file_a_fake_vehicle(queue):
    """Variant (a) of the third review's finding. Any actor files an item carrying
    `metadata.verifies` and the `verify:` key; routing sees the marker, believes
    the gate is routed, files nothing, emits nothing — and the clock passes the
    gate with no verifier ever offered it. Reserved at the write boundary."""
    item = park(queue, gate=gate(on_timeout="pass"))
    with pytest.raises(Refusal) as caught:
        queue.create("decoy", metadata={"verifies": item.id})
    assert caught.value.code == "metadata_reserved"
    with pytest.raises(Refusal) as caught:
        queue.create("decoy", natural_key=f"verify:{item.id}:0")
    assert caught.value.code == "natural_key_reserved"

    # And so the real vehicle is filed and a real verifier sees it.
    assert len(verifiers.route_open_gates(queue)["created"]) == 1
    assert [i.title for i in queue.ready(agent="reviewer")] == [f"Verify: {item.title}"]


def test_every_plane_owned_key_is_reserved(queue):
    """One test per key would be a list nobody keeps in step with the constant, so
    the constant is the list: each name in it must be refused from a caller and
    accepted from the plane."""
    from agentco.work import RESERVED_METADATA_KEYS

    for key in sorted(RESERVED_METADATA_KEYS):
        with pytest.raises(Refusal):
            queue.create("forged", metadata={key: "x"})
    queue.create("the plane's own", metadata={"claims": []}, by_plane=True)


def test_a_vehicle_cannot_be_closed_by_reporting_it(queue):
    """Variant (b). Capabilities are self-asserted, so the executor's own node
    declares `verify`, claims the real vehicle and reports it done without
    attesting anything. The vehicle was ungated, so that used to succeed;
    routing then saw a closed vehicle for the current attempt and filed no other,
    and the clock passed the gate. A vehicle is closed by the verdict landing on
    its item, never by a report on itself."""
    item = park(queue, gate=gate(on_timeout="pass"))
    verifiers.route_open_gates(queue)
    [vehicle] = vehicles(queue)
    claimed = queue.claim(vehicle.id, "executor", capabilities=["verify"])

    with pytest.raises(Refusal) as caught:
        queue.report_result(vehicle.id, claimed.lease_attempt, WorkStatus.DONE,
                            result="nothing to see", submitted_by="executor")
    assert "answers nothing" in caught.value.message
    assert queue.get(vehicle.id).status is WorkStatus.IN_PROGRESS

    # The clock then finds the gate still routed and unanswered — and the item
    # still parked, because closing the vehicle was the only way past.
    verifiers.sweep_park_clocks(queue, now=later())
    assert queue.get(item.id).status is WorkStatus.DONE, "the clock still resolves it..."
    assert queue.get(item.id).metadata["verify_resolution"]["by"] == "park-clock"
    # ...which is the declared default doing its job, with the record saying so.
    # What the executor could not do is make it look like a verifier was offered
    # the work and declined: the vehicle stayed open the whole time.
    assert queue.get(vehicle.id).status is WorkStatus.IN_PROGRESS


def test_a_vehicle_still_retires_once_its_item_has_a_verdict(queue):
    """The legitimate path is unchanged: verdict on the item, then routing
    retires the vehicle. Only the shortcut is refused."""
    item = park(queue)
    verifiers.route_open_gates(queue)
    [vehicle] = vehicles(queue)
    queue.claim(vehicle.id, "reviewer", capabilities=["verify"])
    queue.attest(item.id, attestation(JUDGED["check"]), "reviewer", capabilities=["verify"])
    # The reviewer still holds the lease, so routing refuses to retire under them;
    # once the lease lapses or is released, it retires.
    queue.report_result(vehicle.id, queue.get(vehicle.id).lease_attempt, WorkStatus.DONE,
                        result="answered", submitted_by="reviewer")
    assert queue.get(vehicle.id).status is WorkStatus.DONE


# --------------------------------------------------------------------------- #
# M47 — the attest-side guards for rows a report could never have produced
# --------------------------------------------------------------------------- #


def test_attest_refuses_a_routed_gate_with_no_recorded_executor(queue):
    """A store written before FIX-L3.10 could hold an `awaiting_verify` item with
    no `lease_report` — a report accepted at attempt 0 by nobody. The separation
    check compares against that executor, and against None it compares against
    nobody. The refusal existed and had no test: the third review's M47 patched
    it out and 736 tests stayed green."""
    item = queue.create("legacy row", verify=JUDGED)
    queue._mutate(item.id, lambda i: {"status": WorkStatus.AWAITING_VERIFY,
                                      "metadata": {k: v for k, v in (i.metadata or {}).items()
                                                   if k != "lease_report"}})
    stored = queue.get(item.id)
    assert stored.status is WorkStatus.AWAITING_VERIFY and "lease_report" not in stored.metadata

    with pytest.raises(Refusal) as caught:
        queue.attest(item.id, attestation(JUDGED["check"]), "reviewer", capabilities=["verify"])
    assert "no recorded executor" in caught.value.message
    assert queue.get(item.id).status is WorkStatus.AWAITING_VERIFY


def test_attest_refuses_a_stored_human_gate_that_names_nobody(queue):
    """The read-side twin of FIX-L3.4. `validate_gate` now requires `verifier`
    on a human gate, but a row written before that rule has none, and the named-
    person check is skipped when there is no name — so anyone not the executor
    could close it. Refused, naming the fix."""
    item = park(queue, gate=gate(kind="human", on_timeout="escalate"))
    queue._mutate(item.id, lambda i: {"verify": {**i.verify, "verifier": None}})
    with pytest.raises(Refusal) as caught:
        queue.attest(item.id, attestation(gate(kind="human", on_timeout="escalate")["check"]),
                     "some-third-party")
    assert "names nobody" in caught.value.message
