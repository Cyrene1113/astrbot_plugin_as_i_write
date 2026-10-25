"""核心常量、JSON 存储与冒险上下文工具。"""

from __future__ import annotations

import json
import os
import random as _random
import string
import tempfile
import time
from pathlib import Path

from astrbot.api import logger

PLUGIN_ID = "astrbot_plugin_as_i_write"
DATA_DIR = Path(os.environ.get("ASTRBOT_DATA_DIR", "data")) / "plugin_data" / PLUGIN_ID


def _ensure_within_data_dir(path: Path) -> Path:
    """拒绝越出插件数据目录的路径，防止路径穿越。"""
    root = DATA_DIR.resolve()
    resolved = Path(path).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"拒绝访问插件数据目录之外的路径: {path}")
    return Path(path)


def _load_json(path: Path) -> dict:
    _ensure_within_data_dir(Path(path))
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        corrupt_path = path.with_suffix(path.suffix + f".corrupt.{int(time.time())}")
        try:
            path.replace(corrupt_path)
            logger.warning(f"📖 如我所书: JSON 文件损坏，已备份为 {corrupt_path.name}: {exc}")
        except OSError as backup_exc:
            logger.warning(f"📖 如我所书: JSON 文件损坏且备份失败 {path}: {backup_exc}; 原因: {exc}")
        return {}
    except OSError as exc:
        logger.warning(f"📖 如我所书: 读取 JSON 失败 {path}: {exc}")
        return {}


def _save_json(path: Path, data: dict) -> None:
    path = _ensure_within_data_dir(Path(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _generate_id() -> str:
    return "".join(_random.choices(string.ascii_lowercase + string.digits, k=8))


def _strip_prefix(text: str, prefix: str) -> str:
    for fmt in [f"{prefix} ", prefix]:
        if text.startswith(fmt):
            return text[len(fmt):].strip()
    return text.strip()


def _compact_adventure_history(game_state: dict, keep_messages: int = 16) -> None:
    """压缩冒险上下文，避免长线冒险把历史无限塞进 LLM 请求和存档。"""
    history = game_state.get("history", [])
    if len(history) <= keep_messages + 2:
        return
    system_msgs = [m for m in history if m.get("role") == "system"][:1]
    dialogue = [m for m in history if m.get("role") != "system"]
    old_dialogue = dialogue[:-keep_messages]
    recent_dialogue = dialogue[-keep_messages:]
    fragments = []
    for msg in old_dialogue[-6:]:
        content = str(msg.get("content", "")).strip().replace("\n", " ")
        if content:
            fragments.append(f"{msg.get('role', 'unknown')}: {content[:160]}")
    previous_summary = str(game_state.get("summary", "")).strip()
    summary = "；".join([s for s in [previous_summary, " / ".join(fragments)] if s])[:1000]
    game_state["summary"] = summary
    if summary:
        recent_dialogue.insert(0, {"role": "system", "content": f"【此前冒险摘要】{summary}"})
    game_state["history"] = system_msgs + recent_dialogue
