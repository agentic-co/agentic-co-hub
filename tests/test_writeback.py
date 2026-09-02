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

import json

import pytest

from agentco import db, events, writeback


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.delenv(writeback.WRITEBACK_URL_ENV_VAR, raising=False)
    monkeypatch.setenv(writeback.CURSOR_ENV_VAR, str(tmp_path / "writeback.cursor"))
    monkeypatch.setenv(writeback.DEADLETTER_ENV_VAR, str(tmp_path / "writeback.deadletter.jsonl"))
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


# --------------------------------------------------------------------------- #
# FIX-L3.7 — a poison event must not block the pass, or erase the cursor
# --------------------------------------------------------------------------- #


def test_a_permanent_failure_is_dead_lettered_and_the_pass_continues(conn):
    """Reproduced against the reviewer's exact shape: three `WorkParked`
    events, the writer failing on the second with a permanent (4xx) status —
    a deleted ticket, the paradigm case. Before this fix `run` advanced its
    cursor only after the whole loop finished, and a writer exception
    propagated before that write ever happened, so three successive runs each
    delivered `['acme/1', 'acme/1', 'acme/1']`: the first notice forever,
    the cursor never moving off `None`, and nothing after the poison event
    ever sent.

    A 4xx is the caller's fault forever, not the network's fault today — the
    ticket is not coming back by retrying the same POST — so it is recorded
    and skipped rather than retried indefinitely.
    """
    parked(conn, source_id="acme/1")
    parked(conn, source_id="acme/2")
    parked(conn, source_id="acme/3")

    calls: list[str] = []

    def flaky(notice):
        calls.append(notice["sourceId"])
        if notice["sourceId"] == "acme/2":
            raise writeback.WritebackFailed(400, "ticket acme/2 was deleted")

    writeback.register_writer("flaky", flaky)
    result = writeback.run(conn, via="flaky")

    assert calls == ["acme/1", "acme/2", "acme/3"]
    assert [n["sourceId"] for n in result["notices"]] == ["acme/1", "acme/3"]
    assert result["sent"] == 2
    assert result["deadlettered"] == 1

    deadletter_path = writeback._deadletter_path()
    lines = [json.loads(line) for line in deadletter_path.read_text("utf-8").splitlines()]
    assert len(lines) == 1
    assert lines[0]["notice"]["sourceId"] == "acme/2"
    assert lines[0]["status"] == 400

    # Idempotent: the whole pass, including the dead-lettered event, is not
    # replayed just because it happened to fail once.
    calls.clear()
    assert writeback.run(conn, via="flaky")["sent"] == 0
    assert calls == []


def test_a_transient_failure_persists_what_already_landed_then_stops(conn):
    """Pins the ordering `write_cursor` depends on, which is exactly the
    ordering the reviewer's report found broken: the cursor may only advance
    up to the LAST event actually handled before a transient failure, never
    past the event that failed.

    A mutant that advanced `cursor` to the current event's seq BEFORE calling
    the writer, instead of after it succeeds, would have the except branch
    persist the poison event's own cursor rather than the one before it — the
    retry below would then never re-attempt `acme/2`. This test dies against
    that mutant because the second run would deliver nothing.
    """
    parked(conn, source_id="acme/1")
    parked(conn, source_id="acme/2")

    calls: list[str] = []

    def flaky(notice):
        calls.append(notice["sourceId"])
        if notice["sourceId"] == "acme/2":
            raise writeback.WritebackFailed(503, "the endpoint is down")

    writeback.register_writer("flaky", flaky)
    with pytest.raises(writeback.WritebackFailed):
        writeback.run(conn, via="flaky")
    assert calls == ["acme/1", "acme/2"]
    assert writeback.read_cursor() is not None, "acme/1 was handled and must not be lost"

    seen = collector()
    assert writeback.run(conn, via="test")["sent"] == 1
    assert [n["sourceId"] for n in seen] == ["acme/2"], (
        "a retry must redeliver only the event that actually failed"
    )


# --------------------------------------------------------------------------- #
# F4 — a 4xx is not uniformly permanent
# --------------------------------------------------------------------------- #


def test_401_403_408_429_are_retried_not_dead_lettered(conn):
    """`_is_permanent_failure` used to treat every 4xx as the caller's fault
    forever, and four of them are the opposite case. 408 (timeout) and 429
    (rate limited) are the network's fault today, not the ticket's fault
    ever; 401 and 403 are the operator's to fix — rotate the token, grant
    access — and then they want the SAME notice replayed, not one silently
    dropped into a dead-letter file next to tickets that no longer exist.

    A mutant that made `_is_permanent_failure` "all 4xx are permanent" dies
    here: each of these would be dead-lettered and the pass would report
    success instead of raising with the notice still owed a retry.
    """
    for status in (401, 403, 408, 429):
        parked(conn, source_id=f"acme/{status}")

        def flaky(notice, status=status):
            raise writeback.WritebackFailed(status, f"http {status}")

        writeback.register_writer("flaky", flaky)
        with pytest.raises(writeback.WritebackFailed):
            writeback.run(conn, via="flaky")
        assert not writeback._deadletter_path().exists(), (
            f"{status} was dead-lettered; it must be retried instead"
        )

        seen = collector()
        assert writeback.run(conn, via="test")["sent"] == 1
        assert seen[0]["sourceId"] == f"acme/{status}"


def test_other_4xx_statuses_besides_400_are_also_permanent(conn):
    """Pins the other side of `TRANSIENT_4XX`. A mutant that narrowed
    `_is_permanent_failure` to recognise only 400 — the status every other
    test in this file happens to use — would treat 404 as transient and stop
    the pass instead of dead-lettering it and moving on.
    """
    parked(conn, source_id="acme/1")
    parked(conn, source_id="acme/2")

    def flaky(notice):
        if notice["sourceId"] == "acme/1":
            raise writeback.WritebackFailed(404, "ticket acme/1 was never valid")

    writeback.register_writer("flaky", flaky)
    result = writeback.run(conn, via="flaky")

    assert result["sent"] == 1 and result["deadlettered"] == 1
    assert [n["sourceId"] for n in result["notices"]] == ["acme/2"]


# --------------------------------------------------------------------------- #
# F5 — an unwritable dead-letter path must not re-open the poison loop
# --------------------------------------------------------------------------- #


def test_an_unwritable_deadletter_path_persists_the_cursor_and_raises(conn, tmp_path, monkeypatch):
    """The dead-letter write is what turns "permanent failure" into "handled".
    Before this fix, IT raising inside the except block happened before the
    cursor was ever persisted, so the run died, the cursor never moved past
    the event before the poison one, and — because the write is retried
    unconditionally on the next run — the same crash repeated forever with no
    record of which notice caused it. This falls through to the transient
    contract instead: the cursor still advances only to the event before the
    poison one, so a fixed path retries it rather than skipping it, and the
    raised error names both the notice and the path that refused it.
    """
    parked(conn, source_id="acme/1")
    parked(conn, source_id="acme/2")

    trap = tmp_path / "deadletter-is-a-directory"
    trap.mkdir()
    monkeypatch.setenv(writeback.DEADLETTER_ENV_VAR, str(trap))

    def flaky(notice):
        if notice["sourceId"] == "acme/2":
            raise writeback.WritebackFailed(400, "ticket acme/2 was deleted")

    writeback.register_writer("flaky", flaky)
    with pytest.raises(writeback.WritebackFailed) as caught:
        writeback.run(conn, via="flaky")
    assert "acme/2" in str(caught.value)
    assert writeback.read_cursor() is not None, "acme/1 was handled and must not be lost"

    # Fix the path and retry: the poison event is redelivered, not skipped —
    # the transient contract, which is exactly what this fell through to.
    monkeypatch.setenv(writeback.DEADLETTER_ENV_VAR, str(tmp_path / "writeback.deadletter.jsonl"))
    seen = collector()
    assert writeback.run(conn, via="test")["sent"] == 1
    assert [n["sourceId"] for n in seen] == ["acme/2"]


def test_deadletter_records_are_appended_across_separate_runs(conn):
    """A mutant that opened the dead-letter file with `"w"` instead of `"a"`
    would lose every record but the last. Two permanent failures across two
    separate runs must both survive in the file — the docstring's promise
    that it is "appended, never overwritten", pinned rather than taken on
    faith.
    """
    parked(conn, source_id="acme/1")

    def always_permanent(notice):
        raise writeback.WritebackFailed(404, "not found")

    writeback.register_writer("flaky", always_permanent)
    writeback.run(conn, via="flaky")

    parked(conn, source_id="acme/2")
    writeback.run(conn, via="flaky")

    deadletter_path = writeback._deadletter_path()
    lines = [json.loads(line) for line in deadletter_path.read_text("utf-8").splitlines()]
    assert [line["notice"]["sourceId"] for line in lines] == ["acme/1", "acme/2"]
