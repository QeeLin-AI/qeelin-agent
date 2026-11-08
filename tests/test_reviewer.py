"""复盘官与绩效官单测：确定性归因口径 + KPI 统计。"""
from __future__ import annotations

from cryptoagent.agents.reviewer import review_trade, summarize_reviews
from cryptoagent.agents.scorekeeper import compute_kpis, kpi_delta


# --------------------------------------------------------------------------
# 复盘官
# --------------------------------------------------------------------------
def test_good_trade():
    rv = review_trade({"realized_r": 1.5, "mfe_r": 2.0, "r_pct": 0.02, "rule": "tp"})
    assert rv["verdict"] == "good"
    assert rv["capture_ratio"] == 0.75
    assert rv["mistakes"] == []


def test_early_exit():
    rv = review_trade({"realized_r": 0.2, "mfe_r": 1.5, "r_pct": 0.02})
    assert rv["verdict"] == "bad_exit"
    assert "exit_too_early" in rv["mistakes"]


def test_entry_immediately_wrong():
    rv = review_trade({"realized_r": -1.0, "mfe_r": 0.05, "r_pct": 0.02})
    assert rv["verdict"] == "bad_entry"
    assert "entry_immediately_wrong" in rv["mistakes"]


def test_gave_back():
    rv = review_trade({"realized_r": -1.0, "mfe_r": 1.5, "r_pct": 0.02})
    assert rv["verdict"] == "gave_back"
    assert "gave_back_profit" in rv["mistakes"]


def test_tight_stop_flagged():
    rv = review_trade({"realized_r": -0.8, "mfe_r": 0.1, "r_pct": 0.004})
    assert "stop_too_tight" in rv["mistakes"]


def test_missing_fields_neutral():
    """老记录缺 realized_r 时必须降级，不能抛异常。"""
    rv = review_trade({"rule": "manual"})
    assert rv["verdict"] == "neutral"
    assert rv["capture_ratio"] is None


def test_summarize_reviews():
    rows = [
        {"review": {"verdict": "good", "mistakes": []}},
        {"review": {"verdict": "bad_entry", "mistakes": ["entry_immediately_wrong"]}},
        {"review": {"verdict": "bad_entry", "mistakes": ["entry_immediately_wrong", "stop_too_tight"]}},
    ]
    s = summarize_reviews(rows)
    assert s["n"] == 3
    assert s["by_verdict"]["bad_entry"] == 2
    assert s["by_mistake"]["entry_immediately_wrong"] == 2
    assert s["good_ratio"] == round(1 / 3, 4)


# --------------------------------------------------------------------------
# 绩效官
# --------------------------------------------------------------------------
def _rows():
    return [
        {"realized_r": 1.5, "mfe_r": 2.0, "rule": "tp", "symbol": "SOLUSDT", "side": "LONG"},
        {"realized_r": -1.0, "mfe_r": 0.1, "rule": "stop", "symbol": "SOLUSDT", "side": "LONG"},
        {"realized_r": 0.5, "mfe_r": 1.2, "rule": "trail", "symbol": "ZECUSDT", "side": "SHORT"},
        {"realized_r": -0.5, "mfe_r": 0.9, "rule": "time_stop", "symbol": "ZECUSDT", "side": "SHORT"},
        {"realized_r": 2.0, "mfe_r": 2.4, "rule": "tp", "symbol": "SOLUSDT", "side": "LONG"},
        {"realized_r": -1.0, "mfe_r": 0.2, "rule": "stop", "symbol": "SOLUSDT", "side": "SHORT"},
        {"realized_r": 0.3, "mfe_r": 1.1, "rule": "trail", "symbol": "ZECUSDT", "side": "LONG"},
        {"realized_r": -0.8, "mfe_r": 0.05, "rule": "stop", "symbol": "ZECUSDT", "side": "LONG"},
    ]


def test_kpi_overall():
    rows = _rows()
    for r in rows:
        r["review"] = review_trade(r)
    k = compute_kpis(rows)
    o = k["overall"]
    assert o["n_with_r"] == 8
    assert o["win_rate"] == 0.5
    assert o["expectancy_r"] == 0.125
    assert o["total_r"] == 1.0
    # 8 笔远低于最小样本，不允许据此下结论
    assert k["sample_ok"] is False
    assert k["verdict_ready"] is False


def test_kpi_groups():
    rows = _rows()
    for r in rows:
        r["review"] = review_trade(r)
    k = compute_kpis(rows)
    assert set(k["by_rule"]) == {"tp", "stop", "trail", "time_stop"}
    assert set(k["by_symbol"]) == {"SOLUSDT", "ZECUSDT"}
    assert k["by_direction"]["LONG"]["n_with_r"] == 5
    assert k["by_direction"]["SHORT"]["n_with_r"] == 3
    assert k["review_summary"]["by_mistake"]["entry_immediately_wrong"] >= 1


def test_capture_ignores_never_profitable():
    """从未浮盈过的单不计入捕获率，否则均值会被负值污染。"""
    rows = [
        {"realized_r": -1.0, "mfe_r": 0.05},
        {"realized_r": 0.9, "mfe_r": 2.0},
    ]
    k = compute_kpis(rows)
    assert k["capture_sample"] == 1
    assert k["capture_ratio_avg"] == 0.45


def test_kpi_skips_missing_r():
    k = compute_kpis([{"realized_r": None}, {"mfe_r": 1.0}, {}])
    assert k["overall"]["n"] == 3
    assert k["overall"]["n_with_r"] == 0
    assert k["sample_ok"] is False


def test_kpi_delta():
    d = kpi_delta({"overall": {"expectancy_r": 0.1, "win_rate": 0.4}},
                  {"overall": {"expectancy_r": 0.25, "win_rate": 0.5}})
    assert d["expectancy_r"]["delta"] == 0.15
    assert d["win_rate"]["delta"] == 0.1
    assert d["r_sharpe"]["delta"] is None
