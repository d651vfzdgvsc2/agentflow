"""共享黑板（Blackboard）：多 Agent 之间唯一的状态交换通道。

设计要点：
- Agent 之间不直接互相调用、不互相传参，只读写黑板，降低耦合；
- 黑板可整体序列化成 JSON，因此每次运行都能落盘、回放、做评测；
- 数据来源（source）与置信度随值一起记录，为"禁止编造数据"提供依据。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# 黑板的标准分区（允许扩展，但这些是编排器依赖的）
SECTIONS = (
    "task",           # 用户任务原文
    "scenario",       # 场景 id
    "status",         # 运行状态
    "plan",           # Planner 产出的计划
    "approval",       # 人工审批结果
    "files",          # 发现的文件清单
    "schema",         # 各文件结构摘要
    "collected",      # Retrieval 收集到的数据（带 source）
    "writes",         # Executor 的写入记录
    "verifications",  # Verifier 的校验结果
    "findings",       # 问题清单（缺项/差异/异常）
    "errors",         # 错误记录
    "report",         # 最终报告路径/内容
)


class Blackboard:
    """键值型共享状态，带标准分区与序列化能力。"""

    def __init__(self, task: str = "", scenario: str = "") -> None:
        self._data: dict[str, Any] = {k: None for k in SECTIONS}
        self._data["task"] = task
        self._data["scenario"] = scenario
        self._data["status"] = "created"
        self._data["files"] = []
        self._data["collected"] = []
        self._data["writes"] = []
        self._data["verifications"] = []
        self._data["findings"] = []
        self._data["errors"] = []

    # ---- 基础访问 ----
    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def update(self, **kwargs: Any) -> None:
        self._data.update(kwargs)

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    # ---- 分区追加助手（避免各处手写 append 逻辑）----
    def add_finding(self, finding: dict) -> None:
        self._data["findings"].append(finding)

    def add_error(self, where: str, error: str) -> None:
        self._data["errors"].append({"where": where, "error": error})

    def add_source(self, item: str, value: Any, source: str, confidence: float = 1.0) -> None:
        """记录一条"带来源"的数据，用于溯源与幻觉检查。"""
        self._data["collected"].append({
            "item": item,
            "value": value,
            "source": source,
            "confidence": confidence,
        })

    # ---- 序列化 ----
    def snapshot(self) -> dict:
        return json.loads(json.dumps(self._data, ensure_ascii=False, default=str))

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return path
