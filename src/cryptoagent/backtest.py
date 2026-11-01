"""事件驱动的单品种永续合约回测引擎。

建模口径（有意保守）：
- 信号在 bar t 收盘生成，bar t+1 开盘执行 —— 代码里体现为 `shift(1)`，杜绝未来函数。
- 每次调仓按 |Δ仓位| 收取 手续费 + 滑点，双边都收。
- 资金费率按配置的结算周期（默认 8h）在对应 bar 上收取，多头为正付费方。
- 用 bar 内 high/low 判断强平：当不利波动吃穿维持保证金时，账户归零。
- 不含：盘口冲击、部分成交、交易所宕机、借币成本。

任何一条被省略的成本都会让回测变好看，所以它们必须在报告里显式列出。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import BacktestConfig
from .data import bars_per_year


@dataclass
class BacktestResult:
    equity: pd.Series
    position: pd.Series
    net_returns: pd.Series
    metrics: dict
    trades: pd.DataFrame = field(default_factory=pd.DataFrame)
    liquidated_at: pd.Timestamp | None = None

    def summary(self) -> str:
        m = self.metrics
        lines = [
            f"总收益      {m['total_return']:>9.2%}",
            f"年化收益    {m['cagr']:>9.2%}",
            f"夏普比率    {m['sharpe']:>9.2f}",
            f"索提诺      {m['sortino']:>9.2f}",
            f"最大回撤    {m['max_drawdown']:>9.2%}",
            f"卡玛比率    {m['calmar']:>9.2f}",
            f"胜率        {m['win_rate']:>9.2%}",
            f"盈亏比      {m['profit_factor']:>9.2f}",
            f"交易次数    {m['n_trades']:>9d}",
            f"平均换手    {m['turnover_per_bar']:>9.4f}",
            f"手续费损耗  {m['fee_drag']:>9.2%}",
            f"资金费率    {m['funding_drag']:>9.2%}",
            f"平均暴露    {m['avg_exposure']:>9.2f}x",
        ]
        if self.liquidated_at is not None:
            lines.append(f"⚠ 强平于      {self.liquidated_at}")
        return "\n".join(lines)


def _max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(dd.min()) if len(dd) else 0.0


def _extract_trades(position: pd.Series, net: pd.Series, equity: pd.Series) -> pd.DataFrame:
    """把连续同向持仓合并成一笔交易，用于统计胜率和盈亏比。"""
    trades: list[dict] = []
    cur_sign = 0
    start = None
    for ts, pos in position.items():
        sign = 0 if abs(pos) < 1e-9 else (1 if pos > 0 else -1)
        if sign != cur_sign:
            if cur_sign != 0 and start is not None:
                seg = equity.loc[start:ts]
                pnl = float(seg.iloc[-1] / seg.iloc[0] - 1.0) if len(seg) > 1 else 0.0
                trades.append(
                    {
                        "side": "long" if cur_sign > 0 else "short",
                        "entry": start,
                        "exit": ts,
                        "bars": len(seg),
                        "pnl_pct": pnl,
                    }
                )
            cur_sign = sign
            start = ts if sign != 0 else None
    if cur_sign != 0 and start is not None:
        seg = equity.loc[start:]
        trades.append(
            {
                "side": "long" if cur_sign > 0 else "short",
                "entry": start,
                "exit": seg.index[-1],
                "bars": len(seg),
                "pnl_pct": float(seg.iloc[-1] / seg.iloc[0] - 1.0) if len(seg) > 1 else 0.0,
            }
        )
    return pd.DataFrame(trades)


def run_backtest(
    df: pd.DataFrame,
    target: pd.Series,
    cfg: BacktestConfig,
    interval: str = "1h",
) -> BacktestResult:
    """target 是「目标名义敞口 / 权益」的倍数，取值 [-leverage, leverage]。"""
    if len(df) < 10:
        raise ValueError("回测数据不足 10 根 K 线")

    close = df["close"].astype(float)
    prev_close = close.shift(1)
    ret = (close / prev_close - 1.0).fillna(0.0)

    lev = float(cfg.leverage)
    pos = target.reindex(df.index).astype(float).shift(1).fillna(0.0).clip(-lev, lev)
    turnover = pos.diff().abs()
    if len(turnover):
        turnover.iloc[0] = abs(pos.iloc[0])

    costs = cfg.costs
    cost_rate = turnover * (costs.fee_bps + costs.slippage_bps) / 10_000.0
    funding_rate = (
        df["funding_rate"].fillna(0.0).astype(float)
        if "funding_rate" in df.columns
        else pd.Series(0.0, index=df.index)
    )
    funding_cost = pos * funding_rate          # 多头在正费率时付费
    gross = pos * ret
    net = gross - cost_rate - funding_cost

    equity = cfg.initial_equity * (1.0 + net).cumprod()

    # --- 强平：用 bar 内最不利价格判断（没有 high/low 时退化为收盘价）---
    high = df["high"].to_numpy() if "high" in df.columns else close.to_numpy()
    low = df["low"].to_numpy() if "low" in df.columns else close.to_numpy()
    pc = np.where(prev_close.to_numpy() == 0, np.nan, prev_close.to_numpy())
    adverse = np.where(
        pos.to_numpy() > 0,
        low / pc - 1.0,
        high / pc - 1.0,
    )
    adverse = np.nan_to_num(adverse, nan=0.0)
    loss_frac = pos.to_numpy() * adverse
    liq_threshold = -(1.0 / lev - cfg.maintenance_margin_rate)
    liq_mask = loss_frac <= liq_threshold
    liquidated_at = None
    if liq_mask.any():
        i = int(np.argmax(liq_mask))
        liquidated_at = df.index[i]
        equity.iloc[i:] = 0.0

    bpy = bars_per_year(interval)
    n = len(net)
    mean_r = float(net.mean())
    std_r = float(net.std(ddof=1)) or 1e-12
    downside = net[net < 0]
    down_std = float(downside.std(ddof=1)) if len(downside) > 1 else 1e-12
    total_return = float(equity.iloc[-1] / cfg.initial_equity - 1.0) if not liquidated_at else -1.0
    cagr = (
        (1.0 + total_return) ** (bpy / max(n, 1)) - 1.0 if total_return > -1 else -1.0
    )
    trades = _extract_trades(pos, net, equity)
    wins = trades[trades["pnl_pct"] > 0] if len(trades) else trades
    gross_win = float(wins["pnl_pct"].sum()) if len(wins) else 0.0
    gross_loss = float(abs(trades[trades["pnl_pct"] < 0]["pnl_pct"].sum())) if len(trades) else 0.0

    fee_drag = float((cost_rate * equity.shift(1).fillna(cfg.initial_equity)).sum() / cfg.initial_equity)
    funding_drag = float((funding_cost.abs() * equity.shift(1).fillna(cfg.initial_equity)).sum() / cfg.initial_equity)

    metrics = {
        "total_return": total_return,
        "cagr": float(cagr),
        "sharpe": float(mean_r / std_r * math.sqrt(bpy)) if std_r > 0 else 0.0,
        "sortino": float(mean_r / down_std * math.sqrt(bpy)) if down_std > 0 else 0.0,
        "max_drawdown": _max_drawdown(equity),
        "calmar": float(cagr / abs(_max_drawdown(equity))) if _max_drawdown(equity) < 0 else 0.0,
        "win_rate": float(len(wins) / len(trades)) if len(trades) else 0.0,
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0,
        "n_trades": int(len(trades)),
        "turnover_per_bar": float(turnover.mean()),
        "fee_drag": fee_drag,
        "funding_drag": funding_drag,
        "avg_exposure": float(pos.abs().mean()),
        "bars": n,
        "liquidated": liquidated_at is not None,
    }
    return BacktestResult(
        equity=equity,
        position=pos,
        net_returns=net,
        metrics=metrics,
        trades=trades,
        liquidated_at=liquidated_at,
    )


def factor_to_target(
    factor: pd.Series,
    window: int = 200,
    cap: float = 1.0,
    deadband: float = 0.15,
) -> pd.Series:
    """把因子值转成 [-cap, cap] 的目标敞口：滚动 z-score 后压缩。

    只用历史窗口，天然无未来函数。
    deadband: 目标变化小于该阈值时不调仓 —— 这是真实交易里控制换手的手段，
    没有它，回测里 80% 的收益会被手续费吃掉。
    """
    f = factor.astype(float).replace([np.inf, -np.inf], np.nan)
    mean = f.rolling(window, min_periods=max(20, window // 4)).mean()
    std = f.rolling(window, min_periods=max(20, window // 4)).std()
    z = (f - mean) / std.replace(0.0, np.nan)
    raw = (z / 2.0).clip(-cap, cap).fillna(0.0)

    held = np.zeros(len(raw))
    prev = 0.0
    for i, value in enumerate(raw.to_numpy()):
        if abs(value - prev) >= deadband:
            prev = float(value)
        held[i] = prev
    return pd.Series(held, index=raw.index)


def buy_and_hold(df: pd.DataFrame, cfg: BacktestConfig, interval: str = "1h") -> BacktestResult:
    target = pd.Series(1.0, index=df.index)
    return run_backtest(df, target, cfg, interval)