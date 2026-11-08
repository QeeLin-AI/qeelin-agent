"""交易所账户权益口径：OKX 的 eq 已含浮盈，不能再叠加一次 upl。

回归背景：币安 walletBalance 不含 unrealizedProfit，OKX details.eq
（币种权益）= 余额 + 全仓/逐仓浮动收益，本身已含 upl。旧代码统一用
`balance + unrealized_pnl` 算权益，OKX 有持仓时会把浮盈算两遍，
总览页权益、风控本金、切账户基准、重置基准全部偏高/偏低一个浮盈。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cryptoagent.execution import (  # noqa: E402
    BinanceFuturesClient,
    OKXClient,
    account_equity,
)


class TestAccountEquityHelper(unittest.TestCase):
    """account_equity 是唯一取权益入口：有 equity 用它，没有才回退旧拼法。"""

    def test_binance_style_fallback(self):
        self.assertAlmostEqual(
            account_equity({"balance": 100.0, "unrealized_pnl": 5.0}), 105.0
        )

    def test_okx_style_uses_equity_once(self):
        acct = {"balance": 100.0, "unrealized_pnl": 5.0, "equity": 105.0}
        self.assertAlmostEqual(account_equity(acct), 105.0)
        self.assertAlmostEqual(acct["balance"] + acct["unrealized_pnl"], 105.0)

    def test_equity_field_wins_when_it_disagrees(self):
        # 模拟旧客户端把 eq 塞进 balance 的情形：应以 equity 为准，不双计
        self.assertAlmostEqual(
            account_equity({"balance": 105.0, "unrealized_pnl": 5.0, "equity": 105.0}),
            105.0,
        )

    def test_zero_or_broken_equity_falls_back(self):
        self.assertAlmostEqual(
            account_equity({"balance": 100.0, "unrealized_pnl": 5.0, "equity": 0}), 105.0
        )
        self.assertAlmostEqual(
            account_equity({"balance": 100.0, "unrealized_pnl": 5.0, "equity": "bad"}), 105.0
        )

    def test_bad_payload_returns_zero(self):
        self.assertEqual(account_equity(None), 0.0)
        self.assertEqual(account_equity({}), 0.0)
        self.assertEqual(account_equity({"balance": "x", "unrealized_pnl": "y"}), 0.0)


class _StubRequest:
    """只替换 _request，其余客户端逻辑保持不变。"""

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = []

    def __call__(self, method, path, params=None, signed=False):
        self.calls.append((method, path, signed))
        return self.payload


class TestClientAccountFields(unittest.TestCase):
    def test_okx_account_equal_eq_not_double_counted(self):
        client = OKXClient("k", "s", "p", testnet=True, armed=True)
        client._request = _StubRequest({
            "data": [{
                "totalEq": "105",
                "details": [{
                    "ccy": "USDT", "eq": "105", "cashBal": "100",
                    "upl": "5", "availBal": "98",
                }],
            }]
        })
        acct = client.account()
        self.assertAlmostEqual(acct["equity"], 105.0)
        self.assertAlmostEqual(acct["balance"], 100.0)   # eq − upl，不含浮盈
        self.assertAlmostEqual(acct["unrealized_pnl"], 5.0)
        self.assertAlmostEqual(account_equity(acct), 105.0)
        self.assertAlmostEqual(acct["balance"] + acct["unrealized_pnl"], 105.0)

    def test_okx_account_without_usdt_detail(self):
        client = OKXClient("k", "s", "p", testnet=True, armed=True)
        client._request = _StubRequest({"data": [{"totalEq": "3000", "details": []}]})
        acct = client.account()
        self.assertAlmostEqual(acct["equity"], 3000.0)
        self.assertAlmostEqual(account_equity(acct), 3000.0)

    def test_binance_account_equity_is_wallet_plus_upl(self):
        client = BinanceFuturesClient("k", "s", testnet=True, armed=True)
        client._request = _StubRequest({
            "assets": [{
                "asset": "USDT", "walletBalance": "100",
                "availableBalance": "98", "unrealizedProfit": "-3",
            }]
        })
        acct = client.account()
        self.assertAlmostEqual(acct["balance"], 100.0)
        self.assertAlmostEqual(acct["unrealized_pnl"], -3.0)
        self.assertAlmostEqual(acct["equity"], 97.0)
        self.assertAlmostEqual(account_equity(acct), 97.0)


class TestEngineUsesEquityField(unittest.TestCase):
    def test_live_equity_does_not_add_upl_twice(self):
        import threading

        from cryptoagent.engine import TradingEngine

        e = object.__new__(TradingEngine)
        e.state = {"mode": "live", "equity": 100.0}
        e.lock = threading.RLock()
        e.armed = True

        class _Client:
            def account(self):
                # 旧客户端形式：balance 是 eq 本身。若再叠加 upl 会得到 110。
                return {"balance": 105.0, "unrealized_pnl": 5.0, "equity": 105.0}

        e.live_client = _Client()
        self.assertAlmostEqual(e._live_equity(), 105.0)


if __name__ == "__main__":
    unittest.main()
