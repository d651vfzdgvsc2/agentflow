"""Retrieval Agent：为"填报"类任务收集外部数据，且每条数据必须带来源。

原则（沿用 v1 的诚实底线）：
- 检索不到就标 not_found，绝不编造；
- 每条值都记录 source 与 confidence，供 Verifier 的幻觉检查使用；
- 内置参考数据只有在用户明确选择时才使用，且来源标注为"内置参考数据(非实时)"。
"""
from __future__ import annotations

import json

from agents.base import AgentResult, BaseAgent
from core.blackboard import Blackboard
from core.utils import extract_json

RETRIEVAL_PROMPT = """你是数据检索 Agent（Retrieval）。任务是为填报任务收集每个条目的数据，并且每一条都必须可溯源。

场景：{scenario_name}
需要填写的数据列：{value_header}
要收集的条目（共 {count} 条）：
{items}

工作方式：
1. 先 web_search 找候选页，再用 fetch_web 打开页面读正文/数字（搜索结果页通常没有具体数值）。
2. 若 web_search 返回 unavailable，不要反复无效重试：标注 available=false 后停止联网。
{reference_hint}
3. 严禁编造：查不到或不确定的条目，value 填 null，并在 source 里写明原因。
4. 只输出一个 JSON，不要多余文字：
{{
  "available": true,
  "values": {{
    "条目名": {{"value": 数字或null, "source": "来源URL或说明", "confidence": 0到1的小数}}
  }}
}}
"""


class RetrievalAgent(BaseAgent):
    name = "retrieval"
    description = "联网收集外部数据（带来源）"

    def run(self) -> AgentResult:
        ctx = self.ctx
        bb = ctx.blackboard
        items = self._collect_items(bb)
        if not items:
            bb.set("collected", [])
            return AgentResult(ok=True, summary="无可检索条目，跳过检索", data={"values": {}})

        allow_reference = bool((bb.get("approval") or {}).get("use_reference"))
        value_header = (
            ctx.scenario.defaults.get("price_header")
            or (ctx.scenario.compat or {}).get("price_header")
            or "目标列"
        )
        reference_hint = (
            "5. 用户已授权使用内置参考数据：可调用 reference_price 兜底，但 source 必须标注为『内置参考数据(非实时)』。"
            if allow_reference else
            "5. 用户未授权使用内置参考数据：不得调用 reference_price，查不到就标 null。"
        )

        system = RETRIEVAL_PROMPT.format(
            scenario_name=ctx.scenario.name,
            value_header=value_header,
            count=len(items),
            items="\n".join(f"- {x}" for x in items),
            reference_hint=reference_hint,
        )
        content, _ = self.tool_loop(
            system, f"请为以下条目收集【{value_header}】的数据：\n" + "\n".join(items),
            tool_names=["web_search", "fetch_web", "reference_price"],
            max_tokens=4000,
        )
        parsed = extract_json(content or "") or {}
        values = parsed.get("values") or {}
        available = bool(parsed.get("available", True))

        # 确定性兜底：联网没拿到、且用户已授权参考数据时，由代码补齐
        # （不依赖 LLM 是否记得调用 reference_price，保证结果可复现）
        if allow_reference:
            for item in items:
                entry = values.get(item) or {}
                if entry.get("value") is None:
                    res = ctx.registry.call("reference_price", {"item": item})
                    if res.get("status") == "ok":
                        ctx.trace.add(self.name, "tool", "reference_price", ok=True,
                                      detail=json.dumps({"item": item, "price": res.get("price")}, ensure_ascii=False))
                        values[item] = {"value": res["price"],
                                        "source": "内置参考数据(非实时)", "confidence": 1.0}

        # 写入黑板（带来源），并标记未检索到的条目
        collected = []
        for item in items:
            entry = values.get(item) or {}
            val = entry.get("value")
            source = entry.get("source") or "未检索到"
            conf = entry.get("confidence", 1.0 if val is not None else 0.0)
            if val is None:
                bb.add_finding({"type": "not_found", "item": item, "detail": f"未检索到可靠数据（{source}）"})
            bb.add_source(item, val, source, float(conf) if conf is not None else 0.0)
            collected.append({"item": item, "value": val, "source": source, "confidence": conf})
        bb.set("collected", collected)

        found = sum(1 for c in collected if c["value"] is not None)
        return AgentResult(
            ok=available or found > 0,
            summary=f"检索完成：{found}/{len(items)} 条获得数据，来源可溯源",
            data={"values": {c["item"]: c["value"] for c in collected}, "available": available},
        )

    # ------------------------------------------------------------------
    def _collect_items(self, bb: Blackboard) -> list[str]:
        """确定需要检索的条目：优先用计划里的关键列，从目标表读取去重后的值。"""
        ctx = self.ctx
        plan = bb.get("plan") or {}
        files = plan.get("target_files") or []
        key_cols = plan.get("key_columns") or []
        item_header = (
            (key_cols[0] if key_cols else None)
            or (ctx.scenario.compat or {}).get("item_header")
            or ctx.scenario.defaults.get("item_header")
        )
        seen: list[str] = []
        for f in files[:5]:  # 控制读取范围，避免一次读太多文件
            res = ctx.registry.call("read_table", {"file_name": f})
            if "error" in res:
                continue
            header = res.get("header") or []
            col = item_header if item_header in header else (header[0] if header else None)
            if not col:
                continue
            for rec in res.get("records", []):
                v = rec.get(col)
                if v is None:
                    continue
                s = str(v).strip()
                if s and s not in seen:
                    seen.append(s)
        return seen
