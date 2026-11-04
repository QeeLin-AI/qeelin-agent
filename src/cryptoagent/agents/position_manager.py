"""持仓管理员（第五个角色）：负责「开仓之后到平仓之前」的退出决策。

原来的决策链只管开仓：3 分析师 → 辩论 → 交易员 → 风控官 → 风控引擎。
仓位开出来之后，止损止盈是挂在交易所的静态条件单，引擎每轮直接 skipped，
既不跑 LLM 也不做任何判断 —— 趋势走反不提前退、浮盈不保本、横盘不撤出。

这个角色补的就是这段空白。设计上和全项目保持一致：

- **动作由确定性规则决定**（移动止损 / 时间止损 / 趋势反转），LLM 只写解释；
- 规则算不出结论一律 hold，模型幻觉不能触发平仓；
- 所有阈值用「R」归一化（R = 开仓时 |entry - stop| / entry），
  不同杠杆、不同止损宽度的仓位共用一套参数；
- 只在持仓时运行。此时开仓决策本来就是 skipped，规则计算不增加任何网络/LLM 成本；
  `llm_explain` 默认关闭，打开才会多一次 LLM 调用。
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from ..config import PositionConfig

SYSTEM_POSITION_MANAGER = (
    "你是持仓管理员。你只解释已经由规则引擎做出的持仓管理动作，不要建议新动作、"
    "不要发明数字、不要开新仓。用一两句话说明这个动作在权衡什么（锁定利润、"
    "控制时间成本、避免趋势反转回吐），中文，不超过 60 字。"
)


@dataclass
class PositionDecision:
    action: str = "hold"                 # hold | tighten | close
    reason: str = "持仓无变化"
    new_stop: float | None = None
    details: dict[str, Any] = field(default_factory=dict)
    explanation: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _opened_epoch(pos: dict) -> float | None:
    """开仓时刻（epoch 秒）。新仓位直接读字段；老仓位从 opened_at 字符串还原。"""
    raw = pos.get("opened_epoch")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    text = str(pos.get("opened_at") or "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


def _bar_seconds(df: pd.DataFrame) -> float:
    try:
        idx = pd.DatetimeIndex(df.index)
        if len(idx) >= 3:
            delta = (idx[-1] - idx[-2]).total_seconds()
            if 1.0 <= delta <= 86400.0 * 7:
                return float(delta)
    except Exception:  # noqa: BLE001 - 索引异常时退回默认
        pass
    return 3600.0


def _atr_pct(df: pd.DataFrame, price: float) -> float:
    try:
        h, l, c = df["high"], df["low"], df["close"]
        tr = pd.concat(
            [h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1
        ).max(axis=1)
        atr = float(tr.rolling(14, min_periods=5).mean().iloc[-1])
        if atr > 0 and price > 0:
            return min(0.20, atr / price)
    except Exception:  # noqa: BLE001
        pass
    return 0.01


def _hold_state(pos: dict, df: pd.DataFrame, price: float) -> dict:
    """把仓位与行情整理成规则需要的最小状态。"""
    side = str(pos.get("side") or "LONG").upper()
    sign = 1.0 if side.startswith("L") else -1.0
    entry = float(pos.get("entry") or 0.0)
    stop = float(pos.get("stop") or 0.0)
    if entry <= 0 or stop <= 0:
        return {"valid": False}

    # R 固化在开仓时；老仓位没有该字段就用当前止损近似（保守）
    r_pct = float(pos.get("initial_r_pct") or 0.0)
    if r_pct <= 0:
        r_pct = abs(entry - stop) / entry
    r_pct = max(r_pct, 1e-4)

    opened = _opened_epoch(pos)
    bars = 0
    if opened is not None:
        bars = max(0, int((time.time() - opened) / _bar_seconds(df)))

    fav = (price - entry) / entry * sign
    # 开仓以来的极值（含当前价）；窗口按持有根数截取，避免把开仓前的行情算进来
    n = max(3, min(len(df), bars + 2))
    tail = df.tail(n)
    try:
        extreme = float(tail["high"].max()) if sign > 0 else float(tail["low"].min())
    except Exception:  # noqa: BLE001
        extreme = price
    peak = max(extreme, price) if sign > 0 else min(extreme, price)

    return {
        "valid": True,
        "side": side,
        "sign": sign,
        "entry": entry,
        "stop": stop,
        "r_pct": r_pct,
        "bars": bars,
        "fav": fav,
        "peak": peak,
        "atr_pct": _atr_pct(df, price),
    }


def _reversal(st: dict, df: pd.DataFrame, price: float, cfg: PositionConfig,
              composite: float | None, details: dict) -> tuple[bool, str]:
    """趋势反转：均线转向 + 近 3 根反向动量，或因子强烈反向。"""
    sign = st["sign"]
    c = df["close"].astype(float)
    if len(c) >= 50:
        ema_slow = c.ewm(span=48, adjust=False).mean()
        below = sign * (float(c.iloc[-1]) - float(ema_slow.iloc[-1])) < 0
        mom = 0.0
        if len(c) >= 4:
            mom = sign * (float(c.iloc[-1]) / max(float(c.iloc[-4]), 1e-9) - 1.0)
        details["below_slow_ma"] = bool(below)
        details["mom_3"] = round(mom, 5)
        if below and mom < -0.002:
            return True, "趋势反转：价格跌破慢均线且近 3 根持续反向，提前退出"
    if composite is not None:
        details["factor_composite"] = round(float(composite), 4)
        if sign * float(composite) <= -abs(cfg.reversal_composite):
            return True, f"因子信号强烈反向（{composite:+.2f}），提前退出"
    return False, ""


def manage_position(
    pos: dict,
    df: pd.DataFrame,
    price: float,
    cfg: PositionConfig | None = None,
    composite: float | None = None,
) -> PositionDecision:
    """纯规则：返回 hold / tighten / close。绝不抛异常，异常一律退化为 hold。"""
    cfg = cfg or PositionConfig()
    if not cfg.enabled or df is None or len(df) < 20 or price <= 0:
        return PositionDecision("hold", "持仓管理员未启用或数据不足")
    try:
        st = _hold_state(pos, df, price)
        if not st["valid"]:
            return PositionDecision("hold", "仓位缺少开仓价/止损，跳过管理")

        details: dict[str, Any] = {
            "bars_held": st["bars"],
            "floating_r": round(st["fav"] / st["r_pct"], 2),
            "atr_pct": round(st["atr_pct"], 5),
            "side": st["side"],
        }

        # 1) 时间止损：横盘耗时间、资金效率太低
        if st["bars"] >= int(cfg.max_hold_bars) and st["fav"] < cfg.min_progress_r * st["r_pct"]:
            details["max_hold_bars"] = int(cfg.max_hold_bars)
            return PositionDecision(
                "close",
                f"时间止损：持有 {st['bars']} 根，浮盈仅 {st['fav'] / st['r_pct']:+.2f}R，退出释放资金",
                details=details,
            )

        # 2) 趋势反转：均线与因子都指向反方时不等静态止损
        if cfg.reversal_enabled and st["bars"] >= int(cfg.reversal_min_hold_bars):
            hit, why = _reversal(st, df, price, cfg, composite, details)
            if hit:
                return PositionDecision("close", why, details=details)

        # 3) 移动止损：浮盈达到 1R 后先保本，再按 ATR 跟踪极值
        if st["fav"] >= cfg.trail_activate_r * st["r_pct"]:
            sign, dist = st["sign"], cfg.trail_atr_mult * st["atr_pct"]
            if sign > 0:
                candidate = st["peak"] * (1.0 - dist)
                breakeven = st["entry"] * 1.001
                new_stop = min(max(candidate, breakeven, st["stop"]), price * 0.999)
                improved = (new_stop - st["stop"]) / max(st["stop"], 1e-9)
            else:
                candidate = st["peak"] * (1.0 + dist)
                breakeven = st["entry"] * 0.999
                new_stop = max(min(candidate, breakeven, st["stop"]), price * 1.001)
                improved = (st["stop"] - new_stop) / max(st["stop"], 1e-9)
            details["candidate_stop"] = round(float(new_stop), 8)
            details["improvement_pct"] = round(float(improved), 5)
            if improved >= cfg.trail_min_gap_pct:
                return PositionDecision(
                    "tighten",
                    f"移动止损：浮盈 {st['fav'] / st['r_pct']:+.2f}R，止损移到 {new_stop:.6g}（保本/跟踪极值）",
                    new_stop=float(new_stop),
                    details=details,
                )

        return PositionDecision("hold", "持仓条件未触发调整", details=details)
    except Exception as exc:  # noqa: BLE001 - 持仓管理是旁路，绝不因它中断主流程
        return PositionDecision("hold", f"持仓管理计算失败，保守不动：{type(exc).__name__}",
                                details={"error": str(exc)[:120]})


def explain_management(llm, decision: PositionDecision, pos: dict) -> str:
    """可选：让 LLM 写一段「为什么这样管」的解释。失败返回空串，不影响动作。"""
    if decision.action == "hold":
        return ""
    try:
        user = (
            f"品种 {pos.get('symbol', '')}，方向 {pos.get('side', '')}，"
            f"开仓价 {pos.get('entry')}，当前动作 {decision.action}，"
            f"规则理由：{decision.reason}。请只解释权衡，不要建议别的动作。"
        )
        resp = llm.text(SYSTEM_POSITION_MANAGER, user, mock=decision.reason)
        return str(getattr(resp, "text", "") or "").strip()[:200]
    except Exception:  # noqa: BLE001
        return ""
