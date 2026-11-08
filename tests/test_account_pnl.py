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


# ---------------------------------------------------------------------------
# 费用即时入账：浮盈 / 平仓 pnl 只记价格变动毛额，手续费单独进已实现
# ---------------------------------------------------------------------------
def test_open_fee_enters_realized_immediately():
    """开仓后没有任何平仓记录，累计已实现盈亏就应该是 −开仓费。"""
    e = _engine()
    e.state["fills"] = [{
        "ts": "2026-11-08 11:00:00", "action": "OPEN", "symbol": "BNBUSDT",
        "fee": 4.001473, "fee_mode": "gross", "account_scope": "binance:demo",
    }]
    s = e._pnl_summary()
    assert s["trades"] == 0
    assert s["gross_pnl"] == 0.0
    assert s["fees_paid"] == pytest.approx(4.001473)
    assert s["realized"] == pytest.approx(-4.001473)
    daily = e._daily_pnl()
    assert sum(d["pnl"] for d in daily) == pytest.approx(s["realized"])
    assert daily[0]["pnl"] == pytest.approx(-4.001473)
    assert daily[0]["trades"] == 0                     # 手续费不是一笔交易


def test_close_pnl_is_gross_and_fees_are_counted_separately():
    """平仓 pnl 保持毛额；开仓费 + 平仓费各自在发生时进已实现。"""
    e = _engine()
    e.state["fills"] = [
        {"ts": "2026-11-08 11:00:00", "action": "OPEN", "fee": 2.5,
         "fee_mode": "gross", "account_scope": "binance:demo"},
        {"ts": "2026-11-08 11:30:00", "action": "CLOSE", "pnl": 5.95, "fee": 2.5,
         "fee_mode": "gross", "account_scope": "binance:demo"},
    ]
    e.state["position_history"] = [_close(5.95, "binance:demo", "2026-11-08")]
    s = e._pnl_summary()
    assert s["gross_pnl"] == pytest.approx(5.95)
    assert s["fees_paid"] == pytest.approx(5.0)
    assert s["realized"] == pytest.approx(0.95)
    assert sum(d["pnl"] for d in e._daily_pnl()) == pytest.approx(0.95)


def test_legacy_close_fee_not_double_counted():
    """旧口径 CLOSE 的 pnl 已扣过平仓费，不能再扣一次；开仓费仍要补上。"""
    e = _engine()
    e.state["fills"] = [
        {"ts": "2026-11-06 11:00:00", "action": "OPEN", "fee": 2.5,
         "account_scope": "binance:demo"},
        {"ts": "2026-11-06 12:00:00", "action": "CLOSE", "pnl": 3.45, "fee": 2.5,
         "account_scope": "binance:demo"},
    ]
    e.state["position_history"] = [_close(3.45, "binance:demo")]
    s = e._pnl_summary()
    assert s["fees_paid"] == pytest.approx(2.5)
    assert s["realized"] == pytest.approx(0.95)


def test_account_store_summary_uses_same_fee_rule(tmp_path):
    """跨账户摘要与页面累计必须同口径，日历合计也要对得上。"""
    from cryptoagent.accounts import AccountStore

    store = AccountStore(tmp_path / "accounts")
    store.save_state("binance:demo", {
        "equity": 1000.0,
        "fills": [
            {"ts": "2026-11-08 11:00:00", "action": "OPEN", "fee": 2.5, "fee_mode": "gross"},
            {"ts": "2026-11-08 11:30:00", "action": "CLOSE", "pnl": 5.95, "fee": 2.5,
             "fee_mode": "gross"},
        ],
        "position_history": [_close(5.95, "binance:demo", "2026-11-08")],
    })
    s = store.summarize("binance:demo")
    assert s["fees_paid"] == pytest.approx(5.0)
    assert s["realized"] == pytest.approx(0.95)
    assert sum(d["pnl"] for d in s["daily"]) == pytest.approx(0.95)


def test_paper_round_trip_keeps_gross_pnl_and_fees_in_realized(tmp_path):
    """端到端：浮盈是毛额、平仓 pnl 是同一个毛额、费用即时进已实现。"""
    import time

    from cryptoagent.config import AppConfig
    from cryptoagent.engine import TradingEngine

    cfg = AppConfig()
    cfg.runs_dir = str(tmp_path)
    cfg.backtest.costs.fee_bps = 5.0
    cfg.backtest.costs.slippage_bps = 0.0
    e = TradingEngine(cfg)
    e.cfg.market.symbol = "BTCUSDT"
    e.state.update({"equity": 10000.0, "fills": [], "position_history": [],
                    "positions": {}, "position": None})

    e._paper_open("LONG", 0.1, 75000.0, 5, 74000.0, 76000.0, "test")
    open_fee = 75000.0 * 0.1 * 0.0005                    # 3.75
    assert e.state["equity"] == pytest.approx(10000.0 - open_fee)
    assert e._pnl_summary()["realized"] == pytest.approx(-open_fee)

    e._last_prices["BTCUSDT"] = 76000.0
    e._price_ts["BTCUSDT"] = time.time()
    pos = e.status(price_override=76000.0)["positions"][0]
    assert pos["unrealized"] == pytest.approx(100.0)     # 毛额，未扣任何手续费
    assert pos["unrealized"] == pos["unrealized_gross"]

    rec = e._paper_close(76000.0, "test")
    close_fee = 76000.0 * 0.1 * 0.0005                   # 3.8
    assert rec["pnl"] == pytest.approx(100.0)            # 与平仓前浮盈完全一致
    assert rec["fee"] == pytest.approx(close_fee)
    assert e._pnl_summary()["realized"] == pytest.approx(100.0 - open_fee - close_fee)
    assert e.state["equity"] == pytest.approx(10000.0 - open_fee + 100.0 - close_fee)
    assert sum(d["pnl"] for d in e._daily_pnl()) == pytest.approx(
        e._pnl_summary()["realized"])
