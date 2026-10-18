"""`agentco prune` runs at all (found while adding feed retention, 2026-10-18).

It imported `resolve_db_path` from `agentco.stores`, where it does not exist, so
every invocation — dry run included — died with ImportError before reading a
row. Its logic (`metrics.roll_up_and_prune_calls`) is tested in
test_retention.py; nothing ran the COMMAND. This does, as an operator would.
"""

from __future__ import annotations

import os
import subprocess
import sys

from agentco import db


def run(*args, env):
    return subprocess.run([sys.executable, "-m", "agentco", "prune", *args], capture_output=True, text=True, env=env)


def test_prune_dry_run_and_apply_through_the_cli(tmp_path):
    path = tmp_path / "registry.sqlite3"
    db.connect(path)
    env = {**os.environ, "AGENTCO_REGISTRY_DB": str(path)}
    dry = run("--days", "30", env=env)
    assert dry.returncode == 0, dry.stderr
    assert "retention window: 30 days" in dry.stdout and "dry run" in dry.stderr
    applied = run("--days", "30", "--apply", env=env)
    assert applied.returncode == 0, applied.stderr
    assert "deleted" in applied.stdout
