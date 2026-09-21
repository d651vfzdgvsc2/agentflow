"""通用小工具：严格 JSON 提取、文本截断等。"""
from __future__ import annotations

import json
from typing import Any


def extract_json(text: str) -> dict | None:
    """从 LLM 输出里尽最大努力解析出 JSON 对象。

    容忍 markdown 代码块、前后说明文字、尾部截断（逐步回退找合法边界）。
    全部失败返回 None，由调用方决定兜底策略。
    """
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]

    # 直接解析
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    start = t.find("{")
    if start == -1:
        return None
    end = t.rfind("}")
    if end <= start:
        return None

    # 优先完整边界
    try:
        obj = json.loads(t[start:end + 1])
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # 尾部截断时逐步回退
    for cut in range(end, start, -1):
        try:
            obj = json.loads(t[start:cut + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def clip(text: Any, limit: int = 300) -> str:
    """把任意对象压成不超过 limit 的字符串，用于 trace / 日志。"""
    if not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(text)
    return text if len(text) <= limit else text[:limit] + "…"
