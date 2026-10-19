"""The hub's generated id (ADR 0008 §3.1), beside the configured label.

The label (`AGENTCO_HUB_ID`) stays a name for people and keeps every meaning it
had, shared memory's `authority` above all. The id is 128 generated bits kept in
the registry database's `meta` table, the same on SQLite and Postgres. Synthetic
data only.
"""

from __future__ import annotations

import re
import threading

import pytest

from agentco import cli, db, hubid
from agentco.app import create_app
from agentco.clearance import Clearance
from agentco.errors import Refusal
from agentco.memory import MemoryStore, SqlMemoryStore

HEX32 = re.compile(r"^[0-9a-f]{32}$")


@pytest.fixture()
def target(backend, tmp_path, request):
    """A registry database target on each backend: a path, or a Postgres DSN."""
    if backend == "postgres":
        return request.getfixturevalue("_pg_dsn")
    return tmp_path / "registry.sqlite3"


def test_the_id_is_generated_once_as_32_lowercase_hex(target):
    conn = db.connect(target)
    assert hubid.read(conn) is None, "nothing is generated until a hub (or hub-id) asks"
    first = hubid.ensure(conn)
    assert HEX32.match(first) and hubid.read(conn) == first
    assert hubid.ensure(conn) == first


def test_the_id_is_stable_across_reconnects(target):
    first = hubid.ensure(db.connect(target))
    for _ in range(3):
        assert hubid.ensure(db.connect(target)) == first


def test_two_databases_get_different_ids(backend, tmp_path, request):
    if backend == "postgres":
        a = db.connect(request.getfixturevalue("_pg_dsn"))
        b = db.connect(tmp_path / "other.sqlite3")
    else:
        a, b = db.connect(tmp_path / "a.sqlite3"), db.connect(tmp_path / "b.sqlite3")
    assert hubid.ensure(a) != hubid.ensure(b)


def test_a_concurrent_first_start_yields_one_id(target):
    db.connect(target)  # the schema exists; the race is for the id
    seen, barrier = [], threading.Barrier(6)

    def start():
        conn = db.connect(target)
        barrier.wait()
        seen.append(hubid.ensure(conn))

    threads = [threading.Thread(target=start) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(seen) == 6 and len(set(seen)) == 1 and HEX32.match(seen[0])
    assert hubid.read(db.connect(target)) == seen[0]


def test_a_stored_value_that_is_not_an_id_is_refused_not_overwritten(target):
    conn = db.connect(target)
    with conn:
        conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (hubid.META_KEY, "local"))
    with pytest.raises(Refusal) as exc:
        hubid.ensure(conn)
    assert exc.value.code == "hub_id_invalid"
    assert hubid.read(conn) == "local", "an id someone may have pinned is never replaced"


def test_require_bound_refuses_until_an_id_exists_and_while_the_label_is_local(target):
    conn = db.connect(target)
    with pytest.raises(Refusal) as exc:
        hubid.require_bound(conn, "hub-a")
    assert exc.value.code == "hub_id_unbound" and "agentco hub-id" in exc.value.remediation
    uid = hubid.ensure(conn)
    assert hubid.require_bound(conn, "hub-a") == uid
    assert hubid.require_bound(conn) == uid
    with pytest.raises(Refusal) as exc:
        hubid.require_bound(conn, "local")
    assert exc.value.code == "hub_id_unbound" and "AGENTCO_HUB_ID" in exc.value.remediation


def test_create_app_generates_it_and_a_restart_keeps_it(tmp_path):
    path = str(tmp_path / "registry.sqlite3")
    first = create_app(db_path=path, keys={"dana": "k"}, operator="dana").state.hub_uid
    again = create_app(db_path=path, keys={"dana": "k"}, operator="dana").state.hub_uid
    assert HEX32.match(first) and first == again
    assert create_app(db_path=str(tmp_path / "x.sqlite3"), keys={"dana": "k"}, operator="dana").state.hub_uid != first


def test_the_cli_prints_the_id_and_the_label(tmp_path, capsys, monkeypatch):
    path = str(tmp_path / "registry.sqlite3")
    monkeypatch.setenv("AGENTCO_HUB_ID", "hub-a")
    assert cli.main(["hub-id", "--db", path, "--json"]) == 0
    import json

    out = json.loads(capsys.readouterr().out)
    assert HEX32.match(out["hubId"]) and out["label"] == "hub-a"
    assert cli.main(["hub-id", "--db", path]) == 0
    text = capsys.readouterr().out
    assert out["hubId"] in text and "hub-a" in text


def test_the_pulse_facts_carry_the_id(tmp_path):
    from datetime import datetime, timezone

    from agentco import pulse

    conn = db.connect(tmp_path / "registry.sqlite3")
    facts, _ = pulse.check_plane(conn, now=datetime.now(timezone.utc))
    assert facts["hubId"] is None
    uid = hubid.ensure(conn)
    facts, _ = pulse.check_plane(conn, now=datetime.now(timezone.utc))
    assert facts["hubId"] == uid


# --------------------------------------------------------------------------- #
# the label keeps its meaning: memory authority does not move
# --------------------------------------------------------------------------- #

LEVELS = ("all", "internal", "confidential", "private")
ACME = Clearance({"acme": "confidential"}, LEVELS)
PROV = {"kind": "run", "ref": "run:w-0001"}


def remember(store, title):
    return store.create(actor="kofi", clearance=ACME, owner="acme", level="internal", layer="shared",
                        title=title, body="A stalled export resumes after one retry.", provenance=PROV)


@pytest.mark.parametrize("label", ["local", "hub-a"])
@pytest.mark.parametrize("sql", [False, True])
def test_memory_authority_is_the_label_and_survives_id_generation_and_restart(tmp_path, monkeypatch, label, sql):
    monkeypatch.delenv("AGENTCO_HUB_ID", raising=False)
    registry = str(tmp_path / "agentco.sqlite3")
    memory_path = registry if sql else str(tmp_path / "memory.jsonl")
    cls = SqlMemoryStore if sql else MemoryStore
    before = remember(cls(memory_path, hub_id=label), "written before an id existed")
    assert before.authority == label

    # the hub starts (generating its id), is restarted, and starts again
    for _ in range(2):
        app = create_app(db_path=registry, keys={"dana": "k"}, operator="dana", hub_id=label,
                         memory_enabled=True, memory_store=memory_path)
        assert HEX32.match(app.state.hub_uid) and app.state.hub_uid != label

    restarted = cls(memory_path, hub_id=label)
    assert restarted.get(before.item_id, clearance=ACME).authority == label, "an existing item keeps its label"
    after = remember(restarted, "written after the id existed")
    assert after.authority == label and after.authority != app.state.hub_uid, "new items get the label"
