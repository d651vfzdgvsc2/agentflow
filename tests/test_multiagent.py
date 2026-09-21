"""多 Agent 编排集成测试（用 MockLLM 驱动，零 API 成本、可重复）。

验证：
1. 核对场景全流程（规划→执行→校验→报告）能跑通并产出差异；
2. 填报场景在"LLM 没有真正写入"时，确定性兜底能补上写入；
3. 预算分账覆盖到每个 Agent。

python tests/test_multiagent.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _util import make_xlsx  # noqa: E402

from agents.base import AppConfig  # noqa: E402
from core.budget import TokenBudget  # noqa: E402
from core.llm import BudgetedLLM, LLMResponse, MockLLM, ToolCall  # noqa: E402
from core.orchestrator import Orchestrator  # noqa: E402
from scenarios import load_scenario  # noqa: E402


def tool_resp(name: str, args: dict) -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id="c1", name=name, arguments=json.dumps(args, ensure_ascii=False))],
        prompt_tokens=20, completion_tokens=10,
    )


def text_resp(text: str) -> LLMResponse:
    return LLMResponse(content=text, prompt_tokens=20, completion_tokens=10)


def _orch(scenario_id: str, script: list[LLMResponse], task: str) -> Orchestrator:
    llm = BudgetedLLM(MockLLM(script), TokenBudget(max_total_tokens=1_000_000))
    return Orchestrator(
        task=task,
        scenario=load_scenario(scenario_id),
        llm=llm,
        app_config=AppConfig(max_steps_per_agent=6),
    )


def test_reconcile_end_to_end():
    with tempfile.TemporaryDirectory() as d:
        left = make_xlsx(Path(d) / "left.xlsx", ["订单号", "金额"], [
            ["A001", 100], ["A002", 200], ["A003", 300]])
        right = make_xlsx(Path(d) / "right.xlsx", ["订单号", "金额"], [
            ["A001", 100], ["A002", 250], ["A004", 400]])

        plan = {
            "task_summary": "核对两张表",
            "task_type": "reconcile",
            "needs_retrieval": False,
            "target_files": [],
            "key_columns": ["订单号"],
            "compare_columns": ["金额"],
            "actions": ["扫描目录", "比对两表", "生成差异报告"],
            "note": "",
        }
        script = [
            tool_resp("scan_directory", {"directory": d}),
            text_resp(json.dumps(plan, ensure_ascii=False)),
            tool_resp("diff_tables", {
                "left_file": str(left), "right_file": str(right),
                "key_columns": ["订单号"], "compare_columns": ["金额"],
            }),
            text_resp("已完成核对，差异见报告。"),
            text_resp("核对完成：左缺 A003、右缺 A004、A002 金额不一致。"),
        ]
        orch = _orch("reconcile", script, f"核对 {d} 下的两张表")
        result = orch.run()

        assert result.status == "done", result.status
        diff = result.blackboard.get("diff")
        assert diff and diff["diff_count"] == 3, diff
        assert Path(result.report_path).exists()
        # 预算分账应覆盖 planner / executor / reporter
        per_agent = result.usage["per_agent"]
        assert per_agent["planner"]["calls"] >= 2
        assert per_agent["executor"]["calls"] >= 2
        print("    reconcile diff_count =", diff["diff_count"])


def test_fill_with_deterministic_fallback():
    with tempfile.TemporaryDirectory() as d:
        target = make_xlsx(Path(d) / "报价表.xlsx", ["项目名称", "最新市场价"], [
            ["香樟", None], ["桂花", None]])

        plan = {
            "task_summary": "填报最新市场价",
            "task_type": "fill",
            "needs_retrieval": True,
            "target_files": [],
            "key_columns": ["项目名称"],
            "compare_columns": ["最新市场价"],
            "actions": ["扫描", "检索价格", "批量填报"],
            "note": "",
        }
        retrieval_values = {
            "available": True,
            "values": {
                "香樟": {"value": 85, "source": "http://example.com/xiangzhang", "confidence": 0.9},
                "桂花": {"value": 120, "source": "http://example.com/guihua", "confidence": 0.9},
            },
        }
        script = [
            tool_resp("scan_directory", {"directory": d}),
            text_resp(json.dumps(plan, ensure_ascii=False)),
            text_resp(json.dumps(retrieval_values, ensure_ascii=False)),
            # 执行器故意什么都不做 → 触发确定性兜底
            text_resp("我暂时没有需要执行的操作。"),
            text_resp("填报完成，已写入 2 行。"),
        ]
        orch = _orch("quote_fill", script, f"把 {d} 下的报价表按最新行情填报")
        result = orch.run()

        assert result.status == "done", result.status
        from openpyxl import load_workbook

        wb = load_workbook(target)
        ws = wb.active
        assert ws["B2"].value == 85, ws["B2"].value
        assert ws["B3"].value == 120, ws["B3"].value
        wb.close()
        # 校验不应出现严重问题
        errors = [f for f in result.findings if f.get("severity") == "error"]
        assert not errors, errors
        print("    filled 香樟=85 桂花=120, errors=0")


def test_replan_on_verification_failure():
    """校验失败后应回到规划（带失败反馈）重来，而不是原地重试。"""
    with tempfile.TemporaryDirectory() as d:
        left = make_xlsx(Path(d) / "left.xlsx", ["订单号", "金额"], [["A001", 100], ["A002", 200]])
        right = make_xlsx(Path(d) / "right.xlsx", ["订单号", "金额"], [["A001", 100], ["A002", 250]])

        plan = {
            "task_summary": "核对两张表", "task_type": "reconcile", "needs_retrieval": False,
            "target_files": [], "key_columns": ["订单号"], "compare_columns": ["金额"],
            "actions": ["扫描", "比对"], "note": "",
        }
        script = [
            tool_resp("scan_directory", {"directory": d}),
            text_resp(json.dumps(plan, ensure_ascii=False)),
            # 第一轮执行器故意不产出差异 → 校验失败
            text_resp("我不需要做任何操作。"),
            # 重规划：Planner 再来一轮
            tool_resp("scan_directory", {"directory": d}),
            text_resp(json.dumps(plan, ensure_ascii=False)),
            # 第二轮执行器正确调用 diff_tables
            tool_resp("diff_tables", {"left_file": str(left), "right_file": str(right),
                                      "key_columns": ["订单号"], "compare_columns": ["金额"]}),
            text_resp("已产出核对结果。"),
            text_resp("核对完成，发现 1 处差异。"),
        ]
        orch = _orch("reconcile", script, f"核对 {d} 下的两张表")
        result = orch.run()

        assert result.status == "done", result.status
        assert result.blackboard.get("replan_rounds") == 1, result.blackboard.get("replan_rounds")
        assert result.blackboard.get("diff"), "重规划后应产出差异"
        print("    replan_rounds =", result.blackboard.get("replan_rounds"))


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            import traceback
            failed += 1
            print(f"  FAIL  {fn.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
