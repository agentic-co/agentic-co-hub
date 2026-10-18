"""Could not evaluate: the third gate outcome, as the plane records it (spec v3.6 §3.3, §5.4).

Owner decision 2026-10-18, option A: a deterministic gate result that exits with
the gate's declared `cannot_evaluate_exit` (or an exit 0 below `min_cases`)

  * still lands `verify_failed`, so dependents stay held;
  * does NOT increment `verify_failures`;
  * marks the item `metadata.verify_cannot_evaluate = {pending, count, last, reason}`;
  * records `verify_retry.decision = "re-run"`, never "fix" or "escalate";
  * makes the item pullable again — a re-file — under a NEW fence: the claim
    advances `lease_attempt`, and a report on the old attempt is refused.

A real failure (any other nonzero exit) counts and escalates exactly as before.
Found live by the chain proof (D2): before this, exit 3 was charged as a failure
and the decision was "fix". Every surface is asserted, including the real wire.
"""

from __future__ import annotations

import json
import time

import pytest

from agentco.errors import Refusal
from agentco.work import LeaseError, WorkStatus, rerun_pending

asop_gates = pytest.importorskip("asop.gates")
if not hasattr(asop_gates, "gate_outcome"):
    pytest.skip("installed asop-spec predates v3.6 (0.6.0)", allow_module_level=True)

GATE = {"kind": "deterministic", "check": "make test", "max_park_seconds": 900, "on_timeout": "fail",
        "cannot_evaluate_exit": 3, "min_cases": 1}


def att(exit_status=0, cases=5):
    return {"check": "make test", "exit_status": exit_status, "environment": "ci/synthetic",
            "at": "2026-10-18T00:00:00+00:00", "cases_run": cases}


def claim_report(queue, item_id, exit_status, cases=5, agent="worker-a"):
    claimed = queue.claim(item_id, agent)
    return claimed, queue.report_result(item_id, claimed.lease_attempt, WorkStatus.DONE,
                                        attestation=att(exit_status, cases))


def flag(item):
    return item.metadata["verify_cannot_evaluate"]


# --------------------------------------------------------------------------- #
# the queue, on every backend
# --------------------------------------------------------------------------- #


def test_n_consecutive_cannot_evaluate_results_never_charge_or_escalate(queue):
    item = queue.create("run the suite", verify=GATE)
    attempts = []
    for n in range(1, 6):
        claimed, landed = claim_report(queue, item.id, 3)
        attempts.append(claimed.lease_attempt)
        assert landed.status is WorkStatus.VERIFY_FAILED
        assert landed.verify_failures == 0, f"charged on cannot_evaluate #{n}"
        assert landed.metadata["verify_retry"]["decision"] == "re-run"
        assert landed.metadata["verify_retry"]["outcome"] == "cannot_evaluate"
        assert flag(landed)["pending"] is True and flag(landed)["count"] == n
        assert flag(landed)["last"]["exit_status"] == 3
        assert rerun_pending(landed)
        assert item.id in {i.id for i in queue.ready()}, "a re-run is offered again"
    assert attempts == sorted(set(attempts)), "every re-run is a new, higher fence"


def test_a_real_failure_after_them_counts_from_zero_and_escalates(queue):
    item = queue.create("run the suite", verify=GATE)
    for _ in range(3):
        claim_report(queue, item.id, 3)
    _, failed = claim_report(queue, item.id, 1)
    assert failed.verify_failures == 1 and failed.metadata["verify_retry"]["decision"] == "fix"
    assert flag(failed)["pending"] is False and flag(failed)["count"] == 3, "the history stays"
    assert not rerun_pending(failed)
    assert item.id not in {i.id for i in queue.ready()}, "a real failure is held for a fix, not re-run"
    assert queue.claim(item.id, "worker-a") is None, "a refused claim returns nothing, as for any lost claim"
    again = queue.attest(item.id, att(1), "worker-a")
    assert again.verify_failures == 2 and again.metadata["verify_retry"]["decision"] == "escalate"


def test_dependents_are_never_released_by_cannot_evaluate(queue):
    item = queue.create("run the suite", verify=GATE)
    after = queue.create("ship it", blocked_by=[item.id])
    for _ in range(3):
        claim_report(queue, item.id, 3)
        assert after.id not in {i.id for i in queue.ready()}
    _, done = claim_report(queue, item.id, 0)
    assert done.status is WorkStatus.DONE and done.verify_failures == 0
    assert flag(done)["pending"] is False
    assert after.id in {i.id for i in queue.ready()}


def test_the_fence_holds_across_a_re_run(queue):
    item = queue.create("run the suite", verify=GATE)
    first, _ = claim_report(queue, item.id, 3)
    second = queue.claim(item.id, "worker-b")
    assert second.lease_attempt > first.lease_attempt
    with pytest.raises((LeaseError, Refusal)):
        queue.report_result(item.id, first.lease_attempt, WorkStatus.DONE, attestation=att(0))
    landed = queue.report_result(item.id, second.lease_attempt, WorkStatus.DONE, attestation=att(0))
    assert landed.status is WorkStatus.DONE


def test_an_exit_zero_below_min_cases_could_not_evaluate(queue):
    item = queue.create("run the suite", verify=GATE)
    _, landed = claim_report(queue, item.id, 0, cases=0)
    assert landed.status is WorkStatus.VERIFY_FAILED and landed.verify_failures == 0
    assert flag(landed)["pending"]


def test_the_executor_re_attest_path_classifies_it_too(queue):
    item = queue.create("run the suite", verify=GATE)
    claim_report(queue, item.id, 1)
    landed = queue.attest(item.id, att(3), "worker-a")
    assert landed.verify_failures == 1, "the earlier real failure stays; this one is not charged"
    assert landed.metadata["verify_retry"]["decision"] == "re-run" and rerun_pending(landed)


def test_a_gate_that_declares_no_third_outcome_charges_exit_3_as_before(queue):
    plain = {k: v for k, v in GATE.items() if k not in ("cannot_evaluate_exit", "min_cases")}
    item = queue.create("run the suite", verify=plain)
    _, landed = claim_report(queue, item.id, 3)
    assert landed.verify_failures == 1 and landed.metadata["verify_retry"]["decision"] == "fix"
    assert "verify_cannot_evaluate" not in landed.metadata


def test_a_caller_cannot_forge_the_flag(queue):
    from agentco.work import reject_reserved

    with pytest.raises(Exception):
        reject_reserved({"verify_cannot_evaluate": {"pending": True}}, None)


# --------------------------------------------------------------------------- #
# outcomes and the pulse
# --------------------------------------------------------------------------- #


def run_one_step(library, queue):
    asop = library.create("suite", inputs=[], roles={"w": {"kind": "agent"}},
                          steps=[{"name": "test", "role": "w", "purpose": "test", "gate": GATE}])
    library.activate(asop.asop_id, 1, author_kind="human")
    run = library.run(asop.asop_id, queue, bindings={"w": "worker-a"})
    return asop, run["steps"][0]["itemId"]


def test_outcomes_report_cannot_evaluate_in_its_own_column_not_as_a_failure(library, queue):
    asop, step = run_one_step(library, queue)
    claim_report(queue, step, 3)
    row = library.outcomes_by_version(asop.asop_id, queue)[0]["steps"][0]
    assert row["cannotEvaluate"] == 1 and row["verifyFailed"] == 0 and row["failed"] == 0
    assert row["unresolved"] == 1
    claim_report(queue, step, 1)
    row = library.outcomes_by_version(asop.asop_id, queue)[0]["steps"][0]
    assert row["cannotEvaluate"] == 0 and row["verifyFailed"] == 1


def test_the_pulse_counts_pending_re_runs(queue, tmp_path):
    from datetime import datetime, timezone

    from agentco import db, pulse

    item = queue.create("run the suite", verify=GATE)
    claim_report(queue, item.id, 3)
    facts, _ = pulse.housekeeping(queue, db.connect(tmp_path / "r.sqlite3"),
                                  now=datetime.now(timezone.utc), apply=False)
    assert facts["cannotEvaluate"] == 1


# --------------------------------------------------------------------------- #
# over the real routes: HTTP, the MCP remote relay, and a real socket
# --------------------------------------------------------------------------- #

from tests.test_mcp_remote import KEYS, LoopbackRegistry, client, remote, tool  # noqa: E402,F401


def test_http_shows_the_flag_and_the_decision_and_offers_the_re_run(client):
    reg = LoopbackRegistry("macbook", client)
    item = reg.work_create("run the suite", verify=GATE)["item"]
    pulled = reg.work_pull()
    report = reg.work_report(item["id"], pulled["attempt"], "done", attestation=att(3))["item"]
    assert report["status"] == "verify_failed" and report["verify_failures"] == 0
    assert report["metadata"]["verify_cannot_evaluate"]["pending"] is True
    assert report["metadata"]["verify_retry"]["decision"] == "re-run"
    again = reg.work_pull()
    assert again["item"]["id"] == item["id"] and again["attempt"] > pulled["attempt"]


def test_the_mcp_remote_relay_shows_the_flag_and_offers_the_re_run(remote, client):
    item = tool(remote, "work_create")(title="run the suite", verify=GATE)
    first = tool(remote, "work_pull")()
    landed = tool(remote, "work_report")(item_id=item["id"], attempt=first["lease_attempt"],
                                         status="done", attestation=att(3))
    assert landed["metadata"]["verify_cannot_evaluate"]["pending"] is True
    assert landed["metadata"]["verify_retry"]["decision"] == "re-run"
    assert landed["verify_failures"] == 0
    again = tool(remote, "work_pull")()
    assert again["id"] == item["id"] and again["lease_attempt"] > first["lease_attempt"]


@pytest.fixture()
def live_hub(tmp_path):
    """A real listening hub, so the `Registry` client crosses a real socket."""
    import socket
    import threading

    import uvicorn

    from agentco.app import create_app

    app = create_app(db_path=str(tmp_path / "live.sqlite3"), keys=KEYS, operator="operator",
                     work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def test_live_wire_through_the_http_client(live_hub):
    from agentco.publish import Registry

    reg = Registry("macbook", KEYS["macbook"], live_hub)
    item = reg.work_create("run the suite", verify=GATE)["item"]
    for n in range(1, 4):
        pulled = reg.work_pull()
        assert pulled["item"]["id"] == item["id"]
        landed = reg.work_report(item["id"], pulled["attempt"], "done", attestation=att(3))["item"]
        assert landed["verify_failures"] == 0 and landed["metadata"]["verify_cannot_evaluate"]["count"] == n
    pulled = reg.work_pull()
    done = reg.work_report(item["id"], pulled["attempt"], "done", attestation=att(0))["item"]
    assert done["status"] == "done" and done["verify_failures"] == 0
    assert json.dumps(done["metadata"]["verify_cannot_evaluate"]).count('"pending": false') == 1
