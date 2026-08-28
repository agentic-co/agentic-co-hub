"""The `ScopeLease` scope model — the scope-model decision (docs/decisions/0001), specified before the POST was written.

The rationale is recorded in docs/decisions/0001-scope-model.md. The candidate design shipped
`ScopeLease` as its headline stage-1 feature with `scope` UNDEFINED, while
stating elsewhere that path globs are "a filter, not a partition, because path
globs do not partition cleanly". That is a statement that the natural scope
representation does not compose — and a lease registry's entire value IS
composition: does my claim intersect yours?

Ten devs each holding a lease on `src/` is the DEFAULT outcome, not the edge
case. Every claim intersects every other, conflicts fire constantly, and
within four days everyone learns the registry is noise. So:

  * Scope is `(repo, path-prefix set)` at DIRECTORY granularity. Prefixes,
    not globs.
  * A prefix must name at least `MIN_SEGMENTS` path segments below the repo
    root. A lease on `src/` or on the repo root is REFUSED, with the
    remediation naming the requirement (`errors.scope_too_broad`).
  * Intersection is prefix-overlap computed as a SET OPERATION on path
    segments, never a glob match.

`MIN_SEGMENTS = 2` initially, "tuned once" per the scope-model decision (docs/decisions/0001). It is tuned from the
registry's own published precision (see `metrics.conflict_precision`) — if
precision sits below the floor, the granularity rule is wrong and the fix is
`k`, NOT more leases. That is why the constant lives here next to the
intersection logic rather than in a config file: the two are one decision.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from agentco.errors import Refusal, scope_too_broad

# the scope-model decision (docs/decisions/0001): "at least k path segments below the repo root (k = 2 initially,
# tuned once)". Tuning input is `metrics.conflict_precision`, not taste.
MIN_SEGMENTS = 2

# the scope-model decision (docs/decisions/0001): ScopeConflict "carries both intents so 'prototype vs implement'
# reads differently from 'implement vs implement'". The set is closed so a
# typo becomes a refusal rather than a third intent nobody queries for.
INTENTS = ("prototype", "implement", "review", "refactor")


def normalize_prefix(raw: str) -> str:
    """Canonical form for one path prefix: no leading/trailing slash, no dot segments.

    Normalising BEFORE validation and before storage is what makes the
    intersection a pure set operation later — `src/budget/`, `./src/budget`
    and `src/budget` must not be able to produce three non-intersecting rows
    for the same directory. `posixpath.normpath` is used (not `os.path`)
    because these are repo-relative POSIX paths in a payload, never local
    filesystem paths, and must behave identically on the Windows VM.
    """
    cleaned = (raw or "").strip().replace("\\", "/")
    cleaned = cleaned.strip("/")
    if not cleaned:
        return ""
    normalized = posixpath.normpath(cleaned)
    if normalized in (".", "/"):
        return ""
    return normalized.strip("/")


def segments(prefix: str) -> tuple[str, ...]:
    return tuple(s for s in normalize_prefix(prefix).split("/") if s)


def validate_prefix(raw: str, min_segments: int = MIN_SEGMENTS) -> str:
    """Return the canonical prefix, or raise the the scope-model decision (docs/decisions/0001) refusal.

    Escape attempts (`../`) are refused rather than normalised away: a prefix
    that climbs out of the repo root is not a scope, and silently clamping it
    to something inside the repo would record a lease over a directory the
    caller never named.
    """
    reject_control_characters("path prefix", raw or "")
    if ".." in (raw or "").replace("\\", "/").split("/"):
        raise Refusal(
            code="scope_escapes_repo",
            message=f"path prefix {raw!r} contains a '..' segment",
            remediation=(
                "Claim a prefix relative to the repo root, with no parent-directory "
                "segments — e.g. 'src/budget/grid'."
            ),
        )
    prefix = normalize_prefix(raw)
    if len(segments(prefix)) < min_segments:
        raise scope_too_broad(raw, min_segments)
    return prefix


def validate_intent(raw: str) -> str:
    intent = (raw or "").strip().lower()
    if intent not in INTENTS:
        raise Refusal(
            code="unknown_intent",
            message=f"intent {raw!r} is not one of {', '.join(INTENTS)}",
            remediation=(
                f"Declare one of: {', '.join(INTENTS)}. The intent pair is what makes a "
                f"conflict readable — 'prototype vs implement' is usually fine, "
                f"'implement vs implement' usually is not."
            ),
        )
    return intent


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def reject_control_characters(field: str, value: str) -> str:
    """Refuse a control character in caller-supplied text. Never strip it.

    An embedded newline in a `holder` or a path prefix is not a formatting
    nuisance — these values are interpolated into a managed block that is
    spliced into a repository's agent-context file, so a newline plus an END
    marker escapes the block and writes permanent content into a file the whole
    team commits and every teammate's agent reads.

    Refusing rather than stripping, for the same reason `keys.normalize_component`
    refuses: a silently repaired value is a value the caller did not send and
    cannot see, and the repair hides the attempt. Stripping would also make the
    injected text vanish without anyone learning somebody tried.
    """
    if _CONTROL_CHARS.search(value):
        raise Refusal(
            code="control_character",
            message=f"{field} contains a control character ({value!r})",
            remediation=(
                f"Remove it. {field} is rendered into a shared agent-context file, "
                f"so an embedded newline can break out of the managed block and "
                f"write permanent content other people read. Refused rather than "
                f"stripped, because a silently repaired value is one you did not send."
            ),
        )
    return value


def validate_repo(raw: str) -> str:
    repo = reject_control_characters("repo", raw or "").strip()
    if not repo:
        raise Refusal(
            code="repo_required",
            message="a scope claim must name the repo it applies to",
            remediation="Include 'repo' — e.g. 'acme/web-platform'.",
        )
    return repo


@dataclass(frozen=True)
class Scope:
    """`(repo, path-prefix set)` — the whole scope model, validated on construction."""

    repo: str
    prefixes: tuple[str, ...]

    @classmethod
    def parse(
        cls,
        repo: str,
        prefixes: Sequence[str],
        min_segments: int = MIN_SEGMENTS,
    ) -> "Scope":
        if not prefixes:
            raise Refusal(
                code="scope_required",
                message="a scope claim must name at least one path prefix",
                remediation=(
                    "Include 'prefixes' with at least one directory path at least "
                    f"{min_segments} segments deep — e.g. ['src/budget/grid']."
                ),
            )
        seen: list[str] = []
        for raw in prefixes:
            prefix = validate_prefix(raw, min_segments)
            if prefix not in seen:
                seen.append(prefix)
        return cls(repo=validate_repo(repo), prefixes=tuple(seen))


def prefixes_overlap(a: str, b: str) -> bool:
    """True iff two canonical prefixes intersect — a pure segment-set operation.

    Two directory prefixes intersect exactly when one is a segment-wise
    prefix of the other: `src/budget` contains `src/budget/grid`, so a lease
    on either covers work in the latter. `src/budget` and `src/budgeting` do
    NOT intersect — which is precisely the case a naive `startswith` on the
    raw strings gets wrong, and the reason this compares tuples of segments
    rather than characters.
    """
    sa, sb = segments(a), segments(b)
    shorter, longer = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    return longer[: len(shorter)] == shorter


def scopes_intersect(a: Scope, b: Scope) -> tuple[tuple[str, str], ...]:
    """Every intersecting prefix pair between two scopes. Empty tuple = disjoint.

    Returns the PAIRS rather than a bool because a conflict is only useful if
    it names which directories collided — "your claim overlaps mine" with no
    path is the shape of a notification people mute.
    """
    if a.repo != b.repo:
        return ()
    return tuple(
        (pa, pb) for pa in a.prefixes for pb in b.prefixes if prefixes_overlap(pa, pb)
    )


def find_conflicts(
    candidate: Scope,
    holder: str,
    live: Iterable[tuple[str, Scope, str]],
) -> list[dict]:
    """Conflicts between `candidate` and each live lease, per the scope-model decision (docs/decisions/0001).

    `live` yields `(holder, scope, intent)` for leases that have not expired
    or been released. A conflict fires ONLY between two DIFFERENT holders —
    renewing or widening your own lease is not a conflict, and treating it as
    one is the fastest way to train people to ignore the field.

    The candidate's own intent is supplied by the caller rather than read off
    `candidate` because `Scope` is `(repo, prefixes)` only; intent belongs to
    the lease, not to the scope, and keeping them separate is what lets a
    holder hold one scope under two intents over time.
    """
    conflicts: list[dict] = []
    for other_holder, other_scope, other_intent in live:
        if other_holder == holder:
            continue
        pairs = scopes_intersect(candidate, other_scope)
        if not pairs:
            continue
        conflicts.append(
            {
                "withHolder": other_holder,
                "overlaps": [{"mine": mine, "theirs": theirs} for mine, theirs in pairs],
                "theirIntent": other_intent,
            }
        )
    return conflicts
