"""全品种缓存预热的护栏测试。

高频档（`auto_top_k > 1`）一轮会依次切多个品种，切换走 `load_market`——
盘缓存过期就当场重下 6000 根 K 线。预热线程要守住三条：
1. 只预热「可交易品种」，且**跳过当前品种**（它的行情由 refresh_data 负责）；
2. 主周期按 `cache_warm_main_sec` 节流，别每轮都重读 6 份 CSV；
3. 任一品种失败都不影响其它品种，也不影响调用方。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _bare_engine(symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"), cur="BTCUSDT", **mkt):
    from cryptoagent.config import AppConfig
    from cryptoagent.engine import TradingEngine

    eng = TradingEngine.__new__(TradingEngine)
    eng.cfg = AppConfig()
    eng.cfg.market.symbols = list(symbols)
    eng.cfg.market.symbol = cur
    eng.cfg.market.source = "binance"
    eng.cfg.market.cache_all_symbols = True
    eng.cfg.market.cache_warm_main_sec = 300
    for k, v in mkt.items():
        setattr(eng.cfg.market, k, v)
    eng.state = {"exchange": "binance"}
    eng._quote_cache = {}
    eng._kl_short_warm_at = {}
    eng._warm_busy = False
    eng._warm_main_at = 0.0
    eng._warm_last = 0.0
    eng._warm_last_result = {}
    eng._stamp_price = lambda sym, px: None
    eng._frame_cache = {}
    return eng


def _patch(monkeypatch, calls, price_ok=True):
    def _price(sym, prefer=None):
        if not price_ok:
            raise RuntimeError("network down")
        calls["price"].append(sym)
        return 100.0, "binance"

    def _load(cfg, prefer=None, **kw):
        calls["load"].append(cfg.symbol)
        return None, "stub"

    def _kl(sym, iv, n, prefer=None, **kw):
        calls["kl"].append(sym)
        return None, "stub"

    monkeypatch.setattr("cryptoagent.engine.fetch_live_price", _price)
    monkeypatch.setattr("cryptoagent.engine.load_market", _load)
    monkeypatch.setattr("cryptoagent.engine.fetch_klines", _kl)
    monkeypatch.setattr("cryptoagent.engine.kl_disk_ttl", lambda iv: 0.0)


def test_warms_all_symbols_but_skips_current(monkeypatch):
    eng = _bare_engine()
    calls = {"price": [], "load": [], "kl": []}
    _patch(monkeypatch, calls)

    out = eng.warm_symbol_cache()

    assert calls["price"] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]   # 报价覆盖全部品种
    assert calls["load"] == ["ETHUSDT", "SOLUSDT"]               # 主周期跳过当前品种
    assert calls["kl"] == ["ETHUSDT", "SOLUSDT"]                 # 短周期同理
    assert out["symbols"] == 3 and out["quotes"] == 3 and out["main"] == 2


def test_disabled_does_nothing(monkeypatch):
    eng = _bare_engine(cache_all_symbols=False)
    calls = {"price": [], "load": [], "kl": []}
    _patch(monkeypatch, calls)
    assert eng.warm_symbol_cache() == {}
    assert calls["price"] == []


def test_main_interval_is_throttled(monkeypatch):
    eng = _bare_engine()
    eng._warm_main_at = time.time()          # 刚预热过
    calls = {"price": [], "load": [], "kl": []}
    _patch(monkeypatch, calls)

    out = eng.warm_symbol_cache()

    assert calls["load"] == []               # 主周期被节流
    assert out["main"] == 0
    assert calls["price"]                    # 报价仍然刷新


def test_price_failure_does_not_block_others(monkeypatch):
    eng = _bare_engine()
    calls = {"price": [], "load": [], "kl": []}
    _patch(monkeypatch, calls, price_ok=False)

    out = eng.warm_symbol_cache()

    assert out["quotes"] == 0
    assert calls["load"] == ["ETHUSDT", "SOLUSDT"]   # K 线预热照常进行


def test_synthetic_source_skips_klines(monkeypatch):
    eng = _bare_engine()
    eng.cfg.market.source = "synthetic"
    calls = {"price": [], "load": [], "kl": []}
    _patch(monkeypatch, calls)

    out = eng.warm_symbol_cache()

    assert calls["price"]      # 合成行情不需要打交易所，但报价仍可走
    assert calls["load"] == [] and calls["kl"] == []
    assert out["main"] == 0 and out["short"] == 0


def test_reentrancy_guard(monkeypatch):
    eng = _bare_engine()
    eng._warm_busy = True
    calls = {"price": [], "load": [], "kl": []}
    _patch(monkeypatch, calls)
    assert eng.warm_symbol_cache() == {}
    assert calls["price"] == []


def test_symbols_are_capped(monkeypatch):
    many = tuple(f"S{i}USDT" for i in range(20))
    eng = _bare_engine(symbols=many, cur="S0USDT")
    calls = {"price": [], "load": [], "kl": []}
    _patch(monkeypatch, calls)
    eng.warm_symbol_cache()
    assert len(calls["price"]) == 12      # 上限 12，避免一次打爆限频


# ---------------- 运行时长 ----------------

def test_uptime_seconds_counts_from_start():
    import time as _t
    eng = _bare_engine()
    eng.started_at = _t.time() - 3661
    assert 3660 <= eng.uptime_seconds() <= 3665


def test_uptime_seconds_is_zero_when_missing():
    eng = _bare_engine()                  # __new__ 构造：没有 started_at
    assert not hasattr(eng, "started_at")
    assert eng.uptime_seconds() == 0      # 进 status 的字段，绝不能抛错
