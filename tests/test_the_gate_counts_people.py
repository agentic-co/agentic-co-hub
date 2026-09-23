"""GATE-1 counts people. One person's three agents are one publisher.

The gate — two publishers other than the operator, four consecutive weeks — is
the single instrument that says whether anybody but its author uses this. It
counted distinct authenticated ACTORS, and at two or three agents per person
that is not the unit it means: one adopter with a laptop key and two agent keys
clears the bar alone, every week, and the report shows three names the reader
has no way to recognise as one person.

`docs/known-issues.md` recorded this under "not defects, stated because they
read like them", with the note that "no mechanism here can tell". Owner-binding
is that mechanism. This file is the part of it that makes the number true.

Same defect as the letter-case one, one level up — and the case fix is the
precedent for the shape of this one: the ambiguity is settled where identity is
CONFIGURED (the key table refuses a self-owning entry and a two-level chain),
and only then folded where it is counted.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agentco import auth, db, metrics

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def conn(tmp_path):
    return db.connect(tmp_path / "gate.sqlite3")


def published(conn, actor: str, *, weeks_ago: int) -> None:
    metrics.record_call(
        conn,
        verb="snapshot",
        actor=actor,
        status="accepted",
        latency_ms=1.0,
        at=NOW - timedelta(weeks=weeks_ago),
    )


def five_weeks(conn, *actors: str) -> None:
    """Enough history to clear a four-week streak, the current week excluded."""
    for weeks_ago in range(1, 6):
        for actor in actors:
            published(conn, actor, weeks_ago=weeks_ago)


# --------------------------------------------------------------------------- #
# the count
# --------------------------------------------------------------------------- #


def test_one_persons_agents_are_one_publisher(conn):
    """Three keys, one person, one publisher — so the gate is NOT met.

    This is the whole defect in one assertion. Before parties, `dana`,
    `dana-codex-01` and `dana-claude-02` were three identities publishing every
    week for five weeks, which is "≥2 publishers other than the operator" twice
    over, and the programme would have been told its adoption gate had passed
    on a sample of one person.
    """
    five_weeks(conn, "dana", "dana-codex-01", "dana-claude-02")
    parties = {"dana-codex-01": "dana", "dana-claude-02": "dana"}

    status = metrics.gate1_status(conn, operator="operator", now=NOW, parties=parties)

    assert status["met"] is False, (
        f"one person's three agents cleared a two-publisher gate: {status['byWeek']}"
    )
    assert all(week == ["dana"] for week in status["byWeek"].values() if week)


def test_two_people_with_agents_each_still_clear_the_gate(conn):
    """The fix must not make the gate unreachable — two PEOPLE pass it.

    Worth its own test because a count that never passes and a count that
    always passes are equally useless, and the first is the easier mistake to
    make when the change is "count fewer things".
    """
    five_weeks(conn, "dana", "dana-codex-01", "ravi-claude-01")
    parties = {"dana-codex-01": "dana", "ravi-claude-01": "ravi"}

    status = metrics.gate1_status(conn, operator="operator", now=NOW, parties=parties)

    assert status["met"] is True
    assert status["currentStreakWeeks"] >= 4
    for week, publishers in status["byWeek"].items():
        if publishers:
            assert publishers == ["dana", "ravi"], week


def test_an_undeclared_actor_is_its_own_party(conn):
    """No ownership declared anywhere → exactly the numbers this reported before.

    Every existing deployment has a flat key table and must read identically
    after this change, or the gate's history becomes incomparable across the
    upgrade that was supposed to make it honest.
    """
    five_weeks(conn, "dana", "ravi")

    with_parties = metrics.gate1_status(conn, operator="operator", now=NOW, parties={})
    assert with_parties["met"] is True
    assert with_parties["byWeek"] == metrics.gate1_status(
        conn, operator="operator", now=NOW, parties={"someone-else": "unrelated"}
    )["byWeek"]


def test_an_owner_name_folds_case_like_every_other_name(conn):
    """`Dana` owning one agent and `dana` owning another is one party.

    The case fix settled actors. An owner is a free-text name in the same
    table and nothing refuses two capitalisations of it, so the fold has to
    reach it too — otherwise the defect simply moves from the actor column to
    the owner column.
    """
    five_weeks(conn, "dana-codex-01", "dana-claude-02")
    parties = {"dana-codex-01": "Dana", "dana-claude-02": "dana"}

    status = metrics.gate1_status(conn, operator="operator", now=NOW, parties=parties)

    assert status["met"] is False, f"two spellings of one owner counted twice: {status['byWeek']}"


# --------------------------------------------------------------------------- #
# the subtraction
# --------------------------------------------------------------------------- #


def test_the_operators_own_agents_are_excluded_with_him(conn):
    """The gate's one subtraction, undone by the tools of the person subtracted.

    Excluding the operator by actor name removed `operator` and left
    `operator-codex-01` publishing — an agent of the very identity the gate
    exists to discount, counted as a publisher other than the operator. At
    two or three agents per person, the operator is the person MOST likely to
    have several actors, so this was not a corner case but the first one.
    """
    five_weeks(conn, "operator", "operator-codex-01", "operator-claude-02")
    parties = {"operator-codex-01": "operator", "operator-claude-02": "operator"}

    status = metrics.gate1_status(conn, operator="operator", now=NOW, parties=parties)

    assert status["met"] is False
    assert all(week == [] for week in status["byWeek"].values()), status["byWeek"]


def test_naming_one_of_a_persons_actors_excludes_the_person(conn):
    """`operator=dana-codex-01` excludes Dana, not just that key.

    The operator is configured once, by hand, and whoever writes it down will
    write whichever of their names comes to mind. Resolving it to a party
    first means the configuration says who is excluded rather than which key
    of theirs is.
    """
    five_weeks(conn, "dana", "dana-codex-01", "ravi")
    parties = {"dana-codex-01": "dana"}

    status = metrics.gate1_status(conn, operator="dana-codex-01", now=NOW, parties=parties)

    for week, publishers in status["byWeek"].items():
        if publishers:
            assert publishers == ["ravi"], week
    assert status["met"] is False


# --------------------------------------------------------------------------- #
# where the parties come from when nobody passes them
# --------------------------------------------------------------------------- #


def test_the_default_reads_the_deployments_own_key_table(conn, tmp_path, monkeypatch):
    """`parties=None` is the production path, and it must need no argument.

    `cli.py` and `app.py` call this with an operator and nothing else. If the
    ownership had to be threaded through by hand, the deployment that forgot
    would go on counting identities and say nothing — which is the failure
    this whole change is about, with a new place to hide.
    """
    table = tmp_path / "keys.json"
    table.write_text(
        json.dumps(
            {
                "dana": "s1",
                "dana-codex-01": {"secret": "s2", "owner": "dana", "label": "codex"},
                "dana-claude-02": {"secret": "s3", "owner": "dana", "label": "claude"},
            }
        )
    )
    table.chmod(0o600)
    monkeypatch.setenv(auth.KEYS_ENV_VAR, str(table))
    auth._KEY_CACHE.clear()

    five_weeks(conn, "dana", "dana-codex-01", "dana-claude-02")

    status = metrics.gate1_status(conn, operator="operator", now=NOW)

    assert status["met"] is False, f"the key table's owners were not consulted: {status['byWeek']}"
    assert all(week == ["dana"] for week in status["byWeek"].values() if week)


def test_no_key_table_declared_counts_actors(conn, monkeypatch):
    """No table, no ownership, no change. The conformance world runs this way."""
    monkeypatch.delenv(auth.KEYS_ENV_VAR, raising=False)
    auth._KEY_CACHE.clear()

    five_weeks(conn, "dana", "ravi")

    assert metrics.gate1_status(conn, operator="operator", now=NOW)["met"] is True


# --------------------------------------------------------------------------- #
# the report says what it counted
# --------------------------------------------------------------------------- #


def test_the_report_states_the_unit_it_counted(conn):
    """A gate whose terms are argued after the fact is not a gate.

    The criterion sentence and the definitions travel with the verdict for
    exactly this reason, so both have to change with the count — a report
    that still says "identities" over a number of people is a report that
    will be read wrong by whoever it convinces.
    """
    five_weeks(conn, "dana")

    status = metrics.gate1_status(conn, operator="operator", now=NOW, parties={})

    assert "identities" not in status["criterion"]
    assert "PEOPLE" in status["criterion"]
    assert "party" in status["definitions"]
    assert status["definitions"]["publisher"].startswith("a party")
