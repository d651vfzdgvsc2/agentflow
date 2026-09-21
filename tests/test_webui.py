"""WebUI 冒烟测试（用 MockLLM 替换真实客户端，零成本）。

python tests/test_webui.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _util import make_xlsx  # noqa: E402

import webui  # noqa: E402
from core.budget import TokenBudget  # noqa: E402
from core.llm import BudgetedLLM, LLMResponse, MockLLM, ToolCall  # noqa: E402


def _plan_client_factory(scan_dir: str):
    def fake_build_client(*, api_key, base_url, model, budget):
        plan = {
            "task_summary": "核对两张表", "task_type": "reconcile", "needs_retrieval": False,
            "target_files": [], "key_columns": ["订单号"], "compare_columns": ["金额"],
            "actions": ["扫描", "比对"], "note": "",
        }
        script = [
            LLMResponse(tool_calls=[ToolCall("c1", "scan_directory",
                                             json.dumps({"directory": scan_dir}))],
                        prompt_tokens=10, completion_tokens=5),
            LLMResponse(content=json.dumps(plan, ensure_ascii=False), prompt_tokens=10, completion_tokens=5),
        ]
        return BudgetedLLM(MockLLM(script), budget)
    return fake_build_client


def test_page_and_scenarios():
    client = webui.app.test_client()
    r = client.get("/")
    assert r.status_code == 200 and "AgentFlow".encode() in r.data
    r = client.get("/api/scenarios")
    ids = [s["id"] for s in r.get_json()["scenarios"]]
    assert {"reconcile", "quote_fill", "clean"} <= set(ids), ids


def test_plan_flow_with_mock_llm():
    with tempfile.TemporaryDirectory() as d:
        make_xlsx(Path(d) / "left.xlsx", ["订单号", "金额"], [["A001", 1]])
        make_xlsx(Path(d) / "right.xlsx", ["订单号", "金额"], [["A001", 2]])
        webui.build_client = _plan_client_factory(d)
        client = webui.app.test_client()
        r = client.post("/api/plan", json={
            "task": f"核对 {d} 下的两张表", "scenario": "reconcile", "dry_run": True,
        })
        data = r.get_json()
        assert r.status_code == 200, data
        assert data["plan"]["task_type"] == "reconcile"
        run_id = data["run_id"]

        r = client.get(f"/api/progress?run_id={run_id}")
        prog = r.get_json()
        assert prog["status"] == "planned"

        r = client.get("/api/report?run_id=" + run_id)
        assert r.status_code == 404  # 尚未执行，无报告


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {fn.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
