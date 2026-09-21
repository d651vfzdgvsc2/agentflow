"""命令行入口：一句话驱动多 Agent 协同完成办公数据处理任务。

用法：
  python cli.py "核对 data/对账 下的两张表，按订单号找差异" --scenario reconcile
  python cli.py "把 data/报价表 按最新行情填报" --scenario quote_fill
  python cli.py --list                      # 列出可用场景
  python cli.py "..." --dry-run             # 只读试跑，不写任何文件
  python cli.py "..." --yes                 # 不交互，自动确认
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import config  # noqa: E402
from agents.base import AppConfig  # noqa: E402
from core.budget import TokenBudget  # noqa: E402
from core.llm import build_client  # noqa: E402
from core.orchestrator import Orchestrator  # noqa: E402
from scenarios import list_scenarios, load_scenario  # noqa: E402

# Windows 控制台中文输出
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass


def _print_scenarios() -> None:
    print("可用场景：")
    for s in list_scenarios():
        flag = "联网" if s["retrieval_enabled"] else "离线"
        print(f"  - {s['id']:12s} [{flag}] {s['name']}")
        if s.get("description"):
            print(f"      {s['description']}")


def _make_approve_plan(plan: dict) -> dict:
    print("\n" + "=" * 60)
    print("【执行计划】")
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    print("=" * 60)
    try:
        answer = input("确认执行？[y/n] ").strip().lower()
        use_reference = False
        if answer == "y" and plan.get("needs_retrieval"):
            ans2 = input("若联网检索不可用，是否允许使用内置参考数据？[y/n] ").strip().lower()
            use_reference = ans2 == "y"
        return {"approved": answer == "y", "use_reference": use_reference}
    except (EOFError, KeyboardInterrupt):
        print("\n未获得确认输入，按取消处理。")
        return {"approved": False, "use_reference": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="多 Agent 协同办公数据处理平台")
    parser.add_argument("task", nargs="?", help="自然语言任务")
    parser.add_argument("--scenario", default=config.DEFAULT_SCENARIO, help="场景 id")
    parser.add_argument("--list", action="store_true", help="列出可用场景")
    parser.add_argument("--yes", action="store_true", help="非交互，自动确认计划")
    parser.add_argument("--use-reference", action="store_true", help="允许使用内置参考数据（联网不可用时兜底）")
    parser.add_argument("--dry-run", action="store_true", help="只读试跑，禁止任何写操作")
    parser.add_argument("--max-tokens", type=int, default=None, help="覆盖单次任务 token 预算")
    parser.add_argument("--model", default=None, help="覆盖模型名")
    args = parser.parse_args(argv)

    if args.list:
        _print_scenarios()
        return 0

    if not args.task:
        parser.print_help()
        return 1

    try:
        scenario = load_scenario(args.scenario)
    except ValueError as e:
        print(f"[错误] {e}")
        return 1

    app_config = AppConfig(dry_run=args.dry_run)
    if args.max_tokens:
        app_config.max_total_tokens = args.max_tokens

    # 预算参数取自场景配置，保证记账口径与编排器一致
    budget_cfg = scenario.budget or {}
    max_tokens = args.max_tokens or int(budget_cfg.get("max_total_tokens", 800_000))
    budget = TokenBudget(
        max_total_tokens=max_tokens,
        price_in_per_m=float(budget_cfg.get("price_in_per_m", 2.0)),
        price_out_per_m=float(budget_cfg.get("price_out_per_m", 8.0)),
    )

    try:
        llm = build_client(
            api_key=config.get_deepseek_api_key(),
            base_url=config.DEEPSEEK_BASE_URL,
            model=args.model or config.DEEPSEEK_MODEL,
            budget=budget,
        )
    except RuntimeError as e:
        print(f"[错误] {e}")
        return 1

    def on_event(step: dict) -> None:
        tool = f" {step['tool']}" if step.get("tool") else ""
        mark = "" if step.get("ok") else " [失败]"
        print(f"  · [{step['agent']}]{tool}{mark} {step['detail'][:80]}")

    if args.yes or args.dry_run or args.use_reference:
        # 非交互：需要参考数据时用显式回调把 use_reference 传下去
        approve_plan = (lambda plan: {"approved": True, "use_reference": True}) if args.use_reference else None
    else:
        approve_plan = _make_approve_plan

    orch = Orchestrator(
        task=args.task,
        scenario=scenario,
        llm=llm,
        app_config=app_config,
        approve_plan=approve_plan,
        on_event=on_event,
    )

    print(f"\n[AgentFlow] 场景={scenario.name}  任务：{args.task}\n")
    result = orch.run()

    print("\n" + "=" * 60)
    print(f"状态：{result.status}")
    print(f"报告：{result.report_path}")
    print(f"运行记录：{result.run_dir}")
    u = result.usage
    print(f"用量：LLM {u['llm_calls']} 次，{u['total_tokens']:,} tokens，估算 ¥{u['estimated_cost_yuan']}")
    for name, au in u["per_agent"].items():
        print(f"  - {name:10s} {au['calls']} 次 / {au['total_tokens']:,} tokens / ¥{au['estimated_cost_yuan']}")
    print("=" * 60)
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
