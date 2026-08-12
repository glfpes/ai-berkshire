#!/usr/bin/env python3
"""
batch_screen.py — 波段策略批量筛选器

对一批股票代号并发跑 strategy_screen 的四关体检，汇总 BUY / WAIT / REJECT。
用于扫描指数成分股（如 Nasdaq-100），快速找出适合波段低吸的标的。

用法：
  python3 batch_screen.py --nasdaq100          # 扫描内置 Nasdaq-100 名单
  python3 batch_screen.py NVDA MU AVGO AAPL    # 扫描指定标的
  python3 batch_screen.py --nasdaq100 --json   # JSON 输出
  python3 batch_screen.py --nasdaq100 --workers 12

依赖：同目录 strategy_screen.py / market_data.py
"""

import argparse
import concurrent.futures
import json
import sys

from strategy_screen import screen

# Nasdaq-100 成分股（截至知识范围的主流名单，实际成分以交易所公告为准）
NASDAQ100 = [
    "AAPL", "MSFT", "NVDA", "AMZN", "AVGO", "META", "GOOGL", "GOOG", "TSLA", "COST",
    "NFLX", "ADBE", "AMD", "PEP", "LIN", "TMUS", "CSCO", "INTU", "QCOM", "TXN",
    "AMGN", "ISRG", "CMCSA", "AMAT", "HON", "BKNG", "VRTX", "PANW", "ADP", "GILD",
    "SBUX", "MU", "REGN", "ADI", "LRCX", "MDLZ", "KLAC", "SNPS", "CDNS", "PYPL",
    "MELI", "MAR", "CRWD", "ORLY", "CTAS", "ASML", "ABNB", "CEG", "PDD", "MRVL",
    "FTNT", "DASH", "WDAY", "NXPI", "ADSK", "CPRT", "ROP", "PCAR", "MNST", "PAYX",
    "AEP", "ROST", "ODFL", "FANG", "KDP", "CHTR", "DDOG", "FAST", "EA", "VRSK",
    "CTSH", "EXC", "GEHC", "KHC", "CSGP", "BKR", "TTWO", "IDXX", "XEL", "ANSS",
    "CCEP", "ON", "DXCM", "ZS", "TEAM", "CDW", "BIIB", "MDB", "GFS", "ILMN",
    "WBD", "MRNA", "DLTR", "WBA", "SIRI", "LULU", "AZN", "TTD", "ARM", "SMCI",
]


def run_one(ticker, days):
    try:
        return screen(ticker, days=days)
    except Exception as e:
        return {"ticker": ticker, "ok": False, "error": f"异常: {e}"}


def batch(tickers, days=60, workers=8):
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(run_one, t, days): t for t in tickers}
        done = 0
        total = len(tickers)
        for fut in concurrent.futures.as_completed(futs):
            r = fut.result()
            results.append(r)
            done += 1
            print(f"\r  进度 {done}/{total} ...", end="", file=sys.stderr, flush=True)
    print("", file=sys.stderr)
    return results


def _sort_key(r):
    # BUY 内部按折价越深越靠前（越便宜越好）
    return r["gates"]["baseline"]["discount_pct"]


def print_summary(results, days):
    ok = [r for r in results if r.get("ok")]
    err = [r for r in results if not r.get("ok")]

    buys = [r for r in ok if r["verdict"] == "BUY"]
    waits = [r for r in ok if r["verdict"] == "WAIT"]
    rejects = [r for r in ok if r["verdict"] == "REJECT"]

    buys.sort(key=_sort_key)          # 折价最深在前
    waits.sort(key=_sort_key)

    print(f"\n{'=' * 78}")
    print(f"  Nasdaq-100 波段策略批量筛选   |   回溯 {days} 交易日   |   共 {len(results)} 只")
    print(f"{'=' * 78}")
    print(f"  🟢 BUY {len(buys)}   🟡 WAIT {len(waits)}   🔴 REJECT {len(rejects)}   "
          f"⚠️ 数据不足 {len(err)}")

    print(f"\n  {'─' * 74}")
    print(f"  🟢 BUY —— 适合投资（四关全过 + 当前价低于 baseline）")
    print(f"  {'─' * 74}")
    if buys:
        print(f"  {'代码':<8}{'现价':>10}{'baseline':>11}{'折价%':>8}"
              f"{'PE':>7}{'年化波动%':>10}{'区间涨跌%':>10}")
        for r in buys:
            b = r["gates"]["baseline"]
            v = r["gates"]["volatility"]
            pe = r["gates"]["pe"]["pe"]
            print(f"  {r['ticker']:<8}{b['current']:>10.2f}{b['baseline']:>11.2f}"
                  f"{b['discount_pct']:>8.2f}{pe:>7.1f}"
                  f"{(v['vol_annual']*100):>10.1f}{r['stats']['pct_period']:>10.2f}")
    else:
        print("  （本次扫描无 BUY 标的）")

    print(f"\n  {'─' * 74}")
    print(f"  🟡 WAIT —— 质地合格但当前价不够便宜（四关过，价≥baseline，等回落）")
    print(f"  {'─' * 74}")
    if waits:
        print(f"  {'代码':<8}{'现价':>10}{'baseline':>11}{'折价%':>8}{'PE':>7}{'年化波动%':>10}")
        for r in waits:
            b = r["gates"]["baseline"]
            v = r["gates"]["volatility"]
            pe = r["gates"]["pe"]["pe"]
            print(f"  {r['ticker']:<8}{b['current']:>10.2f}{b['baseline']:>11.2f}"
                  f"{b['discount_pct']:>8.2f}{pe:>7.1f}{(v['vol_annual']*100):>10.1f}")
    else:
        print("  （无）")

    if err:
        print(f"\n  ⚠️ 数据不足/获取失败：{', '.join(r['ticker'] for r in err)}")

    print(f"\n  {'─' * 74}")
    print(f"  提示：BUY 按折价从深到浅排序。折价越深=相对近期越便宜，但仍须遵守")
    print(f"        分批建仓、止盈 3%~5%、跌破 baseline 下方 8% 止损的纪律。")
    print(f"{'=' * 78}\n")


def main():
    ap = argparse.ArgumentParser(description="波段策略批量筛选（Nasdaq-100 等）")
    ap.add_argument("tickers", nargs="*", help="指定标的；留空需配合 --nasdaq100")
    ap.add_argument("--nasdaq100", action="store_true", help="扫描内置 Nasdaq-100 名单")
    ap.add_argument("--days", type=int, default=60, help="回溯交易日数（默认 60）")
    ap.add_argument("--workers", type=int, default=8, help="并发数（默认 8）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    if args.nasdaq100:
        tickers = NASDAQ100
    elif args.tickers:
        tickers = [t.upper() if "." not in t else t for t in args.tickers]
    else:
        ap.error("请指定标的，或使用 --nasdaq100")

    results = batch(tickers, days=args.days, workers=args.workers)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print_summary(results, args.days)


if __name__ == "__main__":
    main()
