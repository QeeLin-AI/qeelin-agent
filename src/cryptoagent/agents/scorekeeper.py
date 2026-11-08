"""绩效官：全系统唯一的 KPI 口径。

为什么单列一个模块：L1 参数校准、L2 反事实回放、L3 持仓进化、因子研究
各自都在算「最近表现怎么样」。口径不一致时，一个系统说变好、另一个说
变差，进化官就无法判断改动是否真的有效。这里只做一件事：给所有系统
同一份数字。

口径选择（重要）：
- 一律以「按笔 R」为主，不看账户权益。权益会被充值、切换账户、重置锚点
  污染，不是策略能力；R 是每笔交易相对初始风险的结果，才是可比较的。
- 少于 `MIN_SAMPLE` 笔时 `sample_ok=False`，调用方不得据此下结论。
"""
from __future__ import annotations

import math
import time

# 统计显著所需的最小样本。低于这个数只报样本量，不允许据此调参。
MIN_SAMPLE = 20

# 接近/超越真人交易员的门槛（与项目文档中的五维标准一致）。
# 只作为判断常量导出，不参与自动决策；跨 regime 稳定性需另行统计。
BENCHMARKS = {
    "pass": {"r_sharpe": 1.0, "calmar": 0.8, "max_dd_r": -25.0, "expectancy_r": 0.02},
    "near_human": {"r_sharpe": 1.5, "calmar": 1.5, "max_dd_r": -15.0, "expectancy_r": 0.08},
    "beyond": {"r_sharpe": 2.0, "calmar": 2.5, "max_dd_r": -12.0, "expectancy_r": 0.15},
}

# 达到「可评估」状态所需的最少平仓笔数（用户标准：>=100 笔 + >=3 个月）
MIN_TRADES_FOR_VERDICT = 100


def _num(value) -> float | None:
    try:
        f = float(value)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _stats(rows: list[dict]) -> dict:
    rs = [x for x in (_num(r.get("realized_r")) for r in rows) if x is not None]
    n = len(rs)
    if n == 0:
        return {"n": len(rows), "n_with_r": 0}

    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    mean = sum(rs) / n
    var = sum((x - mean) ** 2 for x in rs) / (n - 1) if n > 1 else 0.0
    std = math.sqrt(var)

    # 累计 R 曲线的最大回撤（负数，单位 R）
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for x in rs:
        cum += x
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)

    return {
        "n": len(rows),
        "n_with_r": n,
        "win_rate": round(len(wins) / n, 4),
        "avg_r": round(mean, 4),
        "expectancy_r": round(mean, 4),
        "avg_win_r": round(gross_win / len(wins), 4) if wins else None,
        "avg_loss_r": round(-gross_loss / len(losses), 4) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
        "r_sharpe": round(mean / std, 4) if std > 0 else None,
        "max_dd_r": round(mdd, 4),
        "total_r": round(cum, 4),
    }


def _group(rows: list[dict], key_fn) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        try:
            k = str(key_fn(r) or "")
        except Exception:  # noqa: BLE001 - 分组键异常不能影响整体绩效
            continue
        if k:
            out.setdefault(k, []).append(r)
    return out


def compute_kpis(rows: list[dict], min_sample: int = MIN_SAMPLE) -> dict:
    """从平仓历史算统一 KPI。rows 允许含老记录（缺字段自动跳过）。"""
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    overall = _stats(rows)

    out: dict = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "min_sample": int(min_sample),
        "min_trades_for_verdict": MIN_TRADES_FOR_VERDICT,
        "sample_ok": overall.get("n_with_r", 0) >= int(min_sample),
        # 只有达到 100 笔才允许对外宣称「接近/超越真人」，之前一律视为观察期
        "verdict_ready": overall.get("n_with_r", 0) >= MIN_TRADES_FOR_VERDICT,
        "overall": overall,
        "benchmarks": BENCHMARKS,
    }

    # 捕获率：实现 R / 最大浮盈 R，衡量「看对了能不能拿住」。
    # 只统计「浮盈曾到过 0.5R」的样本，并把负值截到 0：
    # 把从未盈利的单也算进来会把均值拉成负数，指标就失去意义了。
    caps = []
    for r in rows:
        rr, mfe = _num(r.get("realized_r")), _num(r.get("mfe_r"))
        if rr is not None and mfe is not None and mfe >= 0.5:
            caps.append(max(0.0, min(2.0, rr / mfe)))
    if caps:
        out["capture_ratio_avg"] = round(sum(caps) / len(caps), 4)
        out["capture_sample"] = len(caps)

    # 复盘官的结构化归因汇总：哪类错误最多，直接决定进化官下一步该修什么
    reviews: dict[str, int] = {}
    mistakes: dict[str, int] = {}
    for r in rows:
        rv = r.get("review")
        if not isinstance(rv, dict):
            continue
        v = str(rv.get("verdict") or "")
        if v:
            reviews[v] = reviews.get(v, 0) + 1
        for m in rv.get("mistakes") or []:
            mistakes[str(m)] = mistakes.get(str(m), 0) + 1
    if reviews:
        total = sum(reviews.values())
        out["review_summary"] = {
            "n": total,
            "by_verdict": reviews,
            "by_mistake": dict(sorted(mistakes.items(), key=lambda kv: -kv[1])),
            "good_ratio": round(reviews.get("good", 0) / total, 4),
        }

    out["by_rule"] = {
        k: _stats(v) for k, v in _group(
            rows, lambda r: r.get("rule") or r.get("reason") or "").items()
    }
    out["by_symbol"] = {
        k: _stats(v) for k, v in _group(
            rows, lambda r: str(r.get("symbol") or "").upper()).items()
    }
    out["by_direction"] = {
        k: _stats(v) for k, v in _group(
            rows,
            lambda r: "SHORT" if str(r.get("side") or "").upper().startswith("S") else "LONG",
        ).items()
    }
    out["by_verdict"] = {
        k: _stats(v) for k, v in _group(
            rows, lambda r: (r.get("review") or {}).get("verdict") or "").items()
    }
    return out


def kpi_delta(before: dict, after: dict) -> dict:
    """比较两份 KPI（进化官用它判断「改完到底有没有变好」）。

    只比可比的量，不做统计显著性检验——样本不足时由调用方看 sample_ok。
    """
    keys = ("expectancy_r", "win_rate", "profit_factor", "r_sharpe", "max_dd_r", "total_r")
    a = (before or {}).get("overall") or {}
    b = (after or {}).get("overall") or {}
    out = {}
    for k in keys:
        va, vb = _num(a.get(k)), _num(b.get(k))
        out[k] = {
            "before": va,
            "after": vb,
            "delta": round(vb - va, 4) if va is not None and vb is not None else None,
        }
    return out
