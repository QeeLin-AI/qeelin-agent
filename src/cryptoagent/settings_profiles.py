"""交易参数与模型参数的「方案」：预设 + 一键恢复默认。

设计原则：

1. **方案只是一组 payload**，不直接 setattr。应用时走既有的
   `set_risk` / `set_position_settings` / `set_rotate`，因此沿用它们的
   范围校验、保证金不等式校验和「失败整体回滚」——不在第二个地方重复实现规则。
2. **每套方案内部自洽**：`单笔仓位上限 × 同时持仓 ≤ 保证金占用上限`，
   且 `单笔仓位上限 × 杠杆上限 ≤ 组合名义敞口上限`。这两个不等式写死在测试里，
   改方案数值时会被当场拦下。
3. **保守优先**：`balanced` 是出厂默认，也是「恢复默认」的目标；`frequency`
   靠降低置信度门槛换频率，同时把单笔风险压小，使「满仓总风险」不高于 balanced。
"""
from __future__ import annotations

from typing import Any

# 方案名 → 三组 payload。键名必须与对应 setter 接受的一致。
PROFILES: dict[str, dict[str, Any]] = {
    "balanced": {
        "label": "稳健（默认）",
        "summary": "出厂默认：单笔风险 1%、单次 1 仓、置信度门槛 0.55、杠杆上限 3x；"
                   "不开自动轮动。最保守的可用起点，与 AppConfig 默认值逐项一致。",
        "risk": {
            "sizing_mode": "llm",
            "max_loss_per_trade_pct": 0.010,
            "max_position_pct": 0.30,
            "max_concurrent_positions": 1,
            "max_margin_usage_pct": 0.95,
            "min_confidence": 0.55,
            "max_leverage": 3.0,
            "min_leverage": 1.0,
            "leverage_override": 0.0,
            "daily_loss_limit_pct": 0.03,
            "max_drawdown_pct": 0.15,
            "max_portfolio_notional_pct": 1.2,
            "correlated_exposure_pct": 0.9,
        },
        "position": {
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
        },
        "market": {
            "auto_rotate": False,
            "min_score": 0.2,
            "every_cycles": 1,
            "auto_top_k": 1,
        },
    },
    "frequency": {
        "label": "高频",
        "summary": "门槛降到 0.32、单笔风险压到 0.8%、最多 4 仓、每轮评 3 个品种。"
                   "笔数约 2~3 倍，满仓总风险 3.2%（高于稳健档的 1%，用频率换机会）。",
        "risk": {
            "sizing_mode": "risk",
            "max_loss_per_trade_pct": 0.008,
            "max_position_pct": 0.12,
            "max_concurrent_positions": 4,
            "max_margin_usage_pct": 0.80,
            "min_confidence": 0.32,
            "max_leverage": 15.0,
            "min_leverage": 3.0,
            "leverage_override": 0.0,
            "daily_loss_limit_pct": 0.06,
            "max_drawdown_pct": 0.20,
            "max_portfolio_notional_pct": 7.0,
            "correlated_exposure_pct": 5.0,
        },
        "position": {
            "auto_tune": True,
            # 高频档持仓多、巡检频繁：只保留「有动作时写解释」（代价极小），
            # 关掉逐轮影子复核（4 仓 × 900s ≈ 384 次/天，成本会线性放大）。
            "llm_explain": True,
            "llm_shadow": False,
            "llm_offpeak_only": True,
            "bars_from_index": True,
            "peak_drawdown_exit_enabled": False,
            "peak_drawdown_r": 1.0,
            "reversal_ema_bars": 48,
            "composite_max_age_sec": 300,
            "llm_min_interval_sec": 1800,
        },
        "market": {
            "auto_rotate": True,
            "min_score": 0.10,
            "every_cycles": 1,
            "auto_top_k": 3,
        },
    },
    "conservative": {
        "label": "保守",
        "summary": "门槛 0.60、单笔风险 0.5%、只持 1 仓。信号少但每笔都要求高置信。",
        "risk": {
            "sizing_mode": "risk",
            "max_loss_per_trade_pct": 0.005,
            "max_position_pct": 0.10,
            "max_concurrent_positions": 1,
            "max_margin_usage_pct": 0.50,
            "min_confidence": 0.60,
            "max_leverage": 10.0,
            "min_leverage": 2.0,
            "leverage_override": 0.0,
            "daily_loss_limit_pct": 0.03,
            "max_drawdown_pct": 0.12,
            "max_portfolio_notional_pct": 3.0,
            "correlated_exposure_pct": 2.5,
        },
        "position": {
            "auto_tune": True,
            "llm_explain": True,
            "llm_shadow": True,
            "llm_offpeak_only": True,
            "bars_from_index": True,
            "peak_drawdown_exit_enabled": False,
            "peak_drawdown_r": 1.0,
            "reversal_ema_bars": 48,
            "composite_max_age_sec": 300,
            "llm_min_interval_sec": 1800,
        },
        "market": {
            "auto_rotate": True,
            "min_score": 0.25,
            "every_cycles": 1,
            "auto_top_k": 1,
        },
    },
}

# 「恢复默认设置」指向哪一套
DEFAULT_PROFILE = "balanced"

# 方案**只允许**覆盖这三组。LLM 的 provider / model / api_key / base_url
# 一律不在方案范围内——「恢复默认」绝不能把用户填好的 Key 清掉或改成别人的。
ALLOWED_GROUPS: tuple[str, ...] = ("risk", "position", "market")
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
    return out


def matches_profile(cfg, name: str) -> bool:
    """当前配置是否已经完全等于该方案（用于页面上标「当前」）。"""
    return not profile_diff(cfg, name)
