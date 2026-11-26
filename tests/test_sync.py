"""Acquisition engine: completeness, resumability and exactly-once storage.

The central property: however a sync is split up (interrupted, killed mid-write, run
incrementally, run for a subset of events first, or with ranges bisected), the final
store is byte-identical to one uninterrupted sync.
"""

from __future__ import annotations

import json

import pytest
from conftest import FakeRpc, chain_logs, make_target, snapshot

from ethswarm_chain_sync import store
from ethswarm_chain_sync.sync import block_ranges, sync_target

TO = 2000


def reference(tmp_path, **kw):
    """Store produced by one uninterrupted sync to :data:`TO`."""
    root = tmp_path / "reference"
    sync_target(FakeRpc(), root, make_target(), to_block=TO, chunk_size=97, workers=4, **kw)
    return snapshot(root)


def dep_dir(root):
    t = make_target()
    return store.deployment_dir(root, t.chain_id, t.contract, t.address)


def test_block_ranges_cover_exactly():
    assert list(block_ranges(5, 14, 4)) == [(5, 8), (9, 12), (13, 14)]
    assert list(block_ranges(5, 5, 4)) == [(5, 5)]
    assert list(block_ranges(6, 5, 4)) == []


def test_full_sync_writes_every_log_in_order(tmp_path, target):
    result = sync_target(FakeRpc(), tmp_path, target, to_block=TO, chunk_size=97, workers=4)
    expected = [log for b in range(target.start_block, TO + 1) for log in chain_logs(b)]
    assert sum(result.rows.values()) == len(expected)

    d = dep_dir(tmp_path)
    cps = store.load_checkpoints(d)
    assert set(cps) == {"BatchCreated", "WinnerSelected", "PriceUpdate"}
    for key, cp in cps.items():
        path = store.event_path(d, key)
        rows = list(store.iter_event_file(path))
        assert cp == store.Checkpoint(block=TO, size=path.stat().st_size)
        assert all(r["event"] == key for r in rows)
        positions = [(r["block_number"], r["log_index"]) for r in rows]
        assert positions == sorted(positions)
        assert len(set(positions)) == len(positions)
        assert rows[0]["block_number"] >= target.start_block


@pytest.mark.parametrize("fail_after", [0, 1, 5, 13])
def test_interrupted_then_resumed_equals_uninterrupted(tmp_path, fail_after):
    root = tmp_path / "resumed"
    with pytest.raises(RuntimeError, match="simulated"):
        sync_target(
            FakeRpc(fail_after=fail_after),
            root,
            make_target(),
            to_block=TO,
            chunk_size=97,
            workers=4,
        )
    sync_target(FakeRpc(), root, make_target(), to_block=TO, chunk_size=97, workers=4)
    assert snapshot(root) == reference(tmp_path)


def test_uncheckpointed_tail_and_torn_line_are_discarded(tmp_path):
    root = tmp_path / "torn"
    sync_target(FakeRpc(), root, make_target(), to_block=1000, chunk_size=97, workers=4)
    d = dep_dir(root)
    # A crash after appending but before checkpointing: whole rows plus a torn line.
    with store.event_path(d, "PriceUpdate").open("ab") as fh:
        fh.write(b'{"duplicate":1}\n{"torn": ')
    # An event file written before its first checkpoint ever landed.
    store.event_path(d, "Orphan").write_bytes(b"junk\n")
    cps = store.load_checkpoints(d)
    cps.pop("WinnerSelected")
    store.event_path(d, "WinnerSelected").write_bytes(b'{"never checkpointed":1}\n')
    store.save_checkpoints(d, cps)

    sync_target(FakeRpc(), root, make_target(), to_block=TO, chunk_size=97, workers=4)
    store.event_path(d, "Orphan").unlink()  # not a selected event: left untouched
    assert snapshot(root) == reference(tmp_path)


def test_incremental_sync_equals_single_sync(tmp_path):
    root = tmp_path / "incremental"
    for to in (150, 151, 999, TO, TO):
        sync_target(FakeRpc(), root, make_target(), to_block=to, chunk_size=97, workers=4)
    assert snapshot(root) == reference(tmp_path)


def test_event_subset_then_superset(tmp_path):
    root = tmp_path / "subset"
    sync_target(
        FakeRpc(), root, make_target({"PriceUpdate"}), to_block=1200, chunk_size=97, workers=4
    )
    assert set(store.load_checkpoints(dep_dir(root))) == {"PriceUpdate"}
    rpc = FakeRpc()
    sync_target(rpc, root, make_target(), to_block=TO, chunk_size=97, workers=4)
    # Two groups: the new events from the start block, PriceUpdate from its checkpoint.
    assert (100, 196) in rpc.calls and (1201, 1297) in rpc.calls
    assert snapshot(root) == reference(tmp_path)


def test_ranges_refused_as_too_large_are_bisected(tmp_path):
    root = tmp_path / "split"
    rpc = FakeRpc(max_span=30)
    sync_target(rpc, root, make_target(), to_block=TO, chunk_size=97, workers=4)
    assert all(b - a + 1 <= 30 for a, b in rpc.calls)
    assert snapshot(root) == reference(tmp_path)


def test_concurrent_sync_of_same_deployment_is_refused(tmp_path, target):
    d = dep_dir(tmp_path)
    with store.locked(d):
        with pytest.raises(store.StoreLockedError):
            sync_target(FakeRpc(), tmp_path, target, to_block=TO)


def test_truncated_file_is_reported_as_corrupt(tmp_path, target):
    sync_target(FakeRpc(), tmp_path, target, to_block=TO, chunk_size=500)
    path = store.event_path(dep_dir(tmp_path), "PriceUpdate")
    path.write_bytes(path.read_bytes()[:10])
    with pytest.raises(store.StoreCorruptError):
        sync_target(FakeRpc(), tmp_path, target, to_block=TO)


def test_head_json_shape(tmp_path, target):
    sync_target(FakeRpc(), tmp_path, target, to_block=TO, chunk_size=500)
    head = json.loads((dep_dir(tmp_path) / "head.json").read_text())
    assert head["events"]["PriceUpdate"]["block"] == TO
