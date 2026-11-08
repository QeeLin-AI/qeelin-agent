"""Web 控制台的集成测试：真起一个 HTTP 服务，用 urllib 打接口。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cryptoagent.agents.pipeline import Decision  # noqa: E402
from cryptoagent.config import load_config  # noqa: E402
from cryptoagent.web.server import Handler, WebApp  # noqa: E402


def _fake_decision(symbol: str = "BTCUSDT") -> Decision:
    return Decision(
        ts="2026-01-01 00:00:00",
        symbol=symbol,
        snapshot={},
        analysts=[],
        debate={},
        proposal={"action": "long"},
        risk_officer={"verdict": "approve"},
        risk_decision={"approved": True, "action": "long", "size_pct": 0.2, "leverage": 2.0},
        order={
            "symbol": symbol,
            "side": "BUY",
            "position_side": "LONG",
            "type": "MARKET",
            "quantity": 0.05,
            "notional_usd": 1500.0,
            "leverage": 2.0,
            "entry_ref_price": 30000.0,
            "stop_loss_price": 29400.0,
            "take_profit_price": 31200.0,
            "risk_usd": 30.0,
            "max_loss_pct_of_equity": 0.006,
            "status": "PLANNED",
        },
    )


class TestWebApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = load_config(ROOT / "config" / "default.json")
        cfg.market.bars = 900
        cfg.market.cache_dir = str(Path(cls.tmp.name) / "cache")
        cfg.market.source = "synthetic"
        cfg.runs_dir = str(Path(cls.tmp.name) / "runs")
        cfg.research.out_dir = str(Path(cls.tmp.name) / "research")
        cfg.research.factor_library = str(Path(cls.tmp.name) / "lib")
        cfg.risk.kill_switch_file = str(Path(cls.tmp.name) / "HALT")
        # 别让测试写到真实的项目根 .env
        os.environ["CRYPTOAGENT_ENV_FILE"] = str(Path(cls.tmp.name) / ".env")
        os.environ["CRYPTOAGENT_CONFIG"] = str(Path(cls.tmp.name) / "default.json")
        cls.cfg = cfg
        cls.app = WebApp(cfg)
        handler = type("H", (Handler,), {"app": cls.app})
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()
        os.environ.pop("CRYPTOAGENT_CONFIG", None)

    # -- helpers ------------------------------------------------------
    def _get(self, path: str):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _post(self, path: str, payload: dict):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    # -- tests --------------------------------------------------------
    def test_index_and_static(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"QeeLin-Agent", body)
        status, _ = self._get("/static/style.css")
        self.assertEqual(status, 200)
        status, _ = self._get("/static/app.js")
        self.assertEqual(status, 200)

    def test_status(self):
        status, body = self._get("/api/status")
        self.assertEqual(status, 200)
        data = json.loads(body)["data"]
        self.assertEqual(data["mode"], "paper")
        self.assertFalse(data["armed"])
        self.assertGreater(data["price"], 0)
        # 实时报价字段必须存在（单测里 live=False，不联网）
        self.assertIn("live", data)
        self.assertFalse(data["live"]["running"])

    def test_live_toggle_offline_safe(self):
        """没网时开关实时报价也不能把服务打挂。"""
        status, body = self._post("/api/live", {"on": False})
        self.assertEqual(status, 200)
        self.assertFalse(body["data"]["running"])

    def test_decision_returns_full_pipeline(self):
        status, body = self._post("/api/decision", {"rounds": 1})
        self.assertEqual(status, 200)
        d = body["data"]
        self.assertEqual(len(d["analysts"]), 3)
        self.assertIn("thesis", d["debate"])
        self.assertIn("approved", d["risk_decision"])
        self.assertIn("order", d)

    def test_execute_and_close_paper(self):
        self.app.engine.last_decision = _fake_decision()
        status, body = self._post("/api/trading/execute", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["data"]["ok"])
        self.assertEqual(body["data"]["mode"], "paper")

        status, body = self._get("/api/status")
        self.assertIsNotNone(json.loads(body)["data"]["position"])

        status, body = self._post("/api/trading/close", {})
        self.assertTrue(body["data"]["ok"])
        status, body = self._get("/api/status")
        self.assertIsNone(json.loads(body)["data"]["position"])

    def test_live_arm_requires_confirmation(self):
        status, body = self._post("/api/trading/arm", {"mode": "live", "confirm": "no", "testnet": True})
        self.assertEqual(status, 403)
        self.assertIn("确认", body["error"])

    def test_live_arm_without_keys_fails(self):
        status, body = self._post(
            "/api/trading/arm",
            {"mode": "live", "confirm": "我确认使用真实资金", "testnet": True},
        )
        self.assertIn(status, (403, 502))
        self.assertFalse(body["ok"])

    def test_arm_testnet_without_keys_fails(self):
        """测试网模式不需要确认文本，但必须有 Key，且失败后不能留在 live 状态。"""
        status, body = self._post("/api/trading/arm", {"mode": "testnet"})
        self.assertIn(status, (403, 502))
        self.assertFalse(body["ok"])
        status, raw = self._get("/api/status")
        data = json.loads(raw)["data"]
        self.assertEqual(data["mode"], "paper")
        self.assertFalse(data["armed"])

    def test_exchange_keys_validation(self):
        status, body = self._post("/api/exchange/keys", {"key": "short", "secret": "alsoshort"})
        self.assertEqual(status, 400)
        self.assertIn("长度", body["error"])

    def test_symbols_and_switch(self):
        status, body = self._get("/api/symbols")
        self.assertEqual(status, 200)
        items = json.loads(body)["data"]
        self.assertGreaterEqual(len(items), 2)
        self.assertTrue(any(x["active"] for x in items))

        # 选一个「非当前活跃」的品种来切换，避免默认品种被运行时改写导致 changed=False
        target = next(x["symbol"] for x in items if not x["active"])
        original = next(x["symbol"] for x in items if x["active"])
        status, body = self._post("/api/symbol", {"symbol": target})
        self.assertEqual(status, 200)
        self.assertTrue(body["data"]["changed"])

        status, raw = self._get("/api/status")
        self.assertEqual(json.loads(raw)["data"]["symbol"], target)

        # 切回去，别影响后面的用例
        self._post("/api/symbol", {"symbol": original})

    def test_symbol_switch_allowed_with_position(self):
        """多品种持仓后不再阻塞切品种：仓位自带 symbol，切走不会失管。"""
        original = json.loads(self._get("/api/status")[1])["data"]["symbol"]
        self.app.engine.last_decision = _fake_decision()
        self._post("/api/trading/execute", {})
        status, body = self._post("/api/symbol", {"symbol": "SOLUSDT"})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["symbol"], "SOLUSDT")
        # 原来那个仓位必须还在（只是不再显示为「当前持仓」）
        st = json.loads(self._get("/api/status")[1])["data"]
        self.assertEqual(len(st["positions"]), 1)
        self.assertEqual(st["positions"][0]["symbol"], original)
        self._post("/api/symbol", {"symbol": original})
        self._post("/api/trading/close", {})

    def test_scan_returns_all_symbols(self):
        status, body = self._post("/api/scan", {"symbols": ["BTCUSDT", "ETHUSDT"]})
        self.assertEqual(status, 200)
        rows = body["data"]["rows"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["ok"] for r in rows))
        self.assertTrue(all("action" in r for r in rows))
        # 结果要能回填
        status, body2 = self._get("/api/scan")
        self.assertEqual(len(json.loads(body2)["data"]["rows"]), 2)

    def test_auto_arm_toggle(self):
        status, raw = self._get("/api/status")
        self.assertIn("auto_arm", json.loads(raw)["data"])

        # 关闭：写进 .env 的应该是 off
        status, body = self._post("/api/trading/auto-arm", {"on": False})
        self.assertEqual(status, 200)
        self.assertFalse(body["data"]["auto_arm"]["enabled"])
        self.assertEqual(body["data"]["saved"], "off")

        # 真钱环境必须拒绝
        status, body = self._post("/api/trading/auto-arm", {"on": True, "env": "live"})
        self.assertEqual(status, 400)
        self.assertIn("手动", body["error"])

        # demo 允许（没有网络时会记录错误，但不能抛 500）
        status, body = self._post("/api/trading/auto-arm", {"on": True, "env": "demo"})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["auto_arm"]["env"], "demo")
        # 收尾：关掉，避免影响别的用例
        self._post("/api/trading/auto-arm", {"on": False})

    def test_klines_endpoint_supports_interval(self):
        """5m/15m/1h 切换走的是实时接口，不能回落到本地单一周期的 CSV。"""
        import pandas as pd

        import cryptoagent.data as d

        seen = {}

        def fake(symbol, interval, limit, prefer=None):
            seen.update(symbol=symbol, interval=interval, limit=limit)
            idx = pd.date_range("2026-11-01 00:00", periods=3, freq="5min", tz="UTC")
            df = pd.DataFrame(
                {"open": [1.0, 2.0, 3.0], "high": [1, 2, 3], "low": [1, 2, 3], "close": [1, 2, 3], "volume": [1, 1, 1]},
                index=idx,
            )
            return df, "fake"

        orig = d.fetch_klines
        d.fetch_klines = fake
        try:
            status, body = self._get("/api/klines?interval=5m&limit=3")
        finally:
            d.fetch_klines = orig

        self.assertEqual(status, 200)
        data = json.loads(body)["data"]
        self.assertEqual(data["interval"], "5m")
        self.assertEqual(len(data["bars"]), 3)
        self.assertEqual(data["source"], "fake")
        self.assertEqual(seen["interval"], "5m")

    def test_klines_endpoint_rejects_unknown_interval(self):
        status, body = self._get("/api/klines?interval=7m&limit=3")
        self.assertEqual(status, 502)
        self.assertIn("不支持的周期", json.loads(body)["error"])

    def test_leverage_override_saved_and_applied(self):
        status, raw = self._get("/api/status")
        risk0 = json.loads(raw)["data"]["risk"]
        baseline_max = risk0["max_leverage"]

        status, body = self._post("/api/risk", {"leverage_override": 5})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["risk"]["leverage_override"], 5)
        # max_leverage 是「未连交易所时的兜底」，不随 leverage_override 抬升
        self.assertEqual(body["data"]["risk"]["max_leverage"], baseline_max)
        self.assertTrue(body["data"]["saved"])

        # 落盘了没有
        saved = json.loads((Path(self.tmp.name) / "default.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["risk"]["leverage_override"], 5)

        # 切回自动
        status, body = self._post("/api/risk", {"leverage_override": 0})
        self.assertEqual(body["data"]["risk"]["leverage_override"], 0)
        self.assertEqual(body["data"]["risk"]["max_leverage"], baseline_max)

    def test_leverage_override_range(self):
        status, body = self._post("/api/risk", {"leverage_override": 999})
        self.assertEqual(status, 400)
        self.assertIn("125", body["error"])

    def test_margin_rule_blocks_oversized_position_cap(self):
        """单笔仓位上限 × 品种数 超过保证金占用上限时必须拒绝，且不留半改状态。"""
        status, raw = self._get("/api/status")
        risk0 = json.loads(raw)["data"]["risk"]
        slots = risk0["max_concurrent_positions"]
        margin = risk0["max_margin_usage_pct"]
        # 顶到刚好超过上限一档
        too_big = (margin / slots) + 0.05
        status, body = self._post("/api/risk", {"max_position_pct": too_big})
        self.assertEqual(status, 400)
        self.assertIn("保证金占用上限", body["error"])

        # 回滚：磁盘和内存都还是原值
        status, raw = self._get("/api/status")
        self.assertAlmostEqual(json.loads(raw)["data"]["risk"]["max_position_pct"],
                               risk0["max_position_pct"], places=6)
        saved = json.loads((Path(self.tmp.name) / "default.json").read_text(encoding="utf-8"))
        self.assertAlmostEqual(saved["risk"]["max_position_pct"], risk0["max_position_pct"], places=6)

    def test_margin_rule_blocks_lowering_margin_cap(self):
        status, raw = self._get("/api/status")
        risk0 = json.loads(raw)["data"]["risk"]
        need = risk0["max_position_pct"] * risk0["max_concurrent_positions"]
        if need <= 0.02:
            self.skipTest("当前配置乘积过小，构造不出越界场景")
        status, body = self._post("/api/risk", {"max_margin_usage_pct": round(need / 2, 4)})
        self.assertEqual(status, 400)
        self.assertIn("保证金占用上限", body["error"])
        status, raw = self._get("/api/status")
        self.assertAlmostEqual(json.loads(raw)["data"]["risk"]["max_margin_usage_pct"],
                               risk0["max_margin_usage_pct"], places=6)

    def test_margin_rule_allows_exact_fit(self):
        """乘积正好等于上限（0.3×3=0.9）是合法的，不能因浮点误差判越界。"""
        status, raw = self._get("/api/status")
        risk0 = json.loads(raw)["data"]["risk"]
        margin = 0.9
        pos = margin / risk0["max_concurrent_positions"]
        status, body = self._post("/api/risk", {"max_margin_usage_pct": margin,
                                                "max_position_pct": pos})
        self.assertEqual(status, 200, body)
        self.assertAlmostEqual(body["data"]["risk"]["max_position_pct"], pos, places=6)

    def test_research_auto_toggle(self):
        status, raw = self._get("/api/status")
        self.assertIn("research_auto", json.loads(raw)["data"])

        # 关闭并落盘（开启会启动后台线程真的跑研究，这里只验证关闭路径）
        status, body = self._post("/api/research/auto", {"on": False})
        self.assertEqual(status, 200)
        self.assertFalse(body["data"]["research_auto"]["enabled"])
        saved = json.loads((Path(self.tmp.name) / "default.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["research"]["auto_interval_sec"], 0)

    def test_killswitch_blocks_decision(self):
        self._post("/api/killswitch", {"on": True})
        status, body = self._post("/api/decision", {})
        self.assertEqual(status, 409)
        self.assertIn("熔断", body["error"])
        self._post("/api/killswitch", {"on": False})
        status, _ = self._post("/api/decision", {})
        self.assertEqual(status, 200)

    def test_research_endpoint_small(self):
        import time

        status, body = self._post("/api/research", {"iterations": 2})
        self.assertEqual(status, 200)
        self.assertTrue(body["data"].get("started"))
        # 研究已改为后台异步，轮询状态直到完成
        deadline = time.time() + 120
        while time.time() < deadline and self.app._research_state.get("running"):
            time.sleep(0.2)
        self.assertFalse(self.app._research_state.get("running"), self.app._research_state.get("error"))
        self.assertIsNotNone(self.app.last_research)
        data = self.app.last_research
        self.assertEqual(len(data["rows"]), 2)
        self.assertIn("accepted", data)

    def test_latest_decision_survives_reload(self):
        """刷新页面 / 重启进程后，决策面板还能回填完整结构（而不是只有 markdown）。"""
        self._post("/api/decision", {"rounds": 1})
        self.app.last_decision = None          # 模拟进程重启
        status, body = self._get("/api/decisions/latest")
        self.assertEqual(status, 200)
        data = json.loads(body)["data"]
        self.assertEqual(len(data["analysts"]), 3)
        self.assertIn("markdown", data)

    def test_skipped_decision_does_not_overwrite_latest(self):
        """本品种有持仓 → run_decision 直接 skipped（不跑 LLM），不能顶掉上次完整决策。"""
        full = {"ts": "2026-01-01 00:00:00", "symbol": "BTCUSDT",
                "analysts": [{"role": "技术面分析师"}], "order": None}
        skipped = {"skipped": True, "reason": "BNBUSDT 已有持仓，等平仓完成后再做开仓决策",
                   "symbol": "BNBUSDT", "position": {"symbol": "BNBUSDT"}, "management": {"action": "hold"}}
        self.app.last_decision = full
        orig = self.app.engine.run_decision
        self.app.engine.run_decision = lambda rounds=2: skipped
        try:
            out = self.app.decision(rounds=1)
        finally:
            self.app.engine.run_decision = orig
        self.assertIs(out, skipped)
        self.assertIs(self.app.last_decision, full)          # 跳过不写缓存
        status, body = self._get("/api/decisions/latest")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"]["symbol"], "BTCUSDT")

    def test_latest_research_rebuilds_rows(self):
        """排行榜从 leaderboard.json 的 history 重建，不能是空表。"""
        self.app._run_research_once(2)
        self.app.last_research = None          # 模拟进程重启
        status, body = self._get("/api/research/latest")
        self.assertEqual(status, 200)
        data = json.loads(body)["data"]
        self.assertEqual(len(data["rows"]), 2)
        self.assertTrue(all("name" in r for r in data["rows"]))

    def test_execute_uses_persisted_plan_after_restart(self):
        """重启后「执行当前决策」仍能用 state 里恢复的订单计划。"""
        from cryptoagent.engine import TradingEngine

        eng = self.app.engine
        with eng.lock:
            eng.state["positions"] = {}
            eng.state["position"] = None
            eng.state["last_decision"] = {
                "ts": "2026-01-01 00:00:00",
                "action": "long",
                "size_pct": 0.2,
                "approved": True,
                "order": _fake_decision().order,
            }
            eng._save()

        fresh = TradingEngine(self.cfg)        # 新进程：只从 engine_state.json 恢复
        self.assertIsNotNone(fresh.pending_order)
        res = fresh.execute()
        self.assertTrue(res["ok"], res)
        self.assertIsNone(fresh.pending_order)  # 用过即作废
        fresh.close(reason="test")


if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestWebAuth(unittest.TestCase):
    """开了 CRYPTOAGENT_WEB_TOKEN 后，没令牌不能读也不能下单。"""

    @classmethod
    def setUpClass(cls):
        import os

        cls.tmp = tempfile.TemporaryDirectory()
        cfg = load_config(ROOT / "config" / "default.json")
        cfg.market.bars = 300
        cfg.market.cache_dir = str(Path(cls.tmp.name) / "cache")
        cfg.market.source = "synthetic"
        cfg.runs_dir = str(Path(cls.tmp.name) / "runs")
        cfg.research.out_dir = str(Path(cls.tmp.name) / "research")
        cfg.research.factor_library = str(Path(cls.tmp.name) / "lib")
        cfg.risk.kill_switch_file = str(Path(cls.tmp.name) / "HALT")
        os.environ["CRYPTOAGENT_WEB_TOKEN"] = "s3cret-token"
        try:
            cls.app = WebApp(cfg)
        finally:
            os.environ.pop("CRYPTOAGENT_WEB_TOKEN", None)
        cls.app.token = "s3cret-token"          # 环境变量改了也不影响已经建好的实例
        handler = type("H", (Handler,), {"app": cls.app})
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def _get(self, path: str, headers: dict | None = None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_no_token_rejected(self):
        self.assertEqual(self._get("/"), 401)
        self.assertEqual(self._get("/api/status"), 401)

    def test_bearer_token_ok(self):
        self.assertEqual(self._get("/api/status", {"Authorization": "Bearer s3cret-token"}), 200)

    def test_query_token_ok_for_api(self):
        self.assertEqual(self._get("/api/status?token=s3cret-token"), 200)

    def test_cookie_token_ok(self):
        self.assertEqual(self._get("/api/status", {"Cookie": "ca_token=s3cret-token"}), 200)

    def test_wrong_token_rejected(self):
        self.assertEqual(self._get("/api/status", {"Authorization": "Bearer nope"}), 401)


class _FakeExchange:
    """假装是币安客户端：只实现总览页要读的那几个接口。"""

    def __init__(self, balance=4999.87, available=4980.0, unrealized=12.5, position=None):
        self._balance = balance
        self._available = available
        self._unrealized = unrealized
        self._position = position or []
        self.calls = 0

    def account(self):
        return {"balance": self._balance, "available": self._available, "unrealized_pnl": self._unrealized}

    def positions(self, symbol=None):
        return self._position

    def trades(self, symbol, limit=30):
        return [
            {
                "id": "2",
                "ts": "11-01 22:50:00",
                "action": "买入",
                "side": "LONG",
                "qty": 0.001,
                "price": 77992.1,
                "pnl": 0.0,
                "fee": -0.031,
            }
        ]

    def open_algo_orders(self, symbol):
        return [{"triggerPrice": "74876.49"}]

    def income(self, start_ms, limit=1000):
        return [
            {"incomeType": "REALIZED_PNL", "income": "5.0"},
            {"incomeType": "COMMISSION", "income": "-0.12"},
            {"incomeType": "TRANSFER", "income": "1000.0"},   # 划转不算盈亏，必须排除
        ]


class _FakeOKXExchange(_FakeExchange):
    """OKX：balance 是不含浮盈的钱包口径，equity 是已含浮盈的账户权益。"""

    def account(self):
        return {
            "balance": 100.0,
            "available": 98.0,
            "unrealized_pnl": 5.0,
            "equity": 105.0,
        }


class TestAccountView(unittest.TestCase):
    """总览页要跟着模式走：模拟看本地账本，解锁后看交易所真实账户。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = load_config(ROOT / "config" / "default.json")
        cfg.market.bars = 300
        cfg.market.cache_dir = str(Path(self.tmp.name) / "cache")
        cfg.market.source = "synthetic"
        cfg.runs_dir = str(Path(self.tmp.name) / "runs")
        cfg.research.out_dir = str(Path(self.tmp.name) / "research")
        cfg.research.factor_library = str(Path(self.tmp.name) / "lib")
        cfg.risk.kill_switch_file = str(Path(self.tmp.name) / "HALT")
        os.environ["CRYPTOAGENT_ENV_FILE"] = str(Path(self.tmp.name) / ".env")
        self.app = WebApp(cfg)
        self.app.engine.refresh_data()

    def tearDown(self):
        self.tmp.cleanup()

    def test_paper_mode_reads_local_ledger(self):
        st = self.app.status()
        self.assertEqual(st["account_view"], "local")
        self.assertEqual(st["equity"], 10000.0)
        self.assertIsNone(st["position"])

    def test_live_mode_reads_exchange(self):
        self.app.engine.armed = True
        self.app.engine.state["mode"] = "live"
        self.app.engine.state["testnet"] = True
        self.app.engine.live_client = _FakeExchange()
        st = self.app.status()
        self.assertEqual(st["account_view"], "exchange")
        self.assertAlmostEqual(st["equity"], 4999.87 + 12.5, places=2)
        self.assertAlmostEqual(st["cash_equity"], 4980.0, places=2)
        self.assertEqual(len(st["fills"]), 1)
        self.assertEqual(st["fills"][0]["action"], "买入")
        # 今日盈亏排除划转：5.0 - 0.12 = 4.88
        self.assertAlmostEqual(st["day_pnl_abs"], 4.88, places=2)
        self.assertEqual(st["account_error"], "")

    def test_exchange_equity_field_not_double_counted(self):
        # OKX 的 eq 已含 upl，总览页权益必须是 105，不能是 100 + 5 + 5
        self.app.engine.armed = True
        self.app.engine.state["mode"] = "live"
        self.app.engine.state["testnet"] = True
        self.app.engine.live_client = _FakeOKXExchange()
        st = self.app.status()
        self.assertEqual(st["account_view"], "exchange")
        self.assertAlmostEqual(st["equity"], 105.0)
        self.assertAlmostEqual(st["wallet_balance"], 100.0)
        self.assertAlmostEqual(st["unrealized_pnl"], 5.0)

    def test_live_mode_exposes_exchange_position(self):
        self.app.engine.armed = True
        self.app.engine.state["mode"] = "live"
        self.app.engine.live_client = _FakeExchange(
            position=[
                {
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "qty": 0.001,
                    "entry": 77992.1,
                    "mark": 78078.0,
                    "unrealized": 0.086,
                    "leverage": 2.0,
                    "liquidation": 0.0,
                }
            ]
        )
        st = self.app.status()
        pos = st["position"]
        self.assertIsNotNone(pos)
        self.assertEqual(pos["side"], "LONG")
        self.assertEqual(pos["mode"], "模拟")     # 假钱环境要标出来
        self.assertIsNotNone(pos["stop"])            # 条件单要能对上止损价

    def test_exchange_failure_is_reported_not_hidden(self):
        self.app.engine.armed = True
        self.app.engine.state["mode"] = "live"

        class Broken(_FakeExchange):
            def account(self):
                raise OSError("network down")

        self.app.engine.live_client = Broken()
        st = self.app.status()
        self.assertIn("network down", st["account_error"])
        # 出错时不能假装是交易所数字
        self.assertEqual(st["equity"], 10000.0)

    def test_switching_back_to_paper_drops_cached_snapshot(self):
        self.app.engine.armed = True
        self.app.engine.state["mode"] = "live"
        self.app.engine.live_client = _FakeExchange()
        self.assertEqual(self.app.status()["account_view"], "exchange")
        self.app.engine.disarm()
        self.app.reset_exchange_view()
        st = self.app.status()
        self.assertEqual(st["account_view"], "local")
        self.assertEqual(st["equity"], 10000.0)


class TestLiveOrderCleanup(unittest.TestCase):
    """实盘下单前必须先撤掉残留条件单，否则 closePosition 的止损单会报 -4130。"""

    def test_cancel_before_open(self):
        calls = []

        class Recorder:
            armed = True

            def __init__(self):
                self.armed = True

            def set_leverage(self, symbol, lev):
                calls.append("set_leverage")

            def cancel_all(self, symbol):
                calls.append("cancel_all")
                raise RuntimeError("没有旧单")          # 没旧单时要能忽略

            def place_market(self, symbol, side, qty, reduce_only=False):
                from cryptoagent.execution import OrderResult

                calls.append(f"market:{side}")
                return OrderResult(ok=True, order_id="1", price=78000.0)

            def place_stop_market(self, symbol, side, trigger_price, close_position=True, order_type="STOP_MARKET"):
                from cryptoagent.execution import OrderResult

                calls.append(order_type)
                return OrderResult(ok=True, order_id="100")

        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(ROOT / "config" / "default.json")
            cfg.market.bars = 300
            cfg.market.cache_dir = str(Path(tmp) / "cache")
            cfg.market.source = "synthetic"
            cfg.runs_dir = str(Path(tmp) / "runs")
            cfg.risk.kill_switch_file = str(Path(tmp) / "HALT")
            os.environ["CRYPTOAGENT_ENV_FILE"] = str(Path(tmp) / ".env")

            from cryptoagent.engine import TradingEngine

            eng = TradingEngine(cfg)
            eng.refresh_data()
            eng.armed = True
            eng.state["mode"] = "live"
            eng.live_client = Recorder()
            eng.last_decision = _fake_decision()

            res = eng.execute(price=78000.0, reason="test")
            self.assertTrue(res["ok"], res)

        # 顺序必须是：先撤单，再市价，再挂止损，最后挂止盈
        self.assertEqual(calls[:2], ["set_leverage", "cancel_all"])
        self.assertIn("market:BUY", calls)
        self.assertIn("STOP_MARKET", calls)
        self.assertIn("TAKE_PROFIT_MARKET", calls)
        self.assertEqual(calls[-1], "TAKE_PROFIT_MARKET")

    def test_stop_loss_failure_closes_position(self):
        """止损单下失败绝不能留裸仓：应立即反向平仓并返回失败。"""
        calls = []

        class StopFailExchange:
            def set_leverage(self, symbol, lev):
                calls.append("set_leverage")

            def cancel_all(self, symbol):
                calls.append("cancel_all")

            def place_market(self, symbol, side, qty, reduce_only=False):
                from cryptoagent.execution import OrderResult

                calls.append(f"market:{side}:reduce={reduce_only}")
                return OrderResult(ok=True, order_id="1", price=78000.0)

            def place_stop_market(self, symbol, side, trigger_price, close_position=True, order_type="STOP_MARKET"):
                from cryptoagent.execution import ExchangeError

                calls.append(order_type)
                if order_type == "STOP_MARKET":
                    raise ExchangeError("[-2021] Order would immediately trigger")
                return OrderResult(ok=True, order_id="100")

        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(ROOT / "config" / "default.json")
            cfg.market.bars = 300
            cfg.market.cache_dir = str(Path(tmp) / "cache")
            cfg.market.source = "synthetic"
            cfg.runs_dir = str(Path(tmp) / "runs")
            cfg.risk.kill_switch_file = str(Path(tmp) / "HALT")
            os.environ["CRYPTOAGENT_ENV_FILE"] = str(Path(tmp) / ".env")

            from cryptoagent.engine import TradingEngine

            eng = TradingEngine(cfg)
            eng.refresh_data()
            eng.armed = True
            eng.state["mode"] = "live"
            eng.live_client = StopFailExchange()
            eng.last_decision = _fake_decision()

            res = eng.execute(price=78000.0, reason="test")
            self.assertFalse(res["ok"])
            self.assertIn("止损单下失败", res["error"])

        # 市价开仓后，止损失败必须触发 reduce_only 反向平仓，且绝不能继续挂止盈
        self.assertIn("market:BUY:reduce=False", calls)
        self.assertIn("market:SELL:reduce=True", calls)
        self.assertIn("STOP_MARKET", calls)
        self.assertNotIn("TAKE_PROFIT_MARKET", calls)
