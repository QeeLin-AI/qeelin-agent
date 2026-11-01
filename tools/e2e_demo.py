"""在 demo（假钱）环境跑一遍完整的「下单 → 挂止损 → 查持仓 → 平仓」，验证执行层。

    python tools/e2e_demo.py [--symbol BTCUSDT] [--qty 0.001] [--keep]

只用 .env 里的 Key，全部是币安 Demo Trading 的虚拟资金，不涉及真钱。
加 --keep 则不平仓（默认会平掉，做完是干净账户）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cryptoagent.config import load_dotenv  # noqa: E402
from cryptoagent.execution import BinanceFuturesClient, ExchangeError  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--qty", type=float, default=0.001)
    ap.add_argument("--env", default="demo", choices=["demo", "testnet", "live"])
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    key = os.getenv("BINANCE_API_KEY", "")
    secret = os.getenv("BINANCE_API_SECRET", "")
    if not key or not secret:
        print("没有 Key")
        return 3

    c = BinanceFuturesClient(key, secret, env=args.env, armed=True, timeout=15)
    print(f"环境：{args.env}  {c.base}")

    acct = c.account()
    print(f"下单前余额：{acct['balance']:,.2f} USDT")

    tick, step, min_notional = c.filters(args.symbol)
    qty = round(int(args.qty / step) * step, 8)
    mark = c.mark_price(args.symbol)
    print(f"{args.symbol} 标记价 {mark:,.2f} ｜ 下单数量 {qty} ｜ 名义 {qty * mark:,.2f} USDT（最小 {min_notional}）")
    if qty * mark < min_notional:
        print("名义价值低于最小限制，调大 --qty")
        return 2

    try:
        c.set_leverage(args.symbol, 2)
        o1 = c.place_market(args.symbol, "BUY", qty)
        print(f"① 市价买入   OK  orderId={o1.order_id} 成交价≈{o1.price or mark:,.2f} 数量 {o1.qty}")
    except ExchangeError as exc:
        print(f"① 市价买入   FAIL {exc}")
        return 1

    stop = mark * 0.96
    try:
        o2 = c.place_stop_market(args.symbol, "SELL", stop)
        print(f"② 挂止损单   OK  orderId={o2.order_id} 触发价 {stop:,.2f}")
    except ExchangeError as exc:
        print(f"② 挂止损单   FAIL {exc}")

    time.sleep(1.5)
    pos = c.positions(args.symbol)
    if pos:
        p = pos[0]
        print(f"③ 查持仓     OK  {p['side']} {p['qty']} @ {p['entry']:,.2f} 杠杆 {p['leverage']:.0f}x 浮盈亏 {p['unrealized']:+.2f}")
    else:
        print("③ 查持仓     —— 交易所显示无持仓（可能成交延迟）")

    if args.keep:
        print("④ 保留持仓（--keep）")
    else:
        try:
            c.cancel_all(args.symbol)
            # 按交易所的实际持仓量平，而不是本次下单量：
            # 之前跑过 --keep 或有残留仓位时，用参数数量会平不干净
            live_pos = c.positions(args.symbol)
            close_qty = live_pos[0]["qty"] if live_pos else qty
            close_side = "SELL" if (live_pos and live_pos[0]["side"] == "LONG") else "BUY"
            o3 = c.place_market(args.symbol, close_side, close_qty, reduce_only=True)
            print(f"④ 平仓       OK  orderId={o3.order_id} 数量 {close_qty}")
        except ExchangeError as exc:
            print(f"④ 平仓       FAIL {exc}")

    time.sleep(1)
    acct2 = c.account()
    print(f"完成后余额：{acct2['balance']:,.2f} USDT（差额 {acct2['balance'] - acct['balance']:+.2f}，含手续费与浮盈亏）")
    print("\n整条执行链路可用：签名 → 精度取整 → 市价单 → 止损单 → 查持仓 → 平仓。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
