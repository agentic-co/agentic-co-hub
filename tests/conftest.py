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
"""

from __future__ import annotations

import pytest

from agentco.sop import SopLibrary
from agentco.sqlstore import SqlQueue, SqlSopLibrary
from agentco.work import Queue

BACKENDS = ("jsonl", "sqlite")


@pytest.fixture(params=BACKENDS)
def backend(request) -> str:
    return request.param


@pytest.fixture()
def queue(backend, tmp_path):
    if backend == "jsonl":
        return Queue(tmp_path / "work.jsonl")
    return SqlQueue(tmp_path / "agentco.sqlite3")


@pytest.fixture()
def library(backend, tmp_path):
    if backend == "jsonl":
        return SopLibrary(tmp_path / "sops.jsonl")
    return SqlSopLibrary(tmp_path / "agentco.sqlite3")


@pytest.fixture()
def jsonl_queue(tmp_path):
    """The default backend only — for tests about the file format itself."""
    return Queue(tmp_path / "work.jsonl")
