"""A thin adapter so the existing SQL keeps working against Postgres.

The storage design (`db.py`) is "one SQLite file behind three plain HTTP
endpoints"; this module makes it "one SQLite file, OR one Postgres database,
behind the same code". Every call site in `events.py`, `leases.py`,
`snapshots.py`, `metrics.py`, `divergence.py`, `migrations.py` and
`sqlstore.py` was written against `sqlite3.Connection` — `?` placeholders,
row-by-column-name AND row-by-index reads, `with conn:` for a transaction,
`conn.execute("BEGIN IMMEDIATE")` / `"COMMIT"` / `"ROLLBACK"` as literal SQL
text, `cur.lastrowid`, `conn.in_transaction`, `conn.isolation_level`. Rather
than a second copy of every query in a PG dialect, `PgConnection` and
`PgCursor` present that same surface over a real `psycopg` connection, so the
query text in every one of those modules is untouched.

What is NOT here: a connection pool, an ORM, ANY new SQL. `sqlstore.py`
already says the design's position on that ("connection pooling, an ORM, a
second dialect" are the things deliberately absent from the SQLite backend);
this module is the seam that makes a second *transport* possible without a
second *dialect* existing in the codebase.

**Translation is textual, not a SQL parser**, and deliberately narrow:

  * `?` → `%s` — both drivers are purely positional, so a blind replace is
    correct. Nothing in this codebase's SQL text contains a literal `?`
    outside a placeholder (no LIKE wildcards, no JSONB `?` operators — the
    JSON columns are TEXT, encoded/decoded in Python).
  * `PRAGMA quick_check(1)` → `SELECT 'ok'`. There is no hosted-Postgres
    equivalent of a page-level integrity scan, and the brief for this probe
    (`pulse.check_plane`) asks for exactly this: "a trivial SELECT 1
    integrity probe". Doing the translation here means `pulse.py` needed no
    change for it at all.
  * `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON` → no-ops. WAL is a
    SQLite file-locking mode; Postgres's MVCC needs no equivalent knob, and
    foreign keys are always enforced.
  * `PRAGMA busy_timeout=<ms>` → `SET lock_timeout='<ms>ms'` on the session —
    the same intent (give up waiting for a lock after N ms instead of hanging
    forever), the Postgres session-level knob for it.
  * `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`, sent as literal SQL text
    throughout `sqlstore.py`, `migrations.py` and `pulse.py`, are intercepted
    rather than sent to the server — Postgres has no `IMMEDIATE` modifier.
    They toggle `psycopg`'s own `autocommit` flag, which is this adapter's
    transaction control (see `PgConnection` below for why that is still
    correct without SQLite's whole-database exclusive lock).
  * `INSERT INTO events(...)` gets `RETURNING seq` appended, because
    `events.append` reads `cur.lastrowid` — which `psycopg` does not
    populate — and `seq` is the one AUTOINCREMENT column anything reads back
    that way.

**Why `BEGIN IMMEDIATE`'s exclusivity does not need to survive the
translation.** SQLite's variant takes the whole-database write lock before
the read the transaction is conditioned on, so a second writer can never
interleave between "read the row" and "write it back". Postgres has no
whole-database lock to ask for, and does not need one: `sqlstore._mutate`'s
compare-and-swap is expressed as `UPDATE ... WHERE id = ? AND lease_attempt =
? AND updated_at = ?`, and that WHERE clause is stated in the docstring as
"a backstop that fails loudly rather than silently overwriting if [the
IMMEDIATE lock] ever stops being true" — which is exactly what carries the
correctness under Postgres's own READ COMMITTED semantics: a concurrent
UPDATE that touched the row first makes the second UPDATE's WHERE clause not
match the row's current values, so it matches zero rows and raises the same
`WorkError` SQLite's belt-and-suspenders check exists to raise. The one place
this is not automatically true is `SqlQueue.create`'s natural-key duplicate
check (a SELECT with no matching row yet, so there is nothing for Postgres to
lock) — that path is hardened separately, see `sqlstore.SqlQueue.create`.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Any, Optional, Sequence

try:
    import psycopg
    import psycopg.errors
    from psycopg_pool import ConnectionPool
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    psycopg = None
    ConnectionPool = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


#: How many server connections one process may hold. The ceiling matters more
#: than the floor: this backend exists to be pointed at a SHARED Postgres, and
#: a process that opens as many connections as it has threads is a process that
#: can exhaust a server other applications are also using.
DEFAULT_POOL_MIN = 1
DEFAULT_POOL_MAX = 10
POOL_MIN_ENV_VAR = "AGENTCO_PG_POOL_MIN"
POOL_MAX_ENV_VAR = "AGENTCO_PG_POOL_MAX"


#: `AGENTCO_DB=postgresql://...` or `AGENTCO_DB=postgres://...` selects this
#: backend; anything else is a filesystem path and stays SQLite. Checked with
#: a plain prefix test — no URL parsing needed to answer "which backend".
POSTGRES_SCHEMES = ("postgresql://", "postgres://")


def _pool_bounds() -> tuple[int, int]:
    """`(min, max)` connections, from the environment, clamped to something sane.

    An operator pointing this at a shared server needs to be able to say how
    much of it this process may take, without editing code. A max below one is
    a process that cannot talk to its database, and a min above max is a
    configuration that would refuse to open — both are corrected here rather
    than raised, because a registry that will not start over a pool size is a
    worse outcome than one that starts with a sensible number.
    """
    def _read(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            return default
    maximum = max(1, _read(POOL_MAX_ENV_VAR, DEFAULT_POOL_MAX))
    minimum = max(0, min(_read(POOL_MIN_ENV_VAR, DEFAULT_POOL_MIN), maximum))
    return minimum, maximum


def is_postgres_target(target: Any) -> bool:
    """Is `target` a Postgres DSN, as opposed to a SQLite file path?"""
    return isinstance(target, str) and target.lower().startswith(POSTGRES_SCHEMES)


class OperationalError(Exception):
    """Raised for a Postgres failure a caller wants to treat the way it would
    treat `sqlite3.OperationalError` — a lock or connection problem, not a
    programming error. `pulse.check_plane`'s write-lock probe is the one
    place that distinguishes this from every other failure, and it catches
    this alongside `sqlite3.OperationalError` rather than psycopg's own
    exception hierarchy, so that check reads the same on both backends."""


def _translate_placeholders(sql: str) -> str:
    return sql.replace("?", "%s")


_PRAGMA_NOOP = re.compile(r"^\s*PRAGMA\s+(journal_mode|foreign_keys)\s*=", re.IGNORECASE)
_PRAGMA_BUSY_TIMEOUT = re.compile(r"^\s*PRAGMA\s+busy_timeout\s*=\s*(\d+)\s*$", re.IGNORECASE)
_PRAGMA_QUICK_CHECK = re.compile(r"^\s*PRAGMA\s+quick_check", re.IGNORECASE)
_INSERT_INTO_EVENTS = re.compile(r"^\s*INSERT\s+INTO\s+events\s*\(", re.IGNORECASE)


class PgCursor:
    """The `sqlite3.Cursor` subset the codebase reads: `fetchone`, `fetchall`,
    `lastrowid`, `rowcount`. Results are fetched eagerly at `execute()` time
    (see `PgConnection.execute`), so this class is a plain holder for them —
    it does nothing lazily and therefore needs nothing from the network
    after construction, which is what makes it safe to hand back after the
    connection lock has been released.
    """

    __slots__ = ("_rows", "_index", "lastrowid", "rowcount")

    def __init__(self, rows: list["PgRow"], *, lastrowid: Optional[int], rowcount: int) -> None:
        self._rows = rows
        self._index = 0
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def fetchone(self) -> Optional["PgRow"]:
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchall(self) -> list["PgRow"]:
        rest = self._rows[self._index:]
        self._index = len(self._rows)
        return rest

    def __iter__(self):
        return iter(self.fetchall())


class PgRow:
    """A result row addressable by column name (`row["col"]`, what every
    caller in this codebase uses) AND by position (`row[0]`, what
    `migrations.applied_versions` uses) — the same two access modes
    `sqlite3.Row` supports, over a `psycopg` row that (with any single
    built-in row factory) only supports one or the other.
    """

    __slots__ = ("_columns", "_values")

    def __init__(self, columns: Sequence[str], values: Sequence[Any]) -> None:
        self._columns = columns
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return self._values[self._columns.index(key)]
            except ValueError:
                raise KeyError(key) from None
        return self._values[key]

    def keys(self) -> list[str]:
        return list(self._columns)

    def __contains__(self, key: str) -> bool:
        return key in self._columns

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"PgRow({dict(zip(self._columns, self._values))!r})"


class PgConnection:
    """Presents the `sqlite3.Connection` surface this codebase uses, over one
    `psycopg` connection.

    **Transaction model.** The underlying connection runs `autocommit=True`:
    every bare `execute()` — every plain SELECT with no surrounding `with
    conn:` — is its own committed statement, which is what a bare read
    (`events.read`, `metrics.verb_latency`, ...) needs and what the codebase
    never wraps in a transaction. `with conn:` (used the same way
    `sqlite3.Connection`'s own context-manager protocol is used throughout
    `events.py`/`leases.py`/`snapshots.py`) and the three literal strings
    `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` (used the same way in
    `sqlstore.py`, `migrations.py` and `pulse.py`) toggle `autocommit` off
    for the duration and commit or roll back on exit — a depth counter makes
    this correct if a caller ever nests one inside the other, though nothing
    in this codebase currently does.

    **Thread safety.** One connection object is shared across a threaded
    server the same way `app.py` shares one `sqlite3.Connection` with
    `check_same_thread=False` — correct there because SQLite serialises
    internally. `psycopg`'s connection is not safe for concurrent use from
    multiple threads, so `execute()` takes an `RLock` for its full duration,
    including result materialisation, and `__enter__`/literal `BEGIN
    IMMEDIATE` hold that same lock across every statement until `__exit__`/
    `COMMIT`/`ROLLBACK` releases it — the same shape `sqlstore._SqlBacked`
    already documents and tests for the SQLite backend's own multi-statement
    transactions.
    """

    dialect = "postgres"

    def __init__(self, dsn: str) -> None:
        if psycopg is None:  # pragma: no cover - exercised only without the extra
            raise RuntimeError(
                "AGENTCO_DB names a postgresql:// target but 'psycopg' is not "
                "installed. Install the optional dependency: "
                "pip install 'agentco[postgres]' (or uv add --optional postgres "
                "'psycopg[binary]')."
            ) from _IMPORT_ERROR
        # A POOL, not a connection, and no process-wide lock around it.
        #
        # What stood here was `psycopg.connect(...)` plus a `threading.RLock`
        # held for the duration of every `execute`. One connection is a
        # capacity decision; the LOCK was the real ceiling, because it made
        # every request in the process wait for every other one regardless of
        # what the database could have served in parallel. A hundred pollers
        # against that is a hundred pollers in a queue of one.
        #
        # Removing it is safe for the reason this module's own docstring
        # already gives for not needing `BEGIN IMMEDIATE`'s whole-database
        # lock: correctness here rests on the compare-and-swap in
        # `sqlstore._mutate`'s WHERE clause, which fails loudly rather than
        # overwriting, not on callers being serialised. The lock was never
        # what made concurrent writers correct; it only made them rare.
        #
        # Transactions still need ONE connection for their whole span, so a
        # thread checks one out at `_begin` and returns it at `_end`. Outside
        # a transaction each statement borrows and returns immediately, which
        # is what lets a small pool serve many more callers than it has
        # connections.
        self._min, self._max = _pool_bounds()
        self._lock_timeout_ms: Optional[int] = None
        self._pool = ConnectionPool(
            dsn,
            min_size=self._min,
            max_size=self._max,
            kwargs={"autocommit": True},
            configure=self._configure,
            open=True,
        )
        self._local = threading.local()
        # Compatibility attribute only — `migrations.apply` saves, sets to
        # `None`, and restores this. This adapter always manages transactions
        # explicitly (see class docstring), so the value itself does nothing;
        # it exists so that code written against `sqlite3.Connection` does
        # not need a branch to skip it.
        self.isolation_level: Optional[str] = None
        self.row_factory = None  # compatibility only; rows are always PgRow

    # -- transaction control ---------------------------------------------

    def _configure(self, conn) -> None:
        """Applied to every connection the pool opens, including replacements.

        Session settings have to live here rather than on one connection,
        because with a pool there is no "the" connection to set them on — and a
        `lock_timeout` that held only for whichever connection happened to be
        first would be a timeout that applies to some requests and not others.
        """
        if self._lock_timeout_ms is not None:
            conn.execute(f"SET lock_timeout = '{self._lock_timeout_ms}ms'")

    @property
    def _depth(self) -> int:
        return getattr(self._local, "depth", 0)

    @property
    def in_transaction(self) -> bool:
        """Per THREAD, which is the only reading that means anything now.

        Two threads can be inside transactions on two connections at the same
        instant; a process-wide answer would be true for a caller that is not
        in one.
        """
        return self._depth > 0

    def _begin(self) -> None:
        if self._depth == 0:
            conn = self._pool.getconn()
            conn.autocommit = False
            self._local.conn = conn
        self._local.depth = self._depth + 1

    def _end(self, *, commit: bool) -> None:
        depth = self._depth - 1
        if depth < 0:  # pragma: no cover - defensive, should not happen
            depth = 0
        self._local.depth = depth
        if depth == 0:
            conn = self._local.conn
            self._local.conn = None
            try:
                if commit:
                    conn.commit()
                else:
                    conn.rollback()
                conn.autocommit = True
            finally:
                # Returned even if the commit raised: a connection kept out of
                # the pool by a failure is a connection the pool never gets
                # back, and enough of those is an outage that looks like a hang.
                self._pool.putconn(conn)

    def __enter__(self) -> "PgConnection":
        self._begin()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._end(commit=exc_type is None)
        return False

    # -- the one entry point every caller uses ----------------------------

    def execute(self, sql: str, params: Sequence[Any] = ()) -> PgCursor:
        """One statement. Inside a transaction it runs on that transaction's
        pinned connection; outside one it borrows from the pool and gives it
        straight back, which is what lets ten connections serve a hundred
        pollers.
        """
        if self._depth > 0:
            return self._execute_on(self._local.conn, sql, params)
        with self._pool.connection() as conn:
            return self._execute_on(conn, sql, params)

    def _execute_on(self, conn, sql: str, params: Sequence[Any] = ()) -> PgCursor:
        """The translation layer, against whichever connection the caller holds."""
        if True:  # keeps the body's indentation; see `execute` for the choice of conn
            stripped = sql.strip()
            upper = stripped.upper()

            if upper == "BEGIN IMMEDIATE" or upper == "BEGIN":
                self._begin()
                return PgCursor([], lastrowid=None, rowcount=0)
            if upper == "COMMIT":
                self._end(commit=True)
                return PgCursor([], lastrowid=None, rowcount=0)
            if upper == "ROLLBACK":
                self._end(commit=False)
                return PgCursor([], lastrowid=None, rowcount=0)
            if _PRAGMA_NOOP.match(stripped):
                return PgCursor([], lastrowid=None, rowcount=0)
            busy = _PRAGMA_BUSY_TIMEOUT.match(stripped)
            if busy:
                ms = int(busy.group(1))
                # Remembered as well as applied: `configure` replays it onto
                # every connection the pool opens later, so the timeout is a
                # property of the deployment rather than of whichever
                # connection happened to receive the PRAGMA.
                self._lock_timeout_ms = ms
                conn.execute(f"SET lock_timeout = '{ms}ms'")
                return PgCursor([], lastrowid=None, rowcount=0)
            if _PRAGMA_QUICK_CHECK.match(stripped):
                stripped = "SELECT 'ok'"

            translated = _translate_placeholders(stripped)
            returning_seq = False
            if _INSERT_INTO_EVENTS.match(stripped):
                # `events.append` reads `cur.lastrowid`; psycopg does not
                # populate it, so the one AUTOINCREMENT column anything reads
                # back that way is fetched explicitly via RETURNING.
                translated = f"{translated} RETURNING seq"
                returning_seq = True

            try:
                cur = conn.execute(translated, tuple(params) if params else None)
            except psycopg.errors.OperationalError as exc:
                raise OperationalError(str(exc)) from exc

            columns = tuple(d.name for d in cur.description) if cur.description else ()
            try:
                fetched = cur.fetchall() if cur.description else []
            except psycopg.ProgrammingError:
                fetched = []
            rows = [PgRow(columns, values) for values in fetched]
            lastrowid = None
            if returning_seq and rows:
                lastrowid = rows[0]["seq"]
            rowcount = cur.rowcount if cur.rowcount is not None else -1
            return PgCursor(rows, lastrowid=lastrowid, rowcount=rowcount)

    def executemany(self, sql: str, seq_of_params: Sequence[Sequence[Any]]) -> None:  # pragma: no cover - unused today
        translated = _translate_placeholders(sql.strip())
        rows = [tuple(p) for p in seq_of_params]
        if self._depth > 0:
            self._local.conn.executemany(translated, rows)
            return
        with self._pool.connection() as conn:
            conn.executemany(translated, rows)

    def commit(self) -> None:
        """Only meaningful inside a transaction now.

        Outside one every statement already committed as it ran, and there is
        no connection this object owns to commit — borrowing one from the pool
        to commit nothing on it would be a no-op wearing a method name.
        """
        if self._depth > 0:
            self._local.conn.commit()

    def rollback(self) -> None:
        if self._depth > 0:
            self._local.conn.rollback()

    def close(self) -> None:
        self._pool.close()


def connect(dsn: str) -> PgConnection:
    """Open a Postgres-backed connection presenting the sqlite3 surface.

    Callers are `agentco/db.py::connect` and `agentco/sqlstore.py::connect` —
    both apply `migrations.apply(conn)` themselves immediately afterwards,
    exactly as they do for the SQLite path, so this function does not apply
    migrations itself (one call site for "migrate on open", not two).
    """
    return PgConnection(dsn)
