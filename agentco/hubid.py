"""The hub's generated identity (ADR 0008 §3.1).

The configured id (`AGENTCO_HUB_ID`, `queue.hub_id`) is an operator string whose
default is `local` on every hub, so it binds nothing: two hubs with the default
cannot be told apart, and a signature naming "local" names no hub. The id an
envelope will name is therefore not configured. It is 128 random bits generated
at first start and kept in the registry database's `meta` table, the same place
the change feed keeps its retention floor, so it lives and moves with the
database it identifies and works the same on SQLite and Postgres.

The configured name stays a LABEL, and nothing that reads it changes meaning:
ownership records, shared memory's `authority` and the `AGENTCO_HUB_ID` setting
all keep using it. This module only adds the generated id beside it.
"""

from __future__ import annotations

import re
import secrets
from typing import Optional, TypeGuard

from agentco.errors import Refusal

#: Where the id is persisted in `meta`.
META_KEY = "hub_uid"

#: The configured label a hub has when nobody named it. It binds nothing.
UNBOUND_LABEL = "local"

_SHAPE = re.compile(r"^[0-9a-f]{32}$")


def _valid(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _SHAPE.match(value) is not None


def read(conn) -> Optional[str]:
    """The persisted id, or None when this database has never generated one."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (META_KEY,)).fetchone()
    return row[0] if row else None


def ensure(conn) -> str:
    """The hub's id: generated once, then the same on every later start.

    Insert-if-absent, then read back, so two processes starting together on one
    database both return whichever row won rather than each returning its own
    candidate. A stored value that is not 32 lowercase hex digits is refused
    rather than overwritten: replacing an id someone may have pinned would
    silently orphan every key enrolled against it.
    """
    with conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING",
            (META_KEY, secrets.token_hex(16)),
        )
    stored = read(conn)
    if not _valid(stored):
        raise Refusal(
            code="hub_id_invalid",
            message=f"the registry's stored hub id is not 32 lowercase hex digits: {stored!r}",
            remediation=(
                "Do not overwrite it blindly: a person's partition may have pinned it. Restore the "
                "registry database from a backup, or delete the `hub_uid` meta row only if no key "
                "was ever enrolled against this hub."
            ),
            http_status=500,
        )
    return stored


def require_bound(conn, label: Optional[str] = None) -> str:
    """The id an envelope may name, or a refusal saying why there is none yet.

    Envelopes cannot be enabled on a hub without a generated id, and not on one
    whose label is still `local` (ADR 0008 §3.1): a hub that cannot be told apart
    from every other default hub gives a signature nothing to bind to.
    """
    stored = read(conn)
    if not _valid(stored):
        raise Refusal(
            code="hub_id_unbound",
            message="this hub has no generated id, so an envelope has nothing to name",
            remediation=(
                "Start the hub once (`agentco serve`) or run `agentco hub-id`: the id is generated "
                "at first start and persisted in the registry database."
            ),
            http_status=409,
        )
    if label is not None and label.strip() == UNBOUND_LABEL:
        raise Refusal(
            code="hub_id_unbound",
            message=f"this hub's configured name is still {UNBOUND_LABEL!r}, the default every hub shares",
            remediation=(
                "Set AGENTCO_HUB_ID to a name for this hub before enabling envelopes. The name is a "
                "label for people; the generated id is what is signed."
            ),
            http_status=409,
        )
    return stored
