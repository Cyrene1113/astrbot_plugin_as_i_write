"""archetypes 图鉴库与 rpg._validate_attrs 修复回归测试。

测试环境通常没有安装 astrbot：先尝试真实导入，失败则打桩
astrbot.api（含 logger）与 astrbot.api.event；再把插件目录包装成包，
使 archetypes.py 的相对导入（from .core import ...）可用。
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent


class _StubLogger:
    """最小 logger 桩（core.py 顶部需要 astrbot.api.logger）。"""

    def info(self, *args, **kwargs) -> None:
        pass

    def warning(self, *args, **kwargs) -> None:
        pass

    def error(self, *args, **kwargs) -> None:
        pass


# ── astrbot 桩：仅满足 core.py / rpg.py 顶部的导入 ──
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
    _api.logger = _StubLogger()
    _astrbot.api = _api
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _api
    sys.modules["astrbot.api.event"] = _event

# 若桩由其他测试文件先注册（如 test_settle.py）而缺少 logger，此处补齐
_api_module = sys.modules.get("astrbot.api")
if _api_module is not None and not hasattr(_api_module, "logger"):
    _api_module.logger = _StubLogger()

# ── 把插件目录包装成包，支持 archetypes.py 的相对导入 ──
_PKG = "astrbot_plugin_as_i_write"
if _PKG not in sys.modules:
    _pkg = types.ModuleType(_PKG)
    _pkg.__path__ = [str(_HERE)]
    sys.modules[_PKG] = _pkg

archetypes = importlib.import_module(f"{_PKG}.archetypes")
core = importlib.import_module(f"{_PKG}.core")
rpg = importlib.import_module(f"{_PKG}.rpg")


def _patch_data_dir(tmp_path, monkeypatch) -> None:
    """把 core.DATA_DIR 指向 tmp_path，使 _save_json 的防穿越校验放行测试路径。"""
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)


# ── normalize_archetype ──


def test_normalize_type_fallback_and_casefold() -> None:
    assert archetypes.normalize_archetype({"name": "x", "type": "warrior"})["type"] == "race"
    assert archetypes.normalize_archetype({"name": "x", "type": "JOB "})["type"] == "job"
    assert archetypes.normalize_archetype({"name": "x"}, type_="job")["type"] == "job"
    assert archetypes.normalize_archetype({"name": "x", "type": ""})["type"] == "race"
    assert archetypes.normalize_archetype({"name": "x"})["type"] == "race"


def test_normalize_attr_bonus_fill_and_invalid() -> None:
    filled = archetypes.normalize_archetype({"name": "x", "attr_bonus": {"str": "2", "spr": 1.9}})
    assert filled["attr_bonus"] == {"str": 2, "agi": 0, "spr": 1}
    bad = archetypes.normalize_archetype({"name": "x", "attr_bonus": "bad"})
    assert bad["attr_bonus"] == {"str": 0, "agi": 0, "spr": 0}
    missing = archetypes.normalize_archetype({"name": "x"})
    assert missing["attr_bonus"] == {"str": 0, "agi": 0, "spr": 0}


def test_normalize_string_list_cleaning() -> None:
    entry = archetypes.normalize_archetype({
        "name": "x",
        "innate_passives": [" 夜视 ", "", "   ", None],
        "innate_negatives": "畏光",
        "tags": ["奇幻", " 主流 ", ""],
    })
    assert entry["innate_passives"] == ["夜视"]
    assert entry["innate_negatives"] == ["畏光"]  # 单字符串兼容
    assert entry["tags"] == ["奇幻", "主流"]
    non_list = archetypes.normalize_archetype({"name": "x", "tags": 123})
    assert non_list["tags"] == []


def test_normalize_job_skill_structure() -> None:
    ok = archetypes.normalize_archetype({
        "name": "战士", "type": "job", "job_skill": {"name": " 重击 ", "desc": "d", "lv": 3}
    })
    assert ok["job_skill"] == {"name": "重击", "desc": "d"}  # 只保留 name/desc
    assert archetypes.normalize_archetype({"name": "j", "type": "job", "job_skill": "x"})["job_skill"] is None
    assert archetypes.normalize_archetype({"name": "j", "type": "job", "job_skill": {"desc": "无名"}})["job_skill"] is None
    assert archetypes.normalize_archetype({"name": "j", "type": "job"})["job_skill"] is None
    race = archetypes.normalize_archetype({"name": "r", "job_skill": {"name": "s"}})
    assert race["job_skill"] is not None  # 结构层不区分 race/job，展示层再区分


def test_normalize_wraps_non_dict_and_keeps_unknown_fields() -> None:
    entry = archetypes.normalize_archetype("精灵", name="fallback")
    assert entry["name"] == "精灵"
    assert entry["description"] == ""
    assert entry["builtin"] is False
    assert entry["created_at"] == 0.0 and entry["updated_at"] == 0.0
    src = {"name": "x", "custom": {"a": 1}, "extra": "保留"}
    result = archetypes.normalize_archetype(src)
    assert result["custom"] == {"a": 1} and result["extra"] == "保留"
    src["custom"]["a"] = 99  # deepcopy：入参后续变化不影响结果
    assert result["custom"]["a"] == 1


# ── find_archetype ──

_DB = {
    "精灵": {"name": "精灵", "type": "race", "attr_bonus": {"str": 0, "agi": 2, "spr": 2}},
    "战士": {"name": "战士", "type": "job", "attr_bonus": {"str": 3, "agi": 0, "spr": 0}},
}


def test_find_exact_hit_and_type_filter() -> None:
    assert archetypes.find_archetype(_DB, "精灵")["name"] == "精灵"
    assert archetypes.find_archetype(_DB, "战士", "job")["type"] == "job"
    assert archetypes.find_archetype(_DB, "战士", "race") is None
    assert archetypes.find_archetype(_DB, "精灵", "JOB") is None  # type 过滤大小写不敏感


def test_find_fuzzy_casefold_and_space_stripped() -> None:
    db = {"Wood Elf": {"name": "Wood Elf", "type": "race"}}
    assert archetypes.find_archetype(db, "  woodelf ")["name"] == "Wood Elf"
    assert archetypes.find_archetype(db, "WOOD ELF")["name"] == "Wood Elf"
    assert archetypes.find_archetype(_DB, "精 灵")["name"] == "精灵"  # 去内部空格
    # 条目 name 与字典键不一致时也能命中
    db2 = {"key_elf": {"name": "高等精灵", "type": "race"}}
    assert archetypes.find_archetype(db2, "高等精灵")["name"] == "高等精灵"


def test_find_miss_returns_none_and_deepcopy() -> None:
    assert archetypes.find_archetype(_DB, "不存在") is None
    assert archetypes.find_archetype(_DB, "  ") is None
    assert archetypes.find_archetype(None, "精灵") is None
    assert archetypes.find_archetype(_DB, None) is None
    hit = archetypes.find_archetype(_DB, "精灵")
    hit["name"] = "改过的精灵"
    assert _DB["精灵"]["name"] == "精灵"  # 返回 deepcopied 条目


# ── archetype_to_char_field ──


def test_to_char_field_race_structure() -> None:
    race = archetypes.archetype_to_char_field({
        "name": "精灵", "type": "race", "description": "d",
        "attr_bonus": {"str": 0, "agi": 2, "spr": 2},
        "innate_passives": ["夜视", "轻盈步伐"], "innate_negatives": ["体质孱弱"],
        "job_skill": {"name": "不该出现在 race 字段"}, "extra": 1,
    })
    assert race == {
        "name": "精灵", "description": "d",
        "innate_passives": ["夜视", "轻盈步伐"], "innate_negatives": ["体质孱弱"],
        "attr_bonus": {"str": 0, "agi": 2, "spr": 2},
    }


def test_to_char_field_job_structure() -> None:
    job = archetypes.archetype_to_char_field({
        "name": "战士", "type": "job", "description": "d2",
        "attr_bonus": {"str": 3, "agi": 0, "spr": 0},
        "job_skill": {"name": "重击", "desc": "x"}, "innate_passives": ["不该出现在 job 字段"],
    })
    assert job == {
        "name": "战士", "description": "d2",
        "job_skill": {"name": "重击", "desc": "x"},
        "attr_bonus": {"str": 3, "agi": 0, "spr": 0},
    }
    jobless = archetypes.archetype_to_char_field({"name": "剑士", "type": "job"})
    assert jobless["job_skill"] is None
    assert jobless["attr_bonus"] == {"str": 0, "agi": 0, "spr": 0}


def test_to_char_field_compatible_with_recalc() -> None:
    race = archetypes.archetype_to_char_field(_DB["精灵"])
    job = archetypes.archetype_to_char_field(_DB["战士"])
    char = {
        "attr": {"str": 10, "agi": 10, "spr": 10}, "charm": "C", "luck": "C",
        "race": race, "job": job,
    }
    rpg._recalc_rpg(char)
    assert char["power"] == rpg._calc_power(13, 12, 12, "C", "C")  # 10+0+3 / 10+2+0 / 10+2+0


# ── render_archetype_line ──


def test_render_line_race_with_passive_and_negative() -> None:
    line = archetypes.render_archetype_line({
        "name": "精灵", "type": "race",
        "attr_bonus": {"str": 0, "agi": 2, "spr": 2},
        "innate_passives": ["夜视", "轻盈步伐"], "innate_negatives": ["体质孱弱"],
    })
    assert line == "精灵（敏捷+2 精神+2｜被动：夜视、轻盈步伐｜负面：体质孱弱）"


def test_render_line_zero_bonus_and_segments_optional() -> None:
    assert archetypes.render_archetype_line({
        "name": "人类", "type": "race", "innate_passives": ["适应与学习"],
    }) == "人类（无补正｜被动：适应与学习）"
    assert archetypes.render_archetype_line({"name": "侏儒", "type": "race"}) == "侏儒（无补正）"
    assert archetypes.render_archetype_line({}) == "未命名（无补正）"


def test_render_line_job_with_skill_and_negative_bonus() -> None:
    line = archetypes.render_archetype_line({
        "name": "战士", "type": "job", "attr_bonus": {"str": 3, "agi": 0, "spr": 0},
        "job_skill": {"name": "重击", "desc": "x"},
    })
    assert line == "战士（力量+3｜专属：重击）"
    undead = archetypes.render_archetype_line({
        "name": "亡灵", "type": "race", "attr_bonus": {"str": 1, "agi": -1, "spr": 3},
        "innate_negatives": ["日光厌恶性状"],
    })
    assert "敏捷-1" in undead and "无补正" not in undead


# ── seed_builtin_archetypes / load_archetypes ──


def test_seed_builtin_seeds_empty_dir(tmp_path, monkeypatch) -> None:
    _patch_data_dir(tmp_path, monkeypatch)
    path = tmp_path / "archetypes.json"
    assert not path.exists()
    seeded = archetypes.seed_builtin_archetypes(path)
    assert path.exists()
    assert len(seeded) >= 22  # 10 种族 + 12 职业
    assert all(entry["builtin"] is True for entry in seeded.values())
    assert all(entry["created_at"] > 0 for entry in seeded.values())
    assert all(-2 <= v <= 3 for entry in seeded.values() for v in entry["attr_bonus"].values())
    assert seeded["精灵"]["innate_passives"] == ["夜视", "轻盈步伐"]
    assert seeded["战士"]["job_skill"]["name"] == "重击"
    assert seeded["精灵"]["type"] == "race" and seeded["战士"]["type"] == "job"


def test_seed_builtin_idempotent_and_no_overwrite(tmp_path, monkeypatch) -> None:
    _patch_data_dir(tmp_path, monkeypatch)
    path = tmp_path / "archetypes.json"
    seeded = archetypes.seed_builtin_archetypes(path)
    again = archetypes.seed_builtin_archetypes(path)
    assert len(again) == len(seeded)  # 不重复播种
    # 已有个别条目的文件不被覆盖、不合并
    custom_path = tmp_path / "custom.json"
    core._save_json(custom_path, {"自定义种族": {"name": "自定义种族", "type": "race"}})
    loaded = archetypes.seed_builtin_archetypes(custom_path)
    assert list(loaded.keys()) == ["自定义种族"]
    raw = json.loads(custom_path.read_text(encoding="utf-8"))
    assert list(raw.keys()) == ["自定义种族"]  # 文件未被追加内置条目


def test_load_archetypes_normalizes_and_keys_by_name(tmp_path, monkeypatch) -> None:
    _patch_data_dir(tmp_path, monkeypatch)
    path = tmp_path / "archetypes.json"
    core._save_json(path, {
        "schema_version": 1,
        "键名": {"type": "job", "attr_bonus": {"str": "2"}, "job_skill": {"name": "技能"}, "junk": 1},
        "空名": {"name": "", "type": "?"},
    })
    loaded = archetypes.load_archetypes(path)
    assert "schema_version" not in loaded
    entry = loaded["键名"]
    assert entry["type"] == "job"
    assert entry["attr_bonus"] == {"str": 2, "agi": 0, "spr": 0}
    assert entry["job_skill"] == {"name": "技能", "desc": ""}
    assert entry["junk"] == 1  # 未知字段保留
    assert loaded["空名"]["name"] == "空名"  # 名为空时回退字典键
    # 空文件 / 缺文件 → 空 dict
    assert archetypes.load_archetypes(tmp_path / "missing.json") == {}


# ── rpg._validate_attrs（随机角色校验倒置修复）──


def test_validate_attrs_random_locked() -> None:
    ok, msg = rpg._validate_attrs(999, 999, 999, False, True)
    assert ok is False and msg
    assert "随机角色" in msg
    ok2, _ = rpg._validate_attrs(3, 3, 3, True, True)  # 管理员也不能改随机角色
    assert ok2 is False


def test_validate_attrs_normal_character() -> None:
    ok, msg = rpg._validate_attrs(10, 10, 10, False, False)
    assert ok is True and msg == ""
    over_sum = rpg._validate_attrs(10, 10, 11, False, False)  # 31 > 30
    assert over_sum[0] is False and "超过上限" in over_sum[1]
    below_min = rpg._validate_attrs(2, 10, 10, False, False)
    assert below_min[0] is False and "不能低于" in below_min[1]
    admin_ok, _ = rpg._validate_attrs(100, 100, 100, True, False)  # 管理员绕过总和上限
    assert admin_ok is True
