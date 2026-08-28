"""The work queue and its lease protocol.

This is what lets more than one harness pull from the same queue without two
of them doing the same job — a compare-and-set claim plus a **fencing token**.
Both halves are load-bearing, and each closes a failure that actually happened
rather than one that was imagined.

**The CAS closes the double-claim.** Before it, claiming was a bare status
write with no owner check and no expiry: two claimants both "succeeded", both
executed, and the second completion silently overwrote the first. On a single
machine that stays latent, because there is only ever one claimant. The moment
a second machine pulls the same store it becomes the default outcome, not the
race condition.

**The fence closes the late report.** A worker that lost its lease — slept,
hit a network partition, was reaped as stuck — may still be running and may
still come back with an answer. By then the item may have been handed to
someone else and finished. Accepting that late report would overwrite a real
result with one derived from an execution the queue already abandoned. So
every report is fenced on the attempt number it was issued under, and a
mismatch **raises** and writes nothing.

Those two are different failures with different fixes, which is why there are
two mechanisms and not one.

**Storage is append-structured JSONL under an advisory lock.** Not because
JSONL is elegant, but because it is greppable when something goes wrong at
02:00, it diffs in review, and a corrupt line can be quarantined instead of
taking the store with it. A database is the right answer at a scale this is
not designed for.

WHAT IS DELIBERATELY NOT HERE: approval workflows, verification gates,
hierarchical goals, retry policy, notification. Those are opinions about how an
organisation works, and they belong above this layer. What is here is the
narrow thing everything else needs — hand a unit of work to exactly one worker,
and be certain whose answer you are looking at.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence

from agentco.keys import derive_natural_key, natural_key_of

DEFAULT_LEASE_TTL_S = 3600


class WorkError(Exception):
    """Base for every refusal this module makes."""


class LeaseError(WorkError):
    """A claim lost, or a report arrived against a lease that is no longer current.

    Two distinct situations share this type because callers treat them the
    same way — stop, do not write, do not retry blindly:

    1. **Contention.** Someone else holds the lease. `claim()` reports this by
       returning `None` rather than raising, because for a polling worker it is
       a normal answer and not an error.
    2. **Stale fence.** `report_result()` was handed an attempt number that is
       no longer current. This always raises. A caller must never be able to
       mistake "your work was superseded" for "your work was recorded".
    """


class BlockedError(WorkError):
    """The item's dependencies are not done — a caller error, not contention.

    Raised rather than returning `None`, because `None` means "someone else got
    there first, try again in a moment" and retrying a blocked item cannot help
    until something unrelated finishes. A poller should never see this: `ready()`
    filters blocked items out, so reaching here means the id came from somewhere
    that does not know about dependencies, and that is worth telling the caller
    loudly rather than absorbing into a quiet miss.

    Distinct from `CapabilityError` because the two have different answers:
    a capability mismatch means "not on this worker, ever"; this means "not yet,
    on any worker".
    """


class CapabilityError(WorkError):
    """This worker can never run this item — a misroute, not contention.

    Raised rather than returning `None` on purpose. A worker that cannot ever
    satisfy an item's requirements is in a permanently different situation from
    one that merely lost a race, and filing the first as the second produces a
    queue that looks busy while making no progress. Retrying cannot help, so
    the caller must be told loudly enough to stop.
    """


class DuplicateSuppressed(Exception):
    """Internal signal that `create()` found an existing item with the same key.

    Never escapes: `create()` catches it and returns the EXISTING item. Callers
    wanted that behaviour and were each hand-rolling it before there was one
    rule; raising at them would break every one of those call sites for no gain.
    """


class WorkStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    # DERIVED, never stored. See `WorkItem.unmet_blockers`: an item's
    # dependency list is the fact, and a status field copying it is a cache
    # that nothing invalidates. Storing it stranded every dependent item ever
    # filed — `create()` wrote BLOCKED and no code anywhere wrote it back.
    # Reported by `blocked_items()`; never written to disk.
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"


TERMINAL = (WorkStatus.DONE, WorkStatus.FAILED)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class WorkItem:
    """One unit of work, and the lease state that says who holds it.

    `lease_attempt` is **the fence**: monotonic per item, bumped on every claim,
    and never reset — not on release, not on failure, not on completion. It is
    the record of how many times this item has been handed out, and a report is
    only accepted if it names the current value.
    """

    id: str
    title: str
    status: WorkStatus = WorkStatus.PENDING
    assigned_agent: Optional[str] = None

    # What a worker must declare to be allowed to claim this.
    requires: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)

    leased_by: Optional[str] = None
    lease_attempt: int = 0
    lease_expires_at: Optional[str] = None

    result: Optional[str] = None
    natural_key: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    created_at: str = field(default_factory=lambda: _iso(_now()))
    updated_at: str = field(default_factory=lambda: _iso(_now()))

    def lease_active_at(self, now: datetime) -> bool:
        """True iff a live lease is held. Expiry is evaluated here, not swept.

        Evaluating in the read means an expired lease stops blocking a claim the
        moment it lapses, even if no reaper has run. A sweeper that fell over
        would otherwise leave phantom leases holding work nobody is doing —
        which is the shape of an outage that looks like an empty queue.
        """
        if not self.leased_by:
            return False
        expires = _parse(self.lease_expires_at)
        return expires is not None and expires > now

    def unmet_blockers(self, done_ids: set[str]) -> list[str]:
        """Dependencies not yet done. Empty means nothing is holding this back.

        This is the single source of truth for blockedness. `WorkStatus.BLOCKED`
        exists for REPORTING and is computed from this — it is never written to
        disk, because a stored copy of a derived fact is a cache, and this one
        had no invalidation path at all.
        """
        return [dep for dep in self.blocked_by if dep not in done_ids]

    def to_json(self) -> str:
        payload = asdict(self)
        payload["status"] = self.status.value
        return json.dumps(payload, sort_keys=True)

    @classmethod
    def from_json(cls, line: str) -> "WorkItem":
        raw = json.loads(line)
        known = {f.name for f in fields(cls)}
        # Unknown columns are DROPPED on read but preserved on disk — see
        # Queue._write_all. A newer writer's field must never be deleted by an
        # older reader doing a routine round trip.
        data = {k: v for k, v in raw.items() if k in known}
        data["status"] = WorkStatus(data.get("status", "pending"))
        return cls(**data)


class Queue:
    """A JSONL work store with an advisory lock around every mutation."""

    def __init__(self, path: Path | str = "work.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Raw BYTES, not str — a line that failed to decode has no faithful
        # string form. Read by a health check, and carried through every
        # write so a quarantined line is preserved rather than deleted.
        self.quarantined: list[bytes] = []

    # -- storage ---------------------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Exclusive advisory lock, held for read-modify-write as one unit.

        The lock file is separate from the data file so that the atomic
        `os.replace` below cannot swap the inode out from under a held lock.
        """
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _read_raw(self) -> tuple[list[dict], list[bytes]]:
        """`(parseable rows, quarantined raw lines)`. One bad line costs one row.

        **Read BYTES and decode per line.** Decoding the whole file at once puts
        `UnicodeDecodeError` outside the per-line handler — and it is a
        `ValueError`, not a `JSONDecodeError`, so it escapes and every single
        read of the store fails. One stray byte from a truncated write or a
        mis-encoded external tool then costs the entire queue, which is the
        opposite of what quarantining is for.

        The same reasoning covers the other two ways a line can be unusable: a
        newer writer's unknown status value (`ValueError` out of the enum) and a
        row missing a required field (`TypeError` out of the constructor). All
        three are caught here, at the line, because the boundary has to be the
        line or it is not a boundary.

        Quarantined lines are returned as **raw bytes** rather than decoded
        text: a line that failed to decode has no faithful string form, and
        re-encoding a guess at it is how the original bytes get lost on the
        next write.
        """
        if not self.path.exists():
            return [], []
        rows: list[dict] = []
        quarantined: list[bytes] = []
        for raw_line in self.path.read_bytes().split(b"\n"):
            if not raw_line.strip():
                continue
            try:
                parsed = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                quarantined.append(raw_line)
                continue
            if isinstance(parsed, dict) and "id" in parsed:
                rows.append(parsed)
            else:
                quarantined.append(raw_line)
        self.quarantined = quarantined
        return rows, quarantined

    def _read_all(self) -> list[WorkItem]:
        """Rows this version can model. A row it cannot is quarantined, not fatal.

        `WorkItem.from_json` raises `ValueError` on a status this version does
        not know and `TypeError` on a row missing a required field — both of
        which a NEWER writer can legitimately produce. Letting either escape
        would mean a forward-compatible store bricks an older reader, so they
        land in the same quarantine as a corrupt line.
        """
        rows, quarantined = self._read_raw()
        items: list[WorkItem] = []
        for row in rows:
            try:
                items.append(WorkItem.from_json(json.dumps(row)))
            except (ValueError, TypeError):
                quarantined.append(json.dumps(row).encode("utf-8"))
        self.quarantined = quarantined
        return items

    def _write_all(self, rows: list[dict], quarantined: Sequence[bytes] = ()) -> None:
        """Atomic whole-file replace, CARRYING QUARANTINED LINES THROUGH VERBATIM.

        Same directory so the rename stays on one filesystem and is therefore
        atomic; fsync before the rename so a crash cannot leave a truncated file
        where a complete one used to be.

        The `quarantined` argument is the half that was missing, and its absence
        turned "quarantine" into deletion: `_read_raw` withheld the bad lines
        from the row list, and the next ordinary write — a claim, a create,
        anything — then persisted only the rows and erased them from disk. The
        docstring promised a corrupt line "must not vanish either" while the
        code removed it on the next mutation.

        They are written back as the exact bytes that were read, appended after
        the good rows. Not re-encoded, because a line that failed to decode has
        no faithful string form; and not interleaved at their original offsets,
        because a byte sequence that is not a line cannot be given a position
        among lines. Preserving the bytes is the promise; preserving the order
        was never one.
        """
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                for row in rows:
                    handle.write(json.dumps(row, sort_keys=True).encode("utf-8") + b"\n")
                for line in quarantined:
                    handle.write(line + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _merge(self, raw_rows: list[dict], item: WorkItem) -> list[dict]:
        """Write `item` back over its raw row, PRESERVING unknown columns.

        The round trip through the dataclass drops any field this version does
        not know about. Merging over the raw dict instead means a column added
        by a newer writer survives an older reader's update — the alternative is
        silent data loss that only shows up as "the field I added keeps
        disappearing".
        """
        updated = json.loads(item.to_json())
        out = []
        for row in raw_rows:
            if row.get("id") == item.id:
                merged = dict(row)
                merged.update(updated)
                out.append(merged)
            else:
                out.append(row)
        return out

    # -- creation --------------------------------------------------------

    def create(
        self,
        title: str,
        *,
        requires: Sequence[str] = (),
        blocked_by: Sequence[str] = (),
        assigned_agent: Optional[str] = None,
        natural_key: Optional[str] = None,
        source: Optional[str] = None,
        source_id: Optional[str] = None,
        kind: Optional[str] = None,
        subject: Optional[str] = None,
        period: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> WorkItem:
        """Create one item. A duplicate natural key is a LOUD no-op.

        Returns the EXISTING item when the key is already present, and says so
        on stderr naming the key, the suppressed title and the holder's id.
        Silence was never an option: a suppressed duplicate nobody announces is
        indistinguishable from a create that worked, and the caller goes on to
        reference an id that is not theirs.

        Returning rather than raising is deliberate — every ingest path already
        wanted exactly this and hand-rolled it before there was one rule.
        """
        key = derive_natural_key(
            explicit=natural_key,
            source=source,
            source_id=source_id,
            kind=kind,
            subject=subject,
            period=period,
        )
        item = WorkItem(
            id=f"w-{uuid.uuid4().hex[:8]}",
            title=title,
            requires=list(requires),
            blocked_by=list(blocked_by),
            assigned_agent=assigned_agent,
            natural_key=key,
            metadata=dict(metadata or {}),
        )

        with self._locked():
            raw_rows, quarantined = self._read_raw()
            if key:
                for row in raw_rows:
                    if natural_key_of(row) == key:
                        existing = WorkItem.from_json(json.dumps(row))
                        print(
                            f"[work] DUPLICATE-SUPPRESSED key={key!r} "
                            f"title={title!r} held-by={existing.id}",
                            file=sys.stderr,
                        )
                        existing.metadata = dict(existing.metadata or {})
                        existing.metadata["natural_key_conflict"] = True
                        return existing
            raw_rows.append(json.loads(item.to_json()))
            self._write_all(raw_rows, quarantined)
        return item

    # -- reading ---------------------------------------------------------

    def get(self, item_id: str) -> Optional[WorkItem]:
        for item in self._read_all():
            if item.id == item_id:
                return item
        return None

    def list(self, status: Optional[WorkStatus] = None) -> list[WorkItem]:
        items = self._read_all()
        return [i for i in items if status is None or i.status == status] if items else []

    def ready(
        self,
        agent: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> list[WorkItem]:
        """Claimable right now: pending, unblocked, and not under a live lease.

        Capability is deliberately **not** filtered here. Visibility is not the
        gate — the enforcement lives in `claim()`, inside the lock. A queue that
        hides work a node cannot run is a queue where nobody notices a lane has
        stopped, because the work it was doing is no longer visible to anyone.
        """
        at = now or _now()
        items = self._read_all()
        done = {i.id for i in items if i.status == WorkStatus.DONE}
        out = []
        for item in items:
            # PENDING, or IN_PROGRESS whose lease has lapsed — the same set
            # `claim()` will accept. These two must agree: an item that is
            # claimable but never listed is work no poller ever offers to do,
            # and it strands silently because nothing reports an empty queue as
            # a problem.
            if item.status not in (WorkStatus.PENDING, WorkStatus.IN_PROGRESS):
                continue
            if item.status == WorkStatus.IN_PROGRESS and item.lease_active_at(at):
                continue
            if item.unmet_blockers(done):
                continue
            if item.lease_active_at(at):
                continue
            if agent and item.assigned_agent and item.assigned_agent != agent:
                continue
            out.append(item)
        return out

    # -- the lease protocol ----------------------------------------------

    def _mutate(self, item_id: str, change: Callable[[WorkItem], dict]) -> Optional[WorkItem]:
        """Read-modify-write under one lock. `change` may raise to abort.

        A raising `change` leaves the store byte-identical, which is what makes
        the CAS and the fence safe to express as ordinary exceptions.
        """
        with self._locked():
            raw_rows, quarantined = self._read_raw()
            target = None
            for row in raw_rows:
                if row.get("id") == item_id:
                    target = WorkItem.from_json(json.dumps(row))
                    break
            if target is None:
                return None
            updates = change(target)  # may raise; nothing written yet
            for key, value in updates.items():
                setattr(target, key, value)
            target.updated_at = _iso(_now())
            self._write_all(self._merge(raw_rows, target), quarantined)
            return target

    def claim(
        self,
        item_id: str,
        agent: str,
        ttl_seconds: int = DEFAULT_LEASE_TTL_S,
        capabilities: Optional[Sequence[str]] = None,
        now: Optional[datetime] = None,
    ) -> Optional[WorkItem]:
        """Compare-and-set claim. Returns the leased item, or `None` if it lost.

        Succeeds only if, at the instant the lock is held, the item is PENDING
        and free. On success the lease is taken: `leased_by` set,
        `lease_attempt` bumped, `lease_expires_at` = now + ttl.

        `None` is not an error — it is the answer a polling worker is asking
        for. The reason still goes to stderr, because a claimant that keeps
        losing is the first symptom of two workers wrongly sharing a lane, and
        that must not be invisible.

        **The capability gate fails closed.** `capabilities=None` means "this
        worker declares nothing", identical to `[]` — it does not mean "skip
        the check". Any other reading would make the safe default the insecure
        one, and every not-yet-updated caller a hole. Items with no `requires`
        are unaffected: nothing is required, so nothing can be missing.
        """
        at = now or _now()
        held = frozenset(capabilities or ())
        done_ids = {i.id for i in self._read_all() if i.status == WorkStatus.DONE}

        def cas(item: WorkItem) -> dict:
            # Capability BEFORE the CAS checks, on purpose: given a permanent
            # problem and a transient one, report the permanent one. "This
            # worker can never run this" is more actionable than "someone else
            # holds it right now", which may also be true.
            missing = [r for r in item.requires if r not in held]
            if missing:
                raise CapabilityError(
                    f"cannot claim {item_id} for {agent!r}: it requires "
                    f"{', '.join(item.requires)} but this worker declares "
                    f"{', '.join(sorted(held)) or '(none)'} — missing "
                    f"{', '.join(missing)}. Run it on a worker that declares "
                    f"those capabilities, or fix the item's requires. "
                    f"Retrying here cannot help."
                )
            # WHO IT IS FOR is not WHO IS DOING IT. `assigned_agent` is a
            # routing decision made when the item was filed; `leased_by` is who
            # holds it right now. `claim()` checked neither and then overwrote
            # the first with the claimant — so a targeted assignment could be
            # taken by anyone, and the record of who it was meant for was
            # destroyed in the same write that took it. Afterwards nothing
            # anywhere could answer "who was this for?", which makes a misroute
            # undetectable rather than merely possible.
            if item.assigned_agent and item.assigned_agent != agent:
                raise BlockedError(
                    f"cannot claim {item_id} for {agent!r}: it is assigned to "
                    f"{item.assigned_agent!r}. `ready()` does not offer it to you, "
                    f"so this id came from somewhere that does not know about "
                    f"assignment. Retrying cannot help — claim it as "
                    f"{item.assigned_agent!r}, or re-file it unassigned."
                )
            unmet = item.unmet_blockers(done_ids)
            if unmet:
                # `ready()` filtered these out and `claim()` did not look at all,
                # so a caller holding an id from anywhere else could start work
                # whose prerequisites had not happened. The two must agree, or
                # "ready" is advice rather than a contract.
                raise BlockedError(
                    f"cannot claim {item_id} for {agent!r}: it is blocked by "
                    f"{', '.join(unmet)}, which {'is' if len(unmet) == 1 else 'are'} "
                    f"not done. Finish those first — this is not contention and "
                    f"retrying now cannot help."
                )
            if item.lease_active_at(at):
                raise LeaseError(
                    f"cannot claim {item_id} for {agent!r}: held by "
                    f"{item.leased_by!r} until {item.lease_expires_at} "
                    f"(attempt {item.lease_attempt})"
                )
            # PENDING, or IN_PROGRESS under a lease that has lapsed. The second
            # case is what makes expiry SELF-HEALING: an abandoned item becomes
            # claimable the moment its lease dies, whether or not a reaper has
            # run. Requiring the reaper would make it load-bearing — and a
            # queue whose recovery depends on a cron job that quietly stopped
            # is a queue that stalls with no error anywhere. `reap_expired_leases`
            # is still worth running, for the visible state transition; it is an
            # optimisation, not a correctness requirement.
            claimable = (WorkStatus.PENDING, WorkStatus.IN_PROGRESS)
            if item.status not in claimable:
                raise LeaseError(
                    f"cannot claim {item_id} for {agent!r}: status is "
                    f"{item.status.value}, which is terminal"
                )
            return {
                "status": WorkStatus.IN_PROGRESS,
                # `assigned_agent` is deliberately NOT written here. Taking work
                # is not the same act as being assigned it, and overwriting the
                # routing decision with the claimant erased the only record that
                # could show a misroute after the fact.
                "leased_by": agent,
                "lease_attempt": item.lease_attempt + 1,
                "lease_expires_at": _iso(at + timedelta(seconds=ttl_seconds)),
            }

        try:
            return self._mutate(item_id, cas)
        except BlockedError as exc:
            print(f"[work] claim REFUSED (blocked): {exc}", file=sys.stderr)
            raise
        except CapabilityError as exc:
            # Louder than a lost race, and it propagates. The check raised
            # before anything was written, so the store is byte-identical.
            print(f"[work] claim REFUSED (capability): {exc}", file=sys.stderr)
            raise
        except LeaseError as exc:
            print(f"[work] claim refused: {exc}", file=sys.stderr)
            return None

    def report_result(
        self,
        item_id: str,
        attempt: int,
        status: WorkStatus,
        result: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Optional[WorkItem]:
        """Apply a worker's outcome, fenced on `attempt`.

        A mismatch raises `LeaseError` and writes nothing, so the caller gets a
        non-zero exit rather than a silent no-op. The message says the work was
        *superseded*, not lost, because those prompt different human responses.

        `idempotency_key` makes an honest retry safe. Any lossy transport lets a
        worker apply a result and lose the acknowledgement, and it must be able
        to send the same result again. A repeat of a recorded key returns the
        stored item unchanged.

        The lease is released either way — `leased_by` and `lease_expires_at`
        cleared — while `lease_attempt` is **kept**. The count is the history of
        how many times this item was handed out, and nothing should erase it.
        """
        if status not in TERMINAL:
            raise ValueError(
                f"report_result applies terminal outcomes only (done/failed), "
                f"got {status.value}"
            )

        current = self.get(item_id)
        if current is None:
            return None
        if idempotency_key:
            prior = (current.metadata or {}).get("lease_report") or {}
            if prior.get("idempotency_key") == idempotency_key:
                return current

        def fence(item: WorkItem) -> dict:
            if item.lease_attempt != attempt:
                raise LeaseError(
                    f"refusing result for {item_id}: reported against lease "
                    f"attempt {attempt}, but the item is on attempt "
                    f"{item.lease_attempt} (holder {item.leased_by!r}). The "
                    f"lease this result came from is no longer current — the "
                    f"work was superseded, not lost."
                )
            if item.status in TERMINAL:
                raise LeaseError(
                    f"refusing result for {item_id}: it is already "
                    f"{item.status.value}. Reporting over a finished item would "
                    f"replace a recorded outcome with a later opinion of it."
                )
            metadata = dict(item.metadata or {})
            metadata["lease_report"] = {
                "attempt": attempt,
                "reported_by": item.leased_by,
                "reported_at": _iso(_now()),
                "status": status.value,
                "idempotency_key": idempotency_key,
            }
            return {
                "status": status,
                "result": result,
                "metadata": metadata,
                "leased_by": None,
                "lease_expires_at": None,
                # THE ATTEMPT ENDS WITH THE LEASE. Without this the reporter
                # still holds a number the fence accepts, so it can report
                # again — and did: a second call at the same attempt overwrote
                # a real result with a later one. Bumping here is not a reset;
                # the counter still only ever climbs.
                "lease_attempt": item.lease_attempt + 1,
            }

        return self._mutate(item_id, fence)

    def reap_expired_leases(self, now: Optional[datetime] = None) -> list[WorkItem]:
        """Return in-progress items whose lease has expired to the ready set.

        **Expiry does not fail the item.** A failure is a claim about the work;
        an expired lease is a claim about the *worker*, and conflating them
        burns a retry and files an incident about work that was never even
        attempted. The item goes back to PENDING and its attempt is ADVANCED —
        revoking the lease revokes the number with it, which is what fences out
        a late report from the holder just reaped. An earlier version of this
        docstring claimed the counter was left intact and did the fencing; it
        was left intact and it fenced nothing, because only a subsequent claim
        advanced it.
        """
        at = now or _now()
        reaped: list[WorkItem] = []
        for item in self.list(WorkStatus.IN_PROGRESS):
            if item.lease_active_at(at) or not item.leased_by:
                continue

            def release(target: WorkItem) -> dict:
                # RE-CHECK INSIDE THE LOCK. The candidate list above was read
                # outside it, so between the snapshot and this write another
                # worker may have legitimately claimed the item. Writing
                # unconditionally revoked a LIVE lease: that worker's completed
                # work was discarded and the item ran twice.
                if target.lease_active_at(at):
                    raise LeaseError("lease went live between the scan and the write")
                if target.status != WorkStatus.IN_PROGRESS:
                    raise LeaseError("no longer in progress")
                return {
                    "status": WorkStatus.PENDING,
                    "leased_by": None,
                    "lease_expires_at": None,
                    # Same rule as a terminal report: revoking the lease must
                    # revoke the attempt. The docstring claimed the counter
                    # "is what makes the next report from the old holder get
                    # fenced out" — it was not, because only a SUBSEQUENT claim
                    # bumped it, so a reaped worker's late report was accepted
                    # by an item nobody held.
                    "lease_attempt": target.lease_attempt + 1,
                }

            try:
                updated = self._mutate(item.id, release)
            except LeaseError:
                # Lost the race, which is a normal answer for a sweeper: the
                # item is someone else's now and nothing needs doing.
                continue
            if updated:
                reaped.append(updated)
        return reaped
