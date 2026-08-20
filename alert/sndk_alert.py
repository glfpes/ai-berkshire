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
    python3 sndk_alert.py --threshold 1700   # 自定义阈值(价格 > 阈值报警)
    python3 sndk_alert.py --low-threshold 1540  # 价格下限(价格 < 1540 报警)
    python3 sndk_alert.py --surge-pct 1         # 涨速(过去5分钟涨幅>=1%报警)
    python3 sndk_alert.py --symbol SNDK      # 自定义标的
    python3 sndk_alert.py --cooldown 300     # 报警后静默秒数(默认300)

    # 持仓收益评估（--position 单股成本:股数，可多笔，逗号或空格分隔）
    python3 sndk_alert.py --position 1276.75:1 --position 1800:2
    python3 sndk_alert.py --position "1276.75:1,1800:2"

    # 收益报警：浮动收益 > 0 时也报警（阈值可自定义）
    python3 sndk_alert.py --position 1276.75:1 --position 1800:2 --pnl-threshold 0
    python3 sndk_alert.py --position 1276.75:1 --position 1800:2 --pnl-threshold 500

    # 止损报警：亏损达到 -8% 时报警（阈值可自定义，须为负数）
    python3 sndk_alert.py --position 1726.7581:1 --position 1800:2 --stop-loss-pct -8

    # Telegram 推送：报警时推送到 Telegram（无限量、免费）
    # token 从 @BotFather 创建 bot 获取，chat_id 从 getUpdates 获取
    python3 sndk_alert.py --threshold 1730 --telegram-token XXX --telegram-chat-id YYY
    export TELEGRAM_TOKEN=XXX TELEGRAM_CHAT_ID=YYY  # 或用环境变量

依赖：pip install yfinance（Telegram 推送走标准库，无需额外依赖）
注意：国内需保证运行机器能连通 api.telegram.org（代理/VPN）
"""

import argparse
import os
import subprocess
import sys
import threading
import time
from collections import deque
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


# Telegram Bot 推送配置：默认硬编码，可被命令行/环境变量覆盖
# TELEGRAM_TOKEN: @BotFather 创建 bot 得到；TELEGRAM_CHAT_ID: 你与 bot 的会话 id
# 注意：本仓库为公开仓库，token 泄露后他人可控制该 bot，推送前请酌情替换/撤销
TELEGRAM_TOKEN = "8516931756:AAGNL58Q3cR_oYPjEZVZO_H3Hx_p0aEuv68"
TELEGRAM_CHAT_ID = "5174451436"


def notify_telegram(title: str, message: str) -> None:
    """通过 Telegram Bot 推送。无限量、免费。未配置 token/chat_id 时静默跳过。

    走标准库 urllib，无需额外依赖；在后台线程发送，避免阻塞行情主线程。
    国内环境需保证运行机器能连通 api.telegram.org（代理/VPN）。
    """
    token = TELEGRAM_TOKEN
    chat_id = TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return

    def _send():
        import urllib.parse
        import urllib.request
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        text = f"{title}\n{message}" if title else message
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except Exception as e:
            print(f"[warn] Telegram 推送失败: {e}", file=sys.stderr)

    threading.Thread(target=_send, daemon=True).start()


def send_alert(title: str, message: str, sound: str = "Glass") -> None:
    """统一报警出口：同时发 macOS 本地通知 + Telegram 推送。"""
    notify_macos(title, message, sound=sound)
    notify_telegram(title, message)


class Alerter:
    def __init__(self, symbol, threshold, cooldown, lots=None, pnl_threshold=None,
                 stop_loss_pct=None, low_threshold=None,
                 surge_pct=None, surge_window=300,
                 jump_amount=None, jump_window=30):
        self.symbol = symbol
        self.threshold = threshold
        self.cooldown = cooldown
        self.armed = True            # 是否处于“可报警”状态
        self.last_alert_ts = 0.0     # 上次价格报警时间戳
        self.last_pnl_alert_ts = 0.0 # 上次收益报警时间戳
        self.last_sl_alert_ts = 0.0  # 上次止损报警时间戳
        self.last_low_alert_ts = 0.0 # 上次价格下限报警时间戳
        self.last_surge_alert_ts = 0.0  # 上次涨速报警时间戳
        self.last_jump_alert_ts = 0.0   # 上次跳变(绝对额)报警时间戳
        self.last_print = 0.0        # 上次打印时间（限制刷屏）
        self.lots = lots or []       # 持仓明细 [(单股成本, 股数), ...]
        self.total_qty, self.total_cost, self.avg_cost = position_summary(self.lots)
        self.pnl_threshold = pnl_threshold  # 收益报警阈值(美元)，None=不启用
        self.stop_loss_pct = stop_loss_pct  # 止损报警阈值(%,负数如-8)，None=不启用
        self.low_threshold = low_threshold  # 价格下限报警阈值(美元)，价格<该值报警，None=不启用
        self.surge_pct = surge_pct      # 涨速报警阈值(%)，窗口内涨幅>=该值报警，None=不启用
        self.surge_window = surge_window  # 涨速统计窗口(秒)，默认300=5分钟
        self.price_window = deque()     # 涨速滑动窗口 [(时间戳, 价格), ...]
        self.jump_amount = jump_amount  # 跳变报警阈值(美元)，窗口内涨跌额绝对值>=该值报警，None=不启用
        self.jump_window = jump_window  # 跳变统计窗口(秒)，默认30
        self.jump_price_window = deque()  # 跳变滑动窗口 [(时间戳, 价格), ...]

    def compute_pnl(self, price):
        """计算持仓收益，返回 (市值, 净收益, 收益率%)；无持仓返回 None。"""
        if self.total_qty <= 0:
            return None
        market_value = price * self.total_qty
        pnl = market_value - self.total_cost
        pnl_pct = pnl / self.total_cost * 100 if self.total_cost else 0.0
        return market_value, pnl, pnl_pct

    def pnl_str(self, price):
        """根据当前价与持仓，返回收益字符串；无持仓返回空串。"""
        res = self.compute_pnl(price)
        if res is None:
            return ""
        market_value, pnl, pnl_pct = res
        sign = "+" if pnl >= 0 else ""
        return (f" | 持仓 {self.total_qty:g}股 市值 {market_value:.2f} "
                f"成本 {self.total_cost:.2f} 收益 {sign}{pnl:.2f} "
                f"({sign}{pnl_pct:.2f}%)")

    def pnl_detail(self, price):
        """返回用于通知的收益详情文本；无持仓返回空串。"""
        res = self.compute_pnl(price)
        if res is None:
            return ""
        market_value, pnl, pnl_pct = res
        sign = "+" if pnl >= 0 else ""
        return (f"\n持仓 {self.total_qty:g} 股 | 市值 {market_value:.2f} | "
                f"成本 {self.total_cost:.2f} | 净收益 {sign}{pnl:.2f} USD "
                f"({sign}{pnl_pct:.2f}%)")

    def update_window(self, now, price):
        """更新滑动窗口：加入新点，弹出超过 surge_window 秒的旧点。"""
        self.price_window.append((now, price))
        cutoff = now - self.surge_window
        while self.price_window and self.price_window[0][0] < cutoff:
            self.price_window.popleft()

    def window_change_pct(self):
        """返回窗口内涨幅% = (当前价 - 窗口最早价)/窗口最早价*100。
        数据不足(只有1个点)时返回 None。同时返回窗口均价供展示。"""
        if len(self.price_window) < 2:
            return None, None
        first_price = self.price_window[0][1]
        last_price = self.price_window[-1][1]
        if first_price <= 0:
            return None, None
        change = (last_price - first_price) / first_price * 100
        avg = sum(p for _, p in self.price_window) / len(self.price_window)
        return change, avg

    def update_jump_window(self, now, price):
        """更新跳变滑动窗口：加入新点，弹出超过 jump_window 秒的旧点。"""
        self.jump_price_window.append((now, price))
        cutoff = now - self.jump_window
        while self.jump_price_window and self.jump_price_window[0][0] < cutoff:
            self.jump_price_window.popleft()

    def window_change_amount(self):
        """返回窗口内涨跌额 = 当前价 - 窗口最早价。数据不足返回 (None, None)。
        同时返回窗口最早价供展示。"""
        if len(self.jump_price_window) < 2:
            return None, None
        first_price = self.jump_price_window[0][1]
        last_price = self.jump_price_window[-1][1]
        return last_price - first_price, first_price

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

        # 维护涨速滑动窗口（仅在启用时）
        if self.surge_pct is not None:
            self.update_window(now, price)

        # 维护跳变(绝对额)滑动窗口（仅在启用时）
        if self.jump_amount is not None:
            self.update_jump_window(now, price)

        # 每条推送都打印，日志与实时行情同步（约每 2-3 秒一条）
        if price > self.threshold:
            flag = " >>> 突破!"
        elif self.low_threshold is not None and price < self.low_threshold:
            flag = " <<< 跌破下限!"
        else:
            flag = ""
        pnl_str = self.pnl_str(price)
        print(f"[{ts}] {self.symbol} {session}价 = {price:.2f} USD "
              f"(阈值 {self.threshold}){pnl_str}{flag}")
        self.last_print = now

        # 实时模式：只要当前价 > 阈值，且距上次报警 >= cooldown 就报警
        # cooldown=0 时，每条突破推送都会报警（真·实时提醒）
        if price > self.threshold and (now - self.last_alert_ts) >= self.cooldown:
            detail = self.pnl_detail(price)
            title = f"SNDK 突破 {price:.2f} USD"
            message = f"{session}价 {price:.2f} 已突破阈值 {self.threshold}{detail}"
            send_alert(title, message)
            self.last_alert_ts = now
            print(f"[{ts}] *** 已发送报警：{price:.2f} > {self.threshold}"
                  f"{self.pnl_str(price)} ***")

        # 收益报警：当浮动收益 > pnl_threshold 时报警（需有持仓且已启用）
        if (self.pnl_threshold is not None and self.total_qty > 0
                and (now - self.last_pnl_alert_ts) >= self.cooldown):
            pnl = price * self.total_qty - self.total_cost
            if pnl > self.pnl_threshold:
                pnl_pct = pnl / self.total_cost * 100 if self.total_cost else 0.0
                title = f"SNDK 收益报警：+{pnl:.2f} USD"
                message = (f"{session}价 {price:.2f}，浮动收益 +{pnl:.2f} "
                           f"(+{pnl_pct:.2f}%) 已超阈值 {self.pnl_threshold}")
                send_alert(title, message)
                self.last_pnl_alert_ts = now
                print(f"[{ts}] *** 已发送收益报警：+{pnl:.2f} > {self.pnl_threshold} ***")

        # 止损报警：当收益率 <= stop_loss_pct(如 -8) 时报警（需有持仓且已启用）
        if (self.stop_loss_pct is not None and self.total_qty > 0
                and (now - self.last_sl_alert_ts) >= self.cooldown):
            pnl = price * self.total_qty - self.total_cost
            pnl_pct = pnl / self.total_cost * 100 if self.total_cost else 0.0
            if pnl_pct <= self.stop_loss_pct:
                title = f"SNDK 止损报警：{pnl_pct:.2f}%"
                message = (f"{session}价 {price:.2f}，浮动亏损 {pnl:.2f} USD "
                           f"({pnl_pct:.2f}%) 已触及止损线 {self.stop_loss_pct}%")
                send_alert(title, message, sound="Sosumi")
                self.last_sl_alert_ts = now
                print(f"[{ts}] *** 已发送止损报警：{pnl_pct:.2f}% <= "
                      f"{self.stop_loss_pct}% (亏损 {pnl:.2f}) ***")

        # 价格下限报警：当价格 < low_threshold 时报警（纯价格，不依赖持仓）
        if (self.low_threshold is not None
                and price < self.low_threshold
                and (now - self.last_low_alert_ts) >= self.cooldown):
            detail = self.pnl_detail(price)
            title = f"SNDK 跌破 {price:.2f} USD"
            message = (f"{session}价 {price:.2f} 已跌破下限 "
                       f"{self.low_threshold}{detail}")
            send_alert(title, message, sound="Sosumi")
            self.last_low_alert_ts = now
            print(f"[{ts}] *** 已发送下限报警：{price:.2f} < "
                  f"{self.low_threshold} ***")

        # 涨速报警：过去 surge_window 秒内涨跌幅绝对值 >= surge_pct 时报警(涨跌都报)
        if (self.surge_pct is not None
                and (now - self.last_surge_alert_ts) >= self.cooldown):
            change, win_avg = self.window_change_pct()
            if change is not None and abs(change) >= self.surge_pct:
                mins = self.surge_window / 60
                sign = "+" if change >= 0 else ""
                direction = "涨" if change >= 0 else "跌"
                title = f"SNDK {direction}速报警：{mins:g}分钟 {sign}{change:.2f}%"
                message = (f"{session}价 {price:.2f}，过去 {mins:g} 分钟{direction}幅 "
                           f"{sign}{change:.2f}% (窗口均价 {win_avg:.2f}，"
                           f"{len(self.price_window)}个报价) 绝对值已达阈值 "
                           f"{self.surge_pct}%")
                send_alert(title, message, sound="Sosumi" if change < 0 else "Glass")
                self.last_surge_alert_ts = now
                print(f"[{ts}] *** 已发送{direction}速报警：{mins:g}分钟 "
                      f"{sign}{change:.2f}% (|{change:.2f}| >= {self.surge_pct}%) ***")

        # 跳变报警：过去 jump_window 秒内涨跌额绝对值 >= jump_amount(美元) 时报警(涨跌都报)
        if (self.jump_amount is not None
                and (now - self.last_jump_alert_ts) >= self.cooldown):
            amount, first_price = self.window_change_amount()
            if amount is not None and abs(amount) >= self.jump_amount:
                sign = "+" if amount >= 0 else ""
                direction = "涨" if amount >= 0 else "跌"
                title = f"SNDK {direction}{abs(amount):.2f}美元：{price:.2f} USD"
                message = (f"{session}价 {price:.2f}，过去 {self.jump_window}秒 "
                           f"{direction} {sign}{amount:.2f} USD "
                           f"(由 {first_price:.2f} → {price:.2f}) "
                           f"已达阈值 {self.jump_amount} USD")
                send_alert(title, message, sound="Sosumi" if amount < 0 else "Glass")
                self.last_jump_alert_ts = now
                print(f"[{ts}] *** 已发送跳变报警：{self.jump_window}秒 "
                      f"{sign}{amount:.2f} (|{amount:.2f}| >= {self.jump_amount}) ***")


def run(symbol, threshold, cooldown, lots=None, pnl_threshold=None,
        stop_loss_pct=None, low_threshold=None,
        surge_pct=None, surge_window=300,
        jump_amount=None, jump_window=30):
    alerter = Alerter(symbol, threshold, cooldown, lots, pnl_threshold,
                      stop_loss_pct, low_threshold, surge_pct, surge_window,
                      jump_amount, jump_window)
    print(f"开始实时监控 {symbol}，阈值 > {threshold} USD"
          f"（WebSocket 实时流，覆盖盘前/主板/盘后/夜盘）。Ctrl+C 退出。")
    if low_threshold is not None:
        print(f"价格下限报警：价格 < {low_threshold} USD 时触发")
    if surge_pct is not None:
        print(f"涨跌速报警：过去 {surge_window/60:g} 分钟涨/跌幅 >= {surge_pct}% "
              f"时触发（涨跌都报）")
    if jump_amount is not None:
        print(f"跳变报警：过去 {jump_window} 秒涨/跌 >= {jump_amount} USD "
              f"时触发（涨跌都报）")
    if alerter.total_qty > 0:
        print(f"持仓明细：" + "；".join(
            f"{q:g}股@{c:.2f}" for c, q in alerter.lots))
        print(f"合计 {alerter.total_qty:g} 股，总成本 {alerter.total_cost:.2f} USD，"
              f"平均单股成本 {alerter.avg_cost:.2f} USD")
        if pnl_threshold is not None:
            print(f"收益报警：浮动收益 > {pnl_threshold} USD 时触发")
        if stop_loss_pct is not None:
            sl_price = alerter.avg_cost * (1 + stop_loss_pct / 100)
            print(f"止损报警：收益率 <= {stop_loss_pct}% 时触发"
                  f"（约当价格 {sl_price:.2f} USD）")
    print("Telegram 推送：" + ("已启用" if (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
                              else "未配置"))
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
    ap.add_argument("--low-threshold", type=float, default=None,
                    help="价格下限报警(美元)，价格 < 该值时报警，如 --low-threshold 1540")
    ap.add_argument("--surge-pct", type=float, default=None,
                    help="涨跌速报警阈值(百分比,正数)，过去窗口内涨跌幅绝对值 >= 该值时报警"
                         "(涨跌都报)，如 --surge-pct 1")
    ap.add_argument("--surge-window", type=int, default=300,
                    help="涨速统计窗口(秒)，默认 300(5分钟)")
    ap.add_argument("--jump-amount", type=float, default=None,
                    help="跳变报警阈值(美元,正数)，过去窗口内涨跌额绝对值 >= 该值时报警"
                         "(涨跌都报)，如 --jump-amount 10")
    ap.add_argument("--jump-window", type=int, default=30,
                    help="跳变统计窗口(秒)，默认 30")
    ap.add_argument("--cooldown", type=int, default=0,
                    help="报警最小间隔秒数，0=每条突破都报(实时)，默认 0")
    ap.add_argument("--position", action="append", default=None,
                    help="持仓，格式 '单股成本:股数'，可多次传入，"
                         "如 --position 1276.75:1 --position 1800:2")
    ap.add_argument("--pnl-threshold", type=float, default=None,
                    help="收益报警阈值(美元)，浮动收益 > 该值时报警，"
                         "需配合 --position 使用，如 --pnl-threshold 0")
    ap.add_argument("--stop-loss-pct", type=float, default=None,
                    help="止损报警阈值(百分比,负数)，收益率 <= 该值时报警，"
                         "需配合 --position 使用，如 --stop-loss-pct -8")
    ap.add_argument("--telegram-token", default=None,
                    help="Telegram Bot token，配置后报警推送到 Telegram；"
                         "不填则读环境变量 TELEGRAM_TOKEN 或文件默认值")
    ap.add_argument("--telegram-chat-id", default=None,
                    help="Telegram chat id；不填则读环境变量 TELEGRAM_CHAT_ID 或文件默认值")
    args = ap.parse_args()

    global TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
    # 命令行/环境变量优先，否则用文件顶部硬编码的默认值
    TELEGRAM_TOKEN = (args.telegram_token or os.environ.get("TELEGRAM_TOKEN")
                      or TELEGRAM_TOKEN)
    TELEGRAM_CHAT_ID = (args.telegram_chat_id or os.environ.get("TELEGRAM_CHAT_ID")
                        or TELEGRAM_CHAT_ID)

    lots = parse_positions(args.position)

    if args.pnl_threshold is not None and not lots:
        sys.exit("--pnl-threshold 需要配合 --position 使用（先录入持仓）")
    if args.stop_loss_pct is not None and not lots:
        sys.exit("--stop-loss-pct 需要配合 --position 使用（先录入持仓）")
    if args.stop_loss_pct is not None and args.stop_loss_pct > 0:
        sys.exit("--stop-loss-pct 应为负数（止损是亏损），如 -8 表示亏损 8%")
    if args.surge_pct is not None and args.surge_pct <= 0:
        sys.exit("--surge-pct 应为正数（涨跌幅绝对值阈值），如 1 表示涨或跌 1%")
    if args.jump_amount is not None and args.jump_amount <= 0:
        sys.exit("--jump-amount 应为正数（涨跌额绝对值阈值），如 10 表示涨或跌 10 美元")

    try:
        run(args.symbol, args.threshold, args.cooldown, lots,
            args.pnl_threshold, args.stop_loss_pct, args.low_threshold,
            args.surge_pct, args.surge_window,
            args.jump_amount, args.jump_window)
    except KeyboardInterrupt:
        print("\n已停止监控。")


if __name__ == "__main__":
    main()
