"""中频波动突破信号（5m/1m 叠加层）。

与 factors/signal.py 里走三段回测的因子不同，这里跑在**短周期 K 线**上，
决策时实时计算，不参与 1h 研究回测。思路：唐奇安通道突破 + ATR 过滤噪声，
有突破给出方向强度 ∈ [-1, 1]，无突破返回 0（不干扰 1h 因子）。
"""
from __future__ import annotations

import pandas as pd


def compute_breakout_signal(
    df: pd.DataFrame | None,
    window: int = 20,
    atr_window: int = 14,
    min_strength: float = 0.4,
) -> float:
    """唐奇安通道突破信号。

    - 用前 window 根的 high/low 做通道（不含当前根，避免未来函数）。
    - 收盘价向上突破通道上沿 → 看多；向下突破下沿 → 看空。
    - 突破幅度用 ATR 归一化，< min_strength 个 ATR 视为噪音返回 0。
    - 返回 [-1, 1]：正=向上突破，负=向下突破，0=通道内无信号。
    """
    if df is None or len(df) < window + 2:
        return 0.0
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    c = df["close"].astype(float)

    hi = h.rolling(window).max().shift(1)
    lo = l.rolling(window).min().shift(1)

    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.rolling(atr_window).mean().replace(0.0, float("nan"))

    up = (c - hi) / atr     # 向上突破强度（ATR 倍数）
    dn = (lo - c) / atr     # 向下突破强度（ATR 倍数）
    u = float(up.iloc[-1])
    d = float(dn.iloc[-1])
    if pd.isna(u) or pd.isna(d):
        return 0.0

    if u > min_strength and u >= d:
        return min(1.0, round(u / 2.0, 4))
    if d > min_strength and d > u:
        return max(-1.0, round(-d / 2.0, 4))
    return 0.0


def compute_frame_metrics(df: pd.DataFrame | None) -> dict:
    """从一根短周期 K 线算一组「对分析师有信息量」的指标。

    与 1h 主周期的 `MarketSnapshot`（趋势/结构/资金费率）互补，这里只负责
    回答三个问题：短周期动能（近 N 根收益 + RSI）、是否在突破（唐奇安）、
    短期支撑/阻力（近 20 根高低点）。数字全由 pandas 算，LLM 只做多周期权衡。

    数据不足（<30 根）返回空字典，调用方静默跳过该周期，不阻断决策。
    """
    if df is None or len(df) < 30:
        return {}
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    n = len(close)
    price = float(close.iloc[-1])

    # 近 12 根收益：15m×12≈3h、5m×12≈1h 的短周期动能
    ret_12 = float(close.iloc[-1] / close.iloc[-13] - 1.0) if n > 13 else 0.0

    # RSI(14)
    delta = close.diff()
    up = delta.clip(lower=0).rolling(14).mean()
    down = (-delta.clip(upper=0)).rolling(14).mean().replace(0.0, float("nan"))
    rs = up / down
    rsi = float((100 - 100 / (1 + rs)).iloc[-1]) if n > 15 else 50.0
    if pd.isna(rsi):
        rsi = 50.0

    # 趋势：价格相对 SMA20 / SMA50 的排列
    sma20 = float(close.rolling(20).mean().iloc[-1])
    sma50 = float(close.rolling(50).mean().iloc[-1]) if n >= 50 else sma20
    if price > sma20 > sma50:
        trend = "多头"
    elif price < sma20 < sma50:
        trend = "空头"
    else:
        trend = "纠缠"

    # 唐奇安突破（复用同一套实现，避免两处口径不一致）
    breakout = compute_breakout_signal(df)

    # ATR 百分比
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    atr_pct = float(tr.rolling(14).mean().iloc[-1] / price) if n > 15 else 0.0

    # 近 20 根高低点：短期支撑 / 阻力
    high_20 = float(high.iloc[-20:].max())
    low_20 = float(low.iloc[-20:].min())

    return {
        "price": round(price, 8),
        "ret_12": round(ret_12, 8),
        "rsi_14": round(rsi, 4),
        "trend": trend,
        "breakout": round(breakout, 4),
        "atr_pct": round(max(atr_pct, 1e-4), 8),
        "high_20": round(high_20, 8),
        "low_20": round(low_20, 8),
    }
