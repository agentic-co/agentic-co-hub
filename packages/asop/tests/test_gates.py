"""The unified gate schema, tested directly against `validate_gate` —
no queue, no store, just the write-boundary contract this package owns.

Most of these are ported from the plane's `tests/test_gates.py`, which
exercised the same rules through `agentco.work.Queue.create(verify=...)`.
Here they call `asop.gates.validate_gate` directly with
`require=("clock",)` and the plane's own ceiling, reproducing the plane's
calling convention exactly — that parity is what makes `agentco/gates.py`
a safe thin shim over this module. The tests below the "unified schema"
marker are new: they exercise the `class`/`kind` alias, the staged `checks`
form, and the `require=()` calling convention the Harness adopts in P2.
"""

from __future__ import annotations

import pytest

from asop import gates
from asop.errors import Refusal

PLANE_CEILING = 30 * 24 * 3600

DETERMINISTIC = {
    "kind": "deterministic",
    "check": "pytest -q",
    "max_park_seconds": 900,
    "on_timeout": "fail",
}
JUDGED = {
    "kind": "judged",
    "check": "the migration is reversible and the rollback was exercised",
    "max_park_seconds": 3600,
    "on_timeout": "escalate",
    "escalate_to": "release-owner",
}


def validate(payload, **kw):
    """The plane's calling convention: clock required, plane's ceiling."""
    kw.setdefault("require", ("clock",))
    kw.setdefault("max_park_seconds_ceiling", PLANE_CEILING)
    return gates.validate_gate(payload, **kw)


# --------------------------------------------------------------------------- #
# Ported from the plane's suite — same rules, called directly
# --------------------------------------------------------------------------- #


def test_a_malformed_gate_is_refused():
    with pytest.raises(Refusal) as caught:
        validate({"kind": "deterministic", "check": "pytest -q"})  # no clock
    assert caught.value.code == gates.GATE_INVALID
    assert caught.value.remediation


def test_a_misspelled_gate_field_is_refused_rather_than_ignored():
    with pytest.raises(Refusal) as caught:
        validate(dict(DETERMINISTIC, max_park_second=900))
    assert "unknown gate field(s)" in caught.value.message
    assert "max_park_second" in caught.value.message


def test_max_park_seconds_cannot_exceed_the_ceiling():
    with pytest.raises(Refusal) as caught:
        validate(dict(DETERMINISTIC, max_park_seconds=PLANE_CEILING + 1))
    assert caught.value.code == gates.GATE_INVALID
    assert "exceeds the ceiling" in caught.value.message

    at_the_ceiling = validate(dict(DETERMINISTIC, max_park_seconds=PLANE_CEILING))
    assert at_the_ceiling["max_park_seconds"] == PLANE_CEILING


def test_max_park_seconds_rejects_a_bool():
    with pytest.raises(Refusal) as caught:
        validate(dict(DETERMINISTIC, max_park_seconds=True))
    assert caught.value.code == gates.GATE_INVALID
    assert "must be a positive integer" in caught.value.message


def test_an_escalation_with_no_destination_is_refused():
    with pytest.raises(Refusal):
        validate(dict(JUDGED, escalate_to=None))


def test_escalate_to_set_without_escalate_is_refused():
    with pytest.raises(Refusal) as caught:
        validate(dict(DETERMINISTIC, escalate_to="release-owner"))
    assert caught.value.code == gates.GATE_INVALID
    assert "nothing would ever read it" in caught.value.message


def test_a_human_gate_with_nobody_named_to_answer_it_is_refused():
    human = {"kind": "human", "check": "the owner signs off", "max_park_seconds": 900, "on_timeout": "fail"}
    with pytest.raises(Refusal) as caught:
        validate(human)
    assert "must name the person who answers it" in caught.value.message
    assert "escalate_to is not a substitute" in caught.value.remediation

    named = validate(dict(human, verifier="dana"))
    assert named["verifier"] == "dana"
    assert named["escalate_to"] is None


def test_a_deterministic_gate_may_not_name_a_verifier():
    with pytest.raises(Refusal) as caught:
        validate(dict(DETERMINISTIC, verifier="dana"))
    assert "nothing would ever read it" in caught.value.message


def test_a_judged_gate_may_narrow_its_route_to_one_verifier():
    named = validate(dict(JUDGED, verifier="reviewer-a"))
    assert named["verifier"] == "reviewer-a"
    assert validate(JUDGED)["verifier"] is None


def test_a_stored_gate_is_normalised():
    normalised = validate(DETERMINISTIC)
    assert normalised["kind"] == "deterministic"
    assert normalised["check"] == "pytest -q"
    assert normalised["checks"] is None
    assert normalised["escalate_to"] is None
    assert normalised["verifier"] is None
    assert normalised["schema_version"] == gates.SCHEMA_VERSION
    # cwd/timeout_s/rubric/judge_route: present, None where absent.
    assert normalised["cwd"] is None
    assert normalised["timeout_s"] is None


# --------------------------------------------------------------------------- #
# The unified schema — class/kind alias, staged checks, require=()
# --------------------------------------------------------------------------- #


def test_class_is_accepted_as_a_read_alias_for_kind():
    payload = dict(DETERMINISTIC)
    payload["class"] = payload.pop("kind")
    normalised = validate(payload)
    assert normalised["kind"] == "deterministic"
    assert "class" not in normalised


def test_class_and_kind_disagreeing_is_refused():
    with pytest.raises(Refusal) as caught:
        validate(dict(DETERMINISTIC, **{"class": "judged"}))
    assert caught.value.code == gates.GATE_INVALID
    assert "disagree" in caught.value.message


def test_class_and_kind_agreeing_is_accepted():
    normalised = validate(dict(DETERMINISTIC, **{"class": "deterministic"}))
    assert normalised["kind"] == "deterministic"


def test_a_staged_checks_gate_is_accepted():
    payload = dict(DETERMINISTIC)
    del payload["check"]
    payload["checks"] = ["lint", "mypy", "pytest -q"]
    normalised = validate(payload)
    assert normalised["check"] is None
    assert normalised["checks"] == ["lint", "mypy", "pytest -q"]


def test_a_one_element_checks_list_normalises_to_check():
    payload = dict(DETERMINISTIC)
    del payload["check"]
    payload["checks"] = ["pytest -q"]
    normalised = validate(payload)
    assert normalised["check"] == "pytest -q"
    assert normalised["checks"] is None


def test_check_and_checks_together_is_refused():
    with pytest.raises(Refusal) as caught:
        validate(dict(DETERMINISTIC, checks=["a", "b"]))
    assert caught.value.code == gates.GATE_INVALID
    assert "mutually exclusive" in caught.value.message


def test_an_empty_checks_list_is_refused():
    payload = dict(DETERMINISTIC)
    del payload["check"]
    payload["checks"] = []
    with pytest.raises(Refusal):
        validate(payload)


def test_a_bare_string_checks_is_refused_rather_than_iterated():
    payload = dict(DETERMINISTIC)
    del payload["check"]
    payload["checks"] = "pytest -q"
    with pytest.raises(Refusal) as caught:
        validate(payload)
    assert "LIST" in caught.value.message


def test_a_partially_declared_clock_is_always_refused():
    """Declare the clock or don't. Some but not all of max_park_seconds /
    on_timeout present is refused regardless of `require`."""
    partial = {"kind": "deterministic", "check": "pytest -q", "max_park_seconds": 900}
    with pytest.raises(Refusal) as caught:
        gates.validate_gate(partial, require=())
    assert caught.value.code == gates.GATE_INVALID
    assert "part of its park clock" in caught.value.message

    with pytest.raises(Refusal):
        gates.validate_gate(partial, require=("clock",))


def test_require_empty_accepts_a_fully_clockless_gate():
    """The Harness's calling convention (P2): no park-clock concept, so no
    clock fields at all — legal when the clock is not required."""
    clockless = {"kind": "human", "check": "the owner signs off"}
    normalised = gates.validate_gate(clockless, require=())
    assert normalised["max_park_seconds"] is None
    assert normalised["on_timeout"] is None
    # With the clock group entirely absent, the human-must-name-a-verifier
    # rule (a clock-group rule) does not fire — a harness with no verifier
    # concept yet is not blocked from declaring a human gate.
    assert normalised["verifier"] is None


def test_require_clock_still_demands_it_even_with_no_ceiling():
    with pytest.raises(Refusal):
        gates.validate_gate({"kind": "deterministic", "check": "x"}, require=("clock",))


def test_ceiling_is_enforced_only_when_passed():
    huge = {
        "kind": "deterministic",
        "check": "x",
        "max_park_seconds": PLANE_CEILING * 100,
        "on_timeout": "fail",
    }
    # No ceiling argument: no ceiling enforced.
    normalised = gates.validate_gate(huge, require=("clock",))
    assert normalised["max_park_seconds"] == PLANE_CEILING * 100
    # Ceiling supplied: enforced.
    with pytest.raises(Refusal):
        gates.validate_gate(huge, require=("clock",), max_park_seconds_ceiling=PLANE_CEILING)


def test_rubric_on_deterministic_is_refused():
    with pytest.raises(Refusal) as caught:
        validate(dict(DETERMINISTIC, rubric="does this look right?"))
    assert caught.value.code == gates.GATE_INVALID
    assert "nothing would ever read it" in caught.value.message


def test_rubric_on_judged_is_accepted():
    normalised = validate(dict(JUDGED, rubric="the migration is reversible"))
    assert normalised["rubric"] == "the migration is reversible"


def test_judge_route_on_human_is_refused():
    human = {
        "kind": "human",
        "check": "the owner signs off",
        "max_park_seconds": 900,
        "on_timeout": "fail",
        "verifier": "dana",
    }
    with pytest.raises(Refusal) as caught:
        validate(dict(human, judge_route="council"))
    assert caught.value.code == gates.GATE_INVALID
    assert "nothing would ever read it" in caught.value.message


def test_judge_route_on_judged_is_accepted():
    normalised = validate(dict(JUDGED, judge_route="council"))
    assert normalised["judge_route"] == "council"


def test_execution_fields_cwd_and_timeout_s():
    normalised = validate(dict(DETERMINISTIC, cwd="src/billing", timeout_s=120))
    assert normalised["cwd"] == "src/billing"
    assert normalised["timeout_s"] == 120


def test_timeout_s_rejects_a_bool():
    with pytest.raises(Refusal):
        validate(dict(DETERMINISTIC, timeout_s=True))


def test_an_unknown_top_level_field_is_refused():
    with pytest.raises(Refusal) as caught:
        validate(dict(DETERMINISTIC, tiemout_s=5))
    assert "unknown gate field(s)" in caught.value.message


def test_a_gate_that_is_not_a_dict_is_refused():
    with pytest.raises(Refusal) as caught:
        gates.validate_gate("deterministic")
    assert caught.value.code == gates.GATE_INVALID


def test_a_normalised_gate_validates_as_itself():
    """A store re-validates on every write. If the contract refused its own
    output, every gated bead would be rejected the first time anything else
    on it changed — found adopting the schema in the Harness, 2026-09-04."""
    from asop.gates import validate_gate, SCHEMA_VERSION
    once = validate_gate({"class": "deterministic", "check": "pytest -q", "timeout_s": 5})
    assert once["schema_version"] == SCHEMA_VERSION
    assert validate_gate(once) == once


def test_a_foreign_schema_version_is_refused():
    from asop.gates import validate_gate
    from asop.errors import Refusal
    import pytest
    with pytest.raises(Refusal, match="schema_version=99"):
        validate_gate({"kind": "deterministic", "check": "x", "schema_version": 99})
