"""Reporter Agent：汇总黑板与轨迹，生成可交付的执行报告。

报告必须可追溯：结论、差异明细、每条数据的来源、以及本次运行的 token/成本分账，
全部来自黑板与轨迹的真实记录，不允许"美化作答"。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import config
from agents.base import AgentResult, BaseAgent
from core.blackboard import Blackboard
from core.budget import BudgetExceeded

SUMMARY_PROMPT = """你是报告 Agent。请用 3 句话以内概括这次数据处理任务的结果，要求：
- 只依据给定的事实，不得添加未出现的信息；
- 先说结论（完成/部分完成/失败），再说关键数字，最后说有风险或需人工确认的点；
- 注意：核对(reconcile)与清洗(clean)类任务的主要产物是差异结论与报告，不写文件属于正常，
  不要因为没有写文件就判定为"未完成"或"未落盘"。
只输出这段概括文字。"""


class ReporterAgent(BaseAgent):
    name = "reporter"
    description = "生成执行报告"

    def run(self, use_llm: bool = True) -> AgentResult:
        ctx = self.ctx
        bb: Blackboard = ctx.blackboard

        exec_summary = self._executive_summary(bb) if use_llm else self._fallback_summary(self._facts(bb))
        content = self._render(bb, exec_summary)

        out_dir = config.DATA_DIR / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "report.md"
        path.write_text(content, encoding="utf-8")
        bb.set("report", {"path": str(path), "summary": exec_summary})

        return AgentResult(ok=True, summary=f"报告已生成: {path}", data={"path": str(path)})

    # ------------------------------------------------------------------
    @staticmethod
    def _facts(bb: Blackboard) -> dict:
        verifications = bb.get("verifications") or []
        errors = [f for f in verifications if f.get("severity") == "error"]
        warnings = [f for f in verifications if f.get("severity") == "warning"]
        return {
            "task": bb.get("task"),
            "status": bb.get("status"),
            "files": len(bb.get("plan", {}).get("target_files", []) or []),
            "errors": len(errors),
            "warnings": len(warnings),
            "diff": (bb.get("diff") or {}).get("diff_count") if bb.get("diff") else None,
            "written_files": len([w for w in (bb.get("writes") or []) if w.get("saved")]),
        }

    def _executive_summary(self, bb: Blackboard) -> str:
        """尝试让 LLM 写摘要；预算超限或失败时退回确定性摘要。"""
        facts = self._facts(bb)
        try:
            resp = self.ctx.llm.chat(
                [{"role": "system", "content": SUMMARY_PROMPT},
                 {"role": "user", "content": f"事实：{facts}"}],
                max_tokens=300, agent=self.name,
            )
            if resp.content:
                return resp.content.strip()
        except BudgetExceeded:
            pass
        except Exception:  # noqa: BLE001
            pass
        return self._fallback_summary(facts)

    @staticmethod
    def _fallback_summary(facts: dict) -> str:
        status = facts.get("status") or "未知"
        parts = [f"任务状态：{status}。"]
        if facts.get("diff") is not None:
            parts.append(f"核对发现 {facts['diff']} 处差异。")
        if facts.get("written_files"):
            parts.append(f"已写入 {facts['written_files']} 个文件。")
        parts.append(f"校验问题：{facts.get('errors', 0)} 个严重、{facts.get('warnings', 0)} 个提醒。")
        return "".join(parts)
    # ------------------------------------------------------------------
    def _render(self, bb: Blackboard, exec_summary: str) -> str:
        ctx = self.ctx
        lines: list[str] = []
        title = ctx.scenario.report_title
        lines.append(f"# {title}")
        lines.append("")
        lines.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"- 用户任务：{bb.get('task')}")
        lines.append(f"- 场景：{ctx.scenario.name}（{ctx.scenario.id}）")
        lines.append(f"- 运行状态：{bb.get('status')}")
        lines.append("")
        lines.append("## 执行摘要")
        lines.append("")
        lines.append(exec_summary)
        lines.append("")

        # 计划
        plan = bb.get("plan") or {}
        lines.append("## 执行计划")
        lines.append("")
        lines.append(f"- 任务类型：{plan.get('task_type')}")
        lines.append(f"- 目标文件：{len(plan.get('target_files') or [])} 个")
        lines.append(f"- 关键列：{plan.get('key_columns') or '—'}")
        lines.append(f"- 比对列：{plan.get('compare_columns') or '—'}")
        if plan.get("actions"):
            lines.append("- 步骤：")
            for a in plan["actions"]:
                lines.append(f"  1. {a}")
        lines.append("")

        # 核对差异
        diff = bb.get("diff")
        if diff:
            lines.append("## 核对差异")
            lines.append("")
            lines.append(f"- 左表 {diff['left']['file']}（{diff['left']['rows']} 行）")
            lines.append(f"- 右表 {diff['right']['file']}（{diff['right']['rows']} 行）")
            lines.append(f"- 差异合计：{diff['diff_count']}")
            lines.append(f"  - 仅左表存在：{len(diff['only_in_left'])}")
            lines.append(f"  - 仅右表存在：{len(diff['only_in_right'])}")
            lines.append(f"  - 字段不一致：{len(diff['value_mismatches'])}")
            lines.append(f"  - 重复关键值：左 {len(diff['duplicate_keys_left'])} / 右 {len(diff['duplicate_keys_right'])}")
            lines.append("")
            if diff["only_in_left"][:20]:
                lines.append("仅左表存在的关键值（前 20）：")
                for k in diff["only_in_left"][:20]:
                    lines.append(f"- {k}")
                lines.append("")
            if diff["value_mismatches"][:20]:
                lines.append("字段不一致明细（前 20）：")
                lines.append("")
                lines.append("| 关键值 | 字段 | 左表 | 右表 |")
                lines.append("|---|---|---|---|")
                for m in diff["value_mismatches"][:20]:
                    lines.append(f"| {m['key']} | {m['column']} | {m['left']} | {m['right']} |")
                lines.append("")

        # 写入统计
        writes = bb.get("writes") or []
        if writes:
            saved = [w for w in writes if w.get("saved")]
            total_written = sum(w.get("written_count", 0) for w in writes)
            total_filled = sum(w.get("filled_count", 0) for w in writes)
            lines.append("## 写入统计")
            lines.append("")
            lines.append(f"- 成功文件数：{len(saved)}")
            lines.append(f"- 写入单元格/行：{total_written + total_filled}")
            lines.append("")

        # 校验结果
        lines.append("## 校验结果")
        lines.append("")
        verifications = bb.get("verifications") or []
        errors = [f for f in verifications if f.get("severity") == "error"]
        warnings = [f for f in verifications if f.get("severity") == "warning"]
        infos = [f for f in verifications if f.get("severity") == "info"]
        lines.append(f"- 严重问题：{len(errors)}")
        for f in errors[:30]:
            lines.append(f"  - [{f['rule']}] {f['message']}")
        lines.append(f"- 提醒：{len(warnings)}")
        for f in warnings[:30]:
            lines.append(f"  - [{f['rule']}] {f['message']}")
        lines.append(f"- 通过项：{len(infos)}")
        for f in infos[:20]:
            lines.append(f"  - [{f['rule']}] {f['message']}")
        lines.append("")

        # 数据来源
        collected = bb.get("collected") or []
        if collected:
            lines.append("## 数据来源（溯源）")
            lines.append("")
            lines.append("| 条目 | 值 | 来源 | 置信度 |")
            lines.append("|---|---|---|---|")
            for c in collected[:50]:
                lines.append(f"| {c['item']} | {c['value']} | {c['source']} | {c.get('confidence')} |")
            lines.append("")

        # 成本分账
        usage = ctx.budget.summary()
        lines.append("## Token 用量与成本")
        lines.append("")
        lines.append(f"- 合计：{usage['total_tokens']:,} tokens / 预算 {usage['budget']:,}"
                     f"（LLM 调用 {usage['llm_calls']} 次，估算 ¥{usage['estimated_cost_yuan']}）")
        lines.append("")
        lines.append("| Agent | 调用 | tokens | 估算成本(元) |")
        lines.append("|---|---|---|---|")
        for name, u in usage["per_agent"].items():
            lines.append(f"| {name} | {u['calls']} | {u['total_tokens']:,} | {u['estimated_cost_yuan']} |")
        lines.append("")

        # 轨迹
        lines.append("## 执行轨迹")
        lines.append("")
        for s in ctx.trace.steps:
            mark = "" if s.ok else " [失败]"
            tool = f" `{s.tool}`" if s.tool else ""
            lines.append(f"{s.seq}. [{s.agent}]{tool}{mark} {s.detail}")
        lines.append("")

        # 未解决
        errors_bb = bb.get("errors") or []
        if errors_bb:
            lines.append("## 运行期错误")
            lines.append("")
            for e in errors_bb[:50]:
                lines.append(f"- {e.get('where')}: {e.get('error')}")
            lines.append("")

        return "\n".join(lines)
