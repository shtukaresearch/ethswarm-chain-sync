# ethswarm_chain_sync

| file | description |
|---|---|
| `__init__.py` | Public API: `load`, `iter_rows` and `__version__`. |
| `cli.py` | Command line entry point with the `sync` and `status` subcommands. |
| `decode.py` | Decodes raw `eth_getLogs` entries into named, JSON-ready rows using `eth_abi`. |
| `deployments.py` | Resolves contract and event selectors into sync targets from the `ethswarm-deployments` cache. |
| `load.py` | Reads the cache back as rows, filtered by contract, event, address and network, in chain order. |
| `rpc.py` | JSON-RPC client protocol and HTTP implementation with short timeouts, retries and range-too-large detection. |
| `schema.py` | Builds per-deployment event schemas (signature, `topic0`, inputs, store key) from an ABI. |
| `store.py` | JSONLines store layout, per-event checkpoints, tail rewind on resume, and the per-deployment lock. |
| `sync.py` | Acquisition engine: concurrent range fetches committed in order, with range bisection and checkpointing. |
