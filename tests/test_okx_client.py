"""OKX 客户端与交易所边界的一致性测试。

这些用例对应线上实测暴露过的问题（不是假想场景）：
- 未签名 GET 的参数被丢弃 → filters/max_leverage/mark_price 全部失效；
- 永续 sz/pos 是「张数」，内部按「币数」记账 → 下单量差 ctVal 倍；
- side 必须小写，传 BUY 得到 51000；
- 单向持仓下 posSide="net"，多单被误判成空单；
- fee 负数=扣费，与币安 commission 符号相反；
- 资金费是 bills type=8，不是 6；
- 触发单列表 ordType 只能是 trigger，撤单接口是 cancel-algos；
- "All operations failed" 必须带出 data[0].sCode/sMsg。
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from cryptoagent.execution import ExchangeError, OKXClient, account_equity


BTC_INFO = {
    "instId": "BTC-USDT-SWAP",
    "tickSz": "0.01",
    "lotSz": "0.01",
    "minSz": "0.01",
    "ctVal": "0.01",
    "lever": "100",
}


class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _client() -> OKXClient:
    c = OKXClient("k" * 24, "s" * 24, "pass", testnet=True, armed=True)
    c._inst_cache["BTC-USDT-SWAP"] = {
        "instId": "BTC-USDT-SWAP",
        "tickSz": 0.01,
        "lotSz": 0.01,
        "minSz": 0.01,
        "ctVal": 0.01,
        "lever": 100.0,
    }
    c._pos_mode_cache = "net_mode"
    return c


class TestOKXRequest(unittest.TestCase):
    def test_unsigned_get_keeps_query_params(self):
        c = _client()
        c._inst_cache.clear()
        with mock.patch("cryptoagent.execution.urllib.request.urlopen") as m:
            m.return_value = _Resp({"code": "0", "data": [BTC_INFO]})
            info = c._inst_info("BTCUSDT")
        url = m.call_args[0][0].full_url
        self.assertIn("instType=SWAP", url)
        self.assertIn("instId=BTC-USDT-SWAP", url)
        self.assertAlmostEqual(info["ctVal"], 0.01)
        self.assertAlmostEqual(info["tickSz"], 0.01)

    def test_error_message_surfaces_order_scode(self):
        c = _client()
        payload = {
            "code": "1",
            "msg": "All operations failed",
            "data": [{"ordId": "", "sCode": "51121",
                      "sMsg": "Order quantity must be a multiple of the lot size."}],
        }
        with mock.patch("cryptoagent.execution.urllib.request.urlopen") as m:
            m.return_value = _Resp(payload)
            with self.assertRaises(ExchangeError) as ctx:
                c._request("POST", "/api/v5/trade/order", body={"sz": "0.047"})
        self.assertIn("51121", str(ctx.exception))
        self.assertIn("lot size", str(ctx.exception))

    def test_inst_info_failure_is_not_cached(self):
        c = _client()
        c._inst_cache.clear()
        with mock.patch("cryptoagent.execution.urllib.request.urlopen") as m:
            m.return_value = _Resp({"code": "50014", "msg": "Parameter instType can not be empty."})
            first = c._inst_info("BTCUSDT")
        self.assertNotIn("BTC-USDT-SWAP", c._inst_cache)
        self.assertAlmostEqual(first["ctVal"], 1.0)   # 兜底值，不是币的真实面值


class TestOKXOrderUnits(unittest.TestCase):
    def test_market_order_sends_contracts_and_lowercase_side(self):
        c = _client()
        calls = []

        def fake(method, path, params=None, signed=False, body=None):
            calls.append((method, path, params, body))
            if path == "/api/v5/trade/order" and method == "POST":
                return {"code": "0", "data": [{"ordId": "42", "sCode": "0"}]}
            if path == "/api/v5/trade/order":      # 订单详情回读
                return {"code": "0", "data": [{
                    "ordId": "42", "avgPx": "70000", "accFillSz": "2", "fee": "-0.07",
                }]}
            return {"code": "0", "data": []}

        c._request = fake
        res = c.place_market("BTCUSDT", "BUY", 0.02)
        order = next(b for m_, p, _q, b in calls if p == "/api/v5/trade/order" and b)
        self.assertEqual(order["side"], "buy")
        self.assertEqual(order["sz"], "2")            # 0.02 BTC ÷ ctVal 0.01 = 2 张
        self.assertAlmostEqual(res.qty, 0.02)          # 对外仍回报币数
        self.assertAlmostEqual(res.price, 70000.0)
        self.assertAlmostEqual(res.fee, 0.07)          # 负 fee 取反 → 正=支出

    def test_market_order_rejects_below_min_size(self):
        c = _client()
        with self.assertRaises(ExchangeError) as ctx:
            c.place_market("BTCUSDT", "buy", 0.00001)
        self.assertIn("最小下单量", str(ctx.exception))

    def test_doge_contract_value_is_respected(self):
        c = _client()
        c._inst_cache["DOGE-USDT-SWAP"] = {
            "instId": "DOGE-USDT-SWAP", "tickSz": 0.00001, "lotSz": 0.01,
            "minSz": 0.01, "ctVal": 1000.0, "lever": 50.0,
        }
        seen = {}

        def fake(method, path, params=None, signed=False, body=None):
            if path == "/api/v5/trade/order":
                seen.update(body or {})
                return {"code": "0", "data": [{"ordId": "1"}]}
            return {"code": "0", "data": []}

        c._request = fake
        c.place_market("DOGEUSDT", "buy", 7500.0)      # 7500 DOGE
        self.assertEqual(seen["sz"], "7.5")            # 7500 ÷ 1000 = 7.5 张

    def test_stop_order_uses_tick_precision_and_reduce_only(self):
        c = _client()
        seen = {}
        c.positions = lambda symbol=None: [{"qty": 0.02, "contracts": 2.0}]

        def fake(method, path, params=None, signed=False, body=None):
            if path == "/api/v5/trade/order-algo":
                seen.update(body or {})
                return {"code": "0", "data": [{"algoId": "a1"}]}
            return {"code": "0", "data": []}

        c._request = fake
        c.place_stop_market("BTCUSDT", "SELL", 70123.4567)
        self.assertEqual(seen["side"], "sell")
        self.assertEqual(seen["sz"], "2")
        self.assertEqual(seen["triggerPx"], "70123.46")   # tickSz=0.01
        self.assertEqual(seen["reduceOnly"], "true")
        self.assertEqual(seen["ordType"], "trigger")

    def test_trigger_price_has_no_float_noise(self):
        """实测踩过：75984.19×0.97 取整后是 73704.66000000001，OKX 判 triggerPx error。"""
        from cryptoagent.execution import _fmt_by_step, _round_price

        self.assertEqual(_fmt_by_step(73604.66000000001, 0.01), "73604.66")
        self.assertEqual(_fmt_by_step(_round_price(75984.19 * 0.97, 0.01), 0.01), "73704.66")
        self.assertEqual(_fmt_by_step(0.00001, 0.00001), "0.00001")
        self.assertNotIn("e", _fmt_by_step(0.0000001, 0.00000001))

    def test_long_short_mode_is_rejected_with_clear_error(self):
        c = _client()
        c._pos_mode_cache = "long_short_mode"
        with self.assertRaises(ExchangeError) as ctx:
            c.place_market("BTCUSDT", "buy", 0.02)
        self.assertIn("单向持仓", str(ctx.exception))


class TestOKXReads(unittest.TestCase):
    def test_net_mode_long_position_keeps_side_and_converts_qty(self):
        c = _client()
        c._request = lambda method, path, params=None, signed=False, body=None: {
            "code": "0",
            "data": [{
                "instId": "BTC-USDT-SWAP", "pos": "0.5", "posSide": "net",
                "avgPx": "70000", "markPx": "71000", "upl": "0.05",
                "lever": "5", "liqPx": "0",
            }],
        }
        rows = c.positions("BTCUSDT")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["side"], "LONG")
        self.assertAlmostEqual(rows[0]["qty"], 0.005)        # 0.5 张 × 0.01
        self.assertAlmostEqual(rows[0]["contracts"], 0.5)

    def test_net_mode_short_position(self):
        c = _client()
        c._request = lambda method, path, params=None, signed=False, body=None: {
            "code": "0",
            "data": [{"instId": "BTC-USDT-SWAP", "pos": "-1.2", "posSide": "net",
                      "avgPx": "70000", "markPx": "69000", "upl": "0.1", "lever": "3"}],
        }
        self.assertEqual(c.positions("BTCUSDT")[0]["side"], "SHORT")

    def test_trades_normalises_fee_sign_and_qty(self):
        c = _client()
        c._request = lambda method, path, params=None, signed=False, body=None: {
            "code": "0",
            "data": [{
                "instId": "BTC-USDT-SWAP", "tradeId": "9", "ordId": "o1",
                "ts": "1794147859827", "fillSz": "2", "fillPx": "70000",
                "fee": "-0.1234", "pnl": "1.5", "side": "buy", "posSide": "net",
            }],
        }
        row = c.trades("BTCUSDT", limit=5)[0]
        self.assertAlmostEqual(row["qty"], 0.02)
        self.assertAlmostEqual(row["fee"], 0.1234)     # 正数=支出，与币安 commission 同向
        self.assertEqual(row["side"], "LONG")
        self.assertEqual(row["action"], "买入")

    def test_income_maps_funding_to_type_8_and_keeps_fee_sign(self):
        c = _client()
        c._request = lambda method, path, params=None, signed=False, body=None: {
            "code": "0",
            "data": [
                {"type": "2", "subType": "2", "ts": "1000", "pnl": "1.5", "fee": "-0.2"},
                {"type": "8", "subType": "1", "ts": "2000", "balChg": "-0.05"},
                {"type": "6", "subType": "1", "ts": "3000", "balChg": "500"},   # 保证金划转，不是盈亏
                {"type": "1", "subType": "11", "ts": "4000", "balChg": "1000"},
            ],
        }
        rows = c.income(0)
        by_type: dict[str, list[float]] = {}
        for r in rows:
            by_type.setdefault(r["incomeType"], []).append(r["income"])
        self.assertEqual(by_type["REALIZED_PNL"], [1.5])
        self.assertEqual(by_type["COMMISSION"], [-0.2])        # 手续费是负收入
        self.assertEqual(by_type["FUNDING_FEE"], [-0.05])
        self.assertEqual(sum(abs(x) for x in by_type.get("TRANSFER", [])), 0.0)

    def test_filters_convert_lot_size_to_coins(self):
        c = _client()
        c.mark_price = lambda symbol: 70000.0
        tick, step, min_notional = c.filters("BTCUSDT")
        self.assertAlmostEqual(tick, 0.01)
        self.assertAlmostEqual(step, 0.0001)               # lotSz 0.01 × ctVal 0.01
        self.assertAlmostEqual(min_notional, 0.0001 * 70000.0, places=4)

    def test_max_leverage_reads_instruments(self):
        c = _client()
        self.assertEqual(c.max_leverage("BTCUSDT"), 100)


class TestOKXCancel(unittest.TestCase):
    def test_cancel_all_uses_trigger_and_batch_endpoint(self):
        c = _client()
        gets, posts = [], []

        def fake(method, path, params=None, signed=False, body=None):
            if method == "GET" and path == "/api/v5/trade/orders-pending":
                gets.append(params)
                return {"code": "0", "data": [{"ordId": "o1"}]}
            if method == "GET" and path == "/api/v5/trade/orders-algo-pending":
                gets.append(params)
                return {"code": "0", "data": [{"algoId": "a1"}, {"algoId": "a2"}]}
            posts.append((path, body))
            return {"code": "0", "data": []}

        c._request = fake
        res = c.cancel_all("BTCUSDT")
        algo_get = [p for p in gets if p and "ordType" in p][0]
        self.assertEqual(algo_get["ordType"], "trigger")     # 不是 "1"
        batch = [b for p, b in posts if p == "/api/v5/trade/cancel-algos"]
        self.assertEqual(len(batch), 1)
        self.assertEqual([x["algoId"] for x in batch[0]], ["a1", "a2"])
        self.assertEqual(res["cancelled"], 3)
        self.assertEqual(res["errors"], [])

    def test_open_algo_orders_parses_trigger_rows(self):
        c = _client()
        c._request = lambda method, path, params=None, signed=False, body=None: {
            "code": "0",
            "data": [{"algoId": "a1", "triggerPx": "70000.5", "side": "sell"}],
        }
        rows = c.open_algo_orders("BTCUSDT")
        self.assertEqual(rows[0]["triggerPrice"], 70000.5)


class TestOKXEnvSeparation(unittest.TestCase):
    """模拟盘 / 主网是两套独立 Key，任何一边都不能拿另一边的 Key 顶上。"""

    ENV = {
        "OKX_DEMO_API_KEY": "demo-key-000000000001",
        "OKX_DEMO_API_SECRET": "demo-secret-0000000001",
        "OKX_DEMO_PASSPHRASE": "demo-pass",
        "OKX_API_KEY": "live-key-000000000001",
        "OKX_API_SECRET": "live-secret-0000000001",
        "OKX_PASSPHRASE": "live-pass",
    }

    def test_reads_matching_env_only(self):
        from cryptoagent.execution import read_api_keys

        with mock.patch.dict("os.environ", self.ENV, clear=False):
            self.assertEqual(read_api_keys("okx", True)[0], "demo-key-000000000001")
            self.assertEqual(read_api_keys("okx", False)[0], "live-key-000000000001")

    def test_no_fallback_when_demo_missing(self):
        from cryptoagent.execution import read_api_keys

        only_live = {k: v for k, v in self.ENV.items() if not k.startswith("OKX_DEMO")}
        with mock.patch.dict("os.environ", {**only_live, "OKX_DEMO_API_KEY": "",
                                            "OKX_DEMO_API_SECRET": "", "OKX_DEMO_PASSPHRASE": ""},
                             clear=False):
            key, secret, phrase = read_api_keys("okx", True)
        self.assertEqual((key, secret, phrase), ("", "", ""))

    def test_simulated_header_only_for_demo(self):
        demo = OKXClient("k", "s", "p", env="demo")
        live = OKXClient("k", "s", "p", env="live")
        self.assertEqual(demo._sim_header(), {"x-simulated-trading": "1"})
        self.assertEqual(live._sim_header(), {})
        self.assertTrue(demo.testnet)
        self.assertFalse(live.testnet)
        self.assertEqual(live.base, "https://www.okx.com")


class TestOKXEquity(unittest.TestCase):
    def test_account_equity_not_double_counted(self):
        c = _client()
        c._request = lambda method, path, params=None, signed=False, body=None: {
            "code": "0",
            "data": [{"totalEq": "3000", "details": [
                {"ccy": "USDT", "eq": "3010", "upl": "10", "availBal": "2990"},
            ]}],
        }
        acct = c.account()
        self.assertAlmostEqual(acct["balance"], 3000.0)
        self.assertAlmostEqual(acct["unrealized_pnl"], 10.0)
        self.assertAlmostEqual(account_equity(acct), 3010.0)


if __name__ == "__main__":
    unittest.main()
