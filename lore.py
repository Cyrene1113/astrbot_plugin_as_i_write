"""角色卡/世界书 schema 兼容层与统一冒险上下文编译器。"""

from __future__ import annotations

import copy
import json
import uuid
from typing import Any, Iterable

CURRENT_SCHEMA_VERSION = 2
DEFAULT_LORE_BUDGET = 4000
INSERTION_POSITIONS = (
    "system",
    "before_char",
    "after_char",
    "before_history",
    "after_history",
)

CHARACTER_DEFAULTS: dict[str, Any] = {
    "personality": "",
    "scenario": "",
    "first_mes": "",
    "mes_example": "",
    "system_prompt": "",
    "post_history_instructions": "",
    "tags": [],
    "creator_notes": "",
    "character_version": "1.0.0",
    "alternate_greetings": [],
    "character_book": {"entries": []},
}


def _new_entry_id() -> str:
    return uuid.uuid4().hex


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def normalize_lore_entry(entry: Any, *, fallback_name: str = "未命名条目") -> dict[str, Any]:
    """补齐世界书条目字段，同时保留未知扩展字段。"""
    if not isinstance(entry, dict):
        entry = {"content": str(entry or "")}
    result = copy.deepcopy(entry)
    result["id"] = str(result.get("id") or _new_entry_id())
    result["name"] = str(result.get("name") or result.get("comment") or fallback_name)
    result["keys"] = _as_string_list(result.get("keys"))
    result["secondary_keys"] = _as_string_list(result.get("secondary_keys"))
    result["content"] = str(result.get("content") or "")
    result["constant"] = bool(result.get("constant", False))
    result["enabled"] = bool(result.get("enabled", True))
    try:
        result["priority"] = int(result.get("priority", 10))
    except (TypeError, ValueError):
        result["priority"] = 10
    try:
        result["insertion_order"] = int(result.get("insertion_order", 100))
    except (TypeError, ValueError):
        result["insertion_order"] = 100
    position = str(result.get("insertion_position") or result.get("position") or "system")
    result["insertion_position"] = position if position in INSERTION_POSITIONS else "system"
    return result


def _egg_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("text") or item.get("content") or "").strip()
    return str(item or "").strip()


def sync_world_eggs(world: dict[str, Any]) -> dict[str, Any]:
    """合并 eggs/easter_eggs，并同步写回结构化与旧版字段。"""
    structured: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in (world.get("eggs", []), world.get("easter_eggs", [])):
        if not isinstance(source, list):
            continue
        for item in source:
            text = _egg_text(item)
            if not text or text in seen:
                continue
            seen.add(text)
            if isinstance(item, dict):
                egg = copy.deepcopy(item)
                egg["text"] = text
                egg.setdefault("trigger_condition", "")
                egg.setdefault("reward", "")
            else:
                egg = {"text": text, "trigger_condition": "", "reward": ""}
            structured.append(egg)
    world["eggs"] = structured
    world["easter_eggs"] = [item["text"] for item in structured]
    return world


def get_world_eggs(world: Any) -> list[str]:
    """统一读取彩蛋文本，不要求调用方知道新旧字段。"""
    if not isinstance(world, dict):
        return []
    normalized = sync_world_eggs(copy.deepcopy(world))
    return normalized["easter_eggs"]


def normalize_world(world: Any, *, name: str = "") -> dict[str, Any]:
    """把旧 world 原地语义迁移为 schema v2，不删除任何旧字段。"""
    if not isinstance(world, dict):
        world = {"content": str(world or "")}
    result = copy.deepcopy(world)
    result["schema_version"] = CURRENT_SCHEMA_VERSION
    if name:
        result.setdefault("_name", name)
    content = str(result.get("content") or "")
    entries = result.get("entries")
    if not isinstance(entries, list):
        entries = []
    normalized_entries = [
        normalize_lore_entry(item, fallback_name=f"{name or '世界观'}条目")
        for item in entries
    ]
    # 旧 content 仅在尚无同内容条目时迁移，避免每次加载重复生成。
    if content.strip() and not any(item.get("content") == content for item in normalized_entries):
        normalized_entries.insert(0, normalize_lore_entry({
            "name": f"{name or '世界观'}总览",
            "keys": [],
            "secondary_keys": [],
            "content": content,
            "constant": True,
            "enabled": True,
            "priority": 10,
            "insertion_order": 100,
            "insertion_position": "system",
        }))
    result["entries"] = normalized_entries
    return sync_world_eggs(result)


def normalize_character(character: Any) -> dict[str, Any]:
    """补齐 Chara Card 风格字段，并保留插件 RPG 旧字段。"""
    if not isinstance(character, dict):
        character = {"description": str(character or "")}
    result = copy.deepcopy(character)
    result["schema_version"] = CURRENT_SCHEMA_VERSION
    for key, default in CHARACTER_DEFAULTS.items():
        if key not in result or result[key] is None:
            result[key] = copy.deepcopy(default)
    result["tags"] = _as_string_list(result.get("tags"))
    result["alternate_greetings"] = _as_string_list(result.get("alternate_greetings"))
    book = result.get("character_book")
    if not isinstance(book, dict):
        book = {"entries": []}
    else:
        book = copy.deepcopy(book)
    entries = book.get("entries")
    if not isinstance(entries, list):
        entries = []
    book["entries"] = [normalize_lore_entry(item, fallback_name="角色设定") for item in entries]
    result["character_book"] = book
    return result


def normalize_collections(
    characters: Any, worlds: Any
) -> tuple[dict[str, Any], dict[str, Any], bool, bool]:
    """规范化两份顶层映射，并返回各自是否发生变化。"""
    raw_characters = characters if isinstance(characters, dict) else {}
    raw_worlds = worlds if isinstance(worlds, dict) else {}
    normalized_characters = {
        str(uid): normalize_character(character)
        for uid, character in raw_characters.items()
        if str(uid) != "schema_version"
    }
    normalized_worlds = {
        str(name): normalize_world(world, name=str(name))
        for name, world in raw_worlds.items()
        if str(name) != "schema_version"
    }
    return (
        normalized_characters,
        normalized_worlds,
        normalized_characters != raw_characters,
        normalized_worlds != raw_worlds,
    )


def _history_text(recent_history: Any) -> str:
    if isinstance(recent_history, str):
        return recent_history
    if not isinstance(recent_history, Iterable):
        return str(recent_history or "")
    parts: list[str] = []
    for item in recent_history:
        if isinstance(item, dict):
            parts.append(str(item.get("content") or ""))
        else:
            parts.append(str(item or ""))
    return "\n".join(parts)


def _entry_matches(entry: dict[str, Any], current_text: str) -> bool:
    if entry.get("constant"):
        return True
    folded = current_text.casefold()
    return any(key.casefold() in folded for key in entry.get("keys", []) if key)


def _collect_entries(world: dict[str, Any], character: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for source, owner in (
        (world.get("entries", []), "world"),
        (character.get("character_book", {}).get("entries", []), "character"),
    ):
        if not isinstance(source, list):
            continue
        for raw in source:
            entry = normalize_lore_entry(raw)
            entry["_owner"] = owner
            entries.append(entry)
    return entries


def _character_text(character: dict[str, Any]) -> str:
    parts = []
    labels = (
        ("name", "角色"),
        ("description", "描述"),
        ("personality", "性格"),
        ("scenario", "情景"),
        ("system_prompt", "角色系统指令"),
        ("post_history_instructions", "历史后指令"),
    )
    for key, label in labels:
        value = str(character.get(key) or "").strip()
        if value:
            parts.append(f"【{label}】{value}")
    return "\n".join(parts)


def build_adventure_context(
    world: Any,
    character: Any,
    recent_history: Any,
    mode: str,
    max_content_length: int = DEFAULT_LORE_BUDGET,
) -> dict[str, Any]:
    """统一编译单人/群体冒险使用的世界、角色和世界书上下文。

    返回值中的 ``positions`` 对应五个简单插入位置；``world_context`` 与
    ``character_context`` 已按位置合成为可直接填入现有提示词模板的文本。
    长度预算只裁剪世界书条目内容，不裁剪角色基础资料。
    """
    world_data = normalize_world(world)
    char_data = normalize_character(character) if character else {}
    current_text = "\n".join(filter(None, (
        _history_text(recent_history),
        _character_text(char_data),
    )))
    matched = [
        entry for entry in _collect_entries(world_data, char_data)
        if entry.get("enabled", True) and entry.get("content", "").strip()
        and _entry_matches(entry, current_text)
    ]
    matched.sort(key=lambda item: (-item["priority"], item["insertion_order"], item["id"]))

    try:
        remaining = max(0, int(max_content_length))
    except (TypeError, ValueError):
        remaining = DEFAULT_LORE_BUDGET
    positions: dict[str, list[str]] = {position: [] for position in INSERTION_POSITIONS}
    used_entries: list[dict[str, Any]] = []
    for entry in matched:
        if remaining <= 0:
            break
        content = entry["content"].strip()
        accepted = content[:remaining]
        if not accepted:
            continue
        position = entry["insertion_position"]
        positions[position].append(f"【世界书·{entry['name']}】\n{accepted}")
        used_entries.append({
            "id": entry["id"],
            "name": entry["name"],
            "owner": entry["_owner"],
            "position": position,
            "content_length": len(accepted),
            "truncated": len(accepted) < len(content),
        })
        remaining -= len(accepted)

    rendered = {key: "\n\n".join(value) for key, value in positions.items()}
    world_name = str(world_data.get("_name") or world_data.get("name") or "").strip()
    world_header = f"你所在的世界是「{world_name}」。" if world_name else ""
    character_base = _character_text(char_data)
    world_context = "\n\n".join(filter(None, (
        rendered["system"],
        rendered["before_char"],
        world_header,
    )))
    character_context = "\n\n".join(filter(None, (
        character_base,
        rendered["after_char"],
        rendered["before_history"],
        rendered["after_history"],
    )))
    return {
        "mode": str(mode or "single"),
        "world": world_data,
        "character": char_data,
        "positions": rendered,
        "world_context": world_context,
        "character_context": character_context,
        "matched_entries": used_entries,
        "budget": int(max_content_length),
        "used_length": int(max_content_length) - remaining,
    }


def context_debug_json(context: dict[str, Any]) -> str:
    """供诊断与测试输出使用，确保结果可 JSON 序列化。"""
    return json.dumps(context, ensure_ascii=False, indent=2)
