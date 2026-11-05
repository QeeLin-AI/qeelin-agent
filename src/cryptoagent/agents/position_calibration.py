"""持仓管理员参数的事后自迭代（与 `calibration.py` 同一套思想）。

分析师那边靠「平仓方向命中率」校准说话权重；持仓管理员这边靠
「每笔交易的最终 R 与持仓期峰值 MFE」校准退出参数。

三条保守原则，全部由代码强制（不依赖提示词或人的自觉）：

1. **样本不足一律不动**：少于 `MIN_SAMPLES` 笔平仓时返回配置原值，`calibrated=False`。
2. **单轮小步**：每次最多调整 `MAX_STEP`（12%），从当前生效值出发，不许跳变。
   反复迭代就是「多次小步」，任何单次坏样本都不会把参数带跑。
3. **硬边界**：每个参数都有 `BOUNDS`，无论学习多少轮都不会越界；
   规则开关、LLM 开关一律不在可调列表里。

只调 4 个连续参数：
- `trail_activate_r` / `trail_atr_mult`：移动止损的启动点与跟踪宽度；
- `min_progress_r` / `max_hold_bars`：时间止损的宽容度与时限。

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

# 可调参数白名单 + 硬边界。只有这里列出的键会被读写。
BOUNDS: dict[str, tuple[float, float]] = {
    "trail_activate_r": (0.4, 2.5),
    "trail_atr_mult": (1.0, 4.0),
    "min_progress_r": (0.1, 0.8),
    "max_hold_bars": (16.0, 120.0),
}
TUNABLE: tuple[str, ...] = tuple(BOUNDS)

# 单条样本保留上限：只留最近 200 笔，避免 state 无限膨胀
MAX_SAMPLES = 200


def empty_stats() -> dict[str, Any]:
    """新建一份空的绩效记录。"""
    return {"n": 0, "rules": {}, "samples": []}


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
    return stats


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    m = len(s) // 2
    return float(s[m]) if len(s) % 2 else (float(s[m - 1]) + float(s[m])) / 2.0


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def learned_params(base: dict[str, float], stats: dict | None) -> tuple[dict[str, float], bool, list[dict]]:
    """根据绩效记录推出下一版参数。

    返回 `(参数表, 是否已启用校准, 本轮改动列表)`。
    样本不足时原样返回 `base`，`calibrated=False`，改动列表为空。
    """
    eff = {k: float(base.get(k, 0.0) or 0.0) for k in TUNABLE}
    samples = [s for s in ((stats or {}).get("samples") or []) if isinstance(s, dict)]
    if len(samples) < MIN_SAMPLES:
        return eff, False, []

    planned: list[tuple[str, float, str]] = []

    # --- 移动止损：看捕获率与峰值 ---
    trailed = [s for s in samples
               if s.get("rule") == "trail" and float(s.get("mfe") or 0.0) >= 0.3]
    if len(trailed) >= 5:
        caps = [float(s["r"]) / max(float(s["mfe"]), 1e-6) for s in trailed]
        cap_med = _median(caps)
        mfe_med = _median([float(s["mfe"]) for s in trailed])
        if cap_med < CAPTURE_LOW:
            planned.append((
                "trail_atr_mult", 1.0 - MAX_STEP,
                f"捕获率仅 {cap_med:.2f}（冲高后回吐），收紧移动止损的跟踪距离",
            ))
        elif mfe_med < MFE_SMALL:
            planned.append((
                "trail_activate_r", 1.0 + MAX_STEP,
                f"峰值中位仅 {mfe_med:.2f}R（启动过早），推迟移动止损的触发点让盈利跑",
            ))

    # --- 时间止损：看它砍掉的仓是不是好仓 ---
    timed = [float(s.get("r") or 0.0) for s in samples if s.get("rule") == "time_stop"]
    if len(timed) >= 5:
        all_r = [float(s.get("r") or 0.0) for s in samples]
        edge = _mean(timed) - _mean(all_r)
        if edge > TIME_STOP_EDGE:
            planned.append((
                "min_progress_r", 1.0 + MAX_STEP,
                f"时间止损砍掉的仓位平均 {edge:+.2f}R 优于全体，放宽进度要求",
            ))
            planned.append((
                "max_hold_bars", 1.0 + MAX_STEP,
                "同上：给横盘仓更多时间，避免过早清仓",
            ))
        elif edge < -TIME_STOP_EDGE:
            planned.append((
                "max_hold_bars", 1.0 - MAX_STEP,
                f"时间止损砍掉的仓位平均 {edge:+.2f}R 劣于全体，缩短持有上限止损更积极",
            ))

    changes: list[dict] = []
    for name, factor, why in planned:
        lo, hi = BOUNDS[name]
        before = float(eff[name])
        after = before * factor
        after = min(hi, max(lo, after))
        after = float(int(round(after))) if name == "max_hold_bars" else round(after, 4)
        if abs(after - before) < 1e-9:
            continue  # 已顶到边界，不再记一条「没变」的改动
        eff[name] = after
        changes.append({"param": name, "from": round(before, 4), "to": after, "why": why})
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
        "capture_median": capture,
        "avg_r": round(_mean([float(s.get("r") or 0.0) for s in samples]), 3) if samples else None,
        "effective": {k: float(v) for k, v in (effective or {}).items() if k in BOUNDS},
    }
