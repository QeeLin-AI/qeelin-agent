"""行情数据层。

两个来源：
1. synthetic —— 内置的合成行情生成器，离线可跑，用于验证整条流水线。
   它含有「波动率聚集 + 动量/反转交替 + 偶发跳空」三种真实市场特征，
   但**不代表真实市场存在可利用的 alpha**，只用于确认工程链路正确。
2. binance —— 币安 U 本位永续公开接口（K 线 + 资金费率），需要能访问外网。

统一输出 DataFrame，索引为 UTC 时间，列：
    open, high, low, close, volume, funding_rate
其中 funding_rate 是该 bar 对应的资金费率（每 8 小时结算一次，其余 bar 为 0）。
"""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import numpy as np
import pandas as pd

from .config import MarketConfig
from .netutil import ssl_context

FAPI = "https://fapi.binance.com"
DEMO = "https://demo-fapi.binance.com"   # 币安 U 本位合约「模拟盘」公开行情接口
VISION = "https://data-api.binance.vision"   # 现货镜像，仅用于 fetch_24h 的成交额兜底
GATE = "https://api.gateio.ws"

INTERVAL_HOURS = {
    # 分钟级周期必须写全：缺项会 fallback 成 1 小时，导致 Gate 翻页步长
    # 和缓存时效都算错（5m 被当成 1h，一次跳掉 11 根 K 线）。
    # 10s 仅 Gate 合约支持，不在 KL_INTERVALS 里：统一取数路径不接受它。
    "10s": 10.0 / 3600.0,
    "1m": 1.0 / 60.0,
    "3m": 3.0 / 60.0,
    "5m": 5.0 / 60.0,
    "15m": 0.25,
    "30m": 0.5,
    "1h": 1.0,
    "2h": 2.0,
    "4h": 4.0,
    "6h": 6.0,
    "8h": 8.0,
    "12h": 12.0,
    "1d": 24.0,
}


class DataError(RuntimeError):
    pass


def bars_per_year(interval: str) -> float:
    hours = INTERVAL_HOURS.get(interval, 1.0)
    return 365.0 * 24.0 / hours


# --------------------------------------------------------------------------
# 合成行情
# --------------------------------------------------------------------------
def synthetic_ohlcv(
    n: int = 6000,
    seed: int = 7,
    start_price: float = 30_000.0,
    interval: str = "1h",
    start: str = "2025-01-01",
) -> pd.DataFrame:
    """生成带有波动率聚集、OU 趋势和跳空的合成永续合约行情。"""
    rng = np.random.default_rng(seed)

    # --- 波动率：GARCH 式的均值回复过程 ---
    base_vol = 0.008 * math.sqrt(INTERVAL_HOURS.get(interval, 1.0))
    vol = np.empty(n)
    vol[0] = base_vol
    for i in range(1, n):
        vol[i] = 0.97 * vol[i - 1] + 0.03 * base_vol + 0.00035 * rng.standard_normal()
        vol[i] = min(max(vol[i], 0.3 * base_vol), 4.0 * base_vol)

    # --- 趋势：OU 过程（均值回复的漂移），比"台阶式"状态更接近真实市场 ---
    # theta 控制趋势半衰期（0.015 ≈ 46 根），0.009 控制趋势强度。
    # 强度刻意压到「可被动量因子捕捉，但不足以预测每一天」的水平。
    theta, drift_sigma = 0.015, 0.009
    drift = np.empty(n)
    d = 0.0
    for i in range(n):
        d = d * (1.0 - theta) + drift_sigma * vol[i] * rng.standard_normal()
        drift[i] = d

    shocks = rng.standard_normal(n)
    ret = drift + vol * shocks

    # --- 偶发跳空（插针）---
    jump_mask = rng.random(n) < 0.0008
    ret[jump_mask] += rng.choice([-1.0, 1.0], jump_mask.sum()) * 0.03

    close = start_price * np.exp(np.cumsum(ret))
    open_ = np.empty(n)
    open_[0] = start_price
    open_[1:] = close[:-1]
    wick = np.abs(rng.standard_normal(n)) * vol * 0.6
    high = np.maximum(open_, close) * (1.0 + wick)
    low = np.minimum(open_, close) * (1.0 - wick)
    volume = np.exp(rng.standard_normal(n) * 0.45 + 10.0)

    # --- 资金费率：与基差/趋势同向，做多拥挤时为负（空头付钱）---
    funding = np.zeros(n)
    every = int(8 / INTERVAL_HOURS.get(interval, 1.0)) or 1
    for i in range(0, n, every):
        momentum = float(np.sum(ret[max(0, i - 24) : i + 1]))
        funding[i] = float(np.tanh(momentum * 8.0)) * 0.00015

    # --- 成交明细代理：让基于主动买占比/平均单笔规模的因子在合成数据上也能验证 ---
    # 主动买占比与当根收益同向（涨的时候主动买更多），这是真实市场里最稳定的一阶特征。
    buy_share = np.clip(0.5 + np.tanh(ret / np.maximum(vol, 1e-6)) * 0.18, 0.05, 0.95)
    taker_base = volume * buy_share
    quote_volume = volume * close
    taker_quote = taker_base * close
    trades = np.maximum(1.0, volume / np.exp(rng.normal(0.0, 0.15, n)) / 0.5)

    idx = pd.date_range(start=start, periods=n, freq=_pandas_freq(interval), tz="UTC")
    df = pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "funding_rate": funding,
            "quote_volume": quote_volume,
            "trades": trades,
            "taker_base": taker_base,
            "taker_quote": taker_quote,
        },
        index=idx,
    )
    df.index.name = "ts"
    return df


def _pandas_freq(interval: str) -> str:
    if interval.endswith("m"):
        return f"{interval[:-1]}min"
    if interval.endswith("h"):
        return f"{interval[:-1]}h"
    if interval.endswith("d"):
        return f"{interval[:-1]}D"
    return "1h"


# --------------------------------------------------------------------------
# 真实行情下载（多源回退）
# --------------------------------------------------------------------------
_DEAD_UNTIL: dict[str, float] = {}   # 源 -> 恢复时间戳；避免每次启动都去撞一个死的域名
DEAD_COOLDOWN = 600.0

# 实时价单独一套冷却：它每 5 秒重试一次，冷却 10 分钟等于漏掉 120 个心跳，所以只冷却 30 秒
_DEAD_LIVE: dict[str, float] = {}
LIVE_COOLDOWN = 30.0


def _dead(host: str) -> bool:
    return float(_DEAD_UNTIL.get(host, 0.0)) > time.time()


def _mark_dead(host: str) -> None:
    _DEAD_UNTIL[host] = time.time() + DEAD_COOLDOWN


# OKX 会拦非浏览器 UA（403），统一用浏览器 UA。
# 版本号必须是近期版本：Gate 的 WAF 按 UA 指纹拉黑旧浏览器，Chrome/120 稳定 403，
# 换成 131 立即 200（2026-11 实测）。别为了「好看」往回改版本号。
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 兜底 UA：不带浏览器指纹，Gate / 币安实测都放行。只在主 UA 被 403 时补一次，
# 这样 WAF 哪天把当前 Chrome 版本也拉黑了，备用源不会整体瘫掉。
_UA_FALLBACK = "QeeLin-Agent (+https://github.com/QeeLin-AI/qeelin-agent)"


def _get(url: str, params: dict, timeout: int = 6):
    q = urllib.parse.urlencode(params)
    last: Exception | None = None
    for ua in (_UA, _UA_FALLBACK):
        req = urllib.request.Request(
            f"{url}?{q}",
            headers={"User-Agent": ua, "Accept": "application/json", "Accept-Language": "en-US,en;q=0.9"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 只有 403 值得换 UA 重试：它通常是 WAF 按指纹拦的，不是接口本身出问题。
            # 429/5xx/404 换 UA 也没用，直接抛给上层回退到下一个数据源。
            if exc.code != 403:
                raise
            last = exc
    raise last if last is not None else RuntimeError("请求失败")


def _klines_frame(rows: list) -> pd.DataFrame:
    """币安 K 线 → DataFrame。

    除了 OHLCV，还要保留 quote_volume / trades / taker_base / taker_quote：
    这四列是「主动买占比、订单流不平衡、平均单笔规模、换手率」等衍生因子的
    唯一原料，之前在这里被直接丢弃，导致后续因子无数据可用。
    """
    df = pd.DataFrame(
        rows,
        columns=[
            "open_time", "open", "high", "low", "close", "volume", "close_time",
            "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
        ],
    )
    df.index = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df.index.name = "ts"
    df = df[[
        "open", "high", "low", "close", "volume",
        "quote_volume", "trades", "taker_base", "taker_quote",
    ]].astype(float)
    return df


# 没有成交明细的数据源统一补齐这些列，保证下游因子代码永远拿得到（值为 NaN）。
EXTRA_FIELDS = ("quote_volume", "trades", "taker_base", "taker_quote")


def _fill_extra_fields(df: pd.DataFrame) -> pd.DataFrame:
    for col in EXTRA_FIELDS:
        if col not in df.columns:
            df[col] = float("nan")
    return df


def _fetch_fapi(cfg: MarketConfig, need: int, base: str = FAPI) -> pd.DataFrame:
    """U 本位永续（有资金费率）。国内大多直连不通。"""
    end_time = int(time.time() * 1000)
    rows: list[list] = []
    while len(rows) < need:
        limit = min(1500, need - len(rows))
        batch = _get(
            f"{base}/fapi/v1/klines",
            {"symbol": cfg.symbol, "interval": cfg.interval, "limit": limit, "endTime": end_time},
        )
        if not batch:
            break
        rows = batch + rows
        end_time = int(batch[0][0]) - 1
        if len(batch) < limit:
            break
    if not rows:
        raise DataError("fapi 返回空数据")
    df = _klines_frame(rows)

    # 资金费率（8 小时一条），对齐到 bar
    funding = pd.Series(0.0, index=df.index)
    start = int(df.index[0].timestamp() * 1000)
    fr_rows: list[dict] = []
    cursor = start
    # 上限保护：若交易所忽略 startTime 持续返回满页，没有这个上限就是死循环
    for _page in range(200):
        batch = _get(
            f"{base}/fapi/v1/fundingRate",
            {"symbol": cfg.symbol, "limit": 1000, "startTime": cursor},
        )
        if not batch:
            break
        fr_rows.extend(batch)
        cursor = int(batch[-1]["fundingTime"]) + 1
        if len(batch) < 1000:
            break
    for item in fr_rows:
        ts = pd.to_datetime(int(item["fundingTime"]), unit="ms", utc=True)
        idx = funding.index.get_indexer([ts], method="nearest")[0]
        if idx >= 0:
            funding.iloc[idx] = float(item["fundingRate"])
    df["funding_rate"] = funding.values
    return df


def _fetch_demo_fapi(cfg: MarketConfig, need: int) -> pd.DataFrame:
    """币安 U 本位合约「模拟盘」行情（demo-fapi），同样有资金费率。"""
    return _fetch_fapi(cfg, need, base=DEMO)


def _fetch_gate(cfg: MarketConfig, need: int) -> pd.DataFrame:
    """Gate.io 永续（备用源）。合约名 BTCUSDT → BTC_USDT。"""
    contract = gate_contract(cfg.symbol)
    rows: list[dict] = []
    to_ts = int(time.time())
    if cfg.interval not in GATE_INTERVALS:
        # 静默降级成 1h 会把「2h 的缓存文件」里装满 1h K 线，下游年化/回测
        # 全按 2h 算却拿到 1h 数据，偏差无法察觉。宁可直接失败换源。
        raise DataError(f"Gate 不支持周期 {cfg.interval}，支持 {sorted(GATE_INTERVALS)}")
    interval = cfg.interval
    span = int(INTERVAL_HOURS.get(interval, 1.0) * 3600)
    while len(rows) < need:
        limit = max(1, min(2000, need - len(rows)))
        batch = _get(
            f"{GATE}/api/v4/futures/usdt/candlesticks",
            {"contract": contract, "interval": interval, "limit": limit, "to": to_ts},
        )
        if not batch:
            break
        rows = batch + rows
        to_ts = int(batch[0]["t"]) - span
        if len(batch) < limit:
            break
    if not rows:
        raise DataError("gate 返回空数据")
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df["t"].astype(int), unit="s", utc=True)
    df.index.name = "ts"
    df = df[["o", "h", "l", "c", "v"]].astype(float)
    df.columns = ["open", "high", "low", "close", "volume"]
    df = df[~df.index.duplicated()].sort_index()
    df["funding_rate"] = 0.0
    return _fill_extra_fields(df)


OKX_BARS = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "8h": "8H", "12h": "12H", "1d": "1D",
}


QUOTE_ASSETS = ("USDT", "USDC", "USD")


def split_symbol(symbol: str) -> tuple[str, str]:
    """BTCUSDT → (BTC, USDT)；BTC-USDC-SWAP → (BTC, USDC)。"""
    s = str(symbol or "").upper().replace("-", "").replace("_", "").replace("/", "").replace(" ", "")
    for suffix in ("SWAP", "PERP"):
        if s.endswith(suffix) and len(s) > len(suffix) + 3:
            s = s[: -len(suffix)]
            break
    for quote in QUOTE_ASSETS:
        if s.endswith(quote) and len(s) > len(quote):
            return s[: -len(quote)], quote
    return s, "USDT"


def okx_inst(symbol: str) -> str:
    """BTCUSDT → BTC-USDT-SWAP，BTCUSDC → BTC-USDC-SWAP（OKX 永续）。"""
    base, quote = split_symbol(symbol)
    if quote in ("USDT", "USDC"):
        return f"{base}-{quote}-SWAP"
    return str(symbol).upper()


def okx_live_inst(symbol: str) -> str:
    """行情/实时价用的 instId：BTCUSDT → BTC-USDT。"""
    base, quote = split_symbol(symbol)
    return f"{base}-{quote}"


def gate_contract(symbol: str) -> str:
    """Gate 合约名：BTCUSDT → BTC_USDT。"""
    base, quote = split_symbol(symbol)
    return f"{base}_{quote}"


def _okx_rows_to_df(rows: list[list]) -> pd.DataFrame:
    df = pd.DataFrame(
        rows, columns=["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"]
    )
    df.index = pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True)
    df.index.name = "ts"
    df = df[["open", "high", "low", "close", "vol"]].astype(float)
    df.columns = ["open", "high", "low", "close", "volume"]
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df["funding_rate"] = 0.0
    return _fill_extra_fields(df)


def _fetch_okx_funding(inst: str, since_ms: int) -> list[dict]:
    """拉 OKX 永续历史资金费率（从最新往回翻到 since_ms 之前）。"""
    rows: list[dict] = []
    before = ""
    for _page in range(200):  # 上限保护，避免翻页死循环
        params: dict = {"instId": inst, "limit": "100"}
        if before:
            params["before"] = before
        try:
            batch = _get(f"{OKX}/api/v5/public/funding-rate", params, timeout=10).get("data") or []
        except Exception:  # noqa: BLE001 - funding 是增强数据，拉不到就停下
            break
        if not batch:
            break
        rows.extend(batch)
        oldest = int(batch[-1].get("fundingTime") or 0)
        if oldest <= since_ms or len(batch) < 100:
            break
        before = str(oldest)
    return rows


def _attach_okx_funding(df: pd.DataFrame, inst: str) -> pd.DataFrame:
    """把 OKX 资金费率对齐到 K 线（8 小时一条，其余 bar 为 0）。

    资金费率拿不到/对齐失败时静默保持 0，绝不阻断行情加载——它只是增强信号。
    """
    try:
        start_ms = int(df.index[0].timestamp() * 1000)
        fr_rows = _fetch_okx_funding(inst, start_ms)
        if not fr_rows:
            return df
        funding = pd.Series(0.0, index=df.index)
        for item in fr_rows:
            ft = int(item.get("fundingTime") or 0)
            if ft < start_ms:
                continue
            ts = pd.to_datetime(ft, unit="ms", utc=True)
            idx = funding.index.get_indexer([ts], method="nearest")[0]
            if 0 <= idx < len(funding):
                funding.iloc[idx] = float(item.get("fundingRate") or 0.0)
        df["funding_rate"] = funding.values
    except Exception:  # noqa: BLE001 - 单品种 funding 失败不影响行情
        pass
    return df


def _fetch_okx(cfg: MarketConfig, need: int) -> pd.DataFrame:
    """OKX U 本位永续历史 K 线（公开接口，100 根/次，用 after 往前翻）。"""
    inst = okx_inst(cfg.symbol)
    bar = OKX_BARS.get(cfg.interval, "1H")
    rows: list[list] = []
    after = ""
    while len(rows) < need:
        params = {"instId": inst, "bar": bar, "limit": "100"}
        if after:
            params["after"] = after
        batch = []
        for attempt in range(2):  # 429/网络抖动重试一次，别整源判死
            try:
                payload = _get(f"{OKX}/api/v5/market/history-candles", params, timeout=10)
                batch = payload.get("data") or []
                break
            except Exception:
                if attempt == 1:
                    raise
                time.sleep(1.0)
        if not batch:
            break
        rows = batch + rows
        after = batch[-1][0]  # 本批最老的一根，下一页取比它更早的
        if len(batch) < 100:
            break
        time.sleep(0.12)  # 限速：OKX history-candles 20 次/2 秒
    if not rows:
        raise DataError("okx 返回空数据")
    df = _okx_rows_to_df(rows)
    df = _attach_okx_funding(df, inst)
    # OKX 的 volume 是「张数」，量纲和币安不同，用成交额近似还原成 base 量
    return df.tail(need)


def _fetch_okx_recent(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    """最近 N 根（含未收盘），给页面短周期图用。"""
    bar = OKX_BARS.get(interval, "5m")
    payload = _get(
        f"{OKX}/api/v5/market/candles",
        {"instId": okx_inst(symbol), "bar": bar, "limit": str(min(max(limit, 20), 300))},
        timeout=8,
    )
    rows = payload.get("data") or []
    if not rows:
        raise DataError("okx candles 返回空数据")
    return _okx_rows_to_df(rows)


def fetch_binance(cfg: MarketConfig, max_bars: int | None = None, prefer: str = "binance") -> pd.DataFrame:
    """按可用性依次尝试：fapi 永续 → demo 合约 → Gate 永续。

    返回的 DataFrame 带 `attrs["source"]`（哪个源成功）与 `attrs["has_funding"]`。
    全部失败才抛 DataError——绝不静默返回合成数据。
    """
    need = max_bars or cfg.bars
    errors: list[str] = []
    # 只做合约：fapi 永续 → demo 合约 → Gate 永续，不再回退现货镜像
    pairs = [
        ("binance-fapi", _fetch_fapi, FAPI),
        ("binance-fapi-demo", _fetch_demo_fapi, DEMO),
        ("gate-futures", _fetch_gate, GATE),
    ]
    # 选的是 OKX 就把 OKX 永续排在第一位；它挂了仍会依次兜底，不会没数据
    if (prefer or "").lower() == "okx":
        pairs.insert(0, ("okx-swap", _fetch_okx, OKX))
    for name, fn, host in pairs:
        if _dead(host):
            errors.append(f"{name}: 上次失败，{int(DEAD_COOLDOWN)}s 内跳过")
            continue
        try:
            df = fn(cfg, need)
            df = df.tail(need)
            df.attrs["source"] = name
            df.attrs["has_funding"] = bool(df["funding_rate"].abs().sum() > 0)
            return df
        except Exception as exc:  # noqa: BLE001 - 换源继续试
            _mark_dead(host)
            errors.append(f"{name}: {type(exc).__name__} {str(exc)[:80]}")
    raise DataError(
        "所有真实行情源都不可达：\n  " + "\n  ".join(errors) + "\n"
        "国内网络通常无法直连 fapi.binance.com，可设置 HTTPS_PROXY；"
        "或把 config 里 market.source 改成 synthetic（明确标注为假数据）。"
    )


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------
OKX = "https://www.okx.com"
BYBIT = "https://api.bybit.com"


def _live_fapi(symbol: str, timeout: int) -> tuple[float, str]:
    # U 本位永续最新成交价 —— 合约交易的价格基准，和持仓标记价最接近
    data = _get(f"{FAPI}/fapi/v1/ticker/price", {"symbol": symbol}, timeout=timeout)
    return float(data["price"]), "binance-fapi"


def _live_demo_fapi(symbol: str, timeout: int) -> tuple[float, str]:
    # 币安合约模拟盘最新价（同样是永续价格，主网不通时兜底）
    data = _get(f"{DEMO}/fapi/v1/ticker/price", {"symbol": symbol}, timeout=timeout)
    return float(data["price"]), "binance-fapi-demo"


def _live_gate(symbol: str, timeout: int) -> tuple[float, str]:
    contract = gate_contract(symbol)
    data = _get(f"{GATE}/api/v4/futures/usdt/tickers", {"contract": contract}, timeout=timeout)
    return float(data[0]["last"]), "gate-futures"


def _live_okx(symbol: str, timeout: int) -> tuple[float, str]:
    inst = okx_live_inst(symbol)
    data = _get(f"{OKX}/api/v5/market/ticker", {"instId": inst}, timeout=timeout)
    return float(data["data"][0]["last"]), "okx"


def _live_bybit(symbol: str, timeout: int) -> tuple[float, str]:
    data = _get(f"{BYBIT}/v5/market/tickers", {"category": "linear", "symbol": symbol}, timeout=timeout)
    return float(data["result"]["list"][0]["lastPrice"]), "bybit"


LIVE_SOURCES = (
    # 只做合约：永续优先，全部为合约价格基准，不再回退现货
    ("binance-fapi", _live_fapi, FAPI),
    ("binance-fapi-demo", _live_demo_fapi, DEMO),
    ("gate-futures", _live_gate, GATE),
    ("okx", _live_okx, OKX),
    ("bybit", _live_bybit, BYBIT),
)


# 实时 K 线：页面切 5m/15m/1h、决策算中频突破都走这里。
# 进程内缓存 → 落盘缓存 → 交易所三级取数（见下面的 KL_CACHE_DIR）。
_KL_CACHE: dict[tuple[str, str, int, str], tuple[float, pd.DataFrame, str]] = {}
KL_TTL = 15.0

# 统一周期集合：所有取数路径（币安 / OKX / Gate / 衍生品）都从这里派生，
# 不再各自维护一份互相打架的白名单。
KL_INTERVALS = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d")
# Gate 的 candlesticks 接口支持的周期（比币安少 3m/2h 等，明确列出而不是静默降级）
GATE_INTERVALS = {"10s", "1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d"}


# --------------------------------------------------------------------------
# 短周期 K 线落盘缓存
# --------------------------------------------------------------------------
# 页面图表每 3 秒轮询一次、每轮决策都要算一次中频突破信号，两个场景都走
# fetch_klines。进程内缓存只有 15 秒，跨 bar 后照样打交易所，进程重启更是
# 一次不剩。这里把最近 N 根落成 CSV：
#   · 同一根 bar 内直接读盘 → 零网络往返、毫秒级返回；
#   · 跨 bar 后整段重拉一次并原子覆盖（一次请求，代价很小）；
#   · 交易所分开存，切了交易所绝不会读到另一家的行情。
# 有效期 = 半个 bar 周期，最长封顶 KL_DISK_TTL_MAX，保证最后一根不至于太旧。
KL_CACHE_DIR = Path("data/cache/klines")
KL_DISK_TTL_MAX = 180.0
# 只有常规请求量才落盘：外部调用方传个 1000 根的一次性请求没必要占一个文件
KL_DISK_MAX_LIMIT = 500


def set_kl_cache_dir(cache_dir: str | Path) -> None:
    """跟随 market.cache_dir，别把 K 线缓存散落在项目根。"""
    global KL_CACHE_DIR
    KL_CACHE_DIR = Path(cache_dir) / "klines"


def kl_disk_ttl(interval: str) -> float:
    """盘缓存有效期（秒）：距上一次写入的时长，半个 bar 周期、封顶 3 分钟、最少 15 秒。

    注意不能用「最后一根 bar 的开盘时刻」来判新旧——15m 周期的当前那根可能
    已经开盘 14 分钟，但它是 30 秒前才拉下来的，完全新鲜。
    下界必须显著小于最短周期的一半：1m 的半个 bar 是 30 秒，若写成 60 秒，
    新 bar 已经开盘 59 秒还会返回上一根。
    """
    span = INTERVAL_HOURS.get(interval, 1.0) * 3600.0
    return max(15.0, min(span * 0.5, KL_DISK_TTL_MAX))


def kl_cache_path(symbol: str, interval: str, limit: int, prefer: str = "binance") -> Path:
    """不同交易所的 K 线分开存，否则切交易所会读到另一家的旧价。"""
    tag = "okx" if (prefer or "").lower() == "okx" else "binance"
    return KL_CACHE_DIR / f"{symbol}_{interval}_{int(limit)}.{tag}.csv"


def _read_kl_disk(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        df.index = pd.to_datetime(df.index, utc=True)
        df.index.name = "ts"
        return df if not df.empty else None
    except Exception:  # noqa: BLE001 - 缓存文件损坏/写了一半，当作没有缓存
        return None


def _write_kl_disk(path: Path, df: pd.DataFrame) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 临时文件名必须唯一：预热线程、页面请求、决策线程可能同时写同一个 key，
        # 固定叫 .tmp 会互相踩踏，replace 时可能把半截内容定为最终文件。
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:12]}.tmp")
        df.to_csv(tmp)
        tmp.replace(path)   # 原子替换：并发读到半截文件会解析失败，宁可不换
    except Exception:  # noqa: BLE001 - 缓存写不进去不影响行情主流程
        try:
            if "tmp" in locals() and tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def fetch_klines(
    symbol: str,
    interval: str = "5m",
    limit: int = 240,
    ttl: float = KL_TTL,
    prefer: str = "binance",
    use_disk: bool = True,
    force: bool = False,
) -> tuple[pd.DataFrame, str]:
    """拉最近 N 根实时 K 线（含当前未收盘的那根）。返回 (df, 来源)。

    三级取数：进程内缓存（ttl，默认 15 秒）→ 落盘 CSV（半个 bar）→ 交易所。

    - use_disk=False：跳过盘缓存，只走内存 + 网络。给「必须确认这家交易所
      真的在线」的场景用（例如添加品种时的连通性校验）。
    - force=True：两级缓存全跳过，强制刷新。给后台预热用。
    """
    interval = (interval or "5m").strip()
    if interval not in KL_INTERVALS:
        raise DataError(f"不支持的周期 {interval}，可选 {', '.join(KL_INTERVALS)}")
    limit = max(20, min(int(limit), 1000))
    prefer = (prefer or "binance").lower()
    if prefer == "okx":
        # OKX 单次最多返回 300 根：limit 更大时，落盘文件永远凑不够
        # `len(cached) >= limit-5` 的命中条件，盘缓存等于失效且每次重写。
        limit = min(limit, 300)
    key = (symbol, interval, limit, prefer)
    now = time.time()
    disk_path = (
        kl_cache_path(symbol, interval, limit, prefer)
        if (use_disk and limit <= KL_DISK_MAX_LIMIT)
        else None
    )
    tag = "okx" if prefer == "okx" else "binance"

    def _ok(df: pd.DataFrame, src: str) -> tuple[pd.DataFrame, str]:
        # 只有「数据确实来自 prefer 指定的交易所」才写入该交易所的缓存。
        # 否则 OKX 挂掉回退币安时，币安行情会被写进 *.okx.csv，之后 15 秒内
        # 读到 disk-cache/okx 却是币安的价，决策与图表基准静默串台。
        same_source = (tag == "okx") == str(src).startswith("okx")
        # 所有来源都保证有 funding_rate 列：白名单允许它，但 short 周期取数
        # 的三条分支都不补，因子会在沙箱里 KeyError。
        if "funding_rate" not in df.columns:
            df["funding_rate"] = 0.0
        if same_source:
            _KL_CACHE[key] = (now, df, src)
            if disk_path is not None:
                _write_kl_disk(disk_path, df)
        return df, src

    if not force:
        hit = _KL_CACHE.get(key)
        if hit and (now - hit[0]) < ttl:
            # 跨所兜底的数据不能拿来充「prefer=okx」的缓存命中
            if (tag == "okx") == str(hit[2]).startswith("okx"):
                return hit[1], hit[2]

        if disk_path is not None:
            cached = _read_kl_disk(disk_path)
            # 条数对不上说明换了 limit 或上次只拉到一半，别拿去充数
            if cached is not None and len(cached) >= max(20, limit - 5):
                # 新鲜度看「文件写入时刻」（mtime 就是我们上次落盘的时间），
                # 不是最后一根 bar 的开盘时刻，理由见 kl_disk_ttl 的说明。
                try:
                    fetched_at = disk_path.stat().st_mtime
                except OSError:
                    fetched_at = 0.0
                if 0 <= (now - fetched_at) < kl_disk_ttl(interval):
                    _KL_CACHE[key] = (now, cached, f"disk-cache/{tag}")
                    return cached, f"disk-cache/{tag}"

    errors: list[str] = []
    # 0) 选了 OKX → 先用 OKX 永续行情，和下单的交易所保持一致
    if prefer == "okx":
        try:
            df = _fetch_okx_recent(symbol, interval, limit)
            return _ok(df, "okx-swap")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"okx: {type(exc).__name__} {str(exc)[:60]}")

    # 1) U 本位永续（合约交易的价格基准，优先）
    if not _dead(FAPI):
        try:
            rows = _get(
                f"{FAPI}/fapi/v1/klines",
                {"symbol": symbol, "interval": interval, "limit": limit},
                timeout=6,
            )
            if rows:
                df = _klines_frame(rows)
                return _ok(df, "binance-fapi")
            errors.append("fapi: 空数据")
        except Exception as exc:  # noqa: BLE001
            _mark_dead(FAPI)
            errors.append(f"fapi: {type(exc).__name__} {str(exc)[:60]}")

    # 2) 币安合约模拟盘（同样是永续价格，主网不通时兜底，不回退现货）
    try:
        rows = _get(
            f"{DEMO}/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
            timeout=6,
        )
        if rows:
            df = _klines_frame(rows)
            return _ok(df, "binance-fapi-demo")
        errors.append("demo-fapi: 空数据")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"demo-fapi: {type(exc).__name__} {str(exc)[:60]}")

    # 3) Gate 永续
    try:
        contract = gate_contract(symbol)
        rows = _get(
            f"{GATE}/api/v4/futures/usdt/candlesticks",
            {"contract": contract, "interval": interval, "limit": limit},
            timeout=6,
        )
        if rows:
            df = pd.DataFrame(rows)
            df.index = pd.to_datetime(df["t"].astype(float), unit="s", utc=True)
            df.index.name = "ts"
            df = df[["o", "h", "l", "c", "v"]].astype(float)
            df.columns = ["open", "high", "low", "close", "volume"]
            df = df.sort_index()
            df = _fill_extra_fields(df)
            return _ok(df, "gate-futures")
        errors.append("gate: 空数据")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"gate: {type(exc).__name__} {str(exc)[:60]}")

    raise DataError("实时 K 线获取失败：\n  " + "\n  ".join(errors))


def fetch_live_price(symbol: str, timeout: int = 6, prefer: str = "binance") -> tuple[float, str]:
    """最新成交价（秒级）。依次试 4 个交易所，返回 (price, source)，全失败才抛错。

    币安镜像在部分地区会返回 451（法律限制），所以必须有备胎。
    """
    errors: list[str] = []
    now = time.time()
    sources = LIVE_SOURCES
    if (prefer or "").lower() == "okx":
        sources = tuple(x for x in LIVE_SOURCES if x[0] == "okx") + tuple(
            x for x in LIVE_SOURCES if x[0] != "okx"
        )
    for name, fn, host in sources:
        if float(_DEAD_LIVE.get(host, 0.0)) > now:
            errors.append(f"{name}: 30s 内失败过，跳过")
            continue
        try:
            price, _src = fn(symbol, timeout)
            if price and price > 0:
                return float(price), name
            errors.append(f"{name}: 返回非正数")
        except Exception as exc:  # noqa: BLE001
            _DEAD_LIVE[host] = time.time() + LIVE_COOLDOWN
            errors.append(f"{name}: {type(exc).__name__} {str(exc)[:80]}")
    raise DataError("实时报价获取失败：\n  " + "\n  ".join(errors))


# 24h 涨跌幅：顶栏显示用，权重 40（fapi ticker/24hr），60 秒缓存
_24H_CACHE: dict[str, tuple[float, tuple[float, float]]] = {}
_24H_TTL = 60.0


def fetch_24h(symbol: str, timeout: int = 6, prefer: str = "binance") -> tuple[float, float]:
    """返回 (最新价, 24h 涨跌幅%)，多源回退，60 秒进程内缓存。"""
    prefer = (prefer or "binance").lower()
    key = (symbol, prefer)
    now = time.time()
    hit = _24H_CACHE.get(key)
    if hit and (now - hit[0]) < _24H_TTL:
        return hit[1]

    errors: list[str] = []
    # 0) 选了 OKX → 用 OKX 永续的 24h 统计，和价格同一个交易所
    if prefer == "okx":
        try:
            payload = _get(
                f"{OKX}/api/v5/market/ticker", {"instId": okx_inst(symbol)}, timeout=timeout
            )
            item = payload["data"][0]
            last = float(item["last"])
            open24 = float(item.get("open24h") or 0.0)
            chg = ((last - open24) / open24 * 100.0) if open24 else 0.0
            _24H_CACHE[key] = (now, (last, chg))
            return last, chg
        except Exception as exc:  # noqa: BLE001
            errors.append(f"okx: {type(exc).__name__} {str(exc)[:60]}")

    # 1) 币安永续 24hr
    try:
        data = _get(f"{FAPI}/fapi/v1/ticker/24hr", {"symbol": symbol}, timeout=timeout)
        out = (float(data["lastPrice"]), float(data["priceChangePercent"]))
        _24H_CACHE[key] = (now, out)
        return out
    except Exception as exc:  # noqa: BLE001
        errors.append(f"fapi: {type(exc).__name__} {str(exc)[:60]}")

    # 2) 币安现货 24hr
    try:
        data = _get(f"{VISION}/api/v3/ticker/24hr", {"symbol": symbol}, timeout=timeout)
        out = (float(data["lastPrice"]), float(data["priceChangePercent"]))
        _24H_CACHE[key] = (now, out)
        return out
    except Exception as exc:  # noqa: BLE001
        errors.append(f"spot: {type(exc).__name__} {str(exc)[:60]}")

    # 3) Gate 永续
    try:
        contract = gate_contract(symbol)
        data = _get(f"{GATE}/api/v4/futures/usdt/tickers", {"contract": contract}, timeout=timeout)
        item = data[0]
        out = (float(item["last"]), float(item.get("change_percentage", 0.0)))
        _24H_CACHE[key] = (now, out)
        return out
    except Exception as exc:  # noqa: BLE001
        errors.append(f"gate: {type(exc).__name__} {str(exc)[:60]}")

    raise DataError("24h 行情获取失败：\n  " + "\n  ".join(errors))


def load_market(
    cfg: MarketConfig,
    force_download: bool = False,
    strict: bool = False,
    max_age_bars: int = 2,
    prefer: str = "binance",
) -> tuple[pd.DataFrame, str]:
    """返回 (行情, 来源说明)。

    - 真实缓存与合成缓存**分开存放**，合成数据永远不会冒充真实行情。
    - 真实缓存过期（最后一根 K 线落后超过 max_age_bars 根）会尝试重新下载。
    - cfg.source == "synthetic" 时直接用合成数据，来源字符串明确带 `synthetic`。
    - strict=True（下载命令）时，下载失败直接抛错，不回退。
    """
    cache_dir = Path(cfg.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{cfg.symbol}_{cfg.interval}_{cfg.bars}"
    # 不同交易所的行情分开缓存，免得切了交易所还在读币安的旧 CSV
    tag = ".okx" if (prefer or "").lower() == "okx" else ""
    real_cache = cache_dir / f"{stem}{tag}.real.csv"
    synth_cache = cache_dir / f"{stem}_s{cfg.seed}.synthetic.csv"

    def _read(path: Path) -> pd.DataFrame | None:
        try:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            df.index = pd.to_datetime(df.index, utc=True)
            return df
        except Exception:  # noqa: BLE001 - 缓存损坏/写了一半，当作没有缓存
            return None

    def _stale(df: pd.DataFrame) -> bool:
        if df.empty:
            return True
        now = pd.Timestamp.now(tz="UTC")
        hours = INTERVAL_HOURS.get(cfg.interval, 1.0)
        return (now - df.index[-1]).total_seconds() > max_age_bars * hours * 3600

    if cfg.source == "binance":
        if real_cache.exists() and not force_download:
            df = _read(real_cache)
            if df is not None and not _stale(df):
                kind = "okx-cache" if tag else "cache"
                return df, f"{kind}:{cfg.symbol} {cfg.interval} 末根 {df.index[-1]:%Y-%m-%d %H:%M}Z"
        try:
            df = fetch_binance(cfg, prefer=prefer)
            src = df.attrs.get("source", "binance")
            # 兜底源（币安/Gate）的数据别写进 OKX 专用缓存，否则下次会拿别人的行情当 OKX
            if not (tag and not str(src).startswith("okx")):
                # 原子写：预热线程与页面/决策线程可能同时读写同一份缓存，
                # 直接 to_csv 会让读方解析到半截文件（EmptyDataError）。
                _write_kl_disk(real_cache, df)
            note = "" if df.attrs.get("has_funding") else "（无资金费率，按 0 计）"
            return df, f"{src}:{cfg.symbol} {cfg.interval}{note}"
        except DataError as exc:
            if strict:
                raise
            if real_cache.exists():
                df = _read(real_cache)
                print(f"[data] 刷新失败，沿用旧缓存（末根 {df.index[-1]}）：{exc}")
                return df, f"stale-cache:{cfg.symbol} 末根 {df.index[-1]:%Y-%m-%d %H:%M}Z"
            raise

    # 合成行情：写独立文件，永不覆盖真实缓存
    df = synthetic_ohlcv(cfg.bars, cfg.seed, interval=cfg.interval)
    _write_kl_disk(synth_cache, df)
    return df, f"synthetic(假数据,seed={cfg.seed})"


def split_market(
    df: pd.DataFrame, train_frac: float = 0.55, valid_frac: float = 0.20
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """按时间切分 train / valid / test，绝不随机打乱（防止未来信息泄漏）。"""
    n = len(df)
    a = int(n * train_frac)
    b = int(n * (train_frac + valid_frac))
    return df.iloc[:a], df.iloc[a:b], df.iloc[b:]