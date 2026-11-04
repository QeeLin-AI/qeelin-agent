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
    # 是否经过 train/valid/test 三段验证。新增的学术研究因子默认 False，
    # 在 signal.load_factor_files 里按 0.25 权重参与（而不是和老因子一样 1.0），
    # 避免「论文说有效」就直接主导实盘信号。跑过 validate_candidates 后再改成 True。
    validated: bool = True


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

    # ------------------------------------------------------------------
    # A 档：只依赖 OHLCV 的学术/微观结构因子（zero-cost，无需新数据源）
    # 文献依据：Crypto factor zoo 里主导 alpha 的是换手、流动性、波动结构，
    # 而不是 MACD/RSI 的同质变体。这些都经过未来函数检查，但尚未跑 OOS 验证，
    # 因此 validated=False（权重 0.25），验证通过后才升到 1.0。
    # ------------------------------------------------------------------
    "turnover_volatility": BuiltinFactor(
        name="turnover_volatility",
        family="liquidity",
        hypothesis="换手率波动率：换手忽高忽低说明参与者不稳定，风险调整后收益更差（低波动溢价）。",
        code='''def factor(df):
    to = df["volume"] / df["volume"].rolling(96).mean().replace(0.0, np.nan)
    return -to.rolling(48).std()
''',
        validated=False,
    ),
    "amihud_illiquidity": BuiltinFactor(
        name="amihud_illiquidity",
        family="liquidity",
        hypothesis="Amihud 非流动性：单位成交额推动的价格越大，流动性越差，需要更高的预期收益补偿。",
        code='''def factor(df):
    ret = (df["close"] / df["close"].shift(1) - 1.0).abs()
    illiq = ret / df["quote_volume"].replace(0.0, np.nan)
    return illiq.rolling(48).mean()
''',
        validated=False,
    ),
    "variance_ratio": BuiltinFactor(
        name="variance_ratio",
        family="structure",
        hypothesis="方差比 VR(24)>1 表示趋势持续，<1 表示均值回归；用它给动量调方向。",
        code='''def factor(df):
    ret = df["close"] / df["close"].shift(1) - 1.0
    var1 = ret.rolling(96).var()
    varq = ret.rolling(24).sum().rolling(96).var()
    vr = varq / (24.0 * var1.replace(0.0, np.nan))
    mom = df["close"] / df["close"].shift(24) - 1.0
    return (vr - 1.0) * mom
''',
        validated=False,
    ),
    "kaufman_efficiency": BuiltinFactor(
        name="kaufman_efficiency",
        family="trend",
        hypothesis="Kaufman 效率比：净位移/路径长度，趋势越干净动量越可靠。",
        code='''def factor(df):
    ch = df["close"].diff()
    net = (df["close"] - df["close"].shift(24)).abs()
    path = ch.abs().rolling(24).sum().replace(0.0, np.nan)
    mom = df["close"] / df["close"].shift(24) - 1.0
    return (net / path) * mom
''',
        validated=False,
    ),
    "downside_upside_vol": BuiltinFactor(
        name="downside_upside_vol",
        family="volatility",
        hypothesis="下行/上行波动比：下行波动占优是崩盘风险前兆，减仓信号。",
        code='''def factor(df):
    ret = df["close"] / df["close"].shift(1) - 1.0
    up = ret.clip(lower=0).rolling(48).std()
    dn = (-ret.clip(upper=0)).rolling(48).std()
    return -(dn / up.replace(0.0, np.nan))
''',
        validated=False,
    ),
    "return_skew": BuiltinFactor(
        name="return_skew",
        family="structure",
        hypothesis="已实现偏度：正偏（彩票型收益）之后往往均值回归。",
        code='''def factor(df):
    ret = df["close"] / df["close"].shift(1) - 1.0
    return -ret.rolling(96).skew()
''',
        validated=False,
    ),
    "return_kurtosis": BuiltinFactor(
        name="return_kurtosis",
        family="structure",
        hypothesis="已实现峰度：厚尾意味着跳空/强平风险，降低敞口。",
        code='''def factor(df):
    ret = df["close"] / df["close"].shift(1) - 1.0
    return -ret.rolling(96).kurt()
''',
        validated=False,
    ),
    "parkinson_vol": BuiltinFactor(
        name="parkinson_vol",
        family="volatility",
        hypothesis="Parkinson 极差波动：用高低价估计波动，比收盘价波动更高效。",
        code='''def factor(df):
    hl = np.log(df["high"] / df["low"].replace(0.0, np.nan))
    pv = (hl * hl).rolling(48).mean() / (4.0 * np.log(2.0))
    return -pv
''',
        validated=False,
    ),
    "garman_klass_vol": BuiltinFactor(
        name="garman_klass_vol",
        family="volatility",
        hypothesis="Garman-Klass 波动：同时用开高低收，效率高于 Parkinson。",
        code='''def factor(df):
    c = df["close"]
    o = df["open"].replace(0.0, np.nan)
    h = df["high"]
    l = df["low"].replace(0.0, np.nan)
    hl = 0.5 * np.log(h / l) ** 2
    co = (2.0 * np.log(2.0) - 1.0) * np.log(c / o) ** 2
    return -(hl - co).rolling(48).mean()
''',
        validated=False,
    ),
    "vwap_deviation": BuiltinFactor(
        name="vwap_deviation",
        family="reversal",
        hypothesis="VWAP 偏离：价格显著高于滚动 VWAP 时短期回落概率上升。",
        code='''def factor(df):
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = tp * df["volume"]
    vwap = pv.rolling(48).sum() / df["volume"].rolling(48).sum().replace(0.0, np.nan)
    return -(df["close"] / vwap - 1.0)
''',
        validated=False,
    ),
    "price_volume_corr": BuiltinFactor(
        name="price_volume_corr",
        family="volume",
        hypothesis="价量相关性：量价同向放大说明趋势有真实资金支撑。",
        code='''def factor(df):
    ret = df["close"] / df["close"].shift(1) - 1.0
    volchg = df["volume"] / df["volume"].shift(1) - 1.0
    return ret.rolling(48).corr(volchg)
''',
        validated=False,
    ),
    "close_location_value": BuiltinFactor(
        name="close_location_value",
        family="structure",
        hypothesis="区间收盘位置 CLV：收盘靠近最高价说明买盘承接强。",
        code='''def factor(df):
    rng = (df["high"] - df["low"]).replace(0.0, np.nan)
    clv = (2.0 * df["close"] - df["high"] - df["low"]) / rng
    return clv.rolling(24).mean()
''',
        validated=False,
    ),
    "momentum_consistency": BuiltinFactor(
        name="momentum_consistency",
        family="momentum",
        hypothesis="动量一致性：连续同向 K 线占比比单看涨跌幅更抗噪。",
        code='''def factor(df):
    up = (df["close"].diff() > 0).astype(float).rolling(48).mean()
    return (up - 0.5) * 2.0
''',
        validated=False,
    ),

    # ------------------------------------------------------------------
    # B 档：依赖 K 线 API 原生返回、但以前被 data.py 丢弃的成交明细列。
    # 数据源没有这些列（Gate/OKX）时因子全 NaN，signal 层会自动把权重置 0。
    # ------------------------------------------------------------------
    "taker_buy_ratio": BuiltinFactor(
        name="taker_buy_ratio",
        family="orderflow",
        hypothesis="主动买占比：taker 主动买入占成交量比例高于 50% 说明买方更急切。",
        code='''def factor(df):
    r = df["taker_base"] / df["volume"].replace(0.0, np.nan) - 0.5
    return r.rolling(24).mean()
''',
        validated=False,
    ),
    "order_flow_imbalance": BuiltinFactor(
        name="order_flow_imbalance",
        family="orderflow",
        hypothesis="订单流不平衡：主动买量减主动卖量的标准化净额。",
        code='''def factor(df):
    sell = df["volume"] - df["taker_base"]
    ofi = (df["taker_base"] - sell) / df["volume"].replace(0.0, np.nan)
    return ofi.rolling(12).mean()
''',
        validated=False,
    ),
    "avg_trade_size": BuiltinFactor(
        name="avg_trade_size",
        family="orderflow",
        hypothesis="平均单笔规模：相对均值突增通常伴随大单/机构进场。",
        code='''def factor(df):
    ats = df["quote_volume"] / df["trades"].replace(0.0, np.nan)
    return ats / ats.rolling(96).mean().replace(0.0, np.nan) - 1.0
''',
        validated=False,
    ),
    "turnover_zscore": BuiltinFactor(
        name="turnover_zscore",
        family="liquidity",
        hypothesis="换手率异常放大 × 短期动量：放量突破比缩量突破更可靠。",
        code='''def factor(df):
    to = df["quote_volume"] / df["close"].replace(0.0, np.nan)
    m = to.rolling(96).mean()
    s = to.rolling(96).std().replace(0.0, np.nan)
    mom = df["close"] / df["close"].shift(12) - 1.0
    return ((to - m) / s) * mom
''',
        validated=False,
    ),

    # ------------------------------------------------------------------
    # C 档：衍生品维度（basis / OI / 多空比）。列由 derivatives 模块对齐后附加，
    # 缺列时同样由 signal 层自动降权为 0，不会污染复合信号。
    # ------------------------------------------------------------------
    "basis_carry": BuiltinFactor(
        name="basis_carry",
        family="carry",
        hypothesis="基差：永续相对指数溢价过高说明多头拥挤，做空方向有正 carry。",
        code='''def factor(df):
    return -df["basis"].rolling(9).mean()
''',
        validated=False,
    ),
    "oi_momentum": BuiltinFactor(
        name="oi_momentum",
        family="derivatives",
        hypothesis="持仓量变化 × 价格动量：价涨仓增是新增多头，价涨仓减是空头回补。",
        code='''def factor(df):
    oi_chg = df["open_interest"] / df["open_interest"].shift(24) - 1.0
    mom = df["close"] / df["close"].shift(24) - 1.0
    return oi_chg * mom
''',
        validated=False,
    ),
    "oi_price_divergence": BuiltinFactor(
        name="oi_price_divergence",
        family="derivatives",
        hypothesis="价涨 OI 跌背离：上涨由空头平仓推动，趋势难以持续，反向。",
        code='''def factor(df):
    oi_chg = df["open_interest"] / df["open_interest"].shift(24) - 1.0
    mom = df["close"] / df["close"].shift(24) - 1.0
    return -(mom * (-oi_chg))
''',
        validated=False,
    ),
    "taker_long_short": BuiltinFactor(
        name="taker_long_short",
        family="derivatives",
        hypothesis="taker 多空比：主动买卖账户比极端时反向（拥挤度反转）。",
        code='''def factor(df):
    return -(df["taker_ratio"].rolling(12).mean() - 1.0)
''',
        validated=False,
    ),
    "account_long_short": BuiltinFactor(
        name="account_long_short",
        family="derivatives",
        hypothesis="账户多空比：散户仓位极端拥挤时是反向指标。",
        code='''def factor(df):
    return -(df["ls_ratio"].rolling(12).mean() - 1.0)
''',
        validated=False,
    ),
}


# 因子 → 必需的数据列。缺省表示只需 OHLCV / funding_rate。
# 数据源没有这些列时，signal 层会把对应因子**整体跳过**（不加载、不占位、不稀释信号）。
# 注意「列存在但值为 NaN」是另一种情况（Gate/OKX 补了空列），由 signal 层的
# 有效值检查负责淘汰。
FACTOR_REQUIRES: dict[str, tuple[str, ...]] = {
    "taker_buy_ratio": ("taker_base",),
    "order_flow_imbalance": ("taker_base",),
    "avg_trade_size": ("quote_volume", "trades"),
    "turnover_zscore": ("quote_volume",),
    "amihud_illiquidity": ("quote_volume",),
    "basis_carry": ("basis",),
    "oi_momentum": ("open_interest",),
    "oi_price_divergence": ("open_interest",),
    "taker_long_short": ("taker_ratio",),
    "account_long_short": ("ls_ratio",),
}


def get_factor(name: str) -> BuiltinFactor:
    if name not in LIBRARY:
        raise KeyError(f"未知因子 {name}，可选: {', '.join(LIBRARY)}")
    return LIBRARY[name]