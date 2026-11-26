"""Sync targets: which deployed contracts (and which of their events) to sync.

Deployment data (addresses, deployment blocks, ABIs) comes from the
``ethswarm-deployments`` package, read from its shared user-local cache
(``~/.cache/ethswarm-deployments`` unless ``$ETHSWARM_DEPLOYMENTS_CACHE`` says otherwise),
so the cache is built once and reused across projects.

A :class:`Target` is one deployed contract address. A contract name that was redeployed
in several releases (``Redistribution``, ``StakeRegistry``, ...) yields one target per
distinct address; an address carried unchanged across releases (``Token``) yields one.

Selection rules of :func:`resolve_targets`, in one sentence: a contract selector picks
deployments by name, ``name@version`` or address; an event selector then narrows each
picked deployment to the events it names (dropping deployments that have none of them);
with no contract selector, every non-token contract is a candidate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .schema import EventSchema, event_schemas

#: Networks known to ``ethswarm-deployments`` and their EIP-3770 chain short names,
#: which name the RPC environment variable (``$GNO_RPC_URL``, ``$SEP_RPC_URL``).
NETWORK_SHORT_NAMES = {"mainnet": "GNO", "testnet": "SEP"}

#: ERC-20 contracts excluded when no contract is selected: their ``Transfer`` /
#: ``Approval`` streams are far larger than the storage-incentives events and are only
#: synced when asked for by name.
TOKEN_CONTRACTS = frozenset({"Token", "TestToken"})


@dataclass(frozen=True)
class Target:
    """One deployed contract to sync.

    Fields:
        network: ``ethswarm-deployments`` network name (``"mainnet"`` / ``"testnet"``).
        chain_id: EVM chain id of that network.
        contract: Canonical contract name, e.g. ``"Redistribution"``.
        version: First storage-incentives release this address appeared in.
        address: Contract address, lowercase hex.
        start_block: Deployment block; the first block synced.
        schemas: ``topic0 -> EventSchema`` for every event in this deployment's ABI.
        selected: Store keys of the events to sync (a subset of ``schemas``' keys).
    """

    network: str
    chain_id: int
    contract: str
    version: str
    address: str
    start_block: int
    schemas: dict[str, EventSchema] = field(repr=False)
    selected: frozenset[str]

    @property
    def label(self) -> str:
        """Human-readable ``Contract@version (0xaddr…)`` label."""
        return f"{self.contract}@{self.version} ({self.address[:10]}…)"

    def selected_schemas(self) -> list[EventSchema]:
        """The schemas of the selected events, sorted by key."""
        return sorted(
            (s for s in self.schemas.values() if s.key in self.selected), key=lambda s: s.key
        )


class SelectionError(ValueError):
    """A contract or event selector matched nothing."""


def deployment_manager():
    """An ``ethswarm-deployments`` manager over the shared user-local cache."""
    from ethswarm_deployments import DeploymentManager

    return DeploymentManager(location="user")


def all_targets(network: str = "mainnet", manager=None) -> list[Target]:
    """Every distinct deployment on ``network``, all events selected.

    Ordered by contract name, then deployment block.
    """
    mgr = manager or deployment_manager()
    chain_id = mgr.network_info(network)["chain_id"]
    names = sorted({c for v in mgr.versions(network) for c in mgr.contract_names(v, network)})
    out: list[Target] = []
    for name in names:
        for d in sorted(mgr.all_deployments(name, network), key=lambda d: d.block):
            schemas = event_schemas(d.abi)
            out.append(
                Target(
                    network=network,
                    chain_id=chain_id,
                    contract=d.name,
                    version=d.version,
                    address=d.address.lower(),
                    start_block=d.block,
                    schemas=schemas,
                    selected=frozenset(s.key for s in schemas.values()),
                )
            )
    return out


def _matches(target: Target, selector: str) -> bool:
    """Whether a contract selector (``name``, ``name@version`` or address) picks ``target``."""
    if selector.lower().startswith("0x"):
        return target.address == selector.lower()
    name, _, version = selector.partition("@")
    return target.contract == name and (not version or target.version == version)


def _event_keys(target: Target, events: Sequence[str]) -> frozenset[str]:
    """Store keys of ``target`` events matching ``events`` (by name, key or signature)."""
    return frozenset(
        s.key
        for s in target.schemas.values()
        if s.name in events or s.key in events or s.signature in events
    )


def resolve_targets(
    network: str = "mainnet",
    contracts: Sequence[str] = (),
    events: Sequence[str] = (),
    manager=None,
) -> list[Target]:
    """Resolve CLI-style selectors into sync targets (rules in the module docstring).

    Raises :class:`SelectionError` if a contract selector matches no deployment, if an
    event selector matches no event of any candidate, or if nothing is left to sync.
    """
    candidates = all_targets(network, manager)
    if contracts:
        picked: list[Target] = []
        for sel in contracts:
            hits = [t for t in candidates if _matches(t, sel)]
            if not hits:
                raise SelectionError(f"no {network} deployment matches {sel!r}")
            picked.extend(t for t in hits if t not in picked)
    else:
        picked = [t for t in candidates if t.contract not in TOKEN_CONTRACTS]

    if events:
        for ev in events:
            if not any(_event_keys(t, [ev]) for t in picked):
                raise SelectionError(f"no selected contract declares event {ev!r}")
        narrowed = []
        for t in picked:
            keys = _event_keys(t, events)
            if keys:
                narrowed.append(Target(**{**t.__dict__, "selected": keys}))
        picked = narrowed

    if not picked:
        raise SelectionError("nothing selected")
    return picked
