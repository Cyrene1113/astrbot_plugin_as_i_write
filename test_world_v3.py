"""世界观 v3 结构化板块、冒险上下文注入与 AI 助手解析的回归测试。"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# ── astrbot 桩（与 test_archetypes.py 相同的引导方式）──
try:
    from astrbot.api.event import AstrMessageEvent  # noqa: F401
except Exception:  # noqa: BLE001
    for _name in [m for m in sys.modules if m == "astrbot" or m.startswith("astrbot.")]:
        del sys.modules[_name]
    _astrbot = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")
    _event = types.ModuleType("astrbot.api.event")
    _event.AstrMessageEvent = type("_StubAstrMessageEvent", (), {})
    _api.event = _event
    _api.logger = type("_StubLogger", (), {"info": lambda *a, **k: None,
                                           "warning": lambda *a, **k: None,
                                           "error": lambda *a, **k: None})()
    _astrbot.api = _api
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _api
    sys.modules["astrbot.api.event"] = _event

_api_module = sys.modules.get("astrbot.api")
if _api_module is not None and not hasattr(_api_module, "logger"):
    _api_module.logger = type("_L", (), {"info": lambda *a, **k: None,
                                         "warning": lambda *a, **k: None,
                                         "error": lambda *a, **k: None})()

_PKG = "astrbot_plugin_as_i_write"
if _PKG not in sys.modules:
    _pkg = types.ModuleType(_PKG)
    _pkg.__path__ = [str(_HERE)]
    sys.modules[_PKG] = _pkg

lore = importlib.import_module(f"{_PKG}.lore")
ai_gen = importlib.import_module(f"{_PKG}.ai_gen")
web_api = importlib.import_module(f"{_PKG}.web_api")


# ── normalize_world：结构化板块归一化 ──


def test_normalize_world_sections_defaults_and_idempotent() -> None:
    world = lore.normalize_world({"content": "总览文本"}, name="测试界")
    for key in ("overview", "npc", "plotlines", "clues", "locations", "factions", "eggs", "entries"):
        assert key in world
    assert world["overview"]["title"] == "测试界"
    assert world["overview"]["description"] == "总览文本"
    assert world["schema_version"] == lore.CURRENT_SCHEMA_VERSION
    again = lore.normalize_world(world, name="测试界")
    assert again == world  # 二次归一化完全幂等


def test_normalize_world_legacy_plot_nodes_and_power_fields() -> None:
    world = lore.normalize_world({
        "content": "旧世界",
        "power_tier": "超凡",
        "power_reference": "士兵：80~150",
        "plot_nodes": [{
            "title": "失踪的商队", "type": "支线", "summary": "商队在雾谷失踪",
            "trigger": "玩家抵达雾谷", "related_npc": "雾谷猎手, 老村长",
        }],
        "easter_eggs": ["老村长的秘密"],
    }, name="旧世界")
    assert world["overview"]["power_tier"] == "超凡"
    assert world["overview"]["power_reference"] == "士兵：80~150"
    assert world["plotlines"][0]["title"] == "失踪的商队"
    assert world["plotlines"][0]["related_npc"] == ["雾谷猎手", "老村长"]
    assert world["plotlines"][0]["stages"][0]["trigger"] == "玩家抵达雾谷"
    assert world["eggs"][0]["text"] == "老村长的秘密"
    assert "easter_eggs" not in world


def test_normalize_world_section_item_fields() -> None:
    world = lore.normalize_world({
        "npc": [{"name": "艾尔莎", "title": "守卫长", "alignment": "友方", "description": "d",
                 "traits": "冷静, 护短", "power": "2800", "is_key": True}],
        "clues": [{"name": "第13声钟响", "content": "c", "related_npc": "艾尔莎"}],
        "locations": [{"name": "钟楼", "is_important": True}],
        "factions": [{"name": "夜巡会", "members": "艾尔莎,无名"}],
        "plotlines": [{"title": "钟楼之谜", "stages": [{"title": "起兆", "clues": "第13声钟响"}]}],
    }, name="钟界")
    npc = world["npc"][0]
    assert npc["traits"] == ["冷静", "护短"]  # 字符串拆分为列表
    assert npc["power"] == 2800
    clue = world["clues"][0]
    assert clue["related_npc"] == ["艾尔莎"]
    assert clue["revealed"] is False
    assert clue["type"] == "物证"
    assert world["locations"][0]["type"] == "地标"
    assert world["factions"][0]["members"] == ["艾尔莎", "无名"]
    assert world["plotlines"][0]["stages"][0]["clues"] == ["第13声钟响"]


# ── render_world_sections ──


def _rich_world() -> dict:
    return lore.normalize_world({
        "content": "测试世界总览",
        "power_tier": "神话",
        "power_reference": "巡逻守卫：80~150\n守卫长：300~500",
        "npc": [{"name": "艾尔莎", "title": "守卫长", "alignment": "友方", "description": "冷静的守卫长",
                 "traits": ["冷静"], "dialog_samples": ["钟声不会说谎"], "is_key": True},
                {"name": "", "description": "无名条目应被跳过"}],
        "plotlines": [{"title": "钟楼之谜", "type": "主线", "summary": "钟楼每逢雨夜多敲一声",
                       "stages": [{"title": "起兆", "description": "守卫开始失眠", "trigger": "雨夜",
                                   "clues": ["第13声钟响"]}],
                       "related_npc": ["艾尔莎"]}],
        "clues": [{"name": "第13声钟响", "type": "环境", "content": "多出的钟声", "related_npc": ["艾尔莎"]}],
        "locations": [{"name": "钟楼", "type": "地标", "is_important": True, "related_npcs": ["艾尔莎"]}],
        "factions": [{"name": "夜巡会", "alignment": "守序", "members": ["艾尔莎"]}],
        "eggs": [{"text": "钟楼顶藏着旧钥匙", "trigger_condition": "雨夜登楼"}],
        "entries": [{"name": "钟楼的构造", "keys": ["钟楼"], "content": "钟楼共十二层"}],
    }, name="钟界")


def test_render_world_sections_contains_all_blocks() -> None:
    text = lore.render_world_sections(_rich_world())
    assert "【世界人物（GM 参考）】" in text
    assert "艾尔莎" in text and "★关键" in text and "钟声不会说谎" in text
    assert "【剧情线" in text and "钟楼之谜" in text and "阶段「起兆」" in text
    assert "【伏笔清单" in text and "【未揭晓】" in text and "第13声钟响" in text
    assert "【重要地点】" in text and "钟楼" in text
    assert "【势力】" in text and "夜巡会" in text
    assert "【彩蛋储备" in text and "旧钥匙" in text and "雨夜登楼" in text


def test_render_world_sections_revealed_state_and_budget() -> None:
    world = _rich_world()
    world["clues"][0]["revealed"] = True
    text = lore.render_world_sections(world)
    assert "【已揭晓】" in text and "【未揭晓】" not in text
    # 预算极小时截断并提示
    tiny = lore.render_world_sections(world, max_chars=120)
    assert len(tiny) <= 140
    assert "截断" in tiny
    # 空世界输出空串
    assert lore.render_world_sections({"entries": []}) == ""


def test_adventure_context_includes_sections() -> None:
    world = _rich_world()
    context = lore.build_adventure_context(world, None, "雨夜的钟楼", "single", 4000)
    assert "【世界人物（GM 参考）】" in context["world_context"]
    assert context["world_sections"]
    # 空世界时 sections 为空且不产生多余空行
    empty = lore.build_adventure_context({"content": ""}, None, "x", "single", 4000)
    assert empty["world_sections"] == ""


# ── ai_gen：JSON 解析与补全合并 ──


def test_parse_llm_json_fences_and_noise() -> None:
    assert ai_gen.parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert ai_gen.parse_llm_json('好的，以下是结果：\n{"a": {"b": 2}}\n希望有帮助') == {"a": {"b": 2}}
    assert ai_gen.parse_llm_json('{"a": 1}') == {"a": 1}
    assert ai_gen.parse_llm_json("完全没有 JSON") is None
    assert ai_gen.parse_llm_json('{"a": 1') is None
    assert ai_gen.parse_llm_json('{"a": 1}}') is None  # 多余右括号解析失败（与结算解析同策略）


def test_missing_world_sections_detection() -> None:
    assert set(ai_gen.missing_world_sections({"entries": []})) == {
        "npc", "plotlines", "clues", "locations", "factions", "eggs", "entries", "overview"}
    rich = _rich_world()
    assert ai_gen.missing_world_sections(rich) == []  # 齐备的世界无缺失（自动总览条目不算实条目）


def test_merge_world_draft_only_fills_missing() -> None:
    world = lore.normalize_world({
        "content": "半成品世界",
        "power_tier": "神话",
        "npc": [{"name": "艾尔莎", "title": "守卫长"}],
        "plotlines": [{"title": "钟楼之谜", "type": "主线", "summary": "s"}],
    }, name="半成品")
    draft = {
        "npc": [{"name": "不应覆盖的新人", "title": "", "alignment": "敌方"}],
        "clues": [{"name": "新线索", "type": "传闻", "content": "x"}],
        "locations": [{"name": "新地点", "type": "地标", "description": ""}],
        "overview": {"power_tier": "不应覆盖", "power_reference": "新参考"},
    }
    merged, added = ai_gen.merge_world_draft(world, draft)
    assert "伏笔" in added and "地点" in added and "概要" in added
    assert merged["npc"][0]["name"] == "艾尔莎"  # 已有板块不被覆盖
    assert merged["plotlines"][0]["title"] == "钟楼之谜"
    assert merged["clues"][0]["name"] == "新线索"   # 缺失板块被填上
    assert merged["locations"][0]["name"] == "新地点"
    # overview：已有 power_tier 不覆盖；缺失 power_reference 被补上
    assert merged["overview"]["power_tier"] == "神话"
    assert merged["overview"]["power_reference"] == "新参考"
    # 非突变：传入的 world（往往是插件内存中的活数据）不被污染
    assert world.get("clues") in (None, [])
    assert world.get("locations") in (None, [])
    assert "power_reference" not in world.get("overview", {}) or not world["overview"].get("power_reference")
    assert len(world.get("entries", [])) == 1  # 仅自动总览，未被 append


def test_normalize_drafts() -> None:
    world_draft = ai_gen.normalize_world_draft({"overview": {"title": "星界"}, "npc": "非法"}, name="星界")
    assert world_draft["overview"]["title"] == "星界"
    assert world_draft["npc"] == []
    arch = ai_gen.normalize_archetype_draft(
        {"name": "星咏者", "type": "job", "attr_bonus": {"str": 99, "spr": -9},
         "job_skills": [{"name": "星辉", "desc": "d"}]}, name="星咏者", type_="job")
    assert arch["attr_bonus"]["str"] == 3 and arch["attr_bonus"]["spr"] == -3  # 钳制到 ±3
    assert arch["job_skills"][0]["name"] == "星辉"
    char = ai_gen.normalize_character_draft({
        "name": "测试者", "description": "d", "attr": {"str": 99, "agi": 0, "spr": 5},
        "skills": {"active": [{"name": "斩击", "desc": "x"}]}})
    assert char["attr"]["str"] == 20 and char["attr"]["agi"] == 1  # 钳制到 1~20
    assert "power" in char and "hp" in char  # RPG 层已补齐


def test_world_summary_for_complete_marks_missing() -> None:
    summary = ai_gen.world_summary_for_complete({"entries": []}, "空界")
    assert "空界" in summary
    assert summary.count("【缺失】") >= 7
    rich_summary = ai_gen.world_summary_for_complete(_rich_world(), "钟界")
    assert "艾尔莎" in rich_summary and "钟楼之谜" in rich_summary


# ── rpg 战力锚点覆盖（power_override）──


def test_power_override_in_recalc() -> None:
    rpg = importlib.import_module(f"{_PKG}.rpg")
    char = {"attr": {"str": 5, "agi": 5, "spr": 1000}, "charm": "S", "luck": "S",
            "power_override": 9999}
    rpg._recalc_rpg(char)
    assert char["power"] == 9999  # 手工锚定的神明级不被公式覆盖
    assert char["hp"] > 0 and char["mp"] > 0
    auto = {"attr": {"str": 5, "agi": 5, "spr": 1000}, "charm": "S", "luck": "S"}
    rpg._recalc_rpg(auto)
    assert auto["power"] == rpg._calc_power(5, 5, 1000, "S", "S")  # 无覆盖时按公式
    bad = {"attr": {"str": 5, "agi": 5, "spr": 5}, "charm": "C", "luck": "C",
           "power_override": "abc"}
    rpg._recalc_rpg(bad)  # 非法覆盖被忽略，不抛异常
    assert bad["power"] == rpg._calc_power(5, 5, 5, "C", "C")


# ── web_api._sanitize_character ──


def test_sanitize_character_clamps_and_recalcs() -> None:
    cleaned = web_api._sanitize_character({
        "name": "测试", "attr": {"str": 99999, "agi": "8", "spr": None},
        "charm": "SSS", "luck": "b",
        "skills": {"active": [{"name": "斩击", "desc": "d"}, {"desc": "无名被丢"},
                              "字符串技能", {"name": "四", "desc": ""}, {"name": "五", "desc": ""}],
                   "passive": [{"name": "夜视"}]},
        "has_adventured": "true",
        "power_override": 9999,
    })
    assert cleaned["attr"] == {"str": 1000, "agi": 8, "spr": 5}
    assert cleaned["charm"] == "C"  # 非法评级回退
    assert cleaned["luck"] == "B"   # 大小写归一
    assert len(cleaned["skills"]["active"]) == 3  # 超限截断
    assert cleaned["skills"]["active"][0] == {"name": "斩击", "desc": "d", "mastery": "生疏", "lv": 1}
    assert cleaned["skills"]["active"][1]["name"] == "字符串技能"
    assert cleaned["skills"]["passive"][0]["mastery"] == "常驻"
    assert cleaned["has_adventured"] is True
    assert cleaned["power"] == 9999  # 覆盖锚点生效
    assert "attr_locked" not in cleaned  # 遗留字段被剥离
    cleared = web_api._sanitize_character({"name": "x", "power_override": 0})
    assert "power_override" not in cleared  # 0/留空清除覆盖
    cleared2 = web_api._sanitize_character({"name": "x", "power_override": "abc"})
    assert "power_override" not in cleared2


def test_sanitize_character_preserves_valid_grades() -> None:
    cleaned = web_api._sanitize_character({"name": "x", "charm": "S", "luck": "A+"})
    assert cleaned["charm"] == "S" and cleaned["luck"] == "A+"
