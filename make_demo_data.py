"""生成演示数据：核对（对账）与清洗两个场景的示例表。

生成到：
  data/对账/银行流水.xlsx、data/对账/企业台账.xlsx
  data/清洗/客户名单.xlsx
差异/重复/空值是刻意注入的，便于演示与评测。
"""
from __future__ import annotations

import random
from pathlib import Path

from openpyxl import Workbook

DATA = Path(__file__).resolve().parent / "data"


def _save(path: Path, header: list, rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(header)
    for r in rows:
        ws.append(r)
    wb.save(path)
    print(f"  生成 {path}  ({len(rows)} 行)")


def make_reconcile() -> None:
    random.seed(20260917)
    companies = ["张江建设", "华润物业", "绿地园林", "万科工程", "中建八局", "城建集团", "金螳螂", "东方园林"]
    header = ["订单号", "客户", "金额", "日期"]

    ledger = []
    for i in range(1, 31):
        oid = f"PO2026{i:04d}"
        ledger.append([oid, random.choice(companies), random.choice([1200, 3500, 8800, 15600, 24000]), f"2026-09-{random.randint(1,28):02d}"])
    # 银行流水：以台账为基准，注入差异
    bank = [row[:] for row in ledger]
    # 金额不一致 3 处
    for idx in (2, 7, 15):
        bank[idx][2] = bank[idx][2] + 100
    # 银行多出 2 笔
    bank.append(["PO2026099", "张江建设", 9999, "2026-09-29"])
    bank.append(["PO2026100", "绿地园林", 4500, "2026-09-30"])
    # 台账多出 2 笔
    ledger.append(["PO2026201", "华润物业", 3000, "2026-09-05"])
    ledger.append(["PO2026202", "东方园林", 7600, "2026-09-06"])
    # 台账重复 1 行
    ledger.append(ledger[4][:])

    _save(DATA / "对账" / "银行流水.xlsx", header, bank)
    _save(DATA / "对账" / "企业台账.xlsx", header, ledger)
    print("  注入：3 处金额不一致 / 各自多出 2 笔 / 台账 1 处重复键")


def make_clean() -> None:
    header = ["编号", "姓名", "电话", "备注"]
    rows = [
        ["C001", "张三", "13800000001", ""],
        ["C002", "李四", "13800000002", "老客户"],
        ["C003", "王五", "", "新客户"],
        ["C002", "李四", "13800000002", "老客户"],   # 重复
        ["C005", "赵六", "13800000005", ""],
        ["C006", "", "13800000006", ""],              # 姓名缺失
        ["C007", "孙七", "13800000007", "需回访"],
        ["C001", "张三", "13800000001", ""],          # 重复
        ["C009", "周八", "", ""],                     # 电话缺失
    ]
    _save(DATA / "清洗" / "客户名单.xlsx", header, rows)
    print("  注入：2 组重复编号 / 姓名缺失 1 / 电话缺失 2")


def main() -> None:
    print("生成演示数据：")
    make_reconcile()
    make_clean()
    print("完成。")


if __name__ == "__main__":
    main()
