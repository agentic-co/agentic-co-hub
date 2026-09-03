"""Verification gates — Phase 1 of the connection harness.

The property under test is the one every other ASOP claim rests on: an item
whose gate has not passed is **not done**, and nothing downstream may start on
the strength of its completion claim.

Two of these tests are written to be provable by mutation rather than merely
present, and the docstring of each says which mechanism it dies without. That
distinction is the repository's own hard-won lesson: six earlier tests asserted
the exact property their code lacked by testing the half that held, and a test
that cannot fail when its mechanism is removed looks identical in the summary
line to one that can.

The `queue` fixture is the parametrised one from `conftest.py`, so every test
here runs against BOTH backends. That is not incidental coverage: `verify` and
`attestation` are nullable JSON columns on the SQLite side, and "absent means
ungated" is the kind of property a storage layer breaks by normalising a NULL
into an empty object — which would gate every item ever filed.
"""

from __future__ import annotations

import pytest

from agentco import gates
from agentco.errors import Refusal
from agentco.work import WorkStatus

DETERMINISTIC = {
    "kind": "deterministic",
    "check": "pytest -q",
    "max_park_seconds": 900,
    "on_timeout": "fail",
}
JUDGED = {
    "kind": "judged",
    "check": "the migration is reversible and the rollback was exercised",
    "max_park_seconds": 3600,
    "on_timeout": "escalate",
    "escalate_to": "release-owner",
}


def attestation(check: str = "pytest -q", exit_status: int = 0) -> dict:
    return {
        "check": check,
        "exit_status": exit_status,
        "environment": "ci/ubuntu-24.04/py3.12",
        "at": "2026-09-01T12:00:00+00:00",
    }


def claim_and_finish(queue, item, agent="worker-a", **kw):
    """Claim, then report DONE — the ordinary path a worker takes."""
    claimed = queue.claim(item.id, agent)
    return queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE, **kw)


# --------------------------------------------------------------------------- #
# The write boundary — a malformed gate never reaches storage
# --------------------------------------------------------------------------- #


def test_a_malformed_gate_is_refused_and_nothing_is_written(queue):
    """A gate that cannot run must fail at `create`, not at execution.

    The failure this prevents is not an exception later — it is a gate that
    quietly does nothing while the item it guards reports green.
    """
    with pytest.raises(Refusal) as caught:
        queue.create("ship it", verify={"kind": "deterministic", "check": "pytest -q"})
    assert caught.value.code == gates.GATE_INVALID
    assert caught.value.remediation
    assert queue.list() == []


def test_a_misspelled_gate_field_is_refused_rather_than_ignored(queue):
    """The typo case, which is the realistic one.

    `max_park_second` would otherwise be an unread key and the gate would be
    stored missing its clock — the exact shape of a check nobody notices is
    absent.
    """
    with pytest.raises(Refusal) as caught:
        queue.create("ship it", verify=dict(DETERMINISTIC, max_park_second=900))
    assert "unknown gate field(s) ['max_park_second']" in caught.value.message


def test_max_park_seconds_cannot_exceed_the_thirty_day_ceiling(queue):
    """FIX-L3.8. `MAX_PARK_SECONDS` had no test — delete the `park >
    MAX_PARK_SECONDS` refusal and this is the only thing that notices, because
    every other gate test declares a window well under 30 days.

    A window longer than the ceiling is parking forever with extra steps, and
    parking forever is the exact state the clock exists to make impossible.
    """
    with pytest.raises(Refusal) as caught:
        queue.create("ship it", verify=dict(DETERMINISTIC, max_park_seconds=gates.MAX_PARK_SECONDS + 1))
    assert caught.value.code == gates.GATE_INVALID
    assert "exceeds the ceiling" in caught.value.message

    at_the_ceiling = queue.create("ship it too", verify=dict(DETERMINISTIC, max_park_seconds=gates.MAX_PARK_SECONDS))
    assert at_the_ceiling.verify["max_park_seconds"] == gates.MAX_PARK_SECONDS


def test_max_park_seconds_rejects_a_bool(queue):
    """FIX-L3.8. `bool` is an `int` in Python, so `max_park_seconds: True`
    would otherwise normalise to a one-second window without tripping the
    `isinstance(park, int)` check below it. Delete the `isinstance(park,
    bool)` guard and this is the only test that catches the item it creates —
    a gate that reports itself as declaring a sane clock while actually
    parking for one second.
    """
    with pytest.raises(Refusal) as caught:
        queue.create("ship it", verify=dict(DETERMINISTIC, max_park_seconds=True))
    assert caught.value.code == gates.GATE_INVALID
    assert "must be a positive integer" in caught.value.message


def test_an_escalation_with_no_destination_is_refused(queue):
    with pytest.raises(Refusal):
        queue.create("ship it", verify=dict(JUDGED, escalate_to=None))


def test_escalate_to_set_without_escalate_is_refused(queue):
    """FIX-L3.8. `escalate_to` and `on_timeout` answer different questions, so
    a stray `escalate_to` on a gate that never escalates is refused rather
    than silently ignored — a reader would otherwise have to guess whether it
    is a leftover from an edit or a destination something actually consults.

    Delete this refusal and the gate is stored carrying a field nothing reads,
    which is exactly the kind of green-looking no-op this module exists to
    refuse rather than tolerate.
    """
    with pytest.raises(Refusal) as caught:
        queue.create("ship it", verify=dict(DETERMINISTIC, escalate_to="release-owner"))
    assert caught.value.code == gates.GATE_INVALID
    assert "nothing would ever read it" in caught.value.message


def test_a_human_gate_with_nobody_named_to_answer_it_is_refused(queue):
    """FIX-L3.4. `escalate_to` was doing double duty as the verifier, and it
    cannot be declared unless `on_timeout` is `escalate` — so a human gate that
    resolved on its own clock named nobody, and L3 routed it to a work item with
    no assignee and no required capability that `ready()` handed to the executor.

    Refused at the write boundary rather than patched at the routing pass: a gate
    nobody can answer is malformed, and the rule for a malformed gate here is
    that it never reaches storage.
    """
    human = {"kind": "human", "check": "the owner signs off",
             "max_park_seconds": 900, "on_timeout": "fail"}
    with pytest.raises(Refusal) as caught:
        queue.create("ship it", verify=human)
    assert "must name the person who answers it" in caught.value.message
    assert "escalate_to is not a substitute" in caught.value.remediation

    named = queue.create("ship it", verify=dict(human, verifier="dana"))
    assert named.verify["verifier"] == "dana"
    assert named.verify["escalate_to"] is None, "who answers is not where it escalates"


def test_a_deterministic_gate_may_not_name_a_verifier(queue):
    """Its executor is its attester. A name here promises a review that never
    happens — the same rule `escalate_to` follows on a gate that never escalates."""
    with pytest.raises(Refusal) as caught:
        queue.create("ship it", verify=dict(DETERMINISTIC, verifier="dana"))
    assert "nothing would ever read it" in caught.value.message


def test_a_judged_gate_may_narrow_its_route_to_one_verifier(queue):
    """Optional there, because a judged gate is already routed by capability.
    Naming somebody narrows it from "any node declaring verify" to one of them."""
    item = queue.create("ship it", verify=dict(JUDGED, verifier="reviewer-a"))
    assert item.verify["verifier"] == "reviewer-a"
    assert queue.create("ship it too", verify=JUDGED).verify["verifier"] is None


def test_a_stored_gate_is_normalised(queue):
    """Normalised means every field of the shared gate schema present, not
    just the ones this payload set — `asop.gates.validate_gate` also carries
    the Harness's staged-check and runtime-hint fields (`checks`, `cwd`,
    `timeout_s`, `rubric`, `judge_route`) and `schema_version`, all `None`/1
    here since this gate never mentioned them. See `docs/architecture.md`
    § "The ASOP contract package"."""
    item = queue.create("ship it", verify=DETERMINISTIC)
    assert item.verify == dict(
        DETERMINISTIC,
        escalate_to=None,
        verifier=None,
        checks=None,
        cwd=None,
        timeout_s=None,
        rubric=None,
        judge_route=None,
        schema_version=1,
    )
    assert queue.get(item.id).verify == item.verify
    assert item.is_gated


# --------------------------------------------------------------------------- #
# The momentarily-done race
# --------------------------------------------------------------------------- #


def test_neither_verify_state_releases_a_dependent_item(queue):
    """**The momentarily-done race.** Dies if `releases_blockers` admits either
    verify state — which is the one-line "simplification" a future editor is
    most likely to make, since both statuses mean the worker has finished.

    The assertion is deliberately on `ready()`, the queue's own answer to "what
    may be worked on", rather than on the status field: it is the downstream
    item starting early that does the damage, not the label on the upstream one.
    """
    upstream = queue.create("migrate the schema", verify=JUDGED)
    downstream = queue.create("backfill from the new column", blocked_by=[upstream.id])

    assert claim_and_finish(queue, upstream).status == WorkStatus.AWAITING_VERIFY
    assert downstream.id not in {i.id for i in queue.ready()}
    assert queue.get(downstream.id).unmet_blockers(
        {i.id for i in queue.list() if i.status == WorkStatus.DONE}
    ) == [upstream.id]

    # And once the gate says no, it is still not released.
    queue.attest(upstream.id, attestation(check=JUDGED["check"], exit_status=1), "reviewer-b", capabilities=["verify"])
    assert queue.get(upstream.id).status == WorkStatus.VERIFY_FAILED
    assert downstream.id not in {i.id for i in queue.ready()}

    # Only the gate passing releases it.
    queue.attest(upstream.id, attestation(check=JUDGED["check"]), "reviewer-b", capabilities=["verify"])
    assert queue.get(upstream.id).status == WorkStatus.DONE
    assert downstream.id in {i.id for i in queue.ready()}


def test_a_gated_item_awaiting_verification_is_not_claimable(queue):
    """Not offered by `ready()`, and refused to a caller holding the id anyway.

    `claim` returns None on a refused claim, as it does for a lost race — the
    two are the same non-event from a poller's point of view. What matters here
    is that no second worker is handed an item whose gate is still open.
    """
    item = queue.create("ship it", verify=JUDGED)
    claim_and_finish(queue, item)
    assert item.id not in {i.id for i in queue.ready()}
    assert queue.claim(item.id, "worker-b") is None
    assert queue.get(item.id).status == WorkStatus.AWAITING_VERIFY


# --------------------------------------------------------------------------- #
# The refusal rule — what buys back the atomicity of a bundled attest
# --------------------------------------------------------------------------- #


def test_a_gated_report_without_an_attestation_is_refused(queue):
    """Dies if the refusal is softened into a warning or a park.

    This refusal is the whole reason `attest` can be a first-class verb without
    costing integrity: a completion claim on a deterministic gate cannot be
    made without evidence, so nothing is gained by folding the two calls.
    """
    item = queue.create("ship it", verify=DETERMINISTIC)
    claimed = queue.claim(item.id, "worker-a")
    with pytest.raises(Refusal) as caught:
        queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE)
    assert caught.value.code == gates.ATTESTATION_REQUIRED
    # Refused means unmoved: still claimed, still in progress, no evidence.
    stored = queue.get(item.id)
    assert stored.status == WorkStatus.IN_PROGRESS
    assert stored.attestation is None
    assert stored.leased_by == "worker-a"


def test_an_attestation_for_a_different_check_is_refused(queue):
    item = queue.create("ship it", verify=DETERMINISTIC)
    claimed = queue.claim(item.id, "worker-a")
    with pytest.raises(Refusal) as caught:
        queue.report_result(
            item.id,
            claimed.lease_attempt,
            WorkStatus.DONE,
            attestation=attestation(check="pytest -k the_one_that_passes"),
        )
    assert caught.value.code == gates.ATTESTATION_INVALID


def test_a_passing_attestation_completes_the_item(queue):
    item = queue.create("ship it", verify=DETERMINISTIC)
    done = claim_and_finish(queue, item, attestation=attestation())
    assert done.status == WorkStatus.DONE
    assert done.attestation["submitted_by"] == "worker-a"
    assert done.verify_failures == 0


def test_a_failing_attestation_lands_verify_failed_and_records_the_policy(queue):
    item = queue.create("ship it", verify=DETERMINISTIC)
    failed = claim_and_finish(queue, item, attestation=attestation(exit_status=1))
    assert failed.status == WorkStatus.VERIFY_FAILED
    assert failed.verify_failures == 1
    assert failed.metadata["verify_retry"]["decision"] == "fix"


def test_the_body_cannot_name_the_submitter(queue):
    item = queue.create("ship it", verify=DETERMINISTIC)
    claimed = queue.claim(item.id, "worker-a")
    with pytest.raises(Refusal):
        queue.report_result(
            item.id,
            claimed.lease_attempt,
            WorkStatus.DONE,
            attestation=dict(attestation(), submitted_by="somebody-trusted"),
            submitted_by="worker-a",
        )


# --------------------------------------------------------------------------- #
# Who may satisfy which gate
# --------------------------------------------------------------------------- #


def test_the_executor_may_not_attest_its_own_judged_gate(queue):
    """The separation IS the judged gate. Dies if the check is dropped, at which
    point a judged gate is a deterministic one with extra ceremony."""
    item = queue.create("ship it", verify=JUDGED)
    claim_and_finish(queue, item, agent="worker-a")
    with pytest.raises(Refusal) as caught:
        queue.attest(item.id, attestation(check=JUDGED["check"]), "worker-a", capabilities=["verify"])
    assert "cannot also verify" in caught.value.message
    assert queue.get(item.id).status == WorkStatus.AWAITING_VERIFY


def test_an_attestation_offered_on_a_judged_report_is_refused(queue):
    item = queue.create("ship it", verify=JUDGED)
    claimed = queue.claim(item.id, "worker-a")
    with pytest.raises(Refusal):
        queue.report_result(
            item.id,
            claimed.lease_attempt,
            WorkStatus.DONE,
            attestation=attestation(check=JUDGED["check"]),
        )


def test_an_ungated_item_accepts_no_evidence(queue):
    item = queue.create("tidy the readme")
    claimed = queue.claim(item.id, "worker-a")
    with pytest.raises(Refusal):
        queue.report_result(
            item.id, claimed.lease_attempt, WorkStatus.DONE, attestation=attestation()
        )


# --------------------------------------------------------------------------- #
# The legacy scope guard
# --------------------------------------------------------------------------- #


def test_an_item_with_no_gate_keeps_the_old_semantics_exactly(queue):
    """No backfill, no flood. An item filed before gates existed has none, and
    reporting DONE means done — the same call, the same outcome, on the same day
    gates shipped."""
    upstream = queue.create("the way it always was")
    downstream = queue.create("depends on it", blocked_by=[upstream.id])
    assert claim_and_finish(queue, upstream).status == WorkStatus.DONE
    assert downstream.id in {i.id for i in queue.ready()}


# --------------------------------------------------------------------------- #
# Re-verify, and the retry policy
# --------------------------------------------------------------------------- #


def test_a_failed_gate_cannot_be_cleared_by_reporting_again(queue):
    """The re-verify invariant. A worker must not be able to walk its own item
    out of a failed gate; only the gate answering again may."""
    item = queue.create("ship it", verify=DETERMINISTIC)
    failed = claim_and_finish(queue, item, attestation=attestation(exit_status=1))
    with pytest.raises(Exception):
        queue.report_result(item.id, failed.lease_attempt, WorkStatus.DONE,
                            attestation=attestation())
    assert queue.get(item.id).status == WorkStatus.VERIFY_FAILED


def test_re_verifying_the_same_item_is_what_clears_it(queue):
    item = queue.create("ship it", verify=DETERMINISTIC)
    claim_and_finish(queue, item, attestation=attestation(exit_status=1))
    cleared = queue.attest(item.id, attestation(), "worker-a")
    assert cleared.status == WorkStatus.DONE
    assert cleared.metadata["verify_verdict"]["re_verify"] is True
    assert "verify_retry" not in cleared.metadata


def test_the_retry_policy_stops_at_two(queue):
    """One fix item, then a human, then never again autonomously."""
    assert gates.retry_decision(1) == "fix"
    assert gates.retry_decision(2) == "escalate"
    assert gates.retry_decision(3) == "stop"
    assert gates.retry_decision(17) == "stop"
    with pytest.raises(ValueError):
        gates.retry_decision(0)


def test_the_failure_count_accumulates_across_re_verifications(queue):
    item = queue.create("ship it", verify=DETERMINISTIC)
    claim_and_finish(queue, item, attestation=attestation(exit_status=1))
    second = queue.attest(item.id, attestation(exit_status=2), "worker-a")
    assert second.verify_failures == 2
    assert second.metadata["verify_retry"]["decision"] == "escalate"
    third = queue.attest(item.id, attestation(exit_status=2), "worker-a")
    assert third.verify_failures == 3
    assert third.metadata["verify_retry"]["decision"] == "stop"


def test_attesting_an_item_that_never_claimed_completion_is_refused(queue):
    item = queue.create("ship it", verify=DETERMINISTIC)
    with pytest.raises(Refusal):
        queue.attest(item.id, attestation(), "reviewer-b")
    queue.claim(item.id, "worker-a")
    with pytest.raises(Refusal):
        queue.attest(item.id, attestation(), "reviewer-b")


def test_attesting_an_ungated_item_is_refused(queue):
    item = queue.create("tidy the readme")
    claim_and_finish(queue, item)
    with pytest.raises(Refusal):
        queue.attest(item.id, attestation(), "reviewer-b")


# --------------------------------------------------------------------------- #
# The gate belongs to whoever planned the work, not to whoever executes it
# --------------------------------------------------------------------------- #


def test_no_transport_offers_the_executor_a_way_to_change_its_own_gate():
    """A party that can rewrite its own check can write a tautology.

    The ASOP contract requires gates to be authored at plan time and immutable
    to the executor, and today that holds for a weak reason: no write path
    accepts a `verify` field on anything except `create`. Weak because it is
    true by ABSENCE — nobody decided it, and the next person to add a convenient
    `work_update` would undo it without noticing there was a rule.

    Asserted on signatures and behaviour, never on source text: a first draft of
    this test grepped the handlers for the string "verify" and failed on a
    docstring that merely says `awaiting_verify`, which is the same coarse proxy
    this repository has been bitten by before.
    """
    import inspect

    from agentco.mcp_server import create_server
    from agentco.publish import Registry
    from agentco.work import Queue

    for name in ("report_result", "attest", "claim", "reap_expired_leases"):
        params = set(inspect.signature(getattr(Queue, name)).parameters)
        assert "verify" not in params, (
            f"Queue.{name} accepts `verify` — an executor could rewrite the check "
            f"it is about to be judged by. A gate is authored once, at create."
        )

    for name in ("work_report", "attest"):
        assert "verify" not in set(inspect.signature(getattr(Registry, name)).parameters)


def test_the_mcp_mutation_tools_expose_no_gate_parameter(tmp_path):
    from agentco.mcp_server import create_server

    mcp = create_server(
        db_path=str(tmp_path / "r.sqlite3"),
        work_store=str(tmp_path / "work.jsonl"),
        sop_store=str(tmp_path / "sops.jsonl"),
        actor="tester",
    )
    import inspect

    for name in ("work_report", "attest"):
        found = mcp._tool_manager.get_tool(name)
        params = set(inspect.signature(found.fn).parameters)
        assert "verify" not in params, f"the {name} tool accepts a gate"
    assert "verify" in set(inspect.signature(mcp._tool_manager.get_tool("work_create").fn).parameters), (
        "create is where a gate is authored — if this fails, gates are unreachable again"
    )


def test_the_http_report_and_attest_verbs_ignore_a_gate_in_the_body(tmp_path):
    """The behavioural half, and the one that would catch a handler quietly
    passing an unexpected field through to the store."""
    from fastapi.testclient import TestClient

    from agentco.app import create_app

    keys = {"bigmac": "s3cret"}
    app = create_app(
        db_path=str(tmp_path / "r.sqlite3"),
        work_store=str(tmp_path / "work.jsonl"),
        sop_store=str(tmp_path / "sops.jsonl"),
        keys=keys,
    )
    registry = _loopback(TestClient(app), "bigmac", keys)

    original = dict(DETERMINISTIC)
    filed = registry.work_create("ship it", verify=original)["item"]
    leased = registry.work_pull()

    tampered = {"kind": "deterministic", "check": "true", "max_park_seconds": 1, "on_timeout": "pass"}
    registry.call_raw(
        "POST",
        f"/work/{filed['id']}/report",
        {
            "attempt": leased["attempt"],
            "status": "done",
            "verify": tampered,
            "attestation": attestation(),
        },
    )
    stored = registry.work_get(filed["id"])["item"] if hasattr(registry, "work_get") else None
    del stored

    rows = registry.call_raw("GET", "/work", None)["items"]
    [item] = [i for i in rows if i["id"] == filed["id"]]
    assert item["verify"]["check"] == original["check"], (
        "the report handler let the executor swap its own gate for `true`"
    )


def _loopback(client, actor: str, keys: dict):
    """A signed client whose transport is the app in-process, plus a raw escape
    hatch for sending fields the typed client deliberately will not send."""
    import json as _json
    import time as _time

    from agentco import auth as _auth
    from agentco.publish import Registry as _Registry, RegistryError as _RegistryError

    class _Loopback(_Registry):
        def _call(self, method, path, body=None, query=""):
            raw = _json.dumps(body).encode() if body is not None else b""
            ts = str(int(_time.time()))
            response = client.request(
                method,
                f"{path}{query}",
                content=raw if raw else None,
                headers={
                    "X-AgentCo-Actor": self.actor,
                    "X-AgentCo-Timestamp": ts,
                    "X-AgentCo-Signature": _auth.sign(self.secret, method, path, ts, raw),
                    "Content-Type": "application/json",
                },
            )
            if response.status_code >= 400:
                raise _RegistryError(response.status_code, response.json())
            return response.json()

        def call_raw(self, method, path, body):
            return self._call(method, path, body)

    return _Loopback(actor, keys[actor], "http://registry.test")
