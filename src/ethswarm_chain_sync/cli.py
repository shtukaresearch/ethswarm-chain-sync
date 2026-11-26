"""Command line: ``ethswarm-chain-sync {sync,status}``.

``sync`` resolves the selected deployments (``--contract`` / ``--event``, see
:func:`~ethswarm_chain_sync.deployments.resolve_targets`), reads the chain's
``finalized`` block once, and syncs each target up to it. Each event starts from its
stored checkpoint, or from the contract's deployment block on first sync.

``status`` prints the same selection with each event's checkpoint, without touching the
chain.

The RPC endpoint is ``--rpc-url``, else ``$<SHORT>_RPC_URL`` where ``SHORT`` is the
network's EIP-3770 chain short name (``GNO`` for mainnet/Gnosis, ``SEP`` for
testnet/Sepolia).
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import __version__, store
from .deployments import NETWORK_SHORT_NAMES, SelectionError, Target, resolve_targets
from .rpc import HttpRpcClient
from .sync import DEFAULT_CHUNK_SIZE, DEFAULT_WORKERS, sync_target


class _Progress:
    """Per-target progress line on stderr, redrawn in place on a terminal."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.inplace = sys.stderr.isatty()
        self.last = 0.0

    def __call__(self, target: Target, block: int, to_block: int, rows: int) -> None:
        now = time.monotonic()
        if not self.enabled or (now - self.last < 0.5 and block != to_block):
            return
        self.last = now
        line = f"  {target.label}: block {block}/{to_block}, {rows} rows"
        if self.inplace:
            print(f"\r\033[K{line}", end="", file=sys.stderr, flush=True)
        else:
            print(line, file=sys.stderr, flush=True)

    def done(self) -> None:
        if self.enabled and self.inplace:
            print(file=sys.stderr)


def _targets(args) -> list[Target]:
    return resolve_targets(args.network, args.contract or (), args.event or ())


def cmd_sync(args) -> int:
    """Sync every selected target up to ``finalized`` (or ``--to-block``)."""
    url = args.rpc_url or os.environ.get(f"{NETWORK_SHORT_NAMES[args.network]}_RPC_URL")
    if not url:
        env = f"{NETWORK_SHORT_NAMES[args.network]}_RPC_URL"
        print(f"error: no RPC endpoint; pass --rpc-url or set ${env}", file=sys.stderr)
        return 2
    targets = _targets(args)
    rpc = HttpRpcClient(url, timeout=args.timeout)
    finalized = rpc.finalized_block()
    to_block = finalized if args.to_block is None else args.to_block
    if to_block > finalized:
        print(f"error: --to-block {to_block} is past finalized {finalized}", file=sys.stderr)
        return 2
    store_dir = store.resolve_store_dir(args.store_dir)
    print(
        f"store {store_dir}; syncing {len(targets)} contract(s) to block {to_block}",
        file=sys.stderr,
    )
    progress = _Progress(args.progress if args.progress is not None else sys.stderr.isatty())
    for target in targets:
        print(f"{target.label}: {len(target.selected)} event(s)", file=sys.stderr)
        result = sync_target(
            rpc,
            store_dir,
            target,
            to_block=to_block,
            chunk_size=args.chunk_size,
            workers=args.workers,
            progress=progress,
        )
        progress.done()
        total = sum(result.rows.values())
        detail = ", ".join(f"{k}={n}" for k, n in sorted(result.rows.items()) if n)
        print(f"  +{total} rows" + (f" ({detail})" if detail else ""), file=sys.stderr)
    return 0


def cmd_status(args) -> int:
    """Print each selected event's checkpoint."""
    store_dir = store.resolve_store_dir(args.store_dir)
    for t in _targets(args):
        dep_dir = store.deployment_dir(store_dir, t.chain_id, t.contract, t.address)
        cps = store.load_checkpoints(dep_dir)
        print(f"{t.contract}@{t.version} {t.address} (deployed at {t.start_block})")
        for s in t.selected_schemas():
            cp = cps.get(s.key)
            state = f"block {cp.block}, {cp.size} bytes" if cp else "not synced"
            print(f"  {s.key:<32} {state}")
    return 0


def _add_selection(p: argparse.ArgumentParser) -> None:
    p.add_argument("-n", "--network", choices=sorted(NETWORK_SHORT_NAMES), default="mainnet")
    p.add_argument(
        "-c",
        "--contract",
        action="append",
        metavar="SEL",
        help="latest (default), vX.Y.Z, all, Name, Name@vX.Y.Z, Name@latest, or 0xaddress "
        "(repeatable; release-wide selectors exclude token contracts)",
    )
    p.add_argument(
        "-e",
        "--event",
        action="append",
        metavar="NAME",
        help="event name or signature (repeatable; default: all events)",
    )
    p.add_argument(
        "--store-dir",
        help=f"cache directory (default: ${store.STORE_DIR_ENV} or {store.default_store_dir()})",
    )


def build_parser() -> argparse.ArgumentParser:
    """The argument parser (exposed for tests)."""
    parser = argparse.ArgumentParser(prog="ethswarm-chain-sync", description=__doc__.split("\n")[0])
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="sync events into the cache up to finalized")
    _add_selection(p_sync)
    p_sync.add_argument("--rpc-url", help="JSON-RPC endpoint (default: $GNO_RPC_URL/$SEP_RPC_URL)")
    p_sync.add_argument("--to-block", type=int, help="sync to this block instead of finalized")
    p_sync.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help="blocks per eth_getLogs (default: %(default)s)",
    )
    p_sync.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help="concurrent eth_getLogs requests (default: %(default)s)",
    )
    p_sync.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="per-request timeout, seconds (default: %(default)s)",
    )
    p_sync.add_argument(
        "--progress",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="show progress (default: when stderr is a terminal)",
    )
    p_sync.set_defaults(func=cmd_sync)

    p_status = sub.add_parser("status", help="show checkpoints of the selected events")
    _add_selection(p_status)
    p_status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (SelectionError, store.StoreLockedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted; progress is checkpointed, rerun to resume", file=sys.stderr)
        return 130
