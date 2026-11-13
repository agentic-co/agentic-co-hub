"""tools/deploy/hubctl: several separate hubs, walled from each other.

Real processes: two hubs are created with `hubctl init`, started with `hubctl run` (the same entry
point a service definition executes) on free ports, and exercised over HTTP with signed requests.
Then isolation is broken on purpose in every way `check-isolation` claims to cover, and each break
must be reported precisely. Nothing is installed on the machine: service installation is tested
against a temporary directory, and every process is killed in `finally` with a deadline on every wait.
Synthetic names only.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HUBCTL = REPO / "tools" / "deploy" / "hubctl"


def load_hubctl():
    loader = SourceFileLoader("hubctl_under_test", str(HUBCTL))
    spec = spec_from_loader("hubctl_under_test", loader)
    assert spec is not None
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


hubctl = load_hubctl()
PG_HOST = "db.example"


def pg_dsn(user: str, password: str, database: str) -> str:
    return f"postgresql://{user}:{password}@{PG_HOST}:5432/{database}"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENTCO_", "ASOP_", "HUBCTL_"))}
    env["PYTHONPATH"] = str(REPO)
    return env


def ctl(*args: str, root: Path, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HUBCTL), *args, "--root", str(root)], capture_output=True,
                          text=True, env=clean_env(), cwd=REPO, timeout=timeout)


def ok(done: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    assert done.returncode == 0, f"stdout={done.stdout[-500:]} stderr={done.stderr[-500:]}"
    return done


def make_pair(root: Path, names=("client-a", "personal")) -> None:
    for name in names:
        ok(ctl("init", name, "--port", str(free_port()), root=root))


def clone_root(template: Path, dest: Path) -> Path:
    """A private copy of a not-yet-started pair, with every absolute path re-pointed at the copy."""
    shutil.copytree(template, dest)
    for env_file in dest.glob("*/hub.env"):
        text = env_file.read_text().replace(str(template), str(dest))
        env_file.write_text(text)
        env_file.chmod(0o600)
    return dest


def set_env(root: Path, name: str, **changes: str) -> None:
    path = root / name / "hub.env"
    lines = []
    seen = set()
    for line in path.read_text().splitlines():
        key = line.split("=", 1)[0]
        if key in changes:
            line = f"{key}={changes[key]}"
            seen.add(key)
        lines.append(line)
    lines += [f"{k}={v}" for k, v in changes.items() if k not in seen]
    path.write_text("\n".join(lines) + "\n")


def env_of(root: Path, name: str) -> dict:
    return hubctl.parse_env(root / name / "hub.env")


class Running:
    """One hub started the way a service starts it, killed on exit."""

    def __init__(self, root: Path, name: str):
        self.root, self.name = root, name
        self.proc: subprocess.Popen | None = None
        self.inst = hubctl.Instance(root, name)

    def start(self) -> "Running":
        self.proc = subprocess.Popen([sys.executable, str(HUBCTL), "run", self.name, "--root", str(self.root)],
                                     env=clean_env(), cwd=REPO, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 40
        cred = self.inst.operator_credential()
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                log = self.inst.log_dir / "hub.log"
                raise RuntimeError(f"hub {self.name} exited: {log.read_text()[-600:] if log.exists() else ''}")
            status, _ = hubctl.signed_get(self.inst.url, "/hub", *cred, timeout=2)
            if status == 200:
                return self
            time.sleep(0.2)
        raise RuntimeError(f"hub {self.name} not up after 40 s")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=10)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def credential(self, actor: str = "operator") -> tuple[str, str]:
        return actor, hubctl.secret_of(self.inst.keys()[actor])


@pytest.fixture(scope="module")
def pristine(tmp_path_factory) -> Path:
    """A freshly created, never started pair. Copied by tests that tamper with it."""
    root = tmp_path_factory.mktemp("pristine") / "hubs"
    make_pair(root)
    return root


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    root = tmp_path_factory.mktemp("live") / "hubs"
    make_pair(root)
    hubs = []
    try:
        for name in ("client-a", "personal"):
            hubs.append(Running(root, name).start())
        yield root, hubs[0], hubs[1]
    finally:
        for hub in hubs:
            hub.stop()


# ----------------------------------------------------------------------------- init

def test_init_makes_private_instances_with_nothing_shared(pristine):
    a, b = env_of(pristine, "client-a"), env_of(pristine, "personal")
    for name in ("client-a", "personal"):
        inst = pristine / name
        assert stat.S_IMODE(inst.stat().st_mode) == 0o700
        assert stat.S_IMODE((inst / "hub.env").stat().st_mode) == 0o600
        assert stat.S_IMODE((inst / "keys.json").stat().st_mode) == 0o600
        assert stat.S_IMODE((inst / "data").stat().st_mode) == 0o700
    assert a["HUBCTL_PORT"] != b["HUBCTL_PORT"]
    assert a["AGENTCO_DB"] != b["AGENTCO_DB"]
    secrets_ = {hubctl.secret_of(json.loads((pristine / n / "keys.json").read_text())["operator"])
                for n in ("client-a", "personal")}
    assert len(secrets_) == 2
    for key in ("AGENTCO_DB", "AGENTCO_REGISTRY_KEYS", "HUBCTL_DATA_DIR", "AGENTCO_NODE_DIR"):
        assert a[key].startswith(str(pristine / "client-a"))
    assert a["HUBCTL_BIND"] == "127.0.0.1"
    # the file never holds a credential the way keys.json does
    assert hubctl.secret_of(json.loads((pristine / "client-a" / "keys.json").read_text())["operator"]) \
        not in (pristine / "client-a" / "hub.env").read_text()


def test_init_refuses_a_reused_name_or_port_and_unasked_network_exposure(pristine, tmp_path):
    root = clone_root(pristine, tmp_path / "hubs")
    port = env_of(root, "client-a")["HUBCTL_PORT"]
    again = ctl("init", "client-a", "--port", str(free_port()), root=root)
    assert again.returncode != 0 and "already exists" in again.stderr
    taken = ctl("init", "third", "--port", port, root=root)
    assert taken.returncode != 0 and "already taken by hub 'client-a'" in taken.stderr
    exposed = ctl("init", "third", "--port", str(free_port()), "--bind", "0.0.0.0", root=root)
    assert exposed.returncode != 0 and "--expose-network" in exposed.stderr
    assert not (root / "third").exists()
    allowed = ctl("init", "third", "--port", str(free_port()), "--bind", "0.0.0.0", "--expose-network", root=root)
    assert allowed.returncode == 0 and env_of(root, "third")["HUBCTL_BIND"] == "0.0.0.0"
    assert "network-exposed" in ctl("check-isolation", root=root).stdout
    pg = ctl("init", "pg", "--port", str(free_port()), "--backend", "postgres", root=root)
    assert pg.returncode != 0 and "--pg-dsn" in pg.stderr


def test_init_refuses_a_postgres_database_another_hub_uses(tmp_path):
    root = tmp_path / "hubs"
    dsn = pg_dsn("hub1", "pw-one", "hub_one")
    ok(ctl("init", "client-a", "--port", str(free_port()), "--backend", "postgres", "--pg-dsn", dsn, root=root))
    clash = ctl("init", "personal", "--port", str(free_port()), "--backend", "postgres", "--pg-dsn",
                pg_dsn("hub2", "pw-two", "hub_one"), root=root)
    assert clash.returncode != 0 and "own database" in clash.stderr


# ----------------------------------------------------------------------------- add-actor

def test_add_actor_prints_the_credential_once_to_stdout_and_declares_roles(pristine, tmp_path):
    root = clone_root(pristine, tmp_path / "hubs")
    done = ok(ctl("add-actor", "client-a", "dana", "--human", "--verifier", root=root))
    printed = json.loads(done.stdout)
    secret = printed["secret"]
    assert printed["actor"] == "dana" and secret and secret not in done.stderr
    stored = hubctl.secret_of(json.loads((root / "client-a" / "keys.json").read_text())["dana"])
    assert stored == secret
    env = env_of(root, "client-a")
    assert env["ASOP_HUMANS"] == "dana" and env["ASOP_VERIFIERS"] == "dana"
    assert secret not in (root / "client-a" / "hub.env").read_text()
    assert "dana" not in json.loads((root / "personal" / "keys.json").read_text())
    assert env_of(root, "personal")["ASOP_HUMANS"] == ""
    ok(ctl("add-actor", "client-a", "eli", "--human", root=root))
    assert env_of(root, "client-a")["ASOP_HUMANS"] == "dana,eli"
    dup = ctl("add-actor", "client-a", "dana", root=root)
    assert dup.returncode != 0 and "already has a credential" in dup.stderr
    assert stat.S_IMODE((root / "client-a" / "keys.json").stat().st_mode) == 0o600
    assert ok(ctl("check-isolation", root=root)).returncode == 0


# ----------------------------------------------------------------------------- service

@pytest.mark.parametrize("platform,marker", [("launchd", "com.agentco.hub.client-a"),
                                             ("systemd", "agentco-hub-client-a.service")])
def test_service_print_is_default_and_writes_nothing_then_install_uninstall_in_a_temp_dir(
        pristine, tmp_path, platform, marker):
    root = clone_root(pristine, tmp_path / "hubs")
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    printed = ok(ctl("service", "client-a", "--platform", platform, root=root))
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before
    text = printed.stdout
    assert str(root / "client-a") in text and str(root / "personal") not in text
    if platform == "systemd":
        assert f"EnvironmentFile={root / 'client-a' / 'hub.env'}" in text
        assert "Restart=on-failure" in text and "-m agentco serve" in text
        assert f"--port {env_of(root, 'client-a')['HUBCTL_PORT']}" in text
    else:
        assert "<key>KeepAlive</key>" in text and "<string>run</string>" in text
        assert "hub.log" in text
    assert "hub.log" in text
    for secret in (hubctl.secret_of(v) for v in json.loads((root / "client-a" / "keys.json").read_text()).values()):
        assert secret not in text
    units = tmp_path / "units"
    ok(ctl("service", "client-a", "--install", "--platform", platform, "--unit-dir", str(units), root=root))
    written = units / marker if platform == "systemd" else units / f"{marker}.plist"
    assert written.exists() and written.read_text() == text
    ok(ctl("service", "client-a", "--uninstall", "--platform", platform, "--unit-dir", str(units), root=root))
    assert not written.exists()
    both = subprocess.run([sys.executable, str(HUBCTL), "service", "client-a", "--print", "--install",
                           "--root", str(root)], capture_output=True, text=True, env=clean_env())
    assert both.returncode != 0          # mutually exclusive


# ----------------------------------------------------------------------------- two real hubs

def test_both_hubs_serve_with_their_own_identity(live):
    root, a, b = live
    out = json.loads(ok(ctl("status", "--json", root=root)).stdout)
    by_name = {r["name"]: r for r in out}
    assert all(r["up"] for r in out)
    assert len(by_name["client-a"]["hubId"]) == 32 and len(by_name["personal"]["hubId"]) == 32
    assert by_name["client-a"]["hubId"] != by_name["personal"]["hubId"]
    assert by_name["client-a"]["label"] == "client-a" and by_name["personal"]["label"] == "personal"
    assert by_name["client-a"]["port"] != by_name["personal"]["port"]
    status, body = hubctl.signed_get(a.inst.url, "/hub", *a.credential())
    assert status == 200 and body["hubId"] == by_name["client-a"]["hubId"]
    assert "UP" in ok(ctl("status", "client-a", root=root)).stdout


def test_status_of_a_stopped_hub_says_down(pristine, tmp_path):
    root = clone_root(pristine, tmp_path / "hubs")
    done = ctl("status", "client-a", root=root)
    assert done.returncode != 0 and "down" in done.stdout


def test_check_isolation_passes_on_the_running_pair(live):
    root, _a, _b = live
    done = ok(ctl("check-isolation", root=root))
    assert "isolation ok: 2 hub(s)" in done.stdout
    assert "skipped" not in done.stdout       # both running, so the cross-credential check really ran


def test_a_credential_from_one_hub_is_refused_by_the_other_and_vice_versa(live):
    root, a, b = live
    for src, dst in ((a, b), (b, a)):
        actor, secret = src.credential()
        refused, _ = hubctl.signed_get(dst.inst.url, "/hub", actor, secret)
        assert refused == 401, f"{src.name}'s credential reached {dst.name}"
        accepted, _ = hubctl.signed_get(src.inst.url, "/hub", actor, secret)
        assert accepted == 200
    # a freshly minted actor on A is equally unknown to B
    done = ok(ctl("add-actor", "client-a", "dana", root=root))
    secret = json.loads(done.stdout)["secret"]
    assert hubctl.signed_get(b.inst.url, "/hub", "dana", secret)[0] == 401
    assert hubctl.signed_get(a.inst.url, "/hub", "dana", secret)[0] == 200      # keys.json is re-read per request
    assert hubctl.signed_get(b.inst.url, "/hub", "dana", "x" * 43)[0] == 401


def test_the_hub_writes_only_inside_its_own_instance_directory(live):
    root, a, b = live
    for hub in (a, b):
        db = Path(env_of(root, hub.name)["AGENTCO_DB"])
        assert db.stat().st_size > 0
        assert not (db.stat().st_mode & 0o077)
        assert (hub.inst.log_dir / "hub.log").exists()
    leaked = [p for p in (root / "client-a").rglob("*") if "personal" in p.name]
    assert not leaked


def test_backup_of_a_running_hub_is_consistent_and_private(live, tmp_path):
    root, a, b = live
    dest = tmp_path / "backup-a"
    ok(ctl("backup", "client-a", str(dest), root=root))
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700
    for name in ("registry.sqlite3", "keys.json", "hub.env"):
        assert stat.S_IMODE((dest / name).stat().st_mode) == 0o600
    conn = sqlite3.connect(dest / "registry.sqlite3")
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        uid = conn.execute("SELECT value FROM meta WHERE key = 'hub_uid'").fetchone()[0]
    finally:
        conn.close()
    assert uid == hubctl.read_hub_uid(a.inst)
    assert (dest / "keys.json").read_text() == (root / "client-a" / "keys.json").read_text()
    again = ctl("backup", "client-a", str(dest), root=root)
    assert again.returncode != 0 and "not empty" in again.stderr
    inside = ctl("backup", "client-a", str(root / "personal" / "bk"), root=root)
    assert inside.returncode != 0 and "inside hub" in inside.stderr


# ----------------------------------------------------------------------------- deliberately broken isolation

def fresh(pristine, tmp_path) -> Path:
    return clone_root(pristine, tmp_path / "hubs")


def violations(root: Path) -> str:
    done = ctl("check-isolation", root=root)
    assert done.returncode != 0, f"check-isolation passed on a broken layout: {done.stdout}"
    assert "VIOLATION" in done.stderr
    return done.stderr


def test_baseline_clean_copy_passes(pristine, tmp_path):
    assert ctl("check-isolation", root=fresh(pristine, tmp_path)).returncode == 0


def test_catches_a_shared_port(pristine, tmp_path):
    root = fresh(pristine, tmp_path)
    set_env(root, "personal", HUBCTL_PORT=env_of(root, "client-a")["HUBCTL_PORT"])
    assert "port: hubs 'client-a' and 'personal' both use port" in violations(root)


def test_catches_a_shared_sqlite_database(pristine, tmp_path):
    root = fresh(pristine, tmp_path)
    set_env(root, "personal", AGENTCO_DB=env_of(root, "client-a")["AGENTCO_DB"])
    assert "database: hubs 'client-a' and 'personal' share the database file" in violations(root)


def test_catches_a_database_shared_through_a_hard_link(pristine, tmp_path):
    root = fresh(pristine, tmp_path)
    mine = Path(env_of(root, "personal")["AGENTCO_DB"])
    mine.unlink()
    os.link(env_of(root, "client-a")["AGENTCO_DB"], mine)
    assert "same inode" in violations(root)


def test_catches_a_shared_postgres_database(pristine, tmp_path):
    root = fresh(pristine, tmp_path)
    set_env(root, "client-a", HUBCTL_BACKEND="postgres", AGENTCO_DB=pg_dsn("u1", "pw-a", "shared"))
    set_env(root, "personal", HUBCTL_BACKEND="postgres", AGENTCO_DB=pg_dsn("u2", "pw-b", "shared"))
    assert "share Postgres database 'shared'" in violations(root)


def test_catches_a_shared_postgres_password(pristine, tmp_path):
    root = fresh(pristine, tmp_path)
    set_env(root, "client-a", HUBCTL_BACKEND="postgres", AGENTCO_DB=pg_dsn("u1", "same-pw", "one"))
    set_env(root, "personal", HUBCTL_BACKEND="postgres", AGENTCO_DB=pg_dsn("u2", "same-pw", "two"))
    assert "secret: hubs 'client-a' and 'personal' share a key or secret value" in violations(root)


def test_catches_a_shared_key(pristine, tmp_path):
    root = fresh(pristine, tmp_path)
    (root / "personal" / "keys.json").write_text((root / "client-a" / "keys.json").read_text())
    assert "secret: hubs 'client-a' and 'personal' share a key or secret value" in violations(root)


def test_catches_a_shared_data_directory(pristine, tmp_path):
    root = fresh(pristine, tmp_path)
    set_env(root, "personal", HUBCTL_DATA_DIR=env_of(root, "client-a")["HUBCTL_DATA_DIR"])
    out = violations(root)
    assert "data directory: hubs 'client-a' and 'personal' share or nest data directories" in out
    assert "outside its own directory" in out


def test_catches_a_shared_log_directory(pristine, tmp_path):
    root = fresh(pristine, tmp_path)
    set_env(root, "personal", HUBCTL_LOG_DIR=env_of(root, "client-a")["HUBCTL_LOG_DIR"])
    assert "log directory: hubs 'client-a' and 'personal' share or nest log directories" in violations(root)


def test_catches_a_shared_store_file(pristine, tmp_path):
    root = fresh(pristine, tmp_path)
    set_env(root, "personal", AGENTCO_WORK_STORE=env_of(root, "client-a")["AGENTCO_WORK_STORE"])
    assert "share" in violations(root) and "AGENTCO_WORK_STORE" in violations(root)


def test_catches_a_shared_hub_label(pristine, tmp_path):
    root = fresh(pristine, tmp_path)
    set_env(root, "personal", AGENTCO_HUB_ID="client-a")
    assert "share the configured hub name 'client-a'" in violations(root)
    set_env(root, "personal", AGENTCO_HUB_ID="local")
    assert "the default every hub shares" in violations(root)


def test_catches_a_copied_database_that_carries_the_same_hub_id(pristine, tmp_path):
    root = fresh(pristine, tmp_path)
    for name in ("client-a", "personal"):
        conn = sqlite3.connect(env_of(root, name)["AGENTCO_DB"])
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('hub_uid', ?)", ("ab" * 16,))
        conn.commit()
        conn.close()
        os.chmod(env_of(root, name)["AGENTCO_DB"], 0o600)
    assert "carry the same generated hub id" in violations(root)


@pytest.mark.parametrize("what,target,mode", [
    ("keys file", "client-a/keys.json", 0o644),
    ("env file", "personal/hub.env", 0o640),
    ("instance directory", "personal", 0o755),
    ("data directory", "client-a/data", 0o750),
    ("database file", "client-a/data/registry.sqlite3", 0o644),
])
def test_catches_non_private_modes(pristine, tmp_path, what, target, mode):
    root = fresh(pristine, tmp_path)
    os.chmod(root / target, mode)
    out = violations(root)
    assert f"mode: {root / target} has mode {mode:04o}" in out, what


def test_catches_a_group_readable_root(pristine, tmp_path):
    root = fresh(pristine, tmp_path)
    os.chmod(root, 0o750)
    assert f"mode: {root} has mode 0750" in violations(root)


def test_catches_a_symlink_that_could_cross_the_wall(pristine, tmp_path):
    root = fresh(pristine, tmp_path)
    os.symlink(root / "client-a" / "keys.json", root / "personal" / "data" / "borrowed")
    assert "symlink:" in violations(root)


def test_catches_a_credential_one_hub_accepts_from_another(tmp_path):
    """Two running hubs whose key tables were (wrongly) made to agree: both the shared secret and the
    live cross-acceptance are reported."""
    root = tmp_path / "hubs"
    make_pair(root)
    shutil.copyfile(root / "client-a" / "keys.json", root / "personal" / "keys.json")
    os.chmod(root / "personal" / "keys.json", 0o600)
    hubs = []
    try:
        for name in ("client-a", "personal"):
            hubs.append(Running(root, name).start())
        out = violations(root)
        assert "secret: hubs 'client-a' and 'personal' share a key or secret value" in out
        assert "credential: 'operator' from hub 'client-a' was ACCEPTED by hub 'personal'" in out
        assert "credential: 'operator' from hub 'personal' was ACCEPTED by hub 'client-a'" in out
    finally:
        for hub in hubs:
            hub.stop()
