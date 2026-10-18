"""Unknown body keys: refused on strict routes, reported on lenient ones (owner decision 2026-10-18).

The class this closes was found four times by the live chain proof — a field
dropped on the wire with a 200 (D1, D6, D7), and a misspelt `verfiy` on
`POST /work` filing an ungated item. Every route now declares a policy
(`agentco.app.ROUTE_BODIES`); these tests fail on a route without one, assert
both policies over the real routes, and check the field-provenance message the
owner asked for: whether a NEWER released asop-spec has the key.
"""

from __future__ import annotations

import json
import time
from dataclasses import fields

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from agentco import field_provenance, wire
from agentco.app import ROUTE_BODIES, create_app
from agentco.publish import RegistryError
from tests.test_mcp_remote import KEYS, LoopbackRegistry, remote, tool  # noqa: F401

GATE = {"kind": "deterministic", "check": "make check", "max_park_seconds": 900, "on_timeout": "fail"}


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator", humans=["bigmac"],
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl")))


def reg(client, actor="macbook"):
    return LoopbackRegistry(actor, client)


# --------------------------------------------------------------------------- #
# every route is declared
# --------------------------------------------------------------------------- #


def test_every_route_declares_a_body_policy(tmp_path):
    app = create_app(db_path=str(tmp_path / "r.sqlite3"), keys=KEYS,
                     work_store=str(tmp_path / "w.jsonl"), sop_store=str(tmp_path / "s.jsonl"))
    served = {(m, r.path) for r in app.routes if isinstance(r, APIRoute) for m in r.methods if m != "HEAD"}
    undeclared = sorted(served - set(ROUTE_BODIES))
    stale = sorted(set(ROUTE_BODIES) - served)
    assert not undeclared, f"declare these in agentco.app.ROUTE_BODIES as strict or lenient: {undeclared}"
    assert not stale, f"ROUTE_BODIES declares routes that do not exist: {stale}"


# --------------------------------------------------------------------------- #
# strict
# --------------------------------------------------------------------------- #


def test_a_misspelt_gate_is_refused_and_files_nothing(client):
    r = reg(client)
    with pytest.raises(RegistryError) as exc:
        r.work_create("ship it", verfiy=GATE)
    body = exc.value.payload
    assert exc.value.status == 422 and body["code"] == "unknown_field"
    assert "'verfiy'" in body["message"] and "did you mean 'verify'" in body["message"]
    assert "unknown in every released asop-spec" in body["message"]
    assert r._call("GET", "/work")["items"] == []


def test_an_approval_with_a_misspelt_subset_is_refused(client):
    """An absent `subset` approves EVERY subject: the typo would widen an approval."""
    with pytest.raises(RegistryError) as exc:
        reg(client, "bigmac")._call("POST", "/approvals", {"artefact": "plan", "digest": "sha256:aa",
                                                          "subest": ["T1"]})
    assert exc.value.status == 422 and "did you mean 'subset'" in exc.value.payload["message"]


def test_a_named_identity_still_gets_its_own_refusal(client):
    with pytest.raises(RegistryError) as exc:
        reg(client)._call("POST", "/sops", {"title": "t", "author_kind": "human", "bogus": 1})
    assert exc.value.payload["code"] == "author_from_signature"


@pytest.mark.skipif("entry_gates" in __import__("agentco.sop", fromlist=["CHAIN_FIELDS"]).CHAIN_FIELDS,
                    reason="runs where the installed asop-spec predates 0.6.0 (CI's contract jobs)")
def test_a_field_from_a_newer_release_says_which_and_what_to_upgrade_to(client):
    with pytest.raises(RegistryError) as exc:
        reg(client)._call("POST", "/sops", {"title": "t", "entry_gates": []})
    message = exc.value.payload["message"]
    assert "'entry_gates' exists since asop-spec 0.6.0" in message
    assert "upgrade to asop-spec>=0.6.0" in message


# --------------------------------------------------------------------------- #
# lenient
# --------------------------------------------------------------------------- #


def test_a_lenient_route_serves_reports_and_counts_the_ignored_key(client):
    r = reg(client)
    receipt = r._call("POST", "/snapshots", {"artifactUri": "file:///nonexistent/a.md", "purpose": "p",
                                              "tttlDays": 2})
    assert receipt["state"] == "accepted" and receipt["ignored"] == ["tttlDays"]
    clean = r._call("POST", "/snapshots", {"artifactUri": "file:///nonexistent/b.md", "purpose": "p"})
    assert "ignored" not in clean
    assert r._call("GET", "/metrics")["ignoredFields"] == {"snapshot": {"tttlDays": 1}}


# --------------------------------------------------------------------------- #
# the MCP remote relay
# --------------------------------------------------------------------------- #


def test_the_relay_passes_the_strict_routes_and_its_transport_refuses_a_typo(remote, client):
    """The remote MCP backend sends only the keys each tool maps, so its calls
    pass the strict routes unchanged. Its transport is `Registry`, the same
    client this module uses: a typo through it is refused exactly as over HTTP."""
    item = tool(remote, "work_create")(title="ship it", verify=GATE)
    assert item["verify"]["check"] == "make check"
    pulled = tool(remote, "work_pull")()
    landed = tool(remote, "work_report")(item_id=item["id"], attempt=pulled["lease_attempt"], status="done",
                                         attestation={"check": "make check", "exit_status": 0,
                                                      "environment": "ci", "at": "2026-10-18T00:00:00+00:00"})
    assert landed["status"] == "done"
    snap = tool(remote, "snapshot")(artifact_uri="file:///nonexistent/c.md", purpose="p", ttl_days=2)
    assert "ignored" not in snap
    with pytest.raises(RegistryError) as exc:
        LoopbackRegistry("macbook", client)._call("POST", "/work/pull", {"capabilites": ["x"]})
    assert exc.value.status == 422 and "did you mean 'capabilities'" in exc.value.payload["message"]


# --------------------------------------------------------------------------- #
# the message, and the table it reads
# --------------------------------------------------------------------------- #


def test_provenance_note_names_the_release_and_the_upgrade():
    assert wire.provenance_note("entry_gates", installed="0.5.0") == (
        "'entry_gates' exists since asop-spec 0.6.0 (asop/chain field); this hub has 0.5.0 installed: "
        "upgrade to asop-spec>=0.6.0")
    assert "since 0.5.0" in wire.provenance_note("handoffs", installed="0.6.0")
    assert "does not take it here" in wire.provenance_note("handoffs", installed="0.6.0")
    assert wire.provenance_note("cases_run", installed="0.4.0").startswith(
        "'cases_run' exists since asop-spec 0.6.0 (attestation field)")
    assert "unknown in every released asop-spec (0.4.0, 0.5.0, 0.6.0)" in wire.provenance_note("zzz")


def test_every_field_of_the_installed_contract_is_in_the_provenance_table():
    """A lock bump without `tools/asop_fields.py regenerate` fails here."""
    import asop.gates as gates
    from asop.sop import ASOP, Step

    table = field_provenance.FIELDS
    assert {f.name for f in fields(ASOP)} <= set(table["asop"])
    assert {f.name for f in fields(Step)} <= set(table["step"])
    assert set(gates.GATE_FIELDS) <= set(table["gate"])
    assert set(getattr(gates, "ATTESTATION_FIELDS", ())) <= set(table["attestation"])


# --------------------------------------------------------------------------- #
# a real socket
# --------------------------------------------------------------------------- #


@pytest.fixture()
def live_hub(tmp_path):
    import socket
    import threading

    import uvicorn

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


def test_live_wire_strict_and_lenient(live_hub):
    from agentco.publish import Registry

    r = Registry("macbook", KEYS["macbook"], live_hub)
    with pytest.raises(RegistryError) as exc:
        r.work_create("ship it", verfiy=GATE)
    assert exc.value.status == 422 and exc.value.payload["code"] == "unknown_field"
    receipt = r._call("POST", "/snapshots", {"artifactUri": "file:///nonexistent/d.md", "purpose": "p", "x": 1})
    assert receipt["ignored"] == ["x"]
    assert json.dumps(r._call("GET", "/metrics")["ignoredFields"]) == '{"snapshot": {"x": 1}}'
