"""The `verify` capability is bound to declared verifiers — DECIDE-L3, question 1.

Capabilities are asserted by the caller, so until now `verify` was routing
hygiene: a node determined to answer a judged gate needed only to declare the
word, and the executor's own node could declare it, claim the vehicle, and sit
on it. The operator now names the verifiers (`AGENTCO_VERIFIERS`), and once the
set is declared the word counts only for a declared actor — on every claim and
every verdict, on every transport, because the binding lives in the queue.

Undeclared stays self-asserted, on purpose: a registry where nobody may verify
does not become safer, it resolves every judged gate on the clock. What changes
is that the status report says which of the two it is.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth, policy, verifiers
from agentco.app import create_app
from agentco.errors import Refusal
from agentco.mcp_server import create_server
from agentco.work import CapabilityError, WorkStatus

JUDGED = {"kind": "judged", "check": "a reviewer reads the diff", "max_park_seconds": 900,
          "on_timeout": "escalate", "escalate_to": "release-owner"}


def attestation():
    return {"check": JUDGED["check"], "exit_status": 0, "environment": "reviewer laptop",
            "at": "2026-09-02T15:00:00+00:00"}


def parked(queue):
    item = queue.create("migrate the schema", verify=JUDGED)
    leased = queue.claim(item.id, "kofi")
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE)
    return queue.get(item.id)


# --------------------------------------------------------------------------- #
# the declaration
# --------------------------------------------------------------------------- #


def test_the_declaration_is_parsed_exactly():
    assert policy.verifiers_from_env("dana, reviewer-box") == {"dana", "reviewer-box"}
    assert policy.verifiers_from_env("") == frozenset()
    held, stripped = policy.bind_capabilities("kofi", ["verify", "gpu"], {"dana"})
    assert held == {"gpu"} and stripped
    held, stripped = policy.bind_capabilities("dana", ["verify"], {"dana"})
    assert held == {"verify"} and not stripped
    held, stripped = policy.bind_capabilities("kofi", ["verify"], set())
    assert held == {"verify"} and not stripped, "undeclared: self-asserted, unchanged"
    assert policy.bind_capabilities("Dana", ["verify"], {"dana"})[1], "exact spelling"


# --------------------------------------------------------------------------- #
# bound on the claim
# --------------------------------------------------------------------------- #


def test_a_declared_registry_refuses_the_vehicle_to_an_undeclared_node(queue):
    queue.verifiers = frozenset({"dana"})
    vehicle = queue.create("verify w-1", requires=["verify"])
    with pytest.raises(CapabilityError) as caught:
        queue.claim(vehicle.id, "kofi", capabilities=["verify"])
    assert "bound that capability" in str(caught.value) and "AGENTCO_VERIFIERS" in str(caught.value)
    assert queue.get(vehicle.id).status == WorkStatus.PENDING
    assert queue.claim(vehicle.id, "dana", capabilities=["verify"]) is not None


def test_the_executors_own_node_cannot_sit_on_the_vehicle(queue):
    """The scenario the question named: the executor declares verify, claims the
    vehicle for its own gate, and nobody else ever sees it."""
    queue.verifiers = frozenset({"dana"})
    item = parked(queue)
    routing = verifiers.route_open_gates(queue)
    vehicles = [i for i in queue.list() if (i.metadata or {}).get("verifies") == item.id]
    assert vehicles, routing
    with pytest.raises(CapabilityError):
        queue.claim(vehicles[0].id, "kofi", capabilities=["verify"])
    assert queue.claim(vehicles[0].id, "dana", capabilities=["verify"]) is not None


def test_an_undeclared_registry_is_self_asserted_as_before(queue):
    assert queue.verifiers == frozenset()
    vehicle = queue.create("verify w-1", requires=["verify"])
    assert queue.claim(vehicle.id, "anyone", capabilities=["verify"]) is not None


def test_other_capabilities_are_untouched_by_the_binding(queue):
    queue.verifiers = frozenset({"dana"})
    gpu = queue.create("render", requires=["gpu"])
    assert queue.claim(gpu.id, "kofi", capabilities=["gpu", "verify"]) is not None


# --------------------------------------------------------------------------- #
# bound on the verdict
# --------------------------------------------------------------------------- #


def test_a_declared_registry_refuses_the_verdict_from_an_undeclared_node(queue):
    queue.verifiers = frozenset({"dana"})
    item = parked(queue)
    with pytest.raises(Refusal) as caught:
        queue.attest(item.id, attestation(), submitted_by="eve", capabilities=["verify"])
    assert "bound it to ['dana']" in caught.value.message
    assert "Declaring the capability was never the authority" in caught.value.remediation
    assert queue.get(item.id).status == WorkStatus.AWAITING_VERIFY
    closed = queue.attest(item.id, attestation(), submitted_by="dana", capabilities=["verify"])
    assert closed.status == WorkStatus.DONE


def test_a_declared_verifier_who_executed_is_still_the_executor(queue):
    """Binding narrows who may verify; it never widens it past the separation."""
    queue.verifiers = frozenset({"kofi"})
    item = parked(queue)  # kofi executed
    with pytest.raises(Refusal):
        queue.attest(item.id, attestation(), submitted_by="kofi", capabilities=["verify"])


def test_a_deterministic_gate_is_not_a_verifiers_business(queue):
    """Its executor is its own attester; the binding is about judged gates."""
    queue.verifiers = frozenset({"dana"})
    det = {"kind": "deterministic", "check": "pytest -q", "max_park_seconds": 900, "on_timeout": "fail"}
    item = queue.create("gated", verify=det)
    leased = queue.claim(item.id, "kofi")
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE,
                        attestation={**attestation(), "check": "pytest -q", "exit_status": 1})
    rerun = queue.attest(item.id, {**attestation(), "check": "pytest -q"}, submitted_by="kofi")
    assert rerun.status == WorkStatus.DONE


# --------------------------------------------------------------------------- #
# said out loud
# --------------------------------------------------------------------------- #


def test_the_status_report_says_whether_verify_is_a_rail_or_a_word(queue):
    status = verifiers.verifier_status(queue)
    assert status["verifiersDeclared"] is None and status["capabilityBinding"] == "self-asserted"
    queue.verifiers = frozenset({"dana", "reviewer-box"})
    status = verifiers.verifier_status(queue)
    assert status["verifiersDeclared"] == ["dana", "reviewer-box"]
    assert status["capabilityBinding"] == "declared"


# --------------------------------------------------------------------------- #
# every transport inherits it — the binding lives in the queue
# --------------------------------------------------------------------------- #

KEYS = {"kofi": "kofi-secret", "dana": "dana-secret", "eve": "eve-secret", "operator": "op-secret"}


def _post(client, path, actor, body):
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    return client.post(path, content=raw, headers={
        "X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
        "X-AgentCo-Signature": auth.sign(KEYS[actor], "POST", path, ts, raw),
        "Content-Type": "application/json",
    })


def test_over_http_the_binding_holds_on_pull_and_attest(tmp_path):
    client = TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
        verifiers=["dana"],
    ))
    item = _post(client, "/work", "operator", {"title": "migrate", "verify": JUDGED}).json()["item"]
    pulled = _post(client, "/work/pull", "kofi", {}).json()
    _post(client, f"/work/{item['id']}/report", "kofi", {"attempt": pulled["attempt"], "status": "done"})
    vehicle = _post(client, "/work", "operator", {"title": "verify it", "requires": ["verify"]}).json()["item"]

    eve_pull = _post(client, "/work/pull", "eve", {"capabilities": ["verify"]}).json()
    assert eve_pull["state"] == "empty", "the vehicle is not offered to an undeclared node"
    eve_attest = _post(client, f"/work/{item['id']}/attest", "eve",
                       {"attestation": attestation(), "capabilities": ["verify"]})
    assert eve_attest.status_code >= 400 and "bound it to" in eve_attest.json()["message"]

    dana_pull = _post(client, "/work/pull", "dana", {"capabilities": ["verify"]}).json()
    assert dana_pull["state"] == "leased" and dana_pull["item"]["id"] == vehicle["id"]
    dana_attest = _post(client, f"/work/{item['id']}/attest", "dana",
                        {"attestation": attestation(), "capabilities": ["verify"]})
    assert dana_attest.status_code == 200, dana_attest.text


def test_over_mcp_the_binding_comes_from_the_operators_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCO_CAPABILITIES", "verify")
    monkeypatch.setenv("AGENTCO_VERIFIERS", "dana")
    server = lambda actor: create_server(  # noqa: E731
        db_path=str(tmp_path / "r.sqlite3"), work_store=str(tmp_path / "work.jsonl"),
        sop_store=str(tmp_path / "sops.jsonl"), actor=actor,
    )
    tool = lambda s, n: s._tool_manager.get_tool(n).fn  # noqa: E731
    kofi, eve, dana = server("kofi"), server("eve"), server("dana")
    item = tool(kofi, "work_create")(title="judged", verify=JUDGED)
    pulled = tool(kofi, "work_pull")()
    tool(kofi, "work_report")(item_id=item["id"], attempt=pulled["lease_attempt"], status="done")

    assert tool(eve, "whoami")()["verifiers"] == ["dana"]
    with pytest.raises(Exception) as caught:
        tool(eve, "attest")(item_id=item["id"], attestation=attestation())
    assert "bound it to" in str(caught.value)
    assert tool(dana, "attest")(item_id=item["id"], attestation=attestation())["status"] == "done"

    monkeypatch.delenv("AGENTCO_VERIFIERS")
    assert tool(server("eve"), "whoami")()["verifiers"].startswith("undeclared")
