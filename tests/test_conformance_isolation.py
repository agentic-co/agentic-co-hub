"""A conformance scenario declares its own registries. The operator's do not leak in.

`Scenario.env()` unsets every name in `STORE_ENV_VARS` before pinning what the
scenario declares, and the comment above that tuple records why: the second
party on P5.V ran `conform` with `AGENTCO_DB` set and watched it write 29 items
into their live database.

**The `AGENTCO_*` → `ASOP_*` rename opened a hole in exactly that isolation.**
The canonical names were never added to `STORE_ENV_VARS`, so they are not unset,
and `policy._declared()` prefers the canonical name over the legacy one — which
means an operator with `ASOP_VERIFIERS` exported had their real registry
silently override the scenario's. Measured before the fix, with
`ASOP_HUMANS=alice ASOP_VERIFIERS=carol ASOP_ADJUDICATORS=alice`:

    verifier-binding/mcp   carol attest      refused -> ok
    judged-gate/mcp        carol adjudicate  refused -> ok
    procedure/mcp          alice sop_revise  refused -> ok

Three refusals became acceptances. That is the direction that matters: the
scenarios exist to prove the plane refuses the wrong actor, and the operator's
environment was talking them out of it.

**It is transport-specific, and that is worth knowing.** `core` and `http` are
unaffected because `World` hands the registries to `Queue`/`create_app`
explicitly. Only the MCP handlers resolve policy from the environment
(`mcp_server.py` → `policy.humans_from_env()`, `policy.verifiers_from_env()`),
so only `*/mcp` flipped. A test that checked the core path would have passed
while the hole was wide open.
"""

from __future__ import annotations

import pytest

from asop import revision as _revision

from agentco import policy
from agentco.conformance import STORE_ENV_VARS, run_scenario

#: Every environment variable that was renamed, as (canonical, legacy). Read off
#: the modules that define them rather than retyped, so this cannot drift from
#: what `_declared()` actually consults. Two pairs live in `policy` and two in
#: the spec package's `asop.revision` — which is itself part of why the rename
#: landed half-done: the names are not all in one place.
RENAMED = (
    (policy.VERIFIERS_ENV_VAR, policy.LEGACY_VERIFIERS_ENV_VAR),
    (policy.ADJUDICATORS_ENV_VAR, policy.LEGACY_ADJUDICATORS_ENV_VAR),
    (_revision.HUMANS_ENV_VAR, _revision.LEGACY_HUMANS_ENV_VAR),
    (_revision.PROTECTED_TAGS_ENV_VAR, _revision.LEGACY_PROTECTED_TAGS_ENV_VAR),
)


@pytest.mark.parametrize("canonical,legacy", RENAMED)
def test_both_spellings_of_a_renamed_variable_are_pinned(canonical, legacy):
    """The static half: a fallback pair is only pinned if BOTH names are.

    Pinning one spelling of a variable that is read under two spellings is not
    isolation, it is isolation-shaped. This is the check that would have caught
    the rename going in half-done, in a millisecond, without running a scenario.
    """
    assert legacy in STORE_ENV_VARS, f"{legacy} was never pinned"
    assert canonical in STORE_ENV_VARS, (
        f"{canonical} is read by policy._declared() in PREFERENCE to {legacy}, "
        f"but is not unset by the scenario pin — an operator who exports it has "
        f"it override every scenario's own declaration."
    )


def test_an_exported_verifier_registry_does_not_override_the_scenario(monkeypatch):
    """The behavioural half, on the transport that actually reads the environment.

    `verifier-binding` declares exactly one verifier, `bob`. `carol` attesting
    must be refused and `bob` attesting must succeed — REGARDLESS of what the
    person running the suite happens to have exported. Before the fix this
    returned the exact opposite of both.
    """
    monkeypatch.setenv(policy.VERIFIERS_ENV_VAR, "carol")

    outcomes = {o["step"]: o["state"] for o in run_scenario("verifier-binding", "mcp")["outcomes"]}

    assert outcomes["carol attest"] == "refused", (
        "the operator's exported verifier registry bound instead of the "
        "scenario's — carol is not a verifier in this scenario"
    )
    assert outcomes["bob attest"] == "ok", (
        "the scenario's declared verifier was displaced by the operator's"
    )


def test_an_exported_human_registry_does_not_override_the_scenario(monkeypatch):
    """The same hole through a different door — `sop_revise`'s author policy.

    Included because the verifier case alone would suggest this is about the
    `verify` capability. It is not: it is about every registry `_declared()`
    resolves, and `procedure` proves it on the revision path.
    """
    monkeypatch.setenv(policy.HUMANS_ENV_VAR, "alice")

    outcomes = {o["step"]: o["state"] for o in run_scenario("procedure", "mcp")["outcomes"]}

    assert outcomes["alice sop_revise"] == "refused", (
        "alice became a human because the operator's environment said so — "
        "the scenario declares only carol"
    )
    assert outcomes["carol sop_revise"] == "ok"


def test_the_pin_is_a_superset_of_what_the_scenario_declares(monkeypatch):
    """Whatever `env()` PINS must also be in the set it UNSETS first.

    `env()` builds `{name: None for name in STORE_ENV_VARS}` and then writes the
    scenario's declarations over the top. A name it writes but never listed
    would still be pinned — but it would not be unset for scenarios that declare
    nothing, which is the same hole in a narrower window.
    """
    from agentco.conformance import World

    world = World.__new__(World)
    world.humans = frozenset({"carol"})
    world.verifiers = frozenset({"bob"})
    world.adjudicators = frozenset({"bob"})

    unlisted = sorted(set(World.env(world)) - set(STORE_ENV_VARS))
    assert not unlisted, f"env() pins name(s) it does not unset first: {unlisted}"
