"""Sync targets: which deployed contracts (and which of their events) to sync.

Deployment data (addresses, deployment blocks, ABIs) comes from the
``ethswarm-deployments`` package, read from its shared user-local cache
(``~/.cache/ethswarm-deployments`` unless ``$ETHSWARM_DEPLOYMENTS_CACHE`` says otherwise),
so the cache is built once and reused across projects.

A :class:`Target` is one deployed contract address. A contract name that was redeployed
in several releases (``Redistribution``, ``StakeRegistry``, ...) yields one target per
distinct address; an address carried unchanged across releases (``Token``) yields one.

Contract selectors understood by :func:`resolve_targets`:

- ``latest`` (the default when none is given): every contract in the newest release's
  manifest, e.g. ``v0.9.4``;
- ``vX.Y.Z``: every contract in that release's manifest, including contracts carried over
  unchanged from an earlier release (at ``v0.9.4`` PostageStamp is still its ``v0.6.0``
  address);
- ``all``: every deployment of every contract, across all releases;
- ``Name``: every deployment of that contract, across all releases;
- ``Name@vX.Y.Z`` / ``Name@latest``: that contract's deployment as of that release;
- ``0x…``: the deployment at that address.

The release-wide selectors (``latest``, ``vX.Y.Z``, ``all``) leave out the token contracts
(``Token``, ``TestToken``), which are synced only when named or given by address. Event
selectors then narrow each picked deployment to the events they name, dropping
deployments that declare none of them.

"Newest release" is by numeric version order (``v0.10.0`` > ``v0.9.4``), not string order.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

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


#: Release-wide selectors. ``latest`` is the default selection.
LATEST = "latest"
ALL = "all"
_VERSION_RE = re.compile(r"^v\d+(\.\d+)*$")


def _version_key(version: str) -> tuple[int, ...]:
    """Numeric sort key of a ``vX.Y.Z`` release name."""
    return tuple(int(part) for part in version[1:].split("."))


def latest_version(network: str = "mainnet", manager=None) -> str:
    """The newest release on ``network``, by numeric version order."""
    mgr = manager or deployment_manager()
    return max(mgr.versions(network), key=_version_key)


def release_addresses(network: str, version: str, manager) -> dict[str, str]:
    """``contract name -> lowercase address`` from one release's manifest.

    ``version`` may be ``latest``. Raises :class:`SelectionError` for an unknown release.
    """
    if version == LATEST:
        version = latest_version(network, manager)
    versions = manager.versions(network)
    if version not in versions:
        known = ", ".join(sorted(versions, key=_version_key))
        raise SelectionError(f"no {network} release {version!r} (known: {known})")
    return {
        name: manager.deployment(name, version, network).address.lower()
        for name in manager.contract_names(version, network)
    }


def _select_contracts(
    network: str, selector: str, candidates: list[Target], manager
) -> list[Target]:
    """The deployments one contract selector picks (grammar in the module docstring)."""
    if selector == ALL:
        return [t for t in candidates if t.contract not in TOKEN_CONTRACTS]
    if selector == LATEST or _VERSION_RE.match(selector):
        addresses = {
            a
            for name, a in release_addresses(network, selector, manager).items()
            if name not in TOKEN_CONTRACTS
        }
        return [t for t in candidates if t.address in addresses]
    if selector.lower().startswith("0x"):
        return [t for t in candidates if t.address == selector.lower()]
    name, _, version = selector.partition("@")
    if not version:
        return [t for t in candidates if t.contract == name]
    address = release_addresses(network, version, manager).get(name)
    return [t for t in candidates if t.address == address]


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
    """Resolve CLI-style selectors into sync targets (grammar in the module docstring).

    ``contracts`` defaults to ``["latest"]``. Raises :class:`SelectionError` if a contract
    selector matches no deployment, if an event selector matches no event of any picked
    deployment, or if nothing is left to sync.
    """
    mgr = manager or deployment_manager()
    candidates = all_targets(network, mgr)
    picked: list[Target] = []
    for sel in contracts or [LATEST]:
        hits = _select_contracts(network, sel, candidates, mgr)
        if not hits:
            raise SelectionError(f"no {network} deployment matches {sel!r}")
        picked.extend(t for t in hits if t not in picked)

    if events:
        for ev in events:
            if not any(_event_keys(t, [ev]) for t in picked):
                raise SelectionError(f"no selected contract declares event {ev!r}")
        picked = [replace(t, selected=keys) for t in picked if (keys := _event_keys(t, events))]

    if not picked:
        raise SelectionError("nothing selected")
    return picked
