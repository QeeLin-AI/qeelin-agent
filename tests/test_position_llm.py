"""持仓管理员 LLM 接入 + 规则层可配置项的回归测试。

重点守住三条底线：
1. 新增开关默认不改变既有退出行为（升级后动作一致）；
2. LLM 只能产出解释 / 影子意见，绝不能改变 action；
3. 谷时与冷却闸门能真正挡住重复调用。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from cryptoagent.agents.llm import LLMResponse  # noqa: E402
from cryptoagent.agents.position_manager import (  # noqa: E402
    explain_management,
    manage_position,
    review_management,
    shadow_review,
)
from cryptoagent.config import AppConfig, PositionConfig  # noqa: E402


class FakeLLM:
    """记录调用次数，模拟 text()/json() 的真实返回类型。"""

    def __init__(self, json_data: dict | None = None, raise_on_json: bool = False):
        self.calls: list[str] = []
        self.json_data = json_data
        self.raise_on_json = raise_on_json

    def text(self, system, user, mock):
        self.calls.append("text")
        return LLMResponse("模型给出的权衡解释", "fake")

    def json(self, system, user, schema_hint, mock):
        self.calls.append("json")
        if self.raise_on_json:
            raise RuntimeError("模拟模型故障")
        return (self.json_data or mock), LLMResponse("", "fake")


def _df(closes):
    c = np.asarray(closes, dtype=float)
    idx = pd.date_range("2026-01-01", periods=len(c), freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": c, "high": c * 1.001, "low": c * 0.999,
         "close": c, "volume": np.ones(len(c)) * 1000},
        index=idx,
    )


def _pos(entry=100.0, stop=99.0, side="LONG", bars=20):
    return {
        "side": side, "entry": entry, "stop": stop, "tp": 104.0,
        "qty": 1.0, "symbol": "BTCUSDT",
        "opened_epoch": time.time() - bars * 3600,
        "initial_r_pct": abs(entry - stop) / entry,
    }


def _decision(action="tighten"):
    from cryptoagent.agents.position_manager import PositionDecision
    return PositionDecision(action, "移动止损：浮盈 1.20R", 100.5,
                            {"bars_held": 20, "floating_r": 1.2, "atr_pct": 0.0065,
                             "side": "LONG"})


# ---------------------------------------------------------------- 默认值 / 行为不变
def test_new_flags_default_to_existing_behavior():
    p = PositionConfig()
    assert p.llm_explain is False
    assert p.llm_shadow is False
    assert p.llm_offpeak_only is True
    assert p.peak_drawdown_exit_enabled is False   # 新增退出规则必须默认关
    assert p.reversal_ema_bars == 48               # 与原硬编码等价
    assert p.composite_max_age_sec == 300


def test_explain_management_reads_response_content():
    # 历史 bug：这里曾读 resp.text，而 LLMResponse 的字段是 content，永远返回空串
    llm = FakeLLM()
    assert explain_management(llm, _decision(), _pos()) == "模型给出的权衡解释"
    assert llm.calls == ["text"]


def test_explain_management_skips_hold():
    llm = FakeLLM()
    assert explain_management(llm, _decision("hold"), _pos()) == ""
    assert llm.calls == []


# ---------------------------------------------------------------- 影子复核
def test_shadow_review_structure_and_action_whitelist():
    llm = FakeLLM({"explain": "锁定已有利润", "action": "CLOSE",
                   "confidence": 0.82, "reason": "动量转弱"})
    out = shadow_review(llm, _decision(), _pos())
    assert out["action"] == "close"
    assert out["explain"] == "锁定已有利润"
    assert out["confidence"] == 0.82
    assert out["degraded"] is False
    assert llm.calls == ["json"]

    bad = FakeLLM({"explain": "", "action": "开新仓", "confidence": "abc", "reason": ""})
    out2 = shadow_review(bad, _decision(), _pos())
    assert out2["action"] == "hold"       # 非法动作一律收敛成 hold
    assert out2["confidence"] == 0.0


def test_shadow_review_failure_returns_none():
    assert shadow_review(FakeLLM(raise_on_json=True), _decision(), _pos()) is None


def test_review_management_zero_calls_when_switches_off():
    llm = FakeLLM()
    out = review_management(llm, PositionConfig(), _decision(), _pos())
    assert out == {"explanation": "", "shadow": None}
    assert llm.calls == []


def test_review_management_shadow_costs_one_call_and_never_changes_action():
    llm = FakeLLM({"explain": "锁定利润", "action": "close",
                   "confidence": 0.7, "reason": "回撤偏大"})
    cfg = PositionConfig(llm_explain=True, llm_shadow=True)
    dec = _decision("tighten")
    out = review_management(llm, cfg, dec, _pos())
    assert llm.calls == ["json"]          # 两个开关共用一次调用
    assert out["explanation"] == "锁定利润"
    assert out["shadow"]["action"] == "close"
    assert dec.action == "tighten"        # 影子意见绝不改动作


def test_review_management_hold_only_runs_in_shadow_mode():
    llm = FakeLLM()
    review_management(llm, PositionConfig(llm_explain=True), _decision("hold"), _pos())
    assert llm.calls == []


# ---------------------------------------------------------------- 规则层
def _peak_df():
    closes = [100.0] * 260 + list(np.linspace(100, 103, 30)) + list(np.linspace(103, 100.5, 10))
    return _df(closes)


def test_peak_drawdown_exit_off_by_default():
    cfg = PositionConfig(reversal_enabled=False)
    d = manage_position(_pos(), _peak_df(), 100.5, cfg)
    assert d.action == "hold"


def test_peak_drawdown_exit_when_enabled():
    cfg = PositionConfig(reversal_enabled=False, peak_drawdown_exit_enabled=True,
                         peak_drawdown_r=1.0)
    d = manage_position(_pos(), _peak_df(), 100.5, cfg)
    assert d.action == "close"
    assert "峰值回撤" in d.reason
    assert d.details["drawdown_r"] >= 1.0


def test_stale_composite_does_not_force_exit():
    df = _df([100.0] * 300)
    cfg = PositionConfig(reversal_composite=0.7)
    stale = manage_position(_pos(), df, 100.0, cfg, composite=-0.9,
                            composite_ts=time.time() - 1000)
    assert stale.action == "hold"
    assert stale.details.get("composite_stale") is True

    fresh = manage_position(_pos(), df, 100.0, cfg, composite=-0.9,
                            composite_ts=time.time())
    assert fresh.action == "close"
    assert "因子信号强烈反向" in fresh.reason


def test_ema_span_is_configurable():
    df = _df([100.0] * 300)
    cfg = PositionConfig(reversal_ema_bars=20, reversal_composite=0.7)
    d = manage_position(_pos(), df, 100.0, cfg)
    assert d.action == "hold"   # 平坦行情下改跨度不应凭空触发退出


# ---------------------------------------------------------------- LLM 闸门
def _bare_engine(cfg: AppConfig):
    from cryptoagent.engine import TradingEngine
    e = object.__new__(TradingEngine)
    e.cfg = cfg
    e.state = {}
    e._position_llm = None
    e._position_llm_at = {}
    e.position_llm_usage = {"calls": 0, "errors": 0,
                            "skipped_offpeak": 0, "skipped_cooldown": 0}
    return e


def test_llm_cooldown_blocks_second_call():
    cfg = AppConfig()
    cfg.position.llm_offpeak_only = False
    cfg.position.llm_min_interval_sec = 900
    e = _bare_engine(cfg)
    assert e._position_llm_allowed("BTCUSDT") is True
    assert e._position_llm_allowed("BTCUSDT") is False
    assert e.position_llm_usage["skipped_cooldown"] == 1
    assert e._position_llm_allowed("ETHUSDT") is True   # 冷却按品种独立
    # 闸门本身不记「成功调用」：只有真正发起并结束调用后才计次，
    # 否则失败调用也会被算进 calls，账单估算偏乐观。
    e._commit_position_llm_call("BTCUSDT", True)
    e._commit_position_llm_call("ETHUSDT", True)
    assert e.position_llm_usage["calls"] == 2


def test_invalidate_drops_shared_client():
    cfg = AppConfig()
    e = _bare_engine(cfg)
    e._position_llm = SimpleNamespace(provider="stale")
    e.invalidate_position_llm()
    assert e._position_llm is None
