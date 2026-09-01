"""Which backend a surface opens, resolved in one place.

There are four surfaces — the HTTP app, the MCP server, the CLI, the session
hook — and `work.resolve_work_store` already exists because a queue is a
location two of them can disagree about. Backend *selection* is the same
problem one level up: a deployment where the MCP server reads JSONL while the
HTTP app writes SQLite presents as an empty queue on one side, not as an
error, and each side is internally consistent so neither logs anything.

One rule, one env var:

    AGENTCO_DB unset  →  the JSONL stores. Byte for byte today's behaviour.
    AGENTCO_DB=<path> →  the SQLite stores, both in that one file.

Unset is the default on purpose. The JSONL store is greppable at 02:00 and
diffs in review, which is worth more than durability to a single person
running one harness — and that person is who the project has to be usable by
before it is usable by anyone else. `AGENTCO_DB` is what a team turns on when
more than one process is opening the same store.

One file for both, and for the registry too when it has no path of its own,
because "one SQLite file" is the storage design (`db.py`) rather than an
accident of how many stores there happen to be. Two databases that both
happen to be SQLite would give up the one thing having a database buys here:
a work item and the event feed entry about it inside the same transaction, if
that is ever wanted.
"""

from __future__ import annotations

import os
from typing import Optional, Union

from agentco.sop import SopLibrary, resolve_sop_store
from agentco.sqlstore import SqlQueue, SqlSopLibrary
from agentco.work import Queue, resolve_work_store

AGENTCO_DB_ENV_VAR = "AGENTCO_DB"

AnyQueue = Union[Queue, SqlQueue]
AnySopLibrary = Union[SopLibrary, SqlSopLibrary]


def resolve_backend_db(db: Optional[str] = None) -> Optional[str]:
    """The durable-backend path, or `None` for the file stores.

    An empty string counts as unset. `AGENTCO_DB=` in a compose file or a
    `.mcp.json` env block is how a variable gets "cleared", and reading that
    as a request to open a database at the path `""` would fail somewhere far
    from the typo.
    """
    return (db or os.environ.get(AGENTCO_DB_ENV_VAR) or "").strip() or None


def open_queue(work_store: Optional[str] = None, *, db: Optional[str] = None) -> AnyQueue:
    target = resolve_backend_db(db)
    if target:
        return SqlQueue(target)
    return Queue(resolve_work_store(work_store))


def open_sop_library(
    sop_store: Optional[str] = None, *, db: Optional[str] = None
) -> AnySopLibrary:
    target = resolve_backend_db(db)
    if target:
        return SqlSopLibrary(target)
    return SopLibrary(resolve_sop_store(sop_store))


def resolve_registry_db(
    path: Optional[str], env_var: str, default: str
) -> str:
    """Where the registry tables live, with `AGENTCO_DB` as the middle default.

    Precedence: an explicit argument, then the registry's own env var, then
    `AGENTCO_DB`, then the default filename. `AGENTCO_DB` sits between the
    specific variable and the default rather than above it, so a deployment
    that has always pointed `AGENTCO_REGISTRY_DB` somewhere keeps pointing
    there — turning on the durable backend must not silently relocate a
    registry that already exists.
    """
    return (
        path
        or os.environ.get(env_var)
        or resolve_backend_db()
        or default
    )
