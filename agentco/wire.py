"""Request bodies: which keys a route takes, and what happens to the rest.

Owner decision 2026-10-18 (the unknown-key policy). Before it, every route read
the keys it knew and silently ignored the rest with a 200 — the class behind the
live-proof defects D1, D6 and D7, where a field a store method accepted was
dropped on the wire. The worst case was a typo: `verfiy` on `POST /work` filed
an UNGATED item.

Every route now declares one of two policies (`app.ROUTE_BODIES`, pinned by a
test so a new route without one fails CI):

  * **strict** — routes where a dropped key can silently remove a safety
    property: a gate, a fence, an idempotency key, a capability, a blocker, an
    approved subset, an identity. An unknown key is refused **422
    `unknown_field`** before the handler runs.
  * **lenient** — every other route. The request is served, and the response
    carries `ignored: [...]`; each ignored key is logged and counted
    (`GET /metrics` → `ignoredFields`).

Either way the message names the closest valid key and, from the bundled
provenance table (`agentco/field_provenance.py`, generated from the released
contracts; never the network), whether a released asop-spec has the key and
whether this hub's installed one is older than that release.
"""

from __future__ import annotations

import difflib
import importlib.metadata
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

from agentco import field_provenance
from agentco.errors import Refusal

#: Keys `_handle` itself owns on every route.
COMMON_KEYS = frozenset({"agentLabel"})


@dataclass(frozen=True)
class Body:
    strict: bool
    keys: frozenset = field(default_factory=frozenset)
    why: str = ""
    # Keys the HANDLER refuses by name with its own, more specific code (an
    # identity named in the body: `author`, `adopted_by`, ...). When one is
    # present the handler refuses the whole request anyway, so the generic
    # check steps aside and that refusal is the one the caller sees.
    refused_by_name: frozenset = field(default_factory=frozenset)


def strict(keys: Iterable[str], why: str, *, refused_by_name: Iterable[str] = ()) -> Body:
    named = frozenset(refused_by_name)
    return Body(True, frozenset(keys) | named, why, named)


def lenient(keys: Iterable[str] = (), why: str = "", *, refused_by_name: Iterable[str] = ()) -> Body:
    named = frozenset(refused_by_name)
    return Body(False, frozenset(keys) | named, why, named)


def _version_key(version: str) -> tuple:
    return tuple(int(p) if p.isdigit() else p for p in version.replace("-", ".").split("."))


def installed_spec() -> Optional[str]:
    try:
        return importlib.metadata.version("asop-spec")
    except importlib.metadata.PackageNotFoundError:
        return None


def installed_hub() -> str:
    try:
        return importlib.metadata.version("agentco")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def provenance_note(key: str, *, installed: Optional[str] = None,
                    table: Mapping[str, Mapping[str, str]] = field_provenance.FIELDS,
                    releases: Iterable[str] = field_provenance.RELEASES) -> str:
    """What the released contracts say about `key`, for the person reading the error."""
    installed = installed if installed is not None else installed_spec()
    found = sorted(((v, surface) for surface, names in table.items()
                    for name, v in names.items() if name == key), key=lambda p: _version_key(p[0]))
    if not found:
        return (f"{key!r} is unknown in every released asop-spec ({', '.join(releases)}) "
                f"and to this hub (agentco {installed_hub()})")
    since, surface = found[0]
    surfaces = sorted({s for _, s in found})
    if installed and _version_key(installed) < _version_key(since):
        return (f"{key!r} exists since asop-spec {since} ({'/'.join(surfaces)} field); this hub has "
                f"{installed} installed: upgrade to asop-spec>={since}")
    return (f"{key!r} is an asop-spec {'/'.join(surfaces)} field (since {since}), and this hub has "
            f"{installed or 'no'} asop-spec installed — but this route does not take it here")


def closest(key: str, keys: Iterable[str]) -> Optional[str]:
    match = difflib.get_close_matches(key, sorted(keys), n=1, cutoff=0.6)
    return match[0] if match else None


def describe(key: str, keys: Iterable[str]) -> str:
    near = closest(key, keys)
    return (f"{key!r}" + (f" (did you mean {near!r}?)" if near else "")
            + f" — {provenance_note(key)}")


class Ignored:
    """Lenient routes' ignored keys, counted per verb for `GET /metrics`.

    In memory, so it starts at zero when the process does; the call itself is
    in the `calls` table as always. Persisting it would need a migration this
    count does not yet justify."""

    def __init__(self) -> None:
        self.counts: Counter = Counter()

    def note(self, verb: str, keys: Iterable[str]) -> None:
        for key in keys:
            self.counts[(verb, key)] += 1

    def as_dict(self) -> dict:
        out: dict[str, dict[str, int]] = {}
        for (verb, key), n in sorted(self.counts.items()):
            out.setdefault(verb, {})[key] = n
        return out


def check(body: Body, payload: Mapping, *, verb: str, ignored: Ignored) -> list[str]:
    """Refuse (strict) or report (lenient) the keys `body` does not take."""
    allowed = body.keys | COMMON_KEYS
    unknown = sorted(k for k in payload if k not in allowed)
    if not unknown or body.refused_by_name & set(payload):
        return []
    detail = "; ".join(describe(k, allowed - COMMON_KEYS) for k in unknown)
    if body.strict:
        raise Refusal(
            code="unknown_field",
            message=f"this route does not take {detail}",
            remediation=(
                f"Remove or correct the key. This route refuses unknown keys because a dropped "
                f"one can silently remove a safety property ({body.why}). It takes: "
                f"{', '.join(sorted(allowed - COMMON_KEYS)) or 'no body'}."
            ),
            http_status=422,
        )
    ignored.note(verb, unknown)
    print(f"[wire] {verb}: ignored {unknown}: {detail}", file=sys.stderr)
    return unknown
