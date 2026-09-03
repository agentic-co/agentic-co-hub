"""The three DECIDE-L3 calls mabidoli made on 2026-09-03, and the tests that hold them.

  #3  A failed gate's re-verify offer has a clock. When nobody claims the
      re-verify vehicle within the gate's park time, quarantine withdraws the
      offer and the digest names the item — but never resolves it: a failed
      gate that passed on a timer would be the plane manufacturing its own
      green.
  #4  The idempotency key is read inside the lock, so an honest retry racing
      its own first attempt gets the recorded item back rather than a fence
      refusal.
  #5  A human gate whose verifier is the item's assignee is refused at the
      write boundary — it could only ever resolve on its clock.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agentco import auth, gates, verifiers
from agentco.app import create_app
from agentco.errors import Refusal
from agentco.work import LeaseError, WorkStatus

# A clock in the FUTURE, deliberately. `report_result` stamps `verify_parked_at`
# with the real wall clock, and the sweeps below are driven at `T0 + 601s`; a
# fixed T0 was correct the morning it was written and a time bomb by lunch,
# because once the wall clock passed it the gate had been parked for LESS than
# its 600s when swept. One day ahead keeps every relative offset in here on
# the right side of the parking stamp, whenever the suite runs.
T0 = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
JUDGED_FAIL = {"kind": "judged", "check": "a reviewer reads the diff", "max_park_seconds": 600,
               "on_timeout": "fail"}
DETERMINISTIC = {"kind": "deterministic", "check": "pytest -q", "max_park_seconds": 600, "on_timeout": "fail"}


def attestation(check, exit_status=0):
    return {"check": check, "exit_status": exit_status, "environment": "ci", "at": T0.isoformat()}


def failed_by_the_clock(queue):
    """A judged gate declared `fail` that nobody answered: lands verify_failed by default."""
    item = queue.create("migrate", verify=JUDGED_FAIL)
    leased = queue.claim(item.id, "kofi", now=T0)
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE)
    swept = verifiers.sweep_park_clocks(queue, now=T0 + timedelta(seconds=601))
    assert [r["item"] for r in swept["resolved"]] == [item.id]
    assert queue.get(item.id).status == WorkStatus.VERIFY_FAILED
    return queue.get(item.id)


# --------------------------------------------------------------------------- #
# #3 — the re-verify offer terminates, and is named
# --------------------------------------------------------------------------- #


def test_a_failed_gates_offer_is_withdrawn_when_its_clock_runs_out(queue):
    item = failed_by_the_clock(queue)
    routed = verifiers.route_open_gates(queue)
    vehicles = [v for v in queue.list() if verifiers.verifies(v) == item.id]
    assert vehicles and vehicles[0].status == WorkStatus.PENDING, routed

    failed_at = datetime.fromisoformat(queue.get(item.id).metadata["verify_parked_at"])
    early = verifiers.sweep_quarantine(queue, now=failed_at + timedelta(seconds=599))
    assert early["quarantined"] == [], "the clock has not run out"

    late = verifiers.sweep_quarantine(queue, now=failed_at + timedelta(seconds=601))
    assert [q["item"] for q in late["quarantined"]] == [item.id]
    assert late["quarantined"][0]["reason"] == "re-verify unanswered"
    after = queue.get(item.id)
    assert after.status == WorkStatus.VERIFY_FAILED, "never resolved — only withdrawn"
    assert after.metadata["verify_quarantined"]["reason"] == "re-verify unanswered"
    assert queue.get(vehicles[0].id).status == WorkStatus.DONE, "the offer was retired"

    digest = verifiers.quarantine_digest(queue, now=failed_at + timedelta(seconds=700))
    assert [(r["itemId"], r["status"], r["reason"]) for r in digest["stuckGates"]] == [
        (item.id, "verify_failed", "re-verify unanswered")
    ]


def test_the_clock_starts_at_the_failure_on_every_path(queue):
    """Deterministic re-run failing, verifier failing it, or the park clock
    failing it by default — each stamps the moment the offer opened."""
    det = queue.create("gated", verify=DETERMINISTIC)
    leased = queue.claim(det.id, "kofi")
    failed = queue.report_result(det.id, leased.lease_attempt, WorkStatus.DONE,
                                 attestation=attestation("pytest -q", 1))
    assert failed.status == WorkStatus.VERIFY_FAILED and failed.metadata.get("verify_parked_at")

    judged = queue.create("judged", verify={**JUDGED_FAIL, "on_timeout": "escalate", "escalate_to": "dana"})
    leased = queue.claim(judged.id, "kofi")
    queue.report_result(judged.id, leased.lease_attempt, WorkStatus.DONE)
    parked_stamp = queue.get(judged.id).metadata["verify_parked_at"]
    time.sleep(0.01)
    red = queue.attest(judged.id, attestation("a reviewer reads the diff", 2), submitted_by="dana",
                       capabilities=["verify"])
    assert red.status == WorkStatus.VERIFY_FAILED
    assert red.metadata["verify_parked_at"] != parked_stamp, "restamped at the failure, not the parking"

    by_clock = failed_by_the_clock(queue)
    stamped = datetime.fromisoformat(by_clock.metadata["verify_parked_at"])
    assert stamped > T0 + timedelta(seconds=600), (
        "restamped when the park clock failed it, not left at the parking time — "
        "otherwise the re-verify offer's clock would already be spent the moment it opened"
    )


def test_a_vehicle_somebody_is_holding_is_left_alone(queue):
    item = failed_by_the_clock(queue)
    verifiers.route_open_gates(queue)
    vehicle = next(v for v in queue.list() if verifiers.verifies(v) == item.id)
    assert queue.claim(vehicle.id, "dana", capabilities=["verify"]) is not None
    failed_at = datetime.fromisoformat(queue.get(item.id).metadata["verify_parked_at"])
    swept = verifiers.sweep_quarantine(queue, now=failed_at + timedelta(seconds=601))
    assert [q["item"] for q in swept["quarantined"]] == [item.id]
    assert swept["skipped"] and swept["skipped"][0]["item"] == vehicle.id, "somebody is finally looking"
    assert queue.get(vehicle.id).status == WorkStatus.IN_PROGRESS


def test_a_verdict_after_quarantine_still_closes_the_item(queue):
    """Quarantine governs what is offered, never what is permitted."""
    item = failed_by_the_clock(queue)
    failed_at = datetime.fromisoformat(queue.get(item.id).metadata["verify_parked_at"])
    verifiers.sweep_quarantine(queue, now=failed_at + timedelta(seconds=601))
    closed = queue.attest(item.id, attestation("a reviewer reads the diff", 0), submitted_by="dana",
                          capabilities=["verify"])
    assert closed.status == WorkStatus.DONE


def test_escalated_gates_keep_their_grace_period(queue):
    """The old rule is untouched: an escalated gate waits the grace period."""
    item = queue.create("ask a person", verify={**JUDGED_FAIL, "on_timeout": "escalate", "escalate_to": "dana"})
    leased = queue.claim(item.id, "kofi", now=T0)
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE)
    verifiers.sweep_park_clocks(queue, now=T0 + timedelta(seconds=601))
    assert queue.get(item.id).metadata.get("verify_escalated")
    soon = verifiers.sweep_quarantine(queue, now=T0 + timedelta(days=1))
    assert soon["quarantined"] == []
    later = verifiers.sweep_quarantine(queue, now=T0 + timedelta(days=8))
    assert [q["reason"] for q in later["quarantined"]] == ["escalated"]


# --------------------------------------------------------------------------- #
# #4 — an honest retry gets its result back
# --------------------------------------------------------------------------- #


def test_a_retry_racing_its_own_first_attempt_is_returned_not_refused(queue):
    item = queue.create("export")
    leased = queue.claim(item.id, "kofi")
    first = queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE, result="ok",
                                idempotency_key="k-1", submitted_by="kofi")
    assert first.status == WorkStatus.DONE
    # The retry arrives with the SAME attempt number it was issued — which the
    # fence now considers stale — and the same key.
    again = queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE, result="ok",
                                idempotency_key="k-1", submitted_by="kofi")
    assert again.status == WorkStatus.DONE and again.result == "ok"
    assert again.lease_attempt == first.lease_attempt, "nothing was written the second time"


def test_a_different_key_at_a_stale_attempt_is_still_fenced(queue):
    item = queue.create("export")
    leased = queue.claim(item.id, "kofi")
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE, idempotency_key="k-1")
    with pytest.raises(LeaseError):
        queue.report_result(item.id, leased.lease_attempt, WorkStatus.FAILED, idempotency_key="k-2")


def test_the_key_is_read_under_the_lock(queue, monkeypatch):
    """The test of the mechanism: make the outside-the-lock read impossible to
    satisfy, and the retry must still find its key inside."""
    item = queue.create("export")
    leased = queue.claim(item.id, "kofi")
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE, idempotency_key="k-1")
    real_get = queue.get

    def get_that_hides_the_report(item_id):
        got = real_get(item_id)
        if got is not None:
            got.metadata = {k: v for k, v in (got.metadata or {}).items() if k != "lease_report"}
        return got

    monkeypatch.setattr(queue, "get", get_that_hides_the_report)
    again = queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE, idempotency_key="k-1")
    assert again.status == WorkStatus.DONE


# --------------------------------------------------------------------------- #
# #5 — a gate only the clock could answer is refused up front
# --------------------------------------------------------------------------- #

HUMAN = {"kind": "human", "check": "the owner signs off", "verifier": "dana",
         "max_park_seconds": 86400, "on_timeout": "escalate", "escalate_to": "dana"}


def test_a_human_gate_its_assignee_would_answer_is_refused(queue):
    with pytest.raises(Refusal) as caught:
        queue.create("sign off your own work", assigned_agent="dana", verify=HUMAN)
    assert caught.value.code == gates.GATE_INVALID
    assert "exists to exclude" in caught.value.message
    assert queue.list() == [], "nothing reached storage"
    ok = queue.create("sign off kofi's work", assigned_agent="kofi", verify=HUMAN)
    assert ok.verify["verifier"] == "dana"
    unassigned = queue.create("nobody assigned yet", verify=HUMAN)
    assert unassigned.assigned_agent is None


def test_the_refusal_reaches_the_wire(tmp_path):
    keys = {"dana": "dana-secret", "operator": "op-secret"}
    client = TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=keys, operator="operator",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
    ))
    body = {"title": "sign off your own work", "assignedAgent": "dana", "verify": HUMAN}
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    response = client.post("/work", content=raw, headers={
        "X-AgentCo-Actor": "operator", "X-AgentCo-Timestamp": ts,
        "X-AgentCo-Signature": auth.sign(keys["operator"], "POST", "/work", ts, raw),
        "Content-Type": "application/json",
    })
    assert response.status_code == 422, response.text
    assert response.json()["code"] == gates.GATE_INVALID
