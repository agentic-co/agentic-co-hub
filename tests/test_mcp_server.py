"""The MCP encoding of the same core `test_registry.py` and `test_work.py` cover.

These tests do not re-verify the lease fence, the scope model, or the natural-key
rule — those are proven against the library directly elsewhere. What is tested
here is the wrapper itself: that a refusal from the core reaches an MCP caller
as an error carrying its code and remediation rather than a swallowed success,
that a normal "nothing to do" answer (no ready work, a lost claim race) is NOT
turned into an exception, and that the tool budget is a fact this file checks
rather than a number someone remembers.

Tool bodies are called directly (`mcp._tool_manager.get_tool(name).fn`) rather
than over a transport, per CONTRIBUTING.md's instruction to test at the layer
where the claim can actually fail — a transport round trip would only prove
the SDK works.
"""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from agentco.mcp_server import (
    DEFAULT_ACTOR,
    DEFAULT_DB,
    DEFAULT_SOP_STORE,
    DEFAULT_WORK_STORE,
    create_server,
    resolve_actor,
    resolve_db_path,
    resolve_sop_store,
    resolve_work_store,
)


@pytest.fixture()
def mcp(tmp_path):
    return create_server(
        db_path=str(tmp_path / "registry.sqlite3"),
        work_store=str(tmp_path / "work.jsonl"),
        sop_store=str(tmp_path / "sops.jsonl"),
        actor="dana",
    )


def tool(mcp, name):
    """The raw function behind one registered tool, for calling directly."""
    found = mcp._tool_manager.get_tool(name)
    assert found is not None, f"no tool registered as {name!r}"
    return found.fn


# --------------------------------------------------------------------------- #
# The budget — enforced, not remembered
# --------------------------------------------------------------------------- #


def test_tool_count_is_capped_at_nine(mcp):
    """The design states nine as a hard ceiling. A tenth tool must fail this,
    not slip in because nobody counted."""
    names = sorted(t.name for t in mcp._tool_manager.list_tools())
    assert len(names) <= 9, f"{len(names)} tools registered: {names}"


def test_the_nine_tools_are_the_ones_the_design_names(mcp):
    expected = {
        "claim_scope",
        "release_scope",
        "snapshot",
        "events",
        "work_pull",
        "work_report",
        "work_create",
        "sop_get",
        "whoami",
    }
    names = {t.name for t in mcp._tool_manager.list_tools()}
    assert names == expected


# --------------------------------------------------------------------------- #
# Config resolution — env vars, with the same fallback shape as agentco.app
# --------------------------------------------------------------------------- #


def test_config_resolution_prefers_argument_then_env_then_default(monkeypatch):
    monkeypatch.delenv("AGENTCO_REGISTRY_DB", raising=False)
    monkeypatch.delenv("AGENTCO_WORK_STORE", raising=False)
    monkeypatch.delenv("AGENTCO_SOP_STORE", raising=False)
    monkeypatch.delenv("AGENTCO_ACTOR", raising=False)

    assert resolve_db_path() == DEFAULT_DB
    assert resolve_work_store() == DEFAULT_WORK_STORE
    assert resolve_sop_store() == DEFAULT_SOP_STORE
    assert resolve_actor() == DEFAULT_ACTOR

    monkeypatch.setenv("AGENTCO_REGISTRY_DB", "/env/db.sqlite3")
    monkeypatch.setenv("AGENTCO_ACTOR", "kofi")
    assert resolve_db_path() == "/env/db.sqlite3"
    assert resolve_actor() == "kofi"

    # An explicit argument beats the env var, same precedence as app.resolve_db_path.
    assert resolve_db_path("/explicit/db.sqlite3") == "/explicit/db.sqlite3"
    assert resolve_actor("dana") == "dana"


# --------------------------------------------------------------------------- #
# whoami — the self-description a harness checks before it stakes a claim
# --------------------------------------------------------------------------- #


def test_whoami_reports_the_configured_actor_and_stores(mcp, tmp_path):
    result = tool(mcp, "whoami")()
    assert result["actor"] == "dana"
    assert result["enforcement"] == "advisory"
    assert result["stores"]["registryDb"] == str(tmp_path / "registry.sqlite3")
    assert result["stores"]["workStore"] == str(tmp_path / "work.jsonl")
    assert result["stores"]["sopStore"] == str(tmp_path / "sops.jsonl")


# --------------------------------------------------------------------------- #
# Refusals reach the caller as an error carrying code + remediation
# --------------------------------------------------------------------------- #


def test_scope_too_broad_is_a_tool_error_carrying_code_and_remediation(mcp):
    """The scope model's headline refusal. A silent claim on `src/` would be
    exactly the failure the design exists to prevent."""
    with pytest.raises(ToolError) as excinfo:
        tool(mcp, "claim_scope")(repo="acme/web-platform", prefixes=["src"], intent="implement")
    message = str(excinfo.value)
    assert "scope_too_broad" in message
    assert "Re-claim naming at least" in message  # the remediation sentence


def test_release_of_someone_elses_lease_is_refused_not_silently_ignored(mcp):
    claimed = tool(mcp, "claim_scope")(
        repo="acme/web-platform", prefixes=["src/budget"], intent="implement", holder="dana"
    )
    # `who` (the acting identity) is baked in at server-construction time, so a
    # second server pointed at the SAME stores under a different actor name
    # stands in for "a different identity tries to release dana's lease".
    same_stores_other_actor = create_server(
        db_path=str(_registry_db_path(mcp)),
        work_store=str(_work_store_path(mcp)),
        sop_store=str(_sop_store_path(mcp)),
        actor="kofi",
    )
    with pytest.raises(ToolError) as excinfo:
        tool(same_stores_other_actor, "release_scope")(lease_uid=claimed["leaseUid"])
    message = str(excinfo.value)
    assert "not_the_holder" in message
    assert "Only the holder releases a lease" in message


def _registry_db_path(mcp) -> str:
    return tool(mcp, "whoami")()["stores"]["registryDb"]


def _work_store_path(mcp) -> str:
    return tool(mcp, "whoami")()["stores"]["workStore"]


def _sop_store_path(mcp) -> str:
    return tool(mcp, "whoami")()["stores"]["sopStore"]


def test_bad_cursor_on_events_is_refused_with_remediation(mcp):
    with pytest.raises(ToolError) as excinfo:
        tool(mcp, "events")(since="not-a-real-cursor")
    message = str(excinfo.value)
    assert "bad_cursor" in message
    assert "nextCursor" in message


def test_snapshot_without_a_purpose_is_refused(mcp):
    with pytest.raises(ToolError) as excinfo:
        tool(mcp, "snapshot")(artifact_uri="https://example.com/spec.md", purpose="   ")
    message = str(excinfo.value)
    assert "purpose_required" in message


def test_work_create_with_a_partial_generated_key_is_refused_not_repaired(mcp):
    """agentco/keys.py: kind+subject with no period is an error, never a guess
    between 'key it forever' and 'do not key it'."""
    with pytest.raises(ToolError) as excinfo:
        tool(mcp, "work_create")(title="nightly sweep", kind="sweep", subject="inbox")
    assert "period" in str(excinfo.value)


def test_work_report_on_an_unknown_item_is_refused_not_a_silent_success(mcp):
    """work.Queue.report_result returns bare None for a missing item, which the
    wrapper must not pass through as an empty success — that is exactly the
    'no-op that looks like it worked' this project refuses to ship."""
    with pytest.raises(ToolError) as excinfo:
        tool(mcp, "work_report")(item_id="w-doesnotexist", attempt=1, status="done")
    assert "w-doesnotexist" in str(excinfo.value)


def test_work_report_rejects_a_non_terminal_status(mcp):
    item = tool(mcp, "work_create")(title="x")
    with pytest.raises(ToolError) as excinfo:
        tool(mcp, "work_report")(item_id=item["id"], attempt=0, status="in_progress")
    assert "terminal" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# The lease fence still refuses a stale report through this layer
# --------------------------------------------------------------------------- #


def test_a_stale_fenced_report_is_refused_not_recorded(mcp):
    """The failure this closes: a worker that lost its lease comes back with a
    result anyway. Accepting it would silently overwrite whoever holds the
    item now. The wrapper must not paper over this — it must call straight
    into report_result and let the fence do its job."""
    item = tool(mcp, "work_create")(title="build the thing")
    pulled = tool(mcp, "work_pull")()
    assert pulled["id"] == item["id"]
    current_attempt = pulled["lease_attempt"]

    with pytest.raises(ToolError) as excinfo:
        tool(mcp, "work_report")(
            item_id=item["id"], attempt=current_attempt + 1, status="done", result="ok"
        )
    message = str(excinfo.value)
    assert "superseded" in message

    # The real report, against the correct fence, must still succeed — proving
    # the refusal above was about the STALE attempt, not a broken wrapper.
    updated = tool(mcp, "work_report")(
        item_id=item["id"], attempt=current_attempt, status="done", result="ok"
    )
    assert updated["status"] == "done"
    assert updated["result"] == "ok"


# --------------------------------------------------------------------------- #
# A lost claim / no ready work is a normal empty answer, not an exception
# --------------------------------------------------------------------------- #


def test_work_pull_on_an_empty_queue_returns_none_not_an_exception(mcp):
    assert tool(mcp, "work_pull")() is None


def test_work_pull_returns_none_when_the_only_ready_item_is_already_leased(mcp):
    item = tool(mcp, "work_create")(title="x")
    first = tool(mcp, "work_pull")()
    assert first is not None and first["id"] == item["id"]

    # Nothing left to claim — the item is now in_progress under a live lease.
    assert tool(mcp, "work_pull")() is None


def test_work_pull_skips_an_item_it_lacks_the_capability_for(mcp):
    """`ready()` deliberately does not pre-filter by capability (agentco/work.py) —
    the wrapper must not stop on the first CapabilityError, or a worker with no
    declared capabilities could never drain a mixed queue."""
    tool(mcp, "work_create")(title="needs gpu", requires=["gpu"])
    assert tool(mcp, "work_pull")(capabilities=[]) is None

    plain = tool(mcp, "work_create")(title="no requirements")
    pulled = tool(mcp, "work_pull")(capabilities=[])
    assert pulled is not None and pulled["id"] == plain["id"]


# --------------------------------------------------------------------------- #
# Duplicate suppression is a normal answer too
# --------------------------------------------------------------------------- #


def test_work_create_with_a_repeated_natural_key_returns_the_existing_item(mcp):
    first = tool(mcp, "work_create")(title="first title", natural_key="k-1")
    second = tool(mcp, "work_create")(title="a different title", natural_key="k-1")
    assert second["id"] == first["id"]
    assert second["metadata"]["natural_key_conflict"] is True


def test_sop_get_for_an_unknown_id_returns_none_not_an_error(mcp):
    assert tool(mcp, "sop_get")(sop_id="sop-doesnotexist") is None


# --------------------------------------------------------------------------- #
# stdio hygiene — nothing but the JSON-RPC channel may reach real stdout
# --------------------------------------------------------------------------- #


def test_nothing_the_tools_do_writes_to_stdout(mcp, capsys):
    """work.Queue.claim prints to STDERR on a lost claim by design (work.py) —
    that must survive. Nothing anywhere in this module may print to stdout,
    because on stdio transport that byte corrupts the JSON-RPC stream."""
    tool(mcp, "whoami")()
    tool(mcp, "claim_scope")(repo="acme/web-platform", prefixes=["src/budget"], intent="implement")
    item = tool(mcp, "work_create")(title="x")
    tool(mcp, "work_pull")()  # succeeds
    tool(mcp, "work_pull")()  # nothing left — exercises the "lost claim" stderr print path
    tool(mcp, "sop_get")(sop_id="sop-nope")

    captured = capsys.readouterr()
    assert captured.out == "", f"something wrote to stdout: {captured.out!r}"
