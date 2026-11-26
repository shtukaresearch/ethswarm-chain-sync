"""Read the local cache back as a list of row objects.

Loading needs neither web3 nor the deployments package: rows are self-describing and
the directory layout (``store`` module) is enough to find them. Each event file is read
only up to its checkpointed size, so loading is consistent while a sync is running and
never sees a torn line.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Any

from . import store

#: ``ethswarm-deployments`` network names -> chain ids, for selecting by network.
NETWORK_CHAIN_IDS = {"mainnet": 100, "testnet": 11155111}


def _as_set(value: str | Iterable[str] | None) -> set[str] | None:
    if value is None:
        return None
    return {value} if isinstance(value, str) else set(value)


def iter_rows(
    contract: str | Iterable[str] | None = None,
    event: str | Iterable[str] | None = None,
    *,
    network: str | int = "mainnet",
    address: str | Iterable[str] | None = None,
    store_dir: str | os.PathLike[str] | None = None,
    parse_timestamps: bool = True,
) -> Iterator[dict[str, Any]]:
    """Yield cached rows, file by file, unordered across files.

    Filters (each ``None`` = no filter; a string or an iterable of strings):

    - ``contract``: contract name(s), e.g. ``"Redistribution"``;
    - ``event``: event key(s), name or (for overloaded names) full signature;
    - ``address``: emitting contract address(es);
    - ``network``: ``"mainnet"`` / ``"testnet"`` or a chain id.

    With ``parse_timestamps`` each row's ``block_timestamp`` is a timezone-aware UTC
    :class:`~datetime.datetime`; otherwise the ISO 8601 string stored on disk.
    """
    chain_id = NETWORK_CHAIN_IDS.get(network, network) if isinstance(network, str) else network
    chain_dir = store.resolve_store_dir(store_dir) / str(chain_id)
    contracts, events = _as_set(contract), _as_set(event)
    addresses = {a.lower() for a in _as_set(address) or ()} or None
    if not chain_dir.is_dir():
        return
    for dep_dir in sorted(p for p in chain_dir.iterdir() if p.is_dir()):
        name, _, addr = dep_dir.name.rpartition("_")
        if contracts is not None and name not in contracts:
            continue
        if addresses is not None and addr not in addresses:
            continue
        for key, cp in sorted(store.load_checkpoints(dep_dir).items()):
            if events is not None and key not in events:
                continue
            path = store.event_path(dep_dir, key)
            if not path.exists():
                continue
            for row in store.iter_event_file(path, limit=cp.size):
                if parse_timestamps:
                    row["block_timestamp"] = datetime.fromisoformat(
                        row["block_timestamp"].replace("Z", "+00:00")
                    )
                yield row


def load(
    contract: str | Iterable[str] | None = None,
    event: str | Iterable[str] | None = None,
    *,
    network: str | int = "mainnet",
    address: str | Iterable[str] | None = None,
    store_dir: str | os.PathLike[str] | None = None,
    parse_timestamps: bool = True,
) -> list[dict[str, Any]]:
    """Load cached rows as a list in chain order.

    Same filters as :func:`iter_rows`. Rows are sorted by
    ``(block_number, log_index, address)``, which is chain order across every contract
    and event loaded (the address tie-break only matters across chains, never within one).
    """
    rows = list(
        iter_rows(
            contract,
            event,
            network=network,
            address=address,
            store_dir=store_dir,
            parse_timestamps=parse_timestamps,
        )
    )
    rows.sort(key=lambda r: (r["block_number"], r["log_index"], r["address"]))
    return rows
