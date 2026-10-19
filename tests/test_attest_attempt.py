"""The verify-attempt fence on the attestation path, and a second answer to a passed gate.

ADR 0008 §3.3, §6 and §8 S1. The ownership epoch moves only on a transfer, so a
pass typed before a fail landed after it with no transfer at all (the
control-loop review's stale-pass probe). `verifyAttempt` closes that: it equals
the item's `verify_failures`, so a failing verdict advances it and a lease does
not. Optional on the wire (asop-spec 0.6.x has no such field): absent
keeps the earlier behaviour.

Synthetic actors as in tests/test_attest_epoch.py.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from agentco.errors import Refusal
from agentco.work import WorkStatus
from tests.test_attest_epoch import (  # noqa: F401  (fixtures and helpers are shared)
    DET, HUMAN, KEYS, det, failed_step, http, human, make_app, parked_human_step,
)


def test_a_pass_typed_before_a_fail_cannot_land_after_it(failed_step):
    queue, _, step = failed_step
    read = queue.get(step).verify_failures
    queue.attest(step, det(1), "hub-worker", verify_attempt=read)    # a fail lands first
    assert queue.get(step).verify_failures == read + 1, "a failing verdict advances the attempt"
    with pytest.raises(Refusal) as exc:
        queue.attest(step, det(0), "hub-worker", verify_attempt=read)  # the pass typed earlier
    assert (exc.value.code, exc.value.http_status) == ("stale", 409)
    assert f"verify_failures {read + 1}" in exc.value.message, "the refusal states the current count"
    assert queue.get(step).status is WorkStatus.VERIFY_FAILED, "a refused answer writes nothing"
    assert queue.attest(step, det(0), "hub-worker", verify_attempt=read + 1).status is WorkStatus.DONE


def test_a_new_lease_does_not_advance_the_attempt(queue):
    from datetime import datetime, timedelta, timezone

    item = queue.create("loose", verify=DET)
    first = queue.claim(item.id, "hub-worker", ttl_seconds=60)
    queue.reap_expired_leases(datetime.now(timezone.utc) + timedelta(hours=1))
    second = queue.claim(item.id, "hub-worker", ttl_seconds=60)
    assert second.lease_attempt > first.lease_attempt, "a re-execution is a new fence"
    assert second.verify_failures == first.verify_failures == 0, "and not a new verify attempt"
    queue.report_result(item.id, second.lease_attempt, WorkStatus.DONE, attestation=det(1))
    assert queue.get(item.id).verify_failures == 1


@pytest.mark.skipif(
    not hasattr(pytest.importorskip("asop.gates"), "gate_outcome"),
    reason="cannot_evaluate gate fields arrive with asop-spec 0.6.0 (v3.6)",
)
def test_cannot_evaluate_does_not_advance_the_attempt(queue):
    """What the code does: a could-not-evaluate result does not increment
    `verify_failures` (agentco/work.py, the gate outcome), so the attempt a
    verifier read is still current after one."""
    gate = {**DET, "cannot_evaluate_exit": 3, "min_cases": 1}
    pytest.importorskip("asop.gates")
    item = queue.create("run the suite", verify=gate)
    claimed = queue.claim(item.id, "hub-worker")
    cannot = {**det(3), "cases_run": 5}
    landed = queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE, attestation=cannot)
    assert landed.verify_failures == 0 and landed.metadata.get("verify_cannot_evaluate")
    again = queue.attest(item.id, cannot, "hub-worker", verify_attempt=0)
    assert again.verify_failures == 0, "still addressed to slot 0"
    assert queue.attest(item.id, {**det(0), "cases_run": 5}, "hub-worker", verify_attempt=0).status is WorkStatus.DONE


def test_absent_attempt_is_todays_behaviour(failed_step):
    queue, _, step = failed_step
    assert queue.attest(step, det(0), "hub-worker").status is WorkStatus.DONE


@pytest.mark.parametrize("bad", ["2", 2.0, True, -1])
def test_an_attempt_that_is_not_a_count_is_refused(failed_step, bad):
    queue, _, step = failed_step
    with pytest.raises(Refusal) as exc:
        queue.attest(step, det(0), "hub-worker", verify_attempt=bad)
    assert (exc.value.code, exc.value.http_status) == ("verify_attempt_invalid", 400)


def test_the_item_has_no_answer_attempt_field_and_nothing_writes_failed_verdicts(failed_step):
    import json

    queue, _, step = failed_step
    queue.attest(step, det(1), "hub-worker")
    item = queue.get(step)
    shown = json.loads(item.to_json())
    assert "answer_attempt" not in shown and not hasattr(item, "answer_attempt")
    assert shown["verify_failures"] == 2
    assert "failed_verdicts" not in (item.metadata or {})


def test_callers_cannot_forge_the_failed_verdict_counter(queue):
    with pytest.raises(Exception):
        queue.create("forged", metadata={"failed_verdicts": 5})


def test_a_second_answer_to_a_passed_gate_is_not_terminal_and_a_retry_is_not_a_second_answer(failed_step):
    queue, _, step = failed_step
    done = queue.attest(step, det(0), "hub-worker", idempotency_key="k-1")
    assert done.status is WorkStatus.DONE
    same = queue.attest(step, det(0), "hub-worker", idempotency_key="k-1")
    assert same.attestation == done.attestation, "the idempotent retry returns the recorded item"
    for answer in (det(0), det(1)):
        with pytest.raises(Refusal) as exc:
            queue.attest(step, answer, "hub-worker", idempotency_key="k-2")
        assert (exc.value.code, exc.value.http_status) == ("not_terminal", 409)
    assert queue.get(step).status is WorkStatus.DONE and queue.get(step).attestation == done.attestation


# --------------------------------------------------------------------------- #
# over HTTP and a live socket
# --------------------------------------------------------------------------- #


def item_of(call, step):
    return next(i for i in call("dana", "GET", "/work")[1]["items"] if i["id"] == step)


def test_http_the_stale_pass_is_refused_and_verify_attempt_is_a_declared_field(tmp_path):
    client = TestClient(make_app(tmp_path))

    def call(actor, method, path, body=None):
        response = http(client, method, path, actor, body)
        return response.status_code, response.json()

    _, step = parked_human_step(call)
    read = item_of(call, step)["verify_failures"]
    status, failed = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(False), "verifyAttempt": read})
    assert status == 200 and failed["item"]["verify_failures"] == read + 1
    status, refused = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(True), "verifyAttempt": read})
    assert (status, refused["code"]) == (409, "stale") and str(read + 1) in refused["message"]
    status, bad = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(True), "verifyAttempt": "1"})
    assert (status, bad["code"]) == (400, "verify_attempt_invalid")
    status, odd = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(True), "attemps": 1})
    assert (status, odd["code"]) == (422, "unknown_field"), "a misspelt field is not silently ignored"
    status, old = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(True), "attempt": read + 1})
    assert (status, old["code"]) == (422, "unknown_field"), "the first version's `attempt` is gone, not aliased"
    status, done = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(True), "verifyAttempt": read + 1})
    assert status == 200 and done["item"]["status"] == "done"
    status, second = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(False), "verifyAttempt": read + 1})
    assert (status, second["code"]) == (409, "not_terminal")


def test_live_socket_attempt_and_the_outbox(tmp_path):
    import uvicorn

    from agentco.outbox import Outbox, drain, registry_publisher
    from agentco.publish import Registry, RegistryError

    app = make_app(tmp_path)
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
    try:
        clients = {a: Registry(a, KEYS[a], f"http://127.0.0.1:{port}") for a in KEYS}

        def call(actor, method, path, body=None):
            try:
                return 200, clients[actor]._call(method, path, body)
            except RegistryError as exc:
                return exc.status, exc.payload

        _, step = parked_human_step(call)
        read = item_of(call, step)["verify_failures"]
        clients["dana"].attest(step, human(False), verify_attempt=read)
        with pytest.raises(RegistryError) as exc:
            clients["dana"].attest(step, human(True), verify_attempt=read)
        assert exc.value.status == 409 and exc.value.payload["code"] == "stale"

        # a line queued before the fail is refused stale when it drains, and is
        # settled as refused (not retried forever)
        box = Outbox(tmp_path / ".agentco")
        box.push("attest", {"itemId": step, "attestation": human(True), "verifyAttempt": read})
        publish = registry_publisher(clients["dana"])
        assert drain(box, publish)["published"] == 0
        assert box.read_receipts()[-1].get("code") == "stale"
        assert clients["dana"].attest(step, human(True), verify_attempt=read + 1)["item"]["status"] == "done"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
