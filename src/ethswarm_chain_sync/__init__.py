"""Sync Swarm storage-incentives contract events into a local JSONLines cache.

Command line: ``ethswarm-chain-sync sync`` fetches and decodes events from the deployed
storage-incentives contracts (every version, per ``ethswarm-deployments``) up to the
chain's ``finalized`` block, resuming from each event's checkpoint.

As a library, :func:`load` returns the cached rows as a list of dicts in chain order::

    import ethswarm_chain_sync as ecs

    rows = ecs.load("PostageStamp", "BatchCreated")
    rows[0]["args"]["batchId"]

Row format and on-disk layout are described in ``docs/data-model.md``.
"""

from importlib.metadata import PackageNotFoundError, version

from .load import iter_rows, load

try:
    __version__ = version("ethswarm-chain-sync")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.0.0"

__all__ = ["__version__", "iter_rows", "load"]
