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

**Verification gates ARE here now, and that is a reversal.** This docstring
used to name them as deliberately excluded, alongside approval workflows and
retry policy, on the grounds that they are opinions about how an organisation
works. The exclusion was right about workflow and wrong about the gate: the
ASOP contract's `verified` property is not an opinion about process, it is the
answer to "may the next unit start", and that answer has to live where
blockedness is derived. Put it above this layer and every consumer computes its
own version of done — which is the momentarily-done race, arrived at by
architecture instead of by bug. See `agentco/gates.py` and
`docs/connection-harness.md`.

What is still deliberately NOT here: approval workflows, hierarchical goals,
notification, and the ACTING part of retry policy. The queue records what the
policy decided (`gates.retry_decision`) and spawns nothing — creating work as a
side effect of a report is how a queue starts having opinions.

What is here is the narrow thing everything else needs — hand a unit of work to
exactly one worker, be certain whose answer you are looking at, and do not call
it finished until its gate says so.
"""

from __future__ import annotations

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
from typing import Callable, Iterable, Iterator, Optional, Sequence

from agentco import gates, policy
from agentco.errors import Refusal
from agentco.filelock import lock_exclusive, unlock
from agentco.keys import derive_natural_key, natural_key_of

DEFAULT_LEASE_TTL_S = 3600

WORK_STORE_ENV_VAR = "AGENTCO_WORK_STORE"
DEFAULT_WORK_STORE = "work.jsonl"


def resolve_work_store(path: Optional[str] = None) -> str:
    """Where the queue lives, resolved the same way by every surface.

    This is here rather than in a caller because there are now three of them —
    the MCP server, the HTTP app, and the CLI — and a queue is a file that two
    surfaces can disagree about the location of. When they do, each is
    internally consistent and the work simply never meets, which presents as an
    empty queue rather than as an error.
    """
    return path or os.environ.get(WORK_STORE_ENV_VAR) or DEFAULT_WORK_STORE


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


class DecompositionError(WorkError):
    """A child that would break the decomposition bound, or a repair filed beneath.

    The bound is a human review bound, not a law of nature (asop.md
    § Decomposition bounds): a parent holds at most `MAX_CHILDREN` children —
    six work units and the verify unit that closes them — and a tree goes at
    most `MAX_DEPTH` deep. Both are refused at create, BEFORE the store is
    touched, because the reader who pays for an oversized tree is the one
    person accountable for it, and a tree they cannot sanity-check is one
    nobody checks.
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
    # Gated completion. The worker says it is finished and the gate has not
    # answered yet — a judged or human check is routed elsewhere and has not
    # come back. STORED, unlike BLOCKED, because it is a fact about this item
    # rather than a derivation from other items.
    AWAITING_VERIFY = "awaiting_verify"
    # The gate answered no. Not FAILED: the work may well be right and the
    # check disagreed, and the two prompt different responses. What they share
    # is that neither is DONE.
    VERIFY_FAILED = "verify_failed"
    DONE = "done"
    FAILED = "failed"


TERMINAL = (WorkStatus.DONE, WorkStatus.FAILED)

# Outcomes already recorded. Reporting over any of these would replace a
# recorded outcome with a later opinion of it — and for the two verify states
# specifically, it would also let a worker walk its own item out of a failed
# gate by re-reporting success. The only way out of those two is the gate
# answering again (`Queue.attest`).
SETTLED = TERMINAL + (WorkStatus.AWAITING_VERIFY, WorkStatus.VERIFY_FAILED)

# How a gate was resolved WITHOUT a verdict — written by `resolve_by_default`
# and by nothing else. Its presence at the top level of an item's metadata is
# the statement "the last thing that settled this was a clock, not a check", so
# a real verdict arriving afterwards has to displace it. It does not delete it:
# the clock genuinely fired, that is a fact about this item, and it moves under
# HISTORY_KEY. Left in place, a `done` item carried an attestation next to a
# note reading "no check was run" — the store contradicting itself about the one
# thing it exists to be believed on.
RESOLUTION_KEY = "verify_resolution"
HISTORY_KEY = "verify_history"


def parse_terminal_status(raw: object) -> "WorkStatus":
    """The status a report names, or the one refusal every transport gives for a bad one.

    HTTP said `not_terminal`, MCP said prose, and the core raised a ValueError
    the surfaces classified as `invalid_request` — three answers to one wrong
    input. The conformance suite named it; this is the one answer.
    """
    try:
        parsed = WorkStatus(raw)
    except ValueError:
        raise Refusal(
            code="not_terminal",
            message=f"status {raw!r} is not a terminal outcome",
            remediation=(
                f"Report one of {', '.join(sorted(s.value for s in TERMINAL))}. "
                "A lease is released by reporting, not by reporting progress."
            ),
            http_status=400,
        ) from None
    if parsed not in TERMINAL:
        raise Refusal(
            code="not_terminal",
            message=f"{parsed.value} is not a terminal outcome",
            remediation=(
                f"Report one of {', '.join(sorted(s.value for s in TERMINAL))}. "
                "A lease is released by reporting, not by reporting progress."
            ),
            http_status=400,
        )
    return parsed


def unknown_item(item_id: str, doing: str) -> Refusal:
    """The one refusal for an id no store holds, worded once for every transport."""
    return Refusal(
        code="work_item_unknown",
        message=f"no work item {item_id!r} on this queue",
        remediation=(
            f"There is nothing here to {doing}. Check the id came from work_pull or "
            f"work_create against this same store."
        ),
        http_status=404,
    )


def releases_blockers(status: "WorkStatus | str") -> bool:
    """DONE, and nothing else, unblocks what depends on it.

    **This is the momentarily-done race, and this function is the whole of the
    fix.** A gated item that reported success is `AWAITING_VERIFY`: its worker
    is finished, its check has not answered, and the entire value of the gate
    is that downstream work does not start on the strength of an unverified
    claim. `VERIFY_FAILED` is the same argument with the answer already in.

    One function, called from every site that computes a done set, because the
    rule has to be impossible to relax in one place and not another — a queue
    where `ready()` and `claim()` disagree about what counts as finished hands
    out work that is blocked and then refuses the report.
    """
    value = status.value if isinstance(status, WorkStatus) else status
    return value == WorkStatus.DONE.value


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

    # The gate, validated at the write boundary and normalised (`agentco/gates.py`).
    # First-class rather than a corner of `metadata` for one reason: a malformed
    # gate has to be refusable, and `metadata` is precisely where a payload
    # nobody validates goes to be silently ignored.
    #
    # Absent means ungated, which is what every item created before gates
    # existed is. That is the legacy scope guard, and it is a property of the
    # data rather than a flag: there is no backfill, and no flood of suddenly
    # unverified work.
    verify: Optional[dict] = None
    # The accepted evidence, if any. One record, not a log — the history of
    # attempts is the failure count plus the fix items it spawned, and a list
    # here would duplicate that badly.
    attestation: Optional[dict] = None
    # How many times this item's own gate has answered no. Drives the retry
    # policy (`gates.retry_decision`) and never resets: a fix item is a
    # different item, and this counter is about this one.
    verify_failures: int = 0

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

    @property
    def is_gated(self) -> bool:
        """True iff this item declares a gate. See `verify`."""
        return bool(self.verify)

    def unmet_blockers(self, done_ids: set[str]) -> list[str]:
        """Dependencies not yet done. Empty means nothing is holding this back.

        The set is built by `releases_blockers`, which admits DONE only — an
        item awaiting or failing verification is finished from its worker's
        point of view and still holds everything downstream.

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



# Metadata keys the plane writes about an item and reads back as fact. A caller
# who can set them on create can forge what the plane believes: `verifies` makes
# an ordinary item pass for a routing vehicle (so the real one is never filed and
# the gate resolves on its clock unseen), `claims` makes a queue nobody verified
# read as configured, `lease_report` invents an executor. Reserved at the write
# boundary, because the alternative — trusting every reader to re-derive what
# every writer might have lied about — is not a boundary.
RESERVED_METADATA_KEYS = frozenset({
    "verifies", "claims", "lease_report", "verify_parked_at", "verify_resolution",
    "verify_history", "verify_escalated", "verify_quarantined", "verify_retry",
    "verify_verdict", "natural_key_conflict",
    # Written only by `Queue.adjudicate`, which is where the adjudicator ≠
    # executor rule lives. A caller who could set it on create would be an
    # executor grading its own divergence, with the plane's name on the tag.
    "adjudication",
    # `sop_plan` is the procedure's own words, copied at instantiate under the
    # pin; `plan_vs_actual` is what the plane writes beside them at completion.
    # Both read back as fact by the adjudicator, so neither is a caller's to set.
    "sop_plan", "plan_vs_actual",
    # The pin itself. Written only by `SopLibrary.instantiate`, and read back as
    # fact by outcomes_by_version, drifted, proposals and lesson_provenance. A
    # caller who could set it could pin an item to a procedure it never ran,
    # have a colleague adjudicate it `bad`, and feed that procedure's lesson
    # channel through the pass — a lesson nobody learned, in a procedure nobody
    # followed. Found by the second party on P4.V.
    "sop_ref",
    # The run record: what inputs a run was given and which binding fills each
    # role (ASOP v3 §5.1). Written only by `SopLibrary.run` and read back as
    # fact by `run_get`/`promote`. A caller who could set it could claim a run
    # was bound to a validator that never saw it.
    "sop_run",
})
PLAN_KEY = "sop_plan"
PLAN_VS_ACTUAL_KEY = "plan_vs_actual"
# And the natural-key namespace the routing pass uses for vehicles.
RESERVED_KEY_PREFIX = "verify:"


# --------------------------------------------------------------------------- #
# Adjudication — the tag on a divergence, and who may write it
# --------------------------------------------------------------------------- #

#: `metadata.adjudication`: the record that judges a divergence — execution
#: departed from the procedure — as `good` (the procedure was wrong; feeds the
#: next version) or `bad` (the execution took a shortcut; feeds root-cause).
#: An adjudication, not a confession: the party that tags is never the party
#: whose fault a `bad` tag would admit (asop.md § 3).
ADJUDICATION_KEY = "adjudication"
ADJUDICATION_VERDICTS = ("good", "bad")

ADJUDICATION_INVALID = "adjudication_invalid"
ADJUDICATION_SELF = "adjudication_self"
ADJUDICATION_EXISTS = "adjudication_exists"
ADJUDICATION_UNEXECUTED = "adjudication_unexecuted"


def executors_of(item: "WorkItem") -> list[str]:
    """Every identity that executed this item, as the plane recorded it.

    The lease holder while it is held; the holder who reported it (the lease
    is released on report, so `leased_by` alone forgets the executor the
    moment the work is done); and, on a deterministic gate, whoever attested —
    there the executor IS the attester by design. Derived from records the
    plane wrote, never from anything a caller could set.
    """
    found: list[str] = []
    meta = item.metadata or {}
    candidates = [
        item.leased_by,
        (meta.get("lease_report") or {}).get("reported_by"),
        (item.attestation or {}).get("submitted_by")
        if (item.verify or {}).get("kind") == "deterministic" else None,
        # Every holder the item ever had. A first holder whose lease lapsed and
        # was reaped did work on it too — for idempotent work the second poller
        # reports what the first one did — and must not get to grade it.
        *(entry.get("agent") for entry in (meta.get("claims") or []) if isinstance(entry, dict)),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate and candidate not in found:
            found.append(candidate)
    return found


def adjudication_record(
    item: "WorkItem", verdict: object, evidence: object, adjudicator: object,
    *, humans: Optional[Iterable[str]] = None, adjudicators: Optional[Iterable[str]] = None,
) -> dict:
    """Validate an adjudication against the item it judges. Refuses; never repairs.

    Every refusal here fires BEFORE any store is touched, so a refused
    adjudication — like a refused create or a refused revision — leaves the
    item byte-identical. The self check compares against `executors_of`, which
    is derived from what the plane recorded and cannot be self-asserted; that
    is what makes "adjudicator ≠ executor" enforced rather than documented.

    **Two questions, in order.** Whether this party may adjudicate ANYTHING —
    ASOP v3 §6.1: a human always, a route only when the operator declared it
    an adjudicator — and then whether it may adjudicate THIS item, which is
    the separation check. A registry that declares no adjudicators makes the
    operator the only one, and that default is deliberate: an agent grading
    the loop that revises the procedures it follows degrades the evidence
    base, not the throughput, so nobody notices.
    """
    if not isinstance(adjudicator, str) or not adjudicator.strip():
        raise Refusal(
            code=ADJUDICATION_INVALID,
            message="adjudicate needs an authenticated adjudicator and got none",
            remediation=(
                "The adjudicator is the actor the transport authenticated. An "
                "empty one would pass the separation check by never equalling "
                "anything, which is a judgement from nobody."
            ),
        )
    if verdict not in ADJUDICATION_VERDICTS:
        raise Refusal(
            code=ADJUDICATION_INVALID,
            message=f"adjudication verdict must be one of {list(ADJUDICATION_VERDICTS)}, got {verdict!r}",
            remediation=(
                "`good` means the procedure was wrong and the deviation should "
                "feed its next version; `bad` means the execution took a "
                "shortcut and the deviation feeds root-cause. There is no third "
                "value, because a divergence nobody can classify is one nobody "
                "has looked at."
            ),
        )
    if not isinstance(evidence, str) or not evidence.strip():
        raise Refusal(
            code=ADJUDICATION_INVALID,
            message="adjudication needs pointed evidence and got none",
            remediation=(
                "Name what you looked at — a diff, a log line, a transcript "
                "location, a number. A tag without evidence is an opinion with "
                "the plane's name on it, and the next version of the procedure "
                "would be revised on it."
            ),
        )
    declared_humans = policy.humans_from_env() if humans is None else humans
    declared_adjudicators = (
        policy.adjudicators_from_env() if adjudicators is None else adjudicators
    )
    if not policy.may_adjudicate(adjudicator, humans=declared_humans,
                                 adjudicators=declared_adjudicators):
        named = sorted(frozenset(declared_adjudicators))
        raise Refusal(
            code=ADJUDICATION_INVALID,
            message=(
                f"{adjudicator!r} is neither a declared human nor a declared "
                f"adjudicator, so it may not judge a divergence"
            ),
            remediation=(
                f"Adjudication is what revises the procedures everyone follows, so "
                f"who may do it is declared by the operator, never inferred. Declare "
                f"this route in {policy.ADJUDICATORS_ENV_VAR} "
                f"(currently: {named or 'nothing — only declared humans adjudicate'}), "
                f"or route the divergence to a person."
            ),
            http_status=403,
        )
    executors = executors_of(item)
    if not executors:
        raise Refusal(
            code=ADJUDICATION_UNEXECUTED,
            message=f"{item.id} has never been executed, so there is no divergence to judge",
            remediation=(
                "Adjudication judges an execution against its procedure. This "
                "item was never claimed (or was reaped before it was reported); "
                "there is nobody whose work the tag would be about."
            ),
        )
    if adjudicator in executors:
        raise Refusal(
            code=ADJUDICATION_SELF,
            message=(
                f"{adjudicator!r} executed {item.id} and may not adjudicate it "
                f"(executors: {executors})"
            ),
            remediation=(
                "An adjudication is not a confession. Whoever tags a divergence "
                "must be a different party from the executor whose fault a "
                "`bad` tag would admit — route it to a reviewer, or to the "
                "verifier who answered the gate."
            ),
            http_status=403,
        )
    if (item.metadata or {}).get(ADJUDICATION_KEY):
        prior = item.metadata[ADJUDICATION_KEY]
        raise Refusal(
            code=ADJUDICATION_EXISTS,
            message=(
                f"{item.id} is already adjudicated {prior.get('verdict')!r} by "
                f"{prior.get('by')!r}"
            ),
            remediation=(
                "An adjudication is immutable once written — the next version "
                "of the procedure may already have been revised on it. A "
                "disagreement between adjudicators is a dispute, and ASOP v2 "
                "routes disputes to escalation, not to overwriting."
            ),
            http_status=409,
        )
    return {
        "verdict": verdict,
        "by": adjudicator,
        "evidence": evidence.strip(),
        "at": _iso(_now()),
        "executors": executors,
        "sop_ref": (item.metadata or {}).get("sop_ref"),
    }


# --------------------------------------------------------------------------- #
# Decomposition — parent / child, and the bound on both axes
# --------------------------------------------------------------------------- #

#: `metadata.parent` names the item this one decomposes. `metadata.repairs`
#: names the failed item this one is a fix FOR — a repair goes BESIDE the
#: failed unit (same parent, or none), never beneath it, so repair depth stays
#: bounded and a fix does not consume the parent's review budget.
PARENT_KEY = "parent"
REPAIRS_KEY = "repairs"

#: Six work units plus the verify unit that closes them. The escape hatch is
#: explicit and environment-wide, per the contract: a registry may RAISE it.
#: A goal that genuinely needs more is usually two goals.
MAX_CHILDREN = int(os.environ.get("AGENTCO_MAX_CHILDREN", "7"))
#: Root goal at depth 0; its leaves at most this far down. 7³ = 343 leaves.
MAX_DEPTH = int(os.environ.get("AGENTCO_MAX_DEPTH", "3"))


def _decomposition_refusal(message: str) -> DecompositionError:
    return DecompositionError(message)


def enforce_decomposition(
    item: "WorkItem",
    *,
    lookup: Callable[[str], Optional[dict]],
    count_children: Callable[[str], int],
) -> Optional[str]:
    """Check a new item's place in the tree. Returns the parent id to block, or None.

    Shared by both `create` implementations for the reason `build_item` is:
    one rule, two stores. `lookup` returns the RAW row for an id (or None) and
    `count_children` the number of non-repair children a parent already has;
    both run inside the caller's lock, so the count cannot move under the
    check.

    What is refused, each before anything is written:

      * a parent that does not exist — a child of nothing is a loose end by
        construction, and the id is usually a typo that would otherwise sit in
        metadata unread forever;
      * a parent that is DONE — a closed goal cannot grow; file a new one;
      * a parent already holding `MAX_CHILDREN` — the human review bound;
      * a chain deeper than `MAX_DEPTH`;
      * a repair filed BENEATH the item it repairs, or under a parent other
        than the repaired item's own — repairs go beside.

    The parent returned is the one whose `blocked_by` must gain this item, so
    a parent cannot close while a child is open. A repair blocks nobody: the
    failed original it repairs is still red and still blocking.
    """
    meta = item.metadata or {}
    parent_id = meta.get(PARENT_KEY)
    repairs_id = meta.get(REPAIRS_KEY)
    if parent_id is None and repairs_id is None:
        return None

    for key, value in ((PARENT_KEY, parent_id), (REPAIRS_KEY, repairs_id)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise _decomposition_refusal(
                f"metadata.{key} must be a work item id, got {value!r}."
            )

    if repairs_id is not None:
        repaired = lookup(repairs_id)
        if repaired is None:
            raise _decomposition_refusal(
                f"metadata.repairs names {repairs_id!r}, which does not exist. "
                f"A repair is for a failed unit; name the one that failed."
            )
        their_parent = (repaired.get("metadata") or {}).get(PARENT_KEY)
        if parent_id == repairs_id:
            raise _decomposition_refusal(
                f"a repair goes BESIDE the unit it repairs, never beneath it: "
                f"{repairs_id!r} cannot be both metadata.repairs and "
                f"metadata.parent. Use parent={their_parent!r} or omit it."
            )
        if parent_id is not None and parent_id != their_parent:
            raise _decomposition_refusal(
                f"a repair goes beside the unit it repairs: {repairs_id!r} sits "
                f"under {their_parent!r}, so its repair may sit under "
                f"{their_parent!r} or nowhere, not under {parent_id!r}."
            )
        # Beside means: same depth as the original, no budget consumed, and
        # the original — still failed, still blocking — is what holds the
        # parent open. Nothing further to check and nobody new to block.
        return None

    parent = lookup(parent_id)
    if parent is None:
        raise _decomposition_refusal(
            f"metadata.parent names {parent_id!r}, which does not exist. A child "
            f"of nothing is a loose end by construction — file the goal first."
        )
    if parent.get("status") == WorkStatus.DONE.value:
        raise _decomposition_refusal(
            f"{parent_id!r} is done; a closed goal cannot grow. If the work is "
            f"real, it is a new goal."
        )

    depth, seen, cursor = 1, {parent_id}, parent
    while True:
        above = (cursor.get("metadata") or {}).get(PARENT_KEY)
        if above is None:
            break
        if above in seen:
            raise _decomposition_refusal(
                f"the parent chain above {parent_id!r} loops at {above!r}; refusing "
                f"to add to a tree whose depth cannot be measured."
            )
        seen.add(above)
        cursor = lookup(above)
        if cursor is None:
            raise _decomposition_refusal(
                f"the parent chain above {parent_id!r} is broken at {above!r}, which "
                f"does not exist. Repair the chain before adding to it."
            )
        depth += 1
    if depth > MAX_DEPTH:
        raise _decomposition_refusal(
            f"a child of {parent_id!r} would sit at depth {depth}; the bound is "
            f"{MAX_DEPTH}. The tree is deeper than one accountable person can "
            f"review. Close a level, or start a second goal."
        )

    held = count_children(parent_id)
    if held >= MAX_CHILDREN:
        raise _decomposition_refusal(
            f"{parent_id!r} already holds {held} children; the bound is "
            f"{MAX_CHILDREN} — six work units and the verify unit that closes "
            f"them. This is a human review bound, not a capacity: a goal that "
            f"genuinely needs more is usually two goals. (A registry may raise "
            f"it with AGENTCO_MAX_CHILDREN; that is the explicit escape hatch.)"
        )
    return parent_id


def is_child_row(row: dict, parent_id: str) -> bool:
    """A non-repair child of `parent_id`, from a raw row."""
    meta = row.get("metadata") or {}
    return meta.get(PARENT_KEY) == parent_id and meta.get(REPAIRS_KEY) is None


def reject_reserved(metadata: Optional[dict], natural_key: Optional[str] = None) -> None:
    """Refuse a caller's metadata that names a plane-owned key, or a reserved key prefix.

    Split out of `build_item` so a plane-side writer that must ADD a reserved
    key (`SopLibrary.instantiate` copying the plan under the pin) can first
    hold the caller's own metadata to the same rule, then file with
    `by_plane=True`. Without this the plane's own convenience would be the
    hole: anything routed through it would skip the check.
    """
    forged = sorted(RESERVED_METADATA_KEYS & set(metadata or {}))
    if forged:
        raise Refusal(
            code="metadata_reserved",
            message=f"metadata sets plane-owned key(s) {forged}",
            remediation=(
                "Remove them. These keys are written by AgentCo about an item "
                "and read back as fact — a caller who could set `verifies` would "
                "make an ordinary item pass for a routing vehicle, so the real "
                "one is never filed and the gate resolves on its clock with no "
                "verifier ever seeing it. Put your own data under keys of your "
                "own."
            ),
        )
    if isinstance(natural_key, str) and natural_key.startswith(RESERVED_KEY_PREFIX):
        raise Refusal(
            code="natural_key_reserved",
            message=f"natural key {natural_key!r} is in the routing pass's namespace",
            remediation=(
                f"Keys beginning {RESERVED_KEY_PREFIX!r} name verification "
                f"vehicles and are filed by the plane. Filing one yourself "
                f"pre-empts the real vehicle for that item — the routing pass "
                f"sees the key, believes the gate is routed, and never files "
                f"the one a verifier could actually claim."
            ),
        )


def plan_vs_actual(item: "WorkItem", *, reported: "WorkStatus", landed: "WorkStatus",
                   result: Optional[str], attempt: int, record: Optional[dict],
                   failures: int, at: str) -> dict:
    """The review, written at the moment of completion while the context exists.

    Plan is the procedure's own words as pinned at instantiate (`sop_plan`);
    actual is what the plane recorded of the execution. Side by side, with
    nothing judged: the plane does not know whether a divergence was the
    procedure's fault or the executor's — that is the adjudicator's call, and
    this record is the evidence they read. Written every time, not only on
    divergence, because "the plan and the outcome agreed" is itself the
    observation `outcomes_by_version` needs.
    """
    meta = item.metadata or {}
    actual: dict = {
        "reported": reported.value,
        "landed": landed.value,
        "executor": item.leased_by,
        "attempt": attempt,
        "result": result,
        "verify_failures": failures,
        "filed_at": item.created_at,
        "reported_at": at,
    }
    if record:
        actual["attestation"] = {
            k: record.get(k) for k in ("check", "exit_status", "environment", "at", "submitted_by")
        }
    gate = item.verify or {}
    return {
        "generated_at": at,
        "sop_ref": meta.get("sop_ref"),
        "plan": meta.get(PLAN_KEY),
        "gate": {"kind": gate.get("kind"), "check": gate.get("check")} if gate else None,
        "actual": actual,
        # What the reader should look at first. Computed, not judged.
        "flags": sorted(
            f for f, on in (
                # Parking is not a verdict; only a gate that said no disagreed.
                ("gate_disagreed", landed is WorkStatus.VERIFY_FAILED and reported is WorkStatus.DONE),
                ("retried", failures > 0),
                ("failed", landed is WorkStatus.FAILED),
                ("awaiting_verdict", landed is WorkStatus.AWAITING_VERIFY),
            ) if on
        ),
    }


def build_item(
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
    verify: Optional[dict] = None,
    by_plane: bool = False,
) -> WorkItem:
    """Derive the natural key, validate the gate, and return the new item.

    `by_plane` is how the routing pass files a vehicle — it is the one writer
    entitled to the reserved metadata keys and the `verify:` key namespace. It
    is a Python keyword argument and never a wire field: no transport reads it
    off a payload, so a caller cannot ask for it.

    Shared by both `create` implementations on purpose. There are two write
    paths for storage reasons and there is exactly one rule for what a new item
    IS — and a validation that lives in each of them separately is a validation
    that will hold in one and not the other, silently, on whichever backend the
    next deployment happens to use. Everything here refuses BEFORE any store is
    touched, so a refused create leaves the store byte-identical.
    """
    if not by_plane:
        reject_reserved(metadata, natural_key)
    gate = gates.validate_gate(verify) if verify is not None else None
    if gate and gate.get("kind") == "human" and assigned_agent and gate.get("verifier") == assigned_agent:
        # The separation check would refuse this person's verdict when it came,
        # so the gate could only ever resolve on its clock — configured-looking,
        # and unanswerable. Refused here, like every other gate that would
        # quietly do nothing (DECIDE-L3 #5).
        raise Refusal(
            code=gates.GATE_INVALID,
            message=(
                f"the human gate names {assigned_agent!r} as verifier and the item is "
                f"assigned to {assigned_agent!r} — the one party a human gate exists to exclude"
            ),
            remediation=(
                "Name somebody else as verifier, or assign the work to somebody else. "
                "A person may not sign off their own step; the plane would refuse the "
                "verdict, and the gate would resolve on its clock with nobody having looked."
            ),
        )
    return WorkItem(
        id=f"w-{uuid.uuid4().hex[:8]}",
        title=title,
        requires=list(requires),
        blocked_by=list(blocked_by),
        assigned_agent=assigned_agent,
        natural_key=derive_natural_key(
            explicit=natural_key,
            source=source,
            source_id=source_id,
            kind=kind,
            subject=subject,
            period=period,
        ),
        metadata=dict(metadata or {}),
        verify=gate,
    )


class Queue:
    """A JSONL work store with an advisory lock around every mutation."""

    def __init__(self, path: Path | str = "work.jsonl", verifiers: Optional[Sequence[str]] = None,
                 humans: Optional[Sequence[str]] = None,
                 adjudicators: Optional[Sequence[str]] = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The operator's declared verifiers (`AGENTCO_VERIFIERS`). Declared,
        # the `verify` capability is bound to these identities on every claim
        # and every verdict; undeclared, it stays self-asserted and the status
        # report says so. Injectable for tests.
        self.verifiers: frozenset[str] = (
            frozenset(verifiers) if verifiers is not None else policy.verifiers_from_env()
        )
        # Who may judge a divergence (ASOP v3 §6.1). `humans` is the same
        # declaration the revision policy reads; `adjudicators` is the opt-in
        # that lets a ROUTE do it. Undeclared adjudicators means humans only —
        # unlike verifiers, this one fails closed, because what degrades when
        # it fails open is the evidence the procedures are revised from.
        self.humans: frozenset[str] = (
            frozenset(humans) if humans is not None else policy.humans_from_env()
        )
        self.adjudicators: frozenset[str] = (
            frozenset(adjudicators) if adjudicators is not None
            else policy.adjudicators_from_env()
        )
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
            lock_exclusive(handle)
            try:
                yield
            finally:
                unlock(handle)

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
                # NOT appended to `quarantined`. Two docstrings promise that
                # list holds the exact bytes read — `_read_raw` says re-encoding
                # a guess is how the original bytes get lost, `_write_all` says
                # they go back verbatim — and `json.dumps(row)` is a
                # re-serialisation, not the original line. Worse, this row is
                # ALSO in `rows`, so passing both to `_write_all` would write it
                # twice: once as a row and once as a "quarantined line".
                #
                # The row is already preserved by being in `raw_rows`, which
                # every write path carries through. Nothing is lost by leaving
                # it out of a list whose contract it does not satisfy.
                pass
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
        verify: Optional[dict] = None,
        by_plane: bool = False,
    ) -> WorkItem:
        """Create one item. A duplicate natural key is a LOUD no-op.

        Returns the EXISTING item when the key is already present, and says so
        on stderr naming the key, the suppressed title and the holder's id.
        Silence was never an option: a suppressed duplicate nobody announces is
        indistinguishable from a create that worked, and the caller goes on to
        reference an id that is not theirs.

        Returning rather than raising is deliberate — every ingest path already
        wanted exactly this and hand-rolled it before there was one rule.

        `verify` is the gate, and it is validated HERE — before the lock, before
        the duplicate check, before anything is written. A malformed gate must
        never reach storage: the failure mode of a gate that cannot be executed
        is not an error at execution time, it is a check that silently does
        nothing while the item reports green. `agentco/gates.py` holds the rules
        and the refusals.
        """
        item = build_item(
            title,
            requires=requires,
            blocked_by=blocked_by,
            assigned_agent=assigned_agent,
            natural_key=natural_key,
            source=source,
            source_id=source_id,
            kind=kind,
            subject=subject,
            period=period,
            metadata=metadata,
            verify=verify,
            by_plane=by_plane,
        )
        key = item.natural_key

        with self._locked():
            raw_rows, quarantined = self._read_raw()
            if key:
                for row in raw_rows:
                    if natural_key_of(row) == key:
                        try:
                            existing = WorkItem.from_json(json.dumps(row))
                        except (ValueError, TypeError) as exc:
                            # The other half of the same boundary. I fixed
                            # `_mutate` and wrote a commit message claiming both
                            # write paths; this one was left raising a bare
                            # enum error, so filing ANY item whose natural key
                            # matched an unmodellable row failed with an
                            # exception about a status value — and duplicate
                            # suppression stopped working, which is the one
                            # thing the key exists to do.
                            raise WorkError(
                                f"cannot suppress a duplicate of {key!r}: the "
                                f"existing row is not readable by this version "
                                f"({type(exc).__name__}: {exc}). The row is "
                                f"preserved on disk. Creating a second item "
                                f"under the same key would be worse than "
                                f"refusing — upgrade, or repair the row."
                            ) from exc
                        print(
                            f"[work] DUPLICATE-SUPPRESSED key={key!r} "
                            f"title={title!r} held-by={existing.id}",
                            file=sys.stderr,
                        )
                        existing.metadata = dict(existing.metadata or {})
                        existing.metadata["natural_key_conflict"] = True
                        return existing
            by_id = {row.get("id"): row for row in raw_rows}
            blocked_parent = enforce_decomposition(
                item,
                lookup=by_id.get,
                count_children=lambda pid: sum(1 for r in raw_rows if is_child_row(r, pid)),
            )
            if blocked_parent is not None:
                # The parent cannot close while this child is open. Written in
                # the same lock as the child, so there is no instant in which
                # the child exists and the parent is claimable past it.
                parent_row = by_id[blocked_parent]
                parent_row["blocked_by"] = sorted(set(parent_row.get("blocked_by") or []) | {item.id})
                parent_row["updated_at"] = _iso(_now())
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
        done = {i.id for i in items if releases_blockers(i.status)}
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
                    try:
                        target = WorkItem.from_json(json.dumps(row))
                    except (ValueError, TypeError) as exc:
                        # `_read_all` tolerates this row; the write paths did
                        # not, so the quarantine boundary held on read and
                        # leaked on write. A row this version cannot model is
                        # not a row it may act on — refuse, naming the row, so
                        # the caller learns the store is ahead of them rather
                        # than getting a bare type error from inside a lock.
                        raise WorkError(
                            f"cannot act on {item_id}: its stored row is not "
                            f"readable by this version ({type(exc).__name__}: "
                            f"{exc}). The row is preserved on disk. Upgrade, or "
                            f"repair the row — this version must not overwrite "
                            f"a record it cannot understand."
                        ) from exc
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
        held, unbound = policy.bind_capabilities(agent, capabilities, self.verifiers)
        # From the RAW rows, not from `_read_all()`. `_read_all` drops any row
        # this version cannot model — a newer writer's unknown status value, a
        # missing field — and a dropped row is invisible to `done_ids`. So a
        # blocker that IS finished, whose row happens to carry something
        # unmodellable, never counted as done and its dependents became
        # permanently unclaimable, told to "finish those first" about work
        # already complete.
        #
        # Neither of the two fixes that produced this was wrong on its own:
        # tolerating unknown rows keeps an older reader working against a newer
        # store, and deriving blockedness removed a cache that never
        # invalidated. They compose into a defect neither has alone, which is
        # the kind only a fresh pass over changed code finds.
        #
        # `status` is a plain string on the raw dict and stays legible even when
        # a different field in the same row does not, so this needs no
        # modelling at all.
        raw_rows, _ = self._read_raw()
        done_ids = {r["id"] for r in raw_rows if releases_blockers(r.get("status", ""))}

        def cas(item: WorkItem) -> dict:
            # Capability BEFORE the CAS checks, on purpose: given a permanent
            # problem and a transient one, report the permanent one. "This
            # worker can never run this" is more actionable than "someone else
            # holds it right now", which may also be true.
            missing = [r for r in item.requires if r not in held]
            if missing:
                if unbound and policy.VERIFY_CAPABILITY in missing:
                    raise CapabilityError(
                        f"cannot claim {item_id} for {agent!r}: it requires "
                        f"{policy.VERIFY_CAPABILITY!r}, which this worker declares — but "
                        f"the operator has bound that capability to "
                        f"{sorted(self.verifiers)} and {agent!r} is not among them. "
                        f"Declaring the word is not being the verifier. Add the actor to "
                        f"{policy.VERIFIERS_ENV_VAR}, or route the gate to one who is."
                    )
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
                # The two verify states are not terminal, and saying so would
                # send the reader looking for a finished item. They are settled
                # against a gate: the work is claimed complete and only the
                # gate's answer moves it, so a worker cannot pick it back up.
                because = (
                    "which is awaiting its verification gate"
                    if item.status is WorkStatus.AWAITING_VERIFY
                    else "whose verification gate said no — its own gate has to "
                    "pass, and a fix belongs in a separate item"
                    if item.status is WorkStatus.VERIFY_FAILED
                    else "which is terminal"
                )
                raise LeaseError(
                    f"cannot claim {item_id} for {agent!r}: status is "
                    f"{item.status.value}, {because}"
                )
            # Every claim leaves a mark no later event removes. `leased_by` is
            # cleared by a report or a reap, and `lease_attempt` is advanced by
            # things that are not claims — so neither answers "did anybody ever
            # hold this", which is what the verifier-presence report asks of a
            # routing vehicle. Bounded: a poller re-claiming one item a thousand
            # times is not a thousand facts.
            history = list((item.metadata or {}).get("claims") or [])[-19:]
            history.append({"agent": agent, "attempt": item.lease_attempt + 1, "at": _iso(at)})
            return {
                "status": WorkStatus.IN_PROGRESS,
                "metadata": {**(item.metadata or {}), "claims": history},
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

    # -- gates -----------------------------------------------------------

    def _gate_outcome(
        self,
        item: WorkItem,
        reported: WorkStatus,
        *,
        attestation: Optional[dict],
        submitted_by: Optional[str],
        metadata: dict,
        now: Optional[datetime] = None,
    ) -> tuple[WorkStatus, Optional[dict], int]:
        """Where a reported outcome meets the gate. Mutates `metadata` in place.

        `now` is the clock every stamp here reads — the park clock a gate
        starts, and the moment a retry decision was taken. Injectable for the
        same reason `claim` and `sweep_park_clocks` take one: a test that
        stamps the start from the wall clock and checks expiry against a
        synthetic one is comparing two clocks and calling it a bug when they
        drift (two cross-vendor reviews, 2026-09-04). Production passes
        nothing and gets the wall clock.

        Returns the status the report actually lands as, the evidence to store,
        and the failure count. Called from inside `fence`, so every refusal here
        aborts the write with the store byte-identical — a gate that rejects a
        report must not have moved the item first.
        """
        failures = item.verify_failures

        if reported is WorkStatus.FAILED:
            if attestation is not None:
                raise Refusal(
                    code=gates.ATTESTATION_INVALID,
                    message=(
                        f"an attestation was offered while reporting {item.id} "
                        f"FAILED"
                    ),
                    remediation=(
                        "Report the failure on its own. An attestation is "
                        "evidence that the work is correct; it has nothing to "
                        "say about a worker reporting that it is not."
                    ),
                )
            return WorkStatus.FAILED, item.attestation, failures

        if not item.is_gated:
            if attestation is not None:
                raise Refusal(
                    code=gates.ATTESTATION_INVALID,
                    message=f"{item.id} declares no gate, so there is nothing to attest to",
                    remediation=(
                        "File the item with a `verify` gate if its completion "
                        "needs evidence. Accepting evidence against no gate "
                        "would let an item look verified when nothing was ever "
                        "required of it."
                    ),
                )
            return WorkStatus.DONE, None, failures

        gate = item.verify or {}
        if gate.get("kind") != "deterministic":
            if attestation is not None:
                raise Refusal(
                    code=gates.ATTESTATION_INVALID,
                    message=(
                        f"{item.id} has a {gate.get('kind')!r} gate, which the "
                        f"executor may not satisfy itself"
                    ),
                    remediation=(
                        "Report completion without an attestation; the item "
                        "parks as awaiting_verify and the check is routed to a "
                        "party other than the executor. That separation is the "
                        "entire difference between a judged gate and a "
                        "deterministic one."
                    ),
                )
            metadata["verify_parked_at"] = _iso(now or _now())
            return WorkStatus.AWAITING_VERIFY, item.attestation, failures

        if attestation is None:
            raise Refusal(
                code=gates.ATTESTATION_REQUIRED,
                message=(
                    f"{item.id} has a deterministic gate ({gate.get('check')!r}) "
                    f"and the report carries no attestation"
                ),
                remediation=(
                    f"Re-run {gate.get('check')!r}, then report again with an "
                    f"attestation naming the check, its exit status, the "
                    f"environment it ran in and when. A completion claim on a "
                    f"gated item is refused without one — silently accepting it "
                    f"would make the gate report green having checked nothing."
                ),
            )

        record = gates.validate_attestation(
            attestation,
            gate=gate,
            submitted_by=submitted_by or "unknown",
        )
        if gates.attestation_passes(record):
            return WorkStatus.DONE, record, failures

        failures += 1
        metadata["verify_retry"] = {
            "failures": failures,
            # The policy, recorded rather than acted on. Spawning a fix item as
            # a side effect of a report would create work nobody asked this call
            # to create; the caller reads the decision and does it.
            "decision": gates.retry_decision(failures),
            "decided_at": _iso(now or _now()),
        }
        # The clock starts here too. A failed gate's re-verify offer used to
        # have no clock at all, so an unclaimed vehicle sat open forever and
        # the digest never saw it (DECIDE-L3 #3).
        metadata["verify_parked_at"] = _iso(now or _now())
        return WorkStatus.VERIFY_FAILED, record, failures

    def attest(
        self,
        item_id: str,
        attestation: dict,
        submitted_by: str,
        capabilities: Optional[Sequence[str]] = None,
        adjudication: Optional[dict] = None,
        now: Optional[datetime] = None,
    ) -> Optional[WorkItem]:
        """Answer a gate. The only transition out of a verify state.

        `adjudication` — `{verdict, evidence}` — rides along when the verifier
        is also judging the divergence they saw, and is written by
        `adjudicate()` under the same rules, with the submitter as adjudicator.
        It is validated BEFORE the verdict is applied, so an executor re-running
        a deterministic gate who offers one is refused whole: nothing lands,
        and the remediation says to attest without it. Riding here is what
        puts adjudication on the MCP surface without a thirteenth tool.

        **A verdict displaces the park clock's record of there being none.** An
        item the clock failed and a verifier then passed used to close DONE
        carrying both the attestation and a top-level note reading "no check was
        run", and every reader — including this module's own presence report —
        believed the second. The clock's record moves under `HISTORY_KEY`; it is
        not deleted, because the clock did fire and how long the gate waited
        before somebody answered it is worth keeping.

        **A fix item never substitutes for the work it repairs.** A failed unit
        keeps `VERIFY_FAILED`, and keeps blocking everything downstream, until
        ITS OWN gate runs again and passes — which is this call. Anything else
        lets a green sibling stand in for a red original, and the dependency
        that was waiting on correctness gets released by a different item's
        success.

        The executor this compares against is the lease holder, so the claim
        "cannot be self-asserted" is only as true as the transport's claim path.
        HTTP claims as the signed actor; MCP used to accept an `agent` argument
        and does not any more — a model that could name the executor could name
        one that was not itself. Any future claim path has to hold the same line
        or this check is decoration.

        `capabilities` is the same self-declared routing list `claim` takes, and
        a judged gate refuses a verdict from a node that does not declare
        `verify`. Be precise about what that buys: capabilities are asserted by
        the caller, so this is **routing hygiene, not authority** — it stops a
        node answering gates it was never set up for, and a node determined to
        answer one need only declare the string. The authority check for a
        judged gate is the separation below, which is derived from the
        authenticated actor and cannot be self-asserted.

        Without it, though, the capability was decorative: `verifiers` routes a
        vehicle only a declaring node can claim, while the verdict path stayed
        open to anyone holding the item id. Gating the route and not the outcome
        is a queue that looks routed and is not.

        For a `judged` or `human` gate the submitter must not be the party that
        executed the work. That is not a policy preference: a judged gate exists
        precisely because the executor is not entitled to mark its own work
        verified, and a gate that accepts the executor's verdict is a
        deterministic gate with extra ceremony.
        """
        if not isinstance(submitted_by, str) or not submitted_by.strip():
            raise Refusal(
                code=gates.ATTESTATION_INVALID,
                message="attest needs an authenticated submitter and got none",
                remediation=(
                    "The submitter is the actor the transport authenticated. An "
                    "empty one would pass the executor-separation check by never "
                    "equalling anything, which is a verdict from nobody."
                ),
            )
        current = self.get(item_id)
        if current is None:
            return None
        if adjudication is not None:
            if not isinstance(adjudication, dict):
                raise Refusal(
                    code=ADJUDICATION_INVALID,
                    message=f"adjudication must be an object, got {type(adjudication).__name__}",
                    remediation="Send {\"verdict\": \"good\"|\"bad\", \"evidence\": \"...\"}.",
                )
            stray = sorted(set(adjudication) - {"verdict", "evidence"})
            if stray:
                # Refused, not ignored: a rider naming `by` must not be read
                # back as a tag under that name having been recorded.
                raise Refusal(
                    code=ADJUDICATION_INVALID,
                    message=f"adjudication rider carries {stray}; it takes verdict and evidence only",
                    remediation=(
                        "Remove them. The adjudicator is the party submitting this "
                        "attestation — the transport authenticated it — and nothing in "
                        "the rider may say otherwise."
                    ),
                )
            if (current.verify or {}).get("kind") == "deterministic":
                # On a deterministic gate the attester IS an executor the moment
                # the attestation lands, so the rider could only ever be
                # self-adjudication — refused before anything is written, rather
                # than landing the verdict and refusing the tag after.
                raise Refusal(
                    code=ADJUDICATION_SELF,
                    message=(
                        f"{item_id} has a deterministic gate; attesting it makes "
                        f"{submitted_by!r} an executor, so the rider would be "
                        f"self-adjudication"
                    ),
                    remediation=(
                        "Attest without the rider. A deterministic gate's attester "
                        "grades the check, not the divergence; somebody who did not "
                        "run the work adjudicates it separately."
                    ),
                    http_status=403,
                )
            adjudication_record(
                current, adjudication.get("verdict"), adjudication.get("evidence"), submitted_by
            )

        def verdict(item: WorkItem) -> dict:
            if not item.is_gated:
                raise Refusal(
                    code=gates.ATTESTATION_INVALID,
                    message=f"{item.id} declares no gate, so there is nothing to verify",
                    remediation=(
                        "Attest only against an item filed with a `verify` "
                        "gate. Its absence means nobody ever required evidence "
                        "of this item, and inventing the requirement after the "
                        "fact would change what its completion meant."
                    ),
                )
            if item.status not in (WorkStatus.AWAITING_VERIFY, WorkStatus.VERIFY_FAILED):
                raise Refusal(
                    code=gates.ATTESTATION_INVALID,
                    message=(
                        f"{item.id} is {item.status.value}, not awaiting or "
                        f"failing verification"
                    ),
                    remediation=(
                        "A gate answers a completion claim. Wait for the item "
                        "to be reported complete; verifying work that has not "
                        "claimed to be finished would record a verdict about an "
                        "execution still in progress."
                    ),
                )

            gate = item.verify or {}
            if gate.get("kind") == "judged":
                held, unbound = policy.bind_capabilities(submitted_by, capabilities, self.verifiers)
                if unbound:
                    raise Refusal(
                        code=gates.ATTESTATION_INVALID,
                        message=(
                            f"{item.id} has a judged gate; {submitted_by!r} declares "
                            f"{gates.VERIFY_CAPABILITY!r} but the operator bound it to "
                            f"{sorted(self.verifiers)}"
                        ),
                        remediation=(
                            f"A verdict on a judged gate comes from a declared verifier. "
                            f"Add {submitted_by!r} to {policy.VERIFIERS_ENV_VAR} if they are "
                            f"one; otherwise the gate waits for somebody who is. Declaring "
                            f"the capability was never the authority — this is."
                        ),
                    )
                if gates.VERIFY_CAPABILITY not in held:
                    raise Refusal(
                        code=gates.ATTESTATION_INVALID,
                        message=(
                            f"{item.id} has a judged gate and this caller declares "
                            f"{sorted(held) or '(no capabilities)'}"
                        ),
                        remediation=(
                            f"Declare {gates.VERIFY_CAPABILITY!r} on the node answering "
                            f"judged gates — `AGENTCO_CAPABILITIES={gates.VERIFY_CAPABILITY}` "
                            f"for the MCP surface, or the `capabilities` field over HTTP. "
                            f"The routed vehicle already requires it to be claimed; this "
                            f"is the same rule reaching the verdict."
                        ),
                    )
            executor_now = (item.metadata or {}).get("lease_report", {}).get("reported_by")
            if gate.get("kind") != "deterministic" and not executor_now:
                raise Refusal(
                    code=gates.ATTESTATION_INVALID,
                    message=(
                        f"{item.id} has a {gate.get('kind')!r} gate and no recorded "
                        f"executor, so there is nobody to keep the verifier separate from"
                    ),
                    remediation=(
                        "This item reached a verify state without a lease-holder's "
                        "report — a store written by an earlier version, or a row "
                        "edited by hand. Re-claim and re-report it under a real lease; "
                        "a separation check against nobody is no check."
                    ),
                )
            named = gate.get("verifier")
            if gate.get("kind") == "human" and not named:
                raise Refusal(
                    code=gates.ATTESTATION_INVALID,
                    message=f"{item.id}'s human gate names nobody to answer it",
                    remediation=(
                        "Gates written since the `verifier` field existed cannot be "
                        "stored this way; this one predates it. Revise the item's gate "
                        "to name who signs off before anyone attests — the write "
                        "boundary is not the only boundary."
                    ),
                )
            if gate.get("kind") == "human" and named and submitted_by != named:
                raise Refusal(
                    code=gates.ATTESTATION_INVALID,
                    message=(
                        f"{item.id}'s human gate names {named!r} to answer it, and "
                        f"{submitted_by!r} is not that person"
                    ),
                    remediation=(
                        f"Have {named!r} attest, or revise the gate to name whoever "
                        f"should. A human gate exists to put ONE named person's "
                        f"judgement on the work; a verdict from anyone else who is "
                        f"merely not the executor is a judged gate wearing a name."
                    ),
                )
            executor = (item.metadata or {}).get("lease_report", {}).get("reported_by")
            if gate.get("kind") != "deterministic" and submitted_by == executor:
                raise Refusal(
                    code=gates.ATTESTATION_INVALID,
                    message=(
                        f"{submitted_by!r} executed {item.id} and cannot also "
                        f"verify its {gate.get('kind')!r} gate"
                    ),
                    remediation=(
                        "Route this gate to a worker declaring the `verify` "
                        "capability, or to a person. The separation is the "
                        "whole property being bought."
                    ),
                )

            record = gates.validate_attestation(attestation, gate=gate, submitted_by=submitted_by)
            metadata = dict(item.metadata or {})
            metadata["verify_verdict"] = {
                "verified_by": submitted_by,
                "verified_at": _iso(_now()),
                "passed": gates.attestation_passes(record),
                "re_verify": item.status == WorkStatus.VERIFY_FAILED,
            }
            # An answer supersedes the absence of one. The clock may have failed
            # this gate before anybody looked, and that record must not survive
            # at the top level once somebody has: it says no check was run, and
            # a reader six weeks from now would find it sitting beside the
            # evidence of the check. It is moved rather than dropped — the clock
            # firing happened, and how long this waited before it got a verdict
            # is the whole history of the gate.
            # The same goes for the quarantine and escalation records: each says
            # "nobody had answered as of this moment", and a verdict makes that
            # history. Left in place, the digest went on listing an ANSWERED
            # gate as abandoned, with an age that kept growing — a digest lying.
            for key in (RESOLUTION_KEY, "verify_quarantined", "verify_escalated"):
                superseded = metadata.pop(key, None)
                if superseded is not None:
                    metadata[HISTORY_KEY] = [
                        *(metadata.get(HISTORY_KEY) or []),
                        {"kind": key, **superseded} if isinstance(superseded, dict) else superseded,
                    ]
            review = metadata.get(PLAN_VS_ACTUAL_KEY)
            if isinstance(review, dict):
                # The review was written when the executor reported; the verdict
                # arrives later, from somebody else. It lands beside the actual
                # rather than replacing it, so the adjudicator sees both what
                # the executor claimed and what the verifier found.
                review = {**review, "verdict": {
                    "by": record.get("submitted_by"), "exit_status": record.get("exit_status"),
                    "at": record.get("at"), "passed": gates.attestation_passes(record),
                }}
                metadata[PLAN_VS_ACTUAL_KEY] = review
            if gates.attestation_passes(record):
                metadata.pop("verify_retry", None)
                return {
                    "status": WorkStatus.DONE,
                    "attestation": record,
                    "metadata": metadata,
                }

            failures = item.verify_failures + 1
            metadata["verify_retry"] = {
                "failures": failures,
                "decision": gates.retry_decision(failures),
                "decided_at": _iso(now or _now()),
            }
            metadata["verify_parked_at"] = _iso(now or _now())  # the re-verify offer's clock starts now
            return {
                "status": WorkStatus.VERIFY_FAILED,
                "attestation": record,
                "verify_failures": failures,
                "metadata": metadata,
            }

        attested = self._mutate(item_id, verdict)
        if attested is not None and adjudication is not None:
            return self.adjudicate(
                item_id, adjudication.get("verdict"), adjudication.get("evidence"),
                adjudicator=submitted_by,
            )
        return attested

    def adjudicate(
        self,
        item_id: str,
        verdict: object,
        evidence: object,
        *,
        adjudicator: object,
    ) -> Optional[WorkItem]:
        """Tag a divergence `good` or `bad`, as somebody who did not execute it.

        The third ASOP property's first record. `good` says the procedure was
        wrong and the deviation feeds its next version; `bad` says the
        execution took a shortcut and the deviation feeds root-cause. Either way
        the tag carries the adjudicator's identity and pointed evidence, and
        **the adjudicator is never the executor** — compared against every
        identity the plane recorded as having executed the item, none of which
        a caller can set. Immutable once written; a disagreement is a dispute
        and escalates rather than overwrites.

        `adjudicator` is the actor the transport authenticated, never a body
        field: HTTP passes the signed actor, MCP the process identity, the
        outbox the drainer's machine credential — which means, as with a judged
        gate, an adjudication relayed from the machine that executed the work
        is refused as self-adjudication. That is the rule working.
        """
        current = self.get(item_id)
        if current is None:
            raw_rows, _ = self._read_raw()
            if any(r.get("id") == item_id for r in raw_rows):
                raise WorkError(
                    f"work item {item_id} exists but its stored row is not "
                    f"readable by this version, so it cannot be adjudicated. "
                    f"The row is preserved on disk. Upgrade, or repair the row."
                )
            return None
        # Validated on the read, and again inside the lock: the first refuses
        # before anything is touched, the second holds if the item moved.
        adjudication_record(current, verdict, evidence, adjudicator,
                            humans=self.humans, adjudicators=self.adjudicators)

        def tag(item: WorkItem) -> dict:
            record = adjudication_record(item, verdict, evidence, adjudicator,
                                         humans=self.humans, adjudicators=self.adjudicators)
            metadata = dict(item.metadata or {})
            metadata[ADJUDICATION_KEY] = record
            return {"metadata": metadata}

        return self._mutate(item_id, tag)

    def report_result(
        self,
        item_id: str,
        attempt: int,
        status: WorkStatus,
        result: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        attestation: Optional[dict] = None,
        submitted_by: Optional[str] = None,
        now: Optional[datetime] = None,
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

        **On a gated item, DONE is a request, not an outcome.** What it lands as
        is the gate's to decide (`agentco/gates.py`):

          * `deterministic` — the completing process re-ran the check, so an
            attestation must arrive WITH the report. Missing, it is refused:
            that refusal is what buys back the atomicity that folding `attest`
            into this verb would have given, and it is the reason first-class
            verbs cost nothing in integrity
            (`docs/decisions/0002-participation-ladder.md`). Exit 0 lands DONE;
            anything else lands `VERIFY_FAILED`.
          * `judged` / `human` — verification is deliberately not the
            executor's, so the report lands `AWAITING_VERIFY` and an
            attestation offered here is refused rather than accepted from the
            wrong party.

        FAILED needs no gate. A gate answers "is this work correct", and a
        worker reporting its own failure is not making that claim.

        `submitted_by` is the authenticated actor as the transport knows it, and
        when given it must BE the lease holder — the attempt number is readable
        by any actor, so knowing it cannot be what makes a report legitimate. A
        report from a non-holder is refused rather than recorded against the
        holder's name. When absent (in-process callers), the holder is taken as
        the reporter; the holder was authenticated at claim time.

        A report also needs a lease to end. An unclaimed item accepted a report
        at attempt 0, landed `awaiting_verify` with an executor of None, and the
        judged-gate separation check — which compares the verifier against the
        executor — compared against nothing and matched nobody. The party that
        reported could then verify its own report. So: no lease, no report.
        """
        if status not in TERMINAL:
            raise Refusal(
                code="not_terminal",
                message=f"{status.value} is not a terminal outcome",
                remediation=(
                    f"Report one of {', '.join(sorted(s.value for s in TERMINAL))}. "
                    "A lease is released by reporting, not by reporting progress."
                ),
                http_status=400,
            )

        current = self.get(item_id)
        if current is None:
            # `get()` reads through `_read_all()`, which DROPS rows this version
            # cannot model — so "not found" and "found but unreadable" arrived
            # here identically, and callers reported the first. `mcp_server`
            # said "no work item X on this queue" about an item that is on the
            # queue, which is a false statement rather than an unhelpful one.
            raw_rows, _ = self._read_raw()
            if any(r.get("id") == item_id for r in raw_rows):
                raise WorkError(
                    f"work item {item_id} exists but its stored row is not "
                    f"readable by this version, so its lease cannot be fenced. "
                    f"The row is preserved on disk. Upgrade, or repair the row."
                )
            return None
        class _AlreadyRecorded(Exception):
            """Raised from inside the lock: the key is on record, nothing to write."""

            def __init__(self, item: WorkItem):
                self.item = item

        def fence(item: WorkItem) -> dict:
            # INSIDE the lock, before the fence. Read outside it, an honest
            # retry racing its own first attempt saw no key yet, reached the
            # fence with a stale attempt, and was refused as superseded — an
            # error for the one caller doing exactly what the key exists to
            # allow (DECIDE-L3 #4). Raising leaves the store byte-identical.
            if idempotency_key:
                prior = (item.metadata or {}).get("lease_report") or {}
                if prior.get("idempotency_key") == idempotency_key:
                    raise _AlreadyRecorded(item)
            carried = (item.metadata or {}).get("verifies")
            if carried:
                # ANY terminal report, not just DONE — the first version guarded
                # DONE alone and a FAILED report closed the vehicle just as well,
                # with routing then owing nothing further. And a parent in
                # `verify_failed` is owed a re-verify vehicle, which is the same
                # vehicle by another attempt; closing it is the same starvation.
                parent = self.get(carried)
                if parent is not None and parent.status in (
                    WorkStatus.AWAITING_VERIFY, WorkStatus.VERIFY_FAILED
                ):
                    raise Refusal(
                        code=gates.ATTESTATION_REQUIRED,
                        message=(
                            f"{item_id} is the vehicle for {carried}, which still "
                            f"needs a verifier — reporting the vehicle {status.value} "
                            f"answers nothing"
                        ),
                        remediation=(
                            f"Attest {carried} with your verdict; the vehicle retires "
                            f"itself once the item moves. A vehicle that could be closed "
                            f"by reporting it is one the executor could close on the "
                            f"way past, and the gate would then resolve on its clock "
                            f"with nobody having looked."
                        ),
                    )
            if item.lease_attempt != attempt:
                raise LeaseError(
                    f"refusing result for {item_id}: reported against lease "
                    f"attempt {attempt}, but the item is on attempt "
                    f"{item.lease_attempt} (holder {item.leased_by!r}). The "
                    f"lease this result came from is no longer current — the "
                    f"work was superseded, not lost."
                )
            if item.leased_by is None:
                raise LeaseError(
                    f"refusing result for {item_id}: nobody holds it. A report ends "
                    f"the lease it was issued under, and there is none — an item "
                    f"that was never claimed (or was reaped) has no executor, and a "
                    f"completion with no executor is one the separation check on a "
                    f"judged gate can never see. Claim it first."
                )
            if submitted_by is not None and submitted_by != item.leased_by:
                raise LeaseError(
                    f"refusing result for {item_id}: {submitted_by!r} is reporting "
                    f"an item held by {item.leased_by!r}. The fence number is public; "
                    f"the lease is not transferable by knowing it. Only the holder "
                    f"reports, or the recorded executor is somebody who never did "
                    f"the work."
                )
            if item.status in SETTLED:
                extra = (
                    " The only way out of a verify state is the gate answering "
                    "again — see Queue.attest."
                    if item.status in (WorkStatus.AWAITING_VERIFY, WorkStatus.VERIFY_FAILED)
                    else ""
                )
                raise LeaseError(
                    f"refusing result for {item_id}: it is already "
                    f"{item.status.value}. Reporting over a finished item would "
                    f"replace a recorded outcome with a later opinion of it.{extra}"
                )
            metadata = dict(item.metadata or {})
            metadata["lease_report"] = {
                "attempt": attempt,
                "reported_by": item.leased_by,
                "reported_at": _iso(_now()),
                "status": status.value,
                "idempotency_key": idempotency_key,
            }
            landed, record, failures = self._gate_outcome(
                item,
                status,
                attestation=attestation,
                submitted_by=submitted_by or item.leased_by,
                metadata=metadata,
                now=now,
            )
            if metadata.get("sop_ref"):
                # The third property's second record: written at the moment of
                # completion, while the executor and the context still exist —
                # never reconstructed later from a store that has forgotten
                # who held the lease.
                metadata[PLAN_VS_ACTUAL_KEY] = plan_vs_actual(
                    item, reported=status, landed=landed, result=result,
                    attempt=attempt, record=record, failures=failures, at=_iso(_now()),
                )
            return {
                "status": landed,
                "attestation": record,
                "verify_failures": failures,
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

        try:
            return self._mutate(item_id, fence)
        except _AlreadyRecorded as recorded:
            return recorded.item

    def annotate(self, item_id: str, metadata: dict, *, by_plane: bool = False) -> Optional[WorkItem]:
        """Merge keys into an item's metadata, changing nothing else.

        Deliberately narrow. It cannot touch status, lease, blockers or the
        gate — a general `update` is how a gate becomes editable by whoever
        holds the item, which is the tautology `attest` refuses one layer up.

        Nor the plane-owned keys, unless the caller IS the plane (`by_plane`,
        a Python keyword argument no transport reads off a payload): an
        `annotate` that could write `lease_report` would erase the recorded
        executor, and one that could write `adjudication` would forge the tag
        — the same holes `create` closes, reopened one method over.
        """
        if not by_plane:
            reject_reserved(metadata)

        def merge(item: WorkItem) -> dict:
            return {"metadata": {**(item.metadata or {}), **metadata}}

        return self._mutate(item_id, merge)

    def retire(self, item_id: str, result: str) -> Optional[WorkItem]:
        """Close an item nobody is working on, without it ever looking worked.

        For routing vehicles that have become moot. `report_result` was the
        wrong tool for this and the reason was invisible until measured: a
        report ENDS a lease, so it advances the fence — and `lease_attempt > 0`
        was how the verifier-presence report decided a vehicle had ever been
        claimed. Retiring vehicles through the report path therefore flipped a
        queue that no verifier had ever touched to "configured", which is the
        exact condition that report exists to catch.

        Refuses anything under a live lease. Somebody holding this item is
        somebody working it, and the routing pass does not get to take work out
        of a verifier's hands because its own view of the queue went stale
        between the read and the write.
        """

        def close(item: WorkItem) -> dict:
            if item.status in SETTLED:
                raise LeaseError(
                    f"refusing to retire {item_id}: it is already {item.status.value}."
                )
            if item.lease_active_at(_now()):
                raise LeaseError(
                    f"refusing to retire {item_id}: {item.leased_by!r} holds it until "
                    f"{item.lease_expires_at}. A live lease is somebody working; "
                    f"retiring under them would take the work out of their hands."
                )
            # The fence is NOT advanced. This item was never handed out, and the
            # count of times it was handed out has to stay true.
            return {"status": WorkStatus.DONE, "result": result,
                    "leased_by": None, "lease_expires_at": None}

        return self._mutate(item_id, close)

    def resolve_by_default(
        self,
        item_id: str,
        status: WorkStatus,
        resolution: dict,
    ) -> Optional[WorkItem]:
        """Close a gate on the clock rather than on a verdict. Never grants evidence.

        The park clock's only way to move an item, and separate from `attest` on
        purpose: `attest` records that somebody checked, and this records that
        nobody did and the gate said what to do about it. Sharing a path would
        make the two indistinguishable in the store a week later, and "verified"
        would come to mean "either verified or nobody looked".

        So `attestation` is untouched — an item resolved this way keeps whatever
        evidence it genuinely had, which for a parked judged gate is none.

        The record it writes under `RESOLUTION_KEY` is a claim about the LAST
        thing that settled this item, so `attest` moves it into `HISTORY_KEY`
        when a real verdict arrives afterwards. Nothing here has to know that;
        it is stated because the two functions jointly own the meaning of that
        key, and a reader of one of them should not have to find the other.
        """
        if status not in (WorkStatus.DONE, WorkStatus.VERIFY_FAILED):
            raise ValueError(
                f"a park clock resolves to done or verify_failed, got {status.value}"
            )

        def resolve(item: WorkItem) -> dict:
            if item.status is not WorkStatus.AWAITING_VERIFY:
                raise LeaseError(
                    f"refusing to resolve {item_id}: it is {item.status.value}, not "
                    f"awaiting a verdict. The clock runs on parked gates only — "
                    f"anything else has an answer already, and a default must not "
                    f"replace one."
                )
            metadata = {**(item.metadata or {}), RESOLUTION_KEY: resolution}
            updates: dict = {"status": status, "metadata": metadata}
            if status is WorkStatus.VERIFY_FAILED:
                updates["verify_failures"] = item.verify_failures + 1
                metadata["verify_retry"] = {
                    "failures": item.verify_failures + 1,
                    "decision": gates.retry_decision(item.verify_failures + 1),
                    "decided_at": _iso(_now()),
                }
                # The re-verify offer's clock starts when the park clock fired —
                # the sweep's own time, not this process's wall clock.
                metadata["verify_parked_at"] = resolution.get("resolved_at") or _iso(_now())
            return updates

        return self._mutate(item_id, resolve)

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
