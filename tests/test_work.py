"""The work queue and its lease protocol.

The lease protocol is the part where a subtle bug is silent and expensive, so
these tests are written adversarially: each one reconstructs a failure that
actually occurred in the private implementation this was extracted from, and
would fail if the mechanism guarding it were removed.

The four that matter most:

  * two workers claiming the same item — the double-claim the CAS closes;
  * a worker returning after its lease lapsed — the late report the fence
    closes, and the one that silently overwrote a real result before it existed;
  * an expired lease releasing work WITHOUT failing it, because "the worker
    went away" and "the work is broken" are different facts;
  * a newer writer's column surviving an older reader's update.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agentco.work import (
    BlockedError,
    CapabilityError,
    LeaseError,
    Queue,
    WorkItem,
    WorkStatus,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# The CAS — one worker, not two
# --------------------------------------------------------------------------- #


def test_only_one_of_two_workers_can_claim_the_same_item(queue):
    """The failure this closes: both claimants 'succeeded', both executed, and
    the second completion silently overwrote the first."""
    item = queue.create("build the thing")

    first = queue.claim(item.id, "worker-a", now=NOW)
    second = queue.claim(item.id, "worker-b", now=NOW)

    assert first is not None and first.leased_by == "worker-a"
    assert second is None, "the second claim must lose, not co-own"


def test_a_lost_claim_returns_none_rather_than_raising(queue):
    """For a polling worker, contention is a normal answer. Raising would make
    every drain loop wrap its claim in a try block that swallows real errors."""
    item = queue.create("x")
    queue.claim(item.id, "worker-a", now=NOW)
    assert queue.claim(item.id, "worker-b", now=NOW) is None


def test_a_lost_claim_still_says_so_on_stderr(queue, capsys):
    """A claimant that keeps losing is the first symptom of two workers wrongly
    sharing a lane. Returning None quietly would make that invisible."""
    item = queue.create("x")
    queue.claim(item.id, "worker-a", now=NOW)
    queue.claim(item.id, "worker-b", now=NOW)
    assert "claim refused" in capsys.readouterr().err


def test_an_expired_lease_frees_the_item_without_any_reaper_running(queue):
    """Expiry is evaluated on read. A sweeper that fell over must not leave
    phantom leases holding work nobody is doing."""
    item = queue.create("x")
    queue.claim(item.id, "worker-a", ttl_seconds=60, now=NOW)

    later = NOW + timedelta(hours=2)
    # No reap called — the expiry alone must make it claimable.
    assert queue.claim(item.id, "worker-b", now=later) is not None


def test_claiming_bumps_the_attempt_monotonically(queue):
    item = queue.create("x")
    queue.claim(item.id, "worker-a", ttl_seconds=60, now=NOW)
    assert queue.get(item.id).lease_attempt == 1

    later = NOW + timedelta(hours=2)
    queue.claim(item.id, "worker-b", now=later)
    assert queue.get(item.id).lease_attempt == 2


def test_a_terminal_item_cannot_be_claimed(queue):
    item = queue.create("x")
    claimed = queue.claim(item.id, "worker-a", now=NOW)
    queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE)
    assert queue.claim(item.id, "worker-b", now=NOW) is None


# --------------------------------------------------------------------------- #
# The fence — whose answer is this?
# --------------------------------------------------------------------------- #


def test_a_report_from_a_superseded_lease_is_refused_and_writes_nothing(queue):
    """THE test. A worker that lost its lease may still be running and may still
    come back with an answer. Accepting it would overwrite a real result with
    one from an execution the queue already abandoned."""
    item = queue.create("x")
    stale = queue.claim(item.id, "worker-a", ttl_seconds=60, now=NOW)

    later = NOW + timedelta(hours=2)
    fresh = queue.claim(item.id, "worker-b", now=later)
    queue.report_result(item.id, fresh.lease_attempt, WorkStatus.DONE, result="the real answer")

    with pytest.raises(LeaseError) as exc:
        queue.report_result(item.id, stale.lease_attempt, WorkStatus.DONE, result="stale answer")

    assert "superseded, not lost" in str(exc.value)
    assert queue.get(item.id).result == "the real answer", "the good result survived"


def test_a_stale_report_raises_rather_than_returning_none(queue):
    """A caller must never be able to mistake 'your work was superseded' for
    'your work was recorded'. None would read as the latter."""
    item = queue.create("x")
    claimed = queue.claim(item.id, "worker-a", now=NOW)
    with pytest.raises(LeaseError):
        queue.report_result(item.id, claimed.lease_attempt + 5, WorkStatus.DONE)


def test_the_attempt_counter_only_ever_climbs(queue):
    """It is the history of how many times this item was handed out. Resetting
    it would let a stale holder's attempt number become current again.

    Asserts the PROPERTY — monotonic, never reset — rather than a literal
    number. The literal encoded an old rule where only `claim` advanced the
    counter, which is exactly what let a reporter keep a number the fence still
    accepted. A test pinned to the arithmetic of one implementation blocks a
    correct change to it."""
    item = queue.create("x")
    first = queue.claim(item.id, "worker-a", ttl_seconds=60, now=NOW)
    later = NOW + timedelta(hours=2)
    fresh = queue.claim(item.id, "worker-b", now=later)
    assert fresh.lease_attempt > first.lease_attempt

    queue.report_result(item.id, fresh.lease_attempt, WorkStatus.DONE)
    after = queue.get(item.id).lease_attempt
    assert after >= fresh.lease_attempt, "the counter must never go backwards"


def test_completion_releases_the_lease(queue):
    item = queue.create("x")
    claimed = queue.claim(item.id, "worker-a", now=NOW)
    queue.report_result(item.id, claimed.lease_attempt, WorkStatus.FAILED, result="boom")
    stored = queue.get(item.id)
    assert stored.leased_by is None and stored.lease_expires_at is None


def test_an_honest_retry_is_idempotent(queue):
    """A lossy transport lets a worker apply a result and lose the ack. It must
    be able to send the same result again without double-applying."""
    item = queue.create("x")
    claimed = queue.claim(item.id, "worker-a", now=NOW)
    first = queue.report_result(
        item.id, claimed.lease_attempt, WorkStatus.DONE, result="ok", idempotency_key="k1"
    )
    second = queue.report_result(
        item.id, claimed.lease_attempt, WorkStatus.DONE, result="ok", idempotency_key="k1"
    )
    assert first.id == second.id
    assert second.result == "ok"


def test_report_result_refuses_a_non_terminal_status(queue):
    item = queue.create("x")
    claimed = queue.claim(item.id, "worker-a", now=NOW)
    with pytest.raises(ValueError):
        queue.report_result(item.id, claimed.lease_attempt, WorkStatus.PENDING)


# --------------------------------------------------------------------------- #
# Capabilities — a misroute is not contention
# --------------------------------------------------------------------------- #


def test_a_worker_missing_a_required_capability_raises_rather_than_losing(queue):
    """Filing a permanent misroute as a transient race produces a queue that
    looks busy while making no progress."""
    item = queue.create("build", requires=["gpu"])
    with pytest.raises(CapabilityError) as exc:
        queue.claim(item.id, "worker-a", capabilities=["cpu"], now=NOW)
    assert "missing gpu" in str(exc.value)
    assert "Retrying here cannot help" in str(exc.value)


def test_the_capability_gate_fails_closed_on_none(queue):
    """`capabilities=None` means 'declares nothing', NOT 'skip the check'. Any
    other reading makes the safe default the insecure one."""
    item = queue.create("build", requires=["gpu"])
    with pytest.raises(CapabilityError):
        queue.claim(item.id, "worker-a", capabilities=None, now=NOW)


def test_an_item_requiring_nothing_is_claimable_by_a_worker_declaring_nothing(queue):
    """Otherwise the gate breaks every item that predates capabilities."""
    item = queue.create("build")
    assert queue.claim(item.id, "worker-a", capabilities=None, now=NOW) is not None


def test_capability_is_reported_ahead_of_contention(queue):
    """Given a permanent problem and a transient one, report the permanent one —
    it is the more actionable of two simultaneously true statements."""
    item = queue.create("build", requires=["gpu"])
    queue.claim(item.id, "worker-a", capabilities=["gpu"], now=NOW)
    with pytest.raises(CapabilityError):
        queue.claim(item.id, "worker-b", capabilities=["cpu"], now=NOW)


def test_a_refused_claim_leaves_the_store_byte_identical(jsonl_queue):
    item = jsonl_queue.create("build", requires=["gpu"])
    before = jsonl_queue.path.read_bytes()
    with pytest.raises(CapabilityError):
        jsonl_queue.claim(item.id, "worker-a", capabilities=["cpu"], now=NOW)
    assert jsonl_queue.path.read_bytes() == before


# --------------------------------------------------------------------------- #
# Reaping — the worker went away; the work did not break
# --------------------------------------------------------------------------- #


def test_reaping_returns_work_to_pending_and_does_not_fail_it(queue):
    """A failure is a claim about the work; an expired lease is a claim about
    the WORKER. Marking it failed burns a retry and files an incident about work
    that was never attempted."""
    item = queue.create("x")
    queue.claim(item.id, "worker-a", ttl_seconds=60, now=NOW)

    reaped = queue.reap_expired_leases(now=NOW + timedelta(hours=2))
    assert [r.id for r in reaped] == [item.id]

    stored = queue.get(item.id)
    assert stored.status == WorkStatus.PENDING
    assert stored.status != WorkStatus.FAILED
    # Reaping ENDS the lease, so it ends the attempt with it — the revoked
    # holder must not keep a number the fence still accepts. The property is
    # that the counter never goes backwards, not that reaping leaves it alone.
    assert stored.lease_attempt >= 1, "the attempt counter must never go backwards"


def test_reaping_leaves_a_live_lease_alone(queue):
    item = queue.create("x")
    queue.claim(item.id, "worker-a", ttl_seconds=3600, now=NOW)
    assert queue.reap_expired_leases(now=NOW + timedelta(minutes=5)) == []


def test_a_reaped_item_fences_out_its_old_holder(queue):
    """The point of keeping the attempt counter: the original worker coming back
    after a reap must still be refused."""
    item = queue.create("x")
    stale = queue.claim(item.id, "worker-a", ttl_seconds=60, now=NOW)
    later = NOW + timedelta(hours=2)
    queue.reap_expired_leases(now=later)
    queue.claim(item.id, "worker-b", now=later)

    with pytest.raises(LeaseError):
        queue.report_result(item.id, stale.lease_attempt, WorkStatus.DONE)


# --------------------------------------------------------------------------- #
# Idempotency on ingest
# --------------------------------------------------------------------------- #


def test_a_duplicate_natural_key_returns_the_existing_item(queue):
    first = queue.create("nightly report", kind="report", subject="nightly", period="2026-08-28")
    second = queue.create("nightly report", kind="report", subject="nightly", period="2026-08-28")
    assert second.id == first.id
    assert len(queue.list()) == 1


def test_a_suppressed_duplicate_is_announced_loudly(queue, capsys):
    """A suppressed duplicate nobody announces is indistinguishable from a
    create that worked, and the caller goes on to reference an id that is not
    theirs."""
    queue.create("nightly", kind="report", subject="nightly", period="2026-08-28")
    capsys.readouterr()
    queue.create("nightly", kind="report", subject="nightly", period="2026-08-28")
    err = capsys.readouterr().err
    assert "DUPLICATE-SUPPRESSED" in err
    assert "gen|report|nightly|2026-08-28" in err


def test_the_period_makes_a_recurring_job_idempotent_per_period_not_forever(queue):
    queue.create("nightly", kind="report", subject="nightly", period="2026-08-28")
    queue.create("nightly", kind="report", subject="nightly", period="2026-08-29")
    assert len(queue.list()) == 2


def test_an_item_with_no_key_may_repeat(queue):
    """Two identical hand-created items are two pieces of work, and idempotency
    must not decide otherwise."""
    a = queue.create("look into the flaky test")
    b = queue.create("look into the flaky test")
    assert a.id != b.id
    assert len(queue.list()) == 2


def test_an_external_key_mirrors_the_external_record(queue):
    a = queue.create("ticket", source="tracker", source_id="1234")
    b = queue.create("ticket again", source="TRACKER", source_id="1234")
    assert b.id == a.id, "source folds case"


def test_the_external_id_does_not_fold_case(queue):
    """A message id or ticket key can be case-significant, and folding it would
    merge two records that are not the same record."""
    a = queue.create("mail", source="mail", source_id="AbC")
    b = queue.create("mail", source="mail", source_id="abc")
    assert a.id != b.id


# --------------------------------------------------------------------------- #
# Readiness and storage
# --------------------------------------------------------------------------- #


def test_ready_excludes_blocked_leased_and_terminal(queue):
    plain = queue.create("plain")
    blocker = queue.create("blocker")
    blocked = queue.create("blocked", blocked_by=[blocker.id])
    leased = queue.create("leased")
    queue.claim(leased.id, "worker-a", now=NOW)

    ready_ids = {i.id for i in queue.ready(now=NOW)}
    assert plain.id in ready_ids
    assert blocked.id not in ready_ids
    assert leased.id not in ready_ids


def test_a_blocked_item_becomes_ready_when_its_blocker_is_done(queue):
    blocker = queue.create("blocker")
    blocked = queue.create("blocked", blocked_by=[blocker.id])
    claimed = queue.claim(blocker.id, "worker-a", now=NOW)
    queue.report_result(blocker.id, claimed.lease_attempt, WorkStatus.DONE)

    # No hand-written transition. This test used to synthesize the state change
    # the library never performed — `queue._mutate(..., PENDING)` with a comment
    # excusing it — which is how a test can describe a working system that does
    # not exist. Blockedness is derived from the dependency list now, so nothing
    # has to move for this to become true.
    assert blocked.id in {i.id for i in queue.ready(now=NOW)}
    assert queue.claim(blocked.id, "worker-b", now=NOW) is not None


def test_ready_and_claim_agree_about_an_expired_lease(queue):
    """An item that `claim()` accepts but `ready()` never lists is work no
    poller offers to do — and it strands silently, because nothing reports an
    empty queue as a problem. The two predicates must be the same predicate."""
    item = queue.create("x")
    queue.claim(item.id, "worker-a", ttl_seconds=60, now=NOW)
    later = NOW + timedelta(hours=2)

    assert item.id in {i.id for i in queue.ready(now=later)}, "ready must offer it"
    assert queue.claim(item.id, "worker-b", now=later) is not None, "claim must take it"


def test_ready_hides_an_item_under_a_live_lease(queue):
    item = queue.create("x")
    queue.claim(item.id, "worker-a", ttl_seconds=3600, now=NOW)
    assert item.id not in {i.id for i in queue.ready(now=NOW + timedelta(minutes=5))}


def test_a_targeted_assignment_cannot_be_taken_by_someone_else(queue):
    """`ready()` filtered on `assigned_agent` and `claim()` never looked at it,
    so anyone holding the id could take work routed to someone specific. The two
    must agree, or "ready" is advice rather than a contract."""
    item = queue.create("for dana specifically", assigned_agent="dana")

    with pytest.raises(BlockedError) as exc:
        queue.claim(item.id, "kofi", now=NOW)
    assert "assigned to 'dana'" in str(exc.value)

    assert queue.claim(item.id, "dana", now=NOW) is not None


# There WAS a third test here — `test_claiming_does_not_overwrite_who_the_work
# _was_for` — and it was vacuous. It assigned an item to one identity, claimed
# it as that same identity, and asserted `assigned_agent` still named them. It
# passed against the pre-fix code too, because overwriting "dana" with "dana"
# is invisible. Deleted rather than repaired: the only case where the overwrite
# is observable is a claimant who is NOT the assignee, and that is now refused
# outright by the test above, so the property has a real home. The test below
# covers the other direction — claiming must not INVENT an assignment — and
# that one does fail against the pre-fix code.
#
# Recording this because it is the same defect the whole adversarial review was
# about: a test asserting the half that holds. I wrote it while fixing that
# class of bug, and caught it only by running it against the unfixed code.


def test_an_unassigned_item_stays_unassigned_when_claimed(queue):
    """The same property from the other side: claiming must not INVENT an
    assignment either. An item nobody routed is still an item nobody routed."""
    item = queue.create("open to anyone")
    queue.claim(item.id, "kofi", now=NOW)

    stored = queue.get(item.id)
    assert stored.assigned_agent is None
    assert stored.leased_by == "kofi"


def test_ready_does_not_hide_work_a_worker_lacks_capability_for(queue):
    """Visibility is deliberately not the gate. A queue that hides work a node
    cannot run is one where nobody notices a lane has stopped."""
    item = queue.create("gpu job", requires=["gpu"])
    assert item.id in {i.id for i in queue.ready(now=NOW)}


def test_an_unknown_column_survives_an_update(jsonl_queue):
    """A newer writer's field must not be deleted by an older reader doing a
    routine round trip — that is silent data loss which only shows up as 'the
    field I added keeps disappearing'."""
    item = jsonl_queue.create("x")
    rows = [json.loads(l) for l in jsonl_queue.path.read_text().splitlines() if l.strip()]
    rows[0]["future_column"] = {"added_by": "a newer version"}
    jsonl_queue._write_all(rows)

    jsonl_queue.claim(item.id, "worker-a", now=NOW)

    after = [json.loads(l) for l in jsonl_queue.path.read_text().splitlines() if l.strip()]
    assert after[0]["future_column"] == {"added_by": "a newer version"}
    assert after[0]["leased_by"] == "worker-a"


def test_a_corrupt_line_is_quarantined_not_fatal_and_not_dropped(jsonl_queue):
    """One bad line must not take the store down, and it must not vanish
    either — silently skipping bad data is how a queue loses work and nobody
    finds out for a fortnight."""
    good = jsonl_queue.create("good")
    with jsonl_queue.path.open("a") as handle:
        handle.write("{not json at all\n")

    items = jsonl_queue.list()
    assert [i.id for i in items] == [good.id]
    assert jsonl_queue.quarantined == [b"{not json at all"]

    # This assertion is the one the test's own name always claimed and never
    # made. "Not dropped" can only be checked against DISK, because disk is the
    # only place it could be dropped from — an in-memory list immediately after
    # a read cannot tell you what the next write will do to the file.
    jsonl_queue.create("an unrelated later write")
    assert b"{not json at all" in jsonl_queue.path.read_bytes(), (
        "an unrelated write erased the quarantined line; quarantine must preserve"
    )


def test_the_store_survives_a_write_that_raises(jsonl_queue, monkeypatch):
    """Atomic replace: a failed write leaves the previous file intact rather
    than a truncated one."""
    jsonl_queue.create("x")
    before = jsonl_queue.path.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError):
        jsonl_queue.create("y")
    assert jsonl_queue.path.read_bytes() == before


# --------------------------------------------------------------------------- #
# A report needs a lease, and the reporter must hold it (FIX-L3.10 / FIX-L3.11)
# --------------------------------------------------------------------------- #

JUDGED_GATE = {
    "kind": "judged",
    "check": "a reviewer confirms the rollback ran",
    "max_park_seconds": 3600,
    "on_timeout": "escalate",
    "escalate_to": "release-owner",
}


def test_an_item_nobody_holds_cannot_be_reported(queue):
    """**The re-review's critical.** An unclaimed item accepted a report at
    attempt 0, parked with an executor of None, and the judged-gate separation
    check compared the verifier against nothing — so the party that reported
    could verify its own report. A report ends the lease it was issued under;
    no lease, no report."""
    item = queue.create("never claimed", verify=JUDGED_GATE)
    with pytest.raises(LeaseError) as exc:
        queue.report_result(item.id, 0, WorkStatus.DONE, submitted_by="alice")
    assert "nobody holds it" in str(exc.value)
    stored = queue.get(item.id)
    assert stored.status is WorkStatus.PENDING and stored.lease_attempt == 0


def test_a_reaped_item_cannot_be_reported_until_reclaimed(queue):
    """The variant: claim, lapse, reap → pending at attempt 2 with no holder.
    Reporting at attempt 2 is the same hole with a higher number."""
    import time as _t

    item = queue.create("lapsed", verify=JUDGED_GATE)
    queue.claim(item.id, "alice", ttl_seconds=1)
    _t.sleep(1.1)
    assert [r.id for r in queue.reap_expired_leases()] == [item.id]
    reaped = queue.get(item.id)
    with pytest.raises(LeaseError):
        queue.report_result(item.id, reaped.lease_attempt, WorkStatus.DONE, submitted_by="alice")


def test_a_lapsed_but_unreaped_lease_still_reports(queue):
    """Self-healing is preserved: expiry is evaluated on read, and a holder whose
    lease lapsed a second ago but was not reaped is still the holder of record.
    Refusing them would make the reaper load-bearing, which the protocol says it
    must never be."""
    import time as _t

    item = queue.create("slow but honest")
    claimed = queue.claim(item.id, "alice", ttl_seconds=1)
    _t.sleep(1.1)
    done = queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE, submitted_by="alice")
    assert done.status is WorkStatus.DONE


def test_only_the_holder_may_report_an_item(queue):
    """The attempt number is public to every actor, so knowing it cannot be what
    makes a report legitimate. Bob reporting Alice's item was recorded as
    Alice's completion — and Bob could then verify it."""
    item = queue.create("alice's", verify=JUDGED_GATE)
    claimed = queue.claim(item.id, "alice")
    with pytest.raises(LeaseError) as exc:
        queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE, submitted_by="bob")
    assert "held by 'alice'" in str(exc.value)
    assert queue.get(item.id).leased_by == "alice", "nothing moved"
    assert queue.report_result(
        item.id, claimed.lease_attempt, WorkStatus.DONE, submitted_by="alice"
    ).status is WorkStatus.AWAITING_VERIFY
