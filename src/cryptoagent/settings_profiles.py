"""交易参数与模型参数的「方案」：预设 + 一键恢复默认。

设计原则：

1. **方案只是一组 payload**，不直接 setattr。应用时走既有的
   `set_risk` / `set_position_settings` / `set_rotate` / `set_llm_settings`，因此沿用它们的
   范围校验、保证金不等式校验和「失败整体回滚」——不在第二个地方重复实现规则。
2. **每套方案内部自洽**：`单笔仓位上限 × 同时持仓 ≤ 保证金占用上限`，
   且 `单笔仓位上限 × 杠杆上限 ≤ 组合名义敞口上限`。这两个不等式写死在测试里，
   改方案数值时会被当场拦下。
3. **保守优先**：`balanced` 是出厂默认，也是「恢复默认」的目标，与 `AppConfig()`
   默认值逐项一致（「默认」只允许一个语义）；`frequency` 靠降低置信度门槛换频率，
   同时把单笔风险压小，使「满仓总风险」不高于 balanced。
4. **方案覆盖「模型设置」里的峰谷计价策略**（offpeak_research_only / peak_mode），
   但 provider / model / api_key / base_url 这类**接入凭据一律不进方案**——
   「恢复默认」绝不能清掉用户填好的 Key 或改掉接入地址。
"""

from __future__ import annotations

from typing import Any

# 方案名 → 分组 payload。键名必须与对应 setter 接受的一致。
PROFILES: dict[str, dict[str, Any]] = {
    "conservative": {
        "label": "保守",
        "summary": "门槛 0.55、单笔 0.5%、1 仓、杠杆 1~3x、回撤 12%。"
                   "部署完可以放着不管：只做高置信单，止损留足波动空间，"
                   "宁可几周不开仓也不冒险。适合先观察再决定是否加码。",
        "risk": {
            "sizing_mode": "risk",
            "max_loss_per_trade_pct": 0.005,
            "max_position_pct": 0.10,
            "max_concurrent_positions": 1,
            # 只持 1 仓，40% 保证金已足够；留 60% 缓冲应对浮亏与资金费
            "max_margin_usage_pct": 0.40,
            # 0.60 叠 1.5 盈亏比会长期不开仓（稳健变成不运行）；0.55 仍属高门槛
            "min_confidence": 0.55,
            "max_leverage": 3.0,
            "min_leverage": 1.0,
            "leverage_override": 0.0,
            "daily_loss_limit_pct": 0.03,
            "max_drawdown_pct": 0.12,
            "max_portfolio_notional_pct": 1.0,
            "correlated_exposure_pct": 0.8,
            # 只做高质量单：盈亏比要求更高、止损留够波动空间，
            # 辩论权重压低（少让模型改变公式判断）
            "min_risk_reward": 1.5,
            "min_stop_atr_mult": 0.8,
            "debate_score_weight": 0.2,
        },
        "position": {
            "enabled": True,
            "auto_tune": True,
            "llm_explain": True,
            "llm_shadow": True,
            "llm_offpeak_only": True,
            "bars_from_index": True,
            "peak_drawdown_exit_enabled": False,
            "peak_drawdown_r": 1.0,
            # 稳健档的退出规则：让趋势走完，少折腾
            "max_hold_bars": 48,          # 最长 2 天，不急于换手
            "trail_activate_r": 1.2,      # 浮盈 1.2R 才启动移动止损
            "trail_atr_mult": 2.5,        # 跟踪距离更宽，避免正常波动扫掉
            "min_progress_r": 0.3,
            "reversal_composite": 0.75,   # 反转退出更难触发，减少误平
            "reversal_ema_bars": 48,
            "composite_max_age_sec": 300,
            "llm_min_interval_sec": 1800,
            "evolution_mode": "stats",
            "evolution_llm_enabled": False,
            "evolution_llm_allow_action": False,
            "evolution_llm_allow_loosen": False,
        },
        "market": {
            "auto_rotate": True,
            "min_score": 0.25,            # 只选明显有机会的品种
            "every_cycles": 1,
            "auto_top_k": 1,
        },
        "llm": {
            "offpeak_research_only": True,
            "peak_mode": "full",
        },
    },
    "balanced": {
        "label": "稳健（默认）",
        "summary": "单笔 1%、最多 2 仓、杠杆 1~5x、置信 0.50、盈亏比 1.4、回撤 18%。"
                   "胜率与盈亏比都不偏科：一半的单赚 1.4R、一半亏 1R 时期望值仍为正。"
                   "多数人的起点；与 AppConfig 默认值逐项一致。",
        "risk": {
            "sizing_mode": "llm",
            "max_loss_per_trade_pct": 0.010,
            "max_position_pct": 0.30,
            # 1→2 仓：均衡档需要一定分散度（同向相关性由 correlated 上限兜住），
            # 又不至于像高频档那样同时扛 4 份风险
            "max_concurrent_positions": 2,
            # 0.30×2=0.60 ≤ 0.70：留 30% 缓冲，不把保证金顶到 95% 那么紧
            "max_margin_usage_pct": 0.70,
            "min_confidence": 0.50,
            "max_leverage": 5.0,
            "min_leverage": 1.0,
            "leverage_override": 0.0,
            "daily_loss_limit_pct": 0.04,
            "max_drawdown_pct": 0.18,
            "max_portfolio_notional_pct": 2.0,
            "correlated_exposure_pct": 1.5,
            # 必须与 AppConfig() 的 RiskConfig 默认值逐项一致（「恢复默认」的唯一语义）
            # 1.3→1.4：胜率五成时期望值从 +0.15R 提到 +0.2R，这是「均衡」的算术基础
            "min_risk_reward": 1.4,
            "min_stop_atr_mult": 0.6,
            "debate_score_weight": 0.3,
        },
        "position": {
            "enabled": True,
            "auto_tune": True,
            "llm_explain": False,
            "llm_shadow": False,
            "llm_offpeak_only": True,
            "bars_from_index": True,
            "peak_drawdown_exit_enabled": False,
            "peak_drawdown_r": 1.0,
            "reversal_ema_bars": 48,
            "composite_max_age_sec": 300,
            "llm_min_interval_sec": 900,
            "evolution_mode": "stats",
            "evolution_llm_enabled": False,
            "evolution_llm_allow_action": False,
            "evolution_llm_allow_loosen": False,
        },
        "market": {
            "auto_rotate": True,
            "min_score": 0.20,
            "every_cycles": 1,
            "auto_top_k": 2,
        },
        "llm": {
            "offpeak_research_only": True,
            "peak_mode": "full",
        },
    },
    "frequency": {
        "label": "高频",
        "summary": "门槛 0.30、单笔 0.8%、最多 4 仓、每轮评 4 品种、盈亏比 2.0。"
                   "典型趋势跟踪：多数单小止损出局（胜率会低于五成），"
                   "靠少数跑出 2R 以上的单把总账拉正。适合能盯盘、接受连续小亏。",
        "risk": {
            "sizing_mode": "risk",
            "max_loss_per_trade_pct": 0.008,
            "max_position_pct": 0.12,
            "max_concurrent_positions": 4,
            "max_margin_usage_pct": 0.60,          # 0.12×4=0.48 ≤ 0.60
            "min_confidence": 0.30,                # 门槛最低：笔数优先
            "max_leverage": 10.0,
            # 3.0→1.0：高盈亏比靠「让利润跑」，不靠杠杆；强制 3x 只会放大噪音
            "min_leverage": 1.0,
            "leverage_override": 0.0,
            "daily_loss_limit_pct": 0.06,
            "max_drawdown_pct": 0.20,
            "max_portfolio_notional_pct": 2.0,     # 0.12×10=1.2 ≤ 2.0
            "correlated_exposure_pct": 1.2,
            # 1.2→2.0：这是本档的核心。低盈亏比 + 高频 = 被手续费与噪音磨死；
            # 高盈亏比 + 低胜率才是可持续的高频形态（亏 1R 赢 2R，胜率 34% 即可打平）
            "min_risk_reward": 2.0,
            "min_stop_atr_mult": 0.5,              # 允许更紧止损，抬高 R:R
            "debate_score_weight": 0.35,           # 高频更易被噪音带偏，裁判权重略高
        },
        "position": {
            "enabled": True,
            "auto_tune": True,
            # 高频档持仓多、巡检频繁：只保留「有动作时写解释」（代价极小），
            # 关掉逐轮影子复核（4 仓 × 900s ≈ 384 次/天，成本会线性放大）。
            "llm_explain": True,
            "llm_shadow": False,
            "llm_offpeak_only": True,
            "bars_from_index": True,
            "peak_drawdown_exit_enabled": False,
            "peak_drawdown_r": 1.0,
            # 换手要快，但盈利单不能砍太早——这是「高盈亏比」的必要条件
            "max_hold_bars": 12,           # 12 根 1h：半天不涨就走，席位让给下一个机会
            "trail_activate_r": 1.5,       # 1.5R 才启动移动止损，别打断趋势
            "trail_atr_mult": 2.5,         # 跟踪距离放宽，容忍回踩
            "min_progress_r": 0.4,         # 时间止损更严：12 根后还不到 0.4R 就撤
            "reversal_composite": 0.7,     # 不轻易用反转信号退出
            "reversal_ema_bars": 48,
            "composite_max_age_sec": 300,
            "llm_min_interval_sec": 1800,
            "evolution_mode": "stats",
            "evolution_llm_enabled": False,
            "evolution_llm_allow_action": False,
            "evolution_llm_allow_loosen": False,
        },
        "market": {
            "auto_rotate": True,
            # 0.12→0.10：本档本来就要笔数，选品门槛再降一档；
            # 质量由「盈亏比 2.0 + 止损 ATR」两道闸兜住，不靠选品过滤
            "min_score": 0.10,
            "every_cycles": 1,
            "auto_top_k": 4,               # 4 仓容量，每轮评 4 个品种
        },
        "llm": {
            "offpeak_research_only": True,
            "peak_mode": "thrift",         # 峰时降级规则兜底：高频档调用量最大，省钱优先
        },
    },
    "aggressive": {
        "label": "进取",
        "summary": "单笔 2%、最多 3 仓、杠杆 1~10x、置信 0.42、盈亏比 1.8、回撤上限 30%。"
                   "高风险高收益：承受 5 连亏即停的波动，换单笔 2% 的收益弹性；"
                   "研究也峰时跑（信号全但成本高）。需强心脏与充足本金。",
        "risk": {
            "sizing_mode": "risk",
            "max_loss_per_trade_pct": 0.020,
            # 0.25→0.30：单品种敢给更大保证金，是「高收益」的来源
            "max_position_pct": 0.30,
            "max_concurrent_positions": 3,
            "max_margin_usage_pct": 0.95,      # 0.30×3=0.90 ≤ 0.95
            "min_confidence": 0.42,            # 比稳健松，抓更多机会
            "max_leverage": 10.0,
            "min_leverage": 1.0,               # 杠杆交给决策，不强制 3x 起
            "leverage_override": 0.0,
            "daily_loss_limit_pct": 0.10,      # 10%：0.02×5 笔才停，容忍更大幅度波动
            "max_drawdown_pct": 0.30,
            "max_portfolio_notional_pct": 3.5, # 0.30×10=3.0 ≤ 3.5，留一点余量
            "correlated_exposure_pct": 2.0,
            # 1.3→1.8：高收益不靠「高胜率」（做不到），靠小止损 + 让利润跑。
            # 胜率 40% × 1.8R 的期望值，比胜率 55% × 1.0R 更抗噪
            "min_risk_reward": 1.8,
            "min_stop_atr_mult": 0.6,
            "debate_score_weight": 0.3,
        },
        "position": {
            "enabled": True,
            "auto_tune": True,
            "llm_explain": False,
            "llm_shadow": False,
            "llm_offpeak_only": True,
            "bars_from_index": True,
            "peak_drawdown_exit_enabled": False,
            "peak_drawdown_r": 1.0,
            # 激进档要「赚大钱」：持有更久、跟踪更宽，避免把 3R 的单砍在 1R
            "max_hold_bars": 36,
            "trail_activate_r": 1.2,
            "trail_atr_mult": 2.2,
            "min_progress_r": 0.3,
            "reversal_composite": 0.75,
            "reversal_ema_bars": 48,
            "composite_max_age_sec": 300,
            "llm_min_interval_sec": 900,
            "evolution_mode": "stats",
            "evolution_llm_enabled": False,
            "evolution_llm_allow_action": False,
            "evolution_llm_allow_loosen": False,
        },
        "market": {
            "auto_rotate": True,
            "min_score": 0.15,
            "every_cycles": 1,
            "auto_top_k": 3,
        },
        "llm": {
            "offpeak_research_only": False,    # 进取档不省研究钱：全信号优先
            "peak_mode": "full",
        },
    },
    "training": {
        "label": "训练",
        "summary": "单笔 0.5%、最多 3 仓、杠杆 1~5x、置信 0.40、单回合最长 24 根。"
                   "目标是「活得久、样本多、信号干净」：连亏 10 笔也只亏 5%，"
                   "不会一开局就撞熔断；适合长期跑模拟盘积累训练数据。",
        "risk": {
            # 单笔风险压到 0.5%：训练期最怕的是「还没攒够样本就触发熔断停摆」
            "sizing_mode": "risk",
            "max_loss_per_trade_pct": 0.005,
            "max_position_pct": 0.15,
            "max_concurrent_positions": 3,
            "max_margin_usage_pct": 0.60,          # 0.15×3=0.45 ≤ 0.60
            "min_confidence": 0.40,                # 比进取松、比高频严：样本与信噪的折中
            "max_leverage": 5.0,
            "min_leverage": 1.0,                   # 取消「强制 3x 起」
            "leverage_override": 0.0,
            "daily_loss_limit_pct": 0.04,          # 0.5%×8 笔才停，不至于一天就歇
            "max_drawdown_pct": 0.15,
            "max_portfolio_notional_pct": 2.0,     # 0.15×5=0.75 ≤ 2.0
            "correlated_exposure_pct": 1.5,
            "min_risk_reward": 1.25,               # 略低于 1.3，让合格单更容易过
            "min_stop_atr_mult": 0.6,
            "debate_score_weight": 0.3,
        },
        "position": {
            "enabled": True,
            "auto_tune": True,
            "llm_explain": False,
            "llm_shadow": False,
            "llm_offpeak_only": True,
            "bars_from_index": True,
            "peak_drawdown_exit_enabled": False,
            "peak_drawdown_r": 1.0,
            # 退出规则：加快回合速度 + 让退出方式分布更多样（复盘归因才有数据）
            "reversal_enabled": True,
            "reversal_min_hold_bars": 3,
            "reversal_ema_bars": 48,
            "reversal_composite": 0.6,
            "composite_max_age_sec": 300,
            "max_hold_bars": 24,                   # 48→24 根 1h：单回合最长 1 天
            "min_progress_r": 0.3,
            "trail_activate_r": 0.8,
            "trail_atr_mult": 1.8,
            "trail_min_gap_pct": 0.003,
            "llm_min_interval_sec": 900,
            "evolution_mode": "stats",
            "evolution_llm_enabled": False,
            "evolution_llm_allow_action": False,
            "evolution_llm_allow_loosen": False,
        },
        "market": {
            "auto_rotate": True,
            "min_score": 0.12,                     # 0.15→0.12：更多品种入选，样本多样
            "every_cycles": 1,
            "auto_top_k": 3,
        },
        "llm": {
            "offpeak_research_only": True,         # 研究只在谷时跑，省一半
            "peak_mode": "full",                   # 决策峰时照常用 LLM（训练要真信号）
        },
    },
    "evolution": {
        "label": "AI 进化（L3）",
        "summary": "L3 完全主导：LLM 可提议退出参数、甚至改变动作方向，也允许放宽阈值。"
                   "底座刻意压小（单笔 0.8%、2 仓、回撤 20%），把调整空间留给进化。"
                   "⚠️ 应用需 L2 反事实回放已通过，否则会被安全门拒绝（这是设计，不是故障）。",
        "risk": {
            # 底座压小：L3 会改动参数，留足安全边际；单笔 0.8% 让一次误判只亏 0.8%
            "sizing_mode": "risk",
            "max_loss_per_trade_pct": 0.008,
            "max_position_pct": 0.20,
            "max_concurrent_positions": 2,
            "max_margin_usage_pct": 0.50,          # 0.20×2=0.40 ≤ 0.50
            "min_confidence": 0.45,
            "max_leverage": 5.0,
            "min_leverage": 1.0,
            "leverage_override": 0.0,
            "daily_loss_limit_pct": 0.05,
            "max_drawdown_pct": 0.20,
            "max_portfolio_notional_pct": 1.5,     # 0.20×5=1.0 ≤ 1.5
            "correlated_exposure_pct": 1.0,
            "min_risk_reward": 1.4,
            "min_stop_atr_mult": 0.6,
            # 最信任模型的档位：辩论裁判权重 0.4（硬上限 0.5）
            "debate_score_weight": 0.4,
        },
        "position": {
            "enabled": True,
            "auto_tune": True,
            "llm_explain": True,                   # L3 要说明「为什么这么改」，给人审计
            "llm_shadow": False,
            "llm_offpeak_only": True,
            "bars_from_index": True,
            "peak_drawdown_exit_enabled": False,
            "peak_drawdown_r": 1.0,
            # —— L3 权限全开 ——
            "evolution_mode": "llm",
            "evolution_llm_enabled": True,
            "evolution_llm_allow_action": True,     # 允许 LLM 改变动作（不只调参数）
            "evolution_llm_allow_loosen": True,     # 允许放宽阈值（否则只能越调越保守）
            "evolution_llm_shadow_cases": 10,       # 先影子 10 笔再落地，比默认 20 更快接管
            "evolution_llm_min_confidence": 0.60,
            # 单次最多改 10%：即使 LLM 判断激进，也只会渐进移动，避免一次调坏
            "evolution_llm_max_param_step": 0.10,
            # —— L1/L2 的配套门槛 ——
            "evolution_rule_min_samples": 10,       # 比默认 8 严：L3 档的样本质量优先
            "evolution_confirm_rounds": 2,
            "evolution_replay_min_cases": 20,
            "evolution_replay_window": 100,
            "evolution_replay_min_median_delta": 0.0,
            "evolution_replay_max_worst_delta": 0.2,
            "evolution_l2_stable_days": 3,          # 14→3 天：稳定 3 天即放行 L3
            "evolution_rollback_after_losses": 3,   # 连亏 3 笔自动回退上一版
            "evolution_rollback_drawdown_r": 2.0,   # 回撤 2R 自动回退
            # —— 退出规则（L3 会在此基础上继续调）——
            "max_hold_bars": 24,
            "trail_activate_r": 1.0,
            "trail_atr_mult": 2.0,
            "min_progress_r": 0.3,
            "reversal_composite": 0.65,
            "reversal_ema_bars": 48,
            "composite_max_age_sec": 300,
            "llm_min_interval_sec": 900,
        },
        "market": {
            "auto_rotate": True,
            "min_score": 0.15,                     # 品种池质量优先，避免 L3 在噪音品种上学偏
            "every_cycles": 1,
            "auto_top_k": 2,                       # 2 仓容量，每轮评 2 个品种
        },
        "llm": {
            "offpeak_research_only": True,         # 省下的预算留给 L3 的决策调用
            "peak_mode": "full",
        },
    },
}

# 「恢复默认设置」指向哪一套
DEFAULT_PROFILE = "balanced"

# 方案允许覆盖这四组。其中「llm」组只允许非敏感的峰谷计价策略
# （offpeak_research_only / peak_mode / peak_holidays），provider / model /
# api_key / base_url 这类接入凭据一律不在方案范围内——「恢复默认」绝不能
# 把用户填好的 Key 清掉或改掉接入地址。
ALLOWED_GROUPS: tuple[str, ...] = ("risk", "position", "market", "llm")
_ALLOWED_TOP_KEYS = {"label", "summary", *ALLOWED_GROUPS}
# 键名里出现这些字样就直接判定为「越界」：宁可报错，也不能悄悄写进配置
_SECRET_TOKENS = (
    "api_key", "apikey", "api-key", "token", "secret", "password",
    "provider", "model", "base_url", "baseurl",
)


def assert_profile_safe(name: str) -> None:
    """校验方案不含越界内容；不合规直接抛错（导入时就会执行，问题越早暴露越好）。"""
    prof = PROFILES[name]
    extra = set(prof) - _ALLOWED_TOP_KEYS
    if extra:
        raise ValueError(f"方案 {name} 含非法分组：{sorted(extra)}（只允许 {ALLOWED_GROUPS}）")
    for g in ALLOWED_GROUPS:
        for k in (prof.get(g) or {}):
            low = str(k).lower()
            for tok in _SECRET_TOKENS:
                if tok in low:
                    raise ValueError(
                        f"方案 {name} 的 {g}.{k} 命中敏感字段「{tok}」；"
                        f"方案不得包含 API Key / 模型 / provider 配置"
                    )


for _name in PROFILES:
    assert_profile_safe(_name)

# market payload 的短键 → AppConfig.market 的属性名
_MARKET_ATTR = {
    "auto_rotate": "auto_rotate",
    "min_score": "rotate_min_score",
    "every_cycles": "rotate_every_cycles",
    "auto_top_k": "auto_top_k",
}


def market_payload_to_attrs(payload: dict) -> dict[str, Any]:
    """把 set_rotate 的短键翻译成属性名，供 diff 比较用。"""
    out: dict[str, Any] = {}
    for k, v in (payload or {}).items():
        out[_MARKET_ATTR.get(k, k)] = v
    return out


def profile_diff(cfg, name: str) -> list[dict]:
    """返回该方案与当前配置的差异（只列有变化的项）。

    `current` 取当前值、`target` 取方案值，供页面在应用前预览「会改什么」。
    """
    prof = PROFILES.get(name)
    if not prof:
        return []
    out: list[dict] = []

    def _add(group: str, key: str, cur: Any, tgt: Any) -> None:
        if isinstance(tgt, bool) or isinstance(cur, bool):
            if bool(cur) != bool(tgt):
                out.append({"group": group, "key": key, "current": bool(cur), "target": bool(tgt)})
            return
        try:
            if abs(float(cur) - float(tgt)) < 1e-9:
                return
        except (TypeError, ValueError):
            if cur == tgt:
                return
        out.append({"group": group, "key": key, "current": cur, "target": tgt})

    for k, v in (prof.get("risk") or {}).items():
        if hasattr(cfg.risk, k):
            _add("risk", k, getattr(cfg.risk, k), v)
    for k, v in (prof.get("position") or {}).items():
        if hasattr(cfg.position, k):
            _add("position", k, getattr(cfg.position, k), v)
    for k, v in market_payload_to_attrs(prof.get("market") or {}).items():
        if hasattr(cfg.market, k):
            _add("market", k, getattr(cfg.market, k), v)
    for k, v in (prof.get("llm") or {}).items():
        if hasattr(cfg.llm, k):
            _add("llm", k, getattr(cfg.llm, k), v)
    return out


def matches_profile(cfg, name: str) -> bool:
    """当前配置是否已经完全等于该方案（用于页面上标「当前」）。"""
    return not profile_diff(cfg, name)
