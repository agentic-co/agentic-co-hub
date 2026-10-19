"""The attempt fence on the attestation path, and a second answer to a passed gate.

ADR 0008 §3.3 and §6. The ownership epoch moves only on a transfer, so a pass
typed before a fail landed after it with no transfer at all (the control-loop
review's stale-pass probe). The answer attempt closes that: `lease_attempt`
plus the failing verdicts the gate has given, so it advances on every re-run and
every fail. Optional on the wire (asop-spec 0.6.x has no such field): absent
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
    read = queue.get(step).answer_attempt
    queue.attest(step, det(1), "hub-worker", attempt=read)           # a fail lands first
    assert queue.get(step).answer_attempt == read + 1, "a failing verdict advances the attempt"
    with pytest.raises(Refusal) as exc:
        queue.attest(step, det(0), "hub-worker", attempt=read)        # the pass typed earlier
    assert (exc.value.code, exc.value.http_status) == ("stale", 409)
    assert str(read + 1) in exc.value.message, "the refusal states the current attempt"
    assert queue.get(step).status is WorkStatus.VERIFY_FAILED, "a refused answer writes nothing"
    assert queue.attest(step, det(0), "hub-worker", attempt=read + 1).status is WorkStatus.DONE


def test_a_new_lease_advances_the_attempt(queue):
    from datetime import datetime, timedelta, timezone

    item = queue.create("loose", verify=DET)
    first = queue.claim(item.id, "hub-worker", ttl_seconds=60)
    assert first.answer_attempt == first.lease_attempt
    queue.reap_expired_leases(datetime.now(timezone.utc) + timedelta(hours=1))
    second = queue.claim(item.id, "hub-worker", ttl_seconds=60)
    assert second.answer_attempt > first.answer_attempt, "a re-execution moves the slot"


def test_absent_attempt_is_todays_behaviour(failed_step):
    queue, _, step = failed_step
    assert queue.attest(step, det(0), "hub-worker").status is WorkStatus.DONE


@pytest.mark.parametrize("bad", ["2", 2.0, True, -1])
def test_an_attempt_that_is_not_a_count_is_refused(failed_step, bad):
    queue, _, step = failed_step
    with pytest.raises(Refusal) as exc:
        queue.attest(step, det(0), "hub-worker", attempt=bad)
    assert (exc.value.code, exc.value.http_status) == ("attempt_invalid", 400)


def test_the_item_shows_the_slot_it_is_answering(failed_step):
    import json

    queue, _, step = failed_step
    item = queue.get(step)
    assert json.loads(item.to_json())["answer_attempt"] == item.lease_attempt + 0
    queue.attest(step, det(1), "hub-worker")
    assert json.loads(queue.get(step).to_json())["answer_attempt"] == item.answer_attempt + 1


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


def test_http_the_stale_pass_is_refused_and_attempt_is_a_declared_field(tmp_path):
    client = TestClient(make_app(tmp_path))

    def call(actor, method, path, body=None):
        response = http(client, method, path, actor, body)
        return response.status_code, response.json()

    _, step = parked_human_step(call)
    read = item_of(call, step)["answer_attempt"]
    status, failed = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(False), "attempt": read})
    assert status == 200 and failed["item"]["answer_attempt"] == read + 1
    status, refused = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(True), "attempt": read})
    assert (status, refused["code"]) == (409, "stale") and str(read + 1) in refused["message"]
    status, bad = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(True), "attempt": "1"})
    assert (status, bad["code"]) == (400, "attempt_invalid")
    status, odd = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(True), "attemps": 1})
    assert (status, odd["code"]) == (422, "unknown_field"), "a misspelt field is not silently ignored"
    status, done = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(True), "attempt": read + 1})
    assert status == 200 and done["item"]["status"] == "done"
    status, second = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(False), "attempt": read + 1})
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
        read = item_of(call, step)["answer_attempt"]
        clients["dana"].attest(step, human(False), attempt=read)
        with pytest.raises(RegistryError) as exc:
            clients["dana"].attest(step, human(True), attempt=read)
        assert exc.value.status == 409 and exc.value.payload["code"] == "stale"

        # a line queued before the fail is refused stale when it drains, and is
        # settled as refused (not retried forever)
        box = Outbox(tmp_path / ".agentco")
        box.push("attest", {"itemId": step, "attestation": human(True), "attempt": read})
        publish = registry_publisher(clients["dana"])
        assert drain(box, publish)["published"] == 0
        assert box.read_receipts()[-1].get("code") == "stale"
        assert clients["dana"].attest(step, human(True), attempt=read + 1)["item"]["status"] == "done"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
