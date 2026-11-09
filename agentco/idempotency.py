"""Hub-side idempotency keys, and the one write transaction that carries them.

The outbox is at-least-once by design (F2): a drainer that dies after the HTTP
call and before settling its line republishes it. That is only cheap if the hub
answers the second send with the FIRST outcome. `attest` and `work_report` keep
their keys on the item they act on; the verbs here have no such record, so the
outcome is stored in `idempotency_keys` (migration 18), keyed
(actor, verb, key) so a key is never a way to read or pre-empt another actor's
call.

Two shapes, chosen by whether the verb's own write is in this database:

  * `claim_scope` and `snapshot` write here, so the lookup, the write, its
    events and the key commit in ONE transaction: there is no window.
  * `sop_revise`, `sop_activate` write to the SOP store, which may be a file.
    The key is recorded right after, so a crash in between can still repeat
    the write; `once` narrows that window, it cannot close it across stores.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional

from agentco.errors import Refusal

#: Newest keys retained per (actor, verb). A retry arrives within a drain cycle
#: or two; an unbounded table is a leak with a primary key.
KEPT = 500
MAX_KEY_LEN = 200


def clean(key: Any) -> Optional[str]:
    """The key as a string, `None` if absent, a refusal if it is not usable."""
    if key is None:
        return None
    if not isinstance(key, str) or not key.strip() or len(key) > MAX_KEY_LEN:
        raise Refusal(
            code="bad_idempotency_key",
            message=f"idempotencyKey must be a non-empty string of at most {MAX_KEY_LEN} characters",
            remediation="Send a stable unique string per logical call — the outbox uses its line id.",
            http_status=400,
        )
    return key


@contextmanager
def write_tx(conn, lock: Optional[str] = None) -> Iterator[None]:
    """One write transaction: `BEGIN IMMEDIATE` on SQLite.

    On Postgres `BEGIN IMMEDIATE` takes no lock, so `lock` names what to
    serialise on: a transaction-scoped advisory lock, released at commit. On
    SQLite the write lock already serialises everything, and `lock` is moot.
    A caller already inside a transaction keeps it — `events.append`'s own
    `with conn:` would commit it half-way, so code here uses `events.insert`.
    """
    postgres = getattr(conn, "dialect", "sqlite") == "postgres"
    if not postgres and conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        if postgres and lock:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (lock,))
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def recorded(conn, actor: str, verb: str, key: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT outcome FROM idempotency_keys WHERE actor = ? AND verb = ? AND idem_key = ?",
        (actor, verb, key),
    ).fetchone()
    return json.loads(row["outcome"]) if row is not None else None


def record(conn, actor: str, verb: str, key: str, outcome: dict) -> None:
    """Store the outcome (first writer wins) and trim to `KEPT`. Call inside a tx."""
    conn.execute(
        "INSERT INTO idempotency_keys(actor, verb, idem_key, outcome, recorded_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (actor, verb, idem_key) DO NOTHING",
        (actor, verb, key, json.dumps(outcome, sort_keys=True), datetime.now(timezone.utc).isoformat()),
    )
    conn.execute(
        "DELETE FROM idempotency_keys WHERE actor = ? AND verb = ? AND idem_key NOT IN ("
        "SELECT idem_key FROM idempotency_keys WHERE actor = ? AND verb = ? "
        "ORDER BY recorded_at DESC, idem_key LIMIT ?)",
        (actor, verb, actor, verb, KEPT),
    )


def once(conn, actor: str, verb: str, key: Optional[str], fn: Callable[[], dict]) -> dict:
    """Run `fn` unless this actor already ran `verb` under `key`; else replay its outcome."""
    if key is None:
        return fn()
    prior = recorded(conn, actor, verb, key)
    if prior is not None:
        return prior
    outcome = fn()
    with write_tx(conn, lock=f"idem:{actor}:{verb}:{key}"):
        record(conn, actor, verb, key, outcome)
    return outcome
