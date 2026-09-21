"""Token 预算护栏：所有 Agent 共用同一个预算池。

多 Agent 最大的隐性风险是成本放大（每个 Agent 都调 LLM），
因此预算必须全局共享，并在超限时立即熔断，而不是等任务跑完才发现烧超。
"""
from __future__ import annotations

from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    """超出单次任务 token 预算时抛出，由编排器捕获并优雅收尾。"""


@dataclass
class Usage:
    """某一维度的用量统计（全局或单个 Agent）。"""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class TokenBudget:
    """全局 token 预算，按 Agent 分账，支持成本估算。"""

    max_total_tokens: int = 800_000
    price_in_per_m: float = 2.0
    price_out_per_m: float = 8.0
    usage: Usage = field(default_factory=Usage)
    per_agent: dict[str, Usage] = field(default_factory=dict)

    def add(self, agent: str, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        """记一次 LLM 调用的用量；超过预算立刻抛 BudgetExceeded。"""
        p = int(prompt_tokens or 0)
        c = int(completion_tokens or 0)

        self.usage.calls += 1
        self.usage.prompt_tokens += p
        self.usage.completion_tokens += c

        slot = self.per_agent.setdefault(agent, Usage())
        slot.calls += 1
        slot.prompt_tokens += p
        slot.completion_tokens += c

        if self.total_tokens > self.max_total_tokens:
            raise BudgetExceeded(
                f"已超出单次任务 token 预算（{self.total_tokens} > {self.max_total_tokens}），"
                "为避免继续消耗已停止。可调高场景配置里的 max_total_tokens。"
            )

    @property
    def total_tokens(self) -> int:
        return self.usage.total_tokens

    def remaining(self) -> int:
        return max(self.max_total_tokens - self.total_tokens, 0)

    def cost_of(self, usage: Usage) -> float:
        return round(
            usage.prompt_tokens / 1e6 * self.price_in_per_m
            + usage.completion_tokens / 1e6 * self.price_out_per_m,
            4,
        )

    def summary(self) -> dict:
        return {
            "llm_calls": self.usage.calls,
            "prompt_tokens": self.usage.prompt_tokens,
            "completion_tokens": self.usage.completion_tokens,
            "total_tokens": self.total_tokens,
            "budget": self.max_total_tokens,
            "remaining_tokens": self.remaining(),
            "estimated_cost_yuan": self.cost_of(self.usage),
            "per_agent": {
                name: {**u.to_dict(), "estimated_cost_yuan": self.cost_of(u)}
                for name, u in sorted(self.per_agent.items())
            },
        }
