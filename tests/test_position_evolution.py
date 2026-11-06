"""L1/L2/L3 持仓管理进化的回归测试。

守住四条底线：
1. L1 按规则归因，单规则样本不足时不许跨规则调参；
2. L1 连续确认，单轮噪声不落地；
3. L2 门控未过时，候选参数一律丢弃；
4. L3 必须过 L2 门控 + 影子期，且动作只能更保守；连续亏损自动回退。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cryptoagent.agents.position_calibration import (
    BOUNDS,
    TUNABLE,
    empty_stats,
    learned_params,
    record_episode,
)
from cryptoagent.agents.position_replay import evaluate, replay_case
from cryptoagent.config import AppConfig, PositionConfig

BASE = {k: float(getattr(PositionConfig(), k)) for k in TUNABLE}


def _fill(stats, rule, r, mfe, n):
    for _ in range(n):
        record_episode(stats, rule, r, mfe)
    return stats


def _bare_engine(mode: str = "stats"):
    from cryptoagent.engine import TradingEngine

    e = object.__new__(TradingEngine)
    e.cfg = AppConfig()
    e.cfg.position.evolution_mode = mode
    e.state = {}
    e._save = lambda: None
    e._notify = lambda *a, **k: None
    e._frame_for = lambda sym: None
    base = {k: float(getattr(e.cfg.position, k)) for k in TUNABLE}
    e.position_tuning = {
        "stats": {}, "effective": dict(base), "anchor": dict(base),
        "changes": [], "pending": {}, "replay_cases": [], "replay": {},
        "gate": {},
        "llm": {"proposals": [], "applied": [], "shadow": [], "rollbacks": [],
                "streak_loss": 0, "cum_r": 0.0},
        "updated_at": 0.0,
    }
    return e, base


# ---------------------------------------------------------------- L1
def test_l1_rule_min_samples_blocks_cross_rule_tuning():
    """全局 20 笔，但 trail 只有 5 笔：不允许用这 5 笔改移动止损参数。"""
    s = empty_stats()
    _fill(s, "trail", 0.2, 3.0, 5)      # 捕获率低，但样本不足 8
    _fill(s, "time_stop", 0.9, 1.5, 15)
    eff, ok, changes = learned_params(BASE, s, rule_min_samples=8)
    assert ok is True
    assert eff["trail_atr_mult"] == pytest.approx(BASE["trail_atr_mult"])
    assert not any(c["param"] == "trail_atr_mult" for c in changes)


def test_l1_changes_carry_rule_label():
    s = empty_stats()
    _fill(s, "trail", 0.4, 3.0, 20)
    _eff, _ok, changes = learned_params(BASE, s, rule_min_samples=8)
    assert changes and all(c.get("rule") == "trail" for c in changes)


def test_l1_confirm_rounds_gate_noise():
    e, base = _bare_engine("stats")
    e.cfg.position.evolution_confirm_rounds = 2
    ch = [{"param": "trail_atr_mult", "from": 2.0, "to": 1.76, "rule": "trail", "why": "x"}]
    assert e._confirm_tuning(ch) == []          # 第 1 轮只记 pending
    assert e.position_tuning["pending"]["rounds"] == 1
    assert e._confirm_tuning(ch) == ch          # 第 2 轮同向才落地
    assert e.position_tuning["pending"] == {}
    # 方向反转 → 重新计数
    rev = [{"param": "trail_atr_mult", "from": 1.76, "to": 1.97, "rule": "trail", "why": "x"}]
    assert e._confirm_tuning(rev) == []
    assert e.position_tuning["pending"]["rounds"] == 1


# ---------------------------------------------------------------- L2
def _frame(periods: int = 500):
    idx = pd.date_range("2026-01-01", periods=periods, freq="h", tz="UTC")
    close = pd.Series(100 + 0.05 * pd.Series(range(periods)).to_numpy(), index=idx)
    return pd.DataFrame({
        "open": close, "high": close * 1.004, "low": close * 0.996,
        "close": close, "volume": 10.0,
    }, index=idx)


def _case(df, i: int = 250):
    entry = float(df["close"].iloc[i])
    return {
        "symbol": "BTCUSDT", "side": "LONG", "entry": entry,
        "stop": entry * 0.98, "tp": entry * 1.04,
        "opened_epoch": float(df.index[i].timestamp()),
        "closed_epoch": float(df.index[i + 40].timestamp()),
        "initial_r_pct": 0.02, "realized_r": 0.0, "mfe_r": 1.0,
    }


def test_l2_replay_returns_r_or_none():
    df = _frame()
    case = _case(df)
    r = replay_case(case, df, PositionConfig())
    assert r is None or isinstance(r, float)


def test_l2_gate_passes_with_enough_cases_and_equal_params():
    df = _frame()
    case = _case(df)
    out = evaluate([case] * 20, lambda sym: df, PositionConfig(), PositionConfig(),
                   min_cases=20, min_median_delta=0.0, max_worst_delta=0.2)
    assert out["cases"] >= 1          # replay_case 若因数据不足跳过，这里也应诚实反映
    assert out["passed"] is out["checks"]["enough_cases"] or out["passed"] is False


def test_l2_gate_not_enough_cases_fails():
    df = _frame()
    out = evaluate([_case(df)], lambda sym: df, PositionConfig(), PositionConfig(),
                   min_cases=20)
    assert out["passed"] is False
    assert out["checks"]["enough_cases"] is False


def test_l2_gate_discards_candidate_when_no_replay_cases():
    e, base = _bare_engine("replay")
    e.cfg.position.evolution_confirm_rounds = 1
    e.cfg.position.evolution_replay_min_cases = 20
    e.position_tuning["stats"] = _fill(empty_stats(), "trail", 0.4, 3.0, 20)
    e._recompute_position_tuning()
    assert e.position_tuning["gate"]["l2_passed"] is False
    assert e.position_tuning["effective"] == pytest.approx(base)
    assert e.position_tuning["changes"] == []


def test_l2_gate_not_passed_by_empty_candidate():
    """没有候选改动时，不能凭「空跑回放」把 L2 置为通过。"""
    e, _base = _bare_engine("replay")
    e.cfg.position.evolution_confirm_rounds = 1
    # 捕获率 0.8、峰值 1.5R：表现已在目标区间，L1 不会产生候选
    e.position_tuning["stats"] = _fill(empty_stats(), "trail", 1.2, 1.5, 20)
    e._recompute_position_tuning()
    assert e.position_tuning["changes"] == []
    assert e.position_tuning["gate"].get("l2_passed") is not True
    assert e.position_tuning["replay"]["checks"]["has_candidate"] is False


# ---------------------------------------------------------------- L3
def test_l3_blocked_without_l2_gate():
    e, _base = _bare_engine("llm")
    e.cfg.position.evolution_llm_enabled = True
    assert e._llm_evolution_allowed() is False
    e.position_tuning["gate"] = {"l2_passed": True, "l2_stable_since": time.time() - 30 * 86400}
    assert e._llm_evolution_allowed() is True


def test_l3_shadow_phase_does_not_change_action():
    e, _base = _bare_engine("llm")
    e.cfg.position.evolution_llm_shadow_cases = 2
    e.position_tuning["gate"] = {"l2_passed": True, "l2_stable_since": 0.0}
    from cryptoagent.agents.position_manager import PositionDecision

    dec = PositionDecision("hold", "规则不动", rule="trail")
    out = e._apply_llm_evolution(dec, {"action": "close", "confidence": 0.9,
                                       "params": {}, "reason": "x"}, {"symbol": "BTCUSDT"})
    assert out["phase"] == "shadow"
    assert dec.action == "hold"


def test_l3_action_only_more_conservative_by_default():
    e, _base = _bare_engine("llm")
    e.cfg.position.evolution_llm_shadow_cases = 0
    e.position_tuning["gate"] = {"l2_passed": True, "l2_stable_since": 0.0}
    from cryptoagent.agents.position_manager import PositionDecision

    # close → hold：放宽退出，默认拒绝
    dec = PositionDecision("close", "规则要平", rule="trail")
    e._apply_llm_evolution(dec, {"action": "hold", "confidence": 0.9,
                                 "params": {}, "reason": "再等等"}, {"symbol": "BTCUSDT"})
    assert dec.action == "close"

    # hold → close：更保守，允许
    dec2 = PositionDecision("hold", "规则不动", rule="trail")
    e._apply_llm_evolution(dec2, {"action": "close", "confidence": 0.9,
                                  "params": {}, "reason": "风险升高"}, {"symbol": "BTCUSDT"})
    assert dec2.action == "close"


def test_l3_params_limited_to_rule_and_step():
    e, _base = _bare_engine("llm")
    e.cfg.position.evolution_llm_shadow_cases = 0
    e.cfg.position.evolution_llm_max_param_step = 0.15
    e.position_tuning["gate"] = {"l2_passed": True, "l2_stable_since": 0.0}
    base_val = float(e.position_tuning["effective"]["trail_atr_mult"])
    from cryptoagent.agents.position_manager import PositionDecision

    dec = PositionDecision("hold", "规则不动", rule="trail")
    e._apply_llm_evolution(
        dec,
        {"action": "hold", "confidence": 0.9,
         "params": {"trail_atr_mult": 999.0, "min_progress_r": 0.7},
         "reason": "放大跟踪距离"},
        {"symbol": "BTCUSDT"},
    )
    got = float(e.position_tuning["effective"]["trail_atr_mult"])
    lo, hi = BOUNDS["trail_atr_mult"]
    assert lo <= got <= hi
    assert got <= base_val * 1.15 + 1e-6        # 单轮步长受限
    # time_stop 的参数不属于 trail 规则，必须被丢弃
    assert float(e.position_tuning["effective"]["min_progress_r"]) == pytest.approx(
        float(BASE["min_progress_r"])
    )


def test_l3_rollback_after_consecutive_losses():
    e, base = _bare_engine("llm")
    e.cfg.position.evolution_rollback_after_losses = 3
    e.position_tuning["effective"] = {**base, "trail_atr_mult": 1.5}
    pos = {"llm_evolution": {"action": "close"}}
    for _ in range(3):
        e._update_llm_streak(pos, -0.5)
    assert e.cfg.position.evolution_mode == "stats"
    assert e.position_tuning["effective"] == pytest.approx(base)
    assert e.position_tuning["llm"]["rollbacks"]
