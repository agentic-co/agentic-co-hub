"""`python3 -m agentco <command>` — serve, digest, gate1, metrics, keygen.

Dry-run by default everywhere it matters. `digest` prints and does not deliver
unless asked, and does not send anywhere unless asked twice
(`--deliver --post`).

That second gate is not ceremony. Delivery reaches other people, and the
difference between a tool you can hand a colleague and one you cannot is
whether running it by mistake can message somebody.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from dataclasses import replace
from pathlib import Path

from agentco import ado, app as app_module, routing
from agentco import db, divergence, hook, inject, leases, metrics
from agentco.publish import Registry
from agentco.sop import SopLibrary, resolve_sop_store
from agentco.stores import open_queue, open_sop_library
from agentco.work import Queue, resolve_work_store


def _conn(args):
    return db.connect(app_module.resolve_db_path(args.db))


def cmd_serve(args) -> int:
    import uvicorn

    application = app_module.create_app(db_path=args.db, operator=args.operator)
    # Loopback by default. Binding 0.0.0.0 on a service holding a company's
    # scope claims should be a deliberate act, not the default a copy-pasted
    # command inherits.
    uvicorn.run(application, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_serve_mcp(args) -> int:
    """Run the MCP surface over stdio.

    Separate from `serve` because the transports are not interchangeable: HTTP
    listens on a port and several clients share it, while stdio is one process
    per client, launched BY the harness and speaking JSON-RPC on its own
    stdout. Anything else printed to that stdout corrupts the channel, which is
    why this command takes no `--verbose` and prints nothing itself.
    """
    from agentco.mcp_server import create_server

    server = create_server(
        db_path=args.db,
        work_store=args.work_store,
        sop_store=args.sop_store,
        actor=args.actor,
    )
    server.run()
    return 0


def cmd_digest(args) -> int:
    conn = _conn(args)
    collected = divergence.collect(conn)
    text = divergence.render_text(collected)
    print(text)

    if not args.deliver:
        print(
            "\n(dry run — nothing delivered, nothing marked. "
            "Re-run with --deliver at the cadence boundary.)",
            file=sys.stderr,
        )
        return 0

    delivered = divergence.deliver(conn, collected)
    print(f"\nDelivered {delivered} DivergenceObserved event(s).", file=sys.stderr)

    if args.post:
        from agentco import delivery

        delivery.send(text, collected, via=args.via)
        print(f"Delivered via {args.via}.", file=sys.stderr)
    return 0


def cmd_inject(args) -> int:
    """Tier-1 context injection — splice AgentCo status into `CLAUDE.md`/`AGENTS.md`.

    Dry-run by default, same posture as `digest`: a diff is always printed,
    and nothing on disk changes unless `--write` is passed. This is the
    command a scheduled job runs with `--write`; a human runs it bare first
    to see exactly what would change.

    The content is deliberately repo-scoped — live scope claims, which are
    meant to be public. Per-person state (divergence on your own snapshots)
    belongs in a session hook, not in a shared file; see
    `inject.render_session_block`.
    """
    conn = _conn(args)
    # REPO-scoped, and `--repo` is required rather than defaulted. The target is
    # a file the whole team reads and most repos commit, so the content must be
    # about the repo, not about whoever happens to run the scheduled job. An
    # actor default here would quietly publish one person's snapshots and claims
    # to everybody, permanently, via version control.
    live = leases.live_leases(conn, args.repo)
    block = inject.render_repo_block(live, repo=args.repo)

    results = inject.run([Path(t) for t in args.targets], block, write=args.write)
    exit_code = 0
    for result in results:
        print(f"{result.path}: {result.status} — {result.reason}")
        if result.diff:
            print(result.diff)
        if result.status == "error":
            exit_code = 1

    if not args.write and any(r.status == "would_write" for r in results):
        print("\n(dry run — nothing written. Re-run with --write to apply.)", file=sys.stderr)
    return exit_code


def cmd_hook_install(args) -> int:
    result = hook.install(args.settings, command=args.command, write=args.write)
    print(f"{result.path}: {result.status} — {result.reason}")
    if result.diff:
        print(result.diff)
    if not args.write and result.status == "would_install":
        print("\n(dry run — nothing written. Re-run with --write to apply.)", file=sys.stderr)
    return 1 if result.status == "error" else 0


def cmd_hook_uninstall(args) -> int:
    result = hook.uninstall(args.settings, write=args.write)
    print(f"{result.path}: {result.status} — {result.reason}")
    if result.diff:
        print(result.diff)
    if not args.write and result.status == "would_uninstall":
        print("\n(dry run — nothing written. Re-run with --write to apply.)", file=sys.stderr)
    return 1 if result.status == "error" else 0


def cmd_gate1(args) -> int:
    conn = _conn(args)
    status = metrics.gate1_status(conn, operator=args.operator)
    if args.json:
        print(json.dumps(status, indent=2))
        return 0 if status["met"] else 1

    print(f"{status['gate']}: {'MET' if status['met'] else 'NOT MET'}")
    print(f"  criterion: {status['criterion']}")
    print(
        f"  streak: {status['currentStreakWeeks']}/{status['weeksRequired']} "
        f"consecutive weeks (longest ever: {status['longestStreakWeeks']})"
    )
    print(f"  week in progress (excluded): {status['weekInProgress']}")
    print("  by completed week:")
    for week, publishers in status["byWeek"].items():
        marker = "✓" if len(publishers) >= 2 else " "
        names = ", ".join(publishers) if publishers else "—"
        print(f"    {marker} {week}: {len(publishers)} publisher(s)  {names}")
    return 0 if status["met"] else 1


def cmd_metrics(args) -> int:
    conn = _conn(args)
    report = {
        "gate1": metrics.gate1_status(conn, operator=args.operator),
        "latency": metrics.verb_latency(conn),
        "timeToFirstEvent": metrics.time_to_first_event(conn),
        "conflictPrecision": metrics.conflict_precision(conn),
    }
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    g = report["gate1"]
    print(f"GATE 1: {'MET' if g['met'] else 'NOT MET'} — streak {g['currentStreakWeeks']}/{g['weeksRequired']}")
    print("\nPer-verb latency (SLO: submit p99 ≤ 300ms, query p99 ≤ 500ms):")
    for row in report["latency"]:
        p99 = f"{row['p99Ms']}ms" if row["p99Ms"] is not None else "—"
        flag = "  ⚠ SLO BREACH" if row["breach"] else ""
        print(f"  {row['verb']:<16} n={row['calls']:<5} p50={row['p50Ms']} p95={row['p95Ms']} p99={p99}{flag}")
    print("\nTime to first accepted publish:")
    for row in report["timeToFirstEvent"]:
        secs = row["secondsToFirstAccept"]
        when = f"{secs}s" if secs is not None else "NEVER PUBLISHED"
        print(f"  {row['actor']:<24} {when}   refusals before first success: {row['refusalsBeforeFirstAccept']}")
    cp = report["conflictPrecision"]
    print(f"\nConflict precision: {cp['precision']} ({cp['conflictsActedOn']}/{cp['conflictsFired']})")
    print(f"  {cp['verdict']}")
    return 0


def cmd_ado_pull(args) -> int:
    """Read work items from Azure DevOps and file them onto the queue.

    Dry-run by default, the same posture as `digest` and `inject`: this writes
    into a queue that other people's agents poll, so running it by mistake
    should cost nothing but a printed table.

    Nothing here writes to Azure DevOps. The adapter issues reads only, and a
    repeat run is a no-op because every item is keyed on its ADO id.

    With `--routes`, each item is filed as an INSTANCE of the procedure its
    rule selects, and inherits that file's `assign` and `requires`. Without it,
    items are filed bare — which is fine for a queue nobody has written
    procedures for yet, and wrong the moment somebody has.
    """
    connector = _ado_connector(args)
    fetch = ado.make_fetcher(ado.resolve_pat(connector.pat_env))
    ids = [int(i) for i in args.ids.split(",")] if args.ids else None
    routes = routing.load(args.routes) if args.routes else None

    payloads, dropped = ado.pull(
        fetch,
        connector,
        ids=ids,
        assign=args.assign or (routes.assign if routes else None),
    )
    requires = list(args.requires.split(",")) if args.requires else list(routes.requires if routes else ())

    plan = []
    for payload in payloads:
        view = ado.route_view(payload)
        sop_key, sop_id, matched = routes.sop_id_for(view) if routes else (None, None, False)
        plan.append({"payload": payload, "sop_key": sop_key, "sop_id": sop_id, "matched": matched})

    for row in plan:
        meta = row["payload"]["metadata"]
        # A default hit is marked, not hidden: it is the signal that the rules
        # do not yet cover this backlog.
        tag = row["sop_key"] or "-"
        if row["sop_key"] and not row["matched"]:
            tag += " (default)"
        print(f"  {meta['adoState']:<10} {tag:<22} {row['payload']['title']}")
    if not plan:
        print("  (nothing matched)")

    # Never a silent truncation: a filter that quietly discards half the results
    # reads exactly like a query that found half as much.
    for row in dropped:
        print(f"  {'dropped':<10} {'':<22} {row['title']}  — {row['reason']}")

    if not args.write:
        assignee = args.assign or (routes.assign if routes else None)
        print(
            f"\n{len(plan)} item(s) would be filed"
            + (f", assigned to {assignee}" if assignee else "")
            + (f", requiring [{', '.join(requires)}]" if requires else "")
            + ". Re-run with --write."
        )
        if routes and any(r["sop_key"] and not r["matched"] for r in plan):
            print("(items marked (default) matched no rule — add one, or accept the default.)")
        return 0

    if args.registry_url:
        secret = os.environ.get(args.secret_env)
        if not args.actor or not secret:
            print(
                f"--registry-url needs --actor and {args.secret_env} in the environment.",
                file=sys.stderr,
            )
            return 2
        registry = Registry(args.actor, secret, args.registry_url)

        def file_one(row: dict) -> tuple[str, str, str]:
            p = row["payload"]
            common = dict(
                title=p["title"], source=p["source"], sourceId=p["sourceId"],
                metadata=p["metadata"], requires=requires,
                **({"assignedAgent": p["assignedAgent"]} if p["assignedAgent"] else {}),
            )
            if row["sop_id"]:
                item = registry.sop_instantiate(row["sop_id"], **common)["item"]
            else:
                title = common.pop("title")
                item = registry.work_create(title, **common)["item"]
            return item["id"], item.get("assigned_agent") or "-", row["sop_key"] or "-"
    else:
        queue = open_queue(args.work_store)
        library = open_sop_library(args.sop_store)

        def file_one(row: dict) -> tuple[str, str, str]:
            p = row["payload"]
            common = dict(
                source=p["source"], source_id=p["sourceId"], metadata=p["metadata"],
                assigned_agent=p["assignedAgent"], requires=requires,
            )
            if row["sop_id"]:
                item = library.instantiate(row["sop_id"], queue, title=p["title"], **common)
            else:
                item = queue.create(p["title"], **common)
            return item.id, item.assigned_agent or "-", row["sop_key"] or "-"

    filed = [file_one(row) for row in plan]
    print(f"\nfiled {len(filed)} item(s):")
    for item_id, assignee, sop_key in filed:
        print(f"  {item_id}  assigned={assignee:<10} sop={sop_key}")
    return 0


def _ado_connector(args) -> "ado.Connector":
    """The connector file, with any CLI flag overriding one field of it.

    A file rather than a dozen flags because which project, which team area,
    which work item types and which tags count as intake are facts about one
    organisation's backlog — the same class of thing as the org URL, and the
    same reason they are not constants. The flags stay for a one-off query
    without editing config.

    Built with `dataclasses.replace` rather than by naming every field. The
    enumerated version silently dropped `exclude_title_matches` the day it was
    added — the file said to exclude a product, the CLI rebuilt the connector
    without that field, and the pull returned it anyway with nothing reporting
    a problem. Any hand-kept list of fields eventually disagrees with the
    dataclass; this cannot.
    """
    base = ado.load_connector(args.connector) if args.connector else None

    # Only what the caller actually passed. `None` here means "not overridden",
    # which is why an empty --contains cannot be expressed as a flag: clearing a
    # configured value is an edit to the file, where a reader can see it.
    overrides = {
        "org_url": args.org_url,
        "project": args.project,
        "contains": args.contains,
        "wiql": args.wiql,
        "limit": args.limit,
        "pat_env": args.pat_env,
        "area_path": args.area_path,
        "types": tuple(t.strip() for t in args.types.split(",") if t.strip()) if args.types else None,
        "tags": tuple(t.strip() for t in args.tags.split(",") if t.strip()) if args.tags else None,
    }
    overrides = {k: v for k, v in overrides.items() if v is not None}

    if base is None:
        if not overrides.get("org_url") or not overrides.get("project"):
            raise SystemExit(
                "need --connector, or both --org-url and --project. Which project, "
                "which team area and which work item types are pulled is configuration."
            )
        return ado.Connector(**overrides)
    return replace(base, **overrides)


def cmd_drain(args) -> int:
    """Publish everything in the outbox, once, and say what happened.

    This is the other half of L1: an agent appends a line with no credential
    and no package, and this signs it with the machine's. Run it on a schedule
    — a drainer nobody scheduled is a file nobody reads, and the agent that
    wrote the line has already exited by the time anyone notices.

    Exit status is about the RUN, not about the lines. A refused line is a
    successful drain that carries bad news: the line reached the registry, the
    registry declined it, and the receipt says why. Non-zero is reserved for
    "this drain could not do its job" — no credential, or nothing to sign with
    — because a cron job that exits non-zero on somebody else's malformed
    payload trains its owner to ignore the alert.
    """
    from agentco import outbox as outbox_mod
    from agentco.publish import Registry

    box = outbox_mod.Outbox(outbox_mod.resolve_node_dir(args.node_dir))

    pending = box.pending()
    if args.dry_run:
        report = {
            "state": "dry-run",
            "nodeDir": str(box.dir),
            "pending": [{"lineId": r["line_id"], "verb": r["verb"]} for r in pending],
        }
        print(json.dumps(report, indent=2) if args.json else _drain_text(report))
        return 0

    missing = [v for v in ("AGENTCO_ACTOR", "AGENTCO_SECRET", "AGENTCO_URL") if not os.environ.get(v)]
    if missing:
        print(
            f"cannot drain: {', '.join(missing)} not set. The drainer holds the "
            f"machine credential — that is the whole reason the agent side needs "
            f"none. Set these where the SCHEDULER can see them (a launchd "
            f"EnvironmentVariables block, a systemd EnvironmentFile); a shell "
            f"profile is not read by either.",
            file=sys.stderr,
        )
        return 2

    registry = Registry(
        os.environ["AGENTCO_ACTOR"],
        os.environ["AGENTCO_SECRET"],
        os.environ["AGENTCO_URL"],
    )
    result = outbox_mod.drain(box, outbox_mod.registry_publisher(registry))
    result["nodeDir"] = str(box.dir)
    print(json.dumps(result, indent=2) if args.json else _drain_text(result))
    return 0


def _drain_text(result: dict) -> str:
    lines = [f"outbox: {result['nodeDir']}"]
    if result["state"] == "dry-run":
        lines.append(f"  {len(result['pending'])} line(s) pending, nothing sent")
        for row in result["pending"]:
            lines.append(f"    {row['lineId']}  {row['verb']}")
        return "\n".join(lines)
    if result["state"] == "skipped":
        lines.append(f"  skipped: {result['detail']}")
        return "\n".join(lines)
    lines.append(
        f"  published {result['published']}, refused {result['refused']}, "
        f"retryable {result['retryable']}, quarantined {result['quarantined']}"
    )
    for receipt in result.get("receipts", []):
        state = receipt["state"]
        if state == "published":
            continue
        detail = receipt.get("remediation") or receipt.get("detail") or ""
        lines.append(f"    {state}: {receipt.get('verb') or '?'} — {detail}")
    return "\n".join(lines)


def cmd_keygen(args) -> int:
    """Mint a shared secret for one actor and print the key-file line.

    Never writes the key file itself. The operator decides where secrets live
    (`~/.claude/.env` is this repo's canonical store), and a tool that writes
    secrets to a path it guessed is how a secret ends up in a git repo.
    """
    secret = secrets.token_urlsafe(32)
    print(json.dumps({args.actor: secret}, indent=2))
    print(
        f"\nMerge that into the JSON file $AGENTCO_REGISTRY_KEYS points at "
        f"(mode 600, never committed), and give {args.actor} the secret over a "
        f"channel you would send a password over.",
        file=sys.stderr,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentco",
        description="AgentCo stage-1b scope + snapshot registry.",
    )
    parser.add_argument("--db", default=None, help="path to the registry SQLite file")
    parser.add_argument(
        "--operator",
        default=None,
        help="the identity excluded from the adoption gate's publisher count (default: $AGENTCO_REGISTRY_OPERATOR)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the HTTP service")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8787)
    p_serve.set_defaults(func=cmd_serve)

    p_mcp = sub.add_parser("serve-mcp", help="run the MCP surface over stdio")
    p_mcp.add_argument("--work-store", default=None)
    p_mcp.add_argument("--sop-store", default=None)
    p_mcp.add_argument("--actor", default=None, help="identity this harness asserts")
    p_mcp.set_defaults(func=cmd_serve_mcp)

    p_digest = sub.add_parser("digest", help="the cadence-boundary divergence digest")
    p_digest.add_argument("--deliver", action="store_true", help="emit events and mark reported")
    p_digest.add_argument("--post", action="store_true", help="also deliver the digest (needs --deliver)")
    p_digest.add_argument("--via", default="webhook", help="delivery sender to use (default: webhook)")
    p_digest.set_defaults(func=cmd_digest)

    p_inject = sub.add_parser(
        "inject", help="tier-1 context injection — splice AgentCo status into CLAUDE.md/AGENTS.md"
    )
    p_inject.add_argument("targets", nargs="+", help="file(s) to splice into, e.g. CLAUDE.md AGENTS.md")
    p_inject.add_argument(
        "--repo", required=True, help="repo whose live scope claims to render into the block"
    )
    p_inject.add_argument("--write", action="store_true", help="apply the splice (default: dry run)")
    p_inject.set_defaults(func=cmd_inject)

    p_hook = sub.add_parser(
        "hook", help="tier-3 SessionStart hook — install/uninstall in a harness's settings file"
    )
    hook_sub = p_hook.add_subparsers(dest="hook_command", required=True)

    p_hook_install = hook_sub.add_parser("install", help="register the hook")
    p_hook_install.add_argument("settings", help="path to the harness's settings.json")
    p_hook_install.add_argument(
        "--command", default=None, help="override the hook command (default: this interpreter -m agentco.hook)"
    )
    p_hook_install.add_argument("--write", action="store_true", help="apply the change (default: dry run)")
    p_hook_install.set_defaults(func=cmd_hook_install)

    p_hook_uninstall = hook_sub.add_parser("uninstall", help="restore settings.json byte-identically")
    p_hook_uninstall.add_argument("settings", help="path to the harness's settings.json")
    p_hook_uninstall.add_argument("--write", action="store_true", help="apply the change (default: dry run)")
    p_hook_uninstall.set_defaults(func=cmd_hook_uninstall)

    p_gate = sub.add_parser("gate1", help="is the adoption gate met?")
    p_gate.add_argument("--json", action="store_true")
    p_gate.set_defaults(func=cmd_gate1)

    p_metrics = sub.add_parser("metrics", help="stage-1d report")
    p_metrics.add_argument("--json", action="store_true")
    p_metrics.set_defaults(func=cmd_metrics)

    p_ado = sub.add_parser(
        "ado-pull",
        help="file open Azure DevOps work items onto the queue (dry-run by default)",
    )
    p_ado.add_argument("--connector", help="JSON connector config: org, project, types, states")
    p_ado.add_argument("--org-url", help="https://dev.azure.com/<org> (overrides the connector)")
    p_ado.add_argument("--project", help="overrides the connector")
    p_ado.add_argument("--contains", help="filter on words in the title")
    p_ado.add_argument(
        "--types",
        help="comma-separated work item types to pull, e.g. Feature. "
             "Picking a level is picking the size of the thing an agent is handed.",
    )
    p_ado.add_argument("--area-path", help=r"e.g. PROJECT\Team — an ADO team is an area path, not a project")
    p_ado.add_argument("--tags", help="comma-separated tags every item must carry")
    p_ado.add_argument("--ids", help="comma-separated work item ids, instead of a query")
    p_ado.add_argument("--wiql", help="a raw WIQL query, instead of the built one")
    p_ado.add_argument("--assign", help="the agent to assign every filed item to")
    p_ado.add_argument("--routes", help="JSON rules deciding which SOP each item triggers")
    p_ado.add_argument("--requires", help="comma-separated capabilities a worker must declare")
    p_ado.add_argument("--sop-store", help="path to the local SOP library (local only)")
    p_ado.add_argument("--limit", type=int, default=None)
    p_ado.add_argument("--pat-env", default=None)
    # Where the work is filed. Absent, the local store; present, the shared
    # registry over HTTP — the same switch the MCP server takes.
    p_ado.add_argument("--registry-url", help="file into a remote registry instead of local files")
    p_ado.add_argument("--actor", help="the identity filing the work (remote only)")
    p_ado.add_argument("--secret-env", default="AGENTCO_SECRET")
    p_ado.add_argument("--work-store", help="path to the local queue (local only)")
    p_ado.add_argument(
        "--write",
        action="store_true",
        help="actually file the items; without this, print what would be filed",
    )
    p_ado.set_defaults(func=cmd_ado_pull)

    p_drain = sub.add_parser(
        "drain",
        help="publish the outbox and write receipts (L1)",
    )
    p_drain.add_argument(
        "--node-dir",
        default=None,
        help="the .agentco directory to drain (default: $AGENTCO_NODE_DIR, then ./.agentco)",
    )
    p_drain.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be published, send nothing, and touch no file",
    )
    p_drain.add_argument("--json", action="store_true", help="machine-readable output")
    p_drain.set_defaults(func=cmd_drain)

    p_key = sub.add_parser("keygen", help="mint a shared secret for one actor")
    p_key.add_argument("actor")
    p_key.set_defaults(func=cmd_keygen)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "post", False) and not getattr(args, "deliver", False):
        print("--post requires --deliver.", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
