"""Every ASOP body field crosses the wire: `POST /sops` and `/revise` drop nothing.

Found by the live chain proof (2026-10-18): over HTTP, an ASOP authored with
v3.5/v3.6 chaining fields — `entry_gates`, `handoffs`, `rework`, `outcomes` —
was stored WITHOUT them, with a 200. The store kept them fine; the route's
allowlist (`SOP_BODY_KEYS`) predated v3.5 and filtered them out before the
store saw them. A run of that procedure then had no entry gate, no hand-off
and no rework bound, and nothing said so.
"""

from __future__ import annotations

import json
import time
from dataclasses import fields

import pytest
from fastapi.testclient import TestClient

from agentco import auth
from agentco.app import SOP_BODY_KEYS, create_app
from agentco.sop import ASOP, CHAIN_FIELDS

KEYS = {"dana": "dana-secret"}
DET = {"kind": "deterministic", "check": "make check", "cannot_evaluate_exit": 3}
#: Fields the STORE assigns; a body may not set them.
IDENTITY = {"asop_id", "version", "status", "author", "author_kind", "superseded_by", "created_at"}


def test_the_wire_allowlist_is_every_body_field_of_the_installed_contract():
    # `title` travels beside the body (`library.create(title, **body)`).
    assert set(SOP_BODY_KEYS) == {f.name for f in fields(ASOP)} - IDENTITY - {"title"}


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(
        db_path=str(tmp_path / "r.sqlite3"), keys=KEYS, operator="dana", humans=["dana"],
        work_store=str(tmp_path / "w.jsonl"), sop_store=str(tmp_path / "s.jsonl")))


def post(client, path, body):
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    return client.post(path, content=raw, headers={
        "X-AgentCo-Actor": "dana", "X-AgentCo-Timestamp": ts,
        "X-AgentCo-Signature": auth.sign(KEYS["dana"], "POST", path, ts, raw),
        "Content-Type": "application/json"})


@pytest.mark.skipif(not {"entry_gates", "rework"} <= set(CHAIN_FIELDS), reason="needs asop-spec 0.6.0")
def test_chaining_fields_survive_create_and_revise_over_http(client):
    body = {
        "title": "Build", "inputs": [{"name": "source"}], "roles": {"w": {"kind": "agent"}},
        "steps": [{"name": "build", "role": "w", "purpose": "build", "gate": DET},
                  {"name": "prove", "role": "w", "purpose": "prove", "gate": DET, "after": [1]}],
        "entry_gates": [{"id": "source-ready", "over": ["source"], "gate": DET}],
        "outcomes": ["BUILT"], "outputs": [{"name": "report"}],
        "handoffs": [{"id": "review", "to": {"asop_id": "asop-review", "version": 1},
                      "on_outcomes": ["BUILT"], "preflight": True}],
        "on_escalate": {"to": {"asop_id": "asop-decide", "versions": ">=1"}},
        "requires": [{"kind": "human_approved", "artefact": "plan", "binds_digest": True}],
        "rework": [{"id": "redo", "to": {"step": 1}, "on_step_failure": 2, "max_rounds": 2,
                    "consumes_attempt": False}],
    }
    created = post(client, "/sops", body)
    assert created.status_code == 200, created.text
    sop = created.json()["sop"]
    for field in ("entry_gates", "outcomes", "outputs", "handoffs", "on_escalate", "requires", "rework"):
        assert sop[field], f"{field} was dropped on the way in"
    revised = post(client, f"/sops/{sop['asop_id']}/revise", {"purpose": "build carefully"}).json()["sop"]
    for field in ("entry_gates", "handoffs", "rework", "requires"):
        assert revised[field] == sop[field], f"{field} did not carry forward"
    changed = post(client, f"/sops/{sop['asop_id']}/revise", {"outcomes": ["BUILT", "BLOCKED"]}).json()["sop"]
    assert changed["outcomes"] == ["BUILT", "BLOCKED"], "a chaining field can be revised over HTTP"
