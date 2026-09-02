"""Decomposition bounds, enforced at create.

The contract (asop.md § Decomposition bounds) calls the ≤6-plus-verify budget a
human review bound, and readers keep taking it for a cap on total work. It is
neither a cap nor a suggestion: a parent holds at most `MAX_CHILDREN`, a tree
goes at most `MAX_DEPTH` deep, a parent cannot close while a child is open, and
a repair goes BESIDE the unit it repairs. Recursion is how the bound is honoured
— seven children each with seven children is 343 leaves, every one of them
inside a set one person can review.

Each rule has the test that fails when the rule is removed from
`work.enforce_decomposition`; the mutants were run. Everything here runs on
both backends through the `queue` fixture, because the count and the block are
computed differently on each and would drift silently otherwise.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth, work
from agentco.app import create_app
from agentco.work import BlockedError, DecompositionError, Queue, WorkStatus


def goal(queue, title="ship the exporter"):
    return queue.create(title)


def child(queue, parent, title="a unit", **extra):
    return queue.create(title, metadata={"parent": parent.id, **extra})


def finish(queue, item, agent="kofi"):
    leased = queue.claim(item.id, agent)
    assert leased is not None, f"could not claim {item.id}"
    return queue.report_result(item.id, leased.lease_attempt, WorkStatus.DONE)


# --------------------------------------------------------------------------- #
# a parent cannot close while a child is open
# --------------------------------------------------------------------------- #


def test_a_child_blocks_its_parent(queue):
    parent = goal(queue)
    unit = child(queue, parent)
    assert unit.id in queue.get(parent.id).blocked_by

    with pytest.raises(BlockedError):
        queue.claim(parent.id, "kofi")

    finish(queue, unit)
    assert queue.claim(parent.id, "kofi") is not None, "children done, parent claimable"


def test_the_block_is_written_with_the_child_not_after_it(queue):
    """Same lock, same write: there is no instant in which the child exists
    and the parent is claimable past it."""
    parent = goal(queue)
    a = child(queue, parent, "a")
    b = child(queue, parent, "b")
    assert set(queue.get(parent.id).blocked_by) == {a.id, b.id}
    assert queue.get(parent.id).updated_at >= parent.updated_at


# --------------------------------------------------------------------------- #
# the review bound
# --------------------------------------------------------------------------- #


def test_a_parent_holds_at_most_seven_children(queue):
    parent = goal(queue)
    for i in range(work.MAX_CHILDREN):
        child(queue, parent, f"unit {i}")
    with pytest.raises(DecompositionError) as caught:
        child(queue, parent, "one too many")
    assert "human review bound" in str(caught.value)
    assert "AGENTCO_MAX_CHILDREN" in str(caught.value), "the escape hatch is named"
    assert len(queue.get(parent.id).blocked_by) == work.MAX_CHILDREN


def test_the_bound_is_a_registry_setting_a_reader_can_raise(queue, monkeypatch):
    monkeypatch.setattr(work, "MAX_CHILDREN", 2)
    parent = goal(queue)
    child(queue, parent, "a")
    child(queue, parent, "b")
    with pytest.raises(DecompositionError):
        child(queue, parent, "c")


def test_a_repair_does_not_consume_the_review_budget(queue):
    """A fix for a failed child goes beside it. If it counted, a goal at the
    bound with one red unit could never be repaired."""
    parent = goal(queue)
    units = [child(queue, parent, f"unit {i}") for i in range(work.MAX_CHILDREN - 1)]
    fix = queue.create("fix unit 0", metadata={"parent": parent.id, "repairs": units[0].id})
    assert fix.metadata["repairs"] == units[0].id
    assert fix.id not in queue.get(parent.id).blocked_by, (
        "a repair blocks nobody — the red original it repairs already does"
    )
    # The last slot is still free: the repair did not take it.
    last = child(queue, parent, "the verify unit")
    assert last.id in queue.get(parent.id).blocked_by
    with pytest.raises(DecompositionError):
        child(queue, parent, "one too many")


# --------------------------------------------------------------------------- #
# depth
# --------------------------------------------------------------------------- #


def test_a_tree_goes_at_most_three_deep(queue):
    root = goal(queue)
    level1 = child(queue, root, "l1")
    level2 = child(queue, level1, "l2")
    level3 = child(queue, level2, "l3")
    with pytest.raises(DecompositionError) as caught:
        child(queue, level3, "l4")
    assert f"depth {work.MAX_DEPTH + 1}" in str(caught.value)


def test_a_broken_chain_is_refused_rather_than_measured_short(queue):
    root = goal(queue)
    orphaned = queue.create("orphan", metadata={"parent": root.id})
    # Corrupt the chain: point the middle at an id that does not exist.
    queue.annotate(orphaned.id, {"parent": "w-00000000"})
    with pytest.raises(DecompositionError) as caught:
        child(queue, orphaned, "beneath a broken chain")
    assert "broken" in str(caught.value)


# --------------------------------------------------------------------------- #
# what a child may not name
# --------------------------------------------------------------------------- #


def test_a_child_of_nothing_is_refused(queue):
    with pytest.raises(DecompositionError) as caught:
        queue.create("loose end", metadata={"parent": "w-deadbeef"})
    assert "does not exist" in str(caught.value)
    assert queue.list() == [], "nothing was filed"


def test_a_closed_goal_cannot_grow(queue):
    parent = goal(queue)
    finish(queue, parent)
    with pytest.raises(DecompositionError) as caught:
        child(queue, parent, "afterthought")
    assert "closed goal cannot grow" in str(caught.value)


def test_a_malformed_parent_is_refused(queue):
    with pytest.raises(DecompositionError):
        queue.create("x", metadata={"parent": 42})
    with pytest.raises(DecompositionError):
        queue.create("x", metadata={"parent": "  "})


def test_a_repair_goes_beside_never_beneath(queue):
    parent = goal(queue)
    unit = child(queue, parent, "unit")
    other = goal(queue, "another goal")

    with pytest.raises(DecompositionError) as caught:
        queue.create("fix", metadata={"parent": unit.id, "repairs": unit.id})
    assert "never beneath" in str(caught.value)

    with pytest.raises(DecompositionError) as caught:
        queue.create("fix", metadata={"parent": other.id, "repairs": unit.id})
    assert "beside" in str(caught.value)

    beside = queue.create("fix", metadata={"parent": parent.id, "repairs": unit.id})
    assert beside.metadata["parent"] == parent.id
    nowhere = queue.create("fix", metadata={"repairs": unit.id})
    assert "parent" not in nowhere.metadata


def test_a_repair_of_nothing_is_refused(queue):
    with pytest.raises(DecompositionError):
        queue.create("fix", metadata={"repairs": "w-deadbeef"})


def test_items_without_a_parent_are_untouched(queue):
    """The legacy scope guard's shape: no parent, no rule."""
    item = queue.create("flat", metadata={"epic": "x"})
    assert item.blocked_by == []
    assert queue.get(item.id).metadata == {"epic": "x"}


# --------------------------------------------------------------------------- #
# a refusal writes nothing
# --------------------------------------------------------------------------- #


def test_a_refused_child_leaves_the_store_byte_identical(tmp_path):
    queue = Queue(tmp_path / "work.jsonl")
    parent = goal(queue)
    for i in range(work.MAX_CHILDREN):
        child(queue, parent, f"unit {i}")
    before = (tmp_path / "work.jsonl").read_bytes()
    with pytest.raises(DecompositionError):
        child(queue, parent, "one too many")
    assert (tmp_path / "work.jsonl").read_bytes() == before


def test_a_refused_child_leaves_the_parent_unblocked_by_it(queue):
    parent = goal(queue)
    for i in range(work.MAX_CHILDREN):
        child(queue, parent, f"unit {i}")
    blocked = list(queue.get(parent.id).blocked_by)
    with pytest.raises(DecompositionError):
        child(queue, parent, "one too many")
    assert queue.get(parent.id).blocked_by == blocked
    assert len(queue.list()) == work.MAX_CHILDREN + 1


# --------------------------------------------------------------------------- #
# over HTTP — the refusal has a code, and the parent is blocked on the wire
# --------------------------------------------------------------------------- #

KEYS = {"dana": "dana-secret", "operator": "op-secret"}


def _post(client, path, actor, body):
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    headers = {
        "X-AgentCo-Actor": actor,
        "X-AgentCo-Timestamp": ts,
        "X-AgentCo-Signature": auth.sign(KEYS[actor], "POST", path, ts, raw),
        "Content-Type": "application/json",
    }
    return client.post(path, content=raw, headers=headers)


def test_over_http_the_bound_is_a_422_with_a_code(tmp_path):
    client = TestClient(create_app(
        db_path=str(tmp_path / "api.sqlite3"), keys=KEYS, operator="operator",
        work_store=str(tmp_path / "work.jsonl"), sop_store=str(tmp_path / "sops.jsonl"),
    ))
    parent = _post(client, "/work", "dana", {"title": "goal"}).json()["item"]
    for i in range(work.MAX_CHILDREN):
        r = _post(client, "/work", "dana", {"title": f"unit {i}", "metadata": {"parent": parent["id"]}})
        assert r.status_code == 200, r.text
    refused = _post(client, "/work", "dana", {"title": "one too many", "metadata": {"parent": parent["id"]}})
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "decomposition_bound"
    assert "human review bound" in refused.json()["message"]
