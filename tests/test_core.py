"""核心链路的单元测试（只用标准库 unittest，无需额外依赖）。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from cryptoagent.agents.llm import LLMClient  # noqa: E402
from cryptoagent.agents.pipeline import TradingPipeline, render_markdown  # noqa: E402
from cryptoagent.backtest import factor_to_target, run_backtest  # noqa: E402
from cryptoagent.config import AppConfig, RiskConfig, load_config  # noqa: E402
from cryptoagent.data import split_market, synthetic_ohlcv  # noqa: E402
from cryptoagent.factors.sandbox import (  # noqa: E402
    FactorError,
    check_lookahead,
    run_factor,
    validate_factor_code,
)
from cryptoagent.research import ResearchLoop  # noqa: E402
from cryptoagent.risk import AccountState, RiskEngine  # noqa: E402


def flat_df(n: int = 60, price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 1.0,
            "funding_rate": 0.0,
        },
        index=idx,
    )


class TestSandbox(unittest.TestCase):
    def test_blocks_import(self):
        with self.assertRaises(FactorError):
            validate_factor_code("import os\ndef factor(df):\n    return df['close']\n")

    def test_blocks_dunder(self):
        with self.assertRaises(FactorError):
            validate_factor_code("def factor(df):\n    return df.__class__\n")

    def test_blocks_open(self):
        with self.assertRaises(FactorError):
            validate_factor_code("def factor(df):\n    return open('x')\n")

    def test_requires_factor_function(self):
        with self.assertRaises(FactorError):
            validate_factor_code("x = 1\n")

    def test_runs_valid_factor(self):
        df = flat_df(200)
        values, meta = run_factor(
            'def factor(df):\n    return df["close"].pct_change()\n', df
        )
        self.assertEqual(len(values), len(df))
        self.assertTrue(meta["ok"])

    def test_detects_lookahead(self):
        df = synthetic_ohlcv(400, 1)
        _, meta = run_factor(
            'def factor(df):\n    return df["close"].shift(-5)\n', df
        )
        self.assertTrue(check_lookahead(meta), "使用未来数据的因子必须被识别")

    def test_clean_factor_has_no_lookahead(self):
        df = synthetic_ohlcv(400, 1)
        _, meta = run_factor(
            'def factor(df):\n    return df["close"].rolling(20).mean()\n', df
        )
        self.assertEqual(check_lookahead(meta), [])


class TestBacktest(unittest.TestCase):
    def test_signal_is_applied_next_bar(self):
        df = flat_df(10)
        cfg = AppConfig().backtest
        target = pd.Series(1.0, index=df.index)
        res = run_backtest(df, target, cfg)
        # 第一根不能持仓：pos[0] 必须是 0
        self.assertEqual(res.position.iloc[0], 0.0)
        self.assertEqual(res.position.iloc[1], 1.0)

    def test_costs_reduce_equity_on_flat_price(self):
        df = flat_df(10)
        cfg = AppConfig().backtest
        res = run_backtest(df, pd.Series(1.0, index=df.index), cfg)
        total_bps = (cfg.costs.fee_bps + cfg.costs.slippage_bps) / 10_000
        expected = cfg.initial_equity * (1 - total_bps)
        self.assertAlmostEqual(res.equity.iloc[-1], expected, places=6)

    def test_constant_price_no_signal_keeps_equity(self):
        df = flat_df(10)
        cfg = AppConfig().backtest
        res = run_backtest(df, pd.Series(0.0, index=df.index), cfg)
        self.assertAlmostEqual(res.equity.iloc[-1], cfg.initial_equity, places=6)

    def test_liquidation_detected_with_high_leverage(self):
        idx = pd.date_range("2025-01-01", periods=30, freq="h", tz="UTC")
        close = pd.Series(100.0, index=idx)
        close.iloc[10:] = 50.0  # 腰斩
        df = pd.DataFrame(
            {"open": close, "high": close, "low": close, "close": close,
             "volume": 1.0, "funding_rate": 0.0},
            index=idx,
        )
        cfg = AppConfig().backtest
        cfg.leverage = 10.0
        res = run_backtest(df, pd.Series(1.0, index=idx), cfg)
        self.assertTrue(res.metrics["liquidated"])
        self.assertEqual(res.equity.iloc[-1], 0.0)

    def test_factor_to_target_respects_cap(self):
        s = pd.Series(range(300), dtype=float)
        t = factor_to_target(s, window=50, cap=1.0, deadband=0.0)
        self.assertLessEqual(t.abs().max(), 1.0)


class TestRisk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = RiskConfig(
            max_leverage=3.0,
            max_position_pct=0.30,
            max_loss_per_trade_pct=0.01,
            daily_loss_limit_pct=0.03,
            max_drawdown_pct=0.15,
            kill_switch_file=str(Path(self.tmp.name) / "HALT"),
        )
        self.engine = RiskEngine(self.cfg)
        self.state = AccountState(10_000, 10_000, 10_000)

    def tearDown(self):
        self.tmp.cleanup()

    def test_clamps_leverage_and_size(self):
        d = self.engine.check(
            {"action": "long", "size_pct": 0.9, "leverage": 20, "stop_loss_pct": 0.02,
             "confidence": 0.9},
            self.state,
        )
        self.assertTrue(d.approved)
        self.assertLessEqual(d.leverage, 3.0)
        self.assertLessEqual(d.size_pct, 0.30)
        self.assertTrue(d.reasons)

    def test_rejects_low_confidence(self):
        d = self.engine.check(
            {"action": "long", "size_pct": 0.1, "leverage": 2, "stop_loss_pct": 0.02,
             "confidence": 0.2},
            self.state,
        )
        self.assertFalse(d.approved)

    def test_rejects_without_stop_loss(self):
        d = self.engine.check(
            {"action": "long", "size_pct": 0.1, "leverage": 2, "stop_loss_pct": 0.0,
             "confidence": 0.9},
            self.state,
        )
        self.assertFalse(d.approved)

    def test_daily_loss_circuit_breaker(self):
        state = AccountState(9_600, 10_000, 10_000)  # 当日 -4%
        d = self.engine.check(
            {"action": "long", "size_pct": 0.1, "leverage": 2, "stop_loss_pct": 0.02,
             "confidence": 0.9},
            state,
        )
        self.assertFalse(d.approved)
        self.assertIn("熔断", " ".join(d.reasons))

    def test_drawdown_kills_system(self):
        state = AccountState(8_000, 10_000, 9_800)
        d = self.engine.check(
            {"action": "long", "size_pct": 0.1, "leverage": 2, "stop_loss_pct": 0.02,
             "confidence": 0.9},
            state,
        )
        self.assertFalse(d.approved)

    def test_kill_switch_file(self):
        Path(self.cfg.kill_switch_file).write_text("stop", encoding="utf-8")
        d = self.engine.check(
            {"action": "long", "size_pct": 0.1, "leverage": 2, "stop_loss_pct": 0.02,
             "confidence": 0.9},
            self.state,
        )
        self.assertFalse(d.approved)
        self.assertIn("熔断开关", d.reasons[0])

    def test_per_trade_risk_cap(self):
        d = self.engine.check(
            {"action": "long", "size_pct": 0.30, "leverage": 3, "stop_loss_pct": 0.09,
             "confidence": 0.9},
            self.state,
        )
        self.assertTrue(d.approved)
        self.assertLessEqual(d.size_pct * d.leverage * 0.09, 0.0101)


class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_config(ROOT / "config" / "default.json")
        cls.cfg.market.bars = 1200
        cls.cfg.runs_dir = tempfile.mkdtemp()
        cls.cfg.llm.provider = "mock"
        cls.df = synthetic_ohlcv(1200, 5)

    def test_mock_decision_is_consistent(self):
        pipe = TradingPipeline(self.cfg)
        d = pipe.run(self.df, persist=False)
        self.assertIn("risk_decision", d.__dict__)
        self.assertFalse(d.degraded)
        if d.order:
            o = d.order
            self.assertGreater(o["quantity"], 0)
            if o["position_side"] == "LONG":
                self.assertLess(o["stop_loss_price"], o["entry_ref_price"])
                self.assertGreater(o["take_profit_price"], o["entry_ref_price"])
            else:
                self.assertGreater(o["stop_loss_price"], o["entry_ref_price"])
                self.assertLess(o["take_profit_price"], o["entry_ref_price"])

    def test_markdown_has_all_sections(self):
        d = TradingPipeline(self.cfg).run(self.df, persist=False)
        md = render_markdown(d)
        for section in ["分析师团队", "研究辩论", "交易员提案", "风控", "订单计划"]:
            self.assertIn(section, md)

    def test_llm_missing_key_degrades_gracefully(self):
        cfg = load_config(ROOT / "config" / "default.json")
        cfg.llm.provider = "openai"
        cfg.llm.api_key_env = "DEFINITELY_NOT_SET_12345"
        client = LLMClient(cfg.llm)
        self.assertEqual(client.provider, "mock")
        resp = client.text("s", "u", "fallback")
        self.assertTrue(resp.degraded)
        self.assertEqual(resp.content, "fallback")


class TestResearch(unittest.TestCase):
    def test_loop_produces_report_and_ledger(self):
        cfg = load_config(ROOT / "config" / "default.json")
        cfg.market.bars = 1500
        cfg.research.iterations = 2
        cfg.research.out_dir = tempfile.mkdtemp()
        cfg.research.factor_library = tempfile.mkdtemp()
        df = synthetic_ohlcv(1500, 3)
        report = ResearchLoop(cfg, df, verbose=False).run()
        self.assertEqual(len(report["history"]), 2)
        self.assertTrue((Path(cfg.research.out_dir) / "report.md").exists())
        self.assertTrue((Path(cfg.research.out_dir) / "leaderboard.json").exists())
        json.dumps(report, default=str)  # 必须可序列化


class TestConfig(unittest.TestCase):
    def test_unknown_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text('{"markett": {"bars": 10}}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(p)

    def test_defaults_load(self):
        cfg = load_config(ROOT / "config" / "default.json")
        self.assertEqual(cfg.market.symbol, "BTCUSDT")
        self.assertGreater(cfg.risk.max_leverage, 0)

    def test_split_is_chronological(self):
        df = synthetic_ohlcv(1000, 2)
        tr, va, te = split_market(df, 0.5, 0.2)
        self.assertLess(tr.index[-1], va.index[0])
        self.assertLess(va.index[-1], te.index[0])
        self.assertEqual(len(tr) + len(va) + len(te), len(df))


class TestDataSourceSeparation(unittest.TestCase):
    """合成数据绝不能写进真实缓存文件，否则页面会把假价格当真行情。"""

    def test_synthetic_and_real_cache_are_separate(self):
        from cryptoagent.config import load_config
        from cryptoagent.data import load_market

        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(ROOT / "config" / "default.json")
            cfg.market.bars = 200
            cfg.market.cache_dir = str(Path(tmp) / "cache")
            cfg.market.source = "synthetic"

            df, src = load_market(cfg.market)
            self.assertTrue(src.startswith("synthetic"))
            names = sorted(p.name for p in (Path(tmp) / "cache").glob("*.csv"))
            self.assertEqual(len(names), 1)
            self.assertIn(".synthetic.csv", names[0])
            self.assertFalse(list((Path(tmp) / "cache").glob("*.real.csv")))

            # 再读一次：仍然识别为 synthetic，不会因为「有缓存」就变成真实行情
            df2, src2 = load_market(cfg.market)
            self.assertTrue(src2.startswith("synthetic"))
            self.assertEqual(len(df), len(df2))

    def test_live_price_offline_raises(self):
        from cryptoagent.data import DataError, fetch_live_price
        import cryptoagent.data as data_mod

        orig = data_mod._get

        def boom(*_a, **_k):
            raise OSError("network down")

        data_mod._get = boom
        try:
            with self.assertRaises(DataError):
                fetch_live_price("BTCUSDT")
        finally:
            data_mod._get = orig


class TestBinancePrecision(unittest.TestCase):
    """下单精度：优先用 exchangeInfo 的 tickSize/stepSize，拿不到时退回默认值。"""

    def _client(self, offline: bool = True):
        from cryptoagent.execution import BinanceFuturesClient, ExchangeError

        c = BinanceFuturesClient("k", "s", testnet=True, armed=True)
        if offline:
            def boom(*_a, **_k):
                raise ExchangeError("offline")
            c._request = boom  # type: ignore[method-assign]
        return c

    def test_fallback_when_offline(self):
        c = self._client()
        tick, step, min_notional = c.filters("BTCUSDT")
        self.assertEqual((tick, step), (0.1, 0.001))
        self.assertGreater(min_notional, 0)

    def test_quantize_respects_step(self):
        c = self._client()
        self.assertAlmostEqual(c._quantize(0.15234, 0.001), 0.152)
        self.assertAlmostEqual(c._quantize(1234.5678, 0.1), 1234.5)

    def test_filters_cached(self):
        c = self._client()
        c.filters("BTCUSDT")
        calls = {"n": 0}

        def count(*_a, **_k):
            calls["n"] += 1
            raise RuntimeError("should not be called again")

        c._request = count  # type: ignore[method-assign]
        c.filters("BTCUSDT")
        self.assertEqual(calls["n"], 0)


class TestDotenv(unittest.TestCase):
    def test_env_file_is_loaded_without_overriding(self):
        import os

        from cryptoagent.config import load_dotenv

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".env"
            p.write_text('FOO_TEST_KEY="abc"\n# comment\nBAR_TEST_KEY=2\n', encoding="utf-8")
            os.environ.pop("FOO_TEST_KEY", None)
            os.environ["BAR_TEST_KEY"] = "keep-me"
            loaded = load_dotenv(p)
            self.assertEqual(os.environ["FOO_TEST_KEY"], "abc")
            self.assertEqual(os.environ["BAR_TEST_KEY"], "keep-me")  # shell 优先
            self.assertEqual(loaded["BAR_TEST_KEY"], "2")
            os.environ.pop("FOO_TEST_KEY", None)
            os.environ.pop("BAR_TEST_KEY", None)

    def test_update_dotenv_keeps_other_lines(self):
        """存 API Key 时只改这两行，注释和别的键要留着。"""
        import os

        from cryptoagent.config import update_dotenv

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".env"
            p.write_text(
                "# 顶部注释\nCRYPTOAGENT_LLM_PROVIDER=mock\nBINANCE_API_KEY=old\n",
                encoding="utf-8",
            )
            update_dotenv({"BINANCE_API_KEY": "new-key", "BINANCE_API_SECRET": "new-secret"}, p)
            text = p.read_text(encoding="utf-8")
            self.assertIn("# 顶部注释", text)
            self.assertIn("CRYPTOAGENT_LLM_PROVIDER=mock", text)
            self.assertIn("BINANCE_API_KEY=new-key", text)
            self.assertIn("BINANCE_API_SECRET=new-secret", text)
            self.assertNotIn("old", text)
            self.assertEqual(os.environ["BINANCE_API_KEY"], "new-key")
            os.environ.pop("BINANCE_API_KEY", None)
            os.environ.pop("BINANCE_API_SECRET", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestExchangeEnvs(unittest.TestCase):
    """2026 年币安把期货测试网并进了 Demo Trading，三个环境的 base 不能搞混。"""

    def test_env_base_urls(self):
        from cryptoagent.execution import BinanceFuturesClient

        self.assertEqual(BinanceFuturesClient("k", "s", env="testnet").base, "https://testnet.binancefuture.com")
        self.assertEqual(BinanceFuturesClient("k", "s", env="demo").base, "https://demo-fapi.binance.com")
        self.assertEqual(BinanceFuturesClient("k", "s", env="live").base, "https://fapi.binance.com")
        # demo 也是假钱，不该要主网 ack 文件
        self.assertTrue(BinanceFuturesClient("k", "s", env="demo").testnet)
        self.assertFalse(BinanceFuturesClient("k", "s", env="live").testnet)

    def test_env_unknown_rejected(self):
        from cryptoagent.execution import BinanceFuturesClient, ExchangeError

        with self.assertRaises(ExchangeError):
            BinanceFuturesClient("k", "s", env="nope")

    def test_base_url_override_env(self):
        import os

        from cryptoagent.execution import BinanceFuturesClient

        os.environ["CRYPTOAGENT_FAPI_BASE"] = "http://127.0.0.1:9999"
        try:
            self.assertEqual(BinanceFuturesClient("k", "s", env="demo").base, "http://127.0.0.1:9999")
        finally:
            os.environ.pop("CRYPTOAGENT_FAPI_BASE", None)


class TestLiveFallback(unittest.TestCase):
    """实时价要能在多个交易所之间自动换源——某个源的机房被限时不能整页瘫痪。"""

    def setUp(self):
        import cryptoagent.data as d

        self.d = d
        self.orig = d.LIVE_SOURCES
        d._DEAD_LIVE.clear()

    def tearDown(self):
        self.d.LIVE_SOURCES = self.orig
        self.d._DEAD_LIVE.clear()

    def test_falls_through_to_next_source(self):
        calls = []

        def boom(sym, t):
            calls.append("a")
            raise OSError("blocked")

        def ok(sym, t):
            calls.append("b")
            return 123.0, "b"

        self.d.LIVE_SOURCES = (("a", boom, "host-a"), ("b", ok, "host-b"))
        price, src = self.d.fetch_live_price("BTCUSDT")
        self.assertEqual((price, src), (123.0, "b"))
        self.assertEqual(calls, ["a", "b"])

    def test_failed_source_is_skipped_for_a_while(self):
        calls = []

        def boom(sym, t):
            calls.append("a")
            raise OSError("blocked")

        def ok(sym, t):
            calls.append("b")
            return 9.0, "b"

        self.d.LIVE_SOURCES = (("a", boom, "host-a"), ("b", ok, "host-b"))
        self.d.fetch_live_price("BTCUSDT")
        self.d.fetch_live_price("BTCUSDT")
        # 第二次不该再去撞已经失败的源
        self.assertEqual(calls.count("a"), 1)
        self.assertEqual(calls.count("b"), 2)

    def test_all_dead_raises_with_details(self):
        from cryptoagent.data import DataError

        def boom(sym, t):
            raise OSError("blocked")

        self.d.LIVE_SOURCES = (("a", boom, "host-a"), ("b", boom, "host-b"))
        with self.assertRaises(DataError) as ctx:
            self.d.fetch_live_price("BTCUSDT")
        self.assertIn("a:", str(ctx.exception))
        self.assertIn("b:", str(ctx.exception))


class TestLeverageOverride(unittest.TestCase):
    """杠杆可以人工固定，但必须仍受风控夹紧。"""

    def _state(self):
        from cryptoagent.risk import AccountState

        return AccountState(equity=10000, peak_equity=10000, day_start_equity=10000, consecutive_losses=0)

    def _proposal(self, **kw):
        base = {
            "action": "long",
            "size_pct": 0.10,
            "leverage": 2.0,
            "stop_loss_pct": 0.01,
            "take_profit_pct": 0.02,
            "confidence": 0.8,
        }
        base.update(kw)
        return base

    def test_auto_keeps_decision_leverage(self):
        from cryptoagent.config import RiskConfig
        from cryptoagent.risk import RiskEngine

        d = RiskEngine(RiskConfig()).check(self._proposal(), self._state(), 0.01, 0.5)
        self.assertEqual(d.leverage, 2.0)

    def test_override_wins(self):
        from cryptoagent.config import RiskConfig
        from cryptoagent.risk import RiskEngine

        cfg = RiskConfig(leverage_override=5, max_leverage=5)
        d = RiskEngine(cfg).check(self._proposal(), self._state(), 0.01, 0.5)
        self.assertEqual(d.leverage, 5.0)
        self.assertTrue(any("强制使用" in r for r in d.reasons))

    def test_higher_leverage_does_not_raise_risk(self):
        """杠杆从 2x 调到 5x，单笔最大亏损不变 —— 仓位会被压小来抵消。"""
        from cryptoagent.config import RiskConfig
        from cryptoagent.risk import RiskEngine

        prop = self._proposal(size_pct=0.3, stop_loss_pct=0.03)   # 3% × 0.3 × 2 = 1.8% 已超限
        d2 = RiskEngine(RiskConfig(leverage_override=2, max_leverage=3)).check(prop, self._state(), 0.01, 0.5)
        d5 = RiskEngine(RiskConfig(leverage_override=5, max_leverage=5)).check(prop, self._state(), 0.01, 0.5)
        risk2 = d2.size_pct * d2.leverage * prop["stop_loss_pct"]
        risk5 = d5.size_pct * d5.leverage * prop["stop_loss_pct"]
        self.assertLessEqual(risk2, 0.0101)
        self.assertLessEqual(risk5, 0.0101)
        self.assertLess(d5.size_pct, d2.size_pct)                  # 杠杆高了，仓位被压小


class TestFactorSignal(unittest.TestCase):
    """因子信号：留出集为负的因子要被自动淘汰，不参与复合信号。"""

    def test_negative_holdout_excluded(self):
        import tempfile

        from cryptoagent.data import synthetic_ohlcv
        from cryptoagent.factors.signal import compute_factor_signal

        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp)
            # 好因子（留出集为正）
            (lib / "good.py").write_text(
                "# holdout sharpe: 1.5\n\ndef factor(df):\n    return df['close'] / df['close'].shift(24) - 1.0\n",
                encoding="utf-8",
            )
            # 坏因子（留出集为负，应被淘汰）
            (lib / "bad.py").write_text(
                "# holdout sharpe: -0.8\n\ndef factor(df):\n    return df['close'] / df['close'].shift(6) - 1.0\n",
                encoding="utf-8",
            )
            df = synthetic_ohlcv(800, 7)
            sig = compute_factor_signal(lib, df)
            names = {s["name"]: s for s in sig["signals"]}
            # 负留出集因子在读取阶段就被剔除（不参与信号，也不进入 signals 列表）
            self.assertNotIn("bad", names)
            self.assertGreater(names["good"]["weight"], 0.0)
            # 复合信号是「内置因子 + 正留出集研究因子」的加权合成，落在 [-1, 1]
            self.assertGreaterEqual(sig["composite"], -1.0)
            self.assertLessEqual(sig["composite"], 1.0)

    def test_empty_library_falls_back_to_builtin(self):
        """研究没产出好因子时，回退内置参考因子，闭环不能空转。"""
        import tempfile

        from cryptoagent.data import synthetic_ohlcv
        from cryptoagent.factors.signal import compute_factor_signal

        with tempfile.TemporaryDirectory() as tmp:
            df = synthetic_ohlcv(800, 7)
            sig = compute_factor_signal(tmp, df)
            self.assertTrue(sig["signals"])
            self.assertTrue(all(s["weight"] > 0 for s in sig["signals"]))


class TestFactorOverride(unittest.TestCase):
    """因子信号对交易员提案的作用规则。"""

    def _apply(self, action, size, composite):
        from cryptoagent.agents.pipeline import _apply_factor_signal

        p = {"action": action, "size_pct": size}
        _apply_factor_signal(p, composite)
        return p

    def test_align_amplifies(self):
        p = self._apply("long", 0.10, 0.5)
        self.assertEqual(p["action"], "long")
        self.assertGreater(p["size_pct"], 0.10)

    def test_oppose_shrinks(self):
        p = self._apply("long", 0.10, -0.3)
        self.assertEqual(p["action"], "long")
        self.assertLess(p["size_pct"], 0.10)

    def test_strong_oppose_goes_flat(self):
        p = self._apply("long", 0.10, -0.8)
        self.assertEqual(p["action"], "flat")
        self.assertEqual(p["size_pct"], 0.0)

    def test_no_signal_no_change(self):
        p = self._apply("short", 0.15, 0.0)
        self.assertEqual(p, {"action": "short", "size_pct": 0.15})


class TestExchangeLeverageCap(unittest.TestCase):
    """杠杆上限取「配置上限」与「交易所上限」的较小值。"""

    def _state(self):
        from cryptoagent.risk import AccountState

        return AccountState(equity=10000, peak_equity=10000, day_start_equity=10000, consecutive_losses=0)

    def _proposal(self):
        return {
            "action": "long", "size_pct": 0.10, "leverage": 2.0,
            "stop_loss_pct": 0.01, "take_profit_pct": 0.02, "confidence": 0.8,
        }

    def test_config_ceiling_wins_over_exchange(self):
        from cryptoagent.config import RiskConfig
        from cryptoagent.risk import RiskEngine

        cfg = RiskConfig(leverage_override=50, max_leverage=20)
        d = RiskEngine(cfg).check(self._proposal(), self._state(), 0.01, 0.5, exchange_max_leverage=125)
        self.assertEqual(d.leverage, 20.0)                        # 交易所允许 125，但配置只给 20

    def test_exchange_max_leverage_caps(self):
        from cryptoagent.config import RiskConfig
        from cryptoagent.risk import RiskEngine

        cfg = RiskConfig(leverage_override=125, max_leverage=50)
        d = RiskEngine(cfg).check(self._proposal(), self._state(), 0.01, 0.5, exchange_max_leverage=20)
        self.assertEqual(d.leverage, 20.0)                        # 交易所只支持 20，压到 20

    def test_without_exchange_uses_config(self):
        from cryptoagent.config import RiskConfig
        from cryptoagent.risk import RiskEngine

        cfg = RiskConfig(leverage_override=10, max_leverage=8)
        d = RiskEngine(cfg).check(self._proposal(), self._state(), 0.01, 0.5)
        self.assertEqual(d.leverage, 8.0)                         # 没连交易所，配置即上限


class TestRiskBudgetSizing(unittest.TestCase):
    """风险上限制：仓位按 单笔风险上限/止损 反推，而不是听模型随意给。"""

    def _state(self):
        from cryptoagent.risk import AccountState

        return AccountState(equity=10000, peak_equity=10000, day_start_equity=10000, consecutive_losses=0)

    def _cfg(self, **kw):
        from cryptoagent.config import RiskConfig

        base = dict(
            max_leverage=20, max_position_pct=0.5, max_loss_per_trade_pct=0.05,
            max_portfolio_notional_pct=5.0, correlated_exposure_pct=5.0,
        )
        base.update(kw)
        return RiskConfig(**base)

    def _prop(self, size=0.02, lev=2.0, stop=0.015):
        return {
            "action": "long", "size_pct": size, "leverage": lev,
            "stop_loss_pct": stop, "take_profit_pct": stop * 2, "confidence": 0.8,
        }

    def test_risk_mode_scales_position_up_to_cap(self):
        from cryptoagent.risk import RiskEngine

        cfg = self._cfg(sizing_mode="risk")
        d = RiskEngine(cfg).check(self._prop(), self._state(), 0.01, 0.5, exchange_max_leverage=20)
        self.assertTrue(d.approved)
        self.assertGreater(d.size_pct, 0.02)                       # 比模型给的大
        self.assertAlmostEqual(d.size_pct * d.leverage * 0.015, cfg.max_loss_per_trade_pct, places=3)

    def test_llm_mode_keeps_model_size(self):
        from cryptoagent.risk import RiskEngine

        cfg = self._cfg(sizing_mode="llm")
        d = RiskEngine(cfg).check(self._prop(), self._state(), 0.01, 0.5, exchange_max_leverage=20)
        self.assertAlmostEqual(d.size_pct, 0.02, places=4)         # 不放大

    def test_legacy_max_mode_falls_back_to_llm(self):
        from cryptoagent.risk import RiskEngine

        cfg = self._cfg(sizing_mode="max")
        d = RiskEngine(cfg).check(self._prop(), self._state(), 0.01, 0.5, exchange_max_leverage=20)
        self.assertAlmostEqual(d.size_pct, 0.02, places=4)         # 旧值不再放大仓位

    def test_risk_mode_ignores_model_size(self):
        from cryptoagent.risk import RiskEngine

        cfg = self._cfg(sizing_mode="risk", max_loss_per_trade_pct=0.02)
        d = RiskEngine(cfg).check(self._prop(size=0.40), self._state(), 0.01, 0.5, exchange_max_leverage=20)
        self.assertAlmostEqual(d.size_pct * d.leverage * 0.015, 0.02, places=3)

    def test_min_leverage_floor(self):
        from cryptoagent.risk import RiskEngine

        cfg = self._cfg(sizing_mode="llm", min_leverage=5)
        d = RiskEngine(cfg).check(self._prop(lev=2.0), self._state(), 0.01, 0.5, exchange_max_leverage=20)
        self.assertEqual(d.leverage, 5.0)


class TestPositionReconcile(unittest.TestCase):
    """幽灵仓位核对：本地记着实盘持仓、但交易所已无持仓时应被清理。"""

    def _engine(self, pos, real=None, boom=False):
        import threading
        import types

        from cryptoagent.engine import TradingEngine

        e = object.__new__(TradingEngine)
        e.state = {"position": pos, "exchange": "okx", "testnet": True}
        e.cfg = types.SimpleNamespace(market=types.SimpleNamespace(symbol="DOGEUSDT"))
        e.lock = threading.RLock()
        e.live_client = None
        e.saved = 0

        def _save():
            e.saved += 1

        e._save = _save

        class _Client:
            def positions(self, symbol):
                if boom:
                    raise RuntimeError("api down")
                return real or []

        e.connect = lambda **kw: _Client()
        return e

    def test_live_phantom_cleared(self):
        e = self._engine({"side": "SHORT", "qty": 1, "mode": "live"}, real=[])
        e._reconcile_position_with_exchange()
        self.assertIsNone(e.state["position"])
        self.assertEqual(e.saved, 1)

    def test_paper_position_kept(self):
        pos = {"side": "LONG", "qty": 1, "mode": "paper"}
        e = self._engine(pos)
        e._reconcile_position_with_exchange()
        self.assertEqual(e.state["position"], pos)
        self.assertEqual(e.saved, 0)

    def test_live_real_position_kept(self):
        pos = {"side": "SHORT", "qty": 1, "mode": "live"}
        e = self._engine(pos, real=[{"symbol": "DOGEUSDT", "qty": 1}])
        e._reconcile_position_with_exchange()
        self.assertEqual(e.state["position"], pos)
        self.assertEqual(e.saved, 0)

    def test_exchange_error_keeps_position(self):
        pos = {"side": "SHORT", "qty": 1, "mode": "live"}
        e = self._engine(pos, boom=True)
        e._reconcile_position_with_exchange()
        self.assertEqual(e.state["position"], pos)
        self.assertEqual(e.saved, 0)


class TestCloseBackfill(unittest.TestCase):
    """交易所侧平仓（止损/止盈条件单成交、网页手动平仓）后回读 userTrades 补记流水。"""

    def _engine(self, pos, trades):
        import tempfile
        import threading
        import types

        from cryptoagent.engine import TradingEngine

        e = object.__new__(TradingEngine)
        e.state = {
            "position": pos,
            "exchange": "binance",
            "testnet": True,
            "equity": 10000.0,
            "fills": [],
            "position_history": [],
            "consecutive_losses": 0,
        }
        e.cfg = types.SimpleNamespace(
            market=types.SimpleNamespace(symbol="DOGEUSDT"),
            runs_dir=tempfile.mkdtemp(),
        )
        e.lock = threading.RLock()
        e.saved = 0
        e.notices = []
        e._save = lambda: setattr(e, "saved", e.saved + 1)
        e._notify = lambda *a, **k: e.notices.append(a)

        class _Client:
            def positions(self, symbol):
                return []

            def trades(self, symbol, limit=30):
                return trades

        e.live_client = _Client()
        return e

    def _pos(self, **kw):
        pos = {
            "side": "LONG", "qty": 10, "entry": 0.5, "leverage": 10,
            "stop": 0.45, "tp": 0.6, "mode": "live",
            "opened_at": "2026-11-04 10:00:00", "order_id": "OPEN1",
        }
        pos.update(kw)
        return pos

    @staticmethod
    def _trade(action, qty, price, ts_ms, order_id, fee=0.0, pnl=0.0):
        return {
            "action": action, "side": "LONG", "qty": qty, "price": price,
            "ts_ms": ts_ms, "order_id": order_id, "fee": fee, "pnl": pnl,
            "symbol": "DOGEUSDT",
        }

    def test_backfills_close_fill_and_history(self):
        trades = [
            self._trade("卖出", 10, 0.6, 3000, "CLOSE1", fee=0.03, pnl=1.0),
            self._trade("买入", 10, 0.5, 2000, "OPEN1"),
        ]
        e = self._engine(self._pos(), trades)
        e._reconcile_position_with_exchange()

        self.assertIsNone(e.state["position"])
        fills = [f for f in e.state["fills"] if f["action"] == "CLOSE"]
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0]["qty"], 10.0)
        self.assertAlmostEqual(fills[0]["price"], 0.6)
        self.assertAlmostEqual(fills[0]["pnl"], 0.97)        # 1.0 盈亏 - 0.03 手续费
        self.assertTrue(fills[0]["backfilled"])
        self.assertEqual(e.state["position_history"][0]["reason"], "exchange")
        self.assertAlmostEqual(e.state["equity"], 10000.97, places=4)
        self.assertTrue(e.notices)                           # 补记后发一条通知

    def test_ignores_closes_before_open(self):
        trades = [
            self._trade("卖出", 10, 0.6, 3000, "CLOSE1", pnl=1.1),
            self._trade("买入", 10, 0.5, 2000, "OPEN1"),
            self._trade("卖出", 10, 0.2, 1000, "OLD9", pnl=-3.0),   # 上一笔仓位，不能算进来
        ]
        e = self._engine(self._pos(), trades)
        e._reconcile_position_with_exchange()

        fills = [f for f in e.state["fills"] if f["action"] == "CLOSE"]
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0]["qty"], 10.0)
        self.assertAlmostEqual(fills[0]["price"], 0.6)

    def test_partial_qty_skips_backfill_but_clears_position(self):
        trades = [self._trade("卖出", 4, 0.6, 3000, "CLOSE1", pnl=0.4)]
        e = self._engine(self._pos(opened_at="", order_id=""), trades)
        e._reconcile_position_with_exchange()

        self.assertIsNone(e.state["position"])               # 幽灵仓位照样清掉
        self.assertEqual(e.state["fills"], [])               # 成交量对不上就不记流水

    def test_no_trades_still_clears_position(self):
        e = self._engine(self._pos(), [])
        e._reconcile_position_with_exchange()

        self.assertIsNone(e.state["position"])
        self.assertEqual(e.state["fills"], [])


class TestHoldingPausesDecisions(unittest.TestCase):
    """持仓期间不再跑开仓决策：不烧 LLM，也不会每轮刷同一条风控拦截通知。"""

    def _engine(self, pos):
        import threading
        import types

        from cryptoagent.engine import TradingEngine

        e = object.__new__(TradingEngine)
        e.state = {"position": pos}
        e.cfg = types.SimpleNamespace(market=types.SimpleNamespace(symbol="DOGEUSDT"))
        e.lock = threading.RLock()
        e._reconcile_position_with_exchange = lambda: None
        return e

    def test_returns_skipped_when_holding(self):
        from cryptoagent.engine import TradingEngine

        pos = {"side": "LONG", "qty": 1, "mode": "paper"}
        e = self._engine(pos)
        out = TradingEngine.run_decision(e)
        self.assertTrue(out.get("skipped"))
        self.assertEqual(out.get("position"), pos)

    def test_runs_when_flat(self):
        from cryptoagent.engine import TradingEngine

        e = self._engine(None)
        e.df = None
        e.refresh_data = lambda: (_ for _ in ()).throw(RuntimeError("reached-refresh"))
        with self.assertRaises(RuntimeError):      # 没被提前跳过，走到了取行情
            TradingEngine.run_decision(e)


class TestLiveEquitySync(unittest.TestCase):
    """实盘权益与成交回读：风控不能拿内部模拟账本当真实本金。"""

    def _engine(self, mode="live", armed=True, acct=None, trades=None, boom=False):
        import threading
        import types

        from cryptoagent.engine import TradingEngine

        e = object.__new__(TradingEngine)
        e.state = {
            "position": None,
            "exchange": "binance",
            "testnet": True,
            "mode": mode,
            "equity": 10000.0,
            "peak_equity": 10000.0,
            "day_start_equity": 10000.0,
            "consecutive_losses": 0,
            "day": "2026-11-04",
        }
        e.cfg = types.SimpleNamespace(market=types.SimpleNamespace(symbol="SOLUSDT"))
        e.lock = threading.RLock()
        e.armed = armed
        e.saved = 0
        e._save = lambda: setattr(e, "saved", e.saved + 1)

        class _Client:
            def account(self):
                if boom:
                    raise RuntimeError("api down")
                return acct or {"balance": 5000.0, "available": 5000.0, "unrealized_pnl": -3.0}

            def trades(self, symbol=None, limit=30):
                return trades or []

        e.live_client = _Client()
        return e

    def test_paper_mode_ignores_exchange(self):
        self.assertIsNone(self._engine(mode="paper")._live_equity())

    def test_account_state_rebases_to_exchange(self):
        e = self._engine()
        st = e.account_state(price=100.0)
        self.assertAlmostEqual(st.equity, 4997.0)           # 5000 + (-3)
        self.assertAlmostEqual(e.state["equity"], 4997.0)   # 内部账本已对齐
        self.assertAlmostEqual(e.state["peak_equity"], 4997.0)
        self.assertEqual(e.saved, 1)

    def test_exchange_failure_keeps_local_ledger(self):
        e = self._engine(boom=True)
        st = e.account_state(price=100.0)
        self.assertAlmostEqual(st.equity, 10000.0)
        self.assertEqual(e.saved, 0)

    def test_live_fill_aggregates_by_order_id(self):
        rows = [
            {"order_id": "A1", "qty": 1.0, "price": 10.0, "fee": 0.01, "pnl": 0.0},
            {"order_id": "A1", "qty": 3.0, "price": 12.0, "fee": 0.03, "pnl": 0.0},
            {"order_id": "B2", "qty": 9.0, "price": 99.0, "fee": 9.0, "pnl": 9.0},
        ]
        d = self._engine(trades=rows)._live_fill("SOLUSDT", "A1")
        self.assertAlmostEqual(d["qty"], 4.0)
        self.assertAlmostEqual(d["price"], 11.5)            # (10*1 + 12*3) / 4
        self.assertAlmostEqual(d["fee"], 0.04)


class TestPeakOffpeak(unittest.TestCase):
    """DeepSeek 官方峰谷规则：工作日 09-12/14-18 峰时，其余（含周末/节假日）谷时。"""

    def _dt(self, *args):
        from datetime import datetime

        from cryptoagent.agents.llm import _CN_TZ

        return datetime(*args, tzinfo=_CN_TZ)

    def test_windows(self):
        from cryptoagent.agents.llm import is_offpeak, seconds_until_offpeak
        from cryptoagent.config import LLMConfig

        cfg = LLMConfig(peak_holidays=["2026-10-01"])
        # 2026-11-04 是周三
        self.assertFalse(is_offpeak(cfg, self._dt(2026, 11, 4, 9, 0)))
        self.assertFalse(is_offpeak(cfg, self._dt(2026, 11, 4, 11, 59)))
        self.assertTrue(is_offpeak(cfg, self._dt(2026, 11, 4, 12, 0)))
        self.assertTrue(is_offpeak(cfg, self._dt(2026, 11, 4, 13, 59)))
        self.assertFalse(is_offpeak(cfg, self._dt(2026, 11, 4, 14, 0)))
        self.assertTrue(is_offpeak(cfg, self._dt(2026, 11, 4, 18, 0)))
        # 周末全天谷时
        self.assertTrue(is_offpeak(cfg, self._dt(2026, 11, 7, 10, 0)))
        # 配置的节假日按谷时
        self.assertTrue(is_offpeak(cfg, self._dt(2026, 10, 1, 10, 0)))
        # 距离下一个谷时：09:30→12:00 = 9000s；14:30→18:00 = 12600s
        self.assertEqual(seconds_until_offpeak(cfg, self._dt(2026, 11, 4, 9, 30)), 9000)
        self.assertEqual(seconds_until_offpeak(cfg, self._dt(2026, 11, 4, 14, 30)), 12600)
        self.assertEqual(seconds_until_offpeak(cfg, self._dt(2026, 11, 4, 20, 0)), 0)


class TestPositionManager(unittest.TestCase):
    """持仓管理员：移动止损 / 时间止损 / 趋势反转，且异常时一律保守 hold。"""

    def _df(self, closes):
        import numpy as np

        import time as _t

        c = np.asarray(closes, dtype=float)
        idx = pd.date_range("2026-01-01", periods=len(c), freq="1h", tz="UTC")
        return pd.DataFrame(
            {
                "open": c,
                "high": c * 1.001,
                "low": c * 0.999,
                "close": c,
                "volume": np.ones(len(c)) * 1000,
            },
            index=idx,
        )

    def _pos(self, entry=100.0, stop=99.0, side="LONG", bars=20):
        import time as _t

        return {
            "side": side,
            "entry": entry,
            "stop": stop,
            "tp": 104.0 if side == "LONG" else 96.0,
            "qty": 1.0,
            "symbol": "BTCUSDT",
            "opened_epoch": _t.time() - bars * 3600,
            "initial_r_pct": abs(entry - stop) / entry,
        }

    def _flat(self, n=300, last=None):
        import numpy as np

        c = np.ones(n) * 100.0
        if last is not None:
            c[-len(last):] = last
        return c

    def test_trailing_stop_tightens_only_upward(self):
        from cryptoagent.agents.position_manager import manage_position
        from cryptoagent.config import PositionConfig

        df = self._df(self._flat(last=list(np.linspace(100, 103, 30))))
        d = manage_position(self._pos(bars=30), df, 103.0, PositionConfig())
        self.assertEqual(d.action, "tighten")
        self.assertGreater(d.new_stop, 99.0)
        self.assertLess(d.new_stop, 103.0)          # 不能贴到/穿过现价
        self.assertGreaterEqual(d.new_stop, 100.0)  # 至少保本

    def test_trailing_stop_never_widens(self):
        from cryptoagent.agents.position_manager import manage_position
        from cryptoagent.config import PositionConfig

        # 止损已经比 ATR 跟踪价更有利，浮盈又只有 1R → 不该往下改
        df = self._df(self._flat(last=list(np.linspace(100, 101, 30))))
        pos = self._pos(entry=100.0, stop=102.0, bars=30)
        d = manage_position(pos, df, 101.0, PositionConfig(reversal_enabled=False))
        self.assertEqual(d.action, "hold")

    def test_time_stop_closes_stale_position(self):
        import numpy as np

        from cryptoagent.agents.position_manager import manage_position
        from cryptoagent.config import PositionConfig

        df = self._df(self._flat())
        d = manage_position(self._pos(bars=60), df, 100.0,
                            PositionConfig(reversal_enabled=False))
        self.assertEqual(d.action, "close")
        self.assertIn("时间止损", d.reason)

    def test_reversal_closes_before_static_stop(self):
        import numpy as np

        from cryptoagent.agents.position_manager import manage_position
        from cryptoagent.config import PositionConfig

        df = self._df(self._flat(last=list(np.linspace(100, 96, 10))))
        d = manage_position(self._pos(bars=10), df, 96.0, PositionConfig())
        self.assertEqual(d.action, "close")
        self.assertIn("趋势反转", d.reason)

    def test_factor_composite_can_force_exit(self):
        from cryptoagent.agents.position_manager import manage_position
        from cryptoagent.config import PositionConfig

        df = self._df(self._flat())
        d = manage_position(self._pos(bars=10), df, 100.0,
                            PositionConfig(reversal_composite=0.7), composite=-0.9)
        self.assertEqual(d.action, "close")
        self.assertIn("因子信号", d.reason)

    def test_short_side_trailing_moves_stop_down(self):
        import numpy as np

        from cryptoagent.agents.position_manager import manage_position
        from cryptoagent.config import PositionConfig

        df = self._df(self._flat(last=list(np.linspace(100, 97, 30))))
        pos = self._pos(entry=100.0, stop=101.0, side="SHORT", bars=30)
        d = manage_position(pos, df, 97.0, PositionConfig(reversal_enabled=False))
        self.assertEqual(d.action, "tighten")
        self.assertLess(d.new_stop, 101.0)
        self.assertGreater(d.new_stop, 97.0)

    def test_no_signal_and_bad_data_are_safe(self):
        from cryptoagent.agents.position_manager import manage_position
        from cryptoagent.config import PositionConfig

        d = manage_position(self._pos(bars=10), self._df(self._flat()), 100.0,
                            PositionConfig(reversal_enabled=False))
        self.assertEqual(d.action, "hold")
        d2 = manage_position(self._pos(bars=10), self._df([100.0] * 5), 100.0, PositionConfig())
        self.assertEqual(d2.action, "hold")
        d3 = manage_position({}, self._df(self._flat()), 100.0, PositionConfig())
        self.assertEqual(d3.action, "hold")

    def test_disabled_never_acts(self):
        import numpy as np

        from cryptoagent.agents.position_manager import manage_position
        from cryptoagent.config import PositionConfig

        df = self._df(self._flat(last=list(np.linspace(100, 103, 30))))
        d = manage_position(self._pos(bars=30), df, 103.0, PositionConfig(enabled=False))
        self.assertEqual(d.action, "hold")


class TestExecutionCostGate(unittest.TestCase):
    """执行成本闸：净边际为负 / 参与率过高要拒绝，正常单放行。"""

    def _cfg(self, **kw):
        from cryptoagent.config import AppConfig

        cfg = AppConfig()
        cfg.market.interval = "1h"
        for k, v in kw.items():
            setattr(cfg.backtest.costs, k, v)
        return cfg

    def _df(self, bar_quote_volume=None):
        from cryptoagent.data import synthetic_ohlcv

        df = synthetic_ohlcv(300, 7)
        if bar_quote_volume is not None:
            df["quote_volume"] = float(bar_quote_volume)
        return df

    def _proposal(self, **kw):
        p = {"action": "long", "size_pct": 0.10, "leverage": 5.0,
             "take_profit_pct": 0.04, "stop_loss_pct": 0.02}
        p.update(kw)
        return p

    def test_normal_order_passes(self):
        from cryptoagent.costs import evaluate_execution_cost

        r = evaluate_execution_cost(self._proposal(), self._df(), self._cfg(),
                                    price=100.0, equity=10000.0, atr_pct=0.01)
        self.assertTrue(r.approved)
        self.assertEqual(r.size_multiplier, 1.0)
        self.assertGreater(r.net_edge_pct, 0)
        self.assertLess(r.cost_pct, 0.01)

    def test_negative_edge_is_rejected(self):
        from cryptoagent.costs import evaluate_execution_cost

        r = evaluate_execution_cost(self._proposal(take_profit_pct=0.0001),
                                    self._df(), self._cfg(), price=100.0,
                                    equity=10000.0, atr_pct=0.01)
        self.assertFalse(r.approved)
        self.assertEqual(r.size_multiplier, 0.0)
        self.assertIn("净边际", " ".join(r.reasons))

    def test_high_participation_scales_down(self):
        from cryptoagent.costs import evaluate_execution_cost

        # notional = 0.1 × 5 × 10000 = 5000，单根成交额 25000 → 参与率 0.2
        r = evaluate_execution_cost(self._proposal(), self._df(25000), self._cfg(),
                                    price=100.0, equity=10000.0, atr_pct=0.01)
        self.assertTrue(r.approved)
        self.assertLess(r.size_multiplier, 1.0)
        self.assertAlmostEqual(r.participation, 0.2, places=3)

    def test_extreme_participation_is_rejected(self):
        from cryptoagent.costs import evaluate_execution_cost

        r = evaluate_execution_cost(self._proposal(), self._df(1000), self._cfg(),
                                    price=100.0, equity=10000.0, atr_pct=0.01)
        self.assertFalse(r.approved)

    def test_flat_and_disabled_are_noops(self):
        from cryptoagent.costs import evaluate_execution_cost

        r = evaluate_execution_cost(self._proposal(action="flat"), self._df(),
                                    self._cfg(), price=100.0, equity=10000.0)
        self.assertTrue(r.approved)
        r2 = evaluate_execution_cost(self._proposal(take_profit_pct=0.0001), self._df(),
                                     self._cfg(cost_gate_enabled=False), price=100.0,
                                     equity=10000.0)
        self.assertTrue(r2.approved)

    def test_pipeline_attaches_cost_gate(self):
        from cryptoagent.agents.pipeline import TradingPipeline
        from cryptoagent.data import synthetic_ohlcv

        cfg = AppConfig()
        cfg.llm.provider = "mock"
        cfg.market.source = "synthetic"
        d = TradingPipeline(cfg).run(synthetic_ohlcv(600, 7), persist=False)
        self.assertIn("cost_gate", d.proposal)
        self.assertIn("approved", d.proposal["cost_gate"])


class TestDerivativesFactors(unittest.TestCase):
    """C 档：衍生品列对齐 + 基差/OI/多空比因子能参与复合信号。"""

    def _fake_payload(self, df):
        import numpy as np

        from cryptoagent.derivatives import clear_cache

        clear_cache()
        ts = [int(t.timestamp() * 1000) for t in df.index]
        n = len(ts)
        basis = [[ts[i], 0, 0, 0, 0.0002 * np.sin(i / 9.0), 0, 0, 0, 0, 0, 0, 0]
                 for i in range(n)]
        oi = [{"timestamp": ts[i], "sumOpenInterest": str(100000.0 * (1 + i / n))}
              for i in range(n)]
        taker = [{"timestamp": ts[i], "buySellRatio": str(0.9 + 0.2 * np.sin(i / 7.0))}
                 for i in range(n)]
        ls = [{"timestamp": ts[i], "longShortRatio": str(1.1 + 0.3 * np.cos(i / 8.0))}
              for i in range(n)]
        return {
            "basis": (basis, lambda r: r[0], lambda r: r[4]),
            "open_interest": (oi, lambda r: r["timestamp"], lambda r: r["sumOpenInterest"]),
            "taker_ratio": (taker, lambda r: r["timestamp"], lambda r: r["buySellRatio"]),
            "ls_ratio": (ls, lambda r: r["timestamp"], lambda r: r["longShortRatio"]),
        }

    def test_enrich_adds_four_columns_and_factors_participate(self):
        import tempfile
        from unittest.mock import patch

        from cryptoagent.data import synthetic_ohlcv
        from cryptoagent.derivatives import enrich_derivatives
        from cryptoagent.factors.signal import compute_factor_signal

        df = synthetic_ohlcv(800, 7)
        payload = self._fake_payload(df)
        with patch("cryptoagent.derivatives._fetch_all", return_value=payload):
            enriched = enrich_derivatives(df, "BTCUSDT", "1h")
        for col in ("basis", "open_interest", "taker_ratio", "ls_ratio"):
            self.assertIn(col, enriched.columns)
            self.assertGreater(enriched[col].notna().sum(), 700)
        # 原 df 不被改动（返回的是副本）
        self.assertNotIn("basis", df.columns)

        with tempfile.TemporaryDirectory() as lib:
            sig = compute_factor_signal(lib, enriched)
        names = {s["name"] for s in sig["signals"]}
        for expected in ("basis_carry", "oi_momentum", "oi_price_divergence",
                         "taker_long_short", "account_long_short"):
            self.assertIn(expected, names)

    def test_missing_derivatives_degrades_safely(self):
        import tempfile
        from unittest.mock import patch

        from cryptoagent.data import synthetic_ohlcv
        from cryptoagent.derivatives import clear_cache, enrich_derivatives
        from cryptoagent.factors.signal import compute_factor_signal

        df = synthetic_ohlcv(800, 7)
        clear_cache()
        with patch("cryptoagent.derivatives._fetch_all", return_value={}):
            out = enrich_derivatives(df, "BTCUSDT", "1h")
        self.assertNotIn("basis", out.columns)
        with tempfile.TemporaryDirectory() as lib:
            sig = compute_factor_signal(lib, out)
        names = {s["name"] for s in sig["signals"]}
        self.assertNotIn("basis_carry", names)   # 缺列直接不加载，不稀释信号

    def test_fetch_failure_never_raises(self):
        from unittest.mock import patch

        from cryptoagent.data import synthetic_ohlcv
        from cryptoagent.derivatives import clear_cache, enrich_derivatives

        df = synthetic_ohlcv(300, 7)
        clear_cache()
        with patch("cryptoagent.derivatives._fetch_all", side_effect=OSError("network down")):
            out = enrich_derivatives(df, "BTCUSDT", "1h")
        self.assertEqual(list(out.columns), list(df.columns))


class TestAnalystCalibration(unittest.TestCase):
    """分析师权重事后校准：样本不足不启用，命中率改变权重且有上下限。"""

    def test_insufficient_samples_keeps_defaults(self):
        from cryptoagent.agents.calibration import DEFAULT_WEIGHTS, weights_from_accuracy

        stats = {"0": {"hit": 2, "total": 3}, "1": {"hit": 1, "total": 2}}
        w, on = weights_from_accuracy(stats, min_samples=10)
        self.assertFalse(on)
        self.assertEqual(w, list(DEFAULT_WEIGHTS))

    def test_accuracy_changes_weights_within_bounds(self):
        from cryptoagent.agents.calibration import record_accuracy, weights_from_accuracy

        stats = {}
        # 技术分析师 10 次全对，资金费率/情绪 10 次全错
        for _ in range(10):
            record_accuracy(stats, [0.8, -0.8, -0.8], 1.0)
        w, on = weights_from_accuracy(stats, min_samples=10)
        self.assertTrue(on)
        self.assertAlmostEqual(sum(w), 1.0, places=6)
        self.assertTrue(all(0.1 <= x <= 0.7 for x in w))
        self.assertGreater(w[0], 0.5)       # 全对的角色权重上升
        self.assertLess(w[1], 0.3)
        self.assertEqual(stats["0"]["hit"], 10)
        self.assertEqual(stats["1"]["hit"], 0)

    def test_neutral_stance_not_counted(self):
        from cryptoagent.agents.calibration import record_accuracy

        stats = {}
        record_accuracy(stats, [0.0, 0.7, -0.7], -1.0)
        self.assertNotIn("0", stats)
        self.assertEqual(stats["2"]["hit"], 1)

    def test_engine_records_on_close(self):
        import threading
        import types

        from cryptoagent.engine import TradingEngine

        e = object.__new__(TradingEngine)
        e.state = {"position": None, "position_history": []}
        e.cfg = types.SimpleNamespace(market=types.SimpleNamespace(symbol="BTCUSDT"))
        e.lock = threading.RLock()
        pos = {"side": "LONG", "qty": 1.0, "entry": 100.0,
               "analyst_stances": [0.8, 0.1, -0.7]}
        e._record_close(pos, 103.0, 3.0, "test", "paper")
        stats = e.state.get("analyst_accuracy") or {}
        self.assertEqual(stats["0"]["hit"], 1)     # 看多，市场上行 → 对
        self.assertEqual(stats["2"]["hit"], 0)     # 看空，市场上行 → 错
        w, on = e.analyst_weights()
        self.assertEqual(len(w), 3)
        self.assertAlmostEqual(sum(w), 1.0, places=6)

    def test_debate_uses_supplied_weights(self):
        import types

        from cryptoagent.agents.llm import LLMClient
        from cryptoagent.agents.roles import MarketSnapshot, run_debate
        from cryptoagent.config import LLMConfig
        from cryptoagent.data import synthetic_ohlcv

        snap = MarketSnapshot.from_df(synthetic_ohlcv(400, 7), "BTCUSDT", "1h")
        reports = [
            types.SimpleNamespace(role="技术", stance=1.0, summary="多", data_flags=[]),
            types.SimpleNamespace(role="资金费率", stance=-1.0, summary="空", data_flags=[]),
            types.SimpleNamespace(role="情绪", stance=0.0, summary="中", data_flags=[]),
        ]
        llm = LLMClient(LLMConfig(provider="mock"))
        d1 = run_debate(llm, snap, reports, rounds=1)
        d2 = run_debate(llm, snap, reports, rounds=1,
                        weights=[0.1, 0.8, 0.1], calibrated=True)
        self.assertGreater(d1.score, d2.score)     # 把权重从技术挪向资金费率
        self.assertIn("校准", d2.thesis)
