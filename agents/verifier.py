"""Verifier Agent：确定性校验（不调用 LLM）。

这是"多 Agent 不会互相放大幻觉"的关键一环：
- 结论由代码规则给出，可复现、可测试；
- 只报告事实与严重级别，不擅自修改数据；
- 发现 error 级问题时，编排器可决定回退到 Executor 再修一轮。
"""
from __future__ import annotations

from agents.base import AgentResult, BaseAgent
from agents.rules import run_rules
from core.blackboard import Blackboard

# 未显式配置时的默认规则
DEFAULT_RULES = ["no_empty_required", "no_hallucinated_values"]


class VerifierAgent(BaseAgent):
    name = "verifier"
    description = "确定性校验写入结果与业务规则"

    def run(self) -> AgentResult:
        ctx = self.ctx
        bb: Blackboard = ctx.blackboard
        rule_names = ctx.scenario.verification_rules or DEFAULT_RULES
        findings = run_rules(rule_names, ctx, bb)

        errors = [f for f in findings if f.get("severity") == "error"]
        warnings = [f for f in findings if f.get("severity") == "warning"]

        # 兜底汇总重复结果：即使执行器没调用判重工具，报告/评测也应有确定结论
        self._ensure_duplicates_artifact()

        bb.set("verifications", findings)
        for f in errors + warnings:
            bb.add_finding(f)

        ctx.trace.add(
            self.name, "verify",
            ok=not errors,
            detail=f"{len(errors)} errors / {len(warnings)} warnings / {len(findings)} checks",
        )
        return AgentResult(
            ok=not errors,
            summary=f"校验完成：{len(errors)} 个严重问题、{len(warnings)} 个提醒",
            data={"findings": findings, "error_count": len(errors), "warning_count": len(warnings)},
        )

    def _ensure_duplicates_artifact(self) -> None:
        """确保黑板里有确定性的重复统计，供报告与评测使用。"""
        bb = self.ctx.blackboard
        if bb.get("duplicates") is not None:
            return
        plan = bb.get("plan") or {}
        keys = plan.get("key_columns") or (self.ctx.scenario.defaults.get("key_columns") or [])
        files = plan.get("target_files") or []
        if not keys or not files:
            return
        agg = {"status": "ok", "duplicate_count": 0, "duplicate_groups": []}
        from concurrent.futures import ThreadPoolExecutor

        def _check(f: str):
            return self.ctx.registry.call("find_duplicates", {"file_name": f, "key_columns": keys})

        if len(files) > 1:
            with ThreadPoolExecutor(max_workers=min(4, len(files))) as ex:
                results = list(ex.map(_check, files))
        else:
            results = [_check(f) for f in files]
        for res in results:
            if res.get("status") == "ok":
                agg["duplicate_count"] += res.get("duplicate_count", 0)
                agg["duplicate_groups"] += res.get("duplicate_groups", [])
        bb.set("duplicates", agg)
