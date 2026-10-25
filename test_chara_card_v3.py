import base64
import json
import struct
import zlib

import pytest

from chara_card_v3 import (
    character_from_ccv3, character_from_png, character_to_ccv3,
    parse_png_card, world_from_ccv3, world_to_ccv3,
)


def test_character_round_trip_keeps_rpg_fields():
    source = {"name": "旅人", "description": "测试", "attr": {"str": 9}, "skills": {"active": [{"name": "剑术"}]}}
    restored = character_from_ccv3(character_to_ccv3(source))
    assert restored["name"] == "旅人"
    assert restored["attr"] == {"str": 9}
    assert restored["skills"]["active"][0]["name"] == "剑术"


def test_world_round_trip_keeps_entries_and_legacy():
    source = {"content": "总览", "eggs": ["彩蛋"], "entries": [{"id": "fixed", "name": "门", "keys": ["钥匙"], "content": "开启", "enabled": False, "priority": 7, "insertion_order": 3, "insertion_position": "after_history"}]}
    name, restored = world_from_ccv3(world_to_ccv3(source, "世界"))
    entry = next(item for item in restored["entries"] if item["id"] == "fixed")
    assert name == "世界" and restored["content"] == "总览"
    assert (entry["keys"], entry["enabled"], entry["priority"], entry["insertion_position"]) == (["钥匙"], False, 7, "after_history")
    assert restored["eggs"][0]["text"] == "彩蛋"
    assert "easter_eggs" not in restored


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)


def _png(*texts):
    raw = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    for key, payload in texts:
        raw += _chunk(b"tEXt", key.encode() + b"\0" + payload.encode())
    return raw + _chunk(b"IEND", b"")


def test_png_ccv3_priority_and_errors():
    low = base64.b64encode(json.dumps(character_to_ccv3({"name": "低"})).encode()).decode()
    high = base64.b64encode(json.dumps(character_to_ccv3({"name": "高"})).encode()).decode()
    assert character_from_png(_png(("chara", low), ("ccv3", high)))["name"] == "高"
    with pytest.raises(ValueError):
        parse_png_card(b"not png")
    with pytest.raises(ValueError):
        parse_png_card(_png(("ccv3", "bad-base64")))


def test_invalid_spec():
    with pytest.raises(ValueError):
        character_from_ccv3({"spec": "v2", "data": {}})
