"""`POST /scope-claims` — the concurrency primitive, and the political one.

The claim this rests on: "**Making concurrency visible is ~90% of the value and 0%
of the political cost** — but only if the visibility is precise." The
precision is the scope-model decision's scope model (`scope.py`); this module is the lease
lifecycle around it.

Two properties inherited verbatim from the design and easy to lose:

**Advisory for humans.** A conflict is reported, never enforced. Stage 1b has
no actuation to refuse, and a registry that blocked a colleague's work on
first contact would be uninstalled the same week. `ScopeConflict` is
information delivered to two people; what they do with it is theirs. (D-25:
advisory for humans, mandatory for plane-run machines — and there are no
plane-run machines until stage 4.)

**`holder` must equal the authenticated actor, or the lease is `holderAttested`
and cannot block anything** (docs/architecture.md). Otherwise a client claims a lease in
another human's name and blocks work on their scope. The attested form is
kept rather than refused because an agent legitimately claims on behalf of
its principal — it is recorded as a weaker claim, not rejected as a lie.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from agentco import events
from agentco.errors import Refusal
from agentco.scope import Scope, find_conflicts

DEFAULT_TTL_S = 8 * 3600  # one working day; a lease nobody renews should lapse
MAX_TTL_S = 14 * 24 * 3600


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def live_leases(
    conn: sqlite3.Connection,
    repo: str,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Every unreleased, unexpired lease on one repo.

    Expiry is evaluated in the QUERY rather than by a sweeper job, so a lapsed
    lease stops conflicting the moment it lapses even if nothing has run. A
    sweeper that fell over would otherwise leave phantom leases blocking
    nobody's work but generating everybody's conflicts — noise, which is the
    documented failure mode for this exact feature.
    """
    at = now or datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT * FROM leases WHERE repo = ? AND released_at IS NULL", (repo,)
    ).fetchall()
    out = []
    for r in rows:
        if _parse(r["expires_at"]) <= at:
            continue
        out.append(
            {
                "uid": r["uid"],
                "holder": r["holder"],
                "holderAttested": bool(r["holder_attested"]),
                "repo": r["repo"],
                "prefixes": json.loads(r["prefixes"]),
                "intent": r["intent"],
                "claimedAt": r["claimed_at"],
                "expiresAt": r["expires_at"],
            }
        )
    return out


def claim(
    conn: sqlite3.Connection,
    *,
    actor: str,
    repo: str,
    prefixes: list[str],
    intent: str,
    holder: Optional[str] = None,
    ttl_seconds: int = DEFAULT_TTL_S,
    now: Optional[datetime] = None,
) -> dict:
    """Open a `ScopeLease`. Returns the lease plus any conflicts it revealed.

    Conflicts are computed BEFORE the insert and reported in the response, so
    the caller learns about the overlap in the same round trip that created
    the lease. Making them fetch the feed to discover their own conflict is
    the difference between a tool that answers "is anyone else in here?" and
    one that eventually tells you.
    """
    from agentco.scope import validate_intent

    at = now or datetime.now(timezone.utc)
    scope = Scope.parse(repo, prefixes)
    checked_intent = validate_intent(intent)

    if ttl_seconds <= 0 or ttl_seconds > MAX_TTL_S:
        raise Refusal(
            code="bad_ttl",
            message=f"ttlSeconds must be between 1 and {MAX_TTL_S}",
            remediation=(
                f"Claim for the work you are about to do — the default is "
                f"{DEFAULT_TTL_S}s (one working day) and renewing is cheap. A "
                f"lease longer than {MAX_TTL_S}s is a fence, not a claim."
            ),
        )

    # docs/architecture.md — an unattested holder claim cannot block plane-run actuation.
    claimed_holder = (holder or actor).strip() or actor
    attested = claimed_holder != actor

    existing = live_leases(conn, scope.repo, at)
    conflicts = find_conflicts(
        scope,
        claimed_holder,
        [(l["holder"], Scope(l["repo"], tuple(l["prefixes"])), l["intent"]) for l in existing],
    )

    uid = f"lease_{uuid.uuid4().hex[:16]}"
    expires = at + timedelta(seconds=ttl_seconds)
    with conn:
        conn.execute(
            "INSERT INTO leases(uid, holder, holder_attested, repo, prefixes, intent, "
            "claimed_at, expires_at, released_at) VALUES (?,?,?,?,?,?,?,?,NULL)",
            (
                uid,
                claimed_holder,
                1 if attested else 0,
                scope.repo,
                json.dumps(list(scope.prefixes)),
                checked_intent,
                _iso(at),
                _iso(expires),
            ),
        )

    events.append(
        conn,
        kind="ScopeClaimed",
        actor=actor,
        repo=scope.repo,
        occurred_at=_iso(at),
        payload={
            "leaseUid": uid,
            "holder": claimed_holder,
            "holderAttested": attested,
            "prefixes": list(scope.prefixes),
            "intent": checked_intent,
            "expiresAt": _iso(expires),
        },
    )

    for conflict in conflicts:
        events.append(
            conn,
            kind="ScopeConflict",
            actor=actor,
            repo=scope.repo,
            occurred_at=_iso(at),
            payload={
                "leaseUid": uid,
                "holder": claimed_holder,
                # the scope-model decision (docs/decisions/0001): carry BOTH intents so 'prototype vs implement' reads
                # differently from 'implement vs implement'.
                "myIntent": checked_intent,
                **conflict,
            },
        )
        with conn:
            conn.execute(
                "INSERT INTO conflict_actions(lease_uid, with_holder, fired_at) VALUES (?,?,?)",
                (uid, conflict["withHolder"], _iso(at)),
            )

    return {
        "leaseUid": uid,
        "state": "accepted",
        "holder": claimed_holder,
        "holderAttested": attested,
        "repo": scope.repo,
        "prefixes": list(scope.prefixes),
        "intent": checked_intent,
        "expiresAt": _iso(expires),
        "conflicts": conflicts,
        # Advisory, always — stated in the payload so a client author reads it
        # without having to find the design doc.
        "enforcement": "advisory",
    }


def release(
    conn: sqlite3.Connection,
    *,
    actor: str,
    lease_uid: str,
    action: str = "released",
    now: Optional[datetime] = None,
) -> dict:
    """Close a lease, and record whether a reported conflict changed behaviour.

    `action` feeds the scope-model decision's precision metric — "conflicts fired ÷ conflicts a
    holder acted on". Without it the registry can report how many conflicts it
    fired but not whether any of them were worth firing, which is the number
    that decides whether `k` is right. Defaulting to `released` (rather than
    to an optimistic `narrowed`) keeps the metric honest when a client does
    not supply one.
    """
    at = now or datetime.now(timezone.utc)
    row = conn.execute("SELECT * FROM leases WHERE uid = ?", (lease_uid,)).fetchone()
    if row is None:
        raise Refusal(
            code="no_such_lease",
            message=f"no lease {lease_uid!r}",
            remediation="Use the 'leaseUid' returned by POST /scope-claims.",
            http_status=404,
        )
    if row["released_at"] is not None:
        return {"leaseUid": lease_uid, "state": "duplicate", "releasedAt": row["released_at"]}
    if row["holder"] != actor:
        raise Refusal(
            code="not_the_holder",
            message=f"lease {lease_uid!r} is held by another identity",
            remediation=(
                "Only the holder releases a lease. If it is stale, wait for it to "
                "expire or ask the holder — releasing another person's claim is the "
                "behaviour this registry exists to make visible, not to enable."
            ),
            http_status=403,
        )

    with conn:
        conn.execute("UPDATE leases SET released_at = ? WHERE uid = ?", (_iso(at), lease_uid))
        conn.execute(
            "UPDATE conflict_actions SET acted_at = ?, action = ? "
            "WHERE lease_uid = ? AND acted_at IS NULL",
            (_iso(at), action, lease_uid),
        )

    events.append(
        conn,
        kind="ScopeReleased",
        actor=actor,
        repo=row["repo"],
        occurred_at=_iso(at),
        payload={"leaseUid": lease_uid, "action": action},
    )
    return {"leaseUid": lease_uid, "state": "accepted", "releasedAt": _iso(at)}


def conflicts_for(conn: sqlite3.Connection, actor: str, now: Optional[datetime] = None) -> list[dict]:
    """Every conflict `actor`'s OWN live leases have with someone else's, right now.

    A READ, not a claim — this exists for tier-1 context injection (agentco/inject.py),
    which needs to tell a harness at session start "here is what is still live and
    colliding", without re-running the claim path to find out. It deliberately does
    NOT insert into `conflict_actions`: that table counts conflicts FIRED at the
    moment a claim revealed them, for the scope-model decision's own precision
    self-audit (conflicts fired ÷ acted on). Re-surfacing an already-known conflict
    on every scheduled injection run would silently inflate that denominator and
    make the precision metric measure how often this function runs, not how often a
    claim actually collided with one.

    Reuses the exact same query (`live_leases`) and intersection rule
    (`scope.find_conflicts`) the claim path uses, so "conflicting" means the same
    thing here as it does at claim time — this is a second reader, not a second
    definition.
    """
    at = now or datetime.now(timezone.utc)
    repos = [
        row["repo"]
        for row in conn.execute("SELECT DISTINCT repo FROM leases WHERE released_at IS NULL").fetchall()
    ]
    out: list[dict] = []
    for repo in repos:
        live = live_leases(conn, repo, at)
        mine = [lease for lease in live if lease["holder"] == actor]
        if not mine:
            continue
        others = [
            (lease["holder"], Scope(lease["repo"], tuple(lease["prefixes"])), lease["intent"])
            for lease in live
            if lease["holder"] != actor
        ]
        for lease in mine:
            candidate = Scope(lease["repo"], tuple(lease["prefixes"]))
            for conflict in find_conflicts(candidate, actor, others):
                out.append({"repo": repo, "myLeaseUid": lease["uid"], "myIntent": lease["intent"], **conflict})
    return out
