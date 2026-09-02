"""Plan-vs-actual, written at the moment of completion.

ASOP § 3: a plan-vs-actual review is generated at the moment of completion,
while the context still exists. Here that is `report_result` on an item that
pins a procedure: the plan is the procedure's own words as copied under the pin
at instantiate (`metadata.sop_plan`), the actual is what the plane recorded of
the execution, and the two sit side by side under `metadata.plan_vs_actual`
with nothing judged — the adjudicator judges; this is what they read.

The properties defended: the plan is the words the executor was HANDED, so a
later revision of the procedure does not rewrite the review; the review is
written by the plane and cannot be forged at create; the verifier's verdict
lands beside the executor's claim rather than over it; and an item that pins no
procedure gets no review, because there is no plan to compare against.
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


def procedure(library, **over):
    body = {
        "definition_of_done": "the export matches the fixture byte for byte",
        "validation": "diff -q out.csv fixtures/expected.csv",
        "entry_check": "the fixture is current",
    }
    body.update(over)
    sop = library.create("export the ledger", author="dana", author_kind=HUMAN, **body)
    library.activate(sop.sop_id, 1, author="dana", author_kind=HUMAN)
    return sop


def attestation(check, exit_status=0, submitted_by=None):
    return {"check": check, "exit_status": exit_status, "environment": "ci", "at": "2026-09-02T15:00:00+00:00"}


# --------------------------------------------------------------------------- #
# the plan travels with the pin
# --------------------------------------------------------------------------- #


def test_instantiate_copies_the_plan_under_the_pin(library, queue):
    sop = procedure(library)
    item = library.instantiate(sop.sop_id, queue)
    assert item.metadata["sop_ref"] == {"sop_id": sop.sop_id, "version": 1}
    assert item.metadata[PLAN_KEY] == {
        "title": "export the ledger",
        "definition_of_done": "the export matches the fixture byte for byte",
        "validation": "diff -q out.csv fixtures/expected.csv",
        "entry_check": "the fixture is current",
    }


def test_the_plan_is_the_words_the_executor_was_handed(library, queue):
    """Revising the procedure afterwards must not rewrite what this instance
    was measured against — the same reason the pin is immutable."""
    sop = procedure(library)
    item = library.instantiate(sop.sop_id, queue)
    library.revise(sop.sop_id, definition_of_done="something else entirely",
                   author="dana", author_kind=HUMAN)
    library.activate(sop.sop_id, 2, author="dana", author_kind=HUMAN)
    assert queue.get(item.id).metadata[PLAN_KEY]["definition_of_done"] == (
        "the export matches the fixture byte for byte"
    )


def test_a_sparse_procedure_yields_a_sparse_plan(library, queue):
    sop = library.create("thin", purpose="just a purpose", author="dana", author_kind=HUMAN)
    library.activate(sop.sop_id, 1, author="dana", author_kind=HUMAN)
    item = library.instantiate(sop.sop_id, queue)
    assert item.metadata[PLAN_KEY] == {"title": "thin"}, "absent fields stay absent, never blank"


def test_the_plan_and_the_review_cannot_be_forged_at_create(queue):
    for key in (PLAN_KEY, PLAN_VS_ACTUAL_KEY):
        with pytest.raises(Refusal) as caught:
            queue.create("forged", metadata={key: {"definition_of_done": "whatever I say"}})
        assert caught.value.code == "metadata_reserved"


def test_instantiate_still_holds_the_callers_metadata_to_the_create_rule(library, queue):
    """`instantiate` files `by_plane` so it can write the plan. That must not
    become the way past the reserved-key check for everything else."""
    sop = procedure(library)
    with pytest.raises(Refusal) as caught:
        library.instantiate(sop.sop_id, queue, metadata={"verifies": "w-somebody"})
    assert caught.value.code == "metadata_reserved"
    with pytest.raises(Refusal) as caught:
        library.instantiate(sop.sop_id, queue, natural_key="verify:w-somebody")
    assert caught.value.code == "natural_key_reserved"
    assert queue.list() == []


# --------------------------------------------------------------------------- #
# the review, at completion
# --------------------------------------------------------------------------- #


def test_completion_writes_plan_beside_actual(library, queue):
    sop = procedure(library)
    item = library.instantiate(sop.sop_id, queue)
    leased = queue.claim(item.id, "kofi")
    done = queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE,
                               result="exported 1,204 rows; fixture matched")
    review = done.metadata[PLAN_VS_ACTUAL_KEY]
    assert review["sop_ref"] == {"sop_id": sop.sop_id, "version": 1}
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
    sop = procedure(library)
    item = library.instantiate(sop.sop_id, queue, verify=DETERMINISTIC)
    leased = queue.claim(item.id, "kofi")
    landed = queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE,
                                 attestation=attestation("pytest -q", exit_status=1))
    review = landed.metadata[PLAN_VS_ACTUAL_KEY]
    assert review["actual"]["reported"] == "done"
    assert review["actual"]["landed"] == "verify_failed"
    assert review["actual"]["attestation"]["exit_status"] == 1
    assert review["gate"] == {"kind": "deterministic", "check": "pytest -q"}
    assert review["flags"] == ["gate_disagreed", "retried"]
    assert "verdict" not in review, "the plane records; it does not conclude"


def test_a_failure_is_a_review_too(library, queue):
    sop = procedure(library)
    item = library.instantiate(sop.sop_id, queue)
    leased = queue.claim(item.id, "kofi")
    failed = queue.report_result(item.id, leased.lease_attempt, WorkStatus.FAILED, result="fixture missing")
    assert failed.metadata[PLAN_VS_ACTUAL_KEY]["flags"] == ["failed"]
    assert failed.metadata[PLAN_VS_ACTUAL_KEY]["actual"]["result"] == "fixture missing"


def test_the_verifiers_verdict_lands_beside_the_executors_claim(library, queue):
    sop = procedure(library)
    item = library.instantiate(sop.sop_id, queue, verify=JUDGED)
    leased = queue.claim(item.id, "kofi")
    parked = queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE, result="looks right to me")
    assert parked.metadata[PLAN_VS_ACTUAL_KEY]["flags"] == ["awaiting_verdict"]

    closed = queue.attest(item.id, attestation("a reviewer reads the diff", 0),
                          submitted_by="dana", capabilities=["verify"])
    review = closed.metadata[PLAN_VS_ACTUAL_KEY]
    assert review["actual"]["result"] == "looks right to me", "the executor's claim is kept"
    assert review["verdict"] == {"by": "dana", "exit_status": 0, "at": "2026-09-02T15:00:00+00:00", "passed": True}


def test_a_failing_verdict_is_recorded_as_failing(library, queue):
    sop = procedure(library)
    item = library.instantiate(sop.sop_id, queue, verify=JUDGED)
    leased = queue.claim(item.id, "kofi")
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE)
    red = queue.attest(item.id, attestation("a reviewer reads the diff", 2),
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
    sop = procedure(library)
    item = library.instantiate(sop.sop_id, queue)
    leased = queue.claim(item.id, "kofi")
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE, result="skipped the fixture diff")
    review = queue.get(item.id).metadata[PLAN_VS_ACTUAL_KEY]
    assert "diff -q" in review["plan"]["validation"] and "skipped" in review["actual"]["result"]
    tagged = queue.adjudicate(item.id, "bad", "plan says diff the fixture; actual says it was skipped",
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


def test_over_http_an_instance_completes_with_its_review(tmp_path):
    client = TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
        humans=["dana"],
    ))
    sop = _post(client, "/sops", "dana", {"title": "export", "definition_of_done": "matches the fixture"}).json()["sop"]
    assert _post(client, f"/sops/{sop['sop_id']}/activate", "dana", {"version": 1}).status_code == 200
    filed = _post(client, f"/sops/{sop['sop_id']}/instantiate", "dana", {}).json()["item"]
    assert filed["metadata"][PLAN_KEY] == {"title": "export", "definition_of_done": "matches the fixture"}

    pulled = _post(client, "/work/pull", "kofi", {}).json()
    reported = _post(client, f"/work/{filed['id']}/report", "kofi",
                     {"attempt": pulled["attempt"], "status": "done", "result": "matched"})
    assert reported.status_code == 200, reported.text
    review = reported.json()["item"]["metadata"][PLAN_VS_ACTUAL_KEY]
    assert review["plan"]["definition_of_done"] == "matches the fixture"
    assert review["actual"]["executor"] == "kofi"

    forged = _post(client, f"/sops/{sop['sop_id']}/instantiate", "dana",
                   {"metadata": {PLAN_VS_ACTUAL_KEY: {"actual": {"result": "perfect"}}}})
    assert forged.status_code == 422, forged.text
    assert forged.json()["code"] == "metadata_reserved"
