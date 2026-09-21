"""工具注册表：Agent 能用的所有原子能力的唯一入口。

- 工具是纯函数（代码负责实际读写与校验），LLM 只能通过注册表间接调用；
- MCP Server 与 Agent 共用同一份注册表，保证"外部能调到的"和"Agent 能调到的"一致；
- 新增工具只需在此登记，无需改动 Agent。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import excel_ops
import web_search
from tools import data_ops


def _sandbox_enabled() -> bool:
    return os.environ.get("AGENTFLOW_SANDBOX", "").strip().lower() in ("1", "true", "yes", "on")


def _within_data_dir(file_name: str, base_dir: str | None) -> bool:
    """写操作沙箱：仅允许写入 data 目录内部（AGENTFLOW_SANDBOX=1 时生效）。"""
    try:
        target = excel_ops._resolve_path(file_name, base_dir)
        root = excel_ops.DEFAULT_DIR.resolve()
        target.relative_to(root)
        return True
    except (ValueError, OSError):
        return False


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    func: Callable[..., dict]
    # 写操作工具：编排层据此决定是否需要审批门 / 备份
    writes: bool = False


class Registry:
    """工具注册表。call() 永远返回 dict，异常被收敛成 {"error": ...}，不中断 Agent 循环。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"工具重复注册: {spec.name}")
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        """返回所有工具定义（MCP Server 暴露工具时使用）。"""
        return list(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools)

    def write_tools(self) -> set[str]:
        return {n for n, t in self._tools.items() if t.writes}

    def schemas(self, only: list[str] | None = None) -> list[dict]:
        """OpenAI function-calling 工具 schema。only 可限定某几个 Agent 可见的工具。"""
        items = []
        for name, spec in self._tools.items():
            if only is not None and name not in only:
                continue
            items.append({
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            })
        return items

    def call(self, name: str, arguments: dict | str | None) -> dict:
        spec = self._tools.get(name)
        if spec is None:
            return {"error": f"未知工具: {name}"}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                return {"error": f"参数不是合法 JSON: {arguments[:120]}"}
        arguments = arguments or {}
        # 沙箱：写操作默认允许，但开启 AGENTFLOW_SANDBOX 后只允许写 data 目录内部
        if spec.writes and _sandbox_enabled():
            fn = arguments.get("file_name")
            if fn and not _within_data_dir(str(fn), arguments.get("base_dir")):
                return {"error": f"沙箱已开启：禁止写入 data 目录之外的文件（{fn}）"}
        try:
            result = spec.func(**arguments)
        except TypeError as e:
            return {"error": f"参数错误: {e}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"工具执行异常: {str(e)[:200]}"}
        if not isinstance(result, dict):
            return {"result": result}
        return result


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


def build_default_registry() -> Registry:
    """构造默认工具集：Excel 读写 + 联网检索 + 数据核对/清洗。"""
    r = Registry()

    # ---------- Excel 读 ----------
    r.register(ToolSpec(
        "scan_directory",
        "扫描目录，返回每个 Excel 的文件名、sheet 名、表头（不读数据）。规划阶段首选。",
        _obj({"directory": {"type": "string", "description": "目录路径，默认 data 目录"}}),
        lambda directory=None: excel_ops.scan_directory(directory),
    ))
    r.register(ToolSpec(
        "inspect_excel",
        "读取单个 Excel 的结构：sheet 名、表头、行列数、前几行预览。修改前必看。",
        _obj({"file_name": {"type": "string"}, "max_preview_rows": {"type": "integer"}},
             ["file_name"]),
        lambda file_name, max_preview_rows=10, **kw: excel_ops.inspect_excel(file_name, kw.get("base_dir"), max_preview_rows),
    ))
    r.register(ToolSpec(
        "read_excel_range",
        "读取指定 sheet 的指定区域（如 A1:D10）。",
        _obj({"file_name": {"type": "string"}, "sheet_name": {"type": "string"},
              "start": {"type": "string"}, "end": {"type": "string"}},
             ["file_name", "sheet_name", "start", "end"]),
        lambda **kw: excel_ops.read_excel_range(kw["file_name"], kw["sheet_name"], kw["start"], kw["end"], kw.get("base_dir")),
    ))
    r.register(ToolSpec(
        "read_table",
        "把一张 sheet 读成结构化记录（表头 + 每行字典），用于核对/清洗分析。",
        _obj({"file_name": {"type": "string"}, "sheet_name": {"type": "string"}}, ["file_name"]),
        lambda file_name, sheet_name=None, **kw: data_ops.read_records(file_name, sheet_name, kw.get("base_dir")),
    ))
    r.register(ToolSpec(
        "list_sheets",
        "列出某个 Excel 里的所有 sheet 名。",
        _obj({"file_name": {"type": "string"}}, ["file_name"]),
        lambda file_name, **kw: data_ops.list_sheets(file_name, kw.get("base_dir")),
    ))

    # ---------- 数据核对 / 清洗（纯代码）----------
    r.register(ToolSpec(
        "diff_tables",
        "按关键列比对两张表，返回仅左表/仅右表/字段不一致/重复键等差异清单（确定性比对）。",
        _obj({
            "left_file": {"type": "string"}, "right_file": {"type": "string"},
            "key_columns": {"type": "array", "items": {"type": "string"}},
            "compare_columns": {"type": "array", "items": {"type": "string"}},
            "left_sheet": {"type": "string"}, "right_sheet": {"type": "string"},
        }, ["left_file", "right_file", "key_columns"]),
        lambda **kw: data_ops.diff_tables(
            kw["left_file"], kw["right_file"], kw["key_columns"],
            kw.get("compare_columns"), kw.get("left_sheet"), kw.get("right_sheet"), kw.get("base_dir"),
        ),
    ))
    r.register(ToolSpec(
        "find_duplicates",
        "按关键列在单张表里查找重复记录。",
        _obj({"file_name": {"type": "string"}, "sheet_name": {"type": "string"},
              "key_columns": {"type": "array", "items": {"type": "string"}}}, ["file_name"]),
        lambda **kw: data_ops.find_duplicates(kw["file_name"], kw.get("sheet_name"), kw.get("key_columns"), kw.get("base_dir")),
    ))

    # ---------- Excel 写（writes=True）----------
    r.register(ToolSpec(
        "write_excel_cell",
        "修改单个单元格。写操作会自动备份。",
        _obj({"file_name": {"type": "string"}, "sheet_name": {"type": "string"},
              "cell": {"type": "string"}, "value": {}},
             ["file_name", "sheet_name", "cell", "value"]),
        lambda **kw: excel_ops.write_excel_cell(kw["file_name"], kw["sheet_name"], kw["cell"], kw["value"], kw.get("base_dir")),
        writes=True,
    ))
    r.register(ToolSpec(
        "write_excel_range",
        "批量写入一个区域（二维数组）。写操作会自动备份。",
        _obj({"file_name": {"type": "string"}, "sheet_name": {"type": "string"},
              "start_cell": {"type": "string"}, "values": {"type": "array", "items": {"type": "array"}}},
             ["file_name", "sheet_name", "start_cell", "values"]),
        lambda **kw: excel_ops.write_excel_range(kw["file_name"], kw["sheet_name"], kw["start_cell"], kw["values"], kw.get("base_dir")),
        writes=True,
    ))
    r.register(ToolSpec(
        "append_excel_row",
        "在 sheet 末尾追加一行。",
        _obj({"file_name": {"type": "string"}, "sheet_name": {"type": "string"},
              "values": {"type": "array"}}, ["file_name", "sheet_name", "values"]),
        lambda **kw: excel_ops.append_excel_row(kw["file_name"], kw["sheet_name"], kw["values"], kw.get("base_dir")),
        writes=True,
    ))
    r.register(ToolSpec(
        "batch_fill",
        "按【关键列值→目标值】映射批量填写整张 sheet，一次调用完成（省 token）。写操作会自动备份。",
        _obj({"file_name": {"type": "string"}, "sheet_name": {"type": "string"},
              "price_map": {"type": "object"}, "item_col": {"type": "string"}, "price_col": {"type": "string"}},
             ["file_name", "sheet_name", "price_map"]),
        lambda **kw: excel_ops.batch_fill(kw["file_name"], kw["sheet_name"], kw["price_map"],
                                          kw.get("item_col"), kw.get("price_col"), kw.get("base_dir")),
        writes=True,
    ))
    r.register(ToolSpec(
        "batch_fill_many",
        "并行对【多个文件】批量填写（线程池），适合一次处理大量文件时提速。参数：file_names(数组)、price_map、workers(可选，默认4)。",
        _obj({"file_names": {"type": "array", "items": {"type": "string"}},
              "price_map": {"type": "object"},
              "workers": {"type": "integer"}},
             ["file_names", "price_map"]),
        lambda **kw: excel_ops.batch_fill_many(kw["file_names"], kw["price_map"],
                                               kw.get("base_dir"), kw.get("workers", 4)),
        writes=True,
    ))
    r.register(ToolSpec(
        "fill_missing",
        "按 key_column 把 source_map 的「键→值」填入目标 column 的空单元格（只补空，不覆盖）。",
        _obj({"file_name": {"type": "string"}, "sheet_name": {"type": "string"},
              "column": {"type": "string"}, "key_column": {"type": "string"},
              "source_map": {"type": "object"}},
             ["file_name", "sheet_name", "column", "key_column", "source_map"]),
        lambda **kw: data_ops.fill_missing(kw["file_name"], kw["sheet_name"], kw["column"],
                                           kw["source_map"], kw["key_column"], kw.get("base_dir")),
        writes=True,
    ))
    r.register(ToolSpec(
        "backup_excel",
        "把文件备份到 data/backup（写操作内部也会自动备份，此工具用于显式备份）。",
        _obj({"file_name": {"type": "string"}}, ["file_name"]),
        lambda **kw: excel_ops.backup_excel(kw["file_name"], kw.get("base_dir"), kw.get("backup_dir")),
    ))
    r.register(ToolSpec(
        "save_excel",
        "显式保存文件并确认写入状态。",
        _obj({"file_name": {"type": "string"}}, ["file_name"]),
        lambda **kw: excel_ops.save_excel(kw["file_name"], kw.get("base_dir")),
    ))
    r.register(ToolSpec(
        "verify_excel",
        "重新读取并验证指定值是否写入成功。checks 每项含 sheet+cell+expected，或 sheet+find+column+expected。",
        _obj({"file_name": {"type": "string"},
              "checks": {"type": "array", "items": {"type": "object"}}}, ["file_name", "checks"]),
        lambda **kw: excel_ops.verify_excel(kw["file_name"], kw["checks"], kw.get("base_dir")),
    ))
    r.register(ToolSpec(
        "find_rows",
        "按列值精确定位行号（支持多条件）。",
        _obj({"file_name": {"type": "string"}, "sheet_name": {"type": "string"},
              "conditions": {"type": "array", "items": {"type": "object"}}},
             ["file_name", "sheet_name", "conditions"]),
        lambda **kw: excel_ops.find_rows(kw["file_name"], kw["sheet_name"], kw["conditions"], kw.get("base_dir")),
    ))

    # ---------- 联网检索 ----------
    r.register(ToolSpec(
        "web_search",
        "联网搜索（Bing 主 / DuckDuckGo 备）。返回精简 title/url/snippet；不可用时返回 unavailable，不得静默编数。",
        _obj({"query": {"type": "string"}, "max_results": {"type": "integer"}}, ["query"]),
        lambda query, max_results=3, **kw: web_search.search_web(query, max_results),
    ))
    r.register(ToolSpec(
        "fetch_web",
        "抓取网页正文并提取价格数字，配合 web_search 使用（搜索结果页通常没有具体数值）。",
        _obj({"url": {"type": "string"}, "max_chars": {"type": "integer"}}, ["url"]),
        lambda url, max_chars=6000, **kw: web_search.fetch_web(url, max_chars),
    ))
    r.register(ToolSpec(
        "reference_price",
        "内置参考数据（非实时，演示兜底）。仅当用户明确选择使用参考数据时调用。",
        _obj({"item": {"type": "string"}}, ["item"]),
        lambda item, **kw: web_search.reference_price(item),
    ))

    return r


# 供 Excel MCP Server 等复用的单例（按需懒建）
_DEFAULT: Registry | None = None


def default_registry() -> Registry:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = build_default_registry()
    return _DEFAULT
