"""衍生品数据对齐：把基差、持仓量、taker 多空比、账户多空比接到因子 DataFrame 上。

币安 U 本位合约的公开数据端点（新加坡服务器实测全部 HTTP 200）：

- /fapi/v1/premiumIndexKlines  → 溢价率 K 线，第 4 列是基差率
- /futures/data/openInterestHist → sumOpenInterest
- /futures/data/takerlongshortRatio → buySellRatio（主动买卖比）
- /futures/data/globalLongShortAccountRatio → longShortRatio（账户多空比）

设计要点：
- **尽力而为**：任一端点不通只丢对应那一列，绝不抛异常阻断决策；
  缺列时 signal 层会自动把依赖它的因子整体跳过。
- **进程内缓存 5 分钟**：自动循环每 5 分钟一轮，没有必要每轮打 4 个接口。
- 只在引擎的主行情刷新路径上对齐（机会扫描 6 品种不拉，避免 24 次请求）。
- 用的是主网公开行情；demo/testnet 交易时它是近似值，仅作因子输入，不参与下单计算。
"""
from __future__ import annotations

import logging
import time

import pandas as pd

from .data import FAPI, _get

_log = logging.getLogger(__name__)

# 币安 /futures/data/* 只支持 5m 及以上；1m/3m 会按 .get 的默认值 1h 近似。
# 8h 没有对应周期，就近取 12h —— 只影响衍生品列的对齐粒度，不影响 K 线本身。
DERIV_PERIODS = {
    "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "2h": "2h",
    "4h": "4h", "6h": "6h", "8h": "12h", "12h": "12h", "1d": "1d",
}

_CACHE: dict[tuple, tuple[float, dict]] = {}
DEFAULT_TTL = 300.0


def clear_cache() -> None:
    """清空 5 分钟缓存。产品路径不调用，仅供测试隔离用例。"""
    _CACHE.clear()


def _series(df_index: pd.DatetimeIndex, rows: list, ts_of, val_of) -> pd.Series | None:
    """把 [{timestamp, value}] 对齐到行情索引（前向填充，不用未来数据）。"""
    if not rows:
        return None
    stamps, values = [], []
    for r in rows:
        try:
            ts = pd.to_datetime(int(ts_of(r)), unit="ms", utc=True)
            v = float(val_of(r))
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        stamps.append(ts)
        values.append(v)
    if not stamps:
        return None
    s = pd.Series(values, index=pd.DatetimeIndex(stamps)).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    combined = s.index.union(df_index)
    return s.reindex(combined).ffill().reindex(df_index)


def _fetch_all(symbol: str, interval: str, limit: int, base: str = FAPI) -> dict[str, tuple]:
    period = DERIV_PERIODS.get(interval, "1h")
    out: dict[str, tuple] = {}
    endpoints = [
        ("basis", f"{base}/fapi/v1/premiumIndexKlines",
         {"symbol": symbol, "interval": interval, "limit": limit},
         lambda r: r[0], lambda r: r[4]),
        ("open_interest", f"{base}/futures/data/openInterestHist",
         {"symbol": symbol, "period": period, "limit": limit},
         lambda r: r["timestamp"], lambda r: r["sumOpenInterest"]),
        ("taker_ratio", f"{base}/futures/data/takerlongshortRatio",
         {"symbol": symbol, "period": period, "limit": limit},
         lambda r: r["timestamp"], lambda r: r["buySellRatio"]),
        ("ls_ratio", f"{base}/futures/data/globalLongShortAccountRatio",
         {"symbol": symbol, "period": period, "limit": limit},
         lambda r: r["timestamp"], lambda r: r["longShortRatio"]),
    ]
    for name, url, params, ts_of, val_of in endpoints:
        try:
            rows = _get(url, params, timeout=3)
            if isinstance(rows, list) and rows:
                out[name] = (rows, ts_of, val_of)
        except Exception as exc:  # noqa: BLE001 - 单个维度失败不影响其它维度
            _log.debug("衍生品维度 %s 拉取失败：%s", name, type(exc).__name__)
            continue
    return out


def _cached(symbol: str, interval: str, limit: int, ttl: float) -> dict:
    key = (symbol, interval, limit)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        data = _fetch_all(symbol, interval, limit)
    except Exception:  # noqa: BLE001
        data = {}
    # 全失败时让缓存约 ttl/5 后过期（本机 fapi 不通时不要每轮白等超时）
    stamp = time.time() if data else time.time() - ttl * 0.8
    _CACHE[key] = (stamp, data)
    return data


def enrich_derivatives(
    df: pd.DataFrame,
    symbol: str,
    interval: str = "1h",
    enabled: bool = True,
    ttl: float = DEFAULT_TTL,
) -> pd.DataFrame:
    """给行情 DataFrame 附加 basis / open_interest / taker_ratio / ls_ratio 四列。

    返回新的 DataFrame（不改动传入对象）。任何异常都退化为原样返回。
    """
    if not enabled or df is None or len(df) == 0 or not symbol:
        return df
    try:
        idx = pd.DatetimeIndex(df.index)
        limit = max(50, min(500, len(df)))
        raw = _cached(symbol, interval, limit, ttl)
        if not raw:
            return df
        added: dict[str, pd.Series] = {}
        # 覆盖度按「实际请求到的窗口」算，不能用整段 df 的长度：
        # 主行情 6000 根、衍生品最多 500 根时，len(idx)//10 = 600 永远达不到，
        # 四列会被整体丢弃（默认配置下就是这种情况）。
        window = max(1, min(len(idx), limit))
        min_points = max(20, window // 10)
        for col, (rows, ts_of, val_of) in raw.items():
            s = _series(idx, rows, ts_of, val_of)
            if s is not None and int(s.notna().sum()) >= min_points:
                added[col] = pd.Series(s.to_numpy(dtype=float), index=df.index)
        if not added:
            return df
        out = df.copy()
        for col, s in added.items():
            out[col] = s
        return out
    except Exception:  # noqa: BLE001 - 衍生品数据是增强项，绝不阻断主流程
        return df
