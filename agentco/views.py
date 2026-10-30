"""`GET /views/me` — the signer's lens on this hub (docs/decisions/0010).

One projection for every level. A person, a UI, the harness switchboard and an
MCP tool all read the same shape, so no level needs a reader of its own that
re-implements the wall. Who the view is FOR is the signature, never a query
parameter — the rule `authz.read_agent` already applies to a ready read
(ADR 0008 §6). A lens that took `?agent=` would be the unfiltered `GET /work`
again under another name.

Two panels today, both pure projections over the queue (no new store):

  * **askedOfMe** — open verification vehicles assigned to the signer: the
    gates waiting on them. This is the list the switchboard renders as
    `{label, ref, title, ageSeconds}` (ADR 0008 §1); the label is the
    partition's, never the hub's, so it is not here.
  * **mine** — open work assigned to the signer that is not a vehicle.

Terminal items are left out: a lens answers "what is mine to do", and a done
item is not. Rows are ordered oldest first, because the oldest open item is
the one most likely to be stranded, and that is the one a person should see
first. Notices and memory panels are later steps of 0010 §6.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from agentco import verifiers
from agentco.work import TERMINAL, Queue, WorkItem, _parse


def _age_seconds(item: WorkItem, at: datetime) -> int:
    created = _parse(item.created_at)
    if created is None:
        return 0
    return max(0, int((at - created).total_seconds()))


def _row(item: WorkItem, at: datetime) -> dict:
    return {
        "id": item.id,
        # The vehicle key for a gate (`verify:<item>:<attempt>`), else the
        # item's own natural key, else its id: what a caller can hand back.
        "ref": item.natural_key or item.id,
        "title": item.title,
        "status": item.status.value,
        "ageSeconds": _age_seconds(item, at),
    }


def lens(queue: Queue, *, viewer: str, now: Optional[datetime] = None) -> dict:
    """The signer's open items on this hub, split into the two panels.

    `viewer` is the authenticated actor. It is never widened to an owner's
    other actors here: a person who signs with one key per partition sees
    each partition's lens from that partition, which is 0008's design.
    """
    at = now or datetime.now(timezone.utc)
    asked: list[WorkItem] = []
    mine: list[WorkItem] = []
    for item in queue.list():
        if item.assigned_agent != viewer or item.status in TERMINAL:
            continue
        (asked if verifiers.is_vehicle(item) else mine).append(item)

    def rows(items: list[WorkItem]) -> list[dict]:
        return [_row(i, at) for i in sorted(items, key=lambda i: _age_seconds(i, at), reverse=True)]

    return {"viewer": viewer, "askedOfMe": rows(asked), "mine": rows(mine)}
