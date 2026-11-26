# ethswarm-chain-sync

Syncs events from the Swarm storage-incentives contracts into a local JSONLines cache and loads them back as a list of objects. See `docs/data-model.md` for the row format, on-disk layout and usage.

| path | description |
|---|---|
| `.github/workflows/ci.yml` | Lint and tests on Python 3.11–3.14 for every PR and push to main. |
| `.github/workflows/publish.yml` | Builds and publishes to PyPI when a `vX.Y.Z` tag is pushed for a commit on main. |
| `.gitignore` | Ignores virtualenvs, caches, build output and the project-local deployments cache. |
| `plan.md` | The original project brief. |
| `pyproject.toml` | Package metadata, dependencies, the `ethswarm-chain-sync` entry point, and hatch/pytest/ruff configuration. |
| `uv.lock` | Locked dependency versions for uv. |
| `docs/` | Project documentation. |
| `src/` | Package source. |
| `tests/` | Pytest suite (offline, against a fake RPC). |
