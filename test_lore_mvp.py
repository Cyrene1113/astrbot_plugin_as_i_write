"""第一阶段 MVP 的纯函数回归测试。"""

from lore import (
    CURRENT_SCHEMA_VERSION,
    build_adventure_context,
    get_world_eggs,
    normalize_character,
    normalize_collections,
    normalize_world,
)


def test_schema_and_legacy_world_migration() -> None:
    world = normalize_world({"content": "古老王国", "easter_eggs": ["月夜密门"]}, name="测试世界")
    assert world["schema_version"] == CURRENT_SCHEMA_VERSION
    assert world["content"] == "古老王国"
    assert len(world["entries"]) == 1
    assert world["entries"][0]["constant"] is True
    assert world["entries"][0]["content"] == "古老王国"
    assert get_world_eggs(world) == ["月夜密门"]
    assert world["eggs"][0]["text"] == "月夜密门"


def test_character_defaults_keep_legacy_fields() -> None:
    char = normalize_character({"name": "旅人", "skills": {"active": [{"name": "剑术"}]}})
    assert char["schema_version"] == CURRENT_SCHEMA_VERSION
    assert char["skills"]["active"][0]["name"] == "剑术"
    assert char["personality"] == ""
    assert char["character_book"] == {"entries": []}
    assert char["character_version"] == "1.0.0"


def test_matching_sort_budget_and_positions() -> None:
    world = {
        "entries": [
            {"name": "常驻", "content": "A", "constant": True, "priority": 1, "insertion_position": "system"},
            {"name": "命中高优先", "content": "BBBB", "keys": ["月亮"], "priority": 20, "insertion_order": 2, "insertion_position": "before_history"},
            {"name": "禁用", "content": "NO", "constant": True, "enabled": False, "priority": 99},
            {"name": "未命中", "content": "NO", "keys": ["太阳"], "priority": 50},
        ]
    }
    char = {
        "name": "旅人",
        "character_book": {"entries": [
            {"name": "角色常驻", "content": "CC", "constant": True, "priority": 10, "insertion_position": "after_char"}
        ]},
    }
    context = build_adventure_context(world, char, "月亮升起", "single", max_content_length=6)
    assert [item["name"] for item in context["matched_entries"]] == ["命中高优先", "角色常驻"]
    assert context["used_length"] == 6
    assert "BBBB" in context["positions"]["before_history"]
    assert "CC" in context["positions"]["after_char"]
    assert [item["name"] for item in context["matched_entries"]] == ["命中高优先", "角色常驻"]


def test_collection_migration_is_idempotent() -> None:
    chars, worlds, changed_c, changed_w = normalize_collections(
        {"1": {"name": "甲"}}, {"旧世界": {"content": "总览"}}
    )
    assert changed_c and changed_w
    chars2, worlds2, changed_c2, changed_w2 = normalize_collections(chars, worlds)
    assert chars2 == chars and worlds2 == worlds
    assert not changed_c2 and not changed_w2
