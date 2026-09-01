"""The outbox across REAL OS processes — which is the only place its claims live.

L1's whole premise is that the writer and the reader are different programs: an
agent appends a line and exits, a scheduled drainer picks it up later. Every
interesting property of that arrangement is a property of two processes sharing
one file, and **a threaded version of this file would pass while proving
nothing** — `fcntl.flock` locks are held by the open file descriptor and are
reentrant within the process holding one, so two threads never contend the way
two harnesses do. `multiprocessing` with the **spawn** start method throughout:
`fork` would inherit descriptors and muddy exactly the isolation being shown.

Three things are proven here, and they are the three the plan names:

  1. A real second process appending to the outbox while a drain is in flight
     loses nothing and duplicates nothing.
  2. A process killed with SIGKILL mid-write leaves a truncated tail, and the
     drainer recovers from it with **exactly one** line quarantined — not zero
     (silent corruption) and not the whole file (one bad byte costing every
     line).
  3. Several drainers racing publish each line exactly once, because the
     drain lock is real and non-blocking rather than advisory-by-convention.

Workers report through private files rather than a `multiprocessing.Queue`, so a
stuck worker cannot deadlock the test on a blocking read and nothing here is
entangled with the module under test by name.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import signal
import time
from pathlib import Path

import pytest

from agentco.outbox import Outbox, drain

JOIN_TIMEOUT_S = 60
BARRIER_TIMEOUT_S = 30

CLAIM = {"repo": "acme/app", "prefixes": ["src/api/handlers"], "intent": "implement"}


def _join_all(procs, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    for proc in procs:
        proc.join(timeout=max(0.0, deadline - time.monotonic()))
        if proc.is_alive():
            proc.terminate()
            proc.join(5)
            pytest.fail(f"{proc.name} hung — a real deadlock in the outbox lock looks exactly like this")
    for proc in procs:
        # SIGKILL is expected in exactly one test, which checks the code itself.
        if proc.exitcode not in (0, -signal.SIGKILL):
            pytest.fail(f"{proc.name} exited {proc.exitcode}")


# --------------------------------------------------------------------------- #
# Workers — module level, because spawn has to import them
# --------------------------------------------------------------------------- #


def _push_worker(node_dir: str, label: str, count: int, barrier, result_path: str) -> None:
    box = Outbox(node_dir)
    barrier.wait(BARRIER_TIMEOUT_S)
    written = [box.push("claim_scope", CLAIM, agent_label=label)["line_id"] for _ in range(count)]
    Path(result_path).write_text(json.dumps(written))


def _kill_mid_write_worker(outbox_path: str, barrier) -> None:
    """Append half a line, then die the way a killed harness dies.

    Deliberately NOT using `Outbox.push`, which writes one complete line under a
    lock. This is the realistic L1 shape: an agent appending with a shell
    redirect or a hand-rolled writer, killed between the payload and the
    newline. If the drainer cannot survive that, the floor is not a floor.
    """
    barrier.wait(BARRIER_TIMEOUT_S)
    with open(outbox_path, "a") as handle:
        handle.write('{"line_id":"ob_dying","at":"2026-09-01T00:00:00+00:00","verb":"claim_sc')
        handle.flush()
    os.kill(os.getpid(), signal.SIGKILL)


def _drain_worker(node_dir: str, barrier, result_path: str) -> None:
    """Drain until the outbox is empty, recording every line THIS process sent.

    The lock is non-blocking, so a losing drainer returns `skipped` immediately;
    it retries so that the batch actually gets published and the test can then
    ask the question that matters — was any line published twice, by anyone.
    """
    box = Outbox(node_dir)
    published: list[str] = []

    def publish(record: dict) -> dict:
        published.append(record["line_id"])
        return {"leaseUid": f"lease-{record['line_id']}"}

    barrier.wait(BARRIER_TIMEOUT_S)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = drain(box, publish)
        if result["state"] == "drained" and not box.pending():
            break
        time.sleep(0.01)
    Path(result_path).write_text(json.dumps({"pid": os.getpid(), "published": published}))


# --------------------------------------------------------------------------- #
# 1 — a publisher appending during a drain
# --------------------------------------------------------------------------- #


def test_a_real_process_appending_during_a_drain_loses_nothing(tmp_path):
    node = tmp_path / ".agentco"
    box = Outbox(node)
    box.push("claim_scope", CLAIM, agent_label="already-here")

    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(4)
    procs = [
        ctx.Process(
            target=_push_worker,
            args=(str(node), f"harness-{i}", 5, barrier, str(tmp_path / f"pushed-{i}.json")),
            name=f"pusher-{i}",
        )
        for i in range(3)
    ]
    for proc in procs:
        proc.start()

    published: list[str] = []

    def publish(record: dict) -> dict:
        published.append(record["line_id"])
        return {"leaseUid": "lease-1"}

    barrier.wait(BARRIER_TIMEOUT_S)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        drain(box, publish)
        if all(not p.is_alive() for p in procs) and not box.pending():
            break
        time.sleep(0.01)

    _join_all(procs, JOIN_TIMEOUT_S)
    drain(box, publish)

    written: set[str] = set()
    for i in range(3):
        written |= set(json.loads((tmp_path / f"pushed-{i}.json").read_text()))
    assert len(written) == 15, "each pusher's lines must all be on disk"

    assert len(published) == len(set(published)), "no line published twice"
    assert set(published) >= written, "every line a pusher wrote was published"
    assert box.pending() == []
    assert len(published) == 16, "fifteen pushed under contention, plus the one that was already there"


# --------------------------------------------------------------------------- #
# 2 — SIGKILL mid-write
# --------------------------------------------------------------------------- #


def test_a_process_killed_mid_write_costs_exactly_one_line(tmp_path):
    """One truncated tail, one quarantined line, every other line delivered.

    Both halves matter. Zero quarantined would mean the corruption was silently
    swallowed; more than one would mean a single bad byte cost lines that were
    written correctly — and that is the failure `_read_raw` decodes per line to
    avoid.
    """
    node = tmp_path / ".agentco"
    box = Outbox(node)
    good = [box.push("claim_scope", CLAIM, agent_label="survivor")["line_id"] for _ in range(4)]

    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(1)
    victim = ctx.Process(
        target=_kill_mid_write_worker, args=(str(box.path), barrier), name="dying-harness"
    )
    victim.start()
    victim.join(JOIN_TIMEOUT_S)
    assert victim.exitcode == -signal.SIGKILL, "the point is a process that did not clean up"

    raw = box.path.read_bytes()
    assert not raw.endswith(b"\n"), "the file really is truncated mid-line"

    published: list[str] = []
    result = drain(box, lambda record: published.append(record["line_id"]) or {"leaseUid": "l"})

    assert result["quarantined"] == 1, "exactly one line was unreadable"
    assert sorted(published) == sorted(good), "every intact line was delivered"
    assert box.pending() == []
    assert "not parseable" in box.quarantine_path.read_text()
    # And the recovered file is clean enough that the next drain is a no-op.
    assert drain(box, lambda r: {"leaseUid": "l"})["quarantined"] == 0


# --------------------------------------------------------------------------- #
# 3 — drainers racing
# --------------------------------------------------------------------------- #


def test_racing_drainers_publish_each_line_exactly_once(tmp_path):
    """The drain lock, across four real processes contending for it at once.

    Exactly-once here is a claim about the LOCK, not about luck: without it, two
    drainers read the same batch and both publish it, and the duplicate is
    invisible to each of them because each one's own list is clean.
    """
    node = tmp_path / ".agentco"
    box = Outbox(node)
    lines = [box.push("claim_scope", CLAIM, agent_label=f"h-{i}")["line_id"] for i in range(20)]

    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(4)
    results = [str(tmp_path / f"drained-{i}.json") for i in range(4)]
    procs = [
        ctx.Process(target=_drain_worker, args=(str(node), barrier, results[i]), name=f"drainer-{i}")
        for i in range(4)
    ]
    for proc in procs:
        proc.start()
    _join_all(procs, JOIN_TIMEOUT_S)

    everything: list[str] = []
    per_process = []
    for path in results:
        report = json.loads(Path(path).read_text())
        per_process.append(len(report["published"]))
        everything.extend(report["published"])

    assert sorted(everything) == sorted(lines), (
        f"each line exactly once across all drainers; got {len(everything)} "
        f"sends for {len(lines)} lines, split {per_process}"
    )
    assert box.pending() == []
    assert sum(per_process) == len(lines)
