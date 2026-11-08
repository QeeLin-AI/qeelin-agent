"""进化治理层单测：闸门 fail-closed、账本、应用后归因。"""
from __future__ import annotations

from cryptoagent.evolution import (
    Governor,
    attribute_change,
    validate_change,
)


def _kpi(n: int, expectancy: float = 0.1, dd: float = -2.0) -> dict:
    return {"overall": {"n_with_r": n, "expectancy_r": expectancy, "max_dd_r": dd}}


# --------------------------------------------------------------------------
# 闸门：没有证据 / 样本不足一律不放行
# --------------------------------------------------------------------------
def test_validate_rejects_low_sample():
    r = validate_change({"kind": "replay", "delta_expectancy_r": 0.5}, _kpi(5))
    assert r["passed"] is False
    assert r["checks"]["sample_ok"] is False


def test_validate_rejects_no_evidence():
    r = validate_change(None, _kpi(50))
    assert r["passed"] is False
    assert r["checks"]["has_evidence"] is False


def test_validate_passes_when_improved_and_risk_ok():
    r = validate_change(
        {"kind": "replay", "delta_expectancy_r": 0.2, "delta_max_dd_r": 0.1}, _kpi(50))
    assert r["passed"] is True
    assert r["checks"]["improved"] is True
    assert r["checks"]["risk_not_worse"] is True


def test_validate_rejects_risk_worse():
    """期望值再好，回撤明显变大也不许上线。"""
    r = validate_change(
        {"kind": "replay", "delta_expectancy_r": 0.3, "delta_max_dd_r": -2.0}, _kpi(50))
    assert r["passed"] is False
    assert r["checks"]["risk_not_worse"] is False


def test_validate_rejects_no_improvement():
    r = validate_change(
        {"kind": "replay", "delta_expectancy_r": 0.0, "delta_max_dd_r": 0.0}, _kpi(50))
    assert r["passed"] is False
    assert r["checks"]["improved"] is False


# --------------------------------------------------------------------------
# 账本
# --------------------------------------------------------------------------
def test_ledger_record_and_rollback(tmp_path):
    gov = Governor(tmp_path)
    rec = gov.record_applied(
        "config", "risk.min_confidence", 0.5, 0.45, "settings_evolver", "测试", _kpi(30))
    assert rec["status"] == "applied"
    assert rec["n_at_apply"] == 30

    got = gov.ledger.get(rec["id"])
    assert got["before"] == 0.5
    assert got["after"] == 0.45

    gov.rollback(rec["id"])
    assert gov.ledger.get(rec["id"])["status"] == "rolled_back"


def test_ledger_persists_across_instances(tmp_path):
    gov = Governor(tmp_path)
    rec = gov.record_applied("config", "risk.max_position_pct", 0.2, 0.25, "t", "r", _kpi(10))
    again = Governor(tmp_path)
    assert again.ledger.get(rec["id"]) is not None


def test_record_applied_with_explicit_n(tmp_path):
    """因子落库没有 KPI 快照，必须能显式指定归因起点。"""
    gov = Governor(tmp_path)
    rec = gov.record_applied(
        "factor", "factor_library.probe", None, "新建因子文件", "factor_research",
        "假设", None, n_at_apply=42,
        evidence={"kind": "research_gate", "oos_sharpe": 0.5})
    assert rec["n_at_apply"] == 42
    assert rec["evidence"]["kind"] == "research_gate"


def test_propose_rejects_unknown_kind(tmp_path):
    gov = Governor(tmp_path)
    try:
        gov.propose("source_code", "x", 1, 2, "manual", "试图改源码")
    except ValueError as exc:
        assert "未知改动类型" in str(exc)
    else:  # pragma: no cover - 必须抛错
        raise AssertionError("未知 kind 必须拒绝")


# --------------------------------------------------------------------------
# 应用后归因
# --------------------------------------------------------------------------
def test_attribute_needs_samples():
    ch = {"n_at_apply": 20, "status": "applied"}
    rows = [{"realized_r": 1.0}] * 20 + [{"realized_r": 0.5}] * 5
    a = attribute_change(ch, rows)
    assert a["ready"] is False
    assert a["have"] == 5
    assert a["need"] >= 1


def test_attribute_degraded():
    ch = {"n_at_apply": 20, "status": "applied"}
    rows = [{"realized_r": 1.0}] * 20 + [{"realized_r": -1.0}] * 20
    a = attribute_change(ch, rows)
    assert a["ready"] is True
    assert a["verdict"] == "degraded"
    assert a["delta_expectancy_r"] < 0


def test_attribute_improved():
    ch = {"n_at_apply": 20, "status": "applied"}
    rows = [{"realized_r": 0.0}] * 20 + [{"realized_r": 1.0}] * 20
    a = attribute_change(ch, rows)
    assert a["ready"] is True
    assert a["verdict"] == "improved"


def test_governor_marks_degraded_in_ledger(tmp_path):
    """应用后变差必须回写状态，否则「该回滚」的信号会丢。"""
    gov = Governor(tmp_path)
    rec = gov.record_applied(
        "config", "risk.min_confidence", 0.5, 0.45, "t", "r", _kpi(20))
    rows = [{"realized_r": 1.0}] * 20 + [{"realized_r": -1.0}] * 20
    out = gov.changes_with_attribution(rows)
    assert out["items"][0]["id"] == rec["id"]
    assert out["items"][0]["status"] == "degraded"
    assert gov.ledger.get(rec["id"])["status"] == "degraded"
    assert out["summary"]["by_status"]["degraded"] == 1
