"""The revision policy as contract: the four rules of ASOP.md §6.4, checked
against the shared implementation rather than against either side's store.

The plane's own suite (`tests/test_revision_policy.py`, `tests/test_asop_v3.py`)
exercises these rules through `SopLibrary` and over HTTP; the AgentCo Harness's
exercises them through `AsopStore`. Both now call the same functions, and this
file is what those two agree ON — the rules stated once, with no store, no
transport and no lock in the way, so a change that breaks the contract fails
here before it reaches either side's integration tests.
"""

from __future__ import annotations

import pytest

from asop import ASOP, DEFAULT_PROTECTED_TAGS, Step, validate_asop
from asop.revision import (
    AGENT,
    HUMAN,
    RULE_HUMAN_ONLY,
    RULE_NO_UNDO,
    RULE_PROTECTED,
    RULE_RATCHET,
    RevisionPolicyError,
    check_asop_revision,
    humans_from_env,
    kind_of,
    protected_tags_from_env,
    require_human,
)

DET = {"kind": "deterministic", "check": "pytest -q"}
HUMAN_GATE = {"kind": "human", "check": "a person signs off", "verifier": "dana",
              "max_park_seconds": 86400, "on_timeout": "escalate", "escalate_to": "dana"}


def record(steps, *, version=1, author_kind=HUMAN, roles=None, **over):
    """An ASOP record from a body, so every field is the validated shape."""
    body = {
        "title": "Pay a supplier",
        "task_type": "payment",
        "purpose": "Settle an approved invoice.",
        "roles": roles or {"clerk": {"kind": "agent"}},
        "steps": steps,
    }
    body.update(over)
    clean = validate_asop(body)
    return ASOP(
        asop_id="asop-1", version=version, author="somebody", author_kind=author_kind,
        steps=[Step(**s) for s in clean.pop("steps")], **clean,
    )


def step(name="pay", *, role="clerk", gate=DET, **over):
    out = {"name": name, "role": role, "purpose": "do the thing", "gate": gate}
    out.update(over)
    return out


def check(history, baseline, proposed, kind, **over):
    kwargs = dict(
        history=history, baseline=baseline, proposed=proposed, reviser_kind=kind,
        protected_tags=DEFAULT_PROTECTED_TAGS,
    )
    kwargs.update(over)
    check_asop_revision(**kwargs)


# ------------------------------------------------------- rule 1: protected tags

def test_an_agent_may_not_revise_a_procedure_holding_a_protected_step():
    v1 = record([step("pay", gate=HUMAN_GATE, tags=["money"])])
    v2 = record([step("pay", gate=HUMAN_GATE, tags=["money"], purpose="do it faster")], version=2)
    with pytest.raises(RevisionPolicyError) as caught:
        check([v1], v1, v2, AGENT)
    assert caught.value.rule == RULE_PROTECTED


def test_a_human_may_revise_the_same_procedure():
    v1 = record([step("pay", gate=HUMAN_GATE, tags=["money"])])
    v2 = record([step("pay", gate=HUMAN_GATE, tags=["money"], purpose="do it faster")], version=2)
    check([v1], v1, v2, HUMAN)


def test_the_freeze_covers_the_steps_around_the_protected_one():
    """Rule 1 is absolute, not "unless you left the money step alone".

    Changing what runs around a `money` step changes what that step does, so
    an agent rewriting a neighbour and activating the result has changed the
    thing a person was meant to look at.
    """
    v1 = record([step("pay", gate=HUMAN_GATE, tags=["money"]), step("notify")])
    v2 = record([step("pay", gate=HUMAN_GATE, tags=["money"]),
                 step("notify", purpose="a different notification")], version=2)
    with pytest.raises(RevisionPolicyError) as caught:
        check([v1], v1, v2, AGENT)
    assert caught.value.rule == RULE_PROTECTED


def test_an_agent_may_not_add_a_protected_tag():
    v1 = record([step("pay")])
    v2 = record([step("pay", gate=HUMAN_GATE, tags=["money"])], version=2)
    with pytest.raises(RevisionPolicyError) as caught:
        check([v1], v1, v2, AGENT)
    assert caught.value.rule == RULE_PROTECTED


def test_a_registry_may_add_a_protected_tag_but_never_remove_the_defaults(monkeypatch):
    monkeypatch.setenv("AGENTCO_PROTECTED_TAGS", "pii, PHI")
    assert protected_tags_from_env() == DEFAULT_PROTECTED_TAGS | {"pii", "phi"}
    monkeypatch.setenv("AGENTCO_PROTECTED_TAGS", "")
    assert protected_tags_from_env() == DEFAULT_PROTECTED_TAGS

    v1 = record([step("collect", gate=HUMAN_GATE, tags=["pii"])])
    v2 = record([step("collect", gate=HUMAN_GATE, tags=["pii"], purpose="faster")], version=2)
    check([v1], v1, v2, AGENT)                                   # not protected here
    with pytest.raises(RevisionPolicyError) as caught:
        check([v1], v1, v2, AGENT, protected_tags=DEFAULT_PROTECTED_TAGS | {"pii"})
    assert caught.value.rule == RULE_PROTECTED


# ------------------------------------------------------------- rule 2: ratchet

def test_an_agent_may_not_demote_a_human_gate():
    v1 = record([step("pay", gate=HUMAN_GATE)])
    v2 = record([step("pay", gate=DET)], version=2)
    with pytest.raises(RevisionPolicyError) as caught:
        check([v1], v1, v2, AGENT)
    assert caught.value.rule == RULE_RATCHET


def test_an_agent_may_not_demote_a_human_role():
    human_role = {"clerk": {"kind": "human"}}
    v1 = record([step("pay", gate=HUMAN_GATE)], roles=human_role)
    v2 = record([step("pay", gate=DET)], version=2)
    with pytest.raises(RevisionPolicyError) as caught:
        check([v1], v1, v2, AGENT)
    assert caught.value.rule == RULE_RATCHET


def test_deleting_a_human_step_is_the_same_demotion():
    v1 = record([step("pay", gate=HUMAN_GATE), step("notify")])
    v2 = record([step("notify")], version=2)
    with pytest.raises(RevisionPolicyError) as caught:
        check([v1], v1, v2, AGENT)
    assert caught.value.rule == RULE_RATCHET


def test_an_agent_may_ratchet_toward_human_and_a_human_may_ratchet_back():
    v1 = record([step("pay", gate=DET)])
    v2 = record([step("pay", gate=HUMAN_GATE)], version=2)
    check([v1], v1, v2, AGENT)                                   # toward human: allowed
    check([v1, v2], v2, v1, HUMAN)                               # back again: only a human


# --------------------------------------------- rule 2, absolute: first activation

def test_a_first_activation_of_a_human_step_is_refused_to_an_agent():
    """Baseline and target are the same version, so every differential rule is
    vacuous. The absolute form is what closes that door."""
    v1 = record([step("pay", gate=HUMAN_GATE)])
    with pytest.raises(RevisionPolicyError) as caught:
        check([v1], v1, v1, AGENT, action="activate", first_activation=True)
    assert caught.value.rule == RULE_RATCHET


def test_a_first_activation_of_an_all_agent_procedure_is_allowed():
    v1 = record([step("pay", gate=DET)])
    check([v1], v1, v1, AGENT, action="activate", first_activation=True)


def test_a_first_activation_names_money_before_it_names_the_human_gate():
    """A protected step is necessarily human-gated, so both rules fire. The
    one that answers WHY has to be the one that speaks."""
    v1 = record([step("pay", gate=HUMAN_GATE, tags=["money"])])
    with pytest.raises(RevisionPolicyError) as caught:
        check([v1], v1, v1, AGENT, action="activate", first_activation=True)
    assert caught.value.rule == RULE_PROTECTED


def test_a_human_activates_what_an_agent_may_not():
    v1 = record([step("pay", gate=HUMAN_GATE, tags=["money"])])
    check([v1], v1, v1, HUMAN, action="activate", first_activation=True)


# ------------------------------------------------------------- rule 3: no undo

def test_an_agent_may_not_restore_what_a_human_removed():
    v1 = record([step("pay", common_mistakes=["paying twice", "paying the wrong account"])])
    v2 = record([step("pay", common_mistakes=["paying twice"])],
                version=2, author_kind=HUMAN)                    # a human dropped one
    v3 = record([step("pay", common_mistakes=["paying twice", "paying the wrong account"])],
                version=3)
    with pytest.raises(RevisionPolicyError) as caught:
        check([v1, v2], v2, v3, AGENT)
    assert caught.value.rule == RULE_NO_UNDO


def test_an_agent_may_not_remove_what_a_human_added():
    v1 = record([step("pay")])
    v2 = record([step("pay", common_mistakes=["paying twice"])], version=2, author_kind=HUMAN)
    v3 = record([step("pay")], version=3)
    with pytest.raises(RevisionPolicyError) as caught:
        check([v1, v2], v2, v3, AGENT)
    assert caught.value.rule == RULE_NO_UNDO


def test_an_agent_may_not_move_a_field_back_to_what_a_human_moved_it_from():
    v1 = record([step("pay", purpose="pay it")])
    v2 = record([step("pay", purpose="pay it, after checking the total")],
                version=2, author_kind=HUMAN)
    v3 = record([step("pay", purpose="pay it")], version=3)
    with pytest.raises(RevisionPolicyError) as caught:
        check([v1, v2], v2, v3, AGENT)
    assert caught.value.rule == RULE_NO_UNDO


def test_a_later_human_word_lifts_the_ban_and_replaces_it():
    """The rule is about not undoing people, not about freezing the past.

    Once a human's last word is `present`, the ban on re-adding lifts — and a
    ban on REMOVING takes its place.
    """
    v1 = record([step("pay", common_mistakes=["paying twice"])])
    v2 = record([step("pay")], version=2, author_kind=HUMAN)     # human removes it
    back = record([step("pay", common_mistakes=["paying twice"])], version=3)
    with pytest.raises(RevisionPolicyError):
        check([v1, v2], v2, back, AGENT)

    v3 = record([step("pay", common_mistakes=["paying twice"])],
                version=3, author_kind=HUMAN)                    # human puts it back
    gone = record([step("pay")], version=4)
    check([v1, v2, v3], v3, v3, AGENT)                           # keeping it: fine
    with pytest.raises(RevisionPolicyError) as caught:
        check([v1, v2, v3], v3, gone, AGENT)
    assert caught.value.rule == RULE_NO_UNDO


def test_a_version_with_no_recorded_author_forbids_nothing():
    """Versions written before authorship was recorded impose nothing — the
    contract does not invent a human it never saw."""
    v1 = record([step("pay", common_mistakes=["paying twice"])], author_kind=None)
    v2 = record([step("pay")], version=2, author_kind=None)
    v3 = record([step("pay", common_mistakes=["paying twice"])], version=3)
    check([v1, v2], v2, v3, AGENT)


# --------------------------------------------------------- rule 4: human verbs

def test_retire_is_refused_to_an_agent_and_allowed_to_a_human():
    with pytest.raises(RevisionPolicyError) as caught:
        require_human(AGENT, "sop_retire", because="Withdrawing a procedure ends it.")
    assert caught.value.rule == RULE_HUMAN_ONLY
    require_human(HUMAN, "sop_retire", because="Withdrawing a procedure ends it.")


def test_promote_is_the_same_verb_class():
    with pytest.raises(RevisionPolicyError) as caught:
        require_human(AGENT, "promote", because="Promotion opens the door.")
    assert caught.value.rule == RULE_HUMAN_ONLY


# ------------------------------------------------- who is human, and how it fails

def test_an_undeclared_registry_polices_everyone(monkeypatch):
    monkeypatch.delenv("AGENTCO_HUMANS", raising=False)
    assert humans_from_env() == frozenset()
    assert kind_of("dana", humans_from_env()) == AGENT


def test_a_declared_human_is_human_and_nobody_else_is(monkeypatch):
    monkeypatch.setenv("AGENTCO_HUMANS", "dana, sam")
    humans = humans_from_env()
    assert humans == {"dana", "sam"}
    assert kind_of("dana", humans) == HUMAN
    assert kind_of("some-agent", humans) == AGENT
    assert kind_of(None, humans) == AGENT              # unauthenticated: fail closed


def test_a_reviser_kind_that_is_neither_is_a_programming_error():
    v1 = record([step("pay")])
    with pytest.raises(ValueError, match="reviser_kind"):
        check([v1], v1, v1, "superuser")
