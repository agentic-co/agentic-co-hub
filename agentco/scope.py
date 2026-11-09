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

**Two spellings of one place must meet.** Repo names are case-insensitive on
GitHub and Azure DevOps; directory names are case-insensitive on default macOS
and Windows checkouts, and Windows also drops a trailing dot or space from a
path component; macOS hands back decomposed (NFD) Unicode. A comparison that is
stricter than the platforms lets two people hold one directory and be told they
are alone. So comparison goes through `repo_key` / `prefix_key` (NFC, case-folded,
trailing dots and spaces dropped per segment), while the caller's own spelling is
what is stored and echoed. Invisible characters are REFUSED rather than stripped:
a repaired value is one the caller did not send. Case folding errs toward a false
conflict on a case-sensitive filesystem, which costs a conversation; a missed one
costs lost work.
"""

from __future__ import annotations

import posixpath
import re
import unicodedata
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


def _fold(text: str) -> str:
    """NFC, case-folded, NFC again (folding can denormalise). Total: never raises."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())


def repo_key(repo: str) -> str:
    """What two spellings of one repository have in common. Comparison and lock key only.

    Never stored in place of the caller's spelling and never shown. Total on any
    string, because it also runs over rows written before these rules existed.
    """
    return _fold((repo or "").strip())


def prefix_key(prefix: str) -> tuple[str, ...]:
    """Comparison key for a prefix: per-segment fold, minus trailing dots and spaces.

    A dot INSIDE a name survives (`v1.2` is not `v1`); only the trailing run that
    Windows ignores is dropped. Total on any string, for legacy rows.
    """
    return tuple(_fold(seg).rstrip(". ") for seg in segments(prefix))


def reject_invisible_characters(field: str, value: str) -> str:
    """Refuse format and C1 control characters (ZWSP, ZWJ, BOM, bidi marks, soft hyphen...).

    They render as nothing, so two scopes that look identical in a terminal, a
    review and this registry's own output can still be different strings. Refused,
    never stripped, for the reason `reject_control_characters` gives. Legitimate
    ZWJ/ZWNJ in some scripts' directory names is the cost; the remediation says so.
    """
    for ch in value:
        if unicodedata.category(ch) in ("Cf", "Cc", "Zl", "Zp"):
            raise Refusal(
                code="control_character",
                message=f"{field} contains an invisible character (U+{ord(ch):04X}) in {value!r}",
                remediation=(
                    f"Remove it and retype {field}. Zero-width and byte-order-mark "
                    f"characters look like nothing but make two identical-looking "
                    f"scopes differ, so the registry refuses them instead of "
                    f"silently rewriting what you claimed."
                ),
            )
    return value


def validate_prefix(raw: str, min_segments: int = MIN_SEGMENTS) -> str:
    """Return the canonical prefix, or raise the the scope-model decision (docs/decisions/0001) refusal.

    Escape attempts (`../`) are refused rather than normalised away: a prefix
    that climbs out of the repo root is not a scope, and silently clamping it
    to something inside the repo would record a lease over a directory the
    caller never named.
    """
    reject_control_characters("path prefix", raw or "")
    reject_invisible_characters("path prefix", raw or "")
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
    if any(not key for key in prefix_key(prefix)):
        raise Refusal(
            code="bad_path_segment",
            message=f"path prefix {raw!r} has a segment made only of dots or spaces",
            remediation=(
                "Name real directories — e.g. 'src/budget/grid'. A segment of only "
                "dots or spaces is ignored by Windows, so it cannot name anything "
                "the registry could compare."
            ),
        )
    if len(segments(prefix)) < min_segments:
        raise scope_too_broad(raw, min_segments)
    return prefix


def validate_intent(raw: str) -> str:
    if raw is not None and not isinstance(raw, str):
        from agentco import payload as pl

        raise pl.refuse("intent", "a string", raw)
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
    repo = reject_control_characters("repo", raw or "")
    repo = reject_invisible_characters("repo", repo).strip()
    if not repo:
        raise Refusal(
            code="repo_required",
            message="a scope claim must name the repo it applies to",
            remediation="Include 'repo' — e.g. 'acme/web-platform'.",
        )
    return repo


def _canonical(repo: str, prefixes: Sequence[str], min_segments: int) -> tuple[str, tuple[str, ...]]:
    """Validate and canonicalise `(repo, prefixes)`; keeps the caller's spelling.

    Duplicates are dropped by comparison key, first spelling wins, so
    `src/Budget/x` and `SRC/budget/x/` are one prefix, not two rows that conflict
    with themselves.
    """
    if not prefixes:
        raise Refusal(
            code="scope_required",
            message="a scope claim must name at least one path prefix",
            remediation=(
                "Include 'prefixes' with at least one directory path at least "
                f"{min_segments} segments deep — e.g. ['src/budget/grid']."
            ),
        )
    from agentco import payload as pl

    pl.str_list_value("prefixes", prefixes)
    pl.str_value("repo", repo)
    seen: list[str] = []
    keys: set[tuple[str, ...]] = set()
    for raw in prefixes:
        prefix = validate_prefix(raw, min_segments)
        key = prefix_key(prefix)
        if key not in keys:
            keys.add(key)
            seen.append(prefix)
    return validate_repo(repo), tuple(seen)


@dataclass(frozen=True)
class Scope:
    """`(repo, path-prefix set)` — the whole scope model, validated on construction.

    `Scope(...)` and `Scope.parse(...)` apply the same rules, so there is no
    unvalidated door. Canonical input is returned unchanged. Rows already in the
    registry may predate a rule: `from_stored` skips validation for those, and
    ONLY for those, because refusing to read a lease someone holds would hide it.
    """

    repo: str
    prefixes: tuple[str, ...]

    def __post_init__(self) -> None:
        repo, prefixes = _canonical(self.repo, tuple(self.prefixes), MIN_SEGMENTS)
        object.__setattr__(self, "repo", repo)
        object.__setattr__(self, "prefixes", prefixes)

    @classmethod
    def parse(
        cls,
        repo: str,
        prefixes: Sequence[str],
        min_segments: int = MIN_SEGMENTS,
    ) -> "Scope":
        clean_repo, clean = _canonical(repo, prefixes, min_segments)
        return cls._unchecked(clean_repo, clean)

    @classmethod
    def from_stored(cls, repo: str, prefixes: Sequence[str]) -> "Scope":
        """A scope read back from the registry, exactly as written. Not for caller input."""
        return cls._unchecked(repo, tuple(prefixes))

    @classmethod
    def _unchecked(cls, repo: str, prefixes: tuple[str, ...]) -> "Scope":
        scope = object.__new__(cls)
        object.__setattr__(scope, "repo", repo)
        object.__setattr__(scope, "prefixes", prefixes)
        return scope


def prefixes_overlap(a: str, b: str) -> bool:
    """True iff two canonical prefixes intersect — a pure segment-set operation.

    Two directory prefixes intersect exactly when one is a segment-wise
    prefix of the other: `src/budget` contains `src/budget/grid`, so a lease
    on either covers work in the latter. `src/budget` and `src/budgeting` do
    NOT intersect — which is precisely the case a naive `startswith` on the
    raw strings gets wrong, and the reason this compares tuples of segments
    rather than characters.
    """
    sa, sb = prefix_key(a), prefix_key(b)
    shorter, longer = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    return longer[: len(shorter)] == shorter


def scopes_intersect(a: Scope, b: Scope) -> tuple[tuple[str, str], ...]:
    """Every intersecting prefix pair between two scopes. Empty tuple = disjoint.

    Returns the PAIRS rather than a bool because a conflict is only useful if
    it names which directories collided — "your claim overlaps mine" with no
    path is the shape of a notification people mute.
    """
    if repo_key(a.repo) != repo_key(b.repo):
        return ()
    return tuple(
        (pa, pb) for pa in a.prefixes for pb in b.prefixes if prefixes_overlap(pa, pb)
    )


def find_conflicts(
    candidate: Scope,
    holder: str,
    live: Iterable[tuple[str, Scope, str, bool]],
) -> list[dict]:
    """Conflicts between `candidate` and each live lease, per the scope-model decision (docs/decisions/0001).

    `live` yields `(holder, scope, intent, holder_attested)` for leases that have not expired
    or been released. A conflict fires ONLY between two DIFFERENT holders —
    renewing or widening your own lease is not a conflict, and treating it as
    one is the fastest way to train people to ignore the field.

    The candidate's own intent is supplied by the caller rather than read off
    `candidate` because `Scope` is `(repo, prefixes)` only; intent belongs to
    the lease, not to the scope, and keeping them separate is what lets a
    holder hold one scope under two intents over time.

    Each record carries `holderAttested`: True when the OTHER lease's holder was
    named in a payload and differs from the actor who filed it, so a reader can
    weigh a conflict raised by an unverified claim (docs/wire.md). It is
    additive: the conflict still fires.
    """
    conflicts: list[dict] = []
    for other_holder, other_scope, other_intent, other_attested in live:
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
                "holderAttested": bool(other_attested),
            }
        )
    return conflicts
