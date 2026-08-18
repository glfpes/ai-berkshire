#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SNDK 实时价格监控 + 报警脚本（WebSocket 实时流版）

功能：
- 通过 Yahoo Finance WebSocket 实时流读取 SNDK 价格，覆盖：
  盘前(PRE)、盘中/主板(REGULAR)、盘后(POST)、以及夜盘/24h延长交易(OVERNIGHT)
- WebSocket 是 Yahoo 网页前端同源的实时推送，秒级更新，能拿到 REST API 拿不到的夜盘价
- 当价格 > 阈值(默认 1730 美元) 时，触发 macOS 系统通知 + 声音报警
- 报警去重：突破一次只提醒一次，回落到阈值下方后重新武装
- 断线自动重连

用法：
    python3 sndk_alert.py                    # 默认 SNDK，阈值 1730
    python3 sndk_alert.py --threshold 1700   # 自定义阈值
    python3 sndk_alert.py --symbol SNDK      # 自定义标的
    python3 sndk_alert.py --cooldown 300     # 报警后静默秒数(默认300)

    # 持仓收益评估（--position 单股成本:股数，可多笔，逗号或空格分隔）
    python3 sndk_alert.py --position 1276.75:1 --position 1800:2
    python3 sndk_alert.py --position "1276.75:1,1800:2"

    # 收益报警：浮动收益 > 0 时也报警（阈值可自定义）
    python3 sndk_alert.py --position 1276.75:1 --position 1800:2 --pnl-threshold 0
    python3 sndk_alert.py --position 1276.75:1 --position 1800:2 --pnl-threshold 500

依赖：pip install yfinance
"""

import argparse
import subprocess
import sys
import threading
import time
from datetime import datetime

try:
    import yfinance as yf
except ImportError:
    sys.exit("缺少依赖，请先运行: python3 -m pip install yfinance")


# 市场时段标记（Yahoo WebSocket 的 marketHours 字段编码）
MARKET_HOURS = {
    0: "盘前",
    1: "主板/盘中",
    2: "盘后",
    3: "夜盘/延长",
}


def parse_positions(items):
    """解析持仓参数，返回 [(单股成本, 股数), ...]。

    支持格式：--position 1276.75:1 --position 1800:2
    或 --position "1276.75:1,1800:2"（逗号/空格分隔多笔）。
    每笔为 "单股成本:股数"。
    """
    lots = []
    if not items:
        return lots
    raw = []
    for it in items:
        raw.extend(part for part in it.replace(",", " ").split())
    for part in raw:
        if ":" not in part:
            sys.exit(f"持仓格式错误: '{part}'，应为 '单股成本:股数'，如 1276.75:1")
        cost_str, qty_str = part.split(":", 1)
        try:
            cost = float(cost_str)
            qty = float(qty_str)
        except ValueError:
            sys.exit(f"持仓格式错误: '{part}'，成本和股数必须是数字")
        if qty <= 0:
            sys.exit(f"持仓股数必须 > 0: '{part}'")
        lots.append((cost, qty))
    return lots


def position_summary(lots):
    """汇总持仓，返回 (总股数, 总成本, 平均单股成本)。"""
    total_qty = sum(q for _, q in lots)
    total_cost = sum(c * q for c, q in lots)
    avg_cost = total_cost / total_qty if total_qty else 0.0
    return total_qty, total_cost, avg_cost


def notify_macos(title: str, message: str, sound: str = "Glass") -> None:
    """发送 macOS 系统通知 + 声音（系统自带 osascript，无需额外安装）。"""
    safe_title = title.replace('"', '\\"')
    safe_msg = message.replace('"', '\\"')
    script = (
        f'display notification "{safe_msg}" '
        f'with title "{safe_title}" sound name "{sound}"'
    )
    # 非阻塞执行，避免高频报警时阻塞主线程（osascript 通知已自带声音）
    try:
        subprocess.Popen(["osascript", "-e", script])
    except Exception as e:
        print(f"[warn] 通知发送失败: {e}", file=sys.stderr)


class Alerter:
    def __init__(self, symbol, threshold, cooldown, lots=None, pnl_threshold=None):
        self.symbol = symbol
        self.threshold = threshold
        self.cooldown = cooldown
        self.armed = True            # 是否处于“可报警”状态
        self.last_alert_ts = 0.0     # 上次价格报警时间戳
        self.last_pnl_alert_ts = 0.0 # 上次收益报警时间戳
        self.last_print = 0.0        # 上次打印时间（限制刷屏）
        self.lots = lots or []       # 持仓明细 [(单股成本, 股数), ...]
        self.total_qty, self.total_cost, self.avg_cost = position_summary(self.lots)
        self.pnl_threshold = pnl_threshold  # 收益报警阈值(美元)，None=不启用

    def pnl_str(self, price):
        """根据当前价与持仓，返回收益字符串；无持仓返回空串。"""
        if self.total_qty <= 0:
            return ""
        market_value = price * self.total_qty
        pnl = market_value - self.total_cost
        pnl_pct = pnl / self.total_cost * 100 if self.total_cost else 0.0
        sign = "+" if pnl >= 0 else ""
        return (f" | 持仓 {self.total_qty:g}股 市值 {market_value:.2f} "
                f"成本 {self.total_cost:.2f} 收益 {sign}{pnl:.2f} "
                f"({sign}{pnl_pct:.2f}%)")

    def on_message(self, msg):
        if msg.get("id") != self.symbol:
            return
        price = msg.get("price")
        if price is None:
            return

        mh = msg.get("marketHours")
        session = MARKET_HOURS.get(mh, f"时段{mh}") if mh is not None else "夜盘/延长"
        now = time.time()
        ts = datetime.now().strftime("%H:%M:%S")

        # 每条推送都打印，日志与实时行情同步（约每 2-3 秒一条）
        flag = " >>> 突破!" if price > self.threshold else ""
        pnl_str = self.pnl_str(price)
        print(f"[{ts}] {self.symbol} {session}价 = {price:.2f} USD "
              f"(阈值 {self.threshold}){pnl_str}{flag}")
        self.last_print = now

        # 实时模式：只要当前价 > 阈值，且距上次报警 >= cooldown 就报警
        # cooldown=0 时，每条突破推送都会报警（真·实时提醒）
        if price > self.threshold and (now - self.last_alert_ts) >= self.cooldown:
            title = f"SNDK 报警：{price:.2f} USD"
            message = f"{session}价 {price:.2f} 已突破阈值 {self.threshold}"
            notify_macos(title, message)
            self.last_alert_ts = now
            print(f"[{ts}] *** 已发送报警：{price:.2f} > {self.threshold} ***")

        # 收益报警：当浮动收益 > pnl_threshold 时报警（需有持仓且已启用）
        if (self.pnl_threshold is not None and self.total_qty > 0
                and (now - self.last_pnl_alert_ts) >= self.cooldown):
            pnl = price * self.total_qty - self.total_cost
            if pnl > self.pnl_threshold:
                pnl_pct = pnl / self.total_cost * 100 if self.total_cost else 0.0
                title = f"SNDK 收益报警：+{pnl:.2f} USD"
                message = (f"{session}价 {price:.2f}，浮动收益 +{pnl:.2f} "
                           f"(+{pnl_pct:.2f}%) 已超阈值 {self.pnl_threshold}")
                notify_macos(title, message)
                self.last_pnl_alert_ts = now
                print(f"[{ts}] *** 已发送收益报警：+{pnl:.2f} > {self.pnl_threshold} ***")


def run(symbol, threshold, cooldown, lots=None, pnl_threshold=None):
    alerter = Alerter(symbol, threshold, cooldown, lots, pnl_threshold)
    print(f"开始实时监控 {symbol}，阈值 > {threshold} USD"
          f"（WebSocket 实时流，覆盖盘前/主板/盘后/夜盘）。Ctrl+C 退出。")
    if alerter.total_qty > 0:
        print(f"持仓明细：" + "；".join(
            f"{q:g}股@{c:.2f}" for c, q in alerter.lots))
        print(f"合计 {alerter.total_qty:g} 股，总成本 {alerter.total_cost:.2f} USD，"
              f"平均单股成本 {alerter.avg_cost:.2f} USD")
        if pnl_threshold is not None:
            print(f"收益报警：浮动收益 > {pnl_threshold} USD 时触发")
    print()

    while True:
        ws = None
        try:
            ws = yf.WebSocket()
            ws.subscribe([symbol])
            ws.listen(alerter.on_message)
        except KeyboardInterrupt:
            print("\n已停止监控。")
            try:
                if ws: ws.close()
            except Exception:
                pass
            return
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] 连接中断: {type(e).__name__}，"
                  f"3 秒后重连...", file=sys.stderr)
            try:
                if ws: ws.close()
            except Exception:
                pass
            time.sleep(3)


def main():
    ap = argparse.ArgumentParser(description="SNDK 实时价格监控报警(WebSocket版)")
    ap.add_argument("--symbol", default="SNDK", help="股票代码，默认 SNDK")
    ap.add_argument("--threshold", type=float, default=1730.0,
                    help="报警阈值(美元)，价格 > 阈值时报警，默认 1730")
    ap.add_argument("--cooldown", type=int, default=0,
                    help="报警最小间隔秒数，0=每条突破都报(实时)，默认 0")
    ap.add_argument("--position", action="append", default=None,
                    help="持仓，格式 '单股成本:股数'，可多次传入，"
                         "如 --position 1276.75:1 --position 1800:2")
    ap.add_argument("--pnl-threshold", type=float, default=None,
                    help="收益报警阈值(美元)，浮动收益 > 该值时报警，"
                         "需配合 --position 使用，如 --pnl-threshold 0")
    args = ap.parse_args()

    lots = parse_positions(args.position)

    if args.pnl_threshold is not None and not lots:
        sys.exit("--pnl-threshold 需要配合 --position 使用（先录入持仓）")

    try:
        run(args.symbol, args.threshold, args.cooldown, lots, args.pnl_threshold)
    except KeyboardInterrupt:
        print("\n已停止监控。")


if __name__ == "__main__":
    main()
