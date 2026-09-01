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
import threading
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
    """Connection ownership and the one write-transaction primitive.

    **One connection per store, serialised by one lock.** The connection is
    opened `check_same_thread=False` — the HTTP app is a threaded server
    holding one store object — and a connection has exactly one transaction.
    Two threads issuing `BEGIN IMMEDIATE` on it are therefore not two writers
    contending for the database (which SQLite handles, and which the
    twelve-process race in `tests/test_sqlstore.py` proves); they are two
    callers contending for the same session, and the second gets `cannot start
    a transaction within a transaction` — with the first one's transaction
    still open and now carrying the second's half-written intent.

    The lock is the simplest correct answer for this design, and the
    alternative was considered and rejected: a connection per thread would
    make `BEGIN IMMEDIATE` genuinely concurrent, but it also fragments the CAS
    story — the fenced compare-and-swap is documented and tested as one
    transaction on one connection, and thread-local connections turn "the
    write lock is held across the read" into a claim about whichever
    connection this thread happened to get. Contention here is per-process and
    already bounded by SQLite's single-writer rule; the lock costs nothing the
    database was not going to charge anyway.

    It is an `RLock` because the SOP library nests: `_locked()` opens the
    transaction and the read and write inside it take the same lock. A plain
    `Lock` would turn that into a silent hang, which is the one failure mode
    worse than the error it replaces.
    """

    def _open(self, path: str | Path) -> None:
        self.path = Path(path)
        # Before the connection: a caller that raced `_open` must never find
        # the attribute missing.
        self._tx_lock = threading.RLock()
        self._conn = connect(self.path)

    @contextmanager
    def _write_tx(self) -> Iterator[sqlite3.Connection]:
        """One mutation, one transaction. `BEGIN IMMEDIATE`, so the write lock
        is taken before the read that the write is conditioned on.

        A raising body rolls back and leaves the database byte-identical,
        which is what makes the CAS and the fence safe to express as ordinary
        exceptions — the same property the JSONL store gets from doing its
        read-modify-write inside one flock.

        Held for the whole transaction, `BEGIN` through `COMMIT`: the lock is
        what makes "one transaction" true of the connection and not merely of
        the SQL text.
        """
        with self._tx_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    @contextmanager
    def _read_tx(self) -> Iterator[sqlite3.Connection]:
        """A read that must not observe another thread's open transaction.

        The connection is shared, so a SELECT issued while another thread is
        mid-`_write_tx` reads that thread's UNCOMMITTED rows — it is the same
        session, not a second reader. Reads therefore queue behind writes on
        the same lock rather than being merely "atomic enough".
        """
        with self._tx_lock:
            yield self._conn

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
        # Empty for the same reason it is empty on the JSONL side after a
        # read: a row this version cannot MODEL is not a quarantined line.
        # `Queue._read_all` says so at length — such a row is dropped from the
        # modelled set and left in place, and counting it here would report it
        # twice. `_read_raw` therefore returns no quarantine, and the write
        # paths refuse the row by name instead. What genuinely cannot happen
        # here is the JSONL case this list exists for: a byte sequence that is
        # not a record at all.
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
        with self._read_tx() as conn:
            return conn.execute(
                "SELECT * FROM work_items WHERE id = ?", (item_id,)
            ).fetchone()

    def _read_raw(self) -> tuple[list[dict], list[bytes]]:
        with self._read_tx() as conn:
            rows = conn.execute("SELECT * FROM work_items ORDER BY rowid").fetchall()
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
                    try:
                        existing = WorkItem.from_json(json.dumps(_row_to_dict(existing_row)))
                    except (ValueError, TypeError) as exc:
                        # The same boundary `work.py` holds on the JSONL side,
                        # in the same words. `status` is a TEXT column, so a
                        # newer writer's value is stored happily and only fails
                        # when this version tries to model it — and it fails
                        # here, on the duplicate scan, which means filing ANY
                        # item whose natural key matches would otherwise die
                        # with a bare enum error and duplicate suppression
                        # would stop working. It is the one thing the key
                        # exists to do.
                        raise WorkError(
                            f"cannot suppress a duplicate of {key!r}: the "
                            f"existing row is not readable by this version "
                            f"({type(exc).__name__}: {exc}). The row is "
                            f"preserved in the database. Creating a second item "
                            f"under the same key would be worse than refusing "
                            f"— upgrade, or repair the row."
                        ) from exc
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
            try:
                target = WorkItem.from_json(json.dumps(_row_to_dict(row)))
            except (ValueError, TypeError) as exc:
                # `_read_all` tolerates this row and every write path must
                # refuse it — a row this version cannot model is not a row it
                # may act on. Raising inside the transaction rolls it back, so
                # the refusal writes nothing.
                raise WorkError(
                    f"cannot act on {item_id}: its stored row is not readable "
                    f"by this version ({type(exc).__name__}: {exc}). The row is "
                    f"preserved in the database. Upgrade, or repair the row — "
                    f"this version must not overwrite a record it cannot "
                    f"understand."
                ) from exc
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
        # Rows this version cannot model, as raw `sqlite3.Row`s. The JSONL
        # library keeps raw BYTES here for the same reason: the point is to
        # carry the record through untouched, and anything parsed enough to
        # be a `SOP` is by definition not what is in this list. `revise()`
        # only asks whether it is empty and how long it is, so the two shapes
        # satisfy the same contract.
        self.quarantined: list[sqlite3.Row] = []

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._write_tx():
            yield

    def _read_all(self) -> list[SOP]:
        """Every readable version. An unreadable row is quarantined, not fatal.

        The parity that matters is with `SopLibrary._read_all`, which catches
        per LINE. Letting `_row_to_sop` raise out of here instead made ONE row
        written by a newer version — a `status` value this build has no name
        for, in a TEXT column that stores it happily — brick every SOP
        operation on the backend: `get`, `history`, `list_active`, `create`,
        `revise`, `activate`, all of which read through here.

        Populating `self.quarantined` is the other half, and it is not
        cosmetic: `revise()` refuses when it is non-empty, because the next
        version is `max(...) + 1` over what could be PARSED, so an invisible
        row silently frees its number for reissue to different text. With this
        list hardcoded empty, that refusal could never fire on this backend.
        """
        with self._read_tx() as conn:
            rows = conn.execute("SELECT * FROM sops ORDER BY sop_id, version").fetchall()
        out: list[SOP] = []
        quarantined: list[sqlite3.Row] = []
        for row in rows:
            try:
                out.append(_row_to_sop(row))
            except (ValueError, TypeError):
                quarantined.append(row)
        self.quarantined = quarantined
        return out

    def _write_all(self, sops: Sequence[SOP], quarantined: Sequence = ()) -> None:
        """Replace the table's contents. Called only inside `_locked`.

        The delete-then-insert is safe precisely because it is inside the
        caller's transaction: a reader either sees the whole previous history
        or the whole new one. Doing this outside a transaction would expose an
        instant in which every SOP has been destroyed — and `_write_all`'s own
        docstring in `sop.py` explains what a lost version row costs.

        Two things survive the delete that the `SOP` dataclass cannot carry:

          * **`unknown`**, per version, read back before the DELETE and
            re-attached to the row it belongs to. It is deliberately absent
            from `SOP_COLUMNS` — nothing here models those fields — and a
            rewrite that simply omitted it reset a newer writer's field to
            `{}` on every create, revise and activate. Migration 0002 promises
            the opposite in as many words, and the queue keeps that promise;
            this is the SOP side of it.
          * **Quarantined rows**, re-inserted verbatim. Same promise the JSONL
            `_write_all` keeps by writing the raw lines back out: preserving
            the bytes is what stops the data being lost, and only `revise()`'s
            refusal stops the NUMBER being reused. Dropping them here would
            make the refusal moot by deleting the thing it protects.
        """
        columns = SOP_COLUMNS + ("unknown",)
        quoted = ", ".join(_quote(c) for c in columns)
        placeholders = ", ".join("?" for _ in columns)
        insert = f"INSERT INTO sops ({quoted}) VALUES ({placeholders})"
        with self._read_tx() as conn:
            preserved = {
                (row["sop_id"], row["version"]): row["unknown"]
                for row in conn.execute("SELECT sop_id, version, unknown FROM sops")
            }
            conn.execute("DELETE FROM sops")
            for sop in sops:
                conn.execute(
                    insert,
                    _sop_to_row(sop)
                    + (preserved.get((sop.sop_id, sop.version), "{}"),),
                )
            for row in quarantined:
                conn.execute(insert, tuple(row[c] for c in columns))
