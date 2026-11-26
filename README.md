# ethswarm-chain-sync

Syncs events from the [Swarm storage-incentives](https://github.com/ethersphere/storage-incentives) contracts (PostageStamp, PriceOracle, Redistribution, StakeRegistry and the BZZ token, across every deployed version on Gnosis Chain and Sepolia) into a local JSONLines cache, and loads them back as a list of objects.

The purpose is to give analysis code a complete, decoded event history of the Swarm contracts without each project re-implementing log acquisition. A command line tool fetches events up to the chain's finalized block, decodes them against the ABI of the specific deployment that emitted them, and checkpoints progress so that an interrupted sync resumes exactly where it stopped. Imported as a Python package, it reads the cache back with no RPC or web3 dependency at read time. Deployment addresses and ABIs come from [ethswarm-deployments](https://github.com/shtukaresearch/ethswarm-deployments).

See `docs/data-model.md` for the row format, on-disk layout and usage.

| path | description |
|---|---|
| `.github/workflows/ci.yml` | Lint and tests on Linux and macOS, Python 3.11–3.14, for every PR and push to main. |
| `.github/workflows/publish.yml` | Builds and publishes to PyPI when a `vX.Y.Z` tag is pushed for a commit on main. |
| `.gitignore` | Ignores virtualenvs, caches, build output and the project-local deployments cache. |
| `pyproject.toml` | Package metadata, dependencies, the `ethswarm-chain-sync` entry point, and hatch/pytest/ruff configuration. |
| `uv.lock` | Locked dependency versions for uv. |
| `docs/` | Project documentation. |
| `src/` | Package source. |
| `tests/` | Pytest suite (offline, against a fake RPC). |
