"""The revision policy — what an agent may not do to a procedure.

Self-revision is the ASOP property with a behavioural claim behind it, and it
is also a write path into the procedures people follow. Before this module the
path was unpoliced: any actor with write access could revise any SOP to
anything. That is acceptable while every reviser is a person. It is not
acceptable the moment an agent can propose a revision, because an agent that
learns "this step is slow" and removes the step is doing exactly what the loop
asks of it — and the step it removed may have been the one that keeps a
delinquency run from sending letters that cannot be unsent.

Three rules, generic to every registry. They are about **trust domains**, not
about content: a human reviser is bound by none of them, and what they protect
— which steps exist, which are human, which carry `money` — is registry
content the plane never holds.

1. **Protected tags freeze a step against agents.** A version carrying a
   protected tag (`money` and `irreversible` by default; a registry may add its
   own, never remove these) cannot be revised or activated by an agent at all,
   and no agent revision may add or remove a protected tag on any step. Only a
   human decides what is protected, and only a human touches what is.
2. **A step's class ratchets toward human only.** An agent may turn an agent
   step into a human one; it may never turn a human step into an agent one, or
   into an unclassified one, which is the same demotion by omission.
3. **No undoing a human.** An agent revision may not move any field into a
   state a human moved it away from — bring back what a human removed, remove
   what a human added, or restore what a human replaced — unless a human later
   moved it back. Computable because versions are immutable and each records
   who authored it. Versions written before authorship was recorded impose
   nothing: the plane does not invent a human it never saw.

**Who is human is declared, never inferred.** The operator names human actors
(`AGENTCO_HUMANS`, or `create_app(humans=...)`); everyone else is an agent. The
default fails closed — an undeclared registry treats every reviser as an agent
and polices all of them — because the alternative, an agent that becomes
human by asserting it, would make the whole policy a suggestion.

Every refusal names the rule and the field so the caller learns what to change
rather than that something was wrong. A refused revision writes nothing.
"""

from __future__ import annotations

import os
from typing import Iterable, Optional, Sequence

HUMANS_ENV_VAR = "AGENTCO_HUMANS"
PROTECTED_TAGS_ENV_VAR = "AGENTCO_PROTECTED_TAGS"

HUMAN = "human"
AGENT = "agent"
KINDS = (HUMAN, AGENT)

#: The tags every registry protects. A registry may ADD to this set through
#: `AGENTCO_PROTECTED_TAGS`; nothing can remove these two, because a registry
#: that could un-protect `money` by configuration has a policy that is only as
#: strong as its environment file.
DEFAULT_PROTECTED_TAGS: frozenset[str] = frozenset({"money", "irreversible"})

RULE_PROTECTED = "protected"
RULE_RATCHET = "ratchet"
RULE_NO_UNDO = "no-undo"


class RevisionPolicyError(ValueError):
    """An agent tried to do to a procedure what only a human may.

    A `ValueError` so that every caller already catching the library's
    contract errors catches this one too, rather than seeing it surface as a
    500 from a path that was refusing correctly.
    """

    def __init__(self, rule: str, message: str):
        super().__init__(message)
        self.rule = rule


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #


def _split(value: Optional[str]) -> frozenset[str]:
    if not value:
        return frozenset()
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def humans_from_env(value: Optional[str] = None) -> frozenset[str]:
    """The declared human actors. Comma-separated actor names, exact spelling.

    Exact rather than case-folded: the key file already refuses two identities
    that differ only by case, so there is one spelling to match.
    """
    return _split(value if value is not None else os.environ.get(HUMANS_ENV_VAR))


def protected_tags_from_env(value: Optional[str] = None) -> frozenset[str]:
    """The defaults plus whatever the registry adds. Never fewer than the defaults."""
    extra = _split(value if value is not None else os.environ.get(PROTECTED_TAGS_ENV_VAR))
    return DEFAULT_PROTECTED_TAGS | frozenset(tag.lower() for tag in extra)


def kind_of(actor: Optional[str], humans: Iterable[str]) -> str:
    """`human` if the operator declared this actor human; `agent` otherwise.

    `None` — an unauthenticated in-process caller — is an agent. Fail closed.
    """
    return HUMAN if actor is not None and actor in set(humans) else AGENT


# --------------------------------------------------------------------------- #
# the check
# --------------------------------------------------------------------------- #


def _class_of(sop) -> str:
    return getattr(sop, "executor", None) or AGENT


def forbidden_states(
    history: Sequence,
    *,
    scalar_fields: Sequence[str],
    list_fields: Sequence[str],
) -> tuple[set[tuple], set[tuple]]:
    """Every field state a human moved away from, and not since moved back to.

    Walks the versions in order. For each version a HUMAN authored, compare it
    with its predecessor: a scalar the human changed makes the OLD value
    forbidden for that field; a list element the human removed makes
    `present` forbidden for that element, one they added makes `absent`
    forbidden. A later human moving a field back lifts the ban — the rule is
    about not undoing people, not about freezing the past.

    Agent-authored versions contribute nothing. Neither do versions with no
    recorded author: those predate authorship, and inventing a human for them
    would forbid states nobody decided against.
    """
    scalars: set[tuple] = set()
    lists: set[tuple] = set()
    ordered = sorted(history, key=lambda s: s.version)
    for prev, cur in zip(ordered, ordered[1:]):
        if getattr(cur, "author_kind", None) != HUMAN:
            continue
        for name in scalar_fields:
            before, after = getattr(prev, name), getattr(cur, name)
            if before != after:
                scalars.add((name, before))
                scalars.discard((name, after))
        for name in list_fields:
            before, after = set(getattr(prev, name) or ()), set(getattr(cur, name) or ())
            for element in before - after:
                lists.add((name, element, "present"))
                lists.discard((name, element, "absent"))
            for element in after - before:
                lists.add((name, element, "absent"))
                lists.discard((name, element, "present"))
    return scalars, lists


def check_revision(
    *,
    history: Sequence,
    baseline,
    proposed,
    reviser_kind: str,
    protected_tags: Iterable[str],
    scalar_fields: Sequence[str],
    list_fields: Sequence[str],
    action: str = "revise",
) -> None:
    """Refuse `proposed` if `reviser_kind` is an agent and any rule forbids it.

    `baseline` is the version the change is measured against — the latest
    version for a revision, the active one for an activation. `proposed` is
    the version that would result. `history` is every version of the SOP,
    including `baseline`, and is what rule 3 is computed from.

    Humans pass unconditionally. Nothing here is about what a change says;
    it is about who is allowed to say it.
    """
    if reviser_kind == HUMAN:
        return
    if reviser_kind != AGENT:
        raise ValueError(f"reviser_kind must be one of {KINDS}, got {reviser_kind!r}")

    protected = frozenset(protected_tags)
    baseline_tags = frozenset(getattr(baseline, "tags", None) or ())
    proposed_tags = frozenset(getattr(proposed, "tags", None) or ())

    frozen = sorted(baseline_tags & protected)
    if frozen:
        raise RevisionPolicyError(
            RULE_PROTECTED,
            f"policy rule '{RULE_PROTECTED}': {baseline.sop_id} v{baseline.version} "
            f"carries protected tag(s) {frozen}, so an agent may not {action} it. "
            f"A protected step is changed by a human or not at all.",
        )
    touched = sorted((baseline_tags ^ proposed_tags) & protected)
    if touched:
        raise RevisionPolicyError(
            RULE_PROTECTED,
            f"policy rule '{RULE_PROTECTED}': an agent may not add or remove the "
            f"protected tag(s) {touched}. Only a human decides what is protected.",
        )

    if _class_of(baseline) == HUMAN and _class_of(proposed) != HUMAN:
        raise RevisionPolicyError(
            RULE_RATCHET,
            f"policy rule '{RULE_RATCHET}': {baseline.sop_id} v{baseline.version} is a "
            f"human step and an agent may not make it "
            f"{getattr(proposed, 'executor', None) or 'unclassified'}. The class "
            f"ratchets toward human; only a human ratchets it back.",
        )

    scalars, lists = forbidden_states(
        history, scalar_fields=scalar_fields, list_fields=list_fields
    )
    for name in scalar_fields:
        before, after = getattr(baseline, name), getattr(proposed, name)
        if before != after and (name, after) in scalars:
            raise RevisionPolicyError(
                RULE_NO_UNDO,
                f"policy rule '{RULE_NO_UNDO}': a human moved '{name}' away from "
                f"{after!r} and an agent may not move it back. If the human was "
                f"wrong, a human says so.",
            )
    for name in list_fields:
        before, after = set(getattr(baseline, name) or ()), set(getattr(proposed, name) or ())
        for element in sorted(after - before):
            if (name, element, "present") in lists:
                raise RevisionPolicyError(
                    RULE_NO_UNDO,
                    f"policy rule '{RULE_NO_UNDO}': a human removed {element!r} from "
                    f"'{name}' and an agent may not put it back.",
                )
        for element in sorted(before - after):
            if (name, element, "absent") in lists:
                raise RevisionPolicyError(
                    RULE_NO_UNDO,
                    f"policy rule '{RULE_NO_UNDO}': a human added {element!r} to "
                    f"'{name}' and an agent may not remove it.",
                )
