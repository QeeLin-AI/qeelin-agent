"""配置系统：dataclass + JSON 覆盖，零第三方依赖。

设计原则：所有会影响回测结果的参数（手续费、滑点、杠杆、资金费率间隔）
都必须显式存在于配置里，不允许有散落在代码中的魔法数字。
"""
from __future__ import annotations

import json
import os
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
    # 自主选品种：自动循环里按因子信号扫描全部品种，切到最有「机会」的品种（有持仓时不切）
    auto_rotate: bool = False
    # 选品种信号阈值：|因子复合信号| 超过它才算「有机会」，否则留在当前品种
    rotate_min_score: float = 0.2
    # 扫描频率：每 N 个自动循环扫一次（1 = 每轮都扫）
    rotate_every_cycles: int = 1


@dataclass
class CostConfig:
    fee_bps: float = 5.0              # taker 单边 0.05%
    slippage_bps: float = 2.0
    funding_interval_hours: int = 8


@dataclass
class BacktestConfig:
    initial_equity: float = 10_000.0
    leverage: float = 3.0
    maintenance_margin_rate: float = 0.005
    costs: CostConfig = field(default_factory=CostConfig)


@dataclass
class RiskConfig:
    max_leverage: float = 3.0
    # 0 = 跟随决策（AI/规则给多少用多少，再受 max_leverage 封顶）；
    # >0 = 强制使用这个杠杆。注意：提高杠杆不放大风险，因为单笔最大亏损按
    # 仓位×杠杆×止损 计算并受 max_loss_per_trade_pct 限制，杠杆越高仓位会被压得越小。
    leverage_override: float = 0.0
    max_position_pct: float = 0.30       # 单策略最大保证金占用（占权益）
    max_loss_per_trade_pct: float = 0.01
    # —— 仓位怎么定 ——
    # llm  = 用交易员给的 size_pct（模型保守时仓位会很小）
    # risk = 按单笔风险上限反推：名义敞口 = max_loss_per_trade_pct / 止损幅度
    #        （原「按风险预算反推」已废弃：预算字段删除，统一用风险上限做输入）
    sizing_mode: str = "llm"
    # 杠杆下限：模型长期只给 2x 时可托底（1.0 = 不干预）
    min_leverage: float = 1.0
    daily_loss_limit_pct: float = 0.03
    max_drawdown_pct: float = 0.15
    max_slippage_bps: float = 10.0
    min_confidence: float = 0.55
    kill_switch_file: str = "runs/TRADING_HALT"
    # —— 组合层风控（A3）——
    # 注意：单笔最大名义敞口 = max_position_pct × max_leverage = 0.30 × 3 = 0.90（占权益），
    # 所以这两个上限必须 ≥ 0.90，否则会误拒一笔正常的满仓单。它们真正的作用是
    # 在多品种/多笔同时持仓时约束「叠加敞口」，而非限制单笔。
    max_portfolio_notional_pct: float = 1.20   # 全部持仓名义敞口上限（占权益）
    correlated_exposure_pct: float = 0.90      # 同一相关性组内同向敞口上限（占权益）
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
    max_lookback: int = 500
    out_dir: str = "runs/research"
    factor_library: str = "factors_library"
    # 自动研究间隔（秒）。0 = 关闭；>0 = 服务启动后每这么多秒自动跑一轮因子研究，
    # 更新 factor_library（决策会自动用新因子）。这是「因子自动进化」的引擎。
    auto_interval_sec: int = 0


@dataclass
class AppConfig:
    market: MarketConfig = field(default_factory=MarketConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    research: ResearchConfig = field(default_factory=ResearchConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    sentiment: SentimentConfig = field(default_factory=SentimentConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    runs_dir: str = "runs"


# 已删除、但旧配置文件里可能还留着的字段：读取时忽略，避免升级后直接起不来。
# 只登记「确实删过」的字段名，不要用它掩盖真正的拼写错误。
_LEGACY_FIELDS: dict[type, set[str]] = {
    RiskConfig: {"risk_per_trade_pct"},
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
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(asdict(cfg), indent=2, ensure_ascii=False), encoding="utf-8"
    )


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.json"


def config_path() -> Path:
    """当前生效的配置文件（CRYPTOAGENT_CONFIG 可覆盖，测试和多实例用）。"""
    return Path(os.getenv("CRYPTOAGENT_CONFIG") or DEFAULT_CONFIG_PATH)