"""Target selection (with a fake deployments manager) and HTTP RPC retry behaviour."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
import requests

from ethswarm_chain_sync import rpc as rpc_mod
from ethswarm_chain_sync.deployments import SelectionError, all_targets, resolve_targets


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


class FakeManager:
    """Two Redistribution versions with different ``Committed`` signatures, plus Token."""

    DEPLOYMENTS = {
        "Redistribution": [
            FakeDeployment(
                "Redistribution",
                "v0.6.0",
                "0x" + "AA" * 20,
                30,
                [ev("Committed", "uint256", "bytes32"), ev("Paused", "address")],
            ),
            FakeDeployment(
                "Redistribution",
                "v0.9.1",
                "0x" + "BB" * 20,
                10,
                [ev("Committed", "uint256", "bytes32", "uint8"), ev("Paused", "address")],
            ),
        ],
        "PostageStamp": [
            FakeDeployment(
                "PostageStamp",
                "v0.6.0",
                "0x" + "CC" * 20,
                20,
                [ev("BatchCreated", "bytes32"), ev("Paused", "address")],
            ),
        ],
        "Token": [
            FakeDeployment(
                "Token",
                "v0.4.0",
                "0x" + "DD" * 20,
                1,
                [ev("Transfer", "address", "address", "uint256")],
            )
        ],
    }

    def network_info(self, network):
        return {"chain_id": 100}

    def versions(self, network):
        return ["v0.6.0"]

    def contract_names(self, version, network):
        return list(self.DEPLOYMENTS)

    def all_deployments(self, name, network):
        return self.DEPLOYMENTS[name]


def labels(targets):
    return [(t.contract, t.version, sorted(t.selected)) for t in targets]


def test_all_targets_lowercases_and_orders_by_block():
    ts = all_targets(manager=FakeManager())
    redis = [t for t in ts if t.contract == "Redistribution"]
    assert [t.version for t in redis] == ["v0.9.1", "v0.6.0"]
    assert redis[0].address == "0x" + "bb" * 20
    sigs = {t.version: {s.signature for s in t.schemas.values()} for t in redis}
    assert "Committed(uint256,bytes32,uint8)" in sigs["v0.9.1"]
    assert "Committed(uint256,bytes32)" in sigs["v0.6.0"]


def test_default_selection_excludes_tokens():
    contracts = {t.contract for t in resolve_targets(manager=FakeManager())}
    assert contracts == {"Redistribution", "PostageStamp"}


def test_contract_selectors():
    m = FakeManager()
    assert labels(resolve_targets(contracts=["Redistribution@v0.6.0"], manager=m)) == [
        ("Redistribution", "v0.6.0", ["Committed", "Paused"])
    ]
    assert len(resolve_targets(contracts=["Redistribution"], manager=m)) == 2
    assert labels(resolve_targets(contracts=["0x" + "dd" * 20], manager=m))[0][0] == "Token"


def test_event_selector_narrows_and_drops():
    m = FakeManager()
    assert labels(resolve_targets(events=["Committed"], manager=m)) == [
        ("Redistribution", "v0.9.1", ["Committed"]),
        ("Redistribution", "v0.6.0", ["Committed"]),
    ]
    by_sig = resolve_targets(events=["Committed(uint256,bytes32)"], manager=m)
    assert labels(by_sig) == [("Redistribution", "v0.6.0", ["Committed"])]
    paused = resolve_targets(contracts=["PostageStamp"], events=["Paused"], manager=m)
    assert labels(paused) == [("PostageStamp", "v0.6.0", ["Paused"])]


def test_selection_errors():
    m = FakeManager()
    with pytest.raises(SelectionError):
        resolve_targets(contracts=["Nope"], manager=m)
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
