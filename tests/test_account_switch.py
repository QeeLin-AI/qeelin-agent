"""账户切换：作用域判定、切换保护、缓存隔离与流水归户。"""
from __future__ import annotations

import threading
import time

import pytest

from cryptoagent.config import AppConfig
from cryptoagent.engine import TradingEngine


class _FakeClient:
    armed = False

    def ping(self) -> bool:
        return True

    def account(self) -> dict:
        return {"balance": 500.0, "unrealized_pnl": 0.0, "available": 500.0}


def _engine(scope: str = "", positions: dict | None = None) -> TradingEngine:
    e = object.__new__(TradingEngine)
    e.cfg = AppConfig()
    e.state = {
        "mode": "paper",
        "testnet": True,
        "exchange": "binance",
        "account_scope": scope,
        "equity": 1000.0,
        "peak_equity": 1200.0,
        "day_start_equity": 1100.0,
        "day": "2026-11-06",
        "positions": dict(positions or {}),
        "position": None,
        "fills": [],
        "position_history": [],
        "consecutive_losses": 3,
        "auto": {"running": False, "interval_sec": 300, "execute": False, "last_run": ""},
        "halt": False,
        "history": [{"equity": 1200.0}],
    }
    e.lock = threading.RLock()
    e.armed = False
    e.live_client = None
    e._live_eq_cache = (999.0, time.time())
    e._save = lambda: None
    e.reload_for_exchange = lambda: None
    e._auto_stop = threading.Event()
    return e


def test_scope_uses_exchange_and_env():
    assert TradingEngine._account_scope("binance", True) == "binance:demo"
    assert TradingEngine._account_scope("OKX", False) == "okx:live"


def test_same_exchange_env_change_resets_account_state(monkeypatch):
    """binance demo -> binance live 也必须重置，不能沿用 demo 的峰值/日基准。"""
    monkeypatch.setenv("CRYPTOAGENT_I_UNDERSTAND_THE_RISK", "yes")
    e = _engine(scope="binance:demo")
    monkeypatch.setattr(e, "connect", lambda **k: _FakeClient())

    e.arm("live", "我确认使用真实资金", testnet=False, exchange="binance")

    assert e.state["account_scope"] == "binance:live"
    assert e.state["equity"] == pytest.approx(500.0)
    assert e.state["peak_equity"] == pytest.approx(500.0)
    assert e.state["day_start_equity"] == pytest.approx(500.0)
    assert e.state["consecutive_losses"] == 0
    assert e.state["history"] == []
    assert e._live_eq_cache is None


def test_switch_blocks_live_position_without_force(monkeypatch):
    e = _engine(
        scope="binance:demo",
        positions={"BTCUSDT": {"symbol": "BTCUSDT", "mode": "live", "qty": 1}},
    )
    monkeypatch.setattr(e, "connect", lambda **k: _FakeClient())

    with pytest.raises(PermissionError, match="实盘持仓"):
        e.arm("live", "我确认使用真实资金", testnet=False, exchange="binance")
    assert e.state["account_scope"] == "binance:demo"


def test_switch_force_allows_live_position(monkeypatch):
    monkeypatch.setenv("CRYPTOAGENT_I_UNDERSTAND_THE_RISK", "yes")
    e = _engine(
        scope="binance:demo",
        positions={"BTCUSDT": {"symbol": "BTCUSDT", "mode": "live", "qty": 1}},
    )
    monkeypatch.setattr(e, "connect", lambda **k: _FakeClient())

    e.arm("live", "我确认使用真实资金", testnet=False, exchange="binance", force=True)
    assert e.state["account_scope"] == "binance:live"


def test_switch_blocks_when_auto_running(monkeypatch):
    e = _engine(scope="binance:demo")
    e.state["auto"]["running"] = True
    monkeypatch.setattr(e, "connect", lambda **k: _FakeClient())

    with pytest.raises(PermissionError, match="自动交易"):
        e.arm("live", "我确认使用真实资金", testnet=False, exchange="binance")


def test_same_scope_rearm_keeps_state(monkeypatch):
    e = _engine(scope="binance:demo")
    e.state["peak_equity"] = 1300.0
    monkeypatch.setattr(e, "connect", lambda **k: _FakeClient())

    e.arm("demo", "", testnet=True, exchange="binance")
    assert e.state["peak_equity"] == pytest.approx(1300.0)
    assert e.state["account_scope"] == "binance:demo"


def test_reset_keeps_audit_but_drops_curve():
    e = _engine(scope="binance:demo")
    e.state["position_history"] = [{"pnl": 1.0}]
    e.state["fills"] = [{"action": "OPEN"}]

    e._reset_account_scope({"balance": 800.0, "unrealized_pnl": 0.0}, scope="okx:live")

    assert e.state["position_history"]
    assert e.state["fills"]
    assert e.state["history"] == []
    assert e.state["account_scope"] == "okx:live"


def test_daily_pnl_filters_by_scope():
    e = _engine()
    e.state["position_history"] = [
        {"closed_at": "2026-11-06 10:00:00", "pnl": 10.0, "account_scope": "binance:demo"},
        {"closed_at": "2026-11-06 11:00:00", "pnl": -5.0, "account_scope": "binance:live"},
        # 旧记录没有标签：升级兼容，归入当前查询的 scope
        {"closed_at": "2026-11-05 09:00:00", "pnl": 3.0},
    ]
    rows = e._daily_pnl(scope="binance:demo")
    assert [r["date"] for r in rows] == ["2026-11-05", "2026-11-06"]
    assert rows[0]["pnl"] == pytest.approx(3.0)
    assert rows[1]["pnl"] == pytest.approx(10.0)


def test_reload_for_exchange_keeps_klines(monkeypatch):
    """切换交易所只重置失败熔断，不删任何 K 线缓存。"""
    from cryptoagent import data

    key = ("BTCUSDT", "5m", 10, "binance")
    data._KL_CACHE[key] = (time.time(), None, "cached")
    e = _engine(scope="binance:demo")
    e.data_source = ""
    e.data_loading = False
    e._reloading = False
    e.refresh_data = lambda force=False: (None, "")

    TradingEngine.reload_for_exchange(e)
    time.sleep(0.2)

    assert key in data._KL_CACHE
    data._KL_CACHE.pop(key, None)


def test_disarm_stops_auto_and_switches_to_paper():
    e = _engine()
    e.state["auto"]["running"] = True
    e.stop_auto = lambda: e.state["auto"].update({"running": False})

    e.disarm()

    assert e.state["mode"] == "paper"
    assert e.state["auto"]["running"] is False
    assert e.armed is False
