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
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Callable

from astrbot.api import logger

try:
    from astrbot.api import web as astrbot_web
except Exception:  # pragma: no cover - 旧版基座没有 astrbot.api.web 模块
    astrbot_web = None  # type: ignore[assignment]

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


def _split_csv_text(value: Any) -> list[str]:
    """把逗号/顿号/斜杠分隔的文本拆成干净列表；列表输入原样清洗。"""
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if not value:
        return []
    return [part.strip() for part in re.split(r"[,，、/|｜]", str(value)) if part.strip()]


def _normalize_legacy_world(name: str, world: Any) -> dict[str, Any]:
    """把旧版世界观数据补齐为 WebUI 使用的新结构。

    旧指令投稿通常只保存 content / easter_eggs；中间版本又出现过
    power_tier、plot_nodes 等顶层字段。这里保留原字段，同时补齐
    overview / eggs / plotlines / clues / locations / factions，避免后台编辑
    时出现"类型、描述、标签、线索、地点、势力、剧情线全空"。
    现网数据仍在使用 plot_nodes 兼容分支，不可移除。
    """
    if not isinstance(world, dict):
        world = {"content": str(world)}
    normalized = copy.deepcopy(world)
    content = str(normalized.get("content", "") or "").strip()

    overview = normalized.get("overview")
    if not isinstance(overview, dict):
        overview = {}
    overview.setdefault("title", normalized.get("title") or normalized.get("_name") or name)
    overview.setdefault("type", normalized.get("type") or "")
    overview.setdefault("description", normalized.get("description") or content)
    overview.setdefault("tags", _split_csv_text(normalized.get("tags")))
    overview.setdefault("power_tier", normalized.get("power_tier") or "")
    overview.setdefault("power_reference", normalized.get("power_reference") or "")
    normalized["overview"] = overview

    if "eggs" not in normalized:
        legacy_eggs = normalized.get("easter_eggs", [])
        eggs: list[dict[str, Any]] = []
        if isinstance(legacy_eggs, list):
            for item in legacy_eggs:
                if isinstance(item, dict):
                    eggs.append(item)
                elif str(item).strip():
                    eggs.append({"text": str(item).strip(), "trigger_condition": "", "reward": ""})
        normalized["eggs"] = eggs

    if "plotlines" not in normalized:
        plotlines: list[dict[str, Any]] = []
        for node in normalized.get("plot_nodes", []) if isinstance(normalized.get("plot_nodes"), list) else []:
            if not isinstance(node, dict):
                continue
            stage = {
                "title": node.get("title", ""),
                "description": node.get("summary") or node.get("description", ""),
                "trigger": node.get("trigger", ""),
                "clues": _split_csv_text(node.get("related_clues") or node.get("clues")),
                "choices": [],
            }
            plotlines.append({
                "title": node.get("title", "未命名剧情线"),
                "type": node.get("type", "支线"),
                "summary": node.get("summary") or node.get("description", ""),
                "stages": [stage],
                "related_npc": _split_csv_text(node.get("related_npc")),
                "day": node.get("day"),
                "can_player_intervene": node.get("can_player_intervene"),
            })
        normalized["plotlines"] = plotlines

    for key in ("npc", "clues", "locations", "factions"):
        if not isinstance(normalized.get(key), list):
            normalized[key] = []

    normalized.setdefault("content", content or overview.get("description", ""))
    normalized.setdefault("_name", name)
    return normalize_world(normalized, name=name)


def _as_bool(value: Any) -> bool:
    """宽容地把配置值转换为布尔（兼容字符串 "false"/"0"/"否" 等）。"""
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "否")
    return bool(value)


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
            # normalize_character 会剥离 skill_points / attr_locked / mp_cost 等遗留字段
            characters[uid] = normalize_character(current)
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
