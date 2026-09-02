"""leakguard — the guard that keeps the public repo publishable.

The tests worth having are the ones that would fail if the guard were
decorative: that it catches the exact shapes that actually leaked (a real
email, a cloud tenant GUID, a home path, a colleague's name), that its
suppressions are visible rather than silent, and that a missing config
degrades loudly instead of passing everything.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "leakguard"))

import leakguard  # noqa: E402


def scan(text: str, config: leakguard.Config | None = None) -> list[leakguard.Finding]:
    cfg = config or leakguard.Config()
    rules = leakguard.BASE_RULES + cfg.configured_rules()
    return leakguard.scan_text(text, "f.md", rules)


def rules_hit(findings) -> set[str]:
    return {f.rule for f in findings}


# --------------------------------------------------------------------------- #
# The shapes that actually leaked
# --------------------------------------------------------------------------- #


def test_a_real_email_is_caught():
    assert "email" in rules_hit(scan('requestedBy: "dana@acmecorp.com"'))  # leakguard: allow


def test_reserved_example_domains_are_not_flagged():
    """Docs need example addresses. Flagging them would train people to
    suppress the rule, which is worse than not having it."""
    for safe in ("a@example.com", "b@example.org", "c@example.invalid", "d@localhost"):
        assert "email" not in rules_hit(scan(safe)), safe


def test_a_cloud_tenant_guid_is_caught():
    """Individually not a credential; together with the others it is a map of
    somebody's estate. The GUID below is fabricated — a test fixture for a
    leak detector must never be a real value, or the fixture is the leak."""
    findings = scan("tenant 0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0")  # leakguard: allow
    assert "uuid" in rules_hit(findings)
    assert "TENANT_ID" in findings[0].remediation


def test_a_home_directory_path_is_caught():
    assert "home-path" in rules_hit(scan("cd /Users/somebody/Code/thing"))  # leakguard: allow
    assert "home-path" in rules_hit(scan("cd /home/somebody/src"))  # leakguard: allow


def test_loopback_is_not_flagged_as_a_private_host():
    """The guard must not fire on the addresses a public repo legitimately
    ships — otherwise every default binding becomes a suppression."""
    assert "private-host" not in rules_hit(scan("uvicorn --host 127.0.0.1"))
    assert "private-host" not in rules_hit(scan("bind 0.0.0.0:8787"))


def test_an_internal_hostname_is_caught():
    assert "private-host" in rules_hit(scan("ssh buildbox.internal"))  # leakguard: allow


def test_credential_shapes_are_still_caught():
    """Not this tool's main job, but a token found here is a token found."""
    assert "credential" in rules_hit(scan("token = ghp_" + "a" * 30))
    assert "credential" in rules_hit(scan("https://acme.webhook.office.com/webhookb2/xyz"))  # leakguard: allow


# --------------------------------------------------------------------------- #
# The configured rules — the part that must never be hardcoded
# --------------------------------------------------------------------------- #


def test_a_denylisted_name_is_caught_case_insensitively():
    cfg = leakguard.Config(names=("Dana",))
    assert "person-name" in rules_hit(scan("dana reads it as a metric", cfg))


def test_a_name_inside_a_longer_word_is_not_a_match():
    """Word-bounded, or 'Jim' flags 'Jimmy' and every mention of a JIMdb table,
    and a rule with false positives is a rule people disable."""
    cfg = leakguard.Config(names=("Jim",))
    assert "person-name" not in rules_hit(scan("the jimmying of the lock", cfg))


def test_the_person_rule_says_renaming_does_not_rescue_an_analysis():
    """The remediation has to carry the thing the regex cannot: that a passage
    ANALYSING someone is not fixed by relabelling them."""
    cfg = leakguard.Config(names=("Dana",))
    finding = scan("Dana would object", cfg)[0]
    assert "cut those rather than relabel" in finding.remediation


def test_denylisted_domains_and_terms_are_caught():
    cfg = leakguard.Config(domains=("acmecorp.com",), terms=("ProjectFalcon",))
    hits = rules_hit(scan("see acmecorp.com and ProjectFalcon", cfg))
    assert "company-domain" in hits
    assert "internal-term" in hits


def test_no_company_string_is_hardcoded_in_the_scanner():
    """The guard must obey the rule it enforces — otherwise the tool itself is
    unpublishable, which would be a short and instructive irony."""
    source = Path(leakguard.__file__).read_text(encoding="utf-8")
    empty = leakguard.Config()
    assert empty.configured_rules() == (), "base rules must carry no names/domains/terms"
    # The scanner, run against itself with base rules, must come back clean.
    assert leakguard.scan_text(source, "leakguard.py", leakguard.BASE_RULES) == []


# --------------------------------------------------------------------------- #
# Suppression is visible, and absence of config is loud
# --------------------------------------------------------------------------- #


def test_an_inline_allow_suppresses_that_line_only():
    text = "email a@acmecorp.com  # leakguard: allow\nemail b@acmecorp.com"
    findings = scan(text)
    assert len(findings) == 1
    assert findings[0].line_no == 2


def test_an_allowed_path_is_skipped(tmp_path):
    target = tmp_path / "docs" / "adopting.md"
    target.parent.mkdir(parents=True)
    target.write_text("write to someone@acmecorp.com")  # leakguard: allow
    cfg = leakguard.Config(allow_paths=("docs/adopting.md",))
    assert leakguard.scan_paths([target], tmp_path, cfg) == []


def test_a_missing_config_still_runs_the_base_rules(tmp_path, capsys):
    """Degrading to 'no name checks' is a real reduction in cover. It must not
    read as a pass — main() says so on stderr and the base rules still fire."""
    (tmp_path / "leaky.md").write_text("tenant 0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0")  # leakguard: allow
    exit_code = leakguard.main(["--root", str(tmp_path)])
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "reduced cover, not a pass" in err
    assert "uuid" in err


def test_a_clean_tree_exits_zero(tmp_path, capsys):
    (tmp_path / "ok.md").write_text("Contact us at hello@example.com. Bind 127.0.0.1.")
    assert leakguard.main(["--root", str(tmp_path)]) == 0
    assert "clean" in capsys.readouterr().out


def test_an_undecodable_file_is_reported_rather_than_silently_skipped(tmp_path):
    """This test used to assert the opposite, and asserting it is what made the
    hole permanent rather than merely present.

    It read `scan_paths(...) == []` and called that "skipped, not fatal" — so a
    UTF-16 source with a credential in it produced no finding, the run printed
    `clean`, and the file count included a file nobody had looked at. The
    module's own docstring says a suppression nobody can see is how a scanner
    quietly stops scanning; an unreadable file is a suppression nobody chose,
    and the test encoded it as correct behaviour.

    Not fatal is still right — one bad file must not stop the scan. Silent is
    what was wrong."""
    (tmp_path / "blob.py").write_bytes(
        ("TOKEN = 'ghp_" + "A" * 30 + "'").encode("utf-16")  # leakguard: allow
    )
    findings = leakguard.scan_paths([tmp_path / "blob.py"], tmp_path, leakguard.Config())
    assert [f.rule for f in findings] == ["unreadable"]
    assert "NOT checked" in findings[0].remediation


def test_findings_report_file_line_rule_and_remediation():
    finding = scan("mail me at real@acmecorp.com")[0]  # leakguard: allow
    rendered = finding.render()
    assert "f.md:1" in rendered
    assert "[email]" in rendered
    assert "→" in rendered


# --------------------------------------------------------------------------- #
# Stores are never artefacts of this project
# --------------------------------------------------------------------------- #


def test_live_store_filenames_are_gitignored():
    """`work.jsonl` reached two commits, and leakguard could not have caught it.

    The store held no names, so nothing in the guard's remit fired — and a work
    store in a published tree is still somebody's queue state on the internet.
    Two things conspired: `git add -A`, and a work-store default that is a
    RELATIVE path, so a server or a test started in the repo root writes one here
    where it looks like a source file.

    leakguard scans CONTENT. This is about a filename, which is why it lives
    beside it as its own check rather than as a rule inside it.
    """
    ignored = Path(".gitignore").read_text().splitlines()
    for name in ("work.jsonl", "sops.jsonl", "tasks.jsonl", "receipts.jsonl", ".agentco/"):
        assert name in ignored, (
            f"{name} is not gitignored — a live store is one `git add -A` away "
            f"from being published, and the guard next door cannot see filenames"
        )


def test_no_store_is_tracked_in_this_repository():
    """The other half: gitignoring a file that is ALREADY tracked changes nothing.

    `.gitignore` is not retroactive, so the entry above would have been cosmetic
    while the committed copy kept updating on every `git add -A`. This asserts
    the state, not the intention.
    """
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    offenders = [
        path
        for path in tracked
        if Path(path).name in ("work.jsonl", "sops.jsonl", "tasks.jsonl", "receipts.jsonl")
        or path.startswith(".agentco/")
    ]
    assert offenders == [], f"live store(s) tracked in a public repo: {offenders}"
