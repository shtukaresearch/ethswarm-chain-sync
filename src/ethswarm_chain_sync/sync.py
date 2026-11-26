"""The acquisition engine: fetch, decode and checkpoint one deployment's events.

Contract of :func:`sync_target`:

- **Range.** Each selected event is synced over ``(checkpoint, to_block]``, or
  ``[start_block, to_block]`` if it has no checkpoint. ``to_block`` is normally the
  chain's ``finalized`` block, so nothing synced can be reorged away and there is no
  reorg handling.
- **Grouping.** Selected events that resume from the same block are fetched together
  with one topic-filtered ``eth_getLogs`` per block range (``topics=[[t0, t1, …]]``).
  After one complete sync every event of a deployment shares a checkpoint, so the
  steady state is a single group.
- **Ordering and resumability.** Block ranges are fetched concurrently but committed
  strictly in ascending order: a range's rows are appended and flushed, then the group's
  checkpoints are advanced to the range's end. Interrupting at any point therefore loses
  at most the uncommitted ranges, and the next run :func:`~ethswarm_chain_sync.store.rewind`\\ s
  any uncheckpointed tail and resumes. An interrupted-then-resumed sync produces a store
  byte-identical to an uninterrupted one.
- **Range splitting.** A range the node refuses as too large is bisected until it is
  accepted (a single-block refusal is an error).
- **Exclusivity.** A sync holds an exclusive lock on the deployment directory; a second
  concurrent sync of the same deployment fails immediately with
  :class:`~ethswarm_chain_sync.store.StoreLockedError`.
- **Strictness.** A log that cannot be decoded against the deployment's own ABI aborts
  the sync before anything from its range is written.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import store
from .decode import decode_log, quantity
from .deployments import Target
from .rpc import RangeTooLargeError, RpcClient

#: Default blocks per ``eth_getLogs`` (inclusive span); 10,000 is a common node cap.
DEFAULT_CHUNK_SIZE = 10_000
DEFAULT_WORKERS = 32

#: ``progress(target, block_reached, to_block, rows_written_so_far)``
ProgressFn = Callable[[Target, int, int, int], None]


@dataclass
class SyncResult:
    """Outcome of syncing one target.

    Fields:
        target: The target synced.
        to_block: The block every selected event is now synced to.
        rows: Rows appended per event key during this run.
    """

    target: Target
    to_block: int
    rows: dict[str, int]


def block_ranges(start: int, end: int, size: int) -> Iterator[tuple[int, int]]:
    """Inclusive ``(a, b)`` ranges of at most ``size`` blocks covering ``[start, end]``."""
    a = start
    while a <= end:
        b = min(a + size - 1, end)
        yield a, b
        a = b + 1


def fetch_range(
    rpc: RpcClient, address: str, topics: list[Any], a: int, b: int
) -> list[dict[str, Any]]:
    """All logs in ``[a, b]``, bisecting the range whenever the node refuses it as too large."""
    try:
        return rpc.get_logs(address=address, topics=topics, from_block=a, to_block=b)
    except RangeTooLargeError:
        if a == b:
            raise
        mid = (a + b) // 2
        return fetch_range(rpc, address, topics, a, mid) + fetch_range(
            rpc, address, topics, mid + 1, b
        )


def _ordered_fetch(
    rpc: RpcClient,
    address: str,
    topics: list[Any],
    ranges: Iterator[tuple[int, int]],
    workers: int,
) -> Iterator[tuple[int, int, list[dict[str, Any]]]]:
    """Fetch ranges on a thread pool, yielding ``(a, b, logs)`` in ascending range order.

    At most ``2 * workers`` ranges are in flight. On early exit (exception, interrupt or
    the consumer stopping) queued fetches are cancelled.
    """
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="getlogs")
    pending: deque[tuple[int, int, Future]] = deque()

    def submit_next() -> None:
        nxt = next(ranges, None)
        if nxt is not None:
            pending.append((*nxt, pool.submit(fetch_range, rpc, address, topics, *nxt)))

    try:
        for _ in range(2 * workers):
            submit_next()
        while pending:
            a, b, fut = pending.popleft()
            logs = fut.result()
            submit_next()
            yield a, b, logs
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def sync_target(
    rpc: RpcClient,
    store_dir: str | Path,
    target: Target,
    *,
    to_block: int,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    workers: int = DEFAULT_WORKERS,
    progress: ProgressFn | None = None,
) -> SyncResult:
    """Sync ``target``'s selected events up to ``to_block`` (contract in module docstring)."""
    dep_dir = store.deployment_dir(store_dir, target.chain_id, target.contract, target.address)
    with store.locked(dep_dir):
        return _sync_locked(rpc, dep_dir, target, to_block, chunk_size, workers, progress)


def _sync_locked(
    rpc: RpcClient,
    dep_dir: Path,
    target: Target,
    to_block: int,
    chunk_size: int,
    workers: int,
    progress: ProgressFn | None,
) -> SyncResult:
    """Body of :func:`sync_target`, run while holding the deployment directory lock."""
    checkpoints = store.load_checkpoints(dep_dir)
    sizes: dict[str, int] = {}
    groups: dict[int, list[str]] = {}
    for schema in target.selected_schemas():
        cp = checkpoints.get(schema.key)
        sizes[schema.key] = store.rewind(dep_dir, schema.key, cp)
        start = cp.block + 1 if cp else target.start_block
        groups.setdefault(start, []).append(schema.key)

    by_key = {s.key: s for s in target.schemas.values()}
    written = {k: 0 for k in sizes}
    for start in sorted(groups):
        keys = groups[start]
        if start > to_block:
            continue
        topics = [[by_key[k].topic0 for k in keys]]
        ranges = block_ranges(start, to_block, chunk_size)
        for _, b, logs in _ordered_fetch(rpc, target.address, topics, ranges, workers):
            rows_by_key: dict[str, list[dict[str, Any]]] = {k: [] for k in keys}
            logs = [log for log in logs if not log.get("removed", False)]
            logs.sort(key=lambda log: (quantity(log["blockNumber"]), quantity(log["logIndex"])))
            for log in logs:
                row = decode_log(
                    log,
                    target.schemas,
                    chain_id=target.chain_id,
                    contract=target.contract,
                    version=target.version,
                )
                if row["address"] != target.address or row["event"] not in rows_by_key:
                    raise ValueError(f"node returned an unrequested log: {log}")
                rows_by_key[row["event"]].append(row)
            for k, rows in rows_by_key.items():
                if rows:
                    sizes[k] = store.append_rows(dep_dir, k, rows)
                    written[k] += len(rows)
                checkpoints[k] = store.Checkpoint(block=b, size=sizes[k])
            store.save_checkpoints(dep_dir, checkpoints)
            if progress is not None:
                progress(target, b, to_block, sum(written.values()))
    return SyncResult(target=target, to_block=to_block, rows=written)
