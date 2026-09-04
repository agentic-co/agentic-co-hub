"""This plane's operator declarations, and a shim over the shared revision policy.

**The revision policy itself has moved.** The four rules an agent reviser is
bound by — protected tags, the ratchet, no-undo, and the `human_only` verbs —
are contract, not plane: a harness executing ASOP work polices the same writes
against the same records, and a rule that held here and not there would be a
rule with a door beside it. They now live in `asop.revision`, the package
shared with any harness speaking the ASOP contract (`packages/asop/ASOP.md`
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
- `AGENTCO_VERIFIERS` — undeclared checks NOTHING, and `verify` stays
  self-asserted. The alternative is a registry where nobody may verify, which
  does not become safer: every judged gate then resolves on its clock, which
  is work approved by a timer.
- `AGENTCO_ADJUDICATORS` — undeclared allows only declared humans. This one
  fails closed where verifiers fail open, because what degrades when an agent
  grades the loop that revises its own procedures is the evidence base rather
  than throughput, and nobody notices.
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

VERIFIERS_ENV_VAR = "AGENTCO_VERIFIERS"
VERIFY_CAPABILITY = "verify"

# --------------------------------------------------------------------------- #
# verifier binding — who may hold the `verify` capability
# --------------------------------------------------------------------------- #


def verifiers_from_env(value: Optional[str] = None) -> frozenset[str]:
    """The actors the operator declared as verifiers. Comma-separated, exact spelling.

    Empty means UNDECLARED, and undeclared means the `verify` capability stays
    self-asserted — routing hygiene, as it always was. That is the one place
    this module fails open rather than closed, deliberately: a registry where
    nobody may verify does not become safer, it resolves every judged gate on
    the clock, which is work approved on a timer. Declaring the set is what
    turns the capability into a rail, and `verifier_status` says out loud
    whether the set is declared.
    """
    return _split(value if value is not None else os.environ.get(VERIFIERS_ENV_VAR))


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

ADJUDICATORS_ENV_VAR = "AGENTCO_ADJUDICATORS"


def adjudicators_from_env(value: Optional[str] = None) -> frozenset[str]:
    """The routes the operator declared as adjudicators. Same shape as verifiers.

    Empty means UNDECLARED, and undeclared means **only humans adjudicate** —
    the opposite default from `verifiers_from_env`, and deliberately so. An
    undeclared verifier set fails open because the alternative is every judged
    gate resolving on its clock, which is work approved by a timer. An
    undeclared adjudicator set fails CLOSED because the alternative is an
    agent grading the loop that revises the procedures it follows, and the
    thing that would degrade is not throughput but the evidence base. The
    human-only posture is the default; a declared route is the opt-in.
    """
    return _split(value if value is not None else os.environ.get(ADJUDICATORS_ENV_VAR))


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
