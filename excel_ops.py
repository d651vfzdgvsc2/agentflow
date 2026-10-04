"""Excel 操作层：所有对 .xlsx 的实际读写都在这里完成。

不包含任何 LLM 逻辑，只负责：
- 列目录 / 读结构 / 读区域 / 写单元格 / 写区域 / 追加行 / 保存 / 验证
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.workbook import Workbook

DEFAULT_DIR = Path(__file__).resolve().parent / "data"

# 备份文件名生成的互斥锁：并行写入多个文件时避免时间戳撞车导致相互覆盖
_BACKUP_LOCK = threading.Lock()


def _resolve_path(name: str, base_dir: str | Path | None = None) -> Path:
    """将文件名或路径解析为绝对路径，默认落在 data 目录下。"""
    base = Path(base_dir) if base_dir else DEFAULT_DIR
    p = Path(name)
    if not p.is_absolute():
        p = base / p
    return p.resolve()


def _resolve_dir(directory: str | Path) -> Path:
    """解析目录：绝对路径直接用；相对路径先按当前工作目录，再按 data 目录。

    这样 "data/报价表"（相对项目根）与 "上传/xxx"（相对 data）都能正确命中。
    """
    p = Path(directory)
    if p.is_absolute():
        return p
    cwd_rel = Path.cwd() / p
    if cwd_rel.exists():
        return cwd_rel
    data_rel = DEFAULT_DIR / p
    return data_rel if data_rel.exists() else cwd_rel


def _safe(value: Any) -> Any:
    """防公式注入：字符串若以 = + - @ 开头，加前导单引号强制按文本写入。

    （openpyxl 会把以 '=' 开头的字符串当公式；值可能来自联网检索/用户文件。）
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def _load_sheet_meta(path: Path):
    """返回 (workbook, sheet_name)。sheet_name 为 None 时取第一个 sheet。"""
    wb = load_workbook(path, data_only=False)
    return wb


def _ensure_backup(path: Path) -> str | None:
    """写操作前的自动备份：复制原文件到 data/backup（原名_时间戳.xlsx）。

    与 backup_excel 的区别：此函数由写操作内部强制调用，不依赖 LLM 记得调用。
    备份失败时返回 None，不阻塞写入（避免因备份问题导致任务中断）。
    """
    import shutil
    import time

    try:
        backup_root = DEFAULT_DIR / "backup"
        backup_root.mkdir(parents=True, exist_ok=True)
        with _BACKUP_LOCK:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            target = backup_root / f"{path.stem}_{stamp}{path.suffix}"
            # 同一秒内多次写入时避免覆盖
            i = 1
            while target.exists():
                target = backup_root / f"{path.stem}_{stamp}_{i}{path.suffix}"
                i += 1
            shutil.copy2(path, target)
        return str(target)
    except Exception:  # noqa: BLE001
        return None


def list_excel_files(directory: str | Path | None = None) -> list[str]:
    """列出目录中所有 .xlsx / .xlsm 文件。"""
    base = Path(directory) if directory else DEFAULT_DIR
    if not base.exists():
        return []
    return sorted(str(p.name) for p in base.iterdir() if p.suffix.lower() in {".xlsx", ".xlsm"})


def inspect_excel(file_name: str, base_dir: str | None = None, max_preview_rows: int = 10) -> dict[str, Any]:
    """读取 Excel 结构：sheet 列表、每个 sheet 的表头、行列数和部分预览。"""
    path = _resolve_path(file_name, base_dir)
    if not path.exists():
        return {"error": f"文件不存在: {path}"}
    wb = load_workbook(path, data_only=True)
    sheets = []
    for ws in wb.worksheets:
        real_max_row = ws.max_row
        real_max_col = ws.max_column
        header = [str(c.value) if c.value is not None else "" for c in ws[1]] if real_max_row >= 1 else []
        preview = []
        # 注意：iter_rows 的 max_row 若超过实际行数会扩展工作表的 max_row 属性，
        # 因此这里用真实行数截断，避免污染后续逻辑。
        end_row = min(1 + max_preview_rows, real_max_row)
        for row in ws.iter_rows(min_row=2, max_row=end_row, values_only=True):
            if all(v is None for v in row):
                continue
            preview.append([str(v) if v is not None else "" for v in row])
        sheets.append({
            "name": ws.title,
            "max_row": real_max_row,
            "max_col": real_max_col,
            "dimensions": ws.calculate_dimension(),
            "header": header,
            "preview": preview,
        })
    wb.close()
    return {"file": str(path), "sheets": sheets}


def read_excel_range(file_name: str, sheet_name: str, start: str, end: str, base_dir: str | None = None) -> dict[str, Any]:
    """读取指定 sheet 的指定区域（如 A1:D10）。"""
    path = _resolve_path(file_name, base_dir)
    if not path.exists():
        return {"error": f"文件不存在: {path}"}
    wb = load_workbook(path, data_only=True)
    if sheet_name not in wb.sheetnames:
        wb.close()
        return {"error": f"Sheet 不存在: {sheet_name}，可用: {wb.sheetnames}"}
    ws = wb[sheet_name]
    rows = []
    for row in ws[start:end]:
        rows.append(["" if c.value is None else str(c.value) for c in row])
    wb.close()
    return {"file": str(path), "sheet": sheet_name, "range": f"{start}:{end}", "rows": rows}


def write_excel_cell(file_name: str, sheet_name: str, cell: str, value: Any, base_dir: str | None = None) -> dict[str, Any]:
    """修改指定单元格。"""
    path = _resolve_path(file_name, base_dir)
    if not path.exists():
        return {"error": f"文件不存在: {path}"}
    wb = load_workbook(path, data_only=False)
    if sheet_name not in wb.sheetnames:
        wb.close()
        return {"error": f"Sheet 不存在: {sheet_name}"}
    ws = wb[sheet_name]
    _ensure_backup(path)
    old = ws[cell].value
    ws[cell] = value
    wb.save(path)
    new = ws[cell].value
    wb.close()
    return {"file": str(path), "sheet": sheet_name, "cell": cell, "old_value": old, "new_value": new, "saved": True}


def write_excel_range(file_name: str, sheet_name: str, start_cell: str, values: list[list[Any]], base_dir: str | None = None) -> dict[str, Any]:
    """批量写入指定区域，values 为二维列表，从 start_cell 开始。"""
    path = _resolve_path(file_name, base_dir)
    if not path.exists():
        return {"error": f"文件不存在: {path}"}
    wb = load_workbook(path, data_only=False)
    if sheet_name not in wb.sheetnames:
        wb.close()
        return {"error": f"Sheet 不存在: {sheet_name}"}
    ws = wb[sheet_name]
    _ensure_backup(path)
    from openpyxl.utils.cell import coordinate_from_string, column_index_from_string

    col, row = coordinate_from_string(start_cell)
    start_col = column_index_from_string(col)
    start_row = row
    written = 0
    for r_off, row_vals in enumerate(values):
        for c_off, val in enumerate(row_vals):
            ws.cell(row=start_row + r_off, column=start_col + c_off, value=val)
            written += 1
    wb.save(path)
    wb.close()
    return {"file": str(path), "sheet": sheet_name, "start_cell": start_cell, "written_cells": written, "saved": True}


def append_excel_row(file_name: str, sheet_name: str, values: list[Any], base_dir: str | None = None) -> dict[str, Any]:
    """在指定 Sheet 末尾追加一行。"""
    path = _resolve_path(file_name, base_dir)
    if not path.exists():
        return {"error": f"文件不存在: {path}"}
    wb = load_workbook(path, data_only=False)
    if sheet_name not in wb.sheetnames:
        wb.close()
        return {"error": f"Sheet 不存在: {sheet_name}"}
    ws = wb[sheet_name]
    _ensure_backup(path)
    ws.append(values)
    wb.save(path)
    new_row = ws.max_row
    wb.close()
    return {"file": str(path), "sheet": sheet_name, "appended_row": new_row, "values": values, "saved": True}


def save_excel(file_name: str, base_dir: str | None = None) -> dict[str, Any]:
    """显式保存（openpyxl 已在各写操作内保存，此工具用于显式确认与状态返回）。"""
    path = _resolve_path(file_name, base_dir)
    if not path.exists():
        return {"error": f"文件不存在: {path}"}
    try:
        wb = load_workbook(path)
        wb.save(path)
        wb.close()
        return {"file": str(path), "saved": True, "size_bytes": path.stat().st_size}
    except PermissionError:
        return {"error": f"文件被占用，无法保存: {path}（请先关闭 Excel 中的该文件）"}


def verify_excel(file_name: str, checks: list[dict[str, Any]], base_dir: str | None = None) -> dict[str, Any]:
    """验证：checks 每项支持两种形式。

    1) 单元格验证: {"sheet": "Sheet1", "cell": "B2", "expected": "人工智能"}
    2) 按行定位验证: {"sheet": "Sheet1", "find": [{"header": "项目名称", "value": "香樟"}],
                      "column": "F", "expected": 85}
       （先按条件匹配定位到行，再验证该行指定列的值）

    返回每项是否匹配。
    """
    path = _resolve_path(file_name, base_dir)
    if not path.exists():
        return {"error": f"文件不存在: {path}"}
    wb = load_workbook(path, data_only=True)
    from openpyxl.utils.cell import get_column_letter

    results = []
    for c in checks:
        sheet_name = c.get("sheet")
        if sheet_name not in wb.sheetnames:
            results.append({"check": c, "ok": False, "reason": f"Sheet 不存在: {sheet_name}"})
            continue
        ws = wb[sheet_name]
        if "cell" in c:
            actual = ws[c["cell"]].value
            ok = str(actual) == str(c.get("expected"))
            results.append({"check": c, "ok": ok, "actual": actual})
        elif "find" in c and "column" in c:
            # 按表头名/列字母解析条件，定位行
            header_map = {}
            for col_idx, cell in enumerate(ws[1], start=1):
                if cell.value is not None:
                    header_map[str(cell.value).strip()] = get_column_letter(col_idx)
            resolved = []
            bad_cond = None
            for cond in c["find"]:
                col = cond.get("column")
                if not col and cond.get("header"):
                    col = header_map.get(str(cond["header"]).strip())
                if not col:
                    bad_cond = cond
                    break
                resolved.append((col, str(cond.get("value", ""))))
            if bad_cond is not None:
                results.append({"check": c, "ok": False,
                                "reason": f"无法解析条件列: {bad_cond}，可用表头: {list(header_map)}"})
                continue
            matched = []
            for row in range(2, ws.max_row + 1):
                ok_all = True
                for col, val in resolved:
                    cell_val = ws[f"{col}{row}"].value
                    if cell_val is None or str(cell_val).strip() != val:
                        ok_all = False
                        break
                if ok_all:
                    matched.append(row)
            if not matched:
                results.append({"check": c, "ok": False, "reason": "未找到匹配行", "matched_rows": []})
                continue
            row = matched[0]
            actual = ws[f"{c['column']}{row}"].value
            ok = str(actual) == str(c.get("expected"))
            results.append({"check": c, "ok": ok, "actual": actual, "matched_rows": matched})
        else:
            results.append({"check": c, "ok": False,
                            "reason": "check 需含 cell+expected 或 find+column+expected"})
    wb.close()
    return {"file": str(path), "results": results, "all_ok": all(r.get("ok") for r in results)}


def find_rows(file_name: str, sheet_name: str, conditions: list[dict[str, Any]], base_dir: str | None = None) -> dict[str, Any]:
    """按列值精确定位行号。

    conditions 形如 [{"column": "B", "value": "香樟"}, {"column": "C", "value": "胸径8-10cm"}]
    或 [{"header": "项目名称", "value": "香樟"}]（先按表头名解析列）。

    返回 matched_rows 列表（基于数据首行=第1行）。若传 multiple，返回全部匹配；否则只返回第一个匹配。
    """
    path = _resolve_path(file_name, base_dir)
    if not path.exists():
        return {"error": f"文件不存在: {path}"}
    wb = load_workbook(path, data_only=True)
    if sheet_name not in wb.sheetnames:
        wb.close()
        return {"error": f"Sheet 不存在: {sheet_name}，可用: {wb.sheetnames}"}
    ws = wb[sheet_name]

    # 解析列索引（支持列字母 或 表头名）
    resolved = []
    header_map = {}  # 表头名 -> 列字母
    for col_idx, cell in enumerate(ws[1], start=1):
        if cell.value is not None:
            from openpyxl.utils.cell import get_column_letter

            header_map[str(cell.value).strip()] = get_column_letter(col_idx)

    for cond in conditions:
        col = cond.get("column")
        if not col and cond.get("header"):
            col = header_map.get(str(cond["header"]).strip())
        if not col:
            wb.close()
            return {"error": f"无法解析条件列: {cond}，可用表头: {list(header_map)}"}
        resolved.append((col, str(cond.get("value", ""))))

    matched = []
    for row in range(2, ws.max_row + 1):
        ok = True
        for col, val in resolved:
            cell_val = ws[f"{col}{row}"].value
            if cell_val is None or str(cell_val).strip() != val:
                ok = False
                break
        if ok:
            matched.append(row)

    wb.close()
    return {"file": str(path), "sheet": sheet_name, "conditions": conditions, "matched_rows": matched}


def backup_excel(file_name: str, base_dir: str | None = None, backup_dir: str | None = None) -> dict[str, Any]:
    """修改前自动备份原文件到 backup 目录（原名_时间戳.xlsx）。"""
    import shutil
    import time

    path = _resolve_path(file_name, base_dir)
    if not path.exists():
        return {"error": f"文件不存在: {path}"}
    backup_root = Path(backup_dir) if backup_dir else DEFAULT_DIR / "backup"
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = backup_root / f"{path.stem}_{stamp}{path.suffix}"
    shutil.copy2(path, target)
    return {"file": str(path), "backup": str(target), "backed_up": True}


def scan_directory(directory: str | Path | None = None, max_preview_rows: int = 0) -> dict[str, Any]:
    """扫描目录，只读取每个 Excel 的文件名、sheet 名、表头（默认不读数据，省 Token）。

    用于 Agent 规划阶段：了解"有哪些文件、每个文件什么结构"，不加载全量数据。
    """
    base = _resolve_dir(directory) if directory else DEFAULT_DIR
    if not base.exists() or not base.is_dir():
        return {"error": f"目录不存在: {base}"}

    files = []
    for p in sorted(base.iterdir()):
        if p.suffix.lower() not in {".xlsx", ".xlsm"}:
            continue
        try:
            wb = load_workbook(p, data_only=True, read_only=True)
            sheets = []
            for ws in wb.worksheets:
                header = []
                for row in ws.iter_rows(min_row=1, max_row=1, values_only=True):
                    header = ["" if v is None else str(v) for v in row]
                    break
                sheets.append({"name": ws.title, "max_row": ws.max_row, "header": header})
            wb.close()
            # rel_path：若目录在 data 下，返回相对 data 的路径（如 uploads/xxx.xlsx），
            # 便于 Agent 直接用 file_name 定位；否则返回绝对路径。
            try:
                rel = p.resolve().relative_to(DEFAULT_DIR.resolve())
                file_path = str(rel).replace("\\", "/")
            except ValueError:
                file_path = str(p.resolve())
            files.append({"file": p.name, "file_path": file_path, "sheets": sheets})
        except Exception as e:  # noqa: BLE001
            files.append({"file": p.name, "error": str(e)})

    return {"directory": str(base), "count": len(files), "files": files}


def batch_fill(file_name: str, sheet_name: str, price_map: dict[str, Any],
               item_col: str | None = None, price_col: str | None = None,
               base_dir: str | None = None) -> dict[str, Any]:
    """按"关键列名→值"映射批量填写（场景无关，列名由 config 的 SCENARIO 配置）。

    price_map 形如 {"香樟": 85, "桂花": 120}。
    item_col / price_col 可传列字母（B）或表头名；不传时按 SCENARIO 里的
    item_aliases / price_aliases 依次在表头里自动识别；都找不到再退回 B/F。
    在服务端逐行匹配并填入，返回写入/未匹配明细。单个工具调用即可完成一张 sheet 的批量填写。
    """
    path = _resolve_path(file_name, base_dir)
    if not path.exists():
        return {"error": f"文件不存在: {path}"}
    wb = load_workbook(path, data_only=False)
    if sheet_name not in wb.sheetnames:
        wb.close()
        return {"error": f"Sheet 不存在: {sheet_name}，可用: {wb.sheetnames}"}
    ws = wb[sheet_name]
    _ensure_backup(path)

    # 表头名 -> 列字母 的映射
    header_map = {}
    from openpyxl.utils.cell import get_column_letter

    for col_idx, cell in enumerate(ws[1], start=1):
        if cell.value is not None:
            header_map[str(cell.value).strip()] = get_column_letter(col_idx)

    import config

    def _resolve_col(preferred, aliases):
        """优先用户传的列；其次按配置别名在表头里找；最后退回旧默认。"""
        if preferred:
            return header_map.get(preferred, preferred)  # 传表头名则转列字母
        for alias in aliases:
            if alias in header_map:
                return header_map[alias]
        return None  # 交给最后的 B/F 兜底

    resolved_item = _resolve_col(item_col, config.SCENARIO["item_aliases"])
    resolved_price = _resolve_col(price_col, config.SCENARIO["price_aliases"])
    item_col = resolved_item or "B"
    price_col = resolved_price or "F"

    written = []
    not_found = []
    for row in range(2, ws.max_row + 1):
        item_cell = ws[f"{item_col}{row}"]
        if item_cell.value is None:
            continue
        item = str(item_cell.value).strip()
        if item in price_map:
            ws[f"{price_col}{row}"] = _safe(price_map[item])
            written.append({"row": row, "item": item, "price": price_map[item]})
        else:
            not_found.append({"row": row, "item": item})

    wb.save(path)
    wb.close()
    return {
        "file": str(path),
        "sheet": sheet_name,
        "item_col": item_col,
        "price_col": price_col,
        "written": written,
        "written_count": len(written),
        "not_found": not_found,
        "not_found_count": len(not_found),
        "saved": True,
    }


def batch_fill_many(file_names: list[str], price_map: dict[str, Any],
                    base_dir: str | None = None, workers: int = 4) -> dict[str, Any]:
    """并行对多个文件批量填写（线程池）。

    每个文件相互独立（各自 openpyxl 读写、各自备份），因此可以安全并行；
    这是"文件多时提速"的主要手段（例如 17 张报价表）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    workers = max(1, int(workers or 1))
    files = [str(f) for f in (file_names or [])]

    def _one(fn: str) -> dict[str, Any]:
        info = inspect_excel(fn, base_dir)
        if "error" in info:
            return {"file": fn, "error": info["error"], "saved": False,
                    "written": [], "not_found": [], "written_count": 0, "not_found_count": 0}
        per_sheet, written, not_found = [], [], []
        for sh in info.get("sheets", []):
            r = batch_fill(fn, sh["name"], price_map, base_dir=base_dir)
            per_sheet.append(r)
            written += r.get("written", [])
            not_found += r.get("not_found", [])
        err = next((r["error"] for r in per_sheet if "error" in r), None)
        return {
            "file": fn,
            "saved": any(r.get("saved") for r in per_sheet),
            "written": written,
            "written_count": len(written),
            "not_found": not_found,
            "not_found_count": len(not_found),
            "error": err,
        }

    results: list[dict[str, Any]] = []
    if workers == 1 or len(files) <= 1:
        results = [_one(f) for f in files]
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_one, fn): fn for fn in files}
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception as e:  # noqa: BLE001
                    results.append({"file": futures[fut], "error": str(e), "saved": False,
                                    "written": [], "not_found": [], "written_count": 0, "not_found_count": 0})

    results.sort(key=lambda r: str(r.get("file")))
    return {
        "files": results,
        "file_count": len(results),
        "written_count": sum(r.get("written_count", 0) for r in results),
        "failed_count": sum(1 for r in results if r.get("error") or not r.get("saved")),
        "saved": any(r.get("saved") for r in results),
        "workers": workers,
    }
