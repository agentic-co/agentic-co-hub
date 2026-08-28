"""The tier-3 session hook — fail-open above everything else, and a byte-identical uninstall.

Two properties carry all the weight here, and both are adversarial by nature:

  * **Fail open.** A `SessionStart` hook runs on every session, for every
    colleague, in every repo it is installed into. Each of the three
    dependencies this module touches (registry DB, work store, SOP store) is
    broken IN TURN, and `main()` itself is proven to always exit 0 and print
    exactly one line of valid JSON regardless. A hook that occasionally
    crashes a colleague's session start is disqualified no matter how good
    its content is the rest of the time.
  * **Byte-identical uninstall.** The installer never attempts a lossless
    in-place JSON edit — it backs up verbatim bytes and replays them. The
    test that matters is a settings file with formatting a naive
    `json.load`/`json.dump` round trip would destroy (4-space indent, no
    trailing newline), verified by exact byte equality after a full
    install → uninstall cycle, not by "looks the same".
"""

from __future__ import annotations

import builtins
import json
import subprocess
from pathlib import Path
import sys

import pytest

from agentco import hook


# --------------------------------------------------------------------------- #
# Content — happy path
# --------------------------------------------------------------------------- #


@pytest.fixture()
def stores(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCO_REGISTRY_DB", str(tmp_path / "registry.sqlite3"))
    monkeypatch.setenv("AGENTCO_WORK_STORE", str(tmp_path / "work.jsonl"))
    monkeypatch.setenv("AGENTCO_SOP_STORE", str(tmp_path / "sops.jsonl"))
    monkeypatch.setenv("AGENTCO_ACTOR", "dana")
    return tmp_path


def test_build_additional_context_leads_with_the_pull_instruction(stores):
    context = hook.build_additional_context("dana")
    assert context.startswith(hook.PULL_INSTRUCTION)
    assert "call `events(" in context or "call `events()" in context


def test_build_additional_context_includes_registry_content_when_healthy(stores):
    from agentco import db, leases

    conn = db.connect(hook.resolve_db_path())
    leases.claim(conn, actor="dana", repo="acme/web-platform", prefixes=["src/budget"], intent="prototype")
    leases.claim(conn, actor="kofi", repo="acme/web-platform", prefixes=["src/budget/grid"], intent="implement")

    context = hook.build_additional_context("dana")
    assert "vs kofi" in context
    assert "Unavailable this session" not in context


def test_build_additional_context_includes_work_section_when_healthy(stores):
    from agentco.work import Queue

    queue = Queue(hook.resolve_work_store())
    queue.create("something to do")

    context = hook.build_additional_context("dana")
    assert "Ready work items you could pull: 1" in context


# --------------------------------------------------------------------------- #
# Fail open — each dependency broken IN TURN
# --------------------------------------------------------------------------- #


def test_registry_section_degrades_when_the_db_path_is_unreachable(tmp_path, monkeypatch):
    """A directory in place of the sqlite file is a deterministic, portable way
    to force `sqlite3.connect` to fail without touching real infrastructure."""
    broken = tmp_path / "not-a-file"
    broken.mkdir()
    monkeypatch.setenv("AGENTCO_REGISTRY_DB", str(broken))

    text, warning = hook._registry_section("dana")

    assert text is None
    assert warning is not None
    assert "divergence/scope-conflict" in warning


def test_work_section_degrades_when_the_work_store_is_unreachable(tmp_path, monkeypatch):
    broken = tmp_path / "not-a-file"
    broken.mkdir()
    monkeypatch.setenv("AGENTCO_WORK_STORE", str(broken))

    text, warning = hook._work_section("dana")

    assert text is None
    assert warning is not None
    assert "work queue" in warning


def test_sop_section_degrades_when_the_sop_store_is_unreachable(tmp_path, monkeypatch):
    broken = tmp_path / "not-a-file"
    broken.mkdir()
    monkeypatch.setenv("AGENTCO_SOP_STORE", str(broken))

    text, warning = hook._sop_section()

    assert text is None
    assert warning is not None
    assert "SOP library" in warning


def test_registry_section_degrades_when_importing_agentco_db_fails(monkeypatch):
    """'importing agentco itself' as a named dependency — proven by making the
    import inside `_registry_section` raise, the same way a broken submodule
    (a bad edit, a missing stdlib feature in a stripped interpreter) would."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "agentco.db" or name == "agentco":
            raise ImportError("simulated broken agentco.db import")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    text, warning = hook._registry_section("dana")

    assert text is None
    assert warning is not None
    assert "ImportError" in warning


def test_build_additional_context_names_every_broken_dependency_not_just_one(tmp_path, monkeypatch):
    """Degrade in NAMED pieces — a reader must be able to tell which of the
    three things failed, not just that 'something' did."""
    broken_db = tmp_path / "db-dir"
    broken_db.mkdir()
    broken_work = tmp_path / "work-dir"
    broken_work.mkdir()
    broken_sop = tmp_path / "sop-dir"
    broken_sop.mkdir()
    monkeypatch.setenv("AGENTCO_REGISTRY_DB", str(broken_db))
    monkeypatch.setenv("AGENTCO_WORK_STORE", str(broken_work))
    monkeypatch.setenv("AGENTCO_SOP_STORE", str(broken_sop))

    context = hook.build_additional_context("dana")

    assert context.startswith(hook.PULL_INSTRUCTION), "the pull instruction must survive every dependency failing"
    assert "divergence/scope-conflict" in context
    assert "work queue" in context
    assert "SOP library" in context


def test_build_additional_context_still_useful_when_everything_is_broken(tmp_path, monkeypatch):
    """'Degrade to inject less or nothing' — the floor is the pull instruction
    plus a named explanation, never an exception and never empty silence."""
    broken = tmp_path / "broken"
    broken.mkdir()
    monkeypatch.setenv("AGENTCO_REGISTRY_DB", str(broken))
    monkeypatch.setenv("AGENTCO_WORK_STORE", str(broken))
    monkeypatch.setenv("AGENTCO_SOP_STORE", str(broken))

    context = hook.build_additional_context("dana")
    assert context  # never empty
    assert hook.PULL_INSTRUCTION in context


# --------------------------------------------------------------------------- #
# main() — the outer backstop, and stdio hygiene
# --------------------------------------------------------------------------- #


def test_main_prints_one_line_of_valid_json_and_returns_zero(stores, capsys):
    exit_code = hook.main()
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert isinstance(payload["hookSpecificOutput"]["additionalContext"], str)


def test_main_still_exits_zero_and_prints_json_when_every_dependency_is_broken(tmp_path, monkeypatch, capsys):
    broken = tmp_path / "broken"
    broken.mkdir()
    monkeypatch.setenv("AGENTCO_REGISTRY_DB", str(broken))
    monkeypatch.setenv("AGENTCO_WORK_STORE", str(broken))
    monkeypatch.setenv("AGENTCO_SOP_STORE", str(broken))

    exit_code = hook.main()
    captured = capsys.readouterr()

    assert exit_code == 0
    payload = json.loads(captured.out)
    assert "hookSpecificOutput" in payload


def test_main_survives_a_completely_unanticipated_exception(monkeypatch, capsys):
    """The outer backstop — proven by breaking something main() does not
    otherwise defend against (resolving the actor itself), not one of the
    three named dependencies."""

    def explode(*_args, **_kwargs):
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr(hook, "resolve_actor", explode)

    exit_code = hook.main()
    captured = capsys.readouterr()

    assert exit_code == 0
    assert json.loads(captured.out) == {}
    assert "something nobody anticipated" in captured.err


def test_main_as_a_real_subprocess_with_a_broken_registry_still_exits_zero(tmp_path):
    """End-to-end proof, not just an in-process mock: a real `python -m
    agentco.hook` invocation, in a real subprocess, with a genuinely broken
    dependency, exits 0 and prints parseable JSON on stdout."""
    broken_db = tmp_path / "db-dir"
    broken_db.mkdir()
    env = {
        "PATH": "/usr/bin:/bin",
        # `env=` REPLACES the environment rather than extending it, and `cwd` is
        # outside the repo — so without this the subprocess can only import
        # agentco when the package happens to be installed in the interpreter
        # running the tests. It is under `uv run`; it is not under a bare
        # interpreter, and the test then fails for a reason that has nothing to
        # do with what it is checking. A test that passes only in the author's
        # environment is the same defect class as one that passes only this
        # month.
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "AGENTCO_REGISTRY_DB": str(broken_db),
        "AGENTCO_WORK_STORE": str(tmp_path / "work.jsonl"),
        "AGENTCO_SOP_STORE": str(tmp_path / "sops.jsonl"),
        "AGENTCO_ACTOR": "dana",
    }
    result = subprocess.run(
        [sys.executable, "-m", "agentco.hook"],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert "divergence/scope-conflict" in payload["hookSpecificOutput"]["additionalContext"]


def test_truncation_is_announced_not_silent(stores):
    from agentco import db, leases

    conn = db.connect(hook.resolve_db_path())
    for i in range(50):
        leases.claim(
            conn, actor="dana", repo="acme/web-platform", prefixes=[f"src/module-{i}/sub"], intent="prototype"
        )
        leases.claim(
            conn, actor=f"holder-{i}", repo="acme/web-platform", prefixes=[f"src/module-{i}/sub"], intent="implement"
        )

    context = hook.build_additional_context("dana", max_bytes=500)

    assert len(context.encode("utf-8")) <= 500
    assert "truncated" in context
    assert context.startswith(hook.PULL_INSTRUCTION[:20])  # the instruction survives, never the first casualty


# --------------------------------------------------------------------------- #
# The installer — dry-run default, byte-identical uninstall
# --------------------------------------------------------------------------- #


def test_install_dry_run_never_writes_anything(tmp_path):
    settings = tmp_path / "settings.json"
    result = hook.install(settings, command="do-the-thing", write=False)

    assert result.status == "would_install"
    assert result.diff
    assert not settings.exists()
    assert not (tmp_path / "settings.json.agentco-backup.json").exists()


def test_install_on_a_missing_file_creates_it_and_uninstall_removes_it_entirely(tmp_path):
    settings = tmp_path / "settings.json"

    install_result = hook.install(settings, command="do-the-thing", write=True)
    assert install_result.status == "installed"
    assert settings.exists()
    config = json.loads(settings.read_text())
    assert config["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "do-the-thing"

    uninstall_result = hook.uninstall(settings, write=True)
    assert uninstall_result.status == "uninstalled"
    assert not settings.exists(), "a settings file that did not exist before install must not exist after uninstall"


def test_install_then_uninstall_restores_odd_formatting_byte_for_byte(tmp_path):
    """The load-bearing test: 4-space indent, unusual key order, and NO
    trailing newline — exactly what a naive json.load/json.dump round trip
    would silently destroy."""
    settings = tmp_path / "settings.json"
    original = (
        b'{\n    "permissions": {\n        "allow": ["Bash(git:*)"]\n    },\n'
        b'    "statusLine": {\n        "type": "command",\n        "command": "~/.claude/statusline.sh"\n    }\n}'
    )
    settings.write_bytes(original)

    install_result = hook.install(settings, command="do-the-thing", write=True)
    assert install_result.status == "installed"
    assert settings.read_bytes() != original, "the install must actually have changed the file"

    uninstall_result = hook.uninstall(settings, write=True)
    assert uninstall_result.status == "uninstalled"
    assert settings.read_bytes() == original, "uninstall must restore the EXACT original bytes"


def test_reinstalling_does_not_overwrite_the_pristine_backup(tmp_path):
    """The backup is taken ONCE. A second install (e.g. changing --command)
    must not capture the already-modified file as 'pristine', or uninstall
    would only ever get back to the state after the FIRST install, not
    before AgentCo touched the file at all."""
    settings = tmp_path / "settings.json"
    original = b'{\n  "existingKey": "value"\n}'
    settings.write_bytes(original)

    hook.install(settings, command="first-command", write=True)
    hook.install(settings, command="second-command", write=True)  # different command, appends a second entry

    config = json.loads(settings.read_text())
    commands = [entry["command"] for group in config["hooks"]["SessionStart"] for entry in group["hooks"]]
    assert commands == ["first-command", "second-command"]

    hook.uninstall(settings, write=True)
    assert settings.read_bytes() == original


def test_installing_the_same_command_twice_is_a_no_op_not_a_duplicate(tmp_path):
    settings = tmp_path / "settings.json"
    hook.install(settings, command="do-the-thing", write=True)
    before = settings.read_bytes()

    result = hook.install(settings, command="do-the-thing", write=True)

    assert result.status == "already_installed"
    assert settings.read_bytes() == before


def test_install_refuses_malformed_json_and_touches_nothing(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_bytes(b"{not valid json at all")

    result = hook.install(settings, command="do-the-thing", write=True)

    assert result.status == "error"
    assert settings.read_bytes() == b"{not valid json at all"
    assert not (tmp_path / "settings.json.agentco-backup.json").exists()


def test_uninstall_with_no_prior_install_is_a_named_no_op(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_bytes(b'{"untouched": true}')

    result = hook.uninstall(settings, write=True)

    assert result.status == "nothing_to_uninstall"
    assert settings.read_bytes() == b'{"untouched": true}'


def test_uninstall_dry_run_never_writes_anything(tmp_path):
    settings = tmp_path / "settings.json"
    original = b'{\n  "k": "v"\n}'
    settings.write_bytes(original)
    hook.install(settings, command="do-the-thing", write=True)
    installed_bytes = settings.read_bytes()

    result = hook.uninstall(settings, write=False)

    assert result.status == "would_uninstall"
    assert settings.read_bytes() == installed_bytes, "dry run must not touch the file"


# --------------------------------------------------------------------------- #
# The installer must not re-encode bytes outside its own change
# --------------------------------------------------------------------------- #


def test_install_does_not_re_encode_non_ascii_elsewhere_in_the_file(tmp_path):
    """A non-ASCII character anywhere in the file must survive the install.

    `json.dumps` defaults to `ensure_ascii=True`, which escapes every non-ASCII
    character in the WHOLE document — so an em-dash in a comment field far from
    anything AgentCo touched came back as `\\u2014`. The file stayed valid JSON
    and semantically identical, which is what made it invisible: nothing fails,
    the bytes just changed underneath somebody else's tooling.

    This test could not have been written from a fixture this tool produced.
    Every existing one is ASCII, so the bug was green until an install was
    dry-run against a real settings file that documented itself with em-dashes.
    """
    settings = tmp_path / "settings.json"
    # Real em-dash and accent BYTES in the file, not JSON escapes — that is the
    # input the bug needs, and the reason no existing fixture caught it.
    settings.write_text(
        '{\n  "_docs": "STAGED — not enabled — see the note",\n  "theme": "café"\n}\n',
        encoding="utf-8",
    )

    before = settings.read_bytes()
    assert "—".encode("utf-8") in before

    result = hook.install(settings, command="/usr/bin/python3 -m agentco.hook", write=True)
    assert result.status == "installed"

    after = settings.read_bytes()
    assert "—".encode("utf-8") in after, "the em-dash was re-encoded as an escape"
    assert b"\\u2014" not in after
    assert "café".encode("utf-8") in after

    # And the only semantic difference is the hook AgentCo added.
    reloaded = json.loads(after.decode("utf-8"))
    assert reloaded["_docs"] == "STAGED — not enabled — see the note"
    assert reloaded["theme"] == "café"
    assert reloaded["hooks"]["SessionStart"]


def test_uninstall_restores_a_non_ascii_file_byte_for_byte(tmp_path):
    """The backup is bytes, so this must hold regardless of the bug above —
    it is the safety net that made the encoding defect recoverable rather than
    permanent, and it is worth a test of its own."""
    settings = tmp_path / "settings.json"
    settings.write_text('{\n  "_docs": "an em-dash — and a café"\n}\n', encoding="utf-8")
    original = settings.read_bytes()

    hook.install(settings, command="/usr/bin/python3 -m agentco.hook", write=True)
    assert settings.read_bytes() != original

    hook.uninstall(settings, write=True)
    assert settings.read_bytes() == original
