"""执行层：模拟撮合 + 币安 U 本位合约 / OKX 永续 客户端。

安全模型：
- `BinanceFuturesClient` 默认指向 **testnet**；`OKXClient` 默认用模拟盘（x-simulated-trading 头）。
- 所有写操作（下单、改杠杆、撤单）都要经过 `_guard()`：
  必须 `armed=True`；主网还必须额外存在确认文件。
- 任何异常都转成 `ExchangeError`，带上交易所返回的 code/msg，绝不吞掉。
- 两个客户端的接口保持一致，引擎/服务端只认 `live_client`，不关心底层是哪家。
"""
from __future__ import annotations

import base64
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

# 两家交易所的 key 在 .env 里的前缀，save_keys 按 exchange 落盘、connect 按 exchange 读取
ENV_KEYS = {
    "binance": ("BINANCE_API_KEY", "BINANCE_API_SECRET", None),
    "okx": ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE"),
}


def to_okx_inst(symbol: str) -> str:
    """把内部符号（BTCUSDT）换算成 OKX 永续合约 instId（BTC-USDT-SWAP）。"""
    s = symbol.upper().strip()
    if s.endswith("USDT"):
        return s[:-4] + "-USDT-SWAP"
    if s.endswith("USDC"):
        return s[:-4] + "-USDC-SWAP"
    return s  # 已经是 instId


def from_okx_inst(inst: str) -> str:
    """OKX instId 反推内部符号（BTC-USDT-SWAP → BTCUSDT），找不到就原样返回。"""
    if inst.endswith("-USDT-SWAP"):
        return inst[: -len("-USDT-SWAP")] + "USDT"
    if inst.endswith("-USDC-SWAP"):
        return inst[: -len("-USDC-SWAP")] + "USDC"
    return inst


def build_client(
    exchange: str,
    api_key: str,
    api_secret: str,
    passphrase: str = "",
    testnet: bool = True,
    env: str | None = None,
    base_url: str | None = None,
) -> "BinanceFuturesClient | OKXClient":
    """工厂：按 exchange 选币安或 OKX 客户端（接口一致）。"""
    if exchange == "okx":
        return OKXClient(api_key, api_secret, passphrase, testnet=testnet, env=env, base_url=base_url)
    return BinanceFuturesClient(api_key, api_secret, testnet=testnet, env=env, base_url=base_url)


def read_api_keys(exchange: str, testnet: bool = True) -> tuple[str, str, str]:
    """读取项目 .env 里的 API Key。

    币安 / OKX 的「模拟盘」与「主网」是**两套独立 Key**，四套互不通用、互不回退：
    - 币安模拟盘：BINANCE_DEMO_API_KEY / BINANCE_DEMO_API_SECRET
    - 币安主网  ：BINANCE_API_KEY      / BINANCE_API_SECRET
    - OKX 模拟盘：OKX_DEMO_API_KEY     / OKX_DEMO_API_SECRET / OKX_DEMO_PASSPHRASE
    - OKX 主网  ：OKX_API_KEY          / OKX_API_SECRET      / OKX_PASSPHRASE

    返回 (key, secret, passphrase)，只有 OKX 有 passphrase。
    """
    import os

    ex = (exchange or "binance").lower()
    if ex == "okx":
        if testnet:
            key = os.getenv("OKX_DEMO_API_KEY", "")
            secret = os.getenv("OKX_DEMO_API_SECRET", "")
            passphrase = os.getenv("OKX_DEMO_PASSPHRASE", "")
        else:
            key = os.getenv("OKX_API_KEY", "")
            secret = os.getenv("OKX_API_SECRET", "")
            passphrase = os.getenv("OKX_PASSPHRASE", "")
    else:  # binance
        if testnet:
            key = os.getenv("BINANCE_DEMO_API_KEY", "")
            secret = os.getenv("BINANCE_DEMO_API_SECRET", "")
        else:
            key = os.getenv("BINANCE_API_KEY", "")
            secret = os.getenv("BINANCE_API_SECRET", "")
        passphrase = ""
    return key, secret, passphrase


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
        self.exchange = "binance"
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

    def trades(self, symbol: str | None = None, limit: int = 30) -> list[dict]:
        """账户在交易所的真实成交（不是本地模拟撮合）。

        symbol 传 None 时拉全部品种：用户切了品种后，「最近成交」仍要能看到上一笔交易
        （币安 userTrades 省略 symbol 即返回全品种最近成交）。
        """
        self._guard()
        # 币安 userTrades 默认按时间升序返回：只给 limit 拿到的是「最早」的 N 笔，
        # 最新成交反而被截掉。这里限定最近 3 天并放大条数，取回后按时间倒序截断。
        params: dict = {
            "startTime": int((time.time() - 3 * 86400) * 1000),
            "limit": max(50, min(limit * 10, 500)),
        }
        if symbol:
            params["symbol"] = symbol
        rows = self._request("GET", "/fapi/v1/userTrades", params, signed=True)
        out = []
        for t in rows or []:
            side = "LONG" if str(t.get("side", "")).upper() == "BUY" else "SHORT"
            epoch = int(t.get("time") or 0)
            out.append(
                (
                    epoch,
                    {
                        "id": str(t.get("id", "")),
                        "symbol": t.get("symbol") or symbol or "",
                        "ts": time.strftime("%m-%d %H:%M:%S", time.localtime(epoch / 1000)),
                        # 毫秒时间戳：对账补记平仓时要判断「这笔成交在开仓之后」
                        "ts_ms": epoch,
                        "action": "买入" if side == "LONG" else "卖出",
                        "side": side,
                        "qty": float(t.get("qty") or 0.0),
                        "price": float(t.get("price") or 0.0),
                        "pnl": float(t.get("realizedPnl") or 0.0),
                        "fee": float(t.get("commission") or 0.0),
                        "order_id": str(t.get("orderId", "")),
                    },
                )
            )
        out.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in out[:limit]]

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


# --------------------------------------------------------------------------
# OKX 永续（U 本位，v5 API）
# --------------------------------------------------------------------------
class OKXClient:
    """OKX 永续合约客户端，接口与 BinanceFuturesClient 对齐。

    - 模拟盘：在 live 域名上带 `x-simulated-trading: 1` 头（OKX 没有独立测试网域名）。
    - 主网：去掉该头即可，但同样需要磁盘确认文件 + 环境变量双闸门（见 _guard）。
    - 内部符号统一用 BTCUSDT，这里自动映射成 BTC-USDT-SWAP。
    """

    LIVE = "https://www.okx.com"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str = "",
        testnet: bool = True,
        armed: bool = False,
        timeout: int = 10,
        env: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.exchange = "okx"
        self.env = env or ("testnet" if testnet else "live")
        if self.env not in ("testnet", "demo", "live"):
            raise ExchangeError(f"未知环境 {self.env}，只能是 testnet / demo / live")
        self.testnet = self.env != "live"          # 非 live 都算「假钱」
        self.armed = armed
        self.timeout = timeout
        # CRYPTOAGENT_OKX_BASE 可整体覆盖（自建反代 / 换网络环境时用）
        self.base = (base_url or os.getenv("CRYPTOAGENT_OKX_BASE") or self.LIVE).rstrip("/")
        self._last_call = 0.0
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
        if not self.api_key or not self.api_secret or not self.passphrase:
            raise ExchangeError("缺少 API Key / Secret / Passphrase")
        if not self.testnet:
            ack = Path("/.cryptoagent_live_ack")
            if not ack.exists():
                ack = Path(__file__).resolve().parents[2] / ".cryptoagent_live_ack"
            if not ack.exists():
                raise ExchangeError(
                    f"主网交易需要确认文件 {ack}（建个空文件即可），防止误操作"
                )

    @staticmethod
    def _iso_ts() -> str:
        # OKX 要求 ISO8601 UTC + 毫秒 + Z，例如 2020-12-08T09:08:57.715Z
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int((time.time() % 1) * 1000):03d}Z"

    def _sim_header(self) -> dict:
        return {"x-simulated-trading": "1"} if self.testnet else {}

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        signed: bool = False,
        body: dict | None = None,
    ) -> dict:
        params = dict(params or {})
        self._throttle()
        headers = {
            "User-Agent": "QeeLin-Agent/0.2",
            "Content-Type": "application/json",
            **self._sim_header(),
        }
        url = f"{self.base}{path}"
        prehash = ""
        data: bytes | None = None
        if signed:
            ts = self._iso_ts()
            if body is not None:
                body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
                prehash = ts + method.upper() + path + body_str
                data = body_str.encode("utf-8")
            else:
                query = urllib.parse.urlencode(params)
                req_path = path
                if query:
                    url = f"{url}?{query}"
                    req_path = f"{path}?{query}"
                prehash = ts + method.upper() + req_path
            sign = base64.b64encode(
                hmac.new(self.api_secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
            ).decode("ascii")
            headers.update(
                {
                    "OK-ACCESS-KEY": self.api_key,
                    "OK-ACCESS-SIGN": sign,
                    "OK-ACCESS-TIMESTAMP": ts,
                    "OK-ACCESS-PASSPHRASE": self.passphrase,
                }
            )
        elif body is not None:
            data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ssl_context()) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise ExchangeError(f"HTTP {exc.code} {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise ExchangeError(
                f"无法连接 {self.base}（{exc.reason}）。国内网络请设置 HTTPS_PROXY。"
            ) from exc
        try:
            payload = json.loads(raw) if raw else {}
        except Exception as exc:  # noqa: BLE001
            raise ExchangeError(f"OKX 返回非 JSON：{raw[:200]}") from exc
        if isinstance(payload, dict) and "code" in payload and str(payload.get("code")) != "0":
            raise ExchangeError(f"OKX 错误 [{payload.get('code')}] {payload.get('msg')}")
        return payload

    @staticmethod
    def _data(payload: dict) -> list:
        return payload.get("data", []) if isinstance(payload, dict) else []

    # -- 公共接口 -----------------------------------------------------
    def ping(self) -> bool:
        self._request("GET", "/api/v5/public/time")
        return True

    def server_time(self) -> int:
        d = self._data(self._request("GET", "/api/v5/public/time"))
        return int(d[0]["ts"]) if d else int(time.time() * 1000)

    def mark_price(self, symbol: str) -> float:
        inst = to_okx_inst(symbol)
        d = self._data(self._request("GET", "/api/v5/public/mark-price", {"instType": "SWAP", "instId": inst}))
        return float(d[0]["markPx"])

    @staticmethod
    def _f(v, default: float = 0.0) -> float:
        """安全转 float：OKX 某些字段（如模拟盘 upl）会返回空串/None。"""
        if v is None or v == "":
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def account(self) -> dict:
        d = self._data(self._request("GET", "/api/v5/account/balance", signed=True))
        details = (d[0].get("details") if d else []) or []
        usdt = next((x for x in details if x.get("ccy") == "USDT"), None)
        if usdt:
            return {
                "balance": self._f(usdt.get("eq")),
                "available": self._f(usdt.get("availBal")),
                "unrealized_pnl": self._f(usdt.get("upl")),
            }
        # 没有 USDT 明细时退回总权益
        total = self._f(d[0].get("totalEq")) if d else 0.0
        return {"balance": total, "available": total, "unrealized_pnl": 0.0}

    def positions(self, symbol: str | None = None) -> list[dict]:
        inst = to_okx_inst(symbol) if symbol else None
        data = self._data(self._request("GET", "/api/v5/account/positions", {"instType": "SWAP"}, signed=True))
        out = []
        for p in data:
            if inst and p.get("instId") != inst:
                continue
            amt = self._f(p.get("pos"))
            if abs(amt) < 1e-8:
                continue
            out.append(
                {
                    "symbol": from_okx_inst(p.get("instId", symbol or "")),
                    "side": "LONG" if p.get("posSide") == "long" else "SHORT",
                    "qty": abs(amt),
                    "entry": self._f(p.get("avgPx")),
                    "mark": self._f(p.get("markPx")),
                    "unrealized": self._f(p.get("upl")),
                    "leverage": self._f(p.get("lever"), 1.0),
                    "liquidation": self._f(p.get("liqPx")),
                }
            )
        return out

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        self._guard()
        return self._request(
            "POST",
            "/api/v5/account/set-leverage",
            body={"instId": to_okx_inst(symbol), "lever": str(int(leverage)), "mgnMode": "cross"},
            signed=True,
        )

    def max_leverage(self, symbol: str) -> int:
        """该品种支持的最大杠杆（来自 public/instruments 的 lever 字段）。"""
        d = self._data(self._request("GET", "/api/v5/public/instruments",
                                     {"instType": "SWAP", "instId": to_okx_inst(symbol)}))
        if not d:
            return 100
        try:
            return int(float(d[0].get("lever", 100)))
        except (TypeError, ValueError):
            return 100

    def place_market(self, symbol: str, side: str, quantity: float, reduce_only: bool = False) -> OrderResult:
        self._guard()
        tick, step, _ = self.filters(symbol)
        qty = self._quantize(quantity, step)
        if qty <= 0:
            raise ExchangeError(f"下单数量四舍五入后为 0（原始 {quantity}，step {step}）")
        body = {
            "instId": to_okx_inst(symbol),
            "tdMode": "cross",
            "side": side.upper(),
            "ordType": "market",
            "sz": str(qty),
        }
        if reduce_only:
            body["reduceOnly"] = "true"
        raw = self._request("POST", "/api/v5/trade/order", body=body, signed=True)
        d = self._data(raw)
        oid = str(d[0].get("ordId", "")) if d else ""
        # 市价单平均价要再查一次成交回填（接口不直接返回）
        price = 0.0
        try:
            fills = self._data(self._request(
                "GET", "/api/v5/trade/fills",
                {"instType": "SWAP", "instId": to_okx_inst(symbol), "ordId": oid, "limit": 1},
                signed=True,
            ))
            if fills:
                price = float(fills[0].get("fillPx", 0.0) or 0.0)
        except ExchangeError:
            pass
        return OrderResult(ok=True, order_id=oid, price=price, qty=qty, raw=raw)

    def place_stop_market(
        self,
        symbol: str,
        side: str,
        trigger_price: float,
        close_position: bool = True,
        order_type: str = "STOP_MARKET",
    ) -> OrderResult:
        """触发市价单（OKX v5 的 trigger 单，orderPx=-1 表示市价触发后按市价成交）。"""
        self._guard()
        tick, step, _ = self.filters(symbol)
        inst = to_okx_inst(symbol)
        # OKX 的平仓触发单要带数量，取当前持仓量
        pos = self.positions(symbol)
        if not pos:
            raise ExchangeError("没有持仓，无法下平仓触发单")
        qty = self._quantize(pos[0]["qty"], step)
        body = {
            "instId": inst,
            "tdMode": "cross",
            "side": side.upper(),
            "ordType": "trigger",
            "triggerPx": f"{self._round_price(trigger_price, tick)}",
            "triggerPxType": "mark",
            "sz": str(qty),
            "orderPx": "-1",       # -1 = 触发后按市价
            "tgtFillPx": "-1",
        }
        raw = self._request("POST", "/api/v5/trade/order-algo", body=body, signed=True)
        d = self._data(raw)
        oid = str(d[0].get("algoId", "")) if d else ""
        return OrderResult(ok=True, order_id=oid, raw=raw)

    def cancel_all(self, symbol: str) -> dict:
        """撤掉普通挂单 + 触发单（OKX 两类单要分别撤）。"""
        self._guard()
        inst = to_okx_inst(symbol)
        cancelled = 0
        # 普通挂单
        for o in self._data(self._request(
            "GET", "/api/v5/trade/orders-pending",
            {"instType": "SWAP", "instId": inst}, signed=True)):
            try:
                self._request("POST", "/api/v5/trade/cancel-order",
                              body={"instId": inst, "ordId": o.get("ordId")}, signed=True)
                cancelled += 1
            except ExchangeError as exc:
                self.last_error = str(exc)   # noqa: F841 - 单个撤单失败不阻断
        # 触发单（algo）
        for o in self._data(self._request(
            "GET", "/api/v5/trade/orders-algo-pending",
            {"ordType": "1", "instType": "SWAP", "instId": inst}, signed=True)):
            try:
                self._request("POST", "/api/v5/trade/cancel-algo-order",
                              body={"instId": inst, "algoId": o.get("algoId")}, signed=True)
                cancelled += 1
            except ExchangeError as exc:
                self.last_error = str(exc)   # noqa: F841
        return {"cancelled": cancelled}

    def open_algo_orders(self, symbol: str) -> list[dict]:
        inst = to_okx_inst(symbol)
        rows = self._data(self._request(
            "GET", "/api/v5/trade/orders-algo-pending",
            {"ordType": "1", "instType": "SWAP", "instId": inst}, signed=True))
        out = []
        for o in rows:
            try:
                tp = float(o.get("triggerPx", 0.0) or 0.0)
            except (TypeError, ValueError):
                tp = 0.0
            out.append({"algoId": o.get("algoId"), "triggerPrice": tp, "side": o.get("side")})
        return out

    def open_orders(self, symbol: str) -> list[dict]:
        inst = to_okx_inst(symbol)
        return self._data(self._request(
            "GET", "/api/v5/trade/orders-pending",
            {"instType": "SWAP", "instId": inst}, signed=True))

    def trades(self, symbol: str | None = None, limit: int = 30) -> list[dict]:
        """账户在交易所的真实成交。symbol 为空时拉全部品种（切换品种后仍能看到上一笔）。"""
        self._guard()
        params: dict = {"instType": "SWAP", "limit": max(1, min(limit, 100))}
        if symbol:
            params["instId"] = to_okx_inst(symbol)
        rows = self._data(self._request("GET", "/api/v5/trade/fills", params, signed=True))
        out = []
        for i, t in enumerate(rows or []):
            side = "LONG" if str(t.get("posSide", "")).lower() == "long" else "SHORT"
            epoch = int(self._f(t.get("ts")))
            try:
                ts = time.strftime("%m-%d %H:%M:%S", time.localtime(epoch / 1000))
            except (TypeError, ValueError, OSError):
                ts = ""
            out.append(
                (
                    epoch,
                    {
                        "id": f"{t.get('tradeId', i)}",
                        "symbol": from_okx_inst(t.get("instId") or (symbol or "")),
                        "ts": ts,
                        "ts_ms": epoch,
                        "action": "买入" if str(t.get("side", "")).upper() == "BUY" else "卖出",
                        "side": side,
                        "qty": self._f(t.get("fillSz")),
                        "price": self._f(t.get("fillPx")),
                        "pnl": self._f(t.get("pnl")),
                        "fee": self._f(t.get("fee")),
                        "order_id": str(t.get("ordId", "")),
                    },
                )
            )
        out.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in out[:limit]]

    def income(self, start_ms: int, limit: int = 1000) -> list[dict]:
        """资金流水：用于算「当日盈亏」（已实现盈亏 + 手续费 + 资金费）。

        OKX 的 bills 接口以 type 区分（2=交易 / 6=资金费），逐条折算成与币安
        income 同构的 {income, incomeType} 列表，前端/server 的日盈亏算法可直接复用。
        """
        self._guard()
        rows = self._data(self._request(
            "GET", "/api/v5/account/bills",
            {"instType": "SWAP", "limit": min(limit, 100)}, signed=True))
        out = []
        for r in rows or []:
            ts = int(r.get("ts", 0) or 0)
            if ts and ts < start_ms:
                continue
            typ = str(r.get("type", ""))
            if typ == "2":   # 交易：已实现盈亏 + 手续费
                pnl = float(r.get("pnl", 0.0) or 0.0)
                fee = float(r.get("fee", 0.0) or 0.0)
                if pnl:
                    out.append({"income": pnl, "incomeType": "REALIZED_PNL"})
                if fee:
                    out.append({"income": -fee, "incomeType": "COMMISSION"})
            elif typ == "6":  # 资金费
                out.append({"income": float(r.get("balChg", 0.0) or 0.0), "incomeType": "FUNDING_FEE"})
            # 其它类型（划转等）忽略，server 端也不会计入盈亏
        return out

    @staticmethod
    def _round_qty(qty: float, step: float = 0.001) -> float:
        return float(int(qty / step) * step)

    @staticmethod
    def _round_price(price: float, tick: float = 0.1) -> float:
        return round(round(price / tick) * tick, 8)

    # -- 精度 ---------------------------------------------------------
    def filters(self, symbol: str) -> tuple[float, float, float]:
        """(tickSize, stepSize, minNotional)，来自 public/instruments 并缓存。"""
        if symbol in self._filters:
            return self._filters[symbol]
        tick, step, min_notional = 0.1, 0.001, 5.0
        try:
            d = self._data(self._request("GET", "/api/v5/public/instruments",
                                         {"instType": "SWAP", "instId": to_okx_inst(symbol)}))
            if d:
                i = d[0]
                tick = self._f(i.get("tickSz"), tick)
                step = self._f(i.get("lotSz"), step)
                ctval = self._f(i.get("ctVal"), 1.0)
                try:
                    min_notional = float(i.get("minSz", 0.0) or 0.0) * ctval * 1.0
                except (TypeError, ValueError):
                    min_notional = 5.0
        except ExchangeError:
            pass
        self._filters[symbol] = (tick, step, max(min_notional, 1.0))
        return self._filters[symbol]

    def _quantize(self, value: float, step: float) -> float:
        if step <= 0:
            return value
        return round(int(value / step + 1e-9) * step, 10)