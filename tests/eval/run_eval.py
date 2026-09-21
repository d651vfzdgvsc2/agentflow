"""评测与 A/B 对比：多 Agent（v2） vs 单 Agent（基线）。

用法：
  python tests/eval/run_eval.py --mock          # 离线自检（脚本化 LLM，零成本）
  python tests/eval/run_eval.py --real          # 真实调用（会消耗 API）
  python tests/eval/run_eval.py --real --tasks reconcile

产物：storage/eval/eval_<时间戳>.json 与 .md（含对照表）。
指标：任务成功率、差异/重复识别正确性、严重错误数、tokens、成本、工具调用数、耗时。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = Path(__file__).resolve().parent
for p in (str(ROOT), str(EVAL_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import config  # noqa: E402
from agents.base import AppConfig  # noqa: E402
from baseline import run_single_agent  # noqa: E402
from core.budget import TokenBudget  # noqa: E402
from core.llm import BudgetedLLM, LLMResponse, MockLLM, ToolCall, build_client  # noqa: E402
from core.orchestrator import Orchestrator  # noqa: E402
from scenarios import load_scenario  # noqa: E402
from tools.registry import default_registry  # noqa: E402

DATA = ROOT / "data"
RECON_DIR = DATA / "对账"
CLEAN_FILE = "清洗/客户名单.xlsx"  # 相对 data 目录

# 演示数据里刻意注入的差异，作为评测的标准答案
GROUND = {
    "reconcile": {"diff_count": 8, "value_mismatches": 3,
                  "only_in_left": 2, "only_in_right": 2},
    "clean": {"duplicate_groups": 2},
}


# ----------------------------------------------------------------------
def _tool(name: str, args: dict) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall("c", name, json.dumps(args, ensure_ascii=False))],
                       prompt_tokens=30, completion_tokens=15)


def _text(s: str) -> LLMResponse:
    return LLMResponse(content=s, prompt_tokens=30, completion_tokens=15)


def task_specs() -> list[dict]:
    reconcile_plan = {
        "task_summary": "核对两张表", "task_type": "reconcile", "needs_retrieval": False,
        "target_files": [], "key_columns": ["订单号"], "compare_columns": ["金额"],
        "actions": ["扫描目录", "比对两表", "生成报告"], "note": "",
    }
    clean_plan = {
        "task_summary": "清洗客户名单", "task_type": "clean", "needs_retrieval": False,
        "target_files": [], "key_columns": ["编号"], "compare_columns": ["姓名", "电话"],
        "actions": ["扫描目录", "查找重复", "统计缺失", "生成报告"], "note": "",
    }
    return [
        {
            "id": "reconcile",
            "scenario": "reconcile",
            "task": "核对 data/对账 目录下的银行流水.xlsx 与 企业台账.xlsx，按订单号找出金额不一致和只在单方存在的记录，生成差异报告。",
            "ground": GROUND["reconcile"],
            "mock_v2": [
                _tool("scan_directory", {"directory": str(RECON_DIR)}),
                _text(json.dumps(reconcile_plan, ensure_ascii=False)),
                _tool("diff_tables", {
                    "left_file": "对账/银行流水.xlsx", "right_file": "对账/企业台账.xlsx",
                    "key_columns": ["订单号"], "compare_columns": ["金额"]}),
                _text("核对完成。"),
                _text("核对完成：差异 8 处。"),
            ],
            "mock_baseline": [
                _tool("diff_tables", {
                    "left_file": "对账/银行流水.xlsx", "right_file": "对账/企业台账.xlsx",
                    "key_columns": ["订单号"], "compare_columns": ["金额"]}),
                _text("完成核对。"),
            ],
        },
        {
            "id": "clean",
            "scenario": "clean",
            "task": "检查 data/清洗/客户名单.xlsx，找出重复编号与缺失字段并生成清洗报告。",
            "ground": GROUND["clean"],
            "mock_v2": [
                _tool("scan_directory", {"directory": str(DATA / "清洗")}),
                _text(json.dumps(clean_plan, ensure_ascii=False)),
                _tool("find_duplicates", {"file_name": CLEAN_FILE, "key_columns": ["编号"]}),
                _text("已找出重复与缺失。"),
                _text("清洗检查完成。"),
            ],
            "mock_baseline": [
                _tool("find_duplicates", {"file_name": CLEAN_FILE, "key_columns": ["编号"]}),
                _text("完成检查。"),
            ],
        },
    ]


# ----------------------------------------------------------------------
def _make_llm(mode: str, script: list[LLMResponse] | None) -> BudgetedLLM:
    budget = TokenBudget(max_total_tokens=1_000_000)
    if mode == "mock":
        return BudgetedLLM(MockLLM(list(script or [])), budget)
    return build_client(
        api_key=config.get_deepseek_api_key(),
        base_url=config.DEEPSEEK_BASE_URL,
        model=config.DEEPSEEK_MODEL,
        budget=budget,
    )


def run_v2(spec: dict, mode: str) -> dict:
    llm = _make_llm(mode, spec.get("mock_v2"))
    orch = Orchestrator(
        task=spec["task"], scenario=load_scenario(spec["scenario"]), llm=llm,
        app_config=AppConfig(max_steps_per_agent=8),
        run_id=f"eval_v2_{spec['id']}_{int(time.time())}",
    )
    res = orch.run()
    bb = res.blackboard
    diff = bb.get("diff") or {}
    dups = bb.get("duplicates") or {}
    errors = [f for f in res.findings if f.get("severity") == "error"]
    return {
        "status": res.status,
        "diff_count": diff.get("diff_count"),
        "value_mismatches": len(diff.get("value_mismatches", [])) if diff else None,
        "only_in_left": len(diff.get("only_in_left", [])) if diff else None,
        "only_in_right": len(diff.get("only_in_right", [])) if diff else None,
        "duplicate_groups": dups.get("duplicate_count"),
        "errors": len(errors),
        "tokens": res.usage["total_tokens"],
        "cost_yuan": res.usage["estimated_cost_yuan"],
        "llm_calls": res.usage["llm_calls"],
        "tool_calls": res.tool_calls,
        "latency_s": res.latency_s,
        "report": res.report_path,
    }


def run_baseline(spec: dict, mode: str) -> dict:
    llm = _make_llm(mode, spec.get("mock_baseline"))
    t0 = time.time()
    out = run_single_agent(spec["task"], default_registry(), llm, max_steps=12)
    diff = out.get("diff") or {}
    dups = None
    for e in out["tool_log"]:
        if e["tool"] == "find_duplicates" and isinstance(e["result"], dict):
            dups = e["result"]
    return {
        "status": "done" if (diff or dups or out["writes"]) else "no_output",
        "diff_count": diff.get("diff_count"),
        "value_mismatches": len(diff.get("value_mismatches", [])) if diff else None,
        "only_in_left": len(diff.get("only_in_left", [])) if diff else None,
        "only_in_right": len(diff.get("only_in_right", [])) if diff else None,
        "duplicate_groups": (dups or {}).get("duplicate_count"),
        "errors": None,  # 基线没有独立校验，无法自动统计严重错误
        "tokens": llm.budget.total_tokens,
        "cost_yuan": llm.budget.cost_of(llm.budget.usage),
        "llm_calls": llm.budget.usage.calls,
        "tool_calls": len(out["tool_log"]),
        "latency_s": round(time.time() - t0, 2),
        "report": None,
    }


def judge(spec: dict, m: dict) -> dict:
    """按标准答案判定是否成功（只看客观结果）。"""
    g = spec["ground"]
    if spec["id"] == "reconcile":
        ok = m.get("diff_count") == g["diff_count"] and m.get("value_mismatches") == g["value_mismatches"]
    elif spec["id"] == "clean":
        ok = m.get("duplicate_groups") == g["duplicate_groups"]
    else:
        ok = m.get("status") == "done"
    return {**m, "success": bool(ok)}


def _regen() -> None:
    """重建演示数据，保证每一轮的标准答案都成立（也避免上一轮污染）。"""
    try:
        from make_demo_data import main as gen_demo
        gen_demo()
    except Exception as e:  # noqa: BLE001
        print(f"[警告] 演示数据重建失败: {e}")


def _agg(runs: list[dict]) -> dict:
    """把同一任务的多次运行聚合成平均值与成功率。"""
    n = len(runs) or 1

    def avg(key):
        vals = [r.get(key) for r in runs if isinstance(r.get(key), (int, float))]
        return round(sum(vals) / len(vals), 4) if vals else None

    return {
        "n": len(runs),
        "success_rate": round(sum(1 for r in runs if r["success"]) / n, 3),
        "success_count": sum(1 for r in runs if r["success"]),
        "tokens": avg("tokens"),
        "cost_yuan": avg("cost_yuan"),
        "llm_calls": avg("llm_calls"),
        "tool_calls": avg("tool_calls"),
        "latency_s": avg("latency_s"),
        "errors": avg("errors"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="离线脚本化 LLM（零成本，仅自检）")
    ap.add_argument("--real", action="store_true", help="真实调用 LLM（消耗 API）")
    ap.add_argument("--tasks", default="", help="逗号分隔的任务 id，默认全部")
    ap.add_argument("--repeat", type=int, default=1, help="每个任务重复次数（取平均）")
    args = ap.parse_args()

    mode = "real" if args.real else "mock"
    repeat = max(1, args.repeat)
    specs = task_specs()
    if args.tasks:
        keep = {t.strip() for t in args.tasks.split(",") if t.strip()}
        specs = [s for s in specs if s["id"] in keep]

    print(f"\n评测模式：{mode}，任务数：{len(specs)}，每任务重复：{repeat}\n")
    rows = []
    for spec in specs:
        v2_runs, base_runs = [], []
        for i in range(repeat):
            _regen()
            v2_runs.append(judge(spec, run_v2(spec, mode)))
            _regen()
            base_runs.append(judge(spec, run_baseline(spec, mode)))
            print(f"[{spec['id']}] 第 {i+1}/{repeat} 轮："
                  f"v2={'✓' if v2_runs[-1]['success'] else '✗'}/{v2_runs[-1]['tokens']}t  "
                  f"base={'✓' if base_runs[-1]['success'] else '✗'}/{base_runs[-1]['tokens']}t")
        rows.append({"task": spec["id"],
                     "v2": _agg(v2_runs), "baseline": _agg(base_runs),
                     "v2_raw": v2_runs, "baseline_raw": base_runs})

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_dir = config.STORAGE_DIR / "eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"eval_{ts}.json").write_text(
        json.dumps({"mode": mode, "repeat": repeat, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    md = _render_md(mode, rows, repeat)
    (out_dir / f"eval_{ts}.md").write_text(md, encoding="utf-8")
    print("\n" + md)
    print(f"\n报告已写入: {out_dir / f'eval_{ts}.md'}")
    return 0


def _render_md(mode: str, rows: list[dict], repeat: int) -> str:
    lines = [f"# AgentFlow 评测报告（{mode}，每任务 {repeat} 次取平均）", "",
             f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}", ""]

    def block(tag: str, key: str) -> None:
        lines.append(f"## {tag}")
        lines.append("")
        lines.append("| 任务 | 成功率 | 平均 tokens | 平均成本(元) | 平均 LLM调用 | 平均工具调用 | 平均耗时(s) | 严重错误 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for r in rows:
            m = r[key]
            lines.append(
                f"| {r['task']} | {m['success_count']}/{m['n']} | {m['tokens']} | {m['cost_yuan']} | "
                f"{m['llm_calls']} | {m['tool_calls']} | {m['latency_s']} | "
                f"{m['errors'] if m.get('errors') is not None else '—'} |")
        lines.append("")

    block("多 Agent（v2）", "v2")
    block("单 Agent（基线）", "baseline")

    # 成本对比
    lines.append("## 成本对比（v2 vs 基线）")
    lines.append("")
    lines.append("| 任务 | v2 平均 tokens | 基线平均 tokens | 变化 |")
    lines.append("|---|---|---|---|")
    for r in rows:
        a, b = r["v2"]["tokens"], r["baseline"]["tokens"]
        if a and b:
            change = f"{(a - b) / b * 100:+.1f}%"
        else:
            change = "—"
        lines.append(f"| {r['task']} | {a} | {b} | {change} |")
    lines.append("")
    lines.append("> 成功判定基于演示数据中刻意注入的差异作为标准答案；基线与 v2 均使用同一真实模型。")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
