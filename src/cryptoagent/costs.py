"""执行成本闸：开仓前用确定性代码判断「这笔单的预期收益能否覆盖交易成本」。

原决策链里交易员只看方向和止损止盈，完全没有成本/流动性意识：
- 手续费 + 滑点（开平各一次）
- 资金费率（按预期持有时间累计）
- 盘口深度（单笔名义相对近期成交额太大就会自己砸盘）

全部按「占名义敞口的比例」计算，才能直接和 take_profit_pct 比。
纯代码、无 LLM、无网络请求；数据缺失时保守降级为「放行 + 标记」，绝不误拒。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import pandas as pd

from .config import AppConfig, CostConfig
from .data import INTERVAL_HOURS


@dataclass
class CostGateResult:
    approved: bool = True
    size_multiplier: float = 1.0
    cost_pct: float = 0.0            # 总成本 / 名义敞口
    expected_pct: float = 0.0        # 止盈幅度（预期毛利 / 名义敞口）
    net_edge_pct: float = 0.0
    participation: float = 0.0       # 名义 / 单根 K 线成交额
    reasons: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _median_bar_quote_volume(df: pd.DataFrame, price: float) -> float:
    """最近 20 根的单根成交额中位数（用中位数抗插针）。缺列时用 volume×price 估。"""
    try:
        n = min(20, len(df))
        if n <= 0:
            return 0.0
        tail = df.tail(n)
        if "quote_volume" in tail.columns:
            q = float(pd.to_numeric(tail["quote_volume"], errors="coerce").median())
            if math.isfinite(q) and q > 0:
                return q
        v = float(pd.to_numeric(tail["volume"], errors="coerce").median())
        if math.isfinite(v) and v > 0 and price > 0:
            return v * price
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def _mean_funding(df: pd.DataFrame) -> float:
    try:
        if "funding_rate" in df.columns:
            v = float(pd.to_numeric(df["funding_rate"], errors="coerce").tail(27).mean())
            return abs(v) if math.isfinite(v) else 0.0
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def evaluate_execution_cost(
    proposal: dict,
    df: pd.DataFrame,
    cfg: AppConfig | None = None,
    price: float = 0.0,
    equity: float = 0.0,
    atr_pct: float | None = None,
) -> CostGateResult:
    """评估一笔开仓提案的执行成本，返回是否放行及仓位乘数。"""
    cfg = cfg or AppConfig()
    costs: CostConfig = cfg.backtest.costs
    res = CostGateResult()
    if not costs.cost_gate_enabled:
        res.reasons.append("执行成本闸未启用")
        return res
    if str(proposal.get("action", "flat")) not in ("long", "short"):
        res.reasons.append("交易员选择观望，无需成本检查")
        return res

    try:
        size = max(0.0, float(proposal.get("size_pct") or 0.0))
        lev = max(0.0, float(proposal.get("leverage") or 0.0))
        tp = max(0.0, float(proposal.get("take_profit_pct") or 0.0))
        stop = max(0.0, float(proposal.get("stop_loss_pct") or 0.0))
        if price <= 0 and len(df):
            price = float(df["close"].iloc[-1])
        if equity <= 0:
            equity = float(cfg.backtest.initial_equity)
        notional = size * lev * equity

        fee_one = costs.fee_bps / 10_000.0
        slip_one = costs.slippage_bps / 10_000.0

        # 预期持有时间：止盈幅度 / 每根波动，再换算成小时
        if atr_pct is None or atr_pct <= 0:
            try:
                ret = df["close"] / df["close"].shift(1) - 1.0
                atr_pct = float(ret.abs().rolling(14, min_periods=5).mean().iloc[-1])
            except Exception:  # noqa: BLE001
                atr_pct = 0.01
        atr_pct = min(0.20, max(float(atr_pct), 1e-4))
        bar_hours = float(INTERVAL_HOURS.get(cfg.market.interval, 1.0))
        hold_bars = min(336.0, max(1.0, tp / atr_pct))
        hold_hours = hold_bars * bar_hours
        funding_cost = (_mean_funding(df) * hold_hours
                        / max(1.0, float(costs.funding_interval_hours)))

        # 盘口深度：单笔名义相对单根成交额越大，额外冲击成本越高
        bar_qv = _median_bar_quote_volume(df, price)
        participation = notional / bar_qv if bar_qv > 0 else 0.0
        impact = 0.0
        if participation > costs.max_participation:
            impact = slip_one * min(10.0, participation / max(costs.max_participation, 1e-6) - 1.0)

        total = 2.0 * fee_one + 2.0 * slip_one + funding_cost + impact
        res.cost_pct = round(total, 6)
        res.expected_pct = round(tp, 6)
        res.net_edge_pct = round(tp - total, 6)
        res.participation = round(participation, 6)
        res.details = {
            "fee_pct": round(2 * fee_one, 6),
            "slippage_pct": round(2 * slip_one, 6),
            "funding_pct": round(funding_cost, 6),
            "impact_pct": round(impact, 6),
            "hold_hours": round(hold_hours, 1),
            "atr_pct": round(atr_pct, 5),
            "bar_quote_volume": round(bar_qv, 2),
            "notional": round(notional, 2),
            "cost_ratio": round(total / tp, 4) if tp > 0 else None,
        }

        if tp <= 0:
            res.approved = False
            res.size_multiplier = 0.0
            res.reasons.append("止盈幅度为 0，无法覆盖任何成本")
            return res
        if res.net_edge_pct <= 0:
            res.approved = False
            res.size_multiplier = 0.0
            res.reasons.append(
                f"执行成本 {total:.3%} ≥ 止盈 {tp:.2%}，净边际 {res.net_edge_pct:+.3%}，拒绝"
            )
            return res
        # 净赚但太薄：扣完成本只剩零点零几个百分点，实盘会被滑点与费率波动吃掉。
        # 「赚了就跑」必须建立在「净额确实为正」之上，不是毛额为正。
        floor = float(getattr(costs, "min_net_edge_pct", 0.0) or 0.0)
        if floor > 0 and res.net_edge_pct < floor:
            res.approved = False
            res.size_multiplier = 0.0
            res.reasons.append(
                f"净边际 {res.net_edge_pct:.3%} < 下限 {floor:.3%}"
                f"（止盈 {tp:.2%} − 成本 {total:.3%}），扣完手续费不值得做"
            )
            return res
        if participation > costs.max_participation_hard:
            res.approved = False
            res.size_multiplier = 0.0
            res.reasons.append(
                f"单笔名义占单根成交额 {participation:.1%} > 硬顶 {costs.max_participation_hard:.0%}"
            )
            return res

        ratio = total / tp
        if ratio > costs.max_cost_ratio:
            res.reasons.append(
                f"成本占预期收益 {ratio:.1%} > 警戒 {costs.max_cost_ratio:.0%}"
                "（按比例计的成本缩仓无效，仅提示）"
            )
        if participation > costs.max_participation:
            res.size_multiplier = round(
                max(0.1, costs.max_participation / participation), 4
            )
            res.reasons.append(
                f"参与率 {participation:.1%} 偏高，仓位按 {res.size_multiplier:.2f} 缩放"
            )
        if not res.reasons:
            res.reasons.append(f"成本 {total:.3%} / 止盈 {tp:.2%}，净边际 {res.net_edge_pct:+.2%}，放行")
        # 止损幅度只是记录，不参与判定（它由风控引擎管）
        res.details["stop_loss_pct"] = stop
        return res
    except Exception as exc:  # noqa: BLE001 - 成本闸是旁路，算不出来就放行
        # 放行是既定策略（成本闸绝不误拒），但必须留下可审计的痕迹：
        # 静默放行会让「最后一道成本防线失效」完全不可见。
        res.details["error"] = f"{type(exc).__name__}: {exc}"
        res.reasons.append(f"成本估算失败，保守放行：{type(exc).__name__}")
        return res
