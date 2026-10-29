"""如我所书 ⇄ AstrBot 官方 Web API（register_web_api + SSE 实时推送）。

所有路由统一注册在 ``/{PLUGIN_ID}`` 前缀下，由 AstrBot 面板负责鉴权与分发；
资源名一律通过 query 参数传递（规避路径段编码问题），变更操作一律使用 POST。
SSE 事件流（/events）向 WebUI 推送 worlds/characters/saves/config 变更通知。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import json
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Callable

from astrbot.api import logger

try:
    from astrbot.api import web as astrbot_web
except Exception:  # pragma: no cover - 旧版基座没有 astrbot.api.web 模块
    astrbot_web = None  # type: ignore[assignment]

from . import ai_gen
from .chara_card_v3 import (
    character_from_ccv3,
    character_from_png,
    character_to_ccv3,
    world_from_ccv3,
    world_to_ccv3,
)
from .core import PLUGIN_ID, _save_json
from .lore import normalize_character, normalize_world
from .render_pipeline import apply_render_scripts

try:
    from .archetypes import BUILTIN_TEMPLATE_REV, normalize_archetype
except Exception:  # pragma: no cover - archetypes.py 由并行任务稍后落地，未就绪时不阻断插件加载
    BUILTIN_TEMPLATE_REV = 1  # type: ignore[assignment]
    normalize_archetype = None  # type: ignore[assignment]

try:
    from .rpg import (
        GRADE_TIER,
        MAX_ACTIVE_SKILLS,
        MAX_ATTR,
        MAX_PASSIVE_SKILLS,
        _ensure_rpg,
        _recalc_rpg,
    )
except Exception:  # pragma: no cover - rpg.py 未就绪时不阻断插件加载
    GRADE_TIER = ("F", "E", "D", "C", "B", "A", "A+", "S")  # type: ignore[assignment]
    MAX_ACTIVE_SKILLS = 3  # type: ignore[assignment]
    MAX_PASSIVE_SKILLS = 3  # type: ignore[assignment]
    MAX_ATTR = 1000  # type: ignore[assignment]

    def _ensure_rpg(char):  # type: ignore[misc]
        return char

    def _recalc_rpg(char):  # type: ignore[misc]
        return char

ROUTE_BASE = f"/{PLUGIN_ID}"
PLUGIN_TITLE = "如我所书"
SSE_HEARTBEAT_SECONDS = 15
SSE_QUEUE_SIZE = 64
MAX_RENDER_SCRIPTS = 50

# /config/save 允许写入的白名单键 → 插件属性（其余键同名映射）
_CONFIG_ATTR_MAP: dict[str, str] = {
    "default_theme": "default_theme",
    "single_timeout": "single_timeout",
    "group_round_time": "group_round_time",
    "group_max_players": "group_max_players",
    "group_max_clues_per_round": "group_max_clues_per_round",
    "lore_content_budget": "lore_content_budget",
    "kb_name": "_kb_name",
    "enable_settle_extract": "enable_settle_extract",
    "enable_dice_judgment": "enable_dice_judgment",
}
_CONFIG_INT_KEYS = (
    "single_timeout",
    "group_round_time",
    "group_max_players",
    "group_max_clues_per_round",
    "lore_content_budget",
    "ai_gen_timeout",
)
_CONFIG_BOOL_KEYS = ("enable_settle_extract", "enable_dice_judgment")
_CONFIG_STR_KEYS = ("default_theme", "kb_name")


# ════════════════════════════════════════════════
#  数据归一化助手（自 webui_bridge.py 迁移）
# ════════════════════════════════════════════════

def _load_schema(plugin_dir: Path) -> dict[str, Any]:
    """读取插件目录下的 _conf_schema.json；缺失或损坏时返回空 dict。"""
    schema_path = plugin_dir / "_conf_schema.json"
    if not schema_path.exists():
        return {}
    try:
        return json.loads(schema_path.read_text("utf-8"))
    except Exception as exc:
        logger.warning(f"📖 如我所书 WebAPI: 读取配置 schema 失败: {exc}")
        return {}


def _schema_defaults(schema: dict[str, Any]) -> dict[str, Any]:
    """从 schema 中提取各配置项的默认值，作为 runtime 配置的兜底基线。"""
    return {k: v["default"] for k, v in schema.items() if isinstance(v, dict) and "default" in v}


def _normalize_legacy_world(name: str, world: Any) -> dict[str, Any]:
    """把旧版世界观数据补齐为 WebUI 使用的新结构。

    v1.5.0 起 overview / npc / plotlines / clues / locations / factions 与
    旧 plot_nodes、content→entries 的兼容迁移统一收拢到
    ``lore.normalize_world``，本函数仅保留兼容委托（现网数据仍在使用，
    调用点不可移除）。
    """
    if not isinstance(world, dict):
        world = {"content": str(world)}
    return normalize_world(world, name=name)


def _as_bool(value: Any) -> bool:
    """宽容地把配置值转换为布尔（兼容字符串 "false"/"0"/"否" 等）。"""
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "否")
    return bool(value)


def _sanitize_character(char: dict) -> dict:
    """清洗 WebUI 提交的角色卡：数值钳制、评级校验、能力上限，然后补齐并重算 RPG 层。

    管理员可在页面调整一切字段；此处只拦明显非法值，保证落盘数据永远可被
    冒险流程安全消费。
    """
    merged = dict(char if isinstance(char, dict) else {})
    # ── 三维属性：int，1 ~ MAX_ATTR（显式 0 钳到下限 1，非法回退 5）──
    attr = merged.get("attr") if isinstance(merged.get("attr"), dict) else {}
    for key in ("str", "agi", "spr"):
        raw = attr.get(key, 5)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 5
        attr[key] = max(1, min(MAX_ATTR, value))
    merged["attr"] = attr
    # ── 魅力/幸运：必须是合法评级，非法回退 C ──
    for key in ("charm", "luck"):
        value = str(merged.get(key) or "").strip().upper()
        merged[key] = value if value in GRADE_TIER else "C"
    # ── 布尔标记 ──
    for key in ("has_adventured", "is_random"):
        if key in merged:
            merged[key] = _as_bool(merged.get(key))
    # ── 战力锚点覆盖：留空/0 清除，恢复公式自动计算 ──
    if "power_override" in merged:
        override = merged.get("power_override")
        try:
            override_num = int(override)
        except (TypeError, ValueError):
            override_num = 0
        if override_num > 0:
            merged["power_override"] = max(0, min(9999, override_num))
        else:
            merged.pop("power_override", None)
    # ── 能力标签：结构化为 {name, desc, mastery, lv}，超限截断 ──
    skills = merged.get("skills") if isinstance(merged.get("skills"), dict) else {}
    for group, cap, fallback_mastery in (
        ("active", MAX_ACTIVE_SKILLS, "生疏"),
        ("passive", MAX_PASSIVE_SKILLS, "常驻"),
    ):
        items = skills.get(group) if isinstance(skills.get(group), list) else []
        cleaned: list[dict] = []
        for item in items:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                cleaned.append({
                    "name": name,
                    "desc": str(item.get("desc") or ""),
                    "mastery": str(item.get("mastery") or "").strip() or fallback_mastery,
                    "lv": item.get("lv", 1 if group == "active" else 0),
                })
            elif str(item or "").strip():
                cleaned.append({"name": str(item).strip(), "desc": "", "mastery": fallback_mastery,
                                "lv": 1 if group == "active" else 0})
        skills[group] = cleaned[:cap]
    merged["skills"] = skills
    result = normalize_character(merged)
    _ensure_rpg(result)
    now = time.time()
    if not merged.get("created_at"):
        result["created_at"] = now
    result["updated_at"] = now
    _recalc_rpg(result)
    return result


# ════════════════════════════════════════════════
#  Web API 主体
# ════════════════════════════════════════════════

class AsIWriteWebAPI:
    """如我所书的官方 Web API 适配器。

    - 通过 ``context.register_web_api`` 注册全部路由（同路由重复注册会替换，reload 安全）。
    - 通过 ``/events`` SSE 流向 WebUI 推送实时变更通知。
    - 所有 handler 内部兜底 try/except，绝不向框架抛异常刷屏。
    """

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.plugin_dir = Path(__file__).parent
        # SSE 订阅者：每个订阅者一个有界队列，满了丢最旧的一条
        self._subscribers: set[asyncio.Queue] = set()

    # ── 注册与生命周期 ──

    def routes(self) -> list[tuple[str, Callable[..., Any], list[str], str]]:
        """路由表：(相对路径, handler, HTTP 方法, 描述)。"""
        return [
            ("/health", self._health, ["GET"], "健康检查与数据统计"),
            ("/worlds", self._worlds, ["GET"], "全部世界观（含旧数据归一化）"),
            ("/world/get", self._world_get, ["GET"], "查询单个世界观"),
            ("/world/save", self._world_save, ["POST"], "保存世界观"),
            ("/world/delete", self._world_delete, ["POST"], "删除世界观"),
            ("/characters", self._characters, ["GET"], "全部角色卡"),
            ("/character/get", self._character_get, ["GET"], "查询单个角色卡"),
            ("/character/save", self._character_save, ["POST"], "保存角色卡"),
            ("/character/delete", self._character_delete, ["POST"], "删除角色卡"),
            ("/saves", self._saves, ["GET"], "全部存档"),
            ("/save/delete", self._save_delete, ["POST"], "删除存档"),
            ("/archetypes", self._archetypes, ["GET"], "全部种族/职业图鉴条目"),
            ("/archetype/save", self._archetype_save, ["POST"], "保存种族/职业图鉴条目"),
            ("/archetype/delete", self._archetype_delete, ["POST"], "删除种族/职业图鉴条目"),
            ("/config", self._config, ["GET"], "读取配置 schema 与运行时配置"),
            ("/config/save", self._config_save, ["POST"], "保存运行时配置"),
            ("/export/world", self._export_world, ["GET"], "导出世界卡（CCv3）"),
            ("/export/character", self._export_character, ["GET"], "导出角色卡（CCv3）"),
            ("/import/world", self._import_world, ["POST"], "导入世界卡（CCv3）"),
            ("/import/character", self._import_character, ["POST"], "导入角色卡（CCv3）"),
            ("/import/png", self._import_png, ["POST"], "解析 PNG 角色卡（返回预览）"),
            ("/render-scripts", self._render_scripts, ["GET"], "读取正则渲染脚本"),
            ("/render-scripts/save", self._render_scripts_save, ["POST"], "保存正则渲染脚本"),
            ("/render-scripts/test", self._render_scripts_test, ["POST"], "测试正则渲染脚本"),
            ("/ai/generate", self._ai_generate, ["POST"], "AI 一键生成（world/archetype/character 草稿）"),
            ("/ai/complete/world", self._ai_complete_world, ["POST"], "AI 补全世界观缺失板块（返回草稿）"),
            ("/ai/expand", self._ai_expand, ["POST"], "AI 润色文本字段"),
            ("/events", self._events, ["GET"], "SSE 实时事件流"),
        ]

    def register(self) -> bool:
        """把全部路由注册到 AstrBot 官方 Web API；重复调用是安全的。"""
        if astrbot_web is None:
            logger.warning("📖 如我所书 WebAPI: 当前 AstrBot 版本缺少 astrbot.api.web，跳过 Web API 注册")
            return False
        register = getattr(getattr(self.plugin, "context", None), "register_web_api", None)
        if not callable(register):
            logger.warning("📖 如我所书 WebAPI: context 不支持 register_web_api，跳过 Web API 注册")
            return False
        routes = self.routes()
        ok_count = 0
        for path, handler, methods, desc in routes:
            try:
                register(f"{ROUTE_BASE}{path}", handler, methods, desc)
                ok_count += 1
            except Exception as exc:
                logger.warning(f"📖 如我所书 WebAPI: 注册路由 {path} 失败: {exc}")
        if ok_count:
            logger.info(
                f"📖 如我所书 WebAPI: 已注册 {ok_count}/{len(routes)} 条官方 Web API（前缀 {ROUTE_BASE}）"
            )
        return ok_count > 0

    def close(self) -> None:
        """插件卸载/重载时关闭所有 SSE 订阅者并清空状态。"""
        for queue in list(self._subscribers):
            try:
                # 哨兵消息：让事件流协程尽快退出，而不是等下一次心跳
                queue.put_nowait(None)
            except Exception:
                pass
        self._subscribers.clear()

    # ── AI 助手（生成/补完/润色，一律返回草稿，由管理员确认后另行保存） ──

    async def _call_llm(self, prompt: str, session_id: str) -> tuple[bool, str]:
        """调用当前启用的 LLM；返回 (成功与否, 文本或错误说明)。"""
        try:
            provider = self.plugin.context.get_using_provider()
        except Exception:
            provider = None
        if provider is None:
            return False, "暂无可用 LLM 服务：请先在面板「服务提供商」配置并启用模型"
        try:
            timeout = max(15, int(getattr(self.plugin, "ai_gen_timeout", 120) or 120))
        except (TypeError, ValueError):
            timeout = 120
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, session_id=session_id, contexts=[]),
                timeout=timeout,
            )
            return True, (resp.completion_text or "")
        except asyncio.TimeoutError:
            return False, f"生成超时（>{timeout} 秒）。可在插件配置调大 ai_gen_timeout 后重试。"
        except Exception as exc:
            return False, f"LLM 调用失败: {exc}"

    async def _ai_generate(self):
        try:
            body = await self._json_body()
            task = str(body.get("task") or "").strip()
            idea = str(body.get("prompt") or "").strip()
            name = str(body.get("name") or "").strip()
            type_ = str(body.get("type") or "race").strip().lower()
            if task not in ("world", "archetype", "character"):
                return self._fail("task 仅支持 world / archetype / character", 400)
            if not idea:
                return self._fail("请先填写生成灵感或要求", 400)
            base_prompt = {
                "world": ai_gen.WORLD_GEN_PROMPT,
                "archetype": ai_gen.ARCHETYPE_GEN_PROMPT,
                "character": ai_gen.CHARACTER_GEN_PROMPT,
            }[task]
            full = base_prompt + "\n【用户灵感/要求】\n" + idea[:1200]
            if task == "archetype":
                if type_ not in ("race", "job"):
                    type_ = "race"
                full += "\n【条目类型】\n" + ("race（种族）" if type_ == "race" else "job（职业）")
            if task == "world" and name:
                full += "\n【世界名称】\n" + name[:60]
            ok, text = await self._call_llm(full, f"aiweb_gen_{task}")
            if not ok:
                return self._fail(text, 502)
            payload = ai_gen.parse_llm_json(text)
            if payload is None:
                return self._fail("模型没有返回有效的 JSON 草稿，请重试或更换模型", 502)
            if task == "world":
                draft = ai_gen.normalize_world_draft(payload, name)
            elif task == "archetype":
                draft = ai_gen.normalize_archetype_draft(payload, name, type_)
            else:
                draft = ai_gen.normalize_character_draft(payload)
            return {"ok": True, "draft": draft}
        except Exception as exc:
            return self._fail(f"AI 生成失败: {exc}")

    async def _ai_complete_world(self):
        try:
            body = await self._json_body()
            name = self._query("name") or str(body.get("name") or "").strip()
            world = (getattr(self.plugin, "_worlds", None) or {}).get(name)
            if world is None:
                return self._fail(f"未找到世界观「{name}」", 404)
            missing = ai_gen.missing_world_sections(world)
            if not missing:
                return {"ok": True, "world": world, "added": [], "message": "各板块都已完整，无需补全"}
            summary = ai_gen.world_summary_for_complete(world, name)
            prompt = (
                ai_gen.WORLD_COMPLETE_PROMPT
                + "\n【已有世界观摘要】\n" + summary
                + "\n【需要补全的板块】\n" + "、".join(missing)
                + "\n只输出这些板块对应字段，其余板块不要生成。"
            )
            ok, text = await self._call_llm(prompt, "aiweb_complete_world")
            if not ok:
                return self._fail(text, 502)
            payload = ai_gen.parse_llm_json(text)
            if payload is None:
                return self._fail("模型没有返回有效的 JSON 草稿，请重试或更换模型", 502)
            merged, added = ai_gen.merge_world_draft(world, payload)
            merged = normalize_world(merged, name=name)
            return {"ok": True, "world": merged, "added": added}
        except Exception as exc:
            return self._fail(f"AI 补全失败: {exc}")

    async def _ai_expand(self):
        try:
            body = await self._json_body()
            text = str(body.get("text") or "").strip()
            if not text:
                return self._fail("缺少要润色的文本", 400)
            if len(text) > 2000:
                return self._fail("文本过长（>2000 字），请分段润色", 400)
            ok, out = await self._call_llm(ai_gen.EXPAND_PROMPT + text, "aiweb_expand")
            if not ok:
                return self._fail(out, 502)
            cleaned = out.strip().strip('"“”「」')
            return {"ok": True, "text": cleaned[:2000]}
        except Exception as exc:
            return self._fail(f"AI 润色失败: {exc}")

    # ── SSE 实时推送 ──

    def notify(self, event_type: str, data: Any = None) -> None:
        """向所有 SSE 订阅者投递一条 JSON 事件；异常安全，绝不向上抛。"""
        try:
            if not self._subscribers:
                return
            payload = (
                "data: "
                + json.dumps({"type": event_type, "data": data}, ensure_ascii=False, default=str)
                + "\n\n"
            )
            for queue in list(self._subscribers):
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    # 消费过慢的订阅者：丢最旧一条，保住最新事件
                    try:
                        queue.get_nowait()
                        queue.put_nowait(payload)
                    except Exception:
                        pass
                except Exception:
                    pass
        except Exception:
            pass

    async def _event_stream(self) -> AsyncIterator[str]:
        """SSE 事件流：先发 retry 间隔，空闲时定期发送 keep-alive 心跳。"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=SSE_QUEUE_SIZE)
        self._subscribers.add(queue)
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if message is None:  # close() 的哨兵
                    break
                yield message
        finally:
            self._subscribers.discard(queue)

    async def _events(self):
        """GET /events — 建立 SSE 长连接。"""
        try:
            return astrbot_web.stream_response(
                self._event_stream(),
                content_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        except Exception as exc:
            return self._fail(f"建立事件流失败: {exc}")

    # ── 请求解析助手 ──

    @staticmethod
    def _query(key: str, default: str = "") -> str:
        """读取当前请求的 query 参数；缺失或异常时返回默认值。"""
        try:
            value = astrbot_web.request.query.get(key, default)
        except Exception:
            return default
        return str(value if value is not None else default).strip()

    async def _json_body(self) -> dict:
        """解析 JSON 请求体；非法或非对象时返回空 dict。"""
        try:
            payload = await astrbot_web.request.json(default={})
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _fail(message: str, status_code: int = 500):
        """构造官方错误响应（带 status/message 信封）。"""
        return astrbot_web.error_response(message, status_code=status_code)

    # ── 健康检查 ──

    async def _health(self):
        try:
            p = self.plugin
            return {
                "ok": True,
                "plugin": PLUGIN_ID,
                "title": PLUGIN_TITLE,
                "worlds": len(getattr(p, "_worlds", None) or {}),
                "characters": len(getattr(p, "_characters", None) or {}),
                "saves": len(getattr(p, "_saves", None) or {}),
                "archetypes": len(getattr(p, "_archetypes", None) or {}),
            }
        except Exception as exc:
            return self._fail(f"健康检查失败: {exc}")

    # ── 世界观 ──

    async def _worlds(self):
        try:
            return {
                name: _normalize_legacy_world(name, world)
                for name, world in (getattr(self.plugin, "_worlds", None) or {}).items()
            }
        except Exception as exc:
            return self._fail(f"读取世界观列表失败: {exc}")

    async def _world_get(self):
        try:
            name = self._query("name")
            world = (self.plugin._worlds or {}).get(name)
            if world is None:
                return self._fail(f"未找到世界观「{name}」", 404)
            return _normalize_legacy_world(name, world)
        except Exception as exc:
            return self._fail(f"查询世界观失败: {exc}")

    async def _world_save(self):
        try:
            body = await self._json_body()
            # 官方桥的 apiPost 没有 query 通道，资源名兼容从 body 读取
            name = self._query("name") or str(body.get("name") or "").strip()
            if not name:
                return self._fail("缺少世界观名称（query 或 body 的 name）", 400)
            body.pop("name", None)
            if not body:
                return self._fail("请求体必须是 JSON 对象", 400)
            self.plugin._worlds[name] = _normalize_legacy_world(name, body)
            _save_json(self.plugin._worlds_path, self.plugin._worlds)
            self.notify("worlds_changed", {"name": name, "source": "webui"})
            return {"ok": True, "name": name}
        except Exception as exc:
            return self._fail(f"保存世界观失败: {exc}")

    async def _world_delete(self):
        try:
            body = await self._json_body()
            name = self._query("name") or str(body.get("name") or "").strip()
            self.plugin._worlds.pop(name, None)
            _save_json(self.plugin._worlds_path, self.plugin._worlds)
            self.notify("worlds_changed", {"name": name, "source": "webui"})
            return {"ok": True}
        except Exception as exc:
            return self._fail(f"删除世界观失败: {exc}")

    # ── 角色卡 ──

    async def _characters(self):
        try:
            return copy.deepcopy(getattr(self.plugin, "_characters", None) or {})
        except Exception as exc:
            return self._fail(f"读取角色列表失败: {exc}")

    async def _character_get(self):
        try:
            uid = self._query("uid")
            char = (self.plugin._characters or {}).get(uid)
            if char is None:
                return self._fail(f"未找到角色 {uid}", 404)
            return copy.deepcopy(char)
        except Exception as exc:
            return self._fail(f"查询角色失败: {exc}")

    async def _character_save(self):
        try:
            body = await self._json_body()
            # 官方桥的 apiPost 没有 query 通道，资源名兼容从 body 读取
            uid = self._query("uid") or str(body.get("uid") or "").strip()
            if not uid:
                return self._fail("缺少角色 UID（query 或 body 的 uid）", 400)
            body.pop("uid", None)
            if not body:
                return self._fail("请求体必须是 JSON 对象", 400)
            characters = self.plugin._characters
            current = characters.setdefault(uid, {})
            current.update(body)
            # 清洗数值/评级/能力上限，normalize 剥离遗留字段，最后重算 HP/MP/战力
            characters[uid] = _sanitize_character(current)
            _save_json(self.plugin._characters_path, characters)
            self.notify("characters_changed", {"uid": uid, "source": "webui"})
            return {"ok": True, "uid": uid}
        except Exception as exc:
            return self._fail(f"保存角色失败: {exc}")

    async def _character_delete(self):
        try:
            body = await self._json_body()
            uid = self._query("uid") or str(body.get("uid") or "").strip()
            self.plugin._characters.pop(uid, None)
            _save_json(self.plugin._characters_path, self.plugin._characters)
            self.notify("characters_changed", {"uid": uid, "source": "webui"})
            return {"ok": True}
        except Exception as exc:
            return self._fail(f"删除角色失败: {exc}")

    # ── 存档 ──

    async def _saves(self):
        try:
            return copy.deepcopy(getattr(self.plugin, "_saves", None) or {})
        except Exception as exc:
            return self._fail(f"读取存档列表失败: {exc}")

    async def _save_delete(self):
        try:
            body = await self._json_body()
            sid = self._query("id") or str(body.get("id") or "").strip()
            self.plugin._saves.pop(sid, None)
            _save_json(self.plugin._saves_path, self.plugin._saves)
            self.notify("saves_changed", {"id": sid, "source": "webui"})
            return {"ok": True}
        except Exception as exc:
            return self._fail(f"删除存档失败: {exc}")

    # ── 种族/职业图鉴 ──

    async def _archetypes(self):
        try:
            data = copy.deepcopy(getattr(self.plugin, "_archetypes", None) or {})
            type_filter = self._query("type").lower()
            if type_filter:
                if type_filter not in ("race", "job"):
                    return self._fail("type 参数仅支持 race 或 job", 400)
                filtered: dict[str, dict] = {}
                for entry_name, item in data.items():
                    item_type = (
                        str(item.get("type") or "").strip().lower()
                        if isinstance(item, dict)
                        else ""
                    )
                    if item_type == type_filter:
                        filtered[entry_name] = item
                data = filtered
            return data
        except Exception as exc:
            return self._fail(f"读取图鉴库失败: {exc}")

    async def _archetype_save(self):
        try:
            body = await self._json_body()
            # 官方桥的 apiPost 没有 query 通道，资源名兼容从 body 读取
            name = self._query("name") or str(body.get("name") or "").strip()
            if not name:
                return self._fail("缺少条目名称", 400)
            body.pop("name", None)
            type_ = str(body.get("type") or "race").strip()
            if type_ not in ("race", "job"):
                return self._fail("type 仅支持 race 或 job", 400)
            if normalize_archetype is None:
                return self._fail("图鉴库模块（archetypes.py）尚未就绪", 500)
            arche = normalize_archetype(body, name=name, type_=type_)
            # 盖章：rev 达标的内置/自定义条目不会被启动迁移替换，管理员修改得以保留
            arche["template_rev"] = BUILTIN_TEMPLATE_REV
            arche["updated_at"] = time.time()
            if not arche.get("created_at"):
                arche["created_at"] = time.time()
            self.plugin._archetypes[name] = arche
            _save_json(self.plugin._archetypes_path, self.plugin._archetypes)
            self.notify("archetypes_changed", {"name": name})
            return {"ok": True, "name": name}
        except Exception as exc:
            return self._fail(f"保存图鉴条目失败: {exc}")

    async def _archetype_delete(self):
        try:
            body = await self._json_body()
            name = self._query("name") or str(body.get("name") or "").strip()
            self.plugin._archetypes.pop(name, None)
            _save_json(self.plugin._archetypes_path, self.plugin._archetypes)
            self.notify("archetypes_changed", {"name": name})
            return {"ok": True}
        except Exception as exc:
            return self._fail(f"删除图鉴条目失败: {exc}")

    # ── 配置 ──

    async def _config(self):
        try:
            p = self.plugin
            schema = _load_schema(self.plugin_dir)
            runtime = _schema_defaults(schema)
            runtime.update({
                "default_theme": p.default_theme,
                "single_timeout": p.single_timeout,
                "group_round_time": p.group_round_time,
                "group_max_players": p.group_max_players,
                "group_max_clues_per_round": p.group_max_clues_per_round,
                "lore_content_budget": p.lore_content_budget,
                "kb_name": getattr(p, "_kb_name", ""),
                "enable_settle_extract": p.enable_settle_extract,
                "enable_dice_judgment": p.enable_dice_judgment,
                "ai_gen_timeout": int(getattr(p, "ai_gen_timeout", 120) or 120),
            })
            return {
                "schema": schema,
                "runtime": runtime,
                "worlds": list((getattr(p, "_worlds", None) or {}).keys()),
            }
        except Exception as exc:
            return self._fail(f"读取配置失败: {exc}")

    async def _config_save(self):
        try:
            body = await self._json_body()
            if not body:
                return self._fail("请求体必须是 JSON 对象", 400)
            updates: dict[str, Any] = {}
            for key in _CONFIG_STR_KEYS:
                if key in body:
                    updates[key] = str(body[key] or "").strip()
            for key in _CONFIG_INT_KEYS:
                if key in body:
                    try:
                        updates[key] = int(body[key])
                    except (TypeError, ValueError):
                        return self._fail(f"配置项 {key} 必须是整数", 400)
            for key in _CONFIG_BOOL_KEYS:
                if key in body:
                    updates[key] = _as_bool(body[key])
            if not updates:
                return self._fail("没有可识别的配置项", 400)
            p = self.plugin
            # AstrBotConfig 是 dict 子类：原地 update 可保留其 save_config 能力
            config = getattr(p, "config", None)
            if isinstance(config, dict):
                config.update(updates)
            else:
                p.config = dict(updates)
            for key, value in updates.items():
                setattr(p, _CONFIG_ATTR_MAP.get(key, key), value)
            if "kb_name" in updates:
                # 知识库名称变更后重置缓存连接，下次使用时按新名称重连
                p._kb_helper = None
            save_config = getattr(p.config, "save_config", None)
            if callable(save_config):
                try:
                    save_config()
                except Exception as exc:
                    logger.warning(f"📖 如我所书 WebAPI: 配置持久化失败: {exc}")
            else:
                logger.warning(
                    "📖 如我所书 WebAPI: config 无 save_config 方法，配置仅写入运行时；"
                    "请在面板插件配置页保存以持久化"
                )
            self.notify("config_changed", {"saved": list(updates.keys())})
            return {"ok": True, "saved": updates}
        except Exception as exc:
            return self._fail(f"保存配置失败: {exc}")

    # ── Chara Card V3 导入导出 ──

    async def _export_world(self):
        try:
            name = self._query("name")
            world = (self.plugin._worlds or {}).get(name)
            if world is None:
                return self._fail(f"未找到世界观「{name}」", 404)
            return world_to_ccv3(world, name)
        except Exception as exc:
            return self._fail(f"导出世界卡失败: {exc}")

    async def _export_character(self):
        try:
            uid = self._query("uid")
            char = (self.plugin._characters or {}).get(uid)
            if char is None:
                return self._fail(f"未找到角色 {uid}", 404)
            return character_to_ccv3(char)
        except Exception as exc:
            return self._fail(f"导出角色卡失败: {exc}")

    async def _import_world(self):
        try:
            body = await self._json_body()
            if not body:
                return self._fail("请求体必须是 JSON 对象", 400)
            name, world = world_from_ccv3(body.get("card"))
            target_name = str(body.get("name") or name or "").strip()
            if not target_name:
                return self._fail("世界卡缺少名称", 400)
            if target_name in (self.plugin._worlds or {}) and not body.get("overwrite"):
                return self._fail(f"世界观「{target_name}」已存在；请明确确认覆盖", 409)
            self.plugin._worlds[target_name] = normalize_world(world, name=target_name)
            _save_json(self.plugin._worlds_path, self.plugin._worlds)
            self.notify("worlds_changed", {"name": target_name, "source": "ccv3_import"})
            return {"ok": True, "name": target_name, "world": self.plugin._worlds[target_name]}
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self._fail(f"世界卡导入失败: {exc}", 400)
        except Exception as exc:
            return self._fail(f"世界卡导入失败: {exc}")

    async def _import_character(self):
        try:
            body = await self._json_body()
            if not body:
                return self._fail("请求体必须是 JSON 对象", 400)
            character = character_from_ccv3(body.get("card"))
            uid = str(body.get("uid") or "").strip()
            if not uid:
                # 无 uid 时只做预览，前端确认后再带 uid 调用落盘
                return {"preview": character}
            if uid in (self.plugin._characters or {}) and not body.get("overwrite"):
                return self._fail(f"目标 UID {uid} 已存在；请明确确认覆盖", 409)
            self.plugin._characters[uid] = character
            _save_json(self.plugin._characters_path, self.plugin._characters)
            self.notify("characters_changed", {"uid": uid, "source": "ccv3_import"})
            return {"ok": True, "uid": uid, "character": character}
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self._fail(f"角色卡导入失败: {exc}", 400)
        except Exception as exc:
            return self._fail(f"角色卡导入失败: {exc}")

    async def _import_png(self):
        """解析 PNG 角色卡：multipart 字段 file 优先，兼容 JSON body {data_base64}。

        只返回 {"preview": character}，由前端确认后再调 /import/character 落盘。
        """
        try:
            png_data = b""
            try:
                content_type = astrbot_web.request.content_type or ""
            except Exception:
                content_type = ""
            if "multipart/" in content_type:
                files = await astrbot_web.request.files()
                upload = files.get("file")
                if upload is None:
                    return self._fail("multipart 请求中未找到字段 file", 400)
                png_data = await upload.read()
            else:
                body = await self._json_body()
                encoded = str(body.get("data_base64") or "")
                if not encoded:
                    return self._fail("缺少 PNG 数据（multipart 字段 file 或 JSON data_base64）", 400)
                png_data = base64.b64decode(encoded, validate=True)
            if not png_data:
                return self._fail("PNG 数据为空", 400)
            character = character_from_png(png_data)
            return {"preview": character}
        except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
            return self._fail(f"PNG 角色卡解析失败: {exc}", 400)
        except Exception as exc:
            return self._fail(f"PNG 角色卡解析失败: {exc}")

    # ── 正则渲染脚本 ──

    async def _render_scripts(self):
        try:
            return {"scripts": copy.deepcopy(getattr(self.plugin, "render_scripts", None) or [])}
        except Exception as exc:
            return self._fail(f"读取渲染脚本失败: {exc}")

    async def _render_scripts_save(self):
        try:
            body = await self._json_body()
            scripts = body.get("scripts")
            if not isinstance(scripts, list):
                return self._fail("scripts 必须是数组", 400)
            scripts = scripts[:MAX_RENDER_SCRIPTS]
            for item in scripts:
                if not isinstance(item, dict):
                    return self._fail("scripts 数组中每一项都必须是对象", 400)
            self.plugin.render_scripts = copy.deepcopy(scripts)
            _save_json(self.plugin._render_scripts_path, {"scripts": self.plugin.render_scripts})
            self.notify("render_scripts_changed", {"count": len(self.plugin.render_scripts)})
            return {"ok": True, "scripts": self.plugin.render_scripts}
        except Exception as exc:
            return self._fail(f"保存渲染脚本失败: {exc}")

    async def _render_scripts_test(self):
        try:
            body = await self._json_body()
            pattern = str(body.get("find_regex") or "")
            replacement = str(body.get("replace_string") or "")
            sample = body.get("sample")
            if not isinstance(sample, str):
                sample = "" if sample is None else str(sample)
            if not pattern:
                return self._fail("缺少 find_regex", 400)
            try:
                re.compile(pattern)
            except re.error as exc:
                return self._fail(f"正则表达式无效: {exc}", 400)
            result = apply_render_scripts(
                sample,
                [{"enabled": True, "find_regex": pattern, "replace_string": replacement}],
            )
            return {"ok": True, "result": result}
        except Exception as exc:
            return self._fail(f"渲染脚本测试失败: {exc}")
