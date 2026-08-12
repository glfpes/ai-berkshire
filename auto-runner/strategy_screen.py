#!/usr/bin/env python3
"""
strategy_screen.py — 波动率均值回归短线波段策略 · 单标的筛选器

输入 1 个美股代号，自动拉取近 60 天成交数据 + PE，跑完四道关卡，
输出一份简要数字报告，并直接给出「该不该买」的判断。

策略四关（详见 auto-runner-v1.md）：
  第一关  PE 体检   —— PE 为正且落在合理区间（默认 5~60）
  第二关  波动体检  —— 年化波动率够高（默认 ≥ 25%），有波段空间
  第三关  趋势体检  —— 是「区间震荡」而非「单边下跌」（生死线）
  第四关  baseline —— 算出合理价格中枢，看当前价是否已在其下方折价

用法：
  python3 strategy_screen.py SKHY
  python3 strategy_screen.py NVDA --json
  python3 strategy_screen.py TSLA --days 60

判断结果：
  BUY     四关全过 + 当前价低于 baseline 达入场阈值 → 可分批低吸
  WAIT    四关全过，但当前价不够便宜（≥ baseline 或折价不足）→ 空仓等待
  REJECT  任意一关不过（PE不合理 / 波动不足 / 单边下跌）→ 不做

依赖：同目录 market_data.py（数据层）
"""

import argparse
import json
import statistics
import sys

from market_data import get_market_data

# ============================================================
# 策略阈值（可按行业/风险偏好调整）
# ============================================================

PE_MIN = 5.0          # PE 下限（过低往往是价值陷阱/异常）
PE_MAX = 60.0         # PE 上限（过高=泡沫风险）
VOL_ANNUAL_MIN = 0.25  # 年化波动率下限（低于此没有波段空间）

# 第三关：趋势/震荡判定
DRAWDOWN_MIN = -15.0   # 60天区间累计涨跌下限（%），跌破视为单边下跌
SLOPE_ANNUAL_MIN = -20.0  # 年化回归斜率下限（%），更负视为下行趋势
CROSS_RATIO_MIN = 0.08    # 穿越次数/交易日 的最小比例，太低=不震荡

# 第四关：baseline 与入场折价
EMA_SPAN = 20          # baseline 用的 EMA 周期（近期加权，抗趋势市滞后）
BASELINE_EMA_W = 0.7   # baseline = EMA_W×EMA20 + (1-EMA_W)×中位数
ENTRY_DISCOUNT = 2.0   # 当前价需低于 baseline 至少 2% 才算「便宜」（配合EMA贴合特性下调）


# ============================================================
# 关卡实现
# ============================================================

def gate_pe(valuation):
    """第一关：PE 体检"""
    pe = valuation.get("trailingPE") if valuation else None
    if pe is None:
        return {"pass": False, "pe": None,
                "reason": "无 PE 数据（可能亏损/无 TTM 盈利），无法确认估值底"}
    if pe <= 0:
        return {"pass": False, "pe": round(pe, 2),
                "reason": f"PE={pe:.2f} 为负，公司亏损/基本面恶化，一票否决"}
    if pe < PE_MIN:
        return {"pass": False, "pe": round(pe, 2),
                "reason": f"PE={pe:.2f} 过低（<{PE_MIN}），警惕价值陷阱/一次性收益"}
    if pe > PE_MAX:
        return {"pass": False, "pe": round(pe, 2),
                "reason": f"PE={pe:.2f} 过高（>{PE_MAX}），估值泡沫，杀估值风险大"}
    return {"pass": True, "pe": round(pe, 2),
            "reason": f"PE={pe:.2f} 落在合理区间 [{PE_MIN}, {PE_MAX}]"}


def compute_atr_pct(prices):
    """ATR(真实波幅) 占均价比例，衡量日内波动。"""
    trs = []
    for i in range(1, len(prices)):
        h = prices[i]["high"]
        low = prices[i]["low"]
        pc = prices[i - 1]["close"]
        if h is None or low is None or pc is None:
            continue
        tr = max(h - low, abs(h - pc), abs(low - pc))
        trs.append(tr)
    if not trs:
        return None
    atr = sum(trs) / len(trs)
    avg_close = sum(p["close"] for p in prices) / len(prices)
    return atr / avg_close * 100 if avg_close else None


def gate_volatility(stats, atr_pct):
    """第二关：波动体检"""
    vol = stats.get("volatility_annual")
    if vol is None:
        return {"pass": False, "vol_annual": None, "atr_pct": atr_pct,
                "reason": "波动率数据不足"}
    if vol < VOL_ANNUAL_MIN:
        return {"pass": False, "vol_annual": round(vol, 4), "atr_pct": atr_pct,
                "reason": f"年化波动率 {vol*100:.1f}% 偏低（<{VOL_ANNUAL_MIN*100:.0f}%），"
                          f"波段空间不足"}
    return {"pass": True, "vol_annual": round(vol, 4), "atr_pct": atr_pct,
            "reason": f"年化波动率 {vol*100:.1f}%，ATR≈{atr_pct:.1f}%，波段空间充足"}


def _linreg_slope(ys):
    """最小二乘斜率（x=0..n-1）。"""
    n = len(ys)
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den else 0.0


def gate_trend(prices, stats):
    """第三关：趋势体检 —— 区分「震荡」与「单边下跌」（生死线）"""
    closes = [p["close"] for p in prices]
    n = len(closes)

    # 1) 区间累计涨跌
    pct_period = stats["pct_period"]

    # 2) 回归斜率 → 年化
    slope = _linreg_slope(closes)  # 每交易日的价格变化
    avg_price = sum(closes) / n
    slope_annual = (slope * 252 / avg_price * 100) if avg_price else 0.0

    # 3) 穿越均值次数（震荡度）
    mean_close = sum(closes) / n
    crosses = 0
    below_streak_ratio = 0
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

    reasons = []
    ok = True
    if pct_period < DRAWDOWN_MIN:
        ok = False
        reasons.append(f"60天累计 {pct_period:+.1f}% 深跌（<{DRAWDOWN_MIN}%）")
    if slope_annual < SLOPE_ANNUAL_MIN:
        ok = False
        reasons.append(f"回归斜率年化 {slope_annual:+.1f}% 显著向下")
    if cross_ratio < CROSS_RATIO_MIN:
        ok = False
        reasons.append(f"穿越中枢频率 {cross_ratio:.2f} 过低（不震荡）")
    # 单边下跌典型特征：斜率向下 + 大部分时间在均值下方 + 累计为负
    if slope_annual < 0 and below_ratio > 0.6 and pct_period < 0 and cross_ratio < 0.12:
        ok = False
        reasons.append("典型单边下跌形态（持续处于下行中枢下方）")

    if ok:
        reason = (f"区间 {pct_period:+.1f}%、斜率年化 {slope_annual:+.1f}%、"
                  f"穿越中枢 {crosses} 次 → 常规震荡，非单边下跌")
    else:
        reason = "；".join(reasons)

    return {
        "pass": ok,
        "pct_period": pct_period,
        "slope_annual": round(slope_annual, 1),
        "crosses": crosses,
        "cross_ratio": round(cross_ratio, 3),
        "below_ratio": round(below_ratio, 3),
        "reason": reason,
    }


def _ema(values, span):
    """指数移动平均（近期权重更高）。span 越小越贴近当前价。"""
    k = 2.0 / (span + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def gate_baseline(prices):
    """第四关：计算 baseline（合理价格中枢）+ 当前价折溢价

    baseline 主用 EMA20（近期加权），辅以中位数抗极值：
        baseline = BASELINE_EMA_W × EMA20 + (1-BASELINE_EMA_W) × 中位数
    相比纯 60 天均值，EMA 在趋势/急跌市不会被 60 天前的旧价拖偏，
    从而避免「均值假便宜」导致的接飞刀。同时展示均值供对照。
    """
    closes = [p["close"] for p in prices]
    mean_c = statistics.mean(closes)       # 纯60天均值（对照用，趋势市会失真）
    median_c = statistics.median(closes)   # 中位数（抗极值）
    ema20 = _ema(closes, EMA_SPAN)         # EMA20（近期加权，主锚）

    baseline = BASELINE_EMA_W * ema20 + (1 - BASELINE_EMA_W) * median_c
    current = closes[-1]
    discount = (current - baseline) / baseline * 100  # 负=当前价低于baseline（便宜）

    return {
        "baseline": round(baseline, 4),
        "ema20": round(ema20, 4),
        "mean": round(mean_c, 4),
        "median": round(median_c, 4),
        "current": round(current, 4),
        "discount_pct": round(discount, 2),  # 负数=便宜
        "cheap": discount <= -ENTRY_DISCOUNT,
    }


# ============================================================
# 综合决策
# ============================================================

def decide(gates):
    """根据四关结果给出 BUY / WAIT / REJECT"""
    pe = gates["pe"]
    vol = gates["volatility"]
    trend = gates["trend"]
    base = gates["baseline"]

    # 硬性关卡：PE、波动、趋势，任一不过 → REJECT
    if not pe["pass"]:
        return "REJECT", f"估值不合格：{pe['reason']}"
    if not trend["pass"]:
        return "REJECT", f"趋势不合格（单边下跌风险）：{trend['reason']}"
    if not vol["pass"]:
        return "REJECT", f"波动不足：{vol['reason']}"

    # 三关全过，看价格是否便宜
    if base["cheap"]:
        return "BUY", (f"四关全过，当前价 ${base['current']} 低于 baseline "
                       f"${base['baseline']} 达 {abs(base['discount_pct']):.1f}%，"
                       f"处于低吸区间")
    else:
        rel = "高于" if base["discount_pct"] > 0 else "接近"
        return "WAIT", (f"四关全过，但当前价 ${base['current']} {rel} baseline "
                        f"${base['baseline']}（{base['discount_pct']:+.1f}%），"
                        f"不够便宜，空仓等待回落至 baseline 下方 {ENTRY_DISCOUNT:.0f}%")


def screen(ticker, days=60):
    md = get_market_data(ticker, days=days)
    prices = md["prices"]
    if not prices or len(prices) < 20:
        return {"ticker": ticker, "ok": False,
                "error": "成交数据不足（无法获取或交易日太少），无法评估"}

    stats = md["stats"]
    valuation = md["valuation"]
    atr_pct = compute_atr_pct(prices)

    gates = {
        "pe": gate_pe(valuation),
        "volatility": gate_volatility(stats, round(atr_pct, 2) if atr_pct else None),
        "trend": gate_trend(prices, stats),
        "baseline": gate_baseline(prices),
    }
    verdict, rationale = decide(gates)

    return {
        "ticker": ticker,
        "ok": True,
        "as_of": md["as_of"],
        "days": stats["trading_days"],
        "range": f"{stats['date_start']} ~ {stats['date_end']}",
        "stats": stats,
        "valuation": valuation,
        "gates": gates,
        "verdict": verdict,
        "rationale": rationale,
    }


# ============================================================
# 报告输出
# ============================================================

VERDICT_MARK = {"BUY": "🟢 BUY  可考虑低吸",
                "WAIT": "🟡 WAIT 空仓等待",
                "REJECT": "🔴 REJECT 不做"}


def _pf(ok):
    return "✅通过" if ok else "❌淘汰"


def print_report(r):
    t = r["ticker"]
    if not r["ok"]:
        print(f"\n  {t}：{r['error']}\n")
        return
    s = r["stats"]
    g = r["gates"]
    base = g["baseline"]

    print(f"\n{'=' * 66}")
    print(f"  {t}  波段策略体检报告   ({r['range']}, {r['days']}个交易日)")
    print(f"{'=' * 66}")

    # 数字快照
    print(f"  当前价 ${base['current']}    区间涨跌 {s['pct_period']:+.2f}%    "
          f"区间高/低 ${s['high_period']:.2f}/{s['low_period']:.2f}")
    print(f"  日均成交量 {s['avg_volume']:,} 股")

    print(f"\n  ── 四关体检 ──")
    # 第一关 PE
    print(f"  [1] PE 体检   {_pf(g['pe']['pass'])}")
    print(f"      {g['pe']['reason']}")
    # 第二关 波动
    v = g["volatility"]
    print(f"  [2] 波动体检  {_pf(v['pass'])}")
    print(f"      {v['reason']}")
    # 第三关 趋势
    tr = g["trend"]
    print(f"  [3] 趋势体检  {_pf(tr['pass'])}  ← 生死线：是否单边下跌")
    print(f"      {tr['reason']}")
    # 第四关 baseline
    print(f"  [4] baseline 中枢 ${base['baseline']}  "
          f"(EMA20 ${base['ema20']} 为主 / 中位${base['median']} / 纯均值${base['mean']}对照)")
    print(f"      当前价相对 baseline {base['discount_pct']:+.2f}%  "
          f"{'（低于中枢，便宜）' if base['discount_pct'] < 0 else '（不便宜）'}")

    print(f"\n  {'─' * 62}")
    print(f"  结论：{VERDICT_MARK.get(r['verdict'], r['verdict'])}")
    print(f"  理由：{r['rationale']}")
    if r["verdict"] == "BUY":
        print(f"  纪律：分批低吸，止盈 3%~5% 清仓，跌破 baseline 下方 8% 止损")
    print(f"{'=' * 66}\n")


def main():
    ap = argparse.ArgumentParser(
        description="波动率均值回归波段策略 · 单标的筛选（PE+波动+趋势+baseline）")
    ap.add_argument("ticker", help="美股代号，如 SKHY / NVDA / TSLA")
    ap.add_argument("--days", type=int, default=60, help="回溯交易日数（默认 60）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    ticker = args.ticker if "." in args.ticker else args.ticker.upper()
    r = screen(ticker, days=args.days)

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print_report(r)


if __name__ == "__main__":
    main()
