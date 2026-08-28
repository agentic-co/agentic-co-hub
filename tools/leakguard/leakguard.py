"""leakguard — refuse a commit that carries identity into a public repo.

WHY THIS EXISTS: a policy saying "don't add names" fails the same way every
policy without a mechanism fails. This project's own doctrine is to fail
loudly at the layer where the failure occurs, so the rule that keeps the
public repo publishable is a hook, not a paragraph in CONTRIBUTING.md.

WHAT IT LOOKS FOR, and the reasoning behind each:

  * **Email addresses** outside the reserved example domains. A real address
    is both PII and a working contact for someone who did not consent.
  * **UUIDs / GUIDs.** Cloud tenant, subscription, client and directory ids
    all take this shape. Individually they are not credentials; together they
    are a map of somebody's estate.
  * **Home-directory paths.** `/Users/<name>` and `/home/<name>` leak a real
    person's account name, and they also mean the code only runs on one
    machine — so this check earns its place twice.
  * **Denylisted names and domains.** Organisation names, product codenames
    and people. This is the list that cannot be inferred, so it is supplied.
  * **Private hosts and credential shapes.** Belt and braces; a secret scanner
    is not this tool's job, but a token found here is still a token found.

**THE DENYLIST IS CONFIGURATION, NOT CODE.** That is the same principle the
public repo is extracted under — anything naming a company or a person is
configuration. Hardcoding your own employer's name into this file would make
the tool itself unpublishable — an irony this docstring earned honestly, by
doing exactly that in its first draft and being caught by its own scanner. Ship
`leakguard.toml.example`; keep the real list out of the tree, or in a repo
that never goes public.

FALSE POSITIVES are expected and cheap to handle: put `leakguard: allow` in a
line's comment, or list the path under `[allow] paths` in the config. Both are
deliberately visible — a suppression nobody can see is how a scanner quietly
stops scanning.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

# Reserved-for-documentation domains (RFC 2606 / RFC 6761). An address at one
# of these is by definition not a real person's.
SAFE_EMAIL_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "example.invalid",
    "example.test",
    "localhost",
    "invalid",
    "test",
}

INLINE_ALLOW = "leakguard: allow"

# Extensions worth reading. A binary sweep would be noise, and a public repo
# should not be shipping binaries with identity in them anyway.
DEFAULT_INCLUDE_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".rb", ".java", ".cs",
    ".sh", ".bash", ".zsh", ".fish", ".ps1",
    ".md", ".mdx", ".rst", ".txt",
    ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".env.example",
    ".html", ".css", ".sql", ".proto", ".graphql", ".tf",
}

DEFAULT_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", ".next", "target",
}


@dataclass(frozen=True)
class Finding:
    path: str
    line_no: int
    rule: str
    match: str
    remediation: str

    def render(self) -> str:
        return (
            f"{self.path}:{self.line_no}: [{self.rule}] {self.match}\n"
            f"    → {self.remediation}"
        )


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern
    remediation: str
    # Given a match, return True to KEEP it as a finding. Lets a rule express
    # "this shape, except the allowed cases" without a second pattern.
    keep: Optional[object] = None


def _email_is_real(match: re.Match) -> bool:
    return match.group("domain").lower() not in SAFE_EMAIL_DOMAINS


BASE_RULES: tuple[Rule, ...] = (
    Rule(
        name="email",
        pattern=re.compile(
            r"\b[A-Za-z0-9._%+-]+@(?P<domain>[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b"
        ),
        remediation=(
            "Replace with an address at example.com — a real address is PII and a "
            "working contact for someone who did not agree to be listed."
        ),
        keep=_email_is_real,
    ),
    Rule(
        name="uuid",
        pattern=re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        remediation=(
            "Cloud tenant / subscription / client ids take this shape. Replace with "
            "a placeholder like <TENANT_ID> and read the real value from config."
        ),
    ),
    Rule(
        name="home-path",
        pattern=re.compile(r"(?:/Users/|/home/|C:\\\\Users\\\\)[A-Za-z0-9._-]+"),
        remediation=(
            "Leaks a real account name, and pins the code to one machine. Use "
            "$HOME, ~, or a configured path."
        ),
    ),
    Rule(
        name="private-host",
        # The exemptions are ANCHORED to the whole match, not trailing-matched.
        # The previous form put the lookbehinds after `\b`, so `(?<!0\.0\.0\.0)`
        # excluded any address whose last seven characters were "0.0.0.0" —
        # taking 10.0.0.0/8, the most common private range there is, along with  # leakguard: allow
        # 20.0.0.0 through 250.0.0.0. An exemption for one literal must not  # leakguard: allow
        # become a suffix rule.
        pattern=re.compile(
            r"(?<![\w.])(?!(?:0\.0\.0\.0|127\.0\.0\.1)(?![\d.]))"
            r"(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"
            r"|\b[a-z0-9-]+\.(?:local|internal|corp|lan)\b"
        ),
        remediation=(
            "Internal hostname or address. Use a documented placeholder, or "
            "127.0.0.1 / 0.0.0.0 where a real loopback is meant."
        ),
    ),
    Rule(
        name="credential",
        pattern=re.compile(
            r"gh[pousr]_[A-Za-z0-9]{20,}"
            r"|sk-[A-Za-z0-9]{24,}"
            r"|xox[baprs]-[A-Za-z0-9-]{12,}"
            r"|AKIA[0-9A-Z]{16}"
            r"|AIza[0-9A-Za-z_-]{35}"
            r"|https://[a-z0-9.-]*webhook\.office\.com/\S+"
            r"|https://hooks\.slack\.com/\S+"
        ),
        remediation=(
            "This is a live credential shape. Do not commit it — rotate it if it "
            "ever reached a remote, then read it from the environment."
        ),
    ),
)


@dataclass
class Config:
    """Everything company- or person-specific. Never hardcoded above."""

    names: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    allow_paths: tuple[str, ...] = ()
    include_suffixes: frozenset[str] = frozenset(DEFAULT_INCLUDE_SUFFIXES)

    @classmethod
    def load(cls, path: Optional[Path]) -> "Config":
        """Missing config is a WARNING, not a silent pass to an empty denylist.

        An empty denylist still runs every base rule, so the tool degrades to
        "catches emails, GUIDs and paths but no names" — which is a real
        reduction in cover and must be visible. `main()` prints it.
        """
        if path is None or not path.exists():
            return cls()
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        deny = data.get("deny", {})
        allow = data.get("allow", {})
        scan = data.get("scan", {})
        suffixes = scan.get("include_suffixes")
        return cls(
            names=tuple(deny.get("names", [])),
            domains=tuple(deny.get("domains", [])),
            terms=tuple(deny.get("terms", [])),
            allow_paths=tuple(allow.get("paths", [])),
            include_suffixes=frozenset(suffixes) if suffixes else frozenset(DEFAULT_INCLUDE_SUFFIXES),
        )

    def configured_rules(self) -> tuple[Rule, ...]:
        rules: list[Rule] = []
        if self.names:
            rules.append(
                Rule(
                    name="person-name",
                    pattern=re.compile(
                        r"\b(?:" + "|".join(re.escape(n) for n in self.names) + r")\b",
                        re.IGNORECASE,
                    ),
                    remediation=(
                        "Name a role, not a person — 'a platform engineer', not the "
                        "individual. Renaming does not rescue a passage that ANALYSES "
                        "someone; cut those rather than relabel them."
                    ),
                )
            )
        if self.domains:
            rules.append(
                Rule(
                    name="company-domain",
                    pattern=re.compile(
                        r"\b(?:" + "|".join(re.escape(d) for d in self.domains) + r")\b",
                        re.IGNORECASE,
                    ),
                    remediation=(
                        "A company domain is configuration, not code. Move it to a "
                        "config file that the public repo does not carry."
                    ),
                )
            )
        if self.terms:
            rules.append(
                Rule(
                    name="internal-term",
                    pattern=re.compile(
                        r"\b(?:" + "|".join(re.escape(t) for t in self.terms) + r")\b",
                        re.IGNORECASE,
                    ),
                    remediation=(
                        "Internal product or project codename. Describe the CAPABILITY "
                        "generically, and keep the name in the deployment repo."
                    ),
                )
            )
        return tuple(rules)


# Files worth scanning that carry no suffix, or whose suffix lies about them.
# `Path(".env.example").suffix` is ".example", so a suffix list containing
# ".env.example" could never match it — and every extensionless file was
# skipped outright, including `tools/leakguard/pre-commit`, the hook this repo
# ships. A filter that silently declines to look is the same failure as a
# scanner that looks and says nothing.
SCAN_BY_NAME = {
    "dockerfile", "makefile", "procfile", "jenkinsfile", "vagrantfile",
    "pre-commit", "pre-push", "commit-msg", ".env.example", ".env.sample",
    ".env.template", ".envrc", ".gitconfig", ".netrc",
}


def _is_scannable(path: Path, config: Config) -> bool:
    name = path.name.lower()
    if name in SCAN_BY_NAME:
        return True
    if name.startswith(".env"):
        return True
    if path.suffix:
        return path.suffix.lower() in config.include_suffixes
    # No suffix and not a known name: scan it if it looks like text. Guessing
    # from content rather than skipping is the right default for a guard —
    # a false positive costs one suppression, a false negative costs a leak.
    try:
        chunk = path.read_bytes()[:2048]
    except OSError:
        return False
    return b"\x00" not in chunk


def iter_files(root: Path, config: Config) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in DEFAULT_SKIP_DIRS for part in path.parts):
            continue
        if not _is_scannable(path, config):
            continue
        yield path


def _path_allowed(rel: str, config: Config) -> bool:
    return any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in config.allow_paths)


def scan_text(
    text: str,
    rel_path: str,
    rules: Sequence[Rule],
) -> list[Finding]:
    findings: list[Finding] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if INLINE_ALLOW in line:
            continue
        for rule in rules:
            for match in rule.pattern.finditer(line):
                if rule.keep is not None and not rule.keep(match):
                    continue
                findings.append(
                    Finding(
                        path=rel_path,
                        line_no=line_no,
                        rule=rule.name,
                        match=match.group(0),
                        remediation=rule.remediation,
                    )
                )
    return findings


def scan_paths(paths: Iterable[Path], root: Path, config: Config) -> list[Finding]:
    rules = BASE_RULES + config.configured_rules()
    findings: list[Finding] = []
    for path in paths:
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        if _path_allowed(rel, config):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: not this tool's job
        findings.extend(scan_text(text, rel, rules))
    return findings


class GitUnavailable(RuntimeError):
    """git could not be consulted, so what is staged is unknown.

    Raised rather than returning an empty list. An empty list is
    indistinguishable from "nothing is staged", so a git failure used to make
    the hook print `clean` and exit 0 — a guard whose whole purpose is to fail
    loudly, failing open on its own inability to look.
    """


def staged_blobs(root: Path) -> list[tuple[str, bytes]]:
    """`(path, content)` for everything staged — read from the INDEX, not disk.

    This is the fix for the defect that mattered most in this file. The old
    version listed names from the index and then read them from the WORKING
    TREE, so the two could differ — and the case where they differ is exactly
    the dangerous one:

        staged:   TOKEN = "ghp_..."
        worktree: TOKEN = os.environ["TOKEN"]

    The scanner read the clean worktree, reported `clean`, exited 0, and the
    commit recorded the credential. A guard that approves precisely the content
    it did not look at is worse than no guard, because it converts an absent
    check into a false assurance.

    `git cat-file` against the staged blob hash reads what is actually about to
    be committed. A staged-then-deleted file still has its blob, so it is
    scanned too — which the old path skipped silently through its OSError catch.
    """
    listing = subprocess.run(
        ["git", "-C", str(root), "diff", "--cached", "--name-only", "-z",
         "--diff-filter=ACMR"],
        capture_output=True,
    )
    if listing.returncode != 0:
        raise GitUnavailable(
            f"git could not list staged files "
            f"({listing.stderr.decode(errors='replace').strip() or 'no error text'})"
        )

    names = [n for n in listing.stdout.split(b"\x00") if n]
    out: list[tuple[str, bytes]] = []
    for raw_name in names:
        name = raw_name.decode("utf-8", errors="replace")
        blob = subprocess.run(
            ["git", "-C", str(root), "show", f":{name}"],
            capture_output=True,
        )
        if blob.returncode != 0:
            # Cannot read the staged content, so cannot vouch for it. Surfaced
            # as an unreadable finding rather than skipped — see main().
            out.append((name, b""))
            continue
        out.append((name, blob.stdout))
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="leakguard",
        description="Refuse content that carries identity into a public repo.",
    )
    parser.add_argument("--root", default=".", help="repo root (default: cwd)")
    parser.add_argument("--config", default=None, help="path to leakguard.toml")
    parser.add_argument(
        "--staged",
        action="store_true",
        help="scan only files staged for commit (pre-commit hook mode)",
    )
    parser.add_argument("paths", nargs="*", help="explicit paths to scan")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    config_path = Path(args.config) if args.config else root / "leakguard.toml"
    config = Config.load(config_path)

    if not config_path.exists():
        print(
            f"leakguard: no config at {config_path} — running base rules only "
            f"(no name, domain or codename checks). This is reduced cover, not a pass.",
            file=sys.stderr,
        )

    rules = BASE_RULES + config.configured_rules()
    if args.staged:
        # Scan the staged CONTENT, never the file on disk beside it.
        try:
            blobs = staged_blobs(root)
        except GitUnavailable as exc:
            print(f"leakguard: {exc} — refusing rather than reporting clean.", file=sys.stderr)
            return 1
        findings = []
        scanned = len(blobs)
        for name, content in blobs:
            if _path_allowed(name, config):
                continue
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                findings.append(
                    Finding(
                        path=name,
                        line_no=0,
                        rule="unreadable",
                        match="(staged content is not valid UTF-8)",
                        remediation=(
                            "This file could not be decoded, so it was NOT scanned. "
                            "Confirm by hand that it carries no credential or identity, "
                            "then allow the path in leakguard.toml. An unscanned file "
                            "must never be reported as a clean one."
                        ),
                    )
                )
                continue
            findings.extend(scan_text(text, name, rules))
    else:
        targets = [Path(p).resolve() for p in args.paths] if args.paths else list(iter_files(root, config))
        scanned = len(targets)
        findings = scan_paths(targets, root, config)
    if not findings:
        print(f"leakguard: clean ({scanned} file(s) scanned)")
        return 0

    by_rule: dict[str, int] = {}
    for finding in findings:
        by_rule[finding.rule] = by_rule.get(finding.rule, 0) + 1

    print(f"leakguard: {len(findings)} finding(s) — refusing.\n", file=sys.stderr)
    for finding in findings:
        print(finding.render(), file=sys.stderr)
    print(
        "\nsummary: " + ", ".join(f"{k}={v}" for k, v in sorted(by_rule.items())),
        file=sys.stderr,
    )
    print(
        f"\nIf a finding is genuinely fine, add '{INLINE_ALLOW}' to that line's comment "
        f"or list the path under [allow] paths in leakguard.toml. Both are visible on "
        f"purpose — a suppression nobody can see is how a scanner stops scanning.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
