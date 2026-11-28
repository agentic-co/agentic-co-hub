"""The `agentco` console script is declared and points at the real entry point.

`python -m agentco` used to be the only way in. The script and the module must run the same
function, or the two spellings in the docs would drift apart.
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_console_script_is_declared_and_resolves_to_cli_main():
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["agentco"] == "agentco.cli:main"
    module, _, attr = scripts["agentco"].partition(":")
    target = getattr(importlib.import_module(module), attr)
    from agentco.cli import main

    assert target is main and callable(target)


def test_dunder_main_runs_the_same_entry_point():
    assert "from agentco.cli import main" in (ROOT / "agentco" / "__main__.py").read_text()


def test_installed_script_runs_if_present():
    exe = shutil.which("agentco", path=str(Path(sys.executable).parent))
    if exe is None:
        import pytest

        pytest.skip("package not installed with its scripts in this environment")
    r = subprocess.run([exe, "--help"], capture_output=True, text=True, env=os.environ)
    assert r.returncode == 0 and "agentco" in (r.stdout + r.stderr).lower()
