"""A human gate compares actors; a judged gate compares parties.

Owner-binding made the separation check ask who ANSWERS for an actor rather
than which actor it is. For a judged gate that is exactly right — one party's
two agents are one route, and without the fold a second agent is a way around
the rule rather than a second opinion.

Applied to a HUMAN gate it refuses the case the gate exists for. A human gate
buys one named person's judgement on the work, and the person most likely to
have run the tool that did the work is the one it names. Fold to the party and
an owner may not sign off their own agent's step — which is not a separation
failure, it is the separation.

Found on a live registry, not in the code: one declared human who owns every
agent on it. Declaring the ownership honestly would have made every human gate
there unanswerable, and the ownership is honest.

What still holds for a human gate, so that dropping the party comparison costs
nothing: the verifier is ONE NAMED ACTOR, that actor is a DECLARED HUMAN, and
it is NOT THE EXECUTOR. The middle one is new here — without it "human gate"
would mean only "one named actor", and naming an agent would turn the rule into
a formality.
"""

from __future__ import annotations

import pytest

from agentco.work import Queue, WorkStatus

HUMAN = {"kind": "human", "check": "the owner reads it", "verifier": "alice",
         "max_park_seconds": 86400, "on_timeout": "escalate", "escalate_to": "role:owner"}
JUDGED = {"kind": "judged", "check": "somebody reads the diff",
          "max_park_seconds": 86400, "on_timeout": "escalate", "escalate_to": "role:owner"}


def reported(queue, executor, gate):
    item = queue.create("work", verify=dict(gate))
    queue.claim(item.id, executor)
    current = queue.get(item.id)
    queue.report_result(item.id, current.lease_attempt, WorkStatus.DONE, submitted_by=executor)
    return queue.get(item.id)


def attestation(submitted_by, check="the owner reads it"):
    return {"check": check, "exit_status": 0, "environment": "local",
            "at": "2026-09-23T10:00:00Z", "submitted_by": submitted_by}


def owned_queue(tmp_path, **kw):
    queue = Queue(tmp_path / "work.jsonl", humans=["alice"], **kw)
    queue.owners = lambda: {"alice-codex-01": "alice", "alice-claude-01": "alice",
                            "bob-claude-01": "bob"}
    return queue


# --------------------------------------------------------------------------- #
# the case that was refused and should not be
# --------------------------------------------------------------------------- #


def test_a_person_may_answer_a_human_gate_over_their_own_agents_work(tmp_path):
    """The whole point. Alice's agent did the work; Alice signs it off."""
    queue = owned_queue(tmp_path, verifiers=["alice"])
    item = reported(queue, "alice-codex-01", HUMAN)

    queue.attest(item.id, attestation("alice"), "alice", capabilities=["verify"])

    assert queue.get(item.id).status is WorkStatus.DONE


def test_the_registry_with_one_human_who_owns_everything_still_works(tmp_path):
    """The live shape, in miniature: every agent answers to the only human.

    Before this, declaring that truthfully made every human gate on the
    registry unanswerable — so the honest table and the working registry were
    mutually exclusive, and one of them was going to be quietly abandoned.
    """
    queue = Queue(tmp_path / "work.jsonl", humans=["alice"], verifiers=["alice"])
    queue.owners = lambda: {"agent-one": "alice", "agent-two": "alice", "agent-three": "alice"}
    for executor in ("agent-one", "agent-two", "agent-three"):
        item = reported(queue, executor, HUMAN)
        queue.attest(item.id, attestation("alice"), "alice", capabilities=["verify"])
        assert queue.get(item.id).status is WorkStatus.DONE


# --------------------------------------------------------------------------- #
# and everything the human gate still refuses
# --------------------------------------------------------------------------- #


def test_nobody_signs_off_their_own_work_even_as_a_human(tmp_path):
    """The same-ACTOR rule survives the change. It is the one that matters."""
    queue = owned_queue(tmp_path, verifiers=["alice"])
    item = reported(queue, "alice", HUMAN)

    with pytest.raises(Exception) as caught:
        queue.attest(item.id, attestation("alice"), "alice", capabilities=["verify"])
    assert "cannot also" in str(caught.value)


def test_a_human_gate_naming_an_agent_falls_back_to_the_party_rule(tmp_path):
    """Without this, dropping the party comparison WOULD be a hole.

    `alice-claude-01` is not a declared human, so naming it as the verifier of
    a human gate is a judged gate wearing a name. It gets the judged gate's
    rule: two tools answering to Alice are one party, and one cannot verify the
    other. The relaxation is for PEOPLE, and the operator says who those are.
    """
    queue = owned_queue(tmp_path, verifiers=["alice-claude-01"])
    gate = dict(HUMAN, verifier="alice-claude-01")
    item = reported(queue, "alice-codex-01", gate)

    with pytest.raises(Exception) as caught:
        queue.attest(item.id, attestation("alice-claude-01"), "alice-claude-01",
                     capabilities=["verify"])
    assert "answer to 'alice'" in str(caught.value)


def test_somebody_who_is_not_the_named_person_is_still_refused(tmp_path):
    queue = Queue(tmp_path / "work.jsonl", humans=["alice", "bob"], verifiers=["alice", "bob"])
    item = reported(queue, "alice-codex-01", HUMAN)

    with pytest.raises(Exception) as caught:
        queue.attest(item.id, attestation("bob"), "bob", capabilities=["verify"])
    assert "is not that person" in str(caught.value)


# --------------------------------------------------------------------------- #
# the judged gate is untouched
# --------------------------------------------------------------------------- #


def test_a_judged_gate_still_folds_to_the_party(tmp_path):
    """The rule this change must not weaken. Two tools, one party, one route."""
    queue = owned_queue(tmp_path, verifiers=["alice-claude-01"])
    item = reported(queue, "alice-codex-01", JUDGED)

    with pytest.raises(Exception) as caught:
        queue.attest(item.id, attestation("alice-claude-01", "somebody reads the diff"),
                     "alice-claude-01", capabilities=["verify"])
    assert "answer to 'alice'" in str(caught.value)


def test_a_judged_gate_is_not_rescued_by_its_verifier_being_human(tmp_path):
    """A person may answer a HUMAN gate over their agent's work. A judged gate
    asks a different question — an independent route — and being a person does
    not make the party a second one."""
    queue = Queue(tmp_path / "work.jsonl", humans=["alice"], verifiers=["alice"])
    queue.owners = lambda: {"alice-codex-01": "alice"}
    item = reported(queue, "alice-codex-01", JUDGED)

    with pytest.raises(Exception) as caught:
        queue.attest(item.id, attestation("alice", "somebody reads the diff"),
                     "alice", capabilities=["verify"])
    assert "answer to 'alice'" in str(caught.value)
