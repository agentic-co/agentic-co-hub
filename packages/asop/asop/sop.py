"""The SOP RECORD contract — one immutable version of a procedure, and the
rules for what makes its fields honest.

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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SOP:
    """One immutable version of a procedure.

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
