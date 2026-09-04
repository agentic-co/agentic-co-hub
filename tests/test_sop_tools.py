"""`sop_revise` and `sop_activate` — the last two tools, and the last two verbs.

The shared-learning WRITE path on the primary surface (MCP) and the zero-config
floor (outbox), behind the revision policy on both. What these tests defend:

  * the tools exist, and the roster is now twelve of twelve inside the byte
    budget (the roster/budget tests in test_mcp_server.py hold the numbers);
  * the policy reaches them — an agent on MCP cannot touch a protected step,
    and the kind is the OPERATOR's declaration (`AGENTCO_HUMANS`), never the
    caller's;
  * in remote mode the proxy forwards neither author nor kind: the registry
    decides from the signature and its own declaration, so nobody becomes
    human by configuring a proxy;
  * the outbox drains both verbs as drafts under the machine credential, and a
    protected step is refused at the registry with a receipt that says so.

**ASOP v3.** `sop_revise`'s `changes` is no longer a bag of flat fields — a
step's text lives inside `steps` now, so touching one field of one step means
sending the WHOLE `steps` list back (`agentco/sop.py`'s `revise()` carries
everything else forward unchanged). Every ASOP still needs at least one role
and one gated step to exist at all (`validate_asop`), so the fixtures below
build a minimal one-step body rather than the bare `purpose="p"` v2 used to
accept.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth
from agentco.app import create_app
from agentco.mcp_server import create_server
from agentco.outbox import PUSH_VERBS, Outbox, drain, registry_publisher
from agentco.policy import AGENT, HUMAN
from agentco.publish import Registry

KEYS = {"dana": "dana-secret", "bot": "bot-secret", "bigmac": "bigmac-secret", "operator": "op-secret"}

DETERMINISTIC_GATE = {"kind": "deterministic", "check": "pytest -q",
                      "max_park_seconds": 900, "on_timeout": "fail"}


def tool(server, name):
    return server._tool_manager.get_tool(name).fn


def local(tmp_path, actor):
    return create_server(
        db_path=str(tmp_path / "r.sqlite3"), work_store=str(tmp_path / "work.jsonl"),
        sop_store=str(tmp_path / "sops.jsonl"), actor=actor,
    )


def a_step(**over):
    """The one step of a minimal one-step ASOP body — enough to satisfy
    `validate_asop` (a role, a step, a gate) while keeping every test's own
    field of interest easy to see."""
    step = {"name": "ship", "role": "implementer", "purpose": "deploy the release",
            "definition_of_done": "released", "gate": DETERMINISTIC_GATE}
    step.update(over)
    return step


def a_body(*, step_over=None, **over):
    step = a_step(**(step_over or {}))
    body = {"roles": {"implementer": {"kind": "agent"}}, "steps": [step]}
    body.update(over)
    return body, step


# --------------------------------------------------------------------------- #
# MCP, local mode
# --------------------------------------------------------------------------- #


def test_the_two_tools_exist_and_the_roster_is_full(tmp_path):
    names = sorted(t.name for t in local(tmp_path, "bot")._tool_manager.list_tools())
    assert "sop_revise" in names and "sop_activate" in names
    assert len(names) == 12


def test_a_lesson_travels_over_mcp_as_a_draft_and_activates_deliberately(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCO_HUMANS", "dana")
    human, agent = local(tmp_path, "dana"), local(tmp_path, "bot")
    # A human authors v1 over the HTTP-less path used elsewhere in these tests.
    from agentco.sop import SopLibrary
    body, step = a_body(purpose="ship it")
    sop = SopLibrary(tmp_path / "sops.jsonl").create("deploy", author="dana", author_kind=HUMAN, **body)
    assert tool(human, "sop_activate")(sop_id=sop.asop_id, version=1)["status"] == "active"

    draft = tool(agent, "sop_revise")(
        sop_id=sop.asop_id,
        changes={"steps": [{**step, "common_mistakes": ["forgot to run the migration first"]}]},
    )
    assert draft["version"] == 2 and draft["status"] == "draft"
    assert draft["author"] == "bot" and draft["author_kind"] == AGENT
    assert draft["purpose"] == "ship it", "unset ASOP-level fields carry forward"
    assert draft["steps"][0]["common_mistakes"] == ["forgot to run the migration first"]
    assert tool(agent, "sop_get")(sop_id=sop.asop_id)["version"] == 1, "a draft is not promoted"

    active = tool(agent, "sop_activate")(sop_id=sop.asop_id, version=2)
    assert active["status"] == "active" and tool(agent, "sop_get")(sop_id=sop.asop_id)["version"] == 2


def test_the_policy_reaches_the_mcp_tools(tmp_path, monkeypatch):
    """`activate` is policed against whichever version is (or would become)
    active, so a fresh, never-activated protected draft has to be made
    active once — by a human — before there is a baseline for an agent's
    attempt to diverge from it to be measured against."""
    monkeypatch.setenv("AGENTCO_HUMANS", "dana")
    from agentco.sop import SopLibrary
    body, step = a_body(step_over={"name": "pay", "purpose": "p", "tags": ["money"]})
    library = SopLibrary(tmp_path / "sops.jsonl")
    sop = library.create("pay the vendor", author="dana", author_kind=HUMAN, **body)
    library.activate(sop.asop_id, 1, author="dana", author_kind=HUMAN)
    revised = library.revise(sop.asop_id, steps=[{**step, "purpose": "a sharper purpose"}],
                             author="dana", author_kind=HUMAN)

    agent = local(tmp_path, "bot")
    with pytest.raises(Exception) as caught:
        tool(agent, "sop_revise")(sop_id=sop.asop_id, changes={"steps": [{**step, "purpose": "skip approval"}]})
    assert "policy rule 'protected'" in str(caught.value)
    with pytest.raises(Exception) as caught:
        tool(agent, "sop_activate")(sop_id=sop.asop_id, version=revised.version)
    assert "policy rule 'protected'" in str(caught.value)

    human = local(tmp_path, "dana")
    changed = tool(human, "sop_revise")(sop_id=sop.asop_id, changes={"steps": [{**step, "purpose": "sharper"}]})
    assert changed["author_kind"] == HUMAN


def test_who_is_human_is_the_operators_declaration_not_the_process_name(tmp_path, monkeypatch):
    """Same actor name, different declaration, different kind. The MCP process
    cannot promote itself; only the environment the operator set can."""
    from agentco.sop import SopLibrary
    body, step = a_body()
    sop = SopLibrary(tmp_path / "sops.jsonl").create("t", author="dana", author_kind=HUMAN, **body)
    monkeypatch.delenv("AGENTCO_HUMANS", raising=False)
    agent_kind = tool(local(tmp_path, "dana"), "sop_revise")(
        sop_id=sop.asop_id, changes={"steps": [{**step, "inputs": "x"}]})["author_kind"]
    assert agent_kind == AGENT
    monkeypatch.setenv("AGENTCO_HUMANS", "dana")
    human_kind = tool(local(tmp_path, "dana"), "sop_revise")(
        sop_id=sop.asop_id, changes={"steps": [{**step, "inputs": "y"}]})["author_kind"]
    assert human_kind == HUMAN


def test_a_malformed_change_is_a_refusal_not_a_crash(tmp_path):
    """A malformed `changes` payload is a contract refusal, not a stack
    trace — the same property v2 defended, now against the v3 body shape."""
    from agentco.sop import SopLibrary
    body, _ = a_body()
    sop = SopLibrary(tmp_path / "sops.jsonl").create("t", **body)

    with pytest.raises(Exception) as caught:
        tool(local(tmp_path, "bot"), "sop_revise")(sop_id=sop.asop_id, changes={"steps": ["not a field"]})
    assert "must be a mapping" in str(caught.value)

    with pytest.raises(Exception) as caught:
        tool(local(tmp_path, "bot"), "sop_revise")(sop_id=sop.asop_id, changes={"not_a_real_field": "x"})
    assert "unknown ASOP field" in str(caught.value)


# --------------------------------------------------------------------------- #
# MCP, remote mode — the registry decides the kind
# --------------------------------------------------------------------------- #


class LoopbackRegistry(Registry):
    def __init__(self, actor, client, via=None):
        super().__init__(actor, KEYS[actor], "http://registry.test", via=via)
        self.client = client

    def _call(self, method, path, body=None, query=""):
        raw = json.dumps(body).encode() if body is not None else b""
        ts = str(int(time.time()))
        response = self.client.request(method, f"{path}{query}", content=raw or None, headers={
            "X-AgentCo-Actor": self.actor, "X-AgentCo-Timestamp": ts,
            "X-AgentCo-Signature": auth.sign(self.secret, method, path, ts, raw),
            "Content-Type": "application/json", **({"X-AgentCo-Via": self.via} if self.via else {}),
        })
        if response.status_code >= 400:
            from agentco.publish import RegistryError
            raise RegistryError(response.status_code, response.json())
        return response.json()


def _registry_app(tmp_path, humans):
    return TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"), humans=humans,
    ))


def test_in_remote_mode_the_proxy_does_not_decide_who_is_human(tmp_path, monkeypatch):
    """The MCP process declares dana human; the registry does not. The
    registry wins, because the signature is the identity and the registry's
    declaration is the policy — a proxy's env is neither."""
    from agentco.sop import SopLibrary
    body, step = a_body(step_over={"name": "pay", "purpose": "p", "tags": ["money"]})

    client = _registry_app(tmp_path, humans=[])
    monkeypatch.setenv("AGENTCO_HUMANS", "dana")
    proxy = create_server(registry=LoopbackRegistry("dana", client))
    sop_revise = tool(proxy, "sop_revise")  # exists in remote mode too
    created = SopLibrary(tmp_path / "sops.jsonl").create("t", **body)
    with pytest.raises(Exception) as caught:
        sop_revise(sop_id=created.asop_id, changes={"steps": [{**step, "purpose": "x"}]})
    assert "revision_policy:protected" in str(caught.value) or "policy rule 'protected'" in str(caught.value)

    declared = _registry_app(tmp_path / "declared", humans=["dana"])
    proxy2 = create_server(registry=LoopbackRegistry("dana", declared))
    created2 = SopLibrary(tmp_path / "declared" / "sops.jsonl").create("t", **body)
    revised = tool(proxy2, "sop_revise")(sop_id=created2.asop_id, changes={"steps": [{**step, "purpose": "x"}]})
    assert revised["author_kind"] == HUMAN and revised["author"] == "dana"
    assert tool(proxy2, "sop_activate")(sop_id=created2.asop_id, version=2)["status"] == "active"


# --------------------------------------------------------------------------- #
# outbox — the two verbs, drained as drafts under the machine credential
# --------------------------------------------------------------------------- #


def test_the_two_verbs_are_in_the_push_set():
    assert {"sop_revise", "sop_activate"} <= set(PUSH_VERBS)


def test_over_the_outbox_a_lesson_becomes_a_draft_and_activation_is_a_separate_line(tmp_path):
    client = _registry_app(tmp_path, humans=["dana"])
    from agentco.sop import SopLibrary
    library = SopLibrary(tmp_path / "sops.jsonl")
    body, step = a_body(purpose="ship it")
    sop = library.create("deploy", author="dana", author_kind=HUMAN, **body)
    library.activate(sop.asop_id, 1, author="dana", author_kind=HUMAN)

    box = Outbox(tmp_path / "bigmac" / ".agentco")
    box.push("sop_revise", {"sopId": sop.asop_id,
                            "changes": {"steps": [{**step, "common_mistakes": ["ran the migration last"]}]}},
             agent_label="aider")
    result = drain(box, registry_publisher(LoopbackRegistry("bigmac", client, via="outbox")))
    assert result["published"] == 1, result
    draft = library.get(sop.asop_id, version=2)
    assert draft.status.value == "draft" and draft.author == "bigmac" and draft.author_kind == AGENT
    assert draft.steps[0].common_mistakes == ["ran the migration last"], "the changes travelled, not just the verb"
    assert draft.purpose == "ship it"
    assert library.get(sop.asop_id).version == 1, "drained as a draft; nothing promoted"

    box.push("sop_activate", {"sopId": sop.asop_id, "version": 2})
    result = drain(box, registry_publisher(LoopbackRegistry("bigmac", client, via="outbox")))
    assert result["published"] == 1, result
    assert library.get(sop.asop_id).version == 2


def test_over_the_outbox_a_protected_step_is_refused_at_the_registry(tmp_path):
    client = _registry_app(tmp_path, humans=["dana"])
    from agentco.sop import SopLibrary
    library = SopLibrary(tmp_path / "sops.jsonl")
    body, step = a_body(step_over={"name": "pay", "purpose": "p", "tags": ["money"]})
    sop = library.create("pay", author="dana", author_kind=HUMAN, **body)
    box = Outbox(tmp_path / "bigmac" / ".agentco")
    box.push("sop_revise", {"sopId": sop.asop_id, "changes": {"steps": [{**step, "purpose": "skip approval"}]}})
    result = drain(box, registry_publisher(LoopbackRegistry("bigmac", client, via="outbox")))
    assert result["published"] == 0 and result["refused"] == 1, result
    assert library.history(sop.asop_id)[-1].version == 1
