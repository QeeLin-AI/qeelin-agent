# QeeLin-Agent

<p align="center">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/License-MIT-green.svg"></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-blue.svg">
  <img alt="Version" src="https://img.shields.io/badge/version-1.0.1--beta-orange.svg">
  <img alt="Status" src="https://img.shields.io/badge/status-research%20%2F%20education-lightgrey.svg">
</p>

币圈永续合约的**多智能体交易框架 + 自动因子研究循环**。

四类角色接力决策：**分析师 → 研究员（多空辩论）→ 交易员 → 风控官**，最后由一个**完全确定性的风控引擎**做终审。研究循环让模型自己写因子、跑回测，按样本外表现决定是否晋级。

> 核心原则：数字由代码算，语言交给模型。LLM 负责解释与权衡，不负责算术，也没有下单权限。

![总览](docs/ui.png)

---

## 目录

- [设计原则](#设计原则)
- [核心特性](#核心特性)
- [架构](#架构)
- [快速开始](#快速开始)
- [命令](#命令)
- [Web 控制台](#web-控制台)
- [运行模式与安全闸门](#运行模式与安全闸门)
- [风控体系](#风控体系)
- [因子研究循环](#因子研究循环)
- [交易所支持](#交易所支持)
- [LLM 与成本控制](#llm-与成本控制)
- [通知](#通知)
- [配置](#配置)
- [目录结构](#目录结构)
- [测试](#测试)
- [已知限制](#已知限制)

---

## 设计原则

| 原则 | 落地方式 |
|---|---|
| **数字归代码** | 价格、均线、ATR、资金费率 z-score、仓位、盈亏、回撤全部由 pandas 计算后喂给模型；模型只输出判断与理由 |
| **确定性兜底** | 每个角色都有规则化实现：没配 Key、网络失败、输出不合规都会自动降级，流水线不中断，报告标注「LLM 降级」 |
| **偏保守的默认** | 默认 `paper` + `mock` 模式，开箱即跑、零成本、不联网下单；实盘需要显式解锁 |
| **可插拔** | LLM / 交易所 / 通知 / 情绪源任一缺失都能降级运行 |
| **可审计** | 每次决策落盘为 JSON + Markdown，成交记录保留计划价、滑点、手续费与已实现盈亏 |

---

## 核心特性

- **多智能体决策链**：技术面 / 资金费率 / 情绪面三位分析师并发，多空双方交叉质询，交易员输出严格 JSON 提案，风控官独立复核。
- **独立风控引擎**：纯代码实现，拥有最终否决权；风控官只能维持、缩小或拒绝提案，**不能放大**。
- **自动因子研究**：模型写因子代码 → AST 白名单 + 独立子进程沙箱 + 未来函数检测 → 训练/验证/测试三段回测 → 按样本外夏普加权晋级。
- **多交易所**：币安 U 本位合约（Demo / Testnet / 主网）与 OKX 永续（模拟盘 / 主网）。模拟盘与主网使用**两套独立 Key**，互不回退。
- **本地 Web 控制台**：总览、决策、因子研究、交易四个页签，纯静态资源，无需前端构建。
- **峰谷调度**：按 DeepSeek 官方峰谷时段安排可延后的研究任务，谷时半价。
- **通知渠道**：控制台、应用内、Webhook、Telegram、邮件。

---

## 架构

```
                ┌──────────────── 行情数据 ────────────────┐
                │  Binance K线 / OKX / 本地合成数据 + 缓存  │
                └───────────────────┬──────────────────────┘
                                    │  MarketSnapshot（指标已算好）
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
 ┌─────────────┐            ┌─────────────┐            ┌─────────────┐
 │  技术面分析师 │            │ 资金费率分析师 │            │  情绪面分析师 │   ← 并发
 └──────┬──────┘            └──────┬──────┘            └──────┬──────┘
        └───────────────────────────┼───────────────────────────┘
                                    ▼
                          ┌───────────────────┐
                          │ 研究员：多空交叉质询 │  N 轮辩论
                          └─────────┬─────────┘
                                    ▼
                          ┌───────────────────┐
                          │ 交易员：JSON 提案   │  方向 / 仓位 / 杠杆 / 止损止盈
                          └─────────┬─────────┘
                                    ▼
                          ┌───────────────────┐
                          │ 风控官：维持/缩小/拒绝 │  ← LLM，只能收紧
                          └─────────┬─────────┘
                                    ▼
                          ┌───────────────────┐
                          │ 风控引擎（纯代码）   │  ← 最终否决权
                          └─────────┬─────────┘
                                    ▼
                    执行层：paper 模拟撮合 / 交易所实盘
```

每个环节的输出都会写进决策报告；任何一步失败都退回该环节的确定性实现。

![决策页](docs/tab-decision.png)

---

## 快速开始

**要求**：Python 3.10+，核心依赖只有 `numpy` 与 `pandas`。

```bash
git clone https://github.com/QeeLin-AI/qeelin-agent.git
cd qeelin-agent

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

**离线跑通全流程（不联网、不花钱）**：

```bash
python run.py demo
```

默认 `--source synthetic --llm mock`：用合成行情 + 规则引擎跑完研究、回测与决策，产出完整报告。

**接真实行情**：

```bash
python run.py demo --source binance --symbol BTCUSDT --bars 6000
```

**接真实模型**（任何 OpenAI 兼容网关）：

```bash
cp .env.example .env
# 编辑 .env：CRYPTOAGENT_LLM_PROVIDER=openai，并填入 OPENAI_API_KEY
python run.py agents --llm openai
```

**启动 Web 控制台**：

```bash
python run.py web --host 127.0.0.1 --port 8000
```

控制台默认只监听回环地址，并使用访问令牌鉴权；`python run.py token` 可打印带令牌的完整地址。

---

## 命令

安装（`pip install -e .`）后可用 `cryptoagent`，等同于 `python run.py`。

| 命令 | 作用 |
|---|---|
| `demo [-n N]` | 端到端演示：因子研究 → 回测 → 决策 |
| `research [-n N]` | 因子研究循环，产出候选因子并回测晋级 |
| `backtest --factor NAME` / `--code FILE` | 回测单个因子 |
| `agents [--rounds N]` | 跑一次多智能体决策并输出报告 |
| `web [--host H] [--port P] [--no-browser]` | 启动本地 Web 控制台 |
| `download [--symbol S] [--bars N]` | 下载并缓存行情 |
| `exchtest [--envs demo testnet live]` | 用 `.env` 里的 Key 试连交易所，翻译错误码 |
| `token [--port P]` | 打印带访问令牌的控制台地址 |
| `llmtest [--provider] [--model]` | 体检：模型能否调用、延迟、token 与日成本估算 |
| `netcheck [--symbol S]` | 体检：行情 / 下单 / 实时价各域名连通性 |
| `factors` | 列出内置因子 |

公共参数：`--source {synthetic,binance}`、`--bars`、`--symbol`、`--llm {mock,openai}`、`--refresh`。

---

## Web 控制台

| 页签 | 内容 |
|---|---|
| **总览** | 账户权益、当日盈亏、回撤、当前持仓、权益曲线、最近成交 |
| **决策** | 决策全过程：分析师结论、多空辩论、交易员提案、风控官意见、风控引擎检查项；支持多品种批量扫描 |
| **因子研究** | 迭代研究、候选因子排行榜（IS/OOS 夏普、OOS 回撤、评分、结论）、冠军因子代码 |
| **交易** | 解锁与模式切换、开平仓、订单计划、持仓历史、通知列表 |

![因子研究页](docs/tab-research.png)

服务与接口全部使用 Bearer / `?token=` 鉴权，默认绑定 `127.0.0.1`。

---

## 运行模式与安全闸门

| 模式 | 说明 | 是否动用真钱 |
|---|---|---|
| `paper` | 本地模拟撮合，含手续费与滑点模型 | 否 |
| `testnet` / `demo` | 交易所模拟盘（币安 Demo、OKX 模拟盘） | 否 |
| `live` | 交易所主网真实下单 | **是** |

进入 `live` 需要同时满足两个闸门，缺一不可：

1. `.env` 中设置 `CRYPTOAGENT_I_UNDERSTAND_THE_RISK=yes`；
2. 项目根目录存在空文件 `.cryptoagent_live_ack`。

此外，模拟盘与主网是**两套独立 Key**，代码不会在两者之间回退：模拟盘缺 Key 时直接报错，而不是悄悄改用主网 Key 下单。

---

## 风控体系

风控分两层：**LLM 风控官**（只能收紧）+ **确定性风控引擎**（最终否决）。

| 参数 | 默认值 | 含义 |
|---|---|---|
| `max_leverage` | 20 | 杠杆上限（与交易所支持的最大值取较小值） |
| `leverage_override` | 0 | 0 = 跟随决策；>0 = 强制使用该杠杆 |
| `min_leverage` | 1 | 杠杆下限，模型偏保守时的托底（1 = 不干预） |
| `sizing_mode` | `llm` | 仓位怎么定：`llm` 听交易员 / `risk` 按风险预算反推 / `max` 两者取大 |
| `risk_per_trade_pct` | 0.01 | 目标单笔风险预算（`risk`/`max` 模式下用它反推仓位） |
| `max_position_pct` | 0.5 | 单笔最大保证金占用（占权益） |
| `max_loss_per_trade_pct` | 0.05 | 单笔最大亏损（按仓位 × 杠杆 × 止损计算） |
| `daily_loss_limit_pct` | 0.05 | 单日亏损上限，触发后停止开仓 |
| `max_drawdown_pct` | 0.2 | 最大回撤上限 |
| `max_slippage_bps` | 10 | 允许的滑点上限 |
| `min_confidence` | 0.45 | 低于该置信度直接放弃 |
| `max_portfolio_notional_pct` | 1.2 | 全部持仓名义敞口上限（占权益） |
| `correlated_exposure_pct` | 0.9 | 同相关性组内同向敞口上限 |

> 上表是 `config/default.json` 的出厂值；代码内 dataclass 兜底更保守（3x / 30% / 1% / 3% / 15% / 0.55），配置文件缺失时以代码为准。

其他保护：

- **熔断开关**：`runs/TRADING_HALT` 存在时拒绝所有新开仓，可在控制台一键切换。
- **持仓核对**：开仓、换品种、删品种前与交易所核对真实持仓，自动清理「本地记着、交易所已平」的幽灵仓位，且不会替用户主动平仓。
- **实盘权益对齐**：实盘以交易所余额 + 未实现盈亏作为权益口径，避免用模拟账本的本金计算仓位。
- **成交回读**：开平仓后按订单号回读交易所成交，回填真实成交均价、手续费与已实现盈亏。

---

## 因子研究循环

```
写代码  →  静态校验  →  沙箱执行  →  未来函数检测  →  三段回测  →  晋级 / 淘汰
```

1. **写代码**：模型按模板输出 `factor(df) -> pd.Series`。
2. **静态校验**：AST 白名单，只允许数学运算与控制流，禁止 `import`、`class`、`with`、下划线属性，阻断常见逃逸写法。
3. **沙箱执行**：独立子进程 + 受限 `__builtins__`（无 `open` / `eval` / `exec`）+ 超时强杀。
4. **未来函数检测**：用前 k 根 K 线重算，若最后一位与全量结果不一致，判定偷看未来并淘汰。
5. **三段回测**：train / valid / test 切分，只有样本外（OOS）夏普超过阈值才入库；入库因子的权重 = 留出集夏普，负值直接权重 0。

内置 20+ 参考因子始终参与信号合成，研究因子在此基础上叠加，因此因子库为空也不会没有信号。

**性能说明**：单因子沙箱执行的主要成本是子进程启动（约 1.2s/因子，与数据量无关），因此信号计算采用**批量执行**——同一子进程内依次跑完全部因子，全品种扫描从分钟级降到十几秒。

---

## 交易所支持

| 交易所 | 环境 | 需要的变量 |
|---|---|---|
| Binance U 本位合约 | Demo / Testnet | `BINANCE_DEMO_API_KEY` / `BINANCE_DEMO_API_SECRET` |
| Binance U 本位合约 | 主网 | `BINANCE_API_KEY` / `BINANCE_API_SECRET` |
| OKX 永续 | 模拟盘 | `OKX_DEMO_API_KEY` / `OKX_DEMO_API_SECRET` / `OKX_DEMO_PASSPHRASE` |
| OKX 永续 | 主网 | `OKX_API_KEY` / `OKX_API_SECRET` / `OKX_PASSPHRASE` |

签名方式为 HMAC-SHA256。API Key 建议**只勾选读取与合约交易**，不要开提现权限，并尽量绑定出口 IP 白名单。

---

## LLM 与成本控制

- 支持任何 OpenAI 兼容接口；默认 `provider=mock`（规则引擎，零成本），可切换 `openai`。
- 输出经过 schema 校验与重试，畸形 JSON 不会变成订单。
- **峰谷调度**（DeepSeek 官方计价规则）：峰时为工作日 09:00–12:00、14:00–18:00（北京时间，法定节假日除外），其余时间及周末为谷时，价格减半。
  - `offpeak_research_only`（默认开）：因子研究可延后，只在谷时调用 LLM，峰时改用模板提议器；
  - `peak_mode`：`full`（默认，实时决策照常）/ `thrift`（峰时降级为规则兜底，报告标注降级）。

---

## 通知

支持 `console`、`inapp`（应用内列表）、`webhook`、`telegram`、`email`。任一渠道配置失败只影响该渠道，不会中断交易主链路。应用内通知可在控制台一键清除。

---

## 配置

两层配置：

- `config/default.json`：品种列表、周期、风控阈值、研究参数等**非敏感**配置，控制台修改后会回写。
- `.env`：所有密钥与连接信息，**不会入库**（`.gitignore` 已忽略，仓库仅保留 `.env.example` 模板）。

常用环境变量：

| 变量 | 说明 |
|---|---|
| `CRYPTOAGENT_LLM_PROVIDER` / `_MODEL` / `_BASE_URL` | 模型提供方、模型名、网关地址 |
| `OPENAI_API_KEY` | 模型密钥（变量名可配置） |
| `BINANCE_*` / `OKX_*` | 交易所密钥，见上一节 |
| `CRYPTOAGENT_WEB_TOKEN` | 控制台访问令牌 |
| `CRYPTOAGENT_CONFIG` / `CRYPTOAGENT_ENV_FILE` | 自定义配置与 env 文件路径 |

---

## 目录结构

```
src/cryptoagent/
├── agents/            # 多智能体：llm 客户端、roles 角色提示词、pipeline 编排
├── factors/           # library 内置因子、sandbox 沙箱、signal 信号合成、shortterm 短周期
├── web/               # 本地 HTTP 服务 + 静态前端（server.py / static）
├── engine.py          # 交易引擎：状态机、开平仓、自动循环、机会扫描
├── execution.py       # 交易所客户端：签名、下单、止损止盈、持仓、成交、行情
├── risk.py            # 确定性风控引擎与账户状态
├── research.py        # 因子研究循环
├── backtest.py        # 回测与「因子值 → 目标仓位」转换
├── data.py            # 行情加载、缓存与合成数据
├── sentiment.py       # 情绪 / 新闻源（无 Key 时用价格行为代理）
├── notify.py          # 通知渠道
├── config.py          # 配置模型与读写
└── cli.py             # 命令行入口
```

`desktop/` 是可选的 Electron 桌面壳（需自备 Node.js），`desktop_entry.py` 为打包用的后端入口。

---

## 测试

```bash
python -m pytest -q
```

覆盖风控限额、因子沙箱与未来函数检测、回测指标、Web API、持仓核对、峰谷规则等。测试使用合成数据与 mock 模型，不联网、不需要任何 Key。

---

## 已知限制

- **沙箱不是安全边界**：AST 白名单 + 子进程能挡住模型写出危险代码，但不防御恶意攻击者；真要运行不可信代码请放进容器并断网。
- **单进程服务**：本地 HTTP 服务为单进程，CPU 密集任务（因子扫描、研究回测）已放到后台线程，但仍会与轮询争抢资源。
- **未做多账户 / 多策略并行**：一个进程对应一套配置与一个活跃品种。
- **不构成投资建议**：本项目用于研究与教学。加密货币合约风险极高，任何实盘损失由使用者自行承担。

---

## License

[MIT](LICENSE)
