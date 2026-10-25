"""种族/职业图鉴库：内置模板 + 自定义条目的加载、检索与角色字段转换。"""

from __future__ import annotations

import copy
import time

from .core import _load_json, _save_json

# type 只允许这两个值
ARCHETYPE_TYPES = ("race", "job")

_ATTR_KEYS = ("str", "agi", "spr")


def _as_string_list(value) -> list[str]:
    """清洗为字符串列表：strip 后丢弃空项；单个字符串兼容为单元素列表。"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if item is not None and str(item).strip()]


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _normalize_attr_bonus(value) -> dict[str, int]:
    """补齐 str/agi/spr 三键 int，非法转 0。"""
    source = value if isinstance(value, dict) else {}
    bonus: dict[str, int] = {}
    for key in _ATTR_KEYS:
        try:
            bonus[key] = int(source.get(key, 0))
        except (TypeError, ValueError):
            bonus[key] = 0
    return bonus


def _normalize_job_skill(value) -> dict[str, str] | None:
    """job_skill 只保留 {"name","desc"}；无名或非法结构一律视为 None。"""
    if not isinstance(value, dict):
        return None
    name = str(value.get("name") or "").strip()
    if not name:
        return None
    return {"name": name, "desc": str(value.get("desc") or "")}


def normalize_archetype(data, *, name: str = "", type_: str = "race") -> dict:
    """补齐图鉴条目字段，同时保留未知扩展字段（deepcopy，不改入参）。"""
    if not isinstance(data, dict):
        data = {"name": str(data or "")}
    result = copy.deepcopy(data)
    result["name"] = str(result.get("name") or name or "").strip()
    type_value = str(result.get("type") or type_ or "race").strip().casefold()
    result["type"] = type_value if type_value in ARCHETYPE_TYPES else "race"
    result["description"] = str(result.get("description") or "")
    result["attr_bonus"] = _normalize_attr_bonus(result.get("attr_bonus"))
    result["innate_passives"] = _as_string_list(result.get("innate_passives"))
    result["innate_negatives"] = _as_string_list(result.get("innate_negatives"))
    result["job_skill"] = _normalize_job_skill(result.get("job_skill"))
    result["tags"] = _as_string_list(result.get("tags"))
    result["builtin"] = bool(result.get("builtin", False))
    result["created_at"] = _as_float(result.get("created_at"))
    result["updated_at"] = _as_float(result.get("updated_at"))
    return result


def find_archetype(archetypes: dict, name: str, type_: str | None = None) -> dict | None:
    """按名检索图鉴条目。

    先做精确匹配（字典键或条目 name，可带 type 过滤）；找不到再退一步：
    双方 strip + casefold + 去掉全部内部空白后比较。命中返回 deepcopied 条目。
    """
    if not isinstance(archetypes, dict):
        return None
    wanted = str(name or "").strip()
    if not wanted:
        return None
    wanted_type = str(type_).strip().casefold() if type_ else None

    def _type_ok(entry: dict) -> bool:
        if wanted_type is None:
            return True
        return str(entry.get("type") or "").strip().casefold() == wanted_type

    # 第一轮：精确匹配
    for key, entry in archetypes.items():
        if not isinstance(entry, dict):
            continue
        if str(key).strip() == wanted and _type_ok(entry):
            return copy.deepcopy(entry)
        if str(entry.get("name") or "").strip() == wanted and _type_ok(entry):
            return copy.deepcopy(entry)

    # 第二轮：模糊匹配（忽略大小写与所有内部空白，含全角空格）
    def _fold(text) -> str:
        return "".join(str(text or "").split()).casefold()

    folded = _fold(wanted)
    if folded:
        for key, entry in archetypes.items():
            if not isinstance(entry, dict):
                continue
            if not _type_ok(entry):
                continue
            if _fold(key) == folded or _fold(entry.get("name")) == folded:
                return copy.deepcopy(entry)
    return None


def archetype_to_char_field(arche: dict) -> dict:
    """把图鉴条目转换为角色卡 race/job 字段结构（兼容 _recalc_rpg 消费格式）。"""
    entry = normalize_archetype(arche if isinstance(arche, dict) else {})
    bonus = dict(entry["attr_bonus"])
    if entry["type"] == "job":
        skill = entry["job_skill"]
        return {
            "name": entry["name"],
            "description": entry["description"],
            "job_skill": copy.deepcopy(skill) if skill else None,
            "attr_bonus": bonus,
        }
    return {
        "name": entry["name"],
        "description": entry["description"],
        "innate_passives": list(entry["innate_passives"]),
        "innate_negatives": list(entry["innate_negatives"]),
        "attr_bonus": bonus,
    }


def render_archetype_line(arche: dict) -> str:
    """图鉴列表单行展示，如：精灵（敏捷+2 精神+2｜被动：夜视、轻盈步伐）。

    补正为 0 的项不显示，全零显示「无补正」；被动/负面/专属技能存在才显示对应段。
    纯文本，无 emoji（emoji 由调用方添加）。
    """
    entry = normalize_archetype(arche if isinstance(arche, dict) else {})
    bonus = entry["attr_bonus"]
    labels = (("str", "力量"), ("agi", "敏捷"), ("spr", "精神"))
    bonus_parts = [
        f"{label}{'+' if bonus[key] > 0 else ''}{bonus[key]}"
        for key, label in labels
        if bonus.get(key, 0) != 0
    ]
    segments = [" ".join(bonus_parts) if bonus_parts else "无补正"]
    if entry["innate_passives"]:
        segments.append(f"被动：{'、'.join(entry['innate_passives'])}")
    if entry["innate_negatives"]:
        segments.append(f"负面：{'、'.join(entry['innate_negatives'])}")
    if entry["type"] == "job" and entry["job_skill"]:
        segments.append(f"专属：{entry['job_skill']['name']}")
    return f"{entry['name'] or '未命名'}（{'｜'.join(segments)}）"


def load_archetypes(path) -> dict:
    """加载图鉴文件，逐条 normalize，键为条目名。"""
    raw = _load_json(path)
    if not isinstance(raw, dict):
        return {}
    loaded: dict[str, dict] = {}
    for key, value in raw.items():
        if str(key) == "schema_version":
            continue
        entry = normalize_archetype(value, name=str(key))
        loaded[entry["name"] or str(key)] = entry
    return loaded


def seed_builtin_archetypes(path) -> dict:
    """播种内置图鉴：文件不存在或为空时全量写入 builtin 模板并返回。

    文件已有内容时原样返回已加载数据——不合并、不重复播种，文件即真相。
    """
    loaded = load_archetypes(path)
    if loaded:
        return loaded
    now = time.time()
    seeded: dict[str, dict] = {}
    for key, template in DEFAULT_ARCHETYPES.items():
        entry = normalize_archetype(
            template, name=str(key), type_=str(template.get("type") or "race")
        )
        entry["builtin"] = True
        entry["created_at"] = now
        entry["updated_at"] = now
        seeded[entry["name"] or str(key)] = entry
    _save_json(path, seeded)
    return seeded


DEFAULT_ARCHETYPES: dict[str, dict] = {
    # ── 种族（10） ──
    "人类": {
        "name": "人类", "type": "race",
        "description": "适应力极强的平凡种族，在任何世界都能扎根成长。",
        "attr_bonus": {"str": 0, "agi": 0, "spr": 0},
        "innate_passives": ["适应与学习"],
        "innate_negatives": [],
    },
    "精灵": {
        "name": "精灵", "type": "race",
        "description": "长寿优雅的森林民族，感官敏锐而身形单薄。",
        "attr_bonus": {"str": 0, "agi": 2, "spr": 2},
        "innate_passives": ["夜视", "轻盈步伐"],
        "innate_negatives": ["体质孱弱"],
    },
    "矮人": {
        "name": "矮人", "type": "race",
        "description": "山脉深处的工匠种族，体魄坚韧且百毒难侵。",
        "attr_bonus": {"str": 2, "agi": 0, "spr": 1},
        "innate_passives": ["抗毒", "黑暗视觉"],
        "innate_negatives": [],
    },
    "兽人": {
        "name": "兽人", "type": "race",
        "description": "崇尚力量的荒野战士，伤势越重斗志越旺。",
        "attr_bonus": {"str": 3, "agi": 1, "spr": 0},
        "innate_passives": ["狂血战意"],
        "innate_negatives": ["易怒冲动"],
    },
    "半身人": {
        "name": "半身人", "type": "race",
        "description": "爱好安逸的矮小种族，总能逢凶化吉。",
        "attr_bonus": {"str": 0, "agi": 3, "spr": 0},
        "innate_passives": ["天生幸运", "藏匿行家"],
        "innate_negatives": [],
    },
    "龙裔": {
        "name": "龙裔", "type": "race",
        "description": "流淌巨龙血脉的裔民，天生具备龙威与坚鳞。",
        "attr_bonus": {"str": 2, "agi": 0, "spr": 2},
        "innate_passives": ["龙息吐息", "鳞甲护体"],
        "innate_negatives": [],
    },
    "亡灵": {
        "name": "亡灵", "type": "race",
        "description": "挣脱死亡规则的存在，不知疲倦却畏惧阳光。",
        "attr_bonus": {"str": 1, "agi": -1, "spr": 3},
        "innate_passives": ["不倦不食"],
        "innate_negatives": ["日光厌恶性状"],
    },
    "侏儒": {
        "name": "侏儒", "type": "race",
        "description": "痴迷发明的小巧种族，满脑子奇思妙想。",
        "attr_bonus": {"str": 0, "agi": 1, "spr": 2},
        "innate_passives": ["机巧发明"],
        "innate_negatives": [],
    },
    "半精灵": {
        "name": "半精灵", "type": "race",
        "description": "人类与精灵的混血，兼得两族之长却两边都不完全归属。",
        "attr_bonus": {"str": 0, "agi": 1, "spr": 1},
        "innate_passives": ["魅惑抗性"],
        "innate_negatives": [],
    },
    "兽化人": {
        "name": "兽化人", "type": "race",
        "description": "保留兽形的野性种族，直觉敏锐却畏银。",
        "attr_bonus": {"str": 2, "agi": 2, "spr": 0},
        "innate_passives": ["野性直觉"],
        "innate_negatives": ["银器过敏"],
    },
    # ── 职业（12） ──
    "战士": {
        "name": "战士", "type": "job",
        "description": "精通武器与格挡的一线搏杀者。",
        "attr_bonus": {"str": 3, "agi": 0, "spr": 0},
        "job_skill": {"name": "重击", "desc": "蓄力挥出沉重一击，可破坏敌方格挡。"},
    },
    "法师": {
        "name": "法师", "type": "job",
        "description": "以学识驾驭奥术之力的施法者。",
        "attr_bonus": {"str": 0, "agi": 1, "spr": 3},
        "job_skill": {"name": "奥术飞弹", "desc": "凝聚魔力射出数枚追踪奥术箭。"},
    },
    "游侠": {
        "name": "游侠", "type": "job",
        "description": "行走荒野的猎手与向导。",
        "attr_bonus": {"str": 1, "agi": 2, "spr": 1},
        "job_skill": {"name": "精准射击", "desc": "屏息瞄准，对远处目标造成精准打击。"},
    },
    "盗贼": {
        "name": "盗贼", "type": "job",
        "description": "游走于阴影与规则之间的边缘人。",
        "attr_bonus": {"str": 0, "agi": 3, "spr": 1},
        "job_skill": {"name": "暗影步", "desc": "融入阴影，瞬间移动到目标身后。"},
    },
    "牧师": {
        "name": "牧师", "type": "job",
        "description": "侍奉神明、以信仰济世的虔修者。",
        "attr_bonus": {"str": 1, "agi": 0, "spr": 2},
        "job_skill": {"name": "治愈祈祷", "desc": "向神明祈愿，恢复伤者的伤势。"},
    },
    "圣骑士": {
        "name": "圣骑士", "type": "job",
        "description": "立下圣誓的圣武士，信念即铠甲。",
        "attr_bonus": {"str": 2, "agi": 0, "spr": 2},
        "job_skill": {"name": "圣光审判", "desc": "以圣光裁决邪恶，对不洁之物额外有效。"},
    },
    "德鲁伊": {
        "name": "德鲁伊", "type": "job",
        "description": "沟通自然万物的荒野守护者。",
        "attr_bonus": {"str": 1, "agi": 1, "spr": 2},
        "job_skill": {"name": "自然变身", "desc": "短暂化身为野兽形态作战。"},
    },
    "吟游诗人": {
        "name": "吟游诗人", "type": "job",
        "description": "以诗歌与传说编织奇迹的流浪艺人。",
        "attr_bonus": {"str": 0, "agi": 1, "spr": 2},
        "job_skill": {"name": "鼓舞之歌", "desc": "以歌声鼓舞同伴，提升全队士气。"},
    },
    "武僧": {
        "name": "武僧", "type": "job",
        "description": "锤炼身心、以气御体的修行武者。",
        "attr_bonus": {"str": 1, "agi": 3, "spr": 1},
        "job_skill": {"name": "气劲掌", "desc": "将内劲灌注掌心击出，可震慑内腑。"},
    },
    "狂战士": {
        "name": "狂战士", "type": "job",
        "description": "以伤痛点燃怒火的野蛮斗士。",
        "attr_bonus": {"str": 3, "agi": 1, "spr": 0},
        "job_skill": {"name": "血怒", "desc": "进入狂暴状态，力量暴涨但防御下降。"},
    },
    "猎魔人": {
        "name": "猎魔人", "type": "job",
        "description": "接受变异改造、专职讨伐魔物的猎人。",
        "attr_bonus": {"str": 1, "agi": 2, "spr": 1},
        "job_skill": {"name": "猎印", "desc": "在武器上刻下猎魔印记，对魔物特攻。"},
    },
    "学者": {
        "name": "学者", "type": "job",
        "description": "沉迷典籍与研究的知识追寻者。",
        "attr_bonus": {"str": 0, "agi": 0, "spr": 3},
        "job_skill": {"name": "博闻强识", "desc": "回忆典籍知识，识破敌人弱点或机关原理。"},
    },
}
