"""The multi-hub rig: real hubs from the real image, driven over real sockets.

`Rig` owns one compose project (`tests/network/compose.rig.yml`) for the length
of a test session:

  * a unique project name per run (`agentco-rig-<hex>`), and no fixed container
    names, so it never touches containers it did not create;
  * one key table per hub, generated into a temporary directory with fresh
    random secrets that are never printed, and mounted read-only as a directory
    (not a single file) so a host-side `agentco revoke --apply`, which replaces
    the file atomically, reaches the running hub on its next request;
  * `down -v --remove-orphans` at the end, which removes this project's
    containers, network and volumes, and the image if the rig built it.

Synthetic data only: the actors are fixed fictional names, the artifacts are
files the rig writes inside a container.
"""

from __future__ import annotations

import json
import os
import random
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

from agentco.publish import Registry, RegistryError

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = Path(__file__).resolve().parent / "compose.rig.yml"
HUBS = ("hub-a", "hub-b", "hub-c", "chain-sqlite", "chain-pg")

#: Who holds a key on which hub. Object-form entries carry a memory clearance
#: (ADR 0006); plain ones are an actor and nothing more.
ACTORS = {
    "hub-a": {
        "dana": {"acme": "confidential"},   # the declared human and admin
        "kofi": {"acme": "internal"},
        "ana": None,
        "operator": None,
        "hub-b": None,                      # the declared federated child
        "hub-d": None,                      # a second declared child, a key only
        "hub-c": None,                      # a valid key, NOT a declared child
        "rt-agent": None,                   # speaks for the external store runtime-a
        "relay": None,                      # a transport: carries answers, reaches ROUTES["hub-a"]["relay"] only
    },
    "hub-b": {"worker-b": None},
    "hub-c": {"worker-c": None},
    "chain-sqlite": {"dana": None, "kofi": None, "ana": None, "operator": None},
    "chain-pg": {"dana": None, "kofi": None, "ana": None, "operator": None},
}

#: Per-actor route allowlists written into the key table's object form
#: (agentco/authz.py). An actor not named here is unrestricted.
ROUTES = {"hub-a": {"relay": ["attest", "events"]}}


class Rig:
    def __init__(self) -> None:
        self.project = f"agentco-rig-{uuid.uuid4().hex[:10]}"
        self.keys_dir = Path(tempfile.mkdtemp(prefix="agentco-rig-keys-"))
        self.image = os.environ.get("AGENTCO_RIG_IMAGE") or f"agentco:{self.project}"
        self.built_image = not os.environ.get("AGENTCO_RIG_IMAGE")
        self.secrets: dict[str, dict[str, str]] = {}
        self.env = {**os.environ, "RIG_IMAGE": self.image, "RIG_KEYS": str(self.keys_dir),
                    "RIG_PG_PASSWORD": secrets.token_hex(16),
                    # a /24 unlikely to be in use; `up` fails loudly if it is
                    "RIG_NET": f"10.{random.randint(200, 250)}.{random.randint(0, 255)}"}

    # ---------------------------------------------------------------- lifecycle

    def up(self) -> "Rig":
        if self.built_image:
            self._run(["docker", "build", "-q", "-t", self.image, str(ROOT)], timeout=900)
        self._write_keys()
        self.env["RIG_HUB_B_SECRET"] = self.secrets["hub-a"]["hub-b"]
        self.env["RIG_HUB_C_SECRET"] = self.secrets["hub-a"]["hub-c"]
        self.compose("up", "-d", timeout=600)
        for hub in HUBS:
            self.wait_up(hub)
        return self

    def down(self) -> None:
        try:
            self.compose("down", "-v", "--remove-orphans", check=False, timeout=300)
            if self.built_image:
                self._run(["docker", "image", "rm", "-f", self.image], check=False)
        finally:
            shutil.rmtree(self.keys_dir, ignore_errors=True)

    def leftovers(self) -> dict[str, list[str]]:
        """What this project still holds after `down` — the rig's own audit."""
        label = f"label=com.docker.compose.project={self.project}"
        return {kind: self._run(["docker", kind, "ls", "-q", "--filter", label] + (["-a"] if kind == "container" else []),
                                check=False).stdout.split()
                for kind in ("container", "volume", "network")}

    def _write_keys(self) -> None:
        # 0755/0644: the hub runs as a non-root user with a different uid from
        # the host's, and these are throwaway synthetic secrets for one run.
        os.chmod(self.keys_dir, 0o755)
        for hub, actors in ACTORS.items():
            hub_dir = self.keys_dir / hub
            hub_dir.mkdir()
            os.chmod(hub_dir, 0o755)
            table, self.secrets[hub] = {}, {}
            for actor, clearance in actors.items():
                secret = secrets.token_hex(32)
                self.secrets[hub][actor] = secret
                routes = ROUTES.get(hub, {}).get(actor)
                entry = {"secret": secret, **({"clearance": clearance} if clearance else {}),
                         **({"routes": routes} if routes is not None else {})}
                table[actor] = entry if len(entry) > 1 else secret
            path = hub_dir / "keys.json"
            path.write_text(json.dumps(table, indent=2, sort_keys=True), encoding="utf-8")
            os.chmod(path, 0o644)

    # ------------------------------------------------------------------ plumbing

    def _run(self, cmd: list[str], *, check: bool = True, timeout: float = 120,
             env: Optional[dict] = None) -> subprocess.CompletedProcess:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env or self.env)
        if check and done.returncode != 0:
            # The command line names no secret (they travel in the environment),
            # so it is safe to show; stderr is the container's own words.
            raise RuntimeError(f"{' '.join(cmd[:6])} … exited {done.returncode}: {done.stderr.strip()[-800:]}")
        return done

    def compose(self, *args: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
        return self._run(["docker", "compose", "-p", self.project, "-f", str(COMPOSE), *args],
                         check=check, timeout=timeout)

    def url(self, hub: str) -> str:
        """Asked fresh every time: a hub's host port is ephemeral."""
        out = self.compose("port", hub, "8787").stdout.strip().splitlines()[0]
        return f"http://127.0.0.1:{out.rsplit(':', 1)[1]}"

    def container(self, hub: str) -> str:
        return self.compose("ps", "-q", hub).stdout.strip()

    @property
    def network(self) -> str:
        return f"{self.project}_rig"

    def wait_up(self, hub: str, timeout: float = 120) -> None:
        """Up means an unsigned request is REFUSED 401, the healthcheck's rule:
        serving, and the key table loaded closed."""
        deadline = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"{self.url(hub)}/events", timeout=3)
                last = "200 to an unsigned request"
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    return
                last = exc.code
            except (OSError, RuntimeError, IndexError) as exc:
                last = type(exc).__name__
            time.sleep(1)
        logs = self.compose("logs", "--tail", "20", hub, check=False).stdout
        raise RuntimeError(f"{hub} not up after {timeout}s (last: {last})\n{logs}")

    def restart(self, hub: str) -> None:
        self.compose("restart", hub, timeout=180)
        self.wait_up(hub)

    def exec(self, hub: str, *cmd: str, env: Optional[dict[str, str]] = None,
             timeout: float = 120) -> subprocess.CompletedProcess:
        # `env` lands in argv: use it for non-secret overrides only (the
        # deliberately wrong secret of an auth-failure case is one).
        flags = [arg for key, value in (env or {}).items() for arg in ("-e", f"{key}={value}")]
        return self._run(["docker", "compose", "-p", self.project, "-f", str(COMPOSE), "exec", "-T", *flags,
                          hub, *cmd], check=False, timeout=timeout)

    def disconnect(self, hub: str) -> None:
        self._run(["docker", "network", "disconnect", self.network, self.container(hub)])

    def connect(self, hub: str) -> None:
        """Rejoin at the hub's own address (see the compose file's note)."""
        octet = {"hub-a": 11, "hub-b": 12, "hub-c": 13, "chain-sqlite": 14, "chain-pg": 15}[hub]
        self._run(["docker", "network", "connect", "--alias", hub, "--ip", f"{self.env['RIG_NET']}.{octet}",
                   self.network, self.container(hub)])

    # ------------------------------------------------------------------ actors

    def registry(self, hub: str, actor: str) -> Registry:
        return Registry(actor, self.secrets[hub][actor], self.url(hub))

    def call(self, hub: str, actor: str, method: str, path: str, body: Any = None,
             query: str = "") -> tuple[int, Any]:
        """(status, payload); never raises on a refusal, so a test states both."""
        if query and not query.startswith("?"):
            query = "?" + query  # Registry._call appends the query verbatim
        try:
            return 200, self.registry(hub, actor)._call(method, path, body, query=query)
        except RegistryError as exc:
            return exc.status, exc.payload

    def revoke(self, hub: str, actor: str) -> subprocess.CompletedProcess:
        """The operator's own command, run on the host against the hub's mounted table."""
        return self._run([sys.executable, "-m", "agentco", "revoke", actor,
                          "--keys", str(self.keys_dir / hub / "keys.json"), "--apply"])

    def restore_key(self, hub: str, actor: str) -> None:
        """Put an actor back with the SAME secret, as a re-issued key would be."""
        path = self.keys_dir / hub / "keys.json"
        table = json.loads(path.read_text(encoding="utf-8"))
        table[actor] = self.secrets[hub][actor]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(table, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(tmp, 0o644)
        tmp.replace(path)
