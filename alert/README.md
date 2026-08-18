# SNDK 实时价格监控报警

监控 SanDisk（SNDK，NASDAQ）实时价格，价格突破设定阈值时自动弹出 macOS 通知 + 声音报警。

## 核心特性

- **WebSocket 实时流**：走 Yahoo `streamer.finance.yahoo.com`，与浏览器网页同源，秒级推送（约 1-2 秒一条）
- **全时段覆盖**：盘前（PRE）、主板/盘中（REGULAR）、盘后（POST）、**夜盘/24h 延长交易（OVERNIGHT）**
- **不受流控限制**：WebSocket 是服务端主动推送，不像 REST API 那样会被限流（429）
- **macOS 原生报警**：系统通知 + 声音，无需安装额外软件（用系统自带 `osascript`）
- **断线自动重连**：连接中断后 3 秒自动重连

## 为什么用 WebSocket 而不是普通接口

| 通道 | 能拿到的价格 | 是否限流 |
|------|------------|---------|
| REST API（`query1.finance.yahoo.com`） | 只有收盘定格价（如 1658），拿不到夜盘价 | 会限流（429） |
| **WebSocket（本脚本使用）** | 实时价，含夜盘（如 1724） | 基本不限流 |

夜盘价是浏览器通过 WebSocket 动态刷新上去的，REST API 拿不到——这就是本脚本必须用 WebSocket 的原因。

## 环境依赖

- Python 3
- yfinance（1.6.0+，内置 WebSocket 支持）

安装依赖：

```bash
python3 -m pip install yfinance
```

## 使用方法

### 基本用法

打开「终端」App，执行：

```bash
cd /Users/user/git/ai-berkshire/alert
python3 sndk_alert.py --threshold 1730
```

脚本会持续运行，实时监控 SNDK，价格 > 1730 美元时弹通知 + 响声。

**停止监控**：在终端里按 `Ctrl + C`。

### 后台运行（关掉终端也继续跑）

```bash
cd /Users/user/git/ai-berkshire/alert
nohup python3 -u sndk_alert.py --threshold 1730 --cooldown 0 > monitor.log 2>&1 &
```

- 日志会写入 `monitor.log`
- 实时查看日志：`tail -f monitor.log`
- 停止：先 `ps aux | grep sndk_alert` 找到进程号（PID），再 `kill <PID>`；或直接 `pkill -f sndk_alert.py`

## 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--symbol` | 股票代码 | `SNDK` |
| `--threshold` | 报警阈值（美元），价格 > 阈值时报警 | `1730` |
| `--cooldown` | 报警最小间隔秒数。`0` = 每条突破推送都报（真·实时）；`30` = 最多每 30 秒报一条 | `0` |
| `--position` | 持仓成本，格式 `单股成本:股数`，可多次传入。传入后每条行情都会实时计算并显示收益 | 无 |
| `--pnl-threshold` | 收益报警阈值（美元）。浮动收益 > 该值时触发通知+声音报警，需配合 `--position` | 无 |

### 持仓收益评估

传入持仓后，每条实时行情都会附带显示：市值、总成本、收益金额、收益率。

```bash
# 我的持仓：1 股 @ 1276.75，2 股 @ 1800/股（共 3 股，总成本 4876.75）
python3 sndk_alert.py --position 1276.75:1 --position 1800:2

# 也可用逗号分隔写在一起
python3 sndk_alert.py --position "1276.75:1,1800:2"
```

输出示例（价格后追加持仓收益）：

```
[22:15:03] SNDK 夜盘/延长价 = 1700.00 USD (阈值 1730) | 持仓 3股 市值 5100.00 成本 4876.75 收益 +223.25 (+4.58%)
```

启动时还会打印持仓明细和平均单股成本：

```
持仓明细：1股@1276.75；2股@1800.00
合计 3 股，总成本 4876.75 USD，平均单股成本 1625.58 USD
```

### 收益报警（收益 > 阈值时报警）

在持仓基础上加 `--pnl-threshold`，当浮动收益超过设定金额（美元）时，除价格报警外也弹通知+响声。阈值可自定义，`0` 表示一旦浮盈就报警。

```bash
# 浮动收益 > 0 时报警（回本转盈即提醒）
python3 sndk_alert.py --position 1276.75:1 --position 1800:2 --pnl-threshold 0

# 浮动收益 > 500 美元时报警
python3 sndk_alert.py --position 1276.75:1 --position 1800:2 --pnl-threshold 500

# 你的实际命令（后台运行，价格>1730 或 收益>0 时报警）
nohup python3 -u sndk_alert.py --threshold 1730 --cooldown 0 \
  --position 1726.75:1 --position 1800:2 --pnl-threshold 0 \
  > monitor.log 2>&1 &
```

价格报警和收益报警各自独立去重，都遵循 `--cooldown` 间隔。

### 参数示例

```bash
# 阈值改成 1700
python3 sndk_alert.py --threshold 1700

# 实时报警，但每 30 秒最多一条（避免价格在阈值附近抖动时刷屏）
python3 sndk_alert.py --threshold 1730 --cooldown 30

# 监控其他股票
python3 sndk_alert.py --symbol NVDA --threshold 200
```

## 报警模式说明

- **`--cooldown 0`（默认）**：只要收到 > 阈值的推送就立即报警，不去重。价格在阈值附近来回抖动时可能连续弹多条通知。
- **`--cooldown N`（N>0）**：实时监控，但两次报警间隔至少 N 秒，避免通知轰炸。

## 收不到通知怎么办

macOS 可能拦截了脚本通知，需开启权限：

1. 系统设置 → 通知
2. 找到「脚本编辑器」（Script Editor）或「终端」
3. 打开「允许通知」

## 日志说明

- 日志每条 WebSocket 推送都记录一行（约 1-2 秒一行），与实时行情同步
- 日志稀疏不代表监控慢——报警判断对每一条推送都实时执行，不受日志打印影响
- 日志里的 `sent 1000 (OK)` 是 WebSocket 正常关闭信号，脚本会自动重连，不是错误

## 注意事项

- 美股没有传统「夜盘」，脚本中的「夜盘/延长」指 24 小时延长交易时段（overnight session）
- WebSocket 推送为秒级实时，与交易所撮合仍可能有零点几到几秒延迟，用于阈值报警完全够用
- 价格单位为美元（USD）
