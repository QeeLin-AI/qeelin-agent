"""多品种并发持仓：开仓 / 平仓 / 止损 / 组合层风控都要按品种独立。"""
from __future__ import annotations

import sys
import threading
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _cfg(symbol: str = "BTCUSDT", limit: int = 3, **risk):
    r = types.SimpleNamespace(max_concurrent_positions=limit)
    for k, v in risk.items():
        setattr(r, k, v)
    return types.SimpleNamespace(
        market=types.SimpleNamespace(symbol=symbol, short_interval="5m"),
        risk=r,
        backtest=types.SimpleNamespace(
            costs=types.SimpleNamespace(slippage_bps=0.0, fee_bps=0.0),
            initial_equity=10_000.0,
        ),
        runs_dir="runs",
    )


def _engine(symbol: str = "BTCUSDT", limit: int = 3):
    from cryptoagent.engine import TradingEngine

    e = object.__new__(TradingEngine)
    e.lock = threading.RLock()
    e.cfg = _cfg(symbol, limit)
    e.pending_order = None
    e.last_decision = None
    e.last_error = ""
    e.live_client = None
    e.armed = False
    e.state = {
        "mode": "paper",
        "symbol": symbol,
        "exchange": "binance",
        "testnet": True,
        "positions": {},
        "position": None,
        "equity": 10_000.0,
        "peak_equity": 10_000.0,
        "day_start_equity": 10_000.0,
        "day": "2026-11-05",
        "fills": [],
        "position_history": [],
        "consecutive_losses": 0,
        "last_decision": None,
        "pending_stances": [],
    }
    e._last_prices = {}
    e._quote_cache = {}
    e._frame_cache = {}
    e._stop_adjust_at_by_symbol = {}
    e.last_management = {}
    e.last_management_by_symbol = {}
    e._save = lambda: None
    e._log_live = lambda *a, **k: None
    e._notify = lambda *a, **k: None
    e._reconcile_position_with_exchange = lambda: None
    return e


def _order(side: str = "BUY", qty: float = 0.1, lev: float = 5.0,
           stop: float = 95_000.0, tp: float = 110_000.0) -> dict:
    return {
        "side": side, "quantity": qty, "leverage": lev,
        "stop_loss_price": stop, "take_profit_price": tp,
        "entry_ref_price": None,
    }


class TestMultiPosition(unittest.TestCase):
    def test_two_symbols_coexist(self):
        e = _engine()
        e.pending_order = _order()
        self.assertTrue(e.execute(price=100_000.0)["ok"])
        e.cfg.market.symbol = "ETHUSDT"
        e.pending_order = _order(qty=1.0, stop=2_800.0, tp=4_000.0)
        self.assertTrue(e.execute(price=3_000.0)["ok"])
        self.assertEqual(set(e.state["positions"]), {"BTCUSDT", "ETHUSDT"})
        # 兼容视图跟着当前品种走
        self.assertEqual(e.state["position"]["symbol"], "ETHUSDT")

    def test_same_symbol_rejected(self):
        e = _engine()
        e.pending_order = _order()
        self.assertTrue(e.execute(price=100_000.0)["ok"])
        e.pending_order = _order()
        res = e.execute(price=100_000.0)
        self.assertFalse(res["ok"])
        self.assertIn("已有持仓", res["error"])
        self.assertEqual(len(e.state["positions"]), 1)

    def test_position_limit_enforced(self):
        e = _engine(limit=2)
        for sym, px, stop, tp in (
            ("BTCUSDT", 100_000.0, 95_000.0, 110_000.0),
            ("ETHUSDT", 3_000.0, 2_800.0, 4_000.0),
        ):
            e.cfg.market.symbol = sym
            e.pending_order = _order(stop=stop, tp=tp)
            self.assertTrue(e.execute(price=px)["ok"])
        e.cfg.market.symbol = "SOLUSDT"
        e.pending_order = _order(stop=140.0, tp=200.0)
        res = e.execute(price=150.0)
        self.assertFalse(res["ok"])
        self.assertIn("上限 2", res["error"])
        self.assertEqual(len(e.state["positions"]), 2)

    def test_positions_for_risk_notional_has_no_leverage(self):
        """名义 = 数量 × 开仓价；旧实现多乘了一次杠杆，把敞口放大 lev 倍。"""
        e = _engine()
        e._put_position({"symbol": "BTCUSDT", "side": "LONG", "qty": 0.1,
                         "entry": 100_000.0, "leverage": 20.0, "mode": "paper"})
        row = e._positions_for_risk(100_000.0)[0]
        self.assertAlmostEqual(row["notional"], 10_000.0, places=2)
        self.assertAlmostEqual(row["margin"], 500.0, places=2)

    def test_close_only_given_symbol(self):
        e = _engine()
        for sym, px, stop, tp in (
            ("BTCUSDT", 100_000.0, 95_000.0, 110_000.0),
            ("ETHUSDT", 3_000.0, 2_800.0, 4_000.0),
        ):
            e.cfg.market.symbol = sym
            e.pending_order = _order(stop=stop, tp=tp)
            self.assertTrue(e.execute(price=px)["ok"])
        e.cfg.market.symbol = "BTCUSDT"
        res = e.close(price=100_000.0, reason="test", symbol="ETHUSDT")
        self.assertTrue(res["ok"])
        self.assertEqual(set(e.state["positions"]), {"BTCUSDT"})

    def test_stops_use_own_symbol_price(self):
        """两个仓各自用自己品种的价格：不能拿 BTC 的价去比 SOL 的止损。"""
        e = _engine()
        e._put_position({"symbol": "BTCUSDT", "side": "LONG", "qty": 0.1,
                         "entry": 100_000.0, "leverage": 5.0, "stop": 90_000.0,
                         "tp": 120_000.0, "mode": "paper"})
        e._put_position({"symbol": "SOLUSDT", "side": "SHORT", "qty": 10.0,
                         "entry": 150.0, "leverage": 5.0, "stop": 165.0,
                         "tp": 120.0, "mode": "paper"})
        e.cfg.market.symbol = "BTCUSDT"
        e._quote_for = lambda sym, ttl=10.0: 152.0      # SOL 正常价，不该触发
        hits = e.check_stops(price=100_500.0)
        self.assertEqual(hits, [])
        self.assertEqual(len(e.state["positions"]), 2)

    def test_stops_trigger_on_own_price(self):
        e = _engine()
        e._put_position({"symbol": "BTCUSDT", "side": "LONG", "qty": 0.1,
                         "entry": 100_000.0, "leverage": 5.0, "stop": 90_000.0,
                         "tp": 120_000.0, "mode": "paper"})
        e._put_position({"symbol": "SOLUSDT", "side": "SHORT", "qty": 10.0,
                         "entry": 150.0, "leverage": 5.0, "stop": 165.0,
                         "tp": 120.0, "mode": "paper"})
        e.cfg.market.symbol = "BTCUSDT"
        e._quote_for = lambda sym, ttl=10.0: 170.0      # SOL 破止损
        hits = e.check_stops(price=100_500.0)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["symbol"], "SOLUSDT")
        self.assertEqual(set(e.state["positions"]), {"BTCUSDT"})

    def test_legacy_single_position_migrates(self):
        """老存档只有 state["position"]：必须迁进 positions，不能当成空仓。"""
        e = _engine()
        pos = {"symbol": "DOGEUSDT", "side": "LONG", "qty": 100.0, "mode": "paper"}
        e.state["position"] = pos
        self.assertEqual(e._positions_map(), {"DOGEUSDT": pos})

    def test_drop_position_keeps_others(self):
        e = _engine()
        e._put_position({"symbol": "BTCUSDT", "side": "LONG", "qty": 0.1, "mode": "paper"})
        e._put_position({"symbol": "ETHUSDT", "side": "LONG", "qty": 1.0, "mode": "paper"})
        e._drop_position("BTCUSDT")
        self.assertEqual(set(e.state["positions"]), {"ETHUSDT"})
        # 视图自动落到剩下那笔，不能是 None
        self.assertEqual(e.state["position"]["symbol"], "ETHUSDT")


class TestMultiPositionRiskGate(unittest.TestCase):
    """组合层新增的两道闸：并发品种数 + 保证金占用。"""

    @staticmethod
    def _state(equity: float = 10_000.0):
        from cryptoagent.risk import AccountState

        return AccountState(equity=equity, peak_equity=equity, day_start_equity=equity)

    @staticmethod
    def _risk_cfg(**kw):
        from cryptoagent.config import RiskConfig

        c = RiskConfig()
        c.max_leverage = 20.0
        c.min_leverage = 1.0
        c.max_position_pct = 0.8
        c.max_loss_per_trade_pct = 0.05
        c.sizing_mode = "llm"
        c.max_portfolio_notional_pct = 100.0
        c.correlated_exposure_pct = 100.0
        for k, v in kw.items():
            setattr(c, k, v)
        return c

    @staticmethod
    def _prop(size: float = 0.1, lev: float = 5.0, stop: float = 0.02) -> dict:
        return {"action": "long", "size_pct": size, "leverage": lev,
                "confidence": 0.9, "stop_loss_pct": stop, "symbol": "BTCUSDT"}

    def test_concurrent_limit_blocks(self):
        from cryptoagent.risk import RiskEngine

        cfg = self._risk_cfg(max_concurrent_positions=2, max_margin_usage_pct=1.0)
        held = [
            {"symbol": "ETHUSDT", "side": "LONG", "notional": 1000.0, "margin": 200.0},
            {"symbol": "SOLUSDT", "side": "SHORT", "notional": 1000.0, "margin": 200.0},
        ]
        d = RiskEngine(cfg).check(self._prop(), self._state(), 0.01, 0.5,
                                  exchange_max_leverage=20, positions=held)
        self.assertFalse(d.approved)
        self.assertIn("同时持仓已达上限", d.reasons[0])

    def test_margin_cap_blocks(self):
        from cryptoagent.risk import RiskEngine

        # 名义敞口很宽松，但保证金已占 65%，再开 30% 就超 70%
        cfg = self._risk_cfg(max_concurrent_positions=3, max_margin_usage_pct=0.70)
        held = [{"symbol": "ETHUSDT", "side": "LONG", "notional": 20_000.0,
                 "margin": 6_500.0, "leverage": 3.0}]
        d = RiskEngine(cfg).check(self._prop(size=0.30, lev=5.0), self._state(), 0.01, 0.5,
                                  exchange_max_leverage=20, positions=held)
        self.assertFalse(d.approved)
        self.assertIn("保证金占用", d.reasons[0])

    def test_margin_ok_passes(self):
        from cryptoagent.risk import RiskEngine

        cfg = self._risk_cfg(max_concurrent_positions=3, max_margin_usage_pct=0.70)
        held = [{"symbol": "ETHUSDT", "side": "LONG", "notional": 20_000.0,
                 "margin": 2_000.0, "leverage": 3.0}]
        d = RiskEngine(cfg).check(self._prop(size=0.10, lev=5.0), self._state(), 0.01, 0.5,
                                  exchange_max_leverage=20, positions=held)
        self.assertTrue(d.approved)


class TestQuoteFetchUnpack(unittest.TestCase):
    """fetch_klines 返回的是 (df, 来源)，不是裸 DataFrame。

    漏了解包时 df 变成 tuple：`df["close"]` 抛 TypeError 被 except 吞掉 → 返回 0。
    后果全部落在「非当前品种」身上：标记价冻结在旧值（重启后直接显示 --）、
    持仓管理员每轮报「取不到最新价」、非活跃品种的止损检查一并失效。
    """

    def test_quote_for_unpacks_tuple(self):
        from unittest import mock

        import pandas as pd

        e = _engine()
        df = pd.DataFrame({"close": [94.0, 95.5]})
        with mock.patch("cryptoagent.engine.fetch_live_price", side_effect=RuntimeError("ticker down")), \
                mock.patch("cryptoagent.engine.fetch_klines", return_value=(df, "network")):
            self.assertAlmostEqual(e._quote_for("SOLUSDT"), 95.5)
        # 顺带写进缓存，之后 status() 的 _mark_price 才有价可读
        self.assertAlmostEqual(e._last_prices["SOLUSDT"], 95.5)

    def test_quote_for_prefers_ticker_over_klines(self):
        """非活跃品种必须走毫秒级 ticker。

        用 K 线最后一根收盘价顶替时，盘缓存有效期是「半个 bar」（5m → 150 秒），
        页面上的标记价会像卡住一样两三分钟才跳一次。
        """
        from unittest import mock

        e = _engine()
        with mock.patch("cryptoagent.engine.fetch_live_price", return_value=(96.2, "binance")), \
                mock.patch("cryptoagent.engine.fetch_klines", side_effect=AssertionError("不该走 K 线")):
            self.assertAlmostEqual(e._quote_for("SOLUSDT"), 96.2)
        self.assertIn("SOLUSDT", e._price_ts)

    def test_frame_for_returns_dataframe_not_tuple(self):
        from unittest import mock

        import pandas as pd

        e = _engine()
        df = pd.DataFrame({"close": [1.0] * 5, "high": [2.0] * 5, "low": [0.5] * 5})
        with mock.patch("cryptoagent.engine.fetch_klines", return_value=(df, "disk-cache")):
            got = e._frame_for("SOLUSDT")
        self.assertIsInstance(got, pd.DataFrame)
        self.assertEqual(len(got), 5)

    def test_quote_for_failure_falls_back_to_zero(self):
        from unittest import mock

        e = _engine()
        with mock.patch("cryptoagent.engine.fetch_live_price", side_effect=RuntimeError("boom")), \
                mock.patch("cryptoagent.engine.fetch_klines", side_effect=RuntimeError("boom")):
            self.assertEqual(e._quote_for("SOLUSDT"), 0.0)

    def test_refresh_position_quotes_only_inactive(self):
        """报价线程只刷「非当前品种」：当前品种由页面实时价喂，不重复请求。"""
        from unittest import mock

        e = _engine(symbol="SOLUSDT")
        e.state["positions"] = {
            "SOLUSDT": {"symbol": "SOLUSDT", "side": "LONG", "qty": 10.0, "entry": 90.0},
            "ETHUSDT": {"symbol": "ETHUSDT", "side": "LONG", "qty": 1.0, "entry": 3000.0},
        }

        def fake(sym, timeout=6, prefer="binance"):
            return ({"ETHUSDT": 3050.0, "SOLUSDT": 99.0}[sym], "binance")

        with mock.patch("cryptoagent.engine.fetch_live_price", side_effect=fake) as m:
            got = e.refresh_position_quotes()
        self.assertEqual(m.call_count, 1)
        self.assertAlmostEqual(got["ETHUSDT"], 3050.0)
        self.assertAlmostEqual(e._last_prices["ETHUSDT"], 3050.0)
        self.assertNotIn("SOLUSDT", e._last_prices)
        self.assertIn("ETHUSDT", e._price_ts)

    def test_quote_guard_thread_alive(self):
        e = _engine()
        e._quote_stop = threading.Event()
        e._quote_thread = None
        e.start_quote_guard(interval_sec=2)
        try:
            self.assertTrue(e._quote_thread.is_alive())
            self.assertEqual(e._quote_interval, 2)
        finally:
            e.stop_quote_guard()
            e._quote_thread.join(timeout=3)

    def test_mark_price_reads_cached_quote_for_inactive_symbol(self):
        """切成别的品种后，原持仓的标记价只能来自 _last_prices。"""
        e = _engine()
        e.cfg.market.symbol = "DOGEUSDT"
        e._last_prices["SOLUSDT"] = 94.69
        self.assertAlmostEqual(e._mark_price("SOLUSDT"), 94.69)
        # 完全没缓存的品种返回 0（status 会转成 None，前端显示 --），绝不拿别家价格硬套
        self.assertEqual(e._mark_price("XRPUSDT"), 0.0)


class TestManagementHeartbeat(unittest.TestCase):
    """心跳行的时间戳来源。

    只认「当前品种」那份的话，一切到别的品种、当前品种又恰好空仓，时间戳就冻住，
    页面上看着像守护线程挂了——实际上它正在管别的仓。
    反过来，已平仓品种的记录也不能当心跳：它留着平仓那轮的 action=close，直接用
    就会显示很久以前的 close，同样像线程停了。
    """

    @staticmethod
    def _with_positions(e, *symbols):
        e.state["positions"] = {s.upper(): {"symbol": s.upper()} for s in symbols}
        return e

    def test_prefers_current_symbol(self):
        e = self._with_positions(_engine(), "SOLUSDT", "ETHUSDT")
        e.last_management_by_symbol = {
            "SOLUSDT": {"ts": 30.0, "symbol": "SOLUSDT", "action": "hold"},
            "ETHUSDT": {"ts": 20.0, "symbol": "ETHUSDT", "action": "hold"},
        }
        self.assertEqual(e._latest_management("ETHUSDT")["symbol"], "ETHUSDT")

    def test_falls_back_to_newest_when_current_has_no_position(self):
        e = self._with_positions(_engine(), "SOLUSDT", "ETHUSDT")
        e.cfg.market.symbol = "BTCUSDT"          # 当前品种没有持仓
        e.last_management_by_symbol = {
            "SOLUSDT": {"ts": 30.0, "symbol": "SOLUSDT", "action": "hold"},
            "ETHUSDT": {"ts": 20.0, "symbol": "ETHUSDT", "action": "hold"},
        }
        got = e._latest_management("BTCUSDT")
        self.assertEqual(got["symbol"], "SOLUSDT")
        self.assertAlmostEqual(got["ts"], 30.0)

    def test_ignores_closed_symbol_stale_close(self):
        """回归：已平仓品种的陈旧 close 不能冒充心跳。"""
        e = self._with_positions(_engine(), "XRPUSDT")
        e.last_management_by_symbol = {
            # ETH 早就平了，记录一直留着，时间戳还是最新的 —— 也不能选它
            "ETHUSDT": {"ts": 9_000.0, "symbol": "ETHUSDT", "action": "close"},
            "XRPUSDT": {"ts": 100.0, "symbol": "XRPUSDT", "action": "hold"},
        }
        got = e._latest_management("ETHUSDT")     # 当前品种恰是已平仓的 ETH
        self.assertEqual(got["symbol"], "XRPUSDT")
        self.assertAlmostEqual(got["ts"], 100.0)

    def test_flat_market_returns_empty(self):
        """完全空仓时没有心跳可报，返回空字典（前端显示「空仓」）。"""
        e = _engine()                              # positions 为空
        e.last_management = {"ts": 5.0}
        e.last_management_by_symbol = {
            "ETHUSDT": {"ts": 9_000.0, "symbol": "ETHUSDT", "action": "close"},
        }
        self.assertEqual(e._latest_management("BTCUSDT"), {})

    def test_open_position_without_record_returns_empty(self):
        """刚开仓还没巡检过：返回空字典，前端显示「等待首轮巡检」。"""
        e = self._with_positions(_engine(), "SOLUSDT")
        e.last_management_by_symbol = {}
        self.assertEqual(e._latest_management("SOLUSDT"), {})


if __name__ == "__main__":
    unittest.main()
