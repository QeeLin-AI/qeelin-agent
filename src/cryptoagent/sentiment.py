"""市场情绪 / 新闻源（确定性降级兜底）。

本项目只依赖 Python 标准库，并且遵循「确定性降级」哲学：任何一次对外部
数据的依赖，都必须能在无网络、无 API Key 或调用失败时，平滑降级到一条
本地规则，且绝不抛出异常。

本模块提供两套情绪源：
- PriceActionSentiment ：纯本地代理（情绪面代理）。不依赖任何外部服务，
  仅根据行情快照（涨跌幅、波动率、RSI）推导一个代理情绪分。永远可用，
  也是其它源失败时的兜底。
- CryptoPanicSentiment ：可选的真实新闻源。读取环境变量中的 API Key，
  调用 CryptoPanic 公开 API 抓取头条并做关键词打分。当 Key 缺失或任何
  网络 / 解析出错时，自动降级为 PriceActionSentiment，并标注 fresh=False。

对外统一通过 get_sentiment_source(cfg) 拿到一个 SentimentSource 实例，
调用方只管 fetch(symbol, snapshot)，拿到的永远是一个合法的 SentimentResult。
"""

from __future__ import annotations

import abc
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import SentimentConfig
from .netutil import open_url


# --------------------------------------------------------------------------
# 配置与结果
# --------------------------------------------------------------------------
# 配置类型单点定义：AppConfig 用的是 config.SentimentConfig，这里不再重复一份。


@dataclass
class SentimentResult:
    """一次情绪分析的结果，所有字段均为合法值，调用方无需再做空值保护。"""

    score: float  # 情绪分，夹逼到 [-1.0, 1.0]；+1 强烈看多，-1 强烈看空
    label: str  # 中文标签：看涨 / 看跌 / 中性（由 score 派生）
    headlines: list[str]  # 头条短句，可能为空
    source: str  # 实际产出该结果的适配器名
    fresh: bool  # True=本次来自真实源；False=降级兜底
    note: str  # 人类可读说明，例如「数据缺失，使用价格行为代理」


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def _clamp_score(score: float) -> float:
    """将情绪分夹逼到 [-1.0, 1.0]。"""
    if math.isnan(score) or math.isinf(score):
        return 0.0
    return max(-1.0, min(1.0, score))


def _label_from_score(score: float) -> str:
    """由情绪分派生中文标签。"""
    if score > 0.15:
        return "看涨"
    if score < -0.15:
        return "看跌"
    return "中性"


def _safe_attr(snapshot: object, name: str, default: float) -> float:
    """安全读取快照属性，缺失或非数字时回退到 default。"""
    try:
        val = getattr(snapshot, name, None)
    except Exception:  # noqa: BLE001
        return default
    if val is None:
        return default
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


# --------------------------------------------------------------------------
# 抽象基类
# --------------------------------------------------------------------------
class SentimentSource(abc.ABC):
    """情绪源抽象基类。

    所有具体实现都必须保证 fetch 不抛出异常；任何失败都要转化为一个
    带 fresh=False 的兜底结果。
    """

    def __init__(self, cfg: SentimentConfig | None = None) -> None:
        self.cfg = cfg or SentimentConfig()

    @abc.abstractmethod
    def fetch(self, symbol: str, snapshot: object = None) -> SentimentResult:
        """拉取 symbol 的情绪结果。

        snapshot 为可选对象，预期暴露 ret_1 / ret_24 / vol_24 / rsi_14 属性；
        子类必须容忍 snapshot 为 None 或属性缺失。
        """
        raise NotImplementedError


# --------------------------------------------------------------------------
# 价格行为代理（始终可用的兜底）
# --------------------------------------------------------------------------
class PriceActionSentiment(SentimentSource):
    """价格行为情绪代理。

    在没有任何真实新闻源时，仅依据行情快照推导代理情绪：
    - 动量：24h 收益率按波动率归一化（越大越看多）；
    - RSI 极值：RSI>72 视为超买，轻微看空（均值回归）；RSI<28 视为超卖，轻微看多；
    - 短期：1h 收益率给出方向性微调。
    最终以 tanh 压缩组合后夹逼到 [-1, 1]。source="price_action"，fresh=False。
    """

    def fetch(self, symbol: str, snapshot: object = None) -> SentimentResult:
        ret_1 = _safe_attr(snapshot, "ret_1", 0.0)
        ret_24 = _safe_attr(snapshot, "ret_24", 0.0)
        vol_24 = _safe_attr(snapshot, "vol_24", 0.0)
        rsi_14 = _safe_attr(snapshot, "rsi_14", 50.0)

        # 波动率兜底为一个很小的值，避免除零
        vol = vol_24 if vol_24 > 1e-6 else 1e-3

        # 动量项：24h 收益按波动率归一化
        momentum = ret_24 / vol

        # RSI 均值回归项：超买偏空，超卖偏多，幅度温和
        rsi_term = 0.0
        if rsi_14 > 72.0:
            rsi_term = -0.25
        elif rsi_14 < 28.0:
            rsi_term = 0.25

        # 短期方向微调
        short_term = 0.5 * ret_1

        raw = momentum + rsi_term + short_term
        score = _clamp_score(math.tanh(raw))

        note = "数据缺失，使用价格行为代理" if snapshot is None else "价格行为代理（本地规则）"
        return SentimentResult(
            score=score,
            label=_label_from_score(score),
            headlines=[],
            source="price_action",
            fresh=False,
            note=note,
        )


# --------------------------------------------------------------------------
# 真实新闻源（可降级）
# --------------------------------------------------------------------------
_BULL_WORDS = [
    "涨", "突破", "买入", "利好", "新高", "看涨", "牛市", "拉升", "approval",
    "breakout", "bull", "surge", "rally", "gain", "soar",
]
_BEAR_WORDS = [
    "跌", "暴跌", "做空", "利空", "抛售", "看跌", "熊市", "crash", "bear",
    "sec", "lawsuit", "dump", "selloff", "plunge", "ban",
]


def _word_hit(word: str, low: str, words: set[str]) -> bool:
    """英文词按「整词 + 常见词形」匹配，中文词仍按子串。

    子串匹配会把 "second" 里的 "sec"、"bank" 里的 "ban"、"against" 里的
    "gain" 全算成情绪词，实测一条中性标题会被打成 -0.24。
    """
    w = word.lower()
    if not w.isascii():
        return w in low
    return any(form in words for form in (w, w + "s", w + "es", w + "ed", w + "ing"))


def _score_headline(text: str) -> float:
    """对单个标题做简单关键词打分，返回大致落在 [-1, 1] 的局部分。"""
    low = text.lower()
    words = set(re.findall(r"[a-z]+", low))
    bull = sum(1 for w in _BULL_WORDS if _word_hit(w, low, words))
    bear = sum(1 for w in _BEAR_WORDS if _word_hit(w, low, words))
    if bull == 0 and bear == 0:
        return 0.0
    # 净多空比例，用 tanh 压缩
    return math.tanh((bull - bear) / 4.0)


_SENT_CACHE: dict[tuple[str, str], tuple[float, SentimentResult]] = {}


class CryptoPanicSentiment(SentimentSource):
    """CryptoPanic 新闻情绪源（可选真实源）。

    读取 cfg.cryptopanic_api_key_env 指定的环境变量获取 Key：
    - 无 Key          -> 降级为 PriceActionSentiment；
    - 有 Key          -> 调用 CryptoPanic API，抓取头条做关键词打分；
    - 任何异常（网络/HTTP/JSON）-> 捕获并降级，绝不抛出。
    """

    _BASE_URL = "https://cryptopanic.com/api/v1/posts/"

    def fetch(self, symbol: str, snapshot: object = None) -> SentimentResult:
        api_key = os.getenv(self.cfg.cryptopanic_api_key_env, "")
        if not api_key:
            return PriceActionSentiment(self.cfg).fetch(symbol, snapshot)

        # 归一化交易对符号（CryptoPanic 用不带 USDT 的基础币，如 BTC）
        base = symbol.upper().replace("USDT", "").replace("USD", "").strip() or symbol.upper()
        # 按品种缓存：cache_ttl_sec 之前是死字段，每轮决策都真实打一次新闻源。
        cache_key = ("cryptopanic", base)
        ttl = float(getattr(self.cfg, "cache_ttl_sec", 600) or 600)
        hit = _SENT_CACHE.get(cache_key)
        if hit and (time.time() - float(hit[0])) < ttl:
            return hit[1]
        url = f"{self._BASE_URL}?auth_token={api_key}&currencies={base}&kind=news"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "crypto-agent/1.0"})
            with open_url(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            results = payload.get("results") or []
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, json.JSONDecodeError):
            # 任何失败都降级，绝不抛出
            fallback = PriceActionSentiment(self.cfg).fetch(symbol, snapshot)
            fallback.note = "CryptoPanic 拉取失败，降级为价格行为代理"
            return fallback

        headlines: list[str] = []
        scores: list[float] = []
        for item in results[:8]:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            headlines.append(title)
            scores.append(_score_headline(title))

        if not scores:
            fallback = PriceActionSentiment(self.cfg).fetch(symbol, snapshot)
            fallback.note = "CryptoPanic 无有效头条，降级为价格行为代理"
            return fallback

        avg = sum(scores) / len(scores)
        score = _clamp_score(avg)
        result = SentimentResult(
            score=score,
            label=_label_from_score(score),
            headlines=headlines,
            source="cryptopanic",
            fresh=True,
            note=f"来自 CryptoPanic 的 {len(headlines)} 条头条",
        )
        _SENT_CACHE[cache_key] = (time.time(), result)
        return result


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------
def get_sentiment_source(cfg: SentimentConfig) -> SentimentSource:
    """根据配置返回合适的情绪源实例。

    - source=="cryptopanic" 且 enabled   -> CryptoPanicSentiment
    - 其它情况（含显式 price_action / 禁用 / 未知 source） -> PriceActionSentiment
    """
    if cfg.source == "cryptopanic" and cfg.enabled:
        return CryptoPanicSentiment(cfg)
    return PriceActionSentiment(cfg)


# --------------------------------------------------------------------------
# 演示
# --------------------------------------------------------------------------
if __name__ == "__main__":
    # 构造一个类快照对象用于演示（无需依赖项目的 MarketSnapshot）
    class FakeSnapshot:
        ret_1: float = 0.012
        ret_24: float = 0.045
        vol_24: float = 0.03
        rsi_14: float = 68.0

    src = PriceActionSentiment(SentimentConfig())
    result = src.fetch("BTCUSDT", FakeSnapshot())
    print("source   :", result.source)
    print("score    :", round(result.score, 4))
    print("label    :", result.label)
    print("fresh    :", result.fresh)
    print("headlines:", result.headlines)
    print("note     :", result.note)

    # 验证工厂在默认配置下也回退到价格行为代理
    print("\n--- factory default ---")
    default_src = get_sentiment_source(SentimentConfig())
    print("type     :", type(default_src).__name__)
    print("result   :", default_src.fetch("ETHUSDT", None))
