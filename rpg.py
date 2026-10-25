"""RPG 属性、战力锚点与角色卡渲染工具。"""

from __future__ import annotations

import random as _random
import time

from astrbot.api.event import AstrMessageEvent

# ── RPG 属性系统 ──


MAX_ATTR = 1000
BASE_ATTR = 5
GOD_POWER = 9999  # 神明级战力，不可战胜
GRADE_TIER = ["F", "E", "D", "C", "B", "A", "A+", "S"]
_GRADE_COEFF = {"F":0.5, "E":0.7, "D":0.85, "C":1.0, "B":1.3, "A":1.7, "A+":1.85, "S":2.2}

# ── 战力档位系统 ──

POWER_TIERS = {
    "凡人": {"name": "凡人档", "range": (10, 50), "desc": "现实/低武尺度，装备、环境与人数往往比个人体质更重要"},
    "超凡": {"name": "超凡档", "range": (50, 500), "desc": "魔幻/异能/修行尺度，强者能稳定压制普通人"},
    "神话": {"name": "神话档", "range": (500, 9999), "desc": "神明/星际/宇宙尺度，层级差距会形成近乎不可逾越的压制"},
}

POWER_BANDS = [
    (0, 20, "普通人", "普通人或新手，适合面对日常危险与低威胁敌人"),
    (21, 50, "训练者", "受过训练或小有超常，能处理普通杂兵但不宜硬抗精英"),
    (51, 150, "精英", "明显强于常人，可压制普通敌人，但仍需警惕环境与数量"),
    (151, 500, "超凡", "具备超自然或高阶战斗力，可轻易胜过低阶敌人"),
    (501, 999, "灾厄级", "足以改变局部局势，低阶角色正面对抗几乎必败"),
    (1000, GOD_POWER - 1, "神话级", "远超常规尺度，非同阶或特殊条件不应被击败"),
    (GOD_POWER, GOD_POWER, "神明级", "不可战胜锚点，除剧情条件外默认不可被击败"),
]

POWER_GAP_RULES = [
    "同阶或相邻阶层：允许正面对抗，胜负取决于行动、环境、装备、代价与叙事合理性。",
    "低 2 个阶层：正面对抗大多失败，除非有明确克制、准备、地形、盟友或剧情弱点。",
    "低 3 个及以上阶层：不可写成普通战斗胜利，应引导撤退、潜入、谈判、封印、削弱或阶段性目标。",
    "高 2 个阶层以上：可以快速压制低阶敌人，避免把路边杂兵写成难解难分。",
]


def _power_band(power: int) -> dict:
    for idx, (low, high, name, desc) in enumerate(POWER_BANDS):
        if low <= power <= high:
            return {"index": idx, "name": name, "range": f"{low}~{high}", "desc": desc}
    return {"index": len(POWER_BANDS) - 1, "name": "神明级", "range": f"{GOD_POWER}+", "desc": "不可战胜锚点，除剧情条件外默认不可被击败"}


def _trait_band(value: int) -> str:
    if value >= 151:
        return "神话"
    if value >= 51:
        return "超凡"
    if value >= 21:
        return "优秀"
    if value >= 10:
        return "普通"
    return "薄弱"


def _skill_names(skills: list[dict], limit: int = 3) -> str:
    names = []
    for sk in skills[:limit]:
        name = str(sk.get("name", "")).strip()
        if name:
            names.append(name)
    return "、".join(names)


def _build_player_stats(char: dict) -> str:
    """构建轻规则角色摘要：保留强弱锚点，避免把完整数值表塞进叙事提示。"""
    if not char:
        return ""
    _ensure_rpg(char)
    _recalc_rpg(char)
    a = char.get("attr", {})
    power = int(char.get("power", 0))
    band = _power_band(power)
    lines = [
        "【角色强弱锚点】",
        f"- 名字：{char.get('name', '???')}",
        f"- 综合阶层：{band['name']}（内部战力 {power}，{band['desc']}）",
        f"- 倾向：体魄{_trait_band(a.get('str', BASE_ATTR))}、反应{_trait_band(a.get('agi', BASE_ATTR))}、意志{_trait_band(a.get('spr', BASE_ATTR))}",
        f"- 命运特质：魅力{char.get('charm', 'C')}、幸运{char.get('luck', 'C')}，只作为隐藏倾向，不替代行动逻辑。",
    ]
    race = char.get("race")
    if race:
        lines.append(f"- 种族：{race.get('name', '?')}")
    job = char.get("job")
    if job:
        lines.append(f"- 职业：{job.get('name', '?')}")
    skills = char.get("skills", {})
    active_names = _skill_names(skills.get("active", []))
    passive_names = _skill_names(skills.get("passive", []))
    if active_names:
        lines.append(f"- 主动能力：{active_names}")
    if passive_names:
        lines.append(f"- 被动特质：{passive_names}")
    lines.append("请把数值只当作强弱锚点：用于判断能否对抗、是否碾压、是否必须撤退；不要逐项展示或机械扣血。")
    return "\n".join(lines)


def _build_world_tier_context(world_data: dict, world_name: str) -> str:
    """构建世界尺度与敌我强弱约束，避免不同世界只是换皮。"""
    if not world_data:
        return ""
    lines = [f"【世界强度锚点：{world_name}】"]
    overview = world_data.get("overview") if isinstance(world_data.get("overview"), dict) else {}
    tier = str(overview.get("power_tier") or world_data.get("power_tier") or "").strip()
    if tier and tier in POWER_TIERS:
        info = POWER_TIERS[tier]
        lines.append(f"- 世界尺度：{info['name']}（{info['desc']}，常见基准 {info['range'][0]}~{info['range'][1]}）")
    elif tier:
        lines.append(f"- 世界尺度：{tier}（自定义档位，请按叙事合理把握强度差距）")
    ref = str(overview.get("power_reference") or world_data.get("power_reference") or "").strip()
    if ref:
        lines.append(f"- 实力等级参考：\n{ref}")
    npc_list = world_data.get("npc", [])
    key_npcs = [n for n in npc_list if n.get("is_key") and (n.get("power") or n.get("power_tag"))]
    if key_npcs:
        lines.append("- 关键NPC/敌人强度锚点：")
        for n in key_npcs:
            if n.get("power"):
                p = int(n.get("power", 0))
                band = _power_band(p)
                p_text = f"{band['name']}（内部战力 {p}）"
            else:
                p_text = n.get("power_tag", "未知")
            lines.append(f"  · {n['name']}（{n.get('title', '')}）：{p_text} — {n.get('alignment', '未知')}")
    lines.append("- 判定原则：" + "；".join(POWER_GAP_RULES))
    return "\n".join(lines)

MAX_ACTIVE_SKILLS = 3
MAX_PASSIVE_SKILLS = 3
MAX_SELF_ATTR_SUM = 30
MIN_SELF_ATTR = 3
MAX_RANDOM_ATTR = 20
BASE_HP_MP = 100


def _random_grade(max_grade: str = "A+"):
    if max_grade not in GRADE_TIER:
        max_grade = "A+"
    idx = GRADE_TIER.index(max_grade)
    return _random.choice(GRADE_TIER[:idx + 1])


def _grade_coeff(grade: str) -> float:
    return _GRADE_COEFF.get(grade, 1.0)


def _calc_hp(str_val: int) -> int:
    return int(BASE_HP_MP * (1 + 0.1 * max(0, min(MAX_ATTR, str_val))))


def _calc_mp(spr_val: int) -> int:
    return int(BASE_HP_MP * (1 + 0.1 * max(0, min(MAX_ATTR, spr_val))))


def _calc_power(str_v: int, agi_v: int, spr_v: int, charm: str, luck: str) -> int:
    base = str_v * 0.4 + agi_v * 0.3 + spr_v * 0.3
    avg = (_grade_coeff(charm) + _grade_coeff(luck)) / 2
    return round(base * avg)


def _default_rpg() -> dict:
    charm = _random_grade("A+")
    luck = _random_grade("A+")
    return {
        "attr": {"str": BASE_ATTR, "agi": BASE_ATTR, "spr": BASE_ATTR},
        "charm": charm,
        "luck": luck,
        "hp": _calc_hp(BASE_ATTR),
        "mp": _calc_mp(BASE_ATTR),
        "power": _calc_power(BASE_ATTR, BASE_ATTR, BASE_ATTR, charm, luck),
        "has_adventured": False,
        "is_random": False,
        "race": None,
        "job": None,
        "skills": {"active": [], "passive": []},
    }


def _ensure_rpg(char: dict) -> dict:
    if "charm" not in char:
        rpg = _default_rpg()
        for k, v in rpg.items():
            char[k] = v
    return char


def _recalc_rpg(char: dict) -> dict:
    a = char.get("attr", {})
    s = a.get("str", BASE_ATTR)
    ag = a.get("agi", BASE_ATTR)
    sp = a.get("spr", BASE_ATTR)
    ch = char.get("charm", "C")
    lu = char.get("luck", "C")
    race = char.get("race") or {}
    rb = race.get("attr_bonus", {})
    s += rb.get("str", 0)
    ag += rb.get("agi", 0)
    sp += rb.get("spr", 0)
    job = char.get("job") or {}
    jb = job.get("attr_bonus", {})
    s += jb.get("str", 0)
    ag += jb.get("agi", 0)
    sp += jb.get("spr", 0)
    s = max(1, min(MAX_ATTR, s))
    ag = max(1, min(MAX_ATTR, ag))
    sp = max(1, min(MAX_ATTR, sp))
    char["hp"] = _calc_hp(s)
    char["mp"] = _calc_mp(sp)
    char["power"] = _calc_power(s, ag, sp, ch, lu)
    return char


def _render_rpg_sheet(char: dict) -> str:
    _ensure_rpg(char)
    _recalc_rpg(char)
    a = char["attr"]
    lines = [
        f"🎭 **{char.get('name', '???')}**{' 🔒随机角色' if char.get('is_random') else ''}",
        f"📝 {char.get('description', '')[:120]}",
        "",
        f"⚔️ 实力阶层：**{_power_band(int(char.get('power', 0)))['name']}**（锚点 {char.get('power', 0)}）",
        f"❤️ 状态参考：HP {char.get('hp', 0)}  /  MP {char.get('mp', 0)}",
        f"💪 体魄：{a.get('str', BASE_ATTR)}  🏃 反应：{a.get('agi', BASE_ATTR)}  🧠 意志：{a.get('spr', BASE_ATTR)}",
        f"✨ 魅力：{char.get('charm', '?')}  🍀 幸运：{char.get('luck', '?')}",
    ]
    race = char.get("race")
    if race:
        lines.append(f"👤 种族：{race.get('name', '?')}")
    job = char.get("job")
    if job:
        lines.append(f"🏆 职业：{job.get('name', '?')}")
    skills = char.get("skills", {})
    active = skills.get("active", [])
    if active:
        lines.append("⚡ **主动技能：**")
        for sk in active:
            mastery = sk.get("mastery") or ("熟练" if sk.get("lv", 1) >= 2 else "生疏")
            lines.append(f"  · {sk.get('name')} [{mastery}]：{sk.get('desc', '')}")
    passive = skills.get("passive", [])
    if passive:
        lines.append("🌀 **被动能力：**")
        for pk in passive:
            mastery = pk.get("mastery") or "常驻"
            lines.append(f"  · {pk.get('name')} [{mastery}]：{pk.get('desc', '')}")
    if char.get("world"):
        lines.append(f"🌍 {char['world']}")
    if char.get("social_status"):
        lines.append(f"🏛️ {char['social_status']}")
    return "\n".join(lines)


def _is_admin(event: AstrMessageEvent) -> bool:
    return event.role == "admin"


def _validate_attrs(str_v, agi_v, spr_v, is_admin: bool, is_random: bool) -> tuple:
    if is_random:
        return True, ""
    for name, val in [("力量", str_v), ("敏捷", agi_v), ("精神", spr_v)]:
        if val < MIN_SELF_ATTR:
            return False, f"{name}不能低于{MIN_SELF_ATTR}"
    if is_admin:
        return True, ""
    total = str_v + agi_v + spr_v
    if total > MAX_SELF_ATTR_SUM:
        return False, f"三维之和{total}超过上限{MAX_SELF_ATTR_SUM}"
    return True, ""


def _parse_attr_args(args: str) -> tuple | None:
    parts = args.strip().split()
    if len(parts) < 3:
        return None
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def _forget_skill(char: dict, skill_name: str, skill_type: str) -> tuple:
    skills = char.setdefault("skills", {"active": [], "passive": []})
    target = skills.get(skill_type, [])
    for i, sk in enumerate(target):
        if sk.get("name") == skill_name:
            char["updated_at"] = time.time()
            target.pop(i)
            return True, f"已遗忘「{skill_name}」"
    return False, f"未找到能力「{skill_name}」"


# ── 回合判定骰（插件侧真实随机，避免 LLM 自造随机数） ──


def _roll_d20(n: int = 3) -> list[int]:
    """真实随机掷 n 个 d20（插件侧掷骰，避免 LLM 自造随机数）。"""
    try:
        count = max(0, int(n))
    except (TypeError, ValueError):
        count = 3
    return [_random.randint(1, 20) for _ in range(count)]


def build_judgment_context(char: dict) -> str:
    """单人冒险：为本轮生成 GM 内部判定骰注入块。"""
    if not isinstance(char, dict):
        char = {}
    rolls = _roll_d20(3)
    attr = char.get("attr")
    a = attr if isinstance(attr, dict) else {}
    lines = [
        "【本轮判定骰（仅供 GM 内部使用，不要原样复读本块）】",
        f"- 真实随机 d20：{'、'.join(str(r) for r in rolls)}（按顺序取用，每个骰值只用一次，仅对当前回合有效）",
        f"- 判定基准：{char.get('name', '???')} 的倾向：体魄{_trait_band(a.get('str', BASE_ATTR))}、反应{_trait_band(a.get('agi', BASE_ATTR))}、意志{_trait_band(a.get('spr', BASE_ATTR))}",
        "- 参考映射：1~5 不利（失败/大失败）、6~10 惜败或付出代价、11~15 险胜、16~18 成功、19~20 大成功；再叠加倾向锚点与世界强度差修正。",
    ]
    return "\n".join(lines)


def build_group_judgment_context(participants: list[dict]) -> str:
    """群体冒险：为本轮生成判定骰注入块（在场玩家倾向 + 共享骰值）。

    participants 为角色 dict 列表。
    """
    if not isinstance(participants, list):
        participants = []
    rolls = _roll_d20(3)
    lines = [
        "【本轮判定骰（仅供 GM 内部使用，不要原样复读本块）】",
        f"- 真实随机 d20：{'、'.join(str(r) for r in rolls)}（按顺序取用，每个骰值只用一次，仅对当前回合有效）",
    ]
    for char in participants:
        if not isinstance(char, dict):
            continue
        a = char.get("attr", {})
        if not isinstance(a, dict):
            a = {}
        lines.append(
            f"- {char.get('name', '???')}：体魄{_trait_band(a.get('str', BASE_ATTR))}、"
            f"反应{_trait_band(a.get('agi', BASE_ATTR))}、意志{_trait_band(a.get('spr', BASE_ATTR))}"
        )
    lines.append("- 参考映射：1~5 不利（失败/大失败）、6~10 惜败或付出代价、11~15 险胜、16~18 成功、19~20 大成功；再叠加倾向锚点与世界强度差修正。")
    return "\n".join(lines)
