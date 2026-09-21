"""场景加载器：把"办公数据处理"抽象成可替换的场景模板。

换场景 = 换一个 JSON，不改任何代码。这正是"通用引擎 + 垂直模板"的落点。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

SCENARIOS_DIR = Path(__file__).resolve().parent


@dataclass
class Scenario:
    id: str
    name: str
    description: str = ""
    task_examples: list[str] = field(default_factory=list)
    plan_hints: str = ""
    retrieval_enabled: bool = False
    # 该场景是否允许修改数据文件（核对类应设为 false，避免污染源数据）
    writes_data: bool = True
    verification_rules: list[str] = field(default_factory=list)
    report_title: str = "执行报告"
    defaults: dict = field(default_factory=dict)
    budget: dict = field(default_factory=dict)
    compat: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "task_examples": self.task_examples,
            "retrieval_enabled": self.retrieval_enabled,
            "writes_data": self.writes_data,
            "verification_rules": self.verification_rules,
            "report_title": self.report_title,
            "defaults": self.defaults,
        }


def list_scenarios() -> list[dict]:
    """列出所有可用场景（供 UI 下拉与 CLI --list）。"""
    out = []
    for path in sorted(SCENARIOS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({
            "id": data.get("id", path.stem),
            "name": data.get("name", path.stem),
            "description": data.get("description", ""),
            "task_examples": data.get("task_examples", []),
            "retrieval_enabled": bool(data.get("retrieval_enabled", False)),
        })
    return out


def load_scenario(scenario_id: str) -> Scenario:
    path = SCENARIOS_DIR / f"{scenario_id}.json"
    if not path.exists():
        available = [s["id"] for s in list_scenarios()]
        raise ValueError(f"未知场景: {scenario_id}，可用场景: {available}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return Scenario(
        id=data["id"],
        name=data.get("name", data["id"]),
        description=data.get("description", ""),
        task_examples=data.get("task_examples", []),
        plan_hints=data.get("plan_hints", ""),
        retrieval_enabled=bool(data.get("retrieval_enabled", False)),
        writes_data=bool(data.get("writes_data", True)),
        verification_rules=data.get("verification_rules", []),
        report_title=data.get("report_title", "执行报告"),
        defaults=data.get("defaults", {}),
        budget=data.get("budget", {}),
        compat=data.get("compat", {}),
        raw=data,
    )


def apply_compat(scenario: Scenario) -> None:
    """把场景的兼容字段同步进根 config.SCENARIO，供 excel_ops.batch_fill 等复用层读取。

    这是"新引擎复用旧 Excel 工具"的粘合点：excel_ops 仍按老约定读取 config.SCENARIO。
    """
    try:
        import config as root_config
    except Exception:  # noqa: BLE001
        return
    compat = scenario.compat or {}
    for key in (
        "item_header", "item_aliases", "price_header", "price_aliases",
        "search_site_hints", "value_description",
    ):
        if key in compat:
            root_config.SCENARIO[key] = compat[key]
