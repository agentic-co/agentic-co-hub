"""Routing incoming work to the procedure it actually needs.

The failure this file guards against is not a crash. It is a rules file that
looks right and quietly sends the whole backlog to one procedure — because a
predicate was misspelled, because an empty `when` matches everything, or
because a rule that should have won was listed second. Each of those produces
work that runs under the wrong instructions and reports success.

So: an unknown predicate is refused rather than ignored, an empty condition is
refused rather than treated as a catch-all, order is first-match and asserted,
and an item that matched nothing is *reported* as a default hit rather than
being indistinguishable from one a rule chose.
"""

from __future__ import annotations

import json

import pytest

from agentco import routing
from agentco.errors import Refusal

BASE = {
    "sops": {"development": "sop-dev", "testing": "sop-test", "gap-analysis": "sop-gap"},
    "default": "development",
    "assign": "macbook",
    "requires": ["frontsteps"],
    "rules": [
        {"when": {"title_contains": "gap analysis"}, "sop": "gap-analysis"},
        {"when": {"state_in": ["In Beta", "Ready for Test"]}, "sop": "testing"},
        {"when": {"type_in": ["Bug"]}, "sop": "testing"},
    ],
}


def write(tmp_path, doc) -> str:
    path = tmp_path / "routes.json"
    path.write_text(json.dumps(doc))
    return str(path)


def item(title="Add an MCP tool", type_="User Story", state="New") -> dict:
    return {"title": title, "type": type_, "state": state}


# --------------------------------------------------------------------------- #
# Routing the three kinds of work apart
# --------------------------------------------------------------------------- #


def test_gap_analysis_development_and_testing_are_routed_apart(tmp_path):
    routes = routing.load(write(tmp_path, BASE))
    assert routes.sop_for(item(title="Steps — MCP Enablement — Gap Analysis and Enhancements"))[0] == "gap-analysis"
    assert routes.sop_for(item(state="In Beta"))[0] == "testing"
    assert routes.sop_for(item(type_="Bug"))[0] == "testing"
    assert routes.sop_for(item())[0] == "development"


def test_first_match_wins_and_the_order_is_the_files_order(tmp_path):
    """A gap analysis on a Bug is still a gap analysis — rule 1 is listed first."""
    routes = routing.load(write(tmp_path, BASE))
    both = item(title="MCP Gap Analysis", type_="Bug")
    assert routes.sop_for(both)[0] == "gap-analysis"


def test_an_item_that_matched_no_rule_is_reported_as_a_default_hit(tmp_path):
    routes = routing.load(write(tmp_path, BASE))
    _key, matched = routes.sop_for(item())
    assert matched is False
    explained = routing.explain(routes, [item(), item(type_="Bug")])
    assert [row["matchedRule"] for row in explained] == [False, True]


def test_the_route_resolves_to_a_real_sop_id(tmp_path):
    routes = routing.load(write(tmp_path, BASE))
    key, sop_id, _ = routes.sop_id_for(item(type_="Bug"))
    assert (key, sop_id) == ("testing", "sop-test")


def test_assignment_and_capability_come_from_the_routes_file(tmp_path):
    routes = routing.load(write(tmp_path, BASE))
    assert routes.assign == "macbook"
    assert routes.requires == ("frontsteps",)


# --------------------------------------------------------------------------- #
# The misconfigurations that would route everything to one procedure
# --------------------------------------------------------------------------- #


def test_a_misspelled_predicate_is_refused_rather_than_matching_everything(tmp_path):
    """An unknown key makes `when` effectively empty, and an empty `when`
    matches every item — so a typo in rule 1 routes the entire backlog."""
    doc = {**BASE, "rules": [{"when": {"title_contain": "gap"}, "sop": "gap-analysis"}]}
    with pytest.raises(Refusal) as caught:
        routing.load(write(tmp_path, doc))
    assert caught.value.code == "routes_unknown_predicate"
    assert "title_contains" in caught.value.remediation


def test_an_empty_condition_is_refused(tmp_path):
    doc = {**BASE, "rules": [{"when": {}, "sop": "testing"}]}
    with pytest.raises(Refusal) as caught:
        routing.load(write(tmp_path, doc))
    assert caught.value.code == "routes_empty_rule"


def test_a_rule_pointing_at_an_undeclared_sop_is_refused_when_it_fires(tmp_path):
    doc = {**BASE, "rules": [{"when": {"type_in": ["Bug"]}, "sop": "qa"}]}
    routes = routing.load(write(tmp_path, doc))
    with pytest.raises(Refusal) as caught:
        routes.sop_id_for(item(type_="Bug"))
    assert caught.value.code == "route_sop_unknown"
    assert "development" in caught.value.remediation


def test_a_default_that_names_no_declared_sop_is_refused_at_load(tmp_path):
    with pytest.raises(Refusal) as caught:
        routing.load(write(tmp_path, {**BASE, "default": "nope"}))
    assert caught.value.code == "routes_bad_default"


def test_a_routes_file_with_no_sops_is_refused(tmp_path):
    with pytest.raises(Refusal) as caught:
        routing.load(write(tmp_path, {"rules": []}))
    assert caught.value.code == "routes_no_sops"


def test_a_missing_or_malformed_file_names_the_fix(tmp_path):
    with pytest.raises(Refusal) as caught:
        routing.load(tmp_path / "nope.json")
    assert caught.value.code == "routes_missing"

    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(Refusal) as caught:
        routing.load(str(bad))
    assert caught.value.code == "routes_bad_json"


# --------------------------------------------------------------------------- #
# Matching details that bite
# --------------------------------------------------------------------------- #


def test_title_matching_is_case_insensitive(tmp_path):
    routes = routing.load(write(tmp_path, BASE))
    assert routes.sop_for(item(title="GAP ANALYSIS of the exporter"))[0] == "gap-analysis"


def test_every_predicate_in_a_rule_must_hold(tmp_path):
    """Predicates within one `when` are AND, not OR."""
    doc = {**BASE, "rules": [{"when": {"type_in": ["Bug"], "state_in": ["In Beta"]}, "sop": "testing"}]}
    routes = routing.load(write(tmp_path, doc))
    assert routes.sop_for(item(type_="Bug", state="New"))[0] == "development"
    assert routes.sop_for(item(type_="Bug", state="In Beta"))[0] == "testing"


def test_title_matches_any_is_the_or_form(tmp_path):
    doc = {**BASE, "rules": [{"when": {"title_matches_any": ["gap analysis", "assess"]}, "sop": "gap-analysis"}]}
    routes = routing.load(write(tmp_path, doc))
    assert routes.sop_for(item(title="Assess Delinquency API Readiness"))[0] == "gap-analysis"
    assert routes.sop_for(item(title="Add a tool"))[0] == "development"
