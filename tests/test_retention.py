"""Retention removes poll noise and cannot move the number that decides the product.

`calls` had no retention at all. That was survivable while the only traffic was
somebody publishing a scope claim, and stops being survivable the moment a fleet
polls — measured, five hundred actors at a fifteen-second cadence write 3.2
million rows a day, essentially all of them `events`.

The risk of fixing it is worse than the problem: stage-1d is computed FROM this
table, and a retention policy that quietly changed the adoption gate would be a
regression nobody would notice until the gate said something false about whether
anyone is using this. So the assertion that matters is not "rows went away" — it
is **the instruments answer identically before and after**, computed both times
and compared.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentco import db, metrics


@pytest.fixture()
def conn(tmp_path):
    return db.connect(tmp_path / "registry.sqlite3")


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def call(conn, *, actor, verb, at, status="accepted", latency=10.0):
    conn.execute(
        "INSERT INTO calls(verb, actor, status, code, latency_ms, at) VALUES (?, ?, ?, ?, ?, ?)",
        (verb, actor, status, None, latency, at.astimezone(timezone.utc).isoformat()),
    )


def a_fleet_with_history(conn):
    """Four weeks of two people publishing, buried in poll traffic."""
    for week in range(4):
        day = NOW - timedelta(weeks=week + 1)
        for actor in ("dana", "kofi"):
            call(conn, actor=actor, verb="claim_scope", at=day)
            call(conn, actor=actor, verb="snapshot", at=day + timedelta(hours=1))
        # the noise: many polls per publishing act, which is the real ratio
        for i in range(40):
            call(conn, actor=f"agent-{i % 7}", verb="events",
                 at=day + timedelta(minutes=i), latency=5.0 + i)


def test_pruning_does_not_move_the_adoption_gate(conn):
    a_fleet_with_history(conn)

    before_gate = metrics.gate1_status(conn, operator="operator", now=NOW)
    before_publishers = metrics.weekly_active_publishers(conn, exclude=("operator",), now=NOW)

    report = metrics.roll_up_and_prune_calls(conn, before=NOW - timedelta(days=1), now=NOW)
    assert report["deleted"] > 0, "the prune removed nothing, so this proves nothing"

    assert metrics.gate1_status(conn, operator="operator", now=NOW) == before_gate
    assert metrics.weekly_active_publishers(conn, exclude=("operator",), now=NOW) == before_publishers


def test_every_publishing_row_survives_at_any_age(conn):
    """A publish is rare and deliberate, and it is the whole of what the gate
    reads. Keeping them forever costs almost nothing."""
    call(conn, actor="dana", verb="claim_scope", at=NOW - timedelta(days=4000))
    call(conn, actor="dana", verb="events", at=NOW - timedelta(days=4000))

    metrics.roll_up_and_prune_calls(conn, before=NOW - timedelta(days=1), now=NOW)

    rows = conn.execute("SELECT verb FROM calls").fetchall()
    assert [r["verb"] for r in rows] == ["claim_scope"]


def test_what_is_deleted_is_summarised_first(conn):
    for i in range(10):
        call(conn, actor="agent-1", verb="events", at=NOW - timedelta(days=5), latency=float(i))

    metrics.roll_up_and_prune_calls(conn, before=NOW - timedelta(days=1), now=NOW)

    row = conn.execute("SELECT * FROM calls_daily").fetchone()
    assert row["actor"] == "agent-1"
    assert row["verb"] == "events"
    assert row["n"] == 10
    assert row["latency_sum"] == pytest.approx(sum(range(10)))
    assert row["latency_max"] == pytest.approx(9.0)


def test_running_it_twice_does_not_double_count(conn):
    """A prune interrupted half way and re-run must not inflate the summary it
    already wrote — the numbers would be wrong in the direction of looking busy."""
    for i in range(5):
        call(conn, actor="agent-1", verb="events", at=NOW - timedelta(days=5))

    metrics.roll_up_and_prune_calls(conn, before=NOW - timedelta(days=1), now=NOW)
    first = conn.execute("SELECT n FROM calls_daily").fetchone()["n"]
    metrics.roll_up_and_prune_calls(conn, before=NOW - timedelta(days=1), now=NOW)
    second = conn.execute("SELECT n FROM calls_daily").fetchone()["n"]

    assert first == second == 5


def test_recent_traffic_is_left_alone(conn):
    """The window is the point: percentiles stay answerable inside it."""
    call(conn, actor="agent-1", verb="events", at=NOW - timedelta(hours=2))
    call(conn, actor="agent-1", verb="events", at=NOW - timedelta(days=40))

    metrics.roll_up_and_prune_calls(conn, before=NOW - timedelta(days=30), now=NOW)

    assert conn.execute("SELECT COUNT(*) AS n FROM calls").fetchone()["n"] == 1
    assert metrics.verb_latency(conn), "the recent window stopped answering"


def test_liveness_survives_as_a_summary(conn):
    """`pulse` asks when each actor was last seen. After a prune the raw row is
    gone, and the answer has to still exist somewhere."""
    call(conn, actor="agent-9", verb="events", at=NOW - timedelta(days=5))
    metrics.roll_up_and_prune_calls(conn, before=NOW - timedelta(days=1), now=NOW)

    row = conn.execute("SELECT last_at FROM calls_daily WHERE actor = 'agent-9'").fetchone()
    assert row is not None and row["last_at"].startswith("2026-09-16")
