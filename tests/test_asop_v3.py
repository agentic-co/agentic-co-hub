"""ASOP v3 on the plane: the sequence is the artefact, the step is the unit.

Every test here fails against the v2 shape by construction — v2 had no steps,
no roles, no gate on the record, and filed one work item per procedure. What
is defended:

  * **the gate is authored with the version, not supplied at filing** — the
    failure mode v3 exists to close is the executor's own side writing the
    check it will be graded by (ASOP.md §0, §2.2);
  * **a run is a TREE** — parent pinned `(asop_id, version)`, one child per
    step pinned `(asop_id, version, step)`, `blocked_by` from `after`
    (§5.1);
  * **the four run refusals** — a draft or retired version, a missing input,
    an unbound role, an unsatisfiable constraint — each fires before anything
    is filed, so a refused run leaves the queue empty (§8.2);
  * **bindings come from the caller** — a plane that invented one would be
    guessing at a harness's roster, which is the one thing §7 says is local.
"""

from __future__ import annotations

import pytest

from agentco.errors import Refusal
from agentco.policy import RevisionPolicyError
from agentco.sop import ASOP, SopContractError, SopError, SopLibrary, SopStatus, Step
from agentco.work import PARENT_KEY, WorkStatus

# --------------------------------------------------------------------------- #
# fixtures — the running example from ASOP.md §1, trimmed to three steps
# --------------------------------------------------------------------------- #

JUDGED_GATE = {
    "kind": "judged",
    "check": "every acceptance criterion maps to a passing test id",
    "max_park_seconds": 86400,
    "on_timeout": "escalate",
    "escalate_to": "role:owner",
}
DETERMINISTIC_GATE = {
    "kind": "deterministic",
    "check": "pytest -q",
    "max_park_seconds": 900,
    "on_timeout": "fail",
}
HUMAN_GATE = {
    "kind": "human",
    "check": "the owner signs the release off",
    "verifier": "carol",
    "max_park_seconds": 86400,
    "on_timeout": "escalate",
    "escalate_to": "carol",
}


def feature_dev_body(**over) -> dict:
    body = {
        "task_type": "feature",
        "purpose": "take a specified feature from requirement to verified code",
        "trigger": "a feature bead exists with a written requirement and an owner",
        "inputs": [
            {"name": "requirement", "description": "the requirement the feature answers"},
            {"name": "repo", "description": "the repository the feature lands in"},
        ],
        "roles": {"implementer": {"kind": "agent"}, "validator": {"kind": "agent"}},
        "constraints": [{"distinct": ["implementer", "validator"]}],
        "steps": [
            {
                "name": "implement",
                "role": "implementer",
                "purpose": "write the code the requirement describes",
                "definition_of_done": "the feature exists behind its flag",
                "gate": DETERMINISTIC_GATE,
            },
            {
                "name": "run-tests",
                "role": "implementer",
                "purpose": "prove the suite is green",
                "validation": "the suite exits 0 on a clean checkout",
                "gate": DETERMINISTIC_GATE,
            },
            {
                "name": "validate",
                "role": "validator",
                "purpose": "confirm the feature satisfies the requirement as written",
                "validation": "a traceability table maps every criterion to a test id",
                "gate": JUDGED_GATE,
            },
        ],
    }
    body.update(over)
    return body


def an_asop(library, title="develop a feature", **over) -> ASOP:
    return library.create(title, **feature_dev_body(**over))


def an_active_asop(library, **over) -> ASOP:
    asop = an_asop(library, **over)
    return library.activate(asop.asop_id, asop.version)


RUN_INPUTS = {"requirement": "REQ-1", "repo": "org/repo"}
RUN_BINDINGS = {"implementer": "alice", "validator": "bob"}


def a_run(library, queue, asop, **over):
    kwargs = {"inputs": RUN_INPUTS, "bindings": RUN_BINDINGS}
    kwargs.update(over)
    return library.run(asop.asop_id, queue, **kwargs)


def code_of(exc: Exception) -> str:
    from agentco.refusals import classify

    return classify(exc).code


# --------------------------------------------------------------------------- #
# the record: an ASOP is a sequence of gated steps
# --------------------------------------------------------------------------- #


def test_create_stores_the_steps_and_their_gates(library):
    asop = an_asop(library)
    assert isinstance(asop, ASOP)
    assert asop.status is SopStatus.DRAFT
    assert [s.name for s in asop.steps] == ["implement", "run-tests", "validate"]
    assert all(isinstance(s, Step) for s in asop.steps)
    # The gate travels WITH the version. This is the whole of v3's §2.2.
    assert asop.steps[2].gate["kind"] == "judged"
    assert asop.steps[2].gate["max_park_seconds"] == 86400


def test_a_step_without_a_gate_is_refused(library):
    """A step with no gate is advice. The gate is not optional in v3."""
    body = feature_dev_body()
    del body["steps"][0]["gate"]
    with pytest.raises(SopContractError):
        library.create("no gate", **body)


def test_ordering_defaults_to_the_previous_step(library):
    asop = an_asop(library)
    assert [s.after for s in asop.steps] == [[], [1], [2]]


def test_explicit_after_makes_siblings_parallel(library):
    body = feature_dev_body()
    body["steps"][1]["after"] = []
    asop = library.create("parallel", **body)
    assert asop.steps[1].after == []


def test_creating_an_asop_puts_nothing_in_the_work_queue(library, queue):
    an_asop(library)
    assert queue.list() == []


# --------------------------------------------------------------------------- #
# run — the tree (§5.1)
# --------------------------------------------------------------------------- #


def test_run_files_a_tree_pinned_per_step(library, queue):
    asop = an_active_asop(library)
    run = a_run(library, queue, asop)

    parent = queue.get(run["runId"])
    assert parent.metadata["sop_ref"] == {"asop_id": asop.asop_id, "version": 1}
    assert parent.metadata["sop_run"]["inputs"] == RUN_INPUTS

    children = [queue.get(s["itemId"]) for s in run["steps"]]
    assert len(children) == 3
    for n, child in enumerate(children, start=1):
        assert child.metadata["sop_ref"] == {"asop_id": asop.asop_id, "version": 1, "step": n}
        assert child.metadata[PARENT_KEY] == parent.id

    # `after` becomes `blocked_by` on the tree as filed.
    assert children[0].blocked_by == []
    assert children[1].blocked_by == [children[0].id]
    assert children[2].blocked_by == [children[1].id]
    # And every child holds the parent open.
    assert set(queue.get(parent.id).blocked_by) == {c.id for c in children}


def test_each_step_bead_carries_a_copy_of_its_step_and_its_gate(library, queue):
    """The plan-vs-actual review must read the words the executor was handed,
    even after the procedure moves on."""
    asop = an_active_asop(library)
    run = a_run(library, queue, asop)
    third = queue.get(run["steps"][2]["itemId"])

    assert third.verify["kind"] == "judged"
    assert third.metadata["sop_plan"]["name"] == "validate"
    assert "traceability table" in third.metadata["sop_plan"]["validation"]

    library.revise(asop.asop_id, steps=feature_dev_body()["steps"], author="carol", author_kind="human")
    assert queue.get(third.id).metadata["sop_plan"]["name"] == "validate"


def test_run_binds_each_step_to_the_callers_binding(library, queue):
    asop = an_active_asop(library)
    run = a_run(library, queue, asop)
    assert [queue.get(s["itemId"]).assigned_agent for s in run["steps"]] == ["alice", "alice", "bob"]


def test_a_draft_cannot_be_run(library, queue):
    asop = an_asop(library)
    with pytest.raises(SopError) as exc:
        a_run(library, queue, asop)
    assert code_of(exc.value) == "sop_refused"
    assert queue.list() == []


def test_run_refuses_a_missing_declared_input(library, queue):
    asop = an_active_asop(library)
    with pytest.raises(Refusal) as exc:
        a_run(library, queue, asop, inputs={"requirement": "REQ-1"})
    assert exc.value.code == "inputs_missing"
    assert "repo" in str(exc.value)
    assert queue.list() == []


def test_run_refuses_a_role_with_no_binding(library, queue):
    asop = an_active_asop(library)
    with pytest.raises(Refusal) as exc:
        a_run(library, queue, asop, bindings={"implementer": "alice"})
    assert exc.value.code == "role_unbound"
    assert queue.list() == []


def test_run_refuses_bindings_that_violate_a_distinct_constraint(library, queue):
    asop = an_active_asop(library)
    with pytest.raises(Refusal) as exc:
        a_run(library, queue, asop, bindings={"implementer": "alice", "validator": "alice"})
    assert exc.value.code == "constraint_unsatisfiable"
    assert queue.list() == []


def test_the_plane_never_invents_a_binding(library, queue):
    """A harness knows its own agents; the plane does not. No bindings at all
    is `role_unbound`, never a default."""
    asop = an_active_asop(library)
    with pytest.raises(Refusal) as exc:
        a_run(library, queue, asop, bindings=None)
    assert exc.value.code == "role_unbound"


def test_run_refuses_a_caller_supplied_gate(library, queue):
    """The failure v3 exists to close: the filer authoring the check."""
    asop = an_active_asop(library)
    with pytest.raises(SopError) as exc:
        a_run(library, queue, asop, verify=DETERMINISTIC_GATE)
    assert code_of(exc.value) == "sop_refused"
    assert queue.list() == []


def test_run_get_returns_the_tree_with_statuses_and_pins(library, queue):
    asop = an_active_asop(library)
    run = a_run(library, queue, asop)
    view = library.run_get(run["runId"], queue)
    assert view["asopId"] == asop.asop_id
    assert view["version"] == 1
    assert [s["step"] for s in view["steps"]] == [1, 2, 3]
    assert view["steps"][0]["status"] == WorkStatus.PENDING.value


def test_run_list_reports_runs_of_an_asop(library, queue):
    asop = an_active_asop(library)
    a_run(library, queue, asop)
    a_run(library, queue, asop)
    assert len(library.run_list(queue, asop_id=asop.asop_id)) == 2


# --------------------------------------------------------------------------- #
# retire (§4, decision 3)
# --------------------------------------------------------------------------- #


def test_retire_refuses_new_runs_and_keeps_the_record(library, queue):
    asop = an_active_asop(library)
    live = a_run(library, queue, asop)

    library.retire(asop.asop_id, author="carol", author_kind="human")
    assert library.history(asop.asop_id)[-1].status is SopStatus.RETIRED

    with pytest.raises(SopError) as exc:
        a_run(library, queue, asop)
    assert code_of(exc.value) == "sop_refused"

    # In-flight runs finish under their pin; the pin still resolves.
    assert queue.get(live["runId"]) is not None
    assert library.get(asop.asop_id, version=1).steps[0].name == "implement"


def test_only_a_human_may_retire(library):
    asop = an_active_asop(library)
    with pytest.raises(RevisionPolicyError) as exc:
        library.retire(asop.asop_id, author="alice", author_kind="agent")
    assert exc.value.rule == "human_only"
    assert library.get(asop.asop_id).status is SopStatus.ACTIVE


# --------------------------------------------------------------------------- #
# nesting (§3.5)
# --------------------------------------------------------------------------- #


def test_a_nested_step_files_the_inner_tree_as_its_children(library, queue):
    inner = an_active_asop(library)
    outer = library.create(
        "release",
        task_type="release",
        roles={"owner": {"kind": "agent"}},
        steps=[
            {"name": "build", "role": "owner", "purpose": "build it", "gate": DETERMINISTIC_GATE},
            {"name": "develop", "uses": {"asop_id": inner.asop_id, "version": 1}},
        ],
    )
    library.activate(outer.asop_id, 1)

    run = library.run(
        outer.asop_id, queue,
        inputs=RUN_INPUTS, bindings={"owner": "alice", "implementer": "alice", "validator": "bob"},
    )
    nested = run["steps"][1]
    assert [c["step"] for c in nested["children"]] == [1, 2, 3]
    child = queue.get(nested["children"][0]["itemId"])
    # Pinned to the INNER version, and parented on the nested step's bead.
    assert child.metadata["sop_ref"] == {"asop_id": inner.asop_id, "version": 1, "step": 1}
    assert child.metadata[PARENT_KEY] == nested["itemId"]


def _wrapper(library, title, inner_id):
    asop = library.create(
        title,
        roles={"owner": {"kind": "agent"}},
        steps=[{"name": f"run {title}", "uses": {"asop_id": inner_id, "version": 1}}],
    )
    return library.activate(asop.asop_id, 1)


def test_three_levels_of_nesting_are_within_the_bound(library, queue):
    leaf = an_active_asop(library)
    mid = _wrapper(library, "mid", leaf.asop_id)
    top = _wrapper(library, "top", mid.asop_id)

    run = library.run(top.asop_id, queue, inputs=RUN_INPUTS,
                      bindings={"owner": "alice", "implementer": "alice", "validator": "bob"})
    assert len(run["steps"][0]["children"][0]["children"]) == 3


def test_a_fourth_level_of_nesting_is_refused_before_anything_is_filed(library, queue):
    """The bound is three deep. A run that would break it files nothing at all —
    caught in the plan, not half-way through the tree."""
    leaf = an_active_asop(library)
    mid = _wrapper(library, "mid", leaf.asop_id)
    top = _wrapper(library, "top", mid.asop_id)
    roof = _wrapper(library, "roof", top.asop_id)

    with pytest.raises(Refusal) as exc:
        library.run(roof.asop_id, queue, inputs=RUN_INPUTS,
                    bindings={"owner": "alice", "implementer": "alice", "validator": "bob"})
    assert exc.value.code == "decomposition_bound"
    assert queue.list() == []


def test_an_asop_that_nests_itself_is_refused(library, queue):
    leaf = an_active_asop(library)
    loop = library.create(
        "loop",
        roles={"owner": {"kind": "agent"}},
        steps=[{"name": "inner", "uses": {"asop_id": leaf.asop_id, "version": 1}}],
    )
    library.activate(loop.asop_id, 1)
    revised = library.revise(
        loop.asop_id,
        steps=[{"name": "inner", "uses": {"asop_id": loop.asop_id, "version": 1}}],
        author="carol", author_kind="human",
    )
    library.activate(loop.asop_id, revised.version, author="carol", author_kind="human")
    with pytest.raises(Refusal) as exc:
        library.run(loop.asop_id, queue, inputs=RUN_INPUTS, bindings={"owner": "alice"})
    assert exc.value.code == "decomposition_bound"


# --------------------------------------------------------------------------- #
# per-step outcomes and proposals (§6)
# --------------------------------------------------------------------------- #


def _declare(queue, humans=("carol",), adjudicators=()):
    """Who the operator declared. Undeclared, only humans adjudicate — so a
    test that adjudicates has to say who its human is, exactly as an operator
    does with AGENTCO_HUMANS."""
    queue.humans = frozenset(humans)
    queue.adjudicators = frozenset(adjudicators)
    return queue


def _finish(queue, item_id, actor="alice", result="did it", verifier="dave"):
    """Take one step bead all the way to `done`, whatever gate it carries.

    A deterministic gate is answered by the executor's own attestation. A
    judged or human one is not — that is the whole difference — so the step is
    reported without an attestation, parks, and a party other than the
    executor answers it.
    """
    queue.claim(item_id, actor, capabilities=["verify"])
    item = queue.get(item_id)
    kind = (item.verify or {}).get("kind")
    attestation = {"check": item.verify["check"], "exit_status": 0,
                   "environment": "test", "at": "2026-09-04T00:00:00+00:00"}
    queue.report_result(
        item_id, item.lease_attempt, WorkStatus.DONE, result=result,
        attestation=attestation if kind == "deterministic" else None,
        submitted_by=actor,
    )
    if queue.get(item_id).status is not WorkStatus.AWAITING_VERIFY:
        return
    # A parked gate is answered by whoever it names — the verifier for a human
    # gate, any declared verifier for a judged one — never by the executor.
    answerer = (item.verify or {}).get("verifier") if kind == "human" else verifier
    queue.attest(item_id, attestation, submitted_by=answerer or verifier,
                 capabilities=["verify"])


def test_outcomes_are_counted_per_version_and_per_step(library, queue):
    asop = an_active_asop(library)
    run = a_run(library, queue, asop)
    _finish(queue, run["steps"][0]["itemId"])

    rows = library.outcomes_by_version(asop.asop_id, queue)
    assert rows[0]["version"] == 1
    assert rows[0]["runs"] == 1
    by_step = {s["step"]: s for s in rows[0]["steps"]}
    assert by_step[1]["done"] == 1
    assert by_step[2]["done"] == 0
    assert by_step[1]["name"] == "implement"


def test_propose_writes_lessons_onto_the_step_that_earned_them(library, queue):
    asop = an_active_asop(library)
    run = a_run(library, queue, asop)
    first, second = run["steps"][0]["itemId"], run["steps"][1]["itemId"]
    _finish(queue, first)
    _finish(queue, second)
    _declare(queue)
    queue.adjudicate(first, "bad", "skipped the reproduce step", adjudicator="carol")
    queue.adjudicate(second, "good", "the suite already runs in CI", adjudicator="carol")

    draft = library.propose(asop.asop_id, queue, author="carol", author_kind="human")
    assert draft.version == 2
    assert any("reproduce" in m for m in draft.steps[0].common_mistakes)
    assert draft.steps[0].proposals == []
    assert any("already runs in CI" in p for p in draft.steps[1].proposals)
    assert draft.steps[1].common_mistakes == []


def test_plan_vs_actual_is_written_per_step(library, queue):
    asop = an_active_asop(library)
    run = a_run(library, queue, asop)
    _finish(queue, run["steps"][0]["itemId"])
    review = queue.get(run["steps"][0]["itemId"]).metadata["plan_vs_actual"]
    assert review["sop_ref"]["step"] == 1
    assert review["plan"]["name"] == "implement"


# --------------------------------------------------------------------------- #
# the revision policy, per step (§6.4)
# --------------------------------------------------------------------------- #


def _human_asop(library, **over):
    body = feature_dev_body(
        roles={"implementer": {"kind": "agent"}, "owner": {"kind": "human"}},
        constraints=[],
        steps=[
            {"name": "implement", "role": "implementer", "purpose": "write it",
             "gate": DETERMINISTIC_GATE},
            {"name": "sign-off", "role": "owner", "purpose": "approve the release",
             "gate": HUMAN_GATE},
        ],
        **over,
    )
    asop = library.create("release", **body)
    return library.activate(asop.asop_id, 1, author="carol", author_kind="human")


def test_an_agent_may_not_remove_a_human_step(library):
    asop = _human_asop(library)
    remaining = [{"name": "implement", "role": "implementer", "purpose": "write it",
                  "gate": DETERMINISTIC_GATE}]
    with pytest.raises(RevisionPolicyError) as exc:
        library.revise(asop.asop_id, roles={"implementer": {"kind": "agent"}},
                       steps=remaining, author="alice", author_kind="agent")
    assert exc.value.rule == "ratchet"


def test_an_agent_may_not_demote_a_human_step_to_an_agent_one(library):
    asop = _human_asop(library)
    steps = [
        {"name": "implement", "role": "implementer", "purpose": "write it", "gate": DETERMINISTIC_GATE},
        {"name": "sign-off", "role": "implementer", "purpose": "approve the release",
         "gate": DETERMINISTIC_GATE},
    ]
    with pytest.raises(RevisionPolicyError) as exc:
        library.revise(asop.asop_id, roles={"implementer": {"kind": "agent"}},
                       steps=steps, author="alice", author_kind="agent")
    assert exc.value.rule == "ratchet"


def test_an_agent_may_not_touch_a_protected_step(library):
    asop = library.create("pay", **_pay_body())
    library.activate(asop.asop_id, 1, author="carol", author_kind="human")
    steps = [{"name": "pay", "role": "payer", "purpose": "skip the approval",
              "tags": ["money"], "gate": HUMAN_GATE}]
    with pytest.raises(RevisionPolicyError) as exc:
        library.revise(asop.asop_id, steps=steps, author="alice", author_kind="agent")
    assert exc.value.rule == "protected"


def test_a_human_may_do_all_of_it(library):
    asop = _human_asop(library)
    remaining = [{"name": "implement", "role": "implementer", "purpose": "write it",
                  "gate": DETERMINISTIC_GATE}]
    draft = library.revise(asop.asop_id, roles={"implementer": {"kind": "agent"}},
                           steps=remaining, author="carol", author_kind="human")
    assert [s.name for s in draft.steps] == ["implement"]


# --------------------------------------------------------------------------- #
# promotion (§6.5) — human-only in v3
# --------------------------------------------------------------------------- #


def _completed_tree(queue, task_type="deploy"):
    parent = queue.create("ship the release", metadata={"task_type": task_type})
    child = queue.create("build it", metadata={"parent": parent.id},
                         assigned_agent="alice", verify=DETERMINISTIC_GATE)
    _finish(queue, child.id)
    queue.claim(parent.id, "alice")
    queue.report_result(parent.id, queue.get(parent.id).lease_attempt, WorkStatus.DONE,
                        result="shipped", submitted_by="alice")
    return parent


def test_promote_drafts_an_asop_from_a_completed_run(library, queue):
    parent = _completed_tree(queue)
    draft = library.promote(parent.id, queue, author="carol", author_kind="human")
    assert draft.status is SopStatus.DRAFT
    assert draft.task_type == "deploy"
    assert [s.name for s in draft.steps] == ["build it"]
    assert draft.steps[0].gate["kind"] == "deterministic"
    # Roles, never agents (§3.6).
    assert "alice" not in draft.roles
    assert draft.steps[0].role in draft.roles


def test_only_a_human_may_promote(library, queue):
    parent = _completed_tree(queue)
    with pytest.raises(RevisionPolicyError) as exc:
        library.promote(parent.id, queue, author="alice", author_kind="agent")
    assert exc.value.rule == "human_only"


def test_promote_is_refused_when_an_active_asop_covers_the_task_type(library, queue):
    an_active_asop(library)  # task_type: feature
    parent = _completed_tree(queue, task_type="feature")
    with pytest.raises(SopError) as exc:
        library.promote(parent.id, queue, author="carol", author_kind="human")
    assert code_of(exc.value) == "sop_refused"


def test_promote_refuses_an_unfinished_run(library, queue):
    parent = queue.create("ship the release", metadata={"task_type": "deploy"})
    queue.create("build it", metadata={"parent": parent.id})
    with pytest.raises(SopError):
        library.promote(parent.id, queue, author="carol", author_kind="human")


# --------------------------------------------------------------------------- #
# legacy rows stay readable and pinned-resolvable (§2.1)
# --------------------------------------------------------------------------- #


@pytest.fixture()
def legacy_row(tmp_path):
    """A v2 row written straight into a JSONL store, as an existing install has it.

    JSONL only, and deliberately: this is a statement about a file whose lines
    predate the v3 record. The SQLite/Postgres equivalent is the numbered
    migration, and it is tested where migrations are — `tests/test_sqlstore.py`.
    """
    import json

    path = tmp_path / "sops.jsonl"
    path.write_text(json.dumps({
        "sop_id": "sop-legacy1",
        "version": 1,
        "title": "incident response",
        "status": "active",
        "purpose": "restore a service after an alert",
        "definition_of_done": "the alert is closed",
        "validation": "the dashboard is green for ten minutes",
        "common_mistakes": ["closed the alert before the cause was written down"],
        "executor": "agent",
        "tags": [],
        "proposals": [],
        "author": "carol",
        "author_kind": "human",
    }) + "\n")
    return SopLibrary(path), "sop-legacy1"


def test_a_legacy_sop_row_reads_back_as_a_one_step_asop(legacy_row):
    library, sop_id = legacy_row
    asop = library.get(sop_id, version=1)
    assert isinstance(asop, ASOP)
    assert asop.asop_id == sop_id
    assert len(asop.steps) == 1
    assert asop.steps[0].definition_of_done == "the alert is closed"
    # A v2 record carried no gate. The migration will not invent an agent-closable
    # one: it fails closed to a human gate, which a human can revise.
    assert asop.steps[0].gate["kind"] == "human"
    assert asop.status is SopStatus.ACTIVE


# --------------------------------------------------------------------------- #
# first activation — the door a differential rule leaves open (§6.4)
# --------------------------------------------------------------------------- #


def _pay_body(**over) -> dict:
    body = feature_dev_body(
        task_type="payment",
        inputs=[],
        roles={"payer": {"kind": "agent"}},
        constraints=[],
        # Human-gated because it is tagged `money`: the record requires it
        # (ASOP.md §6.4), and so does the intent — a person looks before a
        # payment counts as done.
        steps=[{"name": "pay", "role": "payer", "purpose": "pay the vendor",
                "tags": ["money"], "gate": HUMAN_GATE}],
    )
    body.update(over)
    return body


def test_an_agent_may_not_first_activate_a_protected_step(library):
    """The rule is differential everywhere else, and on a FIRST activation
    there is nothing to differ from: baseline and target are the same object,
    so every diff-shaped check passes over a `money` step an agent wrote and
    is now putting into service. Activating it IS the change."""
    asop = library.create("pay the vendor", author="alice", author_kind="agent", **_pay_body())
    with pytest.raises(RevisionPolicyError) as exc:
        library.activate(asop.asop_id, 1, author="alice", author_kind="agent")
    assert exc.value.rule == "protected"
    assert library.get(asop.asop_id) is None, "a refused activation activated nothing"


def test_an_agent_may_not_first_activate_a_human_step(library):
    """Same door, the other rule. A human role's step is one a person closes;
    putting it live without a person is the demotion the ratchet forbids, and
    a first activation has no earlier version for the ratchet to read."""
    body = feature_dev_body(
        inputs=[], constraints=[],
        roles={"implementer": {"kind": "agent"}, "owner": {"kind": "human"}},
        steps=[
            {"name": "implement", "role": "implementer", "purpose": "write it",
             "gate": DETERMINISTIC_GATE},
            {"name": "sign-off", "role": "owner", "purpose": "approve it", "gate": HUMAN_GATE},
        ],
    )
    asop = library.create("release", author="alice", author_kind="agent", **body)
    with pytest.raises(RevisionPolicyError) as exc:
        library.activate(asop.asop_id, 1, author="alice", author_kind="agent")
    assert exc.value.rule == "ratchet"
    assert library.get(asop.asop_id) is None


def test_a_human_may_first_activate_either(library):
    """The rule is about who, never about what. A human is bound by none of it."""
    asop = library.create("pay the vendor", author="carol", author_kind="human", **_pay_body())
    activated = library.activate(asop.asop_id, 1, author="carol", author_kind="human")
    assert activated.status is SopStatus.ACTIVE


def test_an_agent_may_first_activate_an_ordinary_procedure(library):
    """The absolute check is narrow on purpose: it refuses protected and human
    steps, not every draft an agent wrote. Widening it to all first activations
    would make `sop_activate` a human verb, which §6.4 does not say."""
    asop = an_asop(library, author="alice", author_kind="agent")
    activated = library.activate(asop.asop_id, 1, author="alice", author_kind="agent")
    assert activated.status is SopStatus.ACTIVE


def test_the_second_activation_of_a_protected_step_is_still_refused(library):
    """The absolute check must not have replaced the differential one — the
    ordinary path (an active version exists) still refuses on the diff."""
    asop = library.create("pay the vendor", author="carol", author_kind="human", **_pay_body())
    library.activate(asop.asop_id, 1, author="carol", author_kind="human")
    library.revise(asop.asop_id, purpose="pay, faster", author="carol", author_kind="human")
    with pytest.raises(RevisionPolicyError) as exc:
        library.activate(asop.asop_id, 2, author="alice", author_kind="agent")
    assert exc.value.rule == "protected"


# --------------------------------------------------------------------------- #
# the adjudicator declarations reach every backend and every path
# --------------------------------------------------------------------------- #


def test_every_backend_answers_adjudication_with_a_refusal_not_an_attribute_error(library, queue):
    """`SqlQueue` does not call `Queue.__init__` — it opens a database instead
    of a file — so a declaration added to the base and missed here surfaces as
    an AttributeError from inside `adjudicate`, on one backend only, where a
    refusal belonged. Parity is the assertion."""
    assert isinstance(queue.humans, frozenset)
    assert isinstance(queue.adjudicators, frozenset)

    asop = an_active_asop(library)
    run = a_run(library, queue, asop)
    _finish(queue, run["steps"][0]["itemId"])
    with pytest.raises(Refusal) as exc:
        queue.adjudicate(run["steps"][0]["itemId"], "bad", "nothing", adjudicator="carol")
    assert exc.value.code == "adjudication_invalid"


def test_the_attest_rider_reads_the_queues_own_declarations(library, queue, monkeypatch):
    """`attest`'s pre-verdict rider check has to answer the same question the
    write path answers a moment later. Reading the environment instead meant a
    queue declared through `create_app(humans=[...])` refused the rider here
    and would have accepted the identical adjudication there — two answers to
    one question, depending on how the caller reached the plane."""
    monkeypatch.delenv("AGENTCO_HUMANS", raising=False)
    monkeypatch.delenv("AGENTCO_ADJUDICATORS", raising=False)
    queue.humans = frozenset()
    queue.adjudicators = frozenset({"bob"})

    # One judged step, executed by alice and verified by bob — so bob's
    # attestation may legitimately carry a rider, and the only question left is
    # whether the plane reads its own declaration or the environment's.
    body = feature_dev_body(
        inputs=[], constraints=[],
        roles={"implementer": {"kind": "agent"}},
        steps=[{"name": "implement", "role": "implementer", "purpose": "write it",
                "gate": JUDGED_GATE}],
    )
    asop = library.create("one judged step", **body)
    library.activate(asop.asop_id, 1)
    run = library.run(asop.asop_id, queue, inputs={}, bindings={"implementer": "alice"})
    judged = run["steps"][0]["itemId"]

    queue.claim(judged, "alice")
    item = queue.get(judged)
    queue.report_result(judged, item.lease_attempt, WorkStatus.DONE, result="did it",
                        submitted_by="alice")

    attestation = {"check": item.verify["check"], "exit_status": 0,
                   "environment": "test", "at": "2026-09-04T00:00:00+00:00"}
    out = queue.attest(judged, attestation, submitted_by="bob", capabilities=["verify"],
                       adjudication={"verdict": "good", "evidence": "the step was redundant"})
    assert out is not None
    assert queue.get(judged).metadata["adjudication"]["by"] == "bob"


# --------------------------------------------------------------------------- #
# §5.5 — a run's container closes when its steps do
# --------------------------------------------------------------------------- #


def test_a_run_closes_itself_when_its_last_step_lands(library, queue):
    """The run's parent is not somebody's to remember to close.

    Before this, a run whose every step was `done` sat `pending` until a human
    claimed and reported it — and `outcomes_by_version`, which reads the
    parent's status as the RUN's outcome, counted a finished run as in-flight.
    The per-version counting is the whole reason the grain moved, so a run
    that finishes and does not say so is the measurement quietly not working.
    """
    asop = an_active_asop(library)
    run = a_run(library, queue, asop)

    for entry in run["steps"][:-1]:
        _finish(queue, entry["itemId"], actor=entry["binding"])
        assert queue.get(run["runId"]).status is WorkStatus.PENDING, (
            "a run must not close while a step is still open"
        )

    _finish(queue, run["steps"][-1]["itemId"], actor=run["steps"][-1]["binding"])

    parent = queue.get(run["runId"])
    assert parent.status is WorkStatus.DONE
    row = library.outcomes_by_version(asop.asop_id, queue)[0]
    assert (row["runs"], row["done"], row["inFlight"]) == (1, 1, 0)
    assert row["successRate"] == 1.0


def test_the_runs_review_is_written_from_its_steps(library, queue):
    """§6.2: the run-level plan-vs-actual is assembled from the per-step ones,
    at the moment the run closes, while each step's own review still says what
    happened in it."""
    asop = an_active_asop(library)
    run = a_run(library, queue, asop)
    for entry in run["steps"]:
        _finish(queue, entry["itemId"], actor=entry["binding"])

    review = queue.get(run["runId"]).metadata["plan_vs_actual"]
    assert review["sop_ref"] == {"asop_id": asop.asop_id, "version": 1}
    assert [s["step"] for s in review["steps"]] == [1, 2, 3]
    assert all(s["landed"] == "done" and s["reviewed"] for s in review["steps"])
    assert review["run"]["inputs"] == RUN_INPUTS


def test_a_parked_or_failed_gate_holds_the_run_open(library, queue):
    """`awaiting_verify` and `verify_failed` release nothing downstream (§5.2),
    and a run that closed over a parked gate would be reported finished while
    somebody is still being asked to look at it."""
    body = feature_dev_body(
        inputs=[], constraints=[],
        roles={"implementer": {"kind": "agent"}, "owner": {"kind": "human"}},
        steps=[
            {"name": "implement", "role": "implementer", "purpose": "write it",
             "gate": DETERMINISTIC_GATE, "after": []},
            {"name": "sign-off", "role": "owner", "purpose": "approve it",
             "gate": HUMAN_GATE, "after": []},
        ],
    )
    asop = library.create("release", **body)
    library.activate(asop.asop_id, 1, author="carol", author_kind="human")
    # `owner` is dave, not carol: a human gate may not name its own executor
    # as verifier, which is the one party it exists to exclude.
    run = library.run(asop.asop_id, queue, inputs={},
                      bindings={"implementer": "alice", "owner": "dave"})

    _finish(queue, run["steps"][0]["itemId"])
    parked = run["steps"][1]["itemId"]
    queue.claim(parked, "dave")
    queue.report_result(parked, queue.get(parked).lease_attempt, WorkStatus.DONE,
                        result="signed", submitted_by="dave")
    assert queue.get(parked).status is WorkStatus.AWAITING_VERIFY
    assert queue.get(run["runId"]).status is WorkStatus.PENDING, (
        "a parked human gate is not an answer, and the run is not finished"
    )

    # The verifier answers, and THAT is what closes the run.
    queue.attest(parked, {"check": HUMAN_GATE["check"], "exit_status": 0,
                          "environment": "test", "at": "2026-09-04T00:00:00+00:00"},
                 submitted_by="carol", capabilities=["verify"])
    assert queue.get(parked).status is WorkStatus.DONE
    assert queue.get(run["runId"]).status is WorkStatus.DONE


def test_a_nested_run_closes_bottom_up(library, queue):
    """A nested step is a container of the inner ASOP's steps, and once closed
    it counts as done for the container above it. The close walks up."""
    inner = an_active_asop(library)
    outer = library.create(
        "release",
        task_type="release",
        roles={"owner": {"kind": "agent"}},
        steps=[{"name": "develop", "uses": {"asop_id": inner.asop_id, "version": 1}}],
    )
    library.activate(outer.asop_id, 1)
    run = library.run(outer.asop_id, queue, inputs=RUN_INPUTS,
                      bindings={"owner": "alice", "implementer": "alice", "validator": "bob"})

    nested = run["steps"][0]
    for entry in nested["children"]:
        _finish(queue, entry["itemId"], actor=entry["binding"])

    assert queue.get(nested["itemId"]).status is WorkStatus.DONE, "the container closed"
    assert queue.get(run["runId"]).status is WorkStatus.DONE, "and so did the run above it"


def test_an_ordinary_goal_is_never_closed_for_you(library, queue):
    """Only a run container closes itself. Nobody said an ad-hoc goal's
    children are the whole of it, and closing one because they happened to
    finish would be the plane deciding somebody's goal was met."""
    goal = queue.create("ship the thing")
    child = queue.create("do the work", metadata={"parent": goal.id},
                         assigned_agent="alice", verify=DETERMINISTIC_GATE)
    _finish(queue, child.id)
    assert queue.get(goal.id).status is WorkStatus.PENDING


def test_the_pulse_sweep_repairs_a_run_stranded_before_the_cascade(library, queue):
    """The repair path, on the state it exists for: a run whose steps are all
    done and whose container never heard about it — either because it finished
    before the cascade shipped, or because the process died between the
    child's write and the container's.

    Simulated by reopening the container after the fact, which is the only way
    to reach that state now that the report path closes it.
    """
    asop = an_active_asop(library)
    run = a_run(library, queue, asop)
    for entry in run["steps"]:
        _finish(queue, entry["itemId"], actor=entry["binding"])
    assert queue.get(run["runId"]).status is WorkStatus.DONE

    queue._mutate(run["runId"], lambda _i: {"status": WorkStatus.PENDING, "result": None})
    assert queue.get(run["runId"]).status is WorkStatus.PENDING

    assert queue.close_finished_runs(dry_run=True) == [run["runId"]], "the dry run names it"
    assert queue.get(run["runId"]).status is WorkStatus.PENDING, "and changes nothing"

    assert queue.close_finished_runs() == [run["runId"]]
    assert queue.get(run["runId"]).status is WorkStatus.DONE
    # Idempotent: a second sweep finds nothing, because there is nothing left.
    assert queue.close_finished_runs() == []


def test_the_sweep_leaves_an_unfinished_run_alone(library, queue):
    asop = an_active_asop(library)
    run = a_run(library, queue, asop)
    _finish(queue, run["steps"][0]["itemId"])
    assert queue.close_finished_runs(dry_run=True) == []
