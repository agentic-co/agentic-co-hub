"""The harness's own suite — hermetic, and it must cost nothing to run.

An eval harness that can only be tested by spending money is one that stops
being tested. Every test here runs against `AGENTCO_EVAL_PROVIDER=fake` with an
injected transport, so the whole file executes offline in under a second and
can sit in CI beside everything else.

What is worth testing here is not that the loop runs. It is the specific places
this harness could produce a number that looks fine and is wrong:

  * a fleet whose executor and judge are the same model, which measures
    agreement and reports it as quality,
  * a budget checked after the call it was meant to prevent,
  * an errored trial counted as a failure of the work,
  * a gate that hangs, or that answers differently on the second run,
  * a paired test that manufactures significance from zero disagreement,
  * a lesson arm that silently duplicates its own control.

Each of those is a test below, and each was written by asking what the
comparison would look like if the mechanism were removed — the same standard
`test_work_concurrency.py` holds the lease protocol to.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from evals.arms import Arm, ArmContractError, render
from evals.gate import Gate, GateClass, GateContractError, run_deterministic, scrubbed_env
from evals.gate import run as run_gate
from evals.ledger import Ledger, Trial
from evals.llm import BudgetExceeded, Fleet, FleetContractError
from evals.report import arm_rates, family_deltas, mcnemar, verdict
from evals.tasks import TaskContractError, TaskSet


@pytest.fixture()
def fleet():
    return Fleet.load(
        provider="fake",
        models={"executor": "fake/exec", "judge": "fake/judge"},
    )


DETERMINISTIC_GATE = {
    "kind": "deterministic", "check": "true",
    "max_park_seconds": 900, "on_timeout": "fail",
}


def sop(**body):
    from agentco.sop import SopLibrary
    from asop.sop import STEP_TEXT_FIELDS

    import tempfile

    title = body.pop("title", "Do the thing")
    step_fields = {k: body.pop(k) for k in list(body) if k in STEP_TEXT_FIELDS or k == "common_mistakes"}
    gate = body.pop("gate", None) or DETERMINISTIC_GATE

    lib = SopLibrary(Path(tempfile.mkdtemp()) / "sops.jsonl")
    created = lib.create(
        title,
        roles={"implementer": {"kind": "agent"}},
        steps=[{"name": "do it", "role": "implementer", "gate": gate, **step_fields}],
        **body,
    )
    lib.activate(created.asop_id, created.version)
    return lib.get(created.asop_id)


# --------------------------------------------------------------------------- #
# The fleet, and the contract clause it exists to enforce
# --------------------------------------------------------------------------- #


def test_executor_and_judge_may_not_be_the_same_model():
    with pytest.raises(FleetContractError) as exc:
        Fleet.load(provider="fake", models={"executor": "m/same", "judge": "m/same"})
    assert "agreement, not" in str(exc.value)


def test_a_missing_model_is_refused_rather_than_defaulted(monkeypatch):
    monkeypatch.delenv("AGENTCO_EVAL_EXECUTOR", raising=False)
    monkeypatch.delenv("AGENTCO_EVAL_JUDGE", raising=False)
    with pytest.raises(FleetContractError) as exc:
        Fleet.load(provider="litellm")
    assert "no default" in str(exc.value)


def test_a_bare_model_name_is_routed_through_the_proxy_when_one_is_configured():
    f = Fleet.load(
        provider="fake",
        models={"executor": "sonnet", "judge": "gpt5"},
        api_base="https://proxy.example.com",
    )
    assert f.models["executor"] == "litellm_proxy/sonnet"


def test_an_explicit_provider_prefix_is_never_rewritten():
    f = Fleet.load(
        provider="fake",
        models={"executor": "anthropic/claude-sonnet-5", "judge": "openai/gpt-5"},
        api_base="https://proxy.example.com",
    )
    assert f.models["executor"] == "anthropic/claude-sonnet-5"


def test_the_budget_stops_the_call_before_it_is_made():
    """A cap compared against spend AFTER the response has already spent it."""
    f = Fleet.load(provider="fake", models={"executor": "a/x", "judge": "b/y"}, budget_usd=0.0)
    f.spend.usd = 0.01  # already over
    with pytest.raises(BudgetExceeded):
        f.complete("executor", [{"role": "user", "content": "hi"}])
    assert f.spend.calls == 0, "the refused call must not be recorded as spend"


def test_an_unpriced_call_is_none_and_never_a_measured_zero():
    from evals.llm import Spend

    spend = Spend(usd=1.0, calls=3, unpriced_calls=1)
    assert spend.as_dict()["isFloor"] is True


# --------------------------------------------------------------------------- #
# Gates — ASOP Part III, made executable
# --------------------------------------------------------------------------- #


def test_a_gate_with_no_timeout_is_refused():
    with pytest.raises(GateContractError) as exc:
        Gate(gate_class=GateClass.DETERMINISTIC, spec="true", timeout_s=0)
    assert "deadlock" in str(exc.value)


def test_a_blank_gate_spec_is_refused():
    with pytest.raises(GateContractError):
        Gate(gate_class=GateClass.DETERMINISTIC, spec="   ")


def test_expiry_is_a_failure_and_not_a_hang(tmp_path):
    g = Gate(gate_class=GateClass.DETERMINISTIC, spec="sleep 5", timeout_s=1)
    result = run_deterministic(g, tmp_path)
    assert result.passed is False
    assert result.timed_out is True


def test_a_check_receives_no_ambient_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("SOMETHING_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("PATH", os.environ["PATH"])
    assert "SOMETHING_API_KEY" not in scrubbed_env()

    g = Gate(
        gate_class=GateClass.DETERMINISTIC,
        spec="test -z \"$SOMETHING_API_KEY\"",
        timeout_s=10,
    )
    assert run_deterministic(g, tmp_path).passed is True


def test_even_an_explicitly_allowed_secret_name_is_still_scrubbed(monkeypatch):
    """env_extra widens the allowlist; it does not defeat the secret filter."""
    monkeypatch.setenv("MY_TOKEN", "shhh")
    assert "MY_TOKEN" not in scrubbed_env(extra=("MY_TOKEN",))


def test_a_non_idempotent_gate_is_caught_and_fails(tmp_path):
    """A verdict that changes between runs cannot support retry semantics."""
    counter = tmp_path / "n"
    counter.write_text("0")
    g = Gate(
        gate_class=GateClass.DETERMINISTIC,
        # Passes the first time, fails every time after.
        spec=f"n=$(cat {counter}); echo $((n+1)) > {counter}; test \"$n\" = 0",
        timeout_s=10,
    )
    result = run_gate(g, tmp_path, artifact="")
    assert result.passed is False
    assert "not idempotent" in result.detail


def test_a_judge_that_returns_garbage_fails_the_gate_rather_than_erroring(fleet):
    fleet._transport = lambda role, messages: "I'm not sure, maybe?"
    g = Gate(gate_class=GateClass.JUDGED, spec="Is it correct?", timeout_s=10)
    result = run_gate(g, Path("."), artifact="anything", fleet=fleet)
    assert result.passed is False
    assert "no parseable verdict" in result.detail


def test_a_judge_verdict_survives_a_code_fence(fleet):
    fleet._transport = lambda role, messages: '```json\n{"passed": true, "reason": "meets clause 1"}\n```'
    g = Gate(gate_class=GateClass.JUDGED, spec="Is it correct?", timeout_s=10)
    assert run_gate(g, Path("."), artifact="x", fleet=fleet).passed is True


def test_a_human_gate_parks_and_does_not_pass():
    g = Gate(gate_class=GateClass.HUMAN, spec="the accountable engineer", timeout_s=10)
    result = run_gate(g, Path("."), artifact="x")
    assert result.passed is False
    assert "parked" in result.detail


# --------------------------------------------------------------------------- #
# Arms — the comparison must not measure prompt engineering
# --------------------------------------------------------------------------- #


def test_the_prose_arm_withholds_the_gate_and_the_asop_arm_states_it():
    s = sop(purpose="Parse rows", validation="pytest tests/", definition_of_done="Rows parse")
    prose = render(Arm.PROSE, "do it", sop=s)
    asop = render(Arm.ASOP, "do it", sop=s)
    assert "pytest tests/" not in prose, "a prose SOP announcing its own gate is half an ASOP"
    assert "pytest tests/" in asop
    assert "Parse rows" in prose and "Parse rows" in asop


def test_the_bare_arm_is_the_task_and_nothing_else():
    assert render(Arm.BARE, "just this") == "just this"


def test_a_lesson_arm_with_no_lesson_is_refused_rather_than_duplicating_its_control():
    s = sop(purpose="Parse rows", validation="pytest")
    with pytest.raises(ArmContractError) as exc:
        render(Arm.ASOP_LESSON, "do it", sop=s)
    assert "duplicate" in str(exc.value)


def test_the_placebo_arm_requires_a_placebo():
    s = sop(purpose="Parse rows", validation="pytest")
    with pytest.raises(ArmContractError):
        render(Arm.PLACEBO, "do it", sop=s)


def test_the_lesson_reaches_the_prompt(tmp_path):
    s = sop(purpose="Parse rows", validation="pytest", common_mistakes=["Forgetting quoted commas"])
    out = render(Arm.ASOP_LESSON, "do it", sop=s)
    assert "Forgetting quoted commas" in out
    assert f"v{s.version}" in out


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #


def test_a_duplicate_task_id_is_refused(tmp_path):
    body = {
        "task_id": "same", "family": "f", "prompt": "p",
        "gate": {"class": "deterministic", "spec": "true"},
    }
    (tmp_path / "a.json").write_text(json.dumps([body]))
    (tmp_path / "b.json").write_text(json.dumps([body]))
    with pytest.raises(TaskContractError) as exc:
        TaskSet.load(tmp_path)
    assert "duplicate task_id" in str(exc.value)


def test_an_empty_task_directory_is_refused_not_reported_as_a_clean_run(tmp_path):
    with pytest.raises(TaskContractError) as exc:
        TaskSet.load(tmp_path)
    assert "looks like a pass" in str(exc.value)


def test_the_shipped_task_set_loads_and_every_family_has_a_holdout():
    taskset = TaskSet.load(Path("evals/tasks"))
    assert len(taskset) >= 4
    for family in taskset.families:
        assert taskset.holdout(family), f"{family} has no held-out instance"
        assert taskset.training(family), f"{family} has no instance to learn from"


# --------------------------------------------------------------------------- #
# Ledger — resume must not re-spend
# --------------------------------------------------------------------------- #


def _trial(task="t1", arm="asop", passed=True, run_id="r", replicate=0, **kw):
    return Trial(
        run_id=run_id, task_id=task, family=kw.pop("family", "f"), arm=arm,
        replicate=replicate, passed=passed,
        gate=kw.pop("gate", {"class": "deterministic", "passed": passed}), **kw,
    )


def test_a_resumed_run_knows_what_it_already_paid_for(tmp_path):
    ledger = Ledger(tmp_path / "trials.jsonl")
    ledger.append(_trial())
    assert ("r", "t1", "asop", 0) in ledger.completed_keys()


def test_a_truncated_final_line_costs_one_trial_not_the_file(tmp_path, capsys):
    path = tmp_path / "trials.jsonl"
    ledger = Ledger(path)
    ledger.append(_trial(task="t1"))
    ledger.append(_trial(task="t2"))
    with open(path, "a") as fh:
        fh.write('{"run_id": "r", "task_id": "t3"')  # crash mid-append
    assert len(ledger.read_all()) == 2
    assert "unreadable" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Report — the places a number could be wrong and look fine
# --------------------------------------------------------------------------- #


def test_an_errored_trial_is_excluded_from_the_rate_not_counted_as_a_failure():
    trials = [
        _trial(task="t1", arm="asop", passed=True),
        _trial(task="t2", arm="asop", passed=False, error="provider 500"),
    ]
    row = next(r for r in arm_rates(trials) if r["arm"] == "asop")
    assert row["tasks"] == 1, "the errored trial must leave the denominator"
    assert row["successRate"] == 1.0
    assert row["errored"] == 1


def test_a_parked_human_gate_is_neither_a_pass_nor_a_failure():
    trials = [_trial(task="t1", arm="asop", passed=False, gate={"class": "human", "passed": False})]
    row = next(r for r in arm_rates(trials) if r["arm"] == "asop")
    assert row["tasks"] == 0 and row["parked"] == 1


def test_pass_caret_k_requires_every_replicate(monkeypatch):
    trials = [
        _trial(task="t1", arm="asop", passed=True, replicate=0),
        _trial(task="t1", arm="asop", passed=False, replicate=1),
    ]
    row = next(r for r in arm_rates(trials, replicates=2) if r["arm"] == "asop")
    assert row["passed"] == 0, "one green run out of two is not a pass^2"


def test_no_disagreement_yields_p_one_rather_than_manufactured_significance():
    trials = [
        _trial(task=f"t{i}", arm="asop", passed=True) for i in range(20)
    ] + [_trial(task=f"t{i}", arm="asop_lesson", passed=True) for i in range(20)]
    result = mcnemar(trials, "asop_lesson", "asop")
    assert result["discordant"] == 0
    assert result["pValue"] == 1.0
    assert result["favours"] is None


def test_a_one_sided_disagreement_is_detected_and_named():
    trials = []
    for i in range(10):
        trials.append(_trial(task=f"t{i}", arm="asop", passed=False))
        trials.append(_trial(task=f"t{i}", arm="asop_lesson", passed=True))
    result = mcnemar(trials, "asop_lesson", "asop")
    assert result["discordant"] == 10
    assert result["favours"] == "asop_lesson"
    assert result["pValue"] < 0.01


def test_only_tasks_both_arms_completed_are_paired():
    trials = [
        _trial(task="t1", arm="asop", passed=True),
        _trial(task="t2", arm="asop_lesson", passed=True),
    ]
    assert mcnemar(trials, "asop_lesson", "asop")["pairedTasks"] == 0


def test_a_lesson_that_lifts_one_family_and_drops_another_is_flagged():
    trials = []
    for i in range(4):
        trials.append(_trial(task=f"a{i}", family="target", arm="asop", passed=False))
        trials.append(_trial(task=f"a{i}", family="target", arm="asop_lesson", passed=True))
    for i in range(4):
        trials.append(_trial(task=f"b{i}", family="other", arm="asop", passed=True))
        trials.append(_trial(task=f"b{i}", family="other", arm="asop_lesson", passed=i < 2))
    rows = {r["family"]: r for r in family_deltas(trials, "asop", "asop_lesson")}
    assert rows["target"]["delta"] == 1.0
    assert rows["other"]["regressed"] is True


def test_the_verdict_refuses_to_hold_when_the_placebo_also_lifts():
    """If a true-but-irrelevant lesson works as well, this measures tokens, not learning."""
    trials = []
    for i in range(10):
        trials.append(_trial(task=f"t{i}", arm="asop", passed=False))
        trials.append(_trial(task=f"t{i}", arm="asop_lesson", passed=True))
        trials.append(_trial(task=f"t{i}", arm="placebo", passed=True))
    v = verdict(trials)
    assert v["clauses"]["lessonBeatsItsBaseVersion"] is True
    assert v["clauses"]["placeboDoesNot"] is False
    assert v["sharedLearningHolds"] is False


# --------------------------------------------------------------------------- #
# The dependency direction, which is what keeps the coordination plane clean
# --------------------------------------------------------------------------- #


def test_the_coordination_plane_does_not_import_the_harness():
    offenders = [
        path.name
        for path in Path("agentco").glob("*.py")
        if "evals" in path.read_text()
    ]
    assert not offenders, (
        f"{offenders} reference the eval harness. `agentco` ships with zero "
        f"dependencies; the harness needs an LLM SDK, and the arrow must only "
        f"ever point from evals -> agentco."
    )
