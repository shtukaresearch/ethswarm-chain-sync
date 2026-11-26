"""Event schemas derived from a contract ABI.

An :class:`EventSchema` is everything needed to recognise and decode one event emitted by
one deployed contract: its name, canonical signature, ``topic0`` and ABI inputs (including
struct ``components``). Schemas are always taken from the ABI of the *specific deployment*
that emitted the log, never looked up by event name alone. This is what disambiguates
events that share a name across contracts (``Paused``, ``RoleGranted``, ``PriceUpdate``)
or across versions of one contract (``StakeUpdated`` has three different signatures;
``WinnerSelected``'s struct field order changed in v0.6.0).

The *file key* of an event (:attr:`EventSchema.key`) is its bare name, unless the ABI
overloads that name, in which case it is the full signature. Within one deployment the
key is therefore unique, and it names the event's JSONLines file in the store.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from eth_hash.auto import keccak


def canonical_type(param: Mapping[str, Any]) -> str:
    """The canonical ABI type string of one parameter.

    Tuples are expanded recursively into ``(t1,t2,...)`` form, keeping any array suffix:
    a ``tuple[]`` whose components are ``address`` and ``uint256`` becomes
    ``(address,uint256)[]``. This form serves both as the signature fragment hashed into
    ``topic0`` and as the type string handed to ``eth_abi``.
    """
    typ = param["type"]
    if typ.startswith("tuple"):
        inner = ",".join(canonical_type(c) for c in param["components"])
        return f"({inner}){typ[len('tuple') :]}"
    return typ


@dataclass(frozen=True)
class EventSchema:
    """The decode schema for one event of one deployed contract.

    Fields:
        name: Event name as declared in the ABI, e.g. ``"Committed"``.
        signature: Canonical signature, e.g. ``"Committed(uint256,bytes32,uint8)"``.
        topic0: ``keccak(signature)`` as ``0x``-prefixed lowercase hex.
        inputs: The ABI ``inputs`` list, verbatim (names, types, ``indexed`` flags,
            struct ``components``).
        key: Store key, unique within a deployment (see module docstring).
    """

    name: str
    signature: str
    topic0: str
    inputs: tuple[Mapping[str, Any], ...]
    key: str

    @staticmethod
    def from_abi(entry: Mapping[str, Any], key: str | None = None) -> EventSchema:
        """Build a schema from one ABI ``event`` entry; ``key`` defaults to the name."""
        types = ",".join(canonical_type(i) for i in entry["inputs"])
        signature = f"{entry['name']}({types})"
        return EventSchema(
            name=entry["name"],
            signature=signature,
            topic0="0x" + keccak(signature.encode()).hex(),
            inputs=tuple(entry["inputs"]),
            key=key or entry["name"],
        )


def event_schemas(abi: Iterable[Mapping[str, Any]]) -> dict[str, EventSchema]:
    """All non-anonymous events of an ABI, keyed by ``topic0``.

    Anonymous events carry no ``topic0`` and cannot be recognised from a log, so they are
    omitted (the Swarm contracts declare none). Overloaded names get signature keys.
    """
    events = [e for e in abi if e.get("type") == "event" and not e.get("anonymous", False)]
    counts: dict[str, int] = {}
    for e in events:
        counts[e["name"]] = counts.get(e["name"], 0) + 1
    out: dict[str, EventSchema] = {}
    for e in events:
        schema = EventSchema.from_abi(e)
        if counts[e["name"]] > 1:
            schema = EventSchema.from_abi(e, key=schema.signature)
        out[schema.topic0] = schema
    return out
