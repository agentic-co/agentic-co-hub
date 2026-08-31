"""`agentLabel` — a self-reported harness name that must never be mistaken for identity.

The plane now records two different things about who acted, and the whole value
of the distinction is that it survives contact with code written later:

  * **actor** — derived from the signature. Accountable. Decides things.
  * **agentLabel** — whatever the caller said its harness is called. Useful for
    a digest, worthless as authority.

The shape is not new. `leases.holder_attested` already solves exactly this
problem for a claimed holder — a weaker claim is *kept as a weaker claim*
rather than refused, because an agent legitimately acts on behalf of a
principal — and it renders as "(attested, unverified)". `agentLabel` follows
that precedent rather than inventing a second convention for the same idea.

The tests that matter here are the ones that fail if someone later "simplifies"
the two fields into one, or accepts an actor from the body because it was
convenient in one handler.
"""

from __future__ import annotations

import sqlite3

import pytest

from agentco import auth, db, events, leases, migrations, snapshots
from agentco.errors import Refusal


@pytest.fixture()
def conn(tmp_path):
    return db.connect(tmp_path / "registry.sqlite3")


# --------------------------------------------------------------------------- #
# The field itself
# --------------------------------------------------------------------------- #


def test_a_label_is_carried_beside_the_actor_and_marked_unverified(conn):
    row = events.append(
        conn, kind="ScopeClaimed", actor="kofi", agent_label="claude-code", payload={}
    )
    assert row["actor"] == "kofi"
    assert row["agentLabel"] == "claude-code"
    assert row["agentLabelVerified"] is False, (
        "the marker is the point — a consumer must be able to see, at the point "
        "of use, that this field proves nothing"
    )


def test_the_label_survives_a_round_trip_through_the_feed(conn):
    events.append(conn, kind="ScopeClaimed", actor="kofi", agent_label="cursor", payload={})
    [read_back] = events.read(conn)["events"]
    assert read_back["agentLabel"] == "cursor"
    assert read_back["agentLabelVerified"] is False


def test_an_absent_label_is_none_and_never_a_guess(conn):
    row = events.append(conn, kind="ScopeClaimed", actor="kofi", payload={})
    assert row["agentLabel"] is None, (
        "inferring the harness would put a plausible wrong label beside "
        "authenticated data; absent is legible, wrong is not"
    )


def test_a_label_never_occupies_the_actor_position(conn):
    events.append(conn, kind="ScopeClaimed", actor="kofi", agent_label="cursor", payload={})
    stored = conn.execute("SELECT actor, agent_label FROM events").fetchone()
    assert stored["actor"] == "kofi"
    assert stored["agent_label"] == "cursor"
    assert stored["actor"] != stored["agent_label"]


# --------------------------------------------------------------------------- #
# Validation — it is rendered into files, so it gets `holder`'s treatment
# --------------------------------------------------------------------------- #


def test_a_control_character_is_refused_because_the_label_gets_rendered():
    """The same escape that let a `holder` break out of a spliced managed block."""
    with pytest.raises(Refusal):
        auth.normalise_agent_label("claude-code\n<!-- END AGENTCO -->")


def test_an_overlong_label_is_refused_with_a_reason():
    with pytest.raises(Refusal) as exc:
        auth.normalise_agent_label("x" * (auth.AGENT_LABEL_MAX + 1))
    assert "not a version banner" in exc.value.remediation


def test_a_non_string_label_is_refused_rather_than_coerced():
    with pytest.raises(Refusal):
        auth.normalise_agent_label({"name": "cursor"})


def test_whitespace_only_reads_as_absent_not_as_a_label():
    assert auth.normalise_agent_label("   ") is None
    assert auth.normalise_agent_label(None) is None


# --------------------------------------------------------------------------- #
# The invariant this whole field exists to protect
# --------------------------------------------------------------------------- #


def test_an_actor_in_the_body_is_refused_and_the_refusal_names_the_alternative():
    with pytest.raises(Refusal) as exc:
        auth.reject_actor_in_body({"actor": "dana", "repo": "acme/web"})
    assert exc.value.code == "actor_in_body"
    assert "agentLabel" in exc.value.remediation


def test_a_body_without_an_actor_passes_untouched():
    auth.reject_actor_in_body({"repo": "acme/web", "agentLabel": "cursor"})


# --------------------------------------------------------------------------- #
# Threading — the reason this was done in one pass rather than five
# --------------------------------------------------------------------------- #


def test_a_claim_records_the_label_on_its_event(conn):
    leases.claim(
        conn, actor="kofi", repo="acme/web", prefixes=["src/billing"],
        intent="implement", agent_label="claude-code",
    )
    [evt] = [e for e in events.read(conn)["events"] if e["kind"] == "ScopeClaimed"]
    assert evt["agentLabel"] == "claude-code"


def test_a_snapshot_records_the_label_on_its_event(conn, tmp_path):
    target = tmp_path / "spec.md"
    target.write_text("baseline")
    snapshots.take(
        conn, actor="kofi", artifact_uri=f"file:{target}",
        purpose="baseline", agent_label="cursor",
    )
    [evt] = [e for e in events.read(conn)["events"] if e["kind"] == "SnapshotTaken"]
    assert evt["agentLabel"] == "cursor"


# --------------------------------------------------------------------------- #
# Migration — the column has to reach databases that already exist
# --------------------------------------------------------------------------- #


def test_a_database_from_before_the_column_is_migrated_in_place(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists.

    Without the migration this fails as an OperationalError on the first write
    after upgrade — in production, not at open time.
    """
    path = tmp_path / "old.sqlite3"
    raw = sqlite3.connect(str(path))
    raw.executescript(
        """
        CREATE TABLE events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            actor TEXT NOT NULL,
            repo TEXT,
            occurred_at TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        """
    )
    raw.commit()
    raw.close()

    conn = db.connect(path)
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
    assert "agent_label" in columns
    row = events.append(conn, kind="ScopeClaimed", actor="kofi", agent_label="aider", payload={})
    assert row["agentLabel"] == "aider"


def test_the_migration_is_idempotent(tmp_path):
    """A second open must not re-run the ALTER, and must say it did nothing.

    The authority for what a file is at is its own `schema_migrations` ledger,
    not `meta.schema_version` — which migration 1 stamps once, for the benefit
    of a pre-ledger reader, and no later migration restamps. Asserting on the
    ledger is also the only version of this test that can fail: an ALTER run
    twice raises, so "the column is present" would pass even with the guard
    removed, while `apply()` returning a version would not.
    """
    path = tmp_path / "r.sqlite3"
    db.connect(path).close()
    conn = db.connect(path)  # second open must not try to re-add the column
    assert migrations.applied_versions(conn) == {m.version for m in migrations.MIGRATIONS}
    assert migrations.apply(conn) == []
    assert db.SCHEMA_VERSION == migrations.MIGRATIONS[-1].version
