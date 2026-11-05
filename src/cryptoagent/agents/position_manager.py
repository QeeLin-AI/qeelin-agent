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
- LLM 接入分两级：`llm_explain` 只写解释；`llm_shadow`（影子模式）额外给独立退出意见，
  但**只记日志、绝不改变动作**。两者共用同一次调用，由 engine 的谷时/冷却闸门控制频率。
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

SYSTEM_SHADOW = (
    "你是持仓管理员的独立复核员。只依据给定数字判断，不要发明数据、不要建议开新仓、"
    "不要建议放宽止损。explain 只解释规则已做出的动作；action 是你的独立退出意见"
    "（hold / tighten / close），只能是维持或提前退出。输出严格 JSON，中文，"
    "每个文本字段不超过 60 字。"
)

SHADOW_SCHEMA = (
    "- explain: 用一两句话解释规则动作在权衡什么\n"
    "- action: hold 或 tighten 或 close（你的独立意见）\n"
    "- confidence: 0 到 1 之间的数字\n"
    "- reason: 给出该独立意见的理由"
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


def _held_bars(pos: dict, df: pd.DataFrame, from_index: bool = True) -> int:
    """持有根数。

    用「最后一根 K 线的时间 - 开仓时间」而不是 `time.time() - 开仓时间`：
    行情停更 / 数据源切换时，墙钟会继续把 bars 往上加，可能误触发时间止损。
    `.timestamp()` 对 naive 索引按本地时区解释，与 `_opened_epoch()` 的口径一致。
    """
    opened = _opened_epoch(pos)
    if opened is None:
        return 0
    step = _bar_seconds(df)
    if from_index:
        try:
            last = pd.DatetimeIndex(df.index)[-1]
            last_epoch = float(last.timestamp())
            # K 线明显早于开仓（回测/测试/数据停更/时区错位）时不能按索引算，
            # 否则 bars 会被夹成 0、时间止损永远不触发 —— 回退墙钟估算。
            if last_epoch >= opened:
                return max(0, int((last_epoch - opened) / step))
        except Exception:  # noqa: BLE001 - 索引异常时回退墙钟估算
            pass
    return max(0, int((time.time() - opened) / step))


def _hold_state(pos: dict, df: pd.DataFrame, price: float,
                from_index: bool = True) -> dict:
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

    bars = _held_bars(pos, df, from_index)

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
    span = max(10, int(getattr(cfg, "reversal_ema_bars", 48) or 48))
    if len(c) >= span + 2:
        ema_slow = c.ewm(span=span, adjust=False).mean()
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
    composite_ts: float | None = None,
) -> PositionDecision:
    """纯规则：返回 hold / tighten / close。绝不抛异常，异常一律退化为 hold。"""
    cfg = cfg or PositionConfig()
    if not cfg.enabled or df is None or len(df) < 20 or price <= 0:
        return PositionDecision("hold", "持仓管理员未启用或数据不足")
    try:
        st = _hold_state(pos, df, price, bool(getattr(cfg, "bars_from_index", True)))
        if not st["valid"]:
            return PositionDecision("hold", "仓位缺少开仓价/止损，跳过管理")

        details: dict[str, Any] = {
            "bars_held": st["bars"],
            "floating_r": round(st["fav"] / st["r_pct"], 2),
            "atr_pct": round(st["atr_pct"], 5),
            "side": st["side"],
        }

        # 机会扫描的 composite 有缓存：过期信号不该拿来触发「提前平仓」。
        # 没带时间戳时保持旧行为（调用方还拿不到 ts），有 ts 才做新鲜度校验。
        max_age = int(getattr(cfg, "composite_max_age_sec", 300) or 0)
        if composite is not None and composite_ts is not None and max_age > 0:
            age = max(0.0, time.time() - float(composite_ts))
            details["composite_age_sec"] = round(age, 1)
            if age > max_age:
                composite = None
                details["composite_stale"] = True

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

        # 3) 峰值回撤退出：新增行为，默认关闭（需模拟盘验证后再开）。
        #    只针对「曾经浮盈到过 1R 以上、又回吐 N×R」的仓位，避免把正常震荡当反转。
        if (getattr(cfg, "peak_drawdown_exit_enabled", False)
                and st["bars"] >= int(cfg.reversal_min_hold_bars)):
            peak_fav = (st["peak"] - st["entry"]) / st["entry"] * st["sign"]
            drawdown_r = (peak_fav - st["fav"]) / st["r_pct"]
            details["peak_fav_r"] = round(peak_fav / st["r_pct"], 2)
            details["drawdown_r"] = round(drawdown_r, 2)
            if (peak_fav >= cfg.trail_activate_r * st["r_pct"]
                    and drawdown_r >= float(getattr(cfg, "peak_drawdown_r", 1.0))):
                return PositionDecision(
                    "close",
                    f"峰值回撤：浮盈自高点回吐 {drawdown_r:.2f}R，提前退出保住利润",
                    details=details,
                )

        # 4) 移动止损：浮盈达到 1R 后先保本，再按 ATR 跟踪极值
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
        return str(resp.content or "").strip()[:200]
    except Exception:  # noqa: BLE001
        return ""


def shadow_review(llm, decision: PositionDecision, pos: dict) -> dict | None:
    """影子模式：LLM 独立给一份退出意见，只用于对照记录，绝不触发动作。

    用 `llm.json()`（带 schema 校验 + 重试 + mock 兜底），输出不合规时返回 None。
    """
    d = decision.details or {}
    mock = {
        "explain": decision.reason,
        "action": decision.action,
        "confidence": 0.0,
        "reason": "规则兜底（模型不可用）",
    }
    user = (
        f"品种 {pos.get('symbol', '')}，方向 {pos.get('side', '')}，"
        f"开仓价 {pos.get('entry')}，持有 {d.get('bars_held', '--')} 根，"
        f"浮盈 {d.get('floating_r', '--')}R，ATR% {d.get('atr_pct', '--')}，"
        f"因子复合 {d.get('factor_composite', '--')}。\n"
        f"规则动作：{decision.action}；规则理由：{decision.reason}。\n"
        f"请给出 explain（解释规则动作）与 action（你的独立退出意见）。"
    )
    try:
        data, resp = llm.json(SYSTEM_SHADOW, user, SHADOW_SCHEMA, mock)
        act = str(data.get("action") or "hold").strip().lower()
        if act not in ("hold", "tighten", "close"):
            act = "hold"
        try:
            conf = max(0.0, min(1.0, float(data.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            conf = 0.0
        return {
            "explain": str(data.get("explain") or "").strip()[:200],
            "action": act,
            "confidence": round(conf, 3),
            "reason": str(data.get("reason") or "").strip()[:200],
            "source": str(getattr(resp, "provider", "") or ""),
            "degraded": bool(getattr(resp, "degraded", False)),
        }
    except Exception:  # noqa: BLE001 - 复核是旁路
        return None


def review_management(llm, cfg, decision: PositionDecision, pos: dict) -> dict:
    """一次 LLM 调用同时产出「解释」和「影子意见」；两个开关都关时零调用。

    返回 `{"explanation": str, "shadow": dict|None}`。调用方只把结果写进决策记录，
    **绝不据此修改 action** —— 影子意见正确与否只能靠日志事后对照。
    """
    out: dict[str, Any] = {"explanation": "", "shadow": None}
    want_shadow = bool(getattr(cfg, "llm_shadow", False))
    want_explain = bool(getattr(cfg, "llm_explain", False)) and decision.action != "hold"
    if not want_shadow and not want_explain:
        return out
    if want_shadow:
        sh = shadow_review(llm, decision, pos)
        if sh:
            out["shadow"] = sh
            out["explanation"] = sh["explain"]
            return out
        # 影子调用失败 → 退到纯解释（仍不阻断动作）
    if want_explain:
        out["explanation"] = explain_management(llm, decision, pos)
    return out
