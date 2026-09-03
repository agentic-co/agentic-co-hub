"""The package-level surface: what `import asop` promises, and the identity
the plane's shims are built on (`agentco.errors.Refusal is asop.errors.Refusal`
is asserted from the plane side, in `tests/test_asop_shim.py`, once this
package is installed as a workspace member — nothing in this file reaches
across into `agentco`, so this package stays installable on its own).
"""

from __future__ import annotations

import asop
from asop.errors import Refusal


def test_schema_version_is_exported_and_stable_for_this_release():
    assert asop.SCHEMA_VERSION == 1


def test_refusal_survives_a_pickle_round_trip():
    """The reason `Refusal.__post_init__` populates `args` — see its
    docstring. Nothing in this package crosses a process boundary today, but
    the property is cheap to keep proven."""
    import pickle

    original = Refusal(code="gate_invalid", message="bad", remediation="fix it")
    restored = pickle.loads(pickle.dumps(original))
    assert restored.code == original.code
    assert restored.message == original.message
    assert restored.remediation == original.remediation


def test_refusal_to_dict_shape():
    refusal = Refusal(code="gate_invalid", message="bad", remediation="fix it")
    assert refusal.to_dict() == {
        "state": "refused",
        "code": "gate_invalid",
        "message": "bad",
        "remediation": "fix it",
    }
