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
VERIFIERS_ENV_VAR = "AGENTCO_VERIFIERS"
VERIFY_CAPABILITY = "verify"

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


# --------------------------------------------------------------------------- #
# ASOP v3 — the same three rules, per step
# --------------------------------------------------------------------------- #

#: Rules 1-3 were written for a record that WAS one step. In v3 the record is a
#: sequence and the step is the unit, so each rule is evaluated per step and
#: the record-level fields (title, purpose, trigger, task_type) are diffed the
#: way a scalar always was. Nothing about what the rules MEAN changed; only
#: what they range over.
ASOP_SCALAR_FIELDS = ("title", "task_type", "purpose", "trigger")
STEP_SCALAR_FIELDS = (
    "role", "purpose", "entry_check", "inputs", "definition_of_done",
    "validation", "write_back",
)
STEP_LIST_FIELDS = ("common_mistakes", "tags", "proposals")

#: The fourth rule, and the only one that is about a verb rather than a diff:
#: `retire` and `promote` are human verbs in v3 (ASOP.md §4, §6.5). An agent
#: may draft; only a human withdraws a procedure or opens the door to a new
#: one.
RULE_HUMAN_ONLY = "human_only"


def require_human(reviser_kind: str, action: str, *, because: str) -> None:
    """Refuse `action` unless the operator declared this reviser human.

    Separate from `check_revision` because it is not a diff: there is no
    proposed version to compare. `retire` and `promote` are refused to an
    agent whatever they would produce.
    """
    if reviser_kind == HUMAN:
        return
    raise RevisionPolicyError(
        RULE_HUMAN_ONLY,
        f"policy rule '{RULE_HUMAN_ONLY}': '{action}' is a human verb. {because} "
        f"Who is human is declared by the operator ({HUMANS_ENV_VAR}), never "
        f"inferred — an undeclared registry polices everyone.",
    )


def _steps_by_key(asop) -> dict:
    """Steps keyed for comparison across versions.

    By NAME, because a step's position moves the moment one is inserted above
    it and a positional diff would then read every step below the insertion as
    rewritten. Where a version repeats a name — nothing in the record contract
    forbids it — that version falls back to position for ALL of its steps, so
    the two sides are never compared under two different keying rules.
    """
    steps = list(getattr(asop, "steps", None) or ())
    names = [getattr(s, "name", None) for s in steps]
    if len(set(names)) == len(names):
        return {name: step for name, step in zip(names, steps)}
    return {("#", getattr(s, "step", i + 1)): s for i, s in enumerate(steps)}


def _step_class(step) -> str:
    """`human` when the step is a human one, by either half of what makes it so.

    The role's `kind` says who does it and the gate's `kind` says who closes
    it. Either being human makes the step human for the ratchet, because
    demoting either one is the demotion the ratchet exists to refuse: a human
    step an agent can close is not a human step.
    """
    gate = getattr(step, "gate", None) or {}
    if gate.get("kind") == HUMAN:
        return HUMAN
    return AGENT


def _role_class(asop, step) -> str:
    roles = getattr(asop, "roles", None) or {}
    spec = roles.get(getattr(step, "role", None)) or {}
    return HUMAN if spec.get("kind") == HUMAN else AGENT


def asop_forbidden_states(history: Sequence) -> tuple[set[tuple], set[tuple]]:
    """`forbidden_states`, walked over the record AND every step.

    Scalars are keyed `(field,)` at the record level and `(step_key, field)`
    at the step level, so a human's edit to one step's `validation` never
    freezes another's.
    """
    scalars: set[tuple] = set()
    lists: set[tuple] = set()
    ordered = sorted(history, key=lambda s: s.version)
    for prev, cur in zip(ordered, ordered[1:]):
        if getattr(cur, "author_kind", None) != HUMAN:
            continue
        for name in ASOP_SCALAR_FIELDS:
            before, after = getattr(prev, name, None), getattr(cur, name, None)
            if before != after:
                scalars.add((None, name, before))
                scalars.discard((None, name, after))
        before_steps, after_steps = _steps_by_key(prev), _steps_by_key(cur)
        for key in set(before_steps) | set(after_steps):
            old, new = before_steps.get(key), after_steps.get(key)
            if old is None or new is None:
                continue
            for name in STEP_SCALAR_FIELDS:
                a, b = getattr(old, name, None), getattr(new, name, None)
                if a != b:
                    scalars.add((key, name, a))
                    scalars.discard((key, name, b))
            for name in STEP_LIST_FIELDS:
                a = set(getattr(old, name, None) or ())
                b = set(getattr(new, name, None) or ())
                for element in a - b:
                    lists.add((key, name, element, "present"))
                    lists.discard((key, name, element, "absent"))
                for element in b - a:
                    lists.add((key, name, element, "absent"))
                    lists.discard((key, name, element, "present"))
    return scalars, lists


def _refuse_first_activation(proposed, protected_tags: Iterable[str]) -> None:
    """The absolute form of the ratchet, for a version nobody has run yet.

    The differential rules answer "may an agent make THIS change". On a first
    activation there is no change to measure — the version being activated is
    the only one there has ever been — so the question becomes the simpler
    one the contract already answers: may an agent put a procedure carrying a
    protected step or a human role into service at all. It may not. A step
    tagged `money` is changed by a human or not at all, and a human role's
    step is one a person closes; putting either live is the same act as
    authoring it, and §6.4 reserves it either way.

    Note this refuses on the PROPOSED version's own content rather than on a
    diff, which is the whole point: the draft an agent wrote is exactly what
    nobody has reviewed.
    """
    # The protected half is handled absolutely by `check_asop_revision` itself
    # and needs nothing here. What a first activation uniquely exposes is the
    # RATCHET, which is differential everywhere else: there is no earlier
    # version whose class this one could be a demotion of.
    del protected_tags
    ident = f"{getattr(proposed, 'asop_id', '?')} v{getattr(proposed, 'version', '?')}"
    roles = getattr(proposed, "roles", None) or {}
    for step in getattr(proposed, "steps", None) or ():
        role = getattr(step, "role", None)
        human_role = (roles.get(role) or {}).get("kind") == HUMAN
        human_gate = (getattr(step, "gate", None) or {}).get("kind") == HUMAN
        if human_role or human_gate:
            raise RevisionPolicyError(
                RULE_RATCHET,
                f"policy rule '{RULE_RATCHET}': {ident} has never been active, and its "
                f"step {step.name!r} is a human step "
                f"({'human role' if human_role else 'human gate'}). An agent may not put "
                f"it into service: the class ratchets toward human, and a first "
                f"activation has no earlier version for that ratchet to read. A human "
                f"activates this one.",
            )


def check_asop_revision(
    *,
    history: Sequence,
    baseline,
    proposed,
    reviser_kind: str,
    protected_tags: Iterable[str],
    action: str = "revise",
    first_activation: bool = False,
) -> None:
    """`check_revision` for the v3 record. Same three rules, ranged over steps.

    What is new is only what a sequence makes expressible:

      * a step can be REMOVED. Removing a human step is the ratchet's
        demotion by deletion, and removing a protected one is touching what
        only a human touches — both refused, or the rules would hold for
        every edit except the one that deletes the thing they protect.
      * a step can be ADDED carrying a protected tag, which is rule 1's
        "an agent may not add or remove a protected tag" at the new grain.

    `first_activation` closes the hole a diff-shaped rule leaves open. Every
    check below compares `baseline` with `proposed`, and on a version's FIRST
    activation those are the same object — there is no prior active version to
    measure against — so an agent could activate a brand-new draft carrying a
    `money` step or a human role and no rule would fire, because nothing
    changed. That is the unpoliced door §6.4 exists to close: agents may
    draft, only humans activate. When the flag is set the checks are
    ABSOLUTE rather than differential.
    """
    if reviser_kind == HUMAN:
        return
    if first_activation:
        # Absolute, then fall through: activating an OLDER version while none
        # is active also lands here with a real diff to check, so the
        # differential rules below still have work to do.
        _refuse_first_activation(proposed, protected_tags)
    if reviser_kind != AGENT:
        raise ValueError(f"reviser_kind must be one of {KINDS}, got {reviser_kind!r}")

    protected = frozenset(protected_tags)
    before, after = _steps_by_key(baseline), _steps_by_key(proposed)
    ident = f"{getattr(baseline, 'asop_id', '?')} v{getattr(baseline, 'version', '?')}"

    for key in set(before) | set(after):
        old_tags = frozenset(getattr(before[key], "tags", None) or ()) if key in before else frozenset()
        new_tags = frozenset(getattr(after[key], "tags", None) or ()) if key in after else frozenset()
        touched = sorted((old_tags ^ new_tags) & protected)
        if touched:
            raise RevisionPolicyError(
                RULE_PROTECTED,
                f"policy rule '{RULE_PROTECTED}': an agent may not add or remove the "
                f"protected tag(s) {touched} on step "
                f"{getattr(after.get(key, before.get(key)), 'name', key)!r}. Only a "
                f"human decides what is protected.",
            )

    # ABSOLUTE, not differential. This module's own opening rule says a version
    # carrying a protected tag "cannot be revised or activated by an agent at
    # all", and an earlier draft of this function weakened it to "unless the
    # step's body is unchanged" — which let an agent rewrite everything AROUND
    # a `money` step and then activate the result, on the reasoning that it had
    # not touched the step itself. It had changed the procedure the step runs
    # in, which is the thing a person was meant to look at. A protected step is
    # changed by a human or not at all, and so is the procedure holding one.
    for source, where in ((before, "carries"), (after, "would carry")):
        for key in sorted(source, key=str):
            step = source[key]
            frozen = sorted(frozenset(getattr(step, "tags", None) or ()) & protected)
            if frozen:
                raise RevisionPolicyError(
                    RULE_PROTECTED,
                    f"policy rule '{RULE_PROTECTED}': {ident} {where} a step "
                    f"({step.name!r}) with protected tag(s) {frozen}, so an agent may "
                    f"not {action} it. A protected step is changed by a human or not "
                    f"at all — and so is the procedure it sits in, because changing "
                    f"what runs around a `money` step changes what that step does.",
                )

    for key in sorted(before, key=str):
        old = before[key]
        was_human = _step_class(old) == HUMAN or _role_class(baseline, old) == HUMAN
        if not was_human:
            continue
        new = after.get(key)
        if new is None:
            raise RevisionPolicyError(
                RULE_RATCHET,
                f"policy rule '{RULE_RATCHET}': step {old.name!r} of {ident} is a human "
                f"step and an agent may not remove it. Deleting a step is the one "
                f"demotion that leaves nothing behind to be classified; the class "
                f"ratchets toward human, and only a human ratchets it back.",
            )
        if _step_class(new) != HUMAN and _role_class(proposed, new) != HUMAN:
            raise RevisionPolicyError(
                RULE_RATCHET,
                f"policy rule '{RULE_RATCHET}': step {old.name!r} of {ident} is a human "
                f"step and an agent may not make it an agent one. The class ratchets "
                f"toward human; only a human ratchets it back.",
            )

    scalars, lists = asop_forbidden_states(history)
    for name in ASOP_SCALAR_FIELDS:
        a, b = getattr(baseline, name, None), getattr(proposed, name, None)
        if a != b and (None, name, b) in scalars:
            raise RevisionPolicyError(
                RULE_NO_UNDO,
                f"policy rule '{RULE_NO_UNDO}': a human moved {name!r} away from {b!r} "
                f"and an agent may not move it back. If the human was wrong, a human "
                f"says so.",
            )
    for key in set(before) & set(after):
        old, new = before[key], after[key]
        for name in STEP_SCALAR_FIELDS:
            a, b = getattr(old, name, None), getattr(new, name, None)
            if a != b and (key, name, b) in scalars:
                raise RevisionPolicyError(
                    RULE_NO_UNDO,
                    f"policy rule '{RULE_NO_UNDO}': a human moved step {old.name!r}'s "
                    f"{name!r} away from {b!r} and an agent may not move it back.",
                )
        for name in STEP_LIST_FIELDS:
            a = set(getattr(old, name, None) or ())
            b = set(getattr(new, name, None) or ())
            for element in sorted(b - a):
                if (key, name, element, "present") in lists:
                    raise RevisionPolicyError(
                        RULE_NO_UNDO,
                        f"policy rule '{RULE_NO_UNDO}': a human removed {element!r} from "
                        f"step {old.name!r}'s {name!r} and an agent may not put it back.",
                    )
            for element in sorted(a - b):
                if (key, name, element, "absent") in lists:
                    raise RevisionPolicyError(
                        RULE_NO_UNDO,
                        f"policy rule '{RULE_NO_UNDO}': a human added {element!r} to step "
                        f"{old.name!r}'s {name!r} and an agent may not remove it.",
                    )


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


#: Public name for the step-keying rule, so the store's lessons pass can key
#: its own lookups the same way the policy does. Two different keyings would
#: mean the pass proposing a text onto a step the policy then measured against
#: a different one.
steps_by_key = _steps_by_key
