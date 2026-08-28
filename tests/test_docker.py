"""The container image, as claims the suite can check without a Docker daemon.

A Dockerfile is configuration that nothing else in this repo would notice
breaking: it is not imported, not typed, and CI does not build it. So the
things about it that are load-bearing — that no secret is baked into a layer,
that state is on a volume rather than in the image, that the env var names it
sets are the ones the code actually reads, that the published port stays on
loopback — are asserted here instead, against the file's bytes.

None of these need a daemon, which is the point: a check that only runs where
Docker is installed is a check that does not run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
DOCKERIGNORE = ROOT / ".dockerignore"


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def test_the_image_files_exist():
    for path in (DOCKERFILE, COMPOSE, DOCKERIGNORE):
        assert path.exists(), f"{path.name} is missing"


def test_every_agentco_env_var_the_image_sets_is_one_the_code_reads(dockerfile):
    """A typo'd env var name is silent: the image sets it, nothing reads it, and
    the CLI falls back to a cwd-relative default INSIDE the container — so the
    registry file lands in a layer and vanishes on the next `docker run`. The
    failure looks like data loss, not like a misspelling.
    """
    from agentco import app, delivery, hook, mcp_server

    known = {
        app.DB_ENV_VAR,
        app.OPERATOR_ENV_VAR,
        hook.WORK_STORE_ENV_VAR,
        hook.SOP_STORE_ENV_VAR,
        hook.ACTOR_ENV_VAR,
        mcp_server.ACTOR_ENV_VAR,
        delivery.WEBHOOK_ENV_VAR,
        "AGENTCO_REGISTRY_KEYS",  # auth.KEYS_ENV_VAR
    }
    from agentco import auth

    assert auth.KEYS_ENV_VAR in known

    named = set(re.findall(r"\b(AGENTCO_[A-Z_]+)\b", dockerfile))
    unknown = named - known
    assert not unknown, f"the Dockerfile sets env vars nothing reads: {sorted(unknown)}"


def test_all_three_stores_are_pinned_to_the_volume_path(dockerfile):
    """Left to their defaults, the DB/work/SOP stores are cwd-relative — which
    inside a container means the image layer, not the volume."""
    for var in ("AGENTCO_REGISTRY_DB", "AGENTCO_WORK_STORE", "AGENTCO_SOP_STORE"):
        match = re.search(rf"{var}=(\S+)", dockerfile)
        assert match, f"{var} is not set in the image"
        assert match.group(1).startswith("/var/lib/agentco/"), (
            f"{var} points at {match.group(1)}, which is not on the declared volume"
        )
    assert 'VOLUME ["/var/lib/agentco"]' in dockerfile


def test_no_secret_is_baked_into_a_layer(dockerfile, compose):
    """An image layer is a copyable artifact. A shared secret in one is a shared
    secret in every cache that ever pulled it, and `docker history` shows ENV.
    """
    # Comments are stripped first: this file explains at length WHY the key
    # file is a runtime mount, and a check that cannot tell an explanation from
    # an instruction fails on its own documentation.
    instructions = "\n".join(
        line for line in dockerfile.splitlines() if line.strip() and not line.lstrip().startswith("#")
    )
    assert not re.search(r"^\s*ENV\s+.*SECRET", instructions, re.MULTILINE | re.IGNORECASE)
    assert "keygen" not in instructions, "the image must not mint or hold keys at build time"
    # The key file arrives as a read-only mount, at runtime.
    assert ":ro" in compose
    assert "/run/secrets/agentco-keys.json" in compose


def test_the_container_does_not_run_as_root(dockerfile):
    users = re.findall(r"^\s*USER\s+(\S+)", dockerfile, re.MULTILINE)
    assert users, "no USER instruction — the service would run as root"
    assert users[-1] != "root"


def test_git_is_installed_because_the_git_resolver_shells_out_to_it(dockerfile):
    """`snapshots.resolve_git` runs `git rev-parse`. Without git in the image
    every `git:` pointer is recorded as unresolvable — it can never report
    divergence, which is the silent half of the feature.
    """
    import inspect

    from agentco import snapshots

    source = inspect.getsource(snapshots.resolve_git)
    assert '"git"' in source, "resolve_git no longer shells out — this test's premise is stale"
    assert re.search(r"apt-get install[^\n]*\bgit\b", dockerfile)


def test_the_published_port_stays_on_loopback(compose):
    """Per-actor shared secrets over plain HTTP. On a routable interface those
    cross the network in the clear, so exposure has to be an edit somebody makes
    on purpose rather than the default they inherited.
    """
    published = re.findall(r'^\s*-\s*"([^"]+:\d+:\d+)"', compose, re.MULTILINE)
    assert published, "no published port found in the compose file"
    for mapping in published:
        assert mapping.startswith("127.0.0.1:"), f"{mapping} publishes off loopback"


def test_the_build_context_excludes_state_and_secrets():
    ignored = DOCKERIGNORE.read_text(encoding="utf-8").split()
    for pattern in ("keys.json", "*.sqlite3", "work.jsonl", ".git"):
        assert pattern in ignored, f"{pattern} is not excluded from the build context"


def test_the_healthcheck_treats_a_200_as_unhealthy():
    """401 is the healthy answer here. A 200 to an unauthenticated request means
    `load_keys` failed OPEN, and a check that reported that as healthy would be
    wrong about the one property most worth knowing.
    """
    source = (ROOT / "tools" / "docker" / "healthcheck.py").read_text(encoding="utf-8")
    assert "401" in source
    assert "exc.code == 401" in source
    # The success path of urlopen must be a FAILURE of the check.
    body = source.split("with urllib.request.urlopen", 1)[1]
    assert "return 1" in body.split("except", 1)[0]


def test_serve_takes_no_workers_flag(dockerfile):
    """The Dockerfile's comment says single-process on purpose. If `serve` ever
    grows `--workers`, that comment becomes a lie and two processes against one
    SQLite file can each miss the other's claim — so the claim is pinned here.
    """
    from agentco.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["serve", "--workers", "4"])
    assert "--workers" not in dockerfile.split("ENTRYPOINT")[-1]
