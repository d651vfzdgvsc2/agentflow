"""联网搜索模块：统一 search_web 接口。

主通道：Bing（cn.bing.com / www.bing.com，国内直连，无需 Key、无需代理）。
备用通道：DuckDuckGo（ddgs），仅当 Bing 失败时尝试。
若两者都不可用，返回明确 unavailable 状态，由 Agent 决定如何处理
（重试 / 使用内置参考数据），绝不静默伪造。

内置参考数据：仅当用户明确选择"使用内置参考数据"时由 Agent 调用，且返回中标注来源。
"""
from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request

# 内置参考数据（模拟市场行情）。仅作兜底演示，Agent 必须明确告知用户这是参考数据而非实时搜索。
_REFERENCE_DATA = {
    "香樟": 85,
    "桂花": 120,
    "银杏": 260,
    "红枫": 180,
    "罗汉松": 450,
    "紫薇": 95,
    "樱花": 150,
    "黄杨球": 45,
    "杜鹃": 8,
    "金叶女贞": 3.5,
    "草坪卷": 15,
    "花岗岩路沿石": 32,
    "青石板": 58,
    "透水砖": 4.5,
    "LED庭院灯": 360,
    "防腐木栏杆": 85,
    "仿真石漆": 28,
    "种植土": 68,
    "有机肥": 22,
    "排水管": 18,
}

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0 Edge/119.0",
]


def _fetch(url: str, timeout: int = 10) -> str:
    import random

    req = urllib.request.Request(url, headers={"User-Agent": random.choice(_USER_AGENTS)})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _bing_search(query: str, max_results: int = 3) -> list[dict]:
    """Bing 搜索（国内直连）。"""
    results: list[dict] = []
    for base in ("https://www.bing.com/search", "https://cn.bing.com/search"):
        try:
            url = f"{base}?q={urllib.parse.quote(query)}&count={max_results * 2}&setlang=zh-hans"
            htm = _fetch(url)
            items = re.findall(r'<li class="b_algo".*?</li>', htm, re.DOTALL)
            if not items:
                continue
            results = []
            for a in items[:max_results]:
                parsed = _parse_algo_item(a)
                if parsed and parsed["url"]:
                    results.append(parsed)
            if results:
                return results
        except Exception:  # noqa: BLE001
            continue
    return results


def _parse_algo_item(block: str) -> dict | None:
    """从单个 b_algo 块解析 标题/URL/摘要。

    Bing 结构：<h2 class=""><a href="真实URL">标题</a></h2> + <p>摘要</p>
    """
    # 标题 + URL：h2 内的 a 标签
    title_m = re.search(r'<h2[^>]*>.*?<a[^>]*href="(https?://[^"]*)"[^>]*>(.*?)</a>', block, re.DOTALL)
    url = ""
    title = ""
    if title_m:
        url = title_m.group(1)
        title = html.unescape(re.sub(r"<[^>]+>", "", title_m.group(2))).strip()

    # 摘要：<p> 或 b_caption 块
    p_m = re.search(r"<p[^>]*>(.*?)</p>", block, re.DOTALL)
    snippet = html.unescape(re.sub(r"<[^>]+>", "", p_m.group(1))).strip() if p_m else ""

    if not url:
        # 兜底：任意第一个外链
        m = re.search(r'href="(https?://[^"]*)"', block)
        url = m.group(1) if m else ""
    if not title:
        # 兜底：块内第一个较长的纯文本
        for t in re.findall(r">([^<>]{6,})<", block):
            t = html.unescape(t).strip()
            if t and "http" not in t and not t.startswith("."):
                title = t
                break

    if not url:
        return None
    return {"title": title[:200], "url": url, "snippet": snippet[:200]}


def _ddgs_search(query: str, max_results: int = 3) -> list[dict]:
    """DuckDuckGo 搜索（备用通道）。失败抛异常。"""
    import ddgs

    with ddgs.DDGS() as d:
        res = list(d.text(query, region="cn-zh", max_results=max_results))
    out = []
    for r in res or []:
        out.append({
            "title": r.get("title", ""),
            "url": r.get("href", ""),
            "snippet": (r.get("body") or "")[:200],
        })
    return out


def search_web(query: str, max_results: int = 3) -> dict:
    """统一搜索接口：Bing 优先，DuckDuckGo 备用。

    返回结构：
      {"status": "ok", "query": ..., "results": [...]}
      {"status": "unavailable", "query": ..., "reason": "..."}
    """
    # 主通道：Bing
    try:
        results = _bing_search(query, max_results)
        if results:
            return {"status": "ok", "query": query, "results": results, "engine": "bing"}
    except Exception as e:  # noqa: BLE001
        bing_err = str(e)[:100]
    else:
        bing_err = "无结果"

    # 备用通道：DuckDuckGo
    try:
        results = _ddgs_search(query, max_results)
        if results:
            return {"status": "ok", "query": query, "results": results, "engine": "duckduckgo"}
        return {"status": "unavailable", "query": query, "reason": f"搜索无结果（Bing: {bing_err}）"}
    except Exception as e:  # noqa: BLE001
        return {
            "status": "unavailable",
            "query": query,
            "reason": f"联网搜索不可用（Bing: {bing_err}; DDG: {str(e)[:80]}）",
        }


def reference_price(item_name: str) -> dict:
    """内置参考数据（演示兜底）。返回标注来源，绝不冒充实时搜索。"""
    key = item_name.strip()
    for name, price in _REFERENCE_DATA.items():
        if key == name or key.startswith(name) or name in key:
            return {
                "status": "ok",
                "item": key,
                "price": price,
                "source": "内置参考数据(非实时)",
            }
    return {"status": "not_found", "item": key}


def parse_price_from_results(query: str, results: list[dict]) -> float | None:
    """尝试从搜索结果中提取第一个价格数字（简单启发式，供 Agent 参考）。"""
    for r in results:
        snippet = (r.get("title", "") + " " + r.get("snippet", ""))
        m = re.search(r"(\d+(?:\.\d+)?)\s*(元|块|每|/)?", snippet)
        if m:
            return float(m.group(1))
    return None


def _html_to_text(raw: str, max_chars: int = 6000) -> str:
    """把 HTML 转成可读文本，剥离脚本/样式/标签，保留关键内容。"""
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    # 保留价格类常用标签的语义（用换行分隔）
    raw = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|td|br)>", "\n", raw)
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()[:max_chars]


def fetch_web(url: str, max_chars: int = 6000) -> dict:
    """抓取网页正文，返回文本和其中提取的价格数字。

    与 OpenCode crawl4ai 的 fetch 类似，但更轻量（requests 直抓，适合服务端渲染页面）。
    返回：
      {"status": "ok", "url": ..., "text": "...", "prices": [数字列表]}
      {"status": "error", "url": ..., "reason": "..."}
    """
    try:
        raw = _fetch(url, timeout=12)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "url": url, "reason": f"抓取失败: {str(e)[:120]}"}

    text = _html_to_text(raw, max_chars)

    # 提取价格数字：优先带货币符号的，其次 "数字+元/块/万" 形式
    prices = []
    for m in re.finditer(r"[¥￥]\s*(\d+(?:\.\d+)?)", raw):
        prices.append(float(m.group(1)))
    if not prices:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*元", text):
            prices.append(float(m.group(1)))
    # 去重保序
    seen = set()
    unique = []
    for p in prices:
        if p not in seen:
            seen.add(p)
            unique.append(p)
        if len(unique) >= 10:
            break

    return {"status": "ok", "url": url, "text": text[:max_chars], "prices": unique[:10]}
