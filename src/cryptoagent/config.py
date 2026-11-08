"""配置系统：dataclass + JSON 覆盖，零第三方依赖。

设计原则：所有会影响回测结果的参数（手续费、滑点、杠杆、资金费率间隔）
都必须显式存在于配置里，不允许有散落在代码中的魔法数字。
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints


@dataclass
class LLMConfig:
    provider: str = "mock"            # mock | openai（任何 OpenAI 兼容网关）
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    temperature: float = 0.3
    max_tokens: int = 1500
    timeout: int = 60
    retries: int = 2
    # 峰谷计价策略（DeepSeek 官方：峰时 = 工作日 09:00-12:00 / 14:00-18:00 北京时间，
    # 其余时间及周末、中国法定节假日为谷时，价格为峰时的一半）
    offpeak_research_only: bool = True   # 因子研究可延后：只在谷时调用 LLM，峰时用模板兜底
    peak_mode: str = "full"              # full=峰时照常用 LLM；thrift=峰时降级为规则兜底
    peak_holidays: list[str] = field(default_factory=list)  # 额外节假日 YYYY-MM-DD（这些天按谷时）


@dataclass
class MarketConfig:
    symbol: str = "BTCUSDT"           # 当前活跃品种
    symbols: list[str] = field(
        default_factory=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"]
    )
    # 用户置顶的品种（按顺序排在最前，只影响展示顺序，不影响扫描与交易）
    pinned: list[str] = field(default_factory=list)
    # 每个品种添加时选的交易所（已确认上线，仅用于展示与提示）
    symbol_exchanges: dict[str, list[str]] = field(default_factory=dict)
    interval: str = "1h"
    bars: int = 6000
    seed: int = 7
    source: str = "synthetic"         # synthetic | binance
    cache_dir: str = "data/cache"
    # 中频叠加信号：决策时实时拉这个短周期 K 线，算波动突破信号（1m/5m/15m）
    short_interval: str = "5m"
    short_bars: int = 200
    # 短周期 K 线落盘缓存：页面图表和中频突破信号读本地 CSV，不再每次都打交易所。
    # 有效期是「距上次落盘」的时长 = 半个 bar 周期，最长封顶 3 分钟，到期整段重拉。
    klines_cache: bool = True
    # 主行情刷新后后台预热 short_interval 的 K 线，让上面的缓存始终是热的。
    # 只在主行情刷新时触发，机会扫描的 6 品种扫描不会预热（避免 6 倍请求）。
    klines_prewarm: bool = True
    # 除 short_interval 外，额外保持预热的周期。15m 是中频观察/图表最常用的粒度，
    # 预热后切 15m 图、查 /api/klines?interval=15m 都直接读盘，不再等网络。
    klines_prewarm_intervals: list[str] = field(default_factory=lambda: ["15m"])
    # 自主选品种：自动循环里按因子信号扫描全部品种，切到最有「机会」的品种（有持仓时不切）
    # 默认开启，与「稳健」方案一致：让系统主动在品种间找机会。
    auto_rotate: bool = True
    # 选品种信号阈值：|因子复合信号| 超过它才算「有机会」，否则留在当前品种
    rotate_min_score: float = 0.2
    # 扫描频率：每 N 个自动循环扫一次（1 = 每轮都扫）
    rotate_every_cycles: int = 1
    # 每轮最多评估几个品种（1 = 保持旧行为：一轮只切一个、只开一个仓）。
    # 调大后同一轮会依次评估前 K 个「未持仓且达标」的品种，机会数随 K 增加——
    # 代价是每多评一个就多一次完整 LLM 决策链，别设太大（2~3 是合理区间）。
    # 默认 2，与「稳健」方案一致（单仓也可在一轮里评估 2 个候选再择优开仓）。
    auto_top_k: int = 2
    # —— 全品种缓存预热 ——
    # 一轮会切多个品种后，切换时若目标品种的盘缓存已过期，load_market 会当场重下
    # 6000 根 K 线，把一轮决策拖到十几秒。这里用后台线程把「全部品种」的报价与
    # K 线提前焐热，让切换只读本地。
    cache_all_symbols: bool = True
    # 报价 + 短周期 K 线的节流间隔（秒）；短周期本身还有盘缓存 TTL，到点才会真拉
    cache_warm_interval_sec: int = 15
    # 主周期 K 线（默认 1h × 6000 根，较重）的节流间隔（秒）
    cache_warm_main_sec: int = 300
    # 衍生品数据（基差 / OI / taker 多空比 / 账户多空比）对齐到行情，供 C 档因子使用。
    # 只在主行情刷新时拉，机会扫描不拉；拿不到就静默降级，不影响决策。
    derivatives_enabled: bool = True
    derivatives_ttl_sec: int = 300
    # 持仓品种最新价走交易所行情流（WebSocket）实时推送，不再只靠 4 秒 REST 轮询；
    # 断线自动回退到 REST。仅公开行情，不传 Key、不下单。
    ws_quotes: bool = True


@dataclass
class CostConfig:
    fee_bps: float = 5.0              # taker 单边 0.05%
    slippage_bps: float = 2.0
    funding_interval_hours: int = 8
    # —— 执行成本闸（决策前纯代码检查，不占 LLM）——
    cost_gate_enabled: bool = True
    # 成本 / 预期止盈收益 超过这个比例就发警告（不强行缩仓：按名义比例计的
    # 手续费和滑点不随仓位变小，缩仓并不能改善这个比值）
    max_cost_ratio: float = 0.35
    # 单笔名义 / 最近一根 K 线成交额 的参与率上限；超出就按反比缩仓
    max_participation: float = 0.10
    # 参与率硬顶：超过直接拒绝（这种单子必然砸盘）
    max_participation_hard: float = 0.50


@dataclass
class BacktestConfig:
    initial_equity: float = 10_000.0
    leverage: float = 3.0
    maintenance_margin_rate: float = 0.005
    costs: CostConfig = field(default_factory=CostConfig)


@dataclass
class RiskConfig:
    max_leverage: float = 5.0
    # 0 = 跟随决策（AI/规则给多少用多少，再受 max_leverage 封顶）；
    # >0 = 强制使用这个杠杆。注意：提高杠杆不放大风险，因为单笔最大亏损按
    # 仓位×杠杆×止损 计算并受 max_loss_per_trade_pct 限制，杠杆越高仓位会被压得越小。
    leverage_override: float = 0.0
    max_position_pct: float = 0.30       # 单策略最大保证金占用（占权益）
    max_loss_per_trade_pct: float = 0.01
    # —— 多品种并发持仓 ——
    # 同时持仓的品种数上限。**默认 1 = 单仓**（稳健根基，升级后行为不变）；
    # 想开多仓必须在设置页手动调大，并同步收紧 max_loss_per_trade_pct
    # （N 笔同时打满止损 = N 倍单笔风险）。
    max_concurrent_positions: int = 1
    # 全部持仓的保证金占用上限（占权益）。交易所真正拒绝的是这个，不是名义敞口：
    # 名义 27x 在 5x 杠杆下只占 5.4 倍权益保证金，早就超过账户余额了。
    # 默认 95% 只做「别加到爆仓」的兜底；开多仓时建议降到 60%~80%。
    max_margin_usage_pct: float = 0.95
    # —— 仓位怎么定 ——
    # llm  = 用交易员给的 size_pct（模型保守时仓位会很小）
    # risk = 按单笔风险上限反推：名义敞口 = max_loss_per_trade_pct / 止损幅度
    #        （原「按风险预算反推」已废弃：预算字段删除，统一用风险上限做输入）
    sizing_mode: str = "llm"
    # 杠杆下限：模型长期只给 2x 时可托底（1.0 = 不干预）
    min_leverage: float = 1.0
    daily_loss_limit_pct: float = 0.04
    max_drawdown_pct: float = 0.18
    min_confidence: float = 0.50
    # —— 交易质量闸（交付前发现的两个真实缺口）——
    # 盈亏比下限：止盈必须至少是止损的这么多倍。执行成本闸只管「成本占止盈」，
    # 从不管「赢一点点、亏一大截」；没有这道闸，R:R=1.0 的单也能过。
    min_risk_reward: float = 1.3
    # 止损宽度下限（相对 ATR）：止损比 N×ATR 还近时，正常波动就会把仓位扫掉，
    # 属于「数学上必亏」的设置。0 = 不检查。
    min_stop_atr_mult: float = 0.6
    # 辩论裁判在综合评分中的权重：0 = 辩论不影响分数（旧行为），
    # 0.3 = 分析师加权占 70%、辩论裁判占 30%。
    debate_score_weight: float = 0.3
    kill_switch_file: str = "runs/TRADING_HALT"
    # —— 组合层风控（A3）——
    # 关键不变量：max_portfolio_notional_pct 与 correlated_exposure_pct 都必须 ≥
    # 单笔最大名义敞口 = max_position_pct × max_leverage（占权益）。
    # 本 dataclass 默认 0.30 × 5 = 1.50，故默认上限 2.0 / 1.5 满足要求。
    # ⚠️ 若通过 config/default.json 抬高 max_leverage 或 max_position_pct，
    #    必须同步抬高这两个上限，否则风控会在 risk.py step 11 把每一笔正常交易都拒掉。
    #    它们真正的作用是约束「多品种/多笔同时持仓」的叠加敞口，而非限制单笔。
    max_portfolio_notional_pct: float = 2.0    # 全部持仓名义敞口上限（占权益）
    correlated_exposure_pct: float = 1.5       # 同一相关性组内同向敞口上限（占权益）
    # 相关性分组：同组品种视为高度相关，同向叠加敞口受 correlated_exposure_pct 约束。
    # 默认把大盘币放一组、山寨币放一组；接了真实相关性矩阵后可覆盖。
    correlation_groups: list[list[str]] = field(
        default_factory=lambda: [
            ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"],
            ["XRPUSDT", "DOGEUSDT"],
        ]
    )


@dataclass
class SentimentConfig:
    """情绪/新闻源配置（A1）。默认 price_action 为本地确定性代理，绝不依赖外网。"""
    enabled: bool = True
    source: str = "price_action"          # price_action | cryptopanic
    cryptopanic_api_key_env: str = "CRYPTOPANIC_API_KEY"
    cache_ttl_sec: int = 600


@dataclass
class NotifyConfig:
    """通知/告警配置（A2）。多通道，默认仅控制台 + 应用内日志，绝不因通知失败而中断交易。"""
    enabled: bool = True                 # 主开关：关闭后不投递任何通道
    channels: list[str] = field(default_factory=lambda: ["console", "inapp"])
    webhook_url_env: str = "CRYPTOAGENT_WEBHOOK_URL"
    telegram_token_env: str = "CRYPTOAGENT_TG_TOKEN"
    telegram_chat_env: str = "CRYPTOAGENT_TG_CHAT"
    email_smtp_host_env: str = "CRYPTOAGENT_SMTP_HOST"
    email_smtp_port_env: str = "CRYPTOAGENT_SMTP_PORT"
    email_user_env: str = "CRYPTOAGENT_SMTP_USER"
    email_pass_env: str = "CRYPTOAGENT_SMTP_PASS"
    email_to_env: str = "CRYPTOAGENT_SMTP_TO"
    inapp_path: str = "runs/notifications.jsonl"


@dataclass
class ResearchConfig:
    iterations: int = 6
    train_frac: float = 0.55
    valid_frac: float = 0.20
    min_oos_sharpe: float = 0.30
    out_dir: str = "runs/research"
    factor_library: str = "factors_library"
    # 自动研究间隔（秒）。0 = 关闭；>0 = 服务启动后每这么多秒自动跑一轮因子研究，
    # 更新 factor_library（决策会自动用新因子）。这是「因子自动进化」的引擎。
    auto_interval_sec: int = 0


@dataclass
class PositionConfig:
    """持仓管理员：开仓之后的退出决策（移动止损 / 时间止损 / 趋势反转退出）。

    设计原则与全项目一致：**动作由确定性规则决定，LLM 只负责解释**。
    规则算不出结论时一律 hold，绝不会因为模型幻觉去平仓。
    所有阈值都以「R」（开仓时的初始风险 = |entry - stop| / entry）为单位，
    这样不同杠杆、不同止损宽度的仓位可以用同一套参数。
    """
    enabled: bool = True
    # 移动止损：浮盈达到 trail_activate_r × R 后启动，跟踪距离 = trail_atr_mult × ATR%
    trail_activate_r: float = 1.0
    trail_atr_mult: float = 2.0
    # 新止损相对旧止损至少改善这么多才撤挂条件单，避免每轮反复改单
    trail_min_gap_pct: float = 0.003
    # 时间止损：持有超过 max_hold_bars 根且浮盈不足 min_progress_r × R
    max_hold_bars: int = 48
    min_progress_r: float = 0.3
    # 趋势反转退出：至少持有 3 根后，均线转向 + 连续反向 K 线
    reversal_enabled: bool = True
    reversal_min_hold_bars: int = 3
    # 因子复合信号与持仓方向强烈相反时也允许退出（|composite| ≥ 该值）
    reversal_composite: float = 0.7
    # 实盘是否在交易所撤掉旧条件单、按新止损重挂（模拟盘改本地缓存即可）
    live_stop_adjust: bool = True
    min_adjust_interval_sec: int = 300
    # ---- LLM 接入 ----
    # 是否让 LLM 生成一段「为什么这样管理」的解释（默认关，不增加调用成本）
    llm_explain: bool = False
    # 影子模式：LLM 独立给出退出意见，只记日志/通知，绝不改变动作（默认关）
    llm_shadow: bool = False
    # 峰时（DeepSeek 原价）跳过持仓管理员的 LLM 调用，等谷时半价再跑
    llm_offpeak_only: bool = True
    # 同一品种两次 LLM 调用的最小间隔（秒），避免动作频繁时反复烧 token
    llm_min_interval_sec: int = 900

    # ---- 自迭代（对标分析师权重校准）----
    # 打开后：每笔平仓的最终 R 与峰值 MFE 会累积成样本，样本达标即自动微调
    # trail_activate_r / trail_atr_mult / min_progress_r / max_hold_bars。
    # 单轮最多 12%、硬边界见 position_calibration.BOUNDS，永远跳不出安全区间。
    auto_tune: bool = True

    # ---- 规则层（默认保持既有行为；新增退出规则一律默认关）----
    # 趋势反转用的 EMA 跨度（按 bar 数），不再写死 48
    reversal_ema_bars: int = 48
    # 持有根数按 K 线索引里开仓之后的 bar 数统计（墙钟估算仅作兜底）
    bars_from_index: bool = True
    # 因子复合信号超过这个秒数就不用于退出判断，避免拿过期信号误平
    composite_max_age_sec: int = 300
    # 浮盈从峰值回撤 N×R 时提前退出（新增行为，默认关，需模拟盘验证后再开）
    peak_drawdown_exit_enabled: bool = False
    peak_drawdown_r: float = 1.0

    # ---- 演进控制（三阶段门控）----
    # off    : 关闭全部自迭代
    # stats  : L1，按退出规则归因的统计校准（只调连续参数，不碰动作）
    # replay : L2，L1 提议必须先通过反事实回放才落地
    # llm    : L3，允许 LLM 提议参数/动作；须 L2 门控通过（默认不再设稳定等待期）
    evolution_mode: str = "stats"
    # L1：单个退出规则至少多少笔样本才允许调它（全局仍受 MIN_SAMPLES=20 约束）
    evolution_rule_min_samples: int = 8
    # L1：同一方向连续多少轮才落地（防单笔噪声）
    evolution_confirm_rounds: int = 2
    # L2：回放至少覆盖多少笔平仓，才允许判定门控
    evolution_replay_min_cases: int = 20
    # L2：最多回放最近多少笔（控制耗时）
    evolution_replay_window: int = 100
    # L2：候选中位 R 相对当前的最小提升（>= 该值才算通过）
    evolution_replay_min_median_delta: float = 0.0
    # L2：候选最差单笔 R 相对当前允许的最大恶化
    evolution_replay_max_worst_delta: float = 0.2
    # L2 门控通过后需稳定运行多少天才评估 L3；0 = 不等待（默认），L2 通过即直接进入 L3
    evolution_l2_stable_days: int = 0
    # L3：总开关（默认关；即使打开也必须先过 L2 门控）
    evolution_llm_enabled: bool = False
    # L3：是否允许 LLM 修改退出动作（默认关；开启后仍默认只允许更保守方向）
    evolution_llm_allow_action: bool = False
    # L3：是否允许把 close 放宽为 hold/tighten（默认关，风险最高）
    evolution_llm_allow_loosen: bool = False
    # L3：LLM 提案的最低置信度
    evolution_llm_min_confidence: float = 0.65
    # L3：LLM 单轮参数最大相对步长
    evolution_llm_max_param_step: float = 0.15
    # L3：影子期至少记录多少笔提案后才允许真正应用
    evolution_llm_shadow_cases: int = 20
    # 回退：LLM 覆盖动作后连续亏损多少笔自动回退
    evolution_rollback_after_losses: int = 3
    # 回退：LLM 覆盖后累计 R 低于该值自动回退
    evolution_rollback_drawdown_r: float = 2.0


@dataclass
class AppConfig:
    market: MarketConfig = field(default_factory=MarketConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    research: ResearchConfig = field(default_factory=ResearchConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    sentiment: SentimentConfig = field(default_factory=SentimentConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    position: PositionConfig = field(default_factory=PositionConfig)
    runs_dir: str = "runs"
    # 决策快照 runs/decisions 保留天数（自动循环 5 分钟一轮 ≈ 288 份/天）
    decision_keep_days: int = 7


# 已删除、但旧配置文件里可能还留着的字段：读取时忽略，避免升级后直接起不来。
# 只登记「确实删过」的字段名，不要用它掩盖真正的拼写错误。
_LEGACY_FIELDS: dict[type, set[str]] = {
    RiskConfig: {"risk_per_trade_pct", "max_slippage_bps"},
    # v1.1.8 起删除：研究阶段从未读取过该字段
    ResearchConfig: {"max_lookback"},
}


def _build(cls: type, data: dict[str, Any]) -> Any:
    """把嵌套 dict 递归转成 dataclass，未知字段直接报错（防止拼写错误静默生效）。"""
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        hint = hints.get(f.name, f.type)
        if is_dataclass(hint) and isinstance(value, dict):
            kwargs[f.name] = _build(hint, value)
        else:
            kwargs[f.name] = value
    unknown = set(data) - {f.name for f in fields(cls)} - _LEGACY_FIELDS.get(cls, set())
    if unknown:
        raise ValueError(f"{cls.__name__} 收到未知配置项: {sorted(unknown)}")
    return cls(**kwargs)


def load_dotenv(path: str | Path | None = None) -> dict[str, str]:
    """极简 .env 读取：`KEY=VALUE`，支持 # 注释与引号。

    只补环境里**没有**的键，不覆盖已存在的环境变量（shell 优先）。
    找不到文件就静默返回空 dict——零依赖，不引入 python-dotenv。
    """
    if path is None:
        path = Path(__file__).resolve().parents[2] / ".env"
    p = Path(path)
    loaded: dict[str, str] = {}
    if not p.exists():
        return loaded
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        loaded[key] = value
        if key not in os.environ:
            os.environ[key] = value
    return loaded


def config_problems(cfg: AppConfig) -> list[str]:
    """检查跨字段的硬约束，返回人类可读的问题列表。

    Web 设置页保存时有前后端双重校验，但「直接改 config/default.json」
    这条路径以前完全没有校验：0.8 × 3 > 0.95 也能被静默加载，直到运行时
    风控逐笔拒单才暴露。这里在加载时给出明确告警（不阻塞启动，避免历史
    配置直接导致服务起不来）。
    """
    out: list[str] = []
    r = cfg.risk
    if r.max_position_pct <= 0 or r.max_margin_usage_pct <= 0:
        out.append("max_position_pct / max_margin_usage_pct 必须大于 0")
        return out
    slots = max(1, int(r.max_concurrent_positions or 1))
    if r.max_position_pct * slots > r.max_margin_usage_pct + 1e-9:
        out.append(
            f"max_position_pct({r.max_position_pct}) × max_concurrent_positions({slots}) "
            f"> max_margin_usage_pct({r.max_margin_usage_pct})：多仓时保证金必然不够"
        )
    single_notional = r.max_position_pct * r.max_leverage
    for name in ("max_portfolio_notional_pct", "correlated_exposure_pct"):
        cap = float(getattr(r, name, 0.0) or 0.0)
        if cap + 1e-9 < single_notional:
            out.append(
                f"{name}({cap}) 小于单笔最大名义敞口 {single_notional:.2f}：正常交易会被组合层全部拒绝"
            )
    return out


def load_config(path: str | Path | None = None) -> AppConfig:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    cfg = AppConfig()
    if path and Path(path).exists():
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        cfg = _build(AppConfig, raw)
    # 环境变量覆盖 LLM 配置，方便 CI / 临时切换
    if os.getenv("CRYPTOAGENT_LLM_PROVIDER"):
        cfg.llm.provider = os.environ["CRYPTOAGENT_LLM_PROVIDER"]
    if os.getenv("CRYPTOAGENT_LLM_MODEL"):
        cfg.llm.model = os.environ["CRYPTOAGENT_LLM_MODEL"]
    if os.getenv("CRYPTOAGENT_LLM_BASE_URL"):
        cfg.llm.base_url = os.environ["CRYPTOAGENT_LLM_BASE_URL"]
    problems = config_problems(cfg)
    if problems:
        import sys
        for item in problems:
            print(f"[config] 警告：{item}", file=sys.stderr)
    return cfg


def update_dotenv(updates: dict[str, str], path: str | Path | None = None) -> Path:
    """把键值写进 .env（保留其它行与注释），并同步到当前进程的环境变量。

    只用于本机工具保存 API Key：文件在 .gitignore 里，且不会被任何接口读回。
    """
    p = Path(path) if path else Path(
        os.getenv("CRYPTOAGENT_ENV_FILE") or (Path(__file__).resolve().parents[2] / ".env")
    )
    lines: list[str] = []
    if p.exists():
        lines = p.read_text(encoding="utf-8").splitlines()

    pending = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in pending:
                out.append(f"{key}={pending.pop(key)}")
                continue
        out.append(line)
    for key, value in pending.items():
        out.append(f"{key}={value}")

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    for key, value in updates.items():
        os.environ[key] = value
    return p


def save_config(cfg: AppConfig, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再原子替换：设置页保存时若中途失败（磁盘满、进程被 kill），
    # 直接 write_text 会留下半截 JSON，下次启动整站直接起不来。
    # 临时名必须唯一：两个保存请求并发时，固定 .tmp 会互相覆盖。
    tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex[:12]}.tmp")
    try:
        tmp.write_text(json.dumps(asdict(cfg), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.json"


def config_path() -> Path:
    """当前生效的配置文件（CRYPTOAGENT_CONFIG 可覆盖，测试和多实例用）。"""
    return Path(os.getenv("CRYPTOAGENT_CONFIG") or DEFAULT_CONFIG_PATH)