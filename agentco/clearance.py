"""Clearance per data owner: what a hub and an actor may see of shared memory.

ADR 0006 §2. Every memory item carries an owner label `(owner, level)`, such
as `("acme", "internal")`. A clearance is a grant per OWNER — `{"acme":
"internal", "personal": "private"}` — and it opens an item only when the item's
owner is in it and the item's level is at or below the granted one. Levels
compare only within one owner: `("acme", "internal")` says nothing about
`globex`.

Two clearances apply and both must say yes:

  * **the hub's** (`AGENTCO_HUB_CLEARANCE`), which bounds what this hub may
    hold and retrieve at all;
  * **the actor's** (the key table's object form, `"clearance": {...}`, or
    `create_app(clearances=...)`), which bounds what one actor may open.

`effective()` is their per-owner minimum, which is the "narrowing only" rule:
combining clearances can lose access, never gain it. **Unstated is not
cleared.** A hub with no declared clearance holds nothing but `public`; an
actor with none reads nothing but `public`. Fail closed, for the reason ADR
0005 made the federation allowlist fail closed.

`public` is the one owner every clearance includes, at every level: an item
labeled `(public, ...)` is the library everyone may read (ADR 0006 §1).

This is a GATE, applied in code before any ranking or search, never a weight
(§2). Nothing here asks a model to respect a label.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

from agentco.errors import Refusal

PUBLIC = "public"

LEVELS_ENV_VAR = "AGENTCO_MEMORY_LEVELS"
HUB_CLEARANCE_ENV_VAR = "AGENTCO_HUB_CLEARANCE"

#: Low to high. NOT a finding: the ADR names levels by example only
#: (`internal`, `private`, `all`) and gives no order; this one is a choice, and
#: a deployment sets its own with `AGENTCO_MEMORY_LEVELS`.
DEFAULT_LEVELS: tuple[str, ...] = ("all", "internal", "confidential", "private")


def levels_from_env() -> tuple[str, ...]:
    raw = os.environ.get(LEVELS_ENV_VAR, "")
    levels = tuple(p.strip() for p in raw.split(",") if p.strip())
    return levels or DEFAULT_LEVELS


@dataclass(frozen=True)
class Clearance:
    """A grant per owner, over one ordered set of levels."""

    grants: Mapping[str, str] = field(default_factory=dict)
    levels: tuple[str, ...] = DEFAULT_LEVELS

    def __post_init__(self) -> None:
        for owner, level in self.grants.items():
            if level not in self.levels:
                raise ValueError(
                    f"clearance for {owner!r} names level {level!r}, which is not one of "
                    f"{list(self.levels)}"
                )

    def rank(self, level: str) -> int:
        try:
            return self.levels.index(level)
        except ValueError:
            raise ValueError(f"unknown level {level!r}; the levels are {list(self.levels)}") from None

    def allows(self, owner: str, level: str) -> bool:
        if owner == PUBLIC:
            return level in self.levels
        granted = self.grants.get(owner)
        if granted is None or level not in self.levels:
            return False
        return self.rank(level) <= self.rank(granted)

    def narrow(self, other: "Clearance") -> "Clearance":
        """The per-owner minimum. An owner only one side grants is not granted."""
        out = {}
        for owner in set(self.grants) & set(other.grants):
            a, b = self.grants[owner], other.grants[owner]
            out[owner] = a if self.rank(a) <= self.rank(b) else b
        return Clearance(out, self.levels)

    def as_dict(self) -> dict:
        return {PUBLIC: self.levels[-1], **dict(sorted(self.grants.items()))}


def parse(raw: object, levels: tuple[str, ...]) -> Clearance:
    """`{"acme": "internal"}`, or the env form `acme:internal,personal:private`."""
    if raw is None or raw == "":
        return Clearance({}, levels)
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("{"):
            raw = json.loads(stripped)
        else:
            pairs = {}
            for part in stripped.split(","):
                if not part.strip():
                    continue
                owner, sep, level = part.partition(":")
                if not sep or not owner.strip() or not level.strip():
                    raise ValueError(f"clearance entry {part!r} is not owner:level")
                pairs[owner.strip()] = level.strip()
            raw = pairs
    if not isinstance(raw, Mapping):
        raise ValueError("a clearance is a mapping of owner to level")
    return Clearance({str(k).strip(): str(v).strip() for k, v in raw.items()}, levels)


def hub_from_env(levels: Optional[tuple[str, ...]] = None) -> Clearance:
    levels = levels or levels_from_env()
    return parse(os.environ.get(HUB_CLEARANCE_ENV_VAR), levels)


def actors_from_key_table(levels: tuple[str, ...]) -> dict[str, Clearance]:
    """Each actor's clearance from the key table's object form.

    Read separately from `auth.load_identities` on purpose: the signing path
    must not start refusing a key table because a clearance in it is
    malformed. A malformed clearance here clears that actor for nothing.
    """
    from agentco import auth

    target = os.environ.get(auth.KEYS_ENV_VAR)
    if not target:
        return {}
    try:
        data = json.loads(open(target, encoding="utf-8").read())
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, Clearance] = {}
    for actor, value in (data.items() if isinstance(data, dict) else ()):
        if isinstance(value, dict) and value.get("clearance") is not None:
            try:
                out[str(actor)] = parse(value["clearance"], levels)
            except ValueError:
                out[str(actor)] = Clearance({}, levels)
    return out


class Clearances:
    """The hub's clearance and every actor's, answering one question: what may
    THIS actor see here. Actor clearances are a callable so an edit to the key
    table applies on the next request, as revocation does (auth.py)."""

    def __init__(self, hub: Clearance, actors: Optional[Mapping[str, Clearance]] = None,
                 *, actors_source=None):
        self.hub = hub
        self.levels = hub.levels
        self._static = dict(actors or {})
        self._source = actors_source

    def actor(self, actor: str) -> Clearance:
        if self._source is not None:
            found = self._source().get(actor)
            if found is not None:
                return found
        return self._static.get(actor, Clearance({}, self.levels))

    def effective(self, actor: str) -> Clearance:
        return self.hub.narrow(self.actor(actor))


def build(hub: Optional[Mapping[str, str]] = None,
          actors: Optional[Mapping[str, Mapping[str, str]]] = None,
          levels: Optional[Iterable[str]] = None) -> Clearances:
    """For `create_app`: injected values win, the environment fills the rest."""
    lv = tuple(levels) if levels is not None else levels_from_env()
    hub_c = parse(dict(hub), lv) if hub is not None else hub_from_env(lv)
    if actors is not None:
        return Clearances(hub_c, {a: parse(dict(g), lv) for a, g in actors.items()})
    return Clearances(hub_c, actors_source=lambda: actors_from_key_table(lv))


def not_cleared(owner: str, level: str) -> Refusal:
    """For WRITES only, where the caller named the label and learns nothing by
    being told. A read never says this — see `memory.unknown_item`."""
    return Refusal(
        code="clearance_insufficient",
        message=f"not cleared to write an item labeled ({owner}, {level}) here",
        remediation=(
            "An item can be written only at a label both this hub and your identity "
            "are cleared for. Ask the operator, or write it at a label you hold."
        ),
        http_status=403,
    )
