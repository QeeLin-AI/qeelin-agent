"""因子自动研究循环：提议 → 沙箱执行 → 回测 → 评分 → 反思 → 晋级。

协议（很重要，决定了结论可不可信）：
    train   —— 用来生成假设、调参
    valid   —— 用来挑选因子（模型选择集）
    test    —— **只在最后跑一次**，作为最终留出集，绝不参与任何选择

如果 test 上的表现远差于 valid，说明选出来的东西是噪音。
这个循环会把这个衰减量写进报告，而不是藏起来。
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

from .backtest import BacktestResult, factor_to_target, run_backtest
from .config import AppConfig
from .data import split_market
from .factors.sandbox import FactorError, check_lookahead, run_factor, validate_factor_code


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class Proposal:
    name: str
    family: str
    hypothesis: str
    code: str
    source: str = "template"          # template | llm


@dataclass
class Evaluation:
    proposal: Proposal
    ok: bool = False
    error: str | None = None
    lookahead: list[str] = field(default_factory=list)
    train: dict | None = None
    valid: dict | None = None
    score: float = -math.inf
    accepted: bool = False
    reject_reason: str = ""
    elapsed: float = 0.0

    def brief(self) -> str:
        if not self.ok:
            return f"✗ {self.proposal.name}: {self.error}"
        status = "✓" if self.accepted else "✗"
        tr = self.train or {}
        va = self.valid or {}
        return (
            f"{status} {self.proposal.name:<18} "
            f"IS夏普 {tr.get('sharpe', 0):>5.2f} | "
            f"OOS夏普 {va.get('sharpe', 0):>5.2f} | "
            f"OOS回撤 {va.get('max_drawdown', 0):>6.1%} | "
            f"评分 {self.score:>5.2f} {self.reject_reason}"
        )


class Proposer(Protocol):
    def propose(self, history: list[Evaluation], round_idx: int) -> Proposal: ...


# --------------------------------------------------------------------------
# 离线模板提议器（无 LLM 也能跑，用于验证链路）
# --------------------------------------------------------------------------
TEMPLATES: dict[str, dict[str, Any]] = {
    "momentum": {
        "params": [8, 12, 24, 48, 96, 168],
        "hypothesis": "{n} 根 K 线动量：趋势延续。",
        "code": 'def factor(df):\n    return df["close"] / df["close"].shift({n}) - 1.0\n',
    },
    "reversal": {
        "params": [3, 6, 12, 24],
        "hypothesis": "{n} 根 K 线反转：短期过度反应后均值回归。",
        "code": 'def factor(df):\n    return -(df["close"] / df["close"].shift({n}) - 1.0)\n',
    },
    "volatility": {
        "params": [24, 48, 96, 168],
        "hypothesis": "{n} 根波动率：低波动时段的风险调整收益更高。",
        "code": (
            'def factor(df):\n'
            '    ret = df["close"] / df["close"].shift(1) - 1.0\n'
            '    return -ret.rolling({n}).std()\n'
        ),
    },
    "carry": {
        "params": [3, 9, 21, 42],
        "hypothesis": "{n} 期资金费率均值：费率极端时反向。",
        "code": 'def factor(df):\n    return -df["funding_rate"].rolling({n}).mean()\n',
    },
    "breakout": {
        "params": [48, 96, 168],
        "hypothesis": "{n} 根区间位置：突破区间上沿后延续。",
        "code": (
            'def factor(df):\n'
            '    hi = df["close"].rolling({n}).max()\n'
            '    lo = df["close"].rolling({n}).min()\n'
            '    span = (hi - lo).replace(0.0, np.nan)\n'
            '    return (df["close"] - lo) / span - 0.5\n'
        ),
    },
    "trend": {
        "params": [24, 72, 168, 336],
        "hypothesis": "{n} 均线偏离：价格偏离长期均线后回归或延续。",
        "code": 'def factor(df):\n    sma = df["close"].rolling({n}).mean()\n    return df["close"] / sma - 1.0\n',
    },
    "volume": {
        "params": [12, 24, 48],
        "hypothesis": "{n} 动量 × 成交量 z-score：放量的趋势更可信。",
        "code": (
            'def factor(df):\n'
            '    ret = df["close"] / df["close"].shift({n}) - 1.0\n'
            '    m = df["volume"].rolling(96).mean()\n'
            '    s = df["volume"].rolling(96).std().replace(0.0, np.nan)\n'
            '    return ret * (df["volume"] - m) / s\n'
        ),
    },
    "rsi": {
        "params": [7, 14, 28],
        "hypothesis": "RSI({n}) 超买超卖反转。",
        "code": (
            'def factor(df):\n'
            '    d = df["close"].diff()\n'
            '    up = d.clip(lower=0).rolling({n}).mean()\n'
            '    dn = (-d.clip(upper=0)).rolling({n}).mean().replace(0.0, np.nan)\n'
            '    rs = up / dn\n'
            '    return -(100 - 100 / (1 + rs) - 50)\n'
        ),
    },
}


class TemplateProposer:
    """无 LLM 的提议器：按历史表现给因子家族加权，模拟「迭代」过程。"""

    def __init__(self, seed: int = 42) -> None:
        import random

        self.rng = random.Random(seed)
        self._used: set[str] = set()

    def propose(self, history: list[Evaluation], round_idx: int) -> Proposal:
        stats: dict[str, list[float]] = {}
        for ev in history:
            if ev.ok:
                stats.setdefault(ev.proposal.family, []).append(ev.score)

        families = list(TEMPLATES)
        weights = []
        for fam in families:
            scores = stats.get(fam, [])
            # 没试过的家族给一点探索奖励；试过的按平均分加权
            weights.append(1.5 if not scores else max(0.2, 1.0 + sum(scores) / len(scores)))

        fam = self.rng.choices(families, weights=weights, k=1)[0]
        tpl = TEMPLATES[fam]
        params = [p for p in tpl["params"]]
        self.rng.shuffle(params)
        for n in params:
            name = f"{fam}_{n}"
            if name not in self._used:
                self._used.add(name)
                return Proposal(
                    name=name,
                    family=fam,
                    hypothesis=tpl["hypothesis"].format(n=n),
                    code=tpl["code"].format(n=n),
                )
        # 全部用过就加个随机参数继续
        n = int(self.rng.choice(tpl["params"])) + self.rng.randint(1, 5)
        name = f"{fam}_{n}_{round_idx}"
        self._used.add(name)
        return Proposal(
            name=name,
            family=fam,
            hypothesis=tpl["hypothesis"].format(n=n),
            code=tpl["code"].format(n=n),
        )


# --------------------------------------------------------------------------
# LLM 提议器：让 Agent 真的去写因子代码
# --------------------------------------------------------------------------
FACTOR_SCHEMA = """name: 英文小写蛇形命名，不超过 24 字符
hypothesis: 一句话说明这个因子为什么有效
code: 完整的 Python 代码字符串，必须定义 def factor(df)，返回 pd.Series"""

SYSTEM_FACTOR_WRITER = (
    "你是一名量化研究员，为加密货币永续合约写因子。"
    "约束：只能使用 pandas/numpy；不能 import；不能读写文件；不能访问未来数据"
    "（禁止 shift 负数、禁止对全序列做 .max() 这类跨期统计）；"
    "函数签名必须是 def factor(df)，df 含 open/high/low/close/volume/funding_rate，"
    "返回与 df 等长的 pd.Series。写完代码在脑中检查一遍是否会用到未来信息。"
    "只输出 JSON。"
)

FEW_SHOT = '''可参考的因子家族与写法（df 含 open/high/low/close/volume/funding_rate，全用滚动窗口，禁止未来数据）：
1. 动量：def factor(df):\n    return df["close"] / df["close"].shift(24) - 1.0
2. 反转（RSI）：d = df["close"].diff(); up = d.clip(lower=0).rolling(14).mean(); dn = (-d.clip(upper=0)).rolling(14).mean().replace(0.0, np.nan); rsi = 100 - 100 / (1 + up / dn); return -(rsi - 50)
3. 均线（MACD）：ema12 = df["close"].ewm(span=12, adjust=False).mean(); ema26 = df["close"].ewm(span=26, adjust=False).mean(); return ema12 - ema26
4. 波动（低波溢价）：ret = df["close"] / df["close"].shift(1) - 1.0; return -ret.abs().rolling(48).mean()
5. 资金费率（反向拥挤）：return -df["funding_rate"].rolling(9).mean()
6. 突破（唐奇安）：hi = df["high"].rolling(96).max(); lo = df["low"].rolling(96).min(); span = (hi - lo).replace(0.0, np.nan); return (df["close"] - lo) / span - 0.5
7. 量价（买卖压力）：ret = df["close"] / df["close"].shift(1) - 1.0; upv = df["volume"] * (ret >= 0); dnv = df["volume"] * (ret < 0); tot = df["volume"].rolling(24).sum().replace(0.0, np.nan); return (upv.rolling(24).sum() - dnv.rolling(24).sum()) / tot
8. K线结构（实体方向）：rng = (df["high"] - df["low"]).replace(0.0, np.nan); return (df["close"] - df["open"]) / rng
'''


def _parse_dt(s: Any):
    try:
        return datetime.strptime(str(s), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def build_live_evidence(rows: list[dict], factor_acc: dict | None = None,
                        symbol: str | None = None, min_samples: int = 8,
                        max_factor_rows: int = 8) -> str:
    """把真实平仓回合压成一段「实战反馈」，供 LLM 提议因子时参考方向。

    只用 `position_history` 已有的字段（pnl / realized_r / mfe_r / rule / side /
    持有时间），不依赖任何新埋点，历史数据同样生效。

    协议边界（很重要）：这段文字**只影响「往哪个方向找因子」**，不参与评分。
    因子能否晋级仍由 IS/OOS/留出集三道闸门决定，不能因为「实盘亏了」就降低标准。
    样本不足 `min_samples` 时返回空串——少量样本只会教模型过拟合。
    """
    all_rows = [r for r in (rows or []) if isinstance(r, dict)]
    need = max(3, int(min_samples))
    if len(all_rows) < need:
        return ""
    rows = all_rows
    scope_note = ""
    if symbol:
        same = [r for r in all_rows
                if str(r.get("symbol") or "").upper() == str(symbol).upper()]
        if len(same) >= need:
            rows = same
        else:
            scope_note = (f"（{symbol} 自身样本仅 {len(same)} 笔，以下为当前账户跨品种汇总，"
                          "不要把某个品种的特性当成通用规律。）")

    def _num(v):
        try:
            f = float(v)
            return f if math.isfinite(f) else None
        except (TypeError, ValueError):
            return None

    n = len(rows)
    rs = [x for x in (_num(r.get("realized_r")) for r in rows) if x is not None]
    wins = sum(1 for r in rows if (_num(r.get("pnl")) or 0.0) > 0)
    head = f"样本 {n} 笔平仓回合（胜率 {wins / n * 100:.0f}%"
    if rs:
        head += f"，平均 {sum(rs) / len(rs):+.2f}R"
    lines = [head + "）"]

    # 浮盈回吐：区分「入场就错」和「看对了但没拿住」
    pairs = [(_num(r.get("mfe_r")), _num(r.get("realized_r"))) for r in rows]
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    if pairs:
        avg_mfe = sum(a for a, _ in pairs) / len(pairs)
        avg_real = sum(b for _, b in pairs) / len(pairs)
        if avg_mfe > 0:
            lines.append(
                f"平均最高浮盈 {avg_mfe:+.2f}R，实际落袋 {avg_real:+.2f}R，"
                f"回吐 {avg_mfe - avg_real:.2f}R（落差大 = 退出偏早）"
            )

    # 按退出规则分组：哪类退出在赚钱、哪类在亏钱
    groups: dict[str, list[dict]] = {}
    for r in rows:
        key = str(r.get("rule") or r.get("reason") or "").strip() or "未标注"
        groups.setdefault(key, []).append(r)
    if len(groups) > 1:
        chunks = []
        for key, grp in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:5]:
            gr = [x for x in (_num(g.get("realized_r")) for g in grp) if x is not None]
            win = sum(1 for g in grp if (_num(g.get("pnl")) or 0.0) > 0) / len(grp) * 100
            seg = f"{key}: {len(grp)} 笔 胜率 {win:.0f}%"
            if gr:
                seg += f" 平均 {sum(gr) / len(gr):+.2f}R"
            chunks.append(seg)
        lines.append("按退出方式：" + "；".join(chunks))

    # 按方向（只做多/只做空时没信息量，跳过）
    sides = {str(r.get("side") or "").upper()[:1] for r in rows}
    if len(sides) > 1:
        segs = []
        for label, prefix in (("多", "L"), ("空", "S")):
            grp = [r for r in rows if str(r.get("side") or "").upper().startswith(prefix)]
            if not grp:
                continue
            gr = [x for x in (_num(g.get("realized_r")) for g in grp) if x is not None]
            win = sum(1 for g in grp if (_num(g.get("pnl")) or 0.0) > 0) / len(grp) * 100
            seg = f"{label} {len(grp)} 笔 胜率 {win:.0f}%"
            if gr:
                seg += f" 平均 {sum(gr) / len(gr):+.2f}R"
            segs.append(seg)
        if segs:
            lines.append("按方向：" + "；".join(segs))

    # 持有时间：能区分「入场时机」与「持有耐心」
    holds = []
    for r in rows:
        a, b = _parse_dt(r.get("opened_at")), _parse_dt(r.get("closed_at"))
        if a and b and b > a:
            holds.append((b - a).total_seconds() / 60.0)
    if holds:
        holds.sort()
        lines.append(f"持有时间：中位 {holds[len(holds) // 2]:.0f} 分钟"
                     f"（最短 {holds[0]:.0f}，最长 {holds[-1]:.0f}）")

    # 因子方向命中率：开仓时该因子的方向 vs 之后市场实际走向（不是这笔仓的盈亏）
    fac_rows = []
    for name, st in (factor_acc or {}).items():
        try:
            k = int((st or {}).get("n") or 0)
            h = int((st or {}).get("hit") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if k >= 3:
            fac_rows.append((k, h, str(name)))
    if fac_rows:
        fac_rows.sort(reverse=True)
        lines.append("因子方向命中率（历史累计，开仓时该因子方向 vs 之后市场实际走向）：" + "；".join(
            f"{nm} {k} 次 {h / k * 100:.0f}%" for k, h, nm in fac_rows[:max_factor_rows]
        ))
    if scope_note:
        lines.append(scope_note)
    return "\n".join(lines)


class LLMProposer:
    """让 LLM 根据历史回测结果 + 真实交易反馈提出新因子；输出不合规时退回模板。"""

    def __init__(self, client, fallback: "TemplateProposer | None" = None,
                 live_evidence: str = "") -> None:
        self.client = client
        self.fallback = fallback or TemplateProposer(seed=7)
        self.live_evidence = str(live_evidence or "")

    def propose(self, history: list[Evaluation], round_idx: int) -> Proposal:
        recent = [e for e in history if e.ok][-6:]
        lines = []
        for e in recent:
            lines.append(
                f"- {e.proposal.name}: IS夏普 {e.train['sharpe']:.2f}, "
                f"OOS夏普 {e.valid['sharpe']:.2f}, 是否通过 {e.accepted}"
            )
        tried = "\n".join(lines) or "（暂无）"
        # 真实交易反馈：只作「往哪个方向找」的参考，不能替代回测闸门
        ev = ""
        if self.live_evidence:
            ev = (
                "\n真实交易反馈（来自实际平仓回合，样本有限）：\n"
                f"{self.live_evidence}\n"
                "用法：只用来判断下一步该往哪个方向找因子（例如回吐大 → 找择时/止盈类因子，"
                "某类退出持续亏 → 找能提前识别该类行情的因子）。"
                "不要为了迎合这份反馈反复提同一类因子，也不要把它当拟合目标。\n"
            )
        prompt = (
            f"数据：{FEW_SHOT}\n\n"
            f"已尝试的因子及结果：\n{tried}\n"
            f"{ev}\n"
            f"这是第 {round_idx + 1} 轮。请提出一个与上面都不同的新因子，"
            "优先解释为什么它在加密永续合约上可能有效（资金费率、拥挤度、波动率聚集等）。"
        )
        mock = self.fallback.propose(history, round_idx)
        data, resp = self.client.json(
            SYSTEM_FACTOR_WRITER,
            prompt,
            FACTOR_SCHEMA,
            {"name": mock.name, "hypothesis": mock.hypothesis, "code": mock.code},
        )
        code = str(data.get("code", "")).strip()
        code = code.replace("```python", "").replace("```", "").strip()
        name = str(data.get("name", f"llm_{round_idx}"))[:40]
        proposal = Proposal(
            name=name or f"llm_{round_idx}",
            family="llm",
            hypothesis=str(data.get("hypothesis", ""))[:300],
            code=code,
            source="llm",
        )
        try:
            validate_factor_code(proposal.code)
        except FactorError:
            return self.fallback.propose(history, round_idx)
        return proposal


def build_proposer(cfg: AppConfig, llm, verbose: bool = True,
                   live_evidence: str = "") -> Proposer:
    provider = getattr(llm, "provider", cfg.llm.provider).lower()
    if provider == "mock":
        return TemplateProposer()
    # 峰时省钱：因子研究可延后，峰时不调用 LLM，改用模板提议器；谷时（半价）再跑 LLM
    if getattr(cfg.llm, "offpeak_research_only", True) and not llm.is_offpeak():
        if verbose:
            print("[research] 峰时：改用模板提议器（谷时再调用 LLM，省一半费用）")
        return TemplateProposer()
    if verbose:
        note = "（含真实交易反馈）" if live_evidence else ""
        print(f"[research] 使用 LLM 提议因子：{provider}/{cfg.llm.model}{note}")
    return LLMProposer(llm, live_evidence=live_evidence)


# --------------------------------------------------------------------------
# 研究循环
# --------------------------------------------------------------------------
def _metrics_subset(result: BacktestResult) -> dict:
    m = result.metrics
    keep = [
        "sharpe", "sortino", "total_return", "cagr", "max_drawdown", "calmar",
        "win_rate", "profit_factor", "n_trades", "fee_drag", "funding_drag",
        "avg_exposure", "liquidated",
    ]
    out = {}
    for k in keep:
        v = m.get(k)
        out[k] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
    return out


def _score(train: dict, valid: dict) -> float:
    """评分 = OOS 夏普 − 过拟合惩罚。"""
    if valid["n_trades"] < 3:
        return -math.inf
    overfit = max(0.0, train["sharpe"] - valid["sharpe"])
    return valid["sharpe"] - 0.5 * overfit


class ResearchLoop:
    def __init__(self, cfg: AppConfig, df: pd.DataFrame, proposer: Proposer | None = None,
                 verbose: bool = True, n_at_apply: int = 0) -> None:
        self.cfg = cfg
        self.df = df
        self.proposer = proposer or TemplateProposer()
        self.verbose = verbose
        # 因子落库时的归因起点：之后只用「这一笔之后的平仓」评判它是否真的有用，
        # 否则会被落库前的历史稀释（进化账本的 attribute_change 依赖这个数）。
        self.n_at_apply = max(0, int(n_at_apply or 0))
        self.train, self.valid, self.test = split_market(
            df, cfg.research.train_frac, cfg.research.valid_frac
        )
        self.history: list[Evaluation] = []

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def _evaluate(self, proposal: Proposal) -> Evaluation:
        t0 = time.time()
        ev = Evaluation(proposal=proposal)
        try:
            validate_factor_code(proposal.code)
            # 在**完整历史**上算一次因子值，再按时间边界切三段。
            # 分段各自重算会让 valid/test 段开头缺少 rolling/ewm 的 warmup，
            # 因子值与实盘不一致、OOS 夏普被系统性压低——那是方法学产物，
            # 不能据此判定因子无效。
            values_all, meta = run_factor(proposal.code, self.df)
            problems = check_lookahead(meta)
            ev.lookahead = problems
            if problems:
                ev.error = "前视偏差: " + problems[0]
                ev.elapsed = time.time() - t0
                return ev

            def _seg(seg):
                return values_all.reindex(seg.index)

            target = factor_to_target(_seg(self.train), window=200, cap=1.0)
            train_res = run_backtest(self.train, target, self.cfg.backtest, self.cfg.market.interval)

            target_v = factor_to_target(_seg(self.valid), window=200, cap=1.0)
            valid_res = run_backtest(self.valid, target_v, self.cfg.backtest, self.cfg.market.interval)

            ev.train = _metrics_subset(train_res)
            ev.valid = _metrics_subset(valid_res)
            ev.score = _score(ev.train, ev.valid)
            ev.ok = True

            if ev.train["sharpe"] <= 0:
                ev.reject_reason = "IS 夏普为负"
            elif ev.valid["sharpe"] < self.cfg.research.min_oos_sharpe:
                ev.reject_reason = f"OOS 夏普低于阈值 {self.cfg.research.min_oos_sharpe}"
            elif ev.valid["n_trades"] < 3:
                ev.reject_reason = "交易次数过少"
            elif ev.score == -math.inf:
                ev.reject_reason = "未通过评分"
            else:
                ev.accepted = True
        except FactorError as exc:
            ev.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - 单个因子失败不应中断整个研究
            ev.error = f"{type(exc).__name__}: {exc}"
        ev.elapsed = time.time() - t0
        return ev

    def run(self, iterations: int | None = None) -> dict:
        n = iterations or self.cfg.research.iterations
        self._log(f"研究开始：train={len(self.train)} valid={len(self.valid)} test={len(self.test)} bars\n")
        for i in range(n):
            proposal = self.proposer.propose(self.history, i)
            ev = self._evaluate(proposal)
            self.history.append(ev)
            self._log(f"[{i + 1:02d}/{n}] {ev.brief()}  ({ev.elapsed:.1f}s)")

        accepted = [e for e in self.history if e.accepted]
        best = max(accepted, key=lambda e: e.score) if accepted else None

        holdout = None
        if best is not None:
            # 留出集只跑这一次（同样用全历史算值、再切 test 段）
            values_all, _ = run_factor(best.proposal.code, self.df)
            values_t = values_all.reindex(self.test.index)
            target_t = factor_to_target(values_t, window=200, cap=1.0)
            res_t = run_backtest(self.test, target_t, self.cfg.backtest, self.cfg.market.interval)
            holdout = _metrics_subset(res_t)
            self._log(
                f"\n最终留出集（test，仅供评估不可用于选择）："
                f"夏普 {holdout['sharpe']:.2f} 收益 {holdout['total_return']:.1%} "
                f"回撤 {holdout['max_drawdown']:.1%}"
            )

        report = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": self.cfg.market.symbol,
            "interval": self.cfg.market.interval,
            "splits": {"train": len(self.train), "valid": len(self.valid), "test": len(self.test)},
            "iterations": n,
            "accepted": len(accepted),
            "best": asdict(best) if best else None,
            "holdout": holdout,
            "history": [asdict(e) for e in self.history],
        }
        self._persist(report, best)
        return report

    # ------------------------------------------------------------------
    def _persist(self, report: dict, best: Evaluation | None) -> None:
        out_dir = Path(self.cfg.research.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "leaderboard.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        (out_dir / "report.md").write_text(self._markdown(report, best), encoding="utf-8")

        if best is not None:
            lib = Path(self.cfg.research.factor_library)
            lib.mkdir(parents=True, exist_ok=True)
            header = (
                f'# 由 QeeLin-Agent 研究循环生成\n'
                f'# factor: {best.proposal.name}\n'
                f'# hypothesis: {best.proposal.hypothesis}\n'
                f'# IS sharpe: {best.train["sharpe"]:.2f} | OOS sharpe: {best.valid["sharpe"]:.2f}\n'
                f'# holdout sharpe: {(report.get("holdout") or {}).get("sharpe", float("nan")):.2f}\n\n'
            )
            (lib / f"{best.proposal.name}.py").write_text(header + best.proposal.code, encoding="utf-8")
            # 进化账本：因子被采纳（写进 factor_library）也要留痕，否则「进化采纳率」
            # 只统计得到参数改动。它的证据是研究三闸（IS / OOS / 留出集），
            # 属于事前闸门，比事后归因更强。
            try:
                from .evolution import get_governor

                holdout = report.get("holdout") or {}
                gov = get_governor(self.cfg.runs_dir)
                gov.record_applied(
                    kind="factor",
                    target=f"factor_library.{best.proposal.name}",
                    before=None,
                    after="新建因子文件",
                    source="factor_research",
                    rationale=str(best.proposal.hypothesis or ""),
                    kpi=None,
                    n_at_apply=self.n_at_apply,
                    evidence={
                        "kind": "research_gate",
                        "is_sharpe": round(float(best.train.get("sharpe") or 0.0), 4),
                        "oos_sharpe": round(float(best.valid.get("sharpe") or 0.0), 4),
                        "holdout_sharpe": (
                            round(float(holdout.get("sharpe")), 4)
                            if holdout.get("sharpe") is not None else None
                        ),
                    },
                    note=f"family={best.proposal.family}",
                )
            except Exception:  # noqa: BLE001 - 记账失败不影响因子落库
                pass

    def _markdown(self, report: dict, best: Evaluation | None) -> str:
        lines = [
            f"# 因子研究报告 — {report['symbol']} {report['interval']}",
            "",
            f"- 生成时间：{report['generated_at']}",
            f"- 数据切分：train {report['splits']['train']} / valid {report['splits']['valid']} / test {report['splits']['test']} 根 K 线",
            f"- 迭代 {report['iterations']} 次，通过 {report['accepted']} 个",
            "",
            "## 排行榜",
            "",
            "| 因子 | 家族 | IS夏普 | OOS夏普 | OOS回撤 | 评分 | 结论 |",
            "|---|---|---|---|---|---|---|",
        ]
        for e in sorted(report["history"], key=lambda x: (x["score"] if x["score"] is not None else -9e9), reverse=True):
            if not e["ok"]:
                lines.append(f"| {e['proposal']['name']} | {e['proposal']['family']} | - | - | - | - | 失败：{e['error'][:40]} |")
                continue
            lines.append(
                f"| {e['proposal']['name']} | {e['proposal']['family']} | "
                f"{e['train']['sharpe']:.2f} | {e['valid']['sharpe']:.2f} | "
                f"{e['valid']['max_drawdown']:.1%} | {e['score']:.2f} | "
                f"{'通过' if e['accepted'] else e['reject_reason']} |"
            )
        if best:
            h = report.get("holdout") or {}
            lines += [
                "",
                "## 冠军因子",
                "",
                f"**{best.proposal.name}** — {best.proposal.hypothesis}",
                "",
                "```python",
                best.proposal.code.strip(),
                "```",
                "",
                f"- 样本内（train）夏普：{best.train['sharpe']:.2f}",
                f"- 验证集（valid）夏普：{best.valid['sharpe']:.2f}",
                f"- 留出集（test）夏普：{h.get('sharpe', float('nan')):.2f}",
                f"- 衰减（valid → test）：{best.valid['sharpe'] - h.get('sharpe', 0):.2f}",
                "",
                "> 衰减越大越可疑。留出集只跑了一次，没有参与任何选择。",
            ]
        else:
            lines += ["", "## 结论", "", "本轮没有任何因子通过验证集门槛。**这是正常结果，不要降低门槛去凑一个冠军。**"]
        return "\n".join(lines)