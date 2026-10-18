"""The multi-hub rig runs only when asked: `AGENTCO_RIG=1`.

It needs a Docker daemon, builds the real image and brings up five hubs and a
Postgres, which is minutes, not seconds. The main suite collects `tests/`, so
without the variable this directory contributes nothing to a run at all — not
a skip per test, which would make every ordinary run report rig tests it never
ran. CI runs it in its own workflow (`.github/workflows/network.yml`): on a
path filter for changes that can break a deployed hub, nightly, and on demand.

    AGENTCO_RIG=1 uv run --extra dev --extra server --extra mcp pytest tests/network -v

`AGENTCO_RIG_IMAGE=<tag>` reuses an image already built; unset, the rig builds
one and removes it afterwards.
"""

from __future__ import annotations

import os

import pytest

RIG_ENABLED = os.environ.get("AGENTCO_RIG") == "1"

collect_ignore_glob = [] if RIG_ENABLED else ["test_*.py"]


@pytest.fixture(scope="session")
def rig():
    from tests.network.rig import Rig

    hubs = Rig()
    try:
        yield hubs.up()
    finally:
        hubs.down()
        left = hubs.leftovers()
        assert not any(left.values()), f"the rig left resources behind: {left}"


@pytest.fixture(autouse=True)
def _snapshot_admission():
    """Override the suite-wide autouse fixture: snapshot admission here is the
    hubs' own configuration, in the compose file, not this process's env."""
