"""确定性风控层。

这一层是整个系统里**唯一有最终否决权**的组件，它不调用 LLM。
设计原则：LLM 可以建议，代码负责兜底。所有上限都来自配置，不可被提示词改写。

检查顺序 = 从「一票否决」到「参数夹紧」，任何一项触发都会写进 reasons，
最终订单和风控理由一起落盘，事后可审计。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import RiskConfig


@dataclass
class AccountState:
    equity: float = 10_000.0
    peak_equity: float = 10_000.0
    day_start_equity: float = 10_000.0
    position_notional: float = 0.0
    consecutive_losses: int = 0

    @property
    def drawdown(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return self.equity / self.peak_equity - 1.0

    @property
    def daily_pnl_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return self.equity / self.day_start_equity - 1.0


@dataclass
class RiskDecision:
    approved: bool
    action: str
    size_pct: float
    leverage: float
    reasons: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "approved": self.approved,
            "action": self.action,
            "size_pct": round(self.size_pct, 6),
            "leverage": round(self.leverage, 4),
            "reasons": self.reasons,
            "checks": self.checks,
        }


class RiskEngine:
    def __init__(self, cfg: RiskConfig) -> None:
        self.cfg = cfg

    def _record(self, checks: list, name: str, passed: bool, detail: str) -> bool:
        checks.append({"check": name, "passed": passed, "detail": detail})
        return passed

    def check(
        self,
        proposal: dict,
        state: AccountState,
        atr_pct: float | None = None,
        vol_percentile: float | None = None,
        exchange_max_leverage: float | None = None,
        positions: list[dict] | None = None,
    ) -> RiskDecision:
        cfg = self.cfg
        reasons: list[str] = []
        checks: list[dict] = []

        action = str(proposal.get("action", "flat")).lower()
        size = float(proposal.get("size_pct", 0.0) or 0.0)
        lev = float(proposal.get("leverage", 1.0) or 1.0)
        confidence = float(proposal.get("confidence", 0.0) or 0.0)
        stop = float(proposal.get("stop_loss_pct", 0.0) or 0.0)

        # 1. 熔断文件
        if not self._record(checks, "kill_switch", not Path(cfg.kill_switch_file).exists(),
                            f"{cfg.kill_switch_file} 不存在"):
            return RiskDecision(False, "flat", 0.0, 0.0, ["熔断开关已触发，禁止任何下单"], checks)

        # 2. 总回撤
        dd = state.drawdown
        if not self._record(checks, "max_drawdown", dd > -cfg.max_drawdown_pct,
                            f"当前回撤 {dd:.2%}，上限 -{cfg.max_drawdown_pct:.0%}"):
            return RiskDecision(False, "flat", 0.0, 0.0,
                                [f"总回撤 {dd:.1%} 超过上限，系统停机"], checks)

        # 3. 当日亏损
        daily = state.daily_pnl_pct
        if not self._record(checks, "daily_loss", daily > -cfg.daily_loss_limit_pct,
                            f"当日盈亏 {daily:.2%}，上限 -{cfg.daily_loss_limit_pct:.0%}"):
            return RiskDecision(False, "flat", 0.0, 0.0,
                                [f"当日亏损 {daily:.1%} 触及熔断，今日不再开仓"], checks)

        # 4. 空仓 / 置信度
        if action == "flat":
            return RiskDecision(True, "flat", 0.0, 0.0, ["交易员选择观望"], checks)
        if not self._record(checks, "confidence", confidence >= cfg.min_confidence,
                            f"置信度 {confidence:.2f}，门槛 {cfg.min_confidence}"):
            return RiskDecision(False, "flat", 0.0, 0.0,
                                [f"置信度 {confidence:.2f} 低于门槛 {cfg.min_confidence}"], checks)

        # 5. 杠杆：先应用人工覆盖，再受上限夹紧
        if cfg.leverage_override and cfg.leverage_override > 0:
            if abs(lev - cfg.leverage_override) > 1e-9:
                reasons.append(f"按设置强制使用 {cfg.leverage_override:.1f}x（决策给的 {lev:.1f}x）")
            lev = float(cfg.leverage_override)
        # 真正的上限 = min(交易所支持的最大杠杆, 配置上限)。
        # 之前取的是 max，导致 max_leverage 形同虚设：交易所给 75x 时它能突破配置。
        cap = float(cfg.max_leverage or 0.0)
        if exchange_max_leverage:
            ex_cap = float(exchange_max_leverage)
            cap = min(cap, ex_cap) if cap > 0 else ex_cap
        if cap <= 0:
            cap = 1.0
        if lev > cap:
            reasons.append(f"杠杆 {lev:.1f}x 超过上限，压到 {cap:.1f}x")
            lev = cap
        if cfg.min_leverage and cfg.min_leverage > lev:
            floor = min(float(cfg.min_leverage), cap)
            if floor > lev:
                reasons.append(f"按杠杆下限托底到 {floor:.1f}x")
                lev = floor

        # 仓位：可选用「按风险上限反推」（旧配置里的 max 视为 llm，不再放大仓位）
        mode = cfg.sizing_mode if cfg.sizing_mode in ("llm", "risk") else "llm"
        if mode == "risk" and stop > 0 and cfg.max_loss_per_trade_pct > 0:
            upstream_size = float(size)   # 交易员 / 因子 / 风控官共同给出的建议仓位
            target_ratio = cfg.max_loss_per_trade_pct / max(stop, 1e-4)   # 名义敞口 / 权益
            # 杠杆只降不升：以前为了让「单笔风险」凑满上限，会按 target/max_position_pct
            # 自动抬高杠杆，紧止损时能把杠杆推到 20~30x（反向 3~4% 就接近强平）——风险
            # 并没变，爆仓概率却成倍放大。现在宁可让实际风险低于上限，也不抬杠杆。
            size = target_ratio / lev if lev > 0 else 0.0
            reasons.append(
                f"按风险上限反推：目标名义 {target_ratio:.2f}x 权益 → 仓位 {size:.1%} @ {lev:.2f}x"
            )
            if abs(upstream_size - size) > 1e-6:
                # 明确写出「静默覆盖」，否则决策报告里上游环节看起来都生效了，实际没有
                reasons.append(
                    f"此模式以反推值为准，覆盖上游建议仓位 {upstream_size:.2%}"
                    "（交易员/因子/风控官的仓位调整不参与最终仓位，仅方向与拒绝仍有效）"
                )

        # 6. 仓位夹紧（占权益比例）
        if size > cfg.max_position_pct:
            reasons.append(f"仓位 {size:.1%} 超过上限，压到 {cfg.max_position_pct:.1%}")
            size = cfg.max_position_pct
        if size <= 0:
            return RiskDecision(False, "flat", 0.0, 0.0, reasons + ["仓位为 0"], checks)

        # 7. 名义敞口上限（= 保证金上限 × 杠杆上限）
        notional = size * lev * state.equity
        notional_cap = cfg.max_position_pct * cap * state.equity
        if notional > notional_cap and notional_cap > 0:
            scale = notional_cap / notional
            size *= scale
            reasons.append(f"名义敞口超限，仓位按 {scale:.2f} 缩放")

        # 8. 止损必须存在且在合理区间
        if not self._record(checks, "stop_loss", 0.002 <= stop <= 0.10,
                            f"止损 {stop:.2%}，要求 0.2%~10%"):
            return RiskDecision(False, "flat", 0.0, 0.0, reasons + ["止损缺失或不在合理区间"], checks)

        # 9. 单笔最大亏损 = 仓位 × 杠杆 × 止损，不得超过权益的 max_loss_per_trade
        loss_pct = size * lev * stop
        if loss_pct > cfg.max_loss_per_trade_pct * 1.001:
            scale = cfg.max_loss_per_trade_pct / loss_pct
            size *= scale
            reasons.append(f"单笔风险 {loss_pct:.2%} 超限，仓位按 {scale:.2f} 缩放")

        # 10. 高波动环境下减仓
        if vol_percentile is not None and vol_percentile > 0.9:
            size *= 0.5
            reasons.append(f"波动率处于 {vol_percentile:.0%} 分位，仓位减半")
        if atr_pct is not None and atr_pct > 0.05:
            size *= 0.7
            reasons.append(f"ATR {atr_pct:.1%} 偏高，仓位再打七折")

        size = max(0.0, min(size, cfg.max_position_pct))
        if size < 0.005:
            return RiskDecision(False, "flat", 0.0, 0.0, reasons + ["缩放后仓位过小，放弃"], checks)

        # 11. 组合层风控（A3）：多品种同时持仓时的总敞口与相关性敞口
        positions = positions or []
        equity = state.equity
        dirn = 1.0 if action == "long" else -1.0
        proposed_notional = size * lev * equity
        existing_notional = float(sum(float(p.get("notional", 0.0)) for p in positions))
        total_notional = existing_notional + proposed_notional
        cap_total = cfg.max_portfolio_notional_pct * equity
        if not self._record(checks, "portfolio_notional", total_notional <= cap_total,
                            f"组合名义敞口 {total_notional:,.0f} / 上限 {cap_total:,.0f}"):
            return RiskDecision(False, "flat", 0.0, 0.0,
                                [f"组合总敞口 {total_notional:,.0f} 超过上限 {cap_total:,.0f}"
                                 f"（当前 {cfg.max_portfolio_notional_pct:.1f}x 权益，"
                                 "可在 设置→风险控制→组合名义敞口上限 调整），本次不开仓"], checks)

        # 同一相关性组内、与本次同向的敞口（含本次）不得越线
        group = next((g for g in cfg.correlation_groups if proposal.get("symbol") in g), [])
        same_dir_notional = proposed_notional
        for p in positions:
            if p.get("symbol") in group:
                p_dirn = 1.0 if str(p.get("side", "")).upper().startswith("L") else -1.0
                if abs(p_dirn - dirn) < 1e-9:
                    same_dir_notional += float(p.get("notional", 0.0))
        cap_corr = cfg.correlated_exposure_pct * equity
        if group and not self._record(checks, "correlated_exposure",
                                     same_dir_notional <= cap_corr,
                                     f"相关组同向敞口 {same_dir_notional:,.0f} / 上限 {cap_corr:,.0f}（{','.join(group)}）"):
            return RiskDecision(False, "flat", 0.0, 0.0,
                                [f"相关组 {','.join(group)} 同向敞口 {same_dir_notional:,.0f} "
                                 f"超过上限 {cap_corr:,.0f}（当前 {cfg.correlated_exposure_pct:.1f}x 权益，"
                                 "可在 设置→风险控制→相关组敞口上限 调整），拒绝同向加仓"],
                                checks)

        return RiskDecision(True, action, size, lev, reasons or ["通过全部风控检查"], checks)