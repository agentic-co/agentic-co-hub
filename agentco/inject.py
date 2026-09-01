"""Tier-1 context injection — the only mechanism that reaches a harness we do not control.

docs/architecture.md's integration surface is pull-only: a harness calls a tool, AgentCo
answers. Nothing lets AgentCo say "the spec you snapshotted moved" on its own, because
nothing can push into a model's context — context is assembled by the harness at its own
turn boundaries, on its own schedule, using files it already decided to read. The only
lever left is to become one of those files: splice a short, marker-delimited block into
the repo's own agent-context file (`CLAUDE.md`, `AGENTS.md`), refreshed by a scheduled
job. Zero effort from whoever owns that repo — no config, no auth, no opt-in. Every
CLI-class agent working there picks it up on its next session, because reading that file
is already how it starts one.

The splice is trivial. What is not trivial is the promise it makes: **a single render
must never touch a byte outside the marker pair.** That promise failed once already, and
the failure shipped, so the rest of this module exists to make it fail loudly instead.

**The CRLF trap.** `Path.read_text()` opens in universal-newline mode and silently turns
every `\\r\\n` into `\\n` in memory; `write_text()` then persists that back, re-encoding the
ENTIRE file — every byte, not just the managed block — the moment a CRLF-authored repo's
`CLAUDE.md` gets rendered once. It shipped invisibly because every test fixture was
written with `write_text()`, which never produces a CRLF file in the first place: the
suite was green and was evidence about the fixtures, not the tool. The fix here is to
never decode the file for the splice itself — read bytes, detect the file's OWN
predominant line ending by counting bytes, render the block to match, write bytes. A
fixture with real `\\r\\n` bytes is what proves this, because nothing else can produce the
bug's input.

**The decode-error trap.** Building a human-readable diff DOES need to decode the file —
there is no way to show a text diff of bytes. `UnicodeDecodeError` is a `ValueError`
subclass, not an `OSError`, and a per-target `except OSError` around a batch of targets
lets it escape, killing every target after the bad one while the targets before it have
already been written — list order silently decides who gets touched. The fix is a
deliberately broad `except Exception` per target in `run()`, with the exception type
always recorded, so a decode failure refuses ONE target loudly and the rest proceed.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Union

BEGIN_MARKER = "<!-- agentco:context:begin -->"
END_MARKER = "<!-- agentco:context:end -->"

# A block that grows without bound costs every harness reading this file real
# context on every single turn it takes in this repo — and an injected block
# that keeps growing is the kind a repo owner eventually deletes wholesale
# rather than tolerates. Truncation below is always announced, never silent.
DEFAULT_MAX_BLOCK_BYTES = 2048

PathLike = Union[str, Path]


@dataclass(frozen=True)
class InjectResult:
    """One target's outcome. `diff` is populated whenever there was a change to show."""

    path: str
    status: str  # "written" | "would_write" | "unchanged" | "skipped" | "error"
    reason: str
    diff: Optional[str] = None


class MalformedBlockError(Exception):
    """A BEGIN marker exists with no matching END — refusing to guess the boundary.

    Guessing where an unterminated block "should" end (splice to end of file,
    splice to the next blank line, ...) risks eating content that was never
    part of the managed block, which is the one thing this module promises
    never to do. A human left the file in this state; a human resolves it.
    """


def _detect_newline(raw: bytes) -> bytes:
    """The file's OWN predominant line ending, detected on bytes — never assumed.

    Counting bytes rather than decoding is the entire fix for the CRLF trap
    described in the module docstring: nothing here ever passes the file
    through a mode that could normalise a line ending. A file with no
    newlines at all (empty, or a single line) has nothing to detect, so this
    defaults to `\\n` — the worst case is "you get LF for a file that never
    evidenced any ending", never a rewrite of an ending that WAS there.
    """
    crlf = raw.count(b"\r\n")
    bare_lf = raw.count(b"\n") - crlf
    return b"\r\n" if crlf > 0 and crlf >= bare_lf else b"\n"


class BlockEscapeError(ValueError):
    """The content would break out of the managed block.

    Defence in depth. The callers are validated (`scope.reject_control_characters`),
    but the splice is the layer that can actually GUARANTEE the invariant — and
    the invariant is not the one `_splice`'s docstring states. That one is true:
    bytes outside the markers are copied verbatim. The one that matters is that
    the managed block contains ONLY managed content, and nothing enforced it.

    Content carrying an end marker escapes the block, and because `_splice`
    replaces BEGIN..FIRST-END, everything after the injected marker becomes
    permanent — a later render with clean state cannot remove it. The tool
    cannot undo what it wrote.
    """


def _block_bytes(content: str, newline: bytes) -> bytes:
    """The managed block, rendered with the FILE's line ending, not the caller's.

    `content` is a plain `str` using `\\n` internally by convention — the caller
    (`render_context_block`, or a test, or a future connector) never has to
    know or care what ending the target file happens to use. This is the one
    place that translation happens.
    """
    for marker in (BEGIN_MARKER, END_MARKER):
        if marker in content:
            raise BlockEscapeError(
                f"refusing to render content containing {marker!r}. It would "
                f"close the managed block early, and because the splice replaces "
                f"BEGIN through the FIRST end marker, everything after it would "
                f"be permanent — this tool could not remove it on a later run."
            )

    lines = [BEGIN_MARKER, *content.strip("\n").split("\n"), END_MARKER]
    return newline.join(line.encode("utf-8") for line in lines)


def _splice(raw: bytes, content: str) -> bytes:
    """Pure byte transformation. No decoding — see the module docstring.

    Everything strictly before the begin marker and strictly after the end
    marker is copied by slicing, verbatim, byte for byte. Nothing in this
    function can touch it, which is the property the whole module exists to
    hold and the reason it is factored out on its own rather than inlined
    into `render_into` alongside the diff/write logic below.
    """
    newline = _detect_newline(raw)
    block = _block_bytes(content, newline)

    begin_bytes = BEGIN_MARKER.encode("utf-8")
    end_bytes = END_MARKER.encode("utf-8")

    begin_idx = raw.find(begin_bytes)
    if begin_idx == -1:
        # No existing block: APPEND, never rewrite the file wholesale. A
        # blank separating line is added before the block for readability —
        # new bytes only, nothing existing is touched — and this path only
        # ever runs once per file, since every later render finds the marker
        # and takes the replace path below instead.
        if not raw:
            return block + newline
        prefix = raw if raw.endswith(newline) else raw + newline
        return prefix + newline + block + newline

    end_idx = raw.find(end_bytes, begin_idx)
    if end_idx == -1:
        raise MalformedBlockError(
            f"found {BEGIN_MARKER!r} at byte offset {begin_idx} with no matching "
            f"{END_MARKER!r} after it — refusing to guess where the managed block ends"
        )
    end_of_end = end_idx + len(end_bytes)
    return raw[:begin_idx] + block + raw[end_of_end:]


def render_into(path: PathLike, content: str, *, write: bool = False) -> InjectResult:
    """Splice `content` into one target. Dry-run unless `write=True`.

    Missing target is a no-op with a reason, never an error and never a
    created file — this module never authors somebody's `CLAUDE.md`; it only
    ever edits one that is already there.
    """
    path = Path(path)
    path_str = str(path)

    if not path.exists():
        return InjectResult(
            path=path_str,
            status="skipped",
            reason="target does not exist — this module never creates a file, only edits one",
        )

    raw = path.read_bytes()
    try:
        new_raw = _splice(raw, content)
    except MalformedBlockError as exc:
        return InjectResult(path=path_str, status="error", reason=str(exc))

    if new_raw == raw:
        # No timestamp lives in the rendered content (see render_context_block),
        # so unchanged input reaching here means a byte-identical re-render —
        # the idempotency this module promises. Skipping the write entirely
        # (rather than writing identical bytes) means a scheduled refresh
        # against unchanged state never even touches the file's mtime.
        return InjectResult(path=path_str, status="unchanged", reason="content already matches — nothing to do")

    # Decoding is ONLY for this human-readable diff, never for the splice
    # above — `new_raw` was already computed on raw bytes. A target whose
    # bytes are not valid UTF-8 can still be spliced correctly, but its diff
    # cannot be rendered as text, and that failure is deliberately NOT caught
    # here: it propagates to the per-target catch in `run()`, which is where
    # the equivalent failure escaped a too-narrow `except OSError` before.
    diff = "\n".join(
        difflib.unified_diff(
            raw.decode("utf-8").splitlines(),
            new_raw.decode("utf-8").splitlines(),
            fromfile=f"{path_str} (before)",
            tofile=f"{path_str} (after)",
            lineterm="",
        )
    )

    if not write:
        return InjectResult(path=path_str, status="would_write", reason="dry run — pass write=True to apply", diff=diff)

    path.write_bytes(new_raw)
    return InjectResult(path=path_str, status="written", reason="marker block updated", diff=diff)


def run(targets: Sequence[PathLike], content: str, *, write: bool = False) -> list[InjectResult]:
    """Render into every target, isolating each one's failure from the rest.

    `except Exception`, not `except OSError` — seed decode-error trap in the
    module docstring. Whatever goes wrong for one target, and whatever type
    it is, the exception type is always recorded in the result so a blanket
    catch here can never quietly become a silent one, and the next target is
    still attempted regardless of where in the list the bad one sat.
    """
    results: list[InjectResult] = []
    for target in targets:
        try:
            results.append(render_into(target, content, write=write))
        except Exception as exc:  # noqa: BLE001 - deliberately broad; see module docstring
            results.append(
                InjectResult(
                    path=str(target),
                    status="error",
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )
    return results


def render_repo_block(
    live_leases: list[dict],
    *,
    repo: str,
    max_bytes: int = DEFAULT_MAX_BLOCK_BYTES,
) -> str:
    """Who is working where in THIS REPO — the shared, tier-1 content.

    **Repo-scoped, never actor-scoped, and that is a correctness property rather
    than a style choice.** The target of a tier-1 splice is a file every person
    in the repo reads and most repos commit. Writing one person's state into it
    would mean everyone else reads a stranger's context, the identity of
    whichever machine happens to run the scheduled job gets stamped into a
    shared file, and — the part that actually matters — what one person
    snapshotted and where they are working leaks into version control for
    everyone, permanently.

    Live scope claims are the one thing that genuinely belongs here, because
    they are already meant to be public: the whole value of the registry is
    making concurrency visible, and this is the cheapest surface that does it.
    Someone reading their repo's context file learns that a colleague is in the
    directory they were about to edit, which is the entire feature.

    **Divergence is deliberately NOT here.** A snapshot belongs to whoever took
    it, so its digest is personal. That goes to the individual through a session
    hook (`render_session_block`) or a direct digest — never into a file the
    whole team reads.

    No timestamp, so a scheduled run against unchanged state is a byte-identical
    no-op rather than a diff in every review.
    """
    lines = [
        "AgentCo — auto-generated. The next scheduled refresh overwrites this "
        "block, so hand edits here do not stick.",
        "",
        f"Live scope claims in {repo}: {len(live_leases)}",
    ]
    if live_leases:
        for lease in sorted(live_leases, key=lambda l: (l["holder"], l["prefixes"])):
            paths = ", ".join(lease["prefixes"])
            attested = " (attested, unverified)" if lease.get("holderAttested") else ""
            lines.append(f"  - {lease['holder']}{attested}: {paths} [{lease['intent']}]")
        lines.append("")
        lines.append(
            "Advisory only — nothing here blocks you. It exists so a collision is a "
            "conversation before the merge rather than after it."
        )
    else:
        lines.append("  (nobody else is holding a claim here right now)")

    # DISCOVERY, and the reason L1 is reachable at all. An agent that has
    # configured nothing reads this file and otherwise has no way to learn that
    # a write path exists — a floor nobody can find is not a floor. Static
    # text, deliberately: it carries no state, so a scheduled re-render stays
    # byte-identical, and it is the same four lines in every repo.
    #
    # Only the INSTRUCTIONS go here. What came of a push is local to this
    # checkout and belongs in the per-session block, for the same reason
    # divergence does — this file is committed, and one machine's receipts are
    # not the team's business.
    lines.append("")
    lines.append("To publish from a harness with no AgentCo configuration, append one JSON")
    lines.append("object per line to `.agentco/outbox.jsonl` — a local drainer signs and sends it:")
    lines.append(
        '  {"line_id":"<unique>","at":"<iso8601>","verb":"claim_scope",'
        '"payload":{"repo":"...","prefixes":["dir/sub"],"intent":"implement"},'
        '"agent_label":"<harness name>"}'
    )
    lines.append(
        "Verbs: claim_scope, release_scope, snapshot, work_report. Never set `actor` — "
        "the drainer signs. See docs/outbox.md."
    )

    return _cap(lines, max_bytes)


def render_session_block(
    digest: dict,
    conflicts: list[dict],
    *,
    actor: str,
    receipts: Sequence[dict] = (),
    max_bytes: int = DEFAULT_MAX_BLOCK_BYTES,
) -> str:
    """One person's state — for a per-session hook, NOT for a shared repo file.

    Kept as a separate function from `render_repo_block` so the two audiences
    cannot be conflated by accident. Everything here is about `actor`
    specifically: the pointers they snapshotted, and the collisions against
    their own claims. Splicing this into a committed file would publish it to
    the whole team.

    `receipts` is what came of this machine's outbox pushes, and it is the
    answer to the one question an L1 publisher cannot otherwise ask. An outbox
    write is fire-and-forget: the agent appends a line, exits, and has no way to
    learn whether it arrived. Without this, the L1 experience is "I pushed and
    nothing happened", which this project names as the most adoption-lethal
    outcome available — so a refusal is shown with its remediation, and silence
    is shown as silence.

    Only the failures are listed. A published line needs no report; the whole
    reason to spend context here is the line that did NOT land.
    """
    lines = [
        f"AgentCo — for {actor}.",
        "",
        f"Scope conflicts on your live claims: {len(conflicts)}",
    ]
    if conflicts:
        for c in conflicts:
            lines.append(
                f"  - {c['repo']}: vs {c['withHolder']} ({c['theirIntent']}), you: {c['myIntent']}"
            )
    else:
        lines.append("  (none)")

    moved = digest.get("moved", [])
    tracked = digest.get("tracked", 0)
    lines.append("")
    lines.append(f"Divergence: {len(moved)} of {tracked} tracked pointer(s) moved")
    if moved:
        for item in moved:
            lines.append(f"  - {item['purpose']}: {item['artifactUri']}")
    else:
        lines.append("  (no tracked pointer changed since the last check)")

    if receipts:
        unhappy = [r for r in receipts if r.get("state") != "published"]
        lines.append("")
        lines.append(
            f"Outbox: {len(receipts) - len(unhappy)} published, {len(unhappy)} not"
        )
        for receipt in unhappy:
            detail = receipt.get("remediation") or receipt.get("detail") or ""
            verb = receipt.get("verb") or "unparseable line"
            lines.append(f"  - {receipt.get('state')}: {verb} — {detail}")

    return _cap(lines, max_bytes)


def _cap(lines: list[str], max_bytes: int) -> str:
    """Truncate by BYTES, and always announce it with a count.

    An omitted item nobody is told about reads as "nothing else happened"
    rather than "there was more", which is the one silent failure a formatter
    like this must never produce. Byte-capped rather than item-capped because
    the cost being controlled is context, and context is bytes.
    """
    kept: list[str] = []
    used = 0
    remaining = list(lines)
    while remaining:
        cost = len(remaining[0].encode("utf-8")) + 1
        if used + cost > max_bytes:
            break
        kept.append(remaining.pop(0))
        used += cost
    if remaining:
        kept.append(
            f"  … {len(remaining)} more line(s) omitted to stay within the context budget"
        )
    return "\n".join(kept)
