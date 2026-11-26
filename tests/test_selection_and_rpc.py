"""Target selection (with a fake deployments manager) and HTTP RPC retry behaviour."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
import requests

from ethswarm_chain_sync import rpc as rpc_mod
from ethswarm_chain_sync.deployments import (
    SelectionError,
    all_targets,
    latest_version,
    resolve_targets,
)


def ev(name, *types):
    return {
        "type": "event",
        "name": name,
        "inputs": [{"name": f"a{i}", "type": t, "indexed": False} for i, t in enumerate(types)],
    }


@dataclass
class FakeDeployment:
    name: str
    version: str
    address: str
    block: int
    abi: list[dict[str, Any]]


def addr(byte: str) -> str:
    return "0x" + byte * 20


REDIS_V6 = FakeDeployment(
    "Redistribution",
    "v0.6.0",
    addr("AA"),
    30,
    [ev("Committed", "uint256", "bytes32"), ev("Paused", "address")],
)
REDIS_V9 = FakeDeployment(
    "Redistribution",
    "v0.9.1",
    addr("BB"),
    10,
    [ev("Committed", "uint256", "bytes32", "uint8"), ev("Paused", "address")],
)
REDIS_V10 = FakeDeployment(
    "Redistribution",
    "v0.10.0",
    addr("EE"),
    50,
    [ev("Committed", "uint256", "bytes32", "uint8"), ev("Paused", "address")],
)
STAMP = FakeDeployment(
    "PostageStamp",
    "v0.6.0",
    addr("CC"),
    20,
    [ev("BatchCreated", "bytes32"), ev("Paused", "address")],
)
TOKEN = FakeDeployment(
    "Token", "v0.4.0", addr("DD"), 1, [ev("Transfer", "address", "address", "uint256")]
)


class FakeManager:
    """Three releases; PostageStamp and Token carry over unchanged between them.

    ``versions`` comes back in string order, where ``v0.10.0`` sorts first, so a
    string-ordered "latest" would wrongly pick ``v0.9.1``.
    """

    MANIFESTS = {
        "v0.10.0": {"Redistribution": REDIS_V10, "PostageStamp": STAMP, "Token": TOKEN},
        "v0.6.0": {"Redistribution": REDIS_V6, "PostageStamp": STAMP, "Token": TOKEN},
        "v0.9.1": {"Redistribution": REDIS_V9, "PostageStamp": STAMP, "Token": TOKEN},
    }
    DEPLOYMENTS = {
        "Redistribution": [REDIS_V6, REDIS_V9, REDIS_V10],
        "PostageStamp": [STAMP],
        "Token": [TOKEN],
    }

    def network_info(self, network):
        return {"chain_id": 100}

    def versions(self, network):
        return sorted(self.MANIFESTS)

    def contract_names(self, version, network):
        return list(self.MANIFESTS[version])

    def deployment(self, name, version, network):
        return self.MANIFESTS[version][name]

    def all_deployments(self, name, network):
        return self.DEPLOYMENTS[name]


def labels(targets):
    return [(t.contract, t.version, sorted(t.selected)) for t in targets]


def addresses(targets):
    return {t.address for t in targets}


def test_all_targets_lowercases_and_orders_by_block():
    ts = all_targets(manager=FakeManager())
    redis = [t for t in ts if t.contract == "Redistribution"]
    assert [t.version for t in redis] == ["v0.9.1", "v0.6.0", "v0.10.0"]
    assert redis[0].address == addr("bb")
    sigs = {t.version: {s.signature for s in t.schemas.values()} for t in redis}
    assert "Committed(uint256,bytes32,uint8)" in sigs["v0.9.1"]
    assert "Committed(uint256,bytes32)" in sigs["v0.6.0"]


def test_latest_is_numeric_not_string_order():
    assert latest_version(manager=FakeManager()) == "v0.10.0"


def test_default_selection_is_latest_release_without_tokens():
    m = FakeManager()
    default = resolve_targets(manager=m)
    assert addresses(default) == {addr("ee"), addr("cc")}
    assert default == resolve_targets(contracts=["latest"], manager=m)
    assert default == resolve_targets(contracts=["v0.10.0"], manager=m)


def test_release_selector_includes_carried_over_contracts():
    m = FakeManager()
    assert addresses(resolve_targets(contracts=["v0.9.1"], manager=m)) == {addr("bb"), addr("cc")}
    # PostageStamp's target records the release it first appeared in, not v0.9.1.
    stamp = [
        t for t in resolve_targets(contracts=["v0.9.1"], manager=m) if t.contract == "PostageStamp"
    ]
    assert stamp[0].version == "v0.6.0"


def test_all_selector_spans_releases_without_tokens():
    assert addresses(resolve_targets(contracts=["all"], manager=FakeManager())) == {
        addr("aa"),
        addr("bb"),
        addr("ee"),
        addr("cc"),
    }


def test_named_contract_selectors():
    m = FakeManager()
    assert labels(resolve_targets(contracts=["Redistribution@v0.6.0"], manager=m)) == [
        ("Redistribution", "v0.6.0", ["Committed", "Paused"])
    ]
    assert addresses(resolve_targets(contracts=["Redistribution@latest"], manager=m)) == {
        addr("ee")
    }
    # Name@version resolves through the release manifest, so carried-over contracts match.
    assert addresses(resolve_targets(contracts=["PostageStamp@v0.10.0"], manager=m)) == {addr("cc")}
    assert len(resolve_targets(contracts=["Redistribution"], manager=m)) == 3
    assert labels(resolve_targets(contracts=["Token"], manager=m))[0][0] == "Token"
    assert labels(resolve_targets(contracts=[addr("dd")], manager=m))[0][0] == "Token"


def test_selectors_combine_without_duplicates():
    m = FakeManager()
    ts = resolve_targets(contracts=["latest", "v0.9.1", "PostageStamp"], manager=m)
    assert [t.address for t in ts] == [addr("cc"), addr("ee"), addr("bb")]


def test_event_selector_narrows_and_drops():
    m = FakeManager()
    assert labels(resolve_targets(events=["Committed"], manager=m)) == [
        ("Redistribution", "v0.10.0", ["Committed"]),
    ]
    assert labels(resolve_targets(contracts=["all"], events=["Committed"], manager=m)) == [
        ("Redistribution", "v0.9.1", ["Committed"]),
        ("Redistribution", "v0.6.0", ["Committed"]),
        ("Redistribution", "v0.10.0", ["Committed"]),
    ]
    by_sig = resolve_targets(contracts=["all"], events=["Committed(uint256,bytes32)"], manager=m)
    assert labels(by_sig) == [("Redistribution", "v0.6.0", ["Committed"])]
    paused = resolve_targets(contracts=["PostageStamp"], events=["Paused"], manager=m)
    assert labels(paused) == [("PostageStamp", "v0.6.0", ["Paused"])]


def test_selection_errors():
    m = FakeManager()
    for contracts in (["Nope"], ["v9.9.9"], ["Nope@latest"], ["Redistribution@v9.9.9"]):
        with pytest.raises(SelectionError):
            resolve_targets(contracts=contracts, manager=m)
    with pytest.raises(SelectionError, match="known: v0.6.0, v0.9.1, v0.10.0"):
        resolve_targets(contracts=["v9.9.9"], manager=m)
    with pytest.raises(SelectionError):
        resolve_targets(contracts=["PostageStamp"], events=["Committed"], manager=m)


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


class ScriptedSession:
    """A ``requests.Session`` stand-in replaying a script of responses/exceptions."""

    def __init__(self, script):
        self.script = script
        self.headers = {}

    def post(self, url, json, timeout):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client_with(script, **kw):
    c = rpc_mod.HttpRpcClient("http://x", **kw)
    session = ScriptedSession(script)
    c._session = lambda: session  # bypass thread-local session creation
    return c, session


def test_timeouts_are_retried(monkeypatch):
    monkeypatch.setattr(rpc_mod.time, "sleep", lambda s: None)
    c, s = client_with([requests.Timeout(), FakeResponse({"result": {"number": "0x10"}})])
    assert c.finalized_block() == 16
    assert s.script == []


def test_range_error_is_raised_immediately():
    c, _ = client_with(
        [FakeResponse({"error": {"code": -32602, "message": "Block range limit exceeded"}})]
    )
    with pytest.raises(rpc_mod.RangeTooLargeError):
        c.get_logs(address="0x", topics=None, from_block=1, to_block=100)


def test_repeated_timeouts_on_multi_block_get_logs_request_a_split():
    c, _ = client_with([requests.Timeout()] * 3, split_after_timeouts=3)
    with pytest.raises(rpc_mod.RangeTooLargeError):
        c.get_logs(address="0x", topics=None, from_block=1, to_block=2)


def test_single_block_get_logs_never_requests_a_split():
    c, _ = client_with([requests.Timeout()] * 4, split_after_timeouts=2, max_attempts=4)
    with pytest.raises(rpc_mod.RpcError) as info:
        c.get_logs(address="0x", topics=None, from_block=5, to_block=5)
    assert not isinstance(info.value, rpc_mod.RangeTooLargeError)


def test_other_errors_exhaust_attempts(monkeypatch):
    monkeypatch.setattr(rpc_mod.time, "sleep", lambda s: None)
    c, _ = client_with([FakeResponse({}, status=503)] * 2, max_attempts=2)
    with pytest.raises(rpc_mod.RpcError, match="after 2 attempts"):
        c.finalized_block()
