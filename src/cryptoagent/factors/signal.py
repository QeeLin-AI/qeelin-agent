"""因子信号：把 factor_library 里研究产出的因子接入实盘决策。

因子研究（research.py）会产出候选因子，通过 train/valid/test 三段验证后，
把「最优因子」写进 factor_library 目录。本模块负责在实盘决策时：

1. 读取因子库里的因子代码
2. 对当前行情算因子值，再用 factor_to_target 转成 [-1, 1] 的目标敞口
3. 加权合成一个复合信号

权重口径（2026-11 修正）
------------------------
旧实现有两个结构问题：

- `top_n=8` 一刀切：内置因子权重全是 1.0，稳定排序后永远只有字典里最前的
  7 个内置因子 + 1 个研究因子参与，后面 RSI/布林/ATR/MFI 等十几个全部闲置。
- 量纲不一致：内置因子权重是人为写的 1.0，研究因子权重是回测夏普，
  一个夏普 3.0 的研究因子就能压过全部内置因子，两者根本不可比。

现在改成「分组归一化」：内置组与研究组各自在组内归一化，再按组间权重合成。
组间默认 内置 0.6 / 研究 0.4（没有研究因子时内置自动占满 1.0）。
研究因子的夏普来自单品种回测，实证检验显示单因子三段夏普普遍为负、
绝对数值不可靠，所以不给它压倒性权重。
这样两类因子的权重都变成「相对影响力」，量纲一致，且内置因子不再被截断。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

from ..backtest import factor_to_target
from .library import FACTOR_REQUIRES, LIBRARY
from .sandbox import FactorError, run_factors_batch


VALIDATED_PATH = Path(__file__).with_name("validated.json")


def _validated_names() -> set[str]:
    """读 OOS 验证通过的因子名单（由 tools/validate_factors.py 生成）。"""
    try:
        data = json.loads(VALIDATED_PATH.read_text(encoding="utf-8"))
        return {str(n) for n in data.get("validated", [])}
    except (OSError, ValueError, TypeError, AttributeError):
        return set()


def load_factor_files(lib_dir: str | Path, max_research: int = 8) -> list[dict]:
    """读因子库目录，返回 [{name, code, holdout_sharpe, kind, raw_weight}]。

    - 内置参考因子**全量参与**（不再被 top_n 截断），权重打底；
    - 研究产出的、留出集夏普为正的因子按夏普降序取前 `max_research` 个叠加。
      限制研究因子数量是为了控制机会扫描（6 品种 × N 因子）的计算量。
    """
    passed = _validated_names()
    out: list[dict] = [
        {
            "name": b.name,
            "code": b.code,
            "holdout_sharpe": 1.0,
            "kind": "builtin",
            # 未经 OOS 验证的新因子压到 1/4 影响力；通过三段验证后由
            # validated.json 自动提升到 1.0，和老因子平起平坐。
            "raw_weight": 1.0 if (getattr(b, "validated", True) or b.name in passed) else 0.25,
        }
        for b in LIBRARY.values()
    ]

    d = Path(lib_dir)
    if d.is_dir():
        research: list[dict] = []
        for f in sorted(d.glob("*.py")):
            text = f.read_text(encoding="utf-8")
            m = re.search(r"holdout sharpe:\s*([-+]?\d+\.?\d*)", text)
            holdout = float(m.group(1)) if m else 0.0
            if holdout > 0:
                research.append({
                    "name": f.stem,
                    "code": text,
                    "holdout_sharpe": holdout,
                    "kind": "research",
                    "raw_weight": holdout,
                })
        research.sort(key=lambda x: -x["holdout_sharpe"])
        out.extend(research[: max(0, int(max_research))])

    return out


def _effective_weights(files: list[dict], builtin_group_weight: float) -> list[float]:
    """组内归一化 + 组间配比，返回每个因子的有效权重。

    没有任何研究因子时，内置组独占总权重；否则按 builtin_group_weight 分配。
    """
    builtin_idx = [i for i, f in enumerate(files) if f.get("kind") == "builtin"]
    research_idx = [i for i, f in enumerate(files) if f.get("kind") == "research"]
    bw = sum(max(0.0, float(files[i]["raw_weight"])) for i in builtin_idx)
    rw = sum(max(0.0, float(files[i]["raw_weight"])) for i in research_idx)

    share_b = float(builtin_group_weight) if rw > 0 else 1.0
    share_b = min(1.0, max(0.0, share_b))
    share_r = 1.0 - share_b
    # 某一组权重为 0 时，把配额让给另一组，避免整体信号被稀释成 0
    if bw <= 0:
        share_b, share_r = 0.0, 1.0
    if rw <= 0:
        share_b, share_r = 1.0, 0.0

    scale_b = share_b / bw if bw > 0 else 0.0
    scale_r = share_r / rw if rw > 0 else 0.0
    out = [0.0] * len(files)
    for i in builtin_idx:
        out[i] = max(0.0, float(files[i]["raw_weight"])) * scale_b
    for i in research_idx:
        out[i] = max(0.0, float(files[i]["raw_weight"])) * scale_r
    return out


def compute_factor_signal(
    lib_dir: str | Path,
    df: pd.DataFrame,
    window: int = 200,
    cap: float = 1.0,
    deadband: float = 0.15,
    top_n: int | None = None,
    max_research: int = 8,
    builtin_group_weight: float = 0.6,
) -> dict:
    """对因子库每个因子算当前信号，加权合成复合信号。

    返回 {"composite": float, "signals": [{name, target, weight, holdout_sharpe, kind}]}
    composite ∈ [-1, 1]：正=看多，负=看空，0=无信号。

    `top_n` 仅为兼容旧调用保留：默认 None = 不截断（内置因子全参与）；
    传入正整数时对最终列表做硬截断（不建议再用，会重新引入上述偏斜）。
    """
    files = load_factor_files(lib_dir, max_research=max_research)
    if top_n is not None and int(top_n) > 0:
        files = files[: int(top_n)]
    # 当前数据源没有因子需要的列时，连加载都不做（避免一堆全 NaN 因子稀释信号）
    available = set(map(str, df.columns))
    files = [
        f for f in files
        if all(c in available for c in FACTOR_REQUIRES.get(f["name"], ()))
    ]
    if not files:
        return {"composite": 0.0, "signals": [], "skipped": []}

    base_weights = _effective_weights(files, builtin_group_weight)
    signals: list[dict] = []
    skipped: list[dict] = []
    weights: list[float] = []
    # 一次子进程跑完全部因子：单开进程的固定开销（解释器 + pandas ≈1.25s）远大于
    # 计算本身，逐个 spawn 会随因子数线性变慢；批量把这段开销摊薄成一次。
    values_map: dict[str, Any] = {}
    try:
        values_map, _errors = run_factors_batch(
            files, df, timeout=max(45, min(180, 4 * len(files)))
        )
    except FactorError:
        values_map = {}
    except Exception:  # noqa: BLE001 - 批量失败时退化为「全部因子无信号」，不阻塞决策
        values_map = {}

    for f, w in zip(files, base_weights):
        err = ""
        try:
            values = values_map.get(f["name"])
            if values is None:
                raise FactorError("因子未产出结果")
            # 列存在但全是 NaN（Gate/OKX 补的空列）也要淘汰，否则一堆
            # target=0 会把复合信号稀释向 0。
            if int(values.notna().sum()) < max(20, window // 4):
                raise FactorError("因子有效值不足（所需数据列缺失）")
            if float(values.fillna(0.0).std()) <= 1e-12:
                raise FactorError("因子为常数（如现货数据没有资金费率）")
            target = factor_to_target(values, window=window, cap=cap, deadband=deadband)
            t = float(target.iloc[-1])
        except (FactorError, ValueError, IndexError, KeyError) as exc:
            t, w = 0.0, 0.0
            err = str(exc)[:100]
        if w <= 0:
            skipped.append({"name": f["name"], "reason": err or "权重为 0"})
            continue
        signals.append({
            "name": f["name"],
            "target": round(t, 4),
            "weight": round(w, 6),
            "holdout_sharpe": f["holdout_sharpe"],
            "kind": f.get("kind", "builtin"),
        })
        weights.append(w)

    if not any(weights):
        return {"composite": 0.0, "signals": signals, "skipped": skipped}

    total = sum(weights)
    composite = sum(s["target"] * s["weight"] for s in signals) / total
    return {"composite": round(float(composite), 4),
            "signals": signals, "skipped": skipped}
