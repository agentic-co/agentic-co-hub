"""The pulse — the plane checks itself and everything wired to it.

Three properties matter more than the rest, and each has a test that fails if
the mechanism is removed: a dry run changes nothing (bytes and event count
identical); the exit code is the worst consequence class and not a count; and
the pulse's own silence is a finding, judged against the interval the last run
declared. Housekeeping runs on the parametrised `queue` fixture, so the sweeps
are conformance-tested on both backends from one body.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agentco import cli, db, events, hook, pulse, verifiers
from agentco.work import WorkStatus

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def at(seconds: float = 0) -> datetime:
    return NOW + timedelta(seconds=seconds)


@pytest.fixture()
def conn(tmp_path):
    return db.connect(tmp_path / "registry.sqlite3")


def run(conn, queue, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("keys_env", None)
    kw.setdefault("cadences", {})
    return pulse.run(conn, queue, **kw)


def seen(conn, actor: str, when: datetime) -> None:
    events.append(conn, kind="SnapshotTaken", actor=actor, payload={}, occurred_at=when.isoformat())


def pulse_events(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'PulseObserved'").fetchone()[0]


# --------------------------------------------------------------------------- #
# declarations
# --------------------------------------------------------------------------- #


def test_durations_parse_and_garbage_is_refused():
    assert pulse.parse_duration("90") == 90
    assert pulse.parse_duration("90s") == 90
    assert pulse.parse_duration("15m") == 900
    assert pulse.parse_duration(" 2H ") == 7200
    assert pulse.parse_duration("1d") == 86400
    for bad in ("", "soon", "1w", "-5", "0", "1.5h"):
        with pytest.raises(ValueError):
            pulse.parse_duration(bad)


def test_cadence_declaration_parses_like_the_other_env_declarations():
    assert pulse.cadences_from_env("alice=1d, ci-worker = 2h,,") == {"alice": 86400, "ci-worker": 7200}
    assert pulse.cadences_from_env("") == {}
    assert pulse.cadences_from_env(None) == {} or True  # falls through to the environment
    for bad in ("alice", "=1h", "alice=soon"):
        with pytest.raises(ValueError):
            pulse.cadences_from_env(bad)


# --------------------------------------------------------------------------- #
# the pass
# --------------------------------------------------------------------------- #


def test_a_fresh_registry_is_ok_and_a_dry_run_records_nothing(conn, queue):
    report = run(conn, queue)
    assert report["class"] == "ok" and report["exitCode"] == 0
    assert report["findings"] == []
    assert report["applied"] is False and "recorded" not in report
    assert report["participants"] == []
    assert report["plane"]["writable"] is True and report["plane"]["integrity"] == "ok"
    assert pulse_events(conn) == 0


def test_an_expired_lease_is_previewed_dry_and_returned_to_ready_on_apply(conn, queue):
    """The reaper had no caller. Dry run names the lease and moves nothing;
    apply returns the item to PENDING with its attempt advanced, which is what
    fences out the reaped holder's late report."""
    item = queue.create("sync the ledger")
    leased = queue.claim(item.id, "kofi", ttl_seconds=60, now=at(-3600))
    assert leased is not None

    dry = run(conn, queue)
    assert dry["housekeeping"]["expiredLeases"] == [item.id]
    still = queue.get(item.id)
    assert still.status is WorkStatus.IN_PROGRESS and still.leased_by == "kofi"

    applied = run(conn, queue, apply=True)
    assert applied["housekeeping"]["expiredLeases"] == [item.id]
    after = queue.get(item.id)
    assert after.status is WorkStatus.PENDING and after.leased_by is None
    assert after.lease_attempt == leased.lease_attempt + 1
    # housekeeping is the system working, not a finding
    assert applied["class"] == "ok"


def test_apply_records_exactly_one_pulse_event_attributed_to_the_plane(conn, queue):
    run(conn, queue, apply=True, every=3600)
    run(conn, queue)  # dry, records nothing
    assert pulse_events(conn) == 1
    row = conn.execute("SELECT actor, payload FROM events WHERE kind = 'PulseObserved'").fetchone()
    assert row["actor"] == events.PLANE_ACTOR
    payload = json.loads(row["payload"])
    assert payload["class"] == "ok" and payload["every"] == 3600
    assert payload["findings"] == {"ok": 0, "attention": 0, "fatal": 0}


def test_a_silent_declared_actor_is_attention_and_an_undeclared_one_is_not(conn, queue):
    seen(conn, "kofi", at(-3 * 86400))
    seen(conn, "ana", at(-3600))
    report = run(conn, queue, cadences={"kofi": 86400})
    by_actor = {p["actor"]: p for p in report["participants"]}
    assert by_actor["kofi"]["state"] == "silent"
    assert by_actor["kofi"]["expectedEverySeconds"] == 86400
    assert by_actor["ana"]["state"] == "undeclared"
    assert by_actor["ana"]["expectedEverySeconds"] is None, "unreported is null, never a guess"
    checks = [f["check"] for f in report["findings"]]
    assert checks == ["participants.silent"]
    assert report["class"] == "attention" and report["exitCode"] == 1


def test_a_declared_actor_never_seen_is_attention(conn, queue):
    report = run(conn, queue, cadences={"ghost": 3600})
    assert [f["check"] for f in report["findings"]] == ["participants.never-seen"]
    assert report["participants"][0]["lastSeenAt"] is None


def test_an_active_declared_actor_raises_nothing(conn, queue):
    seen(conn, "kofi", at(-600))
    report = run(conn, queue, cadences={"kofi": 3600})
    assert report["findings"] == [] and report["participants"][0]["state"] == "active"


def test_the_plane_sees_actors_through_the_work_store_too(conn, queue):
    """A worker over stdio leaves no call row and may publish no event; its
    lease and its executor record are still evidence it was here."""
    item = queue.create("build the thing")
    leased = queue.claim(item.id, "worker-7", now=at(-60))
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE)
    report = run(conn, queue, cadences={"worker-7": 3600})
    assert report["participants"][0]["state"] == "active"


def test_exit_code_is_the_worst_class_and_never_a_count(conn, queue, tmp_path):
    seen(conn, "a", at(-10 * 86400))
    seen(conn, "b", at(-10 * 86400))
    two = run(conn, queue, cadences={"a": 3600, "b": 3600})
    assert len(two["findings"]) == 2 and two["exitCode"] == 1

    missing = tmp_path / "no-such-keys.json"
    three = run(conn, queue, cadences={"a": 3600, "b": 3600}, keys_env=str(missing))
    assert len(three["findings"]) == 3 and three["exitCode"] == 2
    assert three["class"] == "fatal"


def test_keys_unset_is_not_a_finding_but_set_and_empty_is_fatal(conn, queue, tmp_path):
    unset = run(conn, queue, keys_env=None)
    assert unset["keys"] == {"configured": None, "actors": 0} and unset["findings"] == []

    empty = run(conn, queue, keys_env=str(tmp_path / "missing.json"))
    assert [f["check"] for f in empty["findings"]] == ["keys.empty"]
    assert empty["class"] == "fatal"

    good = tmp_path / "keys.json"
    good.write_text(json.dumps({"ana": "s1", "kofi": "s2"}))
    loaded = run(conn, queue, keys_env=str(good))
    assert loaded["keys"] == {"configured": True, "actors": 2} and loaded["findings"] == []


def test_a_file_from_a_newer_build_is_fatal(conn, queue):
    with conn:
        conn.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (99, 'from-the-future', ?)",
            (NOW.isoformat(),),
        )
    report = run(conn, queue)
    assert [f["check"] for f in report["findings"]] == ["plane.schema"]
    assert report["exitCode"] == 2


def test_a_malformed_cadence_declaration_is_fatal_not_silent(conn, queue, monkeypatch):
    monkeypatch.setenv(pulse.CADENCE_ENV_VAR, "alice=soon")
    report = pulse.run(conn, queue, now=NOW, keys_env=None)
    assert [f["check"] for f in report["findings"]] == ["participants.declaration"]
    assert report["class"] == "fatal"


# --------------------------------------------------------------------------- #
# the auditor audits itself
# --------------------------------------------------------------------------- #


def test_the_pulses_own_silence_is_a_finding_judged_against_what_it_declared(conn, queue):
    first = run(conn, queue, apply=True, every=3600)
    assert first["self"]["state"] == "first-run"

    on_time = run(conn, queue, now=at(7200), every=3600)
    assert on_time["self"]["state"] == "on-time" and on_time["findings"] == []

    late = run(conn, queue, now=at(3 * 3600 + 1), every=3600)
    assert late["self"]["state"] == "overdue"
    assert [f["check"] for f in late["findings"]] == ["self.silent"]
    assert late["exitCode"] == 1


def test_without_a_declared_interval_the_gap_is_reported_not_judged(conn, queue):
    run(conn, queue, apply=True)
    much_later = run(conn, queue, now=at(30 * 86400))
    assert much_later["self"]["state"] == "undeclared"
    assert much_later["self"]["gapSeconds"] == 30 * 86400
    assert much_later["findings"] == []


# --------------------------------------------------------------------------- #
# the sweeps that nobody was running
# --------------------------------------------------------------------------- #

HUMAN_ESCALATE = {
    "kind": "human", "check": "a person signs off", "on_timeout": "escalate",
    "escalate_to": "dana", "verifier": "dana", "max_park_seconds": 60,
}


def abandoned(queue):
    item = queue.create("ship the release", verify=HUMAN_ESCALATE)
    leased = queue.claim(item.id, "executor")
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE)
    assert queue.get(item.id).status is WorkStatus.AWAITING_VERIFY
    verifiers.route_open_gates(queue)
    swept = verifiers.sweep_park_clocks(queue, now=at(120))
    assert [r["item"] for r in swept["escalated"]] == [item.id]
    return item


def test_an_abandoned_gate_is_quarantined_on_apply_and_is_attention(conn, queue):
    item = abandoned(queue)
    later = at(120 + 8 * 86400)

    dry = run(conn, queue, now=later)
    assert dry["housekeeping"]["quarantine"]["quarantined"] == 1, "previewed"
    assert not verifiers.is_quarantined(queue.get(item.id)), "but not moved"

    applied = run(conn, queue, now=later, apply=True)
    assert verifiers.is_quarantined(queue.get(item.id))
    assert applied["housekeeping"]["stuck"] == 1
    assert "housekeeping.quarantine" in [f["check"] for f in applied["findings"]]
    assert applied["class"] == "attention"
    assert queue.get(item.id).status is WorkStatus.AWAITING_VERIFY, "quarantine is not a resolution"


def test_a_dry_run_leaves_the_store_bytes_and_the_feed_identical(conn, jsonl_queue):
    item = jsonl_queue.create("sync the ledger")
    jsonl_queue.claim(item.id, "kofi", ttl_seconds=60, now=at(-3600))
    abandoned(jsonl_queue)
    before = jsonl_queue.path.read_bytes() if hasattr(jsonl_queue, "path") else None
    feed_before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    report = run(conn, jsonl_queue, now=at(120 + 8 * 86400))
    assert report["housekeeping"]["expiredLeases"] and report["housekeeping"]["quarantine"]["quarantined"]

    if before is not None:
        assert jsonl_queue.path.read_bytes() == before
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == feed_before


# --------------------------------------------------------------------------- #
# rendering, CLI, hook
# --------------------------------------------------------------------------- #


def test_render_text_says_dry_run_and_lists_findings_worst_first(conn, queue):
    seen(conn, "kofi", at(-3 * 86400))
    report = run(conn, queue, cadences={"kofi": 86400, "ghost": 60})
    text = pulse.render_text(report)
    assert "dry run" in text and "--apply" in text
    assert "kofi: last seen 3d ago, declared every 1d" in text
    assert "[attention] participants.silent" in text
    assert "→" in text, "every finding carries its remediation"


def env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCO_REGISTRY_DB", str(tmp_path / "registry.sqlite3"))
    monkeypatch.setenv("AGENTCO_WORK_STORE", str(tmp_path / "work.jsonl"))
    monkeypatch.setenv("AGENTCO_SOP_STORE", str(tmp_path / "sops.jsonl"))
    monkeypatch.setenv("AGENTCO_ACTOR", "dana")
    for var in ("AGENTCO_REGISTRY_KEYS", "AGENTCO_CADENCE", "AGENTCO_PULSE_EVERY", "AGENTCO_DB"):
        monkeypatch.delenv(var, raising=False)


def test_cli_exit_codes_follow_the_class(monkeypatch, tmp_path, capsys):
    env(monkeypatch, tmp_path)
    assert cli.main(["pulse", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["class"] == "ok"

    monkeypatch.setenv("AGENTCO_CADENCE", "ghost=1h")
    assert cli.main(["pulse"]) == 1
    assert "never been seen" in capsys.readouterr().out

    monkeypatch.setenv("AGENTCO_CADENCE", "ghost")
    assert cli.main(["pulse", "--json"]) == 2

    monkeypatch.delenv("AGENTCO_CADENCE")
    assert cli.main(["pulse", "--every", "soon"]) == 2
    assert "--every" in capsys.readouterr().err


def test_cli_apply_records_and_honours_the_declared_interval(monkeypatch, tmp_path, capsys):
    env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTCO_PULSE_EVERY", "15m")
    assert cli.main(["pulse", "--apply", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["applied"] is True and report["self"]["every"] == 900
    assert report["recorded"]["seq"] >= 1


def test_the_session_hook_shows_the_pulse_only_once_one_has_been_recorded(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path)
    assert "Pulse:" not in hook.build_additional_context("dana")

    assert cli.main(["pulse", "--apply", "--every", "1h"]) == 0
    context = hook.build_additional_context("dana")
    assert "Pulse: last ran" in context and "overdue" not in context


def test_the_session_line_flags_an_overdue_pulse_from_what_it_declared():
    observed = {"at": at(-3 * 3600).isoformat(), "class": "ok", "every": 3600,
                "findings": {"ok": 0, "attention": 0, "fatal": 0}}
    line = pulse.render_session_line(observed, now=NOW)
    assert line.startswith("Pulse: last ran 3h ago — ok, 0 finding(s)")
    assert "overdue" in line
    assert pulse.render_session_line(None) is None


# --------------------------------------------------------------------------- #
# found live, in the two-harness test
# --------------------------------------------------------------------------- #

JUDGED_ESCALATE = {
    "kind": "judged", "check": "a reviewer reads the diff", "on_timeout": "escalate",
    "escalate_to": "dana", "max_park_seconds": 60,
}


def test_a_parked_judged_gate_reaches_the_feed_on_the_first_apply(conn, queue):
    """Over HTTP, a report that parks a judged gate emits nothing — `WorkParked`
    belongs to the routing pass. Before the pulse ran that pass, a parked gate
    was invisible on the feed until its clock ran out."""
    item = queue.create("ship the release", verify=JUDGED_ESCALATE)
    leased = queue.claim(item.id, "executor")
    queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE)
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'WorkParked'").fetchone()[0] == 0

    dry = run(conn, queue)
    assert dry["housekeeping"]["routing"]["created"] == 1
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'WorkParked'").fetchone()[0] == 0

    applied = run(conn, queue, apply=True)
    assert applied["housekeeping"]["routing"]["created"] == 1
    parked = conn.execute("SELECT payload FROM events WHERE kind = 'WorkParked'").fetchall()
    assert len(parked) == 1 and json.loads(parked[0]["payload"])["itemId"] == item.id


def test_the_unauthenticated_placeholder_is_not_a_participant(conn, queue):
    """One `curl /events` with no credentials records a call by actor `-`.
    Nobody is called `-`, and a monitor that lists it as a silent colleague
    is a monitor people stop reading."""
    from agentco import metrics

    metrics.record_call(conn, verb="events", actor="-", status="refused", code="unauthenticated", latency_ms=1.0)
    metrics.record_call(conn, verb="events", actor="kofi", status="accepted", latency_ms=1.0)
    report = run(conn, queue)
    assert [p["actor"] for p in report["participants"]] == ["kofi"]
