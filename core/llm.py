"""LLM 抽象层：让内核与 Agent 不依赖任何具体模型供应商。

- LLMClient 定义统一接口（chat）；
- DeepSeekClient 是 OpenAI 兼容的真实实现（openai 懒加载，测试时无需安装）；
- MockLLM 用脚本化响应驱动测试，让多 Agent 流程可以在零 API 成本下被完整验证；
- BudgetedLLM 把每次调用记进全局预算池，并支持中途换模型。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .budget import TokenBudget
from .utils import clip


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str = "{}"


@dataclass
class LLMResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMClient(Protocol):
    model: str

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 4000,
        temperature: float = 0.3,
    ) -> LLMResponse: ...


class DeepSeekClient:
    """OpenAI 兼容客户端（DeepSeek / 可换 base_url 指向其它兼容服务）。"""

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        import httpx  # noqa: PLC0415
        from openai import OpenAI  # 懒加载：只有真正用真实模型时才需要 openai

        # 直连：忽略系统代理环境变量（本地代理没开会导致 Connection error）
        self._client = OpenAI(api_key=api_key, base_url=base_url,
                              http_client=httpx.Client(trust_env=False))
        self.model = model

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 4000,
        temperature: float = 0.3,
    ) -> LLMResponse:
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools or None,
            tool_choice="auto" if tools else None,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        msg = resp.choices[0].message
        calls = []
        for tc in getattr(msg, "tool_calls", None) or []:
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments or "{}"))
        usage = getattr(resp, "usage", None)
        return LLMResponse(
            content=msg.content,
            tool_calls=calls,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            model=self.model,
        )


class MockLLM:
    """脚本化 LLM，用于确定性测试。

    构造时传入一串 LLMResponse（或可调用对象），每次 chat 按顺序返回一个。
    同时记录所有收到的 messages，方便断言"Agent 确实按预期提问"。
    """

    def __init__(self, script: list[LLMResponse] | None = None) -> None:
        self.model = "mock"
        self._script = list(script or [])
        self.calls: list[list[dict]] = []
        self.tools_seen: list[Any] = []

    def push(self, *responses: LLMResponse) -> None:
        self._script.extend(responses)

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 4000,
        temperature: float = 0.3,
    ) -> LLMResponse:
        self.calls.append([dict(m) for m in messages])
        self.tools_seen.append(tools)
        if not self._script:
            # 脚本耗尽：返回一个安全的空回复，避免测试挂死
            return LLMResponse(content="{}", prompt_tokens=1, completion_tokens=1, model="mock")
        item = self._script.pop(0)
        if callable(item):
            item = item(messages, tools)
        return item


class BudgetedLLM:
    """给任意 LLMClient 套一层预算记账。agent 名由每次调用传入，用于分账。"""

    def __init__(self, inner: LLMClient, budget: TokenBudget) -> None:
        self._inner = inner
        self._budget = budget
        self.model = getattr(inner, "model", "")

    @property
    def inner(self) -> LLMClient:
        return self._inner

    @property
    def budget(self) -> TokenBudget:
        """对外暴露记账用的预算对象，保证编排器与记账口径一致。"""
        return self._budget

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 4000,
        temperature: float = 0.3,
        agent: str = "unknown",
    ) -> LLMResponse:
        resp = self._inner.chat(messages, tools=tools, max_tokens=max_tokens, temperature=temperature)
        # 先记账再返回：超预算会在 add 内抛 BudgetExceeded，由编排器统一处理
        self._budget.add(agent, resp.prompt_tokens, resp.completion_tokens)
        return resp


def build_client(*, api_key: str, base_url: str, model: str, budget: TokenBudget) -> BudgetedLLM:
    """工厂：真实场景用 DeepSeek 兼容客户端，并自动套上预算记账。"""
    if not api_key:
        raise RuntimeError(
            "未找到 API Key。请在项目根目录 .env 里配置 DEEPSEEK_API_KEY（参考 .env.example）。"
        )
    return BudgetedLLM(DeepSeekClient(api_key, base_url, model), budget)


def describe(resp: LLMResponse) -> str:
    """给 trace 用的单行摘要。"""
    if resp.tool_calls:
        names = ",".join(c.name for c in resp.tool_calls)
        return f"tool_calls=[{names}] {clip(resp.content or '')}"
    return clip(resp.content or "")
