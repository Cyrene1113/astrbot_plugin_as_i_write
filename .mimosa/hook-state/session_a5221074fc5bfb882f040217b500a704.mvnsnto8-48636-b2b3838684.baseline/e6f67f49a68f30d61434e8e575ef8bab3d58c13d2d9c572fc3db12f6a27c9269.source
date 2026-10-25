"""Chara Card V3 JSON 转换与安全 PNG 角色卡解析。"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import struct
import zlib
from typing import Any

try:
    from .lore import normalize_character, normalize_lore_entry, normalize_world
except ImportError:
    from lore import normalize_character, normalize_lore_entry, normalize_world

SPEC = "chara_card_v3"
SPEC_VERSION = "3.0"
MAX_PNG_SIZE = 20 * 1024 * 1024
MAX_CHUNK_SIZE = 16 * 1024 * 1024

_CHARACTER_FIELDS = (
    "name", "description", "personality", "scenario", "first_mes", "mes_example",
    "system_prompt", "post_history_instructions", "tags", "creator_notes",
    "character_version", "alternate_greetings", "character_book",
)


def _envelope(data: dict[str, Any]) -> dict[str, Any]:
    return {"spec": SPEC, "spec_version": SPEC_VERSION, "data": data}


def _card_data(card: Any) -> dict[str, Any]:
    if not isinstance(card, dict):
        raise ValueError("角色卡必须是 JSON 对象")
    if card.get("spec") != SPEC:
        raise ValueError(f"不支持的角色卡 spec，必须为 {SPEC}")
    data = card.get("data")
    if not isinstance(data, dict):
        raise ValueError("角色卡缺少 data 对象")
    return copy.deepcopy(data)


def _entry_from_external(raw: Any) -> dict[str, Any]:
    item = copy.deepcopy(raw) if isinstance(raw, dict) else {"content": str(raw or "")}
    keys = item.get("keys", item.get("key", []))
    if isinstance(keys, str):
        keys = [part.strip() for part in keys.replace("\n", ",").split(",") if part.strip()]
    secondary = item.get("secondary_keys", item.get("secondary_key", []))
    if isinstance(secondary, str):
        secondary = [part.strip() for part in secondary.replace("\n", ",").split(",") if part.strip()]
    item["keys"] = keys if isinstance(keys, list) else []
    item["secondary_keys"] = secondary if isinstance(secondary, list) else []
    item["insertion_order"] = item.get("insertion_order", item.get("order", 100))
    item["insertion_position"] = item.get("insertion_position", item.get("position", "system"))
    if "enabled" not in item and "disable" in item:
        item["enabled"] = not bool(item["disable"])
    return normalize_lore_entry(item)


def _book_from_external(book: Any) -> dict[str, Any]:
    result = copy.deepcopy(book) if isinstance(book, dict) else {}
    entries = result.get("entries", [])
    if isinstance(entries, dict):
        entries = list(entries.values())
    result["entries"] = [_entry_from_external(item) for item in entries] if isinstance(entries, list) else []
    return result


def character_to_ccv3(character: dict) -> dict:
    """导出角色，插件私有字段放入 extensions 以保证往返无损。"""
    normalized = normalize_character(character)
    data = {key: copy.deepcopy(normalized.get(key)) for key in _CHARACTER_FIELDS}
    data["extensions"] = copy.deepcopy(normalized.get("extensions", {}))
    data["extensions"]["as_i_write"] = {"character": copy.deepcopy(normalized)}
    return _envelope(data)


def character_from_ccv3(card: dict) -> dict:
    """宽容读取 V3 角色卡，并恢复本插件扩展字段。"""
    data = _card_data(card)
    extensions = data.get("extensions", {})
    private = extensions.get("as_i_write", {}) if isinstance(extensions, dict) else {}
    stored = private.get("character", {}) if isinstance(private, dict) else {}
    result = copy.deepcopy(stored) if isinstance(stored, dict) else {}
    for key in _CHARACTER_FIELDS:
        if key in data:
            result[key] = copy.deepcopy(data[key])
    result["character_book"] = _book_from_external(result.get("character_book"))
    return normalize_character(result)


def world_to_ccv3(world: dict, name: str) -> dict:
    """把世界导出为仅含 character_book 的 V3 数据对象。"""
    normalized = normalize_world(world, name=name)
    data = {
        "name": name,
        "description": str(normalized.get("content") or ""),
        "character_book": {"name": name, "entries": copy.deepcopy(normalized["entries"])},
        "extensions": {"as_i_write": {"world": copy.deepcopy(normalized), "world_name": name}},
    }
    return _envelope(data)


def world_from_ccv3(card: dict) -> tuple[str, dict]:
    """从 V3 数据恢复世界名称和完整世界对象。"""
    data = _card_data(card)
    extensions = data.get("extensions", {})
    private = extensions.get("as_i_write", {}) if isinstance(extensions, dict) else {}
    stored = private.get("world", {}) if isinstance(private, dict) else {}
    world = copy.deepcopy(stored) if isinstance(stored, dict) else {}
    name = str(private.get("world_name") or data.get("name") or world.get("_name") or "").strip()
    if not name:
        raise ValueError("世界卡缺少名称")
    book = _book_from_external(data.get("character_book"))
    if book["entries"] or "entries" not in world:
        world["entries"] = book["entries"]
    if "content" not in world:
        world["content"] = str(data.get("description") or "")
    return name, normalize_world(world, name=name)


def _decode_payload(payload: str) -> dict[str, Any]:
    text = payload.strip()
    if not text:
        raise ValueError("PNG 角色卡 payload 为空")
    if text.startswith("{"):
        raw = text.encode("utf-8")
    else:
        try:
            raw = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("PNG 角色卡 payload 不是有效 base64") from exc
    try:
        card = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("PNG 角色卡 payload 不是有效 UTF-8 JSON") from exc
    if not isinstance(card, dict):
        raise ValueError("PNG 角色卡 JSON 必须是对象")
    return card


def parse_png_card(data: bytes, *, max_size: int = MAX_PNG_SIZE) -> dict:
    """解析 PNG 的 ccv3/chara 文本块，ccv3 优先；不执行嵌入内容。"""
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("PNG 数据必须为 bytes")
    if len(data) > max_size:
        raise ValueError(f"PNG 文件超过 {max_size // (1024 * 1024)}MB 上限")
    if bytes(data[:8]) != b"\x89PNG\r\n\x1a\n":
        raise ValueError("文件不是有效 PNG")
    offset = 8
    payloads: dict[str, str] = {}
    saw_iend = False
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError("PNG chunk 被截断")
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        if length > MAX_CHUNK_SIZE:
            raise ValueError("PNG chunk 超过安全长度上限")
        end = offset + 12 + length
        if end > len(data):
            raise ValueError("PNG chunk 长度越界或文件被截断")
        chunk_type = bytes(data[offset + 4:offset + 8])
        chunk_data = bytes(data[offset + 8:offset + 8 + length])
        if chunk_type == b"tEXt" and b"\0" in chunk_data:
            keyword, text = chunk_data.split(b"\0", 1)
            if keyword.decode("latin-1") in ("ccv3", "chara"):
                payloads[keyword.decode("latin-1")] = text.decode("latin-1")
        elif chunk_type == b"zTXt" and b"\0" in chunk_data:
            keyword, rest = chunk_data.split(b"\0", 1)
            key = keyword.decode("latin-1")
            if key in ("ccv3", "chara") and rest[:1] == b"\0":
                try:
                    payloads[key] = zlib.decompress(rest[1:]).decode("utf-8")
                except (zlib.error, UnicodeDecodeError) as exc:
                    raise ValueError("PNG zTXt 角色卡解压失败") from exc
        elif chunk_type == b"iTXt" and b"\0" in chunk_data:
            parts = chunk_data.split(b"\0", 5)
            if len(parts) == 6:
                key = parts[0].decode("latin-1")
                if key in ("ccv3", "chara"):
                    try:
                        text_data = zlib.decompress(parts[5]) if parts[1] == b"\x01" else parts[5]
                        payloads[key] = text_data.decode("utf-8")
                    except (zlib.error, UnicodeDecodeError) as exc:
                        raise ValueError("PNG iTXt 角色卡解码失败") from exc
        offset = end
        if chunk_type == b"IEND":
            saw_iend = True
            break
    if not saw_iend:
        raise ValueError("PNG 缺少 IEND 或文件被截断")
    payload = payloads.get("ccv3") or payloads.get("chara")
    if payload is None:
        raise ValueError("PNG 中未找到 ccv3 或 chara 角色卡块")
    return _decode_payload(payload)


def character_from_png(data: bytes) -> dict:
    return character_from_ccv3(parse_png_card(data))
