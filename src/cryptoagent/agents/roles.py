"""四个角色：分析师 → 研究员 → 交易员 → 风控官。

关键设计：每个角色都有「确定性兜底实现」。
- 数字永远由 pandas 算出来，LLM 只负责解释和权衡，不能发明数据。
- mock 模式下，兜底实现本身就是一套规则引擎，保证流水线可离线复现。
- 接上真 LLM 后，兜底只在调用失败时接管，并在报告里标注 degraded。
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

from ..config import RiskConfig
from ..data import bars_per_year
from ..risk import AccountState
from ..sentiment import SentimentResult
from .llm import (
    LLMClient,
    SYSTEM_ANALYST,
    SYSTEM_JUDGE,
    SYSTEM_RESEARCHER,
    SYSTEM_RISK,
    SYSTEM_TRADER,
)

EPS = 1e-12


# --------------------------------------------------------------------------
# 市场快照
# --------------------------------------------------------------------------
@dataclass
class MarketSnapshot:
    symbol: str
    interval: str
    ts: str
    price: float
    ret_1: float
    ret_24: float
    ret_168: float
    vol_24: float          # 每根 K 线的已实现波动
    vol_annual: float
    vol_percentile: float
    rsi_14: float
    sma_24: float
    sma_72: float
    sma_168: float
    atr_pct: float
    funding_last: float
    funding_24h: float
    funding_z: float
    dd_from_high: float
    high_30d: float
    low_30d: float
    # 资金费率数据是否真实存在：整列为 0 时这位分析师没有任何信息量，
    # 必须把它的权重让出去，而不是给一个 0 立场白白稀释综合分。
    funding_valid: bool = True
    # 短周期多空帧（15m/5m）：注入到分析师视野，实现「1h 定方向、短周期定时机」的共振。
    # 由 pipeline 在决策时实时拉短周期 K 线填充，from_df 默认留空；离线/合成数据下为空。
    short_frames: dict = field(default_factory=dict)

    @classmethod
    def from_df(
        cls,
        df: pd.DataFrame,
        symbol: str,
        interval: str,
        price_override: float | None = None,
    ) -> "MarketSnapshot":
        close = df["close"].astype(float)
        ret = close.pct_change()
        n = len(close)

        def _ret(k: int) -> float:
            return float(close.iloc[-1] / close.iloc[-1 - k] - 1.0) if n > k else 0.0

        vol_24 = float(ret.iloc[-24:].std(ddof=1)) if n > 25 else float(ret.std(ddof=1))
        bpy = bars_per_year(interval)
        roll_vol = ret.rolling(24).std()
        recent = roll_vol.iloc[-720:].dropna()
        vol_pct = float((recent <= vol_24).mean()) if len(recent) > 10 else 0.5

        delta = close.diff()
        up = delta.clip(lower=0).rolling(14).mean()
        down = (-delta.clip(upper=0)).rolling(14).mean().replace(0.0, np.nan)
        rs = up / down
        rsi = float((100 - 100 / (1 + rs)).iloc[-1]) if len(close) > 15 else 50.0
        rsi = 50.0 if math.isnan(rsi) else rsi

        tr = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - close.shift()).abs(),
                (df["low"] - close.shift()).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr_pct = float(tr.rolling(14).mean().iloc[-1] / close.iloc[-1]) if len(close) > 15 else 0.02

        fr = df["funding_rate"].fillna(0.0) if "funding_rate" in df else pd.Series(0.0, index=df.index)
        nonzero = fr[fr != 0]
        # 整列都是 0 = 数据源没有资金费率（OKX 部分品种就是），标记出来让权重让位
        funding_valid = bool(len(nonzero))
        funding_last = float(nonzero.iloc[-1]) if funding_valid else 0.0
        funding_24h = float(fr.iloc[-24:].sum())
        # z-score 必须同量纲：分子是「24 根累计」，分母也必须是「24 根累计」的历史分布。
        # 旧实现拿单根标准差去除 24 根之和（差 sqrt(24)~24 倍），z 恒在 10~14，
        # 经 tanh 后饱和成 ±1——资金费率分析师退化成常数，30% 权重变成系统性偏置。
        roll24 = fr.rolling(24, min_periods=12).sum()
        hist24 = roll24.iloc[-720:].dropna()
        if len(hist24) >= 30:
            f_mean, f_std = float(hist24.mean()), float(hist24.std(ddof=1))
        else:
            f_mean, f_std = 0.0, 0.0
        funding_z = float((funding_24h - f_mean) / f_std) if f_std > EPS else 0.0
        # 封顶：分布退化时也不让单个分析师把综合分打满
        funding_z = float(max(-6.0, min(6.0, funding_z)))

        hi = float(close.iloc[-720:].max())
        lo = float(close.iloc[-720:].min())
        # 最新价：决策时用 ws 实时价（price_override）覆盖 K 线末根收盘，消除滞后
        price = float(price_override) if price_override and price_override > 0 else float(close.iloc[-1])

        return cls(
            symbol=symbol,
            interval=interval,
            ts=str(df.index[-1]),
            price=price,
            ret_1=_ret(1),
            ret_24=_ret(24),
            ret_168=_ret(168),
            vol_24=vol_24,
            vol_annual=vol_24 * math.sqrt(bpy),
            vol_percentile=vol_pct,
            rsi_14=rsi,
            sma_24=float(close.rolling(24).mean().iloc[-1]),
            sma_72=float(close.rolling(72).mean().iloc[-1]),
            sma_168=float(close.rolling(168).mean().iloc[-1]),
            atr_pct=max(atr_pct, 1e-4),
            funding_last=funding_last,
            funding_24h=funding_24h,
            funding_z=funding_z,
            dd_from_high=price / hi - 1.0,
            high_30d=hi,
            low_30d=lo,
            funding_valid=funding_valid,
        )

    def to_prompt(self) -> str:
        funding_line = (
            "资金费率: 数据缺失（整列为 0，本项不参与评分）"
            if not self.funding_valid
            else f"资金费率: 最近结算 {self.funding_last:+.4%}，"
                 f"24h 累计 {self.funding_24h:+.4%}，z-score {self.funding_z:+.2f}"
        )
        base = (
            f"标的: {self.symbol} ({self.interval})   时间: {self.ts}\n"
            f"最新价: {self.price:,.1f}\n"
            f"收益: 1根 {self.ret_1:+.2%} | 24根 {self.ret_24:+.2%} | 168根 {self.ret_168:+.2%}\n"
            f"波动率: 年化 {self.vol_annual:.1%}，处于近 720 根的 {self.vol_percentile:.0%} 分位\n"
            f"RSI(14): {self.rsi_14:.1f}   ATR: {self.atr_pct:.2%}\n"
            f"均线: SMA24 {self.sma_24:,.1f} | SMA72 {self.sma_72:,.1f} | SMA168 {self.sma_168:,.1f}\n"
            f"{funding_line}\n"
            f"距 30 日高点: {self.dd_from_high:+.2%}（高点 {self.high_30d:,.1f} / 低点 {self.low_30d:,.1f}）"
        )
        # 短周期多空帧：让分析师/研究员/交易员在同一份数据里做多周期权衡
        if self.short_frames:
            lines = []
            for interval, m in self.short_frames.items():
                lines.append(
                    f"[{interval}] 价 {m['price']:,.1f}  近12根 {m['ret_12']:+.2%}  RSI {m['rsi_14']:.0f}  "
                    f"{m['trend']}  突破 {m['breakout']:+.2f}  ATR {m['atr_pct']:.2%}  "
                    f"区间 {m['low_20']:,.1f}~{m['high_20']:,.1f}"
                )
            base += "\n短周期（多周期共振参考，用于判断入场/离场时机）：\n" + "\n".join(lines)
        return base

    def to_dict(self) -> dict:
        return {k: (round(v, 8) if isinstance(v, float) else v) for k, v in asdict(self).items()}


# --------------------------------------------------------------------------
# 分析师
# --------------------------------------------------------------------------
@dataclass
class AnalystReport:
    role: str
    stance: float                  # -1 极度看空 ~ +1 极度看多
    summary: str
    data_flags: list[str] = field(default_factory=list)


def _technical_score(s: MarketSnapshot) -> float:
    momentum = s.ret_24 / (s.vol_24 * math.sqrt(24) + EPS)
    trend = 0.0
    if s.sma_24 > s.sma_72 > s.sma_168:
        trend = 0.5
    elif s.sma_24 < s.sma_72 < s.sma_168:
        trend = -0.5
    rsi_adj = 0.0
    if s.rsi_14 > 72:
        rsi_adj = -0.25
    elif s.rsi_14 < 28:
        rsi_adj = 0.25
    return float(np.tanh(0.45 * momentum + trend + rsi_adj))


def _funding_score(s: MarketSnapshot) -> float:
    # 资金费率越高说明多头越拥挤，作为反向指标
    return float(-np.tanh(s.funding_z * 0.8))


def _sentiment_score(s: MarketSnapshot) -> float:
    # 没有新闻源时，用「短期价格行为」作为情绪代理，并明确标注这是代理指标
    short = s.ret_1 / (s.vol_24 + EPS)
    return float(np.tanh(0.5 * short))


ANALYST_SCHEMA = """stance: -1.0 到 1.0 的小数（负=看空，正=看多，绝对值表示把握）
summary: 120 字以内的中文分析，结论必须与 stance 一致"""


def run_analysts(
    llm: LLMClient, snap: MarketSnapshot, sentiment: "SentimentResult | None" = None
) -> list[AnalystReport]:
    t_score = _technical_score(snap)
    f_score = _funding_score(snap)
    s_score = _sentiment_score(snap)
    senti_flag = "无新闻源，使用价格行为代理（非真实情绪）"
    # A1：接到真实舆情时，用其分数覆盖价格行为代理，并把来源写进数据标记
    if sentiment is not None and sentiment.fresh:
        s_score = sentiment.score
        senti_flag = f"真实舆情：{sentiment.label}（{sentiment.source}）"

    tech_mock = (
        f"价格 {snap.price:,.0f}，24 根收益 {snap.ret_24:+.2%}，"
        f"风险调整动量 {snap.ret_24 / (snap.vol_24 * math.sqrt(24) + EPS):+.2f}σ。"
        f"{'均线多头排列' if snap.sma_24 > snap.sma_72 > snap.sma_168 else '均线空头排列' if snap.sma_24 < snap.sma_72 < snap.sma_168 else '均线纠缠'}，"
        f"RSI {snap.rsi_14:.0f}，ATR {snap.atr_pct:.2%}。"
        f"结论：{'偏多' if t_score > 0.15 else '偏空' if t_score < -0.15 else '中性'}。"
    )
    funding_mock = (
        f"最近一次资金费率 {snap.funding_last:+.4%}，24h 累计 {snap.funding_24h:+.4%}，"
        f"z-score {snap.funding_z:+.2f}。"
        f"{'多头拥挤，存在多头付费压力' if snap.funding_z > 1 else '空头拥挤，空头付费' if snap.funding_z < -1 else '费率中性，无显著拥挤'}。"
        f"结论：{'反向偏空' if f_score < -0.15 else '反向偏多' if f_score > 0.15 else '无信号'}。"
    )
    if sentiment is not None and sentiment.fresh and sentiment.headlines:
        hl = "；".join(sentiment.headlines[:4])
        senti_mock = (
            f"舆情源（{sentiment.source}）综合情绪：{sentiment.label}（分 {sentiment.score:+.2f}）。"
            f"近期标题：{hl}"
        )
    else:
        senti_mock = (
            f"缺少新闻/社交数据源，情绪以价格行为代理：1 根收益 {snap.ret_1:+.2%}，"
            f"相对于波动 {snap.vol_24:.2%} 属于{'异常放大' if abs(snap.ret_1) > 2 * snap.vol_24 else '正常范围'}。"
            f"结论：{'短线偏多' if s_score > 0.15 else '短线偏空' if s_score < -0.15 else '中性'}。"
        )

    reports = []
    specs = [
        ("技术面分析师", t_score, tech_mock, "价格/均线/RSI/ATR 均为实时计算"),
        ("资金费率分析师", f_score, funding_mock,
         "资金费率为真实结算数据" if snap.funding_last != 0 else "资金费率数据缺失，按 0 处理"),
        ("情绪面分析师", s_score, senti_mock, senti_flag),
    ]

    def _run_one(spec):
        role, score, mock, flag = spec
        data, resp = llm.json(
            SYSTEM_ANALYST,
            f"数据：\n{snap.to_prompt()}\n\n请以{role}身份分析，给出立场分数与对应的分析。",
            ANALYST_SCHEMA,
            # 兜底值必须与公式逐位相等（不能 round）：mock/降级时合并后要还原成原值
            {"stance": float(score), "summary": mock},
        )
        try:
            llm_stance = float(data.get("stance", score))
        except (TypeError, ValueError):
            llm_stance = float(score)
        llm_stance = float(np.clip(llm_stance, -1.0, 1.0))
        # 公式为主（60%）、LLM 判断为辅（40%）：模型只能修正公式，不能主导。
        # mock/降级时兜底答案就是 score，两者相等 -> 立场与旧行为完全一致。
        # 这一步解决的是「summary 说超卖别追空、stance 却是负数」的自相矛盾：
        # 现在 LLM 看到的形态细节能真正进入评分，但最多改动 0.4。
        stance = float(np.clip(0.6 * float(score) + 0.4 * llm_stance, -1.0, 1.0))
        summary = str(data.get("summary") or mock)
        flags = [flag]
        if abs(llm_stance - float(score)) > 0.02:
            flags.append(f"LLM 立场 {llm_stance:+.2f}（公式 {float(score):+.2f}）")
        if resp.degraded:
            flags.append(f"LLM 降级: {resp.reason}")
        return AnalystReport(role=role, stance=stance, summary=summary, data_flags=flags)

    # 分析师团队并发工作：接真 LLM 时能把等待时间压到 1/3
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        reports = list(pool.map(_run_one, specs))
    return reports


# --------------------------------------------------------------------------
# 研究员：多空辩论
# --------------------------------------------------------------------------
@dataclass
class Debate:
    rounds: list[dict]
    bull_case: str
    bear_case: str
    thesis: str
    score: float
    # 辩论环节的 LLM 降级也要能被上层看到：之前只汇总分析师/交易员/风控官，
    # 辩论降级时报告仍显示「全链正常」。
    degraded: bool = False
    # 辩论裁判给出的原始分（None = 未启用/未跑）。score 是混合后的最终分，
    # 两者都留存，报告里才能审计「辩论到底改了多少」。
    debate_score: float | None = None
    judge: dict = field(default_factory=dict)


def run_debate(
    llm: LLMClient,
    snap: MarketSnapshot,
    reports: list[AnalystReport],
    rounds: int = 2,
    weights: list[float] | None = None,
    calibrated: bool = False,
    funding_valid: bool = True,
) -> Debate:
    # 权重默认仍是经验值；接入事后校准后由 engine 传入校准过的权重
    w = list(weights) if weights and len(weights) == 3 else [0.50, 0.30, 0.20]
    if not funding_valid:
        # 资金费率整列缺失：这位分析师没有信息量，权重按原比例让给技术与情绪，
        # 否则一个必然为 0 的立场会白白稀释综合分（等于给全体降权）。
        rest = w[0] + w[2]
        w = [w[0] / rest, 0.0, w[2] / rest] if rest > 0 else [0.7, 0.0, 0.3]
    debate_degraded = False
    score = sum(w[i] * reports[i].stance for i in range(min(3, len(reports))))
    positives = [r for r in reports if r.stance > 0.1]
    negatives = [r for r in reports if r.stance < -0.1]

    if positives:
        bull_body = "；".join(f"{r.role}({r.stance:+.2f})" for r in positives)
    else:
        bull_body = "当前没有正向信号，多头只能论证超跌反弹与低波动压缩后的向上突破"
    bull_mock = (
        "多头论据：" + bull_body
        + f"。当前价距 30 日高点 {snap.dd_from_high:+.2%}，若趋势延续则上方空间打开。"
    )
    vol_view = (
        f"年化波动 {snap.vol_annual:.0%}，处于 {snap.vol_percentile:.0%} 分位，高波动下止损容易被扫。"
        if snap.vol_percentile > 0.6
        else f"年化波动 {snap.vol_annual:.0%}，处于 {snap.vol_percentile:.0%} 分位的低波动区间，"
        "低波动常是突破前的压缩，方向未定。"
    )
    bear_mock = (
        "空头论据：" + ("；".join(f"{r.role}({r.stance:+.2f})" for r in negatives) if negatives else "无负向信号")
        + f"。当前价距 30 日高点 {snap.dd_from_high:+.2%}，上方套牢盘构成压力。" + vol_view
    )

    history: list[dict] = []
    bull_case, bear_case = bull_mock, bear_mock
    last_bull, last_bear = bull_mock, bear_mock
    for i in range(max(1, rounds)):
        context = "\n".join(
            f"[{r.role}] 立场 {r.stance:+.2f}：{r.summary}" for r in reports
        )
        prior = ""
        if history:
            prior = "\n上一轮：\n" + "\n".join(f"{h['side']}：{h['text']}" for h in history[-2:])

        # 交叉质询：第 1 轮多头先立论，之后每一轮都针对对方上一轮发言反驳，
        # 不再各说各话——这比「多头说完空头说」更能暴露逻辑漏洞。
        if i == 0:
            bull_user = (
                f"{snap.to_prompt()}\n\n分析师观点：\n{context}{prior}\n\n"
                f"你是多头研究员，第 {i + 1} 轮先立论，提出你的核心多头论据。"
            )
            bull_mock_i = bull_mock
        else:
            bull_user = (
                f"{snap.to_prompt()}\n\n分析师观点：\n{context}{prior}\n\n"
                f"你是多头研究员，第 {i + 1} 轮。空头刚才反驳你：\n「{last_bear}」\n"
                "请针对性反驳空头，并强化你的核心论点。"
            )
            bull_mock_i = f"第 {i + 1} 轮：维持多头判断，{bull_mock}"
        bull_resp = llm.text(SYSTEM_RESEARCHER, bull_user, bull_mock_i)

        bear_user = (
            f"{snap.to_prompt()}\n\n分析师观点：\n{context}{prior}\n多头刚说：\n「{bull_resp.content}」\n\n"
            f"你是空头研究员，第 {i + 1} 轮，请针对性反驳多头的论点。"
        )
        bear_mock_i = bear_mock if i == 0 else f"第 {i + 1} 轮：维持空头判断，{bear_mock}"
        bear_resp = llm.text(SYSTEM_RESEARCHER, bear_user, bear_mock_i)

        bull_case, bear_case = bull_resp.content, bear_resp.content
        debate_degraded = debate_degraded or bool(bull_resp.degraded or bear_resp.degraded)
        last_bull, last_bear = bull_resp.content, bear_resp.content
        history.append({"round": i + 1, "side": "多头", "text": bull_resp.content})
        history.append({"round": i + 1, "side": "空头", "text": bear_resp.content})

    if score > 0.15:
        stance = "偏多"
    elif score < -0.15:
        stance = "偏空"
    else:
        stance = "中性"
    thesis = (
        f"分析师加权 {score:+.2f}（技术 {w[0]:.2f} / 资金费率 {w[1]:.2f} / 情绪 {w[2]:.2f} 加权"
        f"{'，权重已按历史命中率校准' if calibrated else ''}），结论{stance}。"
        f"多头核心：{positives[0].role if positives else '无'}；"
        f"空头核心：{negatives[0].role if negatives else '无'}。"
    )
    return Debate(rounds=history, bull_case=bull_case, bear_case=bear_case,
                  thesis=thesis, score=score, degraded=debate_degraded)


# --------------------------------------------------------------------------
# 辩论裁判：把「谁辩赢了」变成可打分的量
# --------------------------------------------------------------------------
JUDGE_SCHEMA = """score: -1.0 到 1.0 的小数（负=空头论证更强，正=多头更强）
reason: 哪一方的哪条论据决定胜负、以及为什么（80 字以内）"""


def judge_debate(
    llm: LLMClient,
    snap: MarketSnapshot,
    debate: Debate,
    analyst_score: float,
    weight: float = 0.3,
) -> tuple[float, float | None, dict]:
    """把辩论胜负量化成 -1~+1，再按 weight 混入分析师加权分。

    之前 `Debate.score` 恒等于分析师加权，辩 N 轮也不改变决策——辩论只是写给
    交易员看的材料。这里补上裁判角色，让辩论真正进入评分，但默认只占 30%：
    分析师（带历史校准权重）仍是主体，裁判只做方向修正，防止一段雄辩翻转决策。

    返回 (混合后分数, 裁判原始分, 裁判说明)。mock/降级时原样返回 analyst_score。
    """
    if weight <= 0 or not debate.rounds:
        return float(analyst_score), None, {}
    transcript = "\n".join(
        f"{h.get('side')}：{h.get('text')}" for h in debate.rounds[-6:]
    )
    mock = {
        # 同上：兜底值不与加权分做任何取整，保证 mock/降级时综合分与旧版完全一致
        "score": float(analyst_score),
        "decisive": "无（降级：沿用分析师加权）",
        "reason": "LLM 不可用，辩论结论回退为分析师加权分数",
    }
    data, resp = llm.json(
        SYSTEM_JUDGE,
        f"{snap.to_prompt()}\n\n分析师加权评分：{float(analyst_score):+.2f}\n\n"
        f"辩论记录：\n{transcript}\n\n"
        "请判断哪一方的论证更有说服力，给出 -1~+1 的分数（正=偏多）。",
        JUDGE_SCHEMA,
        mock,
    )
    try:
        raw = float(data.get("score", analyst_score))
    except (TypeError, ValueError):
        raw = float(analyst_score)
    raw = float(np.clip(raw, -1.0, 1.0))
    w = float(np.clip(weight, 0.0, 0.5))
    mixed = float(np.clip((1.0 - w) * float(analyst_score) + w * raw, -1.0, 1.0))
    info = {
        "score": round(raw, 4),
        "decisive": str(data.get("decisive") or "")[:200],
        "reason": str(data.get("reason") or "")[:200],
        "weight": round(w, 3),
        "analyst_score": round(float(analyst_score), 4),
        "degraded": bool(resp.degraded),
    }
    return mixed, raw, info


# --------------------------------------------------------------------------
# 交易员
# --------------------------------------------------------------------------
# 止损/止盈的合法区间只在这里定义一次：schema 提示词与 _sanitize_proposal
# 共用同一组常量，避免「提示词说 0.004~0.20、解析却按 0~1」的错位。
STOP_MIN, STOP_MAX = 0.002, 0.10
TP_MIN, TP_MAX = 0.004, 0.20


def trader_schema(max_leverage: float = 3.0, max_position_pct: float = 0.30,
                  min_risk_reward: float = 1.3, min_stop_atr_mult: float = 0.6,
                  min_leverage: float = 1.0) -> str:
    """交易员的输出 schema。上下限跟着风控配置走。

    这里以前硬编码 `leverage: 1.0 到 3.0`，模型很听话地永远给 2x——真正的杠杆瓶颈
    在提示词，不在风控。改成按配置生成，并说明「杠杆只影响保证金占用，不改单笔风险」。

    盈亏比提示分两种：低盈亏比档只要求「不低于下限」；高盈亏比档（趋势跟踪）
    必须把目标放到更远处，否则固定止盈会在趋势刚起步时截断，移动止损没机会接管。
    """
    lev_cap = max(1.0, float(max_leverage or 3.0))
    lev_floor = min(max(1.0, float(min_leverage or 1.0)), lev_cap)
    pos_cap = min(max(float(max_position_pct or 0.30), 0.01), 1.0)
    rr = max(1.0, float(min_risk_reward or 1.3))
    atr_floor = max(0.0, float(min_stop_atr_mult or 0.0))
    if rr >= 2.0:
        tp_hint = (
            f"（必须 ≥ stop_loss_pct 的 {rr:.1f} 倍。本档靠趋势盈利：建议给到 "
            f"{rr * 1.4:.1f}~{rr * 2.5:.1f} 倍，把主要退出交给移动止损，"
            "不要在小目标就挂止盈——否则利润跑不起来，还占用一次开仓机会）"
        )
    else:
        tp_hint = f"（必须 ≥ stop_loss_pct 的 {rr:.1f} 倍，否则会被风控直接拒绝）"
    return (
        "action: long 或 short 或 flat\n"
        f"size_pct: 0.0 到 {pos_cap:.2f} 之间的小数（保证金占账户权益的比例）\n"
        f"leverage: {lev_floor:.1f} 到 {lev_cap:.1f}（止损越近可以给越高；杠杆只改变"
        "保证金占用，单笔风险由 仓位×杠杆×止损 决定）\n"
        f"stop_loss_pct: {STOP_MIN} 到 {STOP_MAX}"
        f"（不要比 {atr_floor:.1f}×ATR 还近，否则正常波动就会扫损）\n"
        f"take_profit_pct: {TP_MIN} 到 {TP_MAX}{tp_hint}\n"
        "confidence: 0.0 到 1.0\n"
        "rationale: 一句话理由"
    )

RISK_SCHEMA = """verdict: approve 或 reduce 或 reject
size_multiplier: 0.0 到 1.0（只允许缩小）
reasons: 字符串数组"""


def _fallback_proposal(snap: MarketSnapshot, debate: Debate, caps: dict | None = None) -> dict:
    caps = caps or {}
    lev_cap = max(1.0, float(caps.get("max_leverage") or 3.0))
    # 杠杆下限也读配置：进取档托底 3x 时，降级路径不该再给 1x 让风控多做一次修正
    lev_floor = min(max(1.0, float(caps.get("min_leverage") or 1.0)), lev_cap)
    pos_cap = min(max(float(caps.get("max_position_pct") or 0.30), 0.02), 1.0)
    # 降级路径的止盈必须跟着本档的盈亏比下限走：
    # 写死 2.0 时，高盈亏比档（2.2）用降级提案会被风控直接拒掉——
    # 表现为「LLM 一降级就永远不开仓」，很难看出根因
    min_rr = max(1.0, float(caps.get("min_risk_reward") or 1.3))
    score = debate.score
    if abs(score) < 0.15:
        return {
            "action": "flat",
            "size_pct": 0.0,
            "leverage": 1.0,
            "stop_loss_pct": 0.02,
            "take_profit_pct": round(0.02 * min_rr, 4),
            "confidence": 0.5,
            "rationale": f"综合评分 {score:+.2f} 未达到开仓阈值 0.15，空仓等待。",
        }
    direction = "long" if score > 0 else "short"
    confidence = float(np.clip(0.5 + abs(score) * 0.55, 0.5, 0.92))
    size = float(np.clip((confidence - 0.45) * 0.6, 0.02, max(0.02, pos_cap)))
    stop = float(np.clip(1.5 * snap.atr_pct, 0.006, 0.03))
    # 杠杆不再写死 2x：止损越近给越高（同样风险下，高杠杆只是少占保证金）。
    # 下限跟着配置走，避免降级路径每次都触发一次「按杠杆下限托底」的多余修正
    lev = float(np.clip(0.06 / max(stop, 0.002), lev_floor, lev_cap))
    return {
        "action": direction,
        "size_pct": round(size, 4),
        "leverage": round(lev, 2),
        "stop_loss_pct": round(stop, 4),
        "take_profit_pct": round(min(stop * max(2.0, min_rr), TP_MAX), 4),
        "confidence": round(confidence, 3),
        "rationale": (
            f"综合评分 {score:+.2f}，ATR {snap.atr_pct:.2%} 决定止损 {stop:.2%}，"
            f"波动分位 {snap.vol_percentile:.0%}。"
        ),
    }


def propose_trade(
    llm: LLMClient,
    snap: MarketSnapshot,
    reports: list[AnalystReport],
    debate: Debate,
    caps: dict | None = None,
) -> tuple[dict, bool]:
    caps = caps or {}
    lev_cap = float(caps.get("max_leverage") or 3.0)
    pos_cap = float(caps.get("max_position_pct") or 0.30)
    min_rr = max(1.0, float(caps.get("min_risk_reward") or 1.3))
    atr_floor = max(0.0, float(caps.get("min_stop_atr_mult") or 0.6))
    lev_floor = max(1.0, float(caps.get("min_leverage") or 1.0))
    mock = _fallback_proposal(snap, debate, caps)
    context = "\n".join(f"[{r.role}] {r.summary}" for r in reports)
    user = (
        f"{snap.to_prompt()}\n\n分析师报告：\n{context}\n\n"
        f"研究结论：{debate.thesis}\n多头：{debate.bull_case}\n空头：{debate.bear_case}\n\n"
        "请给出交易提案。若信号不足请输出 flat。\n"
        f"本账户参数：杠杆上限 {lev_cap:.0f}x，单笔保证金上限 {pos_cap:.0%} 权益，"
        f"止盈必须 ≥ 止损的 {min_rr:.1f} 倍（盈亏比下限，硬性要求）。"
    )
    data, resp = llm.json(SYSTEM_TRADER, user,
                          trader_schema(lev_cap, pos_cap, min_rr, atr_floor, lev_floor), mock)
    proposal = _sanitize_proposal(data, mock, caps)
    return proposal, resp.degraded


def _sanitize_proposal(data: dict, fallback: dict, caps: dict | None = None) -> dict:
    """模型输出不可全信：类型、范围、枚举全部重新校验。"""
    caps = caps or {}
    lev_cap = max(1.0, float(caps.get("max_leverage") or 3.0))
    pos_cap = min(max(float(caps.get("max_position_pct") or 0.30), 0.01), 1.0)
    out = dict(fallback)
    action = str(data.get("action", fallback["action"])).lower()
    out["action"] = action if action in {"long", "short", "flat"} else "flat"

    def _num(key: str, lo: float, hi: float) -> float:
        try:
            v = float(data.get(key, fallback[key]))
        except (TypeError, ValueError):
            v = float(fallback[key])
        return float(np.clip(v, lo, hi))

    out["size_pct"] = _num("size_pct", 0.0, pos_cap)
    out["leverage"] = _num("leverage", 1.0, lev_cap)
    out["stop_loss_pct"] = _num("stop_loss_pct", STOP_MIN, STOP_MAX)
    out["take_profit_pct"] = _num("take_profit_pct", TP_MIN, TP_MAX)
    out["confidence"] = _num("confidence", 0.0, 1.0)
    out["rationale"] = str(data.get("rationale", fallback["rationale"]))[:500]
    return out


# --------------------------------------------------------------------------
# 风控官（LLM 只允许收紧，不允许放宽）
# --------------------------------------------------------------------------
def risk_review(
    llm: LLMClient,
    snap: MarketSnapshot,
    proposal: dict,
    state: AccountState,
    cfg: RiskConfig,
    positions: list[dict] | None = None,
) -> tuple[dict, bool]:
    multiplier = 1.0
    reasons = []
    if snap.vol_percentile > 0.9:
        multiplier *= 0.6
        reasons.append(f"波动率 {snap.vol_percentile:.0%} 分位，减仓 40%")
    if state.consecutive_losses >= 2:
        multiplier *= 0.6
        reasons.append(f"连续亏损 {state.consecutive_losses} 笔，减仓 40%")
    if state.drawdown < -cfg.max_drawdown_pct * 0.6:
        multiplier *= 0.5
        reasons.append(f"回撤 {state.drawdown:.1%} 已接近熔断线，减半")
    if proposal.get("action") != "flat" and abs(snap.funding_z) > 2.5:
        multiplier *= 0.7
        reasons.append(f"资金费率 z-score {snap.funding_z:+.1f} 极端，减仓 30%")
    # B2：已有同向/同品种持仓时，独立风控官主动减仓，避免叠加风险（代码硬规则之外的一层）
    if proposal.get("action") != "flat" and positions:
        same = [p for p in positions if p.get("symbol") == snap.symbol]
        if same:
            multiplier *= 0.7
            reasons.append(f"已持有 {snap.symbol} 持仓，减仓 30% 避免叠加风险")

    mock = {
        "verdict": "reduce" if multiplier < 1.0 else "approve",
        "size_multiplier": round(multiplier, 3),
        "reasons": reasons or ["未发现额外风险，维持原仓位"],
    }
    pos_text = "无"
    if positions:
        pos_text = "；".join(
            f"{p.get('symbol')}({p.get('side')}, 名义 {float(p.get('notional', 0)):,.0f})"
            for p in positions
        )
    user = (
        f"{snap.to_prompt()}\n\n账户：权益 {state.equity:,.0f}，回撤 {state.drawdown:.2%}，"
        f"当日盈亏 {state.daily_pnl_pct:+.2%}，连续亏损 {state.consecutive_losses} 笔。\n"
        f"当前持仓：{pos_text}\n"
        f"交易员提案：{proposal}\n\n"
        "请评估风险。你只能维持(approve)或缩小(reduce)仓位，或直接拒绝(reject)。"
        "尤其关注与现有持仓的方向叠加与相关性。"
    )
    data, resp = llm.json(SYSTEM_RISK, user, RISK_SCHEMA, mock)

    verdict = str(data.get("verdict", "reduce")).lower()
    if verdict not in {"approve", "reduce", "reject"}:
        verdict = "reduce"
    try:
        mult = float(data.get("size_multiplier", multiplier))
    except (TypeError, ValueError):
        mult = multiplier
    # LLM 无权放大仓位
    mult = float(np.clip(mult, 0.0, 1.0))
    mult = min(mult, multiplier) if multiplier < 1.0 else mult
    raw_reasons = data.get("reasons") or reasons
    # 模型偶尔把 reasons 输出成一个字符串：直接迭代会得到单字数组
    if isinstance(raw_reasons, str):
        raw_reasons = [raw_reasons]
    if not isinstance(raw_reasons, (list, tuple)):
        raw_reasons = reasons
    out = {
        "verdict": verdict,
        "size_multiplier": mult,
        "reasons": [str(r)[:200] for r in raw_reasons][:6],
    }
    return out, resp.degraded