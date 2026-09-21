"""`ready()` reads the working set, and still answers exactly what a full scan would.

The SQL backends ask for PENDING and IN_PROGRESS rows and then for the status of
the blocker ids those rows actually name, instead of decoding every row the queue
has ever held. That is worth having — measured on Postgres, the naive scan costs
~16us a row, so a queue with a year of history pays for the year on every poll —
and it is worth DEFENDING, because an optimisation that changes an answer is not
an optimisation.

So the test that matters is equivalence: the same queue, the same instant, the
fast path against a reference that reads everything. The edge cases below are the
ones where a plausible-looking WHERE clause gets it wrong.
"""

from __future__ import annotations

import json

import pytest

from agentco.work import WorkItem, WorkStatus, releases_blockers


def _reference(queue, now=None):
    """What `ready()` would answer if it read every row — the base implementation."""
    items = queue._read_all()
    done = {i.id for i in items if releases_blockers(i.status)}
    from agentco.work import _now
    at = now or _now()
    out = []
    for item in items:
        if item.status not in (WorkStatus.PENDING, WorkStatus.IN_PROGRESS):
            continue
        if item.status == WorkStatus.IN_PROGRESS and item.lease_active_at(at):
            continue
        if item.unmet_blockers(done):
            continue
        if item.lease_active_at(at):
            continue
        out.append(item)
    return {i.id for i in out}


def test_the_fast_path_answers_what_a_full_scan_would(queue):
    """A mixed queue, including every shape that could diverge."""
    free = queue.create("nothing holds this")
    blocker = queue.create("the dependency")
    blocked = queue.create("waits on it", blocked_by=[blocker.id])
    retired = queue.create("already finished")
    queue.retire(retired.id, "done")
    after_done = queue.create("waits on the finished one", blocked_by=[retired.id])
    claimed = queue.create("somebody is on it")
    queue.claim(claimed.id, "alice")

    ready = {i.id for i in queue.ready()}
    assert ready == _reference(queue)
    # and the shapes, stated so a failure says which rule broke
    assert free.id in ready
    assert blocker.id in ready
    assert blocked.id not in ready          # its blocker is not done
    assert after_done.id in ready           # its blocker is
    assert retired.id not in ready          # terminal
    assert claimed.id not in ready          # live lease


def test_history_does_not_change_the_answer(queue):
    """The whole point of asking for the working set: a thousand finished items
    must not make a live one invisible, and must not make a blocked one ready."""
    blocker = queue.create("the dependency")
    blocked = queue.create("waits on it", blocked_by=[blocker.id])
    for i in range(50):
        done = queue.create(f"history {i}")
        queue.retire(done.id, "done")

    ready = {i.id for i in queue.ready()}
    assert ready == _reference(queue)
    assert blocker.id in ready
    assert blocked.id not in ready


def test_a_blocker_awaiting_verification_still_blocks(queue):
    """`releases_blockers` admits DONE and nothing else. An item whose worker is
    finished but whose gate has not answered is exactly the case the rule exists
    for, and it is the one a status filter is most likely to get wrong."""
    # JUDGED, deliberately: a deterministic gate with a passing attestation
    # closes on the spot, so it never occupies the state this rule is about.
    gate = {"kind": "judged", "check": "a verifier reads the diff",
            "max_park_seconds": 86400, "on_timeout": "escalate",
            "escalate_to": "role:owner"}
    blocker = queue.create("judged work", verify=gate)
    blocked = queue.create("downstream", blocked_by=[blocker.id])

    queue.claim(blocker.id, "alice")
    item = queue.get(blocker.id)
    queue.report_result(blocker.id, item.lease_attempt, WorkStatus.DONE,
                        submitted_by="alice")
    assert queue.get(blocker.id).status is WorkStatus.AWAITING_VERIFY

    ready = {i.id for i in queue.ready()}
    assert ready == _reference(queue)
    assert blocked.id not in ready, "downstream started on an unverified claim"


def test_an_empty_queue_is_not_an_error(queue):
    assert queue.ready() == []
    assert _reference(queue) == set()
