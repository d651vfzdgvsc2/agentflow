"""agentflow.scenarios：场景模板（通用引擎的可插拔业务配置）。"""
from .loader import SCENARIOS_DIR, Scenario, apply_compat, list_scenarios, load_scenario

__all__ = ["Scenario", "load_scenario", "list_scenarios", "apply_compat", "SCENARIOS_DIR"]
