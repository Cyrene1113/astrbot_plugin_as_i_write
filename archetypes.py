"""种族/职业图鉴库 v2：内置模板 + 自定义条目的加载、检索与角色字段转换。

v2 相对 v1 的增强（保持向后兼容）：
- ``innate_passives`` / ``innate_negatives`` 支持结构化条目 ``{"name","desc"}``，
  兼容旧版纯字符串（归一化时自动包装为 ``{"name": 文本, "desc": ""}``）；
- 职业支持多条专属技能 ``job_skills``（``job_skill`` 保留为首条技能的兼容镜像）；
- 新增叙事字段：种族 ``race_lore``（外观与文化）、职业 ``play_style``（玩法建议）、
  共享 ``power_hint``（推荐实力阶层提示）；
- 内置模板带 ``template_rev`` 版本戳：文件中的旧版内置条目在加载时自动升级为新版，
  自定义条目与已升级/被管理员改过的条目（rev 已达标）永不回写覆盖。
"""

from __future__ import annotations

import copy
import time

from .core import _load_json, _save_json, logger  # noqa: F401  logger 供迁移日志使用

# type 只允许这两个值
ARCHETYPE_TYPES = ("race", "job")

ARCHETYPE_SCHEMA_VERSION = 2
BUILTIN_TEMPLATE_REV = 2  # 内置模板内容版本：旧 rev 的内置条目加载时自动升级

_ATTR_KEYS = ("str", "agi", "spr")


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_string_list(value) -> list[str]:
    """清洗为字符串列表：strip 后丢弃空项；单个字符串兼容为单元素列表。"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if item is not None and str(item).strip()]


def _as_trait_list(value) -> list[dict[str, str]]:
    """清洗为结构化特质列表 [{name, desc}]。

    接受三种形态：字符串（"夜视"）、``{"name","desc"}``、以及旧版可能的
    {"name": "..."} 缺 desc。非法项丢弃；desc 缺失补空串。
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    traits: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("text") or "").strip()
            desc = str(item.get("desc") or item.get("description") or "").strip()
        else:
            name = str(item or "").strip()
            desc = ""
        if not name or name in seen:
            continue
        seen.add(name)
        traits.append({"name": name, "desc": desc})
    return traits


def _as_skill_list(value) -> list[dict[str, str]]:
    """清洗为技能列表 [{name, desc}]；无名技能丢弃。"""
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    skills: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        skills.append({"name": name, "desc": str(item.get("desc") or "")})
    return skills


def trait_names(traits) -> list[str]:
    """从字符串或结构化特质列表中取名字列表（渲染兼容层）。"""
    if not isinstance(traits, list):
        return []
    names: list[str] = []
    for item in traits:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = str(item or "").strip()
        if name:
            names.append(name)
    return names


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
    result["innate_passives"] = _as_trait_list(result.get("innate_passives"))
    result["innate_negatives"] = _as_trait_list(result.get("innate_negatives"))
    result["race_lore"] = str(result.get("race_lore") or "")
    result["job_skills"] = _as_skill_list(result.get("job_skills"))
    if not result["job_skills"]:
        # 旧版数据只有裸 job_skill：升级为列表，保证自定义条目不丢技能
        legacy_skill = _as_skill_list(result.get("job_skill"))
        result["job_skills"] = legacy_skill
    result["play_style"] = str(result.get("play_style") or "")
    result["power_hint"] = str(result.get("power_hint") or "")
    result["tags"] = _as_string_list(result.get("tags"))
    result["builtin"] = bool(result.get("builtin", False))
    # template_rev：缺失视为 0（旧数据），加载层据此触发内置模板升级
    try:
        result["template_rev"] = int(result.get("template_rev", 0) or 0)
    except (TypeError, ValueError):
        result["template_rev"] = 0
    result["created_at"] = _as_float(result.get("created_at"))
    result["updated_at"] = _as_float(result.get("updated_at"))
    result["schema_version"] = ARCHETYPE_SCHEMA_VERSION
    # 兼容镜像：job_skill 始终等于首条专属技能（无则 None）
    result["job_skill"] = copy.deepcopy(result["job_skills"][0]) if result["job_skills"] else None
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
    """把图鉴条目转换为角色卡 race/job 字段结构（兼容 _recalc_rpg 消费格式）。

    种族字段携带结构化天赋（含描述）；职业字段携带 job_skills 列表并保留
    job_skill 首条镜像以兼容旧渲染。
    """
    entry = normalize_archetype(arche if isinstance(arche, dict) else {})
    bonus = dict(entry["attr_bonus"])
    if entry["type"] == "job":
        return {
            "name": entry["name"],
            "description": entry["description"],
            "job_skills": copy.deepcopy(entry["job_skills"]),
            "job_skill": copy.deepcopy(entry["job_skills"][0]) if entry["job_skills"] else None,
            "play_style": entry["play_style"],
            "attr_bonus": bonus,
        }
    return {
        "name": entry["name"],
        "description": entry["description"],
        "innate_passives": copy.deepcopy(entry["innate_passives"]),
        "innate_negatives": copy.deepcopy(entry["innate_negatives"]),
        "race_lore": entry["race_lore"],
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
    passive_names = trait_names(entry["innate_passives"])
    if passive_names:
        segments.append(f"被动：{'、'.join(passive_names)}")
    negative_names = trait_names(entry["innate_negatives"])
    if negative_names:
        segments.append(f"负面：{'、'.join(negative_names)}")
    if entry["type"] == "job":
        skill_names = [s["name"] for s in entry["job_skills"]]
        if skill_names:
            segments.append(f"专属：{'、'.join(skill_names)}")
    return f"{entry['name'] or '未命名'}（{'｜'.join(segments)}）"


def load_archetypes(path) -> dict:
    """加载图鉴文件，逐条 normalize，键为条目名；内置旧版模板自动升级。"""
    raw = _load_json(path)
    if not isinstance(raw, dict):
        return {}
    migrated, upgraded = _migrate_builtin_templates(raw)
    if upgraded:
        try:
            _save_json(path, migrated)
            logger.info(f"📖 如我所书: 内置图鉴模板已升级到 v{BUILTIN_TEMPLATE_REV}: {'、'.join(upgraded)}")
        except Exception as exc:  # 迁移写盘失败不阻断加载
            logger.warning(f"📖 如我所书: 内置图鉴模板升级写盘失败: {exc}")
    loaded: dict[str, dict] = {}
    for key, value in migrated.items():
        if str(key) == "schema_version":
            continue
        entry = normalize_archetype(value, name=str(key))
        loaded[entry["name"] or str(key)] = entry
    return loaded


def _migrate_builtin_templates(raw: dict) -> tuple[dict, list[str]]:
    """把文件中 rev 低于当前版本的内置条目替换为新模板。

    只动 ``builtin`` 为 True 且名字命中内置模板的条目；自定义条目与
    rev 已达标的条目原样保留。返回 (迁移后的数据, 升级名单)。
    """
    if not isinstance(raw, dict):
        return {}, []
    changed = False
    upgraded: list[str] = []
    now = time.time()
    for key, value in raw.items():
        if str(key) == "schema_version" or not isinstance(value, dict):
            continue
        if not value.get("builtin"):
            continue
        template = DEFAULT_ARCHETYPES.get(str(value.get("name") or key))
        if template is None:
            continue
        try:
            rev = int(value.get("template_rev", 0) or 0)
        except (TypeError, ValueError):
            rev = 0
        if rev >= BUILTIN_TEMPLATE_REV:
            continue
        entry = normalize_archetype(template, name=str(key), type_=str(template.get("type") or "race"))
        entry["builtin"] = True
        entry["template_rev"] = BUILTIN_TEMPLATE_REV
        entry["created_at"] = _as_float(value.get("created_at")) or now
        entry["updated_at"] = now
        raw[key] = entry
        upgraded.append(entry["name"] or str(key))
        changed = True
    if changed:
        raw["schema_version"] = ARCHETYPE_SCHEMA_VERSION
    return raw, upgraded


def seed_builtin_archetypes(path) -> dict:
    """播种/升级内置图鉴：文件为空时全量写入；非空时仅升级旧版内置模板。

    文件即真相：自定义条目永不触碰，已升级条目不重复回写。
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
        entry["template_rev"] = BUILTIN_TEMPLATE_REV
        entry["created_at"] = now
        entry["updated_at"] = now
        seeded[entry["name"] or str(key)] = entry
    _save_json(path, seeded)
    return seeded


# ════════════════════════════════════════════════
#  内置模板 v2：每个种族/职业都有明确分工与叙事抓手
# ════════════════════════════════════════════════

DEFAULT_ARCHETYPES: dict[str, dict] = {
    # ── 种族（10）──
    "人类": {
        "name": "人类", "type": "race",
        "description": "适应力极强的平凡种族，在任何世界都能扎根成长。",
        "attr_bonus": {"str": 0, "agi": 0, "spr": 0},
        "innate_passives": [
            {"name": "适应与学习", "desc": "面对陌生环境、新知识与突发状况时，更容易找到可行的办法。"},
            {"name": "人心相通", "desc": "与各族打交道时更容易赢得初步信任。"},
        ],
        "innate_negatives": [],
        "race_lore": "寿命短暂却繁衍广泛，以野心与韧性弥补天赋平凡；各大势力中都有人类的身影。",
        "power_hint": "推荐阶层：普通人~精英",
        "tags": ["均衡", "万金油"],
    },
    "精灵": {
        "name": "精灵", "type": "race",
        "description": "长寿优雅的森林民族，感官敏锐而身形单薄。",
        "attr_bonus": {"str": 0, "agi": 2, "spr": 2},
        "innate_passives": [
            {"name": "夜视", "desc": "黑暗中视物如同暮色，夜间行动几乎不受影响。"},
            {"name": "自然亲和", "desc": "在荒野与林地中辨识方向、寻找食物与庇护所格外顺利。"},
        ],
        "innate_negatives": [
            {"name": "体质孱弱", "desc": "负重伤后恢复缓慢，硬抗重击是下下策。"},
        ],
        "race_lore": "百年如一日的长寿者，举止从容而疏离，视森林为母族圣地。",
        "power_hint": "推荐阶层：训练者~精英",
        "tags": ["敏捷", "奥术"],
    },
    "矮人": {
        "name": "矮人", "type": "race",
        "description": "山脉深处的工匠种族，体魄坚韧且百毒难侵。",
        "attr_bonus": {"str": 2, "agi": 0, "spr": 1},
        "innate_passives": [
            {"name": "抗毒", "desc": "毒素、烈酒与污浊空气难以撼动矮人的身板。"},
            {"name": "黑暗视觉", "desc": "矿道与地城深处照样视物。"},
            {"name": "石之识", "desc": "鉴别矿物、判断地质异动与机关构造有独到眼光。"},
        ],
        "innate_negatives": [],
        "race_lore": "以氏族与工坊为中心生活，视锤凿为第二生命，记仇也记恩。",
        "power_hint": "推荐阶层：训练者~精英",
        "tags": ["坚韧", "工匠"],
    },
    "兽人": {
        "name": "兽人", "type": "race",
        "description": "崇尚力量的荒野战士，伤势越重斗志越旺。",
        "attr_bonus": {"str": 3, "agi": 1, "spr": 0},
        "innate_passives": [
            {"name": "狂血战意", "desc": "伤势越重力量越盛，濒死反扑尤为骇人。"},
        ],
        "innate_negatives": [
            {"name": "易怒冲动", "desc": "涉及羞辱与挑衅时，克制与耐心判定不利。"},
        ],
        "race_lore": "以战功论英雄的部落文明，誓言与血债都刻在骨头上。",
        "power_hint": "推荐阶层：训练者~精英",
        "tags": ["力量", "近战"],
    },
    "半身人": {
        "name": "半身人", "type": "race",
        "description": "爱好安逸的矮小种族，总能逢凶化吉。",
        "attr_bonus": {"str": 0, "agi": 3, "spr": 0},
        "innate_passives": [
            {"name": "天生幸运", "desc": "不确定的处境里常有转机，隐藏判定容易占到便宜。"},
            {"name": "藏匿行家", "desc": "娇小身形加上轻手轻脚，藏身与潜行几乎无师自通。"},
        ],
        "innate_negatives": [],
        "race_lore": "眷恋厨房与炉火的乡居民族，但对宝箱与秘密的好奇心从不输给任何人。",
        "power_hint": "推荐阶层：普通人~训练者",
        "tags": ["敏捷", "幸运"],
    },
    "龙裔": {
        "name": "龙裔", "type": "race",
        "description": "流淌巨龙血脉的裔民，天生具备龙威与坚鳞。",
        "attr_bonus": {"str": 2, "agi": 0, "spr": 2},
        "innate_passives": [
            {"name": "龙息吐息", "desc": "可喷出与血脉对应的元素吐息，短距范围内颇具威力。"},
            {"name": "鳞甲护体", "desc": "细密鳞片如同轻甲，削减利器的伤害。"},
        ],
        "innate_negatives": [
            {"name": "血脉醒目", "desc": "龙裔特征难以掩饰，潜行易被识破。"},
        ],
        "race_lore": "龙血后裔散居各地，血脉浓度因家族而异；龙威使弱者本能退避。",
        "power_hint": "推荐阶层：精英~超凡",
        "tags": ["力量", "奥术"],
    },
    "亡灵": {
        "name": "亡灵", "type": "race",
        "description": "挣脱死亡规则的存在，不知疲倦却畏惧阳光。",
        "attr_bonus": {"str": 1, "agi": -1, "spr": 3},
        "innate_passives": [
            {"name": "不倦不食", "desc": "无需睡眠与饮食，毒素、疲惫与恐惧对躯壳近乎无效。"},
        ],
        "innate_negatives": [
            {"name": "日光厌恶性状", "desc": "白日行动迟缓，阳光下感官迟钝。"},
            {"name": "生机排斥", "desc": "治愈类力量效果减半，甚至引发灼痛。"},
        ],
        "race_lore": "以执念维系存在的躯壳，常被活人惧怕，也常背负未竟之愿。",
        "power_hint": "推荐阶层：精英~超凡",
        "tags": ["奥术", "特殊"],
    },
    "侏儒": {
        "name": "侏儒", "type": "race",
        "description": "痴迷发明的小巧种族，满脑子奇思妙想。",
        "attr_bonus": {"str": 0, "agi": 1, "spr": 2},
        "innate_passives": [
            {"name": "机巧发明", "desc": "摆弄机械、装置与炼金造物时有奇效，也能快速识破其原理。"},
            {"name": "敏锐好奇", "desc": "容易注意到旁人忽略的隐蔽细节。"},
        ],
        "innate_negatives": [],
        "race_lore": "把『为什么』当作问候语的族群，工坊事故率与其创造力同样出名。",
        "power_hint": "推荐阶层：普通人~精英",
        "tags": ["奥术", "机巧"],
    },
    "半精灵": {
        "name": "半精灵", "type": "race",
        "description": "人类与精灵的混血，兼得两族之长却两边都不完全归属。",
        "attr_bonus": {"str": 0, "agi": 1, "spr": 1},
        "innate_passives": [
            {"name": "魅惑抗性", "desc": "对魅惑、迷幻与精神操控有天生抵抗力。"},
            {"name": "两族之交", "desc": "在人类与精灵社会都能找到门路与旧识。"},
        ],
        "innate_negatives": [],
        "race_lore": "生在两个世界的夹缝中，学会用漂泊者的视角打量一切。",
        "power_hint": "推荐阶层：普通人~精英",
        "tags": ["均衡", "社交"],
    },
    "兽化人": {
        "name": "兽化人", "type": "race",
        "description": "保留兽形的野性种族，直觉敏锐却畏银。",
        "attr_bonus": {"str": 2, "agi": 2, "spr": 0},
        "innate_passives": [
            {"name": "野性直觉", "desc": "凭气味与直觉嗅出危险、谎言与情绪波动。"},
            {"name": "兽爪撕裂", "desc": "徒手搏杀时爪牙即是凶器。"},
        ],
        "innate_negatives": [
            {"name": "银器过敏", "desc": "接触银制武器会加重伤势。"},
        ],
        "race_lore": "血脉中沉睡着兽祖的族群，满月之夜格外躁动，聚居在荒野边缘。",
        "power_hint": "推荐阶层：训练者~精英",
        "tags": ["力量", "敏捷"],
    },
    # ── 职业（12）──
    "战士": {
        "name": "战士", "type": "job",
        "description": "精通武器与格挡的一线搏杀者。",
        "attr_bonus": {"str": 3, "agi": 0, "spr": 0},
        "job_skills": [
            {"name": "重击", "desc": "蓄力挥出沉重一击，可破坏敌方格挡。"},
            {"name": "坚壁", "desc": "进入防御架势，替同伴承受攻击。"},
        ],
        "play_style": "正面顶住威胁，用血肉与钢铁为队伍撑出安全区。",
        "power_hint": "推荐阶层：训练者~超凡",
        "tags": ["近战", "防御"],
    },
    "法师": {
        "name": "法师", "type": "job",
        "description": "以学识驾驭奥术之力的施法者。",
        "attr_bonus": {"str": 0, "agi": 1, "spr": 3},
        "job_skills": [
            {"name": "奥术飞弹", "desc": "凝聚魔力射出数枚追踪奥术箭。"},
            {"name": "法术屏障", "desc": "张开护盾格挡一次伤害，过载会反噬自身。"},
        ],
        "play_style": "火力与控制的核心，但躯体脆弱——保持距离就是保持生命。",
        "power_hint": "推荐阶层：训练者~超凡",
        "tags": ["奥术", "远程"],
    },
    "游侠": {
        "name": "游侠", "type": "job",
        "description": "行走荒野的猎手与向导。",
        "attr_bonus": {"str": 1, "agi": 2, "spr": 1},
        "job_skills": [
            {"name": "精准射击", "desc": "屏息瞄准，对远处目标造成精准打击。"},
            {"name": "追踪术", "desc": "循足迹、断枝与气味找到目标或失踪的同伴。"},
        ],
        "play_style": "队伍的眼睛：先敌发现、先敌开火，荒野就是主场。",
        "power_hint": "推荐阶层：训练者~精英",
        "tags": ["远程", "荒野"],
    },
    "盗贼": {
        "name": "盗贼", "type": "job",
        "description": "游走于阴影与规则之间的边缘人。",
        "attr_bonus": {"str": 0, "agi": 3, "spr": 1},
        "job_skills": [
            {"name": "暗影步", "desc": "融入阴影，瞬间移动到目标身后。"},
            {"name": "妙手", "desc": "悄无声息地窃取物品或解除小型机关。"},
        ],
        "play_style": "绕过正面战场：开锁、探路、偷袭与销赃样样在行。",
        "power_hint": "推荐阶层：普通人~精英",
        "tags": ["敏捷", "潜行"],
    },
    "牧师": {
        "name": "牧师", "type": "job",
        "description": "侍奉神明、以信仰济世的虔修者。",
        "attr_bonus": {"str": 1, "agi": 0, "spr": 2},
        "job_skills": [
            {"name": "治愈祈祷", "desc": "向神明祈愿，恢复伤者的伤势。"},
            {"name": "圣言护佑", "desc": "为同伴祝祷，短时间提升意志与抗性。"},
        ],
        "play_style": "队伍的底线：让倒下的人站起来，让恐慌的人定下心。",
        "power_hint": "推荐阶层：训练者~精英",
        "tags": ["神圣", "辅助"],
    },
    "圣骑士": {
        "name": "圣骑士", "type": "job",
        "description": "立下圣誓的圣武士，信念即铠甲。",
        "attr_bonus": {"str": 2, "agi": 0, "spr": 2},
        "job_skills": [
            {"name": "圣光审判", "desc": "以圣光裁决邪恶，对不洁之物额外有效。"},
            {"name": "誓约守护", "desc": "守护立誓对象时防御大幅提升，违背誓言则力量暂时黯淡。"},
        ],
        "play_style": "行走的信条：抗线、审判邪祟，并为誓言付出一切代价。",
        "power_hint": "推荐阶层：精英~超凡",
        "tags": ["神圣", "近战"],
    },
    "德鲁伊": {
        "name": "德鲁伊", "type": "job",
        "description": "沟通自然万物的荒野守护者。",
        "attr_bonus": {"str": 1, "agi": 1, "spr": 2},
        "job_skills": [
            {"name": "自然变身", "desc": "短暂化身为野兽形态作战。"},
            {"name": "荆棘缠绕", "desc": "唤起草木束缚敌人，拖延或禁锢目标。"},
        ],
        "play_style": "自然的代言人：变身缠斗、地形控场，平衡不可轻易打破。",
        "power_hint": "推荐阶层：训练者~超凡",
        "tags": ["自然", "变形"],
    },
    "吟游诗人": {
        "name": "吟游诗人", "type": "job",
        "description": "以诗歌与传说编织奇迹的流浪艺人。",
        "attr_bonus": {"str": 0, "agi": 1, "spr": 2},
        "job_skills": [
            {"name": "鼓舞之歌", "desc": "以歌声鼓舞同伴，提升全队士气。"},
            {"name": "逸闻知识", "desc": "从传说与八卦中打听到关键消息或弱点。"},
        ],
        "play_style": "消息灵通的万金油：嘴和琴就是武器，关键时刻妙语扭转乾坤。",
        "power_hint": "推荐阶层：普通人~精英",
        "tags": ["辅助", "社交"],
    },
    "武僧": {
        "name": "武僧", "type": "job",
        "description": "锤炼身心、以气御体的修行武者。",
        "attr_bonus": {"str": 1, "agi": 3, "spr": 1},
        "job_skills": [
            {"name": "气劲掌", "desc": "将内劲灌注掌心击出，可震慑内腑。"},
            {"name": "身轻如燕", "desc": "蜻蜓点水般越过障碍，或稳稳落在高处。"},
        ],
        "play_style": "以身为器：速度与连击见长，清规戒律亦是力量来源。",
        "power_hint": "推荐阶层：训练者~精英",
        "tags": ["近战", "敏捷"],
    },
    "狂战士": {
        "name": "狂战士", "type": "job",
        "description": "以伤痛点燃怒火的野蛮斗士。",
        "attr_bonus": {"str": 3, "agi": 1, "spr": 0},
        "job_skills": [
            {"name": "血怒", "desc": "进入狂暴状态，力量暴涨但防御下降。"},
            {"name": "痛觉钝化", "desc": "忽视疼痛继续行动，重伤不减战意。"},
        ],
        "play_style": "越险越强：伤疤是勋章，怒火是燃料——但收不住手时会误伤局势。",
        "power_hint": "推荐阶层：训练者~超凡",
        "tags": ["近战", "爆发"],
    },
    "猎魔人": {
        "name": "猎魔人", "type": "job",
        "description": "接受变异改造、专职讨伐魔物的猎人。",
        "attr_bonus": {"str": 1, "agi": 2, "spr": 1},
        "job_skills": [
            {"name": "猎印", "desc": "在武器上刻下猎魔印记，对魔物特攻。"},
            {"name": "魔物知识", "desc": "识破魔物的弱点、习性与共生关系。"},
        ],
        "play_style": "有偿的怪物专家：情报、炼金药剂与银剑，一样都不能少。",
        "power_hint": "推荐阶层：精英~超凡",
        "tags": ["狩猎", "特攻"],
    },
    "学者": {
        "name": "学者", "type": "job",
        "description": "沉迷典籍与研究的知识追寻者。",
        "attr_bonus": {"str": 0, "agi": 0, "spr": 3},
        "job_skills": [
            {"name": "博闻强识", "desc": "回忆典籍知识，识破敌人弱点或机关原理。"},
            {"name": "临阵推演", "desc": "从现场线索推演真相或敌人的下一步。"},
        ],
        "play_style": "以知识破局：解谜、鉴宝、破译古文，战斗则交给同伴。",
        "power_hint": "推荐阶层：普通人~精英",
        "tags": ["知识", "辅助"],
    },
}
