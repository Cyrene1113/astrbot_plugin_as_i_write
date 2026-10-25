"""仅作用于用户显示副本的正则渲染管线。"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)
MAX_SCRIPTS = 50
MAX_PATTERN_LENGTH = 500
MAX_TEXT_LENGTH = 100_000
MAX_OUTPUT_LENGTH = 200_000


def apply_render_scripts(text: str, scripts: list[dict], *, warning_logger: Any = None) -> str:
    """依序应用启用脚本；非法或超限规则只跳过自身。"""
    log = warning_logger or logger
    result = str(text or "")[:MAX_TEXT_LENGTH]
    source = scripts if isinstance(scripts, list) else []
    for index, script in enumerate(source[:MAX_SCRIPTS]):
        if not isinstance(script, dict) or not script.get("enabled", True):
            continue
        pattern = str(script.get("find_regex") or "")
        replacement = str(script.get("replace_string") or "")
        if not pattern or len(pattern) > MAX_PATTERN_LENGTH:
            log.warning("如我所书渲染脚本 #%s 正则为空或过长，已跳过", index + 1)
            continue
        try:
            result = re.sub(pattern, replacement, result)
        except (re.error, ValueError) as exc:
            log.warning("如我所书渲染脚本 #%s 无效，已跳过: %s", index + 1, exc)
            continue
        if len(result) > MAX_OUTPUT_LENGTH:
            log.warning("如我所书渲染输出超过长度上限，已截断")
            result = result[:MAX_OUTPUT_LENGTH]
            break
    return result
