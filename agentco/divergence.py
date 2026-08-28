"""The divergence digest — delivered at the cadence boundary, deliberately.

The delivery rule, and the emphasis is the feature:

    **Real-time divergence pings are exactly what the people who need this
    are already drowning in.** Divergence accumulates and is delivered at a
    cadence boundary the team chooses — a daily stand-up batch, a weekly
    review. **The gating *is* the product.**

So `snapshots.check_all` accumulates and this module delivers. Nothing in
stage 1b sends a message the moment a hash changes, and a future contributor
who "improves" it by doing so has removed the reason the feature is wanted.

The design also states the honest limits, and the digest prints them rather
than hiding them:

  * "no snapshot, no signal" — it covers only artifacts someone snapshotted.
  * it needs a stable URI plus a cheap version token; a local prototype on a
    laptop has neither, "and no design fixes that".
  * it detects THAT something moved, not WHETHER it matters.

The third is why the digest never uses the word "stale" and never ranks. It
reports movement and hands the judgement to the human, which is where docs/architecture.md
puts it.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from agentco import events, snapshots


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def collect(conn: sqlite3.Connection, now: Optional[datetime] = None) -> dict:
    """Run the checks and gather everything the digest needs. No delivery."""
    at = now or datetime.now(timezone.utc)
    moved = snapshots.check_all(conn, at)

    unresolvable = conn.execute(
        "SELECT uid, actor, artifact_uri, purpose FROM snapshots "
        "WHERE expires_at > ? AND delivered_at IS NULL AND (content_hash = '' OR content_hash IS NULL)",
        (_iso(at),),
    ).fetchall()

    tracked = conn.execute(
        "SELECT COUNT(*) AS n FROM snapshots WHERE expires_at > ?", (_iso(at),)
    ).fetchone()["n"]

    return {
        "generatedAt": _iso(at),
        "tracked": tracked,
        "moved": moved,
        "unresolvable": [
            {
                "snapId": r["uid"],
                "actor": r["actor"],
                "artifactUri": r["artifact_uri"],
                "purpose": r["purpose"],
            }
            for r in unresolvable
        ],
    }


def render_text(digest: dict) -> str:
    """Plain text. Leads with the count, then the honest line, then the items.

    Ordering matters: `ado_report`'s verification found that a partial read
    presented after content reads as complete. The same discipline applies to
    a digest whose coverage has a hole — the count of pointers that CANNOT
    fire goes above the list of the ones that did, not in a footnote.
    """
    lines: list[str] = []
    moved = digest["moved"]
    unresolvable = digest["unresolvable"]

    lines.append(
        f"Divergence digest — {len(moved)} of {digest['tracked']} tracked pointer(s) moved"
    )
    lines.append(f"  generated: {digest['generatedAt']}")

    if unresolvable:
        lines.append("")
        lines.append(
            f"⚠ {len(unresolvable)} pointer(s) CANNOT report divergence yet — "
            f"they are tracked but unreadable, so absence of a notice below is not "
            f"evidence they have not changed:"
        )
        for item in unresolvable:
            lines.append(f"    · {item['artifactUri']} — {item['purpose']} ({item['actor']})")

    lines.append("")
    if not moved:
        lines.append("No tracked pointer changed since the last digest.")
        return "\n".join(lines)

    for item in moved:
        lines.append(f"  · {item['purpose']}")
        lines.append(f"      {item['artifactUri']}")
        lines.append(
            f"      snapshotted {item['snapshotHash'][:12]} → now {item['currentHash'][:12]}"
        )
        lines.append(f"      snapshotted by: {item['actor']}   re-snapshot to re-baseline")

    return "\n".join(lines)


def deliver(
    conn: sqlite3.Connection,
    digest: dict,
    *,
    mark_delivered: bool = True,
) -> int:
    """Emit one `DivergenceObserved` event per moved pointer. Returns the count.

    Events, not messages. The feed is the delivery mechanism a subscriber
    reads; a Teams post is one subscriber (`cli.py --post`), never the record.
    Marking `delivered_at` is what stops the same movement being reported at
    every subsequent cadence boundary forever — the pointer stays tracked, but
    THIS divergence has been said once.
    """
    at = digest["generatedAt"]
    for item in digest["moved"]:
        events.append(
            conn,
            kind="DivergenceObserved",
            actor=item["actor"],
            occurred_at=at,
            payload={
                "snapId": item["snapId"],
                "artifactUri": item["artifactUri"],
                "purpose": item["purpose"],
                "snapshotHash": item["snapshotHash"],
                "currentHash": item["currentHash"],
            },
        )
        if mark_delivered:
            with conn:
                conn.execute(
                    "UPDATE snapshots SET delivered_at = ? WHERE uid = ?",
                    (at, item["snapId"]),
                )
    return len(digest["moved"])
