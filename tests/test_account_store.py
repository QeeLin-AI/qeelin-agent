"""分账户账本：隔离、迁移归因、跨账户汇总与查询。"""
from __future__ import annotations

import json

import pytest

from cryptoagent.accounts import AccountStore, scope_of, split_scope


def _row(pnl: float, closed_at: str, scope: str | None = None, symbol: str = "BTCUSDT") -> dict:
    row = {
        "symbol": symbol,
        "side": "LONG",
        "pnl": pnl,
        "opened_at": closed_at,
        "closed_at": closed_at,
        "mode": "testnet",
        "reason": "trail",
    }
    if scope:
        row["account_scope"] = scope
    return row


def _state(scope: str, rows: list[dict], baseline: str = "") -> dict:
    return {
        "account_scope": scope,
        "exchange": split_scope(scope)[0],
        "testnet": split_scope(scope)[1] != "live",
        "equity": 1000.0,
        "peak_equity": 1100.0,
        "day_start_equity": 1000.0,
        "day": "2026-11-06",
        "consecutive_losses": 2,
        "live_baseline_at": baseline,
        "fills": [],
        "position_history": rows,
        "history": [{"ts": "12:00:00", "equity": 1000.0}],
    }


def test_scope_helpers():
    assert scope_of("OKX", True) == "okx:demo"
    assert scope_of("binance", False) == "binance:live"
    assert split_scope("okx:live") == ("okx", "live")
    assert split_scope("") == ("binance", "demo")


def test_each_account_has_its_own_file(tmp_path):
    store = AccountStore(tmp_path / "accounts")
    s1 = _state("binance:demo", [_row(1.0, "2026-11-01 10:00:00")])
    store.save_state("binance:demo", s1)
    s2 = _state("okx:demo", [_row(-2.0, "2026-11-02 10:00:00")])
    store.save_state("okx:demo", s2)

    assert store.path("binance:demo").exists()
    assert store.path("okx:demo").exists()
    assert store.summarize("binance:demo")["realized"] == 1.0
    assert store.summarize("okx:demo")["realized"] == -2.0
    assert sorted(store.scopes()) == ["binance:demo", "okx:demo"]


def test_apply_to_state_clears_history_for_new_account(tmp_path):
    store = AccountStore(tmp_path / "accounts")
    store.save_state("binance:demo", _state("binance:demo", [_row(3.0, "2026-11-01 10:00:00")]))

    state = _state("okx:demo", [_row(99.0, "2026-11-02 10:00:00")])
    existed = store.apply_to_state("okx:demo", state)
    assert existed is False
    assert state["position_history"] == []
    assert state["fills"] == []
    assert state["history"] == []


def test_apply_to_state_restores_existing_account(tmp_path):
    store = AccountStore(tmp_path / "accounts")
    store.save_state("binance:demo", _state("binance:demo", [_row(3.0, "2026-11-01 10:00:00")]))

    state = _state("okx:demo", [])
    existed = store.apply_to_state("binance:demo", state)
    assert existed is True
    assert len(state["position_history"]) == 1
    assert state["equity"] == 1000.0


def test_migrate_splits_by_time_boundary(tmp_path):
    """切换前的无标签记录归上一个账户，切换后的归当前账户。"""
    store = AccountStore(tmp_path / "accounts")
    state = _state(
        "okx:demo",
        [
            _row(5.0, "2026-11-06 10:00:00"),   # 切换前：币安
            _row(-1.0, "2026-11-06 21:00:00"),  # 切换后：OKX
            _row(2.0, "2026-11-06 11:00:00", scope="binance:demo"),
        ],
        baseline="2026-11-06 20:00:00",
    )
    src = tmp_path / "engine_state.json"
    src.write_text(json.dumps(state), encoding="utf-8")

    out = store.migrate(state, src)
    assert out["migrated"] is True
    assert out["accounts"]["binance:demo"]["position_history"] == 2
    assert out["accounts"]["okx:demo"]["position_history"] == 1
    assert store.summarize("binance:demo")["realized"] == 7.0
    assert store.summarize("okx:demo")["realized"] == -1.0
    # state 只剩当前账户
    assert [r["pnl"] for r in state["position_history"]] == [-1.0]
    # 迁移前备份
    assert out["backup"]
    assert (tmp_path / "backup").exists()


def test_migrate_is_idempotent(tmp_path):
    store = AccountStore(tmp_path / "accounts")
    state = _state("okx:demo", [_row(1.0, "2026-11-06 10:00:00")], baseline="2026-11-06 20:00:00")
    assert store.migrate(state)["migrated"] is True
    second = store.migrate(_state("okx:demo", [_row(9.0, "2026-11-06 10:00:00")]))
    assert second["migrated"] is False
    # 二次调用不得改动已有账本
    assert store.summarize("binance:demo")["realized"] == 1.0


def test_migrate_without_baseline_keeps_current_account(tmp_path):
    store = AccountStore(tmp_path / "accounts")
    state = _state("binance:demo", [_row(4.0, "2026-11-01 10:00:00")])
    out = store.migrate(state)
    assert out["accounts"]["binance:demo"]["position_history"] == 1
    assert store.summarize("binance:demo")["realized"] == 4.0


def test_migrate_does_not_use_mode_field_for_scope(tmp_path):
    """交易所模拟盘的平仓记录 mode 也写 'live'（表示走交易所，不是主网）。

    迁移只能用时间边界判断归属；按 mode 判断会把币安模拟盘的历史误归到主网账户。
    """
    store = AccountStore(tmp_path / "accounts")
    row = _row(5.0, "2026-11-06 10:00:00")
    row["mode"] = "live"      # 关键：模拟盘也长这样
    state = _state("okx:demo", [row], baseline="2026-11-06 20:00:00")
    store.migrate(state)
    assert store.summarize("binance:demo")["realized"] == 5.0
    assert store.summarize("okx:demo")["realized"] == 0.0
    assert store.summarize("binance:live")["realized"] == 0.0


def test_migrate_uses_existing_tag_to_guess_previous_account(tmp_path):
    """同交易所换环境：旧记录的标签是 binance:demo，无标签的记录也该归它。"""
    store = AccountStore(tmp_path / "accounts")
    state = _state(
        "binance:live",
        [
            _row(2.0, "2026-11-01 10:00:00", scope="binance:demo"),
            _row(3.0, "2026-11-06 10:00:00"),
        ],
        baseline="2026-11-06 20:00:00",
    )
    store.migrate(state)
    assert store.summarize("binance:demo")["realized"] == 5.0
    assert store.summarize("binance:live")["realized"] == 0.0
    assert store.summarize("okx:live")["realized"] == 0.0


def test_summarize_all_combines_accounts(tmp_path):
    store = AccountStore(tmp_path / "accounts")
    store.save_state("binance:demo", _state("binance:demo", [
        _row(2.0, "2026-11-01 10:00:00"),
        _row(-1.0, "2026-11-01 12:00:00"),
    ]))
    store.save_state("okx:demo", _state("okx:demo", [
        _row(5.0, "2026-11-02 10:00:00"),
    ]))

    all_ = store.summarize_all()
    assert len(all_["accounts"]) == 2
    assert all_["combined"]["realized"] == 6.0
    assert all_["combined"]["trades"] == 3
    assert all_["combined"]["by_exchange"]["binance"]["realized"] == 1.0
    assert all_["combined"]["by_exchange"]["okx"]["realized"] == 5.0
    assert [d["date"] for d in all_["combined"]["by_day"]] == ["2026-11-01", "2026-11-02"]
    assert all_["combined"]["by_day"][1]["pnl"] == 5.0


def test_query_filters_by_scope_time_and_symbol(tmp_path):
    store = AccountStore(tmp_path / "accounts")
    store.save_state("binance:demo", _state("binance:demo", [
        _row(1.0, "2026-11-01 10:00:00", symbol="BTCUSDT"),
        _row(2.0, "2026-11-03 10:00:00", symbol="ETHUSDT"),
    ]))
    store.save_state("okx:demo", _state("okx:demo", [
        _row(3.0, "2026-11-02 10:00:00", symbol="BTCUSDT"),
    ]))

    assert len(store.query()) == 3
    assert len(store.query(scope="okx:demo")) == 1
    assert [r["pnl"] for r in store.query(since="2026-11-02")] == [3.0, 2.0]
    assert [r["pnl"] for r in store.query(symbol="BTCUSDT")] == [1.0, 3.0]
    assert store.query(limit=1)[0]["pnl"] == 2.0   # 取最近 1 条


def test_summarize_reports_drawdown_and_win_rate(tmp_path):
    store = AccountStore(tmp_path / "accounts")
    store.save_state("okx:demo", _state("okx:demo", [
        _row(3.0, "2026-11-01 10:00:00"),
        _row(-1.0, "2026-11-02 10:00:00"),
    ]))
    s = store.summarize("okx:demo")
    assert s["realized"] == 2.0
    assert s["win_rate"] == 50.0
    assert s["profit_factor"] == 3.0
    assert s["drawdown_pct"] == pytest.approx(-9.09, abs=0.02)   # 1000 / 1100


def test_reset_anchor_persists_and_restores(tmp_path):
    """重置锚点（reset_epoch / reset_equity）跟账户走：切走再切回要恢复，新账户要清零。"""
    store = AccountStore(tmp_path / "accounts")
    s1 = _state("binance:demo", [])
    s1["account_reset_epoch"] = 1234567890000
    s1["account_reset_equity"] = 5000.0
    store.save_state("binance:demo", s1)

    # 切到从未重置过的新账户：锚点必须清零，否则会用上一个账户的锚点去过滤
    fresh = _state("okx:demo", [])
    store.apply_to_state("okx:demo", fresh)
    assert fresh["account_reset_epoch"] == 0
    assert fresh["account_reset_equity"] == 0.0

    # 切回重置过的账户：锚点恢复
    back = _state("okx:demo", [])
    store.apply_to_state("binance:demo", back)
    assert back["account_reset_epoch"] == 1234567890000
    assert back["account_reset_equity"] == 5000.0
