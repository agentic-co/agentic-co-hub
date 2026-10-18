"""The plane supports more than one published contract, and loses nothing at any.

asop-spec 0.5.0 (spec v3.5) added the chaining fields `outcomes`, `outputs`,
`handoffs`, `on_escalate` and `requires` to the ASOP record; 0.6.0 (v3.6)
added `entry_gates` and `rework`, and put the rest of v3.6 inside fields that
already existed (hand-off `preflight`, `subject_from.order_by/concurrency/
approved_subset`, gate `on_timeout: proceed_on_default`, `cannot_evaluate_exit`,
`min_cases`, `human_approved.binds_digest`).

Before this file, the SQL backend asserted at import that every ASOP field had a
column, and 0.5.0's five new fields had none — so the plane did not import at
all on any contract newer than the one it was pinned to. The JSONL backend
imported, and then `revise` silently dropped every chaining field, because the
carry-forward payload listed the fields it knew by name.

What is asserted here, on every backend:

  * a v3.5 record and a v3.6 record round-trip through create, get and revise
    byte-for-byte in their chaining fields (skipped where the installed
    contract cannot express them — the matrix in docs/asop.md says which);
  * a record that uses a field the installed contract does NOT model is
    quarantined, carried through a rewrite verbatim, and makes `revise` refuse
    — never read without the field and written back without it.
"""

from __future__ import annotations

import json

import pytest

from agentco import migrations, sqlstore
from agentco.sop import CHAIN_FIELDS, SopError, SopLibrary

DET = {"kind": "deterministic", "check": "make check"}

V35 = ("outcomes", "outputs", "handoffs", "on_escalate", "requires")
V36 = ("entry_gates", "rework")

needs_v35 = pytest.mark.skipif(
    not set(V35) <= set(CHAIN_FIELDS), reason="installed asop-spec predates v3.5 (0.5.0)"
)
needs_v36 = pytest.mark.skipif(
    not set(V35 + V36) <= set(CHAIN_FIELDS), reason="installed asop-spec predates v3.6 (0.6.0)"
)


def _body(*, inputs=("source",), **chain) -> dict:
    body = {
        "purpose": "turn a source document into a plan",
        "inputs": [{"name": n} for n in inputs],
        "roles": {"worker": {"kind": "agent"}},
        "steps": [{"name": "plan", "role": "worker", "purpose": "write the plan", "gate": DET}],
    }
    body.update(chain)
    return body


def v35_body() -> dict:
    """Every v3.5 field, each in a shape the spec's own tests accept."""
    return _body(
        outcomes=["PLANNED", "NOTHING"],
        outputs=[{"name": "tasks"}],
        handoffs=[{
            "id": "build-each",
            "to": {"asop_id": "build", "version": 1},
            "on_outcomes": ["PLANNED"],
            "cardinality": "many",
            "subject_from": {"output": "tasks", "array": "tasks", "id_field": "id"},
            "inputs": {"task": "$subject", "source": "$run.inputs.source"},
        }],
        on_escalate={"to": {"asop_id": "decide", "versions": ">=1"},
                     "inputs": {"question_kind": "escalation"}},
        requires=[{"kind": "human_approved", "artefact": "plan-approval"}],
    )


def v36_body() -> dict:
    """The v3.5 record plus every v3.6 addition, including the ones that live
    inside existing fields — those are the ones a column list cannot see."""
    body = v35_body()
    body["entry_gates"] = [{
        "id": "source-ready",
        "over": ["source"],
        "gate": {"kind": "deterministic", "check": "check-source --final", "cannot_evaluate_exit": 3},
    }]
    handoff = body["handoffs"][0]
    handoff["preflight"] = True
    handoff["subject_from"].update(order_by="depends_on", concurrency=2, approved_subset=True)
    body["requires"] = [{"kind": "human_approved", "artefact": "plan-approval", "binds_digest": True}]
    body["rework"] = [{"id": "redo-plan", "to": {"step": 1}, "on_step_failure": 2,
                       "max_rounds": 2, "consumes_attempt": False}]
    body["roles"]["owner"] = {"kind": "human"}
    body["steps"].append({
        "name": "confirm-retention",
        "role": "owner",
        "purpose": "a person picks the retention period",
        "gate": {
            "kind": "human", "check": "which retention period applies?", "verifier": "owner-a",
            "max_park_seconds": 3600, "on_timeout": "proceed_on_default",
            "default": {"option": "keep the current period",
                        "consequence": "the purge job is built for it",
                        "audience": "product owner"},
        },
    })
    return body


def _chain(asop) -> dict:
    return {f: getattr(asop, f) for f in CHAIN_FIELDS}


def _round_trip(library, body: dict, fields: tuple[str, ...]):
    created = library.create("plan the work", **body)
    stored = library.get(created.asop_id, 1)
    assert _chain(stored) == _chain(created)
    for f in fields:
        assert getattr(stored, f), f"{f} did not persist"

    # The carry-forward: a revision that touches only `purpose` must keep
    # every chaining field, and the steps' v3.6 gate fields with them.
    revised = library.revise(created.asop_id, purpose="a sharper purpose", author_kind="human")
    assert revised.version == 2
    assert _chain(revised) == _chain(created)
    assert [s.gate for s in revised.steps] == [s.gate for s in created.steps]
    again = library.get(created.asop_id, 2)
    assert _chain(again) == _chain(created)
    assert [s.gate for s in again.steps] == [s.gate for s in created.steps]
    return again


@needs_v35
def test_a_v35_record_round_trips(library):
    _round_trip(library, v35_body(), V35)


@needs_v36
def test_a_v36_record_round_trips(library):
    stored = _round_trip(library, v36_body(), V35 + V36)
    handoff = stored.handoffs[0]
    assert handoff["preflight"] is True
    assert handoff["subject_from"]["order_by"] == "depends_on"
    assert handoff["subject_from"]["concurrency"] == 2
    assert handoff["subject_from"]["approved_subset"] is True
    assert stored.requires[0]["binds_digest"] is True
    assert stored.entry_gates[0]["gate"]["cannot_evaluate_exit"] == 3
    question = stored.steps[1].gate
    assert question["on_timeout"] == "proceed_on_default"
    assert question["default"]["option"] == "keep the current period"


def test_a_record_with_no_chaining_reads_back_with_none(library):
    """A v3.4 record — what every row written before migration 0011 is."""
    created = library.create("standalone", **_body())
    stored = library.get(created.asop_id, 1)
    for f in CHAIN_FIELDS:
        assert getattr(stored, f) in (None, []), f


# --------------------------------------------------------------------------- #
# A field the installed contract cannot model
# --------------------------------------------------------------------------- #


def test_sqlite_quarantines_a_row_that_uses_an_unmodelled_column(tmp_path, monkeypatch):
    """Simulated on any installed contract by narrowing what this build models.

    The real case is a plane on 0.5.0 reading a row a 0.6.0 plane wrote with an
    entry gate. Reading it without the gate would run the procedure without
    its precondition; the next authoring call rewrites the table and would
    persist that. So: quarantined, preserved verbatim, and `revise` refuses."""
    path = tmp_path / "plane.sqlite3"
    library = sqlstore.SqlSopLibrary(path)
    kept = library.create("keep me", **_body())
    other = library.create("written by a newer plane", **_body())
    conn = sqlstore.connect(path)
    conn.execute(
        "UPDATE asops SET entry_gates = ? WHERE asop_id = ?",
        (json.dumps([{"id": "source-ready", "over": ["source"], "gate": DET}]), other.asop_id),
    )

    monkeypatch.setattr(sqlstore, "_ASOP_UNMODELLED", ("entry_gates",))
    monkeypatch.setattr(sqlstore, "ASOP_COLUMNS",
                        tuple(c for c in sqlstore.ASOP_COLUMNS if c != "entry_gates"))
    narrowed = sqlstore.SqlSopLibrary(path)

    assert narrowed.get(other.asop_id, 1) is None
    assert narrowed.get(kept.asop_id, 1) is not None
    assert len(narrowed.quarantined) == 1
    with pytest.raises(SopError, match="could not be parsed"):
        narrowed.revise(kept.asop_id, purpose="anything")

    # A rewrite (any authoring call) carries the quarantined row through with
    # the value this build does not model.
    narrowed.activate(kept.asop_id, 1, author_kind="human")
    row = conn.execute("SELECT entry_gates FROM asops WHERE asop_id = ?", (other.asop_id,)).fetchone()
    assert json.loads(row[0])[0]["id"] == "source-ready"


def test_jsonl_quarantines_a_line_that_uses_an_unmodelled_field(tmp_path):
    """`ASOP.from_json` drops keys it does not declare; the store must not."""
    path = tmp_path / "sops.jsonl"
    library = SopLibrary(path)
    kept = library.create("keep me", **_body())
    line = json.loads(path.read_text().splitlines()[0])
    line.update(asop_id="asop-from-the-future", a_field_from_a_later_contract=[{"id": "x"}])
    future = json.dumps(line, sort_keys=True)
    with path.open("a") as handle:
        handle.write(future + "\n")

    assert library.get("asop-from-the-future", 1) is None
    assert library.get(kept.asop_id, 1) is not None
    with pytest.raises(SopError, match="could not be parsed"):
        library.revise(kept.asop_id, purpose="anything")
    library.activate(kept.asop_id, 1, author_kind="human")
    assert future in path.read_text().splitlines()


def test_migration_0011_gives_existing_rows_empty_chaining(tmp_path):
    """A table migrated from 0010 reads every old row back as unchained."""
    import sqlite3

    path = tmp_path / "old.sqlite3"
    raw = sqlite3.connect(str(path), isolation_level=None)
    raw.row_factory = sqlite3.Row
    migrations.apply(raw, [m for m in migrations.MIGRATIONS if m.version <= 10])
    raw.execute(
        'INSERT INTO asops (asop_id, version, title, status, steps, created_at) '
        "VALUES ('asop-old', 1, 'old', 'draft', ?, '2026-10-01T00:00:00Z')",
        (json.dumps([{"step": 1, "name": "do", "role": "worker", "gate": DET}]),),
    )
    assert 11 in migrations.apply(raw)
    raw.close()

    stored = sqlstore.SqlSopLibrary(path).get("asop-old", 1)
    assert stored is not None
    for f in CHAIN_FIELDS:
        assert getattr(stored, f) in (None, []), f
