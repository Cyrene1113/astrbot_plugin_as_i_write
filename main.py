"""
📖 如我所书 — 昔涟的文字冒险游戏插件

翻开这本名为「你」的书，在星空下写一段只属于你的冒险。
无论是独自前行，还是与伙伴并肩，故事都从这里开始。
"""

from __future__ import annotations

import asyncio
import random as _random
import time

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.session_waiter import SessionController, session_waiter

from .archetypes import (
    archetype_to_char_field,
    find_archetype,
    render_archetype_line,
    seed_builtin_archetypes,
)
from .core import (
    DATA_DIR,
    PLUGIN_ID,
    _compact_adventure_history,
    _generate_id,
    _load_json,
    _save_json,
    _strip_prefix,
)
from .lore import (
    build_adventure_context,
    get_world_eggs,
    normalize_character,
    normalize_collections,
    normalize_world,
)
from .prompts import GROUP_SYSTEM_PROMPT, HELP_TEXT, SINGLE_SYSTEM_PROMPT
from .render_pipeline import apply_render_scripts
from .rpg import (
    MAX_ACTIVE_SKILLS,
    MAX_PASSIVE_SKILLS,
    MAX_RANDOM_ATTR,
    _build_player_stats,
    _build_world_tier_context,
    _calc_hp,
    _calc_mp,
    _calc_power,
    _default_rpg,
    _ensure_rpg,
    _forget_skill,
    _is_admin,
    _parse_attr_args,
    _power_band,
    _random_grade,
    _recalc_rpg,
    _render_rpg_sheet,
    _skill_names,
    _validate_attrs,
    build_group_judgment_context,
    build_judgment_context,
)
from .settle import SETTLE_EXTRACT_PROMPT, apply_settle_result, parse_settle_payload
from .web_api import AsIWriteWebAPI


def _get_character_skills(char: dict) -> tuple[list, list]:
    """读取角色主动/被动能力列表。"""
    if not isinstance(char, dict):
        return [], []
    skills = char.get("skills", {})
    if not isinstance(skills, dict):
        skills = {}
    active = skills.get("active", [])
    passive = skills.get("passive", [])
    return (
        active if isinstance(active, list) else [],
        passive if isinstance(passive, list) else [],
    )


# ════════════════════════════════════════════════
#  主插件类
# ════════════════════════════════════════════════

@register(PLUGIN_ID, "昔涟", "📖 如我所书 — 由AI驱动的动态文字冒险游戏", version="1.4.0")
class AsIWritePlugin(Star):
    """如我所书 — 翻开书页，故事便开始流淌。"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.config = config if config is not None else {}
        self.default_theme = self.config.get("default_theme", "奇幻世界")
        self.single_timeout = int(self.config.get("single_timeout", 300))
        self.group_round_time = int(self.config.get("group_round_time", 120))
        self.group_max_players = int(self.config.get("group_max_players", 8))
        self.group_max_clues_per_round = int(self.config.get("group_max_clues_per_round", 2))
        self.lore_content_budget = int(self.config.get("lore_content_budget", 4000))
        self.enable_settle_extract = bool(self.config.get("enable_settle_extract", True))
        self.enable_dice_judgment = bool(self.config.get("enable_dice_judgment", True))

        self._characters_path = DATA_DIR / "characters.json"
        self._worlds_path = DATA_DIR / "worlds.json"
        self._saves_path = DATA_DIR / "saves.json"
        self._logs_path = DATA_DIR / "logs.json"
        self._endings_path = DATA_DIR / "endings.json"
        self._render_scripts_path = DATA_DIR / "regex_scripts.json"
        render_data = _load_json(self._render_scripts_path)
        self.render_scripts = render_data.get("scripts", []) if isinstance(render_data, dict) else []

        # ── 种族/职业图鉴（文件即真相；空文件时播种内置模板） ──
        self._archetypes_path = DATA_DIR / "archetypes.json"
        self._archetypes = seed_builtin_archetypes(self._archetypes_path)

        raw_characters = _load_json(self._characters_path)
        raw_worlds = _load_json(self._worlds_path)
        self._characters, self._worlds, characters_changed, worlds_changed = normalize_collections(
            raw_characters, raw_worlds
        )
        if characters_changed:
            _save_json(self._characters_path, self._characters)
            # 启动阶段 _web_api 尚未注册，_notify 为 no-op，仅保持落盘后必通知的惯例
            self._notify("characters_changed", {"uid": "startup"})
        if worlds_changed:
            _save_json(self._worlds_path, self._worlds)
            self._notify("worlds_changed", {})
        self._saves: dict = _load_json(self._saves_path)
        self._logs: dict = _load_json(self._logs_path)
        self._endings: dict = _load_json(self._endings_path)

        self._active_single: dict[str, SessionController] = {}
        self._adventures: dict[str, dict] = {}
        self._group_sessions: dict[str, dict] = {}
        self._group_locks: dict[str, asyncio.Lock] = {}
        self._group_tasks: dict[str, asyncio.Task] = {}

        self._kb_helper = None
        self._kb_name = self.config.get("kb_name", "如我所书")
        
        # ── 官方 Web API（AstrBot register_web_api）──
        self._web_api = AsIWriteWebAPI(self)
        self._web_api.register()

        logger.info(f"📖 如我所书 v1.4.0 | 角色卡:{len(self._characters)} 世界观:{len(self._worlds)} 图鉴:{len(self._archetypes)}")

    def _notify(self, event_type: str, data: dict | None = None) -> None:
        """把聊天侧的数据变更推送给 WebUI（SSE）。"""
        web_api = getattr(self, "_web_api", None)
        if web_api is not None:
            web_api.notify(event_type, data or {})

    def _render_story(self, text: str) -> str:
        """只转换发给用户的副本，历史与存档继续保留 LLM 原文。"""
        return apply_render_scripts(text, self.render_scripts, warning_logger=logger)

    @filter.command_group("冒险")
    def adventure(self):
        pass

    @adventure.command("帮助")
    async def help_cmd(self, event: AstrMessageEvent):
        yield event.plain_result(HELP_TEXT)

    # ── 知识库 ──

    async def _ensure_kb(self) -> bool:
        if self._kb_helper is not None:
            return True
        if not self._kb_name:
            return False
        try:
            kb_mgr = getattr(self.context, "kb_manager", None)
            if kb_mgr is None:
                return False
            kb = await kb_mgr.get_kb_by_name(self._kb_name)
            if kb is not None:
                self._kb_helper = kb
                logger.info(f"📖 如我所书已连接到知识库「{self._kb_name}」")
                return True
        except Exception as e:
            logger.error(f"📖 如我所书 | 知识库: 连接失败: {e}")
        return False

    async def _kb_store(self, type_: str, content: str, metadata: dict = None) -> None:
        try:
            if not await self._ensure_kb():
                return
            meta = {"source": "as_i_write", "type": type_}
            if metadata:
                meta.update(metadata)
            doc_id = f"aiw_{type_}_{_generate_id()}"
            await self._kb_helper.vec_db.insert(content=content, metadata=meta, id=doc_id)
        except Exception as e:
            logger.error(f"📖 如我所书 | 知识库·存储: {str(e)}")

    def terminate(self):
        """插件卸载/重载时取消群体定时任务、关闭 SSE 订阅并移除本插件的 Web API 路由。"""
        for task in list(self._group_tasks.values()):
            if not task.done():
                task.cancel()
        self._group_tasks.clear()
        web_api = getattr(self, "_web_api", None)
        if web_api is not None:
            web_api.close()
        apis = getattr(self.context, "registered_web_apis", None)
        if isinstance(apis, list):
            prefix = f"/{PLUGIN_ID}"
            apis[:] = [
                api for api in apis
                if not (isinstance(api, tuple) and api and str(api[0]).startswith(prefix))
            ]

    @adventure.command("创建角色")
    async def create_character(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        if uid in self._characters:
            yield event.plain_result("已有一张角色卡。使用「冒险 修改角色」更新。")
            return
        rest = _strip_prefix(event.get_message_str().strip(), "冒险 创建角色")
        if not rest:
            yield event.plain_result("格式：冒险 创建角色 <名字> <描述> 或 于<世界>")
            return
        world_name = ""
        if " 于" in rest:
            parts = rest.split(" 于", 1)
            rest = parts[0].strip()
            world_name = parts[1].strip().rstrip("。，")
            if world_name not in self._worlds:
                yield event.plain_result(f"未找到世界观「{world_name}」。")
                return
        name_desc = rest.split(" ", 1)
        if len(name_desc) < 2:
            yield event.plain_result("格式：冒险 创建角色 <名字> <描述>")
            return
        name, desc = name_desc[0].strip(), name_desc[1].strip()
        char_data = normalize_character({
            "name": name, "description": desc,
            "abilities": [], "inventory": [],
            "world": world_name, "social_status": "",
            "created_at": time.time(), "updated_at": time.time(),
        })
        rpg = _default_rpg()
        for k, v in rpg.items():
            char_data[k] = v
        self._characters[uid] = char_data
        _save_json(self._characters_path, self._characters)
        self._notify("characters_changed", {"uid": uid})
        await self._kb_store("character", f"角色「{name}」：{desc}", {"character_name": name, "world_name": world_name or ""})
        msg = f"🎭 角色卡已创建！\n名字：{name}\n背景：{desc}"
        if world_name:
            msg += f"\n🌍 关联世界观：{world_name}"
        msg += "\n\n使用「冒险 属性」查看完整面板♪"
        yield event.plain_result(msg)

    @adventure.command("我的角色")
    async def my_character(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        char = self._characters.get(uid)
        if not char:
            yield event.plain_result("还没有角色卡。使用「冒险 创建角色」创建一张吧♪")
            return
        _ensure_rpg(char)
        yield event.plain_result(_render_rpg_sheet(char))

    @adventure.command("修改角色")
    async def edit_character(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        char = self._characters.get(uid)
        if not char:
            yield event.plain_result("还没有角色卡。先用「冒险 创建角色」吧。")
            return
        rest = _strip_prefix(event.get_message_str().strip(), "冒险 修改角色")
        if not rest:
            yield event.plain_result("请提供新的描述，或使用「添加社会地位 <内容>」。")
            return
        if rest.startswith("添加社会地位"):
            status = _strip_prefix(rest, "添加社会地位")
            if not status:
                yield event.plain_result("格式：冒险 修改角色 添加社会地位 <你在世界中的身份地位>")
                return
            char["social_status"] = status
            char["updated_at"] = time.time()
            _save_json(self._characters_path, self._characters)
            self._notify("characters_changed", {"uid": uid})
            yield event.plain_result(f"✅ 社会地位已更新：{status}")
            return
        if rest.startswith("关联世界"):
            w = _strip_prefix(rest, "关联世界")
            if not w or w not in self._worlds:
                yield event.plain_result(f"未找到世界观「{w}」。")
                return
            char["world"] = w
            char["updated_at"] = time.time()
            _save_json(self._characters_path, self._characters)
            self._notify("characters_changed", {"uid": uid})
            yield event.plain_result(f"✅ 已关联世界观「{w}」")
            return
        char["description"] = rest
        char["updated_at"] = time.time()
        _save_json(self._characters_path, self._characters)
        self._notify("characters_changed", {"uid": uid})
        yield event.plain_result(f"✅ 角色卡已更新：{rest}")

    # ════════════════════════════════════════════════
    #  RPG 角色属性指令
    # ════════════════════════════════════════════════

    @adventure.command("属性")
    async def view_rpg_sheet(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        char = self._characters.get(uid)
        if not char:
            yield event.plain_result("还没有角色卡。使用「冒险 创建角色」创建一张吧♪")
            return
        _ensure_rpg(char)
        yield event.plain_result(_render_rpg_sheet(char))

    @adventure.command("设置属性")
    async def set_attributes(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        char = self._characters.get(uid)
        if not char:
            yield event.plain_result("还没有角色卡。")
            return
        _ensure_rpg(char)
        if char.get("has_adventured") and not _is_admin(event):
            yield event.plain_result("你的角色已完成首次冒险，属性不再允许手动修改。")
            return
        rest = _strip_prefix(event.get_message_str().strip(), "冒险 设置属性")
        parsed = _parse_attr_args(rest)
        if not parsed:
            yield event.plain_result("格式：冒险 设置属性 <力量> <敏捷> <精神>")
            return
        s, ag, sp = parsed
        ok, err = _validate_attrs(s, ag, sp, _is_admin(event), char.get("is_random", False))
        if not ok:
            yield event.plain_result(f"❌ {err}")
            return
        char["attr"]["str"] = s
        char["attr"]["agi"] = ag
        char["attr"]["spr"] = sp
        _recalc_rpg(char)
        char["updated_at"] = time.time()
        _save_json(self._characters_path, self._characters)
        self._notify("characters_changed", {"uid": uid})
        yield event.plain_result(f"✅ 属性已更新！\n💪力量：{s}  🏃敏捷：{ag}  🧠精神：{sp}\n❤️HP：{char['hp']}  💙MP：{char['mp']}  ⚔️战力：{char['power']}")

    @adventure.command("评级")
    async def view_grades(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        char = self._characters.get(uid)
        if not char:
            yield event.plain_result("还没有角色卡。")
            return
        _ensure_rpg(char)
        yield event.plain_result(f"✨ 魅力：**{char.get('charm', '?')}**\n🍀 幸运：**{char.get('luck', '?')}**\n\n💡 魅力与幸运在创建角色时随机生成，终身不可修改。")

    @adventure.command("种族")
    async def set_race(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        char = self._characters.get(uid)
        if not char:
            yield event.plain_result("还没有角色卡。")
            return
        _ensure_rpg(char)
        rest = _strip_prefix(event.get_message_str().strip(), "冒险 种族")
        if not rest:
            yield event.plain_result("格式：冒险 种族 <种族名称>")
            return
        race_name = rest.split(" ")[0].strip()
        arche = find_archetype(self._archetypes, race_name, "race")
        if arche:
            char["race"] = archetype_to_char_field(arche)
            _recalc_rpg(char)
            char["updated_at"] = time.time()
            _save_json(self._characters_path, self._characters)
            self._notify("characters_changed", {"uid": uid})
            yield event.plain_result(
                f"🧬 种族已设为「{race_name}」（图鉴模板）\n📋 {render_archetype_line(arche)}"
                f"\n⚔️ 阶层：{_power_band(int(char['power']))['name']}（战力 {char['power']}）"
            )
            return
        char["race"] = {"name": race_name, "innate_passives": [], "innate_negatives": [], "attr_bonus": {"str": 0, "agi": 0, "spr": 0}}
        _recalc_rpg(char)
        char["updated_at"] = time.time()
        _save_json(self._characters_path, self._characters)
        self._notify("characters_changed", {"uid": uid})
        yield event.plain_result(f"✅ 种族已设为「{race_name}」\n💡 种族补正和被动能力将由 GM 在冒险中写入。\n💡 未在图鉴中找到，已按自定义种族记录；「冒险 种族列表」查看内置模板")

    @adventure.command("职业")
    async def set_job(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        char = self._characters.get(uid)
        if not char:
            yield event.plain_result("还没有角色卡。")
            return
        _ensure_rpg(char)
        rest = _strip_prefix(event.get_message_str().strip(), "冒险 职业")
        if not rest:
            yield event.plain_result("格式：冒险 职业 <职业名称>")
            return
        job_name = rest.split(" ")[0].strip()
        arche = find_archetype(self._archetypes, job_name, "job")
        if arche:
            char["job"] = archetype_to_char_field(arche)
            _recalc_rpg(char)
            char["updated_at"] = time.time()
            _save_json(self._characters_path, self._characters)
            self._notify("characters_changed", {"uid": uid})
            yield event.plain_result(
                f"🧬 职业已设为「{job_name}」（图鉴模板）\n📋 {render_archetype_line(arche)}"
                f"\n⚔️ 阶层：{_power_band(int(char['power']))['name']}（战力 {char['power']}）"
            )
            return
        char["job"] = {"name": job_name, "job_skill": None, "attr_bonus": {"str": 0, "agi": 0, "spr": 0}}
        _recalc_rpg(char)
        char["updated_at"] = time.time()
        _save_json(self._characters_path, self._characters)
        self._notify("characters_changed", {"uid": uid})
        yield event.plain_result(f"✅ 职业已设为「{job_name}」\n💡 专属技能和补正将由 GM 在冒险中写入。\n💡 未在图鉴中找到，已按自定义职业记录；「冒险 职业列表」查看内置模板")

    def _render_archetype_list(self, type_: str, title: str, label: str) -> str:
        """渲染图鉴列表（最多 30 行，超出省略）。"""
        items = [
            a for a in self._archetypes.values()
            if isinstance(a, dict) and str(a.get("type", "")) == type_
        ]
        if not items:
            return f"📖 **{title}**\n\n图鉴中还没有{label}模板。"
        lines = [f"📖 **{title}**（冒险 {label} <名称> 套用模板）"]
        for a in items[:30]:
            lines.append("  · " + render_archetype_line(a))
        if len(items) > 30:
            lines.append(f"  ...还有 {len(items) - 30} 条{label}模板")
        return "\n".join(lines)

    @adventure.command("种族列表")
    async def list_races(self, event: AstrMessageEvent):
        yield event.plain_result(self._render_archetype_list("race", "种族图鉴", "种族"))

    @adventure.command("职业列表")
    async def list_jobs(self, event: AstrMessageEvent):
        yield event.plain_result(self._render_archetype_list("job", "职业图鉴", "职业"))

    def _apply_archetype_templates(self, char: dict) -> list[str]:
        """图鉴富化：race/job 名称命中模板且当前补正三项全零时，套用模板字段并重算面板。"""
        enriched: list[str] = []
        if not isinstance(char, dict):
            return enriched
        for key, type_ in (("race", "race"), ("job", "job")):
            field = char.get(key)
            if not isinstance(field, dict):
                continue
            name = str(field.get("name", "") or "").strip()
            if not name:
                continue
            bonus = field.get("attr_bonus")
            bonus = bonus if isinstance(bonus, dict) else {}
            try:
                if any(int(bonus.get(k, 0) or 0) != 0 for k in ("str", "agi", "spr")):
                    continue
            except (TypeError, ValueError):
                continue
            arche = find_archetype(self._archetypes, name, type_)
            if not arche:
                continue
            char[key] = archetype_to_char_field(arche)
            _recalc_rpg(char)
            enriched.append(f"🧬 「{name}」已套用图鉴模板补正")
        return enriched

    @adventure.command("技能")
    async def skill_cmd(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        char = self._characters.get(uid)
        if not char:
            yield event.plain_result("还没有角色卡。")
            return
        _ensure_rpg(char)
        rest = _strip_prefix(event.get_message_str().strip(), "冒险 技能")
        if not rest:
            yield event.plain_result("格式：\n  冒险 技能 主动 <名称> <描述>\n  冒险 技能 被动 <名称> <描述>\n  冒险 技能 遗忘 <名称>\n\n能力标签用于叙事判定锚点，不再做复杂MP/等级/技能点结算。")
            return
        parts = rest.split(" ", 1)
        sub = parts[0].strip()
        args = parts[1].strip() if len(parts) > 1 else ""
        if sub == "主动":
            sub_parts = args.split(" ", 1)
            if len(sub_parts) < 2:
                yield event.plain_result("格式：冒险 技能 主动 <名称> <描述>")
                return
            sk_name = sub_parts[0].strip()
            sk_desc = sub_parts[1].strip()
            active = char["skills"].setdefault("active", [])
            if len(active) >= MAX_ACTIVE_SKILLS:
                yield event.plain_result(f"主动能力已达上限（{MAX_ACTIVE_SKILLS}个）")
                return
            active.append({"name": sk_name, "desc": sk_desc, "mastery": "生疏", "lv": 1})
            char["updated_at"] = time.time()
            _save_json(self._characters_path, self._characters)
            self._notify("characters_changed", {"uid": uid})
            yield event.plain_result(f"✅ 主动能力「{sk_name}」已记录。")
        elif sub == "被动":
            sub_parts = args.split(" ", 1)
            if len(sub_parts) < 2:
                yield event.plain_result("格式：冒险 技能 被动 <名称> <描述>")
                return
            sk_name = sub_parts[0].strip()
            sk_desc = sub_parts[1].strip()
            passive = char["skills"].setdefault("passive", [])
            if len(passive) >= MAX_PASSIVE_SKILLS:
                yield event.plain_result(f"被动特质已达上限（{MAX_PASSIVE_SKILLS}个）")
                return
            passive.append({"name": sk_name, "desc": sk_desc, "mastery": "常驻", "lv": 0})
            char["updated_at"] = time.time()
            _save_json(self._characters_path, self._characters)
            self._notify("characters_changed", {"uid": uid})
            yield event.plain_result(f"✅ 被动特质「{sk_name}」已记录。")
        elif sub == "遗忘":
            sk_name = args
            if not sk_name:
                yield event.plain_result("格式：冒险 技能 遗忘 <技能名称>")
                return
            ok, msg = False, ""
            for st in ["active", "passive"]:
                ok, msg = _forget_skill(char, sk_name, st)
                if ok:
                    break
            if not ok:
                yield event.plain_result(f"❌ {msg}")
            else:
                _save_json(self._characters_path, self._characters)
                self._notify("characters_changed", {"uid": uid})
                yield event.plain_result(f"✅ {msg}")
        else:
            yield event.plain_result("未知操作。支持：主动 / 被动 / 遗忘")

    @adventure.command("技能点")
    async def view_skill_points(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        char = self._characters.get(uid)
        if not char:
            yield event.plain_result("还没有角色卡。")
            return
        _ensure_rpg(char)
        skills = char.get("skills", {})
        active_count = len(skills.get("active", []))
        passive_count = len(skills.get("passive", []))
        yield event.plain_result(f"✨ 能力栏位\n⚡ 主动能力：{active_count}/{MAX_ACTIVE_SKILLS}\n🌀 被动特质：{passive_count}/{MAX_PASSIVE_SKILLS}\n\n能力用于叙事判定锚点，不做复杂技能点结算。")

    @adventure.command("栏位")
    async def view_slots(self, event: AstrMessageEvent):
        async for r in self.view_skill_points(event):
            yield r

    @adventure.command("重置属性")
    async def reset_attributes(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        char = self._characters.get(uid)
        if not char:
            yield event.plain_result("还没有角色卡。")
            return
        if char.get("has_adventured") and not _is_admin(event):
            yield event.plain_result("已完成首次冒险，不可重置属性。")
            return
        rpg = _default_rpg()
        rpg["charm"] = char.get("charm", rpg["charm"])
        rpg["luck"] = char.get("luck", rpg["luck"])
        for k, v in rpg.items():
            char[k] = v
        _recalc_rpg(char)
        char["updated_at"] = time.time()
        _save_json(self._characters_path, self._characters)
        self._notify("characters_changed", {"uid": uid})
        yield event.plain_result("✅ 属性已重置为默认值。魅力/幸运保持不变♪")

    @adventure.command("随机角色")
    async def random_character(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        if uid in self._characters:
            yield event.plain_result("已有一张角色卡。")
            return
        rest = _strip_prefix(event.get_message_str().strip(), "冒险 随机角色")
        world_name = rest.strip() if rest and rest in self._worlds else ""
        provider = self.context.get_using_provider()
        if not provider:
            yield event.plain_result("暂无可用 LLM 服务。")
            return
        world_hint = f"该角色来自世界观「{world_name}」——{self._worlds[world_name].get('content', '')[:300]}" if world_name else ""
        prompt = (
            f"请创建一个适合冒险的角色卡。{world_hint}\n\n要求：\n"
            "1. 角色名字(2-5字)\n2. 简短背景描述(50字以内)\n"
            "3. 三维属性(力量/敏捷/精神)各1-20随机，必须严格单行格式：力量：12/10/8\n"
            "4. 可能拥有1个种族和1个职业\n5. 可能拥有0-2个主动技能和0-2个被动能力\n\n"
            "严格按以下格式输出（只用中文冒号）：\n名字：\n背景：\n力量：12/10/8\n种族：(可空)\n职业：(可空)\n主动技能：(可空，多个用；分隔)\n被动能力：(可空，多个用；分隔)"
        )
        try:
            resp = await provider.text_chat(prompt=prompt, session_id=event.get_session_id(), contexts=[])
            text = resp.completion_text.strip()
        except Exception as e:
            logger.error(f"📖 如我所书 | 随机角色: {str(e)}")
            yield event.plain_result("随机生成失败，请稍后再试。")
            return
        name = "旅人"
        desc = "一位来历不明的冒险者"
        str_v, agi_v, spr_v = _random.randint(3, 15), _random.randint(3, 15), _random.randint(3, 15)
        race_info = None
        job_info = None
        active_skills = []
        passive_skills = []
        attr_parsed = False
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("名字：") or line.startswith("名字:"):
                name = _strip_prefix(line, "名字：").strip() or _strip_prefix(line, "名字:").strip() or name
            elif line.startswith("背景：") or line.startswith("背景:"):
                desc = _strip_prefix(line, "背景：").strip() or _strip_prefix(line, "背景:").strip() or desc
            elif line.startswith("力量：") or line.startswith("力量:"):
                try:
                    raw = line.split("：", 1)[-1].split(":", 1)[-1].strip()
                    parts_attr = raw.split("/")
                    if len(parts_attr) >= 2:
                        str_v = min(MAX_RANDOM_ATTR, max(1, int(parts_attr[0])))
                        agi_v = min(MAX_RANDOM_ATTR, max(1, int(parts_attr[1])))
                        spr_v = min(MAX_RANDOM_ATTR, max(1, int(parts_attr[2]) if len(parts_attr) >= 3 else spr_v))
                        attr_parsed = True
                except (ValueError, IndexError):
                    pass
            elif line.startswith("种族：") or line.startswith("种族:"):
                rname = _strip_prefix(line, "种族：").strip() or _strip_prefix(line, "种族:").strip()
                if rname and rname != "无":
                    race_info = {"name": rname, "innate_passives": [], "innate_negatives": [], "attr_bonus": {"str": 0, "agi": 0, "spr": 0}}
            elif line.startswith("职业：") or line.startswith("职业:"):
                jname = _strip_prefix(line, "职业：").strip() or _strip_prefix(line, "职业:").strip()
                if jname and jname != "无":
                    job_info = {"name": jname, "job_skill": None, "attr_bonus": {"str": 0, "agi": 0, "spr": 0}}
            elif line.startswith("主动技能：") or line.startswith("主动技能:"):
                sk_text = _strip_prefix(line, "主动技能：").strip() or _strip_prefix(line, "主动技能:").strip()
                if sk_text and sk_text != "无":
                    for sk in sk_text.split("；"):
                        sk = sk.strip()
                        if sk:
                            active_skills.append({"name": sk, "desc": sk, "mastery": "生疏", "lv": 1})
            elif line.startswith("被动能力：") or line.startswith("被动能力:"):
                pk_text = _strip_prefix(line, "被动能力：").strip() or _strip_prefix(line, "被动能力:").strip()
                if pk_text and pk_text != "无":
                    for pk in pk_text.split("；"):
                        pk = pk.strip()
                        if pk:
                            passive_skills.append({"name": pk, "desc": pk, "mastery": "常驻", "lv": 0})
        charm = _random_grade("A+")
        luck = _random_grade("A+")
        char_data = {
            "name": name, "description": desc,
            "abilities": [], "inventory": [], "world": world_name, "social_status": "",
            "attr": {"str": str_v, "agi": agi_v, "spr": spr_v},
            "charm": charm, "luck": luck,
            "hp": _calc_hp(str_v), "mp": _calc_mp(spr_v),
            "power": _calc_power(str_v, agi_v, spr_v, charm, luck),
            "has_adventured": False, "is_random": True,
            "race": race_info, "job": job_info,
            "skills": {"active": active_skills[:MAX_ACTIVE_SKILLS], "passive": passive_skills},
            "created_at": time.time(), "updated_at": time.time(),
        }
        # LLM 生成的种族/职业名命中图鉴时直接套用内置模板
        self._apply_archetype_templates(char_data)
        self._characters[uid] = normalize_character(char_data)
        _save_json(self._characters_path, self._characters)
        self._notify("characters_changed", {"uid": uid})
        sheet = _render_rpg_sheet(self._characters[uid])
        fallback_note = "" if attr_parsed else "\n⚠️ 属性解析失败，已使用随机值兜底。"
        yield event.plain_result(f"🎲 **随机角色已生成！**\n\n{sheet}{fallback_note}\n\n💡 使用「冒险 保存随机角色 <名字>」确认名称即可开始冒险♪\n⚠️ 随机角色不可修改属性值，只能更改名字。")

    @adventure.command("保存随机角色")
    async def save_random_character(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        char = self._characters.get(uid)
        if not char:
            yield event.plain_result("还没有角色卡。请先使用「冒险 随机角色」生成。")
            return
        if not char.get("is_random"):
            yield event.plain_result("你的角色卡不是随机生成的。")
            return
        rest = _strip_prefix(event.get_message_str().strip(), "冒险 保存随机角色")
        if not rest:
            yield event.plain_result("格式：冒险 保存随机角色 <新名字>")
            return
        new_name = rest.split(" ")[0].strip()
        old_name = char.get("name")
        char["name"] = new_name
        char["updated_at"] = time.time()
        _save_json(self._characters_path, self._characters)
        self._notify("characters_changed", {"uid": uid})
        yield event.plain_result(f"✅ 随机角色已保存！\n名字：{old_name} → **{new_name}**\n\n使用「冒险 开始」翻开第一页吧♪")

    # ════════════════════════════════════════════════
    #  世界观投稿
    # ════════════════════════════════════════════════

    @adventure.command("投稿世界观")
    async def submit_world(self, event: AstrMessageEvent):
        rest = _strip_prefix(event.get_message_str().strip(), "冒险 投稿世界观")
        parts = rest.split(" ", 1)
        if len(parts) < 2:
            yield event.plain_result("格式：冒险 投稿世界观 <名称> <设定>")
            return
        name, content = parts[0].strip(), parts[1].strip()
        if name in self._worlds:
            yield event.plain_result(f"世界观「{name}」已存在。")
            return
        self._worlds[name] = normalize_world({
            "author_id": event.get_sender_id(),
            "content": content,
            "eggs": [],
            "created_at": time.time(),
        }, name=name)
        _save_json(self._worlds_path, self._worlds)
        self._notify("worlds_changed", {"name": name, "source": "chat_command"})
        await self._kb_store("world", f"世界观「{name}」：{content}", {"world_name": name})
        yield event.plain_result(f"🌍 世界观「{name}」已投稿！")

    @adventure.command("世界观列表")
    async def list_worlds(self, event: AstrMessageEvent):
        if not self._worlds:
            yield event.plain_result("还没有世界观。使用「冒险 投稿世界观」创建吧。")
            return
        lines = ["🌍 **世界观列表**"]
        for name in self._worlds:
            egg_count = len(get_world_eggs(self._worlds[name]))
            egg_mark = f" ✨{egg_count}彩蛋" if egg_count else ""
            lines.append(f"  · **{name}**{egg_mark}")
        yield event.plain_result("\n".join(lines))

    @adventure.command("世界观")
    async def view_world(self, event: AstrMessageEvent):
        rest = _strip_prefix(event.get_message_str().strip(), "冒险 世界观")
        if not rest:
            yield event.plain_result("格式：冒险 世界观 <名称>")
            return
        name = rest.split(" ")[0].strip()
        if name not in self._worlds:
            yield event.plain_result(f"未找到世界观「{name}」。")
            return
        data = self._worlds[name]
        content = data.get("content", "")[:1000]
        eggs = get_world_eggs(data)
        egg_text = f"\n\n✨ 彩蛋（{len(eggs)}个）：\n" + "\n".join(f"  · {e}" for e in eggs) if eggs else ""
        yield event.plain_result(f"🌍 **{name}**\n\n{content}{egg_text}")

    # ════════════════════════════════════════════════
    #  冒险
    # ════════════════════════════════════════════════

    @adventure.command("开始")
    async def start_adventure(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        rest = _strip_prefix(event.get_message_str().strip(), "冒险 开始")
        if rest.startswith("群体"):
            async for r in self._start_group_flow(event, uid, rest):
                yield r
        else:
            async for r in self._start_single_flow(event, uid, rest):
                yield r

    async def _start_single_flow(self, event: AstrMessageEvent, uid: str, rest: str = "", saved_state: dict | None = None):
        if not saved_state:
            if uid in self._active_single:
                yield event.plain_result("你已有一场冒险了。")
                return
            world_name, theme = self._parse_adventure_scope(rest)
            if world_name and world_name not in self._worlds:
                yield event.plain_result(f"未找到世界观「{world_name}」。")
                return
            char = self._characters.get(uid, {})
            world = self._worlds.get(world_name, {})
            compiled_context = build_adventure_context(
                world,
                char,
                [theme, "故事开始了，请描述第一个场景并给出选项。"],
                "single",
                self.lore_content_budget,
            )
            world_context = compiled_context["world_context"]
            character_context = compiled_context["character_context"]
            if char and char.get("world") and char["world"] == world_name:
                character_context += f"\n该角色在此世界中的身份：{char.get('social_status', '无特殊身份')}"
            special_notes = ""
            if world_name and char.get("world") == world_name and char.get("social_status"):
                special_notes = f"【特殊】角色「{char['name']}」在「{world_name}」世界中拥有社会身份：{char['social_status']}。"
            if world_name:
                for egg in get_world_eggs(self._worlds[world_name]):
                    if char and char.get("social_status") and any(kw and kw in egg for kw in [char.get("name", ""), char.get("social_status", "")]):
                        special_notes += f"\n【彩蛋触发】{egg}。"
            player_stats = _build_player_stats(char) if char else ""
            world_tier_context = _build_world_tier_context(self._worlds.get(world_name, {}), world_name) if world_name else ""
            sys_prompt = SINGLE_SYSTEM_PROMPT.format(theme=theme, world_context=world_context, character_context=character_context, player_stats=player_stats, world_tier_context=world_tier_context, special_notes=special_notes)
            game_state = {
                "theme": theme, "world": world_name,
                "history": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": "故事开始了，请描述第一个场景并给出选项。"}],
                "created_at": time.time(),
                "summary": "",
                # 开局已命中的世界书条目记账：回合中只注入“新命中”的条目
                "lore_ids": [e["id"] for e in compiled_context["matched_entries"]],
            }
            if self.enable_dice_judgment and char:
                game_state["history"].append({"role": "system", "content": build_judgment_context(char)})
            provider = self.context.get_using_provider()
            if not provider:
                yield event.plain_result("暂无可用 LLM 服务。")
                return
            opening_msg = f"📖 **单人冒险 · {theme}**{' · ' + world_name if world_name else ''}\n正在编织你的故事..."
            if not char:
                opening_msg += "\n💡 你还没有角色卡，本局不会记录日志与结算。建议先「冒险 创建角色」。"
            yield event.plain_result(opening_msg)
            try:
                resp: LLMResponse = await provider.text_chat(prompt="", session_id=event.get_session_id(), contexts=game_state["history"])
                story = resp.completion_text or "世界在你眼前展开，等待你的第一步。\n\nA. 向前探索  B. 环顾四周  C. 驻足聆听"
                game_state["history"].append({"role": "assistant", "content": story})
                game_state["summary"] = story[:120]
                _compact_adventure_history(game_state)
                yield event.plain_result(f"📖 **{theme}**\n\n{self._render_story(story)}\n\n**[输入你的行动，或从选项中选择]**")
            except Exception as e:
                logger.error(f"📖 如我所书 | 单人·开场: {str(e)}")
                yield event.plain_result("故事暂时无法展开。")
                return
            self._active_single[uid] = None
        else:
            game_state = dict(saved_state)
            theme = game_state["theme"]
            world_name = game_state.get("world", "")
            if game_state.get("lore_ids") is None:
                # 旧存档没有 lore_ids 记账：按开局入参重跑一次匹配补种，
                # 避免恢复后把常驻/开局命中的条目当“新命中”重复注入。
                try:
                    seed_context = build_adventure_context(
                        self._worlds.get(world_name, {}),
                        self._characters.get(uid, {}),
                        [theme, "故事开始了，请描述第一个场景并给出选项。"],
                        "single",
                        self.lore_content_budget,
                    )
                    game_state["lore_ids"] = [e["id"] for e in seed_context["matched_entries"]]
                except Exception:
                    game_state["lore_ids"] = []
            provider = self.context.get_using_provider()
            if not provider:
                yield event.plain_result("暂无可用 LLM 服务。")
                return
            yield event.plain_result(f"📖 **冒险继续 · {theme}**\n故事之门再次为你敞开。\n\n*(从上次存档处继续)*")
            story = ""
            for msg in reversed(game_state["history"]):
                if msg["role"] == "assistant":
                    story = msg["content"]
                    break
            self._active_single[uid] = None
            self._adventures[uid] = game_state
            if story:
                yield event.plain_result(f"📖 **{theme}**\n\n{self._render_story(story)}\n\n**[输入你的行动，或从选项中选择]**")
        self._adventures[uid] = game_state
        @session_waiter(timeout=self.single_timeout, record_history_chains=False)
        async def waiter(ctrl: SessionController, ev: AstrMessageEvent):
            e_uid = ev.get_sender_id()
            if e_uid not in self._active_single:
                ctrl.stop()
                return
            self._active_single[e_uid] = ctrl
            action = ev.message_str.strip()
            if not action:
                await ev.send(MessageChain([Comp.Plain("你静静地站在原地。\n**[请输入你的行动]**")]))
                ctrl.keep(timeout=self.single_timeout, reset_timeout=True)
                return
            if action in ("退出", "结束") or action.startswith("冒险 退出") or action.startswith("/冒险 退出"):
                self._active_single.pop(e_uid, None)
                self._adventures.pop(e_uid, None)
                settle_msg = await self._settle_adventure(e_uid, game_state, "exit")
                end_msg = "📖 冒险已结束。期待再次翻开这本书。"
                if settle_msg:
                    end_msg += f"\n⭐ {settle_msg}"
                await ev.send(MessageChain([Comp.Plain(end_msg)]))
                ctrl.stop()
                return
            if action in ("暂停", "存档") or action == "冒险 暂停":
                self._active_single.pop(e_uid, None)
                await self._settle_adventure(e_uid, game_state, "pause")
                self._save_adventure(e_uid, game_state)
                await ev.send(MessageChain([Comp.Plain(f"📖 冒险已暂停并保存。\n输入「冒险 继续」可随时回到这个故事♪")]))
                ctrl.stop()
                return
            await ev.send(MessageChain([Comp.Plain("故事正在展开...请稍候♪")]))
            # 判定骰与世界书命中块仅对本轮有效：先清掉上一轮的注入块
            game_state["history"] = self._strip_round_injections(game_state["history"])
            game_state["history"].append({"role": "user", "content": action})
            char_now = self._characters.get(e_uid)
            if self.enable_dice_judgment and char_now:
                game_state["history"].append({"role": "system", "content": build_judgment_context(char_now)})
            self._inject_round_lore(game_state, char_now, action)
            try:
                resp = await provider.text_chat(prompt="", session_id=ev.get_session_id(), contexts=game_state["history"])
                story = resp.completion_text or "前路延伸，等待你的下一个选择。"
                game_state["history"].append({"role": "assistant", "content": story})
                game_state["summary"] = story[:120]
                _compact_adventure_history(game_state)
            except Exception as e:
                logger.error(f"📖 如我所书 | 单人·推进: {str(e)}")
                await ev.send(MessageChain([Comp.Plain("故事遇到了些许波折，请稍后再试。")]))
                ctrl.keep(timeout=self.single_timeout, reset_timeout=True)
                return
            await ev.send(MessageChain([Comp.Plain(f"📖 **{theme}**\n\n{self._render_story(story)}\n\n**[输入你的行动，或从选项中选择]**")]))
            ctrl.keep(timeout=self.single_timeout, reset_timeout=True)
        await waiter(event)
        # 超时后的自动暂停：结算并保存，可随时「冒险 继续」接上，不丢进度
        if uid in self._active_single:
            self._active_single.pop(uid, None)
            await self._settle_adventure(uid, game_state, "timeout")
            self._save_adventure(uid, game_state)
            try:
                await event.send(MessageChain([Comp.Plain("⏸️ 你迟迟未行动，冒险已自动暂停并保存。\n输入「冒险 继续」随时回到这个故事♪")]))
            except Exception:
                pass
        self._adventures.pop(uid, None)

    def _save_adventure(self, uid: str, game_state: dict) -> str:
        """保存冒险状态到存档"""
        save_id = f"single_{uid}_{int(time.time())}"
        save_data = {
            "id": save_id,
            "type": "single",
            "uid": uid,
            "theme": game_state.get("theme", ""),
            "world": game_state.get("world", ""),
            "status": "paused",
            "created_at": game_state.get("created_at", time.time()),
            "updated_at": time.time(),
            "summary": game_state.get("summary", "")[:200],
            "history": game_state.get("history", []),
            # 结算提取水位线：恢复冒险后继续防重复提取
            "settled_len": game_state.get("settled_len", 0),
        }
        self._saves[save_id] = save_data
        _save_json(self._saves_path, self._saves)
        self._notify("saves_changed", {"id": save_id})
        return save_id

    @staticmethod
    def _strip_round_injections(history: list) -> list:
        """剥掉上一轮注入的判定骰与世界书命中块（两者均为“仅本轮有效”）。"""
        return [
            m for m in history
            if not str(m.get("content", "")).startswith(("【本轮判定骰", "【世界书·新命中】"))
        ]

    def _inject_round_lore(self, game_state: dict, char: dict | None, extra_text: str = "") -> None:
        """回合中世界书触发：按本轮文本重跑关键词匹配，新命中条目以 system 块注入。

        每轮先剥掉上一轮的注入块（与判定骰同为“仅本轮有效”），条目整个冒险只注入一次
        （lore_ids 记账）。新命中内容总量不超过 lore_content_budget 的一半。
        """
        try:
            world_name = str(game_state.get("world", "") or "")
            if not world_name:
                return
            world = self._worlds.get(world_name)
            if not isinstance(world, dict):
                return
            history = game_state.get("history", [])
            recent_texts: list[str] = []
            for msg in reversed(history):
                if len(recent_texts) >= 2:
                    break
                if isinstance(msg, dict) and msg.get("role") != "system":
                    text = str(msg.get("content", "") or "").strip()
                    if text:
                        recent_texts.append(text)
            recent_texts.reverse()
            extra = str(extra_text or "").strip()
            match_parts = ([extra] if extra else []) + recent_texts
            if not match_parts:
                return
            context = build_adventure_context(
                world,
                char or {},
                match_parts,
                "single",
                self.lore_content_budget,
            )
            seen = set(game_state.get("lore_ids", []) or [])
            fresh = [e for e in context.get("matched_entries", []) if e.get("id") not in seen]
            if not fresh:
                return
            # id → 原始条目映射（世界条目 + 角色个人设定集），用于取出完整 content
            entry_map: dict[str, dict] = {}
            for source in (world.get("entries", []),):
                if not isinstance(source, list):
                    continue
                for raw in source:
                    if isinstance(raw, dict) and raw.get("id"):
                        entry_map[str(raw["id"])] = raw
            book = (char or {}).get("character_book")
            book_entries = book.get("entries", []) if isinstance(book, dict) else []
            for raw in book_entries:
                if isinstance(raw, dict) and raw.get("id"):
                    entry_map.setdefault(str(raw["id"]), raw)
            budget = max(200, self.lore_content_budget // 2)
            remaining = budget
            blocks: list[str] = []
            new_ids: list[str] = []
            for item in fresh:
                if remaining <= 0:
                    break
                raw = entry_map.get(str(item.get("id", "")))
                if not isinstance(raw, dict):
                    continue
                content = str(raw.get("content", "") or "").strip()
                if not content:
                    continue
                accepted = content[:remaining]
                if not accepted:
                    continue
                blocks.append(f"【{item.get('name', '未命名条目')}】\n{accepted}")
                new_ids.append(str(item["id"]))
                remaining -= len(accepted)
            if not blocks:
                return
            game_state.setdefault("history", []).append({
                "role": "system",
                "content": "【世界书·新命中】\n" + "\n\n".join(blocks),
            })
            game_state["lore_ids"] = sorted(seen | set(new_ids))
        except Exception:  # noqa: BLE001 - 回合中世界书注入绝不打断冒险流程
            pass

    async def _settle_adventure(self, uid: str, game_state: dict, exit_type: str = "exit") -> str:
        """结算冒险：写入日志、标记首次冒险完成，并按配置做结算提取。

        提取带水位线（settled_len）：只对上次结算后新增的回合做提取；
        解析失败不推进水位线，下次结算自动重试；结局卡仅在主动退出时允许写入。
        """
        char = self._characters.get(uid)
        if not char:
            return ""
        theme = game_state.get("theme", "")
        world = game_state.get("world", "")
        history = game_state.get("history", [])
        # 提取最后一轮 assistant 回复作为结局摘要
        ending_summary = ""
        for msg in reversed(history):
            if msg.get("role") == "assistant":
                ending_summary = msg.get("content", "")[:200]
                break
        # 写入冒险日志
        log_id = _generate_id()
        log_entry = {
            "id": log_id,
            "uid": uid,
            "character_name": char.get("name", ""),
            "theme": theme,
            "world": world,
            "exit_type": exit_type,
            "ended_at": time.time(),
            "summary": ending_summary,
            "history_length": len(history),
        }
        self._logs[log_id] = log_entry
        _save_json(self._logs_path, self._logs)
        # 标记首次冒险完成
        if not char.get("has_adventured"):
            char["has_adventured"] = True
        char["updated_at"] = time.time()
        _save_json(self._characters_path, self._characters)
        self._notify("characters_changed", {"uid": uid})
        # 结算提取（水位线防重复）：属性成长 / 新能力 / 结局卡 / 世界书沉淀（失败静默跳过）
        notes: list[str] = []
        ending: dict | None = None
        watermark = int(game_state.get("settled_len", 0) or 0)
        if watermark > len(history):
            # 历史被压缩变短：水位线对齐到当前长度，避免旧索引卡死后续提取
            watermark = len(history)
            game_state["settled_len"] = len(history)
        new_slice = history[watermark:]
        if self.enable_settle_extract and len(new_slice) >= 4:
            changes, ending = await self._extract_settlement(
                uid,
                char,
                new_slice,
                world,
                str(game_state.get("summary", "")),
                allow_ending=(exit_type == "exit"),
            )
            if changes is not None:
                # 解析成功（哪怕本次无变化）才推进水位线；失败留给下次重试
                game_state["settled_len"] = len(history)
            notes = list(changes or [])
        if ending:
            self._endings[f"{uid}_{log_id}"] = {
                "uid": uid,
                "log_id": log_id,
                "character_name": char.get("name", ""),
                "theme": theme,
                "world": world,
                "ended_at": time.time(),
                **ending,
            }
            _save_json(self._endings_path, self._endings)
        base = "结算：冒险经历已记录，能力成长将以叙事结果为准。" if exit_type == "exit" else ""
        if notes:
            base = (base + "\n" if base else "") + "📜 " + "；".join(notes)
        return base

    async def _extract_settlement(
        self,
        uid: str,
        char: dict,
        history_msgs: list,
        world_name: str,
        summary: str,
        allow_ending: bool,
    ) -> tuple[list[str] | None, dict | None]:
        """结算提取：一次 LLM 调用从冒险经历中产出成长与结局信息。

        history_msgs 只传上次结算后新增的消息（水位线由调用方维护）。
        返回 (changes, ending)；LLM 调用或解析失败时返回 (None, None)，
        调用方据此不推进水位线。allow_ending=False 时丢弃结局卡（暂停/超时只提取成长）。
        """
        provider = self.context.get_using_provider()
        if not provider:
            return None, None
        recent = [
            m for m in (history_msgs or [])
            if isinstance(m, dict) and m.get("role") != "system"
        ][-20:]
        experience = "\n".join(
            f"{m.get('role', '?')}: {str(m.get('content', ''))[:300]}" for m in recent
        )[:6000]
        world_name = str(world_name or "")
        world = self._worlds.get(world_name) if world_name else None
        entry_names = [str(e.get("name", "")) for e in (world or {}).get("entries", [])][:50]
        prompt = (
            SETTLE_EXTRACT_PROMPT
            + "\n\n【角色面板】\n" + _render_rpg_sheet(char)
            + "\n\n【冒险经历】\n" + (experience or "（本轮几乎没有推进）")
            + "\n\n【结局走向】\n" + (str(summary or "") or "（未明确）")
            + "\n\n【世界信息】\n" + (
                f"世界「{world_name}」已有条目：" + "、".join(n for n in entry_names if n)
                if world_name else "（无特定世界）"
            )
        )
        try:
            resp = await provider.text_chat(prompt=prompt, session_id=f"settle_{uid}", contexts=[])
            result = parse_settle_payload(resp.completion_text or "")
        except Exception as e:
            logger.error(f"📖 如我所书 | 结算提取: {str(e)}")
            return None, None
        if not result:
            return None, None
        entries_before = len((world or {}).get("entries", [])) if world else 0
        try:
            changes, ending = apply_settle_result(char, world, result)
        except Exception as e:
            logger.error(f"📖 如我所书 | 结算提取·应用: {str(e)}")
            return None, None
        if not allow_ending and ending is not None:
            # 暂停/超时不写结局卡：丢弃 ending，并剔除对应的“结局已记录”提示
            ending = None
            changes = [c for c in changes if not str(c).startswith("📖 结局已记录")]
        # 图鉴富化：种族/职业命中内置模板且补正全零时套用
        changes.extend(self._apply_archetype_templates(char))
        if changes:
            _save_json(self._characters_path, self._characters)
            self._notify("characters_changed", {"uid": uid, "source": "settle"})
        if world is not None and len(world.get("entries", [])) != entries_before:
            _save_json(self._worlds_path, self._worlds)
            self._notify("worlds_changed", {"name": world_name, "source": "settle"})
        return changes, ending

    async def _settle_group(self, session: dict) -> list[str]:
        """群体冒险收官：写一条群体日志、全员标记首次冒险、逐人结算提取。"""
        participants = session.get("participants", {})
        member_uids = [uid for uid, p in participants.items() if isinstance(p, dict)]
        members: list[tuple[str, dict]] = []
        character_names: list[str] = []
        for uid in member_uids:
            char = self._characters.get(uid)
            if isinstance(char, dict):
                members.append((uid, char))
                character_names.append(str(char.get("name") or f"玩家{uid[-4:]}"))
        log_id = _generate_id()
        log_entry = {
            "id": log_id,
            "type": "group",
            "uid": str(session.get("owner_id", "")),
            "character_name": "、".join(character_names),
            "participants": member_uids,
            "character_names": character_names,
            "theme": session.get("theme", ""),
            "world": session.get("world", ""),
            "exit_type": "exit",
            "ended_at": time.time(),
            "summary": str(session.get("summary", ""))[:200],
            "history_length": len(session.get("history", [])),
        }
        self._logs[log_id] = log_entry
        _save_json(self._logs_path, self._logs)
        for _uid, char in members:
            if not char.get("has_adventured"):
                char["has_adventured"] = True
            char["updated_at"] = time.time()
        _save_json(self._characters_path, self._characters)
        self._notify("characters_changed", {"uid": "group"})
        summary_lines: list[str] = []
        if self.enable_settle_extract:
            history = session.get("history", [])
            world_name = str(session.get("world", "") or "")
            summary = str(session.get("summary", "") or "")
            for uid, char in members:
                name = str(char.get("name") or f"玩家{uid[-4:]}")
                try:
                    changes, ending = await self._extract_settlement(
                        uid, char, history, world_name, summary, allow_ending=True
                    )
                except Exception as e:
                    logger.error(f"📖 如我所书 | 群体·结算 {name}: {str(e)}")
                    changes, ending = [], None
                if changes is None:
                    changes = []
                if ending:
                    self._endings[f"{uid}_{log_id}"] = {
                        "uid": uid,
                        "log_id": log_id,
                        "character_name": name,
                        "theme": session.get("theme", ""),
                        "world": world_name,
                        "ended_at": time.time(),
                        **ending,
                    }
                    _save_json(self._endings_path, self._endings)
                summary_lines.append(f"{name}：{'；'.join(changes)}" if changes else f"{name}：无变化")
        return summary_lines

    def _find_user_saves(self, uid: str) -> list[dict]:
        """查找用户的所有存档"""
        result = []
        for sid, s in self._saves.items():
            if s.get("uid") == uid and s.get("status") == "paused":
                result.append(s)
        result.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
        return result

    def _group_key(self, event: AstrMessageEvent) -> str:
        get_group_id = getattr(event, "get_group_id", None)
        group_id = str(get_group_id() or "").strip() if callable(get_group_id) else ""
        return group_id or str(event.get_session_id() or "").strip()

    def _sender_name(self, event: AstrMessageEvent, uid: str) -> str:
        get_sender_name = getattr(event, "get_sender_name", None)
        if callable(get_sender_name):
            name = str(get_sender_name() or "").strip()
            if name:
                return name
        char = self._characters.get(uid, {})
        return char.get("name") or f"玩家{uid[-4:]}"

    def _parse_adventure_scope(self, text: str) -> tuple[str, str]:
        world_name = ""
        theme_text = text.strip()
        if theme_text.startswith("世界"):
            raw = theme_text[2:].lstrip("：:")
            for sep in ["：", ":", " "]:
                if sep in raw:
                    w, _, t = raw.partition(sep)
                    world_name = w.strip()
                    theme_text = t.strip()
                    break
            else:
                parts = raw.strip().split(" ", 1)
                world_name = parts[0].strip()
                theme_text = parts[1].strip() if len(parts) > 1 else ""
        return world_name, theme_text or self.default_theme

    def _group_roster_text(self, session: dict) -> str:
        lines = []
        for p in session.get("participants", {}).values():
            if p.get("status") != "active":
                continue
            char = p.get("character", {})
            desc = char.get("description", "")[:80]
            active_skills, passive_skills = _get_character_skills(char)
            skills = _skill_names(active_skills + passive_skills, 4)
            line = f"- {char.get('name') or p.get('nickname')}：{desc}"
            if skills:
                line += f"；能力：{skills}"
            lines.append(line)
        return "\n".join(lines) or "- 暂无在场玩家"

    def _group_status_text(self, session: dict) -> str:
        active = [p for p in session.get("participants", {}).values() if p.get("status") == "active"]
        pending = [p for p in session.get("participants", {}).values() if p.get("status") == "pending_join"]
        round_state = session.get("round", {})
        votes = round_state.get("votes", {})
        deadline = round_state.get("deadline_at", 0)
        remain = max(0, int(deadline - time.time())) if deadline else 0
        lines = [
            f"📖 **群体冒险 · {session.get('theme', '未知')}**",
            f"状态：{session.get('status', 'unknown')} | 第 {session.get('round_id', 0)} 轮",
            f"在场：{len(active)}/{self.group_max_players} | 已投票：{len(votes)}/{len(active)} | 剩余：{remain}秒",
        ]
        if active:
            lines.append("玩家：" + "、".join(p.get("character", {}).get("name") or p.get("nickname", "玩家") for p in active))
        if pending:
            lines.append("下轮登场：" + "、".join(p.get("character", {}).get("name") or p.get("nickname", "玩家") for p in pending))
        if votes:
            lines.append("投票：" + "、".join(f"{session['participants'].get(uid, {}).get('nickname', uid[-4:])}->{vote}" for uid, vote in votes.items()))
        return "\n".join(lines)

    async def _generate_group_clues(self, session: dict, scene: str) -> list[str]:
        provider = self.context.get_using_provider()
        if not provider or self.group_max_clues_per_round <= 0:
            return []
        clues = []
        active = [p for p in session.get("participants", {}).values() if p.get("status") == "active"]
        for p in active[: self.group_max_clues_per_round]:
            char = p.get("character", {})
            active_skills, passive_skills = _get_character_skills(char)
            skills = _skill_names(active_skills + passive_skills, 4)
            if not skills and not char.get("race") and not char.get("job"):
                continue
            prompt = (
                "你是文字冒险的助理GM。请根据当前场景和角色能力，生成一句不超过60字的即时线索。"
                "线索只能提供局部感知或氛围提示，不要直接给出最优答案，不要推进主线。\n\n"
                f"场景：{scene[:600]}\n"
                f"角色：{char.get('name') or p.get('nickname')}，{char.get('description', '')[:120]}\n"
                f"种族/职业：{(char.get('race') or {}).get('name', '无')}/{(char.get('job') or {}).get('name', '无')}\n"
                f"能力：{skills or '无'}"
            )
            try:
                resp = await provider.text_chat(prompt=prompt, session_id=f"group_clue_{session.get('session_id')}", contexts=[])
                clue = (resp.completion_text or "").strip().replace("\n", " ")[:90]
                if clue:
                    clues.append(f"🔎 {char.get('name') or p.get('nickname')}发现：{clue}")
            except Exception as e:
                logger.error(f"📖 如我所书 | 群体·线索: {str(e)}")
        return clues

    async def _open_group_round(self, event: AstrMessageEvent, session: dict, action: str = "故事开始了，请描述第一个场景并给出选项。") -> str:
        provider = self.context.get_using_provider()
        if not provider:
            return "暂无可用 LLM 服务。"
        for p in session.get("participants", {}).values():
            if p.get("status") == "pending_join":
                p["status"] = "active"
                p["joined_round"] = session.get("round_id", 0) + 1
        session["round_id"] = int(session.get("round_id", 0)) + 1
        session["status"] = "round_open"
        # 判定骰与世界书命中块仅对本轮有效：先清掉上一轮的注入块
        session["history"] = self._strip_round_injections(session["history"])
        session["history"].append({"role": "user", "content": action})
        if self.enable_dice_judgment:
            active_chars = [
                p.get("character", {})
                for p in session.get("participants", {}).values()
                if p.get("status") == "active" and isinstance(p.get("character"), dict)
            ]
            if active_chars:
                session["history"].append({"role": "system", "content": build_group_judgment_context(active_chars)})
        # 群体回合的世界书触发：只匹配世界条目（不带任何角色的个人设定集）
        self._inject_round_lore(session, None, action)
        try:
            resp = await provider.text_chat(prompt="", session_id=event.get_session_id(), contexts=session["history"])
            story = resp.completion_text or "雾中的道路向前延伸。\n\nA. 谨慎探索  B. 原地观察  C. 结伴前进"
        except Exception as e:
            logger.error(f"📖 如我所书 | 群体·推进: {str(e)}")
            return "群体冒险暂时无法展开。"
        session["history"].append({"role": "assistant", "content": story})
        session["summary"] = story[:160]
        _compact_adventure_history(session, keep_messages=18)
        clues = await self._generate_group_clues(session, story)
        deadline = time.time() + max(10, self.group_round_time)
        session["round"] = {
            "round_id": session["round_id"],
            "scene": story,
            "votes": {},
            "clues": clues,
            "deadline_at": deadline,
            "status": "voting",
            "event": event,
        }
        key = session["session_key"]
        old_task = self._group_tasks.pop(key, None)
        if old_task and not old_task.done():
            old_task.cancel()
        self._group_tasks[key] = asyncio.create_task(self._group_round_timer(key, session["round_id"]))
        display_story = self._render_story(story)
        display_clues = [self._render_story(clue) for clue in clues]
        clue_text = "\n" + "\n".join(display_clues) if display_clues else ""
        return (
            f"📖 **群体冒险 · 第 {session['round_id']} 轮**\n\n{display_story}{clue_text}\n\n"
            f"请在 {self.group_round_time} 秒内投票：冒险 投票 A/B/C 或自定义行动。"
        )

    async def _group_round_timer(self, key: str, round_id: int) -> None:
        session = self._group_sessions.get(key)
        if not session:
            return
        round_state = session.get("round", {})
        delay = max(0, round_state.get("deadline_at", time.time()) - time.time())
        await asyncio.sleep(delay)
        await self._resolve_group_round(key, round_id, reason="timeout")

    async def _resolve_group_round(self, key: str, round_id: int, reason: str = "manual") -> None:
        lock = self._group_locks.setdefault(key, asyncio.Lock())
        async with lock:
            session = self._group_sessions.get(key)
            if not session or session.get("round_id") != round_id:
                return
            round_state = session.get("round", {})
            if round_state.get("status") != "voting":
                return
            round_state["status"] = "resolving"
            active = [uid for uid, p in session.get("participants", {}).items() if p.get("status") == "active"]
            votes = {uid: vote for uid, vote in round_state.get("votes", {}).items() if uid in active}
            if votes:
                counts: dict[str, int] = {}
                for vote in votes.values():
                    counts[vote] = counts.get(vote, 0) + 1
                top_count = max(counts.values())
                winners = [v for v, c in counts.items() if c == top_count]
                leader_vote = votes.get(session.get("owner_id"))
                selected = leader_vote if leader_vote in winners else sorted(winners)[0]
            else:
                selected = "原地观察并整理线索"
            event = round_state.get("event")
            if not event:
                round_state["status"] = "resolved"
                return
            vote_summary = "、".join(f"{session['participants'].get(uid, {}).get('nickname', uid[-4:])}->{vote}" for uid, vote in votes.items()) or "无人投票"
            await event.send(MessageChain([Comp.Plain(f"🗳️ 投票结束：{vote_summary}\n本轮集体行动：{selected}\n故事正在继续...")]))
            action = f"本轮玩家投票结果为：{selected}。投票明细：{vote_summary}。请据此推进群体冒险，并给出下一轮2~3个行动选项。"
        msg = await self._open_group_round(event, session, action=action)
        await event.send(MessageChain([Comp.Plain(msg)]))

    async def _maybe_resolve_group_if_all_voted(self, key: str) -> None:
        session = self._group_sessions.get(key)
        if not session:
            return
        round_state = session.get("round", {})
        if round_state.get("status") != "voting":
            return
        active_count = sum(1 for p in session.get("participants", {}).values() if p.get("status") == "active")
        if active_count > 0 and len(round_state.get("votes", {})) >= active_count:
            await self._resolve_group_round(key, session.get("round_id", 0), reason="all_voted")

    async def _start_group_flow(self, event: AstrMessageEvent, uid: str, rest: str):
        key = self._group_key(event)
        if not key:
            yield event.plain_result("群体冒险需要在群聊中开启。")
            return
        if key in self._group_sessions:
            yield event.plain_result("这个群已经有一场群体冒险在进行了。使用「冒险 加入」加入吧。")
            return
        raw = _strip_prefix(rest, "群体")
        world_name, theme = self._parse_adventure_scope(raw)
        if world_name and world_name not in self._worlds:
            yield event.plain_result(f"未找到世界观「{world_name}」。")
            return
        char = self._characters.get(uid, {})
        if not char:
            yield event.plain_result("你还没有角色卡。先用「冒险 创建角色」或「冒险 随机角色」创建一张吧。")
            return
        world = self._worlds.get(world_name, {})
        compiled_context = build_adventure_context(
            world,
            char,
            [theme, raw, "故事开始了，请描述第一个场景并给出选项。"],
            "group",
            self.lore_content_budget,
        )
        world_context = "\n\n".join(filter(None, (
            compiled_context["world_context"],
            compiled_context["character_context"],
        )))
        world_tier_context = _build_world_tier_context(world, world_name) if world_name else ""
        participant = {
            "user_id": uid,
            "nickname": self._sender_name(event, uid),
            "character": char,
            "status": "active",
            "joined_round": 0,
            "left_round": 0,
        }
        session = {
            "session_id": f"group_{key}_{int(time.time())}",
            "session_key": key,
            "group_id": key,
            "owner_id": uid,
            "theme": theme,
            "world": world_name,
            "status": "created",
            "participants": {uid: participant},
            "retired_user_ids": [],
            "round_id": 0,
            "summary": "",
            # 开局已命中的世界书条目记账：回合中只注入“新命中”的条目
            "lore_ids": [e["id"] for e in compiled_context["matched_entries"]],
            "created_at": time.time(),
        }
        sys_prompt = GROUP_SYSTEM_PROMPT.format(
            theme=theme,
            world_context=world_context,
            roster=self._group_roster_text(session),
            world_tier_context=world_tier_context,
            special_notes="玩家通过固定时间投票决定每轮集体行动；中途加入者应在下一轮以合理方式登场，退场者不要再影响主线。",
        )
        session["history"] = [{"role": "system", "content": sys_prompt}]
        self._group_sessions[key] = session
        self._group_locks[key] = asyncio.Lock()
        intro = await self._open_group_round(event, session)
        yield event.plain_result(
            f"📖 群体冒险已开启：{theme}\n"
            f"发起者「{char.get('name', participant['nickname'])}」已入场。其他玩家可发送「冒险 加入」，会从下一轮合理登场。\n\n{intro}"
        )

    @adventure.command("退出")
    async def exit_adventure(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        key = self._group_key(event)
        session = self._group_sessions.get(key)
        if session and uid in session.get("participants", {}):
            participant = session["participants"][uid]
            if participant.get("status") == "retired":
                yield event.plain_result("你已经从这场群体冒险中退场了。")
                return
            participant["status"] = "retired"
            participant["left_round"] = session.get("round_id", 0)
            if uid not in session.get("retired_user_ids", []):
                session.setdefault("retired_user_ids", []).append(uid)
            round_state = session.get("round", {})
            round_state.get("votes", {}).pop(uid, None)
            name = participant.get("character", {}).get("name") or participant.get("nickname", "某位旅人")
            active_count = sum(1 for p in session.get("participants", {}).values() if p.get("status") == "active")
            if active_count <= 0:
                task = self._group_tasks.pop(key, None)
                if task and not task.done():
                    task.cancel()
                # 收官结算要在 session 还在手里的时候做
                summary_lines: list[str] = []
                try:
                    summary_lines = await self._settle_group(session)
                except Exception as e:
                    logger.error(f"📖 如我所书 | 群体·结算: {str(e)}")
                self._group_sessions.pop(key, None)
                self._group_locks.pop(key, None)
                end_msg = f"🚪 {name}离开了队伍。已无在场玩家，群体冒险结束。"
                if summary_lines:
                    end_msg += "\n📚 结算：\n  " + "\n  ".join(summary_lines)
                yield event.plain_result(end_msg)
                return
            await self._maybe_resolve_group_if_all_voted(key)
            yield event.plain_result(f"🚪 {name}暂时离队，守住了故事的另一条退路。\n本场冒险中，退场后不能再次加入。")
            return

        ctrl = self._active_single.pop(uid, None)
        game_state = self._adventures.pop(uid, None)
        if ctrl is not None:
            try:
                ctrl.stop()
            except Exception:
                pass
        settle_msg = await self._settle_adventure(uid, game_state, "exit") if game_state else ""
        msg = "📖 已退出当前冒险。\n故事的大门永远为你敞开♪"
        if settle_msg:
            msg += f"\n⭐ {settle_msg}"
        yield event.plain_result(msg)

    @adventure.command("存档")
    async def list_saves(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        saves = self._find_user_saves(uid)
        if not saves:
            yield event.plain_result("📖 **存档列表**\n\n目前还没有存档呢～\n冒险时输入「暂停」即可保存进度♪")
            return
        lines = ["📖 **你的存档**", ""]
        for i, s in enumerate(saves, 1):
            theme = s.get("theme", "未知")
            summary = (s.get("summary", "") or "")[:60]
            t = time.strftime("%m-%d %H:%M", time.localtime(s.get("updated_at", 0)))
            lines.append(f"  {i}. {theme}")
            lines.append(f"     {summary}")
            lines.append(f"     🕐 {t}")
        lines.append("")
        lines.append("💡 使用「冒险 继续 <序号>」继续冒险")
        lines.append("💡 使用「冒险 放弃 <序号>」删除存档")
        yield event.plain_result("\n".join(lines))

    @adventure.command("继续")
    async def resume_adventure(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        if uid in self._active_single:
            yield event.plain_result("你已在进行一场冒险。先输入「退出」或「暂停」吧。")
            return
        saves = self._find_user_saves(uid)
        if not saves:
            yield event.plain_result("📖 没有找到可继续的存档。用「冒险 开始」开启新冒险吧♪")
            return
        rest = _strip_prefix(event.get_message_str().strip(), "冒险 继续")
        idx = 0
        if rest.strip().isdigit():
            idx = int(rest.strip()) - 1
        if idx < 0 or idx >= len(saves):
            idx = 0
        save = saves[idx]
        save_id = save.get("id", "")
        # 先确认恢复流程真正启动，再消费旧存档，避免 provider 不可用等失败导致丢档。
        save_consumed = False
        async for r in self._start_single_flow(event, uid, "", saved_state=save):
            yield r
            if not save_consumed and save_id and uid in self._active_single:
                self._saves.pop(save_id, None)
                _save_json(self._saves_path, self._saves)
                self._notify("saves_changed", {"id": save_id})
                save_consumed = True

    @adventure.command("放弃")
    async def delete_save(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        rest = _strip_prefix(event.get_message_str().strip(), "冒险 放弃")
        saves = self._find_user_saves(uid)
        if not saves:
            yield event.plain_result("📖 没有可放弃的存档。")
            return
        if not rest.strip().isdigit():
            yield event.plain_result("请指定要放弃的存档序号。\n使用「冒险 存档」查看序号。")
            return
        idx = int(rest.strip()) - 1
        if idx < 0 or idx >= len(saves):
            yield event.plain_result("序号无效。使用「冒险 存档」查看你的存档。")
            return
        save = saves[idx]
        del self._saves[save.get("id", "")]
        _save_json(self._saves_path, self._saves)
        self._notify("saves_changed", {"id": save.get("id", "")})
        yield event.plain_result(f"🗑️ 已放弃存档「{save.get('theme', '未知')}」。")

    @adventure.command("加入")
    async def join_group(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        key = self._group_key(event)
        session = self._group_sessions.get(key)
        if not session:
            yield event.plain_result("当前群里还没有群体冒险。使用「冒险 开始 群体 [主题]」开启吧。")
            return
        if uid in session.get("retired_user_ids", []):
            yield event.plain_result("你已经从这场冒险中退场，不能再次加入同一场冒险。")
            return
        existing = session.get("participants", {}).get(uid)
        if existing and existing.get("status") in ("active", "pending_join"):
            yield event.plain_result("你已经在这场群体冒险中了。")
            return
        active_or_pending = [p for p in session.get("participants", {}).values() if p.get("status") in ("active", "pending_join")]
        if len(active_or_pending) >= self.group_max_players:
            yield event.plain_result(f"队伍已满（最多 {self.group_max_players} 人）。")
            return
        char = self._characters.get(uid, {})
        if not char:
            yield event.plain_result("你还没有角色卡。先用「冒险 创建角色」或「冒险 随机角色」创建一张吧。")
            return
        status = "pending_join" if session.get("round_id", 0) > 0 else "active"
        session.setdefault("participants", {})[uid] = {
            "user_id": uid,
            "nickname": self._sender_name(event, uid),
            "character": char,
            "status": status,
            "joined_round": session.get("round_id", 0) + (1 if status == "pending_join" else 0),
            "left_round": 0,
        }
        name = char.get("name") or self._sender_name(event, uid)
        if status == "pending_join":
            yield event.plain_result(f"✨ {name}已抵达故事边缘，将在下一轮以合理方式登场。")
        else:
            yield event.plain_result(f"✨ {name}加入了队伍。")

    @adventure.command("日志")
    async def view_logs(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        user_logs = [l for l in self._logs.values() if l.get("uid") == uid]
        if not user_logs:
            yield event.plain_result("📖 **冒险日志**\n\n还没有记录呢～完成一次冒险后这里就会出现♪")
            return
        user_logs.sort(key=lambda x: x.get("ended_at", 0), reverse=True)
        lines = ["📖 **冒险日志**", ""]
        for i, l in enumerate(user_logs[:10], 1):
            theme = l.get("theme", "未知")
            char_name = l.get("character_name", "")
            summary = (l.get("summary", "") or "")[:60]
            t = time.strftime("%m-%d %H:%M", time.localtime(l.get("ended_at", 0)))
            exit_type = l.get("exit_type", "")
            type_icon = "📖" if exit_type == "exit" else "⏸️" if exit_type == "pause" else "❓"
            lines.append(f"  {type_icon} **{theme}** ({char_name})")
            lines.append(f"     {summary}")
            lines.append(f"     🕐 {t}  |  对话 {l.get('history_length', 0)} 轮")
        if len(user_logs) > 10:
            lines.append(f"     ...还有 {len(user_logs) - 10} 条记录")
        yield event.plain_result("\n".join(lines))

    @adventure.command("结局")
    async def view_endings(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        mine = [e for e in self._endings.values() if e.get("uid") == uid]
        if not mine:
            yield event.plain_result("🏆 **结局图鉴**\n\n还没有收集到结局呢～完成一段冒险后这里会亮起♪")
            return
        mine.sort(key=lambda x: x.get("ended_at", 0), reverse=True)
        lines = ["🏆 **结局图鉴**", ""]
        for e in mine[:10]:
            tags = " ".join(f"#{t}" for t in e.get("tags", [])[:3])
            t = time.strftime("%m-%d %H:%M", time.localtime(e.get("ended_at", 0)))
            lines.append(f"  🏅 **{e.get('title', '无名结局')}** {tags}")
            lines.append(f"     {e.get('summary', '')}")
            lines.append(f"     🕐 {t}  |  {e.get('theme', '')}")
        if len(mine) > 10:
            lines.append(f"     ...还有 {len(mine) - 10} 枚结局")
        lines.append("")
        lines.append(f"💡 已收集 {len(mine)} 枚结局，继续书写你的传说吧♪")
        yield event.plain_result("\n".join(lines))

    @adventure.command("状态")
    async def group_status(self, event: AstrMessageEvent):
        key = self._group_key(event)
        session = self._group_sessions.get(key)
        if not session:
            yield event.plain_result("当前没有群体冒险。")
            return
        yield event.plain_result(self._group_status_text(session))

    @adventure.command("投票")
    async def group_vote(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        key = self._group_key(event)
        session = self._group_sessions.get(key)
        if not session:
            yield event.plain_result("当前没有群体冒险。")
            return
        participant = session.get("participants", {}).get(uid)
        if not participant or participant.get("status") != "active":
            yield event.plain_result("你还不在本轮行动中。使用「冒险 加入」后，通常会在下一轮登场。")
            return
        round_state = session.get("round", {})
        if round_state.get("status") != "voting":
            yield event.plain_result("当前不在投票阶段。")
            return
        vote = _strip_prefix(event.get_message_str().strip(), "冒险 投票").strip()
        if not vote:
            yield event.plain_result("格式：冒险 投票 <A/B/C/自定义行动>")
            return
        if len(vote) == 1:
            vote = vote.upper()
        round_state.setdefault("votes", {})[uid] = vote[:80]
        active_count = sum(1 for p in session.get("participants", {}).values() if p.get("status") == "active")
        voted_count = len(round_state.get("votes", {}))
        yield event.plain_result(f"🗳️ 已记录你的投票：{vote[:80]}（{voted_count}/{active_count}）")
        await self._maybe_resolve_group_if_all_voted(key)
