# ethswarm-chain-sync data model

ethswarm-chain-sync syncs events emitted by the Swarm storage-incentives contracts (every deployed version, on Gnosis "mainnet" and Sepolia "testnet") into a local JSONLines cache, and loads that cache back as a list of objects. It follows the acquisition layer of the VolumeRegistry indexer (`swarm-volume-registry/services/indexer`): a finalized-only, event-sourced log cache, partitioned per event type, with web3 confined to the acquisition and decode step. It has no projector.

## Sources

Deployments (address, deployment block, ABI) come from the `ethswarm-deployments` package's user-local cache. A sync target is one distinct contract address. Each target's event schemas come from that address's own ABI, so a log is decoded by `(address, topic0)`, never by event name alone. This disambiguates names shared across contracts (`Paused`, `RoleGranted`, `PriceUpdate`) and across versions of one contract (`StakeUpdated` has three signatures; the field order of the `WinnerSelected` struct changed in v0.6.0).

Acquisition reads `eth_getLogs` up to the chain's `finalized` block only, so the cache never needs reorg handling.

## Row

One row per log:

| field | type | meaning |
|---|---|---|
| `chain_id` | int | 100 (Gnosis) or 11155111 (Sepolia) |
| `contract` | str | canonical contract name, e.g. `Redistribution` |
| `version` | str | first storage-incentives release containing this address |
| `address` | str | emitting contract, lowercase hex |
| `event` | str | event key: the event name, or its full signature if the ABI overloads the name |
| `signature` | str | canonical signature, e.g. `Committed(uint256,bytes32,uint8)` |
| `block_number` | int | |
| `block_timestamp` | str | UTC ISO 8601 (`...Z`), from the log's `blockTimestamp`; `load()` returns a timezone-aware `datetime` |
| `transaction_hash` | str | lowercase hex |
| `transaction_index` | int | |
| `log_index` | int | `(block_number, log_index)` is chain order |
| `args` | object | decoded event arguments keyed by ABI parameter name |

Decoding of `args` is plain ABI decoding, with names attached. Addresses are lowercase hex, `bytes`/`bytesN` are `0x` hex, integers are exact JSON integers (uint256 values are not rounded), and structs are objects keyed by component name, recursively and inside arrays. An indexed dynamic value (string, bytes, array, struct) only appears in the log as its keccak hash, and is stored as that hash. The Swarm contracts have no enums, so no value mapping is applied.

## On disk

```
<store-dir>/                              --store-dir, $ETHSWARM_CHAIN_SYNC_STORE, or ~/.cache/ethswarm-chain-sync
  <chain_id>/
    <Contract>_<address>/                 one directory per deployed contract
      <event>.jsonl                       one append-only file per event, rows in chain order
      head.json                           {"events": {"<event>": {"block": N, "size": B}}}
```

Each event has its own checkpoint: the last block synced (inclusive) and the byte size of its file at that block. Syncing a subset of events and later a superset therefore works, and each event resumes from its own checkpoint. On first sync an event starts at the contract's deployment block.

Each block range is appended and fsynced before `head.json` is atomically replaced. A sync that is interrupted at any point leaves each file as its checkpointed prefix plus, possibly, an uncheckpointed tail, which may include a torn final line. The next sync truncates each file back to its checkpointed size before resuming, so the files are exactly-once (no duplicate or partial rows), and an interrupted and resumed sync produces the same bytes as an uninterrupted one. The loader reads each file only up to its checkpointed size, so it is safe to load while a sync is running.

The store is a cache. Deleting a contract's directory makes the next sync rebuild it from the deployment block.

## Usage

```
ethswarm-chain-sync sync                                   # latest release's contracts, mainnet
ethswarm-chain-sync sync -c v0.6.0                         # every contract in release v0.6.0
ethswarm-chain-sync sync -c all                            # every deployment of every release
ethswarm-chain-sync sync -c PostageStamp -e BatchCreated   # one event, every PostageStamp deployment
ethswarm-chain-sync sync -c Redistribution@v0.9.4          # one contract as of one release
ethswarm-chain-sync sync -n testnet -c 0x...               # by address, on Sepolia
ethswarm-chain-sync status -c StakeRegistry                # checkpoints, no RPC
```

Contract selectors (`-c`, repeatable) are `latest` (the default), a release `vX.Y.Z`, `all`, a contract `Name`, `Name@vX.Y.Z` or `Name@latest`, or an address. A release selects the contracts in that release's manifest, including contracts carried over from an earlier release (at v0.9.4, PostageStamp is still its v0.6.0 deployment). Contracts are labelled with the release they first appeared in. `latest` is the newest release by numeric version order. The release-wide selectors (`latest`, `vX.Y.Z`, `all`) leave out `Token`/`TestToken`, which are synced only when named or given by address. Event selectors (`-e`) narrow the selected contracts to the named events, so `-e Committed` alone covers only the latest Redistribution, and `-c all -e Committed` covers every version.

The RPC endpoint is `--rpc-url`, else `$GNO_RPC_URL` (mainnet) or `$SEP_RPC_URL` (testnet).

```python
import ethswarm_chain_sync as ecs

rows = ecs.load("PostageStamp", "BatchCreated")           # list[dict], chain order
rows = ecs.load(event=["Committed", "Revealed"], network="testnet")
```
