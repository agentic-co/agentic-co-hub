"""Change-feed retention: a floor, and `cursor_expired` instead of a silent gap
(owner decision 10, 2026-10-18; R6).

Pruning deletes a contiguous prefix of the feed and records the oldest seq it
keeps. A reader whose cursor is at or past `floor - 1` resumes with nothing lost
and nothing replayed; a reader behind it is refused, loudly, with the floor and
the remedy. Synthetic events only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from agentco import db, events
from agentco.errors import Refusal

OLD, NEW, CUT = "2026-01-01T00:00:00+00:00", "2026-10-18T00:00:00+00:00", "2026-06-01T00:00:00+00:00"


@pytest.fixture()
def conn(backend, tmp_path, request):
    if backend == "postgres":
        return db.connect(request.getfixturevalue("_pg_dsn"))
    return db.connect(tmp_path / "registry.sqlite3")


def append(conn, n, at):
    return [events.append(conn, kind="ScopeClaimed", actor="worker-a", payload={"n": i}, occurred_at=at)["seq"]
            for i in range(n)]


def test_prune_records_an_exact_floor_and_deletes_only_below_it(conn):
    old = append(conn, 4, OLD)
    new = append(conn, 3, NEW)
    assert events.retention_floor(conn) == 1, "nothing pruned yet"
    dry = events.prune(conn, before=CUT, dry_run=True)
    assert dry["retainedFrom"] == new[0] and dry["deleted"] == 4
    assert len(events.read(conn, limit=500)["events"]) == 7, "a dry run deletes nothing"
    done = events.prune(conn, before=CUT)
    assert done["retainedFrom"] == new[0] and events.retention_floor(conn) == new[0]
    assert [e["seq"] for e in events.read(conn, limit=500)["events"]] == new
    assert events.prune(conn, before=CUT)["deleted"] == 0, "pruning again changes nothing"
    assert old


def test_a_backdated_event_keeps_everything_after_it(conn):
    first = append(conn, 2, OLD)
    inside = append(conn, 1, NEW)
    after = append(conn, 2, OLD)  # backdated, but after the in-window event
    events.prune(conn, before=CUT)
    assert events.retention_floor(conn) == inside[0]
    assert [e["seq"] for e in events.read(conn, limit=500)["events"]] == [*inside, *after]
    assert first


def test_prune_then_resume_no_loss_no_replay_and_an_expired_cursor_is_refused(conn):
    seqs = append(conn, 6, OLD) + append(conn, 4, NEW)
    caught_up = events.read(conn, limit=7)["nextCursor"]  # this reader has consumed 7 events
    behind = events.encode_cursor(seqs[1])                 # this one only 2
    events.prune(conn, before=CUT)                         # floor = seqs[6]
    floor = events.retention_floor(conn)
    assert floor == seqs[6]

    resumed = events.read(conn, since=caught_up, limit=500)
    assert [e["seq"] for e in resumed["events"]] == seqs[7:], "nothing lost, nothing replayed"
    assert resumed["retainedFrom"] == floor

    at_floor = events.read(conn, since=events.encode_cursor(floor - 1), limit=500)
    assert [e["seq"] for e in at_floor["events"]] == seqs[6:], "a cursor at floor - 1 is still whole"

    with pytest.raises(Refusal) as exc:
        events.read(conn, since=behind)
    assert exc.value.code == "cursor_expired" and exc.value.http_status == 410
    assert f"seq {floor}" in exc.value.message and f"since={events.encode_cursor(floor - 1)}" in exc.value.remediation

    fresh = events.read(conn, limit=500)
    assert [e["seq"] for e in fresh["events"]] == seqs[6:], "a fresh read begins at the floor"


def test_the_floor_never_moves_backwards(conn):
    append(conn, 3, OLD)
    append(conn, 2, NEW)
    events.prune(conn, before=CUT)
    floor = events.retention_floor(conn)
    events.prune(conn, before="2000-01-01T00:00:00+00:00")
    assert events.retention_floor(conn) == floor


def test_the_pulse_reports_the_floor_and_prunes_only_when_declared(conn, monkeypatch, tmp_path):
    from datetime import datetime, timezone

    from agentco import pulse
    from agentco.work import Queue

    append(conn, 3, OLD)
    append(conn, 2, NEW)
    queue = Queue(tmp_path / "work.jsonl")
    now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    monkeypatch.delenv(events.RETENTION_ENV_VAR, raising=False)
    facts, _ = pulse.housekeeping(queue, conn, now=now, apply=True)
    assert facts["eventRetention"]["state"] == "not-configured"
    assert pulse.check_plane(conn, now=now)[0]["eventsRetainedFrom"] == 1
    monkeypatch.setenv(events.RETENTION_ENV_VAR, "60d")
    dry, _ = pulse.housekeeping(queue, conn, now=now, apply=False)
    assert dry["eventRetention"]["state"] == "dry-run" and dry["eventRetention"]["deleted"] == 3
    applied, _ = pulse.housekeeping(queue, conn, now=now, apply=True)
    assert applied["eventRetention"]["state"] == "pruned"
    assert pulse.check_plane(conn, now=now)[0]["eventsRetainedFrom"] == events.retention_floor(conn) > 1


@pytest.mark.parametrize("raw,days", [("", None), ("90", 90), ("30d", 30)])
def test_the_window_is_configuration(raw, days):
    assert events.retention_from_env(raw) == days


# --------------------------------------------------------------------------- #
# over the real routes, the MCP events tool, a real socket and the CLI
# --------------------------------------------------------------------------- #

from tests.test_mcp_remote import KEYS, LoopbackRegistry  # noqa: E402


def make_app(tmp_path):
    from agentco.app import create_app

    return create_app(db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator",
                      work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"))


def test_http_and_the_mcp_events_tool_refuse_an_expired_cursor(tmp_path):
    from fastapi.testclient import TestClient

    from agentco.mcp_server import _LocalBackend
    from agentco.publish import RegistryError

    app = make_app(tmp_path)
    conn = app.state.conn
    old = append(conn, 3, OLD)
    append(conn, 2, NEW)
    events.prune(conn, before=CUT)
    reg = LoopbackRegistry("macbook", TestClient(app))
    with pytest.raises(RegistryError) as exc:
        reg.events(since=events.encode_cursor(old[0]))
    assert exc.value.status == 410 and exc.value.payload["code"] == "cursor_expired"
    assert reg.events()["retainedFrom"] == events.retention_floor(conn)
    with pytest.raises(Refusal) as local:
        _LocalBackend(conn, None, None, "macbook", str(tmp_path / "api.sqlite3")).events(
            since=events.encode_cursor(old[0]))
    assert local.value.code == "cursor_expired"


def test_live_wire_resume_across_a_prune(tmp_path):
    import socket
    import threading

    import uvicorn

    from agentco.publish import Registry, RegistryError

    app = make_app(tmp_path)
    conn = app.state.conn
    seqs = append(conn, 4, OLD) + append(conn, 3, NEW)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        reader = Registry("macbook", KEYS["macbook"], f"http://127.0.0.1:{port}")
        cursor = reader.events(limit=5)["nextCursor"]
        events.prune(conn, before=CUT)
        resumed = reader.events(since=cursor)
        assert [e["seq"] for e in resumed["events"]] == seqs[5:]
        with pytest.raises(RegistryError) as exc:
            reader.events(since=events.encode_cursor(seqs[0]))
        assert exc.value.status == 410 and exc.value.payload["code"] == "cursor_expired"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_the_cli_is_dry_by_default(tmp_path):
    path = tmp_path / "cli.sqlite3"
    conn = db.connect(path)
    append(conn, 3, OLD)
    append(conn, 1, NEW)
    env = {**os.environ, "AGENTCO_REGISTRY_DB": str(path)}
    dry = subprocess.run([sys.executable, "-m", "agentco", "prune-events", "--days", "30"], capture_output=True,
                         text=True, env=env)
    assert dry.returncode == 0 and "dry run" in dry.stderr
    assert events.retention_floor(db.connect(path)) == 1
    applied = subprocess.run([sys.executable, "-m", "agentco", "prune-events", "--days", "30", "--apply"],
                             capture_output=True, text=True, env=env)
    assert applied.returncode == 0, applied.stderr
    assert events.retention_floor(db.connect(path)) == 4
    assert json.dumps(events.read(db.connect(path))["events"][0]["seq"]) == "4"
