"""The outbox — L1 of the participation ladder, and the only universal write path.

Every verb that matters lived behind a config line. A harness that configured
nothing received a spliced block and could do nothing with it: an audience, not
a participant. The honest description of that state is *read-agnostic for
everyone, write-agnostic for the configured*, and closing it is the difference
between a coordination layer and an MCP server with good primitives
(`docs/decisions/0002-participation-ladder.md`).

**The filesystem is the floor because it is the only substrate every coding
agent shares.** MCP is not universal — Cursor, Aider, Codex and a bespoke
in-house agent each speak it differently or not at all — but all of them can
write a file in the repo they are working in. So L1 is a file: append a line to
`.agentco/outbox.jsonl` and a local drainer signs it and publishes it.

**"Any tool may push. No tool may file."** The outbox carries the *push* set —
statements an agent makes about work it is doing — and not `work_create`.
Filing work into somebody's queue is an act with a consequence for other
people; reporting what you are doing is not, and the cheapest write path should
only carry the cheap acts.

**The outbox is local IPC, not a trust boundary.** The agent writes an
unauthenticated line; the drainer signs it with the machine credential. Anything
the line says about its own identity is carried as `agentLabel` and rendered
explicitly unverified, exactly as `leases.holder_attested` already does for a
claimed holder. A line that tries to set `actor` is refused rather than
believed — on this transport as on every other, the signature decides who acted.

**Receipts are not optional polish.** An outbox write is fire-and-forget, so
without a receipt the L1 experience is precisely *"I pushed and nothing
happened"* — which this project names as the most adoption-lethal outcome
available. The drainer writes `.agentco/receipts.jsonl` and the tier-1 splice
surfaces it, refusals included, because a colleague whose first three pushes are
silently dropped stops pushing and is right to.

**Delivery is at-least-once, and that is a choice with a reason.** Lines are
removed from the outbox only after they have a terminal receipt. A drainer that
dies between publishing and truncating will publish those lines again on the
next run, and `work_report` carries the line id as its idempotency key so that
retry is free. For `claim_scope` and `snapshot` a duplicate costs a redundant
row; the alternative ordering — truncate first, then publish — loses the write
outright on the same crash. A silently dropped claim is the failure the registry
exists to prevent, and a duplicate one is visible and harmless.

`attest` joined the push set when its endpoint shipped. The rule it had to pass
was never about the verb: a push set naming something the drainer cannot deliver
is a queue that fills with lines nobody can send, so a verb waits for its
transport and is refused with "not yet" until then. `sop_revise` and
`sop_activate` waited there too, and landed with Phase 4.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence

from agentco.errors import Refusal
from agentco.filelock import lock_exclusive, unlock

NODE_DIR_ENV_VAR = "AGENTCO_NODE_DIR"
DEFAULT_NODE_DIR = ".agentco"

OUTBOX_NAME = "outbox.jsonl"
RECEIPTS_NAME = "receipts.jsonl"
QUARANTINE_NAME = "outbox.quarantine.jsonl"
WATERMARK_NAME = "outbox.watermark.json"
DRAIN_LOCK_NAME = "drain.lock"

# The push set. `work_create` is deliberately absent — see the module docstring.
PUSH_VERBS: tuple[str, ...] = (
    "claim_scope",
    "release_scope",
    "snapshot",
    "work_report",
    # Joined the set when its transport shipped. It belongs here on the same
    # test as the rest: attesting is a statement about work you did, not an act
    # with a consequence for somebody who did not ask for it. A verifier
    # answering a judged gate through the zero-config floor is exactly the
    # participant L1 exists for.
    "attest",
    # Same test, same day-it-shipped rule. Judging a divergence is a statement
    # about work somebody did, from somebody who did not do it — and the
    # drainer signing with the machine credential means an adjudication relayed
    # from the executing machine is refused as self-adjudication, exactly as a
    # judged gate's attestation is. The reviewer is somewhere else, or it is
    # not a review.
    "adjudicate",
    # Landed with Phase 4, the last two names the participation ladder reserved.
    # A lesson learned on an unconfigured harness reaches the shared procedure
    # through the same file as everything else — as a DRAFT, authored by the
    # drainer's machine credential, so the revision policy applies to it
    # exactly as to any agent: a protected step is refused at the registry, and
    # nothing here activates unless a line says so and the policy agrees.
    "sop_revise",
    "sop_activate",
)

# Reserved for the push set, blocked on their transport rather than on a
# decision. Named here so that a line using one gets a refusal that says
# "not yet" instead of "never", which are different instructions to the caller.
# `attest` was the first entry and graduated; `sop_revise` and `sop_activate`
# followed with Phase 4. Empty now, kept as the mechanism for the next name.
PENDING_VERBS: tuple[str, ...] = ()

LINE_FIELDS = ("line_id", "at", "verb", "payload", "agent_label")

# Bounded on purpose. Receipts are read by a human through the tier-1 splice and
# by nothing else; an unbounded log of them is a file that grows forever to be
# read never. The most recent window is the part with any information in it.
RECEIPT_HISTORY = 200

# One line is a coordination event, not a document. A payload larger than this
# is a body being smuggled through a pointer-only plane, and the refusal is
# cheaper here than at the far end of an HTTP call.
MAX_LINE_BYTES = 64 * 1024

# A quarantine record keeps enough of the line to recognise it and not enough
# to become a second copy of the payload. The bad line is already preserved in
# full in the quarantine file; the receipt is a pointer to it.
MAX_RAW_EXCERPT = 200

OUTBOX_REFUSED = "outbox_line_invalid"
DRAIN_BUSY = "drain_in_progress"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_node_dir(path: Optional[str] = None) -> Path:
    """Argument, then `AGENTCO_NODE_DIR`, then `.agentco` in the cwd.

    The default is relative on purpose: the outbox belongs to the repository
    the agent is working in, and an absolute default would put every harness on
    one machine into a single shared file where one agent's malformed line
    becomes everyone's quarantine.
    """
    chosen = path or os.environ.get(NODE_DIR_ENV_VAR) or DEFAULT_NODE_DIR
    return Path(chosen).expanduser()


def validate_line(record: Any) -> dict:
    """Return the normalised line, or refuse. The write boundary for L1.

    Same rule as every other boundary here: refuse rather than ignore. A line
    the drainer silently skips is indistinguishable, from the agent's side, from
    a line that was delivered — and the agent has already exited by the time
    anyone could tell it otherwise.
    """
    if not isinstance(record, dict):
        raise Refusal(
            code=OUTBOX_REFUSED,
            message=f"an outbox line must be a JSON object, got {type(record).__name__}",
            remediation=(
                f"Write one JSON object per line with {list(LINE_FIELDS)}. "
                f"`agentco.outbox.Outbox.push` builds it for you."
            ),
        )

    unknown = sorted(set(record) - set(LINE_FIELDS))
    if unknown:
        raise Refusal(
            code=OUTBOX_REFUSED,
            message=f"unknown outbox line field(s) {unknown}",
            remediation=(
                f"The line's fields are {list(LINE_FIELDS)}; everything the "
                f"verb itself needs goes inside `payload`. An unread field at "
                f"this layer is a value the sender believes it sent."
            ),
        )

    verb = record.get("verb")
    if verb in PENDING_VERBS:
        raise Refusal(
            code=OUTBOX_REFUSED,
            message=f"{verb!r} is reserved for the push set but has no transport yet",
            remediation=(
                "Hold this line until the verb ships. It is not that the "
                "outbox refuses to carry it — there is nowhere for the drainer "
                "to send it, and a queue of undeliverable lines is worse than "
                "a refusal you can read."
            ),
        )
    if verb not in PUSH_VERBS:
        raise Refusal(
            code=OUTBOX_REFUSED,
            message=f"verb must be one of {list(PUSH_VERBS)}, got {verb!r}",
            remediation=(
                "Any tool may push; no tool may file. Reporting what you are "
                "doing goes through the outbox; creating work in somebody "
                "else's queue needs an authenticated surface (MCP or HTTP), "
                "because it has a consequence for a person who did not ask for "
                "it."
            ),
        )

    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise Refusal(
            code=OUTBOX_REFUSED,
            message=f"payload must be an object, got {type(payload).__name__}",
            remediation="Put the verb's arguments in `payload` as a JSON object.",
        )
    if "actor" in payload:
        raise Refusal(
            code=OUTBOX_REFUSED,
            message="payload sets `actor`",
            remediation=(
                "Remove it. The drainer signs with the machine credential and "
                "the signature decides the actor — here, over HTTP, and "
                "everywhere else. A self-reported name belongs in "
                "`agent_label`, which is carried and rendered as unverified."
            ),
        )

    line_id = record.get("line_id")
    if not isinstance(line_id, str) or not line_id.strip():
        raise Refusal(
            code=OUTBOX_REFUSED,
            message="line_id must be a non-empty string",
            remediation=(
                "Generate one per line and never reuse it. It is what makes a "
                "retry after a crash free rather than a duplicate."
            ),
        )

    at = record.get("at")
    if not isinstance(at, str) or not at.strip():
        raise Refusal(
            code=OUTBOX_REFUSED,
            message="at must be the timestamp the line was written",
            remediation="Set `at` to an ISO-8601 timestamp.",
        )

    label = record.get("agent_label")
    if label is not None and not isinstance(label, str):
        raise Refusal(
            code=OUTBOX_REFUSED,
            message=f"agent_label must be a string or absent, got {type(label).__name__}",
            remediation="Set `agent_label` to your harness's name, or omit it.",
        )

    return {
        "line_id": line_id.strip(),
        "at": at.strip(),
        "verb": verb,
        "payload": payload,
        "agent_label": label.strip() if isinstance(label, str) and label.strip() else None,
    }


def _why_unsendable(raw: bytes) -> str:
    """The specific reason, because "malformed" is not an instruction.

    An agent reading its own quarantine entry needs to know whether it wrote
    broken JSON, used a verb the outbox does not carry, or tried to name its own
    actor — three different mistakes with three different fixes.
    """
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return (
            f"not parseable as one JSON object ({type(exc).__name__}). A "
            f"truncated line usually means the process died mid-write; the "
            f"rest of the file was published normally."
        )
    try:
        validate_line(parsed)
    except Refusal as exc:
        return f"{exc.code}: {exc.message} — {exc.remediation}"
    return "unsendable for a reason this version cannot name"  # pragma: no cover


class Outbox:
    """The `.agentco/` node directory, and the two ends of the L1 path.

    Publishers call `push`. The drainer calls `claim_batch`, publishes, then
    `settle`. Both ends take the same advisory lock on a sidecar file, so an
    append during a drain is safe and neither end has to know about the other.
    """

    def __init__(self, node_dir: Path | str = DEFAULT_NODE_DIR):
        self.dir = Path(node_dir).expanduser()
        self.path = self.dir / OUTBOX_NAME
        self.receipts_path = self.dir / RECEIPTS_NAME
        self.quarantine_path = self.dir / QUARANTINE_NAME
        self.watermark_path = self.dir / WATERMARK_NAME
        self.drain_lock_path = self.dir / DRAIN_LOCK_NAME

    # -- locking ---------------------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Exclusive advisory lock on a sidecar, held across read-modify-write.

        Separate file from the data for the same reason `work.Queue` uses one:
        the atomic `os.replace` in `_rewrite` swaps the inode, and a lock held
        on the data file would be a lock on a file nobody is using any more.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(self.path.suffix + ".lock"), "a+") as handle:
            lock_exclusive(handle)
            try:
                yield
            finally:
                unlock(handle)

    @contextmanager
    def drain_lock(self) -> Iterator[bool]:
        """Yield True iff this process is the only drainer.

        Non-blocking on purpose. Two drainers is the ordinary shape of a cron
        job overlapping a slow run, and the right response is for the second one
        to do nothing and say so — not to queue up behind the first and then
        publish into a file it has already read.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        handle = open(self.drain_lock_path, "a+")
        try:
            try:
                if sys.platform == "win32":  # pragma: no cover - Windows only
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                unlock(handle)
        finally:
            handle.close()

    # -- the publisher's end ---------------------------------------------

    def push(
        self,
        verb: str,
        payload: dict,
        *,
        agent_label: Optional[str] = None,
        line_id: Optional[str] = None,
    ) -> dict:
        """Append one line. Validated here, so a bad push fails at the pusher.

        Returns the line as written, including its `line_id`, which is what a
        caller correlates against a receipt.
        """
        record = validate_line(
            {
                "line_id": line_id or f"ob_{uuid.uuid4().hex[:16]}",
                "at": _now_iso(),
                "verb": verb,
                "payload": payload,
                "agent_label": agent_label or os.environ.get("AGENTCO_AGENT_LABEL"),
            }
        )
        encoded = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
        if len(encoded) > MAX_LINE_BYTES:
            raise Refusal(
                code=OUTBOX_REFUSED,
                message=f"line is {len(encoded)} bytes, over the {MAX_LINE_BYTES} limit",
                remediation=(
                    "Push a pointer, not a body. This plane stores version "
                    "tokens and claims; a document belongs where it already "
                    "lives, and the snapshot verb records where that is."
                ),
            )
        with self._locked():
            # ONE `write` of a complete line, appended. A partial line can still
            # exist if the process dies inside this call, which is why the
            # drainer quarantines rather than fails — but nothing here writes a
            # line in two pieces and hopes.
            with open(self.path, "ab") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        return record

    # -- the drainer's end -----------------------------------------------

    def _read_raw(self) -> tuple[list[dict], list[bytes]]:
        """`(lines this version can act on, raw bytes it cannot)`.

        Read as BYTES and decoded per line, for the reason `work.Queue._read_raw`
        gives at length: decoding the whole file at once puts
        `UnicodeDecodeError` outside the per-line handler, so one stray byte
        from a truncated write costs every line in the file.
        """
        if not self.path.exists():
            return [], []
        good: list[dict] = []
        bad: list[bytes] = []
        for raw in self.path.read_bytes().split(b"\n"):
            if not raw.strip():
                continue
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                bad.append(raw)
                continue
            try:
                good.append(validate_line(parsed))
            except Refusal:
                bad.append(raw)
        return good, bad

    def _rewrite(self, records: Sequence[dict]) -> None:
        """Replace the outbox with exactly `records`, atomically.

        Same directory so the rename cannot cross a filesystem, fsync before it
        so a crash cannot leave a truncated file where a complete one was.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                for record in records:
                    handle.write((json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _append_jsonl(self, path: Path, rows: Sequence[dict], keep: Optional[int] = None) -> None:
        existing: list[dict] = []
        if keep is not None and path.exists():
            for raw in path.read_bytes().split(b"\n"):
                if not raw.strip():
                    continue
                try:
                    existing.append(json.loads(raw.decode("utf-8")))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
        merged = (existing + list(rows))[-keep:] if keep is not None else list(rows)
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                for row in merged:
                    handle.write((json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def claim_batch(self) -> tuple[list[dict], list[dict]]:
        """Quarantine what cannot be sent, and return what can.

        Quarantined lines are MOVED — written to `outbox.quarantine.jsonl` with
        the reason and removed from the outbox — rather than carried through the
        way `work.Queue` carries a corrupt row. The difference is that a work
        row is data somebody may still want; an unsendable outbox line is a
        message that will never be delivered, and leaving it in place means
        re-quarantining it on every drain forever. It is preserved, visible, and
        out of the way — and its receipt tells the agent that wrote it.

        The batch is NOT removed here. It is removed in `settle`, once each line
        has an outcome: see the module docstring on at-least-once.

        Returns `(sendable, quarantined)`. The second half is returned rather
        than counted afterwards from the file: an earlier version inferred it by
        diffing the receipt count before and after, which was always zero
        because quarantining does not write a receipt — a metric reported as 0
        by construction, which is worse than no metric.
        """
        with self._locked():
            good, bad = self._read_raw()
            quarantined = [
                {
                    "at": _now_iso(),
                    "reason": _why_unsendable(raw),
                    "raw": raw.decode("utf-8", errors="replace")[:MAX_RAW_EXCERPT],
                }
                for raw in bad
            ]
            if bad:
                self._append_jsonl(self.quarantine_path, quarantined, keep=RECEIPT_HISTORY)
                self._rewrite(good)
            self._write_watermark({"in_flight": [r["line_id"] for r in good]})
            return good, quarantined

    def settle(self, receipts: Sequence[dict]) -> None:
        """Remove settled lines, keep the rest, and write the receipts.

        A line is settled when it was published or refused — both are answers.
        A transport failure is not an answer, so those lines stay in the outbox
        and are retried on the next drain; the receipt says which happened, so
        "the registry is down" and "the registry said no" never look the same.

        Lines appended DURING the drain are preserved, because the file is
        re-read here under the lock rather than assumed to be the one that was
        claimed. That is the whole reason the batch is not removed up front.
        """
        settled = {r["line_id"] for r in receipts if r.get("state") in ("published", "refused")}
        with self._locked():
            good, bad = self._read_raw()
            if settled or bad:
                self._rewrite([r for r in good if r["line_id"] not in settled])
            if receipts:
                self._append_jsonl(self.receipts_path, list(receipts), keep=RECEIPT_HISTORY)
            counts: dict[str, int] = {}
            for receipt in receipts:
                state = str(receipt.get("state"))
                counts[state] = counts.get(state, 0) + 1
            self._write_watermark(
                {
                    "in_flight": [],
                    "last_drain_at": _now_iso(),
                    "last_drain": counts,
                    "pending": len([r for r in good if r["line_id"] not in settled]),
                }
            )

    # -- watermark and receipts ------------------------------------------

    def read_watermark(self) -> dict:
        if not self.watermark_path.exists():
            return {}
        try:
            return json.loads(self.watermark_path.read_text("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            # A watermark is an observation about the last run, never the
            # authority for what to send — the outbox file is that. So an
            # unreadable one degrades to "no observation" instead of stopping a
            # drain that would otherwise work.
            return {}

    def _write_watermark(self, update: dict) -> None:
        merged = {**self.read_watermark(), **update}
        merged["drained_total"] = merged.get("drained_total", 0)
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(json.dumps(merged, indent=2, sort_keys=True).encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.watermark_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def read_receipts(self, limit: int = RECEIPT_HISTORY) -> list[dict]:
        if not self.receipts_path.exists():
            return []
        rows = []
        for raw in self.receipts_path.read_bytes().split(b"\n"):
            if not raw.strip():
                continue
            try:
                rows.append(json.loads(raw.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        return rows[-limit:]

    def pending(self) -> list[dict]:
        with self._locked():
            good, _ = self._read_raw()
            return good


# --------------------------------------------------------------------------- #
# The drainer
# --------------------------------------------------------------------------- #


def registry_publisher(registry) -> Callable[[dict], dict]:
    """Map a push line onto the signed client. One place the verbs are wired.

    Takes anything with the `Registry` shape rather than the class, so a test
    can prove the drainer's own behaviour — ordering, settling, receipts, the
    crash windows — without an HTTP server standing in the way of the thing
    actually being tested.
    """

    def publish(record: dict) -> dict:
        verb = record["verb"]
        payload = dict(record["payload"])
        label = record.get("agent_label")
        if verb == "claim_scope":
            return registry.claim_scope(
                payload["repo"],
                payload["prefixes"],
                payload["intent"],
                agent_label=label,
            )
        if verb == "release_scope":
            return registry.release_scope(payload["leaseUid"], action=payload.get("action"))
        if verb == "snapshot":
            return registry.snapshot(
                payload["artifactUri"],
                payload["purpose"],
                agent_label=label,
            )
        if verb == "attest":
            return registry.attest(
                payload["itemId"],
                payload["attestation"],
                agent_label=label,
                capabilities=payload.get("capabilities"),
                adjudication=payload.get("adjudication"),
            )
        if verb == "adjudicate":
            return registry.adjudicate(
                payload["itemId"],
                payload["verdict"],
                payload["evidence"],
                agent_label=label,
            )
        if verb == "sop_revise":
            body = dict(payload.get("changes") or {})
            if payload.get("title") is not None:
                body["title"] = payload["title"]
            return registry.sop_revise(payload["sopId"], **body)
        if verb == "sop_activate":
            return registry.sop_activate(payload["sopId"], payload["version"])
        if verb == "work_report":
            return registry.work_report(
                payload["itemId"],
                payload["attempt"],
                payload["status"],
                result=payload.get("result"),
                # A gated item refuses a completion claim without this, and the
                # first version of this mapping dropped it on the floor — the
                # line carried the evidence, the wire did not, and the receipt
                # said "no attestation" about a push that had one. Every field
                # the push set accepts has to be forwarded here or the outbox
                # is a lossy copy of the API it fronts.
                attestation=payload.get("attestation"),
                # The line id IS the idempotency key. This is what makes the
                # at-least-once choice cheap: a drainer that died after the
                # HTTP call and before settling republishes, and the registry
                # returns the recorded outcome instead of a second one.
                idempotency_key=record["line_id"],
            )
        raise Refusal(  # pragma: no cover - validate_line refuses first
            code=OUTBOX_REFUSED,
            message=f"no transport for verb {verb!r}",
            remediation="Add it to `registry_publisher`, or drop it from PUSH_VERBS.",
        )

    return publish


def drain(outbox: Outbox, publish: Callable[[dict], dict]) -> dict:
    """Publish everything pending, once, and report what happened.

    The whole of the drainer's contract:

      * one drainer at a time, enforced by a lockfile rather than by hoping;
      * a refusal is terminal and carries the registry's own remediation into
        the receipt, because the agent that wrote the line is long gone and the
        receipt is the only place it can learn anything;
      * a transport failure is retried, and is never recorded as a refusal —
        "the registry said no" and "the registry was not there" prompt
        completely different responses from a person reading a receipt;
      * anything unexpected from a single line is caught per line. One bad line
        must not strand the rest of the batch, which is the same boundary the
        stores draw at the row.
    """
    with outbox.drain_lock() as acquired:
        if not acquired:
            return {
                "state": "skipped",
                "reason": DRAIN_BUSY,
                "detail": (
                    "another drainer holds the lock; this run did nothing. Two "
                    "drainers is an overlapping schedule, not an error."
                ),
                "published": 0,
                "refused": 0,
                "retryable": 0,
                "quarantined": 0,
            }

        batch, quarantined = outbox.claim_batch()
        # A quarantined line gets a receipt too. "An agent that writes garbage
        # must find out" is the whole point of quarantining rather than
        # dropping, and the quarantine FILE is not where anyone looks — the
        # receipts are what the tier-1 splice surfaces.
        receipts: list[dict] = [
            {
                "line_id": None,
                "verb": None,
                "at": entry["at"],
                "state": "quarantined",
                "detail": entry["reason"],
                "raw": entry["raw"],
            }
            for entry in quarantined
        ]
        for record in batch:
            base = {
                "line_id": record["line_id"],
                "verb": record["verb"],
                "at": _now_iso(),
                "agent_label": record.get("agent_label"),
            }
            try:
                result = publish(record)
            except Refusal as exc:
                receipts.append({**base, "state": "refused", **exc.to_dict()})
            except Exception as exc:  # noqa: BLE001 - transport, not logic
                refusal = getattr(exc, "payload", None)
                status = getattr(exc, "status", None)
                if isinstance(refusal, dict) and refusal.get("code") and status not in (None, 0):
                    # The client wraps a registry refusal in its own error type.
                    # It is still a refusal, and recording it as a transport
                    # failure would retry a line the registry will refuse every
                    # single time.
                    receipts.append({**base, "state": "refused", **refusal})
                else:
                    receipts.append(
                        {
                            **base,
                            "state": "retryable",
                            "detail": f"{type(exc).__name__}: {exc}",
                        }
                    )
            else:
                receipts.append({**base, "state": "published", "result": _thin(result)})

        outbox.settle(receipts)
        states = [r["state"] for r in receipts]
        return {
            "state": "drained",
            "published": states.count("published"),
            "refused": states.count("refused"),
            "retryable": states.count("retryable"),
            "quarantined": states.count("quarantined"),
            "receipts": receipts,
        }


def _thin(result: Any) -> Any:
    """Keep the identifiers and the outcome, drop the bodies.

    A receipt is read in a spliced context block, where every byte is paid for
    by every conversation in the repo. The lease uid or item id is what a caller
    correlates on; the rest of a response is the plane talking to itself.

    **The nested item's status is lifted out, and that is not tidiness.** A
    gated completion whose gate FAILED is a successful push — the line was
    delivered, the registry accepted it, and the work is now `verify_failed`.
    Without the status, that receipt reads `published` and the agent that wrote
    the line learns nothing: it pushed, something happened, and the thing that
    happened was its work being rejected. That is the same silence receipts
    exist to end, arriving through the one path nobody was watching.
    """
    if not isinstance(result, dict):
        return None
    keep = ("leaseUid", "uid", "id", "itemId", "seq", "status", "state")
    thinned = {k: result[k] for k in keep if k in result}
    item = result.get("item")
    if isinstance(item, dict):
        for field in ("id", "status", "verify_failures"):
            if item.get(field) is not None:
                thinned.setdefault(field, item[field])
    return thinned or None
