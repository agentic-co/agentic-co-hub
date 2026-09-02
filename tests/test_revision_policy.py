"""The revision policy — what an agent may not do to a procedure.

Three rules, each proven by the test that FAILS when the rule is removed from
`agentco/policy.py`. The mutants were run, not reasoned about; the commit
names them.

  * **protected** — a step carrying `money` or `irreversible` is frozen
    against agents, and no agent adds or removes those tags;
  * **ratchet** — an agent may make a step human, never the reverse;
  * **no-undo** — an agent may not move a field into a state a human moved
    it away from, until a human moves it back.

And the two properties that make the rules worth having: a refused revision
writes nothing, and `activate` is policed too — otherwise the rule an agent
cannot break by revising, it breaks by re-activating the version from before
the human's change.

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
from agentco.sop import SopContractError, SopError, SopStatus


def a_step(library, **over):
    body = {
        "purpose": "reproduce the defect before anyone touches a fix",
        "definition_of_done": "the failure is observed on a copy of the data",
    }
    body.update(over)
    return library.create("defect: reproduce", author="dana", author_kind=HUMAN, **body)


def by_human(library, sop_id, **change):
    return library.revise(sop_id, author="dana", author_kind=HUMAN, **change)


def by_agent(library, sop_id, **change):
    return library.revise(sop_id, author="fixer-bot", author_kind=AGENT, **change)


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
        by_agent(library, sop.sop_id, purpose="skip the reproduction, it is slow")
    refused(caught, RULE_PROTECTED)
    assert "['money']" in str(caught.value)


def test_a_human_revises_a_protected_step_freely(library):
    sop = a_step(library, tags=["money"])
    revised = by_human(library, sop.sop_id, purpose="a sharper purpose")
    assert revised.version == 2
    assert revised.tags == ["money"], "the tag carries forward"


def test_an_agent_cannot_add_or_remove_a_protected_tag(library):
    """Adding is refused too: an agent that could tag a step `money` could
    freeze it against every other agent, and only a human decides what is
    protected."""
    plain = a_step(library)
    with pytest.raises(RevisionPolicyError) as caught:
        by_agent(library, plain.sop_id, tags=["irreversible"])
    refused(caught, RULE_PROTECTED)
    assert "add or remove" in str(caught.value)

    # Removing is caught by the freeze on the baseline, and says so.
    protected = a_step(library, tags=["money", "slow"])
    with pytest.raises(RevisionPolicyError) as caught:
        by_agent(library, protected.sop_id, tags=["slow"])
    refused(caught, RULE_PROTECTED)


def test_an_agent_may_change_unprotected_tags(library):
    sop = a_step(library, tags=["slow"])
    revised = by_agent(library, sop.sop_id, tags=["slow", "flaky"])
    assert revised.tags == ["slow", "flaky"]


def test_tag_case_is_folded_so_money_has_one_spelling(library):
    """`Money` must not be a way past a rule written for `money`."""
    sop = a_step(library, tags=["Money"])
    assert sop.tags == ["money"]
    with pytest.raises(RevisionPolicyError):
        by_agent(library, sop.sop_id, purpose="x")


def test_a_registry_adds_protected_tags_and_cannot_remove_the_defaults(library):
    assert policy.protected_tags_from_env("pii, PHI") == DEFAULT_PROTECTED_TAGS | {"pii", "phi"}
    assert policy.protected_tags_from_env("") == DEFAULT_PROTECTED_TAGS

    library.protected_tags = library.protected_tags | {"pii"}
    sop = a_step(library, tags=["pii"])
    with pytest.raises(RevisionPolicyError) as caught:
        by_agent(library, sop.sop_id, purpose="x")
    refused(caught, RULE_PROTECTED)


# --------------------------------------------------------------------------- #
# rule 2 — ratchet
# --------------------------------------------------------------------------- #


def test_an_agent_may_make_a_step_human(library):
    sop = a_step(library, executor=AGENT)
    revised = by_agent(library, sop.sop_id, executor=HUMAN)
    assert revised.executor == HUMAN


def test_an_agent_cannot_make_a_human_step_an_agent_step(library):
    sop = a_step(library, executor=HUMAN)
    with pytest.raises(RevisionPolicyError) as caught:
        by_agent(library, sop.sop_id, executor=AGENT)
    refused(caught, RULE_RATCHET)


def test_an_agent_cannot_demote_by_omission(library):
    """Clearing the class is the same demotion with the label removed."""
    sop = a_step(library, executor=HUMAN)
    with pytest.raises(RevisionPolicyError) as caught:
        by_agent(library, sop.sop_id, executor=None)
    refused(caught, RULE_RATCHET)
    assert "unclassified" in str(caught.value)


def test_a_human_may_demote_a_step(library):
    sop = a_step(library, executor=HUMAN)
    revised = by_human(library, sop.sop_id, executor=AGENT)
    assert revised.executor == AGENT


def test_the_class_is_validated(library):
    with pytest.raises(SopContractError):
        a_step(library, executor="robot")


# --------------------------------------------------------------------------- #
# rule 3 — no undoing a human
# --------------------------------------------------------------------------- #


def test_an_agent_cannot_reintroduce_what_a_human_removed(library):
    sop = a_step(library, common_mistakes=["fixing before reproducing", "testing on prod"])
    by_human(library, sop.sop_id, common_mistakes=["testing on prod"])

    with pytest.raises(RevisionPolicyError) as caught:
        by_agent(library, sop.sop_id, common_mistakes=["testing on prod", "fixing before reproducing"])
    refused(caught, RULE_NO_UNDO)
    assert "put it back" in str(caught.value)

    # A different addition is not an undo.
    revised = by_agent(library, sop.sop_id, common_mistakes=["testing on prod", "skipping the log"])
    assert revised.version == 3


def test_an_agent_cannot_remove_what_a_human_added(library):
    sop = a_step(library)
    by_human(library, sop.sop_id, inputs="a copy of the customer's data, never the live row")
    with pytest.raises(RevisionPolicyError) as caught:
        by_agent(library, sop.sop_id, inputs=None)
    refused(caught, RULE_NO_UNDO)


def test_an_agent_cannot_restore_what_a_human_replaced(library):
    sop = a_step(library)  # purpose: "reproduce the defect before anyone touches a fix"
    by_human(library, sop.sop_id, purpose="reproduce the defect on a COPY before anyone touches a fix")
    with pytest.raises(RevisionPolicyError) as caught:
        by_agent(library, sop.sop_id, purpose="reproduce the defect before anyone touches a fix")
    refused(caught, RULE_NO_UNDO)

    # Moving it somewhere NEW is allowed — the rule is about not undoing
    # people, not about freezing the field.
    revised = by_agent(library, sop.sop_id, purpose="reproduce on a copy, then bisect")
    assert revised.version == 3


def test_a_later_human_change_lifts_the_ban(library):
    sop = a_step(library, common_mistakes=["fixing before reproducing"])
    by_human(library, sop.sop_id, common_mistakes=None)          # human removes it
    by_human(library, sop.sop_id, common_mistakes=["fixing before reproducing"])  # human restores it
    by_human(library, sop.sop_id, common_mistakes=None)          # and removes it again
    with pytest.raises(RevisionPolicyError):
        by_agent(library, sop.sop_id, common_mistakes=["fixing before reproducing"])

    by_human(library, sop.sop_id, common_mistakes=["fixing before reproducing"])
    # The human's LAST word is that it is present: the ban on re-adding is
    # lifted, and a ban on REMOVING it has taken its place.
    with pytest.raises(RevisionPolicyError) as caught:
        by_agent(library, sop.sop_id, common_mistakes=None)
    refused(caught, RULE_NO_UNDO)
    assert "may not remove it" in str(caught.value)
    unchanged = by_agent(library, sop.sop_id, inputs="the failing request id")
    assert unchanged.common_mistakes == ["fixing before reproducing"]


def test_a_human_moving_a_scalar_back_lifts_that_ban_too(library):
    """The lift is per state, for scalars as for lists. A human who changed
    the purpose A→B→C→A has, by their last word, un-forbidden A."""
    A = "reproduce the defect before anyone touches a fix"
    sop = a_step(library, purpose=A)
    by_human(library, sop.sop_id, purpose="B")
    by_human(library, sop.sop_id, purpose="C")
    by_human(library, sop.sop_id, purpose=A)
    by_agent(library, sop.sop_id, purpose="D")
    restored = by_agent(library, sop.sop_id, purpose=A)
    assert restored.purpose == A
    with pytest.raises(RevisionPolicyError):
        by_agent(library, sop.sop_id, purpose="B"), "B and C stay forbidden"


def test_a_human_re_adding_an_element_lifts_the_ban_on_adding_it(library):
    """Lists lift per element too. Once a human's last word is 'present', an
    agent re-adding it after a legacy (unattributed) removal is not undoing
    anyone — and a mutant that forgets to lift the ban would refuse it."""
    X = "fixing before reproducing"
    sop = a_step(library, common_mistakes=[X])
    by_human(library, sop.sop_id, common_mistakes=None)   # present is now forbidden
    by_human(library, sop.sop_id, common_mistakes=[X])    # ...and lifted; absent forbidden
    legacy = by_human(library, sop.sop_id, common_mistakes=None)
    with library._locked():                                # a pre-record version removed it
        rows = library._read_all()
        for row in rows:
            if row.sop_id == sop.sop_id and row.version == legacy.version:
                row.author_kind = None
        library._write_all(rows, library.quarantined)

    restored = by_agent(library, sop.sop_id, common_mistakes=[X])
    assert restored.common_mistakes == [X]


def test_an_agents_own_removal_binds_nobody(library):
    sop = a_step(library, common_mistakes=["fixing before reproducing"])
    by_agent(library, sop.sop_id, common_mistakes=None)
    revised = by_agent(library, sop.sop_id, common_mistakes=["fixing before reproducing"])
    assert revised.common_mistakes == ["fixing before reproducing"]


def test_unrecorded_authorship_imposes_nothing(library):
    """Versions from before authorship was recorded have `author_kind` None.
    The plane does not invent a human it never saw."""
    sop = library.create("legacy", purpose="p", common_mistakes=["m"])
    # Simulate a pre-record history: strip the kinds the library wrote.
    library.revise(sop.sop_id, common_mistakes=None)
    for version in library.history(sop.sop_id):
        version.author_kind = None
    with library._locked():
        rows = library._read_all()
        for row in rows:
            row.author_kind = None
        library._write_all(rows, library.quarantined)
    assert all(v.author_kind is None for v in library.history(sop.sop_id))

    revised = by_agent(library, sop.sop_id, common_mistakes=["m"])
    assert revised.common_mistakes == ["m"]


def test_the_title_is_a_field_too(library):
    sop = a_step(library)
    library.revise(sop.sop_id, title="defect: reproduce on a copy", author="dana", author_kind=HUMAN)
    with pytest.raises(RevisionPolicyError) as caught:
        library.revise(sop.sop_id, title="defect: reproduce", author="bot", author_kind=AGENT)
    refused(caught, RULE_NO_UNDO)


# --------------------------------------------------------------------------- #
# activate is policed, or the policy has a door beside it
# --------------------------------------------------------------------------- #


def test_an_agent_cannot_activate_around_the_policy(library):
    sop = a_step(library, common_mistakes=["fixing before reproducing"])
    library.activate(sop.sop_id, 1, author="dana", author_kind=HUMAN)
    by_human(library, sop.sop_id, common_mistakes=None)
    library.activate(sop.sop_id, 2, author="dana", author_kind=HUMAN)

    with pytest.raises(RevisionPolicyError) as caught:
        library.activate(sop.sop_id, 1, author="fixer-bot", author_kind=AGENT)
    refused(caught, RULE_NO_UNDO)
    assert library.get(sop.sop_id).version == 2, "the active version did not move"

    # A human may.
    back = library.activate(sop.sop_id, 1, author="dana", author_kind=HUMAN)
    assert back.status == SopStatus.ACTIVE


def test_an_agent_cannot_activate_a_protected_step(library):
    sop = a_step(library, tags=["irreversible"])
    with pytest.raises(RevisionPolicyError) as caught:
        library.activate(sop.sop_id, 1, author="bot", author_kind=AGENT)
    refused(caught, RULE_PROTECTED)
    assert "activate" in str(caught.value)


def test_an_agent_may_activate_its_own_lawful_revision(library):
    sop = a_step(library)
    library.activate(sop.sop_id, 1, author="dana", author_kind=HUMAN)
    revised = by_agent(library, sop.sop_id, inputs="the failing request id")
    active = library.activate(sop.sop_id, revised.version, author="fixer-bot", author_kind=AGENT)
    assert active.version == 2


def test_an_agent_cannot_activate_a_demotion(library):
    sop = a_step(library, executor=HUMAN)
    library.activate(sop.sop_id, 1, author="dana", author_kind=HUMAN)
    demoted = by_human(library, sop.sop_id, executor=AGENT)  # a human drafts it
    with pytest.raises(RevisionPolicyError) as caught:
        library.activate(sop.sop_id, demoted.version, author="bot", author_kind=AGENT)
    refused(caught, RULE_RATCHET)


# --------------------------------------------------------------------------- #
# a refused revision writes nothing
# --------------------------------------------------------------------------- #


def test_a_refused_revision_leaves_the_history_unchanged(library):
    sop = a_step(library, tags=["money"])
    before = [v.to_json() for v in library.history(sop.sop_id)]
    with pytest.raises(RevisionPolicyError):
        by_agent(library, sop.sop_id, purpose="x")
    assert [v.to_json() for v in library.history(sop.sop_id)] == before
    assert library.history(sop.sop_id)[-1].superseded_by is None


def test_a_refused_revision_leaves_the_file_byte_identical(tmp_path):
    from agentco.sop import SopLibrary

    library = SopLibrary(tmp_path / "sops.jsonl")
    sop = a_step(library, tags=["money"])
    before = (tmp_path / "sops.jsonl").read_bytes()
    with pytest.raises(RevisionPolicyError):
        by_agent(library, sop.sop_id, purpose="x")
    assert (tmp_path / "sops.jsonl").read_bytes() == before


# --------------------------------------------------------------------------- #
# the class and the tags are load-bearing
# --------------------------------------------------------------------------- #

HUMAN_GATE = {"kind": "human", "check": "the owner signs off in chat", "verifier": "dana",
              "max_park_seconds": 86400, "on_timeout": "escalate", "escalate_to": "dana"}
DETERMINISTIC_GATE = {"kind": "deterministic", "check": "pytest -q",
                      "max_park_seconds": 900, "on_timeout": "fail"}


def test_a_human_steps_instance_must_carry_a_human_gate(library, queue):
    sop = a_step(library, executor=HUMAN)
    library.activate(sop.sop_id, 1, author="dana", author_kind=HUMAN)

    with pytest.raises(SopError) as caught:
        library.instantiate(sop.sop_id, queue)
    assert "human step" in str(caught.value)
    with pytest.raises(SopError):
        library.instantiate(sop.sop_id, queue, verify=DETERMINISTIC_GATE)
    assert queue.list() == [], "nothing was filed"

    item = library.instantiate(sop.sop_id, queue, verify=HUMAN_GATE)
    assert item.verify["kind"] == "human"


def test_a_protected_steps_instance_must_carry_a_human_gate(library, queue):
    sop = a_step(library, tags=["irreversible"])
    library.activate(sop.sop_id, 1, author="dana", author_kind=HUMAN)
    with pytest.raises(SopError) as caught:
        library.instantiate(sop.sop_id, queue, verify=DETERMINISTIC_GATE)
    assert "['irreversible']" in str(caught.value)
    item = library.instantiate(sop.sop_id, queue, verify=HUMAN_GATE)
    assert item.metadata["sop_ref"]["version"] == 1


def test_an_unclassified_step_is_not_gated_by_the_library(library, queue):
    sop = a_step(library)
    library.activate(sop.sop_id, 1, author="dana", author_kind=HUMAN)
    item = library.instantiate(sop.sop_id, queue)
    assert item.verify is None


def test_authorship_is_recorded_and_defaults_to_agent(library):
    sop = a_step(library)
    assert (sop.author, sop.author_kind) == ("dana", HUMAN)
    anonymous = library.revise(sop.sop_id, inputs="x")
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
    created = _post(client, "/sops", "dana", {
        "title": "datafix: apply", "purpose": "apply the fix to the copy first",
        "tags": ["money"], "executor": "human",
    })
    assert created.status_code == 200, created.text
    sop = created.json()["sop"]
    assert sop["tags"] == ["money"] and sop["author_kind"] == HUMAN

    refused_ = _post(client, f"/sops/{sop['sop_id']}/revise", "fixer-bot", {"purpose": "skip the copy"})
    assert refused_.status_code == 403, refused_.text
    body = refused_.json()
    assert body["code"] == f"revision_policy:{RULE_PROTECTED}"
    assert "policy rule 'protected'" in body["message"]

    allowed = _post(client, f"/sops/{sop['sop_id']}/revise", "dana", {"purpose": "apply to the copy, then verify"})
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["sop"]["version"] == 2

    activation = _post(client, f"/sops/{sop['sop_id']}/activate", "fixer-bot", {"version": 2})
    assert activation.status_code == 403
    assert activation.json()["code"] == f"revision_policy:{RULE_PROTECTED}"


def test_over_http_an_undeclared_registry_polices_everyone(tmp_path):
    """Fail closed: no `humans` means no humans, and dana is an agent too."""
    client = _client(tmp_path)
    sop = _post(client, "/sops", "dana", {"title": "t", "purpose": "p", "tags": ["irreversible"]}).json()["sop"]
    assert sop["author_kind"] == AGENT
    response = _post(client, f"/sops/{sop['sop_id']}/revise", "dana", {"purpose": "q"})
    assert response.status_code == 403


def test_over_http_a_human_step_is_instantiated_with_its_gate(tmp_path):
    """The gate travels through the endpoint. Before it did, no instance filed
    over HTTP could be gated at all — and a human step could never be filed."""
    client = _client(tmp_path, humans=["dana"])
    sop = _post(client, "/sops", "dana", {"title": "review", "purpose": "a person reads it", "executor": "human"}).json()["sop"]
    assert _post(client, f"/sops/{sop['sop_id']}/activate", "dana", {"version": 1}).status_code == 200

    bare = _post(client, f"/sops/{sop['sop_id']}/instantiate", "dana", {})
    assert bare.status_code == 422
    assert "human step" in bare.json()["message"]

    gated = _post(client, f"/sops/{sop['sop_id']}/instantiate", "dana", {"verify": HUMAN_GATE})
    assert gated.status_code == 200, gated.text
    assert gated.json()["item"]["verify"]["kind"] == "human"


def test_over_http_the_body_cannot_declare_the_kind(tmp_path):
    """`author_kind` in the payload is not a body key. A caller cannot become
    human by saying so."""
    client = _client(tmp_path)
    created = _post(client, "/sops", "fixer-bot", {"title": "t", "purpose": "p", "author_kind": "human", "author": "dana"})
    assert created.status_code == 200, created.text
    assert created.json()["sop"]["author_kind"] == AGENT
    assert created.json()["sop"]["author"] == "fixer-bot"
