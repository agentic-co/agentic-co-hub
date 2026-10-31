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

A run filed before this existed reads as hub-owned at epoch 0, `backfilled`,
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
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

from agentco.errors import Refusal, transfer_refused

RUN_OWNER_KEY = "run_owner"
RUN_PACK_KEY = "run_pack"
OWNER_EPOCH_KEY = "owner_epoch"
LEASE_VOID_KEY = "lease_voided"
#: The epoch count, ASOP 0.6.1 §7.2: a run never transferred is at epoch 0 and
#: each transfer is the current epoch + 1. This hub first counted from 1 at
#: filing; every record it writes now says which count it uses, so a record
#: from before (no marker) is rebased exactly once and never twice:
#: `epoch_base: 0` inside `run_owner` and inside a `lease_voided` note, and the
#: item key `owner_epoch_base: 0` beside an `owner_epoch` stamp.
EPOCH_BASE_KEY = "epoch_base"
STAMP_BASE_KEY = "owner_epoch_base"

STORES_ENV_VAR = "AGENTCO_STORES"
#: The standard's name for the administrator registry (spec 0.6.1 §9).
ADMINISTRATORS_ENV_VAR = "ASOP_ADMINISTRATORS"
#: DEPRECATED: the name this hub shipped first; read only when the standard's is unset.
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
    """The declared administrators: `ASOP_ADMINISTRATORS` (spec 0.6.1 §9), falling
    back to the deprecated `AGENTCO_ADMINS` only when the standard's name is UNSET —
    presence, not truthiness, so an operator who declares it empty means nobody.
    Comma-separated. Fail closed: unset (both) means nobody."""
    if value is None:
        value = os.environ.get(ADMINISTRATORS_ENV_VAR)
        if value is None:
            value = os.environ.get(ADMINS_ENV_VAR, "")
    return frozenset(a.strip() for a in value.split(",") if a.strip())


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


def run_root(queue, item) -> Optional[Any]:
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
        return rebased(record)
    return {"store": queue.hub_id, "epoch": 0, "since": root.created_at, "filed_by": None,
            "history": [], "backfilled": True, EPOCH_BASE_KEY: 0}


def rebased(record: Mapping) -> dict:
    """A run-owner record in the spec's count. One written before the count
    changed (no `epoch_base`) is one higher everywhere, so it is lowered by
    one, history included; one that carries the marker is returned as is."""
    if record.get(EPOCH_BASE_KEY) == 0:
        return dict(record)
    history = [{**entry, "epoch": int(entry["epoch"]) - 1}
               if isinstance(entry, dict) and isinstance(entry.get("epoch"), int) else entry
               for entry in (record.get("history") or [])]
    return {**record, "epoch": int(record.get("epoch", 1)) - 1, "history": history, EPOCH_BASE_KEY: 0}


def stamped_epoch(item) -> Optional[int]:
    """The epoch a step's lease was taken under, in the spec's count."""
    meta = item.metadata or {}
    stamped = meta.get(OWNER_EPOCH_KEY)
    if stamped is None:
        return None
    return int(stamped) if meta.get(STAMP_BASE_KEY) == 0 else int(stamped) - 1


def initial_record(store: str, filed_by: Optional[str], at: str) -> dict:
    return {"store": store, "epoch": 0, "since": at, "filed_by": filed_by, EPOCH_BASE_KEY: 0,
            "history": [{"at": at, "by": filed_by, "from": None, "to": store, "reason": "filed", "epoch": 0}]}


def rebase_epochs(queue, *, apply: bool) -> dict:
    """The one-time move to the spec's count, for every backend: lower by one
    each stored epoch written before it (run-owner records with their history,
    claim stamps, lease-void notes) and mark it. Each record is rewritten inside
    the store's own lock, from the row as it is at that moment, and the marker
    is what makes a second run (or a crash and a re-run) change nothing."""
    pending: list[str] = []
    for item in queue.list():
        if _legacy_epoch_fields(item):
            pending.append(item.id)
    if apply:
        def rebase(item) -> dict:
            changes = _legacy_epoch_fields(item)
            return {"metadata": {**(item.metadata or {}), **changes}} if changes else {}

        for item_id in pending:
            queue._mutate(item_id, rebase)
    return {"state": "rebased" if apply else "dry-run", "items": len(pending)}


def _legacy_epoch_fields(item) -> dict:
    meta = item.metadata or {}
    changes: dict = {}
    record = meta.get(RUN_OWNER_KEY)
    if isinstance(record, dict) and record.get(EPOCH_BASE_KEY) != 0:
        changes[RUN_OWNER_KEY] = rebased(record)
    if meta.get(OWNER_EPOCH_KEY) is not None and meta.get(STAMP_BASE_KEY) != 0:
        changes[OWNER_EPOCH_KEY] = int(meta[OWNER_EPOCH_KEY]) - 1
        changes[STAMP_BASE_KEY] = 0
    note = meta.get(LEASE_VOID_KEY)
    if isinstance(note, dict) and isinstance(note.get("epoch"), int) and note.get(EPOCH_BASE_KEY) != 0:
        changes[LEASE_VOID_KEY] = {**note, "epoch": note["epoch"] - 1, EPOCH_BASE_KEY: 0}
    return changes


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
    stamped = stamped_epoch(item)
    if stamped is None:
        return
    root = run_root(queue, item)
    if root is None:
        return
    current = owner_record(queue, root)["epoch"]
    if int(stamped) != int(current):
        raise not_the_owner(f"{item.id} was claimed under ownership epoch {stamped}; run {root.id} is now at "
                            f"epoch {current} after a transfer", epoch=current)


def check_attest_epoch(queue, item, claimed: object) -> None:
    """ASOP §7.2: every attestation carries the epoch its store believes it holds.

    `claimed` is the epoch the attester read (`owner.epoch` on `GET /runs/{id}`,
    or the one `not_the_owner` states), counted as the spec counts: 0 for a run
    never transferred, +1 per transfer. Absent is a store that predates epochs,
    which claims 0, so it is accepted exactly while the run is at 0. Anything
    else must equal the current epoch. An item outside a run is at epoch 0 (ADR 0008
    §8 S2): absent or 0 is accepted, any other value is `not_the_owner` stating 0.
    Null in JSON is absent.
    """
    if claimed is not None and (isinstance(claimed, bool) or not isinstance(claimed, int) or claimed < 0):
        raise _refuse(
            "epoch_invalid",
            f"epoch must be a non-negative integer, got {claimed!r}",
            "Send the run's ownership epoch as you read it (`owner.epoch` on GET /runs/{id}), or omit it "
            "on a run that has never been transferred.",
            400,
        )
    root = run_root(queue, item)
    if root is None:
        if claimed:
            raise not_the_owner(
                f"an attestation on {item.id} was made at ownership epoch {claimed}; "
                f"{item.id} is outside a run, whose epoch is 0", epoch=0)
        return
    current = int(owner_record(queue, root)["epoch"])
    if claimed is None:
        if current == 0:
            return
        raise not_the_owner(
            f"an attestation on {item.id} carries no ownership epoch (a store that predates epochs claims 0), "
            f"and run {root.id} has been transferred since", epoch=current)
    if claimed != current:
        raise not_the_owner(
            f"an attestation on {item.id} was made at ownership epoch {claimed}; run {root.id} is now at "
            f"epoch {current} after a transfer", epoch=current)


def check_verify_attempt(item, claimed: object) -> None:
    """ADR 0008 §8 S1: an answer names the failing verdicts it was typed after.

    `claimed` is the item's `verify_failures` as the attester read it. A failing
    verdict advances it and a lease does not, so a pass typed before a fail is
    refused `stale` even when no transfer happened for the epoch to see. Checked
    inside the store lock, beside the epoch.
    """
    if isinstance(claimed, bool) or not isinstance(claimed, int) or claimed < 0:
        raise _refuse(
            "verify_attempt_invalid",
            f"verifyAttempt must be a non-negative integer, got {claimed!r}",
            "Send the item's `verify_failures` as you read it, or omit the field.",
            400,
        )
    current = item.verify_failures
    if claimed != current:
        raise _refuse(
            "stale",
            f"an answer to {item.id} was made at verify attempt {claimed}; the item has "
            f"verify_failures {current}",
            f"Read the item again (its `verify_failures` is {current}) and answer what it shows now. "
            "Each failing verdict advances it, so an answer typed before one must not land after it.",
            409,
        )


# --------------------------------------------------------------------------- #
# the administrative transfer, and the pack reference
# --------------------------------------------------------------------------- #


def check_shape(to: object) -> None:
    """`transfer_refused:malformed` for a `to` that is not an exact name — the
    record's own validity, which spec 0.6.1 §10 checks after the caller and
    BEFORE the administrator rule."""
    if not isinstance(to, str) or not to.strip() or to != to.strip():
        raise transfer_refused("malformed", "transfer 'to' must be an exact, non-empty store name",
                               "Name this hub or a registered store.", alias="transfer_invalid")


def transfer(queue, run_id: str, *, to: object, reason: object, by: str, at: str) -> dict:
    """Move a run to another owner, by a recorded administrative act. The
    caller has already checked that `by` is a declared human admin."""
    from agentco.work import RUN_KEY, WorkStatus

    root = queue.get(run_id)
    if root is None or RUN_KEY not in (root.metadata or {}):
        raise _refuse("work_item_unknown", f"no run {run_id!r}", "Transfer a run's id, as GET /runs lists them.", 404)
    # Spec 0.6.1 §10's order, after the caller (checked by the route): the record's
    # own validity, then the reason, then the target. Each code carries the
    # `transfer_invalid` it replaced, as a deprecated alias.
    check_shape(to)
    if not isinstance(reason, str) or not reason.strip():
        raise transfer_refused("reason", "a transfer must say why",
                               "Say why the owner is being replaced; the reason is recorded on the run for good.",
                               alias="transfer_invalid")
    record = owner_record(queue, root)
    if to == record["store"]:
        raise transfer_refused("same_owner", f"{to!r} already owns run {run_id}",
                               "A transfer to the current owner moves nothing.", alias="transfer_invalid")
    if not (to == queue.hub_id or to in queue.stores):
        raise transfer_refused("malformed", f"{to!r} is not this hub ({queue.hub_id!r}) or a registered store",
                               f"Transfer to {queue.hub_id!r} or to one of {sorted(queue.stores)} ({STORES_ENV_VAR}).",
                               alias="transfer_invalid")
    epoch = int(record["epoch"]) + 1
    entry = {"at": at, "by": by, "from": record["store"], "to": to, "reason": reason.strip(), "epoch": epoch}
    updated = {**{k: v for k, v in record.items() if k != "backfilled"},
               "store": to, "epoch": epoch, "since": at,
               "history": [*(record.get("history") or []), entry]}
    queue.annotate(run_id, {RUN_OWNER_KEY: updated}, by_plane=True)

    voided, released, kept = [], [], []
    former_actors = frozenset(queue.stores.get(record["store"], frozenset()))
    note = {"at": at, "epoch": epoch, EPOCH_BASE_KEY: 0, "by": by, "reason": "run transferred",
            "from": record["store"]}
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


#: Where an item keeps its record of every gate reassignment, oldest first.
GATE_REASSIGNED_KEY = "gate_reassignments"


def reassign_gates(queue, *, from_: object, to: object, reason: object, by: str, at: str,
                   humans: Iterable[str], items: Optional[Iterable[str]] = None) -> dict:
    """Move every open human gate naming `from_` to `to` (ADR 0008 Decision 4).

    The administrative act for a person removed from a company: their identity
    stays on every record they answered, their keys are revoked separately, and
    each gate still waiting on them names somebody who can answer. Parallel to
    `transfer`: the caller has already checked that `by` is a declared human
    admin; this checks the record.

    An open gate is a human gate on any item not yet done, whatever its status:
    a gate on work still in progress names its verifier too, and leaving it to
    the departed person only moves the stranding later. `escalate_to` moves
    with the verifier when it named the same person. The live vehicle for each
    gate moves with it, so the new person is asked and the old one is not.

    The verify attempt does not move: an answer the removed person signed and
    that arrives after is refused because it is no longer the named human's,
    not because the slot changed. `items`, given, narrows the act to those ids,
    each of which must be an open gate naming `from_`.
    """
    from agentco import verifiers
    from agentco.work import TERMINAL

    humans = frozenset(humans)
    if not isinstance(reason, str) or not reason.strip():
        raise _refuse("reassign_invalid", "a reassignment must say why",
                      "Say why the gates move; the reason is recorded on each item for good.", 400)
    if not isinstance(from_, str) or not from_ or not isinstance(to, str) or not to:
        raise _refuse("reassign_invalid", "`from` and `to` name people",
                      "Send the person the gates name now and the person who will answer them.", 400)
    if to == from_:
        raise _refuse("reassign_invalid", f"{to!r} already answers these gates",
                      "A reassignment to the same person moves nothing.", 400)
    if to not in humans:
        raise _refuse("human_required", f"{to!r} is not a declared human, and a human gate needs one",
                      "Reassign to a person declared in ASOP_HUMANS.", 403)

    def open_gate(item) -> bool:
        gate = item.verify or {}
        return (gate.get("kind") == "human" and gate.get("verifier") == from_
                and item.status not in TERMINAL and not verifiers.is_vehicle(item))

    everything = queue.list()
    targets = [i for i in everything if open_gate(i)]
    if items is not None:
        wanted = list(dict.fromkeys(items))
        found = {i.id for i in targets}
        missing = [i for i in wanted if i not in found]
        if missing:
            raise _refuse("reassign_invalid", f"not open human gates naming {from_!r}: {missing}",
                          "Name only items whose open human gate names the person being replaced, "
                          "or omit `items` to move all of them.", 409)
        targets = [i for i in targets if i.id in set(wanted)]

    entry = {"at": at, "by": by, "from": from_, "to": to, "reason": reason.strip()}

    def moved(gate: dict) -> dict:
        out = {**gate, "verifier": to}
        if gate.get("escalate_to") == from_:
            out["escalate_to"] = to
        return out

    reassigned = []
    for item in targets:
        def reassign(current, _id=item.id):
            if not open_gate(current):  # moved on since the read: leave it
                return {}
            metadata = dict(current.metadata or {})
            metadata[GATE_REASSIGNED_KEY] = [*(metadata.get(GATE_REASSIGNED_KEY) or []), entry]
            return {"verify": moved(current.verify), "metadata": metadata}
        after = queue._mutate(item.id, reassign)
        if after is not None and (after.verify or {}).get("verifier") == to:
            reassigned.append(item.id)

    vehicles = []
    for vehicle in everything:
        if (verifiers.is_vehicle(vehicle) and verifiers.verifies(vehicle) in reassigned
                and vehicle.assigned_agent == from_ and vehicle.status not in TERMINAL):
            def change(current):
                metadata = dict(current.metadata or {})
                if isinstance(metadata.get("gate"), dict):
                    metadata["gate"] = moved(metadata["gate"])
                metadata[GATE_REASSIGNED_KEY] = [*(metadata.get(GATE_REASSIGNED_KEY) or []), entry]
                return {"assigned_agent": to, "metadata": metadata}
            queue._mutate(vehicle.id, change)
            vehicles.append(vehicle.id)

    if queue.announce is not None and reassigned:
        try:
            queue.announce("GateReassigned", by, {"from": from_, "to": to, "reason": reason.strip(),
                                                  "items": reassigned, "vehicles": vehicles})
        except Exception:  # noqa: BLE001 - the record is the fact; the feed is a notice
            pass
    return {"from": from_, "to": to, "reassigned": reassigned, "vehicles": vehicles}


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
