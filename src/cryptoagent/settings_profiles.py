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
        "label": "均衡（默认）",
        "summary": "单笔 1%、最多 2 仓、杠杆 1~5x、置信 0.50、盈亏比 1.4、回撤 18%。"
                   "胜率与盈亏比都不偏科：一半的单赚 1.4R、一半亏 1R 时期望值仍为正。"
                   "仓位按风险上限反推，参数表上的 1% 就是真实单笔风险；"
                   "多数人的起点，与 AppConfig 默认值逐项一致。",
        "risk": {
            # llm→risk：llm 模式下「单笔 1%」只是上限，交易员给多少算多少，
            # 参数表上的数字和实际风险对不上。risk 模式按「单笔风险/止损」反推
            # 名义敞口，1% 才是可预期的真实风险。
            "sizing_mode": "risk",
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
            # 退出参数必须写全：方案只覆盖它声明过的字段，缺一项就会沿用
            # 上一档的值（从训练档切回来会残留 24 根 / 0.8R 的快节奏）。
            # 48 根 = 2 天，配 1.0R 启动、2.0×ATR 跟踪 = 「中速持有」，
            # 比训练(24/0.8/1.8)慢、比保守(48/1.2/2.5)紧。
            "max_hold_bars": 48,
            "trail_activate_r": 1.0,
            "trail_atr_mult": 2.0,
            "min_progress_r": 0.3,
            "reversal_composite": 0.7,
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
        "summary": "门槛 0.30、单笔 0.8%、最多 4 仓、盈亏比 1.15、持仓最长 4 根。"
                   "薅波动：小赚就跑、错了立刻认，靠高胜率累积；"
                   "单笔只赚 1.15R，胜率需 >52% 才覆盖手续费与滑点。",
        "risk": {
            "sizing_mode": "risk",
            "max_loss_per_trade_pct": 0.008,
            "max_position_pct": 0.12,
            "max_concurrent_positions": 4,
            "max_margin_usage_pct": 0.60,          # 0.12×4=0.48 ≤ 0.60
            "min_confidence": 0.30,                # 门槛最低：笔数优先
            "max_leverage": 10.0,
            "min_leverage": 1.0,
            "leverage_override": 0.0,
            "daily_loss_limit_pct": 0.06,
            "max_drawdown_pct": 0.20,
            "max_portfolio_notional_pct": 2.0,     # 0.12×10=1.2 ≤ 2.0
            # 1.2→2.0：加密品种几乎同属一个相关性组，1.2 只够开一个顶格仓。
            # 按典型单笔名义（0.8%/2%止损=0.4）算，4 仓共 1.6，2.0 刚好容得下
            "correlated_exposure_pct": 2.0,
            # 1.15：薅波动的核心是「赚了就落袋」，不是让利润奔跑。
            # 止盈只要比止损大一点就走，靠次数和高胜率累积；
            # 下限不能低于 1.1，否则一笔亏损要两笔盈利才能填平，成本会吃掉全部边际
            "min_risk_reward": 1.15,
            "min_stop_atr_mult": 0.45,             # 止损更紧：判断错了立刻出局，不拖
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
            # 「赚了就跑」的三层保障，缺一不可：
            # ① 止盈近（R:R 1.15）② 移动止损早启动且贴身 ③ 峰值回吐就走
            "peak_drawdown_exit_enabled": True,    # 本档唯一开启此规则的档位
            "peak_drawdown_r": 0.5,                # 浮盈到过启动线后，从高点回吐 0.5R 即平
            "max_hold_bars": 4,                    # 4 根 1h：4 小时不动就换下一个机会
            "trail_activate_r": 0.5,               # 浮盈 0.5R 就启动保护，先锁住再说
            "trail_atr_mult": 1.0,                 # 跟踪贴身（1×ATR），不给回撤留空间
            "min_progress_r": 0.2,                 # 4 根后还不到 0.2R 就撤，别占着席位
            "reversal_composite": 0.5,             # 反转信号更敏感，早退
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
            # 0.10：本档本来就要笔数，选品不做过严过滤；
            # 质量由「紧止损 + 峰值回撤退出」两道规则兜住，不靠选品
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
        "summary": "单笔 3%、最多 3 仓、杠杆 1~20x、置信 0.38、盈亏比 1.5、回撤上限 40%。"
                   "真·高风险：3 仓同时止损即亏 9%，连亏 5 笔触发 15% 日亏线；"
                   "本金可以亏掉四成，不是稳健玩法。研究也峰时跑（信号全但成本高）。",
        "risk": {
            "sizing_mode": "risk",
            # 2%→3%：单笔风险直接拉到账户能承受的上限。3 仓同时打满 = 9%，
            # 仍低于 15% 日亏线，但要连亏 5 笔才停——这是本档的真实代价
            "max_loss_per_trade_pct": 0.030,
            "max_position_pct": 0.30,
            "max_concurrent_positions": 3,
            "max_margin_usage_pct": 0.95,      # 0.30×3=0.90 ≤ 0.95
            "min_confidence": 0.38,            # 继续放低：激进档宁可多开、靠仓位赚
            # 不设 20x：0.30×20=6.0 正好等于组合名义上限，第一仓就占满，
            # 第二仓必被风控拒掉，3 仓名存实亡。10x 下单笔名义 3.0，能开满 2 仓、
            # 第三仓视止损宽度而定——这才是「激进但还能同时持仓」的可行解
            "max_leverage": 10.0,
            "min_leverage": 1.0,
            "leverage_override": 0.0,
            "daily_loss_limit_pct": 0.15,      # 15%：3%×5 笔才停
            "max_drawdown_pct": 0.40,          # 40%：从 5000 跌到 3000 才熔断
            "max_portfolio_notional_pct": 6.0, # 单笔名义 3.0，组合上限 6.0
            # 2.5→3.0：加密品种高度相关，放宽到"允许两仓同组满额"，
            # 第三仓必须换组（否则等于把风险叠在一个方向上）
            "correlated_exposure_pct": 3.0,
            # 1.8→1.6：激进档的核心是「敢开 + 仓位重」，不是「等一笔大行情」。
            # 盈亏比要求越高，通过率越低；本档要的是次数 × 仓位，让胜率贡献收益。
            # 取 1.6 而非 1.5：1.5 与保守档完全相同，会让「盈亏比」这一维失去区分度；
            # 改后梯度为 高频1.15 / 训练1.25 / 均衡1.40 / 保守1.50 / 进取1.60，五档各不同。
            "min_risk_reward": 1.6,
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
            "max_hold_bars": 24,
            "trail_activate_r": 1.0,
            "trail_atr_mult": 2.0,
            "min_progress_r": 0.3,
            "reversal_composite": 0.7,
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
        "summary": "均衡底座 + AI 接管：风险参数与「均衡」逐项相同，差别只在 L3 权限全开。"
                   "LLM 可提议退出参数、甚至改变动作方向，也允许放宽阈值。"
                   "⚠️ 应用需 L2 反事实回放已通过，否则会被安全门拒绝（这是设计，不是故障）。",
        "risk": {
            # 与 balanced 逐项相同：本档的差异只在「谁来调参」，不在「承担多少风险」。
            # 之前把底座整体乘 0.8，会让用户切档时困惑「我要的是 AI 接管，
            # 为什么仓位先缩水了」——那是重复而不是独立定位。
            "sizing_mode": "risk",
            "max_loss_per_trade_pct": 0.010,
            "max_position_pct": 0.30,
            "max_concurrent_positions": 2,
            "max_margin_usage_pct": 0.70,
            "min_confidence": 0.50,
            "max_leverage": 5.0,
            "min_leverage": 1.0,
            "leverage_override": 0.0,
            "daily_loss_limit_pct": 0.04,
            "max_drawdown_pct": 0.18,
            "max_portfolio_notional_pct": 2.0,
            "correlated_exposure_pct": 1.5,
            "min_risk_reward": 1.4,
            "min_stop_atr_mult": 0.6,
            # 唯一保留的差异：最信任模型的档位，辩论裁判权重 0.4（硬上限 0.5）
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
