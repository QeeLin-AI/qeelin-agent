"""复盘官：把一笔平仓回合翻译成结构化归因。

设计原则与全项目一致：**判定由确定性规则给出，不调用 LLM**。
理由很实在——归因是所有进化系统的输入。如果「为什么亏」本身来自模型，
后续的参数/规则/因子学习就全部建立在可能不存在的因果上，越学越偏。

判定只用 `position_history` 里已有的字段（realized_r / mfe_r / r_pct /
rule / reason / side），因此对历史数据同样有效，不需要补任何埋点。

`narrative`（人话解释）留给 LLM 旁路去补，不参与 verdict。
"""
from __future__ import annotations

import math

# 归因阈值集中在这里：改动它们会直接改变全系统的学习方向，必须谨慎。
CAPTURE_GOOD = 0.55        # 实现 R / 最大浮盈 R 达到这个比例算「拿住了」
CAPTURE_POOR = 0.35        # 低于这个比例算「看对没拿到」
MFE_MEANINGFUL = 0.8       # 浮盈到过 0.8R 才算「真的有过机会」
MFE_NONE = 0.3             # 浮盈不到 0.3R 就亏 = 入场即错
GOOD_R = 0.5               # 实现 0.5R 以上才算一笔成功的交易
TIGHT_STOP_PCT = 0.006     # 初始止损宽度 < 0.6% 视为过紧（正常波动就扫）


def _num(value) -> float | None:
    try:
        f = float(value)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def review_trade(row: dict) -> dict:
    """对一笔已平仓的回合做归因，返回可直接落盘的结构化结果。

    任何字段缺失都降级为 neutral，绝不抛异常——复盘是旁路，不能影响记账。
    """
    row = row or {}
    realized = _num(row.get("realized_r"))
    mfe = _num(row.get("mfe_r"))
    r_pct = _num(row.get("r_pct"))
    rule = str(row.get("rule") or row.get("reason") or "")
    side = "SHORT" if str(row.get("side") or "").upper().startswith("S") else "LONG"

    # 捕获率：实现 R / 最大浮盈 R。>1 说明平仓价优于浮盈峰值（少见，通常来自滑点口径差异）
    capture = None
    if realized is not None and mfe is not None and mfe > 0:
        capture = round(max(-1.0, min(2.0, realized / mfe)), 4)

    if realized is None:
        return {
            "verdict": "neutral",
            "capture_ratio": None,
            "mistakes": [],
            "lesson": "缺少 realized_r，无法归因",
            "rule": rule,
            "side": side,
        }

    verdict = "neutral"
    mistakes: list[str] = []
    lesson = ""

    if realized > 0:
        if capture is not None and capture < CAPTURE_POOR and (mfe or 0.0) >= MFE_MEANINGFUL:
            verdict = "bad_exit"
            mistakes.append("exit_too_early")
            lesson = f"看对了但只落袋 {realized:+.2f}R，最高浮盈 {mfe:+.2f}R，退出偏早"
        elif realized >= GOOD_R and (capture is None or capture >= CAPTURE_GOOD):
            verdict = "good"
            lesson = f"实现 {realized:+.2f}R"
        else:
            verdict = "good"
            lesson = f"小赚 {realized:+.2f}R"
    else:
        if mfe is not None and mfe >= MFE_MEANINGFUL:
            verdict = "gave_back"
            mistakes.append("gave_back_profit")
            lesson = f"曾浮盈 {mfe:+.2f}R 却亏 {realized:+.2f}R，退出规则没保住利润"
        elif mfe is not None and mfe < MFE_NONE:
            verdict = "bad_entry"
            mistakes.append("entry_immediately_wrong")
            lesson = f"入场即反向（最大浮盈仅 {mfe:+.2f}R），方向判断有问题"
        else:
            verdict = "bad_entry"
            mistakes.append("entry_wrong_side")
            lesson = f"方向错，亏 {realized:+.2f}R"

    # 止损过紧是独立问题：方向对也会被正常波动扫掉，必须单独标出来
    if r_pct is not None and 0 < r_pct < TIGHT_STOP_PCT:
        mistakes.append("stop_too_tight")
        if not lesson:
            lesson = f"初始止损仅 {r_pct:.2%}，正常波动即可扫损"

    return {
        "verdict": verdict,
        "capture_ratio": capture,
        "mistakes": mistakes,
        "lesson": lesson,
        "rule": rule,
        "side": side,
    }


def summarize_reviews(rows: list[dict]) -> dict:
    """按 verdict / mistakes 汇总，供绩效官和进化官一起用。"""
    counts: dict[str, int] = {}
    mistakes: dict[str, int] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        rv = (r.get("review") or {})
        if not isinstance(rv, dict):
            continue
        v = str(rv.get("verdict") or "")
        if v:
            counts[v] = counts.get(v, 0) + 1
        for m in rv.get("mistakes") or []:
            mistakes[str(m)] = mistakes.get(str(m), 0) + 1
    total = sum(counts.values())
    return {
        "n": total,
        "by_verdict": counts,
        "by_mistake": dict(sorted(mistakes.items(), key=lambda kv: -kv[1])),
        "good_ratio": round(counts.get("good", 0) / total, 4) if total else None,
    }
