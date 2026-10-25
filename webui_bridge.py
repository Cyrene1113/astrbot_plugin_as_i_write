"""如我所书 ⇄ 基座 WebUI 全新桥接层。

这个模块只负责一件事：把插件实例以幂等、可重复刷新的方式注册到
astrbot_plugin_base_webui。它不启动独立 HTTP 服务，也不依赖旧的心跳线程。
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import os
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote

from astrbot.api import logger

from .core import _save_json
from .chara_card_v3 import (
    character_from_ccv3,
    character_from_png,
    character_to_ccv3,
    world_from_ccv3,
    world_to_ccv3,
)
from .lore import normalize_character, normalize_world

PLUGIN_NAME = "as_i_write"
PLUGIN_TITLE = "如我所书"
API_PREFIX = f"/api/{PLUGIN_NAME}"


def _load_plugin_icon() -> str:
    """读取插件目录下的 icon.png，作为基座 WebUI 侧栏图标。"""
    icon_path = Path(__file__).with_name("icon.png")
    try:
        data = base64.b64encode(icon_path.read_bytes()).decode("ascii")
        return (
            "<img src=\"data:image/png;base64," + data + "\" "
            "alt=\"如我所书\" "
            "style=\"width:1.35em;height:1.35em;border-radius:7px;object-fit:cover;vertical-align:-0.28em\">"
        )
    except Exception:
        return "📖"


PLUGIN_ICON = _load_plugin_icon()


def _ok(data: Any = None, status: int = 200) -> tuple[Any, int]:
    return ({"ok": True} if data is None else data, status)


def _error(message: str, status: int = 400) -> tuple[dict[str, str], int]:
    return ({"error": message}, status)


def _path_param(params: dict[str, Any]) -> str:
    return unquote(str(params.get("_path_param", "")).strip())


def _load_schema(plugin_dir: Path) -> dict[str, Any]:
    schema_path = plugin_dir / "_conf_schema.json"
    if not schema_path.exists():
        return {}
    try:
        return json.loads(schema_path.read_text("utf-8"))
    except Exception as exc:
        logger.warning(f"📖 如我所书 WebUI: 读取配置 schema 失败: {exc}")
        return {}


def _schema_defaults(schema: dict[str, Any]) -> dict[str, Any]:
    return {k: v["default"] for k, v in schema.items() if isinstance(v, dict) and "default" in v}


def _split_csv_text(value: Any) -> list[str]:
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
    时出现“类型、描述、标签、线索、地点、势力、剧情线全空”。
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


class AsIWriteWebUIBridge:
    """如我所书的基座 WebUI 适配器。

    设计目标：
    1. 无旧状态漂移：每次 register() 都完整覆盖页面与端点。
    2. 无独立端口：所有请求都走基座 /api/as_i_write/*。
    3. 无后台心跳线程：依靠初始化、基座 discovery 自愈与显式重注册。
    """

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.plugin_dir = Path(__file__).parent
        self._registered = False

    @property
    def registered(self) -> bool:
        return self._registered

    def register(self, base_registry=None) -> bool:
        """把页面与 API 端点注册到基座。重复调用是安全的。

        当由基座自愈机制调用时，base_registry 传入当前的注册表实例，
        避免因模块热重载导致注册到陈旧的 registry 单例。
        """
        try:
            if base_registry is not None:
                registry = base_registry
            else:
                from astrbot_plugin_base_webui.registry import registry
        except ImportError:
            logger.info("📖 如我所书 WebUI: 未安装基座 WebUI，跳过接入")
            self._registered = False
            return False
        except Exception as exc:
            logger.warning(f"📖 如我所书 WebUI: 导入基座注册表失败: {exc}")
            self._registered = False
            return False

        try:
            registry.register_page(PLUGIN_NAME, PLUGIN_TITLE, PLUGIN_ICON, self.render_page)
            for method, path, handler in self.routes():
                registry.register_handler(PLUGIN_NAME, method, path, handler)
            self._registered = True
            logger.info("📖 如我所书 WebUI: 已通过全新桥接层接入基座")
            return True
        except Exception as exc:
            self._registered = False
            logger.warning(f"📖 如我所书 WebUI: 注册失败: {exc}")
            return False

    def unregister(self) -> None:
        try:
            from astrbot_plugin_base_webui.registry import registry
            registry.unregister(PLUGIN_NAME)
        except Exception:
            pass
        finally:
            self._registered = False

    def routes(self) -> list[tuple[str, str, Callable[[dict, dict], tuple[Any, int]]]]:
        return [
            ("GET", "/health", self.health),
            ("GET", "/worlds", self.list_worlds),
            ("GET", "/worlds/", self.get_world),
            ("PUT", "/worlds/", self.save_world),
            ("DELETE", "/worlds/", self.delete_world),
            ("GET", "/characters", self.list_characters),
            ("GET", "/characters/", self.get_character),
            ("PUT", "/characters/", self.save_character),
            ("DELETE", "/characters/", self.delete_character),
            ("POST", "/import/character", self.import_character),
            ("POST", "/import/world", self.import_world),
            ("POST", "/import/png", self.import_png),
            ("GET", "/export/character/", self.export_character),
            ("GET", "/export/world/", self.export_world),
            ("GET", "/render-scripts", self.get_render_scripts),
            ("PUT", "/render-scripts", self.save_render_scripts),
            ("GET", "/saves", self.list_saves),
            ("GET", "/saves/", self.get_save),
            ("DELETE", "/saves/", self.delete_save),
            ("GET", "/config", self.get_config),
            ("PUT", "/config", self.save_config),
        ]

    def render_page(self) -> str:
        html_path = self.plugin_dir / "webui_index.html"
        if not html_path.exists():
            return "<h1>404 - 如我所书 WebUI 页面不存在</h1>"
        html = html_path.read_text("utf-8")
        html = self._normalize_frontend_api(html)
        inject = (
            "<script>"
            f"window.AS_I_WRITE_API_PATH='{API_PREFIX}';"
            "window.AS_I_WRITE_BASE_API=(window.parent&&window.parent.location&&window.parent.location.origin?window.parent.location.origin:window.location.origin)+window.AS_I_WRITE_API_PATH;"
            "window.BASE_API=window.AS_I_WRITE_BASE_API;"
            "</script>\n"
        )
        # 基座会把插件页放进 blob: iframe；blob 文档里 fetch('/api/...') 无法解析到
        # http://host:port/api/...，浏览器会报 Failed to parse URL。这里注入父页面
        # origin + API 前缀，保证所有请求都是绝对 URL。
        html = html.replace("<head>", "<head>\n" + inject, 1) if "<head>" in html else inject + html
        return html

    def _normalize_frontend_api(self, html: str) -> str:
        """把旧独立 WebUI 的 /api/* 请求改为基座相对请求。

        页面只需要请求 /worlds、/characters 等相对路径；基座会注入
        window.BASE_API=/api/as_i_write，由统一 api() 包装补齐前缀。
        """
        html = re.sub(r"(['\"])/api/(worlds|characters|saves|config|health)", r"\1/\2", html)
        html = re.sub(
            r"const\s+api\s*=\s*async\s*\(\s*u\s*,\s*o\s*=\s*\{\}\s*\)\s*=>\s*\{\s*const\s+r\s*=\s*await\s+fetch\(\s*u\s*,",
            "const api=async(u,o={})=>{const p=window.BASE_API||window.AS_I_WRITE_BASE_API||'/api/as_i_write';const r=await fetch(p+u,",
            html,
            count=1,
        )
        return html

    # ── 基础状态 ──

    def health(self, params: dict, body: dict) -> tuple[Any, int]:
        return _ok({
            "ok": True,
            "plugin": PLUGIN_NAME,
            "title": PLUGIN_TITLE,
            "worlds": len(getattr(self.plugin, "_worlds", {}) or {}),
            "characters": len(getattr(self.plugin, "_characters", {}) or {}),
            "saves": len(getattr(self.plugin, "_saves", {}) or {}),
        })

    # ── 世界观 ──

    def list_worlds(self, params: dict, body: dict) -> tuple[Any, int]:
        worlds = {
            name: _normalize_legacy_world(name, world)
            for name, world in (self.plugin._worlds or {}).items()
        }
        return _ok(worlds)

    def get_world(self, params: dict, body: dict) -> tuple[Any, int]:
        name = _path_param(params)
        world = self.plugin._worlds.get(name)
        return _ok(_normalize_legacy_world(name, world)) if world is not None else _error("未找到世界观", 404)

    def save_world(self, params: dict, body: dict) -> tuple[Any, int]:
        name = _path_param(params)
        if not name:
            return _error("缺少世界观名称")
        if not isinstance(body, dict):
            return _error("请求体必须是 JSON 对象")
        self.plugin._worlds[name] = _normalize_legacy_world(name, body)
        self._save(self.plugin._worlds_path, self.plugin._worlds)
        self._broadcast("worlds_changed", {"name": name})
        return _ok()

    def delete_world(self, params: dict, body: dict) -> tuple[Any, int]:
        name = _path_param(params)
        self.plugin._worlds.pop(name, None)
        self._save(self.plugin._worlds_path, self.plugin._worlds)
        self._broadcast("worlds_changed", {"name": name})
        return _ok()

    # ── 角色 ──

    def list_characters(self, params: dict, body: dict) -> tuple[Any, int]:
        return _ok(copy.deepcopy(self.plugin._characters))

    def get_character(self, params: dict, body: dict) -> tuple[Any, int]:
        uid = _path_param(params)
        char = self.plugin._characters.get(uid)
        return _ok(copy.deepcopy(char)) if char is not None else _error("未找到角色", 404)

    def save_character(self, params: dict, body: dict) -> tuple[Any, int]:
        uid = _path_param(params)
        if not uid:
            return _error("缺少角色 UID")
        if not isinstance(body, dict):
            return _error("请求体必须是 JSON 对象")
        current = self.plugin._characters.setdefault(uid, {})
        current.update(body)
        self.plugin._characters[uid] = normalize_character(current)
        self._save(self.plugin._characters_path, self.plugin._characters)
        self._broadcast("characters_changed", {"uid": uid})
        return _ok()

    def delete_character(self, params: dict, body: dict) -> tuple[Any, int]:
        uid = _path_param(params)
        self.plugin._characters.pop(uid, None)
        self._save(self.plugin._characters_path, self.plugin._characters)
        self._broadcast("characters_changed", {"uid": uid})
        return _ok()

    # ── Chara Card V3 导入导出 ──

    def import_character(self, params: dict, body: dict) -> tuple[Any, int]:
        try:
            if not isinstance(body, dict):
                return _error("请求体必须是 JSON 对象")
            uid = str(body.get("uid") or "").strip()
            card = body.get("card")
            character = character_from_ccv3(card)
            if not uid:
                return _ok({"preview": character})
            if uid in self.plugin._characters and not body.get("overwrite"):
                return _error("目标 UID 已存在；请明确确认覆盖", 409)
            self.plugin._characters[uid] = character
            self._save(self.plugin._characters_path, self.plugin._characters)
            self._broadcast("characters_changed", {"uid": uid, "source": "ccv3_import"})
            return _ok({"uid": uid, "character": character})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return _error(f"角色卡导入失败: {exc}")

    def import_world(self, params: dict, body: dict) -> tuple[Any, int]:
        try:
            if not isinstance(body, dict):
                return _error("请求体必须是 JSON 对象")
            name, world = world_from_ccv3(body.get("card"))
            target_name = str(body.get("name") or name).strip()
            if target_name in self.plugin._worlds and not body.get("overwrite"):
                return _error("同名世界已存在；请明确确认覆盖", 409)
            self.plugin._worlds[target_name] = normalize_world(world, name=target_name)
            self._save(self.plugin._worlds_path, self.plugin._worlds)
            self._broadcast("worlds_changed", {"name": target_name, "source": "ccv3_import"})
            return _ok({"name": target_name, "world": self.plugin._worlds[target_name]})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return _error(f"世界卡导入失败: {exc}")

    def import_png(self, params: dict, body: dict) -> tuple[Any, int]:
        try:
            if not isinstance(body, dict):
                return _error("请求体必须是 JSON 对象")
            encoded = str(body.get("data_base64") or "")
            png_data = base64.b64decode(encoded, validate=True)
            character = character_from_png(png_data)
            uid = str(body.get("uid") or "").strip()
            if not uid:
                return _ok({"preview": character})
            if uid in self.plugin._characters and not body.get("overwrite"):
                return _error("目标 UID 已存在；PNG 导入不会自动覆盖", 409)
            self.plugin._characters[uid] = character
            self._save(self.plugin._characters_path, self.plugin._characters)
            self._broadcast("characters_changed", {"uid": uid, "source": "png_import"})
            return _ok({"uid": uid, "character": character})
        except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
            return _error(f"PNG 角色卡导入失败: {exc}")

    def export_character(self, params: dict, body: dict) -> tuple[Any, int]:
        uid = _path_param(params)
        character = self.plugin._characters.get(uid)
        return _ok(character_to_ccv3(character)) if character is not None else _error("未找到角色", 404)

    def export_world(self, params: dict, body: dict) -> tuple[Any, int]:
        name = _path_param(params)
        world = self.plugin._worlds.get(name)
        return _ok(world_to_ccv3(world, name)) if world is not None else _error("未找到世界观", 404)

    # ── 正则渲染脚本 ──

    def get_render_scripts(self, params: dict, body: dict) -> tuple[Any, int]:
        return _ok(copy.deepcopy(getattr(self.plugin, "render_scripts", [])))

    def save_render_scripts(self, params: dict, body: dict) -> tuple[Any, int]:
        scripts = body.get("scripts") if isinstance(body, dict) else None
        if not isinstance(scripts, list):
            return _error("scripts 必须是数组")
        self.plugin.render_scripts = copy.deepcopy(scripts[:50])
        self._save(self.plugin._render_scripts_path, {"scripts": self.plugin.render_scripts})
        self._broadcast("config_changed", {"render_scripts": True})
        return _ok({"scripts": self.plugin.render_scripts})

    # ── 存档 ──

    def list_saves(self, params: dict, body: dict) -> tuple[Any, int]:
        return _ok(copy.deepcopy(self.plugin._saves))

    def get_save(self, params: dict, body: dict) -> tuple[Any, int]:
        sid = _path_param(params)
        save = self.plugin._saves.get(sid)
        return _ok(copy.deepcopy(save)) if save is not None else _error("未找到存档", 404)

    def delete_save(self, params: dict, body: dict) -> tuple[Any, int]:
        sid = _path_param(params)
        self.plugin._saves.pop(sid, None)
        self._save(self.plugin._saves_path, self.plugin._saves)
        self._broadcast("saves_changed", {"id": sid})
        return _ok()

    # ── 配置 ──

    def get_config(self, params: dict, body: dict) -> tuple[Any, int]:
        p = self.plugin
        cfg = _load_schema(self.plugin_dir)
        runtime = _schema_defaults(cfg)
        runtime.update(getattr(p, "config", {}) or {})
        runtime.update(dict(
            default_theme=p.default_theme,
            single_timeout=p.single_timeout,
            group_round_time=p.group_round_time,
            group_max_players=p.group_max_players,
            kb_name=p._kb_name,
        ))
        result = copy.deepcopy(cfg)
        result.update(runtime)
        result["_worldsList"] = list(p._worlds.keys())
        result["_runtime"] = runtime
        return _ok(result)

    def save_config(self, params: dict, body: dict) -> tuple[Any, int]:
        if not isinstance(body, dict):
            return _error("请求体必须是 JSON 对象")
        p = self.plugin
        attr_map = {"default_theme": "default_theme", "single_timeout": "single_timeout",
                    "group_round_time": "group_round_time", "group_max_players": "group_max_players",
                    "lore_content_budget": "lore_content_budget", "kb_name": "_kb_name"}
        rt = {k: body[k] for k in attr_map if k in body}
        p.config = dict(getattr(p, "config", {}) or {})
        p.config.update(rt)
        for key, attr in attr_map.items():
            if key in rt:
                setattr(p, attr, rt[key])
        cfg_path = getattr(p, "_runtime_config_path", None)
        if cfg_path:
            self._save(cfg_path, rt)
        self._broadcast("config_changed", {})
        return _ok({"saved": rt})

    # ── 工具 ──

    def _save(self, path: Path, data: dict[str, Any]) -> None:
        _save_json(Path(path), data)

    def _broadcast(self, event_type: str, data: dict[str, Any]) -> None:
        try:
            from astrbot_plugin_base_webui.registry import registry
            registry.broadcast(PLUGIN_NAME, {"type": event_type, "data": data})
        except Exception:
            pass
