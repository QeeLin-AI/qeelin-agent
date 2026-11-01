# QeeLin-Agent

<p align="center">
  <a href="#license"><img alt="License" src="https://img.shields.io/badge/License-MIT-green.svg"></a>
  <a href="#"><img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-blue.svg"></a>
  <a href="#"><img alt="Version" src="https://img.shields.io/badge/version-1.0.0--beta-orange.svg"></a>
  <a href="#"><img alt="Status" src="https://img.shields.io/badge/status-research%20%2F%20education-lightgrey.svg"></a>
</p>

币圈永续合约的**多智能体交易框架 + 自动因子研究循环**。四角色（分析师 → 研究员 → 交易员 → 风控）协作出决策，研究循环让 Agent 自己写因子、跑回测、迭代晋级。

**核心依赖只有 `numpy` + `pandas`**，不接任何 API Key 也能完整跑通。

---

## 目录

- [特性](#特性)
- [架构](#架构)
- [快速开始](#快速开始)
- [命令](#命令)
- [Web 控制台](#web-控制台)
- [HTTP API](#http-api)
- [配置](#配置)
- [因子系统](#因子系统)
- [交易策略与风控](#交易策略与风控)
- [三种运行模式](#三种运行模式)
- [目录结构](#目录结构)
- [已知限制](#已知限制)
- [路线图](#路线图)
- [风险声明](#风险声明)
- [License](#license)

---

## 特性

- **多智能体决策流水线**：三个分析师并发研判（技术面 / 资金费率 / 情绪）→ 多空辩论 N 轮 → 交易员输出结构化订单 → 风控官与风控引擎双重把关。
- **自动因子研究循环**：LLM 提出因子 → 沙箱执行 → 前视偏差检测 → train/valid/test 三段回测 → 留出集评分 → 晋级/淘汰 → 反思迭代。
- **前视偏差自动检测**：用前 45%/60%/75%/90% 的数据重算因子，不一致即判定偷看未来。
- **确定性风控引擎**：代码拥有最终否决权，LLM 只能收紧、不能放宽。
- **多数据源 + 实时行情**：币安永续 / Gate 永续 / 合成数据兜底，实时成交价逐秒更新。
- **自主选品种**：按因子信号扫描多品种，自动切换到最有「机会」的标的（有持仓时不切）。
- **中频叠加信号**：决策时实时拉 5m K 线算唐奇安突破，与 1h 中期信号叠加。
- **本地 Web 控制台**：总览 / 决策 / 因子研究 / 交易四个面板，零前端依赖。
- **三种撮合模式**：本地模拟盘 → 币安测试网 → 主网实盘（多道安全锁）。

---

## 架构

```
                    ┌──────────────────────────┐
                    │  数据层 data.py           │
                    │  合成行情 / 币安永续 K线   │
                    └────────────┬─────────────┘
                                 │
        ┌────────────────────────┴────────────────────────┐
        │                                                 │
        ▼                                                 ▼
┌───────────────────┐                          ┌──────────────────────┐
│ 因子研究循环        │                          │ 多智能体决策流水线      │
│ research.py       │                          │ agents/pipeline.py   │
│                   │                          │                      │
│ 提议 → 沙箱执行     │                          │ 分析师(并发 3 个)      │
│   → 前视检测        │                          │   ↓                  │
│   → train 回测     │                          │ 研究员(多空辩论 N 轮)   │
│   → valid 回测     │                          │   ↓                  │
│   → 评分/淘汰       │                          │ 交易员(结构化 JSON)    │
│   → 反思 → 下一轮   │                          │   ↓                  │
│                   │                          │ 风控官(LLM，只能收紧)  │
│ 产出：排行榜+冠军因子 │                          │   ↓                  │
└─────────┬─────────┘                          │ 风控引擎(代码，否决权) │
          │                                     └──────────┬───────────┘
          ▼                                                ▼
   factors_library/*.py                           runs/decisions/*.json
```

---

## 快速开始

要求：**Python 3.10+**。

```bash
# 1. 装依赖
pip install -r requirements.txt

# 2. 端到端演示（研究 → 回测 → 决策）
python run.py demo            # 或 Windows 双击 run.cmd demo / Linux ./run.sh demo
```

一条命令跑完三件事：**Agent 写因子并迭代 → 冠军因子回测 → 多智能体出决策**。

> 默认走真实行情（启动时自动下载币安 K 线）。想离线跑通，把 `config/default.json` 的 `market.source` 改成 `synthetic`。

---

## 命令

```bash
python run.py <命令> [选项]
```

| 命令 | 作用 |
|---|---|
| `demo` | 端到端演示：研究循环 + 冠军因子回测 + 多智能体决策 |
| `research -n 10` | 只跑因子研究循环 |
| `backtest --factor momentum_96` | 回测内置因子 |
| `backtest --code my_factor.py` | 回测自定义因子代码 |
| `agents --rounds 3` | 只跑一次多智能体决策 |
| `web --port 8000` | 启动本地 Web 控制台 |
| `download --symbol ETHUSDT --bars 3000` | 下载币安永续行情到缓存 |
| `factors` | 列出内置因子 |
| `exchtest` | 用 `.env` 里的 Key 试连交易所，翻译错误码 |
| `token` | 打印带访问令牌的控制台地址 |
| `llmtest` | LLM 接入体检（文本 / JSON / 完整决策） |
| `netcheck` | 网络体检（行情 / 下单 / 实时价各域名连通性） |

通用参数：`--source synthetic|binance`、`--symbol`、`--bars N`、`--llm mock|openai`、`--refresh`。

### 测试

```bash
python -m unittest discover -s tests -v
```

覆盖：沙箱逃逸、前视检测、回测成本、强平、风控否决、Web API、实盘解锁护栏、止损/止盈双单。

---

## Web 控制台

```bash
python run.py web --port 8000    # 浏览器打开 http://127.0.0.1:8000
```

| 面板 | 功能 |
|---|---|
| **总览** | 实时价、盈亏、当日盈亏/回撤、持仓、最近成交、机会信号排行榜 |
| **决策** | 一键跑多智能体，实时看分析师 → 辩论 → 提案 → 风控 → 订单 |
| **因子研究** | 输入迭代次数，实时出排行榜（IS/OOS 夏普、回撤、淘汰原因） |
| **交易** | 模拟/测试网/主网切换、解锁、自动循环、执行/平仓、同步账户 |

> 服务默认只监听 `127.0.0.1`。绑定非本机地址且未设 `CRYPTOAGENT_WEB_TOKEN` 时，服务会**拒绝启动**。

---

## HTTP API

所有界面操作对应一个 JSON 接口，可直接用 curl / 脚本调用：

```
GET  /api/status                 # 账户、持仓、自动循环、模式
GET  /api/candles?limit=240      # K 线
GET  /api/live                   # 实时报价状态
GET  /api/symbols                # 品种清单
POST /api/symbol                 # {symbol} 切换活跃品种（有持仓会拒绝）
POST /api/scan                   # 所有品种各跑一遍决策，出对比表
POST /api/opportunity            # 机会信号排行榜（refresh=true 强制重扫）
POST /api/data/refresh           # 强制重新下载行情
GET  /api/decisions/latest       # 最近一次决策
GET  /api/research/latest        # 最近一次研究
POST /api/decision               # 跑一次决策
POST /api/research               # 跑因子研究 {iterations}
POST /api/trading/arm            # 解锁 {mode, confirm, testnet}
POST /api/trading/disarm
POST /api/trading/execute        # 执行上次决策的订单
POST /api/trading/close          # 平仓
POST /api/trading/auto           # {running, interval_sec, execute}
POST /api/killswitch             # {on: true} 一键熔断
GET  /api/exchange/status        # 交易所连接/余额/持仓
POST /api/exchange/keys          # {key, secret} 存进 .env 并自检
POST /api/exchange/sync          # 同步交易所账户
```

---

## 配置

所有配置在 `config/default.json` 和 `.env`（复制 `.env.example` 生成）。

### LLM（可选）

默认 `mock`：不调用模型，用内置规则引擎兜底，可完全离线跑。接任意 OpenAI 兼容网关（DeepSeek / 通义 / Kimi / vLLM / Ollama）：

```bash
CRYPTOAGENT_LLM_PROVIDER=openai
CRYPTOAGENT_LLM_BASE_URL=https://api.deepseek.com/v1
CRYPTOAGENT_LLM_MODEL=deepseek-chat
OPENAI_API_KEY=sk-xxxx
```

接入后：分析师并发调用、研究循环切到 LLM 自己写因子、交易员输出强制 JSON；**任何一次调用失败都自动降级到规则兜底并标注 `⚠ LLM 降级`**，绝不静默编造。

### 币安合约 API

默认留空 = 只能跑本地模拟盘：

```bash
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
```

> 创建时**只勾「允许合约交易」，绝对不要勾「允许提现」**，并绑定 IP 白名单。

### 风控参数（`config/default.json` 的 `risk` 段）

| 参数 | 默认 | 含义 |
|---|---|---|
| `max_leverage` | 20 | 杠杆上限 |
| `max_position_pct` | 0.5 | 最大保证金占用（50%） |
| `max_loss_per_trade_pct` | 0.05 | 单笔最大亏损（5% 权益） |
| `daily_loss_limit_pct` | 0.05 | 当日亏损熔断线 |
| `max_drawdown_pct` | 0.2 | 总回撤熔断线 |
| `min_confidence` | 0.45 | 最低置信度门槛 |

> **关键认知**：单笔最大亏损 = `仓位 × 杠杆 × 止损`，被 `max_loss_per_trade_pct` 硬顶封住——提高杠杆只会让仓位被自动压小，亏损上限不变，杠杆只是资金效率。

### 自动解锁（可选，仅假钱环境）

```bash
CRYPTOAGENT_AUTO_ARM=demo   # 服务启动后自动连 demo/testnet；主网永远要人工确认
```

---

## 因子系统

### 内置因子库（21 个，按家族）

| 家族 | 因子 |
|---|---|
| 动量 momentum | `momentum_24` `momentum_96` `acceleration` |
| 反转 reversal | `reversal_6` |
| 波动率 volatility | `volatility_48` `bollinger` `volatility_cluster` `atr_breakout` |
| 资金费率 funding | `funding_carry` `funding_momentum` |
| 突破 breakout | `breakout_96` `donchian` |
| 趋势 trend | `trend_sma_72` `macd` |
| 成交量 volume | `volume_momentum` `volume_pressure` `mfi` |
| K 线结构 candle | `candle_body` `upper_shadow` |
| 摆动 oscillator | `rsi_14` `stochastic` |

每个因子都是一段独立可执行的代码字符串，和 Agent 生成的因子走**同一条沙箱链路**——内置因子本身是「参考实现」，LLM 可以照着写。

### 因子进化闭环

```
LLM 提议因子 → 沙箱执行 → 前视检测 → 三段回测 → 留出集评分
   → 夏普 > 0 晋级 / 为负淘汰 → 反思 → 下一轮
```

- 晋级因子写入 `factors_library/*.py`，实盘决策时按 `max(0, holdout_sharpe)` 加权合成复合信号；
- 复合信号方向一致放大仓位（最多 1.4×）、相反缩小、强烈反对（|c|>0.6）直接观望。

### 中频叠加信号

决策时实时拉 **5m K 线**，算「唐奇安通道突破 + ATR 过滤」，有突破时并入决策复合信号（与 1h 因子各占一半权重）。这是实盘叠加层，不占 1h 研究回测的坑位，拉取失败静默降级、不阻断决策。

---

## 交易策略与风控

**一句话：趋势跟随 + 资金费率反向 + 波动率定仓位。**

| 步骤 | 做什么 |
|---|---|
| 三个分析师 | 技术面、资金费率、情绪各出一份报告 |
| 多空辩论 | 研究员互驳 N 轮，每轮都可能改变结论 |
| 综合评分 | `0.5×技术 + 0.3×资金费率 + 0.2×情绪` |
| 资金费率分 | 费率越高越看空（多头拥挤） |
| 开仓阈值 | `|score| < 0.15 → 观望` |
| 止损 | `clip(1.5×ATR%, 0.6%, 3%)`，止盈 = 2×止损 |

风控引擎检查顺序（任一触发即拒绝/夹紧）：熔断文件 → 总回撤 → 当日亏损 → 置信度 → 杠杆 → 仓位 → 名义敞口 → 止损合法性 → 单笔风险 → 波动率分位。

---

## 三种运行模式

| 模式 | 撮合来源 | 资金 | 需要 API Key |
|---|---|---|---|
| **本地模拟**（默认） | 内置 `PaperBroker` | 假 | 否 |
| **币安测试网** | `testnet.binancefuture.com` | 假钱 | 测试网 Key |
| **主网实盘** | `fapi.binance.com` | 真钱 | 主网 Key |

### 测试网（推荐第一步）

1. <https://testnet.binancefuture.com> 注册，生成 Key/Secret（虚拟 USDT）。
2. 交易页「币安测试网」粘贴 Key/Secret → 保存并连接。
3. 跑决策 → 执行 → 验证签名 / 下单 / 止损 / 查仓整条链路。

### 主网实盘

> ⚠️ 真钱。默认不会下单，不点「主网实盘」就没有真实订单。

1. 币安 API 管理创建 Key，**只勾「允许合约交易」、禁用提现**、绑定 IP。
2. 用独立子账户，只放能亏得起的钱。
3. `.env` 换 Key，加 `CRYPTOAGENT_I_UNDERSTAND_THE_RISK=yes`，项目根建空文件 `.cryptoagent_live_ack`。
4. 交易页「主网实盘」→ 输入 `我确认使用真实资金` → 解锁。

主网有**四道锁**：确认文本、`armed` 状态不落盘（重启回模拟盘）、确认文件 + 环境变量、熔断开关。

---

## 目录结构

```
qeelin-agent/
├── config/default.json          # 所有参数：手续费/滑点/杠杆/风控上限
├── src/cryptoagent/
│   ├── config.py                # dataclass 配置，未知字段直接报错
│   ├── data.py                  # 多源真实行情 + 实时报价 + 合成兜底
│   ├── backtest.py              # 事件驱动回测：手续费/滑点/资金费率/强平
│   ├── risk.py                  # 确定性风控引擎（唯一否决权）
│   ├── execution.py             # PaperBroker + 币安签名客户端
│   ├── engine.py                # 持仓状态机、模拟/实盘执行、自动循环、选品种
│   ├── research.py              # 因子研究循环 + LLM 提议器
│   ├── factors/                 # 因子沙箱 + 内置因子库 + 中频叠加信号
│   ├── agents/                  # 四角色 + 决策流水线
│   └── web/                     # 零依赖 HTTP 服务 + 前端
├── tests/                       # 单元测试
├── deploy/                      # install.sh / install-windows.ps1 / systemd / nginx
├── run.py / run.cmd / run.sh    # 启动器
├── start.cmd / start-web.cmd    # 双击启动
└── runs/                        # 所有产出（已 gitignore）
```

---

## 已知限制

| 限制 | 影响 | 补充方向 |
|---|---|---|
| 合成数据无真实微观结构 | 结论不可用于真实市场 | 换 `--source binance` + 更长历史 |
| 回测按 bar 收盘价成交 | 忽略盘口冲击和排队 | 接逐笔 / 订单簿 |
| 单品种、单交易所 | 无组合效应 | 扩成多品种横截面 |
| 无新闻/链上数据源 | 情绪面只是价格代理 | 接 RSS / 公告 / 链上 API |
| 风控参数是经验值 | 需压力测试校准 | 用历史极端行情回测 |
| LLM 决策不可复现 | 同一天两次跑结果不同 | 调低 temperature，或固定种子 |

---

## 路线图

- [x] 本地 Web 控制台（总览 / 决策 / 研究 / 交易）
- [x] 模拟盘状态机 + 自动交易循环 + 熔断
- [x] 币安 U 本位客户端（下单 / 止损单 / 账户查询）
- [x] 多品种自主选择 + 中频波动突破叠加信号
- [x] LLM 驱动的因子研究循环
- [ ] 多品种横截面（相关性约束、组合风控）
- [ ] 订单簿特征（买卖压力、大单撤单）
- [ ] 实盘对账守护进程 + Prometheus 指标
- [ ] Walk-forward 滚动重训

---

## 风险声明

- 本项目仅用于技术研究与学习，**不构成投资建议**。
- 加密货币合约是杠杆产品，可能损失全部本金。
- 中国大陆境内禁止加密货币衍生品交易与相关经营活动；其他司法辖区请自行确认牌照、KYC/AML 与税务要求。
- 回测表现不代表未来收益。

---

## License

[MIT](LICENSE) © 2026 QeeLin-Agent contributors
