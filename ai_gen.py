"""WebUI AI 助手：世界观 / 种族职业图鉴 / 角色卡的一键生成、补完与润色。

职责边界：本模块只负责提示词拼装、宽容解析与草稿归一化；LLM 调用与
文件写入由 web_api.py 承担（生成结果一律以「草稿」返回，由管理员在
页面里确认后再保存，绝不直接落盘）。

注意：各提示词模板含字面花括号（JSON 契约示例），拼接输入时禁止
str.format()，只能字符串相加。
"""

from __future__ import annotations

import json
import random
import time
from typing import Any

from .lore import normalize_character, normalize_world
from .rpg import _default_rpg, _recalc_rpg

# ── 输出契约（与下方提示词中的 JSON 示例保持一致）──

WORLD_JSON_CONTRACT = """{"overview": {"title": "展示标题", "type": "题材类型", "description": "200字以内的世界观概述", "tags": ["3~6个标签"], "power_tier": "凡人|超凡|神话 之一", "power_reference": "每行一条的实力等级参考，如：士兵：战力 80~150"}, "npc": [{"name": "名字", "title": "头衔", "alignment": "友方|中立|敌方", "description": "80字以内的人物速写", "traits": ["2~4个性格特质"], "dialog_samples": ["1~2句代表性台词"], "power_tag": "略强于玩家|势均力敌|碾压级|弱于玩家 之一或留空", "is_key": false}], "plotlines": [{"title": "剧情线名", "type": "主线|支线|隐藏", "summary": "100字以内的剧情概要", "stages": [{"title": "阶段名", "description": "60字以内的阶段描述", "trigger": "进入下一阶段的触发条件"}]}], "clues": [{"name": "伏笔名", "type": "物证|证言|环境|传闻", "content": "60字以内的伏笔内容", "related_npc": ["关联NPC名"]}], "locations": [{"name": "地点名", "type": "城邦|地标|秘境|区域", "description": "60字以内的地点描述"}], "factions": [{"name": "势力名", "alignment": "守序|中立|混乱", "description": "60字以内的势力描述", "relations": "与其他势力的关系"}], "eggs": [{"text": "彩蛋内容", "trigger_condition": "触发条件"}], "entries": [{"name": "世界书条目名", "keys": ["触发关键词"], "content": "150字以内的世界规则/背景知识"}]}"""

ARCHETYPE_JSON_CONTRACT = """{"name": "名称", "type": "race|job", "description": "60字以内的概述", "attr_bonus": {"str": 0, "agi": 0, "spr": 0}, "innate_passives": [{"name": "天赋名", "desc": "30字以内效果描述"}], "innate_negatives": [{"name": "负面名", "desc": "30字以内"}], "race_lore": "种族的外观与文化背景（60字以内，职业给空串）", "job_skills": [{"name": "技能名", "desc": "40字以内效果"}], "play_style": "职业玩法建议（60字以内，种族给空串）", "power_hint": "推荐阶层，如：训练者~精英", "tags": ["2~4个标签"]}"""

CHARACTER_JSON_CONTRACT = """{"name": "角色名(2~6字)", "description": "80字以内的背景描述", "personality": "40字以内的性格", "scenario": "40字以内的开场情景", "race": "种族名(可空串)", "job": "职业名(可空串)", "social_status": "社会身份(可空串)", "attr": {"str": 3, "agi": 3, "spr": 3}, "skills": {"active": [{"name": "能力名", "desc": "一句话描述"}], "passive": [{"name": "特质名", "desc": "一句话描述"}]}}"""


WORLD_GEN_PROMPT = """你是资深文字冒险游戏的世界观设计师。请根据用户的灵感设定，设计一个完整的、可直接开跑的世界观。

要求：
1. 风格自洽：题材、强度尺度、人物与势力必须互相匹配，实力参考要与世界强度档位一致。
2. 人物有戏：每个 NPC 都要有明确的立场与记忆点，关键 NPC 标记 is_key 为 true。
3. 剧情可用：剧情线按阶段拆分，每阶段有清晰的触发条件；伏笔要与 NPC/剧情线互相勾连。
4. 数值克制：力量/敏捷/精神补正（attr_bonus）每项 -3~+3；NPC 强度服从 power_reference。

只输出一个 JSON 对象，无代码围栏、无解释、无多余文字。字段契约（字段名必须完全一致）：
""" + WORLD_JSON_CONTRACT + "\n"

WORLD_COMPLETE_PROMPT = """你是文字冒险游戏的世界观补全师。下面给出一个已有世界观的结构摘要，其中标记为【缺失】的板块需要你补全，已有板块不要重复生成。

要求：新内容必须与已有设定风格自洽、互相勾连（新 NPC 可以关联已有剧情线，新伏笔可以关联已有 NPC）。补正每项 -3~+3。

只输出一个 JSON 对象，无代码围栏、无解释。字段契约（只需包含需要补全的板块，字段名必须完全一致）：
""" + WORLD_JSON_CONTRACT + "\n"

ARCHETYPE_GEN_PROMPT = """你是文字冒险游戏的种族/职业设计师。请根据用户的灵感，设计一个特色鲜明、与同族/同行有明显区分度的图鉴条目。

要求：
1. 补正（attr_bonus）每项 -3~+3，且与定位匹配（如蛮力型偏 str，奥术型偏 spr）。
2. 种族：2~3 条天赋被动（innate_passives，结构化 name+desc），最好带 1 条负面（innate_negatives）形成取舍；race_lore 写外观与文化。
3. 职业：1~2 条专属技能（job_skills，结构化 name+desc），技能要有独到机制而不是「造成伤害」的同义反复；play_style 写玩法建议。
4. 命名与设定要有记忆点，避免与「人类/精灵/战士/法师」这类通用模板同质化。

只输出一个 JSON 对象，无代码围栏、无解释、无多余文字。字段契约（字段名必须完全一致）：
""" + ARCHETYPE_JSON_CONTRACT + "\n"

CHARACTER_GEN_PROMPT = """你是文字冒险游戏的角色设计师。请根据用户的灵感，生成一张可直接开跑的角色卡。

要求：
1. 三维属性（attr.str/agi/spr）各 3~12，总和不超过 30，且与种族/职业定位匹配。
2. 主动能力最多 2 条、被动最多 2 条（可为 0 条），要具体可用，不要空泛。
3. 种族/职业若为空则留空字符串，由管理员之后套用图鉴模板。
4. 背景描述要留有冒险的钩子（未解的执念、目标或秘密）。

只输出一个 JSON 对象，无代码围栏、无解释、无多余文字。字段契约（字段名必须完全一致）：
""" + CHARACTER_JSON_CONTRACT + "\n"

EXPAND_PROMPT = """你是文字冒险游戏的文案润色师。请在保持原意与设定不变的前提下，把下面这段设定文本润色得更有画面感、更具体（补充感官细节、动机或钩子）。
只输出润色后的文本本身，不要任何解释、引号或前后缀。长度控制在原文的 1~2 倍、最多 400 字。

【原文】
"""


def parse_llm_json(text: str) -> dict | None:
    """宽容解析 LLM 输出的 JSON 对象：剥代码围栏、截取首尾花括号；失败返回 None。"""
    try:
        if not isinstance(text, str):
            return None
        cleaned = text.strip()
        if cleaned.startswith("```"):
            first_newline = cleaned.find("\n")
            if first_newline != -1:
                cleaned = cleaned[first_newline + 1:]
            if cleaned.rstrip().endswith("```"):
                cleaned = cleaned.rstrip()[:-3]
            cleaned = cleaned.strip()
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end <= start:
            return None
        payload = json.loads(cleaned[start:end + 1])
        return payload if isinstance(payload, dict) else None
    except Exception:  # noqa: BLE001 - 解析绝不向上抛异常
        return None


def normalize_world_draft(data: Any, name: str = "") -> dict:
    """把 LLM 产出的世界草稿归一化为可保存的 v3 结构。"""
    if not isinstance(data, dict):
        data = {}
    draft = normalize_world(data, name=name or str(data.get("overview", {}).get("title") or "未命名世界"))
    return draft


def normalize_archetype_draft(data: Any, name: str = "", type_: str = "race") -> dict:
    """把 LLM 产出的图鉴草稿归一化（含补正钳制到 -3~+3）。"""
    from .archetypes import normalize_archetype

    if not isinstance(data, dict):
        data = {}
    bonus = data.get("attr_bonus")
    if isinstance(bonus, dict):
        clamped: dict[str, int] = {}
        for key in ("str", "agi", "spr"):
            try:
                clamped[key] = max(-3, min(3, int(bonus.get(key, 0) or 0)))
            except (TypeError, ValueError):
                clamped[key] = 0
        data["attr_bonus"] = clamped
    return normalize_archetype(data, name=name, type_=type_)


def normalize_character_draft(data: Any) -> dict:
    """把 LLM 产出的角色草稿归一化并补齐 RPG 属性层（含三维钳制）。"""
    if not isinstance(data, dict):
        data = {}
    attr = data.get("attr")
    if isinstance(attr, dict):
        clamped: dict[str, int] = {}
        for key in ("str", "agi", "spr"):
            raw = attr.get(key, 5)
            try:
                value = int(raw)
            except (TypeError, ValueError):
                value = 5
            clamped[key] = max(1, min(20, value))
        data["attr"] = clamped
    draft = normalize_character(data)
    rpg = _default_rpg()
    for key, value in rpg.items():
        draft.setdefault(key, value)
    if isinstance(draft.get("attr"), dict):
        _recalc_rpg(draft)
    return draft


def world_summary_for_complete(world: dict, name: str) -> str:
    """把已有世界观压缩为补全提示词用的结构摘要（标注缺失板块）。"""
    w = world if isinstance(world, dict) else {}
    overview = w.get("overview") if isinstance(w.get("overview"), dict) else {}
    lines = [f"世界观名称：{name}"]
    desc = str(overview.get("description") or w.get("content") or "").strip()
    lines.append(f"概述：{desc[:400] or '【缺失】'}")
    tier = str(overview.get("power_tier") or "").strip()
    lines.append(f"强度档位：{tier or '【缺失】'}")
    ref = str(overview.get("power_reference") or "").strip()
    lines.append(f"实力参考：{ref[:300] or '【缺失】'}")
    npcs = [n for n in (w.get("npc") or []) if isinstance(n, dict) and str(n.get("name") or "").strip()]
    lines.append(
        "已有NPC：" + ("；".join(f"{n.get('name')}（{n.get('title') or ''}，{n.get('alignment') or ''}）" for n in npcs[:15]) if npcs else "【缺失】")
    )
    plots = [p for p in (w.get("plotlines") or []) if isinstance(p, dict) and str(p.get("title") or "").strip()]
    lines.append(
        "已有剧情线：" + ("；".join(f"{p.get('title')}（{p.get('type') or ''}）" for p in plots[:10]) if plots else "【缺失】")
    )
    clues = [c for c in (w.get("clues") or []) if isinstance(c, dict) and str(c.get("name") or "").strip()]
    lines.append(
        "已有伏笔：" + ("；".join(str(c.get("name")) for c in clues[:12]) if clues else "【缺失】")
    )
    locations = [l for l in (w.get("locations") or []) if isinstance(l, dict) and str(l.get("name") or "").strip()]
    lines.append(
        "已有地点：" + ("；".join(str(l.get("name")) for l in locations[:12]) if locations else "【缺失】")
    )
    factions = [f for f in (w.get("factions") or []) if isinstance(f, dict) and str(f.get("name") or "").strip()]
    lines.append(
        "已有势力：" + ("；".join(str(f.get("name")) for f in factions[:10]) if factions else "【缺失】")
    )
    eggs = w.get("eggs") or []
    lines.append(f"已有彩蛋数：{len(eggs)}" + ("【缺失】" if not eggs else ""))
    entries = w.get("entries") if isinstance(w.get("entries"), list) else []
    useful_entries = [e for e in entries if isinstance(e, dict) and str(e.get("name") or "").strip() and not str(e.get("name") or "").endswith("总览")]
    lines.append(
        "已有世界书条目：" + ("；".join(str(e.get("name")) for e in useful_entries[:15]) if useful_entries else "【缺失】")
    )
    return "\n".join(lines)


def missing_world_sections(world: dict) -> list[str]:
    """检测世界观中缺失/为空的板块，返回英文键名列表（与 JSON 契约字段对应）。"""
    w = world if isinstance(world, dict) else {}
    missing: list[str] = []
    if not (w.get("npc") or []):
        missing.append("npc")
    if not (w.get("plotlines") or []):
        missing.append("plotlines")
    if not (w.get("clues") or []):
        missing.append("clues")
    if not (w.get("locations") or []):
        missing.append("locations")
    if not (w.get("factions") or []):
        missing.append("factions")
    if not (w.get("eggs") or []):
        missing.append("eggs")
    entries = w.get("entries") if isinstance(w.get("entries"), list) else []
    if not any(isinstance(e, dict) and not str(e.get("name") or "").endswith("总览") for e in entries):
        missing.append("entries")
    overview = w.get("overview") if isinstance(w.get("overview"), dict) else {}
    if not str(overview.get("power_reference") or "").strip():
        missing.append("overview")  # 覆盖实力参考等 overview 字段
    return missing


def merge_world_draft(world: dict, draft: dict) -> tuple[dict, list[str]]:
    """把补全草稿合并进已有世界：只填空缺板块与 overview 空字段，绝不覆盖已有内容。

    合并过程不修改传入的 world（预览场景下 world 往往是插件内存中的活数据，
    管理员取消保存也不能污染它）；返回 (合并后的新对象, 本次补上的板块中文名)。
    """
    merged = dict(world if isinstance(world, dict) else {})
    added: list[str] = []
    section_labels = {
        "npc": "人物", "plotlines": "剧情线", "clues": "伏笔",
        "locations": "地点", "factions": "势力", "eggs": "彩蛋",
    }
    for key, label in section_labels.items():
        existing = merged.get(key)
        if not isinstance(existing, list) or not existing:
            incoming = draft.get(key)
            if isinstance(incoming, list) and incoming:
                merged[key] = incoming
                added.append(label)
    entries = list(merged.get("entries") or [])  # 拷贝列表，避免 append 污染活数据
    has_useful_entry = any(
        isinstance(e, dict) and str(e.get("name") or "").strip() and not str(e.get("name") or "").endswith("总览")
        for e in entries
    )
    if not has_useful_entry and isinstance(draft.get("entries"), list) and draft["entries"]:
        for item in draft["entries"]:
            if isinstance(item, dict):
                entries.append(item)
        merged["entries"] = entries
        added.append("世界书")
    overview = dict(merged.get("overview") or {})  # 拷贝 dict，避免键写入污染活数据
    draft_overview = draft.get("overview") if isinstance(draft.get("overview"), dict) else {}
    overview_changed = False
    for key in ("title", "type", "description", "power_tier", "power_reference"):
        if not str(overview.get(key) or "").strip() and str(draft_overview.get(key) or "").strip():
            overview[key] = draft_overview[key]
            overview_changed = True
    tags = overview.get("tags") if isinstance(overview.get("tags"), list) else []
    if not tags and isinstance(draft_overview.get("tags"), list) and draft_overview["tags"]:
        overview["tags"] = draft_overview["tags"]
        overview_changed = True
    if overview_changed:
        merged["overview"] = overview
        if "概要" not in added:
            added.append("概要")
    return merged, added


def make_session_id(task: str) -> str:
    return f"aiweb_{task}_{int(time.time())}"


def roll_fallback_attr() -> dict[str, int]:
    """角色草稿缺属性时的兜底掷点（与随机角色同数量级）。"""
    rng = random.SystemRandom()
    return {"str": rng.randint(3, 15), "agi": rng.randint(3, 15), "spr": rng.randint(3, 15)}
