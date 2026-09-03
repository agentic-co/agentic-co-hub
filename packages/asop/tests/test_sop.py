"""The SOP record contract — `validate_fields`, the `SOP` dataclass, and
`SopStatus` — tested directly, without a `SopLibrary` store. Ported from the
plane's `tests/test_sop.py`, which exercised the same rules through
`library.create(...)`.
"""

from __future__ import annotations

import pytest

from asop.sop import MAX_COMMON_MISTAKES, SOP, SopContractError, SopStatus, validate_fields


def test_a_partial_sop_is_legal():
    """An SOP is filled in as the work is understood. Demanding all fields
    up front means it is skipped when it is cheapest to start."""
    fields = validate_fields({"purpose": "decide whether it is urgent"})
    assert fields["purpose"] == "decide whether it is urgent"
    assert "trigger" not in fields


def test_an_empty_sop_is_refused():
    with pytest.raises(SopContractError):
        validate_fields({})


def test_a_present_but_blank_field_is_refused():
    with pytest.raises(SopContractError) as exc:
        validate_fields({"purpose": "   "})
    assert "does not answer" in str(exc.value)


def test_an_unknown_field_is_refused_and_the_message_explains_steps():
    with pytest.raises(SopContractError) as exc:
        validate_fields({"steps": "do the thing"})
    assert "no 'steps' field" in str(exc.value)


def test_an_empty_common_mistakes_list_is_refused():
    with pytest.raises(SopContractError) as exc:
        validate_fields({"purpose": "x", "common_mistakes": []})
    assert "never make silently" in str(exc.value)


def test_common_mistakes_is_capped():
    with pytest.raises(SopContractError) as exc:
        validate_fields({"purpose": "x", "common_mistakes": ["a", "b", "c", "d"]})
    assert f"cap is {MAX_COMMON_MISTAKES}" in str(exc.value)


def test_a_malformed_common_mistakes_is_refused_rather_than_repaired():
    with pytest.raises(SopContractError):
        validate_fields({"purpose": "x", "common_mistakes": "not a list"})


def test_tags_are_folded_and_deduplicated():
    fields = validate_fields({"purpose": "x", "tags": ["Money", "money", "Irreversible"]})
    assert fields["tags"] == ["money", "irreversible"]


def test_an_invalid_executor_is_refused():
    with pytest.raises(SopContractError):
        validate_fields({"purpose": "x", "executor": "robot"})


def test_a_valid_executor_is_accepted():
    fields = validate_fields({"purpose": "x", "executor": "human"})
    assert fields["executor"] == "human"


def test_next_sop_must_be_a_non_empty_string():
    with pytest.raises(SopContractError):
        validate_fields({"purpose": "x", "next_sop": "   "})
    assert validate_fields({"purpose": "x", "next_sop": "sop-2"})["next_sop"] == "sop-2"


def test_proposals_are_deduplicated():
    fields = validate_fields({"purpose": "x", "proposals": ["do it right", "do it right"]})
    assert fields["proposals"] == ["do it right"]


def test_the_sop_dataclass_round_trips_through_json():
    sop = SOP(sop_id="triage", version=1, title="Triage", purpose="decide urgency", status=SopStatus.ACTIVE)
    restored = SOP.from_json(sop.to_json())
    assert restored.sop_id == sop.sop_id
    assert restored.version == 1
    assert restored.purpose == "decide urgency"
    assert restored.status == SopStatus.ACTIVE


def test_the_sop_ref_carries_both_id_and_version():
    sop = SOP(sop_id="triage", version=3, title="Triage")
    assert sop.ref == {"sop_id": "triage", "version": 3}
