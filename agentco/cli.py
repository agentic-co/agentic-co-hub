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
import secrets
import sys
from pathlib import Path

from agentco import app as app_module
from agentco import db, divergence, metrics


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

    p_digest = sub.add_parser("digest", help="the cadence-boundary divergence digest")
    p_digest.add_argument("--deliver", action="store_true", help="emit events and mark reported")
    p_digest.add_argument("--post", action="store_true", help="also deliver the digest (needs --deliver)")
    p_digest.add_argument("--via", default="webhook", help="delivery sender to use (default: webhook)")
    p_digest.set_defaults(func=cmd_digest)

    p_gate = sub.add_parser("gate1", help="is the adoption gate met?")
    p_gate.add_argument("--json", action="store_true")
    p_gate.set_defaults(func=cmd_gate1)

    p_metrics = sub.add_parser("metrics", help="stage-1d report")
    p_metrics.add_argument("--json", action="store_true")
    p_metrics.set_defaults(func=cmd_metrics)

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
