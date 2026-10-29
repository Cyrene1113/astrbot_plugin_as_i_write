"""角色卡/世界书 schema 兼容层与统一冒险上下文编译器。

schema v3 相对 v2 的增强（保持向后兼容）：
- 世界观的 ``overview`` / ``npc`` / ``plotlines`` / ``clues`` / ``locations`` /
  ``factions`` 结构化板块归一化逻辑从 web_api 收拢到本模块，所有写入路径
  （聊天投稿、WebUI 保存、启动迁移）统一走 :func:`normalize_world`；
- 伏笔（clues）新增 ``revealed`` 揭晓状态；
- :func:`build_adventure_context` 会把结构化板块渲染为「GM 参考块」注入
  world_context（受独立字符预算约束），让 WebUI 里精细编辑的内容真正生效。
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from typing import Any, Iterable

CURRENT_SCHEMA_VERSION = 3
DEFAULT_LORE_BUDGET = 4000
INSERTION_POSITIONS = (
    "system",
    "before_char",
    "after_char",
    "before_history",
    "after_history",
)

# 结构化板块注入冒险上下文的默认字符预算（与世界书条目预算相互独立）
SECTIONS_BUDGET_DEFAULT = 2000
_SECTIONS_ITEM_CAPS = {"npc": 12, "plotlines": 6, "clues": 10, "locations": 8, "factions": 6, "eggs": 6}

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

# 旧版本遗留字段：归一化时直接剥离，由启动时的 normalize_collections 落盘完成数据迁移
LEGACY_CHARACTER_KEYS = ("skill_points", "attr_locked", "active_skills", "passive_skills")


def _new_entry_id() -> str:
    return uuid.uuid4().hex


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,，、/|｜\n]+", value) if part.strip()]
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


# ════════════════════════════════════════════════
#  世界观结构化板块归一化（v3）
# ════════════════════════════════════════════════


def _split_csv_text(value: Any) -> list[str]:
    """把逗号/顿号/斜杠分隔的文本拆成干净列表；列表输入原样清洗。"""
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if not value:
        return []
    return [part.strip() for part in re.split(r"[,，、/|｜]", str(value)) if part.strip()]


def _normalize_npc(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"name": str(item or ""), "title": "", "alignment": "中立", "description": "",
                "traits": [], "dialog_samples": [], "power_tag": "", "power": 0, "is_key": False}
    result = copy.deepcopy(item)
    result["name"] = str(result.get("name") or "未命名NPC")
    result["title"] = str(result.get("title") or "")
    result["alignment"] = str(result.get("alignment") or "中立")
    result["description"] = str(result.get("description") or "")
    result["traits"] = _as_string_list(result.get("traits"))
    result["dialog_samples"] = _as_string_list(result.get("dialog_samples"))
    result["power_tag"] = str(result.get("power_tag") or "")
    try:
        result["power"] = int(result.get("power", 0) or 0)
    except (TypeError, ValueError):
        result["power"] = 0
    result["is_key"] = bool(result.get("is_key", False))
    return result


def _normalize_plotline(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"title": str(item or ""), "type": "支线", "summary": "", "stages": [], "related_npc": []}
    result = copy.deepcopy(item)
    result["title"] = str(result.get("title") or "未命名剧情线")
    result["type"] = str(result.get("type") or "支线")
    result["summary"] = str(result.get("summary") or "")
    stages_raw = result.get("stages")
    stages: list[dict[str, Any]] = []
    if isinstance(stages_raw, list):
        for raw in stages_raw:
            if not isinstance(raw, dict):
                stages.append({"title": str(raw or ""), "description": "", "trigger": "", "clues": [], "choices": []})
                continue
            stage = copy.deepcopy(raw)
            stage["title"] = str(stage.get("title") or "")
            stage["description"] = str(stage.get("description") or "")
            stage["trigger"] = str(stage.get("trigger") or "")
            stage["clues"] = _as_string_list(stage.get("clues") if stage.get("clues") is not None else stage.get("related_clues"))
            stage.setdefault("choices", [])
            stages.append(stage)
    result["stages"] = stages
    result["related_npc"] = _split_csv_text(result.get("related_npc"))
    return result


def _normalize_clue(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"name": str(item or ""), "type": "物证", "content": "", "related_npc": [],
                "unlocks": [], "revealed": False}
    result = copy.deepcopy(item)
    result["name"] = str(result.get("name") or "未命名伏笔")
    result["type"] = str(result.get("type") or "物证")
    result["content"] = str(result.get("content") or "")
    result["related_npc"] = _split_csv_text(result.get("related_npc"))
    result["unlocks"] = _split_csv_text(result.get("unlocks"))
    result["revealed"] = bool(result.get("revealed", False))
    return result


def _normalize_location(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"name": str(item or ""), "type": "地标", "description": "", "related_npcs": [], "is_important": False}
    result = copy.deepcopy(item)
    result["name"] = str(result.get("name") or "未命名地点")
    result["type"] = str(result.get("type") or "地标")
    result["description"] = str(result.get("description") or "")
    result["related_npcs"] = _split_csv_text(result.get("related_npcs"))
    result["is_important"] = bool(result.get("is_important", False))
    return result


def _normalize_faction(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"name": str(item or ""), "alignment": "中立", "description": "", "members": [], "relations": ""}
    result = copy.deepcopy(item)
    result["name"] = str(result.get("name") or "未命名势力")
    result["alignment"] = str(result.get("alignment") or "中立")
    result["description"] = str(result.get("description") or "")
    result["members"] = _split_csv_text(result.get("members"))
    result["relations"] = str(result.get("relations") or "")
    return result


def _normalize_overview(world: dict[str, Any], name: str) -> dict[str, Any]:
    overview = world.get("overview")
    overview = copy.deepcopy(overview) if isinstance(overview, dict) else {}
    content = str(world.get("content") or "")
    overview.setdefault("title", world.get("title") or world.get("_name") or name)
    overview.setdefault("type", world.get("type") or "")
    overview.setdefault("description", world.get("description") or content)
    overview["tags"] = _split_csv_text(overview.get("tags") if overview.get("tags") is not None else world.get("tags"))
    overview.setdefault("power_tier", world.get("power_tier") or "")
    overview.setdefault("power_reference", world.get("power_reference") or "")
    return overview


def _normalize_plot_nodes_compat(world: dict[str, Any]) -> list[dict[str, Any]]:
    """把旧版顶层 plot_nodes 迁移为 plotlines（现网数据仍在使用，不可移除）。"""
    plotlines: list[dict[str, Any]] = []
    nodes = world.get("plot_nodes")
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        plotlines.append(_normalize_plotline({
            "title": node.get("title", "未命名剧情线"),
            "type": node.get("type", "支线"),
            "summary": node.get("summary") or node.get("description", ""),
            "stages": [{
                "title": node.get("title", ""),
                "description": node.get("summary") or node.get("description", ""),
                "trigger": node.get("trigger", ""),
                "clues": _split_csv_text(node.get("related_clues") or node.get("clues")),
                "choices": [],
            }],
            "related_npc": _split_csv_text(node.get("related_npc")),
        }))
    return plotlines


def normalize_world(world: Any, *, name: str = "") -> dict[str, Any]:
    """把旧 world 原地语义迁移为 schema v3，不删除任何旧字段。

    涵盖：content → entries 迁移、overview 结构化、npc/plotlines/clues/
    locations/factions 板块归一化、旧 plot_nodes 兼容、eggs 规范化。
    """
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

    result["overview"] = _normalize_overview(result, name)

    if not isinstance(result.get("plotlines"), list):
        result["plotlines"] = _normalize_plot_nodes_compat(result)
    else:
        result["plotlines"] = [_normalize_plotline(item) for item in result["plotlines"]]

    for key, normalizer in (
        ("npc", _normalize_npc),
        ("clues", _normalize_clue),
        ("locations", _normalize_location),
        ("factions", _normalize_faction),
    ):
        section = result.get(key)
        result[key] = [normalizer(item) for item in section] if isinstance(section, list) else []

    # 旧版只有 easter_eggs 时先迁移为 eggs，再交给 sync_world_eggs 规范化
    if "eggs" not in result:
        legacy_eggs = result.get("easter_eggs", [])
        if isinstance(legacy_eggs, list):
            result["eggs"] = [
                item if isinstance(item, dict)
                else {"text": str(item).strip(), "trigger_condition": "", "reward": ""}
                for item in legacy_eggs if str(item or "").strip()
            ]
    return sync_world_eggs(result)


def _egg_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("text") or item.get("content") or "").strip()
    return str(item or "").strip()


def sync_world_eggs(world: dict[str, Any]) -> dict[str, Any]:
    """规范化结构化 eggs 字段，并清除旧版 easter_eggs 冗余字段。"""
    structured: list[dict[str, Any]] = []
    seen: set[str] = set()
    source = world.get("eggs", [])
    if isinstance(source, list):
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
    world.pop("easter_eggs", None)
    return world


def get_world_eggs(world: Any) -> list[str]:
    """统一读取彩蛋文本列表。"""
    if not isinstance(world, dict):
        return []
    normalized = sync_world_eggs(copy.deepcopy(world))
    return [str(item.get("text", "")) for item in normalized.get("eggs", []) if item.get("text")]


def _split_name_desc(text: Any) -> dict[str, str] | None:
    """把旧版「名字：描述」合体字符串拆为结构化特质；无名字返回 None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    for sep in ("：", ":"):
        if sep in raw:
            name, desc = raw.split(sep, 1)
            name = name.strip()
            if name:
                return {"name": name, "desc": desc.strip()}
    return {"name": raw, "desc": ""}


def _normalize_char_arch_field(field: Any, kind: str) -> Any:
    """把角色卡 race/job 字段归一化为 v1.5 结构（非 dict 原样返回，交由上层防御）。"""
    if field is None or not isinstance(field, dict):
        return field
    result = copy.deepcopy(field)
    result["name"] = str(result.get("name") or "")
    result["description"] = str(result.get("description") or "")
    bonus = result.get("attr_bonus") if isinstance(result.get("attr_bonus"), dict) else {}
    normalized_bonus: dict[str, int] = {}
    for key in ("str", "agi", "spr"):
        try:
            normalized_bonus[key] = int(bonus.get(key, 0) or 0)
        except (TypeError, ValueError):
            normalized_bonus[key] = 0
    result["attr_bonus"] = normalized_bonus
    if kind == "race":
        for key in ("innate_passives", "innate_negatives"):
            raw = result.get(key)
            traits: list[dict[str, str]] = []
            for item in raw if isinstance(raw, list) else []:
                if isinstance(item, dict):
                    name = str(item.get("name") or "").strip()
                    if name:
                        traits.append({"name": name, "desc": str(item.get("desc") or "")})
                else:
                    split = _split_name_desc(item)
                    if split:
                        traits.append(split)
            result[key] = traits
        result.setdefault("race_lore", str(result.get("race_lore") or ""))
    else:
        job_skill = result.get("job_skill")
        if isinstance(job_skill, dict) and str(job_skill.get("name") or "").strip():
            result["job_skill"] = {"name": str(job_skill["name"]).strip(), "desc": str(job_skill.get("desc") or "")}
        elif isinstance(job_skill, str) and job_skill.strip():
            result["job_skill"] = _split_name_desc(job_skill)
        elif job_skill is not None:
            result["job_skill"] = None
        raw_skills = result.get("job_skills")
        skills: list[dict[str, str]] = []
        for item in raw_skills if isinstance(raw_skills, list) else []:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if name:
                    skills.append({"name": name, "desc": str(item.get("desc") or "")})
            else:
                split = _split_name_desc(item)
                if split:
                    skills.append(split)
        if not skills and isinstance(result.get("job_skill"), dict):
            skills = [copy.deepcopy(result["job_skill"])]
        result["job_skills"] = skills
        result.setdefault("play_style", str(result.get("play_style") or ""))
    return result


def normalize_character(character: Any) -> dict[str, Any]:
    """补齐 Chara Card 风格字段，并保留插件 RPG 旧字段。"""
    if not isinstance(character, dict):
        character = {"description": str(character or "")}
    result = copy.deepcopy(character)
    result["schema_version"] = CURRENT_SCHEMA_VERSION
    for key in LEGACY_CHARACTER_KEYS:
        result.pop(key, None)
    skills = result.get("skills")
    if isinstance(skills, dict):
        for group in ("active", "passive"):
            items = skills.get(group)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        item.pop("mp_cost", None)
    result["race"] = _normalize_char_arch_field(result.get("race"), "race")
    result["job"] = _normalize_char_arch_field(result.get("job"), "job")
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


def render_world_sections(world: Any, max_chars: int = SECTIONS_BUDGET_DEFAULT) -> str:
    """把世界观结构化板块渲染为「GM 参考块」文本。

    各板块有条数上限，整体有字符预算；空板块不出现在结果中。
    """
    world_data = world if isinstance(world, dict) else {}
    blocks: list[str] = []

    def _clip(text: Any, limit: int) -> str:
        text = str(text or "").strip().replace("\n", " ")
        return text[:limit]

    npcs = world_data.get("npc") if isinstance(world_data.get("npc"), list) else []
    npc_lines: list[str] = []
    for n in npcs[: _SECTIONS_ITEM_CAPS["npc"]]:
        if not isinstance(n, dict) or not str(n.get("name") or "").strip():
            continue
        segs = [f"{n['name']}（{n.get('title') or '无名'}｜{n.get('alignment') or '中立'}）"]
        desc = _clip(n.get("description"), 140)
        if desc:
            segs.append(desc)
        traits = [str(t).strip() for t in (n.get("traits") or []) if str(t).strip()]
        if traits:
            segs.append("特质：" + "、".join(traits[:4]))
        dialogs = [str(d).strip() for d in (n.get("dialog_samples") or []) if str(d).strip()]
        if dialogs:
            segs.append(f'口头禅："{_clip(dialogs[0], 30)}"')
        if n.get("power"):
            segs.append(f"战力 {n.get('power')}")
        elif n.get("power_tag"):
            segs.append(f"强度：{n.get('power_tag')}")
        if n.get("is_key"):
            segs.append("★关键")
        npc_lines.append("· " + "：".join(segs[:2]) + ("" if len(segs) <= 2 else "｜" + "｜".join(segs[2:])))
    if npc_lines:
        blocks.append("【世界人物（GM 参考）】\n" + "\n".join(npc_lines))

    plots = world_data.get("plotlines") if isinstance(world_data.get("plotlines"), list) else []
    plot_lines: list[str] = []
    for p in plots[: _SECTIONS_ITEM_CAPS["plotlines"]]:
        if not isinstance(p, dict) or not str(p.get("title") or "").strip():
            continue
        line = f"· 【{p.get('type') or '支线'}】{p['title']}：{_clip(p.get('summary'), 160)}"
        stages = p.get("stages") if isinstance(p.get("stages"), list) else []
        for s in stages[:6]:
            if not isinstance(s, dict):
                continue
            stage_desc = _clip(s.get("description") or s.get("title"), 100)
            if not stage_desc:
                continue
            detail = f"  - 阶段「{s.get('title') or '未命名'}」：{stage_desc}"
            extras: list[str] = []
            if s.get("trigger"):
                extras.append(f"触发：{_clip(s.get('trigger'), 40)}")
            clues = [str(c).strip() for c in (s.get("clues") or []) if str(c).strip()]
            if clues:
                extras.append("关联线索：" + "、".join(clues[:4]))
            if extras:
                detail += "（" + "；".join(extras) + "）"
            line += "\n" + detail
        related = [str(x).strip() for x in (p.get("related_npc") or []) if str(x).strip()]
        if related:
            line += f"\n  关联人物：{'、'.join(related[:5])}"
        plot_lines.append(line)
    if plot_lines:
        blocks.append("【剧情线（GM 参考，按阶段逐步推进，勿一次性揭开）】\n" + "\n".join(plot_lines))

    clues = world_data.get("clues") if isinstance(world_data.get("clues"), list) else []
    clue_lines: list[str] = []
    for c in clues[: _SECTIONS_ITEM_CAPS["clues"]]:
        if not isinstance(c, dict) or not str(c.get("name") or "").strip():
            continue
        state = "已揭晓" if c.get("revealed") else "未揭晓"
        line = f"· 【{state}】{c['name']}（{c.get('type') or '物证'}）：{_clip(c.get('content'), 120)}"
        related = [str(x).strip() for x in (c.get("related_npc") or []) if str(x).strip()]
        unlocks = [str(x).strip() for x in (c.get("unlocks") or []) if str(x).strip()]
        extras: list[str] = []
        if related:
            extras.append("关联：" + "、".join(related[:4]))
        if unlocks:
            extras.append("解锁：" + "、".join(unlocks[:4]))
        if extras:
            line += "（" + "；".join(extras) + "）"
        clue_lines.append(line)
    if clue_lines:
        blocks.append("【伏笔清单（未揭晓的请在合适时机自然揭示，勿提前剧透）】\n" + "\n".join(clue_lines))

    locations = world_data.get("locations") if isinstance(world_data.get("locations"), list) else []
    loc_lines: list[str] = []
    for l in locations[: _SECTIONS_ITEM_CAPS["locations"]]:
        if not isinstance(l, dict) or not str(l.get("name") or "").strip():
            continue
        line = f"· {l['name']}（{l.get('type') or '地标'}{'｜★重要' if l.get('is_important') else ''}）：{_clip(l.get('description'), 110)}"
        related = [str(x).strip() for x in (l.get("related_npcs") or []) if str(x).strip()]
        if related:
            line += f"（关联：{'、'.join(related[:4])}）"
        loc_lines.append(line)
    if loc_lines:
        blocks.append("【重要地点】\n" + "\n".join(loc_lines))

    factions = world_data.get("factions") if isinstance(world_data.get("factions"), list) else []
    fac_lines: list[str] = []
    for f in factions[: _SECTIONS_ITEM_CAPS["factions"]]:
        if not isinstance(f, dict) or not str(f.get("name") or "").strip():
            continue
        line = f"· {f['name']}（{f.get('alignment') or '中立'}）：{_clip(f.get('description'), 110)}"
        members = [str(x).strip() for x in (f.get("members") or []) if str(x).strip()]
        if members:
            line += f"（成员：{'、'.join(members[:4])}）"
        fac_lines.append(line)
    if fac_lines:
        blocks.append("【势力】\n" + "\n".join(fac_lines))

    eggs = world_data.get("eggs") if isinstance(world_data.get("eggs"), list) else []
    egg_lines: list[str] = []
    for e in eggs[: _SECTIONS_ITEM_CAPS["eggs"]]:
        text = _egg_text(e)
        if not text:
            continue
        condition = str(e.get("trigger_condition") or "").strip() if isinstance(e, dict) else ""
        egg_lines.append(f"· {_clip(text, 100)}" + (f"（触发：{_clip(condition, 50)}）" if condition else ""))
    if egg_lines:
        blocks.append("【彩蛋储备（达成触发条件时才揭示）】\n" + "\n".join(egg_lines))

    text = "\n\n".join(blocks)
    try:
        limit = max(0, int(max_chars))
    except (TypeError, ValueError):
        limit = SECTIONS_BUDGET_DEFAULT
    if limit and len(text) > limit:
        cut = text[:limit]
        idx = cut.rfind("\n")
        text = cut[:idx if idx > limit // 2 else limit] + "\n…（其余板块内容因预算截断）"
    return text


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
    长度预算只裁剪世界书条目内容与结构化板块参考块，不裁剪角色基础资料。
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
    sections_budget = max(800, int(max_content_length) // 2) if max_content_length else SECTIONS_BUDGET_DEFAULT
    sections_text = render_world_sections(world_data, sections_budget)
    character_base = _character_text(char_data)
    world_context = "\n\n".join(filter(None, (
        rendered["system"],
        rendered["before_char"],
        world_header,
        sections_text,
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
        "world_sections": sections_text,
        "matched_entries": used_entries,
        "budget": int(max_content_length),
        "used_length": int(max_content_length) - remaining,
    }


def context_debug_json(context: dict[str, Any]) -> str:
    """供诊断与测试输出使用，确保结果可 JSON 序列化。"""
    return json.dumps(context, ensure_ascii=False, indent=2)
