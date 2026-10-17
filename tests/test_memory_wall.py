"""The wall (ADR 0006 §2): every read path, tried by an actor who is not cleared.

Synthetic data only. `dana` is cleared for `(acme, confidential)`, `kofi` for
`(acme, internal)`, `ana` for `(globex, internal)`; the hub for `acme` up to
`confidential` and `globex` up to `internal`.

A path passes when the under-cleared actor learns NOTHING of a walled item's
content — not its text, not a count that includes it, not an error message
that differs from the one for an item that does not exist.

What the wall does NOT cover is pinned at the bottom of this file: tests that
DOCUMENT current behaviour (a sequence gap on the feed, per-verb call counts in
`/metrics`) instead of asserting it away. They are the known limits listed in
`docs/memory.md`, and a change that closes one should flip its test.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth, events
from agentco.app import create_app
from agentco.clearance import Clearance

LEVELS = ("all", "internal", "confidential", "private")
KEYS = {"dana": "dana-secret", "kofi": "kofi-secret", "ana": "ana-secret"}
CLEARANCES = {"dana": {"acme": "confidential"}, "kofi": {"acme": "internal"}, "ana": {"globex": "internal"}}
SECRET_WORD = "zanzibar"  # appears only in the walled item's content
PROV = {"kind": "run", "ref": "run:w-0001"}


def make_app(tmp_path, hub=None):
    return TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="dana",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
        memory_store=str(tmp_path / "memory.jsonl"), hub_id="hub-a", humans=["dana"],
        hub_clearance=hub or {"acme": "confidential", "globex": "internal"},
        clearances=CLEARANCES, memory_levels=LEVELS,
    ))


def signed(method, path, actor, body=None, query=None):
    raw = json.dumps(body).encode() if body is not None else b""
    ts = str(int(time.time()))
    return {"X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
            "X-AgentCo-Signature": auth.sign(KEYS[actor], method, path, ts, raw, query=query),
            "Content-Type": "application/json"}


def post(client, path, actor, body):
    return client.post(path, content=json.dumps(body), headers=signed("POST", path, actor, body))


def get(client, path, actor, query=""):
    return client.get(f"{path}{query}", headers=signed("GET", path, actor, query=query.lstrip("?") or None))


@pytest.fixture()
def world(tmp_path):
    client = make_app(tmp_path)
    walled = post(client, "/memory", "dana", {
        "label": {"owner": "acme", "level": "confidential"}, "layer": "shared",
        "title": f"The {SECRET_WORD} rollout order", "body": f"Ship {SECRET_WORD} to region two first.",
        "tags": [SECRET_WORD], "provenance": PROV}).json()["item"]
    open_item = post(client, "/memory", "kofi", {
        "label": {"owner": "acme", "level": "internal"}, "layer": "shared",
        "title": "Retry the export once", "body": "A stalled export resumes after one retry.",
        "provenance": PROV}).json()["item"]
    return {"client": client, "walled": walled, "open": open_item, "tmp": tmp_path}


def leaks(response) -> bool:
    return SECRET_WORD in response.text


# --------------------------------------------------------------------------- #
# every read path, under-cleared
# --------------------------------------------------------------------------- #


def test_list_excludes_the_walled_item_from_items_and_count(world):
    c = world["client"]
    for actor, expected in (("kofi", 1), ("ana", 0)):
        response = get(c, "/memory", actor)
        assert response.json()["count"] == expected and not leaks(response)
    assert get(c, "/memory", "dana").json()["count"] == 2


def test_search_never_matches_what_the_caller_cannot_see(world):
    c = world["client"]
    for actor in ("kofi", "ana"):
        response = get(c, "/memory/search", actor, f"?q={SECRET_WORD}")
        assert response.status_code == 200
        assert response.json()["count"] == 0 and not leaks(response)
    found = get(c, "/memory/search", "dana", f"?q={SECRET_WORD}").json()
    assert [i["item_id"] for i in found["items"]] == [world["walled"]["item_id"]]


@pytest.mark.parametrize("method,suffix,body", [
    ("GET", "", None),
    ("GET", "/history", None),
    ("POST", "/revise", {"body": "overwritten"}),
    ("POST", "/pin", {"review_by": "2027-01-01"}),
    ("POST", "/retire", {}),
])
def test_a_walled_item_answers_exactly_like_a_missing_one(world, method, suffix, body):
    """For every item route, and for a human who is cleared for SOME of the
    owner but not this level: the body, code and status of the reply are the
    same as for an id that does not exist."""
    c = world["client"]
    walled_id, missing_id = world["walled"]["item_id"], "mem-000000000000"
    for actor in ("kofi", "ana"):
        def call(item_id):
            path = f"/memory/{item_id}{suffix}"
            return get(c, path, actor) if method == "GET" else post(c, path, actor, body)
        hidden, missing = call(walled_id), call(missing_id)
        assert hidden.status_code == missing.status_code
        assert hidden.json() == json.loads(missing.text.replace(missing_id, walled_id)), (actor, suffix)
        assert not leaks(hidden)
    assert get(c, f"/memory/{walled_id}", "dana").json()["item"]["body"].startswith("Ship")


def test_the_feed_carries_labels_never_content_and_only_to_the_cleared(world):
    c = world["client"]
    def memory_events(actor):
        page = get(c, "/events", actor).json()
        return [e for e in page["events"] if e["kind"] == "MemoryChanged"], page
    dana_events, _ = memory_events("dana")
    assert {e["payload"]["itemId"] for e in dana_events} == {world["walled"]["item_id"], world["open"]["item_id"]}
    for event in dana_events:
        assert set(event["payload"]) == {"itemId", "label", "layer", "version", "action", "authority"}
    kofi_events, kofi_page = memory_events("kofi")
    assert [e["payload"]["itemId"] for e in kofi_events] == [world["open"]["item_id"]]
    assert kofi_page["count"] == len(kofi_page["events"])
    assert memory_events("ana")[0] == []
    filtered = get(c, "/events", "ana", "?kind=MemoryChanged").json()
    assert filtered["count"] == 0 and filtered["events"] == []


def test_an_internal_reader_with_no_viewer_gets_no_memory_events(world):
    """The digest, write-back and conformance read the feed with no viewer."""
    from agentco import db

    conn = db.connect(world["tmp"] / "api.sqlite3")
    page = events.read(conn, limit=500)
    assert [e for e in page["events"] if e["kind"] in events.WALLED_KINDS] == []


def test_the_mcp_events_tool_applies_the_same_wall(world, monkeypatch, tmp_path):
    """The local MCP backend reads the clearance from the environment, as the
    MCP server does in production (key table object form + hub clearance)."""
    from agentco import db
    from agentco.mcp_server import _LocalBackend

    keyfile = tmp_path / "keys.json"
    keyfile.write_text(json.dumps({actor: {"secret": KEYS[actor], "clearance": CLEARANCES[actor]}
                                   for actor in KEYS}))
    monkeypatch.setenv("AGENTCO_REGISTRY_KEYS", str(keyfile))
    monkeypatch.setenv("AGENTCO_HUB_CLEARANCE", "acme:confidential,globex:internal")
    monkeypatch.setenv("AGENTCO_MEMORY_LEVELS", ",".join(LEVELS))
    conn = db.connect(world["tmp"] / "api.sqlite3")

    def ids(actor):
        backend = _LocalBackend(conn, None, None, actor, str(world["tmp"] / "api.sqlite3"))
        return {e["payload"]["itemId"] for e in backend.events(limit=500)["events"]
                if e["kind"] == "MemoryChanged"}
    assert ids("kofi") == {world["open"]["item_id"]}
    assert ids("ana") == set()
    assert ids("dana") == {world["open"]["item_id"], world["walled"]["item_id"]}


def test_the_hub_clearance_walls_what_an_actor_is_cleared_for(world):
    """Hub level: the same store served by a hub cleared only to `internal`
    hides the confidential item even from `dana`, who holds `confidential`."""
    narrowed = make_app(world["tmp"], hub={"acme": "internal"})
    assert get(narrowed, "/memory", "dana").json()["count"] == 1
    hidden = get(narrowed, f"/memory/{world['walled']['item_id']}", "dana")
    assert hidden.status_code == 404 and not leaks(hidden)
    assert get(narrowed, "/memory/clearance", "dana").json()["clearance"] == {
        "public": "private", "acme": "internal"}


def test_the_pulse_count_published_on_the_feed_excludes_memory(world):
    """The pulse publishes its event count in its own feed event, which every
    reader sees. A count that included memory events would be a count leak."""
    from datetime import datetime, timezone

    from agentco import db, pulse

    conn = db.connect(world["tmp"] / "api.sqlite3")
    walled = conn.execute("SELECT COUNT(*) AS n FROM events WHERE kind = 'MemoryChanged'").fetchone()["n"]
    others = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind NOT IN ('MemoryChanged', 'PulseObserved')").fetchone()["n"]
    facts, _ = pulse.check_plane(conn, now=datetime.now(timezone.utc))
    assert walled > 0
    assert facts["events"] == others


# --------------------------------------------------------------------------- #
# known limits — documented, not asserted away (docs/memory.md)
# --------------------------------------------------------------------------- #


def test_known_limit_a_feed_sequence_gap_reveals_that_something_happened(world):
    """An under-cleared reader does not see the walled event, but the `seq`
    numbers it does see skip over it, and `nextCursor` moves past it. That
    tells the reader an event exists that it may not read — an EXISTENCE leak
    the ADR leaves open. If this test starts failing, the limit was closed:
    update docs/memory.md."""
    page = get(world["client"], "/events", "ana").json()
    visible = [e["seq"] for e in page["events"]]
    assert events.decode_cursor(page["nextCursor"]) > max(visible, default=0), \
        "the cursor moved past events this reader cannot see"


def test_known_limit_metrics_show_memory_call_counts_per_verb(world):
    """`/metrics` reports latency per verb, including `memory_create`, to any
    authenticated reader. Activity metadata, not content: open per the ADR."""
    latency = get(world["client"], "/metrics", "ana").json()["latency"]
    assert "memory_create" in json.dumps(latency)
    assert SECRET_WORD not in json.dumps(latency)


def test_known_limit_the_label_owner_is_visible_to_whoever_is_cleared_for_that_owner(world):
    """A reader cleared for `(acme, internal)` sees `acme` events at
    `internal` only — but the `authority` and `version` on those, and the
    actors on the item's history, are metadata it does see. Documented."""
    history = get(world["client"], f"/memory/{world['open']['item_id']}/history", "kofi").json()["history"]
    assert history[0]["actor"] == "kofi"


def test_a_clearance_is_a_gate_not_a_weight():
    """Narrowing only: no combination of clearances opens what one side closes."""
    hub = Clearance({"acme": "confidential"}, LEVELS)
    for agent_level in LEVELS:
        effective = hub.narrow(Clearance({"acme": agent_level}, LEVELS))
        for level in LEVELS:
            if effective.allows("acme", level):
                assert hub.allows("acme", level) and Clearance({"acme": agent_level}, LEVELS).allows("acme", level)
