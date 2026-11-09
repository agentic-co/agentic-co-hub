"""Scope normalisation (known-issues claim 3): two spellings of one place must meet.

The strict-xfail properties live in `test_adversarial_findings.py`. This file
covers what they cannot see: leases ALREADY on disk (written before any
normalisation existed), the claim lock, display spelling, the refusal codes,
and construction through `Scope(...)` directly.
"""

from __future__ import annotations

import json
import os
import threading
import unicodedata

import pytest

from agentco import db, leases
from agentco.errors import Refusal
from agentco.scope import Scope, prefixes_overlap, scopes_intersect, validate_prefix

_BACKENDS = ["sqlite"] + (["postgres"] if os.environ.get("AGENTCO_TEST_PG") else [])


@pytest.fixture(params=_BACKENDS)
def conn(request, tmp_path):
    if request.param == "postgres":
        return db.connect(request.getfixturevalue("_pg_dsn"))
    return db.connect(tmp_path / "registry.sqlite3")


def plant_old_style_lease(conn, *, holder, repo, prefixes, uid="lease_old0000000001"):
    """A row exactly as a pre-normalisation build wrote it: repo and prefixes verbatim."""
    conn.execute(
        "INSERT INTO leases(uid, holder, holder_attested, repo, prefixes, intent, "
        "claimed_at, expires_at, released_at) VALUES (?,?,?,?,?,?,?,?,NULL)",
        (uid, holder, 0, repo, json.dumps(prefixes), "implement",
         "2026-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00"),
    )


def test_a_lease_stored_under_another_capitalisation_still_conflicts(conn):
    plant_old_style_lease(conn, holder="dana", repo="Acme/Web-Platform", prefixes=["src/Billing/Invoices"])
    out = leases.claim(conn, actor="kofi", repo="acme/web-platform",
                       prefixes=["src/billing/invoices"], intent="implement")
    assert [c["withHolder"] for c in out["conflicts"]] == ["dana"], (
        "a lease written before normalisation was invisible to a differently-cased claim"
    )
    # The other party's spelling is echoed as stored, not rewritten.
    assert out["conflicts"][0]["overlaps"] == [
        {"mine": "src/billing/invoices", "theirs": "src/Billing/Invoices"}
    ]


def test_session_conflicts_see_stored_leases_across_repo_spellings(conn):
    plant_old_style_lease(conn, holder="dana", repo="Acme/Web-Platform", prefixes=["src/billing/invoices"])
    leases.claim(conn, actor="kofi", repo="acme/web-platform",
                 prefixes=["src/billing/invoices"], intent="implement")
    found = leases.conflicts_for(conn, "kofi")
    assert [c["withHolder"] for c in found] == ["dana"]


def test_the_response_keeps_the_callers_spelling(conn):
    out = leases.claim(conn, actor="dana", repo="Acme/Web-Platform",
                       prefixes=["src/Billing/Invoices"], intent="implement")
    assert out["repo"] == "Acme/Web-Platform"
    assert out["prefixes"] == ["src/Billing/Invoices"]


def test_claims_in_two_spellings_of_one_repo_take_the_same_lock(conn, monkeypatch):
    seen: list[str] = []
    real = leases.idempotency.write_tx

    def spy(c, lock=None):
        seen.append(lock)
        return real(c, lock=lock)

    monkeypatch.setattr(leases.idempotency, "write_tx", spy)
    leases.claim(conn, actor="dana", repo="Acme/X-App", prefixes=["src/api/one"], intent="implement")
    leases.claim(conn, actor="kofi", repo="acme/x-app", prefixes=["src/api/one"], intent="implement")
    assert len(set(seen)) == 1, f"two spellings of one repo serialised on different locks: {seen}"


def test_a_second_spelling_is_told_about_the_first_in_one_registry(conn):
    leases.claim(conn, actor="dana", repo="Acme/Web-Platform", prefixes=["src/api/one"], intent="implement")
    out = leases.claim(conn, actor="kofi", repo="acme/web-platform", prefixes=["SRC/API/one"], intent="implement")
    assert [c["withHolder"] for c in out["conflicts"]] == ["dana"]


def test_the_repo_comparison_folds_unicode_not_just_ascii():
    a = Scope.parse("acme/Straße", ["src/api/one"])
    b = Scope.parse("ACME/STRASSE", ["src/api/one"])
    assert scopes_intersect(a, b)


@pytest.mark.parametrize("char", ["​", "‌", "‍", "⁠", "﻿", "­", "‮", "\u0085"])
def test_invisible_characters_are_refused_in_prefix_and_repo_not_stripped(char):
    with pytest.raises(Refusal) as p:
        validate_prefix(f"src/bud{char}get")
    assert p.value.code == "control_character"
    with pytest.raises(Refusal) as r:
        Scope.parse(f"acme/web{char}-platform", ["src/api/one"])
    assert r.value.code == "control_character"


def test_both_nfc_and_nfd_repo_names_meet():
    nfc = unicodedata.normalize("NFC", "acme/café")
    nfd = unicodedata.normalize("NFD", "acme/café")
    assert nfc != nfd
    assert scopes_intersect(Scope.parse(nfc, ["src/api/one"]), Scope.parse(nfd, ["src/api/one"]))


def test_a_prefix_segment_that_is_only_dots_or_spaces_is_refused():
    for bad in ("src/.../x", "src/ /x", "src/budget/. ."):
        with pytest.raises(Refusal):
            validate_prefix(bad)


def test_trailing_dots_and_spaces_fold_per_segment_not_only_at_the_end():
    assert prefixes_overlap("src/budget./grid", "src/budget/grid")
    assert prefixes_overlap("src/budget /grid", "src/budget/grid")
    assert not prefixes_overlap("src/v1.2/grid", "src/v1/grid")


def test_dedupe_is_by_comparison_key_and_keeps_the_first_spelling():
    s = Scope.parse("acme/x", ["src/Budget/grid", "src/budget/grid/", "SRC/BUDGET/GRID"])
    assert s.prefixes == ("src/Budget/grid",)


def test_constructing_a_scope_directly_cannot_bypass_the_rules():
    for repo, prefixes in (
        ("acme/x", ("src",)),
        ("", ("src/api/one",)),
        ("acme/x", ("../../etc/passwd",)),
        ("acme/x", ("src/bud​get",)),
        ("acme/x", ()),
    ):
        with pytest.raises(Refusal):
            Scope(repo=repo, prefixes=prefixes)


def test_directly_constructed_scopes_compare_like_parsed_ones():
    assert scopes_intersect(Scope("Acme/X", ("src/Api/one",)), Scope.parse("acme/x", ["src/api/one"]))


def test_a_stored_prefix_that_would_now_be_refused_does_not_break_reads(conn):
    """A legacy row holding a prefix current rules refuse must not take the registry down."""
    plant_old_style_lease(conn, holder="dana", repo="acme/web-platform", prefixes=["src/bud​get/grid"])
    out = leases.claim(conn, actor="kofi", repo="acme/web-platform",
                       prefixes=["src/budget/grid"], intent="implement")
    assert out["state"] == "accepted"


def test_concurrent_claims_in_two_spellings_cannot_both_miss_each_other(tmp_path):
    path = tmp_path / "race.sqlite3"
    results: dict[str, dict] = {}
    barrier = threading.Barrier(2)

    def go(name, repo):
        c = db.connect(path)
        barrier.wait()
        results[name] = leases.claim(c, actor=name, repo=repo, prefixes=["src/api/one"], intent="implement")

    ts = [threading.Thread(target=go, args=("dana", "Acme/App")),
          threading.Thread(target=go, args=("kofi", "acme/app"))]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert results["dana"]["conflicts"] or results["kofi"]["conflicts"]
