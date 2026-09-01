"""The durable backend: the work queue and the SOP library, in SQLite.

The default stores are JSONL under an advisory file lock, and `work.py` states
plainly why — greppable at 02:00, diffable in review, a corrupt line
quarantinable instead of fatal. That reasoning has not changed and the JSONL
store is not going anywhere. What changed is that the same file now has more
than one process opening it (the HTTP app, an MCP server per harness, the CLI,
a cadence job), and the honest ceiling of read-whole-file-rewrite-whole-file
under a flock is lower than that.

So: a second backend, selected by `AGENTCO_DB`, behind the same interfaces.

**Both classes are subclasses, deliberately.** A backend contract nobody can
read is not a contract, and the most reliable way to satisfy one is to inherit
the parts that are not about storage. `SqlQueue` overrides four things —
opening, reading, one mutation primitive, and create — and inherits `claim`,
`report_result`, `reap_expired_leases`, `ready`, `list` and `get` **verbatim**.
That is not laziness. The lease protocol's value is entirely in details that
took incidents to find (capability before contention, the attempt bumping on
report AND on reap, `ready` and `claim` agreeing about an expired lease), and
a second hand-written copy of it would drift from the first on the day someone
fixes one of them. There is one copy; only the storage underneath it differs.

**The CAS is a transaction, not a read followed by a write.** `_mutate` opens
`BEGIN IMMEDIATE` — which takes the write lock *before* the SELECT, so no
other writer can interleave between the read and the update — and then issues
the UPDATE with `WHERE id = ? AND lease_attempt = ? AND updated_at = ?`. The
IMMEDIATE lock is what makes it correct; the conditional WHERE is a backstop
that fails loudly rather than silently overwriting if that ever stops being
true. `tests/test_sqlstore.py` races twelve real processes at one item.

WHAT IS DELIBERATELY NOT HERE: connection pooling, an ORM, a second dialect.
One connection per store object, plain `sqlite3`, one file.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from dataclasses import fields
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence

from agentco import migrations
from agentco.db import BUSY_TIMEOUT_MS
from agentco.keys import derive_natural_key
from agentco.sop import SOP, SopLibrary, SopStatus
from agentco.work import Queue, WorkError, WorkItem, WorkStatus, _iso, _now

# Every WorkItem field is a column of the same name. Asserted at import rather
# than trusted: adding a field to the dataclass and forgetting the migration
# would otherwise present as that field silently not persisting.
WORK_COLUMNS: tuple[str, ...] = (
    "id",
    "title",
    "status",
    "assigned_agent",
    "requires",
    "blocked_by",
    "leased_by",
    "lease_attempt",
    "lease_expires_at",
    "result",
    "natural_key",
    "metadata",
    "created_at",
    "updated_at",
)
_WORK_JSON_COLUMNS = frozenset({"requires", "blocked_by", "metadata"})
# Everything except the primary key: what an update is allowed to touch.
# `unknown` is not in here on purpose — see migration 0002.
_WORK_MUTABLE = tuple(c for c in WORK_COLUMNS if c != "id")

SOP_COLUMNS: tuple[str, ...] = (
    "sop_id",
    "version",
    "title",
    "status",
    "purpose",
    "trigger",
    "entry_check",
    "inputs",
    "definition_of_done",
    "validation",
    "write_back",
    "common_mistakes",
    "next_sop",
    "superseded_by",
    "created_at",
)
_SOP_JSON_COLUMNS = frozenset({"common_mistakes"})


def _check_columns(dataclass_type, columns: Sequence[str]) -> None:
    declared = {f.name for f in fields(dataclass_type)}
    missing = declared - set(columns)
    if missing:
        raise AssertionError(
            f"{dataclass_type.__name__} has field(s) {sorted(missing)} with no "
            f"column in the SQLite backend. Add them to the schema (a new "
            f"migration) and to the column tuple — a field with no column does "
            f"not fail, it silently does not persist."
        )


_check_columns(WorkItem, WORK_COLUMNS)
_check_columns(SOP, SOP_COLUMNS)


def _quote(column: str) -> str:
    """`trigger` is a SQLite keyword. Quote every identifier, not just that one."""
    return f'"{column}"'


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the durable store, migrated, in autocommit so BEGIN is explicit.

    `isolation_level=None` because every write here is wrapped in an explicit
    `BEGIN IMMEDIATE`. Leaving Python's implicit transaction handling on would
    mean the transaction that matters starts somewhere the code does not say.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    migrations.apply(conn)
    return conn


class _SqlBacked:
    """Connection ownership and the one write-transaction primitive."""

    def _open(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn = connect(self.path)

    @contextmanager
    def _write_tx(self) -> Iterator[sqlite3.Connection]:
        """One mutation, one transaction. `BEGIN IMMEDIATE`, so the write lock
        is taken before the read that the write is conditioned on.

        A raising body rolls back and leaves the database byte-identical,
        which is what makes the CAS and the fence safe to express as ordinary
        exceptions — the same property the JSONL store gets from doing its
        read-modify-write inside one flock.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def close(self) -> None:
        self._conn.close()


# --------------------------------------------------------------------------- #
# Work items
# --------------------------------------------------------------------------- #


def _item_to_row(item: WorkItem) -> dict:
    row = {}
    for column in WORK_COLUMNS:
        value = getattr(item, column)
        if column == "status":
            value = item.status.value
        elif column in _WORK_JSON_COLUMNS:
            value = json.dumps(value if value is not None else ([] if column != "metadata" else {}))
        row[column] = value
    return row


def _row_to_dict(row: sqlite3.Row) -> dict:
    """A stored row as the plain dict shape the JSONL store hands around.

    Unknown fields are merged back in at the top level so that a newer
    writer's column is visible to anything walking raw rows, exactly as it
    would be in a JSONL line.
    """
    out = dict(json.loads(row["unknown"] or "{}"))
    for column in WORK_COLUMNS:
        value = row[column]
        out[column] = json.loads(value) if column in _WORK_JSON_COLUMNS else value
    return out


class SqlQueue(_SqlBacked, Queue):
    """The work queue on SQLite. Same protocol, different storage underneath."""

    def __init__(self, path: Path | str = "agentco.sqlite3"):
        self._open(path)
        # The JSONL store's quarantine has no analogue here: a row either
        # satisfies the schema or was never inserted. Kept as an empty list so
        # callers that read it — the health check does — need no backend test.
        self.quarantined: list[bytes] = []

    # -- storage ---------------------------------------------------------

    def _locked(self):  # pragma: no cover - guarded by _write_all below
        raise NotImplementedError(
            "SqlQueue does not use the file lock; mutations go through "
            "_write_tx (BEGIN IMMEDIATE). Reaching here means an inherited "
            "code path was not overridden."
        )

    def _write_all(self, rows, quarantined=()):  # pragma: no cover - see above
        raise NotImplementedError(
            "SqlQueue has no whole-store rewrite. Every mutation is a targeted "
            "UPDATE inside _mutate; a caller that wanted to replace the store "
            "wholesale is a caller that has not been ported."
        )

    def _row(self, item_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM work_items WHERE id = ?", (item_id,)
        ).fetchone()

    def _read_raw(self) -> tuple[list[dict], list[bytes]]:
        rows = self._conn.execute("SELECT * FROM work_items ORDER BY rowid").fetchall()
        return [_row_to_dict(r) for r in rows], []

    # -- creation --------------------------------------------------------

    def create(
        self,
        title: str,
        *,
        requires: Sequence[str] = (),
        blocked_by: Sequence[str] = (),
        assigned_agent: Optional[str] = None,
        natural_key: Optional[str] = None,
        source: Optional[str] = None,
        source_id: Optional[str] = None,
        kind: Optional[str] = None,
        subject: Optional[str] = None,
        period: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> WorkItem:
        """One item, with the same loud duplicate suppression as the JSONL store.

        The lookup and the insert are one `BEGIN IMMEDIATE` transaction, and
        the unique partial index on `natural_key` is the rule underneath —
        so a concurrent create with the same key converges on one row whether
        or not the check saw it.
        """
        key = derive_natural_key(
            explicit=natural_key,
            source=source,
            source_id=source_id,
            kind=kind,
            subject=subject,
            period=period,
        )
        item = WorkItem(
            id=f"w-{uuid.uuid4().hex[:8]}",
            title=title,
            requires=list(requires),
            blocked_by=list(blocked_by),
            assigned_agent=assigned_agent,
            natural_key=key,
            metadata=dict(metadata or {}),
        )

        with self._write_tx() as conn:
            if key:
                existing_row = conn.execute(
                    "SELECT * FROM work_items WHERE natural_key = ?", (key,)
                ).fetchone()
                if existing_row is not None:
                    existing = WorkItem.from_json(json.dumps(_row_to_dict(existing_row)))
                    print(
                        f"[work] DUPLICATE-SUPPRESSED key={key!r} "
                        f"title={title!r} held-by={existing.id}",
                        file=sys.stderr,
                    )
                    existing.metadata = dict(existing.metadata or {})
                    existing.metadata["natural_key_conflict"] = True
                    return existing
            row = _item_to_row(item)
            conn.execute(
                f"INSERT INTO work_items ({', '.join(WORK_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in WORK_COLUMNS)})",
                tuple(row[c] for c in WORK_COLUMNS),
            )
        return item

    # -- the one mutation primitive --------------------------------------

    def _mutate(self, item_id: str, change: Callable[[WorkItem], dict]) -> Optional[WorkItem]:
        """Read-modify-write as ONE transaction. `change` may raise to abort.

        Every lease operation inherited from `Queue` — claim, report, reap —
        is expressed in terms of this, so this is the single place the fenced
        CAS has to be right.
        """
        with self._write_tx() as conn:
            row = conn.execute(
                "SELECT * FROM work_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                return None
            target = WorkItem.from_json(json.dumps(_row_to_dict(row)))
            # What the row looked like when the decision was made. The UPDATE
            # is conditioned on both, so if the row moved between the SELECT
            # and the UPDATE the write does not land — and the mismatch is
            # raised rather than counted as success.
            seen_attempt = target.lease_attempt
            seen_updated = target.updated_at

            updates = change(target)  # may raise; nothing written yet
            for key, value in updates.items():
                setattr(target, key, value)
            target.updated_at = _iso(_now())

            new_row = _item_to_row(target)
            assignments = ", ".join(f"{_quote(c)} = ?" for c in _WORK_MUTABLE)
            cursor = conn.execute(
                f"UPDATE work_items SET {assignments} "
                f"WHERE id = ? AND lease_attempt = ? AND updated_at = ?",
                (
                    *(new_row[c] for c in _WORK_MUTABLE),
                    item_id,
                    seen_attempt,
                    seen_updated,
                ),
            )
            if cursor.rowcount != 1:
                # Cannot happen while BEGIN IMMEDIATE holds the write lock
                # across the read. It is checked anyway because the failure it
                # would otherwise produce is the silent one: a mutation that
                # reports success and wrote nothing.
                raise WorkError(
                    f"compare-and-swap on {item_id} matched {cursor.rowcount} "
                    f"rows, expected exactly 1 — the row moved between the read "
                    f"and the write inside a transaction that should have made "
                    f"that impossible. Nothing was written."
                )
            return target


# --------------------------------------------------------------------------- #
# SOPs
# --------------------------------------------------------------------------- #


def _sop_to_row(sop: SOP) -> tuple:
    values = []
    for column in SOP_COLUMNS:
        value = getattr(sop, column)
        if column == "status":
            value = sop.status.value
        elif column in _SOP_JSON_COLUMNS:
            value = json.dumps(value or [])
        values.append(value)
    return tuple(values)


def _row_to_sop(row: sqlite3.Row) -> SOP:
    data = dict(json.loads(row["unknown"] or "{}"))
    for column in SOP_COLUMNS:
        value = row[column]
        data[column] = json.loads(value) if column in _SOP_JSON_COLUMNS else value
    data["status"] = SopStatus(data["status"])
    known = {f.name for f in fields(SOP)}
    return SOP(**{k: v for k, v in data.items() if k in known})


class SqlSopLibrary(_SqlBacked, SopLibrary):
    """The SOP library on SQLite.

    Only three things are overridden — the lock, the read, and the write —
    and the reason is different from the queue's. An SOP library is a version
    HISTORY: `revise()` computes the next version over the whole history,
    `activate()` demotes whichever version is currently active, and both are
    correct only if they see all of it at once. Every authoring method is
    already written as "read the history, change it, write it back under a
    lock", so replacing the lock with `BEGIN IMMEDIATE` and the file with a
    table gives the durable version with the version-numbering rules — the
    part that is genuinely subtle — untouched.

    Rewriting the whole table per authoring call is fine and stays fine: this
    holds procedures, not instances. The queue is the table that grows, and
    the queue does targeted updates.
    """

    def __init__(self, path: Path | str = "agentco.sqlite3"):
        self._open(path)
        self.quarantined: list[bytes] = []

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._write_tx():
            yield

    def _read_all(self) -> list[SOP]:
        rows = self._conn.execute(
            "SELECT * FROM sops ORDER BY sop_id, version"
        ).fetchall()
        self.quarantined = []
        return [_row_to_sop(r) for r in rows]

    def _write_all(self, sops: Sequence[SOP], quarantined: Sequence[bytes] = ()) -> None:
        """Replace the table's contents. Called only inside `_locked`.

        The delete-then-insert is safe precisely because it is inside the
        caller's transaction: a reader either sees the whole previous history
        or the whole new one. Doing this outside a transaction would expose an
        instant in which every SOP has been destroyed — and `_write_all`'s own
        docstring in `sop.py` explains what a lost version row costs.
        """
        quoted = ", ".join(_quote(c) for c in SOP_COLUMNS)
        placeholders = ", ".join("?" for _ in SOP_COLUMNS)
        self._conn.execute("DELETE FROM sops")
        for sop in sops:
            self._conn.execute(
                f"INSERT INTO sops ({quoted}) VALUES ({placeholders})",
                _sop_to_row(sop),
            )
