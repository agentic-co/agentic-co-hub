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
from dataclasses import asdict, fields
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence

from agentco import migrations, policy
from agentco.db import BUSY_TIMEOUT_MS
from agentco.pgadapter import PgConnection, is_postgres_target
from agentco.sop import ASOP, SOP, SopLibrary, SopStatus, Step
from agentco.work import (
    Queue,
    WorkError,
    WorkItem,
    WorkStatus,
    _iso,
    _now,
    build_item,
    enforce_decomposition,
    is_child_row,
)

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
    "verify",
    "attestation",
    "verify_failures",
)
_WORK_JSON_COLUMNS = frozenset({"requires", "blocked_by", "metadata"})
# JSON columns whose NULL means something. `verify IS NULL` is "ungated", and
# collapsing it to `{}` on the way in would gate every item ever filed; the
# same round trip must also give back None rather than an empty dict, or
# `WorkItem.is_gated` becomes true for everything.
_WORK_NULLABLE_JSON_COLUMNS = frozenset({"verify", "attestation"})
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
    "executor",
    "tags",
    "author",
    "author_kind",
    "proposals",
    "superseded_by",
    "created_at",
)
_SOP_JSON_COLUMNS = frozenset({"common_mistakes", "tags", "proposals"})

# The v3 record. Every `ASOP` field is a column of the same name, asserted at
# import for the same reason `WORK_COLUMNS` is: a field with no column does not
# fail, it silently does not persist.
#
# `SOP_COLUMNS` stays above even though nothing reads or writes the `sops`
# table any more. The table stays — migration 0009 copies its rows forward as
# one-step ASOPs rather than rewriting them in place, so the legacy record is
# still there if a rollback ever needs it — and the assertion below is what
# keeps this file's description of that table true while it sits there. The
# row codecs that used it are gone with their callers.
ASOP_COLUMNS: tuple[str, ...] = (
    "asop_id",
    "version",
    "title",
    "status",
    "task_type",
    "purpose",
    "trigger",
    "inputs",
    "roles",
    "constraints",
    "steps",
    "author",
    "author_kind",
    "proposals",
    "superseded_by",
    "created_at",
)
_ASOP_JSON_COLUMNS = frozenset({"inputs", "roles", "constraints", "steps", "proposals"})
# What an EMPTY value of each JSON column serialises to. `roles` is an object
# and the rest are arrays; one shared `or []` would have written `[]` into a
# column every reader decodes as a mapping.
_ASOP_JSON_EMPTY = {"inputs": [], "roles": {}, "constraints": [], "steps": [], "proposals": []}


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
_check_columns(ASOP, ASOP_COLUMNS)


def _quote(column: str) -> str:
    """`trigger` is a SQLite keyword. Quote every identifier, not just that one."""
    return f'"{column}"'


def connect(path: str | Path) -> sqlite3.Connection | PgConnection:
    """Open the durable store, migrated, in autocommit so BEGIN is explicit.

    `isolation_level=None` because every write here is wrapped in an explicit
    `BEGIN IMMEDIATE`. Leaving Python's implicit transaction handling on would
    mean the transaction that matters starts somewhere the code does not say.

    `path` is a Postgres DSN when `AGENTCO_DB` names one — `agentco/stores.py`
    hands the same string to `SqlQueue`/`SqlSopLibrary` that it hands to
    `db.connect` for the registry tables, because "one file for both" is the
    storage design, and a DSN is that file's equivalent for this backend. See
    `agentco/pgadapter.py` for what changes and what does not.
    """
    if is_postgres_target(path):
        conn: sqlite3.Connection | PgConnection = PgConnection(str(path))
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        migrations.apply(conn)
        return conn
    sqlite_path = Path(path)
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(sqlite_path), isolation_level=None, check_same_thread=False)
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
        # A Postgres DSN is not a filesystem path — `Path("postgresql://...")`
        # would not fail here, but `path.parent.mkdir(...)` two lines into
        # `connect()` would try to create a directory named `postgresql:`.
        # `.path` is kept as the raw string for that case; every existing use
        # of `.path` (tests comparing it to a `tmp_path` file, `jsonl_queue`
        # reading it as a file) is on the SQLite/JSONL side, never on a DSN.
        self.path = path if is_postgres_target(path) else Path(path)
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
        elif column in _WORK_NULLABLE_JSON_COLUMNS:
            value = None if value is None else json.dumps(value)
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
        if column in _WORK_NULLABLE_JSON_COLUMNS:
            out[column] = None if value is None else json.loads(value)
        elif column in _WORK_JSON_COLUMNS:
            out[column] = json.loads(value)
        else:
            out[column] = value
    return out


class SqlQueue(_SqlBacked, Queue):
    """The work queue on SQLite. Same protocol, different storage underneath."""

    def __init__(self, path: Path | str = "agentco.sqlite3", verifiers: Optional[Sequence[str]] = None,
                 humans: Optional[Sequence[str]] = None,
                 adjudicators: Optional[Sequence[str]] = None):
        self._open(path)
        # Every declaration `Queue.__init__` sets, set here too. This class
        # does NOT call super().__init__ — it opens a database instead of a
        # file — so a declaration added to the base and forgotten here does
        # not fail loudly, it fails as `AttributeError: 'SqlQueue' object has
        # no attribute 'humans'` from inside `adjudicate`, on one backend
        # only, where a refusal was supposed to be. Which is exactly what
        # happened to `humans`/`adjudicators` when they were added.
        self.verifiers: frozenset[str] = (
            frozenset(verifiers) if verifiers is not None else policy.verifiers_from_env()
        )
        self.humans: frozenset[str] = (
            frozenset(humans) if humans is not None else policy.humans_from_env()
        )
        self.adjudicators: frozenset[str] = (
            frozenset(adjudicators) if adjudicators is not None
            else policy.adjudicators_from_env()
        )
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
            # `rowid` is SQLite's implicit, UPDATE-stable insertion-order
            # column; Postgres has no equivalent (`ctid` moves on every
            # UPDATE, which this table gets constantly via leases and
            # results) so migration 0008 adds `ordinal`, an identity column,
            # for exactly this ORDER BY. See that migration's docstring.
            order_col = "ordinal" if getattr(conn, "dialect", "sqlite") == "postgres" else "rowid"
            rows = conn.execute(f"SELECT * FROM work_items ORDER BY {order_col}").fetchall()
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
        verify: Optional[dict] = None,
        by_plane: bool = False,
    ) -> WorkItem:
        """One item, with the same loud duplicate suppression as the JSONL store.

        The lookup and the insert are one `BEGIN IMMEDIATE` transaction, and
        the unique partial index on `natural_key` is the rule underneath —
        so a concurrent create with the same key converges on one row whether
        or not the check saw it.

        Construction — the natural key and the gate validation — is
        `work.build_item`, shared with the JSONL path. A second copy of those
        rules here is the drift this backend is most likely to introduce, and
        it would show up as a gate refused on one backend and stored on the
        other.
        """
        item = build_item(
            title,
            requires=requires,
            blocked_by=blocked_by,
            assigned_agent=assigned_agent,
            natural_key=natural_key,
            source=source,
            source_id=source_id,
            kind=kind,
            subject=subject,
            period=period,
            metadata=metadata,
            verify=verify,
            by_plane=by_plane,
        )
        key = item.natural_key

        with self._write_tx() as conn:
            if key:
                if getattr(conn, "dialect", "sqlite") == "postgres":
                    # `BEGIN IMMEDIATE`'s whole-database write lock is what
                    # makes the check-then-insert below safe on SQLite: a
                    # second `create()` racing on the SAME key cannot even
                    # start its own SELECT until this transaction commits, so
                    # it always sees whichever row won. Postgres's plain
                    # `BEGIN` has no such lock, and `SELECT ... FOR UPDATE`
                    # (the fix used in `_mutate` for the analogous claim race)
                    # does not apply here — there is no ROW yet for a
                    # genuinely new key, so there is nothing to lock. What
                    # serialises two creates racing on the SAME key instead is
                    # a lock on the KEY ITSELF: `pg_advisory_xact_lock` takes
                    # it now and releases it automatically at this
                    # transaction's COMMIT or ROLLBACK, never left dangling.
                    # Scoped to one key, not the whole table — an unrelated
                    # `create()` under a different key is not serialised
                    # against this one, matching SQLite's own behaviour under
                    # WAL for anything that is not this specific contest.
                    conn.execute("SELECT pg_advisory_xact_lock(hashtext(?)::bigint)", (key,))
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

            def lookup(item_id: str) -> Optional[dict]:
                found = conn.execute(
                    "SELECT * FROM work_items WHERE id = ?", (item_id,)
                ).fetchone()
                return _row_to_dict(found) if found is not None else None

            def count_children(parent_id: str) -> int:
                # Counted in Python over every row's metadata, the way the
                # JSONL store counts — one rule for what a child IS beats two.
                # A LIKE pre-filter on the serialised JSON was considered and
                # rejected: a newer writer's spacing would make it undercount,
                # and an undercount here is a bound that silently stopped
                # holding. Creates are not the hot path; claims are.
                rows = conn.execute(
                    "SELECT metadata FROM work_items WHERE metadata IS NOT NULL"
                ).fetchall()
                return sum(
                    1 for r in rows
                    if is_child_row({"metadata": json.loads(r["metadata"] or "{}")}, parent_id)
                )

            blocked_parent = enforce_decomposition(
                item, lookup=lookup, count_children=count_children
            )
            if blocked_parent is not None:
                parent = lookup(blocked_parent)
                blocked = sorted(set(parent.get("blocked_by") or []) | {item.id})
                conn.execute(
                    "UPDATE work_items SET blocked_by = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(blocked), _iso(_now()), blocked_parent),
                )
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

        **`FOR UPDATE` on Postgres, nothing extra on SQLite.** SQLite's
        `BEGIN IMMEDIATE` (`_write_tx`) takes a whole-database write lock
        before this SELECT runs, so two racing claims are fully serialised:
        the second one's SELECT does not start until the first's transaction
        has committed, and it therefore sees the lease already held — `cas()`
        raises the ordinary `LeaseError`, "someone else got it first". Under
        Postgres's default READ COMMITTED with a plain `BEGIN`, there is no
        such lock: two concurrent transactions could both SELECT the row
        while it is still unleased, both decide (correctly, on what they each
        read) to grant the lease, and only then discover the conflict at the
        UPDATE — which the compare-and-swap WHERE clause does catch, but as a
        `WorkError` ("matched 0 rows"), not the clean `LeaseError` a caller
        expects for a lost race (see `tests/test_sqlstore.py`'s twelve-process
        test, which asserts every loser comes back with `won=False`, never an
        exception). `SELECT ... FOR UPDATE` closes that gap the same way
        `BEGIN IMMEDIATE` does: the second transaction's SELECT blocks until
        the first commits, then re-reads the row's now-current (leased)
        state, so `cas()` sees what SQLite's readers see and raises the same
        `LeaseError`. A row-level lock, not `pg_advisory_xact_lock` or a
        table lock — the contested resource is this one row, and locking
        anything wider would serialise claims on UNRELATED items too, which
        SQLite's own design does not do either (WAL lets readers and writers
        on different rows proceed; only this specific race needs blocking).
        """
        with self._write_tx() as conn:
            suffix = " FOR UPDATE" if getattr(conn, "dialect", "sqlite") == "postgres" else ""
            row = conn.execute(
                f"SELECT * FROM work_items WHERE id = ?{suffix}", (item_id,)
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


def _asop_to_row(asop: ASOP) -> tuple:
    values = []
    for column in ASOP_COLUMNS:
        value = getattr(asop, column)
        if column == "status":
            value = asop.status.value
        elif column == "steps":
            value = json.dumps([asdict(step) for step in value or []])
        elif column in _ASOP_JSON_COLUMNS:
            value = json.dumps(value if value else _ASOP_JSON_EMPTY[column])
        values.append(value)
    return tuple(values)


def _row_to_asop(row: sqlite3.Row) -> ASOP:
    data = dict(json.loads(row["unknown"] or "{}"))
    for column in ASOP_COLUMNS:
        value = row[column]
        data[column] = json.loads(value) if column in _ASOP_JSON_COLUMNS else value
    data["status"] = SopStatus(data["status"])
    data["steps"] = [Step(**step) for step in data.get("steps") or []]
    known = {f.name for f in fields(ASOP)}
    return ASOP(**{k: v for k, v in data.items() if k in known})


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

    def __init__(self, path: Path | str = "agentco.sqlite3", protected_tags: Optional[Sequence[str]] = None):
        self._open(path)
        self.protected_tags = (
            frozenset(protected_tags) if protected_tags is not None
            else policy.protected_tags_from_env()
        )
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

    def _read_all(self) -> list[ASOP]:
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

        **`FOR UPDATE` when called from inside `_locked()`, nothing extra as
        a plain read.** `create`/`revise`/`activate` all read the whole
        history and write it back inside one `_locked()` transaction — under
        SQLite's `BEGIN IMMEDIATE` that whole read-modify-write is already
        serialised against a second author, so two concurrent `revise()`
        calls cannot both compute the same "next version" number. Postgres's
        plain `BEGIN` gives no such guarantee, and this table has no targeted
        row to lock the way `_mutate` locks one work item — the read here IS
        the whole table. `conn.in_transaction` is how this method tells the
        two callers apart without a second method or a parameter every
        caller would have to remember to pass: true only when a `_locked()`
        block is already open (`get`/`history`/`list_active` call this
        outside one, and stay plain reads that do not block on an unrelated
        writer, matching SQLite/WAL's own readers-don't-block-writers
        behaviour there).
        """
        with self._read_tx() as conn:
            postgres = getattr(conn, "dialect", "sqlite") == "postgres"
            suffix = " FOR UPDATE" if postgres and conn.in_transaction else ""
            rows = conn.execute(f"SELECT * FROM asops ORDER BY asop_id, version{suffix}").fetchall()
        out: list[ASOP] = []
        quarantined: list[sqlite3.Row] = []
        for row in rows:
            try:
                out.append(_row_to_asop(row))
            except (ValueError, TypeError):
                quarantined.append(row)
        self.quarantined = quarantined
        return out

    def _write_all(self, asops: Sequence[ASOP], quarantined: Sequence = ()) -> None:
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
        columns = ASOP_COLUMNS + ("unknown",)
        quoted = ", ".join(_quote(c) for c in columns)
        placeholders = ", ".join("?" for _ in columns)
        insert = f"INSERT INTO asops ({quoted}) VALUES ({placeholders})"
        clear = "DELETE FROM asops"
        with self._read_tx() as conn:
            preserved = {
                (row["asop_id"], row["version"]): row["unknown"]
                for row in conn.execute("SELECT asop_id, version, unknown FROM asops")
            }
            conn.execute(clear)
            for asop in asops:
                conn.execute(
                    insert,
                    _asop_to_row(asop)
                    + (preserved.get((asop.asop_id, asop.version), "{}"),),
                )
            for row in quarantined:
                conn.execute(insert, tuple(row[c] for c in columns))
