#!/usr/bin/env python3
"""Prove tokens.css and tokens.json still agree — and that the CSS agrees with itself.

Three ways this file set goes wrong, all silent, all caught here:

  * tokens.json drifts from tokens.css after someone edits one of them;
  * the `[data-theme="dark"]` override drifts from the `prefers-color-scheme`
    block, so a manual toggle renders different colours than the automatic
    one — the classic, and invisible until a user toggles;
  * a semantic token is declared in one ground and forgotten in the other.

Exit 0 means they agree. Exit 1 prints what differs.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
SEMANTIC = (
    "bg", "surface", "fg", "fg-muted", "rule",
    "verdict-pass", "verdict-fail", "verdict-pending",
    "link", "link-underline",
)

DECL = re.compile(r"--ac-([a-z0-9-]+)\s*:\s*([^;]+);")


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _block(css: str, opener: str) -> dict[str, str]:
    """Declarations of the first block introduced by `opener`.

    Brace-counted rather than regex-matched, because the dark block is an
    at-rule wrapping a rule and a lazy regex stops at the inner brace.
    """
    i = css.index(opener)
    depth, start = 0, None
    for j in range(i, len(css)):
        if css[j] == "{":
            depth += 1
            if start is None:
                start = j + 1
        elif css[j] == "}":
            depth -= 1
            if depth == 0:
                return {k: v.strip() for k, v in DECL.findall(css[start:j])}
    raise SystemExit(f"unbalanced braces after {opener!r}")


def _resolve(decls: dict[str, str]) -> dict[str, str]:
    """Flatten var() chains so #1B9E77 and var(--ac-attest) compare equal.

    Chains, not single hops: --ac-link is var(--ac-fg), which is itself
    var(--ac-ink). And each ground resolves against ITSELF — on the dark
    ground --ac-fg is redefined, so resolving link against the root map would
    quietly report the light colour.
    """
    out = dict(decls)
    for _ in range(8):  # depth guard; a cycle would otherwise spin forever
        changed = False
        for k, v in out.items():
            m = re.fullmatch(r"var\(\s*--ac-([a-z0-9-]+)\s*\)", v)
            if m and m.group(1) in out and out[m.group(1)] != v:
                out[k] = out[m.group(1)]
                changed = True
        if not changed:
            return out
    raise SystemExit("var() chain did not settle — cyclic token reference?")


def main() -> int:
    css = _strip_comments((HERE / "tokens.css").read_text())
    data = json.loads((HERE / "tokens.json").read_text())

    root = _block(css, ":root {")
    dark_media = _block(css, "@media (prefers-color-scheme: dark)")
    theme_light = _block(css, ':root[data-theme="light"]')
    theme_dark = _block(css, ':root[data-theme="dark"]')

    light = _resolve(root)
    dark = _resolve({**root, **dark_media})
    t_light = _resolve({**root, **theme_light})
    t_dark = _resolve({**root, **theme_dark})

    bad: list[str] = []

    # 1. every semantic token exists on both grounds
    for name, ground in (("light", light), ("dark", dark)):
        for key in SEMANTIC:
            if key not in ground:
                bad.append(f"semantic token --ac-{key} missing on the {name} ground")

    # 2. the manual toggle matches the automatic one
    for key in SEMANTIC:
        for label, auto, manual in (("light", light, t_light), ("dark", dark, t_dark)):
            if key in auto and key in manual and auto[key] != manual[key]:
                bad.append(
                    f'[data-theme="{label}"] --ac-{key} = {manual[key]!r} but '
                    f"prefers-color-scheme gives {auto[key]!r}"
                )

    # 3. the JSON mirror matches the CSS
    for name, ground in (("light", light), ("dark", dark)):
        for key, want in data["color"]["semantic"][name].items():
            got = ground.get(key)
            if got is None:
                bad.append(f"tokens.json has {name}.{key} but tokens.css does not")
            elif got.lower() != want.lower():
                bad.append(f"{name}.{key}: css {got!r} != json {want!r}")

    for key, spec in data["color"]["primitive"].items():
        got = root.get(key)
        if got is None or got.lower() != spec["value"].lower():
            bad.append(f"primitive {key}: css {got!r} != json {spec['value']!r}")

    for key, want in data["space"].items():
        got = root.get(f"space-{key}")
        if got != want:
            bad.append(f"space.{key}: css {got!r} != json {want!r}")

    # 4. the vendored copies are byte-identical.
    #
    # tokens.css exists three times: here, under design-system/ so the gallery
    # pages resolve it, and inside the blog repo. Copies are the honest choice
    # — there is no build step spanning two repos — but a copy nobody checks is
    # just drift with a delay. The blog lives outside this tree, so only the
    # in-tree copy can be gated here; BRAND.md § Consumers carries the rest.
    for rel in ("tokens.css", "components.css"):
        vendored = HERE / "design-system" / rel
        if not vendored.exists():
            bad.append(f"design-system/{rel} is missing — the gallery pages will render unstyled")
        elif vendored.read_bytes() != (HERE / rel).read_bytes():
            bad.append(f"design-system/{rel} has drifted from brand/{rel} — re-copy it")

    if bad:
        print("tokens: FAIL", file=sys.stderr)
        for line in bad:
            print(f"  - {line}", file=sys.stderr)
        return 1

    print(
        f"tokens: ok — {len(SEMANTIC)} semantic tokens on 2 grounds, "
        f"manual toggle matches, json mirrors css"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
