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
