"""Run ownership: one owner per run, a pack reference, and the recorded administrative transfer.

ASOP §7.2, as amended for spec 0.6.1: ownership is recorded on the run when it
is filed and **no run verb moves it** — but a **recorded administrative
transfer** may (owner decision 2026-10-18: "A, but must have an admin overwrite
if needed and the owner isn't available or doesn't work at the company
anymore").

**The owner is a store.** A run filed through this hub is owned by this hub
(`AGENTCO_HUB_ID`). A runtime that owns its own runs names itself at filing
(`ownerStore`), and only if the signing actor speaks for that store
(`AGENTCO_STORES`). The record lives on the run's parent bead:

    run_owner = {store, epoch, since, filed_by, history: [{at, by, from, to, reason, epoch}]}

A run filed before this existed reads as hub-owned at epoch 1, `backfilled`,
and the pulse writes that record down (`--apply`).

**Who may flip a step.** For a run owned by another store, only that store's
actors may claim, report or attest its steps; anyone else is refused
`not_the_owner` (distinct from `not_the_holder`, which is about a lease). For a
hub-owned run the hub's ordinary rules apply — except that a store which owned
the run in an earlier epoch is refused, so the former owner's in-flight answers
cannot land after a transfer. A step's claim stamps the epoch it was taken
under, and a report under an older epoch is `not_the_owner` with both epochs.

**The transfer** (`POST /runs/{id}/transfer {to, reason}`) is human-only and
admin-only (`AGENTCO_ADMINS`, each also a declared human; unset = nobody), and
works when the owner's key is revoked or the owner is silent — it needs nothing
from the owner. It bumps the epoch, appends the history, emits `RunTransferred`,
and then, for the run's steps:

  * **leased by anyone** (in progress): the lease is VOIDED — the step returns to
    `pending`, `lease_attempt` +1 (the fence), `leased_by` cleared,
    `metadata.lease_voided` records why — so a late report on the old lease is
    refused by the fence and by the epoch;
  * **open and assigned to an actor of the former owner**: the assignment is
    released, so the new owner's ordinary routing can give it to someone;
  * **done**: stays done; its attestation and evidence are kept;
  * **awaiting verification / verify_failed**: unchanged, and from then on only
    answerable under the new owner.

A transfer to the hub makes the open steps workable through the hub's own queue
under normal fencing.

**The pack** is a reference, never content: `{uri, sha256}` set by the owner
(for a hub-owned run, the actor that filed it or an admin).
"""

from __future__ import annotations

import json
import os
import re
from typing import Iterable, Mapping, Optional
from urllib.parse import urlparse

from agentco.errors import Refusal

RUN_OWNER_KEY = "run_owner"
RUN_PACK_KEY = "run_pack"
OWNER_EPOCH_KEY = "owner_epoch"
LEASE_VOID_KEY = "lease_voided"

STORES_ENV_VAR = "AGENTCO_STORES"
ADMINS_ENV_VAR = "AGENTCO_ADMINS"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def stores_from_env(value: Optional[str] = None) -> dict[str, frozenset]:
    """`AGENTCO_STORES`: which actors speak for which external store.

    JSON (`{"runtime-a": ["actor-1", "actor-2"]}`) or `runtime-a=actor-1|actor-2,runtime-b=actor-3`.
    Unset = no external store is registered, so every run is the hub's."""
    raw = (os.environ.get(STORES_ENV_VAR, "") if value is None else value).strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        data = json.loads(raw)
        return {str(k): frozenset(map(str, v)) for k, v in data.items()}
    out: dict[str, frozenset] = {}
    for part in raw.split(","):
        if not part.strip():
            continue
        store, sep, actors = part.partition("=")
        if not sep or not store.strip():
            raise ValueError(f"{STORES_ENV_VAR} entry {part!r} is not store=actor|actor")
        out[store.strip()] = frozenset(a.strip() for a in actors.split("|") if a.strip())
    return out


def admins_from_env(value: Optional[str] = None) -> frozenset:
    """`AGENTCO_ADMINS`, comma-separated. Fail closed: unset means nobody."""
    raw = os.environ.get(ADMINS_ENV_VAR, "") if value is None else value
    return frozenset(a.strip() for a in raw.split(",") if a.strip())


def _refuse(code: str, message: str, remediation: str, status: int) -> Refusal:
    return Refusal(code=code, message=message, remediation=remediation, http_status=status)


def not_the_owner(message: str, *, epoch: Optional[int] = None) -> Refusal:
    return _refuse(
        "not_the_owner",
        message + (f" (ownership epoch {epoch})" if epoch is not None else ""),
        "Only the store that owns a run may flip its steps (ASOP §7.2). If the owner is gone, a "
        "declared admin transfers the run: POST /runs/{id}/transfer {to, reason}.",
        403,
    )


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #


def run_root(queue, item) -> Optional[object]:
    """The run parent this bead belongs to, or None for an ordinary item."""
    from agentco.work import PARENT_KEY, RUN_KEY

    seen = set()
    cursor = item
    while cursor is not None and cursor.id not in seen:
        if RUN_KEY in (cursor.metadata or {}):
            return cursor
        seen.add(cursor.id)
        above = (cursor.metadata or {}).get(PARENT_KEY)
        cursor = queue.get(above) if above else None
    return None


def owner_record(queue, root) -> dict:
    record = (root.metadata or {}).get(RUN_OWNER_KEY)
    if isinstance(record, dict) and record.get("store"):
        return record
    return {"store": queue.hub_id, "epoch": 1, "since": root.created_at, "filed_by": None,
            "history": [], "backfilled": True}


def initial_record(store: str, filed_by: Optional[str], at: str) -> dict:
    return {"store": store, "epoch": 1, "since": at, "filed_by": filed_by,
            "history": [{"at": at, "by": filed_by, "from": None, "to": store, "reason": "filed", "epoch": 1}]}


def speaks_for(queue, actor: Optional[str], store: str) -> bool:
    if store == queue.hub_id:
        return False
    return actor is not None and actor in queue.stores.get(store, frozenset())


def former_owner(queue, actor: Optional[str], record: Mapping) -> Optional[str]:
    """The earlier owner `actor` speaks for, if it is not the current one."""
    for entry in record.get("history") or []:
        store = entry.get("from")
        if store and store != record["store"] and speaks_for(queue, actor, store):
            return store
    return None


# --------------------------------------------------------------------------- #
# the checks the queue runs before a step's status moves
# --------------------------------------------------------------------------- #


def check_actor(queue, item, actor: Optional[str], verb: str) -> Optional[dict]:
    """Refuse `actor` acting on `item`'s run unless its owner allows it.
    Returns the run's owner record (None for an ordinary item)."""
    root = run_root(queue, item)
    if root is None:
        return None
    record = owner_record(queue, root)
    owner = record["store"]
    if owner != queue.hub_id:
        if not speaks_for(queue, actor, owner):
            raise not_the_owner(f"{actor!r} cannot {verb} {item.id}: run {root.id} is owned by store "
                                f"{owner!r}", epoch=record.get("epoch"))
        return record
    earlier = former_owner(queue, actor, record)
    if earlier is not None:
        raise not_the_owner(f"{actor!r} speaks for {earlier!r}, which no longer owns run {root.id}; "
                            f"it was transferred to {owner!r}", epoch=record.get("epoch"))
    return record


def check_epoch(queue, item) -> None:
    """A report under an older ownership epoch than the run's is refused."""
    stamped = (item.metadata or {}).get(OWNER_EPOCH_KEY)
    if stamped is None:
        return
    root = run_root(queue, item)
    if root is None:
        return
    current = owner_record(queue, root).get("epoch", 1)
    if int(stamped) != int(current):
        raise not_the_owner(f"{item.id} was claimed under ownership epoch {stamped}; run {root.id} is now at "
                            f"epoch {current} after a transfer", epoch=current)


# --------------------------------------------------------------------------- #
# the administrative transfer, and the pack reference
# --------------------------------------------------------------------------- #


def transfer(queue, run_id: str, *, to: object, reason: object, by: str, at: str) -> dict:
    """Move a run to another owner, by a recorded administrative act. The
    caller has already checked that `by` is a declared human admin."""
    from agentco.work import RUN_KEY, WorkStatus

    root = queue.get(run_id)
    if root is None or RUN_KEY not in (root.metadata or {}):
        raise _refuse("work_item_unknown", f"no run {run_id!r}", "Transfer a run's id, as GET /runs lists them.", 404)
    if not isinstance(reason, str) or not reason.strip():
        raise _refuse("transfer_invalid", "a transfer needs a `reason`",
                      "Say why the owner is being replaced; the reason is recorded on the run for good.", 400)
    if not isinstance(to, str) or not (to == queue.hub_id or to in queue.stores):
        raise _refuse("transfer_invalid", f"{to!r} is not this hub ({queue.hub_id!r}) or a registered store",
                      f"Transfer to {queue.hub_id!r} or to one of {sorted(queue.stores)} ({STORES_ENV_VAR}).", 400)
    record = owner_record(queue, root)
    if to == record["store"]:
        raise _refuse("transfer_invalid", f"run {run_id} is already owned by {to!r}",
                      "A transfer to the current owner moves nothing.", 400)
    epoch = int(record.get("epoch", 1)) + 1
    entry = {"at": at, "by": by, "from": record["store"], "to": to, "reason": reason.strip(), "epoch": epoch}
    updated = {**{k: v for k, v in record.items() if k != "backfilled"},
               "store": to, "epoch": epoch, "since": at,
               "history": [*(record.get("history") or []), entry]}
    queue.annotate(run_id, {RUN_OWNER_KEY: updated}, by_plane=True)

    voided, released, kept = [], [], []
    former_actors = frozenset(queue.stores.get(record["store"], frozenset()))
    note = {"at": at, "epoch": epoch, "by": by, "reason": "run transferred", "from": record["store"]}
    for step in descendants(queue, run_id):
        if step.status is WorkStatus.DONE:
            kept.append(step.id)
            continue
        after = queue.release_for_transfer(step.id, note, former_actors)
        mark = (after.metadata or {}).get(LEASE_VOID_KEY) if after is not None else None
        if mark and mark.get("at") == at:
            if mark.get("voided_lease_of"):
                voided.append(step.id)
            if mark.get("released_assignment_of"):
                released.append(step.id)
    if queue.announce is not None:
        try:
            queue.announce("RunTransferred", by, {"runId": run_id, "from": record["store"], "to": to,
                                                  "epoch": epoch, "reason": reason.strip(),
                                                  "voided": voided})
        except Exception:  # noqa: BLE001 - the record is the fact; the feed is a notice
            pass
    return {"runId": run_id, "from": record["store"], "to": to, "epoch": epoch,
            "voidedLeases": voided, "releasedAssignments": released, "keptDone": kept, "owner": updated}


def descendants(queue, run_id: str) -> list:
    from agentco.work import PARENT_KEY

    items = queue.list()
    children: dict[str, list] = {}
    for item in items:
        parent = (item.metadata or {}).get(PARENT_KEY)
        if parent:
            children.setdefault(parent, []).append(item)
    out, stack = [], list(children.get(run_id, []))
    while stack:
        item = stack.pop()
        out.append(item)
        stack.extend(children.get(item.id, []))
    return sorted(out, key=lambda i: i.id)


def set_pack(queue, run_id: str, *, uri: object, sha256: object, by: str, admins: Iterable[str],
             at: str) -> dict:
    """Record WHERE a run's pack lives and its digest. Never its content."""
    from agentco.work import RUN_KEY

    root = queue.get(run_id)
    if root is None or RUN_KEY not in (root.metadata or {}):
        raise _refuse("work_item_unknown", f"no run {run_id!r}", "Name a run's id.", 404)
    record = owner_record(queue, root)
    allowed = (speaks_for(queue, by, record["store"]) if record["store"] != queue.hub_id
               else by == record.get("filed_by") or by in set(admins))
    if not allowed:
        raise not_the_owner(f"{by!r} may not set the pack of run {run_id}, owned by {record['store']!r}",
                            epoch=record.get("epoch"))
    if not isinstance(uri, str) or not urlparse(uri).scheme or len(uri) > 2048:
        raise _refuse("pack_invalid", "`uri` must be a location with a scheme (s3://, https://, file://, git:...)",
                      "A pack is stored by reference: send where it lives, never its content.", 400)
    digest = sha256.removeprefix("sha256:") if isinstance(sha256, str) else None
    if digest is None or not _SHA256.match(digest):
        raise _refuse("pack_invalid", "`sha256` must be 64 lowercase hex digits (optionally `sha256:`-prefixed)",
                      "Send the digest of the pack's bytes, so a reader can tell it is the pack that was filed.", 400)
    previous = (root.metadata or {}).get(RUN_PACK_KEY)
    pack = {"uri": uri, "sha256": f"sha256:{digest}", "set_by": by, "set_at": at,
            "previous": [*((previous or {}).get("previous") or []),
                         *([{k: previous[k] for k in ("uri", "sha256", "set_by", "set_at")}] if previous else [])]}
    queue.annotate(run_id, {RUN_PACK_KEY: pack}, by_plane=True)
    return pack
