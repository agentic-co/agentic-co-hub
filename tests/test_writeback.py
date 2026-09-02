"""The narrow, opt-in exception to "AgentCo never writes to your system of record".

The promise is what makes adoption safe: nothing here can damage the tool you
already trust, so if it disappears every tool falls back to what it does today.
This module is the one path that reaches back, and every test below is about a
boundary on it rather than about the feature working.

The decision to build it was the principal's (2026-09-02), taken over the stated
objection that an absolute promise with one exception is no longer absolute. The
answer to that objection is not "it is only a small write" — it is that the
write leaves this process entirely: the built-in path POSTs a notice to a URL
the operator controls, and the code that touches Azure DevOps is theirs, holding
their credential.
"""

from __future__ import annotations

import pytest

from agentco import db, events, writeback


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.delenv(writeback.WRITEBACK_URL_ENV_VAR, raising=False)
    monkeypatch.setenv(writeback.CURSOR_ENV_VAR, str(tmp_path / "writeback.cursor"))
    writeback._WRITERS.clear()
    return db.connect(tmp_path / "registry.sqlite3")


def parked(conn, *, source="ado", source_id="acme/91060", title="fix the retry path"):
    return events.append(
        conn,
        kind="WorkParked",
        actor=events.PLANE_ACTOR,
        payload={
            "itemId": "w-1",
            "title": title,
            "gateKind": "human",
            "check": "the release owner signs off",
            "assignedTo": "dana",
            "dueAt": "2026-09-09T00:00:00+00:00",
            "sourceKey": f"ext|{source}|{source_id}" if source_id else None,
            "source": source,
            "sourceId": source_id,
            "sourceUrl": "https://dev.example.com/acme/_workitems/edit/91060",
        },
    )


def collector():
    seen: list[dict] = []
    writeback.register_writer("test", seen.append)
    return seen


# --------------------------------------------------------------------------- #
# Off, and saying so, is the default state
# --------------------------------------------------------------------------- #


def test_it_is_off_until_somebody_configures_it(conn):
    """Not an error. A scheduled job that failed loudly because an OPTIONAL
    feature was switched off is a job whose owner disables the alerting, and
    then misses the failure that mattered."""
    parked(conn)
    result = writeback.run(conn)
    assert result["state"] == "not-configured"
    assert result["sent"] == 0
    assert "off" in result["detail"]


def test_an_unconfigured_run_does_not_move_the_cursor(conn):
    """Otherwise switching it on later would skip everything that happened while
    it was off — silently, and exactly once, which is the worst frequency for a
    bug like that."""
    parked(conn)
    writeback.run(conn)
    assert writeback.read_cursor() is None
    seen = collector()
    assert writeback.run(conn, via="test")["sent"] == 1
    assert len(seen) == 1


# --------------------------------------------------------------------------- #
# What travels, and what deliberately does not
# --------------------------------------------------------------------------- #


def test_a_notice_carries_the_origin_and_nothing_that_could_change_it(conn):
    """One shape, and no path from here to closing a ticket or editing a field.

    A connector that wants to do more is writing its own integration — this one
    hands over a notice, and the narrowness is the reason the exception to the
    no-writes promise is defensible at all.
    """
    parked(conn)
    seen = collector()
    writeback.run(conn, via="test")

    [notice] = seen
    assert notice["sourceId"] == "acme/91060"
    assert notice["kind"] == "WorkParked"
    assert notice["assignedTo"] == "dana"
    assert set(notice) == {
        "kind", "itemId", "source", "sourceId", "title", "check",
        "assignedTo", "dueAt", "waitedSeconds", "occurredAt",
    }
    for forbidden in ("status", "state", "close", "transition", "fields"):
        assert forbidden not in notice


def test_work_with_no_origin_is_skipped_rather_than_errored(conn):
    """An item filed locally has nowhere to write back to. That is the correct
    answer, not a delivery failure."""
    parked(conn, source=None, source_id=None)
    seen = collector()
    result = writeback.run(conn, via="test")
    assert result["sent"] == 0 and result["skipped"] == 1
    assert seen == []


def test_only_gate_events_travel(conn):
    """Adding a kind here is the decision to notify somebody's ticket about
    something new, and it should look like a decision in a diff."""
    events.append(conn, kind="ScopeClaimed", actor="dana",
                  payload={"sourceId": "acme/1", "repo": "acme/app"})
    parked(conn)
    seen = collector()
    assert writeback.run(conn, via="test")["sent"] == 1
    assert seen[0]["kind"] == "WorkParked"


def test_an_escalation_travels_with_how_long_it_waited(conn):
    events.append(conn, kind="GateEscalated", actor=events.PLANE_ACTOR, payload={
        "itemId": "w-1", "title": "fix it", "to": "dana", "waitedSeconds": 604800,
        "declaredSeconds": 604800, "check": "sign-off",
        "sourceKey": "ext|ado|acme/91060", "source": "ado", "sourceId": "acme/91060",
        "sourceUrl": None,
    })
    seen = collector()
    writeback.run(conn, via="test")
    assert seen[0]["waitedSeconds"] == 604800
    assert seen[0]["assignedTo"] == "dana"


# --------------------------------------------------------------------------- #
# The cursor, which is the whole of the idempotency
# --------------------------------------------------------------------------- #


def test_the_same_gate_is_not_re_notified_on_every_run(conn):
    """A pass re-reading from the beginning puts the same comment on the same
    ticket every five minutes, which is how a channel becomes one people filter."""
    parked(conn)
    seen = collector()
    assert writeback.run(conn, via="test")["sent"] == 1
    for _ in range(3):
        assert writeback.run(conn, via="test")["sent"] == 0
    assert len(seen) == 1


def test_a_new_event_after_the_cursor_is_delivered(conn):
    parked(conn)
    seen = collector()
    writeback.run(conn, via="test")
    parked(conn, source_id="acme/91061")
    assert writeback.run(conn, via="test")["sent"] == 1
    assert [n["sourceId"] for n in seen] == ["acme/91060", "acme/91061"]


def test_a_failed_delivery_is_retried_rather_than_watermarked_past(conn):
    """The cursor advances over events that were HANDLED. A watermark that moved
    regardless would lose exactly the notices that failed to arrive."""
    parked(conn)

    def explode(notice):
        raise writeback.WritebackFailed(503, "the endpoint is down")

    writeback.register_writer("flaky", explode)
    with pytest.raises(writeback.WritebackFailed):
        writeback.run(conn, via="flaky")
    assert writeback.read_cursor() is None

    seen = collector()
    assert writeback.run(conn, via="test")["sent"] == 1
    assert len(seen) == 1


def test_a_dry_run_delivers_nothing_and_leaves_the_cursor_alone(conn):
    parked(conn)
    seen = collector()
    result = writeback.run(conn, via="test", dry_run=True)
    assert result["state"] == "dry-run" and result["sent"] == 1
    assert seen == []
    assert writeback.read_cursor() is None
