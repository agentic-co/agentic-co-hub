"""Standard operating procedures as versioned objects that work items pin.

An SOP used to be a block of text copied into every item that needed it. That
shape has three problems and they compound: the same instruction exists in
fifty places, improving it means editing fifty items, and — the one that
matters — it has no identity, so there is nowhere to attach the answer to
*"does this procedure actually work?"*

Here an SOP is its own object with a version history, and a work item **pins**
the version it was created under.

**A template is not an instance, and the distinction is load-bearing.** An SOP
never enters the queue. If it did it would be claimable, `ready()` would offer
it, somebody would complete it, and the template would be `done` — a template
that can be finished is a bug, and precisely the confusion the queue exists to
prevent. SOPs live in their own store; `instantiate()` creates work items from
them.

**Versions are immutable.** `revise()` writes a NEW version; it never mutates
what is there. It also does not promote: the version in use stays in use until
`activate()` says otherwise, so drafting an improvement is a safe act rather
than one that quietly takes the live procedure out of service. This is what makes evaluation possible
at all: an instance that referenced "the SOP" rather than "v3" would attribute
last month's outcomes to text that has since changed, and every number computed
from it would be fiction.

That pin is the same relationship a snapshot has to a document — pointer plus
version token — and `drifted()` below answers the same question divergence
answers: *what I started against has moved; is that a problem?* It reports and
never migrates. Silently re-pointing an in-flight item at a newer procedure
would change the work under whoever is doing it.

**What is deliberately not here: the improvement loop.** No proposing a better
version from observed failures, no automatic promotion. That machinery is
worthless without instances to learn from, and building it now would mean
tuning against imagination. What ships is the measurement that makes it
possible later — `outcomes_by_version()` — and the honesty rules that stop the
resulting numbers being read as more than they are.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterator, Optional, Sequence

from agentco import policy
from agentco.filelock import lock_exclusive, unlock
from agentco.work import PLAN_KEY, Queue, WorkItem, WorkStatus, reject_reserved

# The cap IS the discipline. An unbounded list of known failure modes is a wiki
# page, and a wiki page is not read at handoff time. Keep the ones that bite.
MAX_COMMON_MISTAKES = 3

# Order matters: this is the order a procedure is READ in, from "may I start"
# through to "where does the result go". The three added after the original four
# exist because a procedure that only says what done means leaves the two most
# expensive questions unanswered — did I have what I needed before I started,
# and who is waiting for what I produced.
TEXT_FIELDS = (
    "purpose",
    "trigger",
    "entry_check",
    "inputs",
    "definition_of_done",
    "validation",
    "write_back",
)

# The successor. Not a TEXT_FIELD because it is a reference, not prose, and it
# is checked for shape rather than for existence: a chain is almost always
# authored in order, so the target of a link routinely does not exist yet at the
# moment the link is written. `SopLibrary.chain()` is where a broken link is
# reported — loudly, by name, rather than by the walk quietly stopping short.
LINK_FIELD = "next_sop"

# The step's class and its labels. Not TEXT_FIELDS — `executor` is an enum and
# `tags` a list — and both are what the revision policy (agentco/policy.py)
# reads. `executor: human` is load-bearing rather than descriptive:
# `instantiate()` refuses an instance of a human step that does not carry a
# human gate. A protected tag carries the same requirement.
EXECUTOR_FIELD = "executor"
TAGS_FIELD = "tags"
EXECUTORS = (policy.HUMAN, policy.AGENT)
# Revision proposals accumulating against the template: one line per `good`
# adjudication — the procedure was wrong here, and this is the evidence. The
# plane cannot author the fix; it puts the proposal in front of whoever revises
# next, on the draft they start from. A human dismissing one (revising it away)
# is final for agents, by rule 3 of the policy.
PROPOSALS_FIELD = "proposals"
# What rule 3 of the policy compares, version to version.
POLICY_SCALAR_FIELDS = ("title", *TEXT_FIELDS, LINK_FIELD, EXECUTOR_FIELD)
POLICY_LIST_FIELDS = ("common_mistakes", TAGS_FIELD, PROPOSALS_FIELD)
#: Written into `metadata.adjudication` by `propose()`: which draft consumed it.
PROPOSED_KEY = "proposed_in"


class SopError(Exception):
    """Base for every refusal in this module."""


class SopContractError(SopError, ValueError):
    """The SOP payload is malformed, or dishonest about what it contains."""


SOP_STORE_ENV_VAR = "AGENTCO_SOP_STORE"
DEFAULT_SOP_STORE = "sops.jsonl"


def resolve_sop_store(path: Optional[str] = None) -> str:
    """Where the library lives — see `work.resolve_work_store` for why here."""
    return path or os.environ.get(SOP_STORE_ENV_VAR) or DEFAULT_SOP_STORE


class SopStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SOP:
    """One immutable version of a procedure.

    Every text field is optional, but the SOP as a whole may not be empty and no
    present field may be blank. **Partial is legal on purpose:** an SOP is filled
    in as the work is understood, and demanding all five fields up front means
    the block is skipped entirely at exactly the moment it is cheapest to start.

    What is refused is the *dishonest* shape — an empty SOP, or a field that is
    present and says nothing, which claims to answer a question it does not
    answer.
    """

    sop_id: str
    version: int
    title: str
    status: SopStatus = SopStatus.DRAFT

    purpose: Optional[str] = None
    trigger: Optional[str] = None
    # What must be true, and in hand, BEFORE starting — phrased so that a
    # missing item becomes a question to ask rather than an assumption to make.
    entry_check: Optional[str] = None
    inputs: Optional[str] = None
    definition_of_done: Optional[str] = None
    # How to prove the claim above. Distinct from it on purpose: the definition
    # of done is what is being claimed, and this is the check that would FAIL if
    # the claim were false. Collapsing the two is how "done" comes to mean "I
    # believe I finished".
    validation: Optional[str] = None
    # Where the outcome is recorded when finishing, and for whom. This is the
    # half of a handoff that gets dropped, and it is the half the next
    # procedure's entry_check is written against.
    write_back: Optional[str] = None
    common_mistakes: list[str] = field(default_factory=list)
    # The procedure that follows this one, by id. Makes a process walkable
    # rather than merely described.
    next_sop: Optional[str] = None
    # Who executes an instance of this step. `human` is load-bearing — see
    # `instantiate()`. None is unclassified, which the policy reads as `agent`.
    executor: Optional[str] = None
    # Lower-cased labels. The protected ones (`money`, `irreversible` by default)
    # freeze the step against agents — agentco/policy.py.
    tags: list[str] = field(default_factory=list)
    # Who wrote this version, and whether the operator had declared them human.
    # None on versions that predate the record. Rule 3 of the revision policy
    # is computed from `author_kind`, and only from versions marked `human`.
    author: Optional[str] = None
    author_kind: Optional[str] = None
    # Open revision proposals, one per good adjudication, carried forward until
    # a reviser addresses or dismisses them. See `SopLibrary.propose`.
    proposals: list[str] = field(default_factory=list)

    # Set when a later version replaces this one. Kept rather than deleted:
    # instances pinned to this version must stay resolvable forever, or their
    # outcomes become unreadable.
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

    Repairing a malformed SOP would be the worst option available: the caller
    believes they wrote one thing and the store holds another, and the
    difference surfaces at handoff time to whoever is least able to notice it.
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
                f"the revision policy reads it. Omit the key to leave the step "
                f"unclassified (which the policy treats as 'agent')."
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


class SopLibrary:
    """Versioned SOP storage. Same JSONL-under-a-lock shape as the work queue."""

    def __init__(self, path: Path | str = "sops.jsonl", protected_tags: Optional[Sequence[str]] = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The tags that freeze a step against agents. Injectable for tests;
        # otherwise the defaults plus whatever the registry added.
        self.protected_tags = (
            frozenset(protected_tags) if protected_tags is not None
            else policy.protected_tags_from_env()
        )
        # Raw BYTES — a line that failed to decode has no faithful string
        # form, and is carried through every write rather than dropped.
        self.quarantined: list[bytes] = []

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with open(lock_path, "a+") as handle:
            lock_exclusive(handle)
            try:
                yield
            finally:
                unlock(handle)

    def _read_all(self) -> list[SOP]:
        """Every readable version. An unreadable line is quarantined, not fatal.

        Reads BYTES and decodes per line for the same reason the work queue
        does: decoding the whole file puts `UnicodeDecodeError` — a `ValueError`,
        not a `JSONDecodeError` — outside the per-line handler, and one stray
        byte then makes the entire SOP library unreadable.
        """
        if not self.path.exists():
            return []
        out: list[SOP] = []
        quarantined: list[bytes] = []
        for raw_line in self.path.read_bytes().split(b"\n"):
            if not raw_line.strip():
                continue
            try:
                out.append(SOP.from_json(raw_line.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                quarantined.append(raw_line)
        self.quarantined = quarantined
        return out

    def _write_all(self, sops: list[SOP], quarantined: Sequence[bytes] = ()) -> None:
        """Atomic replace, carrying quarantined lines through verbatim.

        **Losing this here is worse than losing it in the work queue.** A
        deleted SOP row frees its version number, and `revise()` computes the
        next version as `max(...) + 1` over the SURVIVORS — so it reissues the
        destroyed number to different text. An instance pinned to v2 then
        resolves to a procedure it never ran. The pin is unchanged; what it
        resolves to is not, which is the one thing versioning exists to prevent.
        """
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                for sop in sops:
                    handle.write(sop.to_json().encode("utf-8") + b"\n")
                for line in quarantined:
                    handle.write(line + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # -- authoring -------------------------------------------------------

    def create(
        self,
        title: str,
        *,
        author: Optional[str] = None,
        author_kind: Optional[str] = None,
        **body,
    ) -> SOP:
        """Write version 1, as a DRAFT.

        Draft rather than active because `instantiate()` refuses a draft: a
        procedure should be readable by the person who will follow it before it
        starts producing work for them.

        `author` / `author_kind` record who wrote it. An unstated kind is
        `agent` — fail closed, the same reading the policy gives an actor the
        operator never declared human.
        """
        validated = validate_fields(body)
        sop = SOP(
            sop_id=f"sop-{uuid.uuid4().hex[:8]}",
            version=1,
            title=title,
            status=SopStatus.DRAFT,
            author=author,
            author_kind=author_kind or policy.AGENT,
            **validated,
        )
        with self._locked():
            existing = self._read_all()
            existing.append(sop)
            self._write_all(existing, self.quarantined)
        return sop

    def revise(
        self,
        sop_id: str,
        title: Optional[str] = None,
        *,
        author: Optional[str] = None,
        author_kind: Optional[str] = None,
        **body,
    ) -> SOP:
        """Write the NEXT version. The previous one is superseded, never edited.

        Unset fields carry forward from the version being revised, so a change
        to one line does not silently blank the other four. Pass `None`
        explicitly to clear a field.

        The old version stays in the store with `superseded_by` set. Deleting it
        would orphan every instance pinned to it, and those instances are the
        entire evidence base for whether the revision was an improvement.

        **The revision policy runs here, before anything is written.** When
        `author_kind` is not `human` — and an unstated kind is not — the three
        rules in `agentco/policy.py` are checked against the whole history,
        and a refusal leaves the store byte-identical. This is the write
        boundary every transport funnels through, which is why the policy
        lives here and not in a handler.
        """
        reviser_kind = author_kind or policy.AGENT
        with self._locked():
            all_sops = self._read_all()
            versions = [s for s in all_sops if s.sop_id == sop_id]
            if not versions:
                raise SopError(
                    f"no SOP {sop_id!r}. Revising something that does not exist "
                    f"would create version 1 under a caller's assumption that "
                    f"they were editing a procedure people already follow."
                )
            latest = max(versions, key=lambda s: s.version)

            # REFUSE rather than guess when part of the history is unreadable.
            # The next version is `max(...) + 1` over what can be PARSED, so a
            # quarantined row silently frees its number and `revise` reissues it
            # to different text. An instance pinned to that version then resolves
            # to a procedure it never ran — the pin is unchanged, what it points
            # at is not, and that is the one thing versioning exists to prevent.
            # Preserving the bytes (see `_write_all`) stops the data being lost;
            # only refusing here stops the NUMBER being reused.
            if self.quarantined:
                raise SopError(
                    f"cannot revise {sop_id!r}: {len(self.quarantined)} line(s) in "
                    f"the SOP store could not be parsed, so the full version "
                    f"history is not visible and the next version number cannot "
                    f"be chosen safely. Repair or remove those lines first — they "
                    f"are preserved verbatim in the file. Reissuing a version "
                    f"number would silently re-point every instance pinned to it."
                )

            carried = {
                key: getattr(latest, key)
                for key in TEXT_FIELDS
                if getattr(latest, key) is not None
            }
            if latest.common_mistakes:
                carried["common_mistakes"] = list(latest.common_mistakes)
            if latest.next_sop:
                carried[LINK_FIELD] = latest.next_sop
            if latest.executor:
                carried[EXECUTOR_FIELD] = latest.executor
            if latest.tags:
                carried[TAGS_FIELD] = list(latest.tags)
            if latest.proposals:
                carried[PROPOSALS_FIELD] = list(latest.proposals)
            carried.update({k: v for k, v in body.items()})
            validated = validate_fields({k: v for k, v in carried.items() if v is not None})

            new = SOP(
                sop_id=sop_id,
                version=latest.version + 1,
                title=title or latest.title,
                status=SopStatus.DRAFT,
                author=author,
                author_kind=reviser_kind,
                **validated,
            )
            policy.check_revision(
                history=versions,
                baseline=latest,
                proposed=new,
                reviser_kind=reviser_kind,
                protected_tags=self.protected_tags,
                scalar_fields=POLICY_SCALAR_FIELDS,
                list_fields=POLICY_LIST_FIELDS,
                action="revise",
            )
            for sop in all_sops:
                if sop.sop_id == sop_id and sop.superseded_by is None:
                    # `superseded_by` records that a later version exists. It
                    # does NOT deactivate: an ACTIVE version stays active until
                    # `activate()` promotes the replacement. Otherwise merely
                    # DRAFTING an improvement would take the live procedure out
                    # of service, and the next `instantiate()` would fail with
                    # nothing having been deliberately changed.
                    sop.superseded_by = new.version
            all_sops.append(new)
            self._write_all(all_sops, self.quarantined)
        return new

    def activate(
        self,
        sop_id: str,
        version: int,
        *,
        author: Optional[str] = None,
        author_kind: Optional[str] = None,
    ) -> SOP:
        """Make one version the one `instantiate()` uses by default.

        Policed like a revision, measured against the version currently active
        (or the latest, when none is). Without this, the policy has a door
        beside it: an agent forbidden from re-adding a step a human removed
        could simply re-activate the version from before the human removed it.
        """
        reviser_kind = author_kind or policy.AGENT
        with self._locked():
            all_sops = self._read_all()
            target = None
            for sop in all_sops:
                if sop.sop_id == sop_id and sop.version == version:
                    target = sop
            if target is None:
                raise SopError(f"no SOP {sop_id!r} version {version}")
            versions = [s for s in all_sops if s.sop_id == sop_id]
            baseline = next(
                (s for s in versions if s.status == SopStatus.ACTIVE),
                max(versions, key=lambda s: s.version),
            )
            policy.check_revision(
                history=versions,
                baseline=baseline,
                proposed=target,
                reviser_kind=reviser_kind,
                protected_tags=self.protected_tags,
                scalar_fields=POLICY_SCALAR_FIELDS,
                list_fields=POLICY_LIST_FIELDS,
                action="activate",
            )
            for sop in all_sops:
                if sop.sop_id == sop_id and sop.status == SopStatus.ACTIVE:
                    sop.status = SopStatus.SUPERSEDED
            target.status = SopStatus.ACTIVE
            target.superseded_by = None
            self._write_all(all_sops, self.quarantined)
        return target

    # -- reading ---------------------------------------------------------

    def get(self, sop_id: str, version: Optional[int] = None) -> Optional[SOP]:
        """One version, or the active one when `version` is omitted.

        A pinned lookup must resolve even when the version is superseded —
        otherwise an instance's own procedure becomes unreadable the moment it
        is improved, which is the opposite of what versioning is for.
        """
        versions = [s for s in self._read_all() if s.sop_id == sop_id]
        if not versions:
            return None
        if version is not None:
            return next((s for s in versions if s.version == version), None)
        return next((s for s in versions if s.status == SopStatus.ACTIVE), None)

    def history(self, sop_id: str) -> list[SOP]:
        return sorted(
            (s for s in self._read_all() if s.sop_id == sop_id),
            key=lambda s: s.version,
        )

    def list_active(self) -> list[SOP]:
        return [s for s in self._read_all() if s.status == SopStatus.ACTIVE]

    def chain(self, sop_id: str) -> list[dict]:
        """Walk `next_sop` from here and return the process, one step per entry.

        Every step reports its own `state`, because the two ways a chain lies
        both look like a short chain:

        - **`missing`** — the link names an SOP that does not exist. The link is
          shape-checked at write time and never existence-checked, since a chain
          is authored in order and its successor routinely does not exist yet.
          A walk that simply stopped here would present a broken process as a
          finished one.
        - **`inactive`** — the SOP exists but has no active version. Work cannot
          be instantiated from it, so the process is interrupted even though
          every link resolves.

        A cycle terminates the walk with `state: "cycle"` rather than raising.
        A cycle is usually a real intent expressed badly (test → fix → test),
        and refusing to report a chain because its tail loops would hide the
        first steps, which are the ones being asked about.
        """
        steps: list[dict] = []
        seen: set[str] = set()
        current: Optional[str] = sop_id

        while current:
            if current in seen:
                steps.append({"sop_id": current, "state": "cycle"})
                break
            seen.add(current)

            sop = self.get(current)
            if sop is None:
                # Distinguish "no such id" from "exists, nothing active" — they
                # have different fixes, and a message naming the wrong one sends
                # the reader looking in the wrong place.
                if self.history(current):
                    latest = self.history(current)[-1]
                    steps.append({
                        "sop_id": current,
                        "title": latest.title,
                        "state": "inactive",
                        "latestVersion": latest.version,
                        "latestStatus": latest.status.value,
                    })
                else:
                    steps.append({"sop_id": current, "state": "missing"})
                break

            steps.append({
                "sop_id": sop.sop_id,
                "version": sop.version,
                "title": sop.title,
                "state": "active",
                "entry_check": sop.entry_check,
                "write_back": sop.write_back,
            })
            current = sop.next_sop
        return steps

    # -- instantiation ---------------------------------------------------

    def instantiate(
        self,
        sop_id: str,
        queue: Queue,
        title: Optional[str] = None,
        version: Optional[int] = None,
        **work_kwargs,
    ) -> WorkItem:
        """Create a work item that PINS this SOP version.

        Refuses a draft. A procedure that nobody has marked active is one still
        being written, and generating work from it hands somebody a half-written
        instruction with the authority of a published one.

        The pin is immutable for the life of the item. Later revisions do not
        reach back — see `drifted()` for how an in-flight item learns its
        procedure moved.
        """
        sop = self.get(sop_id, version)
        if sop is None:
            # Distinguish "no such SOP" from "it exists but nothing is active".
            # The second is the common case and has a different fix, and a
            # message that names the wrong problem sends the reader looking in
            # the wrong place.
            versions = self.history(sop_id)
            if version is not None:
                raise SopError(f"no SOP {sop_id!r} version {version}")
            if versions:
                latest = versions[-1]
                raise SopError(
                    f"SOP {sop_id!r} has no active version — v{latest.version} is "
                    f"a {latest.status.value}. Activate it first: generating work "
                    f"from an unactivated procedure hands somebody a half-written "
                    f"instruction with the authority of a published one."
                )
            raise SopError(f"no SOP {sop_id!r}")
        if sop.status == SopStatus.DRAFT:
            raise SopError(
                f"SOP {sop_id!r} v{sop.version} is a draft. Activate it first — "
                f"generating work from an unactivated procedure hands somebody a "
                f"half-written instruction with the authority of a published one."
            )

        # A human step, or a protected one, is a step whose instances a human
        # closes. Enforced here rather than described in the SOP text, because
        # the class is what the revision policy protects — and protecting a
        # label that changed nothing would be protecting nothing.
        protected_here = sorted(set(sop.tags) & self.protected_tags)
        if sop.executor == policy.HUMAN or protected_here:
            gate = work_kwargs.get("verify")
            if not isinstance(gate, dict) or gate.get("kind") != "human":
                reason = (
                    f"is a human step" if sop.executor == policy.HUMAN
                    else f"carries protected tag(s) {protected_here}"
                )
                raise SopError(
                    f"SOP {sop_id!r} v{sop.version} {reason}, so every instance "
                    f"must carry a gate of kind 'human' naming its verifier. "
                    f"Pass verify={{'kind': 'human', 'check': ..., 'verifier': ...}} "
                    f"— a human step that an agent can close is not a human step."
                )

        metadata = dict(work_kwargs.pop("metadata", None) or {})
        # The caller's metadata is held to the create rule FIRST, because the
        # create below is filed `by_plane` so that the plan can be copied under
        # a reserved key — and a plane-side convenience that skipped the
        # caller's check would be the hole in the boundary it exists to keep.
        reject_reserved(metadata, work_kwargs.get("natural_key"))
        metadata["sop_ref"] = sop.ref
        # The plan, in the procedure's own words, pinned with the version. This
        # is what `plan_vs_actual` compares against at completion; copying it
        # here rather than looking it up later means the review reads the words
        # the executor was actually handed, even after the procedure moves on.
        metadata[PLAN_KEY] = {
            "title": sop.title,
            **{k: getattr(sop, k) for k in ("definition_of_done", "validation", "entry_check")
               if getattr(sop, k)},
        }
        return queue.create(title or sop.title, metadata=metadata, by_plane=True, **work_kwargs)

    # -- self-revision ---------------------------------------------------

    def proposals(self, sop_id: str, queue: Queue) -> dict:
        """Revision proposals accumulated against this template, from adjudications.

        Computed from the instances, never stored separately: every adjudicated
        instance pinned to this procedure is either a `good` divergence — the
        procedure was wrong, and the next version should account for it — or a
        `bad` one — the execution took a shortcut, and root-cause owns it. Each
        entry says whether a draft has already consumed it (`proposedIn`), so
        the same adjudication is never proposed twice and a reader can see what
        is still pending.
        """
        history = self.history(sop_id)
        if not history:
            raise SopError(f"no SOP {sop_id!r}")
        active = self.get(sop_id)
        revisions: list[dict] = []
        root_cause: list[dict] = []
        for item in queue.list():
            meta = item.metadata or {}
            ref = meta.get("sop_ref") or {}
            adjudication = meta.get("adjudication")
            if ref.get("sop_id") != sop_id or not isinstance(adjudication, dict):
                continue
            entry = {
                "itemId": item.id,
                "title": item.title,
                "pinnedVersion": ref.get("version"),
                "verdict": adjudication.get("verdict"),
                "by": adjudication.get("by"),
                "evidence": adjudication.get("evidence"),
                "at": adjudication.get("at"),
                "proposedIn": adjudication.get(PROPOSED_KEY),
                "flags": (meta.get("plan_vs_actual") or {}).get("flags"),
            }
            (revisions if entry["verdict"] == "good" else root_cause).append(entry)
        revisions.sort(key=lambda e: e["at"] or "")
        root_cause.sort(key=lambda e: e["at"] or "")
        latest = history[-1]
        return {
            "sopId": sop_id,
            "activeVersion": active.version if active else None,
            "latestVersion": latest.version,
            "latestStatus": latest.status.value,
            "openProposals": list(latest.proposals),
            "revisions": revisions,
            "rootCause": root_cause,
            "pending": sum(1 for e in revisions + root_cause if e["proposedIn"] is None),
        }

    def propose(
        self,
        sop_id: str,
        queue: Queue,
        *,
        author: Optional[str] = None,
        author_kind: Optional[str] = None,
    ) -> Optional[SOP]:
        """Draft the next version from the adjudications nobody has proposed yet.

        The loop closing, deliberately and never silently: a DRAFT, authored by
        whoever ran the pass (an agent unless the operator declared otherwise),
        so the revision policy applies in full — a protected step is refused, a
        lesson a human removed does not come back, and nothing here activates.
        The existing versions are not touched; an instance pinned to v1 reads
        the same v1 afterwards.

        `good` adjudications become entries in `proposals`: the procedure was
        wrong here, and this is the evidence the next author reads. `bad` ones
        become `common_mistakes` — the lesson channel the eval harness measures
        — because a shortcut one executor took is a failure mode the next one is
        warned about; root-cause keeps the pointer through `proposals()`.

        The lesson channel is capped (`MAX_COMMON_MISTAKES`). When the pending
        lessons would overflow it, this refuses rather than dropping one:
        which mistake stops biting is a human's call, and an agent making it
        would be removing what a human added — the policy would refuse that
        too, but this says why first.

        Returns None when nothing is pending, so a scheduled pass that finds
        nothing to do is a quiet run, not an error.
        """
        view = self.proposals(sop_id, queue)
        pending_good = [e for e in view["revisions"] if e["proposedIn"] is None]
        pending_bad = [e for e in view["rootCause"] if e["proposedIn"] is None]
        if not pending_good and not pending_bad:
            return None
        latest = self.history(sop_id)[-1]

        body: dict = {}
        if pending_bad:
            lessons = list(latest.common_mistakes)
            for entry in pending_bad:
                lesson = f"{entry['evidence']} (adjudicated bad on {entry['itemId']} by {entry['by']})"
                if lesson not in lessons:
                    lessons.append(lesson)
            if len(lessons) > MAX_COMMON_MISTAKES:
                raise SopError(
                    f"cannot draft {sop_id!r}: {len(pending_bad)} pending lesson(s) would "
                    f"put common_mistakes at {len(lessons)}; the cap is "
                    f"{MAX_COMMON_MISTAKES}. The cap is the discipline — which mistake "
                    f"has stopped biting is a human's call. Prune the list in a human "
                    f"revision, then run the pass again."
                )
            body["common_mistakes"] = lessons
        if pending_good:
            proposals = list(latest.proposals)
            for entry in pending_good:
                proposal = (
                    f"{entry['evidence']} (adjudicated good on {entry['itemId']} by "
                    f"{entry['by']}; v{entry['pinnedVersion']} was wrong here)"
                )
                if proposal not in proposals:
                    proposals.append(proposal)
            body[PROPOSALS_FIELD] = proposals

        draft = self.revise(sop_id, author=author, author_kind=author_kind, **body)
        for entry in pending_good + pending_bad:
            item = queue.get(entry["itemId"])
            if item is None:
                continue
            record = dict((item.metadata or {}).get("adjudication") or {})
            record[PROPOSED_KEY] = draft.version
            queue.annotate(item.id, {"adjudication": record})
        return draft

    # -- evaluation ------------------------------------------------------

    def outcomes_by_version(self, sop_id: str, queue: Queue) -> list[dict]:
        """Per version: how many instances, and how they ended.

        **Counts, not a success rate.** A bare percentage is gameable in both
        directions — an SOP applied to progressively harder cases looks like it
        is degrading, and failures re-filed as fresh items look like
        improvement. Raw counts with an explicit in-flight column let a reader
        see the denominator they are dividing by.

        `successRate` is present but is `None` until at least one instance has
        finished, and never treats in-flight work as either outcome. An
        unreported number must never read as a measured zero.

        **The two verify states get their own columns rather than a share of an
        outcome.** A gated instance that reported completion is not a success,
        not a failure, and not the same thing as one nobody has started — and
        `verify_failed` in particular is the single most informative number here,
        because it says the work claimed the procedure's own definition of done
        and the gate disagreed. Folded into `inFlight` it was indistinguishable
        from untouched work; folded into `failed` it would be a verdict that a
        later re-verify can overturn.

        Which is why neither enters `successRate`. The denominator is settled
        outcomes only, for the same reason a fractional credit was rejected: a
        number that silently moves when a gate answers later means the report you
        read today does not match the one you read next week, and nobody can
        tell which run changed.
        """
        by_version: dict[int, dict] = {}
        for sop in self.history(sop_id):
            by_version[sop.version] = {
                "version": sop.version,
                "status": sop.status.value,
                "instances": 0,
                "done": 0,
                "failed": 0,
                "awaitingVerify": 0,
                "verifyFailed": 0,
                "inFlight": 0,
                "successRate": None,
            }

        for item in queue.list():
            ref = (item.metadata or {}).get("sop_ref") or {}
            if ref.get("sop_id") != sop_id:
                continue
            row = by_version.get(ref.get("version"))
            if row is None:
                continue
            row["instances"] += 1
            if item.status == WorkStatus.DONE:
                row["done"] += 1
            elif item.status == WorkStatus.FAILED:
                row["failed"] += 1
            elif item.status == WorkStatus.AWAITING_VERIFY:
                row["awaitingVerify"] += 1
            elif item.status == WorkStatus.VERIFY_FAILED:
                row["verifyFailed"] += 1
            else:
                row["inFlight"] += 1

        for row in by_version.values():
            finished = row["done"] + row["failed"]
            if finished:
                row["successRate"] = round(row["done"] / finished, 3)
            # A rate computed over a handful of settled instances while a dozen
            # sit behind open gates is technically true and reads as the whole
            # picture. Say how much of the version is still unresolved, in the
            # same row, rather than leaving the reader to add three columns.
            row["unresolved"] = row["awaitingVerify"] + row["verifyFailed"] + row["inFlight"]
        return [by_version[v] for v in sorted(by_version)]

    def drifted(self, sop_id: str, queue: Queue) -> list[dict]:
        """In-flight items pinned to a version that is no longer the active one.

        Reported, never migrated. Re-pointing running work at a newer procedure
        changes the job under whoever is doing it, and they would have no way to
        know — the same reason a divergence notice re-baselines nothing on its
        own. What to do about it is the human's call.
        """
        active = self.get(sop_id)
        if active is None:
            return []
        out = []
        for item in queue.list():
            ref = (item.metadata or {}).get("sop_ref") or {}
            if ref.get("sop_id") != sop_id:
                continue
            if item.status in (WorkStatus.DONE, WorkStatus.FAILED):
                continue
            pinned = ref.get("version")
            if pinned != active.version:
                out.append(
                    {
                        "itemId": item.id,
                        "title": item.title,
                        "pinnedVersion": pinned,
                        "activeVersion": active.version,
                        "status": item.status.value,
                    }
                )
        return out
