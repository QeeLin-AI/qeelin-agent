"""多智能体决策流水线。

    ┌──────────────┐
    │ 市场快照(代码) │  所有数字由 pandas 计算，LLM 不参与
    └──────┬───────┘
           ▼
    ┌──────────────────────────┐
    │ 分析师团队：技术/资金费率/情绪 │
    └──────┬───────────────────┘
           ▼
    ┌──────────────────────────┐
    │ 研究员：多空辩论 N 轮        │
    └──────┬───────────────────┘
           ▼
    ┌──────────────────────────┐
    │ 交易员：输出结构化提案(JSON)  │
    └──────┬───────────────────┘
           ▼
    ┌──────────────────────────┐
    │ 风控官(LLM)：只能收紧        │
    └──────┬───────────────────┘
           ▼
    ┌──────────────────────────┐
    │ 风控引擎(代码)：最终否决权    │
    └──────┬───────────────────┘
           ▼
        订单计划（默认只落盘，不发送）
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from ..config import AppConfig
from ..costs import evaluate_execution_cost
from ..risk import AccountState, RiskDecision, RiskEngine
from ..factors.signal import compute_factor_signal
from ..factors.shortterm import compute_breakout_signal, compute_frame_metrics
from ..sentiment import get_sentiment_source
from .llm import LLMClient
from .roles import (
    AnalystReport,
    Debate,
    MarketSnapshot,
    propose_trade,
    risk_review,
    run_analysts,
    run_debate,
)


def _apply_factor_signal(proposal: dict, composite: float) -> None:
    """把因子复合信号作用到交易员提案上。

    - 因子与方向一致 → 按对齐度放大仓位（最多 1.4 倍）
    - 因子与方向相反 → 缩小仓位；因子强烈反对（|composite|>0.6）→ 直接观望
    不碰止损/止盈/杠杆，那些仍归交易员和风控管。
    """
    act = proposal.get("action", "flat")
    if act not in ("long", "short") or not composite:
        return
    dirn = 1.0 if act == "long" else -1.0
    align = dirn * composite
    size = float(proposal.get("size_pct", 0.0) or 0.0)
    if align >= 0:
        proposal["size_pct"] = round(size * (1.0 + 0.4 * min(1.0, align)), 4)
    elif abs(composite) > 0.6:
        proposal["action"] = "flat"
        proposal["size_pct"] = 0.0
    else:
        proposal["size_pct"] = round(size * (1.0 - abs(composite)), 4)


@dataclass
class Decision:
    ts: str
    symbol: str
    snapshot: dict
    analysts: list[dict]
    debate: dict
    proposal: dict
    risk_officer: dict
    risk_decision: dict
    order: dict | None
    degraded: bool = False


def round_price(x: float) -> float:
    """按价格量级保留小数位。

    原来固定 round(x, 2)：BTC 没问题，但 DOGE(0.07)、PEPE(0.00001) 这类会被抹成同一个价，
    导致空单止损落到现价下方、交易所报 -2021 Order would immediately trigger。
    """
    x = float(x)
    if x >= 1000:
        return round(x, 2)
    if x >= 100:
        return round(x, 3)
    if x >= 1:
        return round(x, 4)
    if x >= 0.1:
        return round(x, 5)
    if x >= 0.01:
        return round(x, 6)
    return round(x, 8)


def _build_order(proposal: dict, decision: RiskDecision, price: float, equity: float) -> dict | None:
    if not decision.approved or decision.action == "flat" or decision.size_pct <= 0:
        return None
    notional = decision.size_pct * equity * decision.leverage
    qty = notional / price
    stop = float(proposal.get("stop_loss_pct", 0.02))
    tp = float(proposal.get("take_profit_pct", 0.04))
    sign = 1.0 if decision.action == "long" else -1.0
    return {
        "symbol": proposal.get("symbol", ""),
        "side": "BUY" if decision.action == "long" else "SELL",
        "position_side": decision.action.upper(),
        "type": "MARKET",
        "quantity": round(qty, 6),
        "notional_usd": round(notional, 2),
        "leverage": decision.leverage,
        "entry_ref_price": price,
        "stop_loss_price": round_price(price * (1 - sign * stop)),
        "take_profit_price": round_price(price * (1 + sign * tp)),
        "risk_usd": round(notional * stop, 2),
        "max_loss_pct_of_equity": round(decision.size_pct * decision.leverage * stop, 4),
        "status": "PLANNED",  # 默认不发送
    }


class TradingPipeline:
    def __init__(self, cfg: AppConfig, llm: LLMClient | None = None) -> None:
        self.cfg = cfg
        self.llm = llm or LLMClient(cfg.llm)
        self.risk = RiskEngine(cfg.risk)

    def _short_breakout_signal(self, symbol: str, exchange: str = "binance") -> float:
        """中频波动突破信号：实时拉短周期 K 线，算唐奇安突破，失败返回 0（不干扰）。"""
        if self.cfg.market.source == "synthetic":
            return 0.0   # 合成/离线数据不做中频叠加，避免无谓联网
        interval = getattr(self.cfg.market, "short_interval", "5m") or "5m"
        bars = int(getattr(self.cfg.market, "short_bars", 200) or 200)
        try:
            from ..data import fetch_klines

            # prefer 必须跟当前下单的交易所一致，否则在 OKX 上跑却用币安的价格
            # 算突破；engine 的预热也是按同一家交易所落的盘，两边才对得上。
            df_short, _ = fetch_klines(symbol, interval, max(20, min(bars, 500)), prefer=exchange)
            return compute_breakout_signal(df_short)
        except Exception:  # noqa: BLE001 - 短周期拉不到/算不出时静默降级，不阻断决策
            return 0.0

    def _short_frames(self, symbol: str, exchange: str = "binance") -> dict:
        """拉 15m/5m 短周期 K 线，算多周期指标，注入分析师视野。

        与 `_short_breakout_signal`（只输出一个标量给因子）不同，这里把短周期的
        动能/RSI/趋势/突破/支撑阻力打包成结构化指标，让分析师在同一份 prompt 里
        做「1h 定方向、15m/5m 定时机」的多周期共振判断。任何一档拉取失败都静默
        跳过该档，绝不因为短周期取数失败阻断整条决策链。
        """
        if self.cfg.market.source == "synthetic":
            return {}   # 合成/离线数据不拉短周期，避免无谓联网
        from ..data import fetch_klines

        frames: dict = {}
        for interval in ("15m", "5m"):
            try:
                df_short, _ = fetch_klines(symbol, interval, 120, prefer=exchange)
                m = compute_frame_metrics(df_short)
                if m:
                    frames[interval] = m
            except Exception:  # noqa: BLE001 - 单档失败不影响其它档
                continue
        return frames

    def run(
        self,
        df: pd.DataFrame,
        state: AccountState | None = None,
        debate_rounds: int = 2,
        persist: bool = True,
        exchange_max_leverage: float | None = None,
        positions: list[dict] | None = None,
        analyst_weights: list[float] | None = None,
        analyst_calibrated: bool = False,
        exchange: str = "binance",
        live_price: float | None = None,
    ) -> Decision:
        state = state or AccountState(equity=self.cfg.backtest.initial_equity,
                                      peak_equity=self.cfg.backtest.initial_equity,
                                      day_start_equity=self.cfg.backtest.initial_equity)
        snap = MarketSnapshot.from_df(
            df, self.cfg.market.symbol, self.cfg.market.interval, price_override=live_price
        )
        # 短周期多空帧：让分析师/研究员/交易员在同一份数据里看到 15m/5m 的
        # 动能 / RSI / 趋势 / 突破 / 支撑阻力，做多周期共振判断。
        snap.short_frames = self._short_frames(snap.symbol, exchange)
        # A1：情绪/新闻源（默认价格行为代理，配了 CryptoPanic 才用真实舆情）
        sentiment = get_sentiment_source(self.cfg.sentiment).fetch(snap.symbol, snap)
        reports: list[AnalystReport] = run_analysts(self.llm, snap, sentiment=sentiment)
        debate: Debate = run_debate(
            self.llm, snap, reports, rounds=debate_rounds,
            weights=analyst_weights, calibrated=analyst_calibrated,
        )
        proposal, deg_trader = propose_trade(
            self.llm, snap, reports, debate,
            caps={
                "max_leverage": self.cfg.risk.max_leverage,
                "max_position_pct": self.cfg.risk.max_position_pct,
            },
        )
        proposal["symbol"] = snap.symbol
        # 仓位归因：每一步都留痕，否则多个环节连乘后无法解释最终仓位
        trader_size = float(proposal.get("size_pct", 0.0) or 0.0)
        sizing_trace = [{"step": "交易员", "size_pct": round(trader_size, 6)}]

        # 因子信号注入：研究产出的因子参与决策（方向一致放大，相反缩小/观望）
        fsig = compute_factor_signal(self.cfg.research.factor_library, df)
        # 中频叠加：实时拉短周期 K 线算波动突破信号，有突破时并入复合信号
        short_sig = self._short_breakout_signal(snap.symbol, exchange)
        if short_sig != 0.0:
            fsig["signals"].append(
                {
                    "name": f"breakout_{self.cfg.market.short_interval}",
                    "target": short_sig,
                    "weight": 1.0,
                    "holdout_sharpe": 1.0,
                    "short_term": True,
                }
            )
            fsig["composite"] = round((fsig["composite"] + short_sig) / 2.0, 4)
        proposal["factor_composite"] = fsig["composite"]
        proposal["factor_signals"] = fsig["signals"]
        # B4：因子贡献度 = 各因子 target×weight，归一化为占比，用于「哪些因子在赚钱」可视化
        total_w = sum(max(0.0, float(s.get("weight", 0.0))) for s in fsig["signals"]) or 1.0
        contributions = []
        for s in fsig["signals"]:
            w = max(0.0, float(s.get("weight", 0.0)))
            contrib = float(s.get("target", 0.0)) * w
            contributions.append({
                "name": s.get("name"),
                "target": s.get("target"),
                "weight": s.get("weight"),
                "contribution": round(contrib, 4),
                "share": round(contrib / total_w, 4),
                "holdout_sharpe": s.get("holdout_sharpe"),
            })
        proposal["factor_contributions"] = contributions
        _apply_factor_signal(proposal, fsig["composite"])
        sizing_trace.append({"step": "因子信号", "size_pct": round(float(proposal.get("size_pct", 0.0)), 6),
                             "detail": f"复合信号 {fsig['composite']:+.2f}"})

        officer, deg_risk = risk_review(self.llm, snap, proposal, state, self.cfg.risk, positions=positions)

        adjusted = dict(proposal)
        if officer["verdict"] == "reject":
            adjusted["action"] = "flat"
            adjusted["size_pct"] = 0.0
            adjusted["officer_veto"] = True
        else:
            adjusted["size_pct"] = float(adjusted.get("size_pct", 0.0)) * officer["size_multiplier"]
        sizing_trace.append({"step": "风控官", "size_pct": round(float(adjusted["size_pct"]), 6),
                             "detail": f"{officer['verdict']} ×{officer['size_multiplier']:.2f}"
                                       + ("（risk 模式：不参与最终仓位）"
                                          if self.cfg.risk.sizing_mode == "risk" else "")})

        decision = self.risk.check(
            adjusted,
            state,
            atr_pct=snap.atr_pct,
            vol_percentile=snap.vol_percentile,
            exchange_max_leverage=exchange_max_leverage,
            positions=positions,
        )
        sizing_trace.append({"step": "风控引擎", "size_pct": round(decision.size_pct, 6),
                             "detail": "；".join(decision.reasons[:2])})

        # 执行成本闸放在**风控终审之后**：用最终仓位/杠杆核算，才不会被
        # sizing_mode=risk 的反推覆盖，也不会用被风控压小前的名义高估参与率。
        final_proposal = dict(adjusted)
        final_proposal["action"] = decision.action
        final_proposal["size_pct"] = decision.size_pct
        final_proposal["leverage"] = decision.leverage
        gate = evaluate_execution_cost(
            final_proposal, df, self.cfg, price=snap.price,
            equity=state.equity, atr_pct=snap.atr_pct,
        )
        proposal["cost_gate"] = gate.to_dict()
        if decision.approved and decision.action != "flat":
            if not gate.approved:
                decision.approved = False
                decision.action = "flat"
                decision.size_pct = 0.0
                decision.reasons.append("执行成本闸拒绝：" + "；".join(gate.reasons))
                decision.checks.append({"check": "cost_gate", "passed": False,
                                        "detail": f"成本 {gate.cost_pct:.3%} / 止盈 {gate.expected_pct:.2%}"})
            elif gate.size_multiplier < 1.0:
                before = decision.size_pct
                decision.size_pct = round(before * gate.size_multiplier, 6)
                # 缩仓后必须重检「最小仓位」：风控引擎的 size<0.005 闸在 check() 内部，
                # 发生在缩仓之前；不重检会生成 0.0006 这种交易所都懒得接的迷你单。
                if decision.size_pct < 0.005:
                    decision.approved = False
                    decision.action = "flat"
                    decision.size_pct = 0.0
                    decision.reasons.append(
                        "执行成本闸缩仓后低于最小仓位 0.5%，放弃本次开仓"
                    )
                    decision.checks.append({
                        "check": "cost_gate_min_size", "passed": False,
                        "detail": f"缩仓前 {before:.4f} → 低于 0.005，已置 flat",
                    })
                else:
                    note = (
                        "（risk 模式下这是二次缩仓，最终仓位不再等于风险反推值）"
                        if self.cfg.risk.sizing_mode == "risk" else ""
                    )
                    decision.reasons.append(
                        f"执行成本闸：参与率 {gate.participation:.1%} 偏高，"
                        f"最终仓位 ×{gate.size_multiplier:.2f}{note}"
                    )
        if officer["verdict"] == "reject":
            # 风控官否决与「策略主动观望」是两件事：前者要能在报告里被审计到，
            # 否则页面只显示「风控引擎：通过」，看起来像风控放行了。
            decision.checks.append({
                "check": "risk_officer", "passed": False,
                "detail": "风控官否决（veto），最终动作为 flat",
            })
            decision.reasons.insert(0, "风控官否决：交易提案被拒绝")
        sizing_trace.append({"step": "执行成本闸", "size_pct": round(decision.size_pct, 6),
                             "detail": f"成本 {gate.cost_pct:.3%} / 止盈 {gate.expected_pct:.2%}"})
        proposal["sizing_trace"] = sizing_trace
        proposal["final_size_pct"] = round(decision.size_pct, 6)

        order = _build_order(adjusted, decision, snap.price, state.equity)
        if order is not None:
            order["symbol"] = snap.symbol

        result = Decision(
            ts=time.strftime("%Y-%m-%d %H:%M:%S"),
            symbol=snap.symbol,
            snapshot=snap.to_dict(),
            analysts=[asdict(r) for r in reports],
            debate={
                "thesis": debate.thesis,
                "score": round(debate.score, 4),
                "bull_case": debate.bull_case,
                "bear_case": debate.bear_case,
                "rounds": debate.rounds,
            },
            proposal=proposal,
            risk_officer=officer,
            risk_decision=decision.to_dict(),
            order=order,
            degraded=(deg_trader or deg_risk or debate.degraded
                      or any("LLM 降级" in " ".join(r.data_flags) for r in reports)),
        )
        if persist:
            self._persist(result)
        return result

    def _persist(self, d: Decision) -> None:
        out = Path(self.cfg.runs_dir) / "decisions"
        out.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        (out / f"{stamp}_{d.symbol}.json").write_text(
            json.dumps(asdict(d), indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        (out / "latest.md").write_text(render_markdown(d), encoding="utf-8")


def run_decision(cfg: AppConfig, df: pd.DataFrame, **kwargs) -> Decision:
    return TradingPipeline(cfg).run(df, **kwargs)


def render_markdown(d: Decision) -> str:
    s = d.snapshot
    lines = [
        f"# 决策报告 — {d.symbol}",
        "",
        f"生成时间：{d.ts} ｜ 最新价：{s['price']:,.1f} ｜ 波动率分位：{s['vol_percentile']:.0%}"
        + (" ｜ ⚠ LLM 降级" if d.degraded else ""),
        "",
        "## 1. 分析师团队",
        "",
        "| 角色 | 立场 | 观点 |",
        "|---|---|---|",
    ]
    for a in d.analysts:
        stance = 0.0 if abs(a["stance"]) < 0.005 else a["stance"]
        lines.append(f"| {a['role']} | {stance:+.2f} | {a['summary']} |")

    lines += [
        "",
        "## 2. 研究辩论",
        "",
        f"**多头**：{d.debate['bull_case']}",
        "",
        f"**空头**：{d.debate['bear_case']}",
        "",
        f"**结论**：{d.debate['thesis']}",
        "",
        "## 3. 交易员提案",
        "",
        f"- 方向：**{d.proposal['action']}**",
        f"- 仓位：{d.proposal['size_pct']:.1%}（杠杆 {d.proposal['leverage']:.1f}x）",
        f"- 止损 / 止盈：{d.proposal['stop_loss_pct']:.2%} / {d.proposal['take_profit_pct']:.2%}",
        f"- 置信度：{d.proposal['confidence']:.2f}",
        f"- 理由：{d.proposal['rationale']}",
        "",
        "## 4. 风控",
        "",
        f"- 执行成本闸：{'通过' if d.proposal.get('cost_gate', {}).get('approved', True) else '拒绝'}"
        f"（成本 {d.proposal.get('cost_gate', {}).get('cost_pct', 0):.3%}"
        f" / 止盈 {d.proposal.get('cost_gate', {}).get('expected_pct', 0):.2%}，"
        f"净边际 {d.proposal.get('cost_gate', {}).get('net_edge_pct', 0):+.2%}）",
        "",
        f"风控官结论：**{d.risk_officer['verdict']}**（仓位乘数 {d.risk_officer['size_multiplier']:.2f}）",
        "",
    ]
    lines += [f"- {r}" for r in d.risk_officer["reasons"]]
    lines += [
        "",
        f"风控引擎：**{'通过' if d.risk_decision['approved'] else '拒绝'}**，"
        f"最终方向 {d.risk_decision['action']}，仓位 {d.risk_decision['size_pct']:.2%}，"
        f"杠杆 {d.risk_decision['leverage']:.1f}x",
        "",
    ]
    lines += [f"- {r}" for r in d.risk_decision["reasons"]]

    trace = d.proposal.get("sizing_trace") or []
    if trace:
        lines += ["", "## 5. 仓位归因", "", "| 环节 | 仓位 | 说明 |", "|---|---|---|"]
        for t in trace:
            lines.append(
                f"| {t.get('step', '')} | {float(t.get('size_pct', 0.0)):.2%} | {t.get('detail', '')} |"
            )

    lines += ["", "## 6. 订单计划", ""]
    if d.order:
        o = d.order
        lines += [
            f"- {o['side']} {o['quantity']} {o['symbol']}（名义 {o['notional_usd']:,.0f} USDT，{o['leverage']:.1f}x）",
            f"- 止损 {o['stop_loss_price']:,.1f} ｜ 止盈 {o['take_profit_price']:,.1f}",
            f"- 单笔最大亏损 {o['risk_usd']:,.0f} USDT（权益的 {o['max_loss_pct_of_equity']:.2%}）",
            f"- 状态：{o['status']}（默认不发送到交易所）",
        ]
    else:
        lines.append("- 无订单：风控未通过或策略选择观望。")
    lines += ["", "> 本报告由本地程序生成，不构成投资建议。"]
    return "\n".join(lines)