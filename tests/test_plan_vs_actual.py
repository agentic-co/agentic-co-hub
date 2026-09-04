"""Plan-vs-actual, written at the moment of completion.

ASOP § 3: a plan-vs-actual review is generated at the moment of completion,
while the context still exists. In v3 that happens per STEP bead: `run` files
one child per step, each pinned `(asop_id, version, step)` and carrying a copy
of its OWN step's words under `metadata.sop_plan` — copied at filing time, not
looked up later, so a later revision of the ASOP does not rewrite what an
in-flight step was measured against. `report_result` on a step bead writes
`metadata.plan_vs_actual`: the plan (that copy), the actual (what the plane
recorded of the execution), side by side, with nothing judged — the
adjudicator judges; this is what they read.

The properties defended: the plan is the words the executor was HANDED, so a
later revision of the ASOP does not rewrite the review; the review is written
by the plane and cannot be forged at create; the verifier's verdict lands
beside the executor's claim rather than over it; and an item that pins no step
gets no review, because there is no plan to compare against.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth
from agentco.app import create_app
from agentco.errors import Refusal
from agentco.policy import HUMAN
from agentco.work import PLAN_KEY, PLAN_VS_ACTUAL_KEY, WorkStatus

JUDGED = {"kind": "judged", "check": "a reviewer reads the diff", "max_park_seconds": 900,
          "on_timeout": "escalate", "escalate_to": "release-owner"}
DETERMINISTIC = {"kind": "deterministic", "check": "pytest -q", "max_park_seconds": 900,
                 "on_timeout": "fail"}


def export_ledger_body(*, gate=None, **over):
    """A one-step, one-role ASOP: the running example for this file, ported
    from the old flat SOP to a v3 step. `gate` picks the step's gate
    (deterministic by default) — v3 authors the gate WITH the version, so a
    caller of `run()` can no longer supply one at filing time; a test that
    wants a different gate has to build a different ASOP version, not pass
    `verify=` at instantiation."""
    body = {
        "roles": {"implementer": {"kind": "agent"}},
        "steps": [{
            "name": "export",
            "role": "implementer",
            "entry_check": "the fixture is current",
            "definition_of_done": "the export matches the fixture byte for byte",
            "validation": "diff -q out.csv fixtures/expected.csv",
            "gate": gate or DETERMINISTIC,
        }],
    }
    body.update(over)
    return body


def an_asop(library, title="export the ledger", **over):
    return library.create(title, author="dana", author_kind=HUMAN, **export_ledger_body(**over))


def an_active_asop(library, **over):
    asop = an_asop(library, **over)
    return library.activate(asop.asop_id, asop.version, author="dana", author_kind=HUMAN)


RUN_BINDINGS = {"implementer": "kofi"}


def a_run(library, queue, asop, **over):
    kwargs = {"inputs": {}, "bindings": RUN_BINDINGS}
    kwargs.update(over)
    return library.run(asop.asop_id, queue, **kwargs)


def a_run_item_id(library, queue, asop, **over):
    """The single step bead of a one-step ASOP's run — this file's stand-in
    for what `instantiate()` used to hand back directly."""
    return a_run(library, queue, asop, **over)["steps"][0]["itemId"]


def attestation(check, exit_status=0, submitted_by=None):
    return {"check": check, "exit_status": exit_status, "environment": "ci", "at": "2026-09-02T15:00:00+00:00"}


def _declare(queue, humans=(), adjudicators=()):
    """Who the operator declared. Undeclared, only declared humans may
    adjudicate (ASOP v3 §6.1) — so a test that adjudicates has to say who its
    human is, exactly as an operator does with `AGENTCO_HUMANS`."""
    queue.humans = frozenset(humans)
    queue.adjudicators = frozenset(adjudicators)
    return queue


# --------------------------------------------------------------------------- #
# the plan travels with the pin, per step
# --------------------------------------------------------------------------- #


def test_run_copies_the_plan_under_the_pin_per_step(library, queue):
    asop = an_active_asop(library)
    run = a_run(library, queue, asop)
    item = queue.get(run["steps"][0]["itemId"])
    assert item.metadata["sop_ref"] == {"asop_id": asop.asop_id, "version": 1, "step": 1}
    assert item.metadata[PLAN_KEY] == {
        "title": "export the ledger",
        "name": "export",
        "role": "implementer",
        "entry_check": "the fixture is current",
        "definition_of_done": "the export matches the fixture byte for byte",
        "validation": "diff -q out.csv fixtures/expected.csv",
    }


def test_the_plan_is_the_words_the_executor_was_handed(library, queue):
    """Revising the ASOP afterwards must not rewrite what this step instance
    was measured against — the same reason the pin is immutable."""
    asop = an_active_asop(library)
    item_id = a_run_item_id(library, queue, asop)
    revised_steps = export_ledger_body()["steps"]
    revised_steps[0]["definition_of_done"] = "something else entirely"
    library.revise(asop.asop_id, steps=revised_steps, author="dana", author_kind=HUMAN)
    assert queue.get(item_id).metadata[PLAN_KEY]["definition_of_done"] == (
        "the export matches the fixture byte for byte"
    )


def test_a_sparse_procedure_yields_a_sparse_plan(library, queue):
    asop = library.create(
        "thin",
        roles={"implementer": {"kind": "agent"}},
        steps=[{"name": "do it", "role": "implementer", "purpose": "just a purpose", "gate": DETERMINISTIC}],
        author="dana", author_kind=HUMAN,
    )
    asop = library.activate(asop.asop_id, 1, author="dana", author_kind=HUMAN)
    run = library.run(asop.asop_id, queue, inputs={}, bindings={"implementer": "kofi"})
    item = queue.get(run["steps"][0]["itemId"])
    assert item.metadata[PLAN_KEY] == {
        "title": "thin", "name": "do it", "role": "implementer", "purpose": "just a purpose",
    }, "absent fields stay absent, never blank"


def test_the_plan_and_the_review_cannot_be_forged_at_create(queue):
    for key in (PLAN_KEY, PLAN_VS_ACTUAL_KEY):
        with pytest.raises(Refusal) as caught:
            queue.create("forged", metadata={key: {"definition_of_done": "whatever I say"}})
        assert caught.value.code == "metadata_reserved"


def test_run_still_holds_the_callers_metadata_to_the_create_rule(library, queue):
    """`run` files `by_plane` so it can write the pin and each step's plan.
    That must not become the way past the reserved-key check for everything
    else — the same rule `run`'s own refusals defend, checked at the metadata
    boundary rather than at role/binding resolution."""
    asop = an_active_asop(library)
    with pytest.raises(Refusal) as caught:
        library.run(asop.asop_id, queue, inputs={}, bindings=RUN_BINDINGS,
                    metadata={"verifies": "w-somebody"})
    assert caught.value.code == "metadata_reserved"
    with pytest.raises(Refusal) as caught:
        library.run(asop.asop_id, queue, inputs={}, bindings=RUN_BINDINGS,
                    natural_key="verify:w-somebody")
    assert caught.value.code == "natural_key_reserved"
    assert queue.list() == []


# --------------------------------------------------------------------------- #
# the review, at completion
# --------------------------------------------------------------------------- #


def test_completion_writes_plan_beside_actual(library, queue):
    asop = an_active_asop(library)
    item_id = a_run_item_id(library, queue, asop)
    item = queue.get(item_id)
    leased = queue.claim(item_id, "kofi")
    # Every v3 step carries a gate (there is no ungated step any more), so
    # landing DONE synchronously — the old "no verify at all" case — now
    # means a deterministic gate answered WITH the report, exit 0.
    done = queue.report_result(item_id, leased.lease_attempt, WorkStatus.DONE,
                               result="exported 1,204 rows; fixture matched",
                               attestation=attestation("pytest -q", exit_status=0))
    review = done.metadata[PLAN_VS_ACTUAL_KEY]
    assert review["sop_ref"] == {"asop_id": asop.asop_id, "version": 1, "step": 1}
    assert review["plan"]["definition_of_done"] == "the export matches the fixture byte for byte"
    actual = review["actual"]
    assert actual["executor"] == "kofi", "recorded while the lease still named them"
    assert actual["reported"] == "done" and actual["landed"] == "done"
    assert actual["result"] == "exported 1,204 rows; fixture matched"
    assert actual["attempt"] == leased.lease_attempt
    assert actual["filed_at"] == item.created_at
    assert review["flags"] == []
    assert review["generated_at"]
    assert done.leased_by is None, "the lease is gone — the review kept what it knew"


def test_a_gate_that_disagrees_is_flagged_not_judged(library, queue):
    asop = an_active_asop(library)  # deterministic gate, authored with the version
    item_id = a_run_item_id(library, queue, asop)
    leased = queue.claim(item_id, "kofi")
    landed = queue.report_result(item_id, leased.lease_attempt, WorkStatus.DONE,
                                 attestation=attestation("pytest -q", exit_status=1))
    review = landed.metadata[PLAN_VS_ACTUAL_KEY]
    assert review["actual"]["reported"] == "done"
    assert review["actual"]["landed"] == "verify_failed"
    assert review["actual"]["attestation"]["exit_status"] == 1
    assert review["gate"] == {"kind": "deterministic", "check": "pytest -q"}
    assert review["flags"] == ["gate_disagreed", "retried"]
    assert "verdict" not in review, "the plane records; it does not conclude"


def test_a_failure_is_a_review_too(library, queue):
    asop = an_active_asop(library)
    item_id = a_run_item_id(library, queue, asop)
    leased = queue.claim(item_id, "kofi")
    failed = queue.report_result(item_id, leased.lease_attempt, WorkStatus.FAILED, result="fixture missing")
    assert failed.metadata[PLAN_VS_ACTUAL_KEY]["flags"] == ["failed"]
    assert failed.metadata[PLAN_VS_ACTUAL_KEY]["actual"]["result"] == "fixture missing"


def test_the_verifiers_verdict_lands_beside_the_executors_claim(library, queue):
    asop = an_active_asop(library, gate=JUDGED)
    item_id = a_run_item_id(library, queue, asop)
    leased = queue.claim(item_id, "kofi")
    parked = queue.report_result(item_id, leased.lease_attempt, WorkStatus.DONE, result="looks right to me")
    assert parked.metadata[PLAN_VS_ACTUAL_KEY]["flags"] == ["awaiting_verdict"]

    closed = queue.attest(item_id, attestation("a reviewer reads the diff", 0),
                          submitted_by="dana", capabilities=["verify"])
    review = closed.metadata[PLAN_VS_ACTUAL_KEY]
    assert review["actual"]["result"] == "looks right to me", "the executor's claim is kept"
    assert review["verdict"] == {"by": "dana", "exit_status": 0, "at": "2026-09-02T15:00:00+00:00", "passed": True}


def test_a_failing_verdict_is_recorded_as_failing(library, queue):
    asop = an_active_asop(library, gate=JUDGED)
    item_id = a_run_item_id(library, queue, asop)
    leased = queue.claim(item_id, "kofi")
    queue.report_result(item_id, leased.lease_attempt, WorkStatus.DONE)
    red = queue.attest(item_id, attestation("a reviewer reads the diff", 2),
                       submitted_by="dana", capabilities=["verify"])
    assert red.status == WorkStatus.VERIFY_FAILED
    assert red.metadata[PLAN_VS_ACTUAL_KEY]["verdict"]["passed"] is False


def test_an_item_that_pins_no_procedure_gets_no_review(queue):
    item = queue.create("ad hoc")
    leased = queue.claim(item.id, "kofi")
    done = queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE)
    assert PLAN_VS_ACTUAL_KEY not in (done.metadata or {})


def test_the_review_feeds_the_adjudicator(library, queue):
    """The whole point: the adjudicator reads plan against actual and tags."""
    asop = an_active_asop(library)
    item_id = a_run_item_id(library, queue, asop)
    leased = queue.claim(item_id, "kofi")
    queue.report_result(item_id, leased.lease_attempt, WorkStatus.DONE, result="skipped the fixture diff",
                        attestation=attestation("pytest -q", exit_status=0))
    review = queue.get(item_id).metadata[PLAN_VS_ACTUAL_KEY]
    assert "diff -q" in review["plan"]["validation"] and "skipped" in review["actual"]["result"]
    _declare(queue, humans=("dana",))
    tagged = queue.adjudicate(item_id, "bad", "plan says diff the fixture; actual says it was skipped",
                              adjudicator="dana")
    assert tagged.metadata["adjudication"]["sop_ref"] == review["sop_ref"]


# --------------------------------------------------------------------------- #
# over HTTP — the same review, from the wire
# --------------------------------------------------------------------------- #

KEYS = {"kofi": "kofi-secret", "dana": "dana-secret", "operator": "op-secret"}


def _post(client, path, actor, body):
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    return client.post(path, content=raw, headers={
        "X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
        "X-AgentCo-Signature": auth.sign(KEYS[actor], "POST", path, ts, raw),
        "Content-Type": "application/json",
    })


def test_over_http_a_run_completes_with_its_step_review(tmp_path):
    client = TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
        humans=["dana"],
    ))
    sop = _post(client, "/sops", "dana", {
        "title": "export",
        "roles": {"implementer": {"kind": "agent"}},
        "steps": [{
            "name": "export", "role": "implementer",
            "definition_of_done": "matches the fixture",
            "gate": DETERMINISTIC,
        }],
    }).json()["sop"]
    assert _post(client, f"/sops/{sop['asop_id']}/activate", "dana", {"version": 1}).status_code == 200
    run = _post(client, f"/sops/{sop['asop_id']}/run", "dana",
               {"inputs": {}, "bindings": {"implementer": "kofi"}}).json()["run"]
    item_id = run["steps"][0]["itemId"]

    pulled = _post(client, "/work/pull", "kofi", {}).json()
    assert pulled["item"]["id"] == item_id
    assert pulled["item"]["metadata"][PLAN_KEY] == {
        "title": "export", "name": "export", "role": "implementer",
        "definition_of_done": "matches the fixture",
    }

    reported = _post(client, f"/work/{item_id}/report", "kofi",
                     {"attempt": pulled["attempt"], "status": "done", "result": "matched",
                      "attestation": attestation("pytest -q", 0)})
    assert reported.status_code == 200, reported.text
    review = reported.json()["item"]["metadata"][PLAN_VS_ACTUAL_KEY]
    assert review["plan"]["definition_of_done"] == "matches the fixture"
    assert review["actual"]["executor"] == "kofi"

    forged = _post(client, f"/sops/{sop['asop_id']}/run", "dana",
                   {"inputs": {}, "bindings": {"implementer": "kofi"},
                    "metadata": {PLAN_VS_ACTUAL_KEY: {"actual": {"result": "perfect"}}}})
    assert forged.status_code == 422, forged.text
    assert forged.json()["code"] == "metadata_reserved"
