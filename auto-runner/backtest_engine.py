#!/usr/bin/env python3
"""
backtest_engine.py — 可配置参数的滚动回测引擎

与 strategy_screen.py 的区别：strategy_screen 用固定全局常量判断单只股票「现在」
该不该买；本引擎把所有阈值变成可传入的 **参数字典**，用于批量、滚动、多组参数
的历史回测，是 auto_optimize.py 自我迭代 pipeline 的核心依赖。

设计要点（对应此前回测暴露的问题）：
  - 严格用截至建仓日的历史数据选股，不触碰未来数据
  - 扣除双边交易成本（默认单边 0.1%，买卖合计 0.2%）
  - 支持大盘过滤：QQQ 相对其 N 日均线之上才允许开仓，否则空仓
  - 支持止盈/止损（可分别设置百分比，止损相对【建仓价】而非baseline，规避已知bug）
  - 输出：胜率、总/年化收益、相对 QQQ 基准的超额收益、最大回撤、简化夏普

数据来源：仅读本地缓存（data_cache.py），不在回测循环内发起网络请求。
用前需先跑一次 data_cache.ensure_cache(...) 缓存好价格与 PE。
"""

import statistics
from datetime import datetime

import data_cache as dc

# ============================================================
# 默认参数（与 strategy_screen.py 当前生产值一致，作为 baseline 对照组）
# ============================================================

DEFAULT_PARAMS = {
    "pe_min": 5.0,
    "pe_max": 60.0,
    "vol_min": 0.25,       # 年化波动率下限
    "vol_max": 999.0,      # 年化波动率上限（默认不限，可调优收紧）
    "drawdown_min": -15.0,
    "slope_annual_min": -20.0,
    "cross_ratio_min": 0.08,
    "ema_span": 20,
    "baseline_ema_w": 0.7,
    "entry_discount": 2.0,   # 入场折价阈值 %
    "tp_pct": 5.0,           # 止盈 %
    "sl_pct": 8.0,           # 止损 %（相对建仓价）
    "hold_days": 10,         # 最长持有交易日数，到期强制平仓
    "top_n": 5,              # 每轮建仓选几只
    "cost_pct": 0.1,         # 单边交易成本 %（买卖各收一次，合计 2×cost_pct）
    "market_filter": False,  # 是否启用大盘过滤
    "market_ma_days": 50,    # 大盘过滤：QQQ 相对其 N 日均线
    "rebalance_days": 10,    # 每隔多少个交易日重新选股建仓一轮
    "capital_per": 10000.0,
}


# ============================================================
# 基础计算（与 strategy_screen 一致的算法，参数化版本）
# ============================================================

def _ema(values, span):
    k = 2.0 / (span + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def _linreg_slope(ys):
    n = len(ys)
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den else 0.0


def _compute_stats(prices):
    closes = [p["close"] for p in prices]
    n = len(prices)
    rets = [(closes[i] / closes[i - 1] - 1) for i in range(1, n)]
    vol_annual = None
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        vol_annual = (var ** 0.5) * (252 ** 0.5)
    return {
        "close_last": closes[-1],
        "pct_period": (closes[-1] / closes[0] - 1) * 100,
        "volatility_annual": vol_annual,
        "high_period": max(p["high"] for p in prices if p["high"] is not None),
        "low_period": min(p["low"] for p in prices if p["low"] is not None),
    }


def _gate_pe(pe, params):
    if pe is None or pe <= 0:
        return False
    return params["pe_min"] <= pe <= params["pe_max"]


def _gate_volatility(vol_annual, params):
    if vol_annual is None:
        return False
    return params["vol_min"] <= vol_annual <= params["vol_max"]


def _gate_trend(prices, stats, params):
    closes = [p["close"] for p in prices]
    n = len(closes)
    pct_period = stats["pct_period"]
    slope = _linreg_slope(closes)
    avg_price = sum(closes) / n
    slope_annual = (slope * 252 / avg_price * 100) if avg_price else 0.0
    mean_close = sum(closes) / n
    crosses = 0
    below_count = 0
    for i in range(1, n):
        a = closes[i - 1] - mean_close
        b = closes[i] - mean_close
        if a == 0:
            continue
        if (a > 0) != (b > 0):
            crosses += 1
        if closes[i] < mean_close:
            below_count += 1
    cross_ratio = crosses / n
    below_ratio = below_count / n

    ok = True
    if pct_period < params["drawdown_min"]:
        ok = False
    if slope_annual < params["slope_annual_min"]:
        ok = False
    if cross_ratio < params["cross_ratio_min"]:
        ok = False
    if slope_annual < 0 and below_ratio > 0.6 and pct_period < 0 and cross_ratio < 0.12:
        ok = False
    return ok


def _gate_baseline(prices, params):
    closes = [p["close"] for p in prices]
    mean_c = statistics.mean(closes)
    median_c = statistics.median(closes)
    ema20 = _ema(closes, params["ema_span"])
    baseline = params["baseline_ema_w"] * ema20 + (1 - params["baseline_ema_w"]) * median_c
    current = closes[-1]
    discount = (current - baseline) / baseline * 100
    return {"baseline": baseline, "current": current, "discount_pct": discount,
            "cheap": discount <= -params["entry_discount"]}


def screen_ticker(ticker, cutoff_date, params, lookback=60):
    """截至 cutoff_date，用 params 判断该标的是否 BUY。返回 dict 或 None。"""
    rows = dc.load_prices(ticker)
    if not rows:
        return None
    prices = dc.slice_until(rows, cutoff_date, lookback)
    if not prices or len(prices) < 40:
        return None

    val = dc.load_valuation(ticker) or {}
    pe = val.get("trailingPE")
    stats = _compute_stats(prices)

    pe_ok = _gate_pe(pe, params)
    vol_ok = _gate_volatility(stats["volatility_annual"], params)
    trend_ok = _gate_trend(prices, stats, params)
    base = _gate_baseline(prices, params)

    verdict = "REJECT"
    if pe_ok and vol_ok and trend_ok:
        verdict = "BUY" if base["cheap"] else "WAIT"

    return {
        "ticker": ticker, "verdict": verdict, "entry": prices[-1]["close"],
        "pe": pe, "vol_annual": stats["volatility_annual"],
        "discount_pct": base["discount_pct"], "baseline": base["baseline"],
    }


def market_allows_entry(qqq_rows, cutoff_date, params):
    """大盘过滤：QQQ 收盘价是否在其 N 日均线之上。未启用则总允许。"""
    if not params.get("market_filter"):
        return True
    prices = dc.slice_until(qqq_rows, cutoff_date, params["market_ma_days"])
    if not prices or len(prices) < params["market_ma_days"]:
        return True  # 数据不足时不过滤，避免误伤
    closes = [p["close"] for p in prices]
    ma = sum(closes) / len(closes)
    return closes[-1] >= ma


# ============================================================
# 单笔交易模拟：止盈/止损/到期平仓
# ============================================================

def simulate_trade(ticker, entry_date, entry_price, params):
    rows = dc.load_prices(ticker)
    post = dc.slice_after(rows, entry_date, params["hold_days"])
    if not post:
        return None
    tp = entry_price * (1 + params["tp_pct"] / 100)
    sl = entry_price * (1 - params["sl_pct"] / 100)
    cost = 2 * params["cost_pct"]  # 买卖双边成本，百分比

    for i, p in enumerate(post, 1):
        if p["low"] is not None and p["low"] <= sl:
            return {"ticker": ticker, "entry_date": entry_date, "exit_date": p["date"],
                    "hold_days": i, "status": "STOP_LOSS",
                    "ret_pct": -params["sl_pct"] - cost}
        if p["high"] is not None and p["high"] >= tp:
            return {"ticker": ticker, "entry_date": entry_date, "exit_date": p["date"],
                    "hold_days": i, "status": "TAKE_PROFIT",
                    "ret_pct": params["tp_pct"] - cost}
    last = post[-1]
    ret = (last["close"] / entry_price - 1) * 100 - cost
    return {"ticker": ticker, "entry_date": entry_date, "exit_date": last["date"],
            "hold_days": len(post), "status": "EXPIRE", "ret_pct": ret}


# ============================================================
# 滚动回测主流程
# ============================================================

def rolling_backtest(universe, start_date, end_date, params, anchor_ticker="AAPL"):
    """
    在 [start_date, end_date] 内每隔 params['rebalance_days'] 个交易日建仓一轮，
    每轮选 top_n(BUY, 按折价排序)，各投 capital_per，用 hold_days 后判定止盈/止损/到期。
    返回 {trades: [...], rebalance_log: [...]}
    """
    p = dict(DEFAULT_PARAMS)
    p.update(params)

    anchor_rows = dc.load_prices(anchor_ticker)
    all_days = [d for d in dc.trading_days(anchor_rows, start_date, end_date)]
    if not all_days:
        return {"trades": [], "rebalance_log": []}

    qqq_rows = dc.load_prices("QQQ")

    rebalance_dates = all_days[::p["rebalance_days"]]

    trades = []
    rebalance_log = []

    for cutoff in rebalance_dates:
        if not market_allows_entry(qqq_rows, cutoff, p):
            rebalance_log.append({"date": cutoff, "action": "SKIP(市场过滤)", "picks": 0})
            continue
        candidates = []
        for t in universe:
            r = screen_ticker(t, cutoff, p)
            if r and r["verdict"] == "BUY":
                candidates.append(r)
        candidates.sort(key=lambda r: r["discount_pct"])
        picks = candidates[: p["top_n"]]
        rebalance_log.append({"date": cutoff, "action": "BUY", "picks": len(picks),
                               "tickers": [c["ticker"] for c in picks]})
        for c in picks:
            trade = simulate_trade(c["ticker"], cutoff, c["entry"], p)
            if trade:
                trade["pe"] = c["pe"]
                trade["vol_annual"] = c["vol_annual"]
                trade["discount_pct"] = c["discount_pct"]
                trades.append(trade)

    return {"trades": trades, "rebalance_log": rebalance_log, "params": p}


# ============================================================
# 绩效指标计算
# ============================================================

def compute_metrics(result, params, start_date, end_date):
    trades = result["trades"]
    p = result["params"]
    cap = p["capital_per"]

    if not trades:
        return {"n_trades": 0, "error": "无交易样本"}

    rets = [t["ret_pct"] for t in trades]
    wins = [r for r in rets if r > 0]
    win_rate = len(wins) / len(rets) * 100

    pnls = [cap * r / 100 for r in rets]
    total_pnl = sum(pnls)
    total_capital = cap * len(trades)
    total_return_pct = total_pnl / total_capital * 100 if total_capital else 0

    mean_ret = statistics.mean(rets)
    std_ret = statistics.pstdev(rets) if len(rets) > 1 else 0
    # 简化「逐笔夏普」：非严格日频夏普，仅作相对比较用的风险调整指标
    trade_sharpe = (mean_ret / std_ret * (len(rets) ** 0.5)) if std_ret else 0

    # 最大回撤：按平仓日排序后的累计盈亏曲线
    sorted_trades = sorted(zip([t["exit_date"] for t in trades], pnls))
    cum = 0
    peak = 0
    max_dd = 0
    for _, pnl in sorted_trades:
        cum += pnl
        peak = max(peak, cum)
        dd = peak - cum
        max_dd = max(max_dd, dd)
    max_dd_pct = max_dd / total_capital * 100 if total_capital else 0

    # QQQ 基准：同期买入持有收益
    qqq_rows = dc.load_prices("QQQ")
    qqq_window = [r for r in qqq_rows if start_date <= r["date"] <= end_date]
    qqq_ret = ((qqq_window[-1]["close"] / qqq_window[0]["close"] - 1) * 100
               if len(qqq_window) >= 2 else None)

    tp_n = sum(1 for t in trades if t["status"] == "TAKE_PROFIT")
    sl_n = sum(1 for t in trades if t["status"] == "STOP_LOSS")
    ex_n = sum(1 for t in trades if t["status"] == "EXPIRE")

    return {
        "n_trades": len(trades),
        "win_rate": round(win_rate, 1),
        "avg_ret_pct": round(mean_ret, 2),
        "total_pnl": round(total_pnl, 2),
        "total_capital": total_capital,
        "total_return_pct": round(total_return_pct, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "trade_sharpe": round(trade_sharpe, 2),
        "qqq_benchmark_pct": round(qqq_ret, 2) if qqq_ret is not None else None,
        "excess_vs_qqq_pct": (round(total_return_pct - qqq_ret, 2)
                               if qqq_ret is not None else None),
        "take_profit_n": tp_n, "stop_loss_n": sl_n, "expire_n": ex_n,
    }


def run(universe, start_date, end_date, params=None, anchor_ticker="AAPL"):
    """一站式：滚动回测 + 算指标。"""
    params = params or {}
    result = rolling_backtest(universe, start_date, end_date, params, anchor_ticker)
    metrics = compute_metrics(result, params, start_date, end_date)
    return {"trades": result["trades"], "rebalance_log": result["rebalance_log"],
            "params": result["params"], "metrics": metrics}
