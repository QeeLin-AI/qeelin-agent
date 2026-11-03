"""因子信号：把 factor_library 里研究产出的因子接入实盘决策。

因子研究（research.py）会产出候选因子，通过 train/valid/test 三段验证后，
把「最优因子」写进 factor_library 目录。本模块负责在实盘决策时：

1. 读取因子库里的因子代码
2. 对当前行情算因子值，再用 factor_to_target 转成 [-1, 1] 的目标敞口
3. 按「留出集夏普」加权合成一个复合信号

关键：权重 = max(0, holdout_sharpe)。留出集（样本外）表现差的因子（负夏普）
权重为 0，等价于被自动淘汰——这就是「复盘淘汰」在信号层面的一道闸。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd

from ..backtest import factor_to_target
from .sandbox import FactorError, run_factors_batch


def load_factor_files(lib_dir: str | Path) -> list[dict]:
    """读因子库目录，返回 [{name, code, holdout_sharpe}]，按留出集夏普降序。

    内置参考因子**始终参与**（权重 1.0 打底）；研究循环产出的、留出集夏普为正的因子
    叠加进来（权重=留出集夏普，验证过的因子权重更高）。这样既不会因子库为空时无信号，
    也不会因为只有一个研究因子而丢掉内置的 20+ 个参考因子。
    """
    from .library import LIBRARY

    out: list[dict] = [{"name": b.name, "code": b.code, "holdout_sharpe": 1.0} for b in LIBRARY.values()]

    d = Path(lib_dir)
    if d.is_dir():
        for f in sorted(d.glob("*.py")):
            text = f.read_text(encoding="utf-8")
            m = re.search(r"holdout sharpe:\s*([-+]?\d+\.?\d*)", text)
            holdout = float(m.group(1)) if m else 0.0
            if holdout > 0:
                out.append({"name": f.stem, "code": text, "holdout_sharpe": holdout})

    out.sort(key=lambda x: -x["holdout_sharpe"])
    return out


def compute_factor_signal(
    lib_dir: str | Path,
    df: pd.DataFrame,
    window: int = 200,
    cap: float = 1.0,
    deadband: float = 0.15,
    top_n: int = 8,
) -> dict:
    """对因子库每个因子算当前信号，加权合成复合信号。

    返回 {"composite": float, "signals": [{name, target, weight, holdout_sharpe}]}
    composite ∈ [-1, 1]：正=看多，负=看空，0=无信号。
    """
    files = load_factor_files(lib_dir)[:top_n]
    if not files:
        return {"composite": 0.0, "signals": []}

    signals: list[dict] = []
    weights: list[float] = []
    # 一次子进程跑完全部因子：单开进程的固定开销（解释器 + pandas ≈1.2s）远大于计算本身，
    # 8 个因子逐个 spawn 要 10 秒/品种，全品种扫描会被拖到 1 分钟。
    values_map: dict[str, Any] = {}
    try:
        values_map, _errors = run_factors_batch(files, df, timeout=max(30, 10 * len(files)))
    except FactorError:
        values_map = {}
    except Exception:  # noqa: BLE001 - 批量失败时退化为「全部因子无信号」，不阻塞决策
        values_map = {}

    for f in files:
        w = max(0.0, f["holdout_sharpe"])          # 留出集为负 → 权重 0（淘汰）
        if w <= 0:
            signals.append({"name": f["name"], "target": 0.0, "weight": 0.0,
                            "holdout_sharpe": f["holdout_sharpe"]})
            continue
        try:
            values = values_map.get(f["name"])
            if values is None:
                raise FactorError("因子未产出结果")
            target = factor_to_target(values, window=window, cap=cap, deadband=deadband)
            t = float(target.iloc[-1])
        except FactorError:
            t, w = 0.0, 0.0
        signals.append({"name": f["name"], "target": round(t, 4), "weight": round(w, 4),
                        "holdout_sharpe": f["holdout_sharpe"]})
        weights.append(w)

    if not any(weights):
        return {"composite": 0.0, "signals": signals}

    total = sum(weights)
    composite = sum(s["target"] * s["weight"] for s in signals) / total
    return {"composite": round(float(composite), 4), "signals": signals}
