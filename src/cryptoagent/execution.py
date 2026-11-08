"""执行层：模拟撮合 + 币安 U 本位合约 / OKX 永续 客户端。

安全模型：
- `BinanceFuturesClient` 默认指向 **testnet**；`OKXClient` 默认用模拟盘（x-simulated-trading 头）。
- 所有写操作（下单、改杠杆、撤单）都要经过 `_guard()`：
  必须 `armed=True`。
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


def _round_price(price: float, tick: float = 0.1) -> float:
    """按最小变动价位取整价格（两个交易所共用）。"""
    return round(round(price / tick) * tick, 8)


def _quantize(value: float, step: float) -> float:
    """按步长量化：保留浮点误差余量，避免 0.29999 被判成 0.29。"""
    if step <= 0:
        return value
    return round(int(value / step + 1e-9) * step, 10)


def _fmt_by_step(value: float, step: float) -> str:
    """按步长的小数位数格式化成交易所要的十进制字符串。

    OKX 对价格/数量做严格的小数位校验：`repr(73604.66000000001)` 会被判
    "Parameter triggerPx error"。所以小数位数必须由 tickSz / lotSz 决定，
    先按该位数四舍五入再定点输出，顺带避免科学计数法。
    """
    nd = 0
    step = abs(float(step or 0.0))
    if step > 0:
        text = f"{step:.12f}".rstrip("0")
        if "." in text:
            nd = len(text.split(".")[1])
    text = f"{round(float(value), nd):.{nd}f}"
    if nd:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


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


def account_equity(acct: dict) -> float:
    """账户权益：优先取客户端给出的 `equity`，缺少该字段才按「余额 + 浮盈」回退。

    币安 `walletBalance` 不含未实现盈亏，`equity = walletBalance + unrealizedProfit`；
    OKX `details.eq`（币种权益）**本身已含** `upl`（= 余额 + 全仓/逐仓浮动收益），
    再叠一次 upl 会把浮盈算两遍。两家客户端都在 `account()` 里直接给出 `equity`，
    所有调用方统一走这里，不要在别处自己拼。
    """
    if not isinstance(acct, dict):
        return 0.0
    raw = acct.get("equity")
    if raw is not None:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    try:
        return float(acct.get("balance") or 0.0) + float(acct.get("unrealized_pnl") or 0.0)
    except (TypeError, ValueError):
        return 0.0


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
        self.testnet = self.env != "live"          # 非 live 都算「假钱」
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

    def _read_guard(self) -> None:
        """只读接口守卫：只校验凭据，不要求 armed。

        成交查询是 GET；disarm 后守护线程仍要补记交易所侧已成交的平仓，
        沿用写守卫会让补记静默失败、持仓流水缺失。
        """
        if not self.api_key or not self.api_secret:
            raise ExchangeError("缺少 API Key / Secret")

    def _guard(self) -> None:
        if not self.armed:
            raise ExchangeError("客户端未解锁（armed=False），拒绝任何写操作")
        if not self.api_key or not self.api_secret:
            raise ExchangeError("缺少 API Key / Secret")

    def _request(self, method: str, path: str, params: dict | None = None, signed: bool = False) -> dict:
        params = dict(params or {})
        self._throttle()
        headers = {"User-Agent": "QeeLin-Agent/1.2.1"}
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
        wallet = float(usdt.get("walletBalance", 0.0))
        upl = float(usdt.get("unrealizedProfit", 0.0))
        return {
            "balance": wallet,
            "available": float(usdt.get("availableBalance", 0.0)),
            "unrealized_pnl": upl,
            # 币安 walletBalance 不含浮盈，账户权益要显式加上；调用方别再自己加一次
            "equity": wallet + upl,
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
        qty = _quantize(quantity, step)
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
            "triggerPrice": _quantize(trigger_price, tick),
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
        self._read_guard()
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


# --------------------------------------------------------------------------
# OKX 永续（U 本位，v5 API）
# --------------------------------------------------------------------------
class OKXClient:
    """OKX 永续合约客户端，接口与 BinanceFuturesClient 对齐。

    - 模拟盘：在 live 域名上带 `x-simulated-trading: 1` 头（OKX 没有独立测试网域名）。
    - 主网：去掉该头即可（写操作仅受 `armed` 门控；主网解锁见 engine.arm）。
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
        # 合约元数据（tickSz / lotSz / minSz / ctVal / lever）与账户持仓模式，
        # 首次用到时各拉一次。OKX 永续下单单位是「张」，必须靠 ctVal 换算。
        self._inst_cache: dict[str, dict] = {}
        self._pos_mode_cache = ""

    # -- 内部 ---------------------------------------------------------
    def _throttle(self) -> None:
        delta = time.time() - self._last_call
        if delta < 0.05:
            time.sleep(0.05 - delta)
        self._last_call = time.time()

    def _read_guard(self) -> None:
        """只读接口守卫：只校验凭据，不要求 armed（理由同 BinanceFuturesClient）。"""
        if not self.api_key or not self.api_secret or not self.passphrase:
            raise ExchangeError("缺少 API Key / Secret / Passphrase")

    def _guard(self) -> None:
        if not self.armed:
            raise ExchangeError("客户端未解锁（armed=False），拒绝任何写操作")
        if not self.api_key or not self.api_secret or not self.passphrase:
            raise ExchangeError("缺少 API Key / Secret / Passphrase")

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
            "User-Agent": "QeeLin-Agent/1.2.1",
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
        else:
            # 公共接口（instruments / mark-price / funding 等）都用 query 传参：
            # 以前这里只给签名请求拼 query，未签名 GET 的参数被整体丢弃，
            # 结果是 filters() 静默退回 BTC 默认精度、max_leverage() 直接 400。
            query = urllib.parse.urlencode(params)
            if query:
                url = f"{url}?{query}"
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
            # 下单类接口失败时顶层只有 "All operations failed"，真实原因在 data[0].sCode
            # （例如 51000 side error / 51121 数量不是最小变动单位的整数倍）。
            # 不把 sCode/sMsg 带出来，排查时等于没有信息。
            detail = ""
            rows = payload.get("data")
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and str(row.get("sCode") or "0") != "0":
                        detail = f"（{row.get('sCode')}: {row.get('sMsg')}）"
                        break
            raise ExchangeError(f"OKX 错误 [{payload.get('code')}] {payload.get('msg')}{detail}")
        return payload

    @staticmethod
    def _data(payload: dict) -> list:
        return payload.get("data", []) if isinstance(payload, dict) else []

    def _inst_info(self, symbol_or_inst: str) -> dict:
        """合约元数据：tickSz / lotSz / minSz / ctVal / lever（按 instId 缓存）。

        ⚠️ OKX 永续的 `sz` 与持仓 `pos` 都是**张数**，1 张 = ctVal 个币
        （BTC-USDT-SWAP ctVal=0.01、DOGE-USDT-SWAP ctVal=1000）。
        本程序内部一律用「币数」，跨边界时用这里的 ctVal 换算。
        拉不到时返回保守默认值且**不缓存**，下次重新探测。
        """
        inst = (
            symbol_or_inst
            if str(symbol_or_inst).endswith("-SWAP")
            else to_okx_inst(symbol_or_inst)
        )
        cached = self._inst_cache.get(inst)
        if cached is not None:
            return cached
        info = {
            "instId": inst,
            "tickSz": 0.1,
            "lotSz": 1.0,
            "minSz": 1.0,
            "ctVal": 1.0,
            "lever": 100.0,
        }
        try:
            rows = self._data(
                self._request(
                    "GET", "/api/v5/public/instruments",
                    {"instType": "SWAP", "instId": inst},
                )
            )
        except ExchangeError:
            return info
        if rows:
            row = rows[0]
            info["tickSz"] = self._f(row.get("tickSz"), 0.1) or 0.1
            info["lotSz"] = self._f(row.get("lotSz"), 1.0) or 1.0
            info["minSz"] = self._f(row.get("minSz"), 1.0) or 1.0
            info["ctVal"] = self._f(row.get("ctVal"), 1.0) or 1.0
            info["lever"] = self._f(row.get("lever"), 100.0) or 100.0
            self._inst_cache[inst] = info
        return info

    def _pos_mode(self) -> str:
        """账户持仓模式（net_mode / long_short_mode），首次写入操作时探测一次。

        双向持仓模式下单纯下市价单必须带 posSide，否则 OKX 只回一句
        "All operations failed"。本客户端的下单路径按单向持仓实现，
        检测到双向就明确拒绝，而不是发一堆注定失败的单。
        """
        if self._pos_mode_cache:
            return self._pos_mode_cache
        try:
            rows = self._data(self._request("GET", "/api/v5/account/config", {}, signed=True))
        except ExchangeError:
            return ""
        mode = str((rows[0] if rows else {}).get("posMode") or "")
        if mode:
            self._pos_mode_cache = mode
        return mode

    def _require_net_mode(self) -> None:
        if self._pos_mode() == "long_short_mode":
            raise ExchangeError(
                "OKX 账户当前是「双向持仓(long_short_mode)」，本程序只支持「单向持仓(net_mode)」；"
                "请在 OKX 网页端「交易 → 交易设置 → 持仓模式」切换为单向持仓后重试"
            )

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
            # OKX 的 eq（币种权益）= 余额 + 全仓/逐仓浮动收益，已经含 upl；
            # 早期把 eq 直接塞进 balance 再叠加 upl 会把浮盈算两遍。
            # balance 去掉浮盈以对齐币安 walletBalance 口径，equity 单列真值。
            eq = self._f(usdt.get("eq"))
            upl = self._f(usdt.get("upl"))
            return {
                "balance": eq - upl,
                "available": self._f(usdt.get("availBal")),
                "unrealized_pnl": upl,
                "equity": eq,
            }
        # 没有 USDT 明细时退回总权益
        total = self._f(d[0].get("totalEq")) if d else 0.0
        return {"balance": total, "available": total, "unrealized_pnl": 0.0, "equity": total}

    def positions(self, symbol: str | None = None) -> list[dict]:
        inst = to_okx_inst(symbol) if symbol else None
        data = self._data(self._request("GET", "/api/v5/account/positions", {"instType": "SWAP"}, signed=True))
        out = []
        for p in data:
            if inst and p.get("instId") != inst:
                continue
            contracts = self._f(p.get("pos"))
            if abs(contracts) < 1e-12:
                continue
            ct_val = self._inst_info(p.get("instId") or symbol or "")["ctVal"]
            # side 不能只看 posSide：单向持仓（net_mode）下 posSide 恒为 "net"，
            # 老代码把它一律当 SHORT，多单会被标成空单。
            pos_side = str(p.get("posSide") or "").lower()
            if pos_side in ("long", "short"):
                side = "LONG" if pos_side == "long" else "SHORT"
            else:
                side = "LONG" if contracts > 0 else "SHORT"
            out.append(
                {
                    "symbol": from_okx_inst(p.get("instId", symbol or "")),
                    "side": side,
                    # 对外统一「币数」，与币安 positionAmt 口径一致；张数单独给出便于对账
                    "qty": round(abs(contracts) * ct_val, 10),
                    "contracts": round(abs(contracts), 10),
                    "ct_val": ct_val,
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
        self._require_net_mode()
        return self._request(
            "POST",
            "/api/v5/account/set-leverage",
            body={"instId": to_okx_inst(symbol), "lever": str(int(leverage)), "mgnMode": "cross"},
            signed=True,
        )

    def max_leverage(self, symbol: str) -> int:
        """该品种支持的最大杠杆（来自 public/instruments 的 lever 字段）。"""
        try:
            return int(float(self._inst_info(symbol).get("lever") or 100))
        except (TypeError, ValueError):
            return 100

    def place_market(self, symbol: str, side: str, quantity: float, reduce_only: bool = False) -> OrderResult:
        """市价单。`quantity` 是**币数**（与币安一致），这里换算成 OKX 的张数。"""
        self._guard()
        self._require_net_mode()
        info = self._inst_info(symbol)
        ct_val = info["ctVal"]
        lot = info["lotSz"]
        if quantity <= 0:
            raise ExchangeError(f"下单数量必须为正（收到 {quantity}）")
        contracts = _quantize(quantity / ct_val, lot)
        if contracts <= 0 or contracts + 1e-12 < info["minSz"]:
            raise ExchangeError(
                f"下单数量低于 OKX 最小下单量：{quantity:g} 币 ≈ {quantity / ct_val:.8g} 张，"
                f"最小 {info['minSz']:g} 张（1 张 = {ct_val:g} 币）"
            )
        body = {
            "instId": to_okx_inst(symbol),
            "tdMode": "cross",
            # OKX 的 side 只接受小写 buy / sell，传 "BUY" 会得到 51000 side error
            "side": str(side).strip().lower(),
            "ordType": "market",
            "sz": _fmt_by_step(contracts, lot),
        }
        if reduce_only:
            body["reduceOnly"] = "true"
        raw = self._request("POST", "/api/v5/trade/order", body=body, signed=True)
        d = self._data(raw)
        oid = str(d[0].get("ordId", "")) if d else ""
        # 成交回读优先用「订单详情」：/trade/fills 有 1~2 秒索引延迟（实测下单后
        # 立刻查为空），而订单详情当场就有 avgPx / accFillSz / fee（负=扣费），
        # 一次请求就把成交价、成交量、手续费全拿到。
        price, filled, fee = 0.0, 0.0, 0.0
        for attempt in range(3):
            try:
                rows = self._data(self._request(
                    "GET", "/api/v5/trade/order",
                    {"instId": to_okx_inst(symbol), "ordId": oid}, signed=True,
                ))
                if rows:
                    row = rows[0]
                    price = self._f(row.get("avgPx"))
                    filled = self._f(row.get("accFillSz"))
                    fee = -self._f(row.get("fee"))     # OKX 负数=扣费 → 正数=支出
                    if price > 0 and filled > 0:
                        break
            except ExchangeError:
                pass
            if attempt < 2:
                time.sleep(0.2)
        qty = round(filled * ct_val, 10) if filled > 0 else round(contracts * ct_val, 10)
        return OrderResult(ok=True, order_id=oid, price=price, qty=qty, fee=fee, raw=raw)

    def place_stop_market(
        self,
        symbol: str,
        side: str,
        trigger_price: float,
        close_position: bool = True,
        order_type: str = "STOP_MARKET",
    ) -> OrderResult:
        """触发市价单（OKX v5 的 trigger 单，orderPx=-1 表示市价触发后按市价成交）。

        - `side`/`sz` 都必须小写 + 张数，否则 51000 / 51121。
        - 触发价按合约自己的 tickSz 取整（BTC 是 0.01，不是 0.1）。
        - 平仓单带 reduceOnly，避免触发时反手把仓位翻倍。
        """
        self._guard()
        self._require_net_mode()
        info = self._inst_info(symbol)
        inst = to_okx_inst(symbol)
        pos = self.positions(symbol)
        if not pos:
            raise ExchangeError("没有持仓，无法下平仓触发单")
        contracts = _quantize(pos[0]["qty"] / info["ctVal"], info["lotSz"])
        if contracts <= 0:
            raise ExchangeError(f"持仓不足一张（{pos[0]['qty']:g} 币），无法下触发单")
        body = {
            "instId": inst,
            "tdMode": "cross",
            "side": str(side).strip().lower(),
            "ordType": "trigger",
            "triggerPx": _fmt_by_step(_round_price(trigger_price, info["tickSz"]), info["tickSz"]),
            "triggerPxType": "mark",
            "sz": _fmt_by_step(contracts, info["lotSz"]),
            "orderPx": "-1",       # -1 = 触发后按市价
        }
        if close_position:
            body["reduceOnly"] = "true"
        raw = self._request("POST", "/api/v5/trade/order-algo", body=body, signed=True)
        d = self._data(raw)
        oid = str(d[0].get("algoId", "")) if d else ""
        return OrderResult(ok=True, order_id=oid, raw=raw)

    def cancel_all(self, symbol: str) -> dict:
        """撤掉普通挂单 + 触发单（OKX 两类单要分别撤）。

        触发单列表的 ordType 必须是 "trigger"（老代码写 "1" 会被 400 拒掉，
        导致撤单整条链路失败、上一次的止损单残留到下一笔仓位）。
        触发单撤单接口是批量的 `cancel-algos`，`cancel-algo-order` 已 404。
        """
        self._guard()
        inst = to_okx_inst(symbol)
        cancelled = 0
        errors: list[str] = []
        # 普通挂单
        for o in self._data(self._request(
            "GET", "/api/v5/trade/orders-pending",
            {"instType": "SWAP", "instId": inst}, signed=True)):
            try:
                self._request("POST", "/api/v5/trade/cancel-order",
                              body={"instId": inst, "ordId": o.get("ordId")}, signed=True)
                cancelled += 1
            except ExchangeError as exc:
                errors.append(str(exc))   # 单个撤单失败不阻断
        # 触发单（algo）
        algos = self._data(self._request(
            "GET", "/api/v5/trade/orders-algo-pending",
            {"ordType": "trigger", "instType": "SWAP", "instId": inst}, signed=True))
        if algos:
            try:
                self._request("POST", "/api/v5/trade/cancel-algos",
                              body=[{"instId": inst, "algoId": o.get("algoId")} for o in algos],
                              signed=True)
                cancelled += len(algos)
            except ExchangeError as exc:
                errors.append(str(exc))
        if errors:
            self.last_error = errors[0]
        return {"cancelled": cancelled, "errors": errors}

    def open_algo_orders(self, symbol: str) -> list[dict]:
        inst = to_okx_inst(symbol)
        rows = self._data(self._request(
            "GET", "/api/v5/trade/orders-algo-pending",
            {"ordType": "trigger", "instType": "SWAP", "instId": inst}, signed=True))
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
        """账户在交易所的真实成交。symbol 为空时拉全部品种（切换品种后仍能看到上一笔）。

        量纲/符号按币安口径归一：
        - `qty` 用币数（OKX 的 fillSz 是张数，乘 ctVal）；
        - `fee` 用「正数=支出」（OKX 的 fee 负数=扣费，这里取反）；
        - `side` 是持仓方向（OKX 单向持仓下 posSide="net"，要退回按买卖方向推断）。
        """
        self._read_guard()
        params: dict = {"instType": "SWAP", "limit": max(1, min(limit, 100))}
        if symbol:
            params["instId"] = to_okx_inst(symbol)
        rows = self._data(self._request("GET", "/api/v5/trade/fills", params, signed=True))
        out = []
        for i, t in enumerate(rows or []):
            inst = str(t.get("instId") or "")
            ct_val = self._inst_info(inst or (symbol or ""))["ctVal"]
            pos_side = str(t.get("posSide") or "").lower()
            if pos_side in ("long", "short"):
                side = "LONG" if pos_side == "long" else "SHORT"
            else:
                side = "LONG" if str(t.get("side", "")).lower() == "buy" else "SHORT"
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
                        "symbol": from_okx_inst(inst or (symbol or "")),
                        "ts": ts,
                        "ts_ms": epoch,
                        "action": "买入" if str(t.get("side", "")).lower() == "buy" else "卖出",
                        "side": side,
                        "qty": round(self._f(t.get("fillSz")) * ct_val, 10),
                        "price": self._f(t.get("fillPx")),
                        "pnl": self._f(t.get("pnl")),
                        "fee": -self._f(t.get("fee")),
                        "order_id": str(t.get("ordId", "")),
                    },
                )
            )
        out.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in out[:limit]]

    def income(self, start_ms: int, limit: int = 1000) -> list[dict]:
        """资金流水：用于算「当日盈亏」（已实现盈亏 + 手续费 + 资金费）。

        OKX 的 bills 接口以 type 区分，逐条折算成与币安 income 同构的
        {income, incomeType} 列表，前端/server 的日盈亏算法可直接复用：

        - type=2 交易：pnl 已是带符号的已实现盈亏；
          fee 在 OKX 是「负数=扣费」，与币安 COMMISSION 的负数 income 同号，直接透传
          （老代码取反，等于把手续费算成收入）。
        - type=8 资金费：用 balChg（正=收到、负=支付）。
          type=6 是保证金划转，不是资金费（老代码错把 6 当资金费，资金费永远为 0）。
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
            # 统一带毫秒时间戳：重置账户后 server 要按「重置时刻」重新起算当日盈亏
            if typ == "2":   # 交易：已实现盈亏 + 手续费
                pnl = float(r.get("pnl", 0.0) or 0.0)
                fee = float(r.get("fee", 0.0) or 0.0)
                if pnl:
                    out.append({"income": pnl, "incomeType": "REALIZED_PNL", "time": ts})
                if fee:
                    out.append({"income": fee, "incomeType": "COMMISSION", "time": ts})
            elif typ == "8":  # 资金费（Funding rate）
                out.append({"income": float(r.get("balChg", 0.0) or 0.0), "incomeType": "FUNDING_FEE", "time": ts})
            # 其它类型（划转 1 / 保证金划转 6 / 利息 7 / 强平 5 等）忽略，server 端也不会计入盈亏
        return out

    # -- 精度 ---------------------------------------------------------
    def filters(self, symbol: str) -> tuple[float, float, float]:
        """(tickSz, 币数步长, 最小名义 USDT)，来自 public/instruments 并缓存。

        OKX 永续的 lotSz/minSz 都是**张数**：步长要乘 ctVal 换成币数，
        否则引擎按币数下单时会被 51121（数量不是最小变动单位整数倍）拒绝。
        """
        if symbol in self._filters:
            return self._filters[symbol]
        info = self._inst_info(symbol)
        ct_val = info["ctVal"]
        step = (info["lotSz"] or 1.0) * ct_val
        min_qty = (info["minSz"] or 1.0) * ct_val
        min_notional = 5.0
        try:
            px = self.mark_price(symbol)
            if px > 0 and min_qty > 0:
                min_notional = min_qty * px
        except ExchangeError:
            pass
        self._filters[symbol] = (info["tickSz"], step, max(min_notional, 0.0))
        return self._filters[symbol]