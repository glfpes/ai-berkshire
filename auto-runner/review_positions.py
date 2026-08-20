#!/usr/bin/env python3
"""
review_positions.py — auto-runner 模拟建仓复盘工具

针对 2026-08-12 的模拟建仓（见 reports/auto-runner-模拟建仓-20260812.md），
自动拉取建仓日至今每一天的行情，逐日判定「涨 5% 止盈 / 跌破建仓价下方 8% 止损」
是否触发，输出每只标的的每日曲线、触发日、持有天数与盈亏。

止盈/止损判定用「日内最高/最低价」而非收盘价：
  - 止盈：当日 high ≥ 建仓价 × (1 + 止盈%)  → 视为限价单成交，实现该涨幅
  - 止损：当日 low  ≤ baseline × (1 - 止损%) → 视为触发止损离场
  一旦触发即清仓，后续日不再计算。若两者同日触发，按保守起见先记止损。

用法：
  python3 review_positions.py                 # 复盘全部建仓
  python3 review_positions.py --ticker HON    # 只看某只
  python3 review_positions.py --json          # JSON 输出

局限：日线数据只有每日 OHLC，无分时；只能判「当日是否触及」，
      无法判具体时点，也不处理跳空开盘等细节。数据源 Yahoo Finance。

依赖：同目录 market_data.py
"""

import argparse
import json
from datetime import datetime

from market_data import fetch_prices

# ============================================================
# 建仓台账（来自 reports/auto-runner-模拟建仓-20260812.md）
# ============================================================

ENTRY_DATE = "2026-08-12"       # 建仓日（快照数据截至 08-11 收盘）
TAKE_PROFIT_PCT = 5.0           # 止盈：涨 5%
STOP_LOSS_PCT = 8.0             # 止损：跌破建仓价下方 8%
CAPITAL_PER = 10000.0           # 每只名义本金

POSITIONS = [
    # ticker, 建仓价, 整数股, baseline, 策略判断
    {"ticker": "HON",  "entry": 230.12, "shares": 43,  "baseline": 237.58, "verdict": "BUY"},
    {"ticker": "AAPL", "entry": 304.91, "shares": 32,  "baseline": 313.72, "verdict": "BUY"},
    {"ticker": "KDP",  "entry": 29.18,  "shares": 342, "baseline": 30.58,  "verdict": "BUY"},
    {"ticker": "MU",   "entry": 868.52, "shares": 11,  "baseline": 904.28, "verdict": "BUY(存疑)"},
    {"ticker": "SKHY", "entry": 141.65, "shares": 70,  "baseline": 149.56, "verdict": "REJECT(对照)"},
]


# ============================================================
# 复盘单只
# ============================================================

def review_one(pos, days=120):
    ticker = pos["ticker"]
    entry = pos["entry"]
    shares = pos["shares"]
    baseline = pos["baseline"]

    tp_price = round(entry * (1 + TAKE_PROFIT_PCT / 100), 4)   # 止盈目标价
    # 止损相对【建仓价】向下，而非相对 baseline
    # （baseline 可能高于建仓价，若用 baseline×0.92 会使止损线跑到建仓价上方，导致建仓即"被止损"）
    sl_price = round(entry * (1 - STOP_LOSS_PCT / 100), 4)     # 止损价

    prices = fetch_prices(ticker, days=days)
    if not prices:
        return {"ticker": ticker, "ok": False, "error": "无法获取行情"}

    # 只取建仓日（含）之后的交易日
    post = [p for p in prices if p["date"] >= ENTRY_DATE]
    # 若建仓日当天数据尚未产生（如当天盘中运行），post 可能为空
    daily = []
    status = "HOLD"          # HOLD / TAKE_PROFIT / STOP_LOSS
    exit_info = None

    for i, p in enumerate(post, 1):
        high, low, close = p["high"], p["low"], p["close"]
        hit_tp = high is not None and high >= tp_price
        hit_sl = low is not None and low <= sl_price
        max_gain = round((high / entry - 1) * 100, 2) if high else None
        close_gain = round((close / entry - 1) * 100, 2)

        row = {
            "day": i, "date": p["date"],
            "high": high, "low": low, "close": close,
            "max_gain_pct": max_gain, "close_gain_pct": close_gain,
            "flag": "HOLD",
        }

        if status == "HOLD":
            # 保守：同日既触止损又触止盈时，先记止损
            if hit_sl:
                status = "STOP_LOSS"
                row["flag"] = "STOP_LOSS"
                exit_info = {"date": p["date"], "day": i, "price": sl_price,
                             "ret_pct": round((sl_price / entry - 1) * 100, 2)}
            elif hit_tp:
                status = "TAKE_PROFIT"
                row["flag"] = "TAKE_PROFIT"
                exit_info = {"date": p["date"], "day": i, "price": tp_price,
                             "ret_pct": TAKE_PROFIT_PCT}
        else:
            row["flag"] = "CLOSED"  # 已离场后的交易日仅记录，不影响结果

        daily.append(row)

    # 盈亏计算
    if exit_info:
        ret_pct = exit_info["ret_pct"]
        pnl = round(CAPITAL_PER * ret_pct / 100, 2)
        hold_days = exit_info["day"]
    else:
        # 未触发：用最新收盘价计算浮动盈亏
        if daily:
            last_close = daily[-1]["close"]
            ret_pct = round((last_close / entry - 1) * 100, 2)
            pnl = round(CAPITAL_PER * ret_pct / 100, 2)
            hold_days = len(daily)
        else:
            ret_pct, pnl, hold_days = 0.0, 0.0, 0

    return {
        "ticker": ticker, "ok": True, "verdict": pos["verdict"],
        "entry": entry, "shares": shares, "baseline": baseline,
        "tp_price": tp_price, "sl_price": sl_price,
        "status": status, "exit": exit_info,
        "ret_pct": ret_pct, "pnl": pnl, "hold_days": hold_days,
        "daily": daily,
    }


# ============================================================
# 输出
# ============================================================

FLAG_MARK = {"HOLD": "持有", "TAKE_PROFIT": "✅止盈卖出", "STOP_LOSS": "❌止损离场",
             "CLOSED": "已离场"}
STATUS_MARK = {"HOLD": "🟡 持有中", "TAKE_PROFIT": "🟢 已止盈", "STOP_LOSS": "🔴 已止损"}


def print_one(r):
    if not r["ok"]:
        print(f"\n  {r['ticker']}：{r['error']}\n")
        return
    print(f"\n{'=' * 72}")
    print(f"  {r['ticker']}  [{r['verdict']}]   建仓 ${r['entry']} × {r['shares']}股")
    print(f"  止盈目标 ${r['tp_price']}(+{TAKE_PROFIT_PCT}%)   "
          f"止损线 ${r['sl_price']}(建仓价下方{STOP_LOSS_PCT}%)")
    print(f"{'-' * 72}")
    if not r["daily"]:
        print(f"  建仓日({ENTRY_DATE})至今尚无新交易日数据，等收盘后再复盘。")
        print(f"{'=' * 72}")
        return
    print(f"  {'Day':<5}{'日期':<12}{'最高':>10}{'最低':>10}{'收盘':>10}"
          f"{'盘中最大涨%':>12}{'状态':>12}")
    for d in r["daily"]:
        print(f"  {d['day']:<5}{d['date']:<12}"
              f"{d['high']:>10.2f}{d['low']:>10.2f}{d['close']:>10.2f}"
              f"{(d['max_gain_pct'] if d['max_gain_pct'] is not None else 0):>12.2f}"
              f"{FLAG_MARK.get(d['flag'], d['flag']):>12}")
    print(f"{'-' * 72}")
    print(f"  结果：{STATUS_MARK.get(r['status'])}   "
          f"持有 {r['hold_days']} 个交易日   "
          f"{'实现' if r['status'] != 'HOLD' else '浮动'}收益 {r['ret_pct']:+.2f}%   "
          f"盈亏 ${r['pnl']:+,.2f}")
    if r["exit"]:
        print(f"  离场：第 {r['exit']['day']} 天 {r['exit']['date']} @ ${r['exit']['price']}")
    print(f"{'=' * 72}")


def print_portfolio(results):
    ok = [r for r in results if r["ok"]]
    total_pnl = sum(r["pnl"] for r in ok)
    print(f"\n{'#' * 72}")
    print(f"  组合复盘汇总   建仓日 {ENTRY_DATE}   复盘日 {datetime.now():%Y-%m-%d}")
    print(f"  规则：涨 {TAKE_PROFIT_PCT}% 止盈 / 跌破建仓价下方 {STOP_LOSS_PCT}% 止损")
    print(f"{'#' * 72}")
    print(f"  {'标的':<8}{'判断':<14}{'状态':<10}{'持有天':>7}{'收益%':>9}{'盈亏$':>12}")
    for r in ok:
        st = {"HOLD": "持有中", "TAKE_PROFIT": "已止盈", "STOP_LOSS": "已止损"}[r["status"]]
        print(f"  {r['ticker']:<8}{r['verdict']:<14}{st:<10}"
              f"{r['hold_days']:>7}{r['ret_pct']:>9.2f}{r['pnl']:>12,.2f}")
    print(f"  {'-' * 68}")
    print(f"  组合合计盈亏：${total_pnl:+,.2f}（名义本金 ${CAPITAL_PER*len(ok):,.0f}）")
    err = [r for r in results if not r["ok"]]
    if err:
        print(f"  数据获取失败：{', '.join(r['ticker'] for r in err)}")
    print(f"{'#' * 72}\n")


def main():
    ap = argparse.ArgumentParser(description="auto-runner 模拟建仓复盘（止盈/止损判定）")
    ap.add_argument("--ticker", help="只复盘某只标的")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    positions = POSITIONS
    if args.ticker:
        positions = [p for p in POSITIONS if p["ticker"] == args.ticker.upper()]
        if not positions:
            ap.error(f"未找到建仓记录：{args.ticker}")

    results = [review_one(p) for p in positions]

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    for r in results:
        print_one(r)
    if len(results) > 1:
        print_portfolio(results)


if __name__ == "__main__":
    main()
