"""The MCP server pointed at a registry over HTTP instead of at local files.

The claim this file exists to defend is **parity**: a harness's `.mcp.json`
decides where the state lives, and nothing else about the tools may change. A
model on the other end of the pipe cannot know which mode it is in and must not
have to — so `work_pull` returns the bare item or `None` in both modes, and
`sop_get` returns the SOP or `None` in both, even though the HTTP surface wraps
each in an envelope a poller needs.

The failure this guards against is quiet. A server that fell back to local
files when misconfigured would write to a store nobody else can see; the first
symptom is a queue that is permanently empty on one machine, which looks like
"no work today" rather than like a fault.

The registry is driven through the real FastAPI app via `TestClient` — real
signing, real handlers, real refusals — rather than a mock, because a mock of
the transport would prove only that the mock matches the mock.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from agentco import auth
from agentco.app import create_app
from agentco.errors import Refusal
from agentco.mcp_server import create_server
from agentco.publish import Registry, RegistryError

KEYS = {"bigmac": "bigmac-secret", "macbook": "macbook-secret", "operator": "op-secret"}


class LoopbackRegistry(Registry):
    """A real Registry whose transport is the app in-process.

    Overriding `_call` and nothing else keeps every method under test —
    envelope shapes, query building, error translation — exactly as shipped.
    """

    def __init__(self, actor: str, client: TestClient):
        super().__init__(actor, KEYS[actor], "http://registry.test")
        self.client = client

    def _call(self, method: str, path: str, body: Optional[dict] = None, query: str = "") -> dict:
        raw = json.dumps(body).encode() if body is not None else b""
        ts = str(int(time.time()))
        headers = {
            "X-AgentCo-Actor": self.actor,
            "X-AgentCo-Timestamp": ts,
            "X-AgentCo-Signature": auth.sign(self.secret, method, path, ts, raw),
            "Content-Type": "application/json",
        }
        response = self.client.request(
            method, f"{path}{query}", content=raw if raw else None, headers=headers
        )
        payload = response.json()
        if response.status_code >= 400:
            raise RegistryError(response.status_code, payload)
        return payload


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "api.sqlite3"),
        keys=KEYS,
        operator="operator",
        work_store=str(tmp_path / "work.jsonl"),
        sop_store=str(tmp_path / "sops.jsonl"),
    )
    return TestClient(app)


@pytest.fixture()
def remote(client):
    """An MCP server for `macbook`, backed by the shared registry."""
    return create_server(registry=LoopbackRegistry("macbook", client))


@pytest.fixture()
def filer(client):
    """A second MCP server, a second identity — the other machine."""
    return create_server(registry=LoopbackRegistry("bigmac", client))


def tool(mcp, name):
    found = mcp._tool_manager.get_tool(name)
    assert found is not None, f"no tool registered as {name!r}"
    return found.fn


# --------------------------------------------------------------------------- #
# Mode, and the misconfiguration that must not be silent
# --------------------------------------------------------------------------- #


def test_whoami_says_which_mode_it_is_in(remote):
    who = tool(remote, "whoami")()
    assert who["actor"] == "macbook"
    assert who["mode"] == "remote"
    assert who["registryUrl"] == "http://registry.test"
    assert "stores" not in who, "a remote server holds no stores and must not claim to"


def test_a_registry_url_without_a_secret_is_refused_at_construction(monkeypatch):
    """Falling back to local files here is the worst option available."""
    monkeypatch.setenv("AGENTCO_REGISTRY_URL", "http://registry.test")
    monkeypatch.delenv("AGENTCO_SECRET", raising=False)
    with pytest.raises(Refusal) as caught:
        create_server(actor="macbook")
    assert caught.value.code == "secret_required"
    assert "keygen" in caught.value.remediation


def test_no_registry_url_still_means_local_files(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCO_REGISTRY_URL", raising=False)
    mcp = create_server(
        db_path=str(tmp_path / "r.sqlite3"),
        work_store=str(tmp_path / "w.jsonl"),
        sop_store=str(tmp_path / "s.jsonl"),
        actor="dana",
    )
    who = tool(mcp, "whoami")()
    assert who["mode"] == "local"
    assert who["stores"]["workStore"].endswith("w.jsonl")


# --------------------------------------------------------------------------- #
# Parity — the shapes a calling model depends on
# --------------------------------------------------------------------------- #


def test_work_pull_returns_the_bare_item_not_the_http_envelope(remote, filer):
    tool(filer, "work_create")(title="ship the exporter")
    pulled = tool(remote, "work_pull")()
    assert pulled is not None
    assert pulled["title"] == "ship the exporter", (
        "the HTTP envelope leaked through — a caller written against local mode "
        "would read item['title'] and get a KeyError"
    )
    assert pulled["leased_by"] == "macbook"


def test_an_empty_queue_is_none_rather_than_an_error_or_an_envelope(remote):
    assert tool(remote, "work_pull")() is None


def test_sop_get_returns_the_sop_itself_and_none_for_a_miss(remote, filer, client):
    """`sop_create` is not an MCP tool — the twelve-tool budget — so the ASOP
    is authored over HTTP, which is exactly how the other machine would do it."""
    filer_reg = LoopbackRegistry("bigmac", client)
    sop = filer_reg.sop_create(
        "Pull work safely",
        purpose="Keep the lane honest",
        roles={"puller": {"kind": "agent"}},
        steps=[{
            "name": "pull", "role": "puller", "purpose": "claim only what this lane owns",
            "gate": {"kind": "deterministic", "check": "agentco verifiers",
                     "max_park_seconds": 900, "on_timeout": "fail"},
        }],
    )["sop"]
    assert tool(remote, "sop_get")(sop_id=sop["asop_id"]) is None, "a draft is not active yet"

    filer_reg.sop_activate(sop["asop_id"], 1)
    read = tool(remote, "sop_get")(sop_id=sop["asop_id"])
    assert read["asop_id"] == sop["asop_id"]
    assert read["purpose"] == "Keep the lane honest"
    # The proxy carries the whole sequence, gates included — a relay that
    # dropped `steps` would hand the far machine a procedure with no checks.
    assert [st["name"] for st in read["steps"]] == ["pull"]
    assert read["steps"][0]["gate"]["kind"] == "deterministic"
    assert tool(remote, "sop_get")(sop_id="asop-nope") is None


def test_work_create_returns_the_item_and_a_duplicate_key_returns_the_same_one(remote):
    first = tool(remote, "work_create")(title="one", natural_key="k")
    second = tool(remote, "work_create")(title="two", natural_key="k")
    assert first["id"] == second["id"]
    assert second["title"] == "one"


# --------------------------------------------------------------------------- #
# The fence, and refusals, across the proxy
# --------------------------------------------------------------------------- #


def test_the_full_cross_machine_round_trip(remote, filer):
    """One identity files, the other pulls and reports — both over MCP."""
    tool(filer, "work_create")(title="rebuild the exporter")
    pulled = tool(remote, "work_pull")()
    done = tool(remote, "work_report")(
        item_id=pulled["id"],
        attempt=pulled["lease_attempt"],
        status="done",
        result="rebuilt",
    )
    assert done["status"] == "done"
    assert done["result"] == "rebuilt"


def test_a_stale_fence_reaches_the_caller_as_an_error_with_its_remediation(remote, filer):
    tool(filer, "work_create")(title="rebuild the exporter")
    pulled = tool(remote, "work_pull")()
    with pytest.raises(ToolError) as caught:
        tool(remote, "work_report")(
            item_id=pulled["id"],
            attempt=pulled["lease_attempt"] - 1,
            status="done",
        )
    assert "superseded" in str(caught.value).lower()


def test_a_refusal_from_the_registry_is_not_swallowed_into_a_success(remote):
    """`src/` is refused by the scope model; the refusal must cross the proxy."""
    with pytest.raises(ToolError) as caught:
        tool(remote, "claim_scope")(repo="acme/web", prefixes=["src"], intent="implement")
    assert str(caught.value).strip()


def test_a_scope_claim_made_over_the_proxy_is_visible_to_the_other_identity(remote, filer):
    tool(filer, "claim_scope")(repo="acme/web", prefixes=["src/billing"], intent="implement")
    lease = tool(remote, "claim_scope")(repo="acme/web", prefixes=["src/billing"], intent="prototype")
    holders = {c["withHolder"] for c in lease["conflicts"]}
    assert holders == {"bigmac"}


def test_events_read_through_the_proxy_carry_both_identities(remote, filer):
    tool(filer, "work_create")(title="anything")
    tool(filer, "claim_scope")(repo="acme/web", prefixes=["src/billing"], intent="implement")
    tool(remote, "claim_scope")(repo="acme/web", prefixes=["docs/runbooks"], intent="review")
    feed = tool(remote, "events")()
    assert {e["actor"] for e in feed["events"]} == {"bigmac", "macbook"}
