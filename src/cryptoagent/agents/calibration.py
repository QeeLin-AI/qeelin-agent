"""分析师权重的事后校准。

原来的辩论评分写死 `0.5 × 技术 + 0.3 × 资金费率 + 0.2 × 情绪`，
这组数字是拍脑袋定的，从来没有用真实结果验证过。

这里用一个极简、可审计的在线方案：

- 每次开仓把当时三位分析师的 stance 存进仓位；
- 平仓时用「退出价相对开仓价的市场方向」评判每个分析师看对了没有
  （用市场方向而非这笔仓的盈亏，因为开仓方向由交易员定，不该算到分析师头上）；
- 累计命中率，用拉普拉斯平滑 + 与默认权重各占一半的混合方式生成新权重。

保守设计：
- 样本数不足 min_samples 时**完全沿用默认权重**，避免前几笔就剧烈抖动；
- 平滑 + 混合后单角色权重永远落在 [0.1, 0.7]，不会让某个角色一家独大；
- 中性立场（|stance| < 0.05）不计数。
"""
from __future__ import annotations

DEFAULT_WEIGHTS: tuple[float, float, float] = (0.5, 0.3, 0.2)
ROLES = ("技术", "资金费率", "情绪")
NEUTRAL_BAND = 0.05


def record_accuracy(stats: dict, stances: list[float], market_dir: float) -> dict:
    """把一次平仓的方向结果记进 stats；原地修改并返回同一个 dict。

    stats 结构：{"0": {"hit": int, "total": int}, "1": {...}, "2": {...}}
    用索引而不是角色名做键，避免角色文案改动后历史记录失效。
    """
    if market_dir == 0 or not stances:
        return stats
    for i, raw in enumerate(stances[:3]):
        try:
            stance = float(raw)
        except (TypeError, ValueError):
            continue
        if abs(stance) < NEUTRAL_BAND:
            continue
        row = stats.setdefault(str(i), {"hit": 0, "total": 0})
        row["total"] = int(row.get("total", 0)) + 1
        if stance * market_dir > 0:
            row["hit"] = int(row.get("hit", 0)) + 1
    return stats


def _project_weights(weights: list[float], lo: float = 0.1, hi: float = 0.7) -> list[float]:
    """把权重投影到「每个都在 [lo, hi] 且总和为 1」的可行域。

    直接 clamp 再除以总和会把上界重新推高（0.7 / 0.96 ≈ 0.729），
    所以每次归一化后按「可调整空间」重新分配差额，迭代到收敛。
    可行域非空（3 × 0.1 ≤ 1 ≤ 3 × 0.7），该迭代必然收敛。
    """
    w = [float(x) for x in weights]
    for _ in range(12):
        w = [min(hi, max(lo, x)) for x in w]
        diff = 1.0 - sum(w)
        if abs(diff) < 1e-12:
            break
        room = [(hi - x) if diff > 0 else (x - lo) for x in w]
        total_room = sum(room)
        if total_room <= 1e-12:
            break
        w = [x + diff * r / total_room for x, r in zip(w, room)]
    return [min(hi, max(lo, x)) for x in w]


def weights_from_accuracy(
    stats: dict | None, min_samples: int = 10
) -> tuple[list[float], bool]:
    """返回 (权重列表, 是否已启用校准)。样本不足时返回默认权重与 False。"""
    stats = stats or {}
    totals = [int((stats.get(str(i)) or {}).get("total", 0)) for i in range(3)]
    if sum(totals) < int(min_samples):
        return list(DEFAULT_WEIGHTS), False
    # 拉普拉斯平滑：没有样本的角色仍保留基础话语权
    rates = [
        (int((stats.get(str(i)) or {}).get("hit", 0)) + 1.0) / (totals[i] + 2.0)
        for i in range(3)
    ]
    s = sum(rates) or 1.0
    learned = [r / s for r in rates]
    # 与先验各占一半：少数几笔交易不足以推翻先前的经验设定
    blended = [0.5 * d + 0.5 * l for d, l in zip(DEFAULT_WEIGHTS, learned)]
    return _project_weights(blended), True


def summary(stats: dict | None) -> dict:
    """给状态接口用的可读摘要。"""
    weights, calibrated = weights_from_accuracy(stats)
    acc = {}
    for i, role in enumerate(ROLES):
        row = (stats or {}).get(str(i)) or {}
        total = int(row.get("total", 0))
        hit = int(row.get("hit", 0))
        acc[role] = {"hit": hit, "total": total,
                     "accuracy": round(hit / total, 3) if total else None}
    return {"weights": [round(w, 4) for w in weights],
            "calibrated": calibrated, "roles": acc}
