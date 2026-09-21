"""执行轨迹：记录每一步"谁、用什么工具、参数、结果、花了多少 token / 多少毫秒"。

轨迹既是可观测性的数据源（WebUI 面板 / 成本分账），
也是可回放、可评测的原始素材（评测脚本直接消费 trace）。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class Step:
    seq: int
    agent: str
    action: str
    tool: str = ""
    ok: bool = True
    detail: str = ""
    tokens: int = 0
    latency_ms: int = 0
    retries: int = 0
    ts: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))

    def brief(self) -> dict:
        d = asdict(self)
        # 只给 UI 传摘要，避免把大对象塞进事件流
        if len(d["detail"]) > 300:
            d["detail"] = d["detail"][:300]
        return d


class Trace:
    """轨迹容器。on_event 用于把每一步实时推给 WebUI。"""

    def __init__(self, on_event: Callable[[dict], None] | None = None) -> None:
        self.steps: list[Step] = []
        self._on_event = on_event
        self._seq = 0

    def add(
        self,
        agent: str,
        action: str,
        tool: str = "",
        ok: bool = True,
        detail: Any = "",
        tokens: int = 0,
        latency_ms: int = 0,
        retries: int = 0,
    ) -> Step:
        self._seq += 1
        if not isinstance(detail, str):
            try:
                detail = json.dumps(detail, ensure_ascii=False)
            except (TypeError, ValueError):
                detail = str(detail)
        step = Step(self._seq, agent, action, tool, ok, detail, tokens, latency_ms, retries)
        self.steps.append(step)
        if self._on_event:
            try:
                self._on_event(step.brief())
            except Exception:  # noqa: BLE001
                pass  # UI 推送失败绝不影响主流程
        return step

    # ---- 统计 ----
    @property
    def tool_calls(self) -> int:
        return sum(1 for s in self.steps if s.tool)

    @property
    def failures(self) -> int:
        return sum(1 for s in self.steps if not s.ok)

    def tools_used(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.steps:
            if s.tool:
                out[s.tool] = out.get(s.tool, 0) + 1
        return out

    def to_list(self) -> list[dict]:
        return [asdict(s) for s in self.steps]

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_list(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path
