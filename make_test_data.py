"""生成 17 张模拟报价表测试数据。

每张表结构不同（多 sheet、不同表头），模拟真实绿化工程报价场景。
生成到 data/报价表/ 目录。

设计要点：
- 每行有"单价"（旧的参考价）和"最新市场价"（留空，Agent 填写目标）
- 表头统一含"项目名称"，保证 find_rows 能定位
"""
from __future__ import annotations

import random
from pathlib import Path

from openpyxl import Workbook

DATA_DIR = Path(__file__).resolve().parent / "data" / "报价表"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 苗木/建材 清单（名称 -> (旧参考价, 最新市场价区间))
ITEMS = {
    "香樟": (85, 78, 92),
    "桂花": (120, 110, 135),
    "银杏": (260, 240, 280),
    "红枫": (180, 165, 198),
    "罗汉松": (450, 420, 480),
    "紫薇": (95, 88, 105),
    "樱花": (150, 138, 165),
    "黄杨球": (45, 40, 52),
    "杜鹃": (8, 6.5, 9.5),
    "金叶女贞": (3.5, 2.8, 4.2),
    "草坪卷": (15, 13, 18),
    "花岗岩路沿石": (32, 28, 38),
    "青石板": (58, 50, 66),
    "透水砖": (4.5, 3.8, 5.5),
    "LED庭院灯": (360, 320, 420),
    "防腐木栏杆": (85, 75, 98),
    "仿真石漆": (28, 24, 33),
    "种植土(方)": (68, 60, 78),
    "有机肥(袋)": (22, 18, 26),
    "排水管(DN110)": (18, 15, 22),
}

SHEET_PRESETS = [
    ["苗木报价", "材料报价"],
    ["绿化工程报价", "道路工程报价"],
    ["苗木清单", "照明工程报价", "给排水报价"],
    ["主材报价"],
    ["苗木报价", "安装工程报价", "措施费报价"],
    ["绿化报价", "硬景报价"],
    ["苗木采购清单", "石材报价", "灯具报价"],
    ["报价汇总", "苗木明细"],
    ["工程报价", "材料明细", "人工费报价"],
    ["苗木报价", "土建报价"],
    ["绿化苗木", "景观材料"],
    ["报价单"],
    ["苗木清单", "主材清单", "措施项目"],
    ["绿化报价", "水电报价", "土方报价"],
    ["苗木市场价", "建材市场价"],
    ["工程总报价", "分项报价"],
    ["苗木及建材报价"],
]

HEADER_TEMPLATE = ["序号", "项目名称", "规格", "单位", "单价(参考)", "最新市场价", "数量", "合价"]


def build_workbook(preset_sheets: list[str], seed: int) -> Workbook:
    wb = Workbook()
    wb.remove(wb.active)
    rng = random.Random(seed)

    for sheet_name in preset_sheets:
        ws = wb.create_sheet(title=sheet_name)
        ws.append(HEADER_TEMPLATE)

        n_items = rng.randint(6, 12)
        pool = list(ITEMS.items())
        rng.shuffle(pool)

        for i, (name, (old, low, high)) in enumerate(pool[:n_items], start=1):
            spec = rng.choice(["高60-80cm", "冠幅1.5-2m", "胸径8-10cm", "D50cm", "500x300x120", "3m", "1.2m高", "直径110"])
            unit = rng.choice(["株", "棵", "米", "平方米", "块", "套", "袋", "方"])
            qty = rng.randint(10, 500)
            ws.append([i, name, spec, unit, old, None, qty, None])

    return wb


def main():
    files = []
    for i, preset in enumerate(SHEET_PRESETS, start=1):
        wb = build_workbook(preset, seed=i * 31)
        path = DATA_DIR / f"报价表{i:02d}.xlsx"
        wb.save(path)
        files.append(path.name)
    print(f"已生成 {len(files)} 张报价表到 {DATA_DIR}:")
    for f in files:
        print("  -", f)


if __name__ == "__main__":
    main()
