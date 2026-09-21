"""单 Agent 基线：一个 LLM 循环干完所有事（模拟 v1 的做法）。

用途：作为多 Agent 的对照组，量化"分工 + 确定性校验"带来的收益。
不设 Planner/Retrieval/Verifier 的角色边界，也不做写入后的代码级校验。
"""
from __future__ import annotations

import json
import time
from typing import Any

from core.llm import BudgetedLLM
from core.utils import clip
from tools.registry import Registry

SYSTEM = """你是办公数据处理助手（单 Agent）。用可用工具完成用户任务。
规则：写操作会自动备份；不得编造数据；完成或无法完成时，用中文简要总结你实际做了什么。"""


def run_single_agent(
    task: str,
    registry: Registry,
    llm: BudgetedLLM,
    max_steps: int = 15,
) -> dict[str, Any]:
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": task},
    ]
    schemas = registry.schemas()
    tool_log: list[dict] = []
    t0 = time.time()
    final = None

    for _ in range(max_steps):
        resp = llm.chat(messages, tools=schemas, max_tokens=4000, agent="single")
        if not resp.tool_calls:
            final = resp.content
            break
        messages.append({
            "role": "assistant", "content": resp.content,
            "tool_calls": [{"id": tc.id, "type": "function",
                            "function": {"name": tc.name, "arguments": tc.arguments}}
                           for tc in resp.tool_calls],
        })
        for tc in resp.tool_calls:
            try:
                args = json.loads(tc.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = registry.call(tc.name, args)
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result, ensure_ascii=False)})
            tool_log.append({"tool": tc.name, "args": args, "result": result})

    if final is None:
        final = f"达到最大步数 {max_steps} 未收敛。"

    writes = [e["result"] for e in tool_log
              if isinstance(e["result"], dict)
              and (e["result"].get("saved") or "written_count" in e["result"])]
    diff = next((e["result"] for e in tool_log
                 if e["tool"] == "diff_tables" and isinstance(e["result"], dict)
                 and e["result"].get("status") == "ok"), None)

    return {
        "final": final,
        "tool_log": tool_log,
        "writes": writes,
        "diff": diff,
        "latency_s": round(time.time() - t0, 2),
        "tools_used": sorted({e["tool"] for e in tool_log}),
        "detail": clip(final, 200),
    }
