"""配置加载：从 .env 或环境变量读取。API Key 禁止硬编码。"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def get_deepseek_api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "未找到 DEEPSEEK_API_KEY。请在项目根目录创建 .env 文件，"
            "内容为 DEEPSEEK_API_KEY=你的密钥（参考 .env.example）"
        )
    return key


DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
# 优先使用 Flash 模型（可覆盖）
if os.environ.get("DEEPSEEK_MODEL_FLASH"):
    DEEPSEEK_MODEL = os.environ["DEEPSEEK_MODEL_FLASH"]

MAX_AGENT_STEPS = int(os.environ.get("MAX_AGENT_STEPS", "60"))
DATA_DIR = BASE_DIR / "data"
# 运行记录（黑板 / 轨迹 / 回放）落盘目录
STORAGE_DIR = BASE_DIR / "storage"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
# 默认场景（可被 CLI/WebUI 覆盖）
DEFAULT_SCENARIO = os.environ.get("AGENTFLOW_SCENARIO", "quote_fill")

# ===== 场景配置（通用化）：把写死的"品名/价格/列名"抽出来 =====
# 默认适配"报价表"场景；换场景只需在项目根目录放一个 task_config.json 覆盖。
import json  # noqa: E402

SCENARIO_DEFAULTS = {
    # 关键列：用于匹配行的列（表头名，按顺序尝试别名）
    "item_header": "项目名称",
    "item_aliases": ["项目名称", "品名", "项目", "商品名", "名称"],
    # 目标列：要填入数据来源值的列
    "price_header": "最新市场价",
    "price_aliases": ["最新市场价", "最新价格", "市场价", "单价", "价格"],
    # 数据来源网站提示（写进 Agent 提示词）
    "search_site_hints": "惠农网 cnhnb.com、1688 等电商/行情站",
    # 检索到价格后填入的格式说明（写进 Agent 提示词）
    "value_description": "最新市场价（元）",
    # ===== token 预算护栏与成本估算 =====
    # 单次任务最大 token 消耗（prompt+completion 累计），超过即停止
    "max_total_tokens": 800000,
    # 成本估算牌价（元/百万 tokens，deepseek-chat 参考价，可按需改）
    "price_in_per_m": 2.0,
    "price_out_per_m": 8.0,
}

SCENARIO = dict(SCENARIO_DEFAULTS)
_cfg_file = BASE_DIR / "task_config.json"
if _cfg_file.exists():
    try:
        _loaded = json.loads(_cfg_file.read_text(encoding="utf-8"))
        SCENARIO.update({k: v for k, v in _loaded.items() if k in SCENARIO_DEFAULTS})
    except (OSError, ValueError) as e:
        print(f"[config] task_config.json 解析失败，使用默认配置: {e}")

MAX_TOTAL_TOKENS = int(os.environ.get("MAX_TOTAL_TOKENS", str(SCENARIO["max_total_tokens"])))
