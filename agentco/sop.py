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

import fcntl
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

from agentco.work import Queue, WorkItem, WorkStatus

# The cap IS the discipline. An unbounded list of known failure modes is a wiki
# page, and a wiki page is not read at handoff time. Keep the ones that bite.
MAX_COMMON_MISTAKES = 3

TEXT_FIELDS = ("purpose", "trigger", "inputs", "definition_of_done")


class SopError(Exception):
    """Base for every refusal in this module."""


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
    inputs: Optional[str] = None
    definition_of_done: Optional[str] = None
    common_mistakes: list[str] = field(default_factory=list)

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
    allowed = set(TEXT_FIELDS) | {"common_mistakes"}
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

    if not out:
        raise SopContractError(
            "an SOP with no fields set reads as delegation-ready and hands its "
            "executor nothing. Fill at least one field."
        )
    return out


class SopLibrary:
    """Versioned SOP storage. Same JSONL-under-a-lock shape as the work queue."""

    def __init__(self, path: Path | str = "sops.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Raw BYTES — a line that failed to decode has no faithful string
        # form, and is carried through every write rather than dropped.
        self.quarantined: list[bytes] = []

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with open(lock_path, "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

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

    def create(self, title: str, **body) -> SOP:
        """Write version 1, as a DRAFT.

        Draft rather than active because `instantiate()` refuses a draft: a
        procedure should be readable by the person who will follow it before it
        starts producing work for them.
        """
        validated = validate_fields(body)
        sop = SOP(
            sop_id=f"sop-{uuid.uuid4().hex[:8]}",
            version=1,
            title=title,
            status=SopStatus.DRAFT,
            **validated,
        )
        with self._locked():
            existing = self._read_all()
            existing.append(sop)
            self._write_all(existing, self.quarantined)
        return sop

    def revise(self, sop_id: str, title: Optional[str] = None, **body) -> SOP:
        """Write the NEXT version. The previous one is superseded, never edited.

        Unset fields carry forward from the version being revised, so a change
        to one line does not silently blank the other four. Pass `None`
        explicitly to clear a field.

        The old version stays in the store with `superseded_by` set. Deleting it
        would orphan every instance pinned to it, and those instances are the
        entire evidence base for whether the revision was an improvement.
        """
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
            carried.update({k: v for k, v in body.items()})
            validated = validate_fields({k: v for k, v in carried.items() if v is not None})

            new = SOP(
                sop_id=sop_id,
                version=latest.version + 1,
                title=title or latest.title,
                status=SopStatus.DRAFT,
                **validated,
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

    def activate(self, sop_id: str, version: int) -> SOP:
        """Make one version the one `instantiate()` uses by default."""
        with self._locked():
            all_sops = self._read_all()
            target = None
            for sop in all_sops:
                if sop.sop_id == sop_id and sop.version == version:
                    target = sop
            if target is None:
                raise SopError(f"no SOP {sop_id!r} version {version}")
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

        metadata = dict(work_kwargs.pop("metadata", None) or {})
        metadata["sop_ref"] = sop.ref
        return queue.create(title or sop.title, metadata=metadata, **work_kwargs)

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
        """
        by_version: dict[int, dict] = {}
        for sop in self.history(sop_id):
            by_version[sop.version] = {
                "version": sop.version,
                "status": sop.status.value,
                "instances": 0,
                "done": 0,
                "failed": 0,
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
            else:
                row["inFlight"] += 1

        for row in by_version.values():
            finished = row["done"] + row["failed"]
            if finished:
                row["successRate"] = round(row["done"] / finished, 3)
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
