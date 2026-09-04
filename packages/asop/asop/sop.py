"""The ASOP RECORD contract — the procedure, its steps, and the rules for
what makes their fields honest.

Two generations live here during one transition:

* **v3 (current):** `ASOP` is a versioned, ordered sequence of `Step`s for
  one type of task. The gate is on the step, authored with the version. See
  `ASOP.md` §3 — the review of 2026-09-04 decided the grain.
* **legacy:** `SOP` is the v2 shape — one versioned record, one work item,
  the gate supplied by whoever filed it. It stays importable so the plane's
  store (`agentco/sop.py`) keeps working until it is migrated, and is then
  deleted. Nothing new should be written against it.

This is the record shape only: `SopStatus`, the `SOP` dataclass, and
`validate_fields`. The versioned store (`SopLibrary` — draft, revise,
activate, instantiate work items from a template, group outcomes by
version), the file-locking, and the revision policy stay in the plane
(`agentco/sop.py`), because they are about how a procedure is KEPT, not what
a procedure IS. A harness that only ever reads a pinned `(sop_id, version)`
off a work item and renders it to whoever executes the step needs this
module and nothing else.

An SOP used to be a block of text copied into every item that needed it.
That shape has three problems and they compound: the same instruction exists
in fifty places, improving it means editing fifty items, and — the one that
matters — it has no identity, so there is nowhere to attach the answer to
*"does this procedure actually work?"* Here an SOP is its own object with a
version history, and a work item **pins** the version it was created under.

**Every text field is optional, but the SOP as a whole may not be empty and
no present field may be blank.** Partial is legal on purpose: an SOP is
filled in as the work is understood, and demanding all fields up front means
the block is skipped entirely at exactly the moment it is cheapest to start.
What is refused is the *dishonest* shape — an empty SOP, or a field that is
present and says nothing, which claims to answer a question it does not
answer.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

# The cap IS the discipline. An unbounded list of known failure modes is a
# wiki page, and a wiki page is not read at handoff time. Keep the ones that
# actually bite.
MAX_COMMON_MISTAKES = 3

# Order matters: this is the order a procedure is READ in, from "may I
# start" through to "where does the result go". The three added after the
# original four exist because a procedure that only says what done means
# leaves the two most expensive questions unanswered — did I have what I
# needed before I started, and who is waiting for what I produced.
TEXT_FIELDS = (
    "purpose",
    "trigger",
    "entry_check",
    "inputs",
    "definition_of_done",
    "validation",
    "write_back",
)

# The successor. Not a TEXT_FIELD because it is a reference, not prose, and
# it is checked for shape rather than for existence: a chain is almost
# always authored in order, so the target of a link routinely does not
# exist yet at the moment the link is written. Whoever walks the chain
# reports a broken link loudly, by name, rather than stopping short quietly.
LINK_FIELD = "next_sop"

# The step's class and its labels. Not TEXT_FIELDS — `executor` is an enum
# and `tags` a list. `executor: human` is load-bearing rather than
# descriptive wherever a caller instantiates work from this template: a step
# whose `executor` is `human` (or carries a protected tag) must not be
# instantiated without a human gate. `EXECUTORS` is the two literal values a
# revision policy reads by name — kept here as bare strings rather than
# imported from a policy module, because the record contract does not
# depend on any particular policy's enforcement of them.
EXECUTOR_FIELD = "executor"
TAGS_FIELD = "tags"
EXECUTORS = ("human", "agent")

# Revision proposals accumulating against the template: one line per `good`
# adjudication — the procedure was wrong here, and this is the evidence. No
# store here can author the fix; the proposal sits in front of whoever
# revises next, on the draft they start from.
PROPOSALS_FIELD = "proposals"

# What a revision policy compares, version to version, to decide whether a
# change is one an agent may make. Exported here because it is a property of
# the record shape (which fields are scalar prose versus a list to diff
# element-by-element), not of any particular policy's rules.
POLICY_SCALAR_FIELDS = ("title", *TEXT_FIELDS, LINK_FIELD, EXECUTOR_FIELD)
POLICY_LIST_FIELDS = ("common_mistakes", TAGS_FIELD, PROPOSALS_FIELD)

#: Written into `metadata.adjudication` by a store's `propose()`: which
#: draft consumed a given proposal.
PROPOSED_KEY = "proposed_in"


def lesson_text(item_id: str, adjudicator: object, evidence: object) -> str:
    """The `common_mistakes` entry a bad adjudication becomes. One formula,
    shared by whoever writes a proposal and whoever later recognises it as
    one."""
    return f"{evidence} (adjudicated bad on {item_id} by {adjudicator})"


class SopError(Exception):
    """Base for every refusal in this contract."""


class SopContractError(SopError, ValueError):
    """The SOP payload is malformed, or dishonest about what it contains."""


class SopStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    # Withdrawn with no successor. Existing runs finish under their pin; no
    # new runs file. Human-only (ASOP.md §4, decision 3). Kept, like
    # SUPERSEDED, because a pin to it must stay resolvable forever.
    RETIRED = "retired"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SOP:
    """LEGACY (v2) — one immutable version of a single-step procedure.

    Kept for the plane's store during the v3 migration. The v3 shape is
    `ASOP` (the sequence) and `Step` (what this record used to be), below.

    Every text field is optional, but the SOP as a whole may not be empty
    and no present field may be blank — see `validate_fields`, which is what
    a store calls before ever constructing one of these from untrusted input.
    """

    sop_id: str
    version: int
    title: str
    status: SopStatus = SopStatus.DRAFT

    purpose: Optional[str] = None
    trigger: Optional[str] = None
    # What must be true, and in hand, BEFORE starting — phrased so that a
    # missing item becomes a question to ask rather than an assumption to
    # make.
    entry_check: Optional[str] = None
    inputs: Optional[str] = None
    definition_of_done: Optional[str] = None
    # How to prove the claim above. Distinct from it on purpose: the
    # definition of done is what is being claimed, and this is the check
    # that would FAIL if the claim were false. Collapsing the two is how
    # "done" comes to mean "I believe I finished".
    validation: Optional[str] = None
    # Where the outcome is recorded when finishing, and for whom. This is
    # the half of a handoff that gets dropped, and it is the half the next
    # procedure's entry_check is written against.
    write_back: Optional[str] = None
    common_mistakes: list[str] = field(default_factory=list)
    # The procedure that follows this one, by id. Makes a process walkable
    # rather than merely described.
    next_sop: Optional[str] = None
    # Who executes an instance of this step. `human` is load-bearing for
    # whoever instantiates work from this template. `None` is unclassified,
    # which a revision policy reads as `agent`.
    executor: Optional[str] = None
    # Lower-cased labels. The protected ones (`money`, `irreversible` by
    # convention) freeze the step against agents in the plane's policy.
    tags: list[str] = field(default_factory=list)
    # Who wrote this version, and whether the operator had declared them
    # human. `None` on versions that predate the record. A revision policy's
    # human-authorship rule is computed from `author_kind`, and only from
    # versions marked `human`.
    author: Optional[str] = None
    author_kind: Optional[str] = None
    # Open revision proposals, one per good adjudication, carried forward
    # until a reviser addresses or dismisses them.
    proposals: list[str] = field(default_factory=list)

    # Set when a later version replaces this one. Kept rather than deleted:
    # instances pinned to this version must stay resolvable forever, or
    # their outcomes become unreadable.
    superseded_by: Optional[int] = None
    created_at: str = field(default_factory=_now_iso)

    @property
    def ref(self) -> dict:
        """What an instance pins. Both halves — an id alone is not a version."""
        return {"sop_id": self.sop_id, "version": self.version}

    def to_json(self) -> str:
        payload = asdict(self)
        payload["status"] = self.status.value
        return json.dumps(payload, sort_keys=True)

    @classmethod
    def from_json(cls, line: str) -> "SOP":
        raw = json.loads(line)
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in raw.items() if k in known}
        data["status"] = SopStatus(data.get("status", "draft"))
        return cls(**data)


def validate_fields(payload: dict) -> dict:
    """Normalise and check the SOP body. Raises rather than repairing.

    Repairing a malformed SOP would be the worst option available: the
    caller believes they wrote one thing and the store holds another, and
    the difference surfaces at handoff time to whoever is least able to
    notice it.
    """
    allowed = set(TEXT_FIELDS) | {"common_mistakes", LINK_FIELD, EXECUTOR_FIELD, TAGS_FIELD, PROPOSALS_FIELD}
    unknown = set(payload) - allowed
    if unknown:
        raise SopContractError(
            f"unknown SOP field(s): {', '.join(sorted(unknown))} "
            f"(allowed: {', '.join(sorted(allowed))}). There is no 'steps' "
            f"field — the work item's own description carries the steps, so "
            f"that they can differ per instance while the procedure does not."
        )

    out: dict = {}
    for key in TEXT_FIELDS:
        if key not in payload or payload[key] is None:
            continue
        value = payload[key]
        if not isinstance(value, str) or not value.strip():
            raise SopContractError(
                f"SOP field '{key}' must be a non-empty string, got {value!r} — "
                f"a present-but-blank field claims this procedure answers a "
                f"question it does not answer. Omit the key instead."
            )
        out[key] = value.strip()

    if "common_mistakes" in payload and payload["common_mistakes"] is not None:
        mistakes = payload["common_mistakes"]
        if isinstance(mistakes, (str, dict)) or not isinstance(mistakes, (list, tuple)):
            raise SopContractError(
                f"'common_mistakes' must be a LIST of strings, got "
                f"{type(mistakes).__name__} — one entry per mistake, so each can "
                f"be read (and dropped) on its own."
            )
        if not mistakes:
            raise SopContractError(
                "'common_mistakes' is empty — an empty list is the claim that "
                "this work has no known failure modes, which is the one claim a "
                "handoff should never make silently. Omit the key if none are "
                "known yet."
            )
        if len(mistakes) > MAX_COMMON_MISTAKES:
            raise SopContractError(
                f"'common_mistakes' carries {len(mistakes)} entries; the cap is "
                f"{MAX_COMMON_MISTAKES}. The cap is the discipline — an "
                f"unbounded list is a wiki page, and a wiki page is not read at "
                f"handoff time. Keep the ones that actually bite."
            )
        cleaned = []
        for i, mistake in enumerate(mistakes):
            if not isinstance(mistake, str) or not mistake.strip():
                raise SopContractError(
                    f"'common_mistakes'[{i}] must be a non-empty string, got {mistake!r}"
                )
            cleaned.append(mistake.strip())
        out["common_mistakes"] = cleaned

    if LINK_FIELD in payload and payload[LINK_FIELD] is not None:
        link = payload[LINK_FIELD]
        if not isinstance(link, str) or not link.strip():
            raise SopContractError(
                f"'{LINK_FIELD}' must be an SOP id, got {link!r} — omit the key "
                f"if this procedure ends the chain."
            )
        out[LINK_FIELD] = link.strip()

    if EXECUTOR_FIELD in payload and payload[EXECUTOR_FIELD] is not None:
        executor = payload[EXECUTOR_FIELD]
        if executor not in EXECUTORS:
            raise SopContractError(
                f"'{EXECUTOR_FIELD}' must be one of {list(EXECUTORS)}, got "
                f"{executor!r} — it names who runs an instance of this step, and "
                f"a revision policy reads it. Omit the key to leave the step "
                f"unclassified (which a policy treats as 'agent')."
            )
        out[EXECUTOR_FIELD] = executor

    if TAGS_FIELD in payload and payload[TAGS_FIELD] is not None:
        tags = payload[TAGS_FIELD]
        if isinstance(tags, (str, dict)) or not isinstance(tags, (list, tuple)):
            raise SopContractError(
                f"'{TAGS_FIELD}' must be a LIST of strings, got {type(tags).__name__}."
            )
        cleaned_tags: list[str] = []
        for i, tag in enumerate(tags):
            if not isinstance(tag, str) or not tag.strip():
                raise SopContractError(f"'{TAGS_FIELD}'[{i}] must be a non-empty string, got {tag!r}")
            # Folded, so `Money` is not a way past a rule written for `money`.
            folded = tag.strip().lower()
            if folded not in cleaned_tags:
                cleaned_tags.append(folded)
        out[TAGS_FIELD] = cleaned_tags

    if PROPOSALS_FIELD in payload and payload[PROPOSALS_FIELD] is not None:
        proposals = payload[PROPOSALS_FIELD]
        if isinstance(proposals, (str, dict)) or not isinstance(proposals, (list, tuple)):
            raise SopContractError(
                f"'{PROPOSALS_FIELD}' must be a LIST of strings, got {type(proposals).__name__}."
            )
        cleaned_proposals: list[str] = []
        for i, proposal in enumerate(proposals):
            if not isinstance(proposal, str) or not proposal.strip():
                raise SopContractError(
                    f"'{PROPOSALS_FIELD}'[{i}] must be a non-empty string, got {proposal!r}"
                )
            if proposal.strip() not in cleaned_proposals:
                cleaned_proposals.append(proposal.strip())
        out[PROPOSALS_FIELD] = cleaned_proposals

    if not {k for k in out if k not in (TAGS_FIELD, EXECUTOR_FIELD, PROPOSALS_FIELD)}:
        raise SopContractError(
            "an SOP with no fields set reads as delegation-ready and hands its "
            "executor nothing. Fill at least one field."
        )
    return out


# =========================================================================
# v3 — the ASOP is the sequence; the step is what v2 called the procedure
# =========================================================================

from asop.gates import validate_gate as _validate_gate  # noqa: E402

#: The contract revision these records implement. Distinct from
#: `asop.SCHEMA_VERSION`, which covers the gate schema alone and is
#: unchanged by v3 — gates moved, they did not change shape.
ASOP_VERSION = 3

#: The decomposition bounds, restated as a property of the artefact. They
#: are the same numbers the plane enforces on bead trees, and mean the same
#: thing: a procedure sized to what an accountable person can sanity-check.
#: Width is checked here at authoring; depth is checked where a run files,
#: because it is the run tree that nests, not the record.
MAX_STEPS = 7
MAX_DEPTH = 3

ROLE_KINDS = ("agent", "human")

#: The tags every registry protects (ASOP.md §6.4). A plane may ADD to this
#: set; nothing may remove these two. The record enforces the default set
#: here — a step carrying one of these must be gated by a person — and a
#: plane enforces its extensions on its own side, because the record cannot
#: know a set it was never told.
DEFAULT_PROTECTED_TAGS = frozenset({"money", "irreversible"})

#: A step's prose, in the order it is READ. `trigger` moved up to the ASOP:
#: a procedure is triggered, a step is reached.
STEP_TEXT_FIELDS = (
    "purpose",
    "entry_check",
    "inputs",
    "definition_of_done",
    "validation",
    "write_back",
)

ASOP_TEXT_FIELDS = ("purpose", "trigger")


def _clean_text(payload: dict, key: str, out: dict, *, what: str) -> None:
    if key not in payload or payload[key] is None:
        return
    value = payload[key]
    if not isinstance(value, str) or not value.strip():
        raise SopContractError(
            f"{what} field '{key}' must be a non-empty string, got {value!r} — "
            f"a present-but-blank field claims to answer a question it does "
            f"not answer. Omit the key instead."
        )
    out[key] = value.strip()


def _clean_str_list(payload: dict, key: str, out: dict, *, what: str,
                    cap: Optional[int] = None, fold: bool = False,
                    refuse_empty: Optional[str] = None) -> None:
    if key not in payload or payload[key] is None:
        return
    items = payload[key]
    if isinstance(items, (str, dict)) or not isinstance(items, (list, tuple)):
        raise SopContractError(
            f"{what} '{key}' must be a LIST of strings, got {type(items).__name__}."
        )
    if not items and refuse_empty:
        raise SopContractError(refuse_empty)
    if cap is not None and len(items) > cap:
        raise SopContractError(
            f"{what} '{key}' carries {len(items)} entries; the cap is {cap}. "
            f"The cap is the discipline — keep the ones that actually bite."
        )
    cleaned: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            raise SopContractError(f"{what} '{key}'[{i}] must be a non-empty string, got {item!r}")
        value = item.strip().lower() if fold else item.strip()
        if value not in cleaned:
            cleaned.append(value)
    out[key] = cleaned


@dataclass
class Step:
    """One unit of an ASOP: text, gate, role. What v2 called the procedure.

    A step has no version of its own — the ASOP is versioned, and a bead
    pins `(asop_id, version, step)`. It has no status either; runs do.
    """

    step: int                      # 1-based position in the sequence
    name: str
    role: Optional[str] = None     # a role declared on the ASOP; None only when `uses`

    purpose: Optional[str] = None
    entry_check: Optional[str] = None
    inputs: Optional[str] = None
    definition_of_done: Optional[str] = None
    validation: Optional[str] = None
    write_back: Optional[str] = None
    common_mistakes: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    proposals: list[str] = field(default_factory=list)

    # The gate, authored HERE with the version. Normalised by
    # `asop.gates.validate_gate`; a run supplies none (ASOP.md §3.3).
    gate: Optional[dict] = None
    # Ordering. Filled in by `validate_step` when absent: the previous step,
    # or nothing for step 1. May name only LOWER-numbered steps, which is
    # what makes a cycle impossible by construction rather than by search.
    after: list[int] = field(default_factory=list)
    # A step that is another ASOP: `{"asop_id": ..., "version": ...}` in
    # place of a body (ASOP.md §3.5). Its gates are its own.
    uses: Optional[dict] = None

    @property
    def is_nested(self) -> bool:
        return self.uses is not None


@dataclass
class ASOP:
    """A versioned, ordered sequence of steps for one type of task."""

    asop_id: str
    version: int
    title: str
    status: SopStatus = SopStatus.DRAFT
    task_type: Optional[str] = None

    purpose: Optional[str] = None
    trigger: Optional[str] = None
    # What a RUN must be given, by name. `kind` is reserved — optional and
    # unvalidated in v3 (ASOP.md §11.5).
    inputs: list[dict] = field(default_factory=list)
    # Declared here, bound by the harness. `{name: {"kind": "agent"|"human"}}`.
    roles: dict[str, dict] = field(default_factory=dict)
    # Separation of duties over roles: `[{"distinct": ["implementer", "validator"]}]`.
    constraints: list[dict] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)

    author: Optional[str] = None
    author_kind: Optional[str] = None
    proposals: list[str] = field(default_factory=list)
    superseded_by: Optional[int] = None
    created_at: str = field(default_factory=_now_iso)

    @property
    def ref(self) -> dict:
        """What a run pins. Both halves — an id alone is not a version."""
        return {"asop_id": self.asop_id, "version": self.version}

    def step_ref(self, step: int) -> dict:
        """What a step bead pins."""
        return {"asop_id": self.asop_id, "version": self.version, "step": step}

    def to_json(self) -> str:
        payload = asdict(self)
        payload["status"] = self.status.value
        return json.dumps(payload, sort_keys=True)

    @classmethod
    def from_json(cls, line: str) -> "ASOP":
        raw = json.loads(line)
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in raw.items() if k in known}
        data["status"] = SopStatus(data.get("status", "draft"))
        data["steps"] = [Step(**st) for st in data.get("steps") or []]
        return cls(**data)


def validate_step(payload: dict, *, index: int) -> dict:
    """Normalise and check one step body. Raises rather than repairing.

    `index` is the step's 1-based position; `after` may name only lower
    positions. The gate is normalised by the shared gate schema. A nested
    step (`uses`) carries no body of its own and no gate of its own — the
    inner ASOP's gates are the inner ASOP's.
    """
    # Named before the generic unknown-field check, because "unknown field
    # 'next_sop'" reads as a typo when it is a removal the reader may be
    # holding a v2 record for.
    if "next_sop" in payload:
        raise SopContractError(
            f"step {index}: 'next_sop' is not a v3 field. Composition inside a "
            f"procedure is nesting (`uses`); sequencing between procedures is "
            f"the harness's decision (ASOP.md §11.4)."
        )
    allowed = (
        set(STEP_TEXT_FIELDS)
        | {"step", "name", "role", "common_mistakes", "tags", "proposals", "gate", "after", "uses"}
    )
    unknown = set(payload) - allowed
    if unknown:
        raise SopContractError(
            f"step {index}: unknown field(s): {', '.join(sorted(unknown))} "
            f"(allowed: {', '.join(sorted(allowed))})."
        )

    out: dict = {"step": index}
    if "step" in payload and payload["step"] is not None and int(payload["step"]) != index:
        raise SopContractError(
            f"step {index}: declares step={payload['step']!r} but sits at "
            f"position {index}. The position is the number; omit the key."
        )

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SopContractError(f"step {index}: 'name' is required and must be a non-empty string.")
    out["name"] = name.strip()

    uses = payload.get("uses")
    if uses is not None:
        if (not isinstance(uses, dict) or set(uses) != {"asop_id", "version"}
                or not isinstance(uses.get("asop_id"), str) or not uses["asop_id"].strip()
                or not isinstance(uses.get("version"), int) or uses["version"] < 1):
            raise SopContractError(
                f"step {index}: 'uses' must be {{'asop_id': <id>, 'version': <int>=1>}} "
                f"— a nested step pins the inner procedure's version, exactly as a run does."
            )
        body_keys = [k for k in payload if k in STEP_TEXT_FIELDS or k in ("gate", "role", "common_mistakes")]
        if body_keys:
            raise SopContractError(
                f"step {index}: a nested step (`uses`) carries no body of its own, "
                f"but has {', '.join(sorted(body_keys))}. The inner ASOP's steps, "
                f"gates and roles are the inner ASOP's."
            )
        out["uses"] = {"asop_id": uses["asop_id"].strip(), "version": uses["version"]}
    else:
        role = payload.get("role")
        if not isinstance(role, str) or not role.strip():
            raise SopContractError(
                f"step {index}: 'role' is required — a step names who does it, as a "
                f"role the ASOP declares, never as an agent (ASOP.md §3.6)."
            )
        out["role"] = role.strip()
        for key in STEP_TEXT_FIELDS:
            _clean_text(payload, key, out, what=f"step {index}")
        _clean_str_list(payload, "common_mistakes", out, what=f"step {index}", cap=MAX_COMMON_MISTAKES,
                        refuse_empty=(f"step {index}: 'common_mistakes' is empty — an empty list is the "
                                      f"claim that this work has no known failure modes. Omit the key."))
        gate = payload.get("gate")
        if gate is None:
            raise SopContractError(
                f"step {index}: 'gate' is required. A step with no gate is advice; "
                f"the gate is authored here, with the version, and a run supplies "
                f"none (ASOP.md §2.2)."
            )
        try:
            out["gate"] = _validate_gate(gate)
        except Exception as e:  # the gate schema raises its own Refusal
            raise SopContractError(f"step {index}: gate invalid — {e}") from e
        if not {k for k in out if k in STEP_TEXT_FIELDS}:
            raise SopContractError(
                f"step {index}: no text fields set. A step that says nothing hands "
                f"its executor nothing; fill at least one of {', '.join(STEP_TEXT_FIELDS)}."
            )

    _clean_str_list(payload, "tags", out, what=f"step {index}", fold=True)
    _clean_str_list(payload, "proposals", out, what=f"step {index}")

    after = payload.get("after")
    if after is None:
        out["after"] = [index - 1] if index > 1 else []
    else:
        if isinstance(after, (str, dict)) or not isinstance(after, (list, tuple)):
            raise SopContractError(f"step {index}: 'after' must be a LIST of step numbers.")
        cleaned: list[int] = []
        for ref in after:
            if not isinstance(ref, int) or isinstance(ref, bool):
                raise SopContractError(f"step {index}: 'after' entries must be step numbers, got {ref!r}.")
            if ref == index:
                raise SopContractError(f"step {index}: a step cannot be after itself.")
            if ref >= index:
                raise SopContractError(
                    f"step {index}: 'after' names step {ref}, which is not earlier. "
                    f"A step may follow only lower-numbered steps — that is what "
                    f"makes a cycle impossible by construction (ASOP.md §3.4)."
                )
            if ref < 1:
                raise SopContractError(f"step {index}: 'after' names step {ref}; steps start at 1.")
            if ref not in cleaned:
                cleaned.append(ref)
        out["after"] = sorted(cleaned)
    return out


def validate_asop(payload: dict) -> dict:
    """Normalise and check a whole ASOP body: record fields, roles,
    constraints, and every step against the roles. Raises rather than
    repairing. The identity fields (`asop_id`, `version`, `status`, author,
    timestamps) belong to the store and are not part of the body."""
    allowed = set(ASOP_TEXT_FIELDS) | {"title", "task_type", "inputs", "roles", "constraints", "steps", "proposals"}
    unknown = set(payload) - allowed
    if unknown:
        raise SopContractError(
            f"unknown ASOP field(s): {', '.join(sorted(unknown))} (allowed: {', '.join(sorted(allowed))})."
        )
    out: dict = {}
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise SopContractError("'title' is required and must be a non-empty string.")
    out["title"] = title.strip()
    _clean_text(payload, "task_type", out, what="ASOP")
    for key in ASOP_TEXT_FIELDS:
        _clean_text(payload, key, out, what="ASOP")
    _clean_str_list(payload, "proposals", out, what="ASOP")

    # inputs: [{name, description?, kind?}] — presence-checked by name at run time
    inputs = payload.get("inputs") or []
    if isinstance(inputs, (str, dict)) or not isinstance(inputs, (list, tuple)):
        raise SopContractError("'inputs' must be a LIST of {name, description} records.")
    seen: set[str] = set(); cleaned_inputs: list[dict] = []
    for i, rec in enumerate(inputs):
        if not isinstance(rec, dict) or not isinstance(rec.get("name"), str) or not rec["name"].strip():
            raise SopContractError(f"'inputs'[{i}] must be a record with a non-empty 'name'.")
        extra = set(rec) - {"name", "description", "kind"}
        if extra:
            raise SopContractError(f"'inputs'[{i}]: unknown field(s) {', '.join(sorted(extra))}.")
        name = rec["name"].strip()
        if name in seen:
            raise SopContractError(f"'inputs' declares '{name}' twice.")
        seen.add(name)
        rec_out = {"name": name}
        if rec.get("description") is not None:
            _clean_text(rec, "description", rec_out, what=f"'inputs'[{i}]")
        if rec.get("kind") is not None:
            rec_out["kind"] = rec["kind"]      # reserved, unvalidated in v3
        cleaned_inputs.append(rec_out)
    out["inputs"] = cleaned_inputs

    # roles: {name: {kind}}
    roles = payload.get("roles") or {}
    if not isinstance(roles, dict):
        raise SopContractError("'roles' must be a mapping of role name to {kind}.")
    cleaned_roles: dict[str, dict] = {}
    for name, spec in roles.items():
        if not isinstance(name, str) or not name.strip():
            raise SopContractError("a role name must be a non-empty string.")
        if not isinstance(spec, dict) or spec.get("kind") not in ROLE_KINDS or set(spec) - {"kind"}:
            raise SopContractError(
                f"role '{name}' must be {{'kind': 'agent'|'human'}} — a role is a "
                f"responsibility, never an agent, a model or a person (ASOP.md §3.6)."
            )
        cleaned_roles[name.strip()] = {"kind": spec["kind"]}
    out["roles"] = cleaned_roles

    # constraints: [{distinct: [a, b]}]
    constraints = payload.get("constraints") or []
    if isinstance(constraints, (str, dict)) or not isinstance(constraints, (list, tuple)):
        raise SopContractError("'constraints' must be a LIST.")
    cleaned_constraints: list[dict] = []
    for i, c in enumerate(constraints):
        if not isinstance(c, dict) or set(c) != {"distinct"}:
            raise SopContractError(f"'constraints'[{i}] must be {{'distinct': [role, role, ...]}}.")
        pair = c["distinct"]
        if (isinstance(pair, (str, dict)) or not isinstance(pair, (list, tuple)) or len(pair) < 2
                or len(set(pair)) != len(pair)):
            raise SopContractError(f"'constraints'[{i}].distinct must list two or more different roles.")
        for r in pair:
            if r not in cleaned_roles:
                raise SopContractError(f"'constraints'[{i}] names role '{r}', which the ASOP does not declare.")
        cleaned_constraints.append({"distinct": list(pair)})
    out["constraints"] = cleaned_constraints

    # steps
    steps = payload.get("steps")
    if isinstance(steps, (str, dict)) or not isinstance(steps, (list, tuple)) or not steps:
        raise SopContractError(
            "'steps' must be a non-empty LIST. An ASOP with no steps is a title."
        )
    if len(steps) > MAX_STEPS:
        raise SopContractError(
            f"'steps' carries {len(steps)}; the bound is {MAX_STEPS} at one level. "
            f"The bound is a human review bound — a procedure that needs more is "
            f"usually two procedures (ASOP.md §3.1). Nest one (`uses`) if it is not."
        )
    cleaned_steps: list[dict] = []
    for i, st in enumerate(steps, start=1):
        if not isinstance(st, dict):
            raise SopContractError(f"step {i} must be a mapping.")
        clean = validate_step(st, index=i)
        if "uses" not in clean:
            role = clean["role"]
            if role not in cleaned_roles:
                raise SopContractError(
                    f"step {i}: role '{role}' is not declared on the ASOP "
                    f"(declared: {', '.join(sorted(cleaned_roles)) or 'none'})."
                )
            if cleaned_roles[role]["kind"] == "human" and clean["gate"].get("kind") != "human":
                raise SopContractError(
                    f"step {i}: role '{role}' is human, so its gate must be kind "
                    f"'human' — a human step an agent can close is not a human step."
                )
            protected_here = sorted(set(clean.get("tags", [])) & DEFAULT_PROTECTED_TAGS)
            if protected_here and clean["gate"].get("kind") != "human":
                raise SopContractError(
                    f"step {i}: carries protected tag(s) {protected_here}, so its gate "
                    f"must be kind 'human' — the tag means a person looks before this "
                    f"step counts as done, and a deterministic or judged gate is "
                    f"precisely nobody looking (ASOP.md §6.4). v2 enforced this at "
                    f"filing; v3 enforces it where the gate is authored."
                )
        cleaned_steps.append(clean)
    out["steps"] = cleaned_steps

    # a judged gate is evaluated by a route distinct from the executor's —
    # required by construction, so a validator over another step's output
    # may not share that step's role.
    for st in cleaned_steps:
        if "uses" in st:
            continue
        if st["gate"].get("kind") == "judged":
            for ref in st["after"]:
                prev = cleaned_steps[ref - 1]
                if "uses" not in prev and prev["role"] == st["role"]:
                    raise SopContractError(
                        f"step {st['step']}: a judged step over step {ref}'s output "
                        f"shares its role '{st['role']}'. The judge must be a route "
                        f"distinct from the executor's; give the judging step its own "
                        f"role and a `distinct` constraint (ASOP.md §3.6)."
                    )
    return out
