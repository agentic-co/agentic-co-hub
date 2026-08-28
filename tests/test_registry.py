"""Stage 1b — scope registry, snapshots, change feed, and the the adoption gate instrument.

The tests that matter most here are the ones that would fail if a claim in
00-DESIGN.md were false, not the ones that confirm the happy path:

  * the scope-model decision's whole argument is that a lease on `src/` makes the registry noise
    within four days — so `src/` must be REFUSED, with a remediation.
  * `Snapshot` is "a pointer, never a copy" — so no artifact body may reach
    the database, asserted by scanning the file's bytes for content that was
    definitely in the artifact.
  * Divergence is delivered at the cadence boundary, never in real time — so
    taking a snapshot and mutating the artifact must produce NOTHING until
    the digest runs.
  * the adoption gate counts sustained use, not politeness — so a missed week must reset
    the streak rather than being bridged.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentco import auth, db, divergence, events, leases, metrics, scope, snapshots
from agentco.app import create_app
from agentco.errors import Refusal

KEYS = {"dana": "dana-secret", "kofi": "kofi-secret", "operator": "op-secret"}


@pytest.fixture()
def conn(tmp_path):
    return db.connect(tmp_path / "registry.sqlite3")


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator")
    return TestClient(app)


def signed(method: str, path: str, actor: str, body: dict | None = None) -> dict:
    raw = json.dumps(body).encode() if body is not None else b""
    ts = str(int(time.time()))
    return {
        "X-AgentCo-Actor": actor,
        "X-AgentCo-Timestamp": ts,
        "X-AgentCo-Signature": auth.sign(KEYS[actor], method, path, ts, raw),
        "Content-Type": "application/json",
    }


def post(client, path, actor, body):
    return client.post(path, content=json.dumps(body), headers=signed("POST", path, actor, body))


def get(client, path, actor, query=""):
    return client.get(f"{path}{query}", headers=signed("GET", path, actor, None))


# --------------------------------------------------------------------------- #
# the scope-model decision — the scope model. The reason 1b is not noise.
# --------------------------------------------------------------------------- #


def test_a_lease_on_src_is_refused_because_it_would_intersect_everything():
    with pytest.raises(Refusal) as exc:
        scope.validate_prefix("src/")
    assert exc.value.code == "scope_too_broad"
    # The remediation must name the requirement, not restate the rule.
    assert "2" in exc.value.remediation
    assert "src/budget/grid" in exc.value.remediation


def test_a_lease_on_the_repo_root_is_refused():
    for root in ("", "/", ".", "./"):
        with pytest.raises(Refusal):
            scope.validate_prefix(root)


def test_two_segments_is_accepted_and_canonicalised():
    assert scope.validate_prefix("./src/budget/") == "src/budget"
    assert scope.validate_prefix("src\\budget") == "src/budget"


def test_a_prefix_escaping_the_repo_is_refused_not_normalised_away():
    """Clamping '../../etc' to something inside the repo would record a lease
    over a directory the caller never named."""
    with pytest.raises(Refusal) as exc:
        scope.validate_prefix("../../etc/passwd")
    assert exc.value.code == "scope_escapes_repo"


def test_intersection_is_segmentwise_not_string_prefix():
    """`src/budget` and `src/budgeting` share a string prefix and share no
    directory. A naive startswith() gets this wrong and fires a false conflict
    — which is exactly how a registry earns its reputation as noise."""
    assert scope.prefixes_overlap("src/budget", "src/budget/grid")
    assert scope.prefixes_overlap("src/budget/grid", "src/budget")
    assert not scope.prefixes_overlap("src/budget", "src/budgeting")
    assert not scope.prefixes_overlap("src/budget", "src/ledger")


def test_scopes_in_different_repos_never_intersect():
    a = scope.Scope.parse("repo/one", ["src/budget"])
    b = scope.Scope.parse("repo/two", ["src/budget"])
    assert scope.scopes_intersect(a, b) == ()


# --------------------------------------------------------------------------- #
# Leases
# --------------------------------------------------------------------------- #


def test_a_conflict_fires_only_between_different_holders(conn):
    leases.claim(conn, actor="dana", repo="r", prefixes=["src/budget"], intent="implement")
    mine_again = leases.claim(
        conn, actor="dana", repo="r", prefixes=["src/budget/grid"], intent="implement"
    )
    assert mine_again["conflicts"] == [], "renewing your own overlapping scope is not a conflict"

    theirs = leases.claim(
        conn, actor="kofi", repo="r", prefixes=["src/budget/grid"], intent="prototype"
    )
    assert len(theirs["conflicts"]) == 2
    assert {c["withHolder"] for c in theirs["conflicts"]} == {"dana"}


def test_a_conflict_carries_both_intents(conn):
    """the scope-model decision: 'prototype vs implement' must read differently from
    'implement vs implement'. That requires both, not just the incumbent's."""
    leases.claim(conn, actor="dana", repo="r", prefixes=["src/budget"], intent="implement")
    result = leases.claim(
        conn, actor="kofi", repo="r", prefixes=["src/budget"], intent="prototype"
    )
    conflict_event = [
        json.loads(r["payload"])
        for r in conn.execute("SELECT payload FROM events WHERE kind = 'ScopeConflict'").fetchall()
    ][0]
    assert conflict_event["myIntent"] == "prototype"
    assert conflict_event["theirIntent"] == "implement"
    assert result["conflicts"][0]["theirIntent"] == "implement"


def test_enforcement_is_advisory_and_says_so(conn):
    """Stage 1b blocks nobody. A registry that refused a colleague's first
    claim would be uninstalled the same week."""
    leases.claim(conn, actor="dana", repo="r", prefixes=["src/budget"], intent="implement")
    theirs = leases.claim(conn, actor="kofi", repo="r", prefixes=["src/budget"], intent="implement")
    assert theirs["state"] == "accepted"
    assert theirs["enforcement"] == "advisory"


def test_claiming_in_someone_elses_name_is_recorded_as_attested_not_trusted(conn):
    """the design — otherwise a client blocks work on another human's scope."""
    result = leases.claim(
        conn, actor="kofi", repo="r", prefixes=["src/budget"], intent="implement", holder="dana"
    )
    assert result["holder"] == "dana"
    assert result["holderAttested"] is True

    own = leases.claim(conn, actor="kofi", repo="r", prefixes=["src/other"], intent="implement")
    assert own["holderAttested"] is False


def test_an_expired_lease_stops_conflicting_without_a_sweeper(conn):
    """Expiry is evaluated in the query. A sweeper that fell over would leave
    phantom leases generating everybody's conflicts and blocking nobody."""
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    leases.claim(
        conn, actor="dana", repo="r", prefixes=["src/budget"], intent="implement",
        ttl_seconds=60, now=past,
    )
    theirs = leases.claim(conn, actor="kofi", repo="r", prefixes=["src/budget"], intent="implement")
    assert theirs["conflicts"] == []


def test_only_the_holder_may_release(conn):
    mine = leases.claim(conn, actor="dana", repo="r", prefixes=["src/budget"], intent="implement")
    with pytest.raises(Refusal) as exc:
        leases.release(conn, actor="kofi", lease_uid=mine["leaseUid"])
    assert exc.value.code == "not_the_holder"


def test_releasing_twice_is_a_duplicate_not_an_error(conn):
    mine = leases.claim(conn, actor="dana", repo="r", prefixes=["src/budget"], intent="implement")
    leases.release(conn, actor="dana", lease_uid=mine["leaseUid"])
    again = leases.release(conn, actor="dana", lease_uid=mine["leaseUid"])
    assert again["state"] == "duplicate"


# --------------------------------------------------------------------------- #
# Snapshots — pointer, never a copy
# --------------------------------------------------------------------------- #


def test_the_artifact_body_never_reaches_the_database(tmp_path):
    """The one invariant the design states in bold. Asserted against the file's raw
    bytes, not against the API surface — an ORM change must not be able to
    start persisting bodies without this failing."""
    secret_text = "CANARY-a7f3e91d-the-body-of-the-prd"
    artifact = tmp_path / "prd.md"
    artifact.write_text(secret_text * 50)

    db_path = tmp_path / "reg.sqlite3"
    conn = db.connect(db_path)
    receipt = snapshots.take(
        conn, actor="dana", artifact_uri=f"file:{artifact}", purpose="PRD baseline"
    )
    assert receipt["contentHash"]
    conn.commit()
    conn.close()

    for candidate in db_path.parent.glob("reg.sqlite3*"):  # includes -wal
        assert b"CANARY-a7f3e91d" not in candidate.read_bytes(), f"body leaked into {candidate.name}"


def test_an_unresolvable_pointer_is_recorded_loudly_never_silently(conn):
    """A pointer that can never fire, looking exactly like one that might, is
    the silent-failure shape. The receipt must say so."""
    receipt = snapshots.take(
        conn, actor="dana", artifact_uri="widgetstore:01ABC", purpose="the spec"
    )
    assert receipt["state"] == "accepted"
    assert receipt["freshness"]["externalResolution"] == "unresolvable"
    assert "no resolver registered" in receipt["freshness"]["reason"]
    assert "widgetstore" in receipt["freshness"]["reason"]
    assert "warning" in receipt


def test_a_connector_can_register_a_resolver_for_its_own_scheme(conn):
    """The extension point that keeps this module vendor-free. A document-store
    integration teaches the registry one scheme; nothing in core changes."""
    def widget_resolver(uri):
        return "widget-rev", "rev-7"

    snapshots.register_resolver("widgetstore", widget_resolver)
    try:
        receipt = snapshots.take(
            conn, actor="dana", artifact_uri="widgetstore:01ABC", purpose="the spec"
        )
        assert receipt["freshness"]["externalResolution"] == "resolved"
        assert receipt["hashKind"] == "widget-rev"
        assert receipt["contentHash"] == "rev-7"
    finally:
        snapshots.RESOLVERS.pop("widgetstore", None)


def test_a_resolver_that_cannot_read_its_target_degrades_rather_than_crashes(conn):
    """A connector reaching a deleted artifact is a real signal, not a 500. It
    lands in the same 'cannot report divergence' lane, with its own reason."""
    def broken(uri):
        raise snapshots.ResolverError("artifact was deleted upstream")

    snapshots.register_resolver("widgetstore", broken)
    try:
        receipt = snapshots.take(
            conn, actor="dana", artifact_uri="widgetstore:gone", purpose="the spec"
        )
        assert receipt["state"] == "accepted"
        assert "deleted upstream" in receipt["freshness"]["reason"]
    finally:
        snapshots.RESOLVERS.pop("widgetstore", None)


def test_an_https_pointer_is_resolved_by_HEAD_so_no_body_is_fetched(monkeypatch, conn):
    """HEAD rather than GET is the whole point — the version token arrives
    without transferring the document."""
    seen = {}

    class FakeResponse:
        headers = {"ETag": '"abc123"'}
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(request, timeout=None):
        seen["method"] = request.get_method()
        return FakeResponse()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    kind, value, unresolved = snapshots.resolve("https://example.com/spec.pdf")
    assert seen["method"] == "HEAD"
    assert (kind, value, unresolved) == ("etag", '"abc123"', None)


def test_a_snapshot_without_a_purpose_is_refused(conn):
    with pytest.raises(Refusal) as exc:
        snapshots.take(conn, actor="dana", artifact_uri="file:/tmp/x", purpose="  ")
    assert exc.value.code == "purpose_required"


# --------------------------------------------------------------------------- #
# Divergence — the cadence boundary IS the product
# --------------------------------------------------------------------------- #


def test_a_changed_artifact_produces_nothing_until_the_digest_runs(conn, tmp_path):
    artifact = tmp_path / "prd.md"
    artifact.write_text("v1")
    snapshots.take(conn, actor="dana", artifact_uri=f"file:{artifact}", purpose="baseline")

    artifact.write_text("v2 — changed")

    before = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind = 'DivergenceObserved'"
    ).fetchone()["n"]
    assert before == 0, "real-time pings are exactly what the design says must not happen"

    collected = divergence.collect(conn)
    assert len(collected["moved"]) == 1
    divergence.deliver(conn, collected)

    after = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind = 'DivergenceObserved'"
    ).fetchone()["n"]
    assert after == 1


def test_the_same_movement_is_not_reported_at_every_boundary_forever(conn, tmp_path):
    artifact = tmp_path / "prd.md"
    artifact.write_text("v1")
    snapshots.take(conn, actor="dana", artifact_uri=f"file:{artifact}", purpose="baseline")
    artifact.write_text("v2")

    divergence.deliver(conn, divergence.collect(conn))
    second = divergence.collect(conn)
    assert second["moved"] == []


def test_the_digest_leads_with_the_coverage_hole_before_any_content(conn, tmp_path):
    """an earlier review found that a partial read presented after
    content reads as complete. Same discipline: the pointers that CANNOT fire
    appear above the ones that did."""
    artifact = tmp_path / "prd.md"
    artifact.write_text("v1")
    snapshots.take(conn, actor="dana", artifact_uri=f"file:{artifact}", purpose="THE-MOVED-ONE")
    snapshots.take(conn, actor="dana", artifact_uri="onedrive:01XYZ", purpose="THE-BLIND-ONE")
    artifact.write_text("v2")

    text = divergence.render_text(divergence.collect(conn))
    assert text.index("CANNOT report divergence") < text.index("THE-MOVED-ONE")
    assert text.index("THE-BLIND-ONE") < text.index("THE-MOVED-ONE")
    assert "absence of a notice below is not evidence" in text


# --------------------------------------------------------------------------- #
# The change feed
# --------------------------------------------------------------------------- #


def test_the_cursor_is_opaque_and_resumable(conn):
    for i in range(5):
        leases.claim(conn, actor="dana", repo="r", prefixes=[f"src/mod{i}"], intent="implement")

    first = events.read(conn, limit=2)
    assert first["count"] == 2
    assert "v1:" not in first["nextCursor"], "the cursor must not be readable as a position"

    second = events.read(conn, since=first["nextCursor"], limit=10)
    assert second["count"] == 3
    seqs = [e["seq"] for e in first["events"] + second["events"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == 5


def test_an_idle_feed_echoes_the_cursor_rather_than_resetting(conn):
    leases.claim(conn, actor="dana", repo="r", prefixes=["src/budget"], intent="implement")
    page = events.read(conn)
    idle = events.read(conn, since=page["nextCursor"])
    assert idle["count"] == 0
    assert idle["nextCursor"] == page["nextCursor"]


def test_next_cursor_is_the_last_row_returned_not_the_table_maximum(conn):
    """Returning the table max would silently skip anything a concurrent
    writer committed between the SELECT and the response."""
    for i in range(4):
        leases.claim(conn, actor="dana", repo="r", prefixes=[f"src/mod{i}"], intent="implement")
    page = events.read(conn, limit=2)
    assert events.decode_cursor(page["nextCursor"]) == page["events"][-1]["seq"]


def test_a_malformed_cursor_is_refused_not_silently_reset(conn):
    with pytest.raises(Refusal) as exc:
        events.read(conn, since="not-a-cursor-at-all")
    assert exc.value.code == "bad_cursor"


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


def test_an_empty_key_table_refuses_everything(conn):
    """Fail closed. A registry that starts with no keys and accepts everything
    is the failure this ordering forecloses."""
    with pytest.raises(Refusal):
        auth.authenticate(
            {"x-agentco-actor": "dana", "x-agentco-timestamp": str(int(time.time())),
             "x-agentco-signature": "deadbeef"},
            "POST", "/scope-claims", b"{}", keys={},
        )


def test_an_unknown_actor_and_a_bad_signature_are_indistinguishable():
    """Otherwise the endpoint is an identity oracle for anyone who can reach it."""
    ts = str(int(time.time()))
    def attempt(actor, sig):
        try:
            auth.authenticate(
                {"x-agentco-actor": actor, "x-agentco-timestamp": ts, "x-agentco-signature": sig},
                "POST", "/scope-claims", b"{}", keys=KEYS,
            )
        except Refusal as exc:
            return exc.message
    assert attempt("nobody", "x") == attempt("dana", "wrong-signature")


def test_a_replayed_request_outside_the_window_is_refused():
    old = str(int(time.time()) - auth.MAX_SKEW_S - 60)
    sig = auth.sign(KEYS["dana"], "POST", "/scope-claims", old, b"{}")
    with pytest.raises(Refusal) as exc:
        auth.authenticate(
            {"x-agentco-actor": "dana", "x-agentco-timestamp": old, "x-agentco-signature": sig},
            "POST", "/scope-claims", b"{}", keys=KEYS,
        )
    assert "window" in exc.value.message


def test_a_tampered_body_invalidates_the_signature():
    ts = str(int(time.time()))
    sig = auth.sign(KEYS["dana"], "POST", "/scope-claims", ts, b'{"repo":"a"}')
    with pytest.raises(Refusal):
        auth.authenticate(
            {"x-agentco-actor": "dana", "x-agentco-timestamp": ts, "x-agentco-signature": sig},
            "POST", "/scope-claims", b'{"repo":"b"}', keys=KEYS,
        )


# --------------------------------------------------------------------------- #
# Stage 1d — the the adoption gate instrument
# --------------------------------------------------------------------------- #


def _publish_at(conn, actor: str, when: datetime):
    metrics.record_call(
        conn, verb="claim_scope", actor=actor, status="accepted", latency_ms=12.0, at=when
    )


def test_gate1_needs_two_non_operator_publishers_for_four_consecutive_weeks(conn):
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    for weeks_ago in (1, 2, 3, 4):
        when = now - timedelta(weeks=weeks_ago)
        _publish_at(conn, "dana", when)
        _publish_at(conn, "kofi", when)

    status = metrics.gate1_status(conn, operator="operator", now=now)
    assert status["met"] is True
    assert status["currentStreakWeeks"] >= 4


def test_the_operator_does_not_count_towards_his_own_gate(conn):
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    for weeks_ago in (1, 2, 3, 4):
        when = now - timedelta(weeks=weeks_ago)
        _publish_at(conn, "dana", when)
        _publish_at(conn, "operator", when)  # the operator, publishing hard

    status = metrics.gate1_status(conn, operator="operator", now=now)
    assert status["met"] is False, "one colleague plus the builder is not two colleagues"


def test_a_missed_week_resets_the_streak_rather_than_bridging_it(conn):
    """'Four consecutive weeks measures use.' Bridging a gap is how a failing
    gate passes."""
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    for weeks_ago in (1, 2, 4, 5):  # week 3 missing
        when = now - timedelta(weeks=weeks_ago)
        _publish_at(conn, "dana", when)
        _publish_at(conn, "kofi", when)

    status = metrics.gate1_status(conn, operator="operator", now=now)
    assert status["met"] is False
    assert status["currentStreakWeeks"] == 2


def test_the_week_in_progress_is_excluded_so_the_gate_cannot_oscillate(conn):
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    _publish_at(conn, "dana", now)
    _publish_at(conn, "kofi", now)
    status = metrics.gate1_status(conn, operator="operator", now=now)
    assert status["weekInProgress"] not in status["byWeek"]


def test_reading_the_feed_is_not_publishing(conn):
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    for weeks_ago in (1, 2, 3, 4):
        when = now - timedelta(weeks=weeks_ago)
        for actor in ("dana", "kofi"):
            metrics.record_call(
                conn, verb="events", actor=actor, status="accepted", latency_ms=5.0, at=when
            )
    status = metrics.gate1_status(conn, operator="operator", now=now)
    assert status["met"] is False, "the adoption gate counts publishers, not consumers"


def test_refusals_before_a_first_success_are_counted(conn):
    """A colleague refused four times before their first accepted call
    experienced this registry as broken. That is only recoverable if visible."""
    base = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
    for i in range(3):
        metrics.record_call(
            conn, verb="claim_scope", actor="dana", status="refused",
            code="scope_too_broad", latency_ms=4.0, at=base + timedelta(minutes=i),
        )
    metrics.record_call(
        conn, verb="claim_scope", actor="dana", status="accepted",
        latency_ms=6.0, at=base + timedelta(minutes=10),
    )
    row = [r for r in metrics.time_to_first_event(conn) if r["actor"] == "dana"][0]
    assert row["refusalsBeforeFirstAccept"] == 3
    assert row["refusalCodes"] == ["scope_too_broad"] * 3
    assert row["secondsToFirstAccept"] == 600.0


def test_an_identity_that_never_succeeded_is_reported_not_omitted(conn):
    metrics.record_call(
        conn, verb="claim_scope", actor="sam", status="refused",
        code="scope_too_broad", latency_ms=3.0,
    )
    row = [r for r in metrics.time_to_first_event(conn) if r["actor"] == "sam"][0]
    assert row["firstAcceptedAt"] is None
    assert row["secondsToFirstAccept"] is None


def test_an_unmeasured_percentile_is_none_never_zero(conn):
    assert metrics.verb_latency(conn) == []
    assert metrics._percentile([], 99) is None


def test_latency_breach_is_flagged_against_the_published_slo(conn):
    for _ in range(10):
        metrics.record_call(
            conn, verb="claim_scope", actor="dana", status="accepted", latency_ms=900.0
        )
    row = [r for r in metrics.verb_latency(conn) if r["verb"] == "claim_scope"][0]
    assert row["breach"] is True
    assert row["sloMs"] == 300.0


def _conflict(conn, i: int, action: str):
    leases.claim(conn, actor="dana", repo="r", prefixes=[f"src/mod{i}"], intent="implement")
    second = leases.claim(conn, actor="kofi", repo="r", prefixes=[f"src/mod{i}"], intent="implement")
    leases.release(conn, actor="kofi", lease_uid=second["leaseUid"], action=action)


def test_conflict_precision_says_the_fix_is_k_not_more_leases(conn):
    """the scope-model decision's tuning instruction, surfaced in the verdict string so the person
    reading it in three months does not need the decision record open."""
    for i in range(metrics.CONFLICT_PRECISION_MIN_SAMPLE):
        _conflict(conn, i, "ignored")

    precision = metrics.conflict_precision(conn)
    assert precision["conflictsActedOn"] == 0
    assert precision["actionable"] is True
    assert "MIN_SEGMENTS" in precision["verdict"]


def test_precision_refuses_to_recommend_on_a_sample_of_one(conn):
    """A single unacted-on conflict is precision 0.0, and telling the operator
    to raise `k` on that evidence spends the credibility this report needs the
    first time it says something expensive (the alarm-credibility rule, generalised)."""
    _conflict(conn, 0, "ignored")
    precision = metrics.conflict_precision(conn)
    assert precision["precision"] == 0.0
    assert precision["actionable"] is False
    assert "below the 10 needed" in precision["verdict"]
    assert "MIN_SEGMENTS" not in precision["verdict"]


def test_acting_on_a_conflict_is_counted(conn):
    for i in range(metrics.CONFLICT_PRECISION_MIN_SAMPLE):
        _conflict(conn, i, "narrowed")
    assert metrics.conflict_precision(conn)["precision"] == 1.0


def test_telemetry_failure_never_fails_the_call_it_measures(conn):
    conn.close()
    metrics.record_call(conn, verb="claim_scope", actor="dana", status="accepted", latency_ms=1.0)


# --------------------------------------------------------------------------- #
# The HTTP surface
# --------------------------------------------------------------------------- #


def test_the_three_endpoints_work_end_to_end(client):
    claim = post(client, "/scope-claims", "dana",
                 {"repo": "acme/web-platform", "prefixes": ["src/budget/grid"],
                  "intent": "implement"})
    assert claim.status_code == 200, claim.text
    assert claim.json()["state"] == "accepted"

    snap = post(client, "/snapshots", "dana",
                {"artifactUri": "onedrive:01ABC", "purpose": "the spec baseline"})
    assert snap.status_code == 200
    assert snap.json()["snapId"].startswith("snap_")

    feed = get(client, "/events", "kofi")
    assert feed.status_code == 200
    kinds = [e["kind"] for e in feed.json()["events"]]
    assert kinds == ["ScopeClaimed", "SnapshotTaken"]


def test_an_unsigned_request_is_401(client):
    response = client.post("/scope-claims", json={"repo": "r", "prefixes": ["src/a/b"],
                                                  "intent": "implement"})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


def test_a_refusal_over_http_carries_a_remediation(client):
    response = post(client, "/scope-claims", "dana",
                    {"repo": "r", "prefixes": ["src"], "intent": "implement"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "scope_too_broad"
    assert body["remediation"], "the design — every refusal carries a remediation sentence"


def test_refused_calls_are_still_recorded_for_gate1(client):
    post(client, "/scope-claims", "dana", {"repo": "r", "prefixes": ["src"], "intent": "implement"})
    report = get(client, "/metrics", "operator").json()
    dana = [r for r in report["timeToFirstEvent"] if r["actor"] == "dana"][0]
    assert dana["refusalsBeforeFirstAccept"] == 1


def test_the_divergence_endpoint_reads_without_consuming_the_backlog(client, tmp_path):
    artifact = tmp_path / "doc.md"
    artifact.write_text("v1")
    post(client, "/snapshots", "dana", {"artifactUri": f"file:{artifact}", "purpose": "baseline"})
    artifact.write_text("v2")

    first = get(client, "/divergence", "dana").json()
    second = get(client, "/divergence", "dana").json()
    assert len(first["moved"]) == 1
    assert len(second["moved"]) == 1, "a GET must not consume what the cadence job delivers"
