"""Tier 3 — the session hook. A private channel, and the one that must never break a session.

`inject.render_session_block` exists and nothing calls it — this module is what calls it.
Tier 1 (`inject.py`) writes into a file the whole team reads, so its content is repo-scoped
by construction. Personal state (what YOU snapshotted, what YOUR live leases collide with)
has no home there — publishing it into a committed file would leak one person's working
directories and pointers to everyone else, permanently, via version control. The session
hook is the other half: delivered to exactly one person, at exactly one moment (session
start), through a channel nothing else reads.

**Fail open, without exception, is the entire design constraint.** A `SessionStart` hook
runs on every session, for every colleague, in every repo this is installed into. If it
ever exits non-zero or hangs, it does not fail quietly — it fails as "my colleague's editor
would not start," which is disqualifying regardless of how good the content would have
been. So every dependency this module touches — the registry database, the work queue
file, the SOP library file, even the act of importing `agentco`'s other modules — is wrapped
in its own `try/except Exception`, lazily, at the point of use. A broken registry degrades
this hook to "no divergence/conflict section, and say so"; a broken `agentco.db` import
degrades it the same way. Nothing here can turn a dependency problem into a startup
failure. The one outer `try/except` around `main()` is the backstop of last resort — it
exists for whatever this design did not anticipate, not as the primary mechanism.

**Content pulls the model toward using the tools, not just informing it.** A digest nobody
is told to act on is trivia. The rendered context always leads with an explicit instruction
to call the `events` MCP tool with a saved cursor — this is what turns MCP from a tool a
harness might remember from its training data into one this specific session is told to use
right now.

**stdout carries exactly one line of JSON and nothing else** — the same stdio-hygiene rule
`mcp_server.py` follows for the same reason: on this channel, anything else corrupts what
the harness is trying to parse. Every diagnostic goes to stderr.

**stdlib only.** `agentco.app` and `agentco.mcp_server` pull in FastAPI and the `mcp`
package respectively; this module must not gain either dependency just to read a database
and two JSONL files, so — like `mcp_server.py` before it — the tiny config constants below
are duplicated rather than imported.
"""

from __future__ import annotations

import base64
import difflib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

DB_ENV_VAR = "AGENTCO_REGISTRY_DB"
DEFAULT_DB = "registry.sqlite3"

WORK_STORE_ENV_VAR = "AGENTCO_WORK_STORE"
DEFAULT_WORK_STORE = "work.jsonl"

SOP_STORE_ENV_VAR = "AGENTCO_SOP_STORE"
DEFAULT_SOP_STORE = "sops.jsonl"

ACTOR_ENV_VAR = "AGENTCO_ACTOR"
DEFAULT_ACTOR = "mcp-actor"

PathLike = Union[str, Path]

# Real budget is enforced by Claude Code at 8,000 characters of additionalContext,
# but this module holds itself to a tighter number on purpose: this is ONE
# session's worth of context, on top of whatever tier 1 already put in
# CLAUDE.md, paid on every turn for the life of the session — "hard-capped in
# bytes" per the design, not "capped at whatever the harness happens to allow".
DEFAULT_MAX_CONTEXT_BYTES = 4000

PULL_INSTRUCTION = (
    "AgentCo coordination tools are available over MCP this session. If you have a "
    "saved `events` cursor from earlier work in this repo, call `events(since=<cursor>)` "
    "now to catch up on scope claims and divergence you may have missed. If you do not "
    "have one, call `events()` once to get your bearings, and remember the `nextCursor` "
    "it returns so a future session can resume from exactly where this one left off."
)


def resolve_db_path(path: Optional[str] = None) -> str:
    from agentco.stores import resolve_registry_db

    return resolve_registry_db(path, DB_ENV_VAR, DEFAULT_DB)


def resolve_work_store(path: Optional[str] = None) -> str:
    return path or os.environ.get(WORK_STORE_ENV_VAR) or DEFAULT_WORK_STORE


def resolve_sop_store(path: Optional[str] = None) -> str:
    return path or os.environ.get(SOP_STORE_ENV_VAR) or DEFAULT_SOP_STORE


def resolve_actor(actor: Optional[str] = None) -> str:
    return actor or os.environ.get(ACTOR_ENV_VAR) or DEFAULT_ACTOR


# --------------------------------------------------------------------------- #
# Content — three independently-failing sections plus the pull instruction
# --------------------------------------------------------------------------- #


def _registry_section(actor: str) -> tuple[Optional[str], Optional[str]]:
    """(rendered text or None, warning or None). Never raises.

    Imports are LAZY and inside the try on purpose — a broken `agentco.db` (or
    anything it transitively imports) must degrade this ONE section, not the
    whole hook. Reuses `inject.render_session_block` rather than re-deriving
    the digest/conflict format: this is the actor-scoped content that module
    already exists to produce.
    """
    try:
        from agentco import db, divergence, inject, leases

        conn = db.connect(resolve_db_path())
        digest = divergence.collect(conn)
        conflicts = leases.conflicts_for(conn, actor)
        return inject.render_session_block(digest, conflicts, actor=actor), None
    except Exception as exc:  # noqa: BLE001 - one of the named independent dependencies
        return None, f"divergence/scope-conflict check unavailable ({type(exc).__name__}: {exc})"


def _work_section(actor: str) -> tuple[Optional[str], Optional[str]]:
    """Same contract as `_registry_section`, for the work queue store."""
    try:
        from agentco.stores import open_queue
        from agentco.work import WorkStatus

        queue = open_queue()
        ready = queue.ready(agent=actor)
        held = [item for item in queue.list() if item.leased_by == actor and item.status == WorkStatus.IN_PROGRESS]
        lines = []
        if held:
            lines.append(f"Work items you currently hold a lease on: {len(held)}")
        if ready:
            lines.append(f"Ready work items you could pull: {len(ready)}")
        return ("\n".join(lines) if lines else None), None
    except Exception as exc:  # noqa: BLE001 - one of the named independent dependencies
        return None, f"work queue unavailable ({type(exc).__name__}: {exc})"


def _sop_section() -> tuple[Optional[str], Optional[str]]:
    """Same contract again, for the SOP library store."""
    try:
        from agentco.stores import open_sop_library

        library = open_sop_library()
        active = library.list_active()
        return (f"Active SOPs in this library: {len(active)}" if active else None), None
    except Exception as exc:  # noqa: BLE001 - one of the named independent dependencies
        return None, f"SOP library unavailable ({type(exc).__name__}: {exc})"


def _truncate(text: str, max_bytes: int) -> str:
    """Cut at a byte boundary, back off to a full line, and always say so.

    Silence about a cut section reads as "there was nothing more" — the one
    failure mode this whole system exists to avoid — so the notice is never
    optional, and its own size is reserved from the budget up front rather
    than risking it being the thing that gets cut.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    notice = f"\n… truncated to stay under the {max_bytes}-byte session-context budget."
    if len(notice.encode("utf-8")) >= max_bytes:
        # The notice alone does not fit. Returning it anyway EXCEEDED the cap
        # this function is named for — a "hard cap" that is not one is worse
        # than a soft cap that says so, because callers size their budgets
        # against the promise. A marker short enough to fit is the honest
        # answer; if even that does not fit, the caller asked for a budget too
        # small to say anything in and gets nothing rather than a lie.
        short = "…[truncated]"
        return short if len(short.encode("utf-8")) <= max_bytes else ""
    budget = max(0, max_bytes - len(notice.encode("utf-8")))
    truncated = encoded[:budget].decode("utf-8", errors="ignore")
    last_newline = truncated.rfind("\n")
    if last_newline > 0:
        truncated = truncated[:last_newline]
    return truncated + notice


def build_additional_context(actor: str, max_bytes: int = DEFAULT_MAX_CONTEXT_BYTES) -> str:
    """Assemble the session's `additionalContext`, degrading in NAMED pieces.

    The pull instruction is always first and is never a candidate for
    truncation ahead of the sections below it — losing it to a byte cap would
    silently turn this from "the thing that tells the model to use the tools"
    into "a status report nobody was told what to do with", which is the
    entire point this hook exists to avoid.
    """
    sections = [PULL_INSTRUCTION]
    warnings: list[str] = []

    for text, warning in (_registry_section(actor), _work_section(actor), _sop_section()):
        if text:
            sections.append(text)
        if warning:
            warnings.append(warning)

    if warnings:
        # Named, not summarised into "some things failed" — a colleague
        # debugging why their SOP count never showed up needs the dependency
        # name, not just the fact that something, somewhere, was unhappy.
        #
        # AND PLACED SECOND, not last. `_truncate` cuts from the tail, so
        # appending this at the end made it the FIRST thing to disappear under
        # budget pressure — the one piece of content this module argues at
        # length must never vanish silently. Worse, a truncated session is
        # exactly when a degraded dependency matters most, because the reader
        # has less context to notice the gap for themselves.
        #
        # Immediately after the pull instruction: that stays first because a
        # reader who is told nothing else must still be told what to call.
        sections.insert(1, "Unavailable this session: " + "; ".join(warnings))

    return _truncate("\n\n".join(sections), max_bytes)


# --------------------------------------------------------------------------- #
# The hook entry point — the outer backstop
# --------------------------------------------------------------------------- #


def main() -> int:
    """Print one line of `SessionStart` JSON to stdout. ALWAYS returns 0.

    Everything above this function already isolates its own failures; this
    try/except exists for whatever that design did not anticipate — a
    genuinely surprising bug is still not license to fail a colleague's
    session start, so even here the answer is "inject nothing and say why on
    stderr", never a non-zero exit.
    """
    try:
        actor = resolve_actor()
        context = build_additional_context(actor)
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }
        print(json.dumps(payload))
        return 0
    except Exception as exc:  # noqa: BLE001 - the backstop; see docstring
        print(f"agentco session hook failed to build context ({type(exc).__name__}): {exc}", file=sys.stderr)
        print(json.dumps({}))
        return 0


# --------------------------------------------------------------------------- #
# The installer — dry-run default, byte-identical uninstall
# --------------------------------------------------------------------------- #

DEFAULT_HOOK_TIMEOUT_S = 10


@dataclass(frozen=True)
class HookInstallResult:
    path: str
    status: str  # "installed" | "already_installed" | "would_install" |
    #               "uninstalled" | "would_uninstall" | "nothing_to_uninstall" | "error"
    reason: str
    diff: Optional[str] = None


def default_hook_command() -> str:
    """This interpreter, running this module — correct for the environment that installed it."""
    return f"{sys.executable} -m agentco.hook"


def _backup_path(settings_path: Path) -> Path:
    return settings_path.with_name(settings_path.name + ".agentco-backup.json")


def install(settings_path: PathLike, *, command: Optional[str] = None, write: bool = False) -> HookInstallResult:
    """Register this module as a `SessionStart` hook in a harness's settings file.

    Takes a VERBATIM byte backup before the first write rather than attempting
    a lossless in-place JSON edit. Preserving a file's exact formatting and key
    order through a parse/mutate/reserialize round trip is not something the
    `json` module promises, and getting that subtly wrong — a reordered key, a
    different indent width — is worse than not trying, because it looks like
    success. `uninstall()` restores the saved bytes directly instead of
    reverse-engineering what this function changed.
    """
    settings_path = Path(settings_path)
    command = command or default_hook_command()
    backup_path = _backup_path(settings_path)

    existed = settings_path.exists()
    original_bytes: Optional[bytes] = None
    if existed:
        original_bytes = settings_path.read_bytes()
        try:
            config = json.loads(original_bytes.decode("utf-8")) if original_bytes.strip() else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return HookInstallResult(
                path=str(settings_path),
                status="error",
                reason=f"{type(exc).__name__}: settings file is not valid JSON — refusing to guess at its structure",
            )
        if not isinstance(config, dict):
            return HookInstallResult(
                path=str(settings_path), status="error", reason="settings file's top level is not a JSON object"
            )
    else:
        config = {}

    hooks = config.setdefault("hooks", {})
    session_start = hooks.setdefault("SessionStart", [])
    already = any(
        isinstance(group, dict)
        and isinstance(entry, dict)
        and entry.get("command") == command
        for group in session_start
        for entry in group.get("hooks", [])
    )
    if already:
        return HookInstallResult(
            path=str(settings_path), status="already_installed", reason="this exact hook command is already registered"
        )

    session_start.append({"hooks": [{"type": "command", "command": command, "timeout": DEFAULT_HOOK_TIMEOUT_S}]})
    # `ensure_ascii=False`, and it is not cosmetic. The default escapes every
    # non-ASCII character in the WHOLE file — an em-dash in a comment field
    # three hundred lines away from anything AgentCo added becomes `—` —
    # so installing a hook silently re-encoded bytes outside its own change.
    # Found by dry-running the install against a real settings.json carrying
    # em-dashes in its own documentation strings, which is exactly the input a
    # fixture written by this tool could never produce.
    #
    # Same shape as the CRLF trap in `inject.py`: a round trip through a text
    # layer that normalises something, rewriting a file the tool promised only
    # to add one entry to. This module's docstring already argued that getting
    # a parse/mutate/reserialize round trip "subtly wrong" is worse than not
    # trying — and then did it.
    new_bytes = (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    diff = "\n".join(
        difflib.unified_diff(
            (original_bytes or b"").decode("utf-8", errors="replace").splitlines(),
            new_bytes.decode("utf-8").splitlines(),
            fromfile=f"{settings_path} (before)",
            tofile=f"{settings_path} (after)",
            lineterm="",
        )
    )

    if not write:
        return HookInstallResult(
            path=str(settings_path), status="would_install", reason="dry run — pass write=True to apply", diff=diff
        )

    if not backup_path.exists():
        # ONE-TIME snapshot. A second `install()` run — e.g. re-running after
        # editing `--command` — must NEVER overwrite this with the
        # already-modified file, or "pristine" quietly comes to mean
        # "whatever this tool last wrote", and uninstall stops being able to
        # get back to the state before AgentCo ever touched the file.
        record = {
            "existed": existed,
            "content_b64": base64.b64encode(original_bytes).decode("ascii") if existed else None,
        }
        backup_path.write_text(json.dumps(record), encoding="utf-8")

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_bytes(new_bytes)
    return HookInstallResult(path=str(settings_path), status="installed", reason="hook registered", diff=diff)


def uninstall(settings_path: PathLike, *, write: bool = False) -> HookInstallResult:
    """Restore the settings file to exactly what it was before `install()` ever ran.

    This never parses the CURRENT file as JSON — it does not need to, and
    that is the point. Whatever shape the file is in now (hand-edited,
    reformatted, broken), the restore is a plain byte copy from the backup
    taken at install time, so "byte-identical" holds unconditionally rather
    than depending on nobody having touched the file since.
    """
    settings_path = Path(settings_path)
    backup_path = _backup_path(settings_path)

    if not backup_path.exists():
        return HookInstallResult(
            path=str(settings_path),
            status="nothing_to_uninstall",
            reason="no AgentCo install record found next to this settings file",
        )

    record = json.loads(backup_path.read_text(encoding="utf-8"))
    existed = record["existed"]
    original_bytes = base64.b64decode(record["content_b64"]) if existed else None
    current = settings_path.read_bytes() if settings_path.exists() else b""

    if existed:
        diff = "\n".join(
            difflib.unified_diff(
                current.decode("utf-8", errors="replace").splitlines(),
                original_bytes.decode("utf-8", errors="replace").splitlines(),
                fromfile=f"{settings_path} (current)",
                tofile=f"{settings_path} (restored)",
                lineterm="",
            )
        )
    else:
        diff = f"would delete {settings_path} — it did not exist before install"

    if not write:
        return HookInstallResult(
            path=str(settings_path), status="would_uninstall", reason="dry run — pass write=True to apply", diff=diff
        )

    if existed:
        settings_path.write_bytes(original_bytes)
    else:
        settings_path.unlink(missing_ok=True)
    backup_path.unlink(missing_ok=True)
    return HookInstallResult(path=str(settings_path), status="uninstalled", reason="original bytes restored verbatim", diff=diff)


if __name__ == "__main__":
    sys.exit(main())
