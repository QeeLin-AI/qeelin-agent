"""内置因子库。

每个因子都是一段独立可执行的代码字符串，和 Agent 生成的因子走同一条
沙箱链路。这样做的好处：内置因子本身就是「参考实现」，LLM 可以照着写。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BuiltinFactor:
    name: str
    family: str
    hypothesis: str
    code: str


LIBRARY: dict[str, BuiltinFactor] = {
    "momentum_24": BuiltinFactor(
        name="momentum_24",
        family="momentum",
        hypothesis="24 小时动量：趋势具有持续性，过去涨的接下来倾向继续涨。",
        code='''def factor(df):
    return df["close"] / df["close"].shift(24) - 1.0
''',
    ),
    "momentum_96": BuiltinFactor(
        name="momentum_96",
        family="momentum",
        hypothesis="4 日动量：更长的窗口过滤噪音，捕捉中期趋势。",
        code='''def factor(df):
    return df["close"] / df["close"].shift(96) - 1.0
''',
    ),
    "reversal_6": BuiltinFactor(
        name="reversal_6",
        family="reversal",
        hypothesis="6 小时反转：短期超买超卖会均值回归。",
        code='''def factor(df):
    return -(df["close"] / df["close"].shift(6) - 1.0)
''',
    ),
    "volatility_48": BuiltinFactor(
        name="volatility_48",
        family="volatility",
        hypothesis="低波动溢价：波动率低的时段风险调整后收益更好。",
        code='''def factor(df):
    ret = df["close"] / df["close"].shift(1) - 1.0
    return -ret.rolling(48).std()
''',
    ),
    "funding_carry": BuiltinFactor(
        name="funding_carry",
        family="carry",
        hypothesis="资金费率均值回复：费率极端为正说明多头拥挤，反向操作。",
        code='''def factor(df):
    return -df["funding_rate"].rolling(9).mean()
''',
    ),
    "breakout_96": BuiltinFactor(
        name="breakout_96",
        family="breakout",
        hypothesis="区间突破：价格接近近期高点时趋势会延续。",
        code='''def factor(df):
    hi = df["close"].rolling(96).max()
    lo = df["close"].rolling(96).min()
    span = (hi - lo).replace(0.0, np.nan)
    return (df["close"] - lo) / span - 0.5
''',
    ),
    "trend_sma_72": BuiltinFactor(
        name="trend_sma_72",
        family="trend",
        hypothesis="均线偏离：价格相对长期均线的偏离度反映趋势强度。",
        code='''def factor(df):
    sma = df["close"].rolling(72).mean()
    return df["close"] / sma - 1.0
''',
    ),
    "volume_momentum": BuiltinFactor(
        name="volume_momentum",
        family="volume",
        hypothesis="放量突破更可靠：动量信号乘以成交量 z-score 加权。",
        code='''def factor(df):
    ret = df["close"] / df["close"].shift(24) - 1.0
    vol_mean = df["volume"].rolling(96).mean()
    vol_std = df["volume"].rolling(96).std().replace(0.0, np.nan)
    vol_z = (df["volume"] - vol_mean) / vol_std
    return ret * vol_z
''',
    ),
    "rsi_14": BuiltinFactor(
        name="rsi_14",
        family="reversal",
        hypothesis="RSI(14) 超买超卖反转：超卖反弹、超买回落。",
        code='''def factor(df):
    d = df["close"].diff()
    up = d.clip(lower=0).rolling(14).mean()
    dn = (-d.clip(upper=0)).rolling(14).mean().replace(0.0, np.nan)
    rs = up / dn
    rsi = 100 - 100 / (1 + rs)
    return -(rsi - 50)
''',
    ),
    "macd_12_26": BuiltinFactor(
        name="macd_12_26",
        family="momentum",
        hypothesis="MACD 柱状图动量：DIF-DEA 为正说明短期动能强于长期。",
        code='''def factor(df):
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    return dif - dea
''',
    ),
    "bollinger_20": BuiltinFactor(
        name="bollinger_20",
        family="reversal",
        hypothesis="布林带均值回归：价格贴近下轨超卖，贴近上轨超买。",
        code='''def factor(df):
    mid = df["close"].rolling(20).mean()
    sd = df["close"].rolling(20).std()
    up = mid + 2 * sd
    lo = mid - 2 * sd
    span = (up - lo).replace(0.0, np.nan)
    return -((df["close"] - lo) / span - 0.5)
''',
    ),
    "atr_breakout_14": BuiltinFactor(
        name="atr_breakout_14",
        family="breakout",
        hypothesis="ATR 标准化突破：14 根涨幅除以 ATR，过滤噪音后的真实动量。",
        code='''def factor(df):
    h = df["high"]
    l = df["low"]
    c = df["close"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().replace(0.0, np.nan)
    return (c - c.shift(14)) / atr
''',
    ),
    "volatility_cluster_24": BuiltinFactor(
        name="volatility_cluster_24",
        family="volatility",
        hypothesis="波动率聚集：低波动时段风险调整后收益更高（负绝对收益均值）。",
        code='''def factor(df):
    ret = df["close"] / df["close"].shift(1) - 1.0
    return -ret.abs().rolling(24).mean()
''',
    ),
    "volume_pressure_24": BuiltinFactor(
        name="volume_pressure_24",
        family="volume",
        hypothesis="买卖压力：上涨量减下跌量占比，正=主动买盘占优。",
        code='''def factor(df):
    ret = df["close"] / df["close"].shift(1) - 1.0
    upv = df["volume"] * (ret >= 0)
    dnv = df["volume"] * (ret < 0)
    tot = df["volume"].rolling(24).sum().replace(0.0, np.nan)
    return (upv.rolling(24).sum() - dnv.rolling(24).sum()) / tot
''',
    ),
    "candle_body_10": BuiltinFactor(
        name="candle_body_10",
        family="structure",
        hypothesis="K 线实体方向：阳线实体越大，短线多头力量越强。",
        code='''def factor(df):
    rng = (df["high"] - df["low"]).replace(0.0, np.nan)
    return (df["close"] - df["open"]) / rng
''',
    ),
    "upper_shadow_9": BuiltinFactor(
        name="upper_shadow_9",
        family="structure",
        hypothesis="上影线反转：上影线越长说明上方抛压越重，倾向回落。",
        code='''def factor(df):
    rng = (df["high"] - df["low"]).replace(0.0, np.nan)
    up = (df["high"] - df[["open", "close"]].max(axis=1)) / rng
    return -up.rolling(9).mean()
''',
    ),
    "funding_momentum": BuiltinFactor(
        name="funding_momentum",
        family="carry",
        hypothesis="资金费率动量：费率快速上升=多头拥挤加剧，反向做空。",
        code='''def factor(df):
    return -df["funding_rate"].diff().rolling(9).mean()
''',
    ),
    "donchian_96": BuiltinFactor(
        name="donchian_96",
        family="breakout",
        hypothesis="唐奇安通道位置：价格突破 96 根高/低通道后趋势延续。",
        code='''def factor(df):
    hi = df["high"].rolling(96).max()
    lo = df["low"].rolling(96).min()
    span = (hi - lo).replace(0.0, np.nan)
    return (df["close"] - lo) / span - 0.5
''',
    ),
    "stochastic_14": BuiltinFactor(
        name="stochastic_14",
        family="reversal",
        hypothesis="随机指标反转：%K 处于低位超卖、高位超买。",
        code='''def factor(df):
    ll = df["low"].rolling(14).min()
    hh = df["high"].rolling(14).max()
    span = (hh - ll).replace(0.0, np.nan)
    k = (df["close"] - ll) / span
    return -(k - 0.5)
''',
    ),
    "acceleration_6_12": BuiltinFactor(
        name="acceleration_6_12",
        family="momentum",
        hypothesis="动量加速度：近 6 根动量相对前 6 根加速，捕捉趋势拐点。",
        code='''def factor(df):
    m1 = df["close"] / df["close"].shift(6) - 1.0
    m2 = df["close"].shift(6) / df["close"].shift(12) - 1.0
    return m1 - m2
''',
    ),
    "mfi_14": BuiltinFactor(
        name="mfi_14",
        family="volume",
        hypothesis="资金流量指标 MFI(14)：成交量加权的超买超卖。",
        code='''def factor(df):
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    flow = tp * df["volume"]
    d = tp.diff()
    pos = flow.where(d >= 0, 0.0).rolling(14).sum()
    neg = flow.where(d < 0, 0.0).rolling(14).sum().replace(0.0, np.nan)
    mfi = 100 - 100 / (1 + pos / neg)
    return -(mfi - 50)
''',
    ),
}


def get_factor(name: str) -> BuiltinFactor:
    if name not in LIBRARY:
        raise KeyError(f"未知因子 {name}，可选: {', '.join(LIBRARY)}")
    return LIBRARY[name]