"""The revision policy — what an agent may not do to a procedure.

Four rules, each proven by the test that FAILS when the rule is removed from
`agentco/policy.py`. The mutants were run, not reasoned about; the commit
names them.

  * **protected** — a step carrying `money` or `irreversible` is frozen
    against agents, and no agent adds or removes those tags;
  * **ratchet** — an agent may make a step human, never the reverse, and may
    not delete a human step either (deleting is the demotion that leaves
    nothing behind to be classified);
  * **no-undo** — an agent may not move a field into a state a human moved
    it away from, until a human moves it back;
  * **human_only** — `retire` and `promote` are human verbs regardless of
    what they would produce (covered in `tests/test_asop_v3.py`, not here).

And the two properties that make the rules worth having: a refused revision
writes nothing, and `activate` is policed too — otherwise the rule an agent
cannot break by revising, it breaks by re-activating the version from before
the human's change.

**ASOP v3.** The record used to be one versioned procedure; it is now a
versioned SEQUENCE of steps, and the gate is authored on the step with the
version rather than supplied by whoever files the work. Every rule below
still means what it meant in v2 — it now ranges over a step, keyed by NAME,
inside the sequence (`agentco.policy.check_asop_revision`,
`agentco.policy.steps_by_key`). What v2 called "the SOP" — one purpose, one
`definition_of_done`, one `executor`, one set of `tags` — is what this file's
`a_step()` fixture below builds as a single-step ASOP, so most of the tests
port with the field simply moved one level down, onto `steps[0]`. `executor`
is no longer a field on the record at all: a step's class is read off
*either* its role's declared `kind` or its own gate's `kind`
(`agentco.policy._step_class` / `_role_class`) — a human step carries a
human gate by construction (ASOP.md §3.6), so `a_step(..., executor=HUMAN)`
below sets both together.

Every conformance test here runs on both backends through the `library`
fixture. Who is human is DECLARED to the library's callers (`author_kind`),
never inferred: these tests say so explicitly on every call, which is also
how the HTTP layer says it.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentco import auth, policy
from agentco.app import create_app
from agentco.policy import (
    AGENT,
    DEFAULT_PROTECTED_TAGS,
    HUMAN,
    RULE_NO_UNDO,
    RULE_PROTECTED,
    RULE_RATCHET,
    RevisionPolicyError,
)
from agentco.sop import SopContractError, SopError, SopStatus, step_payload
from agentco.work import Queue

DETERMINISTIC_GATE = {"kind": "deterministic", "check": "pytest -q",
                      "max_park_seconds": 900, "on_timeout": "fail"}
HUMAN_GATE = {"kind": "human", "check": "the owner signs off in chat", "verifier": "dana",
              "max_park_seconds": 86400, "on_timeout": "escalate", "escalate_to": "dana"}
JUDGED_GATE = {"kind": "judged", "check": "a reviewer reads the diff",
               "max_park_seconds": 900, "on_timeout": "escalate", "escalate_to": "dana"}
PROTECTED = frozenset({"money", "irreversible"})


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def a_body(*, executor=AGENT, **over) -> dict:
    """The BODY of a single-step ASOP, without filing it.

    Split out of `a_step` so a test can hand a deliberately-wrong shape to
    `create()` and watch it refuse — `a_step` files, which is exactly what
    those tests are asserting cannot happen.
    """
    step_keys = {"name", "role", "purpose", "entry_check", "inputs", "definition_of_done",
                 "validation", "write_back", "common_mistakes", "tags", "gate", "after"}
    # The gate follows from what the step IS, in both directions the record
    # now enforces: a human ROLE carries a human gate (§3.6), and so does a
    # step carrying a protected TAG (§6.4) — `money` means a person looks
    # before it counts as done, and a deterministic gate is nobody looking.
    protected = frozenset(t.lower() for t in over.get("tags") or ()) & PROTECTED
    step = {
        "name": "reproduce",
        "role": "implementer",
        "purpose": "reproduce the defect before anyone touches a fix",
        "definition_of_done": "the failure is observed on a copy of the data",
        "gate": HUMAN_GATE if (executor == HUMAN or protected) else DETERMINISTIC_GATE,
    }
    for key in list(over):
        if key in step_keys:
            step[key] = over.pop(key)
    body = {"roles": {"implementer": {"kind": executor}}, "steps": [step]}
    body.update(over)
    return body


def a_step(library, *, executor=AGENT, **over):
    """A single-step ASOP: 'defect: reproduce'. `executor` sets the class of
    the one step, `implementer` — the role's `kind` and, since a human role
    carries a human gate by construction, the step's gate too. Every other
    v2 field (`purpose`, `tags`, `common_mistakes`, `inputs`, ...) targets
    the step directly, same as it targeted the whole record in v2."""
    return library.create("defect: reproduce", author="dana", author_kind=HUMAN,
                          **a_body(executor=executor, **over))


def _two_step(library, *, second_kind=AGENT, **over):
    """Two steps — 'reproduce' (always agent) and 'sign-off' (kind
    configurable) — for the ratchet tests that need a step to remove,
    promote or demote without leaving the sequence empty. A single-step
    ASOP can't stand in for those: `steps` may never be emptied outright
    (ASOP.md §3.1), so the demotion-by-deletion case needs a second step to
    survive the deletion of the first."""
    body = {
        "roles": {"implementer": {"kind": AGENT}, "owner": {"kind": second_kind}},
        "constraints": [],
        "steps": [
            {"name": "reproduce", "role": "implementer",
             "purpose": "reproduce the defect before anyone touches a fix",
             "definition_of_done": "the failure is observed on a copy of the data",
             "gate": DETERMINISTIC_GATE},
            {"name": "sign-off", "role": "owner", "purpose": "approve the fix",
             "gate": HUMAN_GATE if second_kind == HUMAN else DETERMINISTIC_GATE},
        ],
    }
    body.update(over)
    return library.create("defect: reproduce", author="dana", author_kind=HUMAN, **body)


def by_human(library, asop_id, **change):
    return _revise_step(library, asop_id, author="dana", author_kind=HUMAN, **change)


def by_agent(library, asop_id, **change):
    return _revise_step(library, asop_id, author="fixer-bot", author_kind=AGENT, **change)


def _revise_step(library, asop_id, *, author, author_kind, **change):
    """The v3 shape of v2's flat `library.revise(sop_id, field=value)`: a
    step's text lives inside `steps` now, so revising one field means
    reading the current step, merging `change` onto it, and sending the
    WHOLE `steps` list back — exactly what the MCP/HTTP surface does too
    (`sop_revise`'s `changes` takes the whole list)."""
    latest = library.history(asop_id)[-1]
    step = step_payload(latest.steps[0])
    if "executor" in change:
        executor = change.pop("executor")
        if executor is None:
            # v3 has no 'unclassified' state a step can be cleared into — see
            # test_an_agent_cannot_demote_by_omission below, which exercises
            # the nearest equivalent (deleting the step) on a two-step ASOP
            # instead of going through this helper.
            raise ValueError("executor=None has no v3 equivalent through _revise_step")
        step["gate"] = HUMAN_GATE if executor == HUMAN else DETERMINISTIC_GATE
        return library.revise(asop_id, steps=[step], roles={step["role"]: {"kind": executor}},
                              author=author, author_kind=author_kind)
    step.update(change)
    return library.revise(asop_id, steps=[step], author=author, author_kind=author_kind)


def refused(exc_info, rule: str) -> None:
    assert exc_info.value.rule == rule
    assert f"policy rule '{rule}'" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# rule 1 — protected
# --------------------------------------------------------------------------- #


def test_an_agent_cannot_revise_a_protected_step(library):
    """The whole step is frozen, not just the tag: 'not allowed to change
    anything related to money' means anything."""
    sop = a_step(library, tags=["money"])
    with pytest.raises(RevisionPolicyError) as caught:
        by_agent(library, sop.asop_id, purpose="skip the reproduction, it is slow")
    refused(caught, RULE_PROTECTED)
    assert "['money']" in str(caught.value)


def test_a_human_revises_a_protected_step_freely(library):
    sop = a_step(library, tags=["money"])
    revised = by_human(library, sop.asop_id, purpose="a sharper purpose")
    assert revised.version == 2
    assert revised.steps[0].tags == ["money"], "the tag carries forward"


def test_an_agent_cannot_add_or_remove_a_protected_tag(library):
    """Adding is refused too: an agent that could tag a step `money` could
    freeze it against every other agent, and only a human decides what is
    protected."""
    plain = a_step(library)
    # The proposal carries a human gate alongside the tag, so it is a WELL-FORMED
    # record — the record's own rule (a protected step is human-gated) would
    # otherwise refuse it first, and this test would be asserting the contract
    # rather than the policy. What is refused here is who asked.
    with pytest.raises(RevisionPolicyError) as caught:
        by_agent(library, plain.asop_id, tags=["irreversible"], gate=HUMAN_GATE)
    refused(caught, RULE_PROTECTED)
    assert "add or remove" in str(caught.value)

    # Removing is caught by the freeze on the baseline, and says so.
    protected = a_step(library, tags=["money", "slow"])
    with pytest.raises(RevisionPolicyError) as caught:
        by_agent(library, protected.asop_id, tags=["slow"])
    refused(caught, RULE_PROTECTED)


def test_an_agent_may_change_unprotected_tags(library):
    sop = a_step(library, tags=["slow"])
    revised = by_agent(library, sop.asop_id, tags=["slow", "flaky"])
    assert revised.steps[0].tags == ["slow", "flaky"]


def test_tag_case_is_folded_so_money_has_one_spelling(library):
    """`Money` must not be a way past a rule written for `money`."""
    sop = a_step(library, tags=["Money"])
    assert sop.steps[0].tags == ["money"]
    with pytest.raises(RevisionPolicyError):
        by_agent(library, sop.asop_id, purpose="x")


def test_a_registry_adds_protected_tags_and_cannot_remove_the_defaults(library):
    assert policy.protected_tags_from_env("pii, PHI") == DEFAULT_PROTECTED_TAGS | {"pii", "phi"}
    assert policy.protected_tags_from_env("") == DEFAULT_PROTECTED_TAGS

    library.protected_tags = library.protected_tags | {"pii"}
    # Human-gated for the same reason `money` is, and refused by the PLANE
    # rather than the record: `pii` is a name the contract was never told.
    sop = a_step(library, tags=["pii"], gate=HUMAN_GATE)
    with pytest.raises(RevisionPolicyError) as caught:
        by_agent(library, sop.asop_id, purpose="x")
    refused(caught, RULE_PROTECTED)


# --------------------------------------------------------------------------- #
# rule 2 — ratchet
# --------------------------------------------------------------------------- #


def test_an_agent_may_make_a_step_human(library):
    asop = _two_step(library, second_kind=AGENT)
    steps = [step_payload(asop.steps[0]),
             {**step_payload(asop.steps[1]), "gate": HUMAN_GATE}]
    revised = library.revise(
        asop.asop_id, steps=steps,
        roles={"implementer": {"kind": AGENT}, "owner": {"kind": HUMAN}},
        author="fixer-bot", author_kind=AGENT,
    )
    assert revised.roles["owner"]["kind"] == HUMAN
    assert revised.steps[1].gate["kind"] == "human"


def test_an_agent_cannot_make_a_human_step_an_agent_step(library):
    asop = _two_step(library, second_kind=HUMAN)
    steps = [step_payload(asop.steps[0]),
             {**step_payload(asop.steps[1]), "role": "implementer", "gate": DETERMINISTIC_GATE}]
    with pytest.raises(RevisionPolicyError) as caught:
        library.revise(asop.asop_id, steps=steps, author="fixer-bot", author_kind=AGENT)
    refused(caught, RULE_RATCHET)


def test_an_agent_cannot_demote_by_omission(library):
    """v3 has no 'unclassified' state a step falls into when its
    classification is cleared — omitting the class outright means omitting
    the step itself, and that is what the ratchet refuses here: deleting a
    human step is the one demotion that leaves nothing behind to be
    classified."""
    asop = _two_step(library, second_kind=HUMAN)
    remaining = [step_payload(asop.steps[0])]
    with pytest.raises(RevisionPolicyError) as caught:
        library.revise(asop.asop_id, steps=remaining, author="fixer-bot", author_kind=AGENT)
    refused(caught, RULE_RATCHET)
    assert "nothing behind to be classified" in str(caught.value)


def test_a_human_may_demote_a_step(library):
    asop = _two_step(library, second_kind=HUMAN)
    steps = [step_payload(asop.steps[0]),
             {**step_payload(asop.steps[1]), "role": "implementer", "gate": DETERMINISTIC_GATE}]
    revised = library.revise(
        asop.asop_id, steps=steps,
        roles={"implementer": {"kind": AGENT}, "owner": {"kind": HUMAN}},
        author="dana", author_kind=HUMAN,
    )
    assert revised.steps[1].role == "implementer"


def test_the_class_is_validated(library):
    with pytest.raises(SopContractError):
        a_step(library, executor="robot")


# --------------------------------------------------------------------------- #
# rule 3 — no undoing a human
# --------------------------------------------------------------------------- #


def test_an_agent_cannot_reintroduce_what_a_human_removed(library):
    sop = a_step(library, common_mistakes=["fixing before reproducing", "testing on prod"])
    by_human(library, sop.asop_id, common_mistakes=["testing on prod"])

    with pytest.raises(RevisionPolicyError) as caught:
        by_agent(library, sop.asop_id, common_mistakes=["testing on prod", "fixing before reproducing"])
    refused(caught, RULE_NO_UNDO)
    assert "put it back" in str(caught.value)

    # A different addition is not an undo.
    revised = by_agent(library, sop.asop_id, common_mistakes=["testing on prod", "skipping the log"])
    assert revised.version == 3


def test_an_agent_cannot_remove_what_a_human_added(library):
    sop = a_step(library)
    by_human(library, sop.asop_id, inputs="a copy of the customer's data, never the live row")
    with pytest.raises(RevisionPolicyError) as caught:
        by_agent(library, sop.asop_id, inputs=None)
    refused(caught, RULE_NO_UNDO)


def test_an_agent_cannot_restore_what_a_human_replaced(library):
    sop = a_step(library)  # purpose: "reproduce the defect before anyone touches a fix"
    by_human(library, sop.asop_id, purpose="reproduce the defect on a COPY before anyone touches a fix")
    with pytest.raises(RevisionPolicyError) as caught:
        by_agent(library, sop.asop_id, purpose="reproduce the defect before anyone touches a fix")
    refused(caught, RULE_NO_UNDO)

    # Moving it somewhere NEW is allowed — the rule is about not undoing
    # people, not about freezing the field.
    revised = by_agent(library, sop.asop_id, purpose="reproduce on a copy, then bisect")
    assert revised.version == 3


def test_a_later_human_change_lifts_the_ban(library):
    sop = a_step(library, common_mistakes=["fixing before reproducing"])
    by_human(library, sop.asop_id, common_mistakes=None)          # human removes it
    by_human(library, sop.asop_id, common_mistakes=["fixing before reproducing"])  # human restores it
    by_human(library, sop.asop_id, common_mistakes=None)          # and removes it again
    with pytest.raises(RevisionPolicyError):
        by_agent(library, sop.asop_id, common_mistakes=["fixing before reproducing"])

    by_human(library, sop.asop_id, common_mistakes=["fixing before reproducing"])
    # The human's LAST word is that it is present: the ban on re-adding is
    # lifted, and a ban on REMOVING it has taken its place.
    with pytest.raises(RevisionPolicyError) as caught:
        by_agent(library, sop.asop_id, common_mistakes=None)
    refused(caught, RULE_NO_UNDO)
    assert "may not remove it" in str(caught.value)
    unchanged = by_agent(library, sop.asop_id, inputs="the failing request id")
    assert unchanged.steps[0].common_mistakes == ["fixing before reproducing"]


def test_a_human_moving_a_scalar_back_lifts_that_ban_too(library):
    """The lift is per state, for scalars as for lists. A human who changed
    the purpose A→B→C→A has, by their last word, un-forbidden A."""
    A = "reproduce the defect before anyone touches a fix"
    sop = a_step(library, purpose=A)
    by_human(library, sop.asop_id, purpose="B")
    by_human(library, sop.asop_id, purpose="C")
    by_human(library, sop.asop_id, purpose=A)
    by_agent(library, sop.asop_id, purpose="D")
    restored = by_agent(library, sop.asop_id, purpose=A)
    assert restored.steps[0].purpose == A
    with pytest.raises(RevisionPolicyError):
        by_agent(library, sop.asop_id, purpose="B"), "B and C stay forbidden"


def test_a_human_re_adding_an_element_lifts_the_ban_on_adding_it(library):
    """Lists lift per element too. Once a human's last word is 'present', an
    agent re-adding it after a legacy (unattributed) removal is not undoing
    anyone — and a mutant that forgets to lift the ban would refuse it."""
    X = "fixing before reproducing"
    sop = a_step(library, common_mistakes=[X])
    by_human(library, sop.asop_id, common_mistakes=None)   # present is now forbidden
    by_human(library, sop.asop_id, common_mistakes=[X])    # ...and lifted; absent forbidden
    legacy = by_human(library, sop.asop_id, common_mistakes=None)
    with library._locked():                                # a pre-record version removed it
        rows = library._read_all()
        for row in rows:
            if row.asop_id == sop.asop_id and row.version == legacy.version:
                row.author_kind = None
        library._write_all(rows, library.quarantined)

    restored = by_agent(library, sop.asop_id, common_mistakes=[X])
    assert restored.steps[0].common_mistakes == [X]


def test_an_agents_own_removal_binds_nobody(library):
    sop = a_step(library, common_mistakes=["fixing before reproducing"])
    by_agent(library, sop.asop_id, common_mistakes=None)
    revised = by_agent(library, sop.asop_id, common_mistakes=["fixing before reproducing"])
    assert revised.steps[0].common_mistakes == ["fixing before reproducing"]


def test_unrecorded_authorship_imposes_nothing(library):
    """Versions from before authorship was recorded have `author_kind` None.
    The plane does not invent a human it never saw."""
    sop = a_step(library, common_mistakes=["m"])
    step = step_payload(sop.steps[0])
    step["common_mistakes"] = None
    # Simulate a pre-record history: strip the kinds the library wrote.
    library.revise(sop.asop_id, steps=[step])
    for version in library.history(sop.asop_id):
        version.author_kind = None
    with library._locked():
        rows = library._read_all()
        for row in rows:
            if row.asop_id == sop.asop_id:
                row.author_kind = None
        library._write_all(rows, library.quarantined)
    assert all(v.author_kind is None for v in library.history(sop.asop_id))

    revised = by_agent(library, sop.asop_id, common_mistakes=["m"])
    assert revised.steps[0].common_mistakes == ["m"]


def test_the_title_is_a_field_too(library):
    """`title` lives on the ASOP record, not the step — no-undo covers it
    the same way it covers a step's fields, just diffed at the record
    level (`agentco.policy.ASOP_SCALAR_FIELDS`)."""
    sop = a_step(library)
    library.revise(sop.asop_id, title="defect: reproduce on a copy", author="dana", author_kind=HUMAN)
    with pytest.raises(RevisionPolicyError) as caught:
        library.revise(sop.asop_id, title="defect: reproduce", author="bot", author_kind=AGENT)
    refused(caught, RULE_NO_UNDO)


# --------------------------------------------------------------------------- #
# activate is policed, or the policy has a door beside it
# --------------------------------------------------------------------------- #


def test_an_agent_cannot_activate_around_the_policy(library):
    sop = a_step(library, common_mistakes=["fixing before reproducing"])
    library.activate(sop.asop_id, 1, author="dana", author_kind=HUMAN)
    by_human(library, sop.asop_id, common_mistakes=None)
    library.activate(sop.asop_id, 2, author="dana", author_kind=HUMAN)

    with pytest.raises(RevisionPolicyError) as caught:
        library.activate(sop.asop_id, 1, author="fixer-bot", author_kind=AGENT)
    refused(caught, RULE_NO_UNDO)
    assert library.get(sop.asop_id).version == 2, "the active version did not move"

    # A human may.
    back = library.activate(sop.asop_id, 1, author="dana", author_kind=HUMAN)
    assert back.status == SopStatus.ACTIVE


def test_an_agent_cannot_activate_a_protected_step(library):
    """`activate` is policed against the same baseline `revise` would use —
    whichever version is (or would become) active. Activating a version
    whose protected step differs from that baseline is refused exactly like
    revising it would be: an agent forbidden from touching a protected step
    directly must not get there by re-activating a version that already did."""
    protected = a_step(library, tags=["irreversible"])
    library.activate(protected.asop_id, 1, author="dana", author_kind=HUMAN)
    revised = by_human(library, protected.asop_id, purpose="a sharper purpose")
    with pytest.raises(RevisionPolicyError) as caught:
        library.activate(protected.asop_id, revised.version, author="bot", author_kind=AGENT)
    refused(caught, RULE_PROTECTED)
    assert "activate" in str(caught.value)


def test_an_agent_may_activate_its_own_lawful_revision(library):
    sop = a_step(library)
    library.activate(sop.asop_id, 1, author="dana", author_kind=HUMAN)
    revised = by_agent(library, sop.asop_id, inputs="the failing request id")
    active = library.activate(sop.asop_id, revised.version, author="fixer-bot", author_kind=AGENT)
    assert active.version == 2


def test_an_agent_cannot_activate_a_demotion(library):
    sop = a_step(library, executor=HUMAN)
    library.activate(sop.asop_id, 1, author="dana", author_kind=HUMAN)
    demoted = by_human(library, sop.asop_id, executor=AGENT)  # a human drafts it
    with pytest.raises(RevisionPolicyError) as caught:
        library.activate(sop.asop_id, demoted.version, author="bot", author_kind=AGENT)
    refused(caught, RULE_RATCHET)


# --------------------------------------------------------------------------- #
# a refused revision writes nothing
# --------------------------------------------------------------------------- #


def test_a_refused_revision_leaves_the_history_unchanged(library):
    sop = a_step(library, tags=["money"])
    before = [v.to_json() for v in library.history(sop.asop_id)]
    with pytest.raises(RevisionPolicyError):
        by_agent(library, sop.asop_id, purpose="x")
    assert [v.to_json() for v in library.history(sop.asop_id)] == before
    assert library.history(sop.asop_id)[-1].superseded_by is None


def test_a_refused_revision_leaves_the_file_byte_identical(tmp_path):
    from agentco.sop import SopLibrary

    library = SopLibrary(tmp_path / "sops.jsonl")
    sop = a_step(library, tags=["money"])
    before = (tmp_path / "sops.jsonl").read_bytes()
    with pytest.raises(RevisionPolicyError):
        by_agent(library, sop.asop_id, purpose="x")
    assert (tmp_path / "sops.jsonl").read_bytes() == before


# --------------------------------------------------------------------------- #
# the class and the gate it carries are load-bearing
# --------------------------------------------------------------------------- #


def test_a_human_roles_step_must_carry_a_human_gate(library):
    """v2 enforced this when work was INSTANTIATED from the template
    (`instantiate()`, gone in v3). v3 enforces the same guarantee earlier,
    at AUTHORING, because the gate now travels WITH the step rather than
    being supplied by whoever files the work (ASOP.md §3.6): 'a human
    role's steps carry human gates by construction'."""
    with pytest.raises(SopContractError) as caught:
        a_step(library, executor=HUMAN, gate=DETERMINISTIC_GATE)
    assert "human" in str(caught.value)

    sop = a_step(library, executor=HUMAN)
    assert sop.steps[0].gate["kind"] == "human"


def test_a_protected_step_must_be_gated_by_a_person(library):
    """v2's filing rule, restored where the gate is authored.

    v2 refused to INSTANTIATE a `money`/`irreversible` step without a human
    gate. The first v3 port lost that half — the human-gate requirement was
    tied to a step's ROLE kind only — so a `money` step with a deterministic
    gate was accepted, and the only thing between it and an unattended close
    was who activated the version. The record refuses it now, at authoring,
    which is where the gate is written.
    """
    body = a_body(tags=["money"], gate=DETERMINISTIC_GATE)
    with pytest.raises(SopContractError) as caught:
        library.create("defect", **body)
    assert "money" in str(caught.value)
    assert "human" in str(caught.value)

    # Judged is not a substitute: a route is not a person.
    with pytest.raises(SopContractError):
        library.create("defect", **a_body(tags=["irreversible"], gate=JUDGED_GATE))

    # And the honest shape is accepted.
    sop = library.create("defect", **a_body(tags=["money"], gate=HUMAN_GATE))
    assert sop.steps[0].gate["kind"] == "human"


def test_a_registry_enforces_the_protected_tags_it_added_itself(library):
    """The record enforces the DEFAULT set, because those are the two names it
    knows. A registry may add its own through `AGENTCO_PROTECTED_TAGS`, and a
    set the contract was never told is this registry's to enforce — otherwise
    an added tag freezes a step against agent edits while still letting anyone
    author it closable by nobody, which is the half that matters."""
    library.protected_tags = frozenset({"money", "irreversible", "payroll"})
    with pytest.raises(SopContractError) as caught:
        library.create("payroll run", **a_body(tags=["payroll"], gate=DETERMINISTIC_GATE))
    assert "payroll" in str(caught.value)
    assert library.create("payroll run", **a_body(tags=["payroll"], gate=HUMAN_GATE))


# v2 had one more test here:
#
#   * an "unclassified" step (no `executor` set) could be instantiated with
#     `verify=None`. v3 has no such state: `gate` is a required field on
#     every step from the moment it is authored
#     (`tests/test_asop_v3.py::test_a_step_without_a_gate_is_refused`
#     covers the create-time refusal), so there is nothing left to port —
#     the behaviour is gone by construction, not merely renamed.


def test_authorship_is_recorded_and_defaults_to_agent(library):
    sop = a_step(library)
    assert (sop.author, sop.author_kind) == ("dana", HUMAN)
    anonymous = library.revise(sop.asop_id, task_type="triage")
    assert anonymous.author is None
    assert anonymous.author_kind == AGENT, "an unstated kind fails closed"


def test_who_is_human_is_declared_never_inferred():
    assert policy.kind_of("dana", {"dana"}) == HUMAN
    assert policy.kind_of("dana", set()) == AGENT
    assert policy.kind_of("Dana", {"dana"}) == AGENT, "exact spelling — the key file has one"
    assert policy.kind_of(None, {"dana"}) == AGENT
    assert policy.humans_from_env("dana, kofi") == {"dana", "kofi"}
    assert policy.humans_from_env("") == frozenset()


# --------------------------------------------------------------------------- #
# over HTTP — the actor is the signature, the kind is the operator's declaration
# --------------------------------------------------------------------------- #

KEYS = {"dana": "dana-secret", "fixer-bot": "bot-secret", "operator": "op-secret"}


def _client(tmp_path, **over):
    app = create_app(
        db_path=str(tmp_path / "api.sqlite3"),
        keys=KEYS,
        operator="operator",
        work_store=str(tmp_path / "work.jsonl"),
        sop_store=str(tmp_path / "sops.jsonl"),
        **over,
    )
    return TestClient(app)


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


def test_over_http_the_declared_human_passes_and_the_agent_is_refused(tmp_path):
    client = _client(tmp_path, humans=["dana"])
    step = {"name": "apply", "role": "implementer", "purpose": "apply the fix to the copy first",
            "definition_of_done": "the copy reflects the fix", "tags": ["money"],
            "gate": HUMAN_GATE}
    created = _post(client, "/sops", "dana", {
        "title": "datafix: apply",
        "roles": {"implementer": {"kind": "agent"}},
        "steps": [step],
    })
    assert created.status_code == 200, created.text
    sop = created.json()["sop"]
    assert sop["steps"][0]["tags"] == ["money"] and sop["author_kind"] == HUMAN
    assert _post(client, f"/sops/{sop['asop_id']}/activate", "dana", {"version": 1}).status_code == 200

    refused_ = _post(client, f"/sops/{sop['asop_id']}/revise", "fixer-bot",
                     {"steps": [{**step, "purpose": "skip the copy"}]})
    assert refused_.status_code == 403, refused_.text
    body = refused_.json()
    assert body["code"] == f"revision_policy:{RULE_PROTECTED}"
    assert "policy rule 'protected'" in body["message"]

    allowed = _post(client, f"/sops/{sop['asop_id']}/revise", "dana",
                    {"steps": [{**step, "purpose": "apply to the copy, then verify"}]})
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["sop"]["version"] == 2

    activation = _post(client, f"/sops/{sop['asop_id']}/activate", "fixer-bot", {"version": 2})
    assert activation.status_code == 403
    assert activation.json()["code"] == f"revision_policy:{RULE_PROTECTED}"


def test_over_http_an_undeclared_registry_polices_everyone(tmp_path):
    """Fail closed: no `humans` means no humans, and dana is an agent too."""
    client = _client(tmp_path)
    step = {"name": "apply", "role": "implementer", "purpose": "p", "definition_of_done": "done",
            "tags": ["irreversible"], "gate": HUMAN_GATE}
    sop = _post(client, "/sops", "dana", {
        "title": "t", "roles": {"implementer": {"kind": "agent"}}, "steps": [step],
    }).json()["sop"]
    assert sop["author_kind"] == AGENT
    response = _post(client, f"/sops/{sop['asop_id']}/revise", "dana",
                     {"steps": [{**step, "purpose": "q"}]})
    assert response.status_code == 403


def test_over_http_a_human_step_is_instantiated_with_its_gate(tmp_path):
    """v2: the gate travelled through the `instantiate` endpoint, and a
    caller who omitted it could not file a human step at all. v3: the gate
    travels with the STEP, authored at creation, so a run of a human-role
    step always carries a human gate — there is no bare-filing path that
    could omit one, and the endpoint refuses a caller who tries to supply
    one at all (`POST /sops/{id}/run` refuses `verify`/`gate` in the body,
    exactly the failure v3 exists to close)."""
    client = _client(tmp_path, humans=["dana"])
    step = {"name": "review", "role": "reviewer", "purpose": "a person reads it",
            "definition_of_done": "the review is on record", "gate": HUMAN_GATE}
    sop = _post(client, "/sops", "dana", {
        "title": "review", "roles": {"reviewer": {"kind": "human"}}, "steps": [step],
    }).json()["sop"]
    assert _post(client, f"/sops/{sop['asop_id']}/activate", "dana", {"version": 1}).status_code == 200

    # Bound to someone other than the gate's own verifier ('dana') — a human
    # gate refuses to resolve on the party it exists to exclude — and to an
    # actor this registry actually has a key for, because a binding it cannot
    # authenticate can never pull the bead it is bound to.
    bare = _post(client, f"/sops/{sop['asop_id']}/run", "dana",
                {"bindings": {"reviewer": "fixer-bot"}, "verify": DETERMINISTIC_GATE})
    assert bare.status_code == 422
    assert "the gate belongs to the step" in bare.json()["message"]

    stranger = _post(client, f"/sops/{sop['asop_id']}/run", "dana",
                     {"bindings": {"reviewer": "carol"}})
    assert stranger.status_code == 409, stranger.text
    assert stranger.json()["code"] == "role_unbound"

    filed = _post(client, f"/sops/{sop['asop_id']}/run", "dana",
                  {"bindings": {"reviewer": "fixer-bot"}})
    assert filed.status_code == 200, filed.text
    item_id = filed.json()["run"]["steps"][0]["itemId"]
    item = Queue(tmp_path / "work.jsonl").get(item_id)
    assert item.verify["kind"] == "human"


def test_over_http_the_body_cannot_declare_the_kind(tmp_path):
    """`author_kind` in the payload is refused, not ignored. A caller cannot
    become human by saying so — and must not believe it did."""
    client = _client(tmp_path)
    for body in ({"author_kind": "human"}, {"author": "dana"}):
        created = _post(client, "/sops", "fixer-bot", {"title": "t", "purpose": "p", **body})
        assert created.status_code == 400, created.text
        assert created.json()["code"] == "author_from_signature"

    step = {"name": "apply", "role": "implementer", "purpose": "p", "definition_of_done": "done",
            "gate": DETERMINISTIC_GATE}
    created = _post(client, "/sops", "fixer-bot", {
        "title": "t", "roles": {"implementer": {"kind": "agent"}}, "steps": [step],
    })
    sop = created.json()["sop"]
    assert (sop["author"], sop["author_kind"]) == ("fixer-bot", AGENT)
    for path in (f"/sops/{sop['asop_id']}/revise", f"/sops/{sop['asop_id']}/activate"):
        refused_ = _post(client, path, "fixer-bot", {"purpose": "q", "version": 1, "author_kind": "human"})
        assert refused_.status_code == 400, (path, refused_.text)
