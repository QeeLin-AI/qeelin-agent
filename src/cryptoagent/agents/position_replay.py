"""L2 反事实回放：候选退出参数在同一段历史上到底会不会更好。

为什么需要它：L1 只能看到「已经发生的出场」——一笔仓被时间止损砍掉，
就永远不知道换一组参数会不会让它跑成 2R。只看已实现 R 做参数学习，
本质是在拟合一条不可反事实的路径，很容易被单一行情带偏。

做法：
1. 平仓时把「开仓/平仓时间、方向、开仓价、止损、止盈、初始 R」存成 case；
2. 回放时按开仓时间从当前 K 线里切出「warmup + 持仓期」，逐 bar 调用
   `manage_position()`（与实盘同一套规则，不复制逻辑）；
3. 分别用「当前参数」和「候选参数」跑，得到两条 R 序列；
4. 只有候选中位 R 不劣于当前、且最差单笔不显著恶化，才允许 L2 门控通过。

回放是纯函数、无副作用；任何异常都返回 skipped，绝不阻断交易主流程。
"""
from __future__ import annotations

from typing import Callable

import pandas as pd

from .position_manager import manage_position
from ..config import PositionConfig

# 回放窗口上限：控制耗时（20 笔 × 240 根 ≈ 几千次纯函数调用）
DEFAULT_MAX_BARS = 240
# 开仓前至少保留多少根，供 EMA / ATR 计算
MIN_WARMUP = 200


def _r_of(case: dict, exit_px: float) -> float | None:
    entry = float(case.get("entry") or 0.0)
    r_pct = float(case.get("initial_r_pct") or 0.0)
    if entry <= 0 or r_pct <= 0 or exit_px <= 0:
        return None
    side = str(case.get("side") or "LONG").upper()
    sign = 1.0 if side.startswith("L") else -1.0
    return (float(exit_px) - entry) / entry * sign / r_pct


def replay_case(case: dict, df: pd.DataFrame, cfg: PositionConfig,
                max_bars: int = DEFAULT_MAX_BARS) -> float | None:
    """回放单笔持仓，返回候选参数下的 R；数据不足/异常返回 None。"""
    if df is None or len(df) < 30:
        return None
    opened = case.get("opened_epoch")
    if opened is None:
        return None
    try:
        idx = pd.DatetimeIndex(df.index)
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        opened_ts = pd.Timestamp(float(opened), unit="s", tz="UTC")
        start = int(idx.searchsorted(opened_ts, side="left"))
        if start <= 0 or start >= len(idx):
            return None
        warmup = max(MIN_WARMUP, int(getattr(cfg, "reversal_ema_bars", 48) or 48) + 50)
        lo = max(0, start - warmup)
        hi = min(len(idx) - 1, start + max(1, int(max_bars)))
        pos = {
            "symbol": case.get("symbol", ""),
            "side": case.get("side", "LONG"),
            "entry": float(case.get("entry") or 0.0),
            "stop": float(case.get("stop") or 0.0),
            "tp": float(case.get("tp") or 0.0),
            "opened_epoch": float(opened),
            "initial_r_pct": float(case.get("initial_r_pct") or 0.0),
        }
        if pos["entry"] <= 0 or pos["stop"] <= 0:
            return None
        closes = df["close"].astype(float)
        exit_px: float | None = None
        for i in range(start, hi + 1):
            price = float(closes.iloc[i])
            if price <= 0:
                continue
            sub = df.iloc[lo:i + 1]
            dec = manage_position(pos, sub, price, cfg, composite=None, composite_ts=None)
            if dec.action == "close":
                exit_px = price
                break
            if dec.action == "tighten" and dec.new_stop:
                try:
                    pos["stop"] = float(dec.new_stop)
                except (TypeError, ValueError):
                    pass
        if exit_px is None:
            exit_px = float(closes.iloc[hi])
        return _r_of(case, exit_px)
    except Exception:  # noqa: BLE001 - 回放是旁路，失败即跳过
        return None


def evaluate(
    cases: list[dict],
    get_frame: Callable[[str], pd.DataFrame | None],
    base_cfg: PositionConfig,
    cand_cfg: PositionConfig,
    window: int = 100,
    min_cases: int = 20,
    min_median_delta: float = 0.0,
    max_worst_delta: float = 0.2,
) -> dict:
    """对候选参数做反事实回放，返回门控结果与逐项检查。"""
    picked = [c for c in (cases or []) if isinstance(c, dict)][-max(1, int(window)):]
    deltas: list[float] = []
    skipped = 0
    for case in picked:
        df = get_frame(str(case.get("symbol") or ""))
        if df is None or len(df) < 30:
            skipped += 1
            continue
        base_r = replay_case(case, df, base_cfg)
        cand_r = replay_case(case, df, cand_cfg)
        if base_r is None or cand_r is None:
            skipped += 1
            continue
        deltas.append(float(cand_r) - float(base_r))
    used = len(deltas)
    checks = {
        "enough_cases": used >= max(1, int(min_cases)),
        "median_not_worse": False,
        "worst_not_worse": False,
    }
    median_delta = worst_delta = None
    if used:
        ordered = sorted(deltas)
        mid = used // 2
        median_delta = (ordered[mid] if used % 2
                        else (ordered[mid - 1] + ordered[mid]) / 2.0)
        worst_delta = ordered[0]
        checks["median_not_worse"] = median_delta >= float(min_median_delta)
        checks["worst_not_worse"] = worst_delta >= -abs(float(max_worst_delta))
    passed = all(checks.values())
    return {
        "passed": bool(passed),
        "cases": used,
        "skipped": skipped,
        "median_delta": round(median_delta, 4) if median_delta is not None else None,
        "worst_delta": round(worst_delta, 4) if worst_delta is not None else None,
        "checks": checks,
    }
