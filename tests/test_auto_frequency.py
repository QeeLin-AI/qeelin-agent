"""自动循环的「一轮多品种」选品逻辑。

提频的关键不是让循环更快（一轮仍旧只看一个品种），而是让一轮能看见多个品种：
`auto_top_k` 决定同一轮依次评估几个「未持仓且达标」的品种。这里守住三点：
- 已持仓的品种必须排除（否则切过去只会被「已有持仓」跳过，白烧一次决策）；
- 低于 rotate_min_score 的不算机会；
- 按分数降序，且 K 有上限，避免一次把 LLM 成本打爆。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _bare_engine(top_k: int = 2, min_score: float = 0.2, rows=None, held=None):
    from cryptoagent.config import AppConfig
    from cryptoagent.engine import TradingEngine

    eng = TradingEngine.__new__(TradingEngine)
    eng.cfg = AppConfig()
    eng.cfg.market.auto_top_k = top_k
    eng.cfg.market.rotate_min_score = min_score
    eng.scan_state = {"last_ts": time.time()}
    eng.opportunities = rows if rows is not None else [
        {"symbol": "BTCUSDT", "score": 0.9},
        {"symbol": "ETHUSDT", "score": 0.7},
        {"symbol": "SOLUSDT", "score": 0.5},
        {"symbol": "XRPUSDT", "score": 0.3},
        {"symbol": "DOGEUSDT", "score": 0.1},   # 低于阈值
        {"symbol": "BNBUSDT", "score": 0.95, "error": "取数失败"},
    ]
    held_map = {s: {} for s in (held or ["ETHUSDT"])}
    eng._positions_map = lambda: held_map
    return eng


def test_top_k_returns_ranked_unheld_symbols():
    eng = _bare_engine(top_k=2, held=["ETHUSDT"])
    assert eng._top_symbols() == ["BTCUSDT", "SOLUSDT"]


def test_top_k_one_matches_old_behaviour():
    """top_k=1 时与「一轮只切最强的一个」完全等价。"""
    eng = _bare_engine(top_k=1)
    assert eng._top_symbols() == ["BTCUSDT"]


def test_low_score_and_error_rows_are_skipped():
    eng = _bare_engine(top_k=8)          # 默认已持有 ETHUSDT
    out = eng._top_symbols()
    assert "DOGEUSDT" not in out          # 低于 rotate_min_score
    assert "BNBUSDT" not in out           # 取数失败的行不能当机会
    assert "ETHUSDT" not in out           # 已持仓：切过去只会被跳过
    assert out == ["BTCUSDT", "SOLUSDT", "XRPUSDT"]


def test_top_k_zero_disables_multi_entry():
    eng = _bare_engine(top_k=0)
    assert eng._top_symbols() == []


def test_top_k_is_capped():
    """K 上限 8：防止把 LLM 成本按品种数打爆。"""
    eng = _bare_engine(top_k=99)
    assert len(eng._top_symbols()) <= 8


def test_empty_scan_returns_nothing():
    eng = _bare_engine(top_k=2, rows=[])
    eng.scan_state = {"last_ts": time.time()}
    eng._scan_opportunities = lambda: []
    assert eng._top_symbols() == []
