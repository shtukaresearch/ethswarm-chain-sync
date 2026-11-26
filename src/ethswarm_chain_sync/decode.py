"""Decode raw ``eth_getLogs`` entries into web3-free, JSON-ready rows.

The mechanical part (splitting indexed topics from data, ABI decoding) is plain
``eth_abi``. The only bespoke step is attaching names: each event argument is keyed by its
ABI parameter name, and each struct (``tuple``) value becomes an object keyed by its
component names, recursively, including inside arrays. There are no enums to map.

Value representation in ``args``:

- ``address`` -> lowercase ``0x`` hex string;
- ``bytes`` / ``bytesN`` -> ``0x`` hex string;
- integers -> exact Python ``int`` (exact JSON integers on disk, however large);
- ``bool`` and ``string`` -> as-is;
- arrays -> lists; structs -> dicts;
- an *indexed* dynamic value (``string``, ``bytes``, arrays, structs) is only present in
  the log as its keccak hash, so it is stored as that 32-byte topic in ``0x`` hex.

ABI parameter names are kept verbatim (``batchId``, ``normalisedBalance``). An unnamed
parameter is keyed by its position, ``"_0"``, ``"_1"``, ...
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from eth_abi import decode as abi_decode

from .schema import EventSchema, canonical_type


class DecodeError(ValueError):
    """A log could not be decoded against the schema chosen for it."""


def _hex_to_bytes(value: str) -> bytes:
    """``0x``-prefixed hex string -> bytes."""
    return bytes.fromhex(value[2:] if value.startswith("0x") else value)


def quantity(value: str | int) -> int:
    """A JSON-RPC quantity (hex string or int) as ``int``."""
    return int(value, 16) if isinstance(value, str) else int(value)


def _is_dynamic(param: Mapping[str, Any]) -> bool:
    """Whether an indexed parameter of this type is stored in its topic as a hash.

    Per the ABI spec, every value type of static size (``uintN``, ``address``, ``bool``,
    ``bytesN``, ...) is stored in its topic directly; ``string``, ``bytes``, arrays and
    structs are stored as ``keccak`` of their encoding.
    """
    typ = param["type"]
    return typ in ("string", "bytes") or typ.endswith("]") or typ.startswith("tuple")


def name_value(param: Mapping[str, Any], value: Any) -> Any:
    """Normalize one ``eth_abi``-decoded value, attaching struct component names.

    Recurses through arrays (``T[]``, ``T[k]``) and structs, following the ABI
    parameter's ``type`` and ``components``.
    """
    typ = param["type"]
    if typ.endswith("]"):
        element = dict(param)
        element["type"] = typ[: typ.rindex("[")]
        return [name_value(element, v) for v in value]
    if typ == "tuple":
        return {
            (c.get("name") or f"_{i}"): name_value(c, v)
            for i, (c, v) in enumerate(zip(param["components"], value, strict=True))
        }
    if typ == "address":
        return value.lower()
    if typ.startswith("bytes"):
        return "0x" + bytes(value).hex()
    return value


def decode_args(schema: EventSchema, topics: Sequence[str], data: str) -> dict[str, Any]:
    """Decode a log's ``topics[1:]`` and ``data`` into named ``args`` per ``schema``.

    Raises :class:`DecodeError` when the number of indexed topics does not match the
    schema or ``eth_abi`` rejects the payload.
    """
    indexed = [p for p in schema.inputs if p.get("indexed")]
    plain = [p for p in schema.inputs if not p.get("indexed")]
    if len(topics) - 1 != len(indexed):
        raise DecodeError(
            f"{schema.signature}: expected {len(indexed)} indexed topics, got {len(topics) - 1}"
        )
    try:
        plain_values = abi_decode([canonical_type(p) for p in plain], _hex_to_bytes(data))
    except Exception as exc:  # eth_abi raises a family of decoding errors
        raise DecodeError(f"{schema.signature}: {exc}") from exc

    decoded: dict[int, Any] = {}
    topic_iter = iter(topics[1:])
    plain_iter = iter(plain_values)
    for pos, param in enumerate(schema.inputs):
        if param.get("indexed"):
            topic = next(topic_iter)
            if _is_dynamic(param):
                decoded[pos] = "0x" + _hex_to_bytes(topic).hex()
            else:
                (value,) = abi_decode([canonical_type(param)], _hex_to_bytes(topic))
                decoded[pos] = name_value(param, value)
        else:
            decoded[pos] = name_value(param, next(plain_iter))
    return {(p.get("name") or f"_{i}"): decoded[i] for i, p in enumerate(schema.inputs)}


def block_timestamp(raw_log: Mapping[str, Any]) -> datetime:
    """The UTC block time carried on the log itself (``blockTimestamp``).

    Part of the current execution-apis Log object and served by mainstream clients; there
    is no fallback lookup. Raises :class:`DecodeError` if the node omitted it.
    """
    value = raw_log.get("blockTimestamp")
    if value is None:
        raise DecodeError("RPC returned a log without blockTimestamp; use a newer node")
    return datetime.fromtimestamp(quantity(value), tz=UTC)


def decode_log(
    raw_log: Mapping[str, Any],
    schemas: Mapping[str, EventSchema],
    *,
    chain_id: int,
    contract: str,
    version: str,
) -> dict[str, Any]:
    """Decode one raw log into a store row (see ``docs/data-model.md``).

    ``schemas`` is the emitting deployment's ``topic0 -> EventSchema`` index, so the
    schema is chosen by ``(address, topic0)``, never by name. Raises :class:`DecodeError`
    for a log whose ``topic0`` is not in the index.
    """
    topics = raw_log["topics"]
    if not topics:
        raise DecodeError("anonymous log (no topics) cannot be decoded")
    schema = schemas.get(topics[0].lower())
    if schema is None:
        raise DecodeError(f"unknown topic0 {topics[0]} for {contract} at {raw_log['address']}")
    return {
        "chain_id": chain_id,
        "contract": contract,
        "version": version,
        "address": raw_log["address"].lower(),
        "event": schema.key,
        "signature": schema.signature,
        "block_number": quantity(raw_log["blockNumber"]),
        "block_timestamp": block_timestamp(raw_log).isoformat().replace("+00:00", "Z"),
        "transaction_hash": raw_log["transactionHash"].lower(),
        "transaction_index": quantity(raw_log["transactionIndex"]),
        "log_index": quantity(raw_log["logIndex"]),
        "args": decode_args(schema, topics, raw_log["data"]),
    }
