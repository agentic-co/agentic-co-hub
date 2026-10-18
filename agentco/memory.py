"""Shared memory items: owner-labeled, layered, one authoritative hub each.

ADR 0006 §1. A memory item is a short authored record — the same class of
object as an ASOP, held next to it — carrying:

  * an **owner label** `(owner, level)`, fixed for the item's life. Moving
    content to another label is a new item, written through the residuals
    path (§3), never a relabel: a relabel is how provenance is lost;
  * a **layer**. This hub holds `personal`, `shared` and `public`; `session`
    and `episodic` stay in the harness and are refused here (§1: episodic
    memory never leaves its hub, and this is not its hub);
  * exactly **one authoritative hub** (`authority`). An item written here is
    authoritative here. An item applied from another hub is a **replica**:
    read-only, versioned, and only ever moved forward by a newer version from
    its authority. A replica never becomes a competing version of the fact;
  * a **provenance** pointer — a hard field, never a comment;
  * a **pinned** flag with a calendar `review_by` date, for the class that
    decay and reinforcement must not touch (§5, the engine arrives later);
  * a `version`, and an append-only audit of every write.

**Nothing is deleted.** `retire` hides an item from retrieval and keeps it,
and its audit, for as long as the store exists (§4: decay never deletes; a
purge is an owner decision the ADR does not make).

**Every read takes a clearance** (`agentco/clearance.py`) and returns only
what it opens. An item the caller is not cleared for and an item that does
not exist produce the SAME refusal, so a read cannot be used to probe for
existence by content. What that does not close — metadata, timing, existence
through other channels — is listed in `docs/memory.md`, not claimed.

Two backends behind one interface, selected like the others (`stores.py`):
JSONL by default, the SQL tables (SQLite or Postgres) when `AGENTCO_DB` is set.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

from agentco.clearance import PUBLIC, Clearance, not_cleared
from agentco.errors import Refusal
from agentco.events import now_iso
from agentco.filelock import lock_exclusive, unlock

LAYERS = ("session", "episodic", "personal", "shared", "public")
HELD_LAYERS = ("personal", "shared", "public")
KINDS = ("lesson", "constraint", "fact", "note")
STATUSES = ("active", "retired")

HUB_ID_ENV_VAR = "AGENTCO_HUB_ID"
DEFAULT_HUB_ID = "local"
MEMORY_STORE_ENV_VAR = "AGENTCO_MEMORY_STORE"
DEFAULT_MEMORY_STORE = "memory.jsonl"

MAX_TITLE = 200
MAX_BODY = 20_000
MAX_TAGS = 20

#: Fields an authoritative revision may change. The label, the layer and the
#: authority are not among them, by design.
REVISABLE = ("title", "body", "kind", "tags", "applies_to", "provenance")

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


#: Shared memory is OFF unless an operator turns it on (owner decision
#: 2026-10-18): the slices land on main before the whole of ADR 0006 does, and a
#: hub must not start holding and serving owner-labeled items by upgrading.
MEMORY_ENV_VAR = "AGENTCO_MEMORY"
_ON, _OFF = ("on", "true", "1", "yes"), ("off", "false", "0", "no", "")


def memory_enabled(value: Optional[str] = None) -> bool:
    """`AGENTCO_MEMORY=on|off`, default off. Any other value refuses to start:
    a typo that silently left memory off — or on — is the failure this avoids."""
    raw = (os.environ.get(MEMORY_ENV_VAR, "") if value is None else value).strip().lower()
    if raw in _ON:
        return True
    if raw in _OFF:
        return False
    raise ValueError(f"{MEMORY_ENV_VAR}={raw!r}: expected 'on' or 'off' (default off)")


def memory_disabled() -> Refusal:
    """The one answer every memory path gives while the switch is off."""
    return Refusal(
        code="memory_disabled",
        message="shared memory is not enabled on this hub",
        remediation=(
            f"An operator turns it on with {MEMORY_ENV_VAR}=on (docs/memory.md). It is off by "
            f"default; while off, no memory store is opened and nothing is held or served."
        ),
        http_status=404,
    )


def resolve_hub_id(hub_id: Optional[str] = None) -> str:
    return (hub_id or os.environ.get(HUB_ID_ENV_VAR) or DEFAULT_HUB_ID).strip()


def resolve_memory_store(path: Optional[str] = None) -> str:
    return path or os.environ.get(MEMORY_STORE_ENV_VAR) or DEFAULT_MEMORY_STORE


@dataclass
class MemoryItem:
    item_id: str
    owner: str
    level: str
    layer: str
    authority: str
    version: int
    kind: str
    title: str
    body: str
    provenance: dict
    tags: list[str] = field(default_factory=list)
    applies_to: Optional[dict] = None
    pinned: bool = False
    review_by: Optional[str] = None
    status: str = "active"
    created_by: str = ""
    created_at: str = ""
    updated_by: str = ""
    updated_at: str = ""

    @property
    def label(self) -> dict:
        return {"owner": self.owner, "level": self.level}

    def to_dict(self) -> dict:
        out = asdict(self)
        out["label"] = self.label
        return out

    @classmethod
    def from_dict(cls, raw: Mapping) -> "MemoryItem":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known})


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def unknown_item(item_id: str) -> Refusal:
    """THE answer to a read of an item the caller cannot see — whether or not it
    exists. Same code, same status, same text, so the reply carries no bit."""
    return Refusal(
        code="memory_unknown",
        message=f"no memory item {item_id!r} is visible to you",
        remediation="Check the id. Items you are not cleared for are not visible.",
        http_status=404,
    )


def _invalid(message: str) -> Refusal:
    return Refusal(code="memory_invalid", message=message,
                   remediation="Correct the field and send it again; nothing was stored.",
                   http_status=400)


def _human_only(what: str) -> Refusal:
    return Refusal(
        code="capability_mismatch",
        message=f"only a declared human may {what}",
        remediation=(
            "Ask a person the operator declared in AGENTCO_HUMANS. Pinning exempts an "
            "item from decay and retiring hides it; an agent that could do either could "
            "decide what everyone else remembers."
        ),
        http_status=403,
    )


def _replica_read_only(item: MemoryItem) -> Refusal:
    return Refusal(
        code="replica_read_only",
        message=f"{item.item_id} is a replica; its authoritative hub is {item.authority!r}",
        remediation=(
            "Propose the change to the authoritative hub. A replica changes only when "
            "its authority publishes a newer version."
        ),
        http_status=409,
    )


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _text(value: Any, what: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{what} must be a non-empty string")
    if len(value) > limit:
        raise _invalid(f"{what} is {len(value)} characters; the limit is {limit}")
    return value.strip()


def _clean_fields(raw: Mapping) -> dict:
    """The revisable fields, normalised. Raises `memory_invalid`."""
    out: dict = {}
    if "title" in raw:
        out["title"] = _text(raw["title"], "'title'", MAX_TITLE)
    if "body" in raw:
        out["body"] = _text(raw["body"], "'body'", MAX_BODY)
    if "kind" in raw:
        if raw["kind"] not in KINDS:
            raise _invalid(f"'kind' must be one of {list(KINDS)}")
        out["kind"] = raw["kind"]
    if "tags" in raw:
        tags = raw["tags"] or []
        if isinstance(tags, (str, dict)) or not isinstance(tags, (list, tuple)) or len(tags) > MAX_TAGS:
            raise _invalid(f"'tags' must be a list of at most {MAX_TAGS} strings")
        out["tags"] = sorted({_text(t, "a tag", 64) for t in tags})
    if "applies_to" in raw:
        ref = raw["applies_to"]
        if ref is not None:
            if not isinstance(ref, Mapping) or set(ref) - {"asop_id", "step"} or not ref.get("asop_id"):
                raise _invalid("'applies_to' is {asop_id, step?}: the procedure this lesson belongs in")
            step = ref.get("step")
            if step is not None and (isinstance(step, bool) or not isinstance(step, int) or step < 1):
                raise _invalid("'applies_to.step' is a step number, 1 or more")
            ref = {"asop_id": _text(ref["asop_id"], "'applies_to.asop_id'", 128),
                   **({"step": step} if step is not None else {})}
        out["applies_to"] = ref
    if "provenance" in raw:
        prov = raw["provenance"]
        if not isinstance(prov, Mapping) or set(prov) != {"kind", "ref"}:
            raise _invalid(
                "'provenance' is required and is exactly {kind, ref}: where this item came "
                "from, as a pointer. Provenance is a hard field, not a comment (ADR 0006 §3)."
            )
        out["provenance"] = {"kind": _text(prov["kind"], "'provenance.kind'", 64),
                             "ref": _text(prov["ref"], "'provenance.ref'", 2048)}
    return out


def _review_date(value: Any) -> str:
    if not isinstance(value, str):
        raise _invalid("'review_by' is a calendar date, YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise _invalid("'review_by' is a calendar date, YYYY-MM-DD") from None


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #


class MemoryStore:
    """The JSONL backend, and the behaviour both backends share.

    Subclasses replace five storage primitives — `_locked`, `_load`, `_all`,
    `_put`, `_audit` / `_audits` — and inherit every rule. One copy of the
    rules, for the reason `SqlQueue` inherits the lease protocol verbatim.
    """

    def __init__(self, path: Path | str = DEFAULT_MEMORY_STORE, *, hub_id: Optional[str] = None):
        self.path = Path(path)
        self.audit_path = self.path.with_suffix(self.path.suffix + ".audit")
        self.hub_id = resolve_hub_id(hub_id)
        self.quarantined: list[bytes] = []

    # -- storage primitives (JSONL) --------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(self.path.suffix + ".lock"), "a+") as handle:
            lock_exclusive(handle)
            try:
                yield
            finally:
                unlock(handle)

    def _all(self) -> list[MemoryItem]:
        if not self.path.exists():
            return []
        out, quarantined = [], []
        for raw in self.path.read_bytes().split(b"\n"):
            if not raw.strip():
                continue
            try:
                out.append(MemoryItem.from_dict(json.loads(raw.decode("utf-8"))))
            except (UnicodeDecodeError, ValueError, TypeError):
                quarantined.append(raw)
        self.quarantined = quarantined
        return out

    def _load(self, item_id: str) -> Optional[MemoryItem]:
        return next((i for i in self._all() if i.item_id == item_id), None)

    def _put(self, item: MemoryItem) -> None:
        items = [i for i in self._all() if i.item_id != item.item_id] + [item]
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                for i in sorted(items, key=lambda i: i.item_id):
                    handle.write(json.dumps(asdict(i), sort_keys=True).encode("utf-8") + b"\n")
                for line in self.quarantined:
                    handle.write(line + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _audit(self, entry: dict) -> None:
        with open(self.audit_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _audits(self, item_id: str) -> list[dict]:
        if not self.audit_path.exists():
            return []
        out = []
        for line in self.audit_path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("item_id") == item_id:
                out.append(entry)
        return out

    # -- the rules -------------------------------------------------------

    def _record(self, item: MemoryItem, action: str, actor: str) -> None:
        self._put(item)
        self._audit({"item_id": item.item_id, "version": item.version, "action": action,
                     "actor": actor, "at": item.updated_at, "record": asdict(item)})

    def _visible(self, item: Optional[MemoryItem], clearance: Clearance) -> bool:
        return item is not None and clearance.allows(item.owner, item.level)

    def create(self, *, actor: str, clearance: Clearance, owner: Any, level: Any, layer: Any,
               **fields: Any) -> MemoryItem:
        """Write a new authoritative item. `clearance` is the actor's EFFECTIVE
        clearance (hub ∧ actor): an item can be written only at a label both
        this hub and the actor hold."""
        if not isinstance(owner, str) or not _NAME.match(owner):
            raise _invalid("'label.owner' is a short name: letters, digits, '.', '_' or '-'")
        if level not in clearance.levels:
            raise _invalid(f"'label.level' must be one of {list(clearance.levels)}")
        if layer not in LAYERS:
            raise _invalid(f"'layer' must be one of {list(HELD_LAYERS)}")
        if layer not in HELD_LAYERS:
            raise _invalid(
                f"layer {layer!r} stays in the harness; this hub holds {list(HELD_LAYERS)} "
                f"(ADR 0006 §1: session and episodic memory never leave their hub)"
            )
        if (layer == "public") != (owner == PUBLIC):
            raise _invalid("the public layer holds exactly the items labeled with owner 'public'")
        unknown = set(fields) - set(REVISABLE)
        if unknown:
            raise _invalid(f"unknown field(s) {sorted(unknown)}; a new item takes {list(REVISABLE)}")
        clean = _clean_fields(fields)
        for required in ("title", "body", "provenance"):
            if required not in clean:
                raise _invalid(f"'{required}' is required")
        if not clearance.allows(owner, level):
            raise not_cleared(owner, level)
        at = now_iso()
        item = MemoryItem(
            item_id=f"mem-{uuid.uuid4().hex[:12]}", owner=owner, level=level, layer=layer,
            authority=self.hub_id, version=1, kind=clean.get("kind", "lesson"),
            title=clean["title"], body=clean["body"], provenance=clean["provenance"],
            tags=clean.get("tags", []), applies_to=clean.get("applies_to"),
            created_by=actor, created_at=at, updated_by=actor, updated_at=at,
        )
        with self._locked():
            self._record(item, "create", actor)
        return item

    def get(self, item_id: str, clearance: Clearance) -> MemoryItem:
        item = self._load(item_id)
        if not self._visible(item, clearance):
            raise unknown_item(item_id)
        return item

    def list(self, clearance: Clearance, *, layer: Optional[str] = None, kind: Optional[str] = None,
             include_retired: bool = False) -> list[MemoryItem]:
        """The cleared subset, filtered. The wall is applied FIRST — the filters
        and any ranking a caller adds only ever see what it let through."""
        out = []
        for item in self._all():
            if not clearance.allows(item.owner, item.level):
                continue
            if item.status == "retired" and not include_retired:
                continue
            if layer is not None and item.layer != layer:
                continue
            if kind is not None and item.kind != kind:
                continue
            out.append(item)
        return sorted(out, key=lambda i: i.item_id)

    def history(self, item_id: str, clearance: Clearance) -> list[dict]:
        self.get(item_id, clearance)
        return sorted(self._audits(item_id), key=lambda e: e["version"])

    def _authoritative(self, item_id: str, clearance: Clearance) -> MemoryItem:
        item = self._load(item_id)
        if not self._visible(item, clearance):
            raise unknown_item(item_id)
        if item.authority != self.hub_id:
            raise _replica_read_only(item)
        return item

    def _bump(self, item: MemoryItem, actor: str, expected: Optional[int]) -> None:
        if expected is not None and expected != item.version:
            raise Refusal(
                code="work_conflict",
                message=f"{item.item_id} is at version {item.version}, not {expected}",
                remediation="Read it again and apply your change to the current version.",
                http_status=409,
            )
        item.version += 1
        item.updated_by = actor
        item.updated_at = now_iso()

    def revise(self, item_id: str, *, actor: str, clearance: Clearance,
               expected_version: Optional[int] = None, **changes: Any) -> MemoryItem:
        """Change the content of an authoritative item. Label, layer and
        authority are not revisable; a field naming one is refused."""
        fixed = set(changes) - set(REVISABLE)
        if fixed:
            raise _invalid(
                f"{sorted(fixed)} cannot be revised. The label, layer and authority are "
                f"fixed for an item's life; content for another label is a new item."
            )
        clean = _clean_fields(changes)
        if not clean:
            raise _invalid(f"nothing to revise; send any of {list(REVISABLE)}")
        with self._locked():
            item = self._authoritative(item_id, clearance)
            if item.status == "retired":
                raise _invalid(f"{item_id} is retired; a retired item is kept, not edited")
            self._bump(item, actor, expected_version)
            for key, value in clean.items():
                setattr(item, key, value)
            self._record(item, "revise", actor)
        return item

    def set_pinned(self, item_id: str, *, actor: str, human: bool, clearance: Clearance,
                   pinned: bool, review_by: Any = None,
                   expected_version: Optional[int] = None) -> MemoryItem:
        """Pin (with a calendar review date) or unpin. Human-only."""
        if not human:
            raise _human_only("pin or unpin a memory item")
        review = _review_date(review_by) if pinned else None
        with self._locked():
            item = self._authoritative(item_id, clearance)
            self._bump(item, actor, expected_version)
            item.pinned, item.review_by = bool(pinned), review
            self._record(item, "pin" if pinned else "unpin", actor)
        return item

    def retire(self, item_id: str, *, actor: str, human: bool, clearance: Clearance,
               expected_version: Optional[int] = None) -> MemoryItem:
        """Hide from retrieval; keep the item and its audit. Human-only."""
        if not human:
            raise _human_only("retire a memory item")
        with self._locked():
            item = self._authoritative(item_id, clearance)
            if item.status == "retired":
                return item
            self._bump(item, actor, expected_version)
            item.status = "retired"
            self._record(item, "retire", actor)
        return item

    def apply_replica(self, record: Mapping, *, actor: str, hub_clearance: Clearance) -> dict:
        """Apply a version published by the item's authoritative hub.

        Library-level: the memory channel that carries these (ADR 0007) is not
        built. The rules it will rely on are here now:

          * the record's authority is another hub — this hub never holds a
            replica of its own item;
          * an item keeps ONE authority: a record naming a different authority
            for an id this hub already holds is refused;
          * the label is fixed: a newer version may not move it;
          * the hub must be cleared for the label to hold it at all;
          * versions only move forward; an older or equal one changes nothing.
        """
        try:
            incoming = MemoryItem.from_dict(dict(record))
        except TypeError as exc:
            raise _invalid(f"not a memory item record: {exc}") from exc
        if incoming.authority == self.hub_id:
            raise _invalid("a replica's authority is another hub; this hub is authoritative for its own items")
        if not hub_clearance.allows(incoming.owner, incoming.level):
            raise not_cleared(incoming.owner, incoming.level)
        with self._locked():
            held = self._load(incoming.item_id)
            if held is not None:
                if held.authority != incoming.authority:
                    raise Refusal(
                        code="replica_conflict",
                        message=(f"{incoming.item_id} is held with authority {held.authority!r}; "
                                 f"the record names {incoming.authority!r}"),
                        remediation="An item has exactly one authoritative hub. Resolve which one it is.",
                        http_status=409,
                    )
                if (held.owner, held.level) != (incoming.owner, incoming.level):
                    raise _invalid("a replica's label is fixed; a newer version may not move it")
                if incoming.version <= held.version:
                    return {"state": "unchanged", "version": held.version}
            self._record(incoming, "replicate", actor)
        return {"state": "accepted", "version": incoming.version}


# --------------------------------------------------------------------------- #
# SQL backend
# --------------------------------------------------------------------------- #

_COLUMNS = (
    "item_id", "owner", "level", "layer", "authority", "version", "kind", "title", "body",
    "provenance", "tags", "applies_to", "pinned", "review_by", "status",
    "created_by", "created_at", "updated_by", "updated_at",
)
_JSON = frozenset({"provenance", "tags", "applies_to"})


def _to_row(item: MemoryItem) -> tuple:
    out = []
    for column in _COLUMNS:
        value = getattr(item, column)
        if column in _JSON:
            value = None if value is None else json.dumps(value, sort_keys=True)
        elif column == "pinned":
            value = 1 if value else 0
        out.append(value)
    return tuple(out)


def _from_row(row) -> MemoryItem:
    data = {c: row[c] for c in _COLUMNS}
    for column in _JSON:
        data[column] = None if data[column] is None else json.loads(data[column])
    data["tags"] = data["tags"] or []
    data["pinned"] = bool(data["pinned"])
    return MemoryItem(**data)


class SqlMemoryStore(MemoryStore):
    """The same rules on the `memory_items` / `memory_versions` tables."""

    def __init__(self, path: Path | str = "agentco.sqlite3", *, hub_id: Optional[str] = None):
        from agentco.sqlstore import _SqlBacked

        self._backing = _SqlBacked()
        self._backing._open(path)
        self.path = self._backing.path
        self.hub_id = resolve_hub_id(hub_id)
        self.quarantined = []
        self._in_tx = False

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._backing._write_tx():
            self._in_tx = True
            try:
                yield
            finally:
                self._in_tx = False

    def _conn(self):
        return self._backing._conn

    def _query(self, sql: str, args: Sequence = ()) -> list:
        if self._in_tx:
            return self._conn().execute(sql, args).fetchall()
        with self._backing._read_tx() as conn:
            return conn.execute(sql, args).fetchall()

    def _all(self) -> list[MemoryItem]:
        return [_from_row(r) for r in self._query("SELECT * FROM memory_items ORDER BY item_id")]

    def _load(self, item_id: str) -> Optional[MemoryItem]:
        postgres = getattr(self._conn(), "dialect", "sqlite") == "postgres"
        suffix = " FOR UPDATE" if postgres and self._in_tx else ""
        rows = self._query(f"SELECT * FROM memory_items WHERE item_id = ?{suffix}", (item_id,))
        return _from_row(rows[0]) if rows else None

    def _put(self, item: MemoryItem) -> None:
        quoted = ", ".join(_COLUMNS)
        marks = ", ".join("?" for _ in _COLUMNS)
        updates = ", ".join(f"{c} = excluded.{c}" for c in _COLUMNS if c != "item_id")
        self._conn().execute(
            f"INSERT INTO memory_items ({quoted}) VALUES ({marks}) "
            f"ON CONFLICT (item_id) DO UPDATE SET {updates}",
            _to_row(item),
        )

    def _audit(self, entry: dict) -> None:
        self._conn().execute(
            "INSERT INTO memory_versions (item_id, version, action, actor, at, record) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (entry["item_id"], entry["version"], entry["action"], entry["actor"], entry["at"],
             json.dumps(entry["record"], sort_keys=True)),
        )

    def _audits(self, item_id: str) -> list[dict]:
        rows = self._query(
            "SELECT item_id, version, action, actor, at, record FROM memory_versions "
            "WHERE item_id = ? ORDER BY version", (item_id,))
        return [{"item_id": r["item_id"], "version": r["version"], "action": r["action"],
                 "actor": r["actor"], "at": r["at"], "record": json.loads(r["record"])} for r in rows]


def open_memory_store(memory_store: Optional[str] = None, *, db: Optional[str] = None,
                      hub_id: Optional[str] = None) -> MemoryStore:
    from agentco.stores import resolve_backend_db

    target = resolve_backend_db(db)
    if target:
        return SqlMemoryStore(target, hub_id=hub_id)
    return MemoryStore(resolve_memory_store(memory_store), hub_id=hub_id)
