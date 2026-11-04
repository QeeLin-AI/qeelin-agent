"""用币安公开行情镜像拉真实历史 K 线，跑候选因子的 train/valid/test 三段验证。

国内/服务器上都能直连 data-api.binance.vision（不需要 API Key）。
现货 K 线同样返回 quote_volume / trades / taker_base，所以 A、B 档因子都能验证；
只有 funding_rate 填 0（合约专属），C 档衍生品因子需要在服务器上单独验证。

用法：
    python tools/validate_factors.py                     # 只出报告
    python tools/validate_factors.py --bars 8000 --write  # 通过的写入 validated.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cryptoagent.config import AppConfig  # noqa: E402
from cryptoagent.data import _klines_frame  # noqa: E402
from cryptoagent.factors.validate import validate_candidates, write_validated  # noqa: E402

BASE = "https://data-api.binance.vision/api/v3/klines"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"


def fetch(symbol: str, interval: str, total: int) -> list[list]:
    rows: list[list] = []
    end = int(time.time() * 1000)
    while len(rows) < total:
        limit = min(1000, total - len(rows))
        q = urllib.parse.urlencode({
            "symbol": symbol, "interval": interval, "limit": limit, "endTime": end,
        })
        req = urllib.request.Request(f"{BASE}?{q}", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            batch = json.loads(resp.read().decode("utf-8"))
        if not batch:
            break
        rows = batch + rows
        end = int(batch[0][0]) - 1
        if len(batch) < limit:
            break
    return rows[-total:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--bars", type=int, default=6000)
    ap.add_argument("--write", action="store_true", help="把通过验证的因子写入 validated.json")
    ap.add_argument("--include-validated", action="store_true",
                    help="连老内置因子一起跑，用作门槛严格度的对照组")
    args = ap.parse_args()

    rows = fetch(args.symbol, args.interval, args.bars)
    if len(rows) < 1000:
        print(f"数据不足：只拿到 {len(rows)} 根，至少 1000 根才有意义")
        return 1
    df = _klines_frame(rows)
    df["funding_rate"] = 0.0
    print(f"数据：{args.symbol} {args.interval} {len(df)} 根，"
          f"{df.index[0]} → {df.index[-1]}，列 {list(df.columns)}")

    cfg = AppConfig()
    cfg.market.interval = args.interval
    cfg.market.symbol = args.symbol
    results = validate_candidates(df, cfg, include_validated=args.include_validated)

    print(f"\n{'因子':<26}{'IS':>8}{'OOS':>8}{'TEST':>8}{'笔数':>7}  结论")
    print("-" * 88)
    for r in sorted(results, key=lambda x: (-int(x["passed"]), -x["oos"])):
        flag = "PASS" if r["passed"] else "  - "
        print(f'{r["name"]:<26}{r["train"]:>8.2f}{r["oos"]:>8.2f}{r["test"]:>8.2f}'
              f'{r.get("n_trades", 0):>7}  {flag}  {r["reason"]}')
    passed = [r["name"] for r in results if r["passed"]]
    print(f"\n通过 {len(passed)}/{len(results)}：{', '.join(passed) or '（无）'}")

    if args.write:
        p = write_validated(results, meta={
            "symbol": args.symbol, "interval": args.interval, "bars": len(df),
            "source": "data-api.binance.vision", "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        print(f"已写入 {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
