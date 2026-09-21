"""创建测试 Excel：data/学生信息表.xlsx，5 条学生数据。"""
from pathlib import Path
from openpyxl import Workbook

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)

wb = Workbook()
ws = wb.active
ws.title = "学生信息"
ws.append(["姓名", "学号", "专业", "年级", "联系方式"])
ws.append(["张三", "2023001", "园林", "大一", "13800000001"])
ws.append(["李四", "2023002", "园林", "大一", "13800000002"])
ws.append(["王五", "2023003", "园林", "大二", "13800000003"])
ws.append(["赵六", "2023004", "园林", "大二", "13800000004"])
ws.append(["孙七", "2023005", "园林", "大三", "13800000005"])

path = DATA_DIR / "学生信息表.xlsx"
wb.save(path)
print(f"已生成测试文件: {path}")
