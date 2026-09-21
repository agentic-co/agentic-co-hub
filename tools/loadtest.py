#!/usr/bin/env python3
"""What a connected actor actually experiences, at a population, through the real surface.

Not a benchmark of the storage layer — there are cheaper ways to learn that, and
they miss the thing that matters. This drives the HTTP service a harness talks
to, with signed requests, at a declared cadence, so the number it reports is the
one a person would feel.

WHY IT MEASURES WHAT IT MEASURES. The fleet's steady state is one cursor poll
per actor per cadence, because the feed now carries ASOP lifecycle and work
availability; the expensive reads happen only when an event says so. So the
headline is `GET /events?since=`, and the question is whether p99 stays
unremarkable while every actor in the population is doing it.

It also counts the rows the `calls` table gained during the run. Every
authenticated request writes one, there is no retention, and at four actors that
table already held 11,506 rows against 9 events. A load test that reported
latency and ignored that would be measuring the half that was never in doubt.

    python3 tools/loadtest.py --actors 130 --seconds 30
    python3 tools/loadtest.py --actors 500 --seconds 30 --cadence 30

Starts its own registry against the DSN in AGENTCO_TEST_PG (or SQLite if unset,
which is worth doing once to see the difference), runs the population, stops it.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentco import auth  # noqa: E402


def percentile(values, p):
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1)))))
    return ordered[k]


class Actor(threading.Thread):
    """One connected identity, doing what a harness does between decisions."""

    def __init__(self, name, secret, url, cadence, stop_at, results, work_share):
        super().__init__(daemon=True)
        self.name, self.secret, self.url = name, secret, url
        self.cadence, self.stop_at = cadence, stop_at
        self.results = results
        self.work_share = work_share
        self.cursor = None

    def _call(self, method, path, body=None):
        raw = json.dumps(body).encode() if body is not None else b""
        ts = str(int(time.time()))
        # The signature covers the PATH, not the query string, so a poll signs
        # the same path every time and the cursor rides in the query.
        sig_path = path.split("?")[0]
        req = urllib.request.Request(
            self.url + path, data=raw if body is not None else None, method=method,
            headers={
                "X-AgentCo-Actor": self.name,
                "X-AgentCo-Timestamp": ts,
                "X-AgentCo-Signature": auth.sign(self.secret, method, sig_path, ts, raw),
                "Content-Type": "application/json",
            },
        )
        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read() or b"{}")
            elapsed = (time.monotonic() - start) * 1000
            self.results.record(sig_path, elapsed, ok=True)
            return payload
        except urllib.error.HTTPError as exc:
            elapsed = (time.monotonic() - start) * 1000
            self.results.record(sig_path, elapsed, ok=False, code=exc.code)
            return None
        except Exception as exc:  # noqa: BLE001 - counted, never silent
            elapsed = (time.monotonic() - start) * 1000
            self.results.record(sig_path, elapsed, ok=False, code=type(exc).__name__)
            return None

    def run(self):
        # Jitter the FIRST poll across one cadence. Without it every actor
        # polls on the same tick for the whole run, which measures a thundering
        # herd rather than a fleet — and manufactures the very shape the real
        # deployment is supposed to avoid.
        time.sleep(random.uniform(0, self.cadence))
        while time.monotonic() < self.stop_at:
            query = "?limit=50" + (f"&since={self.cursor}" if self.cursor else "")
            page = self._call("GET", f"/events{query}")
            if page:
                self.cursor = page.get("nextCursor") or self.cursor
            if random.random() < self.work_share:
                self._call("POST", "/work/pull", {})
            time.sleep(self.cadence)


class Results:
    def __init__(self):
        self.lock = threading.Lock()
        self.by_path: dict[str, list[float]] = {}
        self.errors: dict[str, int] = {}
        self.count = 0

    def record(self, path, ms, ok, code=None):
        with self.lock:
            self.by_path.setdefault(path, []).append(ms)
            self.count += 1
            if not ok:
                self.errors[f"{path} {code}"] = self.errors.get(f"{path} {code}", 0) + 1

    def report(self, seconds, actors):
        print(f"\n  {actors} actors, {seconds}s, {self.count} requests "
              f"({self.count / seconds:.1f}/s)\n")
        print(f"  {'endpoint':<16} {'n':>6} {'p50':>9} {'p95':>9} {'p99':>9} {'max':>9}")
        for path, values in sorted(self.by_path.items()):
            print(f"  {path:<16} {len(values):>6} {percentile(values,50):>7.1f}ms "
                  f"{percentile(values,95):>7.1f}ms {percentile(values,99):>7.1f}ms "
                  f"{max(values):>7.1f}ms")
        if self.errors:
            print("\n  errors:")
            for key, n in sorted(self.errors.items()):
                print(f"    {key}: {n}")
        else:
            print("\n  errors: none")


def calls_rows(dsn, state_dir):
    """How many rows the metering table holds — the growth nobody budgets for."""
    try:
        if dsn:
            import psycopg
            with psycopg.connect(dsn, autocommit=True) as conn:
                return conn.execute("SELECT count(*) FROM calls").fetchone()[0]
        import sqlite3
        conn = sqlite3.connect(str(Path(state_dir) / "registry.sqlite3"))
        return conn.execute("SELECT count(*) FROM calls").fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        return f"unreadable ({type(exc).__name__})"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--actors", type=int, default=130)
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--cadence", type=float, default=15.0, help="seconds between polls")
    ap.add_argument("--work-share", type=float, default=0.1,
                    help="fraction of cycles that also pull work")
    # 0 means "ask the OS for a free one". A fixed default turns a crashed
    # previous run into a confusing failure of the next one — which is exactly
    # how the first run of this tool failed.
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()

    dsn = os.environ.get("AGENTCO_TEST_PG")
    state = tempfile.mkdtemp(prefix="agentco-load-")
    keys = {f"actor-{i:04d}": f"secret-{i:04d}" for i in range(args.actors)}
    keys["operator"] = "operator-secret"
    keys_path = Path(state) / "keys.json"
    keys_path.write_text(json.dumps(keys))

    env = {**os.environ,
           "AGENTCO_REGISTRY_KEYS": str(keys_path),
           "AGENTCO_REGISTRY_OPERATOR": "operator"}
    if dsn:
        env["AGENTCO_DB"] = dsn
        for var in ("AGENTCO_REGISTRY_DB", "AGENTCO_WORK_STORE", "AGENTCO_SOP_STORE"):
            env.pop(var, None)
    else:
        env["AGENTCO_REGISTRY_DB"] = str(Path(state) / "registry.sqlite3")
        env["AGENTCO_WORK_STORE"] = str(Path(state) / "work.jsonl")
        env["AGENTCO_SOP_STORE"] = str(Path(state) / "sops.jsonl")

    print(f"backend: {'postgres' if dsn else 'sqlite + jsonl'}")
    print(f"pool max: {env.get('AGENTCO_PG_POOL_MAX', '10 (default)') if dsn else 'n/a'}")

    port = args.port
    if not port:
        import socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

    server = subprocess.Popen(
        [sys.executable, "-m", "agentco", "serve", "--host", "127.0.0.1",
         "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,   # its own group, so the cleanup below reaches uvicorn
    )
    url = f"http://127.0.0.1:{port}"
    print(f"registry: {url}")
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(url + "/events", timeout=2)
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    break          # refusing unauthenticated IS the healthy answer
            except Exception:
                pass
            time.sleep(0.5)
        else:
            print("the registry never came up", file=sys.stderr)
            return 1

        before = calls_rows(dsn, state)
        results = Results()
        stop_at = time.monotonic() + args.seconds
        actors = [Actor(name, secret, url, args.cadence, stop_at, results, args.work_share)
                  for name, secret in keys.items() if name != "operator"]
        for a in actors:
            a.start()
        for a in actors:
            a.join(timeout=args.seconds + 60)
        results.report(args.seconds, args.actors)
        after = calls_rows(dsn, state)
        print(f"\n  calls table: {before} -> {after}"
              + (f"  (+{after - before} rows in {args.seconds}s"
                 f" = {(after - before) * 86400 / args.seconds / 1e6:.2f}M/day at this rate)"
                 if isinstance(before, int) and isinstance(after, int) else ""))
        return 0
    finally:
        # The whole group: `terminate()` on the parent can leave uvicorn holding
        # the port, and the next run then fails as "address already in use" —
        # which reads as a bug in the run that is starting rather than the one
        # that ended.
        import signal
        try:
            os.killpg(os.getpgid(server.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(server.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                server.kill()


if __name__ == "__main__":
    sys.exit(main())
