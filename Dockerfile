# The HTTP registry, containerised. One process, one SQLite file on a volume.
#
# Two things about this image are deliberate and easy to "fix" wrongly:
#
#   * `git` is installed. `snapshots.resolve_git` shells out to `git rev-parse`
#     to read a pointer's version token, so a slim image without it turns every
#     `git:` snapshot into an unresolvable one — recorded, but permanently
#     unable to report divergence. The repos themselves still have to be
#     mounted in; the resolver reads a path, and a path that does not exist in
#     this filesystem is not a path this container can resolve.
#
#   * The server binds 0.0.0.0 IN THE CONTAINER, which is not the same act as
#     exposing it. A container's loopback reaches nothing, so binding it there
#     would make the service unreachable by design rather than private. The
#     "deliberate act" the CLI's default protects is publishing the port, and
#     that decision stays in compose (`127.0.0.1:8787:8787`) where a reader can
#     see it.

FROM python:3.12-slim AS base

# git: required by the git: resolver (see above).
# No curl/wget: the healthcheck below uses the interpreter that is already here.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first — the manifest changes far less often than the code,
# so an ordinary source edit does not reinstall FastAPI.
#
# `agentco-asop` is a workspace member, not a release on an index. `pip` reads
# `[project] dependencies` and nothing else: `[tool.uv.sources]` is uv's, so
# pip resolves the name against PyPI, finds nothing, and the build dies on this
# layer. Installing the contract package from the tree first satisfies the
# requirement by the only route pip has. It is copied whole because hatchling
# builds it from its own pyproject and README.
COPY pyproject.toml README.md ./
COPY agentco/__init__.py agentco/__init__.py
COPY packages ./packages
RUN pip install --no-cache-dir ./packages/asop \
 && pip install --no-cache-dir ".[server,mcp]"

COPY agentco ./agentco
COPY tools ./tools
RUN pip install --no-cache-dir --no-deps .

# State lives on a volume, never in the image layer. All three stores are
# named explicitly rather than left to the CLI's cwd-relative defaults, which
# would put a registry file inside the container and lose it on the next
# `docker run`.
ENV AGENTCO_REGISTRY_DB=/var/lib/agentco/registry.sqlite3 \
    AGENTCO_WORK_STORE=/var/lib/agentco/work.jsonl \
    AGENTCO_SOP_STORE=/var/lib/agentco/sops.jsonl \
    AGENTCO_REGISTRY_KEYS=/run/secrets/agentco-keys.json

# Non-root, and the state directory is owned by it. `keygen` output is mounted
# in read-only at runtime — no secret is ever built into a layer, because an
# image layer is a copyable artifact and a shared secret in one is a shared
# secret in everyone's registry cache.
RUN useradd --system --uid 10001 --create-home agentco \
 && mkdir -p /var/lib/agentco \
 && chown -R agentco:agentco /var/lib/agentco
USER agentco
VOLUME ["/var/lib/agentco"]

EXPOSE 8787

# An unauthenticated request MUST be refused, so a 401 is the healthy answer:
# it proves the app is serving AND that the key table loaded closed rather than
# open. A 200 here would mean the registry is accepting anonymous writes, which
# is a failure the check would otherwise report as health.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python3", "/app/tools/docker/healthcheck.py"]

# Single process on purpose. `serve` takes no --workers and must not grow one:
# conflict detection reads the live leases and writes the new one in separate
# transactions, so two server processes against one file can each miss the
# other's claim and both report zero conflicts — the exact blindness the
# registry exists to remove. Scale is not what this stage is for.
ENTRYPOINT ["python3", "-m", "agentco"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8787"]
