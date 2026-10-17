"""Usefulness and decay, persisted and served (ADR 0006 §4-§5).

The engine's properties are in `test_memory_engine.py`. These tests are about
what the store and the HTTP surface add around it, on every backend:

  * a trace is applied once, atomically, and only over items the reporter can
    see — a walled id refuses the whole trace;
  * the OUTCOME is the plane's, derived from the work item's gate: the
    reporter cannot assert it, and only the item's executor may report;
  * cold items are kept, auditable, out of default retrieval, and come back
    by demand pull or replay, on probation;
  * pinned items are untouched by traces; ratings and replay are human-only.

Synthetic data only.
"""

from __future__ import annotations

import json
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from agentco import auth
from agentco.app import create_app
from agentco.clearance import Clearance
from agentco.errors import Refusal
from agentco.memory import MemoryStore, SqlMemoryStore
from agentco.memory_engine import Params

LEVELS = ("all", "internal", "confidential", "private")
ACME = Clearance({"acme": "confidential"}, LEVELS)
ACME_INTERNAL = Clearance({"acme": "internal"}, LEVELS)
PROV = {"kind": "run", "ref": "run:w-0001"}
# Fast-cooling parameters for the store tests: strength 1.0 goes cold after 3
# exposures. Test configuration, not a proposal for a default.
FAST = Params(age_scale=1.0, decay_exponent=2.0)


@pytest.fixture()
def store(backend, tmp_path, request):
    if backend == "jsonl":
        s = MemoryStore(tmp_path / "memory.jsonl", hub_id="hub-a")
    elif backend == "postgres":
        s = SqlMemoryStore(request.getfixturevalue("_pg_dsn"), hub_id="hub-a")
    else:
        s = SqlMemoryStore(tmp_path / "agentco.sqlite3", hub_id="hub-a")
    s.params = FAST
    return s


def item(store, level="internal", title="Retry the export once"):
    return store.create(actor="kofi", clearance=ACME, owner="acme", level=level, layer="shared",
                        title=title, body="A stalled export resumes after one retry.", provenance=PROV)


def trace(store, n, outcome="success", invoked=(), retrieved=(), clearance=ACME):
    return store.record_trace(actor="kofi", clearance=clearance, trace_id=f"t{n}", outcome=outcome,
                              invoked=list(invoked), retrieved=list(retrieved))


def cool(store, item_id, start=100):
    for n in range(start, start + 5):
        trace(store, n, retrieved=[item_id])


def test_a_trace_is_applied_once(store):
    m = item(store)
    first = trace(store, 1, invoked=[m.item_id], retrieved=[m.item_id])
    assert first["state"] == "accepted" and first["credits"][0]["effect"] == "success"
    assert trace(store, 1, invoked=[m.item_id], retrieved=[m.item_id])["state"] == "unchanged"
    state = store.state(m.item_id, ACME)
    assert state["state"]["successes"] == 1
    assert [c["effect"] for c in state["credits"]] == ["success"]
    assert "NOT VALIDATED" in state["params"]["origin"]


def test_an_unverified_trace_credits_nothing(store):
    m = item(store)
    trace(store, 1, outcome="unverified", invoked=[m.item_id], retrieved=[m.item_id])
    s = store.state(m.item_id, ACME)["state"]
    assert (s["successes"], s["failures"], s["age"], s["strength"]) == (0, 0, 0, FAST.initial_strength)


def test_a_walled_id_refuses_the_whole_trace(store):
    seen = item(store)
    walled = item(store, level="confidential", title="walled")
    with pytest.raises(Refusal) as exc:
        trace(store, 1, invoked=[seen.item_id, walled.item_id], clearance=ACME_INTERNAL)
    assert exc.value.code == "memory_unknown"
    assert store.state(seen.item_id, ACME)["state"]["successes"] == 0, "nothing was applied"
    assert trace(store, 1, invoked=[seen.item_id], clearance=ACME_INTERNAL)["state"] == "accepted", \
        "a refused trace is not recorded as seen"


def test_cold_items_are_kept_out_of_retrieval_and_revive_on_probation(store):
    m = item(store)
    cool(store, m.item_id)
    state = store.state(m.item_id, ACME)["state"]
    assert state["cold"]
    assert store.list(ACME) == []
    assert [i.item_id for i in store.list(ACME, include_cold=True)] == [m.item_id]
    assert store.get(m.item_id, ACME).item_id == m.item_id, "cold is not deleted"
    assert store.search(ACME, "export") == [] and len(store.search(ACME, "export", include_cold=True)) == 1

    revived = store.revive(m.item_id, actor="kofi", clearance=ACME)
    assert revived["revived"] and revived["state"]["probation_left"] == FAST.probation_successes
    assert revived["state"]["reconsolidate"]
    assert [i.item_id for i in store.list(ACME)] == [m.item_id]
    store.revise(m.item_id, actor="kofi", clearance=ACME, body="Retry once; if it stalls again, escalate.")
    assert not store.state(m.item_id, ACME)["state"]["reconsolidate"], "a revision is the re-check"
    effects = [c["effect"] for c in store.state(m.item_id, ACME)["credits"]]
    assert effects.count("exposure") >= 1 and "revived-demand" in effects


def test_pinned_items_ignore_traces(store):
    m = item(store)
    store.set_pinned(m.item_id, actor="dana", human=True, clearance=ACME, pinned=True, review_by="2026-01-01")
    before = store.state(m.item_id, ACME)["state"]
    cool(store, m.item_id)
    trace(store, 1, invoked=[m.item_id], retrieved=[m.item_id])
    after = store.state(m.item_id, ACME)
    assert after["state"] == before and after["activation"] == 1.0
    assert [i.item_id for i in store.reviews_due(ACME, date(2026, 6, 1))] == [m.item_id]
    assert store.reviews_due(ACME, date(2025, 6, 1)) == []


def test_rating_and_replay_are_human_only_and_replay_respects_the_wall(store):
    seen, walled = item(store), item(store, level="confidential", title="walled")
    cool(store, seen.item_id, 100)
    cool(store, walled.item_id, 200)
    for call in (lambda: store.rate(seen.item_id, actor="kofi", human=False, clearance=ACME, up=True),
                 lambda: store.replay(actor="kofi", human=False, clearance=ACME)):
        with pytest.raises(Refusal) as exc:
            call()
        assert exc.value.code == "capability_mismatch"
    assert store.replay(actor="dana", human=True, clearance=ACME_INTERNAL)["revived"] == [seen.item_id]
    assert store.state(walled.item_id, ACME)["state"]["cold"], "a replay never touches what its caller cannot see"
    rated = store.rate(seen.item_id, actor="dana", human=True, clearance=ACME, up=True)
    assert rated["state"]["ratings_up"] == 1


# --------------------------------------------------------------------------- #
# over HTTP: the outcome is the plane's
# --------------------------------------------------------------------------- #

KEYS = {"dana": "dana-secret", "kofi": "kofi-secret"}
GATE = {"kind": "deterministic", "check": "pytest -q", "max_park_seconds": 900, "on_timeout": "fail"}


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="dana",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
        memory_store=str(tmp_path / "memory.jsonl"), hub_id="hub-a", humans=["dana"],
        hub_clearance={"acme": "confidential"},
        clearances={"dana": {"acme": "confidential"}, "kofi": {"acme": "internal"}}, memory_levels=LEVELS,
    ))


def signed(method, path, actor, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    ts = str(int(time.time()))
    return {"X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
            "X-AgentCo-Signature": auth.sign(KEYS[actor], method, path, ts, raw),
            "Content-Type": "application/json"}


def post(client, path, actor, body):
    return client.post(path, content=json.dumps(body), headers=signed("POST", path, actor, body))


def get(client, path, actor):
    return client.get(path, headers=signed("GET", path, actor))


def finished_work(client, *, gate=GATE, exit_status=0, status="done"):
    created = post(client, "/work", "dana", {"title": "rebuild the exporter", **({"verify": gate} if gate else {})})
    item_id = created.json()["item"]["id"]
    pulled = post(client, "/work/pull", "kofi", {}).json()
    body = {"status": status, "attempt": pulled["attempt"]}
    if gate and status == "done":
        body["attestation"] = {"check": "pytest -q", "exit_status": exit_status,
                               "environment": "ci/synthetic", "at": "2026-10-17T00:00:00+00:00"}
    report = post(client, f"/work/{item_id}/report", "kofi", body)
    assert report.status_code == 200, report.text
    return item_id


def memory_item(client):
    return post(client, "/memory", "kofi", {
        "label": {"owner": "acme", "level": "internal"}, "layer": "shared", "title": "Retry the export once",
        "body": "A stalled export resumes after one retry.", "provenance": PROV}).json()["item"]["item_id"]


@pytest.mark.parametrize("gate,exit_status,status,expected", [
    (GATE, 0, "done", "success"),
    (GATE, 1, "done", "failure"),
    (None, 0, "done", "unverified"),
    (None, 0, "failed", "unverified"),
])
def test_the_outcome_is_derived_from_the_gate_never_asserted(client, gate, exit_status, status, expected):
    mem = memory_item(client)
    work_id = finished_work(client, gate=gate, exit_status=exit_status, status=status)
    response = post(client, "/memory/traces", "kofi", {
        "workItemId": work_id, "invoked": [mem], "retrieved": [mem], "outcome": "success"})
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == expected, "the body's own `outcome` is ignored"
    state = get(client, f"/memory/{mem}/state", "kofi").json()["state"]
    assert state["successes"] == (1 if expected == "success" else 0)
    assert state["failures"] == (1 if expected == "failure" else 0)


def test_only_the_executor_reports_a_trace_and_only_once(client):
    mem = memory_item(client)
    work_id = finished_work(client)
    refused = post(client, "/memory/traces", "dana", {"workItemId": work_id, "invoked": [mem]})
    assert refused.status_code == 403 and refused.json()["code"] == "not_the_holder"
    assert post(client, "/memory/traces", "kofi", {"workItemId": work_id, "invoked": [mem]}).json()["state"] == "accepted"
    assert post(client, "/memory/traces", "kofi", {"workItemId": work_id, "invoked": [mem]}).json()["state"] == "unchanged"


def test_a_trace_for_unfinished_work_is_refused(client):
    created = post(client, "/work", "dana", {"title": "still running", "verify": GATE}).json()["item"]["id"]
    post(client, "/work/pull", "kofi", {})
    response = post(client, "/memory/traces", "kofi", {"workItemId": created, "invoked": []})
    assert response.status_code == 409 and response.json()["code"] == "not_terminal"


def test_http_rating_revival_and_reviews(client):
    mem = memory_item(client)
    assert post(client, f"/memory/{mem}/rate", "kofi", {"rating": "up"}).json()["code"] == "capability_mismatch"
    assert post(client, f"/memory/{mem}/rate", "dana", {"rating": "sideways"}).json()["code"] == "memory_invalid"
    assert post(client, f"/memory/{mem}/rate", "dana", {"rating": "up"}).json()["state"]["ratings_up"] == 1
    assert post(client, f"/memory/{mem}/revive", "kofi", {}).json()["revived"] is False, "a warm item has nothing to revive"
    assert post(client, "/memory/replay", "kofi", {}).json()["code"] == "capability_mismatch"
    assert post(client, "/memory/replay", "dana", {}).json()["revived"] == []
    assert get(client, "/memory/reviews", "kofi").json()["count"] == 0
