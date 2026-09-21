"""Agent 基类：统一上下文、结果结构与"工具调用循环"。

所有 Agent 共享同一个 ctx（黑板 / 轨迹 / 预算 / 注册表），
彼此不直接调用，只通过黑板交换状态——这是多 Agent 低耦合的关键。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from core.blackboard import Blackboard
from core.budget import TokenBudget
from core.llm import BudgetedLLM, LLMResponse
from core.trace import Trace
from core.utils import clip
from scenarios.loader import Scenario
from tools.registry import Registry


@dataclass
class AppConfig:
    """运行时参数（非场景相关）。"""

    max_steps_per_agent: int = 12
    max_total_tokens: int = 800_000
    price_in_per_m: float = 2.0
    price_out_per_m: float = 8.0
    dry_run: bool = False  # True 时禁止任何写操作（用于只读试跑/评测）
    # 校验失败时最多"回到规划重新想办法"的轮数
    max_replan_rounds: int = 1
    # 多文件并行处理时的线程数
    parallel_workers: int = 4


@dataclass
class AgentContext:
    task: str
    scenario: Scenario
    registry: Registry
    blackboard: Blackboard
    trace: Trace
    llm: BudgetedLLM
    budget: TokenBudget
    config: AppConfig
    # 人工审批回调：返回 True 才允许写操作；None 表示无需审批（非交互）
    approve_write: Any = None


@dataclass
class AgentResult:
    ok: bool = True
    summary: str = ""
    data: dict = field(default_factory=dict)


class AgentError(RuntimeError):
    """Agent 内部致命错误（非工具级错误）。"""


class BaseAgent:
    name = "base"
    description = ""

    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------
    # 工具调用循环：给定系统提示 + 用户消息 + 可见工具白名单，跑到 LLM 不再调工具为止
    # ------------------------------------------------------------------
    def tool_loop(
        self,
        system: str,
        user: str,
        tool_names: list[str],
        max_steps: int | None = None,
        max_tokens: int = 4000,
        temperature: float = 0.3,
        on_tool: Any = None,
    ) -> tuple[str | None, list[dict]]:
        ctx = self.ctx
        steps = max_steps or ctx.config.max_steps_per_agent
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        schemas = ctx.registry.schemas(only=tool_names)
        tool_log: list[dict] = []

        for _ in range(steps):
            t0 = time.time()
            resp: LLMResponse = ctx.llm.chat(
                messages, tools=schemas, max_tokens=max_tokens,
                temperature=temperature, agent=self.name,
            )
            ctx.trace.add(
                self.name, "llm", detail=clip(resp.content or ""),
                tokens=resp.total_tokens, latency_ms=int((time.time() - t0) * 1000),
            )
            if not resp.tool_calls:
                return resp.content, tool_log

            messages.append({
                "role": "assistant",
                "content": resp.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.name, "arguments": tc.arguments}}
                    for tc in resp.tool_calls
                ],
            })
            for tc in resp.tool_calls:
                args = self._parse_args(tc.arguments)
                # 写操作需要审批（dry-run 直接拒绝）
                if self._is_write(tc.name):
                    allowed = self._approve(tc.name, args)
                    if not allowed:
                        result = {"error": "写操作被拒绝（dry-run 或未通过人工审批）"}
                        ctx.trace.add(self.name, "tool", tc.name, ok=False, detail=result)
                        messages.append({"role": "tool", "tool_call_id": tc.id,
                                         "content": json.dumps(result, ensure_ascii=False)})
                        tool_log.append({"tool": tc.name, "args": args, "result": result})
                        continue

                t1 = time.time()
                result = ctx.registry.call(tc.name, args)
                ok = not (isinstance(result, dict) and result.get("error"))
                ctx.trace.add(
                    self.name, "tool", tc.name, ok=ok,
                    detail=clip(result), latency_ms=int((time.time() - t1) * 1000),
                )
                if on_tool:
                    try:
                        on_tool(tc.name, args, result)
                    except Exception:  # noqa: BLE001
                        pass
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps(result, ensure_ascii=False)})
                tool_log.append({"tool": tc.name, "args": args, "result": result})
        return None, tool_log

    # ------------------------------------------------------------------
    def _parse_args(self, raw: str) -> dict:
        try:
            return json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}

    def _is_write(self, tool: str) -> bool:
        return tool in self.ctx.registry.write_tools()

    def _approve(self, tool: str, args: dict) -> bool:
        if self.ctx.config.dry_run:
            return False
        cb = self.ctx.approve_write
        if cb is None:
            return True
        try:
            return bool(cb(tool, args))
        except Exception:  # noqa: BLE001
            return False

    def run(self, **kwargs: Any) -> AgentResult:  # pragma: no cover - 抽象
        raise NotImplementedError
