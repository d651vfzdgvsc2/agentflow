"""编排器：显式状态机调度多个 Agent，全流程可观测、可回放、有预算护栏。

状态流转：
  planning → (awaiting_approval) → retrieval? → execution
          → [确定性兜底?] → verification → [重试一轮?] → reporting → done
任一环节超预算 → 停止后续 LLM 调用，仍生成报告（如实说明未完成）。

设计原则：
- 编排器只做"决定交给谁、是否重试、是否终止"，绝不直接读写业务文件；
- Agent 之间不直接调用，只通过黑板交换状态；
- 每次运行的 黑板 + 轨迹 + 报告 全部落盘，方便回放与评测。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import config
from agents.base import AgentContext, AppConfig
from agents.executor import ExecutorAgent
from agents.planner import PlannerAgent
from agents.reporter import ReporterAgent
from agents.retrieval import RetrievalAgent
from agents.verifier import VerifierAgent
from core.blackboard import Blackboard
from core.budget import BudgetExceeded, TokenBudget
from core.llm import BudgetedLLM
from core.trace import Trace
from scenarios.loader import Scenario, apply_compat
from tools.registry import Registry, default_registry


@dataclass
class RunResult:
    status: str
    plan: dict = field(default_factory=dict)
    summary: str = ""
    report_path: str = ""
    usage: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)
    run_dir: str = ""
    blackboard: dict = field(default_factory=dict)
    tool_calls: int = 0
    trace_failures: int = 0
    latency_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "done"


class Orchestrator:
    def __init__(
        self,
        *,
        task: str,
        scenario: Scenario,
        llm: BudgetedLLM,
        app_config: AppConfig | None = None,
        registry: Registry | None = None,
        approve_plan: Callable[[dict], dict] | None = None,
        approve_write: Callable[[str, dict], bool] | None = None,
        on_event: Callable[[dict], None] | None = None,
        run_id: str | None = None,
    ) -> None:
        apply_compat(scenario)

        self.task = task
        self.scenario = scenario
        self.app_config = app_config or AppConfig()
        self.registry = registry or default_registry()
        self.approve_plan = approve_plan
        self.approve_write = approve_write
        self.run_id = run_id or time.strftime("%Y%m%d_%H%M%S")
        self._t0 = time.time()

        budget_cfg = scenario.budget or {}
        # 预算口径必须与 LLM 记账口径一致：优先复用传入 BudgetedLLM 的预算池
        if isinstance(llm, BudgetedLLM):
            self.budget = llm.budget
            if self.app_config.max_total_tokens:
                self.budget.max_total_tokens = int(self.app_config.max_total_tokens)
        else:
            self.budget = TokenBudget(
                max_total_tokens=int(self.app_config.max_total_tokens or budget_cfg.get("max_total_tokens", 800_000)),
                price_in_per_m=float(budget_cfg.get("price_in_per_m", self.app_config.price_in_per_m)),
                price_out_per_m=float(budget_cfg.get("price_out_per_m", self.app_config.price_out_per_m)),
            )
            llm = BudgetedLLM(llm, self.budget)
        self.bb = Blackboard(task, scenario.id)
        self.trace = Trace(on_event=on_event)
        self.ctx = AgentContext(
            task=task,
            scenario=scenario,
            registry=self.registry,
            blackboard=self.bb,
            trace=self.trace,
            llm=llm,
            budget=self.budget,
            config=self.app_config,
            approve_write=approve_write,
        )
        self.planner = PlannerAgent(self.ctx)
        self.retrieval = RetrievalAgent(self.ctx)
        self.executor = ExecutorAgent(self.ctx)
        self.verifier = VerifierAgent(self.ctx)
        self.reporter = ReporterAgent(self.ctx)

    # ------------------------------------------------------------------
    def plan_only(self) -> dict:
        """只跑规划阶段并返回计划（供 WebUI 的"先看计划再确认"两步式交互）。

        与后续 run(preset_plan=...) 复用同一个实例，因此预算/轨迹连续累计。
        """
        apply_compat(self.scenario)  # 兼容层依赖全局 SCENARIO，运行时重新同步
        self.bb.set("status", "planning")
        self._run_agent(self.planner)
        plan = self.bb.get("plan") or {}
        self.bb.set("plan", plan)
        return plan

    def run(self, preset_plan: dict | None = None) -> RunResult:
        apply_compat(self.scenario)  # 兼容层依赖全局 SCENARIO，运行时重新同步
        self.bb.set("status", "planning")
        budget_exceeded = False

        # Phase 1: 规划（若外部已确认计划则跳过，避免重复消耗 token）
        if preset_plan is not None:
            plan = dict(preset_plan)
            self.bb.set("plan", plan)
            self.bb.set("approval", {"approved": True,
                                     "use_reference": bool(plan.pop("_use_reference", False))})
            self.trace.add("orchestrator", "plan", detail="使用已确认的计划（跳过 Planner）")
        else:
            try:
                self._run_agent(self.planner)
            except BudgetExceeded:
                budget_exceeded = True

            plan = self.bb.get("plan") or {}
            self.bb.set("plan", plan)

            # Phase 2: 人工审批
            if not budget_exceeded and self.approve_plan is not None:
                decision = self.approve_plan(plan) or {}
                self.bb.set("approval", decision)
                if not decision.get("approved", True):
                    self.bb.set("status", "cancelled")
                    self._finalize_report()
                    return self._result("cancelled", "用户取消执行")
            else:
                self.bb.set("approval", {"approved": True, "use_reference": False})

        # Phase 3: 检索
        needs_retrieval = bool(plan.get("needs_retrieval")) and self.scenario.retrieval_enabled
        if not budget_exceeded and needs_retrieval:
            try:
                self.bb.set("status", "retrieval")
                self._run_agent(self.retrieval)
            except BudgetExceeded:
                budget_exceeded = True

        # Phase 4-5: 执行 ↔ 校验 循环；校验不通过时优先"重规划"（带失败反馈）
        task_type = plan.get("task_type", "other")
        replan_rounds = 0
        self.bb.set("replan_rounds", 0)
        while not budget_exceeded:
            has_data = any(c.get("value") is not None for c in (self.bb.get("collected") or []))
            self.bb.set("status", "execution")
            try:
                result = self._run_agent(self.executor)
                # 确定性兜底：填报/清洗类任务若没写成功但有可用数据，用代码直接写（并行）
                if (not result.data.get("has_write")
                        and task_type in ("fill", "clean")
                        and has_data):
                    self.bb.add_error("orchestrator", "LLM 未完成写入，触发确定性兜底")
                    self.executor.deterministic_write()
            except BudgetExceeded:
                budget_exceeded = True
                break

            self.bb.set("status", "verification")
            try:
                verify = self._run_agent(self.verifier)
            except BudgetExceeded:
                budget_exceeded = True
                break
            if verify.ok:
                break

            # 校验不通过 → 重规划
            if replan_rounds >= self.app_config.max_replan_rounds:
                self.bb.add_error(
                    "orchestrator",
                    f"校验未通过且已达最大重规划轮数（{self.app_config.max_replan_rounds}），结束")
                break
            replan_rounds += 1
            self.bb.set("replan_rounds", replan_rounds)
            self.bb.set("status", "replanning")
            self.trace.add("orchestrator", "replan",
                           detail=f"第 {replan_rounds} 轮重规划（依据上一轮校验失败项）")
            try:
                self._run_agent(self.planner, feedback=verify.data.get("findings"))
            except BudgetExceeded:
                budget_exceeded = True
                break
            plan = self.bb.get("plan") or {}
            task_type = plan.get("task_type", task_type)
            # 重规划后若需要检索但尚无数据，补一次
            if (self.scenario.retrieval_enabled and plan.get("needs_retrieval")
                    and not any(c.get("value") is not None for c in (self.bb.get("collected") or []))):
                try:
                    self._run_agent(self.retrieval)
                except BudgetExceeded:
                    budget_exceeded = True
                    break

        # Phase 6: 报告
        self.bb.set("status", "budget_exceeded" if budget_exceeded else "done")
        self._finalize_report()
        self._persist()

        summary = "完成" if not budget_exceeded else "预算耗尽，未完全完成"
        return self._result("budget_exceeded" if budget_exceeded else "done", summary)

    # ------------------------------------------------------------------
    def _run_agent(self, agent, **kwargs) -> object:
        """统一入口：便于集中做异常记录与计时；kwargs 透传给 agent.run()。"""
        t0 = time.time()
        result = agent.run(**kwargs)
        self.ctx.trace.add(agent.name, "result", ok=result.ok,
                           detail=getattr(result, "summary", ""),
                           latency_ms=int((time.time() - t0) * 1000))
        return result

    def _finalize_report(self) -> str:
        """报告必须落盘：预算耗尽时用确定性渲染，不再消耗 LLM。"""
        try:
            use_llm = self.bb.get("status") != "budget_exceeded"
            res = self.reporter.run(use_llm=use_llm)
            return res.data.get("path", "")
        except Exception as e:  # noqa: BLE001
            self.bb.add_error("reporter", str(e))
            return ""

    def _persist(self) -> None:
        self.bb.set("usage", self.budget.summary())
        run_dir = config.STORAGE_DIR / "runs" / self.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        self.bb.save(run_dir / "blackboard.json")
        self.trace.save(run_dir / "trace.json")

    def _result(self, status: str, summary: str) -> RunResult:
        self.bb.set("usage", self.budget.summary())
        report = self.bb.get("report") or {}
        return RunResult(
            status=status,
            plan=self.bb.get("plan") or {},
            summary=summary,
            report_path=report.get("path", "") if isinstance(report, dict) else "",
            usage=self.budget.summary(),
            findings=self.bb.get("findings") or [],
            run_dir=str(config.STORAGE_DIR / "runs" / self.run_id),
            blackboard=self.bb.snapshot(),
            tool_calls=self.trace.tool_calls,
            trace_failures=self.trace.failures,
            latency_s=round(time.time() - self._t0, 2),
        )
