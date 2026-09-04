"""The v3 records: an ASOP is the sequence, a Step is what v2 called the
procedure, and the gate lives on the step. Pins ASOP.md §3 and the seven
decisions of §11 that touch the record shape."""

from __future__ import annotations

import pytest

from asop import ASOP, MAX_STEPS, SopStatus, Step, validate_asop, validate_step
from asop.sop import SopContractError

DET = {"kind": "deterministic", "check": "uv run pytest -q"}
JUDGED = {"kind": "judged", "check": "every criterion maps to a passing test", "rubric": "r1"}
HUMAN = {"kind": "human", "check": "a person signs off", "verifier": "dana",
         "max_park_seconds": 86400, "on_timeout": "escalate", "escalate_to": "dana"}


def feature_dev(**over):
    body = {
        "title": "Develop a feature",
        "task_type": "feature",
        "purpose": "Take a feature from requirement to verified code.",
        "inputs": [{"name": "requirement", "description": "the requirement"}, {"name": "repo"}],
        "roles": {"analyst": {"kind": "agent"}, "implementer": {"kind": "agent"}, "validator": {"kind": "agent"}},
        "constraints": [{"distinct": ["implementer", "validator"]}],
        "steps": [
            {"name": "validate-requirements", "role": "analyst", "purpose": "read it", "gate": DET},
            {"name": "write-tests", "role": "implementer", "purpose": "tests first", "gate": DET, "after": []},
            {"name": "implement", "role": "implementer", "purpose": "make it pass", "gate": DET, "after": [1, 2]},
            {"name": "run-tests", "role": "implementer", "purpose": "prove it", "gate": DET},
            {"name": "validate", "role": "validator", "purpose": "trace criteria", "gate": JUDGED},
        ],
    }
    body.update(over)
    return body


# ------------------------------------------------------------------ shape

def test_the_running_example_validates_and_normalises():
    out = validate_asop(feature_dev())
    assert [s["step"] for s in out["steps"]] == [1, 2, 3, 4, 5]
    assert out["steps"][0]["after"] == []            # step 1 follows nothing
    assert out["steps"][1]["after"] == []            # declared parallel with 1
    assert out["steps"][2]["after"] == [1, 2]        # explicit join
    assert out["steps"][3]["after"] == [3]           # default: the previous step
    assert out["steps"][4]["gate"]["kind"] == "judged"
    assert out["constraints"] == [{"distinct": ["implementer", "validator"]}]


def test_the_gate_is_on_the_step_and_required():
    """Decision 0/2.2: a run supplies no gate; the step carries it."""
    steps = feature_dev()["steps"]
    del steps[0]["gate"]
    with pytest.raises(SopContractError, match="gate.*required"):
        validate_asop(feature_dev(steps=steps))


def test_a_step_gate_is_normalised_by_the_shared_schema():
    out = validate_step({"name": "x", "role": "r", "purpose": "p", "gate": {"class": "deterministic", "check": "true"}}, index=1)
    assert out["gate"]["kind"] == "deterministic"    # `class` read-alias honoured


def test_a_step_names_a_role_never_an_agent():
    with pytest.raises(SopContractError, match="role.*required"):
        validate_step({"name": "x", "purpose": "p", "gate": DET}, index=1)


def test_a_step_role_must_be_declared_on_the_asop():
    body = feature_dev()
    body["steps"][0]["role"] = "wizard"
    with pytest.raises(SopContractError, match="not declared"):
        validate_asop(body)


def test_next_sop_is_refused_by_name():
    """Decision 4: dropped; sequencing between procedures is the harness's."""
    with pytest.raises(SopContractError, match="next_sop.*not a v3 field"):
        validate_step({"name": "x", "role": "r", "purpose": "p", "gate": DET, "next_sop": "release"}, index=1)


# ------------------------------------------------------------------ ordering (decision 2)

def test_after_may_name_only_earlier_steps():
    with pytest.raises(SopContractError, match="not earlier"):
        validate_step({"name": "x", "role": "r", "purpose": "p", "gate": DET, "after": [3]}, index=2)


def test_a_step_cannot_follow_itself():
    with pytest.raises(SopContractError, match="after itself"):
        validate_step({"name": "x", "role": "r", "purpose": "p", "gate": DET, "after": [2]}, index=2)


def test_after_is_sorted_and_deduplicated():
    out = validate_step({"name": "x", "role": "r", "purpose": "p", "gate": DET, "after": [3, 1, 3]}, index=4)
    assert out["after"] == [1, 3]


# ------------------------------------------------------------------ bounds

def test_more_than_seven_steps_is_refused():
    steps = [{"name": f"s{i}", "role": "analyst", "purpose": "p", "gate": DET} for i in range(MAX_STEPS + 1)]
    with pytest.raises(SopContractError, match="bound is 7"):
        validate_asop(feature_dev(steps=steps))


def test_an_asop_with_no_steps_is_a_title():
    with pytest.raises(SopContractError, match="non-empty LIST"):
        validate_asop(feature_dev(steps=[]))


# ------------------------------------------------------------------ nesting

def test_a_nested_step_pins_the_inner_version_and_carries_no_body():
    out = validate_step({"name": "release", "uses": {"asop_id": "release", "version": 2}}, index=1)
    assert out["uses"] == {"asop_id": "release", "version": 2}
    assert "gate" not in out and "role" not in out


def test_a_nested_step_may_not_also_have_a_body():
    with pytest.raises(SopContractError, match="carries no body"):
        validate_step({"name": "release", "uses": {"asop_id": "release", "version": 2}, "gate": DET}, index=1)


def test_a_nested_step_must_pin_a_version():
    with pytest.raises(SopContractError, match="pins the inner"):
        validate_step({"name": "release", "uses": {"asop_id": "release"}}, index=1)


# ------------------------------------------------------------------ roles and separation of duties (§3.6)

def test_a_human_role_forces_a_human_gate():
    body = feature_dev()
    body["roles"]["validator"] = {"kind": "human"}
    with pytest.raises(SopContractError, match="human, so its gate must be kind 'human'"):
        validate_asop(body)
    body["steps"][4]["gate"] = HUMAN
    assert validate_asop(body)["roles"]["validator"] == {"kind": "human"}


def test_a_judged_step_may_not_share_the_role_of_the_step_it_judges():
    body = feature_dev()
    body["steps"][4]["role"] = "implementer"        # validator == implementer
    with pytest.raises(SopContractError, match="judge must be a route distinct"):
        validate_asop(body)


def test_a_constraint_must_name_declared_roles():
    with pytest.raises(SopContractError, match="does not declare"):
        validate_asop(feature_dev(constraints=[{"distinct": ["implementer", "ghost"]}]))


def test_a_role_is_a_kind_not_an_agent():
    with pytest.raises(SopContractError, match="never an agent"):
        validate_asop(feature_dev(roles={"analyst": {"kind": "agent", "model": "gpt-5"},
                                         "implementer": {"kind": "agent"}, "validator": {"kind": "agent"}}))


# ------------------------------------------------------------------ inputs (decision 5)

def test_inputs_are_names_with_descriptions_and_kind_is_reserved():
    out = validate_asop(feature_dev(inputs=[{"name": "requirement", "kind": "bead"}]))
    assert out["inputs"] == [{"name": "requirement", "kind": "bead"}]


def test_a_duplicate_input_name_is_refused():
    with pytest.raises(SopContractError, match="twice"):
        validate_asop(feature_dev(inputs=[{"name": "repo"}, {"name": "repo"}]))


# ------------------------------------------------------------------ lifecycle (decision 3)

def test_retired_is_a_status():
    assert SopStatus.RETIRED.value == "retired"
    assert set(SopStatus) == {SopStatus.DRAFT, SopStatus.ACTIVE, SopStatus.SUPERSEDED, SopStatus.RETIRED}


# ------------------------------------------------------------------ record round trip

def test_an_asop_round_trips_through_json_with_its_steps():
    body = validate_asop(feature_dev())
    rec = ASOP(asop_id="feature-dev", version=3, status=SopStatus.ACTIVE,
               steps=[Step(**st) for st in body.pop("steps")], **body)
    back = ASOP.from_json(rec.to_json())
    assert back.ref == {"asop_id": "feature-dev", "version": 3}
    assert back.step_ref(5) == {"asop_id": "feature-dev", "version": 3, "step": 5}
    assert [s.name for s in back.steps] == [s.name for s in rec.steps]
    assert back.steps[4].gate["kind"] == "judged"
    assert back.status is SopStatus.ACTIVE
