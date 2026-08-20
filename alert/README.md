# SNDK 实时价格监控报警

监控 SanDisk（SNDK，NASDAQ）实时价格，价格突破设定阈值时自动弹出 macOS 通知 + 声音报警。

## 脚本能做什么（能力一览）

`sndk_alert.py` 是一个**单文件常驻监控进程**：一条 Yahoo WebSocket 长连接拉实时价（覆盖夜盘），
对每一条推送同时跑 5 类独立报警判断，命中就同时发 macOS 通知 + Telegram 消息。

**① 行情采集**
- Yahoo Finance WebSocket 实时流，秒级推送（约 1-2 秒一条），不限流
- 全时段：盘前 / 主板 / 盘后 / 夜盘（24h 延长交易）——夜盘价 REST 接口拿不到
- 任意标的：`--symbol NVDA` 即可换股，不只 SNDK
- 断线 3 秒自动重连，可 `nohup` 后台常驻

**② 5 类报警（互相独立，可任意组合开启）**

| # | 能力 | 触发条件 | 参数 |
|---|------|---------|------|
| 1 | 上破报警 | 价格 > 阈值 | `--threshold`（默认 1730） |
| 2 | 下破报警 | 价格 < 下限 | `--low-threshold` |
| 3 | 涨跌速报警 | 窗口内涨跌**幅** \|%\| ≥ 阈值（涨跌都报） | `--surge-pct` + `--surge-window`（默认 300s） |
| 4 | 跳变报警 | 窗口内涨跌**额** \|USD\| ≥ 阈值（涨跌都报） | `--jump-amount` + `--jump-window`（默认 30s） |
| 5 | 收益 / 止损报警 | 浮盈 > X USD ／ 收益率 ≤ -X% | `--pnl-threshold` ／ `--stop-loss-pct`（需 `--position`） |

**③ 持仓收益实时计算**
- `--position 成本:股数` 可传多笔，自动汇总总股数 / 总成本 / 平均成本
- 每条行情实时算市值、净收益、收益率，直接打在日志里；报警通知也附带收益详情
- 价格突破 ≠ 已盈利，通知会如实显示净收益（可能仍为负）

**④ 通知与降噪**
- macOS 原生通知 + 声音（`osascript`，涨 Glass / 跌 Sosumi 区分方向）
- Telegram Bot 推送，人不在电脑前也能收到；后台线程发送，不阻塞行情
- `--cooldown` 控制每类报警的最小间隔，`0` = 每条推送都报，`30` = 最多 30 秒一条
- 五类报警各自独立去重，互不干扰

**能力边界（不做什么）**：不下单、不做任何交易；不存历史数据（仅内存滑动窗口）；
不做技术指标/回测；通知依赖 macOS，Telegram 推送需机器能连通 `api.telegram.org`。

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
- 停止：用配套脚本 `./stop.sh`（列出进程逐个确认）、`./stop.sh -y`（直接全杀）、`./stop.sh -9`（强制）
- 或手动：`ps aux | grep sndk_alert` 找到 PID 后 `kill <PID>`；也可 `pkill -f sndk_alert.py`

## 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--symbol` | 股票代码 | `SNDK` |
| `--threshold` | 报警阈值（美元），价格 > 阈值时报警 | `1730` |
| `--low-threshold` | 价格下限（美元），价格 < 该值时报警（配 Sosumi 提示音） | 无 |
| `--surge-pct` | 涨跌速报警阈值（百分比，正数）。过去 `--surge-window` 窗口内涨跌幅绝对值 ≥ 该值时报警（涨跌都报） | 无 |
| `--surge-window` | 涨跌速统计窗口（秒） | `300`（5分钟） |
| `--jump-amount` | 跳变报警阈值（美元，正数）。过去 `--jump-window` 窗口内涨跌**金额**绝对值 ≥ 该值时报警（涨跌都报） | 无 |
| `--jump-window` | 跳变统计窗口（秒） | `30` |
| `--cooldown` | 报警最小间隔秒数。`0` = 每条突破推送都报（真·实时）；`30` = 最多每 30 秒报一条 | `0` |
| `--position` | 持仓成本，格式 `单股成本:股数`，可多次传入。传入后每条行情都会实时计算并显示收益 | 无 |
| `--pnl-threshold` | 收益报警阈值（美元）。浮动收益 > 该值时触发通知+声音报警，需配合 `--position` | 无 |
| `--stop-loss-pct` | 止损报警阈值（百分比，须为负数）。收益率 ≤ 该值时触发报警，需配合 `--position` | 无 |
| `--telegram-token` | Telegram Bot token，配置后报警同时推送到 Telegram。不填则读环境变量 `TELEGRAM_TOKEN`，再退回文件内默认值 | 文件内默认值 |
| `--telegram-chat-id` | Telegram chat id。不填则读环境变量 `TELEGRAM_CHAT_ID`，再退回文件内默认值 | 文件内默认值 |

### 跳变报警（短时绝对涨跌额，涨跌都报）

和涨跌速（百分比）不同，这个按**绝对金额**触发。适合"过去 N 秒涨/跌了 X 美元就提醒"。

```bash
# 过去 30 秒涨或跌 >= 10 美元时报警
python3 sndk_alert.py --jump-amount 10

# 自定义窗口：过去 60 秒涨/跌 >= 20 美元时报警
python3 sndk_alert.py --jump-amount 20 --jump-window 60
```

- 涨跌额 = 当前价 − 窗口内最早报价；`abs(涨跌额) ≥ 阈值` 即触发
- 跌用 Sosumi 提示音、涨用 Glass；窗口自动滑动

### 涨跌速报警（短时快速波动，涨跌都报）

统计一个滑动窗口内的涨跌幅（窗口最早价 → 当前价），**涨幅或跌幅绝对值 ≥ 阈值**时报警，用于捕捉短时快速拉升或跳水。

```bash
# 过去 5 分钟涨幅或跌幅 >= 1% 时报警
python3 sndk_alert.py --surge-pct 1

# 自定义窗口：过去 3 分钟涨/跌幅 >= 2% 时报警
python3 sndk_alert.py --surge-pct 2 --surge-window 180
```

- 涨跌幅 = (当前价 − 窗口内最早报价) / 窗口内最早报价 × 100%
- **涨跌都报**：`abs(涨跌幅) ≥ 阈值` 即触发；跌用 Sosumi 提示音、涨用 Glass，便于区分
- 窗口自动滑动，超过 `--surge-window` 秒的旧报价被丢弃
- 数据不足（窗口内少于 2 个报价）时不报警
- 报警信息含涨/跌方向、窗口均价和报价条数

### 手机推送（Telegram Bot）

报警时除 macOS 本地通知外，同时推送到 Telegram，人不在电脑前也能收到。免费、无条数限制。

**配置步骤：**
1. Telegram 里找 `@BotFather`，`/newbot` 创建 bot，拿到 token
2. 给自己的 bot 发一条消息，再访问 `https://api.telegram.org/bot<token>/getUpdates` 拿 `chat.id`
3. 启动时带上（三选一，优先级：命令行 > 环境变量 > 文件内默认值）：

```bash
# 方式一：命令行参数
python3 sndk_alert.py --threshold 1730 \
  --telegram-token XXX --telegram-chat-id YYY

# 方式二：环境变量（推荐，避免 token 出现在进程列表/日志里）
export TELEGRAM_TOKEN=XXX TELEGRAM_CHAT_ID=YYY
python3 sndk_alert.py --threshold 1730

# 方式三：不传，用 sndk_alert.py 顶部硬编码的默认 token
```

启动时会打印「Telegram 推送：已启用 / 未配置」，据此确认是否生效。五类报警都会同时推送。

> 注意：本仓库是公开仓库，脚本顶部硬编码的 token 一旦泄露他人即可控制该 bot，建议自建 bot 并改用环境变量。
> 国内环境需保证运行机器能连通 `api.telegram.org`（代理 / VPN）。推送在后台线程发送，不影响行情监控。

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

### 止损报警（亏损达到 -X% 时报警）

用 `--stop-loss-pct` 设置止损线（须为负数），当浮动收益率 ≤ 该百分比时触发报警（配独立提示音 Sosumi）。适合执行止损纪律。

```bash
# 亏损达到 -8% 时报警（你的持仓：2股@1800 + 1股@1726.7581，均价 1775.59，止损价约 1633.54）
python3 sndk_alert.py --position 1726.7581:1 --position 1800:2 --stop-loss-pct -8

# 完整：价格突破 1730 / 收益转正 / 亏损到 -8% 三重报警
python3 sndk_alert.py --threshold 1730 --cooldown 0 \
  --position 1726.7581:1 --position 1800:2 \
  --pnl-threshold 0 --stop-loss-pct -8
```

启动时会打印止损线对应的约当价格，方便核对。

上破 / 下破 / 涨跌速 / 跳变 / 收益 / 止损各类报警各自独立去重，都遵循 `--cooldown` 间隔。

### 报警通知内容

价格突破报警的通知会附带**持仓收益详情**（有持仓时），包括市值、成本、净收益金额和收益率，例如：

```
标题：SNDK 突破 1740.00 USD
内容：夜盘/延长价 1740.00 已突破阈值 1730
     持仓 3 股 | 市值 5220.00 | 成本 5326.75 | 净收益 -106.75 USD (-2.00%)
```

注意：价格突破阈值不代表已盈利——通知会如实显示净收益（可能仍为负），方便一眼判断是否真赚钱。

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
