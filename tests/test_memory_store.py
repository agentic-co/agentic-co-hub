"""Shared memory items (ADR 0006 §1): the model, the store, and its invariants.

Synthetic data only. Owners are `acme` and `globex`, people `dana` and `kofi`.

The invariants worth a test are the ones a convenient implementation breaks:

  * **an item has exactly one owner label and one authoritative hub, for life**
    — no revision, pin, retire or replica version moves either;
  * **a replica refuses writes**, and only moves forward by a newer version
    from its own authority;
  * **session and episodic memory are refused**: they stay in the harness;
  * **nothing is deleted**: retire hides and keeps, and the audit has every write;
  * **a read cannot tell "not cleared" from "not there"**.
"""

from __future__ import annotations

import json
import random
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth
from agentco.app import create_app
from agentco.clearance import Clearance, Clearances, build, parse
from agentco.errors import Refusal
from agentco.memory import MemoryStore, SqlMemoryStore

LEVELS = ("all", "internal", "confidential", "private")
ACME_INTERNAL = Clearance({"acme": "internal"}, LEVELS)
ACME_PRIVATE = Clearance({"acme": "private"}, LEVELS)
NOBODY = Clearance({}, LEVELS)
PROV = {"kind": "run", "ref": "run:w-0001"}


@pytest.fixture()
def store(backend, tmp_path, request):
    if backend == "jsonl":
        return MemoryStore(tmp_path / "memory.jsonl", hub_id="hub-a")
    if backend == "postgres":
        return SqlMemoryStore(request.getfixturevalue("_pg_dsn"), hub_id="hub-a")
    return SqlMemoryStore(tmp_path / "agentco.sqlite3", hub_id="hub-a")


def lesson(store, clearance=ACME_INTERNAL, owner="acme", level="internal", layer="shared", **over):
    fields = {"title": "Retry the export once", "body": "A stalled export resumes after one retry.",
              "provenance": PROV, **over}
    return store.create(actor="kofi", clearance=clearance, owner=owner, level=level, layer=layer, **fields)


def code_of(call) -> str:
    with pytest.raises(Refusal) as exc:
        call()
    return exc.value.code


# --------------------------------------------------------------------------- #
# the record
# --------------------------------------------------------------------------- #


def test_an_item_round_trips_with_its_label_authority_and_provenance(store):
    item = lesson(store, tags=["export", "retry"], applies_to={"asop_id": "asop-0001", "step": 2})
    read = store.get(item.item_id, ACME_INTERNAL)
    assert read.to_dict() == item.to_dict()
    assert read.label == {"owner": "acme", "level": "internal"}
    assert read.authority == "hub-a" and read.version == 1
    assert read.provenance == PROV and read.applies_to == {"asop_id": "asop-0001", "step": 2}
    assert [e["action"] for e in store.history(item.item_id, ACME_INTERNAL)] == ["create"]


@pytest.mark.parametrize("missing", ["title", "body", "provenance"])
def test_a_required_field_is_required(store, missing):
    fields = {"title": "t", "body": "b", "provenance": PROV}
    fields.pop(missing)
    assert code_of(lambda: store.create(actor="kofi", clearance=ACME_INTERNAL, owner="acme",
                                        level="internal", layer="shared", **fields)) == "memory_invalid"


@pytest.mark.parametrize("prov", [None, {"ref": "x"}, {"kind": "run", "ref": "x", "note": "y"}, {"kind": "", "ref": "x"}])
def test_provenance_is_a_hard_field(store, prov):
    assert code_of(lambda: lesson(store, provenance=prov)) == "memory_invalid"


@pytest.mark.parametrize("layer", ["session", "episodic", "nonsense"])
def test_layers_the_harness_keeps_are_refused(store, layer):
    assert code_of(lambda: lesson(store, layer=layer)) == "memory_invalid"


def test_the_public_layer_holds_exactly_public_items(store):
    assert code_of(lambda: lesson(store, layer="public")) == "memory_invalid"
    assert code_of(lambda: lesson(store, owner="public", level="all", layer="shared")) == "memory_invalid"
    item = lesson(store, owner="public", level="all", layer="public")
    assert store.get(item.item_id, NOBODY).owner == "public", "public is readable by every clearance"


def test_writing_above_your_clearance_is_refused(store):
    assert code_of(lambda: lesson(store, level="confidential")) == "clearance_insufficient"
    assert code_of(lambda: lesson(store, owner="globex")) == "clearance_insufficient"
    assert store.list(ACME_PRIVATE) == []


# --------------------------------------------------------------------------- #
# one owner label and one authority, for life
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("field,value", [("owner", "globex"), ("level", "all"), ("layer", "personal"),
                                         ("authority", "hub-b"), ("label", {"owner": "globex"}),
                                         ("version", 9)])
def test_the_label_layer_authority_and_version_are_not_revisable(store, field, value):
    item = lesson(store)
    assert code_of(lambda: store.revise(item.item_id, actor="kofi", clearance=ACME_INTERNAL,
                                        **{field: value})) == "memory_invalid"
    assert store.get(item.item_id, ACME_INTERNAL).version == 1


def test_no_sequence_of_writes_moves_the_label_or_the_authority(store):
    """A seeded random walk over every authoritative write the store offers."""
    rng = random.Random(6006)
    items = [lesson(store, title=f"lesson {n}") for n in range(3)]
    fixed = {i.item_id: (i.owner, i.level, i.layer, i.authority) for i in items}
    for _ in range(40):
        target = rng.choice(items).item_id
        op = rng.choice(["revise", "pin", "unpin", "retire"])
        try:
            if op == "revise":
                store.revise(target, actor="kofi", clearance=ACME_INTERNAL, body=f"body {rng.random()}")
            elif op == "pin":
                store.set_pinned(target, actor="dana", human=True, clearance=ACME_INTERNAL,
                                 pinned=True, review_by="2027-01-15")
            elif op == "unpin":
                store.set_pinned(target, actor="dana", human=True, clearance=ACME_INTERNAL, pinned=False)
            else:
                store.retire(target, actor="dana", human=True, clearance=ACME_INTERNAL)
        except Refusal as exc:
            assert exc.code == "memory_invalid", exc.code  # revising a retired item
    for item_id, label in fixed.items():
        now = store.get(item_id, ACME_INTERNAL)
        assert (now.owner, now.level, now.layer, now.authority) == label
        versions = [e["version"] for e in store.history(item_id, ACME_INTERNAL)]
        assert versions == list(range(1, now.version + 1)), "every write is audited, in order"


# --------------------------------------------------------------------------- #
# replicas
# --------------------------------------------------------------------------- #


def replica_record(version=1, authority="hub-b", owner="acme", level="internal", **over):
    return {"item_id": "mem-from-hub-b", "owner": owner, "level": level, "layer": "shared",
            "authority": authority, "version": version, "kind": "lesson", "title": "From hub B",
            "body": f"version {version}", "provenance": PROV, "tags": [], "applies_to": None,
            "pinned": False, "review_by": None, "status": "active", "created_by": "remote",
            "created_at": "2026-10-01T00:00:00+00:00", "updated_by": "remote",
            "updated_at": "2026-10-01T00:00:00+00:00", **over}


def test_a_replica_refuses_every_write(store):
    store.apply_replica(replica_record(), actor="hub-b", hub_clearance=ACME_PRIVATE)
    rid = "mem-from-hub-b"
    assert code_of(lambda: store.revise(rid, actor="kofi", clearance=ACME_INTERNAL, body="x")) == "replica_read_only"
    assert code_of(lambda: store.set_pinned(rid, actor="dana", human=True, clearance=ACME_INTERNAL,
                                            pinned=True, review_by="2027-01-01")) == "replica_read_only"
    assert code_of(lambda: store.retire(rid, actor="dana", human=True, clearance=ACME_INTERNAL)) == "replica_read_only"
    assert store.get(rid, ACME_INTERNAL).body == "version 1"


def test_a_replica_only_moves_forward_from_its_own_authority(store):
    assert store.apply_replica(replica_record(2), actor="hub-b", hub_clearance=ACME_PRIVATE)["state"] == "accepted"
    assert store.apply_replica(replica_record(1), actor="hub-b", hub_clearance=ACME_PRIVATE)["state"] == "unchanged"
    assert store.apply_replica(replica_record(2), actor="hub-b", hub_clearance=ACME_PRIVATE)["state"] == "unchanged"
    assert store.apply_replica(replica_record(3), actor="hub-b", hub_clearance=ACME_PRIVATE)["state"] == "accepted"
    assert store.get("mem-from-hub-b", ACME_INTERNAL).body == "version 3"

    assert code_of(lambda: store.apply_replica(replica_record(4, authority="hub-c"), actor="hub-c",
                                               hub_clearance=ACME_PRIVATE)) == "replica_conflict"
    assert code_of(lambda: store.apply_replica(replica_record(4, level="all"), actor="hub-b",
                                               hub_clearance=ACME_PRIVATE)) == "memory_invalid"
    assert code_of(lambda: store.apply_replica(replica_record(1, authority="hub-a"), actor="x",
                                               hub_clearance=ACME_PRIVATE)) == "memory_invalid"


def test_a_hub_does_not_hold_a_replica_above_its_clearance(store):
    assert code_of(lambda: store.apply_replica(replica_record(level="private"), actor="hub-b",
                                               hub_clearance=ACME_INTERNAL)) == "clearance_insufficient"
    assert store.list(ACME_PRIVATE) == []


# --------------------------------------------------------------------------- #
# pinning, retiring, conflicts
# --------------------------------------------------------------------------- #


def test_only_a_human_pins_and_a_pin_carries_a_review_date(store):
    item = lesson(store)
    assert code_of(lambda: store.set_pinned(item.item_id, actor="kofi", human=False, clearance=ACME_INTERNAL,
                                            pinned=True, review_by="2027-01-01")) == "human_required"
    assert code_of(lambda: store.set_pinned(item.item_id, actor="dana", human=True, clearance=ACME_INTERNAL,
                                            pinned=True)) == "memory_invalid"
    pinned = store.set_pinned(item.item_id, actor="dana", human=True, clearance=ACME_INTERNAL,
                              pinned=True, review_by="2027-01-01")
    assert pinned.pinned and pinned.review_by == "2027-01-01" and pinned.version == 2


def test_retire_hides_and_keeps(store):
    item = lesson(store)
    assert code_of(lambda: store.retire(item.item_id, actor="kofi", human=False,
                                        clearance=ACME_INTERNAL)) == "human_required"
    store.retire(item.item_id, actor="dana", human=True, clearance=ACME_INTERNAL)
    assert store.list(ACME_INTERNAL) == []
    assert [i.item_id for i in store.list(ACME_INTERNAL, include_retired=True)] == [item.item_id]
    assert store.get(item.item_id, ACME_INTERNAL).status == "retired"
    assert [e["action"] for e in store.history(item.item_id, ACME_INTERNAL)] == ["create", "retire"]


def test_a_stale_expected_version_is_refused(store):
    item = lesson(store)
    store.revise(item.item_id, actor="kofi", clearance=ACME_INTERNAL, body="second")
    assert code_of(lambda: store.revise(item.item_id, actor="kofi", clearance=ACME_INTERNAL,
                                        expected_version=1, body="third")) == "work_conflict"
    assert store.revise(item.item_id, actor="kofi", clearance=ACME_INTERNAL,
                        expected_version=2, body="third").version == 3


# --------------------------------------------------------------------------- #
# reads behind the clearance
# --------------------------------------------------------------------------- #


def refusal(call) -> dict:
    with pytest.raises(Refusal) as exc:
        call()
    return {"code": exc.value.code, "status": exc.value.http_status,
            "message": exc.value.message, "remediation": exc.value.remediation}


def test_not_cleared_and_not_there_are_the_same_answer(store):
    item = lesson(store, clearance=ACME_PRIVATE, level="confidential")
    walled = refusal(lambda: store.get(item.item_id, ACME_INTERNAL))
    missing = refusal(lambda: store.get("mem-000000000000", ACME_INTERNAL))
    missing["message"] = missing["message"].replace("mem-000000000000", item.item_id)
    assert walled == missing
    assert refusal(lambda: store.history(item.item_id, NOBODY))["code"] == "memory_unknown"
    assert store.list(ACME_INTERNAL) == []
    assert [i.item_id for i in store.list(ACME_PRIVATE)] == [item.item_id]


def test_levels_compare_only_within_one_owner(store):
    item = lesson(store)
    assert store.list(Clearance({"globex": "private"}, LEVELS)) == []
    assert store.list(Clearance({"acme": "all"}, LEVELS)) == []
    assert [i.item_id for i in store.list(Clearance({"acme": "confidential"}, LEVELS))] == [item.item_id]


def test_jsonl_carries_an_unreadable_line_through_a_write(tmp_path):
    store = MemoryStore(tmp_path / "memory.jsonl", hub_id="hub-a")
    lesson(store)
    with (tmp_path / "memory.jsonl").open("ab") as handle:
        handle.write(b"{not json\n")
    lesson(store, title="another")
    assert b"{not json" in (tmp_path / "memory.jsonl").read_bytes()
    assert len(store.list(ACME_INTERNAL)) == 2


# --------------------------------------------------------------------------- #
# clearance
# --------------------------------------------------------------------------- #


def test_clearance_narrows_and_never_widens():
    hub = parse("acme:confidential,globex:internal", LEVELS)
    agent = parse({"acme": "private", "initech": "private"}, LEVELS)
    effective = hub.narrow(agent)
    assert effective.grants == {"acme": "confidential"}
    assert not effective.allows("globex", "all"), "only one side granted globex"
    assert not effective.allows("acme", "private")
    assert effective.allows("public", "private")


def test_an_undeclared_hub_clearance_holds_only_public(monkeypatch):
    monkeypatch.delenv("AGENTCO_HUB_CLEARANCE", raising=False)
    walls = build(None, {"kofi": {"acme": "private"}}, LEVELS)
    assert walls.effective("kofi").grants == {}
    assert walls.effective("nobody").grants == {}


def test_a_clearance_naming_an_unknown_level_is_refused():
    with pytest.raises(ValueError):
        parse({"acme": "secret"}, LEVELS)


# --------------------------------------------------------------------------- #
# over HTTP
# --------------------------------------------------------------------------- #

KEYS = {"dana": "dana-secret", "kofi": "kofi-secret", "ana": "ana-secret"}


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="dana",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
        memory_store=str(tmp_path / "memory.jsonl"), hub_id="hub-a", humans=["dana"],
        hub_clearance={"acme": "confidential", "globex": "internal"},
        clearances={"dana": {"acme": "confidential"}, "kofi": {"acme": "internal"},
                    "ana": {"globex": "internal"}},
        memory_levels=LEVELS, memory_enabled=True,
    )
    return TestClient(app)


def signed(method, path, actor, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    ts = str(int(time.time()))
    return {"X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
            "X-AgentCo-Signature": auth.sign(KEYS[actor], method, path, ts, raw),
            "Content-Type": "application/json"}


def post(client, path, actor, body):
    return client.post(path, content=json.dumps(body), headers=signed("POST", path, actor, body))


def get(client, path, actor, query=""):
    return client.get(f"{path}{query}", headers=signed("GET", path, actor))


def http_lesson(client, actor="kofi", level="internal", owner="acme"):
    response = post(client, "/memory", actor, {
        "label": {"owner": owner, "level": level}, "layer": "shared",
        "title": "Retry the export once", "body": "A stalled export resumes after one retry.",
        "provenance": PROV})
    return response


def test_http_round_trip_and_audit(client):
    created = http_lesson(client)
    assert created.status_code == 200, created.text
    item = created.json()["item"]
    assert item["created_by"] == "kofi" and item["authority"] == "hub-a"
    assert get(client, f"/memory/{item['item_id']}", "kofi").json()["item"] == item
    revised = post(client, f"/memory/{item['item_id']}/revise", "kofi",
                   {"body": "Retry once, then escalate.", "expected_version": 1})
    assert revised.json()["item"]["version"] == 2
    history = get(client, f"/memory/{item['item_id']}/history", "kofi").json()["history"]
    assert [h["action"] for h in history] == ["create", "revise"]


def test_http_hub_and_actor_clearance_both_apply(client):
    assert http_lesson(client, actor="kofi", level="confidential").json()["code"] == "clearance_insufficient"
    assert http_lesson(client, actor="dana", level="private").json()["code"] == "clearance_insufficient"
    walled = http_lesson(client, actor="dana", level="confidential").json()["item"]
    assert get(client, "/memory", "kofi").json()["count"] == 0
    assert get(client, "/memory", "dana").json()["count"] == 1
    assert get(client, "/memory", "ana").json()["count"] == 0
    hidden = get(client, f"/memory/{walled['item_id']}", "kofi")
    missing = get(client, "/memory/mem-000000000000", "kofi")
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json()["code"] == missing.json()["code"] == "memory_unknown"


def test_http_pin_and_retire_are_human_only(client):
    item = http_lesson(client).json()["item"]
    assert post(client, f"/memory/{item['item_id']}/pin", "kofi",
                {"review_by": "2027-01-01"}).json()["code"] == "human_required"
    pinned = post(client, f"/memory/{item['item_id']}/pin", "dana", {"review_by": "2027-01-01"})
    assert pinned.json()["item"]["pinned"] is True
    assert post(client, f"/memory/{item['item_id']}/retire", "kofi", {}).json()["code"] == "human_required"
    assert post(client, f"/memory/{item['item_id']}/retire", "dana", {}).json()["item"]["status"] == "retired"
    assert get(client, "/memory", "kofi").json()["count"] == 0


def test_http_create_refuses_fields_the_hub_assigns(client):
    response = post(client, "/memory", "kofi", {
        "label": {"owner": "acme", "level": "internal"}, "layer": "shared", "title": "t", "body": "b",
        "provenance": PROV, "authority": "hub-z"})
    # The route is strict about unknown keys (agentco/wire.py), so this is refused
    # before the store sees it; the store would refuse it too (memory_invalid).
    assert response.status_code == 422 and response.json()["code"] == "unknown_field"
