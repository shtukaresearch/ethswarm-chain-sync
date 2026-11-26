"""Schema derivation and log decoding: names attached to fields and struct components."""

from __future__ import annotations

import pytest
from conftest import ADDRESS, BY_NAME, SCHEMAS, encode_log
from eth_abi import encode

from ethswarm_chain_sync.decode import DecodeError, decode_args, decode_log
from ethswarm_chain_sync.schema import EventSchema, canonical_type, event_schemas


def test_canonical_type_expands_tuples_and_arrays():
    param = {
        "type": "tuple[]",
        "components": [
            {"name": "a", "type": "address"},
            {"name": "b", "type": "tuple", "components": [{"name": "c", "type": "uint8[2]"}]},
        ],
    }
    assert canonical_type(param) == "(address,(uint8[2]))[]"


def test_topic0_matches_erc20_transfer():
    schema = EventSchema.from_abi(
        {
            "type": "event",
            "name": "Transfer",
            "inputs": [
                {"name": "from", "type": "address", "indexed": True},
                {"name": "to", "type": "address", "indexed": True},
                {"name": "value", "type": "uint256", "indexed": False},
            ],
        }
    )
    assert schema.topic0 == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def test_overloaded_event_names_get_signature_keys():
    abi = [
        {"type": "event", "name": "E", "inputs": [{"name": "x", "type": "uint256"}]},
        {"type": "event", "name": "E", "inputs": [{"name": "x", "type": "address"}]},
        {"type": "event", "name": "F", "inputs": []},
        {"type": "event", "name": "G", "anonymous": True, "inputs": []},
    ]
    keys = sorted(s.key for s in event_schemas(abi).values())
    assert keys == ["E(address)", "E(uint256)", "F"]


def test_decode_log_names_fields_and_normalizes_values():
    log = encode_log(
        BY_NAME["BatchCreated"],
        {
            "batchId": b"\x01" * 32,
            "totalAmount": 2**255 + 1,
            "owner": "0x" + "Ab" * 20,
            "immutableFlag": True,
        },
        block=42,
        log_index=3,
        tx_index=5,
    )
    row = decode_log(log, SCHEMAS, chain_id=100, contract="PostageStamp", version="v1")
    assert row == {
        "chain_id": 100,
        "contract": "PostageStamp",
        "version": "v1",
        "address": ADDRESS,
        "event": "BatchCreated",
        "signature": "BatchCreated(bytes32,uint256,address,bool)",
        "block_number": 42,
        "block_timestamp": "2023-11-14T22:16:50Z",
        "transaction_hash": "0x" + f"{42:064x}",
        "transaction_index": 5,
        "log_index": 3,
        "args": {
            "batchId": "0x" + "01" * 32,
            "totalAmount": 2**255 + 1,
            "owner": "0x" + "ab" * 20,
            "immutableFlag": True,
        },
    }


def test_decode_struct_components_are_named():
    log = encode_log(
        BY_NAME["WinnerSelected"],
        {"winner": (b"\x02" * 32, "0x" + "11" * 20, 7)},
        block=1,
        log_index=0,
    )
    row = decode_log(log, SCHEMAS, chain_id=100, contract="Redistribution", version="v1")
    assert row["args"] == {
        "winner": {"overlay": "0x" + "02" * 32, "owner": "0x" + "11" * 20, "stake": 7}
    }


def test_decode_struct_arrays_nested_structs_and_unnamed_params():
    schema = EventSchema.from_abi(
        {
            "type": "event",
            "name": "X",
            "inputs": [
                {
                    "name": "items",
                    "type": "tuple[]",
                    "indexed": False,
                    "components": [
                        {"name": "id", "type": "uint8"},
                        {
                            "name": "inner",
                            "type": "tuple",
                            "components": [{"name": "", "type": "bytes"}],
                        },
                    ],
                },
                {"name": "", "type": "string", "indexed": False},
            ],
        }
    )
    data = encode(["(uint8,(bytes))[]", "string"], [[(1, (b"\xff",)), (2, (b"",))], "hi"])
    args = decode_args(schema, [schema.topic0], "0x" + data.hex())
    assert args == {
        "items": [{"id": 1, "inner": {"_0": "0xff"}}, {"id": 2, "inner": {"_0": "0x"}}],
        "_1": "hi",
    }


def test_indexed_dynamic_value_is_kept_as_its_hash():
    schema = EventSchema.from_abi(
        {
            "type": "event",
            "name": "Named",
            "inputs": [{"name": "label", "type": "string", "indexed": True}],
        }
    )
    topic = "0x" + "9f" * 32
    assert decode_args(schema, [schema.topic0, topic], "0x") == {"label": topic}


def test_unknown_topic0_and_wrong_topic_count_are_errors():
    log = encode_log(BY_NAME["PriceUpdate"], {"price": 1}, block=1, log_index=0)
    with pytest.raises(DecodeError, match="unknown topic0"):
        decode_log(
            {**log, "topics": ["0x" + "00" * 32]}, SCHEMAS, chain_id=100, contract="C", version="v"
        )
    with pytest.raises(DecodeError, match="indexed topics"):
        decode_log(
            {**log, "topics": [*log["topics"], "0x" + "00" * 32]},
            SCHEMAS,
            chain_id=100,
            contract="C",
            version="v",
        )


def test_missing_block_timestamp_is_an_error():
    log = encode_log(BY_NAME["PriceUpdate"], {"price": 1}, block=1, log_index=0)
    del log["blockTimestamp"]
    with pytest.raises(DecodeError, match="blockTimestamp"):
        decode_log(log, SCHEMAS, chain_id=100, contract="C", version="v")
