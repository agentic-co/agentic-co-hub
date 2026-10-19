"""The ownership epoch on every attestation (ASOP 0.6.1 §7.2), and an idempotent attest.

§7.2: "Every flip, completion and attestation carries the epoch its store
believes it holds… a stale epoch is refused whoever sends it… even if the run
is later transferred back. A store that predates epochs claims epoch 0, which is
accepted only until the run's first transfer." The hub checked the epoch on
renew and report only; an attestation read at epoch 1 landed after a
hub → runtime → hub round trip (the control-loop review's probe). Now it is
refused `not_the_owner`, stating the current epoch.

Epoch numbers are this hub's, as `GET /runs/{id}` shows them: 1 at filing, +1 per
transfer. The epoch is optional on the wire because the spec says so: absent is
epoch 0, accepted only while the run has never been transferred.

Synthetic actors: `dana` is a declared human and admin, `hub-worker` works the
hub's queue, `rt-agent` speaks for the external store `runtime-a`.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth, ownership
from agentco.app import create_app
from agentco.errors import Refusal
from agentco.work import WorkStatus

DET = {"kind": "deterministic", "check": "make check", "max_park_seconds": 900, "on_timeout": "fail"}
HUMAN = {"kind": "human", "check": "a person confirms the rollback was exercised", "verifier": "dana",
         "max_park_seconds": 3600, "on_timeout": "escalate", "escalate_to": "dana"}


def det(code=0):
    return {"check": "make check", "exit_status": code, "environment": "ci/synthetic", "at": "2026-10-18T00:00:00+00:00"}


def human(passed=True):
    return {"check": HUMAN["check"], "exit_status": 0 if passed else 1, "environment": "reviewer laptop",
            "at": "2026-10-18T00:00:00+00:00", "verdict": {"passed": passed, "reason": "seen" if passed else "not yet"}}


# --------------------------------------------------------------------------- #
# the queue, on every backend
# --------------------------------------------------------------------------- #


@pytest.fixture()
def failed_step(library, queue):
    """A hub-owned run whose one step failed its deterministic gate."""
    queue.hub_id, queue.stores = "hub-a", {"runtime-a": frozenset({"rt-agent"})}
    asop = library.create("one step", inputs=[], roles={"w": {"kind": "agent"}},
                          steps=[{"name": "one", "role": "w", "purpose": "p", "gate": DET}])
    library.activate(asop.asop_id, 1, author_kind="human")
    run = library.run(asop.asop_id, queue, bindings={"w": "hub-worker"})
    step = run["steps"][0]["itemId"]
    claimed = queue.claim(step, "hub-worker")
    failed = queue.report_result(step, claimed.lease_attempt, WorkStatus.DONE, attestation=det(1))
    assert failed.status is WorkStatus.VERIFY_FAILED
    return queue, run["runId"], step


def round_trip(queue, run_id):
    ownership.transfer(queue, run_id, to="runtime-a", reason="the runtime takes it", by="dana", at="t1")
    ownership.transfer(queue, run_id, to="hub-a", reason="the runtime gave it back", by="dana", at="t2")


def test_a_never_transferred_run_accepts_no_epoch_epoch_zero_or_its_own(failed_step):
    queue, _, step = failed_step
    for epoch in (None, 0, 1):
        assert queue.attest(step, det(1), "hub-worker", epoch=epoch).status is WorkStatus.VERIFY_FAILED
    with pytest.raises(Refusal) as exc:
        queue.attest(step, det(0), "hub-worker", epoch=2)
    assert exc.value.code == "not_the_owner" and "epoch 1" in exc.value.message


def test_an_answer_read_before_a_round_trip_is_refused_and_states_the_epoch(failed_step):
    queue, run_id, step = failed_step
    round_trip(queue, run_id)
    for stale in (1, None, 0):
        with pytest.raises(Refusal) as exc:
            queue.attest(step, det(0), "hub-worker", epoch=stale)
        assert exc.value.code == "not_the_owner" and "(ownership epoch 3)" in exc.value.message, stale
        assert queue.get(step).status is WorkStatus.VERIFY_FAILED, "a refused attestation writes nothing"
    assert queue.attest(step, det(0), "hub-worker", epoch=3).status is WorkStatus.DONE


@pytest.mark.parametrize("bad", ["1", 1.0, True, -1])
def test_an_epoch_that_is_not_a_count_is_refused(failed_step, bad):
    queue, _, step = failed_step
    with pytest.raises(Refusal) as exc:
        queue.attest(step, det(0), "hub-worker", epoch=bad)
    assert exc.value.code == "epoch_invalid"


def test_an_item_outside_a_run_has_no_epoch_to_check(queue):
    item = queue.create("loose", verify=DET)
    claimed = queue.claim(item.id, "hub-worker")
    queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE, attestation=det(1))
    assert queue.attest(item.id, det(0), "hub-worker", epoch=7).status is WorkStatus.DONE


def test_the_same_key_from_the_same_submitter_is_a_retry_not_a_second_answer(failed_step):
    queue, _, step = failed_step
    done = queue.attest(step, det(0), "hub-worker", idempotency_key="line-1")
    assert done.status is WorkStatus.DONE
    again = queue.attest(step, det(0), "hub-worker", idempotency_key="line-1")
    assert again.status is WorkStatus.DONE and again.attestation == done.attestation
    assert [k["key"] for k in again.metadata["attest_keys"]] == ["line-1"], "the retry wrote nothing"
    with pytest.raises(Refusal) as exc:
        queue.attest(step, det(0), "hub-worker", idempotency_key="line-2")
    assert exc.value.code == "attestation_invalid", "a new key is a second answer, refused as before"
    with pytest.raises(Refusal):
        queue.attest(step, det(0), "someone-else", idempotency_key="line-1")


def test_the_same_owner_stale_pass_is_not_what_the_epoch_fences(failed_step):
    """DOCUMENTS current behaviour, not the property wanted. The review's first
    probe had no transfer: a pass typed before a fail lands after it. The epoch
    cannot see that (it moves only on a transfer); the attempt fence the review
    proposes (`attestation.attempt`) would, and it waits on a spec decision.
    A change that closes this should flip the assertion."""
    queue, _, step = failed_step
    assert queue.attest(step, det(0), "hub-worker", epoch=1).status is WorkStatus.DONE


# --------------------------------------------------------------------------- #
# over HTTP, a real socket and the outbox
# --------------------------------------------------------------------------- #

KEYS = {"dana": "dana-secret", "hub-worker": "hw-secret", "rt-agent": "rt-secret"}


def make_app(tmp_path):
    return create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="dana", hub_id="hub-a",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
        humans=["dana"], admins=["dana"], stores={"runtime-a": ["rt-agent"]})


def http(client, method, path, actor, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    ts = str(int(time.time()))
    headers = {"X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
               "X-AgentCo-Signature": auth.sign(KEYS[actor], method, path, ts, raw),
               "Content-Type": "application/json"}
    return client.request(method, path, content=raw or None, headers=headers)


def parked_human_step(call):
    """A hub-owned run whose one step is parked on a human gate for dana. `call`
    is (actor, method, path, body) -> (status, payload)."""
    _, sop = call("dana", "POST", "/sops", {"title": "one human-gated step", "inputs": [],
                                            "roles": {"w": {"kind": "agent"}},
                                            "steps": [{"name": "one", "role": "w", "purpose": "p", "gate": HUMAN}]})
    sop_id = sop["sop"]["asop_id"]
    call("dana", "POST", f"/sops/{sop_id}/activate", {"version": 1})
    _, filed = call("dana", "POST", f"/sops/{sop_id}/run", {"inputs": {}, "bindings": {"w": "hub-worker"}})
    run_id, step = filed["run"]["runId"], filed["run"]["steps"][0]["itemId"]
    _, pulled = call("hub-worker", "POST", "/work/pull", {})
    _, parked = call("hub-worker", "POST", f"/work/{step}/report", {"status": "done", "attempt": pulled["attempt"]})
    assert parked["item"]["status"] == "awaiting_verify"
    return run_id, step


def test_http_the_reviewers_stale_pass_after_a_round_trip_is_refused(tmp_path):
    client = TestClient(make_app(tmp_path))

    def call(actor, method, path, body=None):
        response = http(client, method, path, actor, body)
        return response.status_code, response.json()

    run_id, step = parked_human_step(call)
    seen = call("dana", "GET", f"/runs/{run_id}")[1]["run"]["owner"]["epoch"]
    assert seen == 1
    # dana fails it now, and will send the pass she typed earlier later
    status, failed = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(False), "epoch": seen})
    assert status == 200 and failed["item"]["status"] == "verify_failed"
    for to in ("runtime-a", "hub-a"):
        assert call("dana", "POST", f"/runs/{run_id}/transfer", {"to": to, "reason": "round trip"})[0] == 200
    status, refused = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(True), "epoch": seen})
    assert (status, refused["code"]) == (403, "not_the_owner") and "(ownership epoch 3)" in refused["message"]
    status, refused = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(True)})
    assert (status, refused["code"]) == (403, "not_the_owner"), "an old client is refused after a transfer"
    status, bad = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(True), "epoch": "3"})
    assert (status, bad["code"]) == (400, "epoch_invalid")
    status, odd = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(True), "epochs": 3})
    assert (status, odd["code"]) == (422, "unknown_field")
    status, done = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(True), "epoch": 3})
    assert status == 200 and done["item"]["status"] == "done"


def test_http_an_attest_retry_with_its_key_returns_the_recorded_item(tmp_path):
    client = TestClient(make_app(tmp_path))

    def call(actor, method, path, body=None):
        response = http(client, method, path, actor, body)
        return response.status_code, response.json()

    _, step = parked_human_step(call)
    body = {"attestation": human(True), "idempotencyKey": "line-9"}
    first = call("dana", "POST", f"/work/{step}/attest", body)
    retry = call("dana", "POST", f"/work/{step}/attest", body)
    assert first[0] == retry[0] == 200 and retry[1]["item"]["status"] == "done"
    assert retry[1]["item"]["attestation"] == first[1]["item"]["attestation"]
    second = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(True)})
    assert (second[0], second[1]["code"]) == (422, "attestation_invalid")


def test_live_socket_epoch_and_the_outbox_retry(tmp_path):
    import socket
    import threading

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
        url = f"http://127.0.0.1:{port}"
        clients = {a: Registry(a, KEYS[a], url) for a in KEYS}

        def call(actor, method, path, body=None):
            try:
                return 200, clients[actor]._call(method, path, body)
            except RegistryError as exc:
                return exc.status, exc.payload

        run_id, step = parked_human_step(call)
        for to in ("runtime-a", "hub-a"):
            clients["dana"]._call("POST", f"/runs/{run_id}/transfer", {"to": to, "reason": "round trip"})
        with pytest.raises(RegistryError) as exc:
            clients["dana"].attest(step, human(True), epoch=1)
        assert exc.value.status == 403 and exc.value.payload["code"] == "not_the_owner"

        # the outbox: a drainer that published and died before settling sends
        # the same line again, and gets the recorded item, not a refusal
        box = Outbox(tmp_path / ".agentco")
        line = box.push("attest", {"itemId": step, "attestation": human(True), "epoch": 3})
        publish = registry_publisher(clients["dana"])
        first = publish(line)
        again = publish(line)
        assert first["item"]["status"] == again["item"]["status"] == "done"
        assert drain(box, publish)["published"] == 1, "the queued line itself lands as a retry"
        assert box.read_receipts()[-1].get("code") is None
    finally:
        server.should_exit = True
        thread.join(timeout=5)
