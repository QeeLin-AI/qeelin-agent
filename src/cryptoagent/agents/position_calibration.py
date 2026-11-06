"""持仓管理员参数的事后自迭代（与 `calibration.py` 同一套思想）。

分析师那边靠「平仓方向命中率」校准说话权重；持仓管理员这边靠
「每笔交易的最终 R 与持仓期峰值 MFE」校准退出参数。

三条保守原则，全部由代码强制（不依赖提示词或人的自觉）：

1. **样本不足一律不动**：少于 `MIN_SAMPLES` 笔平仓时返回配置原值，`calibrated=False`。
2. **单轮小步**：每次最多调整 `MAX_STEP`（12%），从当前生效值出发，不许跳变。
   反复迭代就是「多次小步」，任何单次坏样本都不会把参数带跑。
3. **硬边界**：每个参数都有 `BOUNDS`，无论学习多少轮都不会越界；
   规则开关、LLM 开关一律不在可调列表里。

## 演进分级

- **L1（stats）**：按退出规则归因。`trail` 的样本只调移动止损参数，
  `time_stop` 的样本只调时间止损参数；单规则另有 `rule_min_samples` 门槛。
- **L2（replay）**：L1 的提议还要通过反事实回放（`position_replay.py`）才落地。
- **L3（llm）**：LLM 只提案，代码裁决；动作/参数都必须过门控与边界。

只由 L1 自动调 4 个连续参数（有可靠学习信号）：
- `trail_activate_r` / `trail_atr_mult`：移动止损的启动点与跟踪宽度；
- `min_progress_r` / `max_hold_bars`：时间止损的宽容度与时限。

`trail_min_gap_pct` / `reversal_ema_bars` / `reversal_composite` / `peak_drawdown_r`
已登记边界，但 L1 不给它们学习信号（样本稀疏、缺少反事实对照），只允许 L3
在门控通过后提案——因此它们出现在 `TUNABLE`，不出现在 `L1_TUNABLE`。

学习信号只有两个，都可直接从平仓结果算出：
- **捕获率** `capture = realized_r / mfe_r`（只统计被动过移动止损的仓位）；
  偏低说明「冲高后回吐」，该收紧跟踪；偏高但峰值本身很小，说明「没让它跑」，该推迟启动。
- **时间止损的 R 对比**：被时间止损砍掉的仓位若平均 R 高于全体，
  说明它在砍本来能更好的仓，应放宽；反之说明它有效，可以更早砍。
"""
from __future__ import annotations

from typing import Any

# 样本门槛：持仓管理的可比样本比开仓少得多，20 笔起算
MIN_SAMPLES = 20
# 单轮最大调整幅度
MAX_STEP = 0.12
# 判定「捕获率低/高」的分界
CAPTURE_LOW = 0.55
# 峰值低于该值视为「从未真正跑起来」
MFE_SMALL = 1.0
# 时间止损组与全体的 R 差小于该值时视为无结论
TIME_STOP_EDGE = 0.05
# 单条规则至少多少笔才允许调它（L1 默认；engine 会用配置覆盖）
RULE_MIN_SAMPLES = 8

# 参数默认值：调用方只传部分 base 时用它兜底，避免把缺失项当成 0 而越界
DEFAULTS: dict[str, float] = {
    "trail_activate_r": 1.0,
    "trail_atr_mult": 2.0,
    "trail_min_gap_pct": 0.003,
    "min_progress_r": 0.3,
    "max_hold_bars": 48.0,
    "reversal_ema_bars": 48.0,
    "reversal_composite": 0.7,
    "peak_drawdown_r": 1.0,
}

# 可调参数白名单 + 硬边界。只有这里列出的键会被读写。
BOUNDS: dict[str, tuple[float, float]] = {
    "trail_activate_r": (0.4, 2.5),
    "trail_atr_mult": (1.0, 4.0),
    "trail_min_gap_pct": (0.001, 0.01),
    "min_progress_r": (0.1, 0.8),
    "max_hold_bars": (16.0, 120.0),
    "reversal_ema_bars": (16.0, 96.0),
    "reversal_composite": (0.4, 0.9),
    "peak_drawdown_r": (0.5, 2.0),
}
TUNABLE: tuple[str, ...] = tuple(BOUNDS)

# L1 真正会自动调的键（有可靠学习信号）；其余只允许 L3 提案
L1_TUNABLE: tuple[str, ...] = (
    "trail_activate_r", "trail_atr_mult", "min_progress_r", "max_hold_bars",
)

# 规则 → 它允许影响的参数。L3 提案时按命中规则限定，避免「时间止损的样本
# 去改移动止损参数」这种跨规则污染。
RULE_PARAMS: dict[str, tuple[str, ...]] = {
    "trail": ("trail_activate_r", "trail_atr_mult", "trail_min_gap_pct"),
    "time_stop": ("min_progress_r", "max_hold_bars"),
    "reversal": ("reversal_ema_bars", "reversal_composite"),
    "peak_dd": ("peak_drawdown_r",),
}

RULE_LABELS: dict[str, str] = {
    "trail": "移动止损",
    "time_stop": "时间止损",
    "reversal": "趋势反转",
    "peak_dd": "峰值回撤",
    "stop_loss": "固定止损",
    "take_profit": "止盈",
    "hold": "持有",
    "error": "异常",
}

# 单条样本保留上限：只留最近 200 笔，避免 state 无限膨胀
MAX_SAMPLES = 200


def empty_stats() -> dict[str, Any]:
    """新建一份空的绩效记录。"""
    return {"n": 0, "rules": {}, "by_rule": {}, "samples": []}


def record_episode(stats: dict, rule: str, realized_r: float, mfe_r: float | None) -> dict:
    """把一笔平仓喂进统计；原地修改并返回同一个 dict。

    `mfe_r` 缺失时用 `max(realized_r, 0)` 兜底——宁可低估捕获率，
    也不让缺失值把「利润回吐」误判成「正常」。
    """
    if stats is None:
        return empty_stats()
    try:
        r = float(realized_r)
    except (TypeError, ValueError):
        return stats
    try:
        mfe = float(mfe_r) if mfe_r is not None else max(r, 0.0)
    except (TypeError, ValueError):
        mfe = max(r, 0.0)
    # 极端值截断，避免一笔异常单把中位数带偏
    r = max(-10.0, min(20.0, r))
    mfe = max(-10.0, min(20.0, mfe))

    key = str(rule or "unknown")
    samples = stats.setdefault("samples", [])
    samples.append({"rule": key, "r": round(r, 4), "mfe": round(mfe, 4)})
    if len(samples) > MAX_SAMPLES:
        del samples[: len(samples) - MAX_SAMPLES]

    stats["samples"] = samples
    stats["n"] = int(stats.get("n") or 0) + 1
    rules = stats.setdefault("rules", {})
    rules[key] = int(rules.get(key) or 0) + 1

    # L1：按规则归因。样本池仍然保留全局一份，这里额外记每个规则自己的
    # 笔数 / R 和 / MFE 和，用于「这条规则最近到底在变好还是变差」。
    by = stats.setdefault("by_rule", {})
    row = by.setdefault(key, {"n": 0, "r_sum": 0.0, "mfe_sum": 0.0, "wins": 0})
    row["n"] = int(row.get("n") or 0) + 1
    row["r_sum"] = float(row.get("r_sum") or 0.0) + r
    row["mfe_sum"] = float(row.get("mfe_sum") or 0.0) + mfe
    row["wins"] = int(row.get("wins") or 0) + (1 if r > 0 else 0)
    return stats


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    m = len(s) // 2
    return float(s[m]) if len(s) % 2 else (float(s[m - 1]) + float(s[m])) / 2.0


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _rule_summary(stats: dict | None) -> dict[str, dict]:
    """把每规则的累计值换算成可读指标（不依赖 samples，老存档也能用）。"""
    out: dict[str, dict] = {}
    for rule, row in ((stats or {}).get("by_rule") or {}).items():
        n = int((row or {}).get("n") or 0)
        if n <= 0:
            continue
        avg_r = float(row.get("r_sum") or 0.0) / n
        avg_mfe = float(row.get("mfe_sum") or 0.0) / n
        out[str(rule)] = {
            "n": n,
            "avg_r": round(avg_r, 3),
            "avg_mfe": round(avg_mfe, 3),
            "capture": round(avg_r / max(avg_mfe, 1e-6), 3) if avg_mfe > 0 else None,
            "win_rate": round(int(row.get("wins") or 0) / n, 3),
            "label": RULE_LABELS.get(str(rule), str(rule)),
        }
    return out


def learned_params(
    base: dict[str, float],
    stats: dict | None,
    rule_min_samples: int = RULE_MIN_SAMPLES,
) -> tuple[dict[str, float], bool, list[dict]]:
    """根据绩效记录推出下一版参数。

    返回 `(参数表, 是否已启用校准, 本轮改动列表)`。
    样本不足时原样返回 `base`（补齐默认值），`calibrated=False`，改动列表为空。

    `rule_min_samples` 是 L1 的单规则门槛：全局够 20 笔、但某条规则只有 3 笔时，
    不允许用这 3 笔去改它对应的参数。
    """
    eff = {k: float(base.get(k, DEFAULTS.get(k, 0.0)) or DEFAULTS.get(k, 0.0)) for k in TUNABLE}
    samples = [s for s in ((stats or {}).get("samples") or []) if isinstance(s, dict)]
    if len(samples) < MIN_SAMPLES:
        return eff, False, []

    rule_min = max(5, int(rule_min_samples or RULE_MIN_SAMPLES))
    planned: list[tuple[str, str, float, str]] = []

    # --- 移动止损：看捕获率与峰值 ---
    trailed = [s for s in samples
               if s.get("rule") == "trail" and float(s.get("mfe") or 0.0) >= 0.3]
    if len(trailed) >= rule_min:
        caps = [float(s["r"]) / max(float(s["mfe"]), 1e-6) for s in trailed]
        cap_med = _median(caps)
        mfe_med = _median([float(s["mfe"]) for s in trailed])
        if cap_med < CAPTURE_LOW:
            planned.append((
                "trail", "trail_atr_mult", 1.0 - MAX_STEP,
                f"捕获率仅 {cap_med:.2f}（冲高后回吐），收紧移动止损的跟踪距离",
            ))
        elif mfe_med < MFE_SMALL:
            planned.append((
                "trail", "trail_activate_r", 1.0 + MAX_STEP,
                f"峰值中位仅 {mfe_med:.2f}R（启动过早），推迟移动止损的触发点让盈利跑",
            ))

    # --- 时间止损：看它砍掉的仓是不是好仓 ---
    timed = [float(s.get("r") or 0.0) for s in samples if s.get("rule") == "time_stop"]
    if len(timed) >= rule_min:
        all_r = [float(s.get("r") or 0.0) for s in samples]
        edge = _mean(timed) - _mean(all_r)
        if edge > TIME_STOP_EDGE:
            planned.append((
                "time_stop", "min_progress_r", 1.0 + MAX_STEP,
                f"时间止损砍掉的仓位平均 {edge:+.2f}R 优于全体，放宽进度要求",
            ))
            planned.append((
                "time_stop", "max_hold_bars", 1.0 + MAX_STEP,
                "同上：给横盘仓更多时间，避免过早清仓",
            ))
        elif edge < -TIME_STOP_EDGE:
            planned.append((
                "time_stop", "max_hold_bars", 1.0 - MAX_STEP,
                f"时间止损砍掉的仓位平均 {edge:+.2f}R 劣于全体，缩短持有上限止损更积极",
            ))

    changes: list[dict] = []
    for rule, name, factor, why in planned:
        if name not in L1_TUNABLE:
            continue
        lo, hi = BOUNDS[name]
        before = float(eff[name])
        after = before * factor
        after = min(hi, max(lo, after))
        after = float(int(round(after))) if name == "max_hold_bars" else round(after, 4)
        if abs(after - before) < 1e-9:
            continue  # 已顶到边界，不再记一条「没变」的改动
        eff[name] = after
        changes.append({"param": name, "from": round(before, 4), "to": after,
                        "rule": rule, "why": why})
    return eff, True, changes


def summary(stats: dict | None, effective: dict | None = None) -> dict:
    """给状态接口 / 页面用的可读摘要。"""
    stats = stats or {}
    samples = [s for s in (stats.get("samples") or []) if isinstance(s, dict)]
    rules = dict(stats.get("rules") or {})
    trailed = [s for s in samples
               if s.get("rule") == "trail" and float(s.get("mfe") or 0.0) >= 0.3]
    capture = None
    if trailed:
        capture = round(_median([float(s["r"]) / max(float(s["mfe"]), 1e-6) for s in trailed]), 3)
    return {
        "samples": len(samples),
        "min_samples": MIN_SAMPLES,
        "calibrated": len(samples) >= MIN_SAMPLES,
        "rules": rules,
        # L1：按规则归因的明细（老存档只有 rules 计数时，这里可能为空）
        "by_rule": _rule_summary(stats),
        "capture_median": capture,
        "avg_r": round(_mean([float(s.get("r") or 0.0) for s in samples]), 3) if samples else None,
        "effective": {k: float(v) for k, v in (effective or {}).items() if k in BOUNDS},
    }
