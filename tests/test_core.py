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
            self.assertEqual(names["bad"]["weight"], 0.0)
            self.assertGreater(names["good"]["weight"], 0.0)
            # 复合信号只由好因子贡献
            self.assertAlmostEqual(sig["composite"], names["good"]["target"], places=3)

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
    """杠杆上限以交易所支持为准，代码里的 max_leverage 只是断网兜底。"""

    def _state(self):
        from cryptoagent.risk import AccountState

        return AccountState(equity=10000, peak_equity=10000, day_start_equity=10000, consecutive_losses=0)

    def _proposal(self):
        return {
            "action": "long", "size_pct": 0.10, "leverage": 2.0,
            "stop_loss_pct": 0.01, "take_profit_pct": 0.02, "confidence": 0.8,
        }

    def test_exchange_max_leverage_allows_high_leverage(self):
        from cryptoagent.config import RiskConfig
        from cryptoagent.risk import RiskEngine

        cfg = RiskConfig(leverage_override=50, max_leverage=3)   # 兜底只有 3x
        d = RiskEngine(cfg).check(self._proposal(), self._state(), 0.01, 0.5, exchange_max_leverage=125)
        self.assertEqual(d.leverage, 50.0)                        # 交易所允许 125，50 不夹

    def test_exchange_max_leverage_caps(self):
        from cryptoagent.config import RiskConfig
        from cryptoagent.risk import RiskEngine

        cfg = RiskConfig(leverage_override=125)
        d = RiskEngine(cfg).check(self._proposal(), self._state(), 0.01, 0.5, exchange_max_leverage=20)
        self.assertEqual(d.leverage, 20.0)                        # 交易所只支持 20，压到 20
