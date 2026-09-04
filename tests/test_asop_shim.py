"""The identity every shim rests on, asserted from the plane side.

`agentco/errors.py`, `agentco/gates.py` and `agentco/sop.py` are thin wrappers
over `asop`. The one thing that makes that safe is that they re-export the
SAME objects rather than redefining them: a `try/except Refusal` written
against either side catches the other's. Three docstrings claimed this file
asserted it; the file did not exist (found by review, 2026-09-04).
"""

from __future__ import annotations

import pytest

import asop.errors
import asop.gates
import asop.sop
from agentco import errors, gates, sop


def test_refusal_is_one_class_not_two():
    assert errors.Refusal is asop.errors.Refusal


def test_a_refusal_raised_by_asop_is_caught_as_the_plane_refusal():
    with pytest.raises(errors.Refusal):
        raise asop.errors.Refusal(
            code="sop_refused", message="from the contract", remediation="none needed"
        )


def test_a_refusal_raised_by_the_plane_is_caught_as_the_asop_refusal():
    with pytest.raises(asop.errors.Refusal):
        raise errors.Refusal(
            code="sop_refused", message="from the plane", remediation="none needed"
        )


def test_the_gate_vocabulary_is_shared_by_identity():
    """Kinds and timeout outcomes are the contract's objects, not copies."""
    assert gates.GATE_KINDS is asop.gates.GATE_KINDS
    assert gates.ON_TIMEOUT is asop.gates.ON_TIMEOUT


def test_the_sop_record_is_shared_by_identity():
    assert sop.SOP is asop.sop.SOP
    assert sop.SopStatus is asop.sop.SopStatus
