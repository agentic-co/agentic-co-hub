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

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from agentco import events
from agentco.errors import Refusal

#: Comma-separated actor names this hub accepts a digest FROM. Legacy name
#: kept for the same reason `ASOP_VERIFIERS`/`AGENTCO_VERIFIERS` are paired
#: (`policy.py`): a deployment that already sets one spelling keeps working.
#:
#: **This declaration fails CLOSED, unlike `humans`/`verifiers`.** Those two
#: have a documented low-stakes fallback when undeclared (self-asserted,
#: routing hygiene only). Federation does not: the whole point of ADR 0005's
#: recursion is that a parent's registry size scales with the number of
#: DECLARED CHILDREN, never with the number of correctly-signed actors that
#: happen to exist — an empty declaration that still accepted every signed
#: caller would let any ordinary leaf agent pose as a team hub and defeats
#: the mechanism the ADR argues for. So: nobody declared means nobody may
#: file a digest, and `POST /digests` says exactly that in its remediation.
FEDERATORS_ENV_VAR = "ASOP_FEDERATED_CHILDREN"
LEGACY_FEDERATORS_ENV_VAR = "AGENTCO_FEDERATED_CHILDREN"


def _declared_env(name: str, legacy: str) -> Optional[str]:
    found = os.environ.get(name)
    return found if found is not None else os.environ.get(legacy)


def federators_from_env(value: Optional[str] = None) -> frozenset[str]:
    """The actors this hub declared it will accept a digest from. Comma-separated."""
    raw = value if value is not None else _declared_env(FEDERATORS_ENV_VAR, LEGACY_FEDERATORS_ENV_VAR)
    if not raw:
        return frozenset()
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


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
    declared_federators: Optional[frozenset[str]] = None,
) -> dict:
    """Record one child hub's digest. Returns the receipt.

    `actor` is the authenticated signer — the child hub's own `keygen`'d
    identity — never a name the body supplies (`_handle` already enforces
    this at the transport, the same rule every other verb here follows).
    `generated_at` is advisory, the child's own clock; `occurred_at` on the
    stored event is this registry's own clock, because federation crossing a
    trust boundary is exactly the case where two clocks are worth keeping
    distinct rather than trusting the sender's.

    `declared_federators` is injected the same way `create_app` injects
    `humans`/`verifiers` — so a caller that already has the set in hand does
    not also have to export the env var for this function to agree with it.
    Unset, it is read from `ASOP_FEDERATED_CHILDREN`/`AGENTCO_FEDERATED_CHILDREN`.
    """
    federators = (
        declared_federators if declared_federators is not None else federators_from_env()
    )
    if actor not in federators:
        raise Refusal(
            code="not_a_declared_federator",
            message=f"{actor!r} is not declared as a child this hub accepts a digest from",
            remediation=(
                f"An operator must add {actor!r} to {FEDERATORS_ENV_VAR} before this "
                f"hub will record its digests. This fails closed on purpose — see "
                f"digests.py's module docstring: ADR 0005's scaling argument only "
                f"holds if the parent's registry grows with declared children, not "
                f"with every actor that happens to hold a valid signature."
            ),
        )
    if not isinstance(text, str) or not text.strip():
        raise Refusal(
            code="text_required",
            message="a digest must carry the text a person or dashboard would read, as a string",
            remediation=(
                "Send 'text' as a string — the rendered digest body "
                "(`divergence.render_text`'s output, or your own). An empty or "
                "non-string digest is indistinguishable from a delivery failure, "
                "which is the one confusion this endpoint exists to rule out."
            ),
        )
    if meta is not None and not isinstance(meta, dict):
        raise Refusal(
            code="meta_must_be_object",
            message=f"'meta' must be a JSON object if sent, got {type(meta).__name__}",
            remediation=(
                "Send 'meta' as a JSON object of your own fields (e.g. "
                "{\"scopesClosed\": 3}), or omit it entirely. It is meant to be "
                "structured — a string or list defeats the purpose of shipping it "
                "separately from 'text'."
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
