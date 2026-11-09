"""The MCP surface under ADR 0004 option C, and the two defects it unblocks.

Three properties, each written as the property that should hold:

  * **A retried MCP call does not repeat its action.** HTTP and the `Registry`
    client take `idempotencyKey` on `claim_scope`, `snapshot`, `sop_revise`,
    `sop_activate`, `attest` and `adjudicate` (R4, migration 0018); the MCP
    tools did not, so a harness that lost an acknowledgement and called again
    got a second lease, a second snapshot, a third version, or — worst — a late
    `sop_activate` that undid a later one.
  * **A refusal over MCP carries its code as a field**, on the wire, beside the
    text a current client already parses.
  * **`sop_run` and `adjudicate` are MCP tools** (ADR 0004 option C): the two
    verbs an executing harness performs in its own loop.

Calls go through `MCPServer.call_tool` (or an in-process `mcp.Client`) rather
than the raw tool function, because that is where the defects live: the SDK
validates arguments against the published schema and silently DROPS an
argument the schema does not declare. A key sent to a tool that does not take
one is therefore not an error a client sees — it is a retry that quietly
repeats. Calling `.fn` directly would raise `TypeError` instead and hide that.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from agentco import auth
from agentco.mcp_server import create_server
from agentco.policy import HUMAN
from agentco.publish import Registry, RegistryError
from agentco.sop import SopLibrary

DETERMINISTIC = {"kind": "deterministic", "check": "pytest -q", "max_park_seconds": 900, "on_timeout": "fail"}
JUDGED = {"kind": "judged", "check": "a reviewer reads the diff", "max_park_seconds": 900,
          "on_timeout": "escalate", "escalate_to": "dana"}
ROLES = {"implementer": {"kind": "agent"}}
STEP = {"name": "ship", "role": "implementer", "purpose": "deploy the release",
        "definition_of_done": "released", "gate": DETERMINISTIC}
KEYS = {"dana": "dana-secret", "kofi": "kofi-secret", "operator": "op-secret"}


def local(tmp_path, actor: str):
    return create_server(
        db_path=str(tmp_path / "r.sqlite3"), work_store=str(tmp_path / "work.jsonl"),
        sop_store=str(tmp_path / "sops.jsonl"), actor=actor,
    )


def call(server, name: str, **arguments: Any) -> Any:
    """One call through the SDK's argument validation, unwrapped to what the tool returned.

    A refusal raises `ToolError` exactly as it does for the raw function, so a
    test reads the same either way.
    """
    async def go():
        return await server.call_tool(name, arguments)

    result = anyio.run(go)
    if result.is_error:
        raise ToolError(result.content[0].text if result.content else "")
    text = result.content[0].text if result.content else "null"
    return json.loads(text)


def a_sop(tmp_path) -> str:
    library = SopLibrary(tmp_path / "sops.jsonl")
    sop = library.create("deploy", author="dana", author_kind=HUMAN, roles=ROLES, steps=[STEP])
    return sop.asop_id


class LoopbackRegistry(Registry):
    """A real `Registry` whose transport is the app in-process (as test_mcp_remote.py)."""

    def __init__(self, actor: str, client):
        super().__init__(actor, KEYS[actor], "http://registry.test")
        self.client = client

    def _call(self, method: str, path: str, body: Optional[dict] = None, query: str = "") -> dict:
        raw = json.dumps(body).encode() if body is not None else b""
        ts = str(int(time.time()))
        response = self.client.request(method, f"{path}{query}", content=raw or None, headers={
            "X-AgentCo-Actor": self.actor, "X-AgentCo-Timestamp": ts,
            "X-AgentCo-Signature": auth.sign(self.secret, method, path, ts, raw),
            "Content-Type": "application/json",
        })
        if response.status_code >= 400:
            raise RegistryError(response.status_code, response.json())
        return response.json()


@pytest.fixture()
def registry_client(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from agentco.app import create_app

    return TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator",
        work_store=str(tmp_path / "api-work.jsonl"), sop_store=str(tmp_path / "api-sops.jsonl"),
        humans=["dana"],
    ))


def remote(client, actor: str):
    return create_server(registry=LoopbackRegistry(actor, client))


# --------------------------------------------------------------------------- #
# R4 residue: the idempotency key on the MCP tools
# --------------------------------------------------------------------------- #


XFAIL_R4 = pytest.mark.xfail(
    strict=True,
    reason="R4 residue: the MCP tool does not take an idempotency key, so the SDK drops it and a retry repeats",
)


@XFAIL_R4
def test_a_retried_claim_scope_over_mcp_returns_the_first_lease(tmp_path):
    mcp = local(tmp_path, "dana")
    args = {"repo": "acme/web", "prefixes": ["src/billing/invoices"], "intent": "implement",
            "idempotency_key": "line-1"}
    first = call(mcp, "claim_scope", **args)
    again = call(mcp, "claim_scope", **args)
    assert again == first, "a retry under the same key must be answered with the first outcome"
    claims = [e for e in call(mcp, "events")["events"] if e["kind"] == "scope_claimed"]
    assert len(claims) == 1, f"the retry filed a second claim: {len(claims)} claim events"


@XFAIL_R4
def test_a_retried_snapshot_over_mcp_returns_the_first_receipt(tmp_path):
    mcp = local(tmp_path, "dana")
    args = {"artifact_uri": "git:/repo#main", "purpose": "baseline", "idempotency_key": "line-2"}
    first = call(mcp, "snapshot", **args)
    assert call(mcp, "snapshot", **args) == first


@XFAIL_R4
def test_a_retried_sop_revise_over_mcp_writes_one_version(tmp_path):
    sop_id = a_sop(tmp_path)
    mcp = local(tmp_path, "kofi")
    args = {"sop_id": sop_id, "changes": {"steps": [{**STEP, "common_mistakes": ["forgot the flag"]}]},
            "idempotency_key": "line-3"}
    first = call(mcp, "sop_revise", **args)
    again = call(mcp, "sop_revise", **args)
    assert again["version"] == first["version"] == 2
    assert [v.version for v in SopLibrary(tmp_path / "sops.jsonl").history(sop_id)] == [1, 2]


@XFAIL_R4
def test_a_late_retried_sop_activate_over_mcp_does_not_undo_a_later_activation(tmp_path, monkeypatch):
    """The dangerous one: activate v1, someone activates v2, then the lost
    acknowledgement for v1 is retried. Without the key the retry re-activates
    v1 and silently reverts the procedure everyone is following."""
    monkeypatch.setenv("AGENTCO_HUMANS", "dana")
    sop_id = a_sop(tmp_path)
    mcp = local(tmp_path, "dana")
    call(mcp, "sop_activate", sop_id=sop_id, version=1, idempotency_key="line-4")
    call(mcp, "sop_revise", sop_id=sop_id, changes={"steps": [{**STEP, "purpose": "deploy, carefully"}]})
    call(mcp, "sop_activate", sop_id=sop_id, version=2, idempotency_key="line-5")
    replay = call(mcp, "sop_activate", sop_id=sop_id, version=1, idempotency_key="line-4")
    assert replay["version"] == 1, "the replay is the first outcome"
    assert call(mcp, "sop_get", sop_id=sop_id)["version"] == 2, "and it changed nothing"


@XFAIL_R4
def test_a_retried_attest_over_mcp_is_answered_not_refused(tmp_path, monkeypatch):
    """HTTP's attest takes `idempotencyKey`; the MCP tool did not, so a lost
    acknowledgement retried against the now-closed item was refused."""
    monkeypatch.setenv("AGENTCO_HUMANS", "dana")
    monkeypatch.setenv("AGENTCO_CAPABILITIES", "verify")
    executor, reviewer = local(tmp_path, "kofi"), local(tmp_path, "dana")
    item = call(executor, "work_create", title="judged", verify=JUDGED)
    pulled = call(executor, "work_pull")
    call(executor, "work_report", item_id=item["id"], attempt=pulled["lease_attempt"], status="done")
    attestation = {"check": "a reviewer reads the diff", "exit_status": 0, "environment": "laptop",
                   "at": "2026-11-09T12:00:00+00:00",
                   "verdict": {"passed": True, "reason": "read the diff and judged it correct"}}
    first = call(reviewer, "attest", item_id=item["id"], attestation=attestation, idempotency_key="line-6")
    again = call(reviewer, "attest", item_id=item["id"], attestation=attestation, idempotency_key="line-6")
    assert again["status"] == first["status"] == "done"


@XFAIL_R4
def test_a_retried_call_over_remote_mcp_carries_its_key_to_the_registry(registry_client, tmp_path):
    """Remote mode proxies to HTTP, which already dedupes — but only on a key
    the proxy forwards."""
    mcp = remote(registry_client, "kofi")
    claim = {"repo": "acme/web", "prefixes": ["src/billing/invoices"], "intent": "implement",
             "idempotency_key": "line-7"}
    assert call(mcp, "claim_scope", **claim) == call(mcp, "claim_scope", **claim)
    snap = {"artifact_uri": "git:/repo#main", "purpose": "baseline", "idempotency_key": "line-8"}
    assert call(mcp, "snapshot", **snap) == call(mcp, "snapshot", **snap)


# --------------------------------------------------------------------------- #
# MCP refusals: the code as a field
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    strict=True,
    reason="_refuse() renders the Refusal into ToolError text; the wire result carries no structured code",
)
def test_a_refusal_over_the_mcp_wire_carries_its_code_as_structured_content(tmp_path):
    """Over a real (in-process) MCP session, not the raw tool function: the
    property is about what a client receives. The text a current client
    parses must survive unchanged beside the new field."""
    from mcp import Client

    mcp = local(tmp_path, "dana")

    async def go():
        async with Client(mcp) as client:
            return await client.call_tool(
                "claim_scope", {"repo": "acme/web", "prefixes": ["src"], "intent": "implement"},
            )

    result = anyio.run(go)
    assert result.is_error
    text = result.content[0].text
    assert "scope_too_broad: " in text, "the existing text prefix is kept for current clients"
    assert "Re-claim naming at least" in text, "and the remediation with it"
    structured = result.structured_content or {}
    assert structured.get("code") == "scope_too_broad", f"no machine code on the result: {structured!r}"
    assert structured.get("state") == "refused"
    assert "Re-claim naming at least" in (structured.get("remediation") or "")


@pytest.mark.xfail(strict=True, reason="a remote refusal reaches the MCP client as text only")
def test_a_remote_refusal_keeps_the_registrys_code_as_a_field(registry_client):
    """Remote mode: the registry's own code, carried through the proxy onto the field."""
    from mcp import Client

    mcp = remote(registry_client, "kofi")

    async def go():
        async with Client(mcp) as client:
            return await client.call_tool("sop_activate", {"sop_id": "asop-deadbeef", "version": 1})

    result = anyio.run(go)
    assert result.is_error and "sop_refused: " in result.content[0].text
    assert (result.structured_content or {}).get("code") == "sop_refused", result.structured_content


# --------------------------------------------------------------------------- #
# ADR 0004 option C: sop_run and adjudicate
# --------------------------------------------------------------------------- #


XFAIL_C = pytest.mark.xfail(strict=True, reason="ADR 0004 option C: the tool is not registered yet")


@XFAIL_C
def test_sop_run_over_mcp_files_a_tree_pinned_to_the_active_version(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCO_HUMANS", "dana")
    sop_id = a_sop(tmp_path)
    SopLibrary(tmp_path / "sops.jsonl").activate(sop_id, 1, author="dana", author_kind=HUMAN)
    run = call(local(tmp_path, "kofi"), "sop_run", sop_id=sop_id, inputs={},
               bindings={"implementer": "kofi"}, metadata={"epic": "release-week"})
    assert run["runId"] and [s["step"] for s in run["steps"]] == [1]
    step = call(local(tmp_path, "kofi"), "work_pull")
    assert step["id"] == run["steps"][0]["itemId"]
    assert step["verify"] == DETERMINISTIC, "the step's own gate, never the filer's"


@XFAIL_C
def test_sop_run_over_mcp_refuses_like_http(tmp_path):
    """A draft is not runnable — the same refusal, with the same code, as HTTP."""
    sop_id = a_sop(tmp_path)
    with pytest.raises(ToolError) as caught:
        call(local(tmp_path, "kofi"), "sop_run", sop_id=sop_id, inputs={}, bindings={"implementer": "kofi"})
    assert str(caught.value).split("Error executing tool sop_run: ", 1)[-1].startswith("sop_refused: ")


@XFAIL_C
def test_adjudicate_over_mcp_tags_a_divergence_and_a_retry_is_answered_once(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCO_HUMANS", "dana")
    executor, reviewer = local(tmp_path, "kofi"), local(tmp_path, "dana")
    item = call(executor, "work_create", title="migrate")
    pulled = call(executor, "work_pull")
    call(executor, "work_report", item_id=item["id"], attempt=pulled["lease_attempt"], status="done")

    with pytest.raises(ToolError) as caught:
        call(executor, "adjudicate", item_id=item["id"], verdict="good", evidence="my own shortcut")
    assert "adjudication_self" in str(caught.value) or "executed" in str(caught.value)

    args = {"item_id": item["id"], "verdict": "bad", "evidence": "skipped the diff", "idempotency_key": "line-9"}
    tagged = call(reviewer, "adjudicate", **args)
    assert tagged["metadata"]["adjudication"]["by"] == "dana"
    again = call(reviewer, "adjudicate", **args)
    assert again["metadata"]["adjudication"] == tagged["metadata"]["adjudication"]


@XFAIL_C
def test_sop_run_and_adjudicate_over_remote_mcp(registry_client):
    """The proxy carries both verbs; the registry decides who adjudicates."""
    operator, executor, reviewer = (remote(registry_client, a) for a in ("operator", "kofi", "dana"))
    created = LoopbackRegistry("dana", registry_client).sop_create("deploy", roles=ROLES, steps=[STEP])
    sop_id = created["sop"]["asop_id"]
    call(reviewer, "sop_activate", sop_id=sop_id, version=1)
    run = call(operator, "sop_run", sop_id=sop_id, inputs={}, bindings={"implementer": "kofi"})
    pulled = call(executor, "work_pull")
    assert pulled["id"] == run["steps"][0]["itemId"]
    attestation = {"check": "pytest -q", "exit_status": 0, "environment": "ci", "at": "2026-11-09T12:00:00+00:00"}
    call(executor, "work_report", item_id=pulled["id"], attempt=pulled["lease_attempt"], status="done",
         attestation=attestation)
    tagged = call(reviewer, "adjudicate", item_id=pulled["id"], verdict="good", evidence="redundant step",
                  idempotency_key="line-10")
    assert tagged["metadata"]["adjudication"]["by"] == "dana"
    assert call(reviewer, "adjudicate", item_id=pulled["id"], verdict="good", evidence="redundant step",
                idempotency_key="line-10")["metadata"]["adjudication"] == tagged["metadata"]["adjudication"]
