"""Memory spaces: separation inside one hub (ADR 0009 §3, built first by owner decision).

Synthetic data only: owner `acme`, company space `agency`, team spaces
`agency/team-delivery` and `agency/team-growth`.

What would fail if spaces were dishonest (0009 validation 5): a team item
carrying a marker word reaching another team's view or the company's, by list,
search, get or the feed; an actor writing straight into an ancestor (a
promotion nobody approved); a space moved by an agent, or sideways into another
team's; widening losing the item's id or history; existing items vanishing when
the column arrives; a space ever opening what the owner label does not.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth
from agentco.app import create_app
from agentco.clearance import Clearance, build, parse_space
from agentco.errors import Refusal
from agentco.memory import MemoryStore, SqlMemoryStore
from tests.test_memory_store import LEVELS, PROV, code_of

COMPANY, DELIVERY, GROWTH = "agency", "agency/team-delivery", "agency/team-growth"
MARKER = "zebracrossing"


def cleared(space=""):
    return Clearance({"acme": "internal"}, LEVELS, space=space)


@pytest.fixture()
def store(backend, tmp_path, request):
    if backend == "jsonl":
        return MemoryStore(tmp_path / "memory.jsonl", hub_id="hub-a")
    if backend == "postgres":
        return SqlMemoryStore(request.getfixturevalue("_pg_dsn"), hub_id="hub-a")
    return SqlMemoryStore(tmp_path / "agentco.sqlite3", hub_id="hub-a")


def write(store, space, title, actor="kofi", **over):
    return store.create(actor=actor, clearance=cleared(space), owner="acme", level="internal",
                        layer="shared", title=title, body=f"{title}. {over.pop('body', '')}",
                        provenance=PROV, **over)


def titles(items):
    return sorted(i.title for i in items)


def test_a_reader_sees_its_space_and_every_ancestor_and_nothing_else(store):
    write(store, "", "root lesson")
    write(store, COMPANY, "company lesson")
    team = write(store, DELIVERY, f"delivery lesson {MARKER}")
    write(store, GROWTH, "growth lesson")

    assert titles(store.list(cleared(DELIVERY))) == ["company lesson", f"delivery lesson {MARKER}", "root lesson"]
    assert titles(store.list(cleared(GROWTH))) == ["company lesson", "growth lesson", "root lesson"]
    assert titles(store.list(cleared(COMPANY))) == ["company lesson", "root lesson"], "nothing from below"
    assert titles(store.list(cleared(""))) == ["root lesson"]
    for other in (GROWTH, COMPANY, ""):
        assert store.search(cleared(other), MARKER) == [], f"the marker reached {other or 'root'}"
        assert code_of(lambda: store.get(team.item_id, cleared(other))) == "memory_unknown"
    assert store.get(team.item_id, cleared(DELIVERY)).space == DELIVERY


def test_a_space_never_opens_what_the_owner_label_does_not(store):
    write(store, DELIVERY, f"acme team item {MARKER}")
    outsider = Clearance({"globex": "internal"}, LEVELS, space=DELIVERY)
    assert store.list(outsider) == [] and store.search(outsider, MARKER) == []


def test_an_item_is_written_in_its_writers_space_never_above_it(store):
    assert write(store, DELIVERY, "absent space means mine").space == DELIVERY
    for target in (COMPANY, "", GROWTH):
        assert code_of(lambda: store.create(
            actor="kofi", clearance=cleared(DELIVERY), owner="acme", level="internal", layer="shared",
            space=target, title="t", body="b", provenance=PROV)) == "space_not_writable"
    assert code_of(lambda: store.create(
        actor="kofi", clearance=cleared(DELIVERY), owner="acme", level="internal", layer="shared",
        space="bad//path", title="t", body="b", provenance=PROV)) == "memory_invalid"


def test_only_a_person_moves_an_item_up_or_back_down_its_own_line(store):
    item = write(store, DELIVERY, "deploys go out from the release branch")
    assert code_of(lambda: store.set_space(item.item_id, actor="bot", human=False,
                                           clearance=cleared(DELIVERY), to=COMPANY)) == "capability_mismatch"
    widened = store.set_space(item.item_id, actor="dana", human=True, clearance=cleared(DELIVERY), to=COMPANY)
    assert (widened.item_id, widened.space, widened.version) == (item.item_id, COMPANY, 2)
    assert "deploys go out from the release branch" in titles(store.list(cleared(GROWTH))), \
        "widened to the company space, every team now sees it"
    assert [e["action"] for e in store.history(item.item_id, cleared(GROWTH))] == ["create", "space"]

    sideways = write(store, DELIVERY, "delivery only")
    assert code_of(lambda: store.set_space(sideways.item_id, actor="dana", human=True,
                                           clearance=cleared(DELIVERY), to=GROWTH)) == "space_move_lateral"
    narrowed = store.set_space(item.item_id, actor="dana", human=True, clearance=cleared(DELIVERY),
                               to=DELIVERY, expected_version=2)
    assert narrowed.space == DELIVERY and narrowed.version == 3
    assert "deploys go out from the release branch" not in titles(store.list(cleared(GROWTH)))


def test_a_stale_move_is_refused(store):
    item = write(store, DELIVERY, "stale")
    store.set_space(item.item_id, actor="dana", human=True, clearance=cleared(DELIVERY), to=COMPANY)
    assert code_of(lambda: store.set_space(item.item_id, actor="dana", human=True, clearance=cleared(DELIVERY),
                                           to="", expected_version=1)) == "work_conflict"


def test_a_replica_lands_at_the_root(store):
    record = {"item_id": "mem-remote000001", "owner": "acme", "level": "internal", "layer": "shared",
              "authority": "hub-b", "version": 1, "kind": "lesson", "title": "from the parent",
              "body": "b", "provenance": PROV, "space": "their/team"}
    store.apply_replica(record, actor="relay", hub_clearance=cleared())
    assert store.get("mem-remote000001", cleared(GROWTH)).space == ""


def test_items_from_before_spaces_read_as_root(tmp_path):
    path = tmp_path / "memory.jsonl"
    store = MemoryStore(path, hub_id="hub-a")
    item = write(store, "", "old item")
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for row in rows:
        row.pop("space", None)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert MemoryStore(path, hub_id="hub-a").get(item.item_id, cleared(DELIVERY)).space == ""


@pytest.mark.parametrize("bad", ["/agency", "agency/", "a b", "a/" * 9 + "a", 7])
def test_a_space_is_a_short_path(bad):
    with pytest.raises(ValueError):
        parse_space(bad)


def test_the_hubs_clearance_keeps_the_actors_space():
    walls = build({"acme": "private"}, {"kofi": {"acme": "internal"}}, LEVELS, spaces={"kofi": DELIVERY})
    effective = walls.effective("kofi")
    assert effective.space == DELIVERY and effective.grants == {"acme": "internal"}
    assert walls.effective("nobody").space == ""


# --------------------------------------------------------------------------- #
# over HTTP, and on the feed
# --------------------------------------------------------------------------- #

KEYS = {"dana": "dana-secret", "kofi": "kofi-secret", "lena": "lena-secret", "boss": "boss-secret"}


def http(client, method, path, actor, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    ts = str(int(time.time()))
    query = path.partition("?")[2]
    headers = {"X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
               "X-AgentCo-Signature": auth.sign(KEYS[actor], method, path.partition("?")[0], ts, raw, query=query),
               "Content-Type": "application/json"}
    return client.request(method, path, content=raw or None, headers=headers)


def test_http_two_teams_one_hub(tmp_path):
    grant = {"acme": "internal"}
    app = create_app(db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="dana", hub_id="hub-a",
                     work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
                     memory_store=str(tmp_path / "memory.jsonl"), memory_enabled=True, humans=["dana"],
                     hub_clearance=grant, clearances={a: grant for a in KEYS},
                     spaces={"kofi": DELIVERY, "lena": GROWTH, "boss": COMPANY, "dana": DELIVERY})
    client = TestClient(app)
    created = http(client, "POST", "/memory", "kofi", {
        "label": {"owner": "acme", "level": "internal"}, "layer": "shared",
        "title": f"staging needs the VPN {MARKER}", "body": "b", "provenance": PROV})
    assert created.status_code == 200, created.json()
    item = created.json()["item"]
    assert item["space"] == DELIVERY

    assert http(client, "GET", "/memory/clearance", "lena").json()["space"] == GROWTH
    for reader in ("lena", "boss"):
        assert http(client, "GET", f"/memory/search?q={MARKER}", reader).json()["count"] == 0
        assert http(client, "GET", f"/memory/{item['item_id']}", reader).json()["code"] == "memory_unknown"

    above = http(client, "POST", "/memory", "kofi", {
        "label": {"owner": "acme", "level": "internal"}, "layer": "shared", "space": COMPANY,
        "title": "t", "body": "b", "provenance": PROV})
    assert above.json()["code"] == "space_not_writable"

    assert http(client, "POST", f"/memory/{item['item_id']}/space", "kofi", {"to": COMPANY}).json()["code"] == "capability_mismatch"
    moved = http(client, "POST", f"/memory/{item['item_id']}/space", "dana", {"to": COMPANY, "expected_version": 1})
    assert moved.json()["item"]["space"] == COMPANY
    assert http(client, "GET", f"/memory/search?q={MARKER}", "lena").json()["count"] == 1

    rows = app.state.conn.execute("SELECT payload FROM events WHERE kind = 'MemoryChanged'").fetchall()
    assert [json.loads(r["payload"])["space"] for r in rows] == [DELIVERY, COMPANY]
    assert all(MARKER not in r["payload"] for r in rows), "the feed carries labels and spaces, never content"
