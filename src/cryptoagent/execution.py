"""执行层：模拟撮合 + 币安 U 本位合约客户端。

安全模型：
- `BinanceFuturesClient` 默认指向 **testnet**。
- 所有写操作（下单、改杠杆、撤单）都要经过 `_guard()`：
  必须 `armed=True`；主网还必须额外存在确认文件。
- 任何异常都转成 `ExchangeError`，带上交易所返回的 code/msg，绝不吞掉。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .netutil import ssl_context


class ExchangeError(RuntimeError):
    pass


@dataclass
class OrderResult:
    ok: bool
    order_id: str = ""
    price: float = 0.0
    qty: float = 0.0
    fee: float = 0.0
    raw: dict = field(default_factory=dict)
    error: str = ""


# --------------------------------------------------------------------------
# 模拟撮合
# --------------------------------------------------------------------------
@dataclass
class Fill:
    ts: str
    side: str
    qty: float
    price: float
    fee: float
    order_id: str


@dataclass
class PaperBroker:
    equity: float = 10_000.0
    fee_bps: float = 5.0
    slippage_bps: float = 2.0
    cash: float = field(init=False)
    position_qty: float = 0.0
    avg_entry: float = 0.0
    realized_pnl: float = 0.0
    fills: list[Fill] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cash = self.equity

    def submit(self, order: dict, mark_price: float) -> Fill:
        side = order["side"].upper()
        qty = float(order["quantity"])
        slip = self.slippage_bps / 10_000.0
        price = mark_price * (1 + slip) if side == "BUY" else mark_price * (1 - slip)
        notional = qty * price
        fee = notional * self.fee_bps / 10_000.0

        signed = qty if side == "BUY" else -qty
        if self.position_qty == 0 or (self.position_qty > 0) == (signed > 0):
            total = abs(self.position_qty) + abs(signed)
            self.avg_entry = (
                (self.avg_entry * abs(self.position_qty) + price * abs(signed)) / total
                if total
                else price
            )
            self.position_qty += signed
        else:
            closing = min(abs(signed), abs(self.position_qty))
            direction = 1.0 if self.position_qty > 0 else -1.0
            self.realized_pnl += (price - self.avg_entry) * closing * direction
            self.position_qty += signed
            if abs(signed) > closing:
                self.avg_entry = price
            elif abs(self.position_qty) < 1e-12:
                self.position_qty = 0.0
                self.avg_entry = 0.0
        self.cash -= fee
        fill = Fill(time.strftime("%Y-%m-%d %H:%M:%S"), side, qty, price, fee, f"paper-{len(self.fills)}")
        self.fills.append(fill)
        return fill

    def mark_to_market(self, price: float) -> float:
        unrealized = (price - self.avg_entry) * self.position_qty if self.position_qty else 0.0
        return self.cash + self.realized_pnl + unrealized


# --------------------------------------------------------------------------
# 币安 U 本位合约
# --------------------------------------------------------------------------
class BinanceFuturesClient:
    # 2026 年起币安把期货测试网迁到了 Demo Trading：
    # 老站点 testnet.binancefuture.com 的网页已 301 跳转 demo.binance.com，
    # 但它的 /fapi 接口还在服役（老 Key 仍可用）；新 Key 要在 demo.binance.com 申请，
    # 对应 API 域名是 demo-fapi.binance.com。
    TESTNET = "https://testnet.binancefuture.com"
    DEMO = "https://demo-fapi.binance.com"
    MAINNET = "https://fapi.binance.com"
    ENVS = {"testnet": TESTNET, "demo": DEMO, "live": MAINNET}

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        armed: bool = False,
        timeout: int = 10,
        env: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        # env 优先；没有 env 时按老的 testnet 布尔值推断
        self.env = env or ("testnet" if testnet else "live")
        if self.env not in self.ENVS:
            raise ExchangeError(f"未知环境 {self.env}，只能是 testnet / demo / live")
        self.testnet = self.env != "live"          # 非 live 都算「假钱」，不需要主网确认文件
        self.armed = armed
        self.timeout = timeout
        # CRYPTOAGENT_FAPI_BASE 可整体覆盖（自建反代 / 换网络环境时用）
        self.base = base_url or os.getenv("CRYPTOAGENT_FAPI_BASE") or self.ENVS[self.env]
        self._last_call = 0.0
        # symbol -> (tick_size, step_size, min_notional)，首次下单前拉一次 exchangeInfo
        self._filters: dict[str, tuple[float, float, float]] = {}

    # -- 内部 ---------------------------------------------------------
    def _throttle(self) -> None:
        delta = time.time() - self._last_call
        if delta < 0.05:
            time.sleep(0.05 - delta)
        self._last_call = time.time()

    def _guard(self) -> None:
        if not self.armed:
            raise ExchangeError("客户端未解锁（armed=False），拒绝任何写操作")
        if not self.api_key or not self.api_secret:
            raise ExchangeError("缺少 API Key / Secret")
        if not self.testnet:
            # 主网双闸门之一：磁盘上的确认文件（另一个是 CRYPTOAGENT_I_UNDERSTAND_THE_RISK=yes）
            ack = Path("/.cryptoagent_live_ack")
            if not ack.exists():
                ack = Path(__file__).resolve().parents[2] / ".cryptoagent_live_ack"
            if not ack.exists():
                raise ExchangeError(
                    f"主网交易需要确认文件 {ack}（建个空文件即可），防止误操作"
                )

    def _request(self, method: str, path: str, params: dict | None = None, signed: bool = False) -> dict:
        params = dict(params or {})
        self._throttle()
        headers = {"User-Agent": "QeeLin-Agent/0.2"}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params.setdefault("recvWindow", 5000)
            query = urllib.parse.urlencode(params)
            signature = hmac.new(
                self.api_secret.encode(), query.encode(), hashlib.sha256
            ).hexdigest()
            query = f"{query}&signature={signature}"
            headers["X-MBX-APIKEY"] = self.api_key
        else:
            query = urllib.parse.urlencode(params)
        url = f"{self.base}{path}"
        if query:
            url = f"{url}?{query}"
        req = urllib.request.Request(url, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ssl_context()) as resp:
                body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            code = ""
            try:
                payload = json.loads(detail)
                code = f"[{payload.get('code')}] {payload.get('msg')}"
            except Exception:  # noqa: BLE001
                code = detail[:200]
            raise ExchangeError(f"HTTP {exc.code} {code}") from exc
        except urllib.error.URLError as exc:
            raise ExchangeError(
                f"无法连接 {self.base}（{exc.reason}）。国内网络请设置 HTTPS_PROXY。"
            ) from exc

    # -- 公共接口 -----------------------------------------------------
    def ping(self) -> bool:
        self._request("GET", "/fapi/v1/ping")
        return True

    def server_time(self) -> int:
        return int(self._request("GET", "/fapi/v1/time")["serverTime"])

    def mark_price(self, symbol: str) -> float:
        data = self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        return float(data["markPrice"])

    def account(self) -> dict:
        data = self._request("GET", "/fapi/v2/account", signed=True)
        usdt = next((a for a in data.get("assets", []) if a["asset"] == "USDT"), {})
        return {
            "balance": float(usdt.get("walletBalance", 0.0)),
            "available": float(usdt.get("availableBalance", 0.0)),
            "unrealized_pnl": float(usdt.get("unrealizedProfit", 0.0)),
        }

    def positions(self, symbol: str | None = None) -> list[dict]:
        params = {"symbol": symbol} if symbol else {}
        data = self._request("GET", "/fapi/v2/positionRisk", params, signed=True)
        out = []
        for p in data:
            amt = float(p.get("positionAmt", 0.0))
            if abs(amt) < 1e-12:
                continue
            out.append(
                {
                    "symbol": p["symbol"],
                    "side": "LONG" if amt > 0 else "SHORT",
                    "qty": abs(amt),
                    "entry": float(p["entryPrice"]),
                    "mark": float(p["markPrice"]),
                    "unrealized": float(p["unRealizedProfit"]),
                    "leverage": float(p["leverage"]),
                    "liquidation": float(p.get("liquidationPrice", 0.0)),
                }
            )
        return out

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        self._guard()
        return self._request(
            "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)}, signed=True
        )

    def max_leverage(self, symbol: str) -> int:
        """该品种支持的最大杠杆（U 本位永续，需要签名）。

        leverageBracket 的 brackets 按名义值从小到大排，第一档 initialLeverage 最大。
        """
        data = self._request("GET", "/fapi/v1/leverageBracket", {"symbol": symbol}, signed=True)
        brackets = (data[0].get("brackets", []) if data else []) or []
        if not brackets:
            return 125
        return int(brackets[0].get("initialLeverage", 125))

    def place_market(
        self, symbol: str, side: str, quantity: float, reduce_only: bool = False
    ) -> OrderResult:
        self._guard()
        tick, step, min_notional = self.filters(symbol)
        qty = self._quantize(quantity, step)
        if qty <= 0:
            raise ExchangeError(f"下单数量四舍五入后为 0（原始 {quantity}，step {step}）")
        params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "MARKET",
            "quantity": qty,
            "newClientOrderId": f"ca{int(time.time() * 1000)}",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        raw = self._request("POST", "/fapi/v1/order", params, signed=True)
        return OrderResult(
            ok=True,
            order_id=str(raw.get("orderId", "")),
            price=float(raw.get("avgPrice", 0) or 0),
            qty=float(raw.get("executedQty", qty) or qty),
            raw=raw,
        )

    def place_stop_market(
        self,
        symbol: str,
        side: str,
        trigger_price: float,
        close_position: bool = True,
        order_type: str = "STOP_MARKET",
    ) -> OrderResult:
        """止损/止盈条件单（Algo 服务）。

        2025-12-09 起币安把 USDⓈ-M 的条件单迁到 Algo 服务：
        老的 POST /fapi/v1/order + type=STOP_MARKET 会被拦，返回
        `-4120 Order type not supported for this endpoint`。
        新接口是 POST /fapi/v1/algoOrder，参数名也从 stopPrice 改成 triggerPrice。
        order_type 取 STOP_MARKET（止损）或 TAKE_PROFIT_MARKET（止盈）。
        """
        self._guard()
        tick, _, _ = self.filters(symbol)
        params = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side.upper(),
            "type": order_type,
            "triggerPrice": self._quantize(trigger_price, tick),
            "closePosition": "true" if close_position else "false",
            "workingType": "MARK_PRICE",
            "clientAlgoId": f"cas{int(time.time() * 1000)}",
        }
        raw = self._request("POST", "/fapi/v1/algoOrder", params, signed=True)
        oid = raw.get("algoId") or raw.get("orderId") or ""
        return OrderResult(ok=True, order_id=str(oid), raw=raw)

    def cancel_all(self, symbol: str) -> dict:
        """撤掉普通挂单 + 条件单（条件单在 Algo 服务里，要单独撤）。"""
        self._guard()
        res = self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True)
        try:
            self._request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol}, signed=True)
        except ExchangeError:
            pass  # 没有条件单时会报错，忽略
        return res

    def open_algo_orders(self, symbol: str) -> list[dict]:
        return self._request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True)

    def open_orders(self, symbol: str) -> list[dict]:
        return self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True)

    def trades(self, symbol: str, limit: int = 30) -> list[dict]:
        """账户在交易所的真实成交（不是本地模拟撮合）。"""
        self._guard()
        rows = self._request(
            "GET", "/fapi/v1/userTrades", {"symbol": symbol, "limit": max(1, min(limit, 500))}, signed=True
        )
        out = []
        for t in rows or []:
            side = "LONG" if str(t.get("side", "")).upper() == "BUY" else "SHORT"
            out.append(
                {
                    "id": str(t.get("id", "")),
                    "ts": time.strftime("%m-%d %H:%M:%S", time.localtime(int(t.get("time", 0)) / 1000)),
                    "action": "买入" if side == "LONG" else "卖出",
                    "side": side,
                    "qty": float(t.get("qty", 0.0)),
                    "price": float(t.get("price", 0.0)),
                    "pnl": float(t.get("realizedPnl", 0.0)),
                    "fee": float(t.get("commission", 0.0)),
                    "order_id": str(t.get("orderId", "")),
                }
            )
        out.sort(key=lambda r: r["id"], reverse=True)
        return out[:limit]

    def income(self, start_ms: int, limit: int = 1000) -> list[dict]:
        """资金流水：用于算「当日盈亏」（已实现盈亏 + 手续费 + 资金费）。"""
        self._guard()
        return self._request(
            "GET", "/fapi/v1/income", {"startTime": int(start_ms), "limit": min(limit, 1000)}, signed=True
        )

    @staticmethod
    def _round_qty(qty: float, step: float = 0.001) -> float:
        return float(int(qty / step) * step)

    @staticmethod
    def _round_price(price: float, tick: float = 0.1) -> float:
        return round(round(price / tick) * tick, 8)

    # -- 精度 ---------------------------------------------------------
    def filters(self, symbol: str) -> tuple[float, float, float]:
        """(tickSize, stepSize, minNotional)，来自 /fapi/v1/exchangeInfo 并缓存。

        拿不到就退回 BTC 永续的默认值（0.1 / 0.001 / 5 USDT），保证离线也能跑测试。
        """
        if symbol in self._filters:
            return self._filters[symbol]
        tick, step, min_notional = 0.1, 0.001, 5.0
        try:
            info = self._request("GET", "/fapi/v1/exchangeInfo")
            for sym in info.get("symbols", []):
                if sym.get("symbol") != symbol:
                    continue
                for f in sym.get("filters", []):
                    if f.get("filterType") == "PRICE_FILTER":
                        tick = float(f["tickSize"])
                    elif f.get("filterType") == "LOT_SIZE":
                        step = float(f["stepSize"])
                    elif f.get("filterType") == "MIN_NOTIONAL":
                        min_notional = float(f.get("notional", 5.0))
                break
        except ExchangeError:
            pass
        self._filters[symbol] = (tick, step, min_notional)
        return self._filters[symbol]

    def _quantize(self, value: float, step: float) -> float:
        if step <= 0:
            return value
        return round(int(value / step + 1e-9) * step, 10)