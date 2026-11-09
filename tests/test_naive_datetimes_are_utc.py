"""A naive datetime handed to the hub means UTC, whatever the machine's zone is (A13).

Every module that serialises a timestamp has its own `_iso`, and each was
`dt.astimezone(timezone.utc).isoformat()`. Python treats a naive datetime in
`astimezone` as LOCAL time, so a naive `now=` argument was silently shifted by
the host's UTC offset: the same call stored different instants on a laptop in
Campinas and on a UTC server. The convention is the one the rest of the codebase
already uses when it PARSES a stamp (`parsed.replace(tzinfo=timezone.utc)`):
naive is UTC.

The test sets `TZ` itself, so it fails on a UTC CI runner too — it does not
depend on where it is run.

`agentco/leases.py` has the same `_iso`; it is owned by another lane and is not
covered here.
"""

from __future__ import annotations

import importlib
import os
import time
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset is unavailable")

NAIVE = datetime(2026, 1, 15, 12, 0, 0)
AWARE_UTC = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
EXPECTED = "2026-01-15T12:00:00+00:00"

MODULES = ["work", "envelope", "divergence", "digests", "snapshots", "pulse"]


@pytest.fixture(params=["America/Sao_Paulo", "Asia/Tokyo"])
def host_zone(request):
    """A host zone that is not UTC, restored afterwards."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = request.param
    time.tzset()
    yield request.param
    if before is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = before
    time.tzset()


@pytest.mark.parametrize("module", MODULES)
def test_a_naive_datetime_is_serialised_as_utc_not_as_host_local_time(module, host_zone):
    iso = importlib.import_module(f"agentco.{module}")._iso
    assert iso(AWARE_UTC) == EXPECTED, "precondition: an aware UTC value is unchanged"
    assert iso(NAIVE) == EXPECTED, (
        f"{module}._iso shifted a naive datetime by the {host_zone} offset: {iso(NAIVE)}"
    )


def test_a_naive_cutoff_in_metrics_is_utc_not_host_local_time(host_zone, tmp_path):
    from agentco import db, metrics

    conn = db.connect(tmp_path / "registry.sqlite3")
    result = metrics.roll_up_and_prune_calls(conn, before=NAIVE, now=NAIVE)
    assert result["prunedBefore"] == EXPECTED
    assert result["at"] == EXPECTED
