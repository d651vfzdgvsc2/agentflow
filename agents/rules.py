"""校验规则库：全部是确定性代码，不让 LLM 决定"对错"。

多 Agent 的显性风险是幻觉叠加，因此校验必须由代码给出结论，
LLM 顶多负责把结论翻译成人话（本实现里连这步都可省，以保证可复现）。
每条规则返回 Finding 列表：{"rule","severity"(error/warning/info),"message",...}
"""
from __future__ import annotations

from typing import Any, Callable

from core.blackboard import Blackboard

Finding = dict[str, Any]


def _written_pairs(bb: Blackboard):
    """遍历所有"写入的 条目→值"，兼容 batch_fill / write_excel_cell / fill_missing。"""
    for w in bb.get("writes") or []:
        for entry in w.get("written", []) or []:
            yield entry.get("item"), entry.get("price", entry.get("value"))
        for entry in w.get("filled", []) or []:
            yield entry.get("item"), entry.get("value")


def _sourced_map(bb: Blackboard) -> dict[str, dict]:
    return {c["item"]: c for c in (bb.get("collected") or [])}


def rule_no_duplicate_keys(ctx, bb: Blackboard) -> list[Finding]:
    plan = bb.get("plan") or {}
    keys = plan.get("key_columns") or (ctx.scenario.defaults.get("key_columns") or [])
    files = plan.get("target_files") or []
    if not keys:
        return [{"rule": "no_duplicate_keys", "severity": "info",
                 "message": "未指定关键列，跳过重复检查"}]
    # 重复是"数据事实"而非"任务失败"：检出即写入报告，不阻断流程
    severity = "info"
    findings: list[Finding] = []

    def _check(f: str):
        return f, ctx.registry.call("find_duplicates", {"file_name": f, "key_columns": keys})

    from concurrent.futures import ThreadPoolExecutor

    if len(files) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(files))) as ex:
            checked = list(ex.map(_check, files))
    else:
        checked = [_check(f) for f in files]

    for f, res in checked:
        if res.get("error"):
            findings.append({"rule": "no_duplicate_keys", "severity": "warning",
                             "message": f"重复检查失败: {res['error']}", "file": f})
            continue
        for g in res.get("duplicate_groups", []):
            findings.append({"rule": "no_duplicate_keys", "severity": severity,
                             "message": f"{f} 存在重复关键值 {g['key']}（行 {g['rows']}）",
                             "file": f, "key": g["key"], "rows": g["rows"]})
    if not findings:
        findings.append({"rule": "no_duplicate_keys", "severity": "info", "message": "未发现重复记录"})
    return findings


def rule_no_empty_required(ctx, bb: Blackboard) -> list[Finding]:
    findings: list[Finding] = []
    for w in bb.get("writes") or []:
        if w.get("error"):
            findings.append({"rule": "no_empty_required", "severity": "error",
                             "message": f"写入失败: {w['error']}", "file": w.get("file")})
        for nf in w.get("not_found", []) or []:
            findings.append({"rule": "no_empty_required", "severity": "warning",
                             "message": f"条目『{nf.get('item')}』无数据，未填写", "row": nf.get("row")})
    written = list(_written_pairs(bb))
    for item, val in written:
        if val is None or str(val).strip() == "":
            findings.append({"rule": "no_empty_required", "severity": "error",
                             "message": f"条目『{item}』写入了空值"})
    if not findings:
        findings.append({"rule": "no_empty_required", "severity": "info", "message": "必填项检查通过"})
    return findings


def rule_clean_missing_fields(ctx, bb: Blackboard) -> list[Finding]:
    """清洗场景：扫描目标表，报告"必填列"里的空单元格（缺失字段）。

    只报告、不修改；补全动作由 Executor 的 fill_missing 完成（且只补空、不覆盖）。
    required_columns 未配置或与表头对不上时，退化为检查全部列。
    """
    from pathlib import Path

    plan = bb.get("plan") or {}
    files = plan.get("target_files") or []
    required = plan.get("required_columns") or (ctx.scenario.defaults.get("required_columns") or [])
    findings: list[Finding] = []

    def _check(f: str):
        return f, ctx.registry.call("read_table", {"file_name": f})

    from concurrent.futures import ThreadPoolExecutor

    if len(files) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(files))) as ex:
            checked = list(ex.map(_check, files))
    else:
        checked = [_check(f) for f in files]

    for f, res in checked:
        if "error" in res:
            findings.append({"rule": "clean_missing_fields", "severity": "warning",
                             "message": f"读取失败: {res['error']}", "file": f})
            continue
        header = res.get("header") or []
        cols = [c for c in required if c in header] or header
        for rec, rn in zip(res.get("records") or [], res.get("row_numbers") or []):
            for c in cols:
                v = rec.get(c)
                if v is None or str(v).strip() == "":
                    findings.append({"rule": "clean_missing_fields", "severity": "warning",
                                     "message": f"{Path(f).name} 第{rn}行「{c}」为空",
                                     "file": f, "row": rn, "column": c})
    if not findings:
        findings.append({"rule": "clean_missing_fields", "severity": "info", "message": "未发现缺失字段"})
    return findings


def rule_values_traceable(ctx, bb: Blackboard) -> list[Finding]:
    """每个写入的值都要能对应到一条带来源的检索结果。"""
    collected = _sourced_map(bb)
    if not collected:
        return [{"rule": "values_traceable", "severity": "info", "message": "无外部数据，跳过溯源检查"}]
    findings: list[Finding] = []
    for item, _val in _written_pairs(bb):
        entry = collected.get(item)
        if entry is None:
            findings.append({"rule": "values_traceable", "severity": "error",
                             "message": f"条目『{item}』的写入值在检索结果中找不到来源"})
        elif not entry.get("source"):
            findings.append({"rule": "values_traceable", "severity": "error",
                             "message": f"条目『{item}』缺少来源标注"})
    if not findings:
        findings.append({"rule": "values_traceable", "severity": "info", "message": "所有写入值均可溯源"})
    return findings


def rule_no_hallucinated_values(ctx, bb: Blackboard) -> list[Finding]:
    """写入值必须等于检索到的值，防止 LLM 编造/篡改数字。"""
    collected = _sourced_map(bb)
    external = {k: v for k, v in collected.items() if v.get("value") is not None}
    if not external:
        return [{"rule": "no_hallucinated_values", "severity": "info", "message": "无可比对的外部值，跳过"}]

    def _num(v):
        try:
            return float(str(v).replace(",", ""))
        except (TypeError, ValueError):
            return None

    findings: list[Finding] = []
    for item, val in _written_pairs(bb):
        if item not in external:
            continue
        expected = external[item]["value"]
        ok = (str(val).strip() == str(expected).strip()) or (
            _num(val) is not None and _num(expected) is not None and _num(val) == _num(expected)
        )
        if not ok:
            findings.append({"rule": "no_hallucinated_values", "severity": "error",
                             "message": f"条目『{item}』写入值 {val} 与检索值 {expected} 不一致（疑似编造）"})
    if not findings:
        findings.append({"rule": "no_hallucinated_values", "severity": "info", "message": "未发现编造/篡改"})
    return findings


def rule_diff_consistency(ctx, bb: Blackboard) -> list[Finding]:
    """核对类任务必须产出差异结果，否则视为失败。"""
    diff = bb.get("diff")
    if not diff:
        return [{"rule": "diff_consistency", "severity": "error",
                 "message": "未产出核对差异结果（未调用 diff_tables 或比对失败）"}]
    total = diff.get("diff_count", 0)
    return [{"rule": "diff_consistency", "severity": "info",
             "message": f"核对完成：共 {total} 处差异", "diff_count": total,
             "only_in_left": len(diff.get("only_in_left", [])),
             "only_in_right": len(diff.get("only_in_right", [])),
             "value_mismatches": len(diff.get("value_mismatches", []))}]


RULES: dict[str, Callable[..., list[Finding]]] = {
    "no_duplicate_keys": rule_no_duplicate_keys,
    "no_empty_required": rule_no_empty_required,
    "clean_missing_fields": rule_clean_missing_fields,
    "values_traceable": rule_values_traceable,
    "no_hallucinated_values": rule_no_hallucinated_values,
    "diff_consistency": rule_diff_consistency,
}


def run_rules(names: list[str], ctx, bb: Blackboard) -> list[Finding]:
    findings: list[Finding] = []
    for name in names:
        fn = RULES.get(name)
        if fn is None:
            findings.append({"rule": name, "severity": "warning", "message": f"未知校验规则: {name}"})
            continue
        try:
            findings.extend(fn(ctx, bb))
        except Exception as e:  # noqa: BLE001
            findings.append({"rule": name, "severity": "warning", "message": f"规则执行异常: {e}"})
    return findings
