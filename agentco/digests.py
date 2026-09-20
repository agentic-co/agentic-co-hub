"""`POST /digests` — a child hub's cadence-boundary rollup, filed upward.

ADR 0005 ("Hubs can federate, and federation is optional") decided a parent
hub is just another registry a child connects to as a worker — the same
`Registry` a leaf agent already uses to talk to any hub, one level up. This
module is the write side of that: recording the summary a child hub already
produced (`agentco digest`) rather than re-deriving anything from the child's
raw events. The parent never sees a child's individual scope claims, work
items, or SOPs — only the text the child's own operator chose to publish at
its own cadence boundary, same as a person reading that digest today.

**This is a receipt, not a re-aggregation.** `divergence.collect`/`render_text`
already did the work of turning raw events into a summary on the CHILD's own
registry; shipping the raw events up here and re-summarising them at the
parent would mean the parent needs to understand every child's event shape,
which is the exact coupling a rollup is supposed to avoid. The parent stores
the rendered text plus whatever structured `meta` the child chose to attach.

**No new read surface.** The parent reads received digests through the
existing `GET /events` feed (kind `DigestReceived`) — the same cursor every
other subscriber already uses, so a company-level dashboard is "read the
feed", not a second protocol to learn.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from agentco import events
from agentco.errors import Refusal


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def receive(
    conn: sqlite3.Connection,
    *,
    actor: str,
    text: str,
    generated_at: Optional[str] = None,
    meta: Optional[dict[str, Any]] = None,
    agent_label: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Record one child hub's digest. Returns the receipt.

    `actor` is the authenticated signer — the child hub's own `keygen`'d
    identity — never a name the body supplies (`_handle` already enforces
    this at the transport, the same rule every other verb here follows).
    `generated_at` is advisory, the child's own clock; `occurred_at` on the
    stored event is this registry's own clock, because federation crossing a
    trust boundary is exactly the case where two clocks are worth keeping
    distinct rather than trusting the sender's.
    """
    if not (text or "").strip():
        raise Refusal(
            code="text_required",
            message="a digest must carry the text a person or dashboard would read",
            remediation=(
                "Send 'text' — the rendered digest body (`divergence.render_text`'s "
                "output, or your own). An empty digest is indistinguishable from a "
                "delivery failure, which is the one confusion this endpoint exists "
                "to rule out."
            ),
        )
    at = now or datetime.now(timezone.utc)
    payload: dict[str, Any] = {"text": text.strip(), "generatedAt": generated_at}
    if meta:
        payload["meta"] = meta

    record = events.append(
        conn,
        kind="DigestReceived",
        actor=actor,
        agent_label=agent_label,
        occurred_at=_iso(at),
        payload=payload,
    )
    return {
        "state": "accepted",
        "eventId": record["uid"],
        "seq": record["seq"],
        "receivedAt": record["occurredAt"],
    }
