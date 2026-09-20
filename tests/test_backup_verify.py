"""The export's post-write checks, and the ignore rule they exist to police.

`tools/` has no suite of its own, and the defect these tests pin is exactly the
kind that a tool without one accumulates: eight consecutive registry backups
were committed carrying a MANIFEST that counted `sops` and `work` rows the
directory did not contain, because both filenames are ignored as bare patterns
and a bare pattern matches at any depth.

Written the way CONTRIBUTING asks — each test fails if the mechanism is removed,
not merely if the happy path breaks. Hermetic: no network, no credentials, and
the git cases build their own throwaway work tree rather than reading the
developer's.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_tool():
    """Import `tools/backup_registry.py` by path — it is a script, not a package."""
    spec = importlib.util.spec_from_file_location(
        "backup_registry_under_test", REPO_ROOT / "tools" / "backup_registry.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bk = _load_tool()


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


# --------------------------------------------------------------------------- #
# _readback — what landed, not what we meant to write
# --------------------------------------------------------------------------- #


def test_readback_accepts_a_file_that_holds_what_was_counted(tmp_path):
    path = tmp_path / "events.jsonl"
    _jsonl(path, [{"a": 1}, {"a": 2}])
    assert bk._readback(path, 2) is None


def test_readback_catches_a_short_write(tmp_path):
    """The failure a `len(rows)` return value cannot see: fewer rows on disk."""
    path = tmp_path / "work.jsonl"
    _jsonl(path, [{"a": 1}])
    problem = bk._readback(path, 2)
    assert problem is not None
    assert "counted 2" in problem and "holds 1" in problem
    assert "work.jsonl" in problem, "the problem must name the file"


def test_readback_catches_a_row_that_does_not_parse_back(tmp_path):
    path = tmp_path / "sops.jsonl"
    path.write_text('{"a": 1}\n{oops\n', encoding="utf-8")
    problem = bk._readback(path, 2)
    assert problem is not None
    assert "line 2" in problem, "the problem must name the offending line"


def test_readback_catches_a_file_that_is_not_there(tmp_path):
    problem = bk._readback(tmp_path / "gone.jsonl", 0)
    assert problem is not None
    assert "unreadable" in problem


# --------------------------------------------------------------------------- #
# _git_ignored — written perfectly, and still not survivable
# --------------------------------------------------------------------------- #


def _work_tree(root: Path, gitignore: str) -> Path:
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    (root / ".gitignore").write_text(gitignore, encoding="utf-8")
    return root


def test_git_ignored_names_the_files_a_commit_would_drop(tmp_path):
    """The historical defect, reproduced: bare patterns swallow nested artefacts."""
    tree = _work_tree(tmp_path, "work.jsonl\nsops.jsonl\n")
    backup = tree / "backups" / "registry" / "20260101-0000"
    backup.mkdir(parents=True)
    paths = [backup / n for n in ("sops.jsonl", "outcomes.jsonl", "events.jsonl", "work.jsonl")]
    for path in paths:
        _jsonl(path, [{"x": 1}])

    ignored = bk._git_ignored(paths)
    assert ignored is not None, "a work tree with git present must be checkable"
    names = sorted(Path(entry).name for entry in ignored)
    assert names == ["sops.jsonl", "work.jsonl"]


def test_git_ignored_is_empty_when_the_negation_is_present(tmp_path):
    """The fix: broad ignores kept, the deliberate artefact re-included by path."""
    tree = _work_tree(
        tmp_path,
        "work.jsonl\nsops.jsonl\n"
        "!backups/registry/**/work.jsonl\n"
        "!backups/registry/**/sops.jsonl\n",
    )
    backup = tree / "backups" / "registry" / "20260101-0000"
    backup.mkdir(parents=True)
    paths = [backup / n for n in ("sops.jsonl", "outcomes.jsonl", "events.jsonl", "work.jsonl")]
    for path in paths:
        _jsonl(path, [{"x": 1}])

    assert bk._git_ignored(paths) == []


def test_git_ignored_still_protects_a_live_store(tmp_path):
    """The negation must not re-open the hole it was narrowed around.

    The broad patterns exist because the default work store is a RELATIVE path,
    so a process started anywhere writes one. A store outside backups/registry/
    must stay ignored at any depth.
    """
    tree = _work_tree(
        tmp_path,
        "work.jsonl\nsops.jsonl\n"
        "!backups/registry/**/work.jsonl\n"
        "!backups/registry/**/sops.jsonl\n",
    )
    (tree / "agentco").mkdir()
    stray = tree / "agentco" / "work.jsonl"
    _jsonl(stray, [{"x": 1}])

    ignored = bk._git_ignored([stray])
    assert ignored is not None, "the check must run, not report unchecked"
    # `check-ignore` echoes each path back as it was given, so compare basenames.
    assert [Path(entry).name for entry in ignored] == ["work.jsonl"]


def test_git_ignored_reports_unchecked_rather_than_clean_outside_a_work_tree(tmp_path):
    """`None`, never `[]`. Unreported is not the same as verified."""
    loose = tmp_path / "loose"
    loose.mkdir()
    path = loose / "work.jsonl"
    _jsonl(path, [{"x": 1}])
    # tmp_path is not a git work tree, so the check cannot run.
    assert bk._git_ignored([path]) is None


def test_git_ignored_reports_unchecked_for_a_directory_that_is_not_there(tmp_path):
    assert bk._git_ignored([tmp_path / "absent" / "work.jsonl"]) is None


# --------------------------------------------------------------------------- #
# This repository's own rules — the regression that started it
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["sops.jsonl", "work.jsonl", "events.jsonl", "outcomes.jsonl"])
def test_this_repo_ignores_registry_backups_entirely(name):
    """A registry backup does not belong in this repository at all.

    THIS ASSERTION USED TO SAY THE OPPOSITE, and the reversal is the point.

    It required every file `do_export` writes to be COMMITTABLE under
    backups/registry/, because bare `work.jsonl` / `sops.jsonl` patterns match at
    any depth and were silently dropping two of six — leaving committed backups
    whose MANIFEST counted procedures and queue items they did not contain.
    Reasonable, and it worked.

    What it did not weigh is what those two files hold. `work.jsonl` and
    `sops.jsonl` are live queue state — bead titles, assigned agents,
    attestations — and this repository has a public mirror. So making the
    backups whole put exactly the content `test_no_store_is_tracked_in_this_
    repository` forbids into the tree, that test failed as designed, and because
    `Build_Registry` depends on the test stage, no registry image was published
    between 2026-09-10 and 2026-09-11 (builds 135726, 135769, 135800, 135899).

    Two sound rules that cannot both hold. Resolved by moving the artefact
    rather than by weakening the guard: `--out` is a required argument with no
    default, so pointing a backup outside the work tree costs nothing and keeps
    it whole. `_git_ignored` still reports a backup written somewhere git would
    drop it — under this rule that warning is correct rather than a regression.

    Uses --no-index so nothing has to exist on disk: this asserts the RULES.
    """
    target = f"backups/registry/20260101-0000/{name}"
    proc = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", "--", target],
        cwd=str(REPO_ROOT), capture_output=True,
    )
    assert proc.returncode == 0, (
        f"{target} is committable, so a registry backup written here would carry "
        f"live queue state into a repository that has a public mirror"
    )


@pytest.mark.parametrize("target", ["work.jsonl", "agentco/work.jsonl", "a/b/c/sops.jsonl"])
def test_this_repo_still_ignores_live_stores_anywhere(target):
    """The other half. A store loose in the tree is somebody's queue state."""
    proc = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", "--", target],
        cwd=str(REPO_ROOT), capture_output=True,
    )
    assert proc.returncode == 0, f"{target} is a live store and must stay ignored"
