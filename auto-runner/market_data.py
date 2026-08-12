#!/usr/bin/env python3
"""
market_data.py — 美股行情数据获取（成交数据 + PE）

auto-runner 波动率均值回归短线策略的数据基础层：
  - 第一步基本面初筛：PE 过滤（trailingPE / forwardPE）
  - 第二步波动率确认：过去 60 天 OHLCV（收盘/最高/最低/成交量）

用法：
  python3 market_data.py NVDA               # 单标的：60天成交概况 + PE
  python3 market_data.py NVDA TSLA SK.KS     # 多标的
  python3 market_data.py NVDA --days 90      # 指定回溯天数（默认60）
  python3 market_data.py NVDA --json         # 输出完整 JSON

数据源：Yahoo Finance
  - 成交数据：v8/finance/chart（复用 stock_screener.py 的 curl 模式，无需鉴权）
  - PE 数据：v10/finance/quoteSummary（需 cookie + crumb 鉴权）

作为库调用：
  from market_data import fetch_prices, fetch_valuation, get_market_data
  data = get_market_data("NVDA", days=60)
  # data = {"ticker", "prices"[...], "valuation"{...}, "stats"{...}}
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
TIMEOUT = 15

# crumb + cookie 在进程内缓存，避免每个标的都重新握手
_CRUMB_CACHE = {"crumb": None, "cookie_file": None}


# ============================================================
# 底层：curl 请求
# ============================================================

def _curl_json(url, cookie_file=None, extra_args=None):
    """用 curl 获取 JSON，绕过 Python SSL 问题（沿用 stock_screener 思路）"""
    cmd = ["curl", "-s", "-H", f"User-Agent: {UA}"]
    if cookie_file:
        cmd += ["-b", cookie_file]
    if extra_args:
        cmd += extra_args
    cmd.append(url)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
        if result.returncode != 0 or not result.stdout:
            return None
        return json.loads(result.stdout)
    except Exception:
        return None


def _curl_text(url, cookie_file=None, extra_args=None):
    cmd = ["curl", "-s", "-H", f"User-Agent: {UA}"]
    if cookie_file:
        cmd += ["-b", cookie_file]
    if extra_args:
        cmd += extra_args
    cmd.append(url)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


# ============================================================
# 成交数据（价格 + 成交量），无需鉴权
# ============================================================

def fetch_prices(ticker, days=60):
    """
    获取美股日线 OHLCV。
    返回 [{"date","open","high","low","close","volume","turnover"}...]，最近的在最后。
    turnover = close * volume（成交额估算，成交量为股数）。
    多拉一些日历日以覆盖足够的交易日（周末/假日会缺）。
    """
    calendar_days = int(days * 1.6) + 15  # 交易日≈日历日×0.69，留足冗余
    end_ts = int(datetime.now().timestamp())
    start_ts = int((datetime.now() - timedelta(days=calendar_days)).timestamp())
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?period1={start_ts}&period2={end_ts}&interval=1d"
    )
    data = _curl_json(url)
    if not data:
        return None
    try:
        chart = data["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        return None

    timestamps = chart.get("timestamp", []) or []
    quote = chart.get("indicators", {}).get("quote", [{}])[0]
    opens = quote.get("open", [])
    highs = quote.get("high", [])
    lows = quote.get("low", [])
    closes = quote.get("close", [])
    volumes = quote.get("volume", [])

    rows = []
    for i, ts in enumerate(timestamps):
        c = closes[i] if i < len(closes) else None
        v = volumes[i] if i < len(volumes) else None
        h = highs[i] if i < len(highs) else None
        low = lows[i] if i < len(lows) else None
        o = opens[i] if i < len(opens) else None
        if c is None or v is None:
            continue  # 停牌/缺数据日跳过
        rows.append({
            "date": datetime.fromtimestamp(ts).strftime("%Y-%m-%d"),
            "open": round(o, 4) if o is not None else None,
            "high": round(h, 4) if h is not None else None,
            "low": round(low, 4) if low is not None else None,
            "close": round(c, 4),
            "volume": int(v),
            "turnover": round(c * v, 2),
        })
    # 只保留最近 days 个交易日
    return rows[-days:] if len(rows) > days else rows


# ============================================================
# PE / 估值数据，需 cookie + crumb 鉴权
# ============================================================

def _ensure_crumb():
    """获取（并缓存）cookie + crumb。失败返回 (None, None)。"""
    if _CRUMB_CACHE["crumb"]:
        return _CRUMB_CACHE["crumb"], _CRUMB_CACHE["cookie_file"]
    import tempfile
    cookie_file = tempfile.NamedTemporaryFile(delete=False, suffix=".cookies").name
    # 1. 触发 set-cookie
    subprocess.run(
        ["curl", "-s", "-c", cookie_file, "-H", f"User-Agent: {UA}",
         "https://fc.yahoo.com", "-o", "/dev/null"],
        capture_output=True, timeout=TIMEOUT,
    )
    # 2. 用 cookie 换 crumb
    crumb = _curl_text(
        "https://query1.finance.yahoo.com/v1/test/getcrumb",
        cookie_file=cookie_file,
    )
    if not crumb or "<" in crumb or len(crumb) > 40:
        return None, None
    _CRUMB_CACHE["crumb"] = crumb
    _CRUMB_CACHE["cookie_file"] = cookie_file
    return crumb, cookie_file


def _raw(node):
    """从 Yahoo {'raw':..,'fmt':..} 结构里取 raw 值。"""
    if isinstance(node, dict):
        return node.get("raw")
    return node


def fetch_valuation(ticker):
    """
    获取估值指标：trailingPE / forwardPE / priceToBook / marketCap / eps 等。
    返回 dict；鉴权失败或无数据返回 None。
    """
    crumb, cookie_file = _ensure_crumb()
    if not crumb:
        return None
    url = (
        f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
        f"?modules=summaryDetail,defaultKeyStatistics&crumb={crumb}"
    )
    data = _curl_json(url, cookie_file=cookie_file)
    try:
        result = data["quoteSummary"]["result"][0]
    except (KeyError, IndexError, TypeError):
        return None
    sd = result.get("summaryDetail", {}) or {}
    ks = result.get("defaultKeyStatistics", {}) or {}

    return {
        "trailingPE": _raw(sd.get("trailingPE")),
        "forwardPE": _raw(sd.get("forwardPE")) or _raw(ks.get("forwardPE")),
        "priceToBook": _raw(ks.get("priceToBook")),
        "trailingEps": _raw(ks.get("trailingEps")),
        "forwardEps": _raw(ks.get("forwardEps")),
        "marketCap": _raw(sd.get("marketCap")),
        "dividendYield": _raw(sd.get("dividendYield")),
    }


# ============================================================
# 成交数据统计（60天概况）
# ============================================================

def compute_stats(prices):
    """基于价格序列计算成交概况与波动率（供策略第二步用）。"""
    if not prices:
        return None
    closes = [p["close"] for p in prices]
    volumes = [p["volume"] for p in prices]
    turnovers = [p["turnover"] for p in prices]
    n = len(prices)

    # 日收益率标准差 → 年化波动率
    rets = [(closes[i] / closes[i - 1] - 1) for i in range(1, n)]
    vol_daily = None
    vol_annual = None
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        vol_daily = var ** 0.5
        vol_annual = vol_daily * (252 ** 0.5)

    return {
        "trading_days": n,
        "date_start": prices[0]["date"],
        "date_end": prices[-1]["date"],
        "close_last": closes[-1],
        "pct_period": round((closes[-1] / closes[0] - 1) * 100, 2),
        "avg_volume": int(sum(volumes) / n),
        "avg_turnover": round(sum(turnovers) / n, 2),
        "high_period": max(p["high"] for p in prices if p["high"] is not None),
        "low_period": min(p["low"] for p in prices if p["low"] is not None),
        "volatility_daily": round(vol_daily, 4) if vol_daily else None,
        "volatility_annual": round(vol_annual, 4) if vol_annual else None,
    }


# ============================================================
# 整合入口
# ============================================================

def get_market_data(ticker, days=60):
    """一次性获取某标的的成交数据 + 估值 + 统计。"""
    prices = fetch_prices(ticker, days=days)
    valuation = fetch_valuation(ticker)
    stats = compute_stats(prices) if prices else None
    return {
        "ticker": ticker,
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "requested_days": days,
        "prices": prices,
        "valuation": valuation,
        "stats": stats,
    }


# ============================================================
# CLI 输出
# ============================================================

def _fmt(v, nd=2, dash="—"):
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else dash


def _fmt_big(v):
    if not isinstance(v, (int, float)):
        return "—"
    for unit, div in [("T", 1e12), ("B", 1e9), ("M", 1e6)]:
        if abs(v) >= div:
            return f"{v / div:.2f}{unit}"
    return f"{v:.0f}"


def print_summary(md):
    t = md["ticker"]
    prices = md["prices"]
    if not prices:
        print(f"  {t:<10} ⚠️  无法获取成交数据")
        return
    s = md["stats"]
    v = md["valuation"] or {}
    print(f"\n  {'=' * 62}")
    print(f"  {t}  ({s['date_start']} ~ {s['date_end']}, {s['trading_days']} 个交易日)")
    print(f"  {'-' * 62}")
    print(f"  收盘价     ${_fmt(s['close_last'])}   区间涨跌 {s['pct_period']:+.2f}%   "
          f"区间高/低 ${_fmt(s['high_period'])}/{_fmt(s['low_period'])}")
    print(f"  日均成交量 {s['avg_volume']:,} 股   日均成交额 ${_fmt_big(s['avg_turnover'])}")
    print(f"  年化波动率 {_fmt(s['volatility_annual'] * 100 if s['volatility_annual'] else None)}%   "
          f"日波动率 {_fmt(s['volatility_daily'] * 100 if s['volatility_daily'] else None)}%")
    print(f"  {'-' * 62}")
    print(f"  PE(TTM) {_fmt(v.get('trailingPE'))}   PE(Fwd) {_fmt(v.get('forwardPE'))}   "
          f"PB {_fmt(v.get('priceToBook'))}   市值 ${_fmt_big(v.get('marketCap'))}")


def main():
    ap = argparse.ArgumentParser(description="美股成交数据 + PE 获取（auto-runner 数据层）")
    ap.add_argument("tickers", nargs="+", help="标的代码，如 NVDA TSLA 0700.HK")
    ap.add_argument("--days", type=int, default=60, help="回溯交易日数（默认 60）")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = ap.parse_args()

    results = []
    for ticker in args.tickers:
        md = get_market_data(ticker.upper() if "." not in ticker else ticker, days=args.days)
        results.append(md)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    print(f"\n{'=' * 64}")
    print(f"  美股行情数据  |  回溯 {args.days} 个交易日  |  {datetime.now():%Y-%m-%d %H:%M}")
    print(f"{'=' * 64}")
    for md in results:
        print_summary(md)
    print()


if __name__ == "__main__":
    main()
