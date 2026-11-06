"""配置方案（预设 / 恢复默认）与 LLM 自主调参的护栏测试。

核心守住三件事：
1. **方案绝不碰 API Key / 模型配置**——只允许 risk / position / market 三组；
2. **方案内部自洽**——单笔仓位 × 同时持仓 ≤ 保证金上限，单笔最大名义 ≤ 组合上限；
3. **模型提案过不了代码这道闸**——越界、超步长、超项数、破坏保证金不等式全部拒绝。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cryptoagent.agents.settings_evolver import (  # noqa: E402
    BOUNDS,
    INT_KEYS,
    MAX_KEYS,
    MAX_STEP,
    build_context,
    funnel_stats,
    perf_stats,
    sanitize,
)
from cryptoagent.config import AppConfig, MarketConfig, PositionConfig, RiskConfig  # noqa: E402
from cryptoagent.settings_profiles import (  # noqa: E402
    ALLOWED_GROUPS,
    DEFAULT_PROFILE,
    PROFILES,
    _MARKET_ATTR,
    assert_profile_safe,
    matches_profile,
    profile_diff,
)


# ---------------- 方案安全 ----------------

def test_all_profiles_pass_safety_check():
    for name in PROFILES:
        assert_profile_safe(name)


def test_profiles_never_contain_llm_or_secrets():
    for name, prof in PROFILES.items():
        assert set(prof) <= {"label", "summary", *ALLOWED_GROUPS}, name
        assert "llm" not in prof, f"{name} 不应包含 llm 分组"
        flat = " ".join(
            f"{g}.{k}" for g in ALLOWED_GROUPS for k in (prof.get(g) or {})
        ).lower()
        for tok in ("api_key", "apikey", "token", "secret", "provider", "model"):
            assert tok not in flat, f"{name} 的键名出现了 {tok}"


def test_safety_check_rejects_injected_secret(monkeypatch):
    bad = {"label": "坏方案", "summary": "", "risk": {"api_key": "sk-xxx"}}
    monkeypatch.setitem(PROFILES, "__bad__", bad)
    with pytest.raises(ValueError, match="敏感字段"):
        assert_profile_safe("__bad__")


def test_safety_check_rejects_llm_group(monkeypatch):
    bad = {"label": "坏方案", "summary": "", "llm": {"provider": "openai"}}
    monkeypatch.setitem(PROFILES, "__bad2__", bad)
    with pytest.raises(ValueError, match="非法分组"):
        assert_profile_safe("__bad2__")


def test_webapp_only_snapshots_three_groups():
    from cryptoagent.web.server import WebApp
    assert WebApp._SETTINGS_GROUPS == ("risk", "position", "market")


# ---------------- 方案自洽 ----------------

@pytest.mark.parametrize("name", list(PROFILES))
def test_profile_portfolio_invariants(name):
    r = PROFILES[name]["risk"]
    pos = float(r["max_position_pct"])
    slots = max(1, int(r["max_concurrent_positions"]))
    margin = float(r["max_margin_usage_pct"])
    assert pos * slots <= margin + 1e-9, f"{name}: {pos}×{slots} 超过保证金 {margin}"
    need = pos * float(r["max_leverage"])
    assert need <= float(r["max_portfolio_notional_pct"]) + 1e-9, f"{name}: 单笔名义 {need} 超上限"
    assert float(r["min_leverage"]) <= float(r["max_leverage"])


@pytest.mark.parametrize("name", list(PROFILES))
def test_profile_keys_exist_on_config(name):
    prof = PROFILES[name]
    for k in prof.get("risk") or {}:
        assert hasattr(RiskConfig(), k), f"{name}: risk.{k} 不存在"
    for k in prof.get("position") or {}:
        assert hasattr(PositionConfig(), k), f"{name}: position.{k} 不存在"
    for k in prof.get("market") or {}:
        assert k in _MARKET_ATTR, f"{name}: market.{k} 没有映射"
        assert hasattr(MarketConfig(), _MARKET_ATTR[k]), f"{name}: market.{_MARKET_ATTR[k]} 不存在"


def test_default_profile_is_listed():
    assert DEFAULT_PROFILE in PROFILES


def test_balanced_profile_equals_appconfig_defaults():
    """「默认」只能有一个语义：balanced 必须与 AppConfig() 逐项一致。

    否则新装用户拿到的值与点「恢复默认」后的值会不同（历史上差 6.7 倍杠杆）。
    """
    cfg = AppConfig()
    bal = PROFILES["balanced"]
    for k, v in bal["risk"].items():
        assert getattr(cfg.risk, k) == v, f"risk.{k}"
    for k, v in bal["position"].items():
        assert getattr(cfg.position, k) == v, f"position.{k}"
    for k, v in bal["market"].items():
        assert getattr(cfg.market, _MARKET_ATTR[k]) == v, f"market.{k}"


def test_evolver_never_touches_position_llm_interval():
    """llm_min_interval_sec 只允许设置页写，自动调参不得与之互相覆盖。"""
    assert "llm_min_interval_sec" not in BOUNDS
    ctx = build_context(AppConfig())
    assert "llm_min_interval_sec" not in ctx["current"]


def test_profile_diff_and_match():
    cfg = AppConfig()
    for k, v in PROFILES["frequency"]["risk"].items():
        setattr(cfg.risk, k, v)
    diff = profile_diff(cfg, "frequency")
    # risk 组已经一致，剩下的差异只可能来自 position / market
    assert all(d["group"] != "risk" for d in diff)
    assert not matches_profile(cfg, "frequency")


# ---------------- LLM 提案护栏 ----------------

def _ctx(cfg=None):
    return build_context(cfg or AppConfig())


def test_whitelist_rejects_unknown_key():
    cfg = AppConfig()
    out = sanitize({"changes": [{"key": "llm_provider", "to": "openai"}]}, cfg, _ctx(cfg)["current"])
    assert out["changes"] == []
    assert out["rejected"][0]["reason"].startswith("不在可调白名单")


def test_bounds_rejected():
    cfg = AppConfig()
    out = sanitize({"changes": [{"key": "min_confidence", "to": 0.95}]}, cfg, _ctx(cfg)["current"])
    assert out["changes"] == []
    assert "越界" in out["rejected"][0]["reason"]


def test_step_limit_rejected():
    cfg = AppConfig()
    cur = _ctx(cfg)["current"]
    # 取一个仍在边界内、但超过 25% 步长的目标（0.55 → 0.693 约 +26%）
    target = min(0.70, float(cur["min_confidence"]) * (1 + MAX_STEP * 1.05))
    out = sanitize({"changes": [{"key": "min_confidence", "to": target}]}, cfg, cur)
    assert out["changes"] == []
    assert "单轮变化超过" in out["rejected"][0]["reason"]


def test_max_keys_rejected():
    cfg = AppConfig()
    cur = _ctx(cfg)["current"]
    items = [
        {"key": "min_confidence", "to": float(cur["min_confidence"]) * 0.9},
        {"key": "max_loss_per_trade_pct", "to": float(cur["max_loss_per_trade_pct"]) * 0.9},
        {"key": "max_position_pct", "to": float(cur["max_position_pct"]) * 0.9},
        {"key": "rotate_min_score", "to": float(cur["rotate_min_score"]) * 0.9},
    ]
    out = sanitize({"changes": items}, cfg, cur)
    assert len(out["changes"]) <= MAX_KEYS
    assert any("一轮最多改" in r["reason"] for r in out["rejected"])


def test_int_key_step_is_one():
    cfg = AppConfig()
    cur = _ctx(cfg)["current"]
    out = sanitize({"changes": [{"key": "auto_top_k", "to": float(cur["auto_top_k"]) + 3}]}, cfg, cur)
    assert out["changes"] == []
    assert "±1" in out["rejected"][0]["reason"]
    ok = sanitize({"changes": [{"key": "auto_top_k", "to": float(cur["auto_top_k"]) + 1}]}, cfg, cur)
    assert ok["changes"] and ok["changes"][0]["to"] == int(cur["auto_top_k"]) + 1


def test_margin_invariant_voids_whole_proposal():
    cfg = AppConfig()
    cfg.risk.max_margin_usage_pct = 0.50
    cfg.risk.max_position_pct = 0.20
    cfg.risk.max_concurrent_positions = 2      # 0.20×2=0.40 ≤ 0.50，当前合法
    cur = _ctx(cfg)["current"]
    # 0.50 → 0.38 只降 24%（在步长内），但 0.20×2=0.40 已超过它 → 整份作废
    out = sanitize({"changes": [{"key": "max_margin_usage_pct", "to": 0.38}]}, cfg, cur)
    assert out["ok"] is False
    assert out["changes"] == []
    assert any("整份作废" in r["reason"] for r in out["rejected"])


def test_valid_proposal_passes():
    cfg = AppConfig()
    cur = _ctx(cfg)["current"]
    out = sanitize({"changes": [{"key": "min_confidence", "to": float(cur["min_confidence"]) * 0.9}]},
                   cfg, cur)
    assert out["ok"] is True
    assert len(out["changes"]) == 1
    assert out["rejected"] == []


def test_bounds_cover_int_keys():
    for k in INT_KEYS:
        assert k in BOUNDS


# ---------------- 统计上下文 ----------------

def test_perf_stats_uses_realized_r():
    hist = [
        {"realized_r": 2.0, "mfe_r": 2.5},
        {"realized_r": -1.0, "mfe_r": 0.4},
        {"realized_r": 0.5, "mfe_r": 1.0},
    ]
    out = perf_stats(hist)
    assert out["trades"] == 3 and out["with_r"] == 3
    assert out["win_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert out["avg_r"] == pytest.approx(0.5, abs=1e-6)
    assert "capture_median" in out


def test_perf_stats_empty():
    assert perf_stats([]) == {"trades": 0}


def test_funnel_stats_missing_dir():
    assert funnel_stats("/nonexistent/path") == {}
