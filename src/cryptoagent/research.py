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


class LLMProposer:
    """让 LLM 根据历史回测结果提出新因子；输出不合规时自动退回模板。"""

    def __init__(self, client, fallback: "TemplateProposer | None" = None) -> None:
        self.client = client
        self.fallback = fallback or TemplateProposer(seed=7)

    def propose(self, history: list[Evaluation], round_idx: int) -> Proposal:
        recent = [e for e in history if e.ok][-6:]
        lines = []
        for e in recent:
            lines.append(
                f"- {e.proposal.name}: IS夏普 {e.train['sharpe']:.2f}, "
                f"OOS夏普 {e.valid['sharpe']:.2f}, 是否通过 {e.accepted}"
            )
        tried = "\n".join(lines) or "（暂无）"
        prompt = (
            f"数据：{FEW_SHOT}\n\n"
            f"已尝试的因子及结果：\n{tried}\n\n"
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


def build_proposer(cfg: AppConfig, llm, verbose: bool = True) -> Proposer:
    provider = getattr(llm, "provider", cfg.llm.provider).lower()
    if provider == "mock":
        return TemplateProposer()
    # 峰时省钱：因子研究可延后，峰时不调用 LLM，改用模板提议器；谷时（半价）再跑 LLM
    if getattr(cfg.llm, "offpeak_research_only", True) and not llm.is_offpeak():
        if verbose:
            print("[research] 峰时：改用模板提议器（谷时再调用 LLM，省一半费用）")
        return TemplateProposer()
    if verbose:
        print(f"[research] 使用 LLM 提议因子：{provider}/{cfg.llm.model}")
    return LLMProposer(llm)


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
                 verbose: bool = True) -> None:
        self.cfg = cfg
        self.df = df
        self.proposer = proposer or TemplateProposer()
        self.verbose = verbose
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
            values, meta = run_factor(proposal.code, self.train)
            problems = check_lookahead(meta)
            ev.lookahead = problems
            if problems:
                ev.error = "前视偏差: " + problems[0]
                ev.elapsed = time.time() - t0
                return ev

            target = factor_to_target(values, window=200, cap=1.0)
            train_res = run_backtest(self.train, target, self.cfg.backtest, self.cfg.market.interval)

            # 在 valid 上重算（用 valid 自己的数据窗口，避免跨段泄漏）
            values_v, meta_v = run_factor(proposal.code, self.valid)
            if check_lookahead(meta_v):
                ev.error = "前视偏差（valid 段复现）"
                ev.elapsed = time.time() - t0
                return ev
            target_v = factor_to_target(values_v, window=200, cap=1.0)
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
            # 留出集只跑这一次
            values_t, _ = run_factor(best.proposal.code, self.test)
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