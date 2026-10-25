"""冒险结算提取：属性成长、新能力、种族/职业变更、结局卡与世界书沉淀。

纯函数 + 提示词；LLM 调用与落盘由 main.py 负责，本模块只做解析、校验与应用。

注意：SETTLE_EXTRACT_PROMPT 内含字面花括号（JSON 示例），调用方不得对它使用
str.format()；拼接输入时请直接字符串相加。
"""

from __future__ import annotations

import json
import time
from typing import Any

from .lore import normalize_lore_entry
from .rpg import MAX_ACTIVE_SKILLS, MAX_PASSIVE_SKILLS, MAX_ATTR, _recalc_rpg

# ── 结算约束 ──

MAX_ATTR_DELTA_SUM = 3    # 单次结算三维成长合计上限
MAX_LORE_EXTRACTS = 2     # 单次结算最多沉淀的世界书条目数
MAX_LORE_KEYS = 6         # 世界书条目关键词上限
MAX_LORE_CONTENT = 300    # 世界书条目内容截断长度
MAX_WORLD_ENTRIES = 200   # 世界书条目总量上限，达到后不再追加

_ATTR_LABELS = {
    "str": "💪 力量",
    "agi": "🏃 敏捷",
    "spr": "🧠 精神",
}


SETTLE_EXTRACT_PROMPT = """你是文字冒险游戏的结算官。请根据输入提取本次冒险的结算结果，只输出一个 JSON 对象：无代码围栏（不要输出```）、无解释、无任何多余文字。

输入结构说明（由系统拼装）：
【角色面板】：角色名、三维属性（力量/敏捷/精神）、现有种族、职业与能力列表。
【冒险经历】：本次冒险过程中的关键事件摘要。
【结局走向】：GM 对本次冒险结局的概括（可能为空）。
【世界信息】：当前世界名与已有世界书条目名列表（用于查重）。

输出字段契约（字段名必须完全一致，无值就用 null 或空数组）：
{"attr_delta": {"str": 0, "agi": 0, "spr": 0}, "new_abilities": [], "race": null, "job": null, "ending": null, "lore_extracts": []}
- attr_delta：三维成长，每项 0~2、三项合计不超过 3；仅当叙事中有明确成长依据时才为正，否则全 0。
- new_abilities：最多 2 条，形如 {"type": "active 或 passive", "name": "能力名", "desc": "一句话描述"}；没有新能力时给空数组。
- race / job：仅当冒险中明确获得或变更了种族/职业时给新名称字符串，否则为 null。
- ending：有明确结局时给 {"title": "20字以内标题", "summary": "60字以内概括", "tags": ["1~3个标签"]}，否则为 null。
- lore_extracts：最多 2 条，形如 {"name": "条目名", "keys": ["触发关键词"], "content": "300字以内的世界信息"}；只提取值得长期记住的世界信息（关键NPC、地点、伏笔、世界规则），不要复述角色私人经历。
"""


def parse_settle_payload(text: str) -> dict | None:
    """宽容解析 LLM 输出的结算 JSON；任何异常返回 None，绝不抛出。

    支持剥离 markdown 代码围栏、截取首个 ``{`` 到最后一个 ``}``，并逐字段
    做类型过滤：只保留契约内的合法字段。
    """
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
        if not isinstance(payload, dict):
            return None
        result: dict[str, Any] = {}
        if isinstance(payload.get("attr_delta"), dict):
            result["attr_delta"] = payload["attr_delta"]
        if isinstance(payload.get("new_abilities"), list):
            result["new_abilities"] = [
                item for item in payload["new_abilities"] if isinstance(item, dict)
            ]
        for key in ("race", "job"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                result[key] = value.strip()
        if isinstance(payload.get("ending"), dict):
            result["ending"] = payload["ending"]
        if isinstance(payload.get("lore_extracts"), list):
            result["lore_extracts"] = [
                item for item in payload["lore_extracts"] if isinstance(item, dict)
            ]
        return result
    except Exception:  # noqa: BLE001 - 解析绝不向上抛异常
        return None


def clamp_attr_delta(char: dict, delta: Any) -> dict:
    """把 LLM 给出的属性成长钳制为可安全应用的 ``{str, agi, spr}``。

    规则：只接受 int/float（bool 除外），负值取 0；每项钳到 0~2；合计上限
    MAX_ATTR_DELTA_SUM，超出时按 力量>敏捷>精神 优先截断；单项不超过
    MAX_ATTR-当前值。本函数只计算，不修改 char。
    """
    applied = {"str": 0, "agi": 0, "spr": 0}
    if not isinstance(delta, dict):
        return applied
    attr = char.get("attr") if isinstance(char, dict) else None
    if not isinstance(attr, dict):
        attr = {}
    remaining = MAX_ATTR_DELTA_SUM
    for key in ("str", "agi", "spr"):
        if remaining <= 0:
            break
        raw = delta.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        try:
            current = int(attr.get(key, 0) or 0)
        except (TypeError, ValueError):
            current = 0
        value = min(int(raw), 2, remaining, max(0, MAX_ATTR - current))
        if value <= 0:
            continue
        applied[key] = value
        remaining -= value
    return applied


def _normalize_ending(ending: Any) -> dict | None:
    """归一化结局卡：title ≤20 字、summary ≤60 字、tags ≤3 个。"""
    if not isinstance(ending, dict):
        return None
    title = str(ending.get("title", "") or "").strip()[:20] or "未命名结局"
    summary = str(ending.get("summary", "") or "").strip()[:60]
    tags: list[str] = []
    raw_tags = ending.get("tags")
    if isinstance(raw_tags, list):
        for tag in raw_tags:
            tag_text = str(tag or "").strip()
            if tag_text and tag_text not in tags:
                tags.append(tag_text)
            if len(tags) >= 3:
                break
    return {"title": title, "summary": summary, "tags": tags}


def apply_settle_result(
    char: dict, world: dict | None, result: dict
) -> tuple[list[str], dict | None]:
    """把结算结果应用到角色与世界，返回 (变化说明列表, 结局卡或 None)。

    所有分支均做防御，绝不抛出异常；char/world 不合法时跳过对应部分。
    """
    changes: list[str] = []
    ending_out: dict | None = None
    try:
        if not isinstance(char, dict) or not isinstance(result, dict):
            return changes, ending_out

        # ── 1. 属性成长 ──
        applied = clamp_attr_delta(char, result.get("attr_delta"))
        if any(applied.values()):
            attr = char.get("attr")
            if not isinstance(attr, dict):
                attr = {}
                char["attr"] = attr
            for key in ("str", "agi", "spr"):
                gain = applied[key]
                if gain <= 0:
                    continue
                try:
                    current = int(attr.get(key, 0) or 0)
                except (TypeError, ValueError):
                    current = 0
                attr[key] = min(MAX_ATTR, current + gain)
                changes.append(f"{_ATTR_LABELS[key]} +{gain}（冒险成长）")
            _recalc_rpg(char)
            char["updated_at"] = time.time()

        # ── 2. 新能力 ──
        abilities = result.get("new_abilities")
        if isinstance(abilities, list):
            skills = char.get("skills")
            if not isinstance(skills, dict):
                skills = {}
                char["skills"] = skills
            caps = {"active": MAX_ACTIVE_SKILLS, "passive": MAX_PASSIVE_SKILLS}
            buckets: dict[str, list] = {}
            for group in ("active", "passive"):
                items = skills.get(group)
                if not isinstance(items, list):
                    items = []
                    skills[group] = items
                buckets[group] = items
            for item in abilities:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "") or "").strip()
                sk_type = str(item.get("type", "") or "").strip().lower()
                if not name or sk_type not in caps:
                    continue
                bucket = buckets[sk_type]
                if len(bucket) >= caps[sk_type]:
                    continue
                if any(
                    isinstance(sk, dict) and str(sk.get("name", "") or "").strip() == name
                    for sk in bucket
                ):
                    continue
                desc = str(item.get("desc", "") or "").strip()
                if sk_type == "active":
                    bucket.append({"name": name, "desc": desc, "mastery": "生疏", "lv": 1})
                    changes.append(f"⚡ 新增主动能力「{name}」")
                else:
                    bucket.append({"name": name, "desc": desc, "mastery": "常驻", "lv": 0})
                    changes.append(f"🌀 新增被动能力「{name}」")
                char["updated_at"] = time.time()

        # ── 3. 种族 / 职业 ──
        for field, label, emoji, builder in (
            (
                "race", "种族", "👤",
                lambda n: {
                    "name": n,
                    "innate_passives": [],
                    "innate_negatives": [],
                    "attr_bonus": {"str": 0, "agi": 0, "spr": 0},
                },
            ),
            (
                "job", "职业", "🏆",
                lambda n: {
                    "name": n,
                    "job_skill": None,
                    "attr_bonus": {"str": 0, "agi": 0, "spr": 0},
                },
            ),
        ):
            value = result.get(field)
            if not isinstance(value, str):
                continue
            value = value.strip()
            if not value:
                continue
            current = char.get(field)
            current_name = current.get("name") if isinstance(current, dict) else None
            if value == current_name:
                continue
            char[field] = builder(value)
            changes.append(f"{emoji} {label}变更为「{value}」")
            _recalc_rpg(char)
            char["updated_at"] = time.time()

        # ── 4. 结局卡 ──
        ending_out = _normalize_ending(result.get("ending"))
        if ending_out is not None:
            changes.append(f"📖 结局已记录：「{ending_out['title']}」")

        # ── 5. 世界书沉淀 ──
        extracts = result.get("lore_extracts")
        if isinstance(extracts, list) and isinstance(world, dict):
            entries = world.get("entries")
            if not isinstance(entries, list):
                entries = []
                world["entries"] = entries
            known_names = {
                str(e.get("name", "") or "").strip() for e in entries if isinstance(e, dict)
            }
            accepted = 0
            for item in extracts:
                if accepted >= MAX_LORE_EXTRACTS or len(entries) >= MAX_WORLD_ENTRIES:
                    break
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "") or "").strip()
                content = str(item.get("content", "") or "").strip()
                if not name or not content or name in known_names:
                    continue
                raw_keys = item.get("keys")
                if isinstance(raw_keys, list):
                    key_candidates: list[Any] = raw_keys
                elif isinstance(raw_keys, str) and raw_keys.strip():
                    key_candidates = [raw_keys]
                else:
                    key_candidates = []
                keys: list[str] = []
                for key in key_candidates:
                    key_text = str(key or "").strip()
                    if key_text and key_text not in keys:
                        keys.append(key_text)
                    if len(keys) >= MAX_LORE_KEYS:
                        break
                entries.append(normalize_lore_entry({
                    "name": name,
                    "keys": keys,
                    "content": content[:MAX_LORE_CONTENT],
                }))
                known_names.add(name)
                accepted += 1
                changes.append(f"🌍 世界书沉淀：「{name}」")

        return changes, ending_out
    except Exception:  # noqa: BLE001 - 结算绝不向上抛异常
        return changes, ending_out
