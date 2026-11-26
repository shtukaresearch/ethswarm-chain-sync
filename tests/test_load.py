"""The library read path: ``load`` / ``iter_rows``."""

from __future__ import annotations

from datetime import UTC, datetime

from conftest import OTHER, FakeRpc, make_target

import ethswarm_chain_sync as ecs
from ethswarm_chain_sync import store
from ethswarm_chain_sync.deployments import Target
from ethswarm_chain_sync.sync import sync_target


def synced(tmp_path, to_block=500):
    sync_target(FakeRpc(), tmp_path, make_target(), to_block=to_block, chunk_size=50, workers=2)
    return tmp_path


def test_load_returns_all_rows_in_chain_order(tmp_path):
    rows = ecs.load(store_dir=synced(tmp_path))
    assert rows, "expected rows"
    positions = [(r["block_number"], r["log_index"]) for r in rows]
    assert positions == sorted(positions)
    assert {r["event"] for r in rows} == {"BatchCreated", "WinnerSelected", "PriceUpdate"}
    assert isinstance(rows[0]["block_timestamp"], datetime)
    assert rows[0]["block_timestamp"].tzinfo == UTC


def test_load_filters(tmp_path):
    root = synced(tmp_path)
    assert {r["event"] for r in ecs.load(event="PriceUpdate", store_dir=root)} == {"PriceUpdate"}
    assert ecs.load("Redistribution", store_dir=root) == []
    assert ecs.load(address=OTHER, store_dir=root) == []
    assert ecs.load(network="testnet", store_dir=root) == []
    assert len(ecs.load("PostageStamp", ["PriceUpdate", "BatchCreated"], store_dir=root)) == len(
        [r for r in ecs.load(store_dir=root) if r["event"] != "WinnerSelected"]
    )
    raw = ecs.load(store_dir=root, parse_timestamps=False)
    assert raw[0]["block_timestamp"].endswith("Z")


def test_load_spans_deployments(tmp_path):
    root = synced(tmp_path)
    other = Target(**{**make_target().__dict__, "address": OTHER})
    sync_target(FakeRpc(), root, other, to_block=500)  # FakeRpc emits nothing for OTHER
    assert len(ecs.load(store_dir=root)) == len(
        ecs.load(address=make_target().address, store_dir=root)
    )


def test_load_ignores_uncheckpointed_tail(tmp_path):
    root = synced(tmp_path)
    before = ecs.load(store_dir=root)
    t = make_target()
    d = store.deployment_dir(root, t.chain_id, t.contract, t.address)
    with store.event_path(d, "PriceUpdate").open("ab") as fh:
        fh.write(b'{"torn": ')
    assert ecs.load(store_dir=root) == before


def test_store_dir_env(tmp_path, monkeypatch):
    root = synced(tmp_path)
    monkeypatch.setenv(store.STORE_DIR_ENV, str(root))
    assert ecs.load() == ecs.load(store_dir=root)


def test_missing_store_loads_empty(tmp_path):
    assert ecs.load(store_dir=tmp_path / "nothing") == []
