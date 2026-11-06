"""账户盈亏归属：累计已实现盈亏、交易记录、持仓历史都要按当前账户隔离。"""
from __future__ import annotations

import threading

import pytest

from cryptoagent.config import AppConfig
from cryptoagent.engine import TradingEngine


def _engine(scope: str = "binance:demo") -> TradingEngine:
    e = object.__new__(TradingEngine)
    e.cfg = AppConfig()
    e.state = {
        "mode": "testnet",
        "testnet": True,
        "exchange": "binance",
        "account_scope": scope,
        "equity": 1000.0,
        "peak_equity": 1000.0,
        "day_start_equity": 1000.0,
        "day": "2026-11-06",
        "positions": {},
        "position": None,
        "fills": [],
        "position_history": [],
        "consecutive_losses": 0,
        "auto": {"running": False, "interval_sec": 300, "execute": False, "last_run": ""},
        "halt": False,
        "history": [],
    }
    e.lock = threading.RLock()
    e.armed = False
    e.live_client = None
    e._live_eq_cache = None
    e._save = lambda: None
    return e


def _close(pnl: float, scope: str | None, day: str = "2026-11-06") -> dict:
    row = {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "pnl": pnl,
        "opened_at": f"{day} 08:00:00",
        "closed_at": f"{day} 12:00:00",
        "reason": "trail",
        "mode": "testnet",
    }
    if scope:
        row["account_scope"] = scope
    return row


def _fill(pnl: float, scope: str | None) -> dict:
    row = {"ts": "2026-11-06 12:00:00", "action": "CLOSE", "pnl": pnl, "account_scope": scope}
    if not scope:
        row.pop("account_scope")
    return row


# ---------------------------------------------------------------------------
# 累计已实现盈亏
# ---------------------------------------------------------------------------
def test_pnl_summary_only_counts_current_scope():
    e = _engine("okx:demo")
    e.state["position_history"] = [
        _close(2.0, "okx:demo"),
        _close(-1.0, "okx:demo"),
        _close(9.0, "binance:demo"),
    ]
    out = e._pnl_summary()
    assert out["scope"] == "okx:demo"
    assert out["realized"] == 1.0          # 2.0 - 1.0，币安的 +9 不能算进来
    assert out["trades"] == 2
    assert out["wins"] == 1 and out["losses"] == 1
    assert out["gross_win"] == 2.0 and out["gross_loss"] == 1.0


def test_pnl_summary_is_zero_for_fresh_account():
    """切到从没交易过的新账户：累计盈亏必须是 0，而不是沿用上一个账户。"""
    e = _engine("binance:demo")
    e.state["position_history"] = [_close(12.5, "binance:demo")]
    assert e._pnl_summary()["realized"] == 12.5

    e.state["account_scope"] = "okx:demo"
    out = e._pnl_summary()
    assert out["realized"] == 0.0
    assert out["trades"] == 0


def test_pnl_summary_uses_full_history_not_last_30():
    """「累计」不能用 status 的 30 条窗口：40 笔各 +1 应得 40。"""
    e = _engine("binance:demo")
    e.state["position_history"] = [_close(1.0, "binance:demo") for _ in range(40)]
    assert e._pnl_summary()["realized"] == 40.0
    assert e._pnl_summary()["trades"] == 40


# ---------------------------------------------------------------------------
# 交易记录 / 持仓历史
# ---------------------------------------------------------------------------
def test_fills_and_history_are_filtered_by_scope():
    e = _engine("okx:demo")
    e.state["fills"] = [_fill(1.0, "okx:demo"), _fill(5.0, "binance:demo")]
    e.state["position_history"] = [_close(1.0, "okx:demo"), _close(5.0, "binance:demo")]

    assert len(e._scoped_rows("fills")) == 1
    assert len(e._scoped_rows("position_history")) == 1
    assert e._scoped_rows("fills")[0]["pnl"] == 1.0
    assert e._scoped_rows("position_history")[0]["pnl"] == 1.0


def test_daily_pnl_matches_pnl_summary():
    """日历与累计必须同口径，否则页面会自相矛盾。"""
    e = _engine("okx:demo")
    e.state["position_history"] = [
        _close(2.0, "okx:demo", "2026-11-05"),
        _close(-0.5, "okx:demo", "2026-11-06"),
        _close(7.0, "binance:demo", "2026-11-06"),
    ]
    daily = e._daily_pnl()
    assert sum(d["pnl"] for d in daily) == e._pnl_summary()["realized"] == 1.5
    assert [d["date"] for d in daily] == ["2026-11-05", "2026-11-06"]


# ---------------------------------------------------------------------------
# 旧记录归因：打一次标签后不再随切换漂移
# ---------------------------------------------------------------------------
def test_legacy_rows_are_tagged_with_current_scope_once():
    e = _engine("binance:demo")
    e.state["fills"] = [_fill(3.0, None)]
    e.state["position_history"] = [_close(3.0, None)]

    e._tag_legacy_ledger()
    assert e.state["fills"][0]["account_scope"] == "binance:demo"
    assert e.state["position_history"][0]["account_scope"] == "binance:demo"

    # 切到 OKX：已打标的旧记录不会跟过来
    e.state["account_scope"] = "okx:demo"
    assert e._pnl_summary()["realized"] == 0.0
    assert e._scoped_rows("fills") == []


def test_legacy_tagging_keeps_existing_labels():
    e = _engine("okx:demo")
    e.state["position_history"] = [_close(1.0, "binance:demo"), _close(2.0, None)]
    e._tag_legacy_ledger()
    tags = [r["account_scope"] for r in e.state["position_history"]]
    assert tags == ["binance:demo", "okx:demo"]   # 已有的不被覆盖，只补空标签


def test_untagged_row_falls_back_to_current_scope():
    """运行时兜底：万一有漏标的行，按当前账户算，不会从两个账户都消失。"""
    e = _engine("okx:demo")
    e.state["position_history"] = [_close(1.0, None)]
    assert e._pnl_summary()["realized"] == 1.0


# ---------------------------------------------------------------------------
# 一键重置：只清三类记录
# ---------------------------------------------------------------------------
def test_reset_clears_three_records_but_keeps_positions_and_settings(tmp_path):
    from cryptoagent.accounts import AccountStore

    e = _engine("binance:demo")
    e.accounts = AccountStore(tmp_path / "accounts")
    e.accounts.save_state("binance:demo", {
        "fills": [_fill(5.0, "binance:demo")],
        "position_history": [_close(5.0, "binance:demo")],
        "history": [{"ts": "12:00:00", "equity": 1000.0}],
        "equity": 1000.0,
    })
    e.state["positions"] = {"BTCUSDT": {"symbol": "BTCUSDT", "side": "LONG", "qty": 1.0, "entry": 100.0}}
    e.state["position_history"] = [_close(5.0, "binance:demo")]
    e.state["fills"] = [_fill(5.0, "binance:demo")]
    e.state["history"] = [{"ts": "12:00:00", "equity": 1000.0}]
    e.state["equity"] = 1234.0
    e.state["peak_equity"] = 1500.0
    e.state["consecutive_losses"] = 3
    e.live_client = None
    e._accounts_cache = (0.0, {})

    out = e.reset_account_records()
    assert out["current"] is True
    assert out["fills"] == 1 and out["position_history"] == 1
    assert e.state["position_history"] == []
    assert e.state["fills"] == []
    assert e.state["history"] == []
    # 持仓与配置不动
    assert e.state["positions"]["BTCUSDT"]["qty"] == 1.0
    assert e.cfg.risk.max_position_pct == AppConfig().risk.max_position_pct
    # 基准对齐当前权益，连亏清零
    assert e.state["equity"] == 1234.0
    assert e.state["peak_equity"] == 1234.0
    assert e.state["day_start_equity"] == 1234.0
    assert e.state["consecutive_losses"] == 0
    assert e._pnl_summary()["realized"] == 0.0


def test_reset_does_not_touch_other_accounts(tmp_path):
    from cryptoagent.accounts import AccountStore

    e = _engine("okx:demo")
    e.accounts = AccountStore(tmp_path / "accounts")
    e.accounts.save_state("binance:demo", {
        "fills": [_fill(7.0, "binance:demo")],
        "position_history": [_close(7.0, "binance:demo")],
        "equity": 900.0,
    })
    e.accounts.save_state("okx:demo", {
        "fills": [_fill(1.0, "okx:demo")],
        "position_history": [_close(1.0, "okx:demo")],
        "equity": 1000.0,
    })
    e._accounts_cache = (0.0, {})

    e.reset_account_records()
    assert e.accounts.summarize("okx:demo")["realized"] == 0.0
    assert e.accounts.summarize("binance:demo")["realized"] == 7.0   # 另一个账户原样保留
    assert e.accounts.summarize("binance:demo")["trades"] == 1


def test_reset_sets_anchor_for_overlay(tmp_path):
    """重置当前账户要记下「时刻 + 当时权益」，供交易所 overlay 重新起算。"""
    import time
    from cryptoagent.accounts import AccountStore

    e = _engine("binance:demo")
    e.accounts = AccountStore(tmp_path / "accounts")
    e.accounts.save_state("binance:demo", {"equity": 1000.0})
    e.state["equity"] = 1234.0
    e.live_client = None
    e._accounts_cache = (0.0, {})

    before = int(time.time() * 1000)
    e.reset_account_records()
    after = int(time.time() * 1000)

    assert before <= e.state["account_reset_epoch"] <= after
    assert e.state["account_reset_equity"] == pytest.approx(1234.0)


def test_reset_other_scope_does_not_set_anchor(tmp_path):
    """重置「非当前」账户只清那个账户的文件，不改变当前账户的 overlay 锚点。"""
    from cryptoagent.accounts import AccountStore

    e = _engine("okx:demo")
    e.accounts = AccountStore(tmp_path / "accounts")
    e.accounts.save_state("okx:demo", {"equity": 1000.0})
    e.accounts.save_state("binance:demo", {
        "fills": [_fill(7.0, "binance:demo")],
        "position_history": [_close(7.0, "binance:demo")],
        "equity": 900.0,
    })
    e.state["account_reset_epoch"] = 0
    e.state["account_reset_equity"] = 0.0
    e._accounts_cache = (0.0, {})

    e.reset_account_records("binance:demo")

    # 当前账户（okx:demo）的锚点不变
    assert e.state["account_reset_epoch"] == 0
    assert e.state["account_reset_equity"] == 0.0
    # 目标账户文件被清空
    assert e.accounts.summarize("binance:demo")["realized"] == 0.0
