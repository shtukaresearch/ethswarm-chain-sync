"""Shared fixtures: a small synthetic contract ABI, a fake RPC and a target built from them.

The fake chain is deterministic: event logs are generated from block numbers alone, so
any two syncs over the same range see exactly the same logs, which lets tests compare
store directories byte for byte.
"""

from __future__ import annotations

from typing import Any

import pytest
from eth_abi import encode

from ethswarm_chain_sync.deployments import Target
from ethswarm_chain_sync.rpc import RangeTooLargeError
from ethswarm_chain_sync.schema import EventSchema, canonical_type, event_schemas

ADDRESS = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20

ABI: list[dict[str, Any]] = [
    {
        "type": "event",
        "name": "BatchCreated",
        "anonymous": False,
        "inputs": [
            {"name": "batchId", "type": "bytes32", "indexed": True},
            {"name": "totalAmount", "type": "uint256", "indexed": False},
            {"name": "owner", "type": "address", "indexed": False},
            {"name": "immutableFlag", "type": "bool", "indexed": False},
        ],
    },
    {
        "type": "event",
        "name": "WinnerSelected",
        "anonymous": False,
        "inputs": [
            {
                "name": "winner",
                "type": "tuple",
                "indexed": False,
                "components": [
                    {"name": "overlay", "type": "bytes32"},
                    {"name": "owner", "type": "address"},
                    {"name": "stake", "type": "uint256"},
                ],
            }
        ],
    },
    {
        "type": "event",
        "name": "PriceUpdate",
        "anonymous": False,
        "inputs": [{"name": "price", "type": "uint256", "indexed": False}],
    },
]


def encode_log(
    schema: EventSchema,
    values: dict[str, Any],
    *,
    address: str = ADDRESS,
    block: int,
    log_index: int,
    tx_index: int = 0,
) -> dict[str, Any]:
    """Build a raw ``eth_getLogs`` entry for ``schema`` with argument ``values``.

    Indexed static values are ABI-encoded into topics; non-indexed ones into ``data``.
    Struct values are given as tuples, as ``eth_abi`` expects.
    """
    topics = [schema.topic0]
    plain_types, plain_values = [], []
    for p in schema.inputs:
        if p.get("indexed"):
            topics.append("0x" + encode([canonical_type(p)], [values[p["name"]]]).hex())
        else:
            plain_types.append(canonical_type(p))
            plain_values.append(values[p["name"]])
    return {
        "address": address,
        "topics": topics,
        "data": "0x" + encode(plain_types, plain_values).hex(),
        "blockNumber": hex(block),
        "blockTimestamp": hex(1_700_000_000 + 5 * block),
        "transactionHash": "0x" + f"{block:064x}",
        "transactionIndex": hex(tx_index),
        "logIndex": hex(log_index),
        "removed": False,
    }


SCHEMAS = event_schemas(ABI)
BY_NAME = {s.name: s for s in SCHEMAS.values()}


def chain_logs(block: int) -> list[dict[str, Any]]:
    """The deterministic logs of one block of the fake chain.

    Every 7th block has a ``BatchCreated``, every 11th a ``WinnerSelected``, every 3rd a
    ``PriceUpdate``; a block with several gets increasing log indices.
    """
    out = []
    idx = 0
    if block % 7 == 0:
        out.append(
            encode_log(
                BY_NAME["BatchCreated"],
                {
                    "batchId": block.to_bytes(32, "big"),
                    "totalAmount": 2**200 + block,
                    "owner": "0x" + f"{block:040x}",
                    "immutableFlag": block % 2 == 0,
                },
                block=block,
                log_index=idx,
            )
        )
        idx += 1
    if block % 11 == 0:
        out.append(
            encode_log(
                BY_NAME["WinnerSelected"],
                {"winner": (block.to_bytes(32, "big"), "0x" + "11" * 20, block * 10)},
                block=block,
                log_index=idx,
            )
        )
        idx += 1
    if block % 3 == 0:
        out.append(encode_log(BY_NAME["PriceUpdate"], {"price": block}, block=block, log_index=idx))
    return out


class FakeRpc:
    """In-memory :class:`~ethswarm_chain_sync.rpc.RpcClient` over :func:`chain_logs`.

    Args:
        finalized: Block reported as ``finalized``.
        max_span: Ranges wider than this many blocks raise ``RangeTooLargeError``.
        fail_after: Raise ``RuntimeError`` on the call after this many successful calls
            (simulates an interruption).
    """

    def __init__(
        self, finalized: int = 2000, max_span: int | None = None, fail_after: int | None = None
    ) -> None:
        self.finalized = finalized
        self.max_span = max_span
        self.fail_after = fail_after
        self.calls: list[tuple[int, int]] = []

    def finalized_block(self) -> int:
        return self.finalized

    def get_logs(self, *, address, topics, from_block, to_block):
        if self.max_span is not None and to_block - from_block + 1 > self.max_span:
            raise RangeTooLargeError("range too large")
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise RuntimeError("simulated interruption")
        self.calls.append((from_block, to_block))
        wanted = set(topics[0]) if topics else None
        logs = []
        for b in range(from_block, to_block + 1):
            for log in chain_logs(b):
                if log["address"] == address and (wanted is None or log["topics"][0] in wanted):
                    logs.append(log)
        return logs


def make_target(selected: set[str] | None = None, start_block: int = 100) -> Target:
    """A target over the synthetic ABI at :data:`ADDRESS`."""
    return Target(
        network="mainnet",
        chain_id=100,
        contract="PostageStamp",
        version="v0.0.1",
        address=ADDRESS,
        start_block=start_block,
        schemas=SCHEMAS,
        selected=frozenset(selected or {s.key for s in SCHEMAS.values()}),
    )


@pytest.fixture
def target() -> Target:
    return make_target()


def snapshot(root) -> dict[str, bytes]:
    """Every file under ``root`` (except the lock file) as ``relative path -> bytes``."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != ".lock"
    }
