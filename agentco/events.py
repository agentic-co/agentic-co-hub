"""The change feed — the most valuable verb in the whole surface.

Cursor, not timestamp. 05-PROTOCOL-SURFACE.md docs/architecture.md states the discipline for
`/v1/events` and it is inherited verbatim here: the cursor is **opaque,
monotonic and resumable**, so "a harness that has been off for a month gets
everything it missed rather than a window."

Why opaque matters concretely: a timestamp cursor loses events written inside
the same clock tick, and — worse — invites clients to do arithmetic on it
("give me yesterday"), which quietly turns a resumable feed into a windowed
query. The moment one client does that, changing the storage is a breaking
change. Base64 over `v1:<seq>` costs nothing and forecloses it.
"""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from agentco.errors import Refusal

CURSOR_PREFIX = "v1:"
DEFAULT_LIMIT = 200
MAX_LIMIT = 500

# The event kinds stage 1b emits. Closed set: an unknown kind reaching the
# feed is a bug in a writer, and a subscriber that has to guess at kinds
# cannot be written against a contract.
KINDS = (
    "ScopeClaimed",
    "ScopeReleased",
    "ScopeConflict",
    "SnapshotTaken",
    "DivergenceObserved",
    # Work-queue events, added because their absence was the real reason a
    # parked human gate reached nobody. The feed is how everything else here
    # reaches a harness — the tier-1 splice, the session hook, the digest sender
    # all read it — and a work item entering `awaiting_verify` emitted nothing
    # at all, so no tier could surface it even in principle. The missing piece
    # was the substrate, not the choice of channel.
    "WorkParked",
    "GateEscalated",
    # The pulse's own heartbeat (`pulse.py`): one row per `agentco pulse --apply`,
    # attributed to the plane. Its AGE is what the next run and the session hook
    # judge — a pass that records nothing when it crashes leaves the gap as the
    # signal, which is the one property worth keeping from a heartbeat file.
    "PulseObserved",
    # A child hub's cadence-boundary digest, filed by `POST /digests` (ADR
    # 0005). Federation reuses THIS feed rather than a parallel one — a parent
    # hub reading `GET /events` already gets a per-team rollup for free, on
    # the same cursor every other subscriber uses. No new read surface, one
    # new write.
    "DigestReceived",
    # ------------------------------------------------------------------ #
    # The procedure's own lifecycle, added for the same reason the work-queue
    # kinds above were: the absence was the substrate, not a channel choice.
    #
    # A harness has two standing questions — is there work for me, and have the
    # procedures changed — and until these kinds existed the feed could answer
    # neither. So every harness asked the LIBRARY and the QUEUE directly, on a
    # timer, and both answer by reading everything they hold (`SopLibrary`
    # reads all records; the queue's `ready()` reads the whole table and filters
    # in Python). The scans are not a careless query. They are what a subscriber
    # is forced into when the feed cannot tell it that nothing has changed.
    #
    # With these, a harness polls ONE cursor and touches the expensive paths
    # only when an event says there is a reason to.
    #
    # `AsopVersioned` — a new version exists, whether that is version 1 or the
    # tenth revision. Deliberately one kind rather than Created/Revised: a
    # subscriber's question is "is there a version I have not read", and the
    # ordinal it starts at is not a different event.
    "AsopVersioned",
    # `AsopActivated` — a version became the one every reader gets by default.
    # THE event for synchronisation: a draft changes nothing a harness executes,
    # an activation changes what every future run is pinned to.
    "AsopActivated",
    # `AsopRetired` — withdrawn with no successor. A harness holding it cached
    # must stop offering it; runs already pinned to it stay resolvable, which is
    # why this is an announcement and not a deletion.
    "AsopRetired",
    # `WorkFiled` — an item entered the queue, carrying `requires`,
    # `assignedAgent` and `blockedBy` so a subscriber can decide whether to pull
    # WITHOUT pulling. A kind that forced a pull to find out would move the scan
    # rather than remove it.
    #
    # FILED, not "available", and the word is chosen against the nicer one: a
    # step of a run arrives blocked on its predecessors, and announcing it as
    # available would offer work nothing can claim. `blockedBy` is how a
    # subscriber tells the difference, and an empty one means claim it now.
    #
    # What this kind does NOT yet carry is the moment an item becomes unblocked
    # because its predecessor finished. Emitting that needs the successor set,
    # which nothing stores — `ready()` recomputes blockers on every read. Until
    # it exists a harness waiting on step two still learns from its own next
    # poll, which is the behaviour it has today; this kind removes the scan for
    # everything else and is honest about the case it does not close.
    #
    # `queue.create` returns the EXISTING item for a duplicate natural key, so
    # this can repeat for one item id. Harmless by construction: no clock starts
    # on it, unlike `WorkParked`, so a repeat cannot restart one.
    "WorkFiled",
)

# Events the PLANE observes rather than an actor performing. A reserved name
# rather than borrowing whoever happened to run the pass: attributing a
# routing observation to a person would put their name on a row they did not
# cause, and adoption metrics count actors.
PLANE_ACTOR = "agentco"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode_cursor(seq: int) -> str:
    return base64.urlsafe_b64encode(f"{CURSOR_PREFIX}{seq}".encode()).decode().rstrip("=")


def decode_cursor(cursor: Optional[str]) -> int:
    """Cursor → seq. Absent means "from the beginning"; malformed is REFUSED.

    Refused rather than silently reset to 0: a client that presents a corrupt
    cursor and is handed the whole feed from the start will replay months of
    events and conclude the feed is broken. Refusing names the fault at the
    layer it occurred.
    """
    if cursor is None or cursor == "":
        return 0
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise Refusal(
            code="bad_cursor",
            message=f"cursor {cursor!r} is not a cursor this registry issued",
            remediation=(
                "Drop the cursor to resume from the beginning of the feed, or use the "
                "'nextCursor' value from your last successful GET /events verbatim — "
                "cursors are opaque and must not be constructed or edited."
            ),
            http_status=400,
        )
    if not raw.startswith(CURSOR_PREFIX):
        raise Refusal(
            code="bad_cursor",
            message=f"cursor {cursor!r} is not a cursor this registry issued",
            remediation=(
                "Use the 'nextCursor' value from your last successful GET /events "
                "verbatim, or omit it to start from the beginning."
            ),
            http_status=400,
        )
    try:
        return int(raw[len(CURSOR_PREFIX) :])
    except ValueError:
        raise Refusal(
            code="bad_cursor",
            message=f"cursor {cursor!r} carries a non-numeric position",
            remediation="Omit the cursor to resume from the beginning of the feed.",
            http_status=400,
        )


def append(
    conn: sqlite3.Connection,
    *,
    kind: str,
    actor: str,
    payload: dict[str, Any],
    repo: Optional[str] = None,
    occurred_at: Optional[str] = None,
    agent_label: Optional[str] = None,
) -> dict:
    """Append one event. Returns the stored row (including its `seq`).

    Callers pass an already-validated `kind`; the assert-style check here is
    a writer-side guard, not input validation — an unknown kind means a code
    path invented one, which the closed subscriber contract must not absorb.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown event kind {kind!r} (known: {', '.join(KINDS)})")
    uid = f"evt_{uuid.uuid4().hex[:16]}"
    at = occurred_at or now_iso()
    body = json.dumps(payload, sort_keys=True)
    with conn:
        cur = conn.execute(
            "INSERT INTO events(uid, kind, actor, agent_label, repo, occurred_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uid, kind, actor, agent_label, repo, at, body),
        )
    return {
        "seq": cur.lastrowid,
        "uid": uid,
        "kind": kind,
        "actor": actor,
        # Rendered as a sibling of `actor`, never merged into it. A consumer
        # that wants to attribute an event to a harness must reach for a
        # differently-named key and can see, at the point of use, that it is
        # reaching for the unverified one.
        "agentLabel": agent_label,
        "agentLabelVerified": False,
        "repo": repo,
        "occurredAt": at,
        "payload": payload,
        "cursor": encode_cursor(cur.lastrowid),
    }


def read(
    conn: sqlite3.Connection,
    *,
    since: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
    kind: Optional[str] = None,
) -> dict:
    """`GET /events?since=` — events strictly after the cursor, plus the next one.

    `nextCursor` is the seq of the LAST ROW RETURNED, never the highest seq in
    the table. Returning the table maximum would silently skip anything a
    concurrent writer committed between the SELECT and the response, which is
    the classic feed-loses-events bug and is invisible until someone audits.

    When the page is empty the caller's own cursor is echoed back, so polling
    an idle feed is a no-op rather than a reset.
    """
    start = decode_cursor(since)
    bounded = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    sql = "SELECT * FROM events WHERE seq > ?"
    args: list[Any] = [start]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    sql += " ORDER BY seq ASC LIMIT ?"
    args.append(bounded)

    rows = conn.execute(sql, args).fetchall()
    events = [
        {
            "seq": r["seq"],
            "uid": r["uid"],
            "kind": r["kind"],
            "actor": r["actor"],
            "agentLabel": r["agent_label"],
            "agentLabelVerified": False,
            "repo": r["repo"],
            "occurredAt": r["occurred_at"],
            "payload": json.loads(r["payload"]),
        }
        for r in rows
    ]
    next_seq = rows[-1]["seq"] if rows else start
    return {
        "events": events,
        "nextCursor": encode_cursor(next_seq),
        "count": len(events),
    }


def announcer(conn) -> "Callable[[str, str, dict], None]":
    """A callable a store can hold to put its writes on this feed.

    The inversion exists so that `sop.py` and `work.py` never import this
    module: storage that reached for a registry connection would be storage
    that knows about a feed, and those are separate jobs. The plane builds one
    of these and hands it over; the store calls it and does not know what it is.

    Whoever builds a plane wires this ONCE, for every transport, because the
    announcement belongs to the operation rather than the route that carried it.
    """
    def announce(kind: str, actor: str, payload: dict) -> None:
        append(conn, kind=kind, actor=actor, payload=payload)
    return announce
