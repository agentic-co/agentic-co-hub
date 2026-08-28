"""Tier-1 context injection — the splice, and the two traps it must not repeat.

The tests that matter most here reconstruct the two defects named in
`agentco/inject.py`'s own docstring, because a green suite that cannot
reproduce either one is evidence about the fixtures, not the tool:

  * the CRLF trap — a fixture written with real `\\r\\n` bytes (never
    `write_text()`, which cannot produce a CRLF file at all) proves a render
    does not re-encode the file's line endings.
  * the decode-error trap — a fixture with bytes that are not valid UTF-8
    proves `run()`'s per-target catch is broad enough to survive it, and
    that surviving it does not also touch that target's file.
"""

from __future__ import annotations

import sqlite3

import pytest

from agentco import db, leases
from agentco.inject import (
    BEGIN_MARKER,
    END_MARKER,
    render_repo_block,
    render_session_block,
    render_into,
    run,
)

# --------------------------------------------------------------------------- #
# The splice — plain LF fixtures
# --------------------------------------------------------------------------- #


def test_missing_target_is_a_skip_not_an_error_and_creates_nothing(tmp_path):
    """This module never authors somebody's CLAUDE.md — only edits one that exists."""
    target = tmp_path / "CLAUDE.md"
    result = render_into(target, "content", write=True)
    assert result.status == "skipped"
    assert not target.exists()


def test_first_render_appends_without_rewriting_existing_bytes(tmp_path):
    target = tmp_path / "CLAUDE.md"
    original = b"# Project rules\nalways use uv\n"
    target.write_bytes(original)

    result = render_into(target, "hello from agentco", write=True)
    new = target.read_bytes()

    assert result.status == "written"
    assert new.startswith(original), "existing content must survive untouched at the head of the file"
    assert BEGIN_MARKER.encode() in new
    assert END_MARKER.encode() in new
    assert b"hello from agentco" in new


def test_second_render_with_identical_content_is_byte_identical(tmp_path):
    """No timestamp in the content means re-running against unchanged input must
    produce a byte-identical file — the idempotency the module promises."""
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(b"# rules\n")
    render_into(target, "steady content", write=True)
    before = target.read_bytes()

    result = render_into(target, "steady content", write=True)

    assert result.status == "unchanged"
    assert target.read_bytes() == before


def test_replacing_an_existing_block_touches_nothing_outside_the_markers(tmp_path):
    """The load-bearing promise, proven with real surrounding content on both sides."""
    target = tmp_path / "CLAUDE.md"
    original = (
        b"# Preamble\n"
        b"some rules that must survive\n"
        + BEGIN_MARKER.encode()
        + b"\nstale old content\n"
        + END_MARKER.encode()
        + b"\n# Epilogue\n"
        b"more rules that must survive\n"
    )
    target.write_bytes(original)
    begin_idx = original.find(BEGIN_MARKER.encode())
    end_of_end = original.find(END_MARKER.encode()) + len(END_MARKER.encode())
    prefix, suffix = original[:begin_idx], original[end_of_end:]

    render_into(target, "fresh content", write=True)
    new = target.read_bytes()

    assert new.startswith(prefix), "bytes before the BEGIN marker must be untouched"
    assert new.endswith(suffix), "bytes after the END marker must be untouched"
    assert b"stale old content" not in new
    assert b"fresh content" in new


def test_malformed_block_is_refused_and_the_file_is_left_untouched(tmp_path):
    """A BEGIN with no matching END must not be guessed at — refused loudly instead."""
    target = tmp_path / "CLAUDE.md"
    original = b"stuff\n" + BEGIN_MARKER.encode() + b"\nno end marker anywhere\n"
    target.write_bytes(original)

    result = render_into(target, "y", write=True)

    assert result.status == "error"
    assert "no matching" in result.reason
    assert target.read_bytes() == original, "a refused render must not write anything"


def test_dry_run_never_writes_but_always_has_a_diff(tmp_path):
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(b"unchanged so far\n")
    before = target.read_bytes()

    result = render_into(target, "would-be new content", write=False)

    assert result.status == "would_write"
    assert result.diff
    assert "would-be new content" in result.diff
    assert target.read_bytes() == before, "dry run must not touch the file"


def test_write_is_opt_in_not_the_default(tmp_path):
    """`render_into` itself defaults to dry run, matching the CLI's own default."""
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(b"content\n")
    before = target.read_bytes()

    result = render_into(target, "new stuff")  # write not passed

    assert result.status == "would_write"
    assert target.read_bytes() == before


# --------------------------------------------------------------------------- #
# The CRLF trap — the regression test that needs bytes, not write_text()
# --------------------------------------------------------------------------- #


def test_a_crlf_file_keeps_every_line_ending_crlf_after_a_render(tmp_path):
    """The historical defect: `read_text()`/`write_text()` silently normalise
    every `\\r\\n` in the WHOLE file to `\\n`, not just inside the block. This
    fixture is written with explicit `\\r\\n` bytes — `write_text()` cannot
    produce this input at all, which is exactly why the bug shipped invisibly
    against a suite that only ever used it."""
    target = tmp_path / "CLAUDE.md"
    original = b"# Title\r\nfirst rule\r\nsecond rule\r\n"
    target.write_bytes(original)

    render_into(target, "line a\nline b", write=True)
    new = target.read_bytes()

    assert new.startswith(original), "the original CRLF content must be untouched"
    # No bare LF anywhere: every \n in the file is part of a \r\n pair.
    assert new.replace(b"\r\n", b"").count(b"\n") == 0, f"a bare LF crept into a CRLF file: {new!r}"
    assert BEGIN_MARKER.encode() + b"\r\n" in new
    assert b"line a\r\nline b\r\n" + END_MARKER.encode() in new


def test_replacing_a_block_inside_a_crlf_file_stays_all_crlf(tmp_path):
    """Same proof, but exercising the REPLACE path (existing block), not append —
    the two paths build the block bytes differently and both must honour it."""
    target = tmp_path / "CLAUDE.md"
    original = (
        b"# Preamble\r\n"
        + BEGIN_MARKER.encode()
        + b"\r\nold\r\n"
        + END_MARKER.encode()
        + b"\r\n# Epilogue\r\n"
    )
    target.write_bytes(original)

    render_into(target, "new content", write=True)
    new = target.read_bytes()

    assert new.replace(b"\r\n", b"").count(b"\n") == 0, f"a bare LF crept into a CRLF file: {new!r}"


def test_an_lf_file_never_gains_crlf_from_a_render(tmp_path):
    """The trap runs both ways — an LF file must not pick up CRLF either."""
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(b"# Title\nfirst\nsecond\n")

    render_into(target, "line a\nline b", write=True)
    new = target.read_bytes()

    assert b"\r" not in new


# --------------------------------------------------------------------------- #
# The decode-error trap — per-target isolation
# --------------------------------------------------------------------------- #


def test_a_file_that_is_not_valid_utf8_is_refused_not_a_crashed_batch(tmp_path):
    """UnicodeDecodeError is a ValueError, not an OSError — the historical bug was
    an `except OSError` that let exactly this escape and kill the rest of the
    batch. This proves the catch here is broad enough to survive it."""
    target = tmp_path / "weird.md"
    original = b"# Title\ncontent " + b"\xff\xfe" + b" more\n"
    target.write_bytes(original)

    (result,) = run([target], "new content", write=True)

    assert result.status == "error"
    assert "UnicodeDecodeError" in result.reason
    assert target.read_bytes() == original, "a target that fails to decode must not be written"


def test_per_target_isolation_survives_a_bad_target_anywhere_in_the_list(tmp_path):
    """The exact shape of the historical bug: a bad target BEFORE a good one in
    the list must not stop the good one from being written. List order must
    never decide who gets touched."""
    missing = tmp_path / "missing.md"
    bad = tmp_path / "bad_encoding.md"
    bad_bytes = b"# Title\ncontent " + b"\xff\xfe" + b" more\n"
    bad.write_bytes(bad_bytes)
    good = tmp_path / "good.md"
    good.write_bytes(b"# Good\ncontent\n")

    results = run([bad, missing, good], "new block content", write=True)
    by_path = {r.path: r for r in results}

    assert len(results) == 3
    assert by_path[str(bad)].status == "error"
    assert "UnicodeDecodeError" in by_path[str(bad)].reason
    assert bad.read_bytes() == bad_bytes

    assert by_path[str(missing)].status == "skipped"
    assert not missing.exists()

    assert by_path[str(good)].status == "written"
    assert b"new block content" in good.read_bytes()


def test_run_records_the_exception_type_for_every_error_not_just_a_generic_message(tmp_path):
    """'Catch broadly, always record the exception type' — a blanket catch that
    only says 'something went wrong' is a silent one wearing a costume."""
    target = tmp_path / "bad.md"
    target.write_bytes(b"\xff\xfe\xfd")

    (result,) = run([target], "content", write=True)

    assert result.status == "error"
    assert result.reason.startswith("UnicodeDecodeError:")


# --------------------------------------------------------------------------- #
# The formatters — one per audience, and the split is the point
# --------------------------------------------------------------------------- #


def test_the_repo_block_names_who_is_working_where(monkeypatch):
    live = [
        {"holder": "dana", "prefixes": ["src/billing"], "intent": "implement", "holderAttested": False},
        {"holder": "kofi", "prefixes": ["src/search/index"], "intent": "prototype", "holderAttested": False},
    ]
    block = render_repo_block(live, repo="acme/web-platform")
    assert "dana" in block and "src/billing" in block
    assert "kofi" in block and "prototype" in block
    assert "Advisory only" in block, "it must never read as a lock"


def test_the_repo_block_carries_no_personal_state(monkeypatch):
    """THE test for this split. The target is a file the whole team reads and
    most repos commit. One person's snapshots landing there would publish what
    they are working on to everybody, permanently, through version control."""
    block = render_repo_block([], repo="acme/web-platform")
    lowered = block.lower()
    assert "divergence" not in lowered
    assert "snapshot" not in lowered
    assert "your" not in lowered


def test_the_repo_block_says_so_plainly_when_nobody_holds_a_claim():
    block = render_repo_block([], repo="acme/web-platform")
    assert "nobody else is holding a claim" in block


def test_an_attested_holder_is_marked_as_unverified():
    """A claim filed on someone else's behalf is a weaker statement, and a
    reader deciding whether to interrupt that person deserves to know."""
    live = [{"holder": "dana", "prefixes": ["src/billing"], "intent": "implement", "holderAttested": True}]
    assert "unverified" in render_repo_block(live, repo="acme/web-platform")


def test_the_repo_block_is_byte_identical_across_runs():
    """No timestamp. Otherwise every scheduled run is a diff, and the file
    becomes noise in every review of a repo that commits it."""
    live = [{"holder": "dana", "prefixes": ["src/billing"], "intent": "implement", "holderAttested": False}]
    assert render_repo_block(live, repo="r") == render_repo_block(live, repo="r")


def test_the_session_block_is_the_one_that_carries_personal_state():
    digest = {"moved": [{"purpose": "the spec", "artifactUri": "git:/repo#main"}], "tracked": 3}
    conflicts = [{"repo": "r", "withHolder": "kofi", "theirIntent": "implement", "myIntent": "implement"}]
    block = render_session_block(digest, conflicts, actor="dana")
    assert "dana" in block
    assert "the spec" in block
    assert "kofi" in block


def test_truncation_is_by_bytes_and_always_announced():
    """An omitted item nobody is told about reads as 'nothing else happened'."""
    live = [
        {"holder": f"person-{i}", "prefixes": [f"src/mod{i}"], "intent": "implement", "holderAttested": False}
        for i in range(60)
    ]
    block = render_repo_block(live, repo="r", max_bytes=300)
    assert len(block.encode("utf-8")) <= 400
    assert "more line(s) omitted" in block
