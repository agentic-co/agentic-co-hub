"""This plane's operator declarations, and a shim over the shared revision policy.

**The revision policy itself has moved.** The four rules an agent reviser is
bound by — protected tags, the ratchet, no-undo, and the `human_only` verbs —
are contract, not plane: a harness executing ASOP work polices the same writes
against the same records, and a rule that held here and not there would be a
rule with a door beside it. They now live in `asop.revision`, the package
shared with any harness speaking the ASOP contract (`ASOP.md` (https://github.com/mabidoli/asop)
§6.4), and this module re-exports them so every existing caller and test in
this repo is unchanged. `agentco.policy.RevisionPolicyError is
asop.revision.RevisionPolicyError` holds, and that identity is the point: two
exception classes would mean a `try/except` written against one side silently
missing refusals raised by the other.

What stays here is what only this plane has an opinion on: who may hold the
`verify` capability, and who may adjudicate a divergence.

**What each UNDECLARED set means, in one place, because they differ and the
difference is deliberate.** Read all four before "fixing" any one of them to
match another:

- `AGENTCO_HUMANS` (in `asop.revision`) — undeclared polices EVERYONE. An
  agent that could become human by asserting it makes the policy a suggestion.
- `ASOP_VERIFIERS` — undeclared authenticates NOBODY as a verifier route
  (ASOP §5.3, §9). A `judged` or `human` verdict is accepted only from a
  declared verifier or a declared human (`may_verify`); an undeclared set
  leaves only declared humans, and with neither declared no verdict lands —
  the gate waits for its park clock. This used to fail open on the argument
  that a registry nobody is in "resolves every judged gate on the clock".
  It does not: the clock path (`verifiers.sweep_park_clocks` ->
  `Queue.resolve_by_default`) never attests, so refusing an unresolved
  attester cannot make the timer fire more often. The timer hazard is real
  and is reported, not prevented, by `verifier_status`. The CLAIM side
  (`bind_capabilities`) still checks nothing while undeclared: routing a
  vehicle is hygiene, the verdict is the authority.
- `ASOP_ADJUDICATORS` — undeclared allows only declared humans, the same
  shape as verifiers. What degrades when an agent grades the loop that
  revises its own procedures is the evidence base rather than throughput,
  and nobody notices.
- the registry's ACTORS, from the key file (`SopLibrary.declare`) — undeclared
  checks NOTHING. An empty key file is what an in-process caller, a
  JSONL-only install and every library test have, so refusing every run on
  its absence would be refusing on missing configuration rather than on a
  fact — and it would break the standalone and in-process cases the contract
  requires to work.
"""

from __future__ import annotations

import os
from typing import Iterable, Optional

from asop.revision import (  # noqa: F401 - re-exported; see the module docstring
    AGENT,
    ASOP_SCALAR_FIELDS,
    DEFAULT_PROTECTED_TAGS,
    HUMAN,
    HUMANS_ENV_VAR,
    KINDS,
    PROTECTED_TAGS_ENV_VAR,
    RULE_HUMAN_ONLY,
    RULE_NO_UNDO,
    RULE_PROTECTED,
    RULE_RATCHET,
    STEP_LIST_FIELDS,
    STEP_SCALAR_FIELDS,
    RevisionPolicyError,
    _class_of,
    _refuse_first_activation,
    _role_class,
    _split,
    _step_class,
    _steps_by_key,
    asop_forbidden_states,
    check_asop_revision,
    check_revision,
    forbidden_states,
    humans_from_env,
    kind_of,
    protected_tags_from_env,
    require_human,
    steps_by_key,
)

#: The standard's name, with the pre-split one read as a deprecated fallback —
#: the same courtesy `asop.revision` extends to ASOP_HUMANS / ASOP_PROTECTED_TAGS,
#: and for the same reason: a standard should not ask an adopter to set a
#: variable named after somebody's company (ASOP.md §9 names all four).
VERIFIERS_ENV_VAR = "ASOP_VERIFIERS"
LEGACY_VERIFIERS_ENV_VAR = "AGENTCO_VERIFIERS"
VERIFY_CAPABILITY = "verify"


def _declared(name: str, legacy: str) -> Optional[str]:
    """The standard's variable, falling back to the pre-split name.

    A deprecation with a door, mirroring `asop.revision._from_env`: a
    deployment that already sets the AGENTCO_* name keeps working unchanged,
    and nothing new is written against it. Checked for presence rather than
    truthiness — an operator who deliberately declares an EMPTY set means
    something by it, and falling through to the legacy name would silently
    override that.
    """
    found = os.environ.get(name)
    return found if found is not None else os.environ.get(legacy)

# --------------------------------------------------------------------------- #
# verifier binding — who may hold the `verify` capability
# --------------------------------------------------------------------------- #


def verifiers_from_env(value: Optional[str] = None) -> frozenset[str]:
    """The actors the operator declared as verifiers. Comma-separated, exact spelling.

    Empty means UNDECLARED, and an undeclared set resolves NOBODY (ASOP §5.3,
    §9): no route may answer a `judged` or `human` gate, and only a declared
    human can (`may_verify`). On the claim side it still binds nothing — the
    `verify` capability stays self-asserted for ROUTING a vehicle — and
    `verifier_status` says out loud which of the two this queue is.
    """
    return _split(value if value is not None else _declared(
        VERIFIERS_ENV_VAR, LEGACY_VERIFIERS_ENV_VAR))


def may_verify(actor: Optional[str], *, verifiers: Iterable[str],
               humans: Iterable[str]) -> bool:
    """Whether this actor may answer a `judged` or `human` gate at all (ASOP §5.3, §9).

    A declared verifier ROUTE, or a declared HUMAN — the same two-registry
    shape as `may_adjudicate`, and for the same reason: the registries declare
    routes, and a person verifies by being a declared person, not by also being
    listed as a route. Nothing declared resolves nobody; `None` never resolves.

    This is resolution, not authentication. `actor` is the identity the
    transport already authenticated; this answers whether the operator
    declared it for the role. Everything else a verdict must satisfy —
    the gate's named person, executor separation, the `verify` capability
    binding — is checked after, and separately.
    """
    if not actor:
        return False
    return actor in frozenset(verifiers) or kind_of(actor, humans) == HUMAN


def bind_capabilities(
    actor: Optional[str],
    capabilities: Optional[Iterable[str]],
    verifiers: Iterable[str],
) -> tuple[frozenset[str], bool]:
    """The capabilities this actor actually holds, given the operator's declaration.

    When verifiers are declared, `verify` counts only for a declared actor;
    anyone else's claim to it is dropped here, before the capability gate
    compares it with anything. Returns the bound set and whether `verify` was
    stripped, so the refusal downstream can name the declaration rather than
    telling a node to "declare verify" it already did.
    """
    held = frozenset(capabilities or ())
    declared = frozenset(verifiers)
    if declared and VERIFY_CAPABILITY in held and actor not in declared:
        return held - {VERIFY_CAPABILITY}, True
    return held, False


def _hashable(value):
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_hashable(v) for v in value)
    return value
# --------------------------------------------------------------------------- #
# adjudicator binding — who may judge a divergence (ASOP.md §6.1, decision 6)
# --------------------------------------------------------------------------- #

ADJUDICATORS_ENV_VAR = "ASOP_ADJUDICATORS"
LEGACY_ADJUDICATORS_ENV_VAR = "AGENTCO_ADJUDICATORS"


def adjudicators_from_env(value: Optional[str] = None) -> frozenset[str]:
    """The routes the operator declared as adjudicators. Same shape as verifiers.

    Empty means UNDECLARED, and undeclared means **only humans adjudicate** —
    the same default as `verifiers_from_env` (see `may_verify`). An undeclared
    adjudicator set fails CLOSED because the alternative is an agent grading
    the loop that revises the procedures it follows, and the thing that would
    degrade is not throughput but the evidence base. The human-only posture is
    the default; a declared route is the opt-in.
    """
    return _split(value if value is not None else _declared(
        ADJUDICATORS_ENV_VAR, LEGACY_ADJUDICATORS_ENV_VAR))


def may_adjudicate(actor: Optional[str], *, humans: Iterable[str],
                   adjudicators: Iterable[str]) -> bool:
    """Whether this actor may write an adjudication at all.

    Distinctness from the executor is a separate check, enforced where the
    executors are known (`work.adjudication_record`). This answers only the
    prior question: is this party entitled to judge anything.
    """
    if kind_of(actor, humans) == HUMAN:
        return True
    return bool(actor) and actor in frozenset(adjudicators)
