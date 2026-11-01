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
    model: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    temperature: float = 0.3
    max_tokens: int = 1500
    timeout: int = 60
    retries: int = 2


@dataclass
class MarketConfig:
    symbol: str = "BTCUSDT"           # 当前活跃品种
    symbols: list[str] = field(
        default_factory=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"]
    )
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
    daily_loss_limit_pct: float = 0.03
    max_drawdown_pct: float = 0.15
    max_slippage_bps: float = 10.0
    min_confidence: float = 0.55
    kill_switch_file: str = "runs/TRADING_HALT"


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
    runs_dir: str = "runs"


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
    unknown = set(data) - {f.name for f in fields(cls)}
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