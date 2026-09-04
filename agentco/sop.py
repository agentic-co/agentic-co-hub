"""ASOPs as versioned sequences of gated steps that runs pin.

The record contract — `ASOP`, `Step`, `SopStatus`, `validate_asop`,
`validate_step` — lives in `asop.sop`, the package shared with any harness
that pins the same `(asop_id, version, step)` off a work item (see
`packages/asop/ASOP.md`). This module re-exports that contract and adds
everything about KEEPING a procedure that a bare record shape does not
answer: the versioned store (`SopLibrary` below), file locking, the revision
policy it enforces, and the filing of runs.

**v3 changed the grain, and two things follow from it.**

*The ASOP is the sequence; the step is what v2 called the procedure.* One
ASOP no longer files one work item — `run()` files a TREE: a parent pinned
`(asop_id, version)` carrying the run's inputs, and one child per step pinned
`(asop_id, version, step)`, each carrying a copy of its step's text and its
gate, with `blocked_by` taken from the step's `after`. Outcomes are counted
per version AND per step, which is the only way "did the v3 rewrite of step 2
help?" is answerable by counting rather than by recollection.

*The gate is on the step, authored with the version.* v2's `instantiate()`
took the gate from whoever filed the work. Where the filer is on the
executor's side — the ordinary case for a single-operator organisation — that
is the executor's side authoring the check it will be graded by, which is the
failure mode the contract exists to prevent. So `run()` supplies no gate and
**refuses one if passed**.

**A template is not an instance, and the distinction is load-bearing.** An
ASOP never enters the queue. If it did it would be claimable, `ready()` would
offer it, somebody would complete it, and the template would be `done`.

**Versions are immutable.** `revise()` writes a NEW version; it never mutates
what is there, and it does not promote — the version in use stays in use
until `activate()` says otherwise. `retire()` withdraws one with no
successor: existing runs finish under their pin, no new runs file, and the
record is kept forever, because a pin to it must stay resolvable or its
outcomes become unreadable.

**Legacy v2 rows are upgraded, never dropped.** `upgrade_legacy()` reads a v2
`SOP` as a one-step ASOP so that every pin written before v3 still resolves.
It is the same function the numbered SQL migration runs and the JSONL store
applies on read; see its docstring for the one thing it has to invent.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Iterator, Optional, Sequence

from agentco import policy
from agentco.errors import Refusal
from agentco.filelock import lock_exclusive, unlock
from agentco.work import (
    PARENT_KEY,
    PLAN_KEY,
    Queue,
    WorkItem,
    WorkStatus,
    reject_reserved,
)
from asop.gates import GATE_FIELDS
from asop.gates import validate_gate as _validate_gate
from asop.sop import (
    ASOP,
    ASOP_TEXT_FIELDS,
    ASOP_VERSION,
    MAX_COMMON_MISTAKES,
    MAX_DEPTH,
    MAX_STEPS,
    PROPOSED_KEY,
    STEP_TEXT_FIELDS,
    SOP,
    SopContractError,
    SopError,
    SopStatus,
    Step,
    lesson_text,
    validate_asop,
    validate_fields,
    validate_step,
)

__all__ = [
    "ASOP",
    "ASOP_VERSION",
    "MAX_COMMON_MISTAKES",
    "MAX_DEPTH",
    "MAX_STEPS",
    "PROPOSED_KEY",
    "RUN_KEY",
    "REF_KEY",
    "SOP",
    "SOP_STORE_ENV_VAR",
    "DEFAULT_SOP_STORE",
    "SopContractError",
    "SopError",
    "SopLibrary",
    "SopStatus",
    "Step",
    "lesson_text",
    "pin_of",
    "resolve_sop_store",
    "step_payload",
    "upgrade_legacy",
    "validate_asop",
    "validate_fields",
    "validate_step",
]

SOP_STORE_ENV_VAR = "AGENTCO_SOP_STORE"
DEFAULT_SOP_STORE = "sops.jsonl"

#: `metadata.sop_ref` — the pin. `{asop_id, version}` on a run's parent bead,
#: `{asop_id, version, step}` on each step bead. The key name is unchanged
#: from v2 on purpose: it is a reserved metadata key that half the plane reads
#: back as fact, and renaming it would have been a migration of every reader
#: for a word. `pin_of` normalises the v2 spelling of its CONTENT.
REF_KEY = "sop_ref"

#: `metadata.sop_run` — what a run was given: its inputs, and the bindings the
#: caller chose for each role. Plane-written and read back as fact by
#: `run_get`, so it is reserved like the pin.
RUN_KEY = "sop_run"

#: What a v2 record could not carry and a v3 step must: a gate. The upgrade
#: fails CLOSED to a human gate — see `upgrade_legacy`.
LEGACY_VERIFIER = "operator"
LEGACY_PARK_SECONDS = 86400


def resolve_sop_store(path: Optional[str] = None) -> str:
    """Where the library lives — see `work.resolve_work_store` for why here."""
    return path or os.environ.get(SOP_STORE_ENV_VAR) or DEFAULT_SOP_STORE


def _refuse(code: str, message: str, remediation: str, status: int = 422) -> Refusal:
    return Refusal(code=code, message=message, remediation=remediation, http_status=status)


# --------------------------------------------------------------------------- #
# pins, payloads, and the v2 upgrade
# --------------------------------------------------------------------------- #


def pin_of(item_or_metadata) -> dict:
    """The pin on a work item, with v2's `sop_id` spelling normalised to `asop_id`.

    Read through this rather than off `metadata['sop_ref']` directly. Items
    filed before v3 pin `{sop_id, version}`; §2.1 requires those pins to stay
    resolvable forever, and a reader that only knew one spelling would count
    every pre-v3 outcome as belonging to no procedure at all.
    """
    metadata = getattr(item_or_metadata, "metadata", item_or_metadata) or {}
    ref = dict(metadata.get(REF_KEY) or {})
    if not ref:
        return {}
    if "asop_id" not in ref and "sop_id" in ref:
        ref["asop_id"] = ref.pop("sop_id")
    return ref


def gate_payload(gate: Optional[dict]) -> Optional[dict]:
    """A normalised gate back in the shape `validate_gate` accepts.

    `validate_gate` returns every known field, `None` where absent, plus
    `schema_version` — and refuses an unknown key on the way in. So a gate
    read out of storage cannot be handed straight back to it, which is
    exactly what `revise` does when it carries a step forward unchanged.
    """
    if not gate:
        return None
    return {k: v for k, v in gate.items() if k in GATE_FIELDS and v not in (None, "", [])}


def step_payload(step: Step) -> dict:
    """A `Step` back in the shape `validate_step` accepts.

    Empty values are dropped rather than sent as `None`, because a nested step
    (`uses`) is refused if it carries a body — and `asdict` gives every body
    key, `None`-valued, to every step including the nested ones.
    """
    raw = asdict(step)
    out = {k: v for k, v in raw.items() if k != "step" and v not in (None, [], {})}
    if step.gate is not None:
        out["gate"] = gate_payload(step.gate)
    # `after` is meaningful when EMPTY (a step with no predecessor runs in
    # parallel with step 1), so it survives the drop above explicitly.
    out["after"] = list(step.after or [])
    return out


def asop_payload(asop: ASOP) -> dict:
    """An `ASOP` back in the shape `validate_asop` accepts. The identity fields
    (`asop_id`, `version`, `status`, author, timestamps) belong to the store."""
    return {
        "title": asop.title,
        **{k: getattr(asop, k) for k in ("task_type", *ASOP_TEXT_FIELDS)
           if getattr(asop, k) is not None},
        "inputs": [dict(rec) for rec in asop.inputs or []],
        "roles": {name: dict(spec) for name, spec in (asop.roles or {}).items()},
        "constraints": [dict(c) for c in asop.constraints or []],
        "steps": [step_payload(s) for s in asop.steps],
        **({"proposals": list(asop.proposals)} if asop.proposals else {}),
    }


def upgrade_legacy(sop: SOP) -> ASOP:
    """A v2 record read as the one-step ASOP it always was.

    Everything maps across unchanged except the one thing v2 did not have: a
    gate. The record carried none — whoever filed the work supplied it — so
    there is nothing to migrate and something has to be chosen.

    **It is chosen to fail closed: a `human` gate.** The alternative readings
    are worse in the same direction. A `deterministic` gate would assert that
    the record's `validation` prose is a command that exits 0, which nothing
    ever checked. A `judged` gate would route the step to a route the operator
    never declared. A `human` gate parks the step for a person, and a person
    is exactly who decides what this procedure's real gate should be — by
    revising it, which is one call. The blast radius is small by construction:
    a draft or superseded version cannot be run at all, so this only reaches a
    v2 record that was ACTIVE at the moment of the upgrade.

    The verifier is the version's recorded author when there is one, and
    `operator` when there is not. A name nobody holds does not silently pass:
    the clock escalates to the same destination, which is the visible outcome
    rather than the quiet one.
    """
    verifier = (sop.author or LEGACY_VERIFIER).strip() or LEGACY_VERIFIER
    check = (
        sop.validation
        or sop.definition_of_done
        or sop.purpose
        or f"a person confirms {sop.title!r} was carried out"
    )
    gate = _validate_gate({
        "kind": "human",
        "check": check,
        "verifier": verifier,
        "max_park_seconds": LEGACY_PARK_SECONDS,
        "on_timeout": "escalate",
        "escalate_to": verifier,
    })
    role = "executor"
    step = Step(
        step=1,
        name=sop.title,
        role=role,
        purpose=sop.purpose,
        entry_check=sop.entry_check,
        inputs=sop.inputs,
        definition_of_done=sop.definition_of_done,
        validation=sop.validation,
        write_back=sop.write_back,
        common_mistakes=list(sop.common_mistakes or []),
        tags=list(sop.tags or []),
        # A v2 proposal was a proposal about the procedure, and the procedure
        # was this step. It belongs on the step, not on the sequence.
        proposals=list(sop.proposals or []),
        gate=gate,
        after=[],
    )
    return ASOP(
        asop_id=sop.sop_id,
        version=sop.version,
        title=sop.title,
        status=sop.status,
        task_type=None,
        purpose=sop.purpose,
        trigger=sop.trigger,
        inputs=[],
        roles={role: {"kind": sop.executor or policy.AGENT}},
        constraints=[],
        steps=[step],
        author=sop.author,
        author_kind=sop.author_kind,
        proposals=[],
        superseded_by=sop.superseded_by,
        created_at=sop.created_at,
    )


def read_record(line: str) -> ASOP:
    """One stored line as an ASOP, whichever generation wrote it."""
    raw = json.loads(line)
    if "asop_id" in raw:
        return ASOP.from_json(line)
    if "sop_id" in raw:
        return upgrade_legacy(SOP.from_json(line))
    raise SopContractError("a stored procedure names neither 'asop_id' nor 'sop_id'")


# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #


class SopLibrary:
    """Versioned ASOP storage. Same JSONL-under-a-lock shape as the work queue."""

    def __init__(self, path: Path | str = "sops.jsonl", protected_tags: Optional[Sequence[str]] = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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

    def _read_all(self) -> list[ASOP]:
        """Every readable version, v2 lines upgraded in place on the way out.

        Reads BYTES and decodes per line for the same reason the work queue
        does: decoding the whole file puts `UnicodeDecodeError` — a
        `ValueError`, not a `JSONDecodeError` — outside the per-line handler,
        and one stray byte then makes the entire library unreadable.

        The upgrade is applied on READ rather than by a one-shot rewrite
        because a file store has no migration runner to hang one off. A legacy
        line is therefore resolvable from the first read, and the upgraded
        form is what the next write persists.
        """
        if not self.path.exists():
            return []
        out: list[ASOP] = []
        quarantined: list[bytes] = []
        for raw_line in self.path.read_bytes().split(b"\n"):
            if not raw_line.strip():
                continue
            try:
                out.append(read_record(raw_line.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                quarantined.append(raw_line)
        self.quarantined = quarantined
        return out

    def _write_all(self, asops: list[ASOP], quarantined: Sequence[bytes] = ()) -> None:
        """Atomic replace, carrying quarantined lines through verbatim.

        **Losing a row here is worse than losing one in the work queue.** A
        deleted row frees its version number, and `revise()` computes the next
        version as `max(...) + 1` over the SURVIVORS — so it reissues the
        destroyed number to different text. A run pinned to v2 then resolves
        to a procedure it never ran. The pin is unchanged; what it resolves to
        is not, which is the one thing versioning exists to prevent.
        """
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                for asop in asops:
                    handle.write(asop.to_json().encode("utf-8") + b"\n")
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
    ) -> ASOP:
        """Write version 1, as a DRAFT.

        Draft rather than active because `run()` refuses a draft: a procedure
        should be readable by the people who will follow it before it starts
        producing work for them.

        `author` / `author_kind` record who wrote it. An unstated kind is
        `agent` — fail closed, the same reading the policy gives an actor the
        operator never declared human.
        """
        validated = validate_asop({"title": title, **body})
        asop = self._compose(
            asop_id=f"asop-{uuid.uuid4().hex[:8]}",
            version=1,
            validated=validated,
            author=author,
            author_kind=author_kind or policy.AGENT,
        )
        with self._locked():
            existing = self._read_all()
            existing.append(asop)
            self._write_all(existing, self.quarantined)
        return asop

    @staticmethod
    def _compose(*, asop_id: str, version: int, validated: dict,
                 author: Optional[str], author_kind: str) -> ASOP:
        body = dict(validated)
        steps = [Step(**st) for st in body.pop("steps")]
        return ASOP(
            asop_id=asop_id,
            version=version,
            title=body.pop("title"),
            status=SopStatus.DRAFT,
            steps=steps,
            author=author,
            author_kind=author_kind,
            **body,
        )

    def revise(
        self,
        asop_id: str,
        title: Optional[str] = None,
        *,
        author: Optional[str] = None,
        author_kind: Optional[str] = None,
        **body,
    ) -> ASOP:
        """Write the NEXT version. The previous one is superseded, never edited.

        Unset fields carry forward from the version being revised — including
        the whole `steps` list, so a change to `purpose` does not silently
        blank the procedure. Pass `steps` to replace the sequence.

        **The revision policy runs here, before anything is written.** When
        `author_kind` is not `human` — and an unstated kind is not — the three
        rules in `agentco/policy.py` are checked per step against the whole
        history, and a refusal leaves the store byte-identical. This is the
        write boundary every transport funnels through, which is why the
        policy lives here and not in a handler.
        """
        reviser_kind = author_kind or policy.AGENT
        with self._locked():
            all_asops = self._read_all()
            versions = [a for a in all_asops if a.asop_id == asop_id]
            if not versions:
                raise SopError(
                    f"no ASOP {asop_id!r}. Revising something that does not exist "
                    f"would create version 1 under a caller's assumption that "
                    f"they were editing a procedure people already follow."
                )
            latest = max(versions, key=lambda a: a.version)

            # REFUSE rather than guess when part of the history is unreadable.
            # The next version is `max(...) + 1` over what can be PARSED, so a
            # quarantined row silently frees its number and `revise` reissues
            # it to different text. A run pinned to that version then resolves
            # to a procedure it never ran.
            if self.quarantined:
                raise SopError(
                    f"cannot revise {asop_id!r}: {len(self.quarantined)} line(s) in "
                    f"the ASOP store could not be parsed, so the full version "
                    f"history is not visible and the next version number cannot "
                    f"be chosen safely. Repair or remove those lines first — they "
                    f"are preserved verbatim in the file. Reissuing a version "
                    f"number would silently re-point every run pinned to it."
                )

            carried = asop_payload(latest)
            if title is not None:
                carried["title"] = title
            carried.update({k: v for k, v in body.items() if v is not None})
            validated = validate_asop(carried)

            new = self._compose(
                asop_id=asop_id,
                version=latest.version + 1,
                validated=validated,
                author=author,
                author_kind=reviser_kind,
            )
            policy.check_asop_revision(
                history=versions,
                baseline=latest,
                proposed=new,
                reviser_kind=reviser_kind,
                protected_tags=self.protected_tags,
                action="revise",
            )
            for asop in all_asops:
                if asop.asop_id == asop_id and asop.superseded_by is None:
                    # `superseded_by` records that a later version exists. It
                    # does NOT deactivate: an ACTIVE version stays active until
                    # `activate()` promotes the replacement. Otherwise merely
                    # DRAFTING an improvement would take the live procedure out
                    # of service.
                    asop.superseded_by = new.version
            all_asops.append(new)
            self._write_all(all_asops, self.quarantined)
        return new

    def activate(
        self,
        asop_id: str,
        version: int,
        *,
        author: Optional[str] = None,
        author_kind: Optional[str] = None,
    ) -> ASOP:
        """Make one version the one `run()` uses by default.

        Policed like a revision, measured against the version currently active
        (or the latest, when none is). Without this, the policy has a door
        beside it: an agent forbidden from re-adding a step a human removed
        could simply re-activate the version from before the human removed it.
        """
        reviser_kind = author_kind or policy.AGENT
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise Refusal(
                code="version_required",
                message="activate names a specific version",
                remediation="Send {\"version\": N} — the version you mean to make active.",
                http_status=400,
            )
        with self._locked():
            all_asops = self._read_all()
            target = None
            for asop in all_asops:
                if asop.asop_id == asop_id and asop.version == version:
                    target = asop
            if target is None:
                raise SopError(f"no ASOP {asop_id!r} version {version}")
            if target.status is SopStatus.RETIRED:
                raise SopError(
                    f"ASOP {asop_id!r} v{version} is retired. A withdrawn procedure "
                    f"is not re-activated in place — the record is kept so pins to "
                    f"it stay resolvable, and bringing it back is a new version "
                    f"through `revise`, which the revision policy can see."
                )
            versions = [a for a in all_asops if a.asop_id == asop_id]
            baseline = next(
                (a for a in versions if a.status == SopStatus.ACTIVE),
                max(versions, key=lambda a: a.version),
            )
            policy.check_asop_revision(
                history=versions,
                baseline=baseline,
                proposed=target,
                reviser_kind=reviser_kind,
                protected_tags=self.protected_tags,
                action="activate",
            )
            for asop in all_asops:
                if asop.asop_id == asop_id and asop.status == SopStatus.ACTIVE:
                    asop.status = SopStatus.SUPERSEDED
            target.status = SopStatus.ACTIVE
            target.superseded_by = None
            self._write_all(all_asops, self.quarantined)
        return target

    def retire(
        self,
        asop_id: str,
        *,
        author: Optional[str] = None,
        author_kind: Optional[str] = None,
    ) -> ASOP:
        """Withdraw the active version with no successor (ASOP.md §4).

        Human-only, in the same spirit as the ratchet rule: an agent that
        learns "this procedure is slow" and withdraws it is doing exactly what
        the loop asks of it, and the procedure it withdrew may have been the
        one that keeps a payment run from sending money that cannot be
        recalled.

        In-flight runs finish under their pin and the record is kept forever —
        a retired version that stopped resolving would make every outcome
        counted against it unreadable, which is the opposite of what
        versioning is for.
        """
        policy.require_human(
            author_kind or policy.AGENT,
            "sop_retire",
            because=(
                "Withdrawing a procedure ends it with no successor, and an agent "
                "that finds a step expensive would be the party deciding nobody "
                "follows it any more."
            ),
        )
        with self._locked():
            all_asops = self._read_all()
            versions = [a for a in all_asops if a.asop_id == asop_id]
            if not versions:
                raise SopError(f"no ASOP {asop_id!r}")
            target = next((a for a in versions if a.status == SopStatus.ACTIVE), None)
            if target is None:
                latest = max(versions, key=lambda a: a.version)
                raise SopError(
                    f"ASOP {asop_id!r} has no active version to retire — "
                    f"v{latest.version} is a {latest.status.value}. Retiring "
                    f"withdraws the procedure people are following; there is "
                    f"nothing here anybody is following."
                )
            target.status = SopStatus.RETIRED
            target.superseded_by = None
            self._write_all(all_asops, self.quarantined)
        return target

    # -- reading ---------------------------------------------------------

    def get(self, asop_id: str, version: Optional[int] = None) -> Optional[ASOP]:
        """One version, or the active one when `version` is omitted.

        A pinned lookup must resolve even when the version is superseded or
        retired — otherwise a run's own procedure becomes unreadable the
        moment it is improved or withdrawn.
        """
        versions = [a for a in self._read_all() if a.asop_id == asop_id]
        if not versions:
            return None
        if version is not None:
            return next((a for a in versions if a.version == version), None)
        return next((a for a in versions if a.status == SopStatus.ACTIVE), None)

    def history(self, asop_id: str) -> list[ASOP]:
        return sorted(
            (a for a in self._read_all() if a.asop_id == asop_id),
            key=lambda a: a.version,
        )

    def list_active(self) -> list[ASOP]:
        return [a for a in self._read_all() if a.status == SopStatus.ACTIVE]

    # -- running ---------------------------------------------------------

    def run(
        self,
        asop_id: str,
        queue: Queue,
        *,
        inputs: Optional[dict] = None,
        bindings: Optional[dict] = None,
        version: Optional[int] = None,
        title: Optional[str] = None,
        metadata: Optional[dict] = None,
        **work_kwargs,
    ) -> dict:
        """File one execution of one ASOP version as a TREE (ASOP.md §5.1).

        A parent bead pins `(asop_id, version)` and carries the run's inputs;
        one child per step pins `(asop_id, version, step)`, carries a COPY of
        its step's text and its gate, and is `blocked_by` the beads of the
        steps it is `after`. The child is written into the parent's
        `blocked_by` in the same lock that files it, so a parent cannot close
        while a step is open.

        **Bindings come from the caller.** A harness knows its own agents; the
        plane does not, and §7 puts bindings squarely on the local side of the
        line. There is no default and no inference: a role with no binding is
        `role_unbound`, every time.

        **The caller supplies no gate.** Passing `verify` is refused — that is
        the v2 failure this version exists to close.

        Every refusal fires before anything is filed, so a refused run leaves
        the queue byte-identical.
        """
        if "verify" in work_kwargs:
            raise SopError(
                f"run({asop_id!r}) was passed a gate. In v3 the gate is part of the "
                f"step, authored with the version, and immutable to whoever files "
                f"the work — a filer who could supply one is the executor's own "
                f"side authoring the check it will be graded by, which is the "
                f"failure this contract exists to prevent (ASOP.md §2.2). Remove "
                f"`verify`; every step bead already carries its step's gate."
            )
        asop = self._runnable(asop_id, version)
        supplied = dict(inputs or {})
        bound = dict(bindings or {})
        plan = self._plan(asop, supplied, bound, depth=1, seen=())

        reject_reserved(metadata, work_kwargs.get("natural_key"))
        parent_metadata = dict(metadata or {})
        parent_metadata[REF_KEY] = asop.ref
        parent_metadata[RUN_KEY] = {
            "asop_id": asop.asop_id,
            "version": asop.version,
            "inputs": supplied,
            "bindings": bound,
        }
        parent_metadata[PLAN_KEY] = {
            "title": asop.title,
            **{k: getattr(asop, k) for k in ASOP_TEXT_FIELDS if getattr(asop, k)},
        }
        parent = queue.create(
            title or asop.title, metadata=parent_metadata, by_plane=True, **work_kwargs
        )
        return {
            "runId": parent.id,
            "asopId": asop.asop_id,
            "version": asop.version,
            "inputs": supplied,
            "bindings": bound,
            "steps": self._file(plan, queue, parent.id),
        }

    def _runnable(self, asop_id: str, version: Optional[int], *, nested: bool = False) -> ASOP:
        """The version a run may file from, or the refusal that says why not."""
        asop = self.get(asop_id, version)
        if asop is None:
            versions = self.history(asop_id)
            if version is not None:
                raise SopError(f"no ASOP {asop_id!r} version {version}")
            if versions:
                latest = versions[-1]
                raise SopError(
                    f"ASOP {asop_id!r} has no active version — v{latest.version} is "
                    f"a {latest.status.value}. Activate it first: generating work "
                    f"from an unactivated procedure hands somebody a half-written "
                    f"instruction with the authority of a published one."
                )
            raise SopError(f"no ASOP {asop_id!r}")
        if asop.status is SopStatus.DRAFT:
            raise SopError(
                f"ASOP {asop_id!r} v{asop.version} is a draft. Activate it first — "
                f"generating work from an unactivated procedure hands somebody a "
                f"half-written instruction with the authority of a published one."
            )
        if asop.status is SopStatus.RETIRED:
            raise SopError(
                f"ASOP {asop_id!r} v{asop.version} is retired: it was withdrawn with "
                f"no successor, so no new runs file from it. Runs already in flight "
                f"finish under their pin, and the record is kept so those pins stay "
                f"resolvable."
            )
        # A nested step names an exact version, so a SUPERSEDED inner ASOP is
        # legitimate — the outer author pinned it deliberately and a later
        # inner version must not reach back into it. Only DRAFT and RETIRED
        # are refused, above, and they are refused for a nested step too.
        if not nested and version is None and asop.status is not SopStatus.ACTIVE:
            raise SopError(f"ASOP {asop_id!r} v{asop.version} is {asop.status.value}, not active")
        return asop

    def _plan(self, asop: ASOP, inputs: dict, bindings: dict, *, depth: int,
              seen: tuple) -> list[dict]:
        """Resolve one ASOP into node specs, recursively, refusing before filing.

        Recursion is how the depth bound is honoured, not how it is broken:
        `depth` counts ASOP nesting levels and the bound is `MAX_DEPTH`, the
        same number the bead tree enforces one level lower. Checking it here
        rather than letting `enforce_decomposition` catch it mid-file is what
        keeps a refused run from leaving half a tree behind.
        """
        if depth > MAX_DEPTH:
            raise _refuse(
                "decomposition_bound",
                f"filing {asop.asop_id!r} would nest procedures {depth} deep; the "
                f"bound is {MAX_DEPTH}",
                "A procedure nested deeper than three levels is deeper than one "
                "accountable person can review. Flatten a level, or file the inner "
                "procedure as a run of its own.",
            )
        if asop.asop_id in seen:
            raise _refuse(
                "decomposition_bound",
                f"{asop.asop_id!r} nests itself ({' -> '.join((*seen, asop.asop_id))})",
                "A procedure that uses itself has no bottom. Break the cycle, or "
                "make the recursive part a run the harness files when it needs one.",
            )

        missing = [rec["name"] for rec in asop.inputs or [] if rec["name"] not in inputs]
        if missing:
            raise _refuse(
                "inputs_missing",
                f"{asop.asop_id!r} v{asop.version} declares input(s) {missing} and the "
                f"run supplied none of them",
                f"Send every declared input by name: {[r['name'] for r in asop.inputs]}. "
                f"A run missing an input hands its first step a question the step's "
                f"entry_check was written to make somebody ask.",
            )

        needed = sorted({s.role for s in asop.steps if s.role})
        unbound = [role for role in needed if not (bindings.get(role) or "").strip()]
        if unbound:
            raise _refuse(
                "role_unbound",
                f"{asop.asop_id!r} v{asop.version} declares role(s) {unbound} and the "
                f"run bound none of them",
                "Send bindings={role: actor} for every role the procedure names. The "
                "plane never invents a binding — an ASOP names roles precisely so "
                "that two organisations can run the same version with different "
                "agents, and only the harness knows its own.",
                status=409,
            )
        for constraint in asop.constraints or []:
            roles = list(constraint.get("distinct") or ())
            filled = [bindings.get(r) for r in roles]
            if len(set(filled)) != len(filled):
                raise _refuse(
                    "constraint_unsatisfiable",
                    f"{asop.asop_id!r} v{asop.version} requires {roles} to be distinct "
                    f"and the run binds them to {filled}",
                    "Bind a different actor to one of them. A separation of duties "
                    "satisfied by binding the same agent twice is not a separation "
                    "of duties, and the plane will not quietly do it for you.",
                    status=409,
                )
        # The constraint the contract requires by construction (§3.6): a judged
        # step is evaluated by a route distinct from the one that executed the
        # step it judges. Two DIFFERENT roles bound to the SAME actor would
        # otherwise defeat a `distinct` the author never had to write.
        by_position = {s.step: s for s in asop.steps}
        for step in asop.steps:
            if step.uses is not None or (step.gate or {}).get("kind") != "judged":
                continue
            judge = bindings.get(step.role)
            for ref in step.after or []:
                prior = by_position.get(ref)
                if prior is None or prior.role is None:
                    continue
                if bindings.get(prior.role) == judge:
                    raise _refuse(
                        "constraint_unsatisfiable",
                        f"step {step.step} ({step.name!r}) has a judged gate and is bound "
                        f"to {judge!r}, which also executes step {ref} ({prior.name!r})",
                        "Bind a different actor to the judging step's role. No "
                        "self-grading is the one constraint the contract requires "
                        "whether or not the author wrote it down.",
                        status=409,
                    )

        nodes: list[dict] = []
        for step in asop.steps:
            node = {
                "step": step.step,
                "name": step.name,
                "pin": asop.step_ref(step.step),
                "after": list(step.after or []),
                "role": step.role,
                "binding": bindings.get(step.role) if step.role else None,
                "gate": step.gate,
                "plan": self._step_plan(asop, step),
                "children": [],
            }
            if step.uses is not None:
                inner = self._runnable(step.uses["asop_id"], step.uses["version"], nested=True)
                node["uses"] = dict(step.uses)
                node["children"] = self._plan(
                    inner, inputs, bindings, depth=depth + 1, seen=(*seen, asop.asop_id)
                )
            nodes.append(node)
        return nodes

    @staticmethod
    def _step_plan(asop: ASOP, step: Step) -> dict:
        """The step's own words, copied under the pin at filing time.

        Copied rather than looked up later so `plan_vs_actual` reads the words
        the executor was actually handed, even after the procedure moves on.
        """
        plan = {
            "title": asop.title,
            "name": step.name,
            **({"role": step.role} if step.role else {}),
            **{k: getattr(step, k) for k in STEP_TEXT_FIELDS if getattr(step, k)},
        }
        if step.common_mistakes:
            plan["common_mistakes"] = list(step.common_mistakes)
        if step.uses is not None:
            plan["uses"] = dict(step.uses)
        return plan

    def _file(self, nodes: list[dict], queue: Queue, parent_id: str) -> list[dict]:
        """File one level of the tree, in order, resolving `after` to ids."""
        by_step: dict[int, str] = {}
        filed: list[dict] = []
        for node in nodes:
            metadata = {
                PARENT_KEY: parent_id,
                REF_KEY: node["pin"],
                PLAN_KEY: node["plan"],
            }
            item = queue.create(
                node["name"],
                metadata=metadata,
                verify=gate_payload(node["gate"]),
                assigned_agent=node["binding"],
                blocked_by=[by_step[ref] for ref in node["after"] if ref in by_step],
                by_plane=True,
            )
            by_step[node["step"]] = item.id
            entry = {
                "step": node["step"],
                "name": node["name"],
                "itemId": item.id,
                "role": node["role"],
                "binding": node["binding"],
                "children": [],
            }
            if node["children"]:
                entry["uses"] = node["uses"]
                entry["children"] = self._file(node["children"], queue, item.id)
            filed.append(entry)
        return filed

    def run_get(self, run_id: str, queue: Queue) -> dict:
        """The tree with statuses and pins (ASOP.md §8.2)."""
        parent = queue.get(run_id)
        if parent is None:
            from agentco.work import unknown_item

            raise unknown_item(run_id, "read as a run")
        return self._run_view(parent, queue, self._children_index(queue))

    def run_list(self, queue: Queue, asop_id: Optional[str] = None,
                 status: Optional[WorkStatus] = None) -> list[dict]:
        """Runs, newest first. A run parent is an item pinned without a step."""
        index = self._children_index(queue)
        out = []
        for item in queue.list():
            ref = pin_of(item)
            if not ref or "step" in ref or RUN_KEY not in (item.metadata or {}):
                continue
            if asop_id is not None and ref.get("asop_id") != asop_id:
                continue
            if status is not None and item.status != status:
                continue
            out.append(self._run_view(item, queue, index))
        out.sort(key=lambda r: r["createdAt"] or "", reverse=True)
        return out

    @staticmethod
    def _children_index(queue: Queue) -> dict:
        index: dict[str, list[WorkItem]] = {}
        for item in queue.list():
            parent = (item.metadata or {}).get(PARENT_KEY)
            if parent:
                index.setdefault(parent, []).append(item)
        return index

    def _run_view(self, parent: WorkItem, queue: Queue, index: dict) -> dict:
        ref = pin_of(parent)
        record = (parent.metadata or {}).get(RUN_KEY) or {}
        active = self.get(ref.get("asop_id")) if ref.get("asop_id") else None
        return {
            "runId": parent.id,
            "asopId": ref.get("asop_id"),
            "version": ref.get("version"),
            "title": parent.title,
            "status": parent.status.value,
            "createdAt": parent.created_at,
            "inputs": record.get("inputs") or {},
            "bindings": record.get("bindings") or {},
            "drifted": bool(active and active.version != ref.get("version")),
            "activeVersion": active.version if active else None,
            "steps": self._step_views(parent.id, index),
        }

    def _step_views(self, parent_id: str, index: dict) -> list[dict]:
        out = []
        for item in index.get(parent_id, []):
            ref = pin_of(item)
            if "step" not in ref:
                continue
            out.append({
                "step": ref["step"],
                "name": item.title,
                "itemId": item.id,
                "status": item.status.value,
                "pin": ref,
                "binding": item.assigned_agent,
                "gate": (item.verify or {}).get("kind"),
                "blockedBy": list(item.blocked_by or []),
                "children": self._step_views(item.id, index),
            })
        out.sort(key=lambda s: s["step"])
        return out

    def drifted(self, asop_id: str, queue: Queue) -> list[dict]:
        """In-flight runs pinned to a version that is no longer the active one.

        Reported, never migrated. Re-pointing running work at a newer procedure
        changes the job under whoever is doing it, and they would have no way
        to know. What to do about it is the human's call.
        """
        active = self.get(asop_id)
        if active is None:
            return []
        out = []
        for item in queue.list():
            ref = pin_of(item)
            if ref.get("asop_id") != asop_id or "step" in ref:
                continue
            if item.status in (WorkStatus.DONE, WorkStatus.FAILED):
                continue
            if ref.get("version") != active.version:
                out.append({
                    "runId": item.id,
                    "title": item.title,
                    "pinnedVersion": ref.get("version"),
                    "activeVersion": active.version,
                    "status": item.status.value,
                })
        return out

    # -- self-revision ---------------------------------------------------

    def proposals(self, asop_id: str, queue: Queue) -> dict:
        """Revision proposals accumulated against this procedure, per step.

        Computed from the runs, never stored separately: every adjudicated
        step bead pinned to this procedure is either a `good` divergence — the
        procedure was wrong, and the next version should account for it — or a
        `bad` one — the execution took a shortcut, and root-cause owns it.
        Each entry says whether a draft has already consumed it (`proposedIn`)
        and which STEP it belongs to, so a lesson lands where it was learned
        rather than on the sequence as a whole.
        """
        history = self.history(asop_id)
        if not history:
            raise SopError(f"no ASOP {asop_id!r}")
        active = self.get(asop_id)
        revisions: list[dict] = []
        root_cause: list[dict] = []
        for item in queue.list():
            meta = item.metadata or {}
            ref = pin_of(item)
            adjudication = meta.get("adjudication")
            if ref.get("asop_id") != asop_id or not isinstance(adjudication, dict):
                continue
            entry = {
                "itemId": item.id,
                "title": item.title,
                "pinnedVersion": ref.get("version"),
                "step": ref.get("step"),
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
            "asopId": asop_id,
            "activeVersion": active.version if active else None,
            "latestVersion": latest.version,
            "latestStatus": latest.status.value,
            "openProposals": list(latest.proposals),
            "openStepProposals": {s.step: list(s.proposals) for s in latest.steps},
            "revisions": revisions,
            "rootCause": root_cause,
            "pending": sum(1 for e in revisions + root_cause if e["proposedIn"] is None),
        }

    def propose(
        self,
        asop_id: str,
        queue: Queue,
        *,
        author: Optional[str] = None,
        author_kind: Optional[str] = None,
    ) -> Optional[ASOP]:
        """Draft the next version from the adjudications nobody has proposed yet.

        The loop closing, deliberately and never silently: a DRAFT, authored by
        whoever ran the pass (an agent unless the operator declared otherwise),
        so the revision policy applies in full.

        `good` adjudications become entries in the STEP's `proposals`: the
        procedure was wrong here, and this is the evidence the next author
        reads. `bad` ones become the STEP's `common_mistakes` — the lesson
        channel the eval harness measures. A step that diverged more than once
        in one pass also earns a **structural** proposal on the sequence: the
        same divergence recurring at the same boundary is the evidence §6.3
        says a split or a reordering should be drafted from.

        The lesson channel is capped per step (`MAX_COMMON_MISTAKES`). When
        the pending lessons for a step would overflow it, they stay pending
        rather than being dropped: which mistake has stopped biting is a
        human's call.

        Returns None when nothing is pending, so a scheduled pass that finds
        nothing to do is a quiet run, not an error.
        """
        view = self.proposals(asop_id, queue)
        pending_good = [e for e in view["revisions"] if e["proposedIn"] is None]
        pending_bad = [e for e in view["rootCause"] if e["proposedIn"] is None]
        if not pending_good and not pending_bad:
            return None
        history = self.history(asop_id)
        latest = history[-1]

        # What a human has already dismissed. An entry a human removed is one
        # rule 3 forbids an agent to put back — and the pass is an agent.
        # Proposing it again would refuse the whole draft, every time, for
        # every adjudication that came after: one dismissed text would jam the
        # loop forever. So a dismissed text is CONSUMED as dismissed.
        _, forbidden = policy.asop_forbidden_states(history)
        keyed = policy._steps_by_key(latest)
        key_of = {id(step): key for key, step in keyed.items()}

        def dismissed(step: Step, field_name: str, text: str) -> bool:
            return (key_of.get(id(step)), field_name, text, "present") in forbidden

        steps = [step_payload(s) for s in latest.steps]
        by_position = {s.step: (i, s) for i, s in enumerate(latest.steps)}
        consumed: list[dict] = []
        dismissed_entries: list[dict] = []
        present_entries: list[dict] = []
        deferred: list[dict] = []
        structural: list[str] = []
        changed = False

        for entry in pending_bad:
            found = by_position.get(entry["step"])
            if found is None:
                # A lesson from a step the current version no longer has. It
                # stays pending rather than being written onto whatever now
                # sits at that position — the numbers are positions, not
                # identities, and guessing here would attach evidence from one
                # step to another one's record.
                continue
            index, step = found
            lesson = lesson_text(entry["itemId"], entry["by"], entry["evidence"])
            lessons = list(steps[index].get("common_mistakes") or [])
            if dismissed(step, "common_mistakes", lesson):
                dismissed_entries.append(entry)
            elif lesson in lessons:
                present_entries.append(entry)
            elif len(lessons) >= MAX_COMMON_MISTAKES:
                deferred.append(entry)
            else:
                lessons.append(lesson)
                steps[index]["common_mistakes"] = lessons
                consumed.append(entry)
                changed = True

        good_per_step: dict[int, int] = {}
        for entry in pending_good:
            found = by_position.get(entry["step"])
            if found is None:
                continue
            index, step = found
            proposal = (
                f"{entry['evidence']} (adjudicated good on {entry['itemId']} by "
                f"{entry['by']}; v{entry['pinnedVersion']} was wrong here)"
            )
            if dismissed(step, "proposals", proposal):
                dismissed_entries.append(entry)
                continue
            proposals = list(steps[index].get("proposals") or [])
            if proposal not in proposals:
                proposals.append(proposal)
                steps[index]["proposals"] = proposals
                changed = True
            consumed.append(entry)
            good_per_step[entry["step"]] = good_per_step.get(entry["step"], 0) + 1

        for number, count in sorted(good_per_step.items()):
            if count < 2:
                continue
            index, step = by_position[number]
            note = (
                f"step {number} ({step.name!r}) diverged {count} times in one pass; "
                f"the same boundary recurring is evidence for a structural change — "
                f"splitting it, reordering it, or moving its check"
            )
            if note not in (latest.proposals or []) and note not in structural:
                structural.append(note)
                changed = True

        if not changed:
            if deferred and not dismissed_entries:
                # Nothing could move and the ONLY reason is the cap. Loud, and
                # names the human decision — which mistake has stopped biting
                # is not the pass's call.
                raise SopError(
                    f"cannot draft {asop_id!r}: {len(deferred)} pending lesson(s) and "
                    f"the steps they belong to already hold {MAX_COMMON_MISTAKES} "
                    f"common_mistakes each, the cap. Which mistake has stopped biting "
                    f"is a human's call. Prune the list in a human revision, then run "
                    f"the pass again."
                )
            draft = None
        else:
            body = {"steps": steps}
            if structural:
                body["proposals"] = [*(latest.proposals or []), *structural]
            draft = self.revise(asop_id, author=author, author_kind=author_kind, **body)

        marker = draft.version if draft is not None else latest.version
        for entry, note in (
            *((e, None) for e in consumed),
            *((e, "dismissed_by_human") for e in dismissed_entries),
            *((e, "already_present") for e in present_entries),
        ):
            item = queue.get(entry["itemId"])
            if item is None:
                continue
            record = dict((item.metadata or {}).get("adjudication") or {})
            record[PROPOSED_KEY] = marker
            if note:
                record[note] = True
            queue.annotate(item.id, {"adjudication": record}, by_plane=True)
        # Deferred lessons stay PENDING — not consumed, not dropped — and show
        # in `proposals()` until a human makes room.
        return draft

    def lesson_provenance(self, asop_id: str, queue: Queue,
                          version: Optional[int] = None) -> dict:
        """Per step of one version: which lessons the loop wrote, which a hand did.

        A lesson is **loop-fed** when it is the exact entry a `bad`
        adjudication on a step bead of this procedure became through
        `propose()` — matched on the adjudication record (verdict, item,
        adjudicator, evidence, consumed by a draft at or before this version),
        not on the wording alone, so a person typing the same sentence is
        still a hand.
        """
        asop = self.get(asop_id, version) if version is not None else self.get(asop_id)
        if asop is None:
            history = self.history(asop_id)
            if not history:
                raise SopError(f"no ASOP {asop_id!r}")
            if version is not None:
                raise SopError(f"no ASOP {asop_id!r} version {version}")
            asop = history[-1]
        loop_entries: dict[tuple, dict] = {}
        for item in queue.list():
            meta = item.metadata or {}
            ref = pin_of(item)
            adjudication = meta.get("adjudication")
            if ref.get("asop_id") != asop_id or not isinstance(adjudication, dict):
                continue
            consumed = adjudication.get(PROPOSED_KEY)
            if adjudication.get("verdict") != "bad" or consumed is None or consumed > asop.version:
                continue
            if adjudication.get("already_present"):
                # The pass found this lesson already typed. It did not write it.
                continue
            text = lesson_text(item.id, adjudication.get("by"), adjudication.get("evidence"))
            loop_entries[(ref.get("step"), text)] = {
                "itemId": item.id, "by": adjudication.get("by"), "proposedIn": consumed,
            }
        steps = []
        loop_any = hand_any = False
        for step in asop.steps:
            loop, hand = [], []
            for entry in step.common_mistakes:
                found = loop_entries.get((step.step, entry))
                if found:
                    loop.append({"lesson": entry, **found})
                else:
                    hand.append(entry)
            loop_any = loop_any or bool(loop)
            hand_any = hand_any or bool(hand)
            steps.append({"step": step.step, "name": step.name, "loop": loop, "hand": hand})
        return {
            "asopId": asop_id,
            "version": asop.version,
            "steps": steps,
            "loopFed": loop_any,
            "handFed": hand_any,
        }

    # -- evaluation ------------------------------------------------------

    def outcomes_by_version(self, asop_id: str, queue: Queue) -> list[dict]:
        """Per version: how many runs, how they ended, and the same per step.

        **Counts, not a success rate.** A bare percentage is gameable in both
        directions — a procedure applied to progressively harder cases looks
        like it is degrading, and failures re-filed as fresh runs look like
        improvement. Raw counts with an explicit in-flight column let a reader
        see the denominator they are dividing by.

        `successRate` is present but is `None` until at least one run has
        finished, and never treats in-flight work as either outcome.

        **The two verify states get their own columns rather than a share of
        an outcome.** A gated step that reported completion is not a success,
        not a failure, and not the same thing as one nobody has started — and
        `verifyFailed` in particular is the single most informative number
        here, because it says the work claimed the step's own definition of
        done and the gate disagreed.

        The per-step rows are what v3 adds, and they are the reason the grain
        changed: "did the rewrite of step 2 help?" is a comparison of two
        step rows, and under v2 there was no such row to compare.
        """
        by_version: dict[int, dict] = {}
        for asop in self.history(asop_id):
            by_version[asop.version] = {
                "version": asop.version,
                "status": asop.status.value,
                "runs": 0,
                "done": 0,
                "failed": 0,
                "awaitingVerify": 0,
                "verifyFailed": 0,
                "inFlight": 0,
                "successRate": None,
                "steps": [
                    {
                        "step": s.step,
                        "name": s.name,
                        "instances": 0,
                        "done": 0,
                        "failed": 0,
                        "awaitingVerify": 0,
                        "verifyFailed": 0,
                        "inFlight": 0,
                        "successRate": None,
                    }
                    for s in asop.steps
                ],
            }

        def tally(row: dict, item: WorkItem, count_key: str) -> None:
            row[count_key] += 1
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

        for item in queue.list():
            ref = pin_of(item)
            if ref.get("asop_id") != asop_id:
                continue
            row = by_version.get(ref.get("version"))
            if row is None:
                continue
            step = ref.get("step")
            if step is None:
                tally(row, item, "runs")
            else:
                target = next((s for s in row["steps"] if s["step"] == step), None)
                if target is not None:
                    tally(target, item, "instances")

        for row in by_version.values():
            for scope, total in ((row, row["runs"]), *((s, s["instances"]) for s in row["steps"])):
                finished = scope["done"] + scope["failed"]
                if finished:
                    scope["successRate"] = round(scope["done"] / finished, 3)
                # A rate computed over a handful of settled runs while a dozen
                # sit behind open gates is technically true and reads as the
                # whole picture. Say how much is still unresolved, in the same
                # row, rather than leaving the reader to add three columns.
                scope["unresolved"] = (
                    scope["awaitingVerify"] + scope["verifyFailed"] + scope["inFlight"]
                )
                del total
        return [by_version[v] for v in sorted(by_version)]

    # -- promotion -------------------------------------------------------

    def promote(
        self,
        run_id: str,
        queue: Queue,
        *,
        task_type: Optional[str] = None,
        title: Optional[str] = None,
        author: Optional[str] = None,
        author_kind: Optional[str] = None,
    ) -> ASOP:
        """Draft an ASOP from a completed run tree (ASOP.md §6.5).

        The front door of the loop: the standard grows from evidence that a
        shape worked, not from a blank page. A planner's ad-hoc decomposition
        that completed — with its gates and its adjudications — becomes a
        draft whose steps are its beads, whose roles are its executors, and
        whose ordering is its `blocked_by`.

        **Human-only in v3.** An agent may draft a revision of a procedure
        people already follow; opening a NEW one is a different act, and the
        contract reserves it. A later version lets agents promote to a draft
        and lets a harness auto-promote past a threshold it configures;
        activation stays human either way.

        **Refused when an active ASOP already covers the run's `task_type`.**
        The path for a variant is a new version through `revise`, not a second
        procedure for the same kind of work — two active procedures for one
        task type means outcomes counted in two places and compared in
        neither.

        Roles are named `role_1..role_n`, one per distinct executor, in first
        appearance order. The mapping to actual executors is deliberately not
        stored: a binding is local (§7), and writing one into the artefact is
        exactly what makes a procedure unshareable.
        """
        policy.require_human(
            author_kind or policy.AGENT,
            "promote",
            because=(
                "Promotion opens a new standard for a whole type of task, from one "
                "run that happened to work. An agent may draft a revision; a person "
                "decides that a shape is a procedure."
            ),
        )
        parent = queue.get(run_id)
        if parent is None:
            from agentco.work import unknown_item

            raise unknown_item(run_id, "promote")
        index = self._children_index(queue)
        children = sorted(
            (c for c in index.get(run_id, []) if not (c.metadata or {}).get("repairs")),
            key=lambda c: c.created_at or "",
        )
        if not children:
            raise SopError(
                f"{run_id} has no children, so there is no shape to promote. An ASOP "
                f"is a sequence of steps; a single work item that went well is not one."
            )
        unfinished = [c.id for c in children if c.status != WorkStatus.DONE]
        if parent.status != WorkStatus.DONE or unfinished:
            raise SopError(
                f"{run_id} is not a completed run (parent is {parent.status.value}"
                f"{f'; open children {unfinished}' if unfinished else ''}). Promotion "
                f"drafts a standard from evidence that a shape WORKED — a tree that "
                f"has not finished is not that evidence yet."
            )

        kind = task_type or (parent.metadata or {}).get("task_type")
        if not isinstance(kind, str) or not kind.strip():
            raise SopError(
                f"promote({run_id}) needs a task_type — the type of task the new "
                f"procedure standardises. It is what a later promotion is checked "
                f"against, and what outcomes are compared within."
            )
        kind = kind.strip()
        covering = [a for a in self.list_active() if (a.task_type or "") == kind]
        if covering:
            raise SopError(
                f"ASOP {covering[0].asop_id!r} v{covering[0].version} is already active "
                f"for task_type {kind!r}. Two active procedures for one type of task "
                f"means outcomes counted in two places and compared in neither — the "
                f"path for a variant is a new VERSION through `revise`."
            )

        verifier = (author or LEGACY_VERIFIER).strip() or LEGACY_VERIFIER
        roles: dict[str, dict] = {}
        role_of_executor: dict[str, str] = {}
        by_id = {c.id: c for c in children}
        position = {c.id: i + 1 for i, c in enumerate(children)}
        steps: list[dict] = []
        for child in children:
            from agentco.work import executors_of

            executors = executors_of(child) or [child.assigned_agent or ""]
            actor = next((e for e in executors if e), "")
            role = role_of_executor.get(actor)
            if role is None:
                role = f"role_{len(roles) + 1}"
                role_of_executor[actor] = role
                roles[role] = {"kind": policy.AGENT}
            gate = gate_payload(child.verify) or {
                # A bead with no gate of its own cannot become a step with no
                # gate — a step without one is advice. It fails closed to a
                # human gate for the same reason `upgrade_legacy` does, and a
                # human is already reading this draft before it can activate.
                "kind": "human",
                "check": f"a person confirms {child.title!r} was carried out",
                "verifier": verifier,
                "max_park_seconds": LEGACY_PARK_SECONDS,
                "on_timeout": "escalate",
                "escalate_to": verifier,
            }
            after = sorted(
                position[b] for b in (child.blocked_by or [])
                if b in by_id and position[b] < position[child.id]
            )
            steps.append({
                "name": child.title,
                "role": role,
                "purpose": f"as executed in {child.id}",
                **({"definition_of_done": child.result} if child.result else {}),
                "gate": gate,
                "after": after,
            })
        return self.create(
            title or parent.title,
            task_type=kind,
            purpose=f"promoted from run {run_id}",
            roles=roles,
            steps=steps,
            author=author,
            author_kind=author_kind,
        )
