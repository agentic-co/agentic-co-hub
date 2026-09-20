#!/usr/bin/env python3
"""Export a registry's procedure library, and restore it into another one.

Why this exists: a deployed registry's whole state is one file on one volume. A
`helm.sh/resource-policy: keep` annotation stops a release from deleting that volume, which is
not a backup — it is a promise that one particular actor will not destroy it. A node loss, a
storage-class migration, or a wrong `kubectl delete pvc` all still take everything.

What it copies, and what it deliberately does not:

  * EVERY VERSION of every ASOP, not just the active one. A backup holding only the active
    version keeps the text and throws away the lineage — and the lineage is the point, because
    `outcomes_by_version` is how "did the revision help" is answerable at all. Restoring a
    procedure at v1 when production had v4 would silently discard three revisions' worth of
    learning.
  * The outcomes, the event feed and the work items, because an ASOP's value is its record.
  * NOT the ids. Ids are server-assigned; a restore is a new lineage carrying the same content
    and the same version ORDER, and `MANIFEST.json` records the old→new mapping so a reference
    held elsewhere (`metadata.sop_ref` on a bead, a link in a document) can be translated rather
    than quietly pointing at nothing.

Stdlib only, and the same env vars every other tool here reads:

    AGENTCO_URL=… AGENTCO_ACTOR=… AGENTCO_SECRET=… python3 tools/backup_registry.py export --out DIR
    AGENTCO_URL=… AGENTCO_ACTOR=… AGENTCO_SECRET=… python3 tools/backup_registry.py restore --from DIR

Restore is additive and never destructive: it authors new procedures on the target. Pointing it
at a registry that already holds the same library gives you a second copy, which is a mess but
not a loss — the reverse tradeoff would let a typo in `AGENTCO_URL` delete production.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentco.publish import Registry, RegistryError  # noqa: E402

# Fields that belong to the STORE rather than to the procedure: server-assigned, or derived.
# Sending them back on a create/revise is either refused or silently dropped, and both are worse
# than not sending them — the first looks like a broken backup, the second like a good restore.
#
# `author`/`author_kind` are on this list because the server REFUSES them outright
# (`author_from_signature`): the author is the actor that signed the request, and the kind is
# whether the operator listed that actor in AGENTCO_HUMANS — "a caller cannot become human by
# saying so". Found by running a restore, not by reading the schema, which is the argument for
# a backup tool whose test is a restore rather than a file that exists.
NOT_AUTHORED = frozenset({
    "asop_id", "version", "status", "created_at", "updated_at", "created_by",
    "activated_at", "activated_by", "outcomes", "outcomes_by_version", "proposals",
    "author", "author_kind",
})


def _prune(value):
    """Drop what the store materialised but an author may not assert.

    This registry distinguishes ABSENT from EMPTY, and says so in its refusals: `'common_mistakes'
    is empty — an empty list is the claim that this work has no known failure modes. Omit the key.`
    A read gives you the materialised shape (`common_mistakes: []`), so replaying a read verbatim
    turns "nobody has recorded a failure mode yet" into "this work has none", which is a stronger
    and false statement. Recursive, because the claim lives on a STEP, not on the procedure.
    """
    if isinstance(value, dict):
        cleaned = {k: _prune(v) for k, v in value.items()}
        return {k: v for k, v in cleaned.items() if v not in (None, [], {}, "")}
    if isinstance(value, list):
        return [_prune(v) for v in value]
    return value


def _authored(row: dict) -> dict:
    """The subset of a stored procedure that an author is allowed to send back."""
    body = {k: v for k, v in row.items() if k not in NOT_AUTHORED and not k.startswith("_")}
    return _prune(body)


def registry(url: str | None = None) -> Registry:
    missing = [v for v in ("AGENTCO_ACTOR", "AGENTCO_SECRET") if not os.environ.get(v)]
    target = url or os.environ.get("AGENTCO_URL")
    if not target:
        missing.append("AGENTCO_URL")
    if missing:
        sys.exit(f"FAIL  missing env: {', '.join(missing)}")
    return Registry(os.environ["AGENTCO_ACTOR"], os.environ["AGENTCO_SECRET"], target)


def _write(path: Path, rows: list[dict]) -> int:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    return len(rows)


def _readback(path: Path, expected: int) -> str | None:
    """Re-read one file from disk and check it holds what we counted into it.

    `_write` returning `len(rows)` measures what we INTENDED to write, not what
    landed — a short write, a full volume or a row that serialises to something
    unparseable all leave that number intact. The manifest then publishes a
    count nothing verified. Returns a problem sentence, or None.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"{path.name}: written but unreadable ({type(exc).__name__}: {exc})"
    rows = [line for line in text.splitlines() if line.strip()]
    for n, line in enumerate(rows, 1):
        try:
            json.loads(line)
        except ValueError as exc:
            return f"{path.name}: line {n} does not parse back ({exc})"
    if len(rows) != expected:
        return f"{path.name}: counted {expected} row(s), file holds {len(rows)}"
    return None


def _git_ignored(paths: list[Path]) -> list[str] | None:
    """Which of these files git would drop from a commit. `None` = could not check.

    This is the check the readback above cannot make, and the one that matters
    most in practice: a backup's files can all be written perfectly and still
    not survive being committed. Eight consecutive backups in this repo carried
    a MANIFEST counting `sops` and `work` they did not contain, because both
    filenames are ignored as bare patterns and a bare pattern matches at any
    depth (fixed 2026-09-10, but the class of failure outlives the instance —
    the next store filename added to .gitignore re-creates it).

    `None` rather than `[]` when git is absent or the directory is outside a
    work tree: unreported is not the same as clean, and this file's own manifest
    was the last thing to blur that distinction.
    """
    root = paths[0].parent
    if not root.is_dir():
        return None                     # nothing written there; nothing to vouch for
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "--", *(str(p) for p in paths)],
            capture_output=True, text=True, timeout=10, cwd=str(root),
        )
    except (OSError, subprocess.SubprocessError):
        return None                     # no git on PATH — unchecked, not clean
    # 0 = at least one ignored, 1 = none ignored. Anything else (128: not a
    # work tree) is a check that did not run, not a check that passed.
    if proc.returncode not in (0, 1):
        return None
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def do_export(args: argparse.Namespace) -> int:
    reg = registry()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    listed = reg.sop_list().get("sops", [])
    sops: list[dict] = []
    outcomes: list[dict] = []

    for entry in listed:
        sop_id = entry.get("asop_id") or entry.get("id")
        active = entry.get("version")
        # Walk DOWN from the active version rather than up from 1: the active version is the only
        # number the listing guarantees, and a gap (a retired version) then ends the walk instead
        # of making it stop at 1 and report a one-version history.
        version = active
        while version and version >= 1:
            try:
                got = reg.sop_get(sop_id, version)["sop"]
            except RegistryError as exc:
                print(f"      {sop_id} v{version}: unreadable ({exc})", file=sys.stderr)
                break
            got["_active_version"] = active
            sops.append(got)
            version -= 1
        try:
            outcomes.append({"asop_id": sop_id, **reg.sop_outcomes(sop_id)})
        except RegistryError:
            pass  # A procedure with no runs has no outcomes; that is not a failure.

    events = reg.events(limit=1000).get("events", [])
    work = reg.work_list().get("items", [])

    # path -> rows, so the manifest key and the filename cannot drift apart.
    written = {
        out / "sops.jsonl": sops,
        out / "outcomes.jsonl": outcomes,
        out / "events.jsonl": events,
        out / "work.jsonl": work,
    }
    counts = {path.stem: _write(path, rows) for path, rows in written.items()}

    # Verify BEFORE the manifest is written. The manifest is the claim a restore
    # trusts, so an unverifiable backup must not get one — "a backup that
    # reports success and carries nothing is worse than no backup, because it
    # is trusted".
    problems = [p for p in (_readback(path, counts[path.stem]) for path in written) if p]

    ignored = _git_ignored(list(written))
    if ignored:
        problems += [
            f"{Path(entry).name}: written to disk but git-ignored — committing this "
            f"backup would silently drop it"
            for entry in sorted(ignored)
        ]

    if problems:
        for problem in problems:
            print(f"      {problem}", file=sys.stderr)
        print(
            f"FAIL  wrote {out} but cannot vouch for it; no MANIFEST.json written. "
            f"Fix the files named above and re-run — do not restore from this directory, "
            f"and do not treat its presence as a backup.",
            file=sys.stderr,
        )
        return 1

    manifest = {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "source_url": reg.base_url,
        "taken_by": reg.actor,
        "procedures": len({s.get("asop_id") for s in sops}),
        "counts": counts,
        # What the counts above are worth. `readback` is every file re-read and
        # re-parsed; `durability` is null when git could not be consulted, never
        # true-by-omission — a reader has to be able to tell "survives a commit"
        # from "nobody asked".
        "verified": {
            "readback": "ok",
            "durability": "ok" if ignored == [] else None,
        },
        "versions_by_sop": {
            sid: sorted(s["version"] for s in sops if s.get("asop_id") == sid)
            for sid in sorted({s.get("asop_id") for s in sops})
        },
    }
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"      {manifest['procedures']} procedure(s), " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    if ignored is None:
        print(
            "      durability UNCHECKED — no git work tree here, so nothing confirmed "
            "these files survive a commit. Check it yourself if this directory is versioned.",
            file=sys.stderr,
        )
    print(f"PASS  exported {reg.base_url} -> {out} (readback ok, {len(counts)} file(s))")
    return 0


def do_restore(args: argparse.Namespace) -> int:
    reg = registry(args.url)
    src = Path(getattr(args, "from"))
    rows = [json.loads(l) for l in (src / "sops.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    if not rows:
        print("FAIL  nothing to restore — sops.jsonl is empty", file=sys.stderr)
        return 1

    by_sop: dict[str, list[dict]] = {}
    for row in rows:
        by_sop.setdefault(row["asop_id"], []).append(row)

    mapping: dict[str, str] = {}
    failures = 0
    for old_id, versions in by_sop.items():
        versions.sort(key=lambda r: r["version"])          # v1 first: revisions must replay in order
        active = versions[0].get("_active_version")
        try:
            first = _authored(versions[0])
            title = first.pop("title", old_id)
            created = reg.sop_create(title, **first)["sop"]
            new_id = created["asop_id"]
            for row in versions[1:]:
                body = _authored(row)
                body.pop("title", None)
                reg.sop_revise(new_id, **body)
            if active:
                reg.sop_activate(new_id, active)
            mapping[old_id] = new_id
            print(f"      {old_id} -> {new_id}  versions={[r['version'] for r in versions]} active={active}")
        except RegistryError as exc:
            failures += 1
            print(f"      {old_id}: REFUSED {exc}", file=sys.stderr)

    (src / "RESTORE-MAP.json").write_text(
        json.dumps({"restored_at": datetime.now(timezone.utc).isoformat(),
                    "target_url": reg.base_url, "id_map": mapping}, indent=2),
        encoding="utf-8")

    if failures:
        print(f"FAIL  {failures} of {len(by_sop)} procedure(s) did not restore", file=sys.stderr)
        return 1
    print(f"PASS  restored {len(mapping)} procedure(s) into {reg.base_url}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("export", help="write the library to a directory")
    p_export.add_argument("--out", required=True)
    p_export.set_defaults(func=do_export)

    p_restore = sub.add_parser("restore", help="author the library into a registry")
    p_restore.add_argument("--from", required=True, dest="from")
    p_restore.add_argument("--url", help="target registry; defaults to $AGENTCO_URL")
    p_restore.set_defaults(func=do_restore)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
