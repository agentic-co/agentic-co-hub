"""Which procedure a piece of incoming work triggers — as data, not as code.

Work arriving from a system of record is not all the same job. A tool that has
been built and published to beta needs *testing*; one that has not been built
needs *development*; an item asking what is missing needs a *gap analysis*.
Handing all three the same procedure is how an SOP becomes something people
skip, because it is right for a third of the work it is attached to.

The rules live in a file rather than in this module for the same reason the org
URL does: which words mean "test this" is a fact about one organisation's
backlog, not about coordination. A team that writes "Ready for QA" where
another writes "In Beta" changes a config line, not a release.

**First match wins, and the order is the file's order.** Not "most specific
wins" — that requires a specificity metric everyone would then argue about, and
a rule that quietly lost to another is far harder to see than one that is
simply listed second.

**An unmatched item takes the default and says so.** Refusing it would strand
work at the door over a missing rule; filing it silently under `development`
would hide the fact that the rules do not cover the backlog. It is filed, and
`explain()` reports it as a default hit so the gap is visible in the dry run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from agentco.errors import Refusal

# Every predicate a rule may use. Deliberately small and all string-shaped:
# a rule language that grows a boolean algebra becomes a program in a config
# file, which is a program nobody tests.
PREDICATES = ("title_contains", "title_matches_any", "type_in", "state_in")


@dataclass(frozen=True)
class Rule:
    when: dict
    sop: str

    def matches(self, item: dict) -> bool:
        title = (item.get("title") or "").lower()
        for predicate, expected in self.when.items():
            if predicate == "title_contains":
                if str(expected).lower() not in title:
                    return False
            elif predicate == "title_matches_any":
                if not any(str(word).lower() in title for word in expected):
                    return False
            elif predicate == "type_in":
                if item.get("type") not in expected:
                    return False
            elif predicate == "state_in":
                if item.get("state") not in expected:
                    return False
        return True


@dataclass(frozen=True)
class Routes:
    """One organisation's answer to 'who does this, and under what procedure'."""

    sops: dict[str, str]
    rules: tuple[Rule, ...] = ()
    default: str = ""
    assign: Optional[str] = None
    requires: tuple[str, ...] = ()

    def sop_for(self, item: dict) -> tuple[str, bool]:
        """Return `(sop key, matched a rule)`. The flag is what makes a gap visible."""
        for rule in self.rules:
            if rule.matches(item):
                return rule.sop, True
        return self.default, False

    def sop_id_for(self, item: dict) -> tuple[str, str, bool]:
        key, matched = self.sop_for(item)
        if key not in self.sops:
            raise Refusal(
                code="route_sop_unknown",
                message=f"route resolved to {key!r}, which is not in `sops`",
                remediation=(
                    f"Add {key!r} to the `sops` map with the id of an ACTIVE SOP, "
                    "or point the rule at one of: " + ", ".join(sorted(self.sops)) + "."
                ),
                http_status=400,
            )
        return key, self.sops[key], matched


def load(path: str | Path) -> Routes:
    """Read and validate a routes file. Every error names the fix."""
    try:
        raw = json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise Refusal(
            code="routes_missing",
            message=f"no routes file at {path}",
            remediation="Point --routes at a JSON file; see docs/ado-routing.md for the shape.",
            http_status=400,
        ) from None
    except json.JSONDecodeError as exc:
        raise Refusal(
            code="routes_bad_json",
            message=f"{path} is not valid JSON: {exc}",
            remediation="Fix the JSON. A routes file decides who does what — it is not guessed at.",
            http_status=400,
        ) from None

    sops = raw.get("sops") or {}
    default = raw.get("default") or ""
    if not sops:
        raise Refusal(
            code="routes_no_sops",
            message="the routes file declares no SOPs",
            remediation='Add "sops": {"development": "sop-…"} mapping each key to an ACTIVE SOP id.',
            http_status=400,
        )
    if default and default not in sops:
        raise Refusal(
            code="routes_bad_default",
            message=f"default {default!r} is not one of the declared sops",
            remediation="Set `default` to one of: " + ", ".join(sorted(sops)) + ".",
            http_status=400,
        )

    rules = []
    for index, entry in enumerate(raw.get("rules") or []):
        when = entry.get("when") or {}
        unknown = sorted(set(when) - set(PREDICATES))
        if unknown:
            # Loud, because a misspelled predicate is an empty `when`, and an
            # empty `when` matches EVERYTHING — so a typo in rule 1 silently
            # routes the entire backlog to one procedure.
            raise Refusal(
                code="routes_unknown_predicate",
                message=f"rule {index} uses unknown predicate(s): {', '.join(unknown)}",
                remediation="Use one of: " + ", ".join(PREDICATES) + ".",
                http_status=400,
            )
        if not when:
            raise Refusal(
                code="routes_empty_rule",
                message=f"rule {index} has an empty `when`, which matches every item",
                remediation="Give the rule a condition, or express it as `default` instead.",
                http_status=400,
            )
        rules.append(Rule(when=when, sop=entry["sop"]))

    return Routes(
        sops=sops,
        rules=tuple(rules),
        default=default,
        assign=raw.get("assign"),
        requires=tuple(raw.get("requires") or ()),
    )


def explain(routes: Routes, items: Iterable[dict]) -> list[dict]:
    """What each item would be routed to, and whether a rule or the default did it.

    This is what `--dry-run` prints. Seeing the routing before it is applied is
    the difference between a rules file you trust and one you hope about.
    """
    out = []
    for item in items:
        key, _sop_id, matched = routes.sop_id_for(item)
        out.append({**item, "sop": key, "matchedRule": matched})
    return out
