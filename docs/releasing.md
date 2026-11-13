# Releasing

The procedure for the maintainer who tags a version. It is written for `1.0.0` and works for any
later `X.Y.Z`. The aim is that on the day, the only steps left are a version bump and a tag; the rest of
this page is why those are safe and what to check afterwards.

Version 1 is defined by [ADR 0011](decisions/0011-v1-definition.md): five gates, and gate 5 is this
page's output: a tagged version, a changelog, upgrade notes, a published container image, and the
supported `asop-spec` range stated.

## Preconditions

Do not start until every line is true.

1. **Gates 1 to 4 of ADR 0011 have passed.** Gate 2 includes the two-machine run of
   [`runbooks/first-signed-answer-loop.md`](runbooks/first-signed-answer-loop.md); its result is
   recorded where the ADR's status line says. Gate 5 is what this procedure completes.
2. **CI is green on `main`** at the commit you will tag: every job in `.github/workflows/ci.yml`
   (`leakguard`, `test` on 3.11 to 3.13, `typecheck`, `contract`, `postgres`, `image`). CI does not run
   on a tag, so the run on `main` is the evidence for the tagged commit.
3. **The multi-hub rig is green** on that commit (`.github/workflows/network.yml`, run on demand if the
   last run is older than the commit).
4. **`docs/known-issues.md` has no open defect** that loses work, reports wrong, or crosses parties, and
   every accepted residue has a test.
5. **The release workflow is on `main`:** `.github/workflows/release-image.yml`. A tag only runs the
   workflow as it exists at the tagged commit.
6. **`CHANGELOG.md` has an `## [Unreleased]` section** that matches what is on `main`, and
   [`upgrading.md`](upgrading.md) lists every migration (the count in `agentco/migrations.py`,
   `migrations.MIGRATIONS[-1].version`, equals the number it states).

## 1. Prepare the release commit

One pull request, titled `release: 1.0.0`, containing only these edits:

**The version string lives in these places.** Change all of them to the new version:

| File | What to change |
|---|---|
| `pyproject.toml` | `[project] version = "0.1.0"` |
| `agentco/__init__.py` | `__version__ = "0.1.0"` |
| `uv.lock` | the `agentco` package entry's `version`; run `uv lock` rather than editing by hand |
| `README.md` | the **Status** paragraph that says the package version is `0.1.0` and that no release has been tagged |
| `docs/roadmap.md` | the opening **Version 1** paragraph ("still pre-release (`0.1.0`, no tag)") |
| `docs/decisions/0011-v1-definition.md` | the status line: gate 5 passed |

Also decide, as a maintainer, whether `Development Status :: 3 - Alpha` in `pyproject.toml` should
change with this release. Nothing else in the code reads the version string: the hub reports its own
version from the installed package metadata (`agentco/wire.py`), so it follows `pyproject.toml`.
`docker-compose.yml` builds locally as `agentco:local` and does not change.

**Move the changelog.** In `CHANGELOG.md`, rename `## [Unreleased] — to be 1.0.0` to
`## [1.0.0] — YYYY-MM-DD` using the date you will tag, and put a new, empty `## [Unreleased]` above it.
Read the "Breaking changes" block once more against `main`; a line that was true when it was written
may not be.

Then run, from the branch:

```bash
uv lock
uv run --extra server --extra mcp --extra envelope --extra dev pytest -q -p no:cacheprovider
AGENTCO_TEST_PG=postgresql://... uv run --extra server --extra mcp --extra envelope --extra dev --extra postgres pytest -q -p no:cacheprovider
uv run --extra server --extra mcp --extra postgres --extra envelope --with pyright==1.1.414 pyright
uv run python tools/leakguard/leakguard.py --root .
```

and confirm the version the package now reports:
`uv run python -c "import agentco, importlib.metadata as m; print(agentco.__version__, m.version('agentco'))"`
prints the new version twice. The release workflow refuses a tag that disagrees with `pyproject.toml`,
so this is where a mismatch is cheapest to find. Merge the pull request when CI is green.

## 2. Tag

Tag the merge commit on `main`, exactly as it was tested:

```bash
git fetch origin
git switch main && git pull --ff-only
git log -1 --oneline            # the release commit, and CI green on it
git tag -a v1.0.0 -m "1.0.0"
git push origin v1.0.0
```

The tag name is `v` plus the version in `pyproject.toml`. It must match `v*.*.*`, or the image workflow
does not run. A tag with a hyphen (`v1.0.0-rc1`) publishes the version tag only and does not move
`latest`.

## 3. What CI does on the tag

Pushing the tag starts one workflow, **Release image** (`.github/workflows/release-image.yml`), and
nothing else. It runs on tags only, never on branches or pull requests. In order:

1. Checks that the tag equals `v` plus the version in `pyproject.toml`, and stops if not.
2. Builds the `Dockerfile` for `linux/amd64` and `linux/arm64`.
3. Logs in to `ghcr.io` with the run's `GITHUB_TOKEN` (the job holds `packages: write`; no secret is
   configured or read).
4. Pushes `ghcr.io/<owner>/<repository>:1.0.0` and `:latest`, and prints the manifest.

The image name is the repository's own, lowercase: for this repository
`ghcr.io/agentic-co/agentic-co-hub`. Watch it with `gh run watch` or the Actions tab.

## 4. Verify what was published

```bash
IMAGE=ghcr.io/agentic-co/agentic-co-hub
docker buildx imagetools inspect "$IMAGE:1.0.0"        # linux/amd64 and linux/arm64 both listed
docker buildx imagetools inspect "$IMAGE:latest"       # same digest as 1.0.0
docker run --rm --entrypoint python "$IMAGE:1.0.0" -c \
  "import importlib.metadata as m; print(m.version('agentco'), m.version('asop-spec'))"
```

That last command prints one line: `1.0.0` and then the locked `asop-spec` version (`uv.lock`). Then run the container the way the `image` job in CI does and check that it refuses a stranger:

```bash
docker run -d --name agentco-release-check -p 8787:8787 \
  -e AGENTCO_REGISTRY_DB=/tmp/registry.sqlite3 -e AGENTCO_WORK_STORE=/tmp/work.jsonl \
  -e AGENTCO_SOP_STORE=/tmp/sops.jsonl "$IMAGE:1.0.0" serve --host 0.0.0.0 --port 8787
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/events    # 401; a 200 is a failure
docker rm -f agentco-release-check
```

Check the package page under the repository's **Packages**: the image is linked to the repository and
is **public**. A package created by a workflow can start private; if an anonymous `docker pull` is refused,
set its visibility to public in the package settings once.

Finally, create the GitHub release from the changelog section, so the notes are the same text:

```bash
gh release create v1.0.0 --title "1.0.0" --notes-file <the 1.0.0 section of CHANGELOG.md>
```

## If the publish fails

The workflow is only exercised by a real tag, so a failure on the first one is possible. Nothing is
published until the last build step, which makes most failures safe to retry.

- **The version check failed** (tag and `pyproject.toml` disagree): nothing was built. Delete the tag
  (`git push origin :refs/tags/v1.0.0` and `git tag -d v1.0.0`), correct the version through a pull
  request, and tag again. This is safe only because no image exists under that name.
- **Login or push refused (403)**: the job's token lacks write access to the package. Check the
  repository's *Settings, Actions, General, Workflow permissions* does not forbid the job-level
  `packages: write`, and, if a package of this name already exists, that the repository has write access
  under the package's *Manage Actions access*. Then re-run the failed job: `gh run rerun <run-id> --failed`.
- **The build failed** (a base-image or dependency problem, or the arm64 emulation timed out): re-run
  the failed job once. If it fails the same way, reproduce locally with
  `docker buildx build --platform linux/amd64,linux/arm64 .`, fix through a pull request, and tag the
  next patch version instead of moving the tag.
- **Only one tag was pushed** (for example `1.0.0` but not `latest`): re-run the job; pushing the same
  digest again is harmless.
- **A wrong image was published** (wrong contents under a version people can pull): do not move or
  reuse the tag. Delete that version in the package settings, mark the release as superseded in
  `CHANGELOG.md`, and publish the fix as the next patch version. A registry tag is something other
  people have pulled by name.

Not part of this procedure: publishing the Python package to PyPI. Gate 5 asks for a container image,
and `.github/workflows/release-image.yml` does not publish a wheel.
