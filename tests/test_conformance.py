"""One semantic core, three transports, identical results — Phase 5.

`agentco.conformance` runs each scenario through the in-process core and then
through every transport, and names any difference in step outcomes or in the
resulting world. These tests assert (a) that today there are none, per scenario
and per transport, so a drift fails one named test rather than a bundle; (b)
that the suite CAN fail — a harness that passes when a transport is broken has
proven only that it does not look; and (c) the two things the suite found the
day it was written, pinned so they do not return: MCP truncating a refusal code
at its first colon, and an outbox receipt for `sop_revise` that did not say
which version it drafted.
"""

from __future__ import annotations

import pytest

from agentco import conformance
from agentco.conformance import CARRIES, SCENARIOS, TRANSPORTS, compare, conformance_report, run_scenario
from agentco.outbox import PUSH_VERBS


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_transport_is_the_core(name, transport):
    report = compare(name, transports=(transport,))
    result = report["transports"][transport]
    assert result["conforms"], "\n".join(result["diffs"])


def test_every_transport_carries_something_in_the_scenarios_that_concern_it():
    """A transport that carries zero steps of a scenario has not been tested by
    it. The push set must actually be exercised, not just declared."""
    exercised = {t: set() for t in TRANSPORTS}
    for name in SCENARIOS:
        for transport in TRANSPORTS:
            for outcome in run_scenario(name, transport)["outcomes"]:
                if outcome["via"] == transport:
                    exercised[transport].add(outcome["step"].split(" ", 1)[1])
    assert exercised["outbox"] == set(PUSH_VERBS), exercised["outbox"] ^ set(PUSH_VERBS)
    assert exercised["mcp"] == set(CARRIES["mcp"]), exercised["mcp"] ^ set(CARRIES["mcp"])
    assert exercised["mcp-remote"] == set(CARRIES["mcp-remote"])
    assert exercised["http"] == set(CARRIES["http"]), exercised["http"] ^ set(CARRIES["http"])


def test_the_suite_can_fail(monkeypatch):
    """The test of the test: break a transport and the suite must say where."""
    real = conformance._http

    def http_that_drops_the_rider(world, s):
        if s["verb"] == "attest":
            s = {**s, "args": {k: v for k, v in s["args"].items() if k != "adjudication"}}
        return real(world, s)

    monkeypatch.setitem(conformance.DRIVERS, "http", http_that_drops_the_rider)
    report = compare("judged-gate", transports=("http",))
    result = report["transports"]["http"]
    assert not result["conforms"]
    assert any("adjudication" in d for d in result["diffs"]), result["diffs"]


def test_a_refusal_code_difference_is_a_finding(monkeypatch):
    real = conformance._mcp

    def mcp_with_a_different_code(world, s):
        out = real(world, s)
        if out.get("state") == "refused":
            out = {**out, "code": "something_else"}
        return out

    monkeypatch.setitem(conformance.DRIVERS, "mcp", mcp_with_a_different_code)
    result = compare("procedure", transports=("mcp",))["transports"]["mcp"]
    assert not result["conforms"]
    assert any("revision_policy:protected" in d and "something_else" in d for d in result["diffs"])


def test_a_transport_that_reports_the_same_outcome_but_leaves_a_different_world_is_caught(monkeypatch):
    """Outcomes can agree while the world does not. The photograph is what
    catches a transport that says 'ok' and writes something else."""
    real = conformance._outbox

    def outbox_that_flips_the_verdict(world, s):
        if s["verb"] == "adjudicate":
            s = {**s, "args": {**s["args"], "verdict": "good" if s["args"]["verdict"] == "bad" else "bad"}}
        return real(world, s)

    monkeypatch.setitem(conformance.DRIVERS, "outbox", outbox_that_flips_the_verdict)
    result = compare("adjudication", transports=("outbox",))["transports"]["outbox"]
    assert not result["conforms"]
    assert all("step" not in d for d in result["diffs"]), "every step still reported ok"
    assert any("adjudication.verdict" in d for d in result["diffs"]), result["diffs"]


def test_a_transport_with_a_side_effect_is_caught_by_the_photograph(monkeypatch):
    real = conformance._http

    def http_that_files_an_extra_item(world, s):
        out = real(world, s)
        if s["verb"] == "work_create":
            world.queue.create("a stowaway")
        return out

    monkeypatch.setitem(conformance.DRIVERS, "http", http_that_files_an_extra_item)
    result = compare("work", transports=("http",))["transports"]["http"]
    assert not result["conforms"]
    assert any("state.items" in d and "entries" in d for d in result["diffs"]), result["diffs"]


def test_the_whole_report_conforms():
    report = conformance_report()
    assert report["conforms"], "\n".join(report["failures"])
    assert {r["scenario"] for r in report["scenarios"]} == set(SCENARIOS)


# --------------------------------------------------------------------------- #
# what the suite found on day one, pinned
# --------------------------------------------------------------------------- #


def test_a_code_with_a_colon_survives_rendering():
    """`revision_policy:protected` came back from MCP as `revision_policy`."""
    from agentco.errors import Refusal
    rendered = str(Refusal(code="revision_policy:protected", message="m", remediation="r"))
    assert conformance._code_of(rendered) == "revision_policy:protected"


def test_an_outbox_receipt_for_a_revision_names_the_version():
    """The publisher who drafts a version through the outbox has to be able to
    activate it — which needs the version number the receipt did not carry."""
    from agentco.outbox import _thin
    thinned = _thin({"state": "accepted", "sop": {"sop_id": "sop-1", "version": 2, "status": "draft",
                                                   "purpose": "a long body the receipt should not carry"}})
    assert thinned == {"state": "accepted", "sop_id": "sop-1", "version": 2, "status": "draft"}


# --------------------------------------------------------------------------- #
# agentco conform --level — the owner runs it themselves
# --------------------------------------------------------------------------- #

import json as _json  # noqa: E402

from agentco import cli  # noqa: E402


@pytest.mark.parametrize("level", ["L1", "L2", "L3"])
def test_conform_exits_zero_when_the_level_conforms(level, capsys):
    assert cli.main(["conform", "--level", level, "--json"]) == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["conforms"] and out["missing"] == [] and out["level"] == level
    if level == "L2":
        assert out["budget"]["tools"] <= 12 and out["budget"]["schemaBytes"] <= 12_500


def test_conform_exits_non_zero_and_names_what_is_missing(monkeypatch, capsys):
    real = conformance._outbox

    def outbox_that_forgets_evidence(world, s):
        if s["verb"] == "adjudicate":
            s = {**s, "args": {**s["args"], "evidence": "x"}}
        return real(world, s)

    monkeypatch.setitem(conformance.DRIVERS, "outbox", outbox_that_forgets_evidence)
    # Evidence is not photographed, so that drift is invisible — deliberately:
    # make one that is.
    def outbox_that_drops_adjudications(world, s):
        if s["verb"] == "adjudicate":
            return {"state": "ok"}
        return real(world, s)

    monkeypatch.setitem(conformance.DRIVERS, "outbox", outbox_that_drops_adjudications)
    code = cli.main(["conform", "--level", "L1", "--json"])
    out = _json.loads(capsys.readouterr().out)
    assert code == 1 and not out["conforms"]
    assert any("adjudication" in m and "outbox" in m for m in out["missing"]), out["missing"]
    assert cli.main(["conform", "--level", "L3"]) == 1
    assert "NOT CONFORMANT" in capsys.readouterr().out


def test_conform_refuses_an_unknown_level(capsys):
    assert cli.main(["conform", "--level", "L9"]) == 2


def test_the_budget_is_held_by_conform_not_only_by_the_test_suite(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_mcp_budget", lambda: {"tools": 13, "toolCeiling": 12, "schemaBytes": 13_000, "byteBudget": 12_500})
    assert cli.main(["conform", "--level", "L2", "--json"]) == 1
    out = _json.loads(capsys.readouterr().out)
    assert any("13 MCP tools" in m for m in out["missing"]) and any("13000 bytes" in m for m in out["missing"])


# --------------------------------------------------------------------------- #
# second-party findings on P5.V (max-p5v, e710ebc)
# --------------------------------------------------------------------------- #


def test_the_suite_never_touches_the_operators_stores(tmp_path, monkeypatch):
    """Finding 1: with AGENTCO_DB set, `conform` wrote 29 items into the live
    database. Every store-finding variable is unset for the run now."""
    live = tmp_path / "live.sqlite3"
    monkeypatch.setenv("AGENTCO_DB", str(live))
    monkeypatch.setenv("AGENTCO_REGISTRY_DB", str(tmp_path / "live-registry.sqlite3"))
    monkeypatch.setenv("AGENTCO_WORK_STORE", str(tmp_path / "live-work.jsonl"))
    monkeypatch.setenv("AGENTCO_SOP_STORE", str(tmp_path / "live-sops.jsonl"))
    monkeypatch.setenv("AGENTCO_REGISTRY_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("AGENTCO_SECRET", "not-a-registry")
    monkeypatch.setenv("AGENTCO_ACTOR", "somebody-else")
    result = compare("work", transports=("mcp", "http"))
    assert all(r["conforms"] for r in result["transports"].values())
    assert not live.exists() and not (tmp_path / "live-work.jsonl").exists() and not (tmp_path / "live-sops.jsonl").exists()
    for level in ("L2", "L3"):   # L2 runs the budget probe, which opens an MCP server of its own
        assert cli.main(["conform", "--level", level, "--json"]) == 0
        assert not live.exists(), level
    # ...and the environment is handed back as it was.
    import os
    assert os.environ["AGENTCO_DB"] == str(live) and os.environ["AGENTCO_REGISTRY_URL"] == "http://127.0.0.1:9"


def test_the_wrong_inputs_are_sent_and_agree_everywhere():
    """Finding 2: no scenario sent an unknown id, a non-terminal status or a
    version below one — and the transports disagreed on all three."""
    for name in ("work", "judged-gate", "procedure"):
        report = compare(name)
        for transport, result in report["transports"].items():
            assert result["conforms"], f"{name}/{transport}: " + "\n".join(result["diffs"])
    outcomes = {o["step"] + "#" + str(i): o for i, o in enumerate(run_scenario("work", "mcp")["outcomes"])}
    codes = [o["code"] for o in outcomes.values() if o["state"] == "refused"]
    assert "not_terminal" in codes and "work_item_unknown" in codes and "work_conflict" in codes


def test_the_photograph_sees_the_result_and_the_attestation_body(monkeypatch):
    """Finding 4 / mutants I and J: a transport that dropped `result` or rewrote
    the attestation's environment passed, because neither was photographed."""
    real = conformance._http

    def http_that_loses_the_result(world, s):
        if s["verb"] == "work_report":
            s = {**s, "args": {k: v for k, v in s["args"].items() if k != "result"}}
        return real(world, s)

    monkeypatch.setitem(conformance.DRIVERS, "http", http_that_loses_the_result)
    result = compare("work", transports=("http",))["transports"]["http"]
    assert any(".result:" in d for d in result["diffs"]), result["diffs"]

    def http_that_rewrites_the_environment(world, s):
        if s["verb"] == "attest":
            s = {**s, "args": {**s["args"], "attestation": {**s["args"]["attestation"], "environment": "elsewhere"}}}
        return real(world, s)

    monkeypatch.setitem(conformance.DRIVERS, "http", http_that_rewrites_the_environment)
    result = compare("judged-gate", transports=("http",))["transports"]["http"]
    assert any("attestation.environment" in d for d in result["diffs"]), result["diffs"]


def test_run_carries_its_bindings_over_http(monkeypatch):
    """Mutant C, restated for v3.

    The v2 mutant dropped `verify` from `sop_instantiate` — a mutation the
    contract has since made unexpressible, because a filer no longer sends a
    gate at all. What a transport CAN still silently send less of is the
    bindings, and the consequence is worse than a missing gate: a tree filed
    with nobody bound to its roles. So that is the mutation now.
    """
    real = conformance._http

    def http_that_drops_the_bindings(world, s):
        if s["verb"] == "sop_run":
            s = {**s, "args": {k: v for k, v in s["args"].items() if k != "bindings"}}
        return real(world, s)

    monkeypatch.setitem(conformance.DRIVERS, "http", http_that_drops_the_bindings)
    result = compare("run-tree", transports=("http",))["transports"]["http"]
    assert not result["conforms"], "a run lost its role bindings and nobody noticed"


def test_run_carries_its_declared_inputs_over_http(monkeypatch):
    """The other half: a transport that drops the run's inputs files nothing at
    all (`inputs_missing`), and a suite that did not notice would be blessing a
    transport on which no procedure with declared inputs can ever be run."""
    real = conformance._http

    def http_that_drops_the_inputs(world, s):
        if s["verb"] == "sop_run":
            s = {**s, "args": {k: v for k, v in s["args"].items() if k != "inputs"}}
        return real(world, s)

    monkeypatch.setitem(conformance.DRIVERS, "http", http_that_drops_the_inputs)
    result = compare("run-tree", transports=("http",))["transports"]["http"]
    assert not result["conforms"], "a run lost its declared inputs and nobody noticed"


def test_the_lease_length_requires_and_instance_metadata_are_conformed(monkeypatch):
    """Second review, mutants N5/N6/N8: ttlSeconds, `requires` and instantiate
    metadata were never sent. They are now, and a transport dropping any of
    them is caught."""
    real = conformance._http

    def http_with_a_one_second_lease(world, s):
        if s["verb"] == "work_pull" and s["args"].get("ttl_seconds"):
            s = {**s, "args": {**s["args"], "ttl_seconds": 1}}
        return real(world, s)

    monkeypatch.setitem(conformance.DRIVERS, "http", http_with_a_one_second_lease)
    result = compare("work", transports=("http",))["transports"]["http"]
    assert any("lease_ttl_s" in d for d in result["diffs"]), result["diffs"]

    def http_that_drops_requires(world, s):
        if s["verb"] == "work_create":
            s = {**s, "args": {k: v for k, v in s["args"].items() if k != "requires"}}
        return real(world, s)

    monkeypatch.setitem(conformance.DRIVERS, "http", http_that_drops_requires)
    result = compare("work", transports=("http",))["transports"]["http"]
    assert any(".requires" in d for d in result["diffs"]), result["diffs"]

    def http_that_drops_run_metadata(world, s):
        if s["verb"] == "sop_run":
            s = {**s, "args": {k: v for k, v in s["args"].items() if k != "metadata"}}
        return real(world, s)

    monkeypatch.setitem(conformance.DRIVERS, "http", http_that_drops_run_metadata)
    result = compare("run-tree", transports=("http",))["transports"]["http"]
    assert any("other_metadata" in d for d in result["diffs"]), result["diffs"]
