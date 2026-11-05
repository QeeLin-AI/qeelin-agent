"""持仓管理员自迭代（参数校准）的回归测试。

守住三条底线：
1. 样本不足时**一个参数都不能动**；
2. 每轮最多调 12%，且永远不越出 BOUNDS；
3. 学习方向与观测到的绩效一致（回吐→收紧、峰值小→推迟启动、砍好仓→放宽）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cryptoagent.agents.position_calibration import (
    BOUNDS,
    MAX_STEP,
    MIN_SAMPLES,
    TUNABLE,
    empty_stats,
    learned_params,
    record_episode,
    summary,
)

BASE = {
    "trail_activate_r": 1.0,
    "trail_atr_mult": 2.0,
    "min_progress_r": 0.3,
    "max_hold_bars": 48.0,
}


def _fill(stats, rule, r, mfe, n):
    for _ in range(n):
        record_episode(stats, rule, r, mfe)
    return stats


def test_empty_stats_shape():
    s = empty_stats()
    assert s["n"] == 0 and s["samples"] == [] and s["rules"] == {}


def test_record_episode_counts_and_caps():
    s = empty_stats()
    _fill(s, "trail", 1.0, 2.0, 3)
    assert s["n"] == 3
    assert s["rules"]["trail"] == 3
    assert len(s["samples"]) == 3


def test_record_episode_extreme_clipped():
    s = empty_stats()
    record_episode(s, "trail", 999.0, -999.0)
    assert s["samples"][0]["r"] <= 20.0
    assert s["samples"][0]["mfe"] >= -10.0


def test_insufficient_samples_changes_nothing():
    s = empty_stats()
    _fill(s, "trail", 0.2, 3.0, MIN_SAMPLES - 1)
    eff, ok, changes = learned_params(BASE, s)
    assert ok is False and changes == []
    assert eff == pytest.approx(BASE)


def test_low_capture_tightens_trailing():
    """捕获率低（冲高后回吐）→ 收紧跟踪距离：trail_atr_mult 下降。"""
    s = empty_stats()
    _fill(s, "trail", 0.4, 3.0, MIN_SAMPLES)  # capture ≈ 0.13
    eff, ok, changes = learned_params(BASE, s)
    assert ok is True
    assert eff["trail_atr_mult"] == pytest.approx(BASE["trail_atr_mult"] * (1 - MAX_STEP))
    assert any(c["param"] == "trail_atr_mult" for c in changes)


def test_small_peak_postpones_activation():
    """峰值普遍不到 1R（启动过早）→ 推迟移动止损触发点：trail_activate_r 上升。"""
    s = empty_stats()
    _fill(s, "trail", 0.6, 0.7, MIN_SAMPLES)  # capture ≈ 0.86，mfe 0.7 < 1.0
    eff, _ok, changes = learned_params(BASE, s)
    assert eff["trail_activate_r"] == pytest.approx(BASE["trail_activate_r"] * (1 + MAX_STEP))
    assert any(c["param"] == "trail_activate_r" for c in changes)


def test_time_stop_cutting_winners_loosens():
    """时间止损砍掉的仓位平均 R 高于全体 → 放宽进度要求、延长持有上限。"""
    s = empty_stats()
    _fill(s, "trail", -0.5, 0.8, 15)      # 全体平均被拉低
    _fill(s, "time_stop", 1.2, 1.4, 8)    # 被时间止损砍掉的反而更好
    eff, _ok, changes = learned_params(BASE, s)
    assert eff["min_progress_r"] == pytest.approx(BASE["min_progress_r"] * (1 + MAX_STEP), abs=1e-4)
    assert eff["max_hold_bars"] > BASE["max_hold_bars"]
    assert {c["param"] for c in changes} >= {"min_progress_r", "max_hold_bars"}


def test_time_stop_cutting_losers_shortens():
    s = empty_stats()
    _fill(s, "trail", 0.9, 1.5, 15)
    _fill(s, "time_stop", -0.8, 0.2, 8)
    eff, _ok, changes = learned_params(BASE, s)
    assert eff["max_hold_bars"] < BASE["max_hold_bars"]
    assert any(c["param"] == "max_hold_bars" for c in changes)


def test_never_exceeds_bounds_even_after_many_rounds():
    """反复学习到顶也不能越界，且顶到边界后不再产生「没变」的改动。"""
    s = empty_stats()
    _fill(s, "trail", 0.1, 5.0, MIN_SAMPLES)
    eff = dict(BASE)
    for _ in range(200):
        eff, _ok, changes = learned_params(eff, s)
        for k, v in eff.items():
            lo, hi = BOUNDS[k]
            assert lo - 1e-9 <= v <= hi + 1e-9, f"{k} 越界: {v}"
    assert eff["trail_atr_mult"] == pytest.approx(BOUNDS["trail_atr_mult"][0])
    assert changes == []


def test_max_hold_bars_stays_integer():
    s = empty_stats()
    _fill(s, "trail", 0.9, 1.5, 15)
    _fill(s, "time_stop", -0.9, 0.1, 8)
    eff, _ok, _c = learned_params(BASE, s)
    assert float(eff["max_hold_bars"]).is_integer()


def test_summary_reports_progress():
    s = empty_stats()
    _fill(s, "trail", 1.0, 2.0, MIN_SAMPLES)
    out = summary(s, BASE)
    assert out["samples"] == MIN_SAMPLES
    assert out["calibrated"] is True
    assert out["capture_median"] == pytest.approx(0.5, abs=1e-6)
    assert set(out["effective"]) == set(TUNABLE)


def test_summary_before_min_samples_not_calibrated():
    s = empty_stats()
    _fill(s, "trail", 1.0, 2.0, 3)
    out = summary(s, BASE)
    assert out["calibrated"] is False and out["samples"] == 3


def test_hold_and_error_rules_do_not_tune_trailing():
    """hold / error 样本不该被算进移动止损组。"""
    s = empty_stats()
    _fill(s, "hold", 0.0, 0.0, MIN_SAMPLES)
    _fill(s, "error", 0.0, 0.0, 5)
    eff, ok, _c = learned_params(BASE, s)
    assert ok is True
    assert eff["trail_atr_mult"] == pytest.approx(BASE["trail_atr_mult"])


# ---------------- 引擎侧：生效参数是副本，用户改配置即重置 ----------------

def _bare_engine():
    """只装配自迭代需要的属性，不跑完整引擎初始化（避免建目录/连数据源）。"""
    from cryptoagent.config import AppConfig
    from cryptoagent.engine import TradingEngine

    eng = TradingEngine.__new__(TradingEngine)
    eng.cfg = AppConfig()
    eng.state = {}
    eng._save = lambda: None
    base = {k: float(getattr(eng.cfg.position, k)) for k in TUNABLE}
    eng.position_tuning = {
        "stats": {}, "effective": dict(base), "anchor": dict(base),
        "changes": [], "updated_at": 0.0,
    }
    return eng, base


def test_effective_config_is_a_copy_not_mutation():
    eng, base = _bare_engine()
    eng.position_tuning["effective"] = {**base, "trail_atr_mult": base["trail_atr_mult"] * 0.88}
    eff = eng.effective_position_config()
    assert eff is not eng.cfg.position
    assert eff.trail_atr_mult == pytest.approx(base["trail_atr_mult"] * 0.88)
    # 原配置一个字节都不能被改
    assert eng.cfg.position.trail_atr_mult == pytest.approx(base["trail_atr_mult"])


def test_user_config_change_resets_learning():
    eng, base = _bare_engine()
    eng.position_tuning["effective"] = {**base, "trail_atr_mult": 1.5}
    eng.cfg.position.trail_atr_mult = 3.0
    eff = eng.effective_position_config()
    assert eff.trail_atr_mult == pytest.approx(3.0)  # 以用户新配置为准
    assert eng.position_tuning["anchor"]["trail_atr_mult"] == pytest.approx(3.0)


def test_auto_tune_off_returns_original_config():
    eng, base = _bare_engine()
    eng.cfg.position.auto_tune = False
    eng.position_tuning["effective"] = {**base, "trail_atr_mult": 1.0}
    assert eng.effective_position_config() is eng.cfg.position


def test_learning_feeds_effective_params_end_to_end():
    """攒够样本 → 重算 → 生效参数真的变了（且原配置不动）。"""
    eng, base = _bare_engine()
    eng.position_tuning["stats"] = _fill(empty_stats(), "trail", 0.2, 3.0, MIN_SAMPLES)
    eng._recompute_position_tuning()
    eff = eng.effective_position_config()
    assert eff.trail_atr_mult < base["trail_atr_mult"]
    assert eng.cfg.position.trail_atr_mult == pytest.approx(base["trail_atr_mult"])


def test_record_close_writes_r_fields_and_feeds_stats():
    """平仓要把「赚了几个 R」写进历史，并把这一笔喂给校准器。"""
    eng, _base = _bare_engine()
    eng.state = {"position_history": [], "equity": 1000.0}
    pos = {
        "symbol": "SOLUSDT", "side": "LONG", "qty": 1.0, "entry": 100.0,
        "stop": 96.0, "initial_r_pct": 0.02, "opened_at": "2026-11-06 01:00:00",
        "mfe_r": 1.8, "trailed": True, "mode": "paper",
    }
    eng._record_close(pos, 104.0, 4.0, "止损触发", "paper", rule="stop_loss")
    h = eng.state["position_history"][-1]
    assert h["realized_r"] == pytest.approx(2.0)          # (104-100)/100 ÷ 0.02
    assert h["r_pct"] == pytest.approx(0.02)
    assert h["initial_stop"] == pytest.approx(100.0 * (1 - 0.02))
    assert h["mfe_r"] == pytest.approx(1.8)
    assert h["rule"] == "stop_loss"
    # 被移动止损动过的仓位，止损出场归到 trail 组（而不是普通止损）
    assert eng.position_tuning["stats"]["rules"].get("trail") == 1
    assert eng.position_tuning["stats"]["n"] == 1
