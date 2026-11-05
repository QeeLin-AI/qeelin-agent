"""因子库健康检查：每个内置因子都必须能在沙箱里跑出有效序列。

存在的意义：因子是一段段独立代码字符串，写错（列名拼错、除零、pandas API 用错）
在运行期才会暴露，而信号层会把出错的因子静默降权为 0——不测就发现不了，
表现为「因子越来越多、复合信号却没变化」。

这里用一份合成的 OHLCV + 衍生品数据批量跑全部因子，断言：
- 没有沙箱错误（语法白名单、运行时异常）；
- 有效值足够多、且不是常数（否则信号层会跳过它）；
另外对新增因子抽样做一次未来函数检查。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cryptoagent.factors.library import FACTOR_REQUIRES, LIBRARY
from cryptoagent.factors.sandbox import run_factor, run_factors_batch

N = 700


def _frame(seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-01", periods=N, freq="h")
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.004, N) + 0.0002))
    vol = rng.lognormal(6, 0.5, N)
    return pd.DataFrame({
        "open": close * (1 + rng.normal(0, 0.001, N)),
        "high": close * (1 + np.abs(rng.normal(0, 0.003, N))),
        "low": close * (1 - np.abs(rng.normal(0, 0.003, N))),
        "close": close,
        "volume": vol,
        "funding_rate": pd.Series(rng.normal(0, 8e-5, N)).ewm(span=20).mean().values,
        "quote_volume": vol * close,
        "trades": rng.integers(500, 5000, N).astype(float),
        "taker_base": vol * rng.uniform(0.4, 0.6, N),
        "taker_quote": vol * close * rng.uniform(0.4, 0.6, N),
        "basis": pd.Series(rng.normal(2e-4, 4e-4, N)).ewm(span=10).mean().values,
        "open_interest": 1e6 * np.exp(np.cumsum(rng.normal(0, 0.01, N))),
        "taker_ratio": rng.uniform(0.8, 1.2, N),
        "ls_ratio": rng.uniform(0.8, 1.4, N),
    }, index=idx)


def test_library_has_no_duplicate_names():
    names = [f.name for f in LIBRARY.values()]
    assert len(names) == len(set(names))
    for key, f in LIBRARY.items():
        assert key == f.name, f"字典键 {key} 与因子名 {f.name} 不一致"


def test_required_columns_are_known():
    df = _frame()
    for name, cols in FACTOR_REQUIRES.items():
        assert name in LIBRARY, f"FACTOR_REQUIRES 里的 {name} 不在因子库"
        for c in cols:
            assert c in df.columns, f"{name} 要求的列 {c} 不存在"


def test_every_factor_runs_in_sandbox():
    """全量跑一遍：不许有错误，且产出不是常数。"""
    df = _frame()
    items = [{"name": n, "code": f.code} for n, f in LIBRARY.items()]
    values, errors = run_factors_batch(items, df, timeout=120)
    assert errors == {}, f"以下因子执行失败：{list(errors.items())[:3]}"

    dead = []
    for name in LIBRARY:
        v = values.get(name)
        if v is None or int(v.notna().sum()) < 50 or float(v.fillna(0).std()) <= 1e-12:
            dead.append(name)
    assert dead == [], f"以下因子产出退化为常数/空值：{dead}"


@pytest.mark.parametrize("name", ["adx_14", "aroon_25", "roll_spread", "rsi_divergence_14"])
def test_new_factors_have_no_lookahead(name):
    """抽样做未来函数检查：用前 k 根算出的末值必须与全量算出的同一点一致。"""
    vals, meta = run_factor(LIBRARY[name].code, _frame())
    checks = meta.get("lookahead") or []
    assert checks, "没有执行未来函数检查"
    for c in checks:
        assert c["consistent"], f"{name} 在 k={c['k']} 处不一致：{c}"
