"""A refused run leaves the queue byte-identical — including the human-gate case.

Found by the live chain proof (2026-10-18): a run whose SECOND step has a human
gate naming, as verifier, the actor bound to that step was refused 422
`gate_invalid` — but only when the queue reached that step. The parent and step
1 were already filed. Step 1 was then pullable, an agent completed it, and the
parent closed DONE: a run the plane had refused read as a finished run that
never had its human step.

`SopLibrary.run` promises every refusal fires before anything is filed; the
self-verification rule lived only in the queue's `create`. These tests hold the
promise for that rule on every backend.
"""

from __future__ import annotations

import pytest

from agentco.errors import Refusal

DET = {"kind": "deterministic", "check": "make check", "max_park_seconds": 900, "on_timeout": "fail"}
HUMAN = {"kind": "human", "check": "is the retention period right?", "verifier": "owner-a",
         "max_park_seconds": 900, "on_timeout": "escalate", "escalate_to": "owner-a"}


def two_step(library):
    asop = library.create(
        "frame then decide", inputs=[], roles={"worker": {"kind": "agent"}, "owner": {"kind": "human"}},
        steps=[{"name": "frame", "role": "worker", "purpose": "frame", "gate": DET},
               {"name": "decide", "role": "owner", "purpose": "decide", "gate": HUMAN, "after": [1]}],
    )
    library.activate(asop.asop_id, 1, author_kind="human")
    return asop


def test_a_human_step_bound_to_its_own_verifier_files_nothing(library, queue):
    asop = two_step(library)
    with pytest.raises(Refusal) as exc:
        library.run(asop.asop_id, queue, bindings={"worker": "agent-a", "owner": "owner-a"})
    assert exc.value.code == "gate_invalid"
    assert queue.list() == [], "a refused run left beads behind"
    assert queue.ready(agent="agent-a") == []


def test_the_same_run_with_a_different_owner_files_the_whole_tree(library, queue):
    asop = two_step(library)
    run = library.run(asop.asop_id, queue, bindings={"worker": "agent-a", "owner": "owner-b"})
    assert [s["step"] for s in run["steps"]] == [1, 2]
