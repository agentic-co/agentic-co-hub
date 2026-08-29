"""The Azure DevOps adapter — mapping, keying, and the promise not to write.

Three claims are worth defending here, because each one fails silently:

  * **It never writes.** An ingest holding a PAT that also has write scope is
    one bug from editing somebody's backlog. The test asserts on the requests
    actually issued, not on intent, so a PATCH added later fails this file.
  * **A repeat pull is a no-op.** The natural key is what makes this safe on a
    schedule. If the key were derived from the title, an ADO rename would file
    a second copy of the same work and nobody would notice until two agents ran
    it.
  * **Ids are org-qualified.** Two organisations both having work item 41234 is
    ordinary. Keying on the bare id silently merges them, which is worse than
    filing nothing.

The fetcher is a recorded-shape fake. That is the layer where the mapping can
actually be wrong; a live call would test Microsoft's uptime.
"""

from __future__ import annotations

import pytest

import json

from agentco import ado
from agentco.errors import Refusal
from agentco.work import Queue

ORG = "https://dev.azure.com/example-org"


def connector(**over) -> ado.Connector:
    return ado.Connector(org_url=ORG, project="Platform", **over)


def fake_fetch(recorder: list | None = None, items: list[dict] | None = None, ids: list[int] | None = None):
    """A fetcher that records every call it is asked to make."""
    calls = recorder if recorder is not None else []

    def fetch(url: str, body: dict | None = None) -> dict:
        calls.append({"url": url, "body": body, "method": "POST" if body is not None else "GET"})
        if "/wiql" in url:
            return {"workItems": [{"id": i} for i in (ids or [41234])]}
        return {"value": items or []}

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def work_item(item_id: int, title: str, item_type: str = "Task", state: str = "Active",
              project: str = "Platform") -> dict:
    return {
        "id": item_id,
        "fields": {
            "System.Id": item_id,
            "System.Title": title,
            "System.WorkItemType": item_type,
            "System.State": state,
            "System.TeamProject": project,
        },
    }


# --------------------------------------------------------------------------- #
# The promise not to write
# --------------------------------------------------------------------------- #


def test_a_pull_issues_reads_only(monkeypatch):
    """A WIQL query is a POST that reads; nothing else may be a POST."""
    fetch = fake_fetch(items=[work_item(41234, "Add an MCP tool")])
    ado.pull(fetch, connector(contains="MCP"))

    posts = [c for c in fetch.calls if c["method"] == "POST"]
    assert len(posts) == 1
    assert posts[0]["url"].endswith(f"_apis/wit/wiql?api-version={ado.API_VERSION}")
    assert set(posts[0]["body"]) == {"query"}, "the only POST body may be a query"
    assert all("workitems" not in c["url"] or c["method"] == "GET" for c in fetch.calls)


def test_the_query_excludes_closed_work():
    """Filing finished work as pending is how a queue fills with things nobody
    should do."""
    wiql = ado.build_wiql("Platform", contains="MCP")
    assert "[System.State] IN ('New', 'Active', 'To Do')" in wiql
    assert "Closed" not in wiql


# --------------------------------------------------------------------------- #
# Keying — what makes a scheduled pull safe
# --------------------------------------------------------------------------- #


def test_the_key_is_org_qualified_not_the_bare_id():
    payload = ado.to_work_payload(work_item(41234, "Add an MCP tool"), ORG)
    assert payload["source"] == "ado"
    assert payload["sourceId"] == "example-org/41234"


def test_two_orgs_with_the_same_id_do_not_collide():
    a = ado.to_work_payload(work_item(41234, "theirs"), "https://dev.azure.com/org-a")
    b = ado.to_work_payload(work_item(41234, "ours"), "https://dev.azure.com/org-b")
    assert a["sourceId"] != b["sourceId"]


def test_a_second_pull_files_nothing_new(tmp_path, capsys):
    """The whole basis for putting this on a schedule."""
    queue = Queue(tmp_path / "work.jsonl")
    payload = ado.to_work_payload(work_item(41234, "Add an MCP tool"), ORG, assign="macbook")

    def file_it(title: str) -> str:
        return queue.create(
            title, source=payload["source"], source_id=payload["sourceId"],
            assigned_agent=payload["assignedAgent"], metadata=payload["metadata"],
        ).id

    first = file_it(payload["title"])
    # A rename in ADO must not produce a second item — the key is the id.
    second = file_it("[Task 41234] Add an MCP tool (renamed upstream)")
    assert first == second
    assert len(queue.list()) == 1


# --------------------------------------------------------------------------- #
# The pointer, and what deliberately does not cross
# --------------------------------------------------------------------------- #


def test_the_payload_carries_a_pointer_and_not_a_copy():
    raw = work_item(41234, "Add an MCP tool", item_type="User Story", state="New")
    raw["fields"]["System.Description"] = "<div>a long description ADO owns</div>"
    payload = ado.to_work_payload(raw, ORG, assign="macbook")

    assert payload["title"] == "[User Story 41234] Add an MCP tool"
    assert payload["assignedAgent"] == "macbook"
    assert payload["metadata"]["url"] == f"{ORG}/Platform/_workitems/edit/41234"
    assert payload["metadata"]["adoState"] == "New"
    assert "description" not in str(payload).lower(), (
        "the description is a fact ADO owns; caching it here makes this a "
        "second system of record"
    )


def test_only_the_declared_fields_are_requested():
    fetch = fake_fetch(items=[work_item(41234, "x")])
    ado.fetch_items(fetch, ORG, [41234])
    url = fetch.calls[-1]["url"]
    assert "System.Title" in url
    assert "System.Description" not in url


def test_ids_are_batched_rather_than_sent_in_one_oversized_request():
    """Exceeding the ceiling fails the whole request rather than truncating."""
    fetch = fake_fetch(items=[])
    ado.fetch_items(fetch, ORG, list(range(ado.MAX_IDS_PER_BATCH + 5)))
    assert len(fetch.calls) == 2


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_a_missing_pat_is_refused_with_the_scope_it_needs(monkeypatch):
    monkeypatch.delenv("AGENTCO_ADO_PAT", raising=False)
    with pytest.raises(Refusal) as caught:
        ado.resolve_pat()
    assert caught.value.code == "ado_pat_missing"
    assert "Read" in caught.value.remediation


def test_a_quote_in_a_filter_cannot_break_out_of_the_query():
    wiql = ado.build_wiql("Platform", contains="it's fine")
    assert "'it''s fine'" in wiql


def test_a_newline_in_a_filter_is_refused_rather_than_repaired():
    with pytest.raises(Refusal) as caught:
        ado.build_wiql("Platform", contains="one\nDROP")
    assert caught.value.code == "ado_bad_filter"


# --------------------------------------------------------------------------- #
# The connector config — which level of the backlog counts as a unit of work
# --------------------------------------------------------------------------- #


def write_connector(tmp_path, doc) -> str:
    path = tmp_path / "connector.json"
    path.write_text(json.dumps(doc))
    return str(path)


def test_the_type_filter_is_in_the_query_not_applied_to_the_results():
    """`limit` bounds what the query returns. Filtering afterwards means asking
    for twenty, being handed twenty Epics, and filing nothing while reporting
    success."""
    fetch = fake_fetch(items=[work_item(1, "x", item_type="Feature")])
    ado.pull(fetch, connector(types=("Feature",)))
    query = fetch.calls[0]["body"]["query"]
    assert "[System.WorkItemType] IN ('Feature')" in query


def test_several_types_are_expressed_as_one_in_clause():
    wiql = ado.build_wiql("Platform", types=("Feature", "Bug"))
    assert "[System.WorkItemType] IN ('Feature', 'Bug')" in wiql


def test_no_types_configured_means_no_type_clause_at_all():
    """An empty filter must widen to everything, not narrow to nothing."""
    assert "WorkItemType" not in ado.build_wiql("Platform")


def test_an_explicit_id_of_the_wrong_type_is_dropped_and_reported(tmp_path):
    """Naming an id is not a licence to file an Epic into a Feature-level queue —
    but it is not something to swallow either."""
    fetch = fake_fetch(items=[
        work_item(1, "a feature", item_type="Feature"),
        work_item(2, "a whole programme", item_type="Epic"),
    ])
    kept, dropped = ado.pull(fetch, connector(types=("Feature",)), ids=[1, 2])
    assert [k["metadata"]["adoId"] for k in kept] == [1]
    assert [d["metadata"]["adoId"] for d in dropped] == [2]
    assert "Epic" in dropped[0]["reason"]
    assert "Feature" in dropped[0]["reason"]


def test_a_raw_wiql_is_still_held_to_the_configured_types():
    """A hand-written query bypasses the builder; the level filter still holds."""
    fetch = fake_fetch(items=[work_item(2, "an epic", item_type="Epic")])
    kept, dropped = ado.pull(fetch, connector(types=("Feature",), wiql="SELECT [System.Id] FROM WorkItems"))
    assert kept == []
    assert len(dropped) == 1


def test_a_connector_file_carries_the_level_and_the_project(tmp_path):
    conn = ado.load_connector(write_connector(tmp_path, {
        "orgUrl": ORG, "project": "Platform", "types": ["Feature"], "contains": "MCP",
    }))
    assert conn.types == ("Feature",)
    assert conn.project == "Platform"
    assert conn.contains == "MCP"
    assert conn.states == ado.DEFAULT_STATES


def test_a_connector_missing_org_or_project_is_refused(tmp_path):
    with pytest.raises(Refusal) as caught:
        ado.load_connector(write_connector(tmp_path, {"orgUrl": ORG}))
    assert caught.value.code == "connector_incomplete"
    assert "project" in caught.value.message


def test_a_blank_type_entry_is_refused_because_it_would_widen_the_query(tmp_path):
    with pytest.raises(Refusal) as caught:
        ado.load_connector(write_connector(tmp_path, {
            "orgUrl": ORG, "project": "Platform", "types": ["Feature", ""],
        }))
    assert caught.value.code == "connector_bad_types"


def test_a_missing_connector_file_names_the_shape_it_wanted(tmp_path):
    with pytest.raises(Refusal) as caught:
        ado.load_connector(tmp_path / "nope.json")
    assert caught.value.code == "connector_missing"
    assert "orgUrl" in caught.value.remediation


def test_a_team_is_an_area_path_not_a_project():
    """Pulling one team's work means querying UNDER its area — teams share a project."""
    wiql = ado.build_wiql("Platform", area_path="Platform\\Guardians")
    assert "[System.AreaPath] UNDER 'Platform\\Guardians'" in wiql
    assert " = 'Platform\\Guardians'" not in wiql, (
        "UNDER, not =: a team that later splits its area into children would "
        "silently stop matching"
    )


def test_tags_are_anded_as_separate_contains_clauses():
    """ADO stores tags as one semicolon-joined string, so CONTAINS is all there is."""
    wiql = ado.build_wiql("Platform", tags=("AI Accepted and Ready", "CommOps"))
    assert wiql.count("[System.Tags] CONTAINS") == 2
    assert "[System.Tags] CONTAINS 'AI Accepted and Ready'" in wiql


def test_a_connector_carries_the_team_area_and_its_intake_tag(tmp_path):
    conn = ado.load_connector(write_connector(tmp_path, {
        "orgUrl": ORG, "project": "Platform",
        "areaPath": "Platform\\Guardians", "tags": ["AI Accepted and Ready"],
    }))
    assert conn.area_path == "Platform\\Guardians"
    assert conn.tags == ("AI Accepted and Ready",)


def test_a_blank_tag_is_refused_because_it_would_match_everything(tmp_path):
    with pytest.raises(Refusal) as caught:
        ado.load_connector(write_connector(tmp_path, {
            "orgUrl": ORG, "project": "Platform", "tags": ["AI Accepted and Ready", "  "],
        }))
    assert caught.value.code == "connector_bad_tags"


def test_the_area_and_tag_filters_reach_the_query():
    fetch = fake_fetch(items=[work_item(1, "x", item_type="Bug")])
    ado.pull(fetch, connector(area_path="Platform\\Guardians", tags=("AI Accepted and Ready",)))
    query = fetch.calls[0]["body"]["query"]
    assert "UNDER 'Platform\\Guardians'" in query
    assert "CONTAINS 'AI Accepted and Ready'" in query


def test_an_excluded_product_never_reaches_the_query(tmp_path):
    """A board can carry several products; one deliberately out of scope should
    not be filed and left for nobody to pick up."""
    wiql = ado.build_wiql("Platform", exclude_title_matches=("[CAL]",))
    assert "NOT [System.Title] CONTAINS '[CAL]'" in wiql


def test_an_excluded_title_is_dropped_and_reported_on_the_ids_path():
    fetch = fake_fetch(items=[
        work_item(1, "[MGR] a manager bug", item_type="Bug"),
        work_item(2, "[CAL] a caliber bug", item_type="Bug"),
    ])
    kept, dropped = ado.pull(fetch, connector(exclude_title_matches=("[CAL]",)), ids=[1, 2])
    assert [k["metadata"]["adoId"] for k in kept] == [1]
    assert "[CAL]" in dropped[0]["reason"]
