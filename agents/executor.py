"""Executor Agent：按计划执行写操作（复用 v1 的 Excel 工具链）。

可靠性设计：
- 只允许写入检索到的、带来源的值；
- 优先 batch_fill 一次写完一张 sheet（省 token）；
- 若 LLM 循环没有真正写入，编排器会调用本 Agent 的确定性兜底 deterministic_write()，
  用代码直接完成写入，保证"任务不会因为模型犯懒而空转"。
"""
from __future__ import annotations

import json

from agents.base import AgentResult, BaseAgent
from core.blackboard import Blackboard
from core.utils import clip

EXECUTOR_PROMPT = """你是执行 Agent（Executor）。用户已确认计划，现在开始执行。

场景：{scenario_name}
执行计划：
{plan}
{mode_hint}

可用数据（key → value，来自检索，均带来源；value 为 null 表示未检索到，禁止填写）：
{data_map}

目标文件：
{files}

规则：
{rules}
"""

WRITE_RULES = """1. 写操作会自动备份，无需担心；但不要写 null 或不确定的值。
2. 优先用 batch_fill 一次填完整张 sheet（参数 price_map 传 {"条目": 值} 映射），不要逐单元格写。
3. 若需要写入的文件较多（多个文件），改用 batch_fill_many 一次并行处理（参数 file_names 传文件数组），比逐个文件调用更快。
4. 表格里的列名与配置的别名不一致时，先用 read_table/inspect_excel 确认真实表头，再决定写入方式。
5. 单个文件失败不要中断，继续处理下一个；如实记录失败原因。
6. 只有当确实调用了写工具并成功后，才能在总结里说"已写入"；否则如实说明未写入。
7. 完成后简要总结：写了哪些文件/多少行、跳过了哪些、原因。"""

READ_RULES = """1. 本场景只读：系统未开放任何写类工具，禁止修改数据文件。
2. 必须调用 diff_tables 做确定性比对，参数：left_file / right_file / key_columns / compare_columns。
3. 如需判重，调用 find_duplicates（file_name / key_columns）。
4. 严禁自己心算差异，也不要把差异写进数据文件；差异结果由系统汇总生成报告。
5. 完成后简要说明：调用了哪些比对工具、比对了哪些文件。"""

CLEAN_HINT = """本场景为数据清洗：补全只能补空缺、不能覆盖已有值；补全来源必须可追溯；重复记录只标记不擅自删除。"""

CLEAN_RULES = """1. 本场景只允许「补空缺」：仅能用 fill_missing，把已有可追溯来源的值填进空单元格，严禁覆盖已有值。
2. 严禁新增列/新增行，严禁把报告、标记、说明写进数据表——清洗结论由系统单独生成（report.md）。
3. 找不到可追溯来源的缺失项，一律不填、不猜，留给报告。
4. 完成后简要说明：发现多少重复、多少缺失、补了多少。"""

RECONCILE_HINT = """本场景为核对/对账：只做确定性比对，不修改任何数据。"""


class ExecutorAgent(BaseAgent):
    name = "executor"
    description = "按计划写入表格 / 执行确定性比对"

    def run(self) -> AgentResult:
        ctx = self.ctx
        bb = ctx.blackboard
        plan = bb.get("plan") or {}
        files = plan.get("target_files") or []
        collected = bb.get("collected") or []
        task_type = plan.get("task_type", "other")
        writes_data = ctx.scenario.writes_data

        # 只把"有值"的条目交给执行器
        data_map = {c["item"]: c["value"] for c in collected if c.get("value") is not None}
        if not writes_data:
            mode_hint = RECONCILE_HINT
            rules = READ_RULES
            tool_names = ["inspect_excel", "read_table", "find_rows", "list_sheets",
                          "diff_tables", "find_duplicates"]
        elif task_type == "clean":
            # 清洗只读 + 仅允许 fill_missing：杜绝把"报告/标记"写回数据表造成污染
            mode_hint = CLEAN_HINT
            rules = CLEAN_RULES
            tool_names = ["inspect_excel", "read_table", "find_rows", "list_sheets",
                          "find_duplicates", "fill_missing", "verify_excel"]
        else:
            mode_hint = ""
            rules = WRITE_RULES
            tool_names = [
                "inspect_excel", "read_table", "find_rows", "list_sheets", "verify_excel",
                "find_duplicates",
                "backup_excel", "save_excel",
                "batch_fill", "batch_fill_many", "write_excel_cell", "write_excel_range",
                "append_excel_row", "fill_missing",
            ]

        system = EXECUTOR_PROMPT.format(
            scenario_name=ctx.scenario.name,
            plan=json.dumps(plan, ensure_ascii=False, indent=2),
            mode_hint=mode_hint,
            data_map=json.dumps(data_map, ensure_ascii=False, indent=2) if data_map else "（无外部数据，按计划直接操作表格）",
            files="\n".join(f"- {f}" for f in files) or "（未发现文件）",
            rules=rules,
        )

        content, tool_log = self.tool_loop(
            system,
            f"请执行计划。任务原文：{ctx.task}",
            tool_names=tool_names,
            max_tokens=4000,
        )

        writes = self._collect_writes(tool_log)
        artifacts = self._collect_artifacts(tool_log)
        if "diff" in artifacts:
            bb.set("diff", artifacts["diff"])
        if "duplicates" in artifacts:
            bb.set("duplicates", artifacts["duplicates"])
        bb.set("writes", writes)
        has_write = any(w.get("saved") for w in writes)
        # 核对类任务不一定写文件：只要产出了差异结果也算成功
        ok = has_write or bool(artifacts)
        return AgentResult(
            ok=ok,
            summary=(content or "执行结束")[:500],
            data={"writes": writes, "has_write": has_write, "artifacts": artifacts, "tool_log": tool_log},
        )

    # ------------------------------------------------------------------
    def deterministic_write(self) -> AgentResult:
        """确定性兜底：用黑板里已收集的 key→value，并行 batch_fill 所有目标文件。"""
        ctx = self.ctx
        bb: Blackboard = ctx.blackboard
        plan = bb.get("plan") or {}
        files = plan.get("target_files") or []
        collected = bb.get("collected") or []
        data_map = {c["item"]: c["value"] for c in collected if c.get("value") is not None}
        if not data_map:
            return AgentResult(ok=False, summary="兜底跳过：没有可写入的数据", data={"writes": []})

        res = ctx.registry.call("batch_fill_many", {
            "file_names": files,
            "price_map": data_map,
            "workers": ctx.config.parallel_workers,
        })
        ctx.trace.add(self.name, "tool", "batch_fill_many", ok=not res.get("error"),
                      detail=clip({"files": len(files), "written": res.get("written_count"),
                                   "workers": res.get("workers")}))
        writes = res.get("files") or []
        if not writes:
            writes = [{"file": f, "error": res.get("error", "未知错误"), "saved": False} for f in files]
        bb.set("writes", writes)
        ok = any(w.get("saved") for w in writes)
        return AgentResult(
            ok=ok,
            summary=(f"确定性兜底完成：并行处理 {len(files)} 个文件"
                     f"（{res.get('workers')} 线程），写入 {res.get('written_count', 0)} 项"),
            data={"writes": writes, "has_write": ok},
        )

    @staticmethod
    def _collect_writes(tool_log: list[dict]) -> list[dict]:
        out = []
        for entry in tool_log:
            res = entry.get("result") or {}
            if not isinstance(res, dict):
                continue
            if res.get("saved") or "written_count" in res or "written_cells" in res or "appended_row" in res:
                out.append(res)
        return out

    @staticmethod
    def _collect_artifacts(tool_log: list[dict]) -> dict:
        """收集"非写入类产物"，例如核对比对结果。"""
        artifacts: dict = {}
        for entry in tool_log:
            res = entry.get("result") or {}
            if not isinstance(res, dict):
                continue
            if entry["tool"] == "diff_tables" and res.get("status") == "ok":
                artifacts["diff"] = res
            if entry["tool"] == "find_duplicates" and res.get("status") == "ok":
                artifacts["duplicates"] = res
        return artifacts
