"""Backend conformance fixtures.

A backend contract is a claim about behaviour, and a claim nothing tests
against a second implementation is a claim about one implementation. So the
work-queue and SOP-library tests run **twice** — once against the JSONL store
that ships as the default, once against the SQLite store — from the same test
bodies.

Two fixtures, deliberately not one:

  * ``queue`` / ``library`` are **parametrised**. Every test taking them is a
    conformance test and runs on both backends.
  * ``jsonl_queue`` is **not**. A handful of tests assert properties of the
    file format itself — quarantined bytes surviving a write, an atomic
    ``os.replace``, a byte-identical file after a refused claim. Those are
    statements about JSONL and meaningless against a database, and
    parametrising them would produce either a skip that reads as a pass or an
    assertion rewritten until it no longer said anything. The SQLite backend
    gets its own equivalents in ``tests/test_sqlstore.py``.

The SQLite parameter points both stores at ONE file, because that is the
deployment shape: `AGENTCO_DB` names a single database and the work items,
SOPs and registry tables live in it together.

A third parameter, ``postgres``, joins ``BACKENDS`` only when
``AGENTCO_TEST_PG`` names a DSN — a Postgres server is not assumed to be
running, and a suite that silently dropped this backend when it is not set
would be a suite whose green run stopped proving anything about it without
saying so. There is no separate skip-with-reason for it: parametrised fixture
values are what the conformance claim is stated in terms of, so leaving
``postgres`` out of the tuple entirely (rather than adding it and then
skipping every test) is what keeps a run with the variable unset from
reporting a pass count that quietly excludes it.

Each test gets a fresh, disposable Postgres **schema**, not a fresh
database — creating a database needs a privilege the test role may not have
and is slow enough per-test to change what "run the suite" costs; a schema is
namespace-cheap and just as isolated for this codebase's purposes, since
nothing here ever queries across schemas. `_pg_dsn` creates one, points a DSN
at it via `search_path`, and drops it again after the test — `CASCADE`
because a test that leaves the queue mid-transaction on failure can leave
tables the plain `DROP SCHEMA` would otherwise refuse.
"""

from __future__ import annotations

import os
import uuid

import pytest

from agentco.sop import SopLibrary
from agentco.sqlstore import SqlQueue, SqlSopLibrary
from agentco.work import Queue

PG_ENV_VAR = "AGENTCO_TEST_PG"

BACKENDS = ("jsonl", "sqlite")
if os.environ.get(PG_ENV_VAR):
    BACKENDS = BACKENDS + ("postgres",)


@pytest.fixture(params=BACKENDS)
def backend(request) -> str:
    return request.param


@pytest.fixture()
def _pg_dsn(request):
    """A DSN pointed at a fresh, empty schema in `AGENTCO_TEST_PG`'s database.

    Imports `psycopg` lazily — this fixture is only ever instantiated via
    `request.getfixturevalue` from inside the `if backend == "postgres":`
    branches below, so a `jsonl`/`sqlite` test run never needs the optional
    dependency installed at all.
    """
    import psycopg

    base = os.environ[PG_ENV_VAR]
    schema = f"pgtest_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(base, autocommit=True) as boot:
        boot.execute(f'CREATE SCHEMA "{schema}"')
    sep = "&" if "?" in base else "?"
    dsn = f"{base}{sep}options=-csearch_path%3D{schema}"
    yield dsn
    with psycopg.connect(base, autocommit=True) as boot:
        boot.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture()
def queue(backend, tmp_path, request):
    if backend == "jsonl":
        return Queue(tmp_path / "work.jsonl")
    if backend == "postgres":
        # `getfixturevalue`, not a plain fixture argument: a test using ONLY
        # `queue` (never `library`) must not pay for a schema when the
        # backend is jsonl/sqlite, and a test using BOTH gets the SAME schema
        # for both — pytest caches a function-scoped fixture's value once per
        # test, so the second `getfixturevalue("_pg_dsn")` call (from
        # `library`, below) returns the identical DSN rather than a second
        # schema. That is the same "one string, both stores" shape the
        # SQLite parameter already has.
        return SqlQueue(request.getfixturevalue("_pg_dsn"))
    return SqlQueue(tmp_path / "agentco.sqlite3")


@pytest.fixture()
def library(backend, tmp_path, request):
    if backend == "jsonl":
        return SopLibrary(tmp_path / "sops.jsonl")
    if backend == "postgres":
        return SqlSopLibrary(request.getfixturevalue("_pg_dsn"))
    return SqlSopLibrary(tmp_path / "agentco.sqlite3")


@pytest.fixture()
def jsonl_queue(tmp_path):
    """The default backend only — for tests about the file format itself."""
    return Queue(tmp_path / "work.jsonl")
