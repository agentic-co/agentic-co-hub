"""The ownership epoch on every attestation (ASOP 0.6.1 §7.2), and an idempotent attest.

§7.2: "Every flip, completion and attestation carries the epoch its store
believes it holds… a stale epoch is refused whoever sends it… even if the run
is later transferred back. A store that predates epochs claims epoch 0, which is
accepted only until the run's first transfer." The hub checked the epoch on
renew and report only; an attestation read at epoch 1 landed after a
hub → runtime → hub round trip (the control-loop review's probe). Now it is
refused `not_the_owner`, stating the current epoch.

Epochs count as §7.2 counts them: 0 for a run never transferred, the current
epoch + 1 per transfer (`GET /runs/{id}` shows `owner.epoch`). The hub first
counted from 1; one marked rebase moves every stored epoch down by one, and is
tested at the bottom. The epoch is optional on the wire because the spec says
so: absent claims 0, accepted only while the run is at 0. No other leniency.

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


def test_a_never_transferred_run_is_at_epoch_zero_and_accepts_only_zero_or_none(failed_step):
    queue, run_id, step = failed_step
    assert ownership.owner_record(queue, queue.get(run_id))["epoch"] == 0
    for epoch in (None, 0):
        assert queue.attest(step, det(1), "hub-worker", epoch=epoch).status is WorkStatus.VERIFY_FAILED
    for wrong in (1, 2):
        with pytest.raises(Refusal) as exc:
            queue.attest(step, det(0), "hub-worker", epoch=wrong)
        assert exc.value.code == "not_the_owner" and "(ownership epoch 0)" in exc.value.message


def test_an_answer_read_before_a_round_trip_is_refused_and_states_the_epoch(failed_step):
    queue, run_id, step = failed_step
    round_trip(queue, run_id)
    record = ownership.owner_record(queue, queue.get(run_id))
    assert record["epoch"] == 2 and [h["epoch"] for h in record["history"]] == [0, 1, 2]
    for stale in (0, None, 1, 3):
        with pytest.raises(Refusal) as exc:
            queue.attest(step, det(0), "hub-worker", epoch=stale)
        assert exc.value.code == "not_the_owner" and "(ownership epoch 2)" in exc.value.message, stale
        assert queue.get(step).status is WorkStatus.VERIFY_FAILED, "a refused attestation writes nothing"
    assert queue.attest(step, det(0), "hub-worker", epoch=2).status is WorkStatus.DONE


@pytest.mark.parametrize("bad", ["1", 1.0, True, -1])
def test_an_epoch_that_is_not_a_count_is_refused(failed_step, bad):
    queue, _, step = failed_step
    with pytest.raises(Refusal) as exc:
        queue.attest(step, det(0), "hub-worker", epoch=bad)
    assert exc.value.code == "epoch_invalid"


def test_an_item_outside_a_run_is_at_epoch_zero(queue):
    """ADR 0008 §8 S2: absent or 0 accepted, anything else `not_the_owner` stating 0."""
    item = queue.create("loose", verify=DET)
    claimed = queue.claim(item.id, "hub-worker")
    queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE, attestation=det(1))
    for wrong in (7, 1):
        with pytest.raises(Refusal) as exc:
            queue.attest(item.id, det(0), "hub-worker", epoch=wrong)
        assert (exc.value.code, exc.value.http_status) == ("not_the_owner", 403)
        assert "(ownership epoch 0)" in exc.value.message
    assert queue.get(item.id).status is WorkStatus.VERIFY_FAILED, "a refused answer writes nothing"
    assert queue.attest(item.id, det(1), "hub-worker", epoch=0).status is WorkStatus.VERIFY_FAILED
    assert queue.attest(item.id, det(0), "hub-worker", epoch=None).status is WorkStatus.DONE


def test_http_an_item_outside_a_run_is_at_epoch_zero(tmp_path):
    client = TestClient(make_app(tmp_path))

    def call(actor, method, path, body=None):
        response = http(client, method, path, actor, body)
        return response.status_code, response.json()

    status, filed = call("dana", "POST", "/work", {"title": "loose", "verify": HUMAN})
    assert status == 200, filed
    item = filed["item"]["id"]
    status, pulled = call("hub-worker", "POST", "/work/pull", {})
    assert pulled["item"]["id"] == item, pulled
    call("hub-worker", "POST", f"/work/{item}/report", {"status": "done", "attempt": pulled["attempt"]})
    status, refused = call("dana", "POST", f"/work/{item}/attest", {"attestation": human(True), "epoch": 3})
    assert (status, refused["code"]) == (403, "not_the_owner") and "(ownership epoch 0)" in refused["message"]
    status, done = call("dana", "POST", f"/work/{item}/attest", {"attestation": human(True), "epoch": None})
    assert status == 200 and done["item"]["status"] == "done", "null is absent"


def test_the_same_key_from_the_same_submitter_is_a_retry_not_a_second_answer(failed_step):
    queue, _, step = failed_step
    done = queue.attest(step, det(0), "hub-worker", idempotency_key="line-1")
    assert done.status is WorkStatus.DONE
    again = queue.attest(step, det(0), "hub-worker", idempotency_key="line-1")
    assert again.status is WorkStatus.DONE and again.attestation == done.attestation
    assert [k["key"] for k in again.metadata["attest_keys"]] == ["line-1"], "the retry wrote nothing"
    with pytest.raises(Refusal) as exc:
        queue.attest(step, det(0), "hub-worker", idempotency_key="line-2")
    assert exc.value.code == "not_terminal", "a new key is a second answer to a passed gate (ADR 0008 §6)"
    with pytest.raises(Refusal):
        queue.attest(step, det(0), "someone-else", idempotency_key="line-1")


def test_the_same_owner_stale_pass_is_refused_by_the_attempt_not_the_epoch(failed_step):
    """The review's first probe had no transfer: a pass typed before a fail lands
    after it. The epoch cannot see that (it moves only on a transfer); `verify_attempt`
    does, because a failing verdict advances it. Full coverage in
    tests/test_attest_attempt.py."""
    queue, _, step = failed_step
    typed_at = queue.get(step).verify_failures
    assert queue.attest(step, det(1), "hub-worker", epoch=0, verify_attempt=typed_at).status is WorkStatus.VERIFY_FAILED
    with pytest.raises(Refusal) as exc:
        queue.attest(step, det(0), "hub-worker", epoch=0, verify_attempt=typed_at)
    assert exc.value.code == "stale" and exc.value.http_status == 409
    assert queue.get(step).status is WorkStatus.VERIFY_FAILED
    assert queue.attest(step, det(0), "hub-worker", epoch=0).status is WorkStatus.DONE, "no attempt: as before"


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
    assert seen == 0
    # dana fails it now, and will send the pass she typed earlier later
    status, failed = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(False), "epoch": seen})
    assert status == 200 and failed["item"]["status"] == "verify_failed"
    epochs = [call("dana", "POST", f"/runs/{run_id}/transfer", {"to": to, "reason": "round trip"})[1]["epoch"]
              for to in ("runtime-a", "hub-a")]
    assert epochs == [1, 2], "the first transfer makes it 1"
    owner = call("dana", "GET", f"/runs/{run_id}")[1]["run"]["owner"]
    assert owner["epoch"] == 2 and [h["epoch"] for h in owner["history"]] == [0, 1, 2]
    assert "epoch_base" not in owner, "the storage marker never reaches the wire"
    status, refused = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(True), "epoch": seen})
    assert (status, refused["code"]) == (403, "not_the_owner") and "(ownership epoch 2)" in refused["message"]
    status, refused = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(True)})
    assert (status, refused["code"]) == (403, "not_the_owner"), "an old client is refused after a transfer"
    status, bad = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(True), "epoch": "2"})
    assert (status, bad["code"]) == (400, "epoch_invalid")
    status, odd = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(True), "epochs": 2})
    assert (status, odd["code"]) == (422, "unknown_field")
    status, done = call("dana", "POST", f"/work/{step}/attest", {"attestation": human(True), "epoch": 2})
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
    assert (second[0], second[1]["code"]) == (409, "not_terminal")


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
            clients["dana"].attest(step, human(True), epoch=0)
        assert exc.value.status == 403 and exc.value.payload["code"] == "not_the_owner"

        # the outbox: a drainer that published and died before settling sends
        # the same line again, and gets the recorded item, not a refusal
        box = Outbox(tmp_path / ".agentco")
        line = box.push("attest", {"itemId": step, "attestation": human(True), "epoch": 2})
        publish = registry_publisher(clients["dana"])
        first = publish(line)
        again = publish(line)
        assert first["item"]["status"] == again["item"]["status"] == "done"
        assert drain(box, publish)["published"] == 1, "the queued line itself lands as a retry"
        assert box.read_receipts()[-1].get("code") is None
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# the one-time rebase of epochs stored in the old count (1 at filing)
# --------------------------------------------------------------------------- #


def legacy(queue, run_id, step):
    """Write the old count back onto a run and its step, as a store from before
    the change holds it: no markers, every epoch one higher."""
    def old_owner(item):
        meta = dict(item.metadata or {})
        record = {k: v for k, v in meta[ownership.RUN_OWNER_KEY].items() if k != ownership.EPOCH_BASE_KEY}
        record["epoch"] += 1
        record["history"] = [{**h, "epoch": h["epoch"] + 1} for h in record["history"]]
        return {"metadata": {**meta, ownership.RUN_OWNER_KEY: record}}

    def old_step(item):
        meta = {k: v for k, v in (item.metadata or {}).items() if k != ownership.STAMP_BASE_KEY}
        meta[ownership.OWNER_EPOCH_KEY] = 3
        meta[ownership.LEASE_VOID_KEY] = {"at": "t2", "epoch": 3, "by": "dana", "reason": "run transferred"}
        return {"metadata": meta}

    queue._mutate(run_id, old_owner)
    queue._mutate(step, old_step)


def test_the_rebase_lowers_every_old_epoch_once_and_never_twice(failed_step):
    queue, run_id, step = failed_step
    round_trip(queue, run_id)
    legacy(queue, run_id, step)
    stored = queue.get(run_id).metadata[ownership.RUN_OWNER_KEY]
    assert stored["epoch"] == 3 and ownership.EPOCH_BASE_KEY not in stored
    assert ownership.rebase_epochs(queue, apply=False) == {"state": "dry-run", "items": 2}
    assert queue.get(run_id).metadata[ownership.RUN_OWNER_KEY]["epoch"] == 3, "a dry run writes nothing"
    assert ownership.rebase_epochs(queue, apply=True) == {"state": "rebased", "items": 2}
    record = queue.get(run_id).metadata[ownership.RUN_OWNER_KEY]
    assert record["epoch"] == 2 and [h["epoch"] for h in record["history"]] == [0, 1, 2]
    meta = queue.get(step).metadata
    assert meta[ownership.OWNER_EPOCH_KEY] == 2 and meta[ownership.STAMP_BASE_KEY] == 0
    assert meta[ownership.LEASE_VOID_KEY]["epoch"] == 2
    for _ in range(2):
        assert ownership.rebase_epochs(queue, apply=True)["items"] == 0, "the marker is the guard"
    assert queue.get(run_id).metadata[ownership.RUN_OWNER_KEY]["epoch"] == 2
    assert queue.attest(step, det(0), "hub-worker", epoch=2).status is WorkStatus.DONE


def test_an_unmigrated_record_already_reads_in_the_new_count(failed_step):
    """Between an upgrade and the rebase (a CLI-only node), reads are not wrong."""
    queue, run_id, step = failed_step
    round_trip(queue, run_id)
    legacy(queue, run_id, step)
    assert ownership.owner_record(queue, queue.get(run_id))["epoch"] == 2
    assert ownership.stamped_epoch(queue.get(step)) == 2
    with pytest.raises(Refusal):
        queue.attest(step, det(0), "hub-worker", epoch=3)


def test_the_hub_rebases_at_start_and_the_pulse_finds_nothing_after(tmp_path):
    from datetime import datetime, timezone

    from agentco import db, pulse
    from agentco.stores import open_queue

    client = TestClient(make_app(tmp_path))

    def call(actor, method, path, body=None):
        response = http(client, method, path, actor, body)
        return response.status_code, response.json()

    run_id, step = parked_human_step(call)
    for to in ("runtime-a", "hub-a"):
        call("dana", "POST", f"/runs/{run_id}/transfer", {"to": to, "reason": "round trip"})
    queue = open_queue(str(tmp_path / "work.jsonl"))
    legacy(queue, run_id, step)
    restarted = TestClient(make_app(tmp_path))
    response = http(restarted, "GET", f"/runs/{run_id}", "dana")
    assert response.json()["run"]["owner"]["epoch"] == 2
    facts, _ = pulse.housekeeping(open_queue(str(tmp_path / "work.jsonl")), db.connect(tmp_path / "api.sqlite3"),
                                  now=datetime.now(timezone.utc), apply=True)
    assert facts["epochsRebased"] == {"written": 0}


def test_a_step_filed_before_the_rebase_and_claimed_after_it_reports_cleanly(library, queue):
    """The claim stamps the run's epoch as it reads it (the rebased count), and
    the report's epoch check compares that stamp with the same count."""
    queue.hub_id, queue.stores = "hub-a", {"runtime-a": frozenset({"rt-agent"})}
    asop = library.create("two steps", inputs=[], roles={"w": {"kind": "agent"}}, steps=[
        {"name": "one", "role": "w", "purpose": "p", "gate": DET},
        {"name": "two", "role": "w", "purpose": "p", "gate": DET}])
    library.activate(asop.asop_id, 1, author_kind="human")
    run = library.run(asop.asop_id, queue, bindings={"w": "hub-worker"})
    one, two = (s["itemId"] for s in run["steps"])
    round_trip(queue, run["runId"])
    early = queue.claim(one, "hub-worker")            # claimed BEFORE the rebase
    legacy(queue, run["runId"], one)                  # ...as an old store holds it

    def old_stamp(item):
        meta = {k: v for k, v in (item.metadata or {}).items() if k != ownership.STAMP_BASE_KEY}
        return {"metadata": {**meta, ownership.OWNER_EPOCH_KEY: 3}}

    queue._mutate(one, old_stamp)
    ownership.rebase_epochs(queue, apply=True)
    assert queue.get(one).metadata[ownership.OWNER_EPOCH_KEY] == 2
    assert queue.report_result(one, early.lease_attempt, WorkStatus.DONE, attestation=det(0)).status \
        is WorkStatus.DONE, "claimed before, reported after"
    late = queue.claim(two, "hub-worker")              # filed before, claimed AFTER the rebase
    assert late.metadata[ownership.OWNER_EPOCH_KEY] == 2 and late.metadata[ownership.STAMP_BASE_KEY] == 0
    assert queue.report_result(two, late.lease_attempt, WorkStatus.DONE, attestation=det(0)).status \
        is WorkStatus.DONE, "filed before, claimed after"
