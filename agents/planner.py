"""Planner Agent：把一句话需求转成可执行计划（不写任何文件）。"""
from __future__ import annotations

import json
from pathlib import Path

from agents.base import AgentResult, BaseAgent
from core.blackboard import Blackboard
from core.utils import extract_json

PLANNER_PROMPT = """你是「智能办公数据处理平台」的规划 Agent（Planner）。你的唯一职责是：理解任务、摸清数据结构、产出一份可执行计划。

场景：{scenario_name}
场景说明：{scenario_description}
规划要点：{plan_hints}

可用信息（默认参数，可能被任务覆盖）：{defaults}

工作要求：
1. 必须先用 scan_directory（必要时再用 inspect_excel / read_table）了解"有哪些文件、每个文件什么表头和 sheet"，不要凭空假设文件和列名。
2. 判断任务类型与所需字段：关键列（用于匹配/判重）和需要处理/比对的数据列。
3. 判断是否需要联网检索外部数据（本场景 retrieval_enabled={retrieval_enabled}；若为 false，不要安排联网步骤）。
4. 不要调用任何写类工具，也不要修改文件。
5. 不确定的地方如实写入 note，不要编造文件名或列名。

最后只输出一个 JSON（不要多余文字）：
{{
  "task_summary": "一句话概括任务",
  "task_type": "reconcile | fill | clean | other",
  "needs_retrieval": false,
  "target_files": ["可直接使用的文件路径列表"],
  "key_columns": ["用于匹配/判重的列名"],
  "compare_columns": ["需要比对或处理的列名"],
  "required_columns": ["清洗场景的必填列（用于检查缺失字段）；非清洗场景可留空"],
  "actions": ["按顺序描述将要执行的步骤"],
  "note": "对用户的重要提醒"
}}
示例任务：{example}
"""


class PlannerAgent(BaseAgent):
    name = "planner"
    description = "理解任务、扫描结构、生成执行计划"

    def run(self, feedback: list[dict] | None = None) -> AgentResult:
        ctx = self.ctx
        scenario = ctx.scenario
        example = scenario.task_examples[0] if scenario.task_examples else ""
        system = PLANNER_PROMPT.format(
            scenario_name=scenario.name,
            scenario_description=scenario.description,
            plan_hints=scenario.plan_hints,
            defaults=json.dumps(scenario.defaults, ensure_ascii=False),
            retrieval_enabled=str(scenario.retrieval_enabled).lower(),
            example=example,
        )

        forced = [str(f) for f in (ctx.blackboard.get("forced_files") or [])]
        user = ctx.task
        if forced:
            user += ("\n\n【本次处理范围已由界面上传确定，且仅限下列文件；"
                     "请只对这些文件调用 inspect_excel/read_table 了解结构，"
                     "不要扫描或处理其它目录、其它文件】\n"
                     + "\n".join(f"- {f}" for f in forced))
        if feedback:
            lines = [f"- [{f.get('rule')}] {f.get('message')}"
                     for f in feedback if f.get("severity") in ("error", "warning")]
            if lines:
                user += ("\n\n【上一轮执行未通过校验，请针对性修订计划，不要重复同样的做法】\n"
                         + "\n".join(lines[:20]))

        content, tool_log = self.tool_loop(
            system, user,
            tool_names=["scan_directory", "inspect_excel", "read_table", "list_sheets"],
            max_tokens=2500,
        )

        plan = extract_json(content or "") or {}
        discovered = self._collect_discovered_files(tool_log)
        if forced:
            # 上传即锁定：无论模型怎么想，目标文件只能是界面上传的那些
            files = forced
        else:
            # 尊重用户对处理范围的限定：计划里若已指定目标文件，就不擅自扩大到扫描到的全部文件
            planned = list(plan.get("target_files") or [])
            files = planned or discovered
        plan["target_files"] = files

        # 补齐缺省字段，保证下游稳定
        plan.setdefault("task_summary", ctx.task)
        plan.setdefault("task_type", "other")
        plan.setdefault("needs_retrieval", bool(scenario.retrieval_enabled))
        plan.setdefault("key_columns", list(scenario.defaults.get("key_columns") or []))
        plan.setdefault("compare_columns", list(scenario.defaults.get("compare_columns") or []))
        plan.setdefault("required_columns", list(scenario.defaults.get("required_columns") or []))
        plan.setdefault("actions", [])
        plan.setdefault("note", "")

        # 只读工具拿到的结构摘要存黑板，供后续 Agent 复用，避免重复扫描
        ctx.blackboard.set("plan", plan)
        ctx.blackboard.set("files", files)
        ctx.blackboard.set("schema", self._collect_schema(tool_log))

        return AgentResult(
            ok=bool(files),
            summary=f"计划生成：{len(files)} 个目标文件，任务类型={plan['task_type']}",
            data=plan,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _collect_discovered_files(tool_log: list[dict]) -> list[str]:
        """从 scan_directory 的结果里提取真实文件路径（优先绝对/相对可用路径）。"""
        files: list[str] = []
        for entry in tool_log:
            if entry["tool"] != "scan_directory":
                continue
            res = entry.get("result") or {}
            for f in res.get("files", []):
                if "error" in f:
                    continue
                fp = f.get("file_path") or f.get("file")
                if fp and fp not in files:
                    files.append(fp)
        return files

    @staticmethod
    def _collect_schema(tool_log: list[dict]) -> dict:
        schema: dict[str, dict] = {}
        for entry in tool_log:
            if entry["tool"] not in ("inspect_excel", "read_table", "scan_directory"):
                continue
            res = entry.get("result") or {}
            if "error" in res:
                continue
            f = res.get("file")
            if f:
                schema[Path(f).name] = {
                    "sheets": res.get("sheets") or res.get("sheet"),
                    "header": res.get("header"),
                }
        return schema
