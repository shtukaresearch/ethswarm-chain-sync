# tests

| file | description |
|---|---|
| `conftest.py` | Synthetic ABI, deterministic fake chain and RPC, target factory and store snapshot helper. |
| `test_decode.py` | Schema derivation, `topic0` hashing, and decoding of named fields, structs, arrays and indexed values. |
| `test_load.py` | The `load` / `iter_rows` read path: ordering, filters, timestamps, and ignoring uncheckpointed tails. |
| `test_selection_and_rpc.py` | Target selection rules against a fake deployments manager, and HTTP RPC retry and split behaviour. |
| `test_sync.py` | Resumability: interrupted, torn, incremental, subset-first and bisected syncs all equal one clean sync. |
