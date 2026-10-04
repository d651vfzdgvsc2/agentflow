"""结果文件收集：把一次运行最终产出的 Excel 找出来，供「下载结果文件」使用。

只做一件事：从黑板里定位处理后的文件（批量写入的、或合并补齐生成的），
去重、只保留真实存在的文件。不做打包、不塞报告/轨迹——
用户要的就是"把我处理好的那张表还给我"。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import config


def _resolve(fp: str) -> Path:
    p = Path(fp)
    return p if p.is_absolute() else (config.DATA_DIR / p)


def _iter_write_files(writes: list[dict] | None) -> list[str]:
    """从 writes 里提取所有被写入的文件路径（兼容 batch_fill / batch_fill_many）。"""
    files: list[str] = []
    for w in writes or []:
        entries = w.get("files") if isinstance(w.get("files"), list) else [w]
        for item in entries:
            if isinstance(item, dict) and item.get("file") and item["file"] not in files:
                files.append(item["file"])
    return files


def result_files(bb: Any) -> list[Path]:
    """返回本次运行产生的结果文件（去重、存在的），供直接下载。"""
    get = bb.get if hasattr(bb, "get") else (lambda k, d=None: (bb or {}).get(k, d))

    out: list[Path] = []
    for fp in _iter_write_files(get("writes")):
        p = _resolve(fp)
        if p.exists() and p.suffix.lower() in {".xlsx", ".xlsm"} and p not in out:
            out.append(p)

    # 合并补齐生成的完整表（如有）
    merged = get("merged") or {}
    mp = merged.get("output") if isinstance(merged, dict) else None
    if mp:
        p = Path(mp)
        if p.exists() and p not in out:
            out.append(p)
    return out
