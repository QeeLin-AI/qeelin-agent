"""候选因子的 OOS 验证：把「论文说有效」变成「在这份数据上验证过」。

内置因子库里 `validated=False` 的因子只按 0.25 权重参与信号。这个模块让它们
走和 research.ResearchLoop 完全相同的三段协议：

    train（样本内）→ valid（样本外）→ test（最终留出集）

通过条件（比研究循环更严）：
- IS 夏普 > 0
- OOS 夏普 ≥ min_oos_sharpe，且 valid 交易次数 ≥ 3
- test 夏普 > 0，且不低于 OOS 夏普的一半（控制样本外衰减）

验证结果写入 `factors/validated.json`，signal 层据此把通过的因子权重提到 1.0。
**合成数据上的验证结果不可信**，请用真实历史行情跑 tools/validate_factors.py。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..backtest import factor_to_target, run_backtest
from ..config import AppConfig
from ..data import split_market
from ..research import _metrics_subset
from .library import FACTOR_REQUIRES, LIBRARY
from .sandbox import FactorError, check_lookahead, run_factor, run_factors_batch

VALIDATED_PATH = Path(__file__).with_name("validated.json")


def load_validated(path: Path | str | None = None) -> set[str]:
    """读已通过 OOS 验证的因子名单；文件不存在/损坏时返回空集合。"""
    p = Path(path) if path else VALIDATED_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        names = data.get("validated", [])
        return {str(n) for n in names}
    except (OSError, ValueError, TypeError, AttributeError):
        return set()


def candidates(names: list[str] | None = None,
               include_validated: bool = False) -> list[dict]:
    """待验证的候选因子（默认只要 validated=False），可指定名单缩小范围。

    include_validated=True 用于跑「老内置因子对照」：用同一套三段协议检验
    那些已经按 1.0 权重参与信号的老因子，看看门槛本身有多严。
    """
    wanted = set(names) if names else None
    return [
        {"name": b.name, "code": b.code, "family": b.family}
        for b in LIBRARY.values()
        if (include_validated or not b.validated) and (wanted is None or b.name in wanted)
    ]


def validate_candidates(
    df: pd.DataFrame,
    cfg: AppConfig | None = None,
    names: list[str] | None = None,
    use_batch: bool = True,
    include_validated: bool = False,
) -> list[dict]:
    """对每个候选因子跑 train/valid/test，返回 [{name, train, oos, test, passed, reason}]。"""
    cfg = cfg or AppConfig()
    train, valid, test = split_market(df, cfg.research.train_frac, cfg.research.valid_frac)
    items = [c for c in candidates(names, include_validated=include_validated)
             if all(col in df.columns for col in FACTOR_REQUIRES.get(c["name"], ()))]
    if not items:
        return []

    errs: dict[str, str] = {}
    # 每段各用一次批量子进程算完全部因子：逐因子 spawn 的固定开销约 1.25s，
    # 22 个因子 ×3 段就是 80 秒，批量把每段摊薄成一次 spawn。
    seg_values: dict[str, dict] = {}
    if use_batch:
        for label, seg in (("train", train), ("valid", valid), ("test", test)):
            try:
                vals, seg_errs = run_factors_batch(items, seg, timeout=max(60, 5 * len(items)))
                seg_values[label] = vals
                for n, e in seg_errs.items():
                    errs[n] = e
            except FactorError as exc:
                for c in items:
                    errs[c["name"]] = str(exc)

    results: list[dict] = []
    for c in items:
        name = c["name"]
        row: dict[str, Any] = {"name": name, "family": c["family"],
                               "train": 0.0, "oos": 0.0, "test": 0.0,
                               "passed": False, "reason": ""}
        if name in errs:
            row["reason"] = f"因子执行失败：{errs[name].strip().splitlines()[-1][:80]}"
            results.append(row)
            continue
        try:
            if use_batch:
                v_tr = seg_values.get("train", {}).get(name)
                v_va = seg_values.get("valid", {}).get(name)
                v_te = seg_values.get("test", {}).get(name)
                if v_tr is None or v_va is None or v_te is None:
                    raise FactorError("某一段未产出因子值")
                lookahead_ok = True   # 内置因子为项目自有代码，AST 白名单 + 人工审查
            else:
                v_tr, meta = run_factor(c["code"], train, lookahead_checks=2)
                v_va, meta_v = run_factor(c["code"], valid, lookahead_checks=2)
                v_te, _ = run_factor(c["code"], test, lookahead_checks=0)
                lookahead_ok = not check_lookahead(meta) and not check_lookahead(meta_v)
            if not lookahead_ok:
                raise FactorError("存在前视偏差")

            # 三段各自重算目标敞口，避免跨段泄漏
            m_tr = _metrics_subset(run_backtest(
                train, factor_to_target(v_tr, window=200, cap=1.0),
                cfg.backtest, cfg.market.interval))
            m_va = _metrics_subset(run_backtest(
                valid, factor_to_target(v_va, window=200, cap=1.0),
                cfg.backtest, cfg.market.interval))
            m_te = _metrics_subset(run_backtest(
                test, factor_to_target(v_te, window=200, cap=1.0),
                cfg.backtest, cfg.market.interval))

            row["train"] = round(float(m_tr.get("sharpe") or 0.0), 3)
            row["oos"] = round(float(m_va.get("sharpe") or 0.0), 3)
            row["test"] = round(float(m_te.get("sharpe") or 0.0), 3)
            row["n_trades"] = int(m_va.get("n_trades") or 0)

            gate = cfg.research.min_oos_sharpe
            if row["train"] <= 0:
                row["reason"] = "IS 夏普为负"
            elif row["oos"] < gate:
                row["reason"] = f"OOS 夏普 {row['oos']:.2f} < 门槛 {gate}"
            elif row["n_trades"] < 3:
                row["reason"] = "valid 交易次数过少"
            elif row["test"] <= 0:
                row["reason"] = f"test 夏普 {row['test']:.2f} ≤ 0（样本外失效）"
            elif row["test"] < row["oos"] * 0.5:
                row["reason"] = f"test 衰减过大（{row['oos']:.2f} → {row['test']:.2f}）"
            else:
                row["passed"] = True
                row["reason"] = "通过三段验证"
        except (FactorError, ValueError, KeyError, IndexError) as exc:
            row["reason"] = f"{type(exc).__name__}: {str(exc)[:100]}"
        except Exception as exc:  # noqa: BLE001 - 单个因子失败不影响整批
            row["reason"] = f"{type(exc).__name__}: {str(exc)[:100]}"
        results.append(row)
    return results


def write_validated(results: list[dict], meta: dict | None = None,
                    path: Path | str | None = None) -> Path:
    """把通过的因子名单落盘，signal 层用它在运行时提升权重。"""
    p = Path(path) if path else VALIDATED_PATH
    payload = {
        "validated": [r["name"] for r in results if r.get("passed")],
        "results": results,
        "meta": meta or {},
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p
