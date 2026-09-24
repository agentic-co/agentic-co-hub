"""Two gaps between what the docs describe and what the tools could do.

Both were found by reading a LIVE registry rather than the code, and both have
the same shape: a fact the system depends on, which no tool on the path could
produce or resolve.

  * `keygen` emitted a bare string. The object form with `owner` has been the
    documented shape since owner-binding landed, two documents explain why it
    matters, and the only command that mints an identity could not write one.
    On the beta registry on 2026-09-23: five identities, five flat strings, no
    owner anywhere — which is not an operator forgetting, it is the tool's
    only output.

  * `agentco gate1` passed `args.operator` straight through, defaulting to
    `None`, while its own help text promised `$AGENTCO_REGISTRY_OPERATOR`. The
    server honoured the variable and the command line did not, so the same
    registry reported a longest-ever streak of 1 on the command line and 0 over
    HTTP — the difference being the operator counted as a publisher against his
    own gate.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from agentco import app as app_module, auth, cli, db, metrics

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# keygen can state what the key table can hold
# --------------------------------------------------------------------------- #


def mint(capsys, *argv) -> tuple[dict, str]:
    """`readouterr` DRAINS, so the entry and the advice come back together —
    reading twice returns an empty second half."""
    assert cli.main(["keygen", *argv]) == 0
    captured = capsys.readouterr()
    return json.loads(captured.out), captured.err


def minted(capsys, *argv) -> dict:
    return mint(capsys, *argv)[0]


def test_an_actor_with_no_owner_is_still_a_bare_string(capsys):
    """The old output, unchanged. A service identity answers to nobody and the
    table has always said that by saying nothing."""
    entry = minted(capsys, "build-bot")
    assert isinstance(entry["build-bot"], str)
    assert len(entry["build-bot"]) >= 32


def test_owner_and_label_produce_the_documented_object(capsys):
    entry = minted(capsys, "alice-codex-01", "--owner", "alice", "--label", "codex")
    value = entry["alice-codex-01"]
    assert value["owner"] == "alice"
    assert value["label"] == "codex"
    assert isinstance(value["secret"], str) and value["secret"]


def test_what_keygen_mints_is_what_auth_will_load(tmp_path, capsys):
    """The two ends of one file, checked against each other.

    A mint command and a parser that disagree about the format is the failure
    this whole pair exists to prevent, and nothing else in the suite puts them
    in the same room.
    """
    table = {}
    table.update(minted(capsys, "alice", "--label", "laptop"))
    table.update(minted(capsys, "alice-codex-01", "--owner", "alice", "--label", "codex"))
    table.update(minted(capsys, "build-bot"))

    path = tmp_path / "keys.json"
    path.write_text(json.dumps(table))
    path.chmod(0o600)
    auth._KEY_CACHE.clear()

    identities = auth.load_identities(path)
    assert auth.owner_of("alice-codex-01", identities) == "alice"
    assert auth.owner_of("alice", identities) == "alice"      # owns itself: no owner declared
    assert auth.owner_of("build-bot", identities) == "build-bot"
    assert identities["alice"].label == "laptop"
    assert set(auth.load_keys(path)) == {"alice", "alice-codex-01", "build-bot"}


def test_self_ownership_is_refused_at_mint_not_at_load(capsys):
    """`auth` raises on this. Raising there means it is discovered AFTER the
    secret has been sent to somebody, which is the expensive half."""
    assert cli.main(["keygen", "alice", "--owner", "alice"]) == 2
    err = capsys.readouterr().err
    assert "cannot own itself" in err


def test_the_advice_names_the_consequence_of_declaring_an_owner(capsys):
    """Declaring an owner silently disarms this actor as a verifier for its
    owner's other agents. That is correct, and it is not guessable — so the
    command that causes it is the one that has to say so."""
    _, err = mint(capsys, "alice-codex-01", "--owner", "alice")
    assert "revoking 'alice' revokes this too" in err
    assert "no longer verify" in err


def test_an_unowned_mint_says_what_was_not_stated(capsys):
    _, err = mint(capsys, "someone")
    assert "--owner" in err and "its own party" in err


# --------------------------------------------------------------------------- #
# one operator, resolved in one place
# --------------------------------------------------------------------------- #


def test_the_cli_honours_the_variable_its_help_text_promises(tmp_path, monkeypatch, capsys):
    """The live defect, reproduced: with the variable set, the operator must not
    appear among the publishers his own gate excludes."""
    monkeypatch.setenv(app_module.OPERATOR_ENV_VAR, "operator")
    monkeypatch.delenv(auth.KEYS_ENV_VAR, raising=False)
    auth._KEY_CACHE.clear()

    dbfile = tmp_path / "registry.sqlite3"
    conn = db.connect(dbfile)
    for weeks_ago in range(1, 6):
        for actor in ("operator", "conflict-probe"):
            metrics.record_call(conn, verb="snapshot", actor=actor, status="accepted",
                                latency_ms=1.0, at=NOW - timedelta(weeks=weeks_ago))
    conn.close()

    assert cli.main(["--db", str(dbfile), "gate1"]) == 1
    out = capsys.readouterr().out
    assert "other than operator" in out, out
    assert "other than None" not in out
    for line in out.splitlines():
        if "publisher(s)" in line:
            assert "operator" not in line.split("publisher(s)")[1], line


def test_explicit_beats_the_environment(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(app_module.OPERATOR_ENV_VAR, "operator")
    dbfile = tmp_path / "registry.sqlite3"
    db.connect(dbfile).close()
    cli.main(["--db", str(dbfile), "--operator", "someone-else", "gate1"])
    assert "other than someone-else" in capsys.readouterr().out


def test_the_server_and_the_command_line_resolve_it_identically(monkeypatch):
    """One setting with two resolutions is a class of bug. There is one resolver."""
    monkeypatch.delenv(app_module.OPERATOR_ENV_VAR, raising=False)
    assert app_module.resolve_operator(None) == app_module.DEFAULT_OPERATOR
    monkeypatch.setenv(app_module.OPERATOR_ENV_VAR, "whoever")
    assert app_module.resolve_operator(None) == "whoever"
    assert app_module.resolve_operator("explicit") == "explicit"
