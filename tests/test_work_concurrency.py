"""The lease protocol, proven across REAL OS processes — not one interpreter.

`test_work.py` proves the CAS and the fence exhaustively, but every one of its
tests claims and reports inside a single Python process. That leaves the
`fcntl` lock, the atomic `os.replace`, and the read-modify-write window in
`Queue._mutate` entirely unexercised under real concurrency: a single-process
test can call `claim()` twice in a row and never once contend for the file
descriptor the way two independent workers actually would. The two-machine
deployment that proved this protocol under real contention was the PRIVATE
implementation this repository was extracted from — this module is a rewrite,
so that evidence does not carry over, and nothing here re-derives it until
these tests do.

**Threads would not prove anything.** `fcntl.flock` locks are scoped to the
open file descriptor and are reentrant within the process that holds one, so
two threads in the same interpreter never actually contend for it the way two
processes do — a threaded version of this file would pass while proving
nothing about the mechanism it claims to test. `multiprocessing` with the
**spawn** start method is used throughout: `fork` would inherit file
descriptors and muddy exactly the isolation this is supposed to demonstrate.

Four things are proven here, each against a real process boundary:

  1. Exactly one of N (>=8) real processes wins a claim contended by all of
     them at once.
  2. The store survives the storm: no corrupt line, no duplicated or vanished
     row, and — the check a bare winner-count would miss — every worker's
     self-reported win matches the store's OWN final state, not just its own
     return value. A lost update could let a worker believe it won while the
     file quietly disagrees.
  3. A lease that expires while its holder is out of process is fenced out
     when that holder reports late, even though the claim that superseded it
     happened in a different interpreter with no shared memory at all.
  4. N processes creating with the same natural key concurrently converge on
     exactly one item, proving the lock — not luck — is what makes the
     dedup hold.

Workers write their own outcome to a private result file rather than a
`multiprocessing.Queue`, so nothing here is entangled with `agentco.work.Queue`
by name or vice versa, and so a stuck worker cannot deadlock the test on a
blocking queue read — the parent just reads files after `join()`.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import time
from pathlib import Path

import pytest

from agentco.errors import Refusal
from agentco.work import CapabilityError, LeaseError, Queue, WorkStatus

# Every wait in this file is sized to absorb a slow, oversubscribed CI runner
# without ever needing a human to retune it per-machine — generous on
# purpose, so a failure here can only mean a real bug, never "the runner was
# a bit slow today".
JOIN_TIMEOUT_S = 60
BARRIER_TIMEOUT_S = 30
EVENT_TIMEOUT_S = 20


def _join_all(procs: list[mp.process.BaseProcess], timeout: float) -> None:
    """Join every process, then fail loudly and specifically if any hung or crashed.

    A hang here means a worker is stuck retrying against something that never
    resolves — exactly the shape a real deadlock in `_locked()` would take —
    so it is reported as a failure, not silently timed out past.
    """
    deadline = time.monotonic() + timeout
    for p in procs:
        p.join(timeout=max(0.0, deadline - time.monotonic()))
        if p.is_alive():
            p.terminate()
            p.join(5)
            pytest.fail(
                f"worker pid={p.pid} did not finish within {timeout}s — a hang here is a "
                f"real bug (likely a deadlock in Queue._locked()), not a slow runner"
            )
    crashed = [(p.pid, p.exitcode) for p in procs if p.exitcode != 0]
    if crashed:
        pytest.fail(
            f"worker(s) exited non-zero: {crashed} — see the captured stderr above, "
            f"which carries each worker's traceback since child fds are inherited"
        )


# --------------------------------------------------------------------------- #
# Proofs 1 + 2 — a contended claim has exactly one winner, and the store
# reflects reality when the storm is over
# --------------------------------------------------------------------------- #


def _claim_storm_worker(
    work_path: str,
    agent_name: str,
    hot_item_id: str,
    ttl_seconds: int,
    result_path: str,
    barrier,
) -> None:
    """One worker's role in the storm: attack one shared row, then drain the rest.

    Phase 1 targets `hot_item_id` directly and immediately after the barrier
    releases — no `ready()` poll in between that would let OS scheduling
    decide who even attempts the claim. That is what makes this a genuine
    proof of the CAS rather than a proof of who happened to run first.

    Phase 2 then drains whatever else is claimable, the way a real poller
    would, purely to keep sustained concurrent write pressure on the file
    past a single row — that sustained pressure is what has a chance of
    surfacing a lost-update bug in the read-modify-write window.
    """
    queue = Queue(work_path)
    result: dict = {
        "agent": agent_name,
        "hotClaimWon": False,
        "hotLeaseAttempt": None,
        "drained": [],
        "errors": [],
    }
    try:
        barrier.wait(timeout=BARRIER_TIMEOUT_S)

        try:
            hot = queue.claim(hot_item_id, agent_name, ttl_seconds=ttl_seconds)
        except CapabilityError as exc:  # pragma: no cover - defensive; no item here declares requires
            result["errors"].append(f"hot claim raised CapabilityError: {exc}")
            hot = None
        result["hotClaimWon"] = hot is not None
        if hot is not None:
            result["hotLeaseAttempt"] = hot.lease_attempt

        for _ in range(500):  # safety cap — see module docstring on hangs vs. bugs
            ready = queue.ready(agent=agent_name)
            if not ready:
                break
            try:
                claimed = queue.claim(ready[0].id, agent_name, ttl_seconds=ttl_seconds)
            except CapabilityError as exc:  # pragma: no cover - same as above
                result["errors"].append(f"drain claim raised CapabilityError: {exc}")
                continue
            if claimed is not None:
                result["drained"].append({"id": claimed.id, "leaseAttempt": claimed.lease_attempt})
        else:
            result["errors"].append("safety cap of 500 iterations hit — ready() never emptied")
    finally:
        Path(result_path).write_text(json.dumps(result))


def test_concurrent_claim_storm_has_one_winner_and_the_store_stays_intact(tmp_path):
    ctx = mp.get_context("spawn")
    work_path = tmp_path / "work.jsonl"
    queue = Queue(str(work_path))

    worker_count = 12  # >= 8 per the proof requirement
    filler_count = 23  # roughly 2x the workers, so the drain phase has real contention
    hot = queue.create("the contended item")
    fillers = [queue.create(f"filler-{i}") for i in range(filler_count)]
    all_ids = {hot.id} | {f.id for f in fillers}

    barrier = ctx.Barrier(worker_count)
    result_paths = [tmp_path / f"storm-result-{i}.json" for i in range(worker_count)]
    procs = [
        ctx.Process(
            target=_claim_storm_worker,
            args=(str(work_path), f"worker-{i}", hot.id, 300, str(result_paths[i]), barrier),
        )
        for i in range(worker_count)
    ]
    for p in procs:
        p.start()
    _join_all(procs, JOIN_TIMEOUT_S)

    results = [json.loads(p.read_text()) for p in result_paths]
    for r in results:
        assert not r["errors"], f"{r['agent']} reported errors: {r['errors']}"

    # Proof 1 — exactly one of the N real processes won the row every one of
    # them attacked in the same instant.
    hot_winners = [r["agent"] for r in results if r["hotClaimWon"]]
    assert len(hot_winners) == 1, f"expected exactly one winner of the contended claim, got {hot_winners}"

    # Proof 2 — the store survived the storm.
    final_queue = Queue(str(work_path))
    final_items = final_queue.list()
    assert final_queue.quarantined == [], "a line in the store failed to parse — corruption under contention"

    raw_ids = [item.id for item in final_items]
    assert len(raw_ids) == len(all_ids), (
        f"expected {len(all_ids)} items, found {len(raw_ids)} — an item vanished or was duplicated "
        f"under concurrent writes"
    )
    assert len(set(raw_ids)) == len(raw_ids), "the same item id appears twice in the store — a row was cloned"
    assert set(raw_ids) == all_ids

    # The check a bare winner-count would miss: every self-reported win must
    # match the FINAL on-disk state, not just the winner's own return value.
    claimed_by: dict[str, tuple[str, int]] = {}
    if hot_winners:
        winner = next(r for r in results if r["hotClaimWon"])
        claimed_by[hot.id] = (winner["agent"], winner["hotLeaseAttempt"])
    for r in results:
        for win in r["drained"]:
            assert win["id"] not in claimed_by, (
                f"item {win['id']} was claimed by two different workers "
                f"({claimed_by[win['id']]} and ({r['agent']}, {win['leaseAttempt']})) — "
                f"the CAS let two winners through"
            )
            claimed_by[win["id"]] = (r["agent"], win["leaseAttempt"])

    assert set(claimed_by) == all_ids, f"never claimed by anyone: {all_ids - set(claimed_by)}"

    final_by_id = {item.id: item for item in final_items}
    for item_id, (agent, lease_attempt) in claimed_by.items():
        item = final_by_id[item_id]
        assert item.status == WorkStatus.IN_PROGRESS
        assert item.leased_by == agent, (
            f"item {item_id}: {agent!r} believes it won the claim, but the store says "
            f"{item.leased_by!r} holds it — a lost update ate the winner's write"
        )
        assert item.lease_attempt == lease_attempt


# --------------------------------------------------------------------------- #
# Proof 3 — a stale holder is fenced out even when the claim that superseded
# it happened in a completely different interpreter
# --------------------------------------------------------------------------- #


def _stale_fence_worker_a(
    work_path: str,
    item_id: str,
    ttl_seconds: int,
    a_claimed,
    b_claimed,
    result_path: str,
) -> None:
    queue = Queue(work_path)
    result: dict = {}
    try:
        claimed = queue.claim(item_id, "process-a", ttl_seconds=ttl_seconds)
        if claimed is None:
            result["error"] = "process-a failed to claim a fresh item"
            return
        attempt = claimed.lease_attempt
        a_claimed.set()

        if not b_claimed.wait(timeout=EVENT_TIMEOUT_S):
            result["error"] = "timed out waiting for process-b to claim after expiry"
            return

        try:
            queue.report_result(item_id, attempt, WorkStatus.DONE, result="late-report")
            result["refused"] = False
        except LeaseError as exc:
            result["refused"] = True
            result["message"] = str(exc)
    finally:
        Path(result_path).write_text(json.dumps(result))


def _stale_fence_worker_b(
    work_path: str,
    item_id: str,
    ttl_seconds: int,
    buffer_seconds: int,
    a_claimed,
    b_claimed,
    result_path: str,
) -> None:
    result: dict = {}
    try:
        if not a_claimed.wait(timeout=EVENT_TIMEOUT_S):
            result["error"] = "timed out waiting for process-a to claim"
            return
        # A real wall-clock sleep — there is no shared Python state to fake
        # `now=` with across a process boundary, so the expiry must actually
        # elapse. The buffer is generous enough that this can only fail by
        # exposing a real bug, never by CI merely being slow.
        time.sleep(ttl_seconds + buffer_seconds)
        queue = Queue(work_path)
        claimed = queue.claim(item_id, "process-b", ttl_seconds=60)
        result["claimed"] = claimed is not None
        result["leaseAttempt"] = claimed.lease_attempt if claimed else None
    finally:
        Path(result_path).write_text(json.dumps(result))
        b_claimed.set()  # unblock A's report attempt regardless of outcome above


def test_a_stale_holder_is_fenced_out_across_processes(tmp_path):
    """The failure this closes: a worker that lost its lease — slept, was
    reaped, hit a partition — may still come back with an answer after
    someone else has taken over the item. This proves the fence catches that
    even when "someone else" is a different OS process with no shared memory,
    which is the only way this failure has ever actually happened."""
    ctx = mp.get_context("spawn")
    work_path = tmp_path / "work.jsonl"
    queue = Queue(str(work_path))
    item = queue.create("long-running task")

    a_claimed = ctx.Event()
    b_claimed = ctx.Event()
    result_a = tmp_path / "a.json"
    result_b = tmp_path / "b.json"

    ttl_seconds = 1  # short on purpose, see buffer note below
    buffer_seconds = 2  # dwarfs any CI scheduling jitter between claim and expiry check

    proc_a = ctx.Process(
        target=_stale_fence_worker_a,
        args=(str(work_path), item.id, ttl_seconds, a_claimed, b_claimed, str(result_a)),
    )
    proc_b = ctx.Process(
        target=_stale_fence_worker_b,
        args=(str(work_path), item.id, ttl_seconds, buffer_seconds, a_claimed, b_claimed, str(result_b)),
    )
    proc_a.start()
    proc_b.start()
    _join_all([proc_a, proc_b], JOIN_TIMEOUT_S)

    a_result = json.loads(result_a.read_text())
    b_result = json.loads(result_b.read_text())
    assert not a_result.get("error"), a_result
    assert not b_result.get("error"), b_result

    assert b_result["claimed"] is True, "process B must be able to claim once A's lease has really expired"
    assert b_result["leaseAttempt"] == 2

    assert a_result["refused"] is True, (
        "A's report against its original lease attempt must be refused once B has claimed the "
        "item in a different process — accepting it would silently overwrite B's work"
    )
    assert "superseded" in a_result["message"]

    # The store itself must reflect B's claim, not a ghost of A's late report.
    final = Queue(str(work_path)).get(item.id)
    assert final.leased_by == "process-b"
    assert final.status == WorkStatus.IN_PROGRESS
    assert final.result is None, "A's late report must never have reached the store"


# --------------------------------------------------------------------------- #
# Proof 4 — concurrent create() under the same natural key still yields one item
# --------------------------------------------------------------------------- #


def _duplicate_create_worker(work_path: str, natural_key: str, title: str, result_path: str, barrier) -> None:
    queue = Queue(work_path)
    barrier.wait(timeout=BARRIER_TIMEOUT_S)
    item = queue.create(title, natural_key=natural_key)
    Path(result_path).write_text(json.dumps({"id": item.id}))


def test_concurrent_create_with_the_same_natural_key_yields_exactly_one_item(tmp_path):
    """agentco/keys.py: 'the lock is the unique index' — duplicate suppression
    runs inside the SAME lock that serialises every append, not as a separate
    check bolted on after. N processes racing `create()` on an identical key
    must converge on one winner, proving the lock is what holds the line —
    every existing test for this ran single-process and could not tell the
    difference between 'the lock enforces it' and 'nothing ever raced'."""
    ctx = mp.get_context("spawn")
    work_path = tmp_path / "work.jsonl"

    worker_count = 12
    barrier = ctx.Barrier(worker_count)
    result_paths = [tmp_path / f"dup-result-{i}.json" for i in range(worker_count)]
    procs = [
        ctx.Process(
            target=_duplicate_create_worker,
            args=(
                str(work_path),
                "shared-natural-key",
                f"title-from-worker-{i}",
                str(result_paths[i]),
                barrier,
            ),
        )
        for i in range(worker_count)
    ]
    for p in procs:
        p.start()
    _join_all(procs, JOIN_TIMEOUT_S)

    returned_ids = {json.loads(p.read_text())["id"] for p in result_paths}
    assert len(returned_ids) == 1, f"workers disagreed on which item won the natural key: {returned_ids}"

    final_queue = Queue(str(work_path))
    final_items = final_queue.list()
    assert final_queue.quarantined == []
    matching = [item for item in final_items if item.natural_key == "shared-natural-key"]
    assert len(matching) == 1, f"expected exactly one row for the shared key, found {len(matching)}"
    assert matching[0].id == returned_ids.pop()


# --------------------------------------------------------------------------- #
# Proofs 5-7 — the gate, under the same real-process conditions
#
# Phase 1 added two statuses that interact with four mechanisms this file
# already proves: the CAS, the fence, the reaper and derived blockedness. Those
# interactions were single-process tested only, which is the gap the roadmap
# names in as many words — "the protocol has grown since and the proof has
# not". Extending the proof here rather than starting a file beside it is the
# point: the claim is about the protocol, and a protocol proven in two places
# under two sets of assumptions is proven in neither.
# --------------------------------------------------------------------------- #

DETERMINISTIC_GATE = {
    "kind": "deterministic",
    "check": "pytest -q",
    "max_park_seconds": 900,
    "on_timeout": "fail",
}
JUDGED_GATE = {
    "kind": "judged",
    "check": "a reviewer confirms the rollback was exercised",
    "max_park_seconds": 3600,
    "on_timeout": "escalate",
    "escalate_to": "release-owner",
}


def _attestation(check: str, exit_status: int = 0) -> dict:
    return {
        "check": check,
        "exit_status": exit_status,
        "environment": "spawned-worker",
        "at": "2026-09-01T12:00:00+00:00",
    }


def _gated_completion_worker(
    work_path: str, item_id: str, agent_name: str, result_path: str, barrier
) -> None:
    """Claim a gated item and report it complete, with no attestation.

    A deterministic gate refuses this. What is being watched is not the refusal
    — that is proven single-process — but whether a refusal raised from inside
    the lock, in a real process, leaves the row intact for the next claimant.
    """
    queue = Queue(work_path)
    result = {"agent": agent_name, "claimed": False, "refusedWith": None, "errors": []}
    try:
        barrier.wait(timeout=BARRIER_TIMEOUT_S)
        claimed = queue.claim(item_id, agent_name)
        result["claimed"] = claimed is not None
        if claimed is not None:
            try:
                queue.report_result(item_id, claimed.lease_attempt, WorkStatus.DONE)
            except Refusal as exc:
                result["refusedWith"] = exc.code
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        Path(result_path).write_text(json.dumps(result))


def _downstream_watcher(work_path: str, downstream_id: str, result_path: str, barrier) -> None:
    """Poll `ready()` throughout, recording whether the dependent ever appears.

    This is the momentarily-done race observed from the outside, by a real
    poller in its own interpreter — which is how it would actually bite. A
    single-process assertion after the fact cannot see a window that opens and
    closes between two writes.
    """
    queue = Queue(work_path)
    sightings: list[str] = []
    barrier.wait(timeout=BARRIER_TIMEOUT_S)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if any(item.id == downstream_id for item in queue.ready()):
            sightings.append(time.strftime("%H:%M:%S"))
        time.sleep(0.002)
    Path(result_path).write_text(json.dumps({"sightings": sightings}))


def test_a_dependent_never_becomes_claimable_while_a_gate_is_open(tmp_path):
    """**The momentarily-done race, watched by a real poller in another process.**

    Eight workers race to complete a judged upstream item while a ninth process
    does nothing but ask the queue what is claimable, thousands of times, for
    the whole duration. If any write ever leaves the dependent momentarily
    unblocked — the exact window a single-process test cannot observe — the
    watcher records it.
    """
    ctx = mp.get_context("spawn")
    work_path = tmp_path / "work.jsonl"
    queue = Queue(str(work_path))
    upstream = queue.create("migrate the schema", verify=JUDGED_GATE)
    downstream = queue.create("backfill", blocked_by=[upstream.id])

    workers = 8
    barrier = ctx.Barrier(workers + 1)
    completion_paths = [tmp_path / f"gated-{i}.json" for i in range(workers)]
    watch_path = tmp_path / "watch.json"

    procs = [
        ctx.Process(
            target=_gated_completion_worker,
            args=(str(work_path), upstream.id, f"worker-{i}", str(completion_paths[i]), barrier),
        )
        for i in range(workers)
    ]
    procs.append(
        ctx.Process(target=_downstream_watcher, args=(str(work_path), downstream.id, str(watch_path), barrier))
    )
    for p in procs:
        p.start()
    _join_all(procs, JOIN_TIMEOUT_S)

    sightings = json.loads(watch_path.read_text())["sightings"]
    assert sightings == [], (
        f"the dependent became claimable {len(sightings)} time(s) while its "
        f"upstream gate was open — downstream work would have started on an "
        f"unverified completion"
    )

    final = Queue(str(work_path))
    assert final.quarantined == []
    assert final.get(upstream.id).status == WorkStatus.AWAITING_VERIFY
    assert downstream.id not in {i.id for i in final.ready()}


def test_a_refused_gated_report_leaves_the_row_claimable_by_the_next_worker(tmp_path):
    """A refusal raised inside the lock must write nothing.

    Eight processes contend; one wins the claim and is refused for reporting a
    deterministic gate with no attestation. The row must come back intact —
    same status, same lease, no evidence — because a refusal that half-writes is
    how an item ends up unclaimable and unfinished with nobody holding it.
    """
    ctx = mp.get_context("spawn")
    work_path = tmp_path / "work.jsonl"
    queue = Queue(str(work_path))
    item = queue.create("ship it", verify=DETERMINISTIC_GATE)

    workers = 8
    barrier = ctx.Barrier(workers)
    paths = [tmp_path / f"refused-{i}.json" for i in range(workers)]
    procs = [
        ctx.Process(
            target=_gated_completion_worker,
            args=(str(work_path), item.id, f"worker-{i}", str(paths[i]), barrier),
        )
        for i in range(workers)
    ]
    for p in procs:
        p.start()
    _join_all(procs, JOIN_TIMEOUT_S)

    results = [json.loads(p.read_text()) for p in paths]
    for r in results:
        assert not r["errors"], r["errors"]
    winners = [r for r in results if r["claimed"]]
    assert len(winners) == 1, f"expected one claimant, got {[w['agent'] for w in winners]}"
    assert winners[0]["refusedWith"] == "attestation_required"

    final = Queue(str(work_path))
    assert final.quarantined == []
    stored = final.get(item.id)
    assert stored.status == WorkStatus.IN_PROGRESS, "a refusal must not move the item"
    assert stored.attestation is None
    assert stored.verify_failures == 0


def _late_gated_reporter(work_path: str, item_id: str, result_path: str, ready_evt, go_evt) -> None:
    """Claim on a short lease, wait to be told the reaper has run, then report.

    The gated equivalent of proof 3. A worker that lost its lease may still be
    running and may still come back — now with an ATTESTATION, which is the new
    part: passing evidence must not buy a fenced-out report its way past the
    fence, or the gate becomes a route around the lease protocol.
    """
    queue = Queue(work_path)
    result = {"claimed": False, "attempt": None, "fenced": False, "errors": []}
    try:
        claimed = queue.claim(item_id, "slow-worker", ttl_seconds=1)
        result["claimed"] = claimed is not None
        result["attempt"] = claimed.lease_attempt if claimed else None
        ready_evt.set()
        if not go_evt.wait(timeout=EVENT_TIMEOUT_S):
            result["errors"].append("never told to proceed")
            return
        try:
            queue.report_result(
                item_id,
                result["attempt"],
                WorkStatus.DONE,
                attestation=_attestation("pytest -q"),
                submitted_by="slow-worker",
            )
        except LeaseError:
            result["fenced"] = True
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        Path(result_path).write_text(json.dumps(result))


def test_a_passing_attestation_does_not_get_a_stale_holder_past_the_fence(tmp_path):
    """Evidence is not authority. The fence is checked first, and stays first.

    The failure this closes is subtle and would look like correct behaviour in a
    log: a reaped worker comes back with a green check, and because the check
    genuinely passed, accepting it feels harmless. It is not — the item may have
    been handed to somebody else and finished since, and this report is derived
    from an execution the queue already abandoned.
    """
    ctx = mp.get_context("spawn")
    work_path = tmp_path / "work.jsonl"
    queue = Queue(str(work_path))
    item = queue.create("ship it", verify=DETERMINISTIC_GATE)

    ready_evt, go_evt = ctx.Event(), ctx.Event()
    result_path = tmp_path / "late.json"
    proc = ctx.Process(target=_late_gated_reporter, args=(str(work_path), item.id, str(result_path), ready_evt, go_evt))
    proc.start()
    assert ready_evt.wait(timeout=EVENT_TIMEOUT_S), "the worker never claimed"

    time.sleep(1.2)  # outlive the 1s lease
    parent = Queue(str(work_path))
    reaped = parent.reap_expired_leases()
    assert [r.id for r in reaped] == [item.id]
    go_evt.set()
    _join_all([proc], JOIN_TIMEOUT_S)

    result = json.loads(result_path.read_text())
    assert not result["errors"], result["errors"]
    assert result["claimed"] and result["fenced"], (
        "the reaped worker's report was accepted — a passing attestation must "
        "not substitute for a live lease"
    )

    final = Queue(str(work_path))
    stored = final.get(item.id)
    assert stored.status == WorkStatus.PENDING, "expiry returns work; it does not complete it"
    assert stored.attestation is None, "no evidence from an abandoned execution was stored"


def _attester(work_path: str, item_id: str, who: str, exit_status: int, result_path: str, barrier) -> None:
    queue = Queue(work_path)
    result = {"who": who, "moved": None, "refused": None, "errors": []}
    try:
        barrier.wait(timeout=BARRIER_TIMEOUT_S)
        item = queue.attest(item_id, _attestation(JUDGED_GATE["check"], exit_status), who)
        result["moved"] = item.status.value if item else None
    except Refusal as exc:
        result["refused"] = exc.code
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        Path(result_path).write_text(json.dumps(result))


def test_only_one_of_many_concurrent_verifiers_can_close_a_gate(tmp_path):
    """Six verifiers, one open gate, and the item may only be closed once.

    `attest` is a read-modify-write like every other mutation here, so it has
    the same window — and the consequence of losing it is worse than a lost
    claim: two verdicts on one unit of work, with the second silently replacing
    the first's evidence on a unit already recorded as done.
    """
    ctx = mp.get_context("spawn")
    work_path = tmp_path / "work.jsonl"
    queue = Queue(str(work_path))
    item = queue.create("ship it", verify=JUDGED_GATE)
    claimed = queue.claim(item.id, "executor")
    queue.report_result(item.id, claimed.lease_attempt, WorkStatus.DONE)
    assert queue.get(item.id).status == WorkStatus.AWAITING_VERIFY

    verifiers = 6
    barrier = ctx.Barrier(verifiers)
    paths = [tmp_path / f"attest-{i}.json" for i in range(verifiers)]
    procs = [
        ctx.Process(
            target=_attester,
            args=(str(work_path), item.id, f"reviewer-{i}", 0, str(paths[i]), barrier),
        )
        for i in range(verifiers)
    ]
    for p in procs:
        p.start()
    _join_all(procs, JOIN_TIMEOUT_S)

    results = [json.loads(p.read_text()) for p in paths]
    for r in results:
        assert not r["errors"], r["errors"]
    closed = [r for r in results if r["moved"] == "done"]
    refused = [r for r in results if r["refused"]]
    assert len(closed) == 1, f"{len(closed)} verifiers closed the same gate"
    assert len(refused) == verifiers - 1, "every loser must be told why, not silently no-op'd"

    final = Queue(str(work_path))
    assert final.quarantined == []
    stored = final.get(item.id)
    assert stored.status == WorkStatus.DONE
    assert stored.attestation["submitted_by"] == closed[0]["who"], (
        "the stored evidence must belong to the verifier that actually closed it"
    )
