"""settle/rpg 判定与结算纯函数回归测试（不 mock LLM）。

测试环境通常没有安装 astrbot：先尝试真实导入，失败则打桩
astrbot.api.event；再把插件目录包装成包，使 settle.py 的相对导入
（from .rpg import ...）可用。
"""

from __future__ import annotations

import importlib
import re
import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# ── astrbot 桩：仅满足 rpg.py 顶部的类型导入 ──
try:
    from astrbot.api.event import AstrMessageEvent  # noqa: F401
except Exception:  # noqa: BLE001
    for _name in [m for m in sys.modules if m == "astrbot" or m.startswith("astrbot.")]:
        del sys.modules[_name]
    _astrbot = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")
    _event = types.ModuleType("astrbot.api.event")

    class _StubAstrMessageEvent:
        """最小 AstrMessageEvent 桩。"""

    _event.AstrMessageEvent = _StubAstrMessageEvent
    _api.event = _event
    _astrbot.api = _api
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _api
    sys.modules["astrbot.api.event"] = _event

# ── 把插件目录包装成包，支持 settle.py 的相对导入 ──
_PKG = "astrbot_plugin_as_i_write"
if _PKG not in sys.modules:
    _pkg = types.ModuleType(_PKG)
    _pkg.__path__ = [str(_HERE)]
    sys.modules[_PKG] = _pkg

settle = importlib.import_module(f"{_PKG}.settle")
rpg = importlib.import_module(f"{_PKG}.rpg")
prompts = importlib.import_module(f"{_PKG}.prompts")

D20_LINE = re.compile(r"d20：(\d+)、(\d+)、(\d+)（按顺序取用")


def _make_char(**overrides) -> dict:
    char = {
        "name": "旅人",
        "attr": {"str": 10, "agi": 10, "spr": 10},
        "charm": "C",
        "luck": "C",
        "skills": {"active": [], "passive": []},
    }
    char.update(overrides)
    return char


# ── rpg.py 判定骰 ──


def test_roll_d20_range() -> None:
    rolls = rpg._roll_d20()
    assert len(rolls) == 3
    assert all(1 <= value <= 20 for value in rolls)
    assert len(rpg._roll_d20(5)) == 5


def test_build_judgment_context_single() -> None:
    text = rpg.build_judgment_context(_make_char())
    match = D20_LINE.search(text)
    assert match, text
    values = [int(g) for g in match.groups()]
    assert all(1 <= v <= 20 for v in values)
    assert "【本轮判定骰" in text
    assert "旅人" in text
    assert "体魄普通" in text  # 10 → 普通
    assert "仅对当前回合有效" in text
    assert "19~20 大成功" in text
    # 缺字段防御：attr 缺省走 BASE_ATTR
    fallback = rpg.build_judgment_context({})
    match2 = D20_LINE.search(fallback)
    assert match2
    assert "???" in fallback
    assert "薄弱" in fallback  # BASE_ATTR=5 → 薄弱
    assert isinstance(rpg.build_judgment_context(None), str)


def test_build_group_judgment_context() -> None:
    participants = [
        _make_char(name="甲"),
        _make_char(name="乙", attr={"str": 60, "agi": 8, "spr": 12}),
        "脏数据",
    ]
    text = rpg.build_group_judgment_context(participants)
    match = D20_LINE.search(text)
    assert match, text
    values = [int(g) for g in match.groups()]
    assert all(1 <= v <= 20 for v in values)
    assert "甲" in text and "乙" in text
    assert "体魄超凡" in text  # 60 → 超凡
    assert "反应薄弱" in text  # 8 → 薄弱
    assert text.count("：体魄") == 2  # 脏数据被跳过
    assert "19~20 大成功" in text
    empty = rpg.build_group_judgment_context([])
    assert "【本轮判定骰" in empty
    assert "：体魄" not in empty
    assert isinstance(rpg.build_group_judgment_context(None), str)


# ── settle.py：parse_settle_payload ──


def test_parse_settle_payload_variants() -> None:
    good = (
        '{"attr_delta": {"str": 1, "agi": 0, "spr": 0}, "new_abilities": [], '
        '"race": null, "job": null, "ending": null, "lore_extracts": []}'
    )
    parsed = settle.parse_settle_payload(good)
    assert parsed is not None
    assert parsed["attr_delta"] == {"str": 1, "agi": 0, "spr": 0}

    fenced = "```json\n" + good + "\n```"
    assert settle.parse_settle_payload(fenced) == parsed

    wrapped = "结算结果如下：\n" + good + "\n以上。"
    assert settle.parse_settle_payload(wrapped) == parsed

    # 坏 JSON / 空输入 / 非字符串
    assert settle.parse_settle_payload("{oops") is None
    assert settle.parse_settle_payload("完全没有JSON") is None
    assert settle.parse_settle_payload("") is None
    assert settle.parse_settle_payload(None) is None
    assert settle.parse_settle_payload(123) is None

    # 缺字段与未知字段：只保留契约内合法字段，不抛异常
    assert settle.parse_settle_payload('{"attr_delta": {"str": 1}, "extra": 1}') == {
        "attr_delta": {"str": 1}
    }
    assert settle.parse_settle_payload('{"new_abilities": ["bad", {"name": "x"}]}') == {
        "new_abilities": [{"name": "x"}]
    }


# ── settle.py：clamp_attr_delta ──


def test_clamp_attr_delta_rules() -> None:
    char = _make_char()  # 10/10/10
    assert settle.clamp_attr_delta(char, {"str": 5, "agi": -3, "spr": 1.9}) == {
        "str": 2, "agi": 0, "spr": 1
    }
    # 合计超 3：按 str>agi>spr 优先截断
    assert settle.clamp_attr_delta(char, {"spr": 2, "agi": 2, "str": 2}) == {
        "str": 2, "agi": 1, "spr": 0
    }
    # 非法类型全部拒绝
    assert settle.clamp_attr_delta(char, "bad") == {"str": 0, "agi": 0, "spr": 0}
    assert settle.clamp_attr_delta(char, {"str": True}) == {"str": 0, "agi": 0, "spr": 0}
    assert settle.clamp_attr_delta(char, {"str": "3"}) == {"str": 0, "agi": 0, "spr": 0}
    assert settle.clamp_attr_delta(None, {"str": 1}) == {"str": 1, "agi": 0, "spr": 0}
    # MAX_ATTR 上限：已满的属性不能再涨
    maxed = {"attr": {"str": rpg.MAX_ATTR, "agi": 0, "spr": 0}}
    applied = settle.clamp_attr_delta(maxed, {"str": 2, "agi": 2})
    assert applied["str"] == 0 and applied["agi"] == 2


# ── settle.py：apply_settle_result ──


def test_apply_attr_growth_recalc() -> None:
    char = _make_char()
    changes, ending = settle.apply_settle_result(
        char, None, {"attr_delta": {"str": 1, "spr": 2, "agi": 5}}
    )
    assert char["attr"] == {"str": 11, "agi": 12, "spr": 10}  # str 优先，agi 拿剩余 2 点，spr 被截断
    assert char["hp"] == int(100 * (1 + 0.1 * 11))  # recalc 已生效
    assert char["power"] == rpg._calc_power(11, 12, 10, "C", "C")
    assert any("力量 +1" in c for c in changes)
    assert any("敏捷 +2" in c for c in changes)
    assert not any("精神" in c for c in changes)
    assert ending is None
    assert isinstance(char.get("updated_at"), float) and char["updated_at"] > 0


def test_apply_new_abilities_cap_and_dedup() -> None:
    char = _make_char()
    char["skills"]["active"] = [{"name": "旧剑术", "desc": "x", "mastery": "熟练", "lv": 2}]
    result = {
        "new_abilities": [
            {"type": "active", "name": "旧剑术", "desc": "与旧能力重名"},
            {"type": "active", "name": "火球术", "desc": "扔火球"},
            {"type": "passive", "name": "夜视", "desc": "黑暗中视物"},
            {"type": "active", "name": "火球术", "desc": "批内重复"},
            {"type": "active", "name": "剑气斩", "desc": "补满栏位"},
            {"type": "magic", "name": "无效类型", "desc": "应被忽略"},
            {"type": "passive", "name": "", "desc": "无名"},
        ]
    }
    changes, _ = settle.apply_settle_result(char, None, result)
    active = char["skills"]["active"]
    passive = char["skills"]["passive"]
    assert [s["name"] for s in active] == ["旧剑术", "火球术", "剑气斩"]
    assert active[-1] == {"name": "剑气斩", "desc": "补满栏位", "mastery": "生疏", "lv": 1}
    assert passive == [{"name": "夜视", "desc": "黑暗中视物", "mastery": "常驻", "lv": 0}]
    assert any("火球术" in c for c in changes)
    assert any("夜视" in c for c in changes)
    # 上限后再来一条主动能力应被跳过
    changes2, _ = settle.apply_settle_result(
        char, None, {"new_abilities": [{"type": "active", "name": "第四招", "desc": "超上限"}]}
    )
    assert [s["name"] for s in char["skills"]["active"]] == ["旧剑术", "火球术", "剑气斩"]
    assert not any("第四招" in c for c in changes2)


def test_apply_race_and_job_change() -> None:
    char = _make_char()
    changes, ending = settle.apply_settle_result(char, None, {"race": "精灵", "job": "剑士"})
    assert char["race"]["name"] == "精灵"
    assert char["race"]["innate_passives"] == []
    assert char["race"]["innate_negatives"] == []
    assert char["race"]["attr_bonus"] == {"str": 0, "agi": 0, "spr": 0}
    assert char["job"]["name"] == "剑士"
    assert char["job"]["job_skill"] is None
    assert char["job"]["attr_bonus"] == {"str": 0, "agi": 0, "spr": 0}
    assert any("精灵" in c for c in changes)
    assert any("剑士" in c for c in changes)
    assert ending is None
    # 同名不覆盖
    changes2, _ = settle.apply_settle_result(char, None, {"race": "精灵"})
    assert changes2 == []
    # 改名才覆盖
    settle.apply_settle_result(char, None, {"race": "兽人"})
    assert char["race"]["name"] == "兽人"
    # recalc 兼容 race/job attr_bonus 结构
    assert char["power"] == rpg._calc_power(
        char["attr"]["str"], char["attr"]["agi"], char["attr"]["spr"], "C", "C"
    )


def test_apply_ending_normalization() -> None:
    char = _make_char()
    result = {
        "ending": {
            "title": "标" * 30,
            "summary": "概" * 100,
            "tags": ["冒险", " 成长 ", "", "牺牲", "额外", "再多"],
        }
    }
    changes, ending = settle.apply_settle_result(char, None, result)
    assert ending is not None
    assert ending["title"] == "标" * 20
    assert len(ending["summary"]) == 60
    assert ending["tags"] == ["冒险", "成长", "牺牲"]
    assert any("结局" in c for c in changes)
    # 非法/空结局
    _, ending2 = settle.apply_settle_result(_make_char(), None, {"ending": "不是字典"})
    assert ending2 is None
    _, ending3 = settle.apply_settle_result(
        _make_char(), None, {"ending": {"title": "", "summary": "", "tags": []}}
    )
    assert ending3 == {"title": "未命名结局", "summary": "", "tags": []}


def test_apply_lore_extracts_dedup_and_cap() -> None:
    world = {"entries": [{"name": "已有条目", "content": "旧"}]}
    result = {
        "lore_extracts": [
            {"name": "新地点", "keys": ["月亮", "废墟", "k"] * 3, "content": "世" * 500},
            {"name": "已有条目", "keys": ["x"], "content": "重复名应跳过"},
            {"name": "第二条", "keys": "线索", "content": "新的世界规则"},
            {"name": "第三条", "keys": [], "content": "超出两条上限"},
            {"name": "", "content": "无名应跳过"},
        ]
    }
    changes, _ = settle.apply_settle_result(_make_char(), world, result)
    names = [e["name"] for e in world["entries"]]
    assert names == ["已有条目", "新地点", "第二条"]
    added = world["entries"][1]
    assert len(added["content"]) == settle.MAX_LORE_CONTENT
    assert added["keys"] == ["月亮", "废墟", "k"]  # 去重且不超过上限
    assert added.get("id")  # normalize_lore_entry 已补齐结构
    assert world["entries"][2]["keys"] == ["线索"]  # 字符串 keys 兼容
    lore_notes = [c for c in changes if "世界书沉淀" in c]
    assert len(lore_notes) == 2


def test_apply_lore_entry_limit_200() -> None:
    world = {"entries": [{"name": f"条目{i}", "content": "x"} for i in range(200)]}
    result = {"lore_extracts": [{"name": "超限条目", "keys": [], "content": "不应追加"}]}
    settle.apply_settle_result(_make_char(), world, result)
    assert len(world["entries"]) == 200
    assert all(e["name"] != "超限条目" for e in world["entries"])


def test_apply_world_none_and_bad_result() -> None:
    char = _make_char()
    # world 缺失：lore 部分静默跳过
    changes, ending = settle.apply_settle_result(
        char, None, {"lore_extracts": [{"name": "x", "content": "y"}]}
    )
    assert changes == [] and ending is None
    # world 非字典同样安全
    changes2, _ = settle.apply_settle_result(
        char, "not-a-world", {"lore_extracts": [{"name": "x", "content": "y"}]}
    )
    assert changes2 == []
    # 非法 result / char
    assert settle.apply_settle_result(char, None, None) == ([], None)
    assert settle.apply_settle_result(char, None, "bad") == ([], None)
    assert settle.apply_settle_result(None, None, {"race": "x"}) == ([], None)


# ── prompts.py 判定规则段 ──


def test_prompts_contain_judgment_rules_and_format() -> None:
    cases = (
        (
            prompts.SINGLE_SYSTEM_PROMPT,
            {
                "theme": "武侠",
                "world_context": "W",
                "character_context": "C",
                "player_stats": "P",
                "world_tier_context": "T",
                "special_notes": "N",
            },
        ),
        (
            prompts.GROUP_SYSTEM_PROMPT,
            {
                "theme": "武侠",
                "world_context": "W",
                "roster": "R",
                "world_tier_context": "T",
                "special_notes": "N",
            },
        ),
    )
    for template, kwargs in cases:
        rendered = template.format(**kwargs)
        assert "【轻规则判定骰】" in rendered
        assert "每个骰值只用一次" in rendered
        assert "映射为大失败/失败/险胜/成功/大成功" in rendered
    assert "只输出一个 JSON 对象" in settle.SETTLE_EXTRACT_PROMPT
