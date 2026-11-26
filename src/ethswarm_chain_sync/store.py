"""The JSONLines event cache and its per-event checkpoints.

Layout (see ``docs/data-model.md``)::

    <store-dir>/
      <chain_id>/
        <Contract>_<address>/          one directory per deployed contract
          <event-key>.jsonl            one append-only file per event; one row per line
          head.json                    {"events": {<event-key>: {"block": N, "size": B}}}

The checkpoint of an event records the last block synced (inclusive) *and* the byte size
of its ``.jsonl`` file at that point. Rows are appended and flushed to disk before the
checkpoint is atomically replaced, so after any interruption the file holds a valid
prefix of exactly ``size`` bytes plus, possibly, a tail written after the last checkpoint
(including a torn final line). :func:`rewind` truncates that tail before a sync resumes,
which makes the on-disk log exactly-once: no duplicates and no partial lines. A ``.jsonl``
file with no checkpoint is a tail in its entirety and is truncated to empty.

The store is a cache, reconstructible from chain: deleting a deployment directory just
makes the next sync start again from the deployment block.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Environment variable overriding the default store location.
STORE_DIR_ENV = "ETHSWARM_CHAIN_SYNC_STORE"
APP_DIR_NAME = "ethswarm-chain-sync"
HEAD_FILENAME = "head.json"
LOCK_FILENAME = ".lock"


class StoreLockedError(RuntimeError):
    """Another process is already syncing this deployment directory."""


class StoreCorruptError(RuntimeError):
    """A ``.jsonl`` file is shorter than its checkpoint says; the cache must be rebuilt."""


@dataclass(frozen=True)
class Checkpoint:
    """Sync progress of one event of one deployment.

    Fields:
        block: Last block synced, inclusive.
        size: Byte length of the event's ``.jsonl`` file once that block was synced.
    """

    block: int
    size: int


def default_store_dir() -> Path:
    """``$XDG_CACHE_HOME/ethswarm-chain-sync``, else ``~/.cache/ethswarm-chain-sync``."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / APP_DIR_NAME


def resolve_store_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Store directory by precedence: ``explicit`` > ``$ETHSWARM_CHAIN_SYNC_STORE`` > default."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get(STORE_DIR_ENV)
    return Path(env) if env else default_store_dir()


def deployment_dir(
    store_dir: str | os.PathLike[str], chain_id: int, contract: str, address: str
) -> Path:
    """``<store-dir>/<chain_id>/<Contract>_<address>``."""
    return Path(store_dir) / str(chain_id) / f"{contract}_{address.lower()}"


def event_path(dep_dir: Path, key: str) -> Path:
    """The ``.jsonl`` file for one event key."""
    return dep_dir / f"{key}.jsonl"


@contextmanager
def locked(dep_dir: Path) -> Iterator[None]:
    """Hold an exclusive, non-blocking ``flock`` on a deployment directory.

    Two writers on one directory would interleave appends and checkpoints, so a sync
    takes this lock for the whole of its run. Raises :class:`StoreLockedError` at once if
    another process holds it. Readers do not lock.
    """
    dep_dir.mkdir(parents=True, exist_ok=True)
    with (dep_dir / LOCK_FILENAME).open("a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StoreLockedError(f"{dep_dir} is being synced by another process") from exc
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load_checkpoints(dep_dir: Path) -> dict[str, Checkpoint]:
    """All event checkpoints of a deployment (empty if never synced)."""
    path = dep_dir / HEAD_FILENAME
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))["events"]
    return {k: Checkpoint(block=v["block"], size=v["size"]) for k, v in raw.items()}


def save_checkpoints(dep_dir: Path, checkpoints: dict[str, Checkpoint]) -> None:
    """Atomically replace a deployment's ``head.json``."""
    dep_dir.mkdir(parents=True, exist_ok=True)
    body = {
        "events": {k: {"block": c.block, "size": c.size} for k, c in sorted(checkpoints.items())}
    }
    tmp = dep_dir / (HEAD_FILENAME + ".tmp")
    tmp.write_text(json.dumps(body, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, dep_dir / HEAD_FILENAME)


def rewind(dep_dir: Path, key: str, checkpoint: Checkpoint | None) -> int:
    """Truncate an event file back to its checkpoint; return the resulting size.

    Raises :class:`StoreCorruptError` if the file is shorter than the checkpoint, which
    can only happen if the file was modified outside this package.
    """
    path = event_path(dep_dir, key)
    want = checkpoint.size if checkpoint else 0
    if not path.exists():
        if want:
            raise StoreCorruptError(f"{path} is missing but its checkpoint says {want} bytes")
        return 0
    have = path.stat().st_size
    if have < want:
        raise StoreCorruptError(
            f"{path} has {have} bytes, checkpoint says {want}; delete {dep_dir} and resync"
        )
    if have > want:
        with path.open("r+b") as fh:
            fh.truncate(want)
    return want


def append_rows(dep_dir: Path, key: str, rows: Iterable[dict[str, Any]]) -> int:
    """Append rows to an event file, flush them to stable storage, return the new size."""
    dep_dir.mkdir(parents=True, exist_ok=True)
    path = event_path(dep_dir, key)
    payload = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows).encode()
    with path.open("ab") as fh:
        if payload:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        return fh.tell()


def iter_event_file(path: Path, limit: int | None = None) -> Iterator[dict[str, Any]]:
    """Rows of one ``.jsonl`` file, reading at most ``limit`` bytes when given.

    Reading only up to the checkpointed size makes loading safe while a sync is
    appending to the same file.
    """
    with path.open("rb") as fh:
        data = fh.read() if limit is None else fh.read(limit)
    for line in data.splitlines():
        if line.strip():
            yield json.loads(line)
