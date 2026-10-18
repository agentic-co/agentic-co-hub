"""Run ownership (ASOP §7.2) and the recorded administrative transfer (owner decision 2026-10-18).

Synthetic actors: `rt-agent` speaks for the external store `runtime-a`; `dana`
is a declared human admin; `eve` a declared human who is not an admin; `kofi`
an agent; `hub-worker` works the hub's own queue.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth, ownership
from agentco.app import create_app
from agentco.errors import Refusal
from agentco.work import LeaseError, WorkStatus

GATE = {"kind": "deterministic", "check": "make check", "max_park_seconds": 900, "on_timeout": "fail"}
ATT = {"check": "make check", "exit_status": 0, "environment": "ci/synthetic", "at": "2026-10-18T00:00:00+00:00"}
STORES = {"runtime-a": frozenset({"rt-agent"})}


@pytest.fixture()
def world(library, queue):
    queue.hub_id = "hub-a"
    queue.stores = dict(STORES)
    asop = library.create("two steps", inputs=[], roles={"w": {"kind": "agent"}}, steps=[
        {"name": "one", "role": "w", "purpose": "one", "gate": GATE},
        {"name": "two", "role": "w", "purpose": "two", "gate": GATE}])
    library.activate(asop.asop_id, 1, author_kind="human")
    return library, queue, asop.asop_id


def runtime_run(world):
    library, queue, asop_id = world
    return library.run(asop_id, queue, bindings={"w": "rt-agent"}, owner_store="runtime-a", filed_by="rt-agent")


def code_of(call):
    with pytest.raises(Refusal) as exc:
        call()
    return exc.value.code


# --------------------------------------------------------------------------- #
# ownership at filing, and backfill
# --------------------------------------------------------------------------- #


def test_a_hub_filed_run_is_owned_by_the_hub(world):
    library, queue, asop_id = world
    run = library.run(asop_id, queue, bindings={"w": "hub-worker"}, filed_by="dana")
    owner = library.run_get(run["runId"], queue)["owner"]
    assert owner["store"] == "hub-a" and owner["epoch"] == 1 and owner["filed_by"] == "dana"
    assert owner["history"][0]["reason"] == "filed"


def test_a_runtime_filed_run_records_that_store_and_only_its_actor_may_file_it(world):
    library, queue, asop_id = world
    run = runtime_run(world)
    assert library.run_get(run["runId"], queue)["owner"]["store"] == "runtime-a"
    before = len(queue.list())
    assert code_of(lambda: library.run(asop_id, queue, bindings={"w": "rt-agent"}, owner_store="runtime-a",
                                       filed_by="kofi")) == "not_the_owner"
    assert code_of(lambda: library.run(asop_id, queue, bindings={"w": "rt-agent"}, owner_store="nowhere",
                                       filed_by="rt-agent")) == "not_the_owner"
    assert len(queue.list()) == before, "a refused run files nothing"


def test_a_run_filed_before_ownership_reads_as_hub_owned_and_the_pulse_writes_it(world):
    from agentco import pulse

    library, queue, asop_id = world
    run = library.run(asop_id, queue, bindings={"w": "hub-worker"}, filed_by="dana")
    queue.annotate(run["runId"], {ownership.RUN_OWNER_KEY: None}, by_plane=True)
    owner = library.run_get(run["runId"], queue)["owner"]
    assert owner["store"] == "hub-a" and owner["backfilled"] is True
    assert pulse.backfill_run_owners(queue, apply=False) == [run["runId"]]
    pulse.backfill_run_owners(queue, apply=True)
    assert isinstance(queue.get(run["runId"]).metadata[ownership.RUN_OWNER_KEY], dict)
    assert pulse.backfill_run_owners(queue, apply=False) == []


# --------------------------------------------------------------------------- #
# not_the_owner
# --------------------------------------------------------------------------- #


def test_only_the_owner_store_may_flip_its_steps(world):
    _, queue, _ = world
    run = runtime_run(world)
    step = run["steps"][0]["itemId"]
    assert step not in {i.id for i in queue.ready(agent="kofi")}
    assert code_of(lambda: queue.claim(step, "kofi")) == "not_the_owner"
    claimed = queue.claim(step, "rt-agent")
    assert claimed.metadata[ownership.OWNER_EPOCH_KEY] == 1
    done = queue.report_result(step, claimed.lease_attempt, WorkStatus.DONE, attestation=ATT)
    assert done.status is WorkStatus.DONE


def test_not_the_owner_is_not_not_the_holder(world):
    _, queue, _ = world
    run = runtime_run(world)
    with pytest.raises(Refusal) as exc:
        queue.claim(run["steps"][0]["itemId"], "kofi")
    assert exc.value.code == "not_the_owner" and "runtime-a" in exc.value.message
    assert "epoch 1" in exc.value.message


# --------------------------------------------------------------------------- #
# the pack reference
# --------------------------------------------------------------------------- #


def test_the_owner_sets_a_pack_reference_and_nobody_else_does(world):
    _, queue, _ = world
    run = runtime_run(world)
    digest = "a" * 64
    pack = ownership.set_pack(queue, run["runId"], uri="s3://packs/run-1.tar", sha256=digest, by="rt-agent",
                              admins=(), at="2026-10-18T00:00:00+00:00")
    assert pack["sha256"] == f"sha256:{digest}" and pack["uri"] == "s3://packs/run-1.tar"
    assert code_of(lambda: ownership.set_pack(queue, run["runId"], uri="s3://x", sha256=digest, by="kofi",
                                              admins=(), at="t")) == "not_the_owner"
    for uri, sha in (("no-scheme", digest), ("s3://x", "short"), ("s3://x", None)):
        assert code_of(lambda: ownership.set_pack(queue, run["runId"], uri=uri, sha256=sha, by="rt-agent",
                                                  admins=(), at="t")) == "pack_invalid"
    again = ownership.set_pack(queue, run["runId"], uri="s3://packs/run-1b.tar", sha256="b" * 64, by="rt-agent",
                               admins=(), at="2026-10-18T01:00:00+00:00")
    assert again["previous"][0]["uri"] == "s3://packs/run-1.tar", "a replaced reference is kept as history"


# --------------------------------------------------------------------------- #
# the administrative transfer
# --------------------------------------------------------------------------- #


def test_a_transfer_fences_the_old_owner_and_makes_the_steps_workable_by_the_hub(world):
    library, queue, _ = world
    run = runtime_run(world)
    one, two = (s["itemId"] for s in run["steps"])
    first = queue.claim(one, "rt-agent")
    queue.report_result(one, first.lease_attempt, WorkStatus.DONE, attestation=ATT)
    held = queue.claim(two, "rt-agent")

    moved = ownership.transfer(queue, run["runId"], to="hub-a", reason="the runtime is decommissioned",
                               by="dana", at="2026-10-18T02:00:00+00:00")
    assert moved["epoch"] == 2 and moved["voidedLeases"] == [two] and moved["keptDone"] == [one]
    owner = library.run_get(run["runId"], queue)["owner"]
    assert owner["store"] == "hub-a"
    assert owner["history"][-1] == {"at": "2026-10-18T02:00:00+00:00", "by": "dana", "from": "runtime-a",
                                    "to": "hub-a", "reason": "the runtime is decommissioned", "epoch": 2}

    step_two = queue.get(two)
    assert step_two.status is WorkStatus.PENDING and step_two.lease_attempt > held.lease_attempt
    assert step_two.assigned_agent is None, "released from the old owner's actor"
    assert queue.get(one).status is WorkStatus.DONE and queue.get(one).attestation, "done stays done, with evidence"

    with pytest.raises(Refusal) as exc:
        queue.report_result(two, held.lease_attempt, WorkStatus.DONE, attestation=ATT)
    assert exc.value.code == "not_the_owner" and "epoch 1" in exc.value.message and "epoch 2" in exc.value.message
    assert code_of(lambda: queue.claim(two, "rt-agent")) == "not_the_owner", "the former owner is fenced out"

    taken = queue.claim(two, "hub-worker")
    assert taken.metadata[ownership.OWNER_EPOCH_KEY] == 2
    done = queue.report_result(two, taken.lease_attempt, WorkStatus.DONE, attestation=ATT)
    assert done.status is WorkStatus.DONE


@pytest.mark.parametrize("to,reason,code", [
    ("hub-a", "", "transfer_refused:reason"), ("hub-a", None, "transfer_refused:reason"),
    ("nowhere", "gone", "transfer_refused:malformed"), ("runtime-a", "noop", "transfer_refused:same_owner"),
    (None, "gone", "transfer_refused:malformed"),
])
def test_a_transfer_needs_a_reason_and_a_real_new_owner(world, to, reason, code):
    """Spec 0.6.1's `transfer_refused:<rule>`; the old `transfer_invalid` rides as an alias."""
    _, queue, _ = world
    run = runtime_run(world)
    with pytest.raises(Refusal) as exc:
        ownership.transfer(queue, run["runId"], to=to, reason=reason, by="dana", at="t")
    assert exc.value.code == code and exc.value.to_dict()["aliases"] == ["transfer_invalid"]
    assert ownership.owner_record(queue, queue.get(run["runId"]))["epoch"] == 1


# --------------------------------------------------------------------------- #
# orphans
# --------------------------------------------------------------------------- #


def test_the_pulse_finds_an_orphaned_run(world, tmp_path):
    from datetime import datetime, timezone

    from agentco import db, pulse

    _, queue, _ = world
    run = runtime_run(world)
    conn = db.connect(tmp_path / "r.sqlite3")
    now = datetime.now(timezone.utc)
    present, _ = pulse.check_orphaned_runs(conn, queue, now=now, cadences={}, identities={"rt-agent": object()})
    assert present == []
    revoked, findings = pulse.check_orphaned_runs(conn, queue, now=now, cadences={}, identities={})
    assert revoked[0]["runId"] == run["runId"] and revoked[0]["owner"] == "runtime-a"
    assert findings[0]["class"] == "attention" and f"/runs/{run['runId']}/transfer" in findings[0]["remediation"]
    silent, _ = pulse.check_orphaned_runs(conn, queue, now=now, cadences={"rt-agent": 60},
                                          identities={"rt-agent": object()})
    assert "silent" in silent[0]["reason"]


# --------------------------------------------------------------------------- #
# over HTTP
# --------------------------------------------------------------------------- #

KEYS = {"dana": "dana-secret", "eve": "eve-secret", "kofi": "kofi-secret",
        "rt-agent": "rt-secret", "hub-worker": "hw-secret"}


def make_app(tmp_path):
    return create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="dana", hub_id="hub-a",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
        humans=["dana", "eve"], admins=["dana"], stores={"runtime-a": ["rt-agent"]})


def signed(method, path, actor, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    ts = str(int(time.time()))
    return raw, {"X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
                 "X-AgentCo-Signature": auth.sign(KEYS[actor], method, path, ts, raw),
                 "Content-Type": "application/json"}


def http(client, method, path, actor, body=None):
    raw, headers = signed(method, path, actor, body)
    return client.request(method, path, content=raw or None, headers=headers)


def http_runtime_run(client):
    sop = http(client, "POST", "/sops", "dana", {"title": "two steps", "inputs": [], "roles": {"w": {"kind": "agent"}},
               "steps": [{"name": "one", "role": "w", "purpose": "one", "gate": GATE}]}).json()["sop"]
    http(client, "POST", f"/sops/{sop['asop_id']}/activate", "dana", {"version": 1})
    response = http(client, "POST", f"/sops/{sop['asop_id']}/run", "rt-agent",
                    {"inputs": {}, "bindings": {"w": "rt-agent"}, "ownerStore": "runtime-a"})
    assert response.status_code == 200, response.text
    return response.json()["run"]


def test_http_ownership_transfer_and_refusals(tmp_path):
    client = TestClient(make_app(tmp_path))
    run = http_runtime_run(client)
    view = http(client, "GET", f"/runs/{run['runId']}", "kofi").json()["run"]
    assert view["owner"]["store"] == "runtime-a" and view["pack"] is None

    assert http(client, "POST", "/work/pull", "kofi", {}).json()["state"] == "empty", "not offered to a non-owner"
    path = f"/runs/{run['runId']}/transfer"
    body = {"to": "hub-a", "reason": "the runtime's maintainer left"}
    # A person without the admin role is a role-binding failure; an agent is not a
    # person at all (decision 9: human_required, formerly capability_mismatch).
    for actor, code in (("eve", "transfer_refused:administrator"), ("kofi", "human_required")):
        refused = http(client, "POST", path, actor, body)
        assert refused.status_code == 403 and refused.json()["code"] == code, actor
    assert http(client, "POST", path, "dana", {"to": "hub-a"}).json()["code"] == "transfer_refused:reason"
    assert http(client, "POST", path, "dana", {**body, "by": "eve"}).json()["code"] == "author_from_signature"
    moved = http(client, "POST", path, "dana", body)
    assert moved.status_code == 200 and moved.json()["epoch"] == 2
    feed = http(client, "GET", "/events", "kofi").json()["events"]
    assert any(e["kind"] == "RunTransferred" and e["payload"]["runId"] == run["runId"] for e in feed)

    pulled = http(client, "POST", "/work/pull", "hub-worker", {}).json()
    assert pulled["item"]["id"] == run["steps"][0]["itemId"]
    report = http(client, "POST", f"/work/{pulled['item']['id']}/report", "hub-worker",
                  {"status": "done", "attempt": pulled["attempt"], "attestation": ATT})
    assert report.status_code == 200 and report.json()["item"]["status"] == "done"

    pack = http(client, "POST", f"/runs/{run['runId']}/pack", "dana", {"uri": "s3://packs/r.tar", "sha256": "c" * 64})
    assert pack.status_code == 200, "an admin may set a hub-owned run's pack"
    assert http(client, "POST", f"/runs/{run['runId']}/pack", "dana",
                {"uri": "s3://x", "sha256": "c" * 64, "content": "..."}).json()["code"] == "unknown_field"


def test_an_orphan_after_agentco_revoke(tmp_path, monkeypatch):
    """The real `agentco revoke` removes the owner store's only key; the pulse names the run."""
    from datetime import datetime, timezone

    from agentco import db, pulse

    keyfile = tmp_path / "keys.json"
    keyfile.write_text(json.dumps(KEYS))
    os.chmod(keyfile, 0o600)
    monkeypatch.setenv("AGENTCO_REGISTRY_KEYS", str(keyfile))
    app = make_app(tmp_path)
    run = http_runtime_run(TestClient(app))
    from agentco.stores import open_queue

    queue = open_queue(str(tmp_path / "work.jsonl"))
    queue.hub_id, queue.stores = "hub-a", {"runtime-a": frozenset({"rt-agent"})}
    conn = db.connect(tmp_path / "api.sqlite3")
    assert pulse.check_orphaned_runs(conn, queue, now=datetime.now(timezone.utc), cadences={})[0] == []
    revoked = subprocess.run([sys.executable, "-m", "agentco", "revoke", "rt-agent", "--apply"], capture_output=True,
                             text=True, env={**os.environ, "AGENTCO_REGISTRY_KEYS": str(keyfile)})
    assert revoked.returncode == 0, revoked.stderr
    orphans, findings = pulse.check_orphaned_runs(conn, queue, now=datetime.now(timezone.utc), cadences={})
    assert [o["runId"] for o in orphans] == [run["runId"]] and "no actor of the owner store" in orphans[0]["reason"]


def test_live_wire_transfer_then_the_hub_works_the_run(tmp_path):
    import socket
    import threading

    import uvicorn

    from agentco.publish import Registry

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
        dana, rt, worker = (Registry(a, KEYS[a], url) for a in ("dana", "rt-agent", "hub-worker"))
        sop = dana._call("POST", "/sops", {"title": "t", "inputs": [], "roles": {"w": {"kind": "agent"}},
                                           "steps": [{"name": "one", "role": "w", "purpose": "p", "gate": GATE}]})["sop"]
        dana._call("POST", f"/sops/{sop['asop_id']}/activate", {"version": 1})
        run = rt.sop_run(sop["asop_id"], {}, {"w": "rt-agent"}, ownerStore="runtime-a")["run"]
        held = rt.work_pull()
        dana._call("POST", f"/runs/{run['runId']}/transfer", {"to": "hub-a", "reason": "owner unavailable"})
        from agentco.publish import RegistryError
        with pytest.raises(RegistryError) as exc:
            rt.work_report(held["item"]["id"], held["attempt"], "done", attestation=ATT)
        assert exc.value.status == 403 and exc.value.payload["code"] == "not_the_owner"
        pulled = worker.work_pull()
        assert pulled["item"]["id"] == held["item"]["id"] and pulled["attempt"] > held["attempt"]
        assert worker.work_report(pulled["item"]["id"], pulled["attempt"], "done", attestation=ATT)["item"]["status"] == "done"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
