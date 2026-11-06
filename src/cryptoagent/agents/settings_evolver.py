"""让 LLM 在**受限**范围内调整交易参数（自主进化）。

为什么不能直接让它写配置：
- 不可复现：同样数据两次给出不同值，事后无法归因「这次变差是因为哪一步」；
- 无闸门：模型可以一步把置信度门槛降到 0.05 来「多开仓」，短期看起来活跃、长期必亏；
- 不可回滚：没有变更记录就没有退路。

所以这里的做法是：**模型只出提案，代码做裁决**。三层硬约束全部由代码执行，
不依赖提示词：

1. **白名单**：只有下面 `BOUNDS` 里列出的键可被修改；布尔开关（是否自动交易、
   是否开某条规则）一律不可由模型改动。
2. **步长**：单键单轮最多变化 `MAX_STEP`（25%），整数键最多 ±1。
3. **总边界 + 交叉校验**：任何键都不得越出 `BOUNDS`；改动后仍须满足
   `单笔仓位上限 × 同时持仓 ≤ 保证金占用上限`，否则整份提案作废。
"""
from __future__ import annotations

import json
from pathlib import Path

from ._util import num_or_none

# 可被模型调整的键 → 硬边界
BOUNDS: dict[str, tuple[float, float]] = {
    "min_confidence": (0.20, 0.70),
    "max_loss_per_trade_pct": (0.002, 0.030),
    "max_position_pct": (0.03, 0.40),
    "max_concurrent_positions": (1.0, 6.0),
    "max_margin_usage_pct": (0.30, 0.95),
    "auto_top_k": (1.0, 4.0),
    "rotate_min_score": (0.05, 0.50),
}
# 整数型键（结果取整）。
INT_KEYS = {"max_concurrent_positions", "auto_top_k"}
# 归 position 组的键。当前为空：`llm_min_interval_sec` 只有一个写入方——
# 设置页 `/api/settings/position`，避免自动调参与人工设置互相覆盖。
POSITION_KEYS: set[str] = set()
# 单轮最多改几个键：改多了无法归因，也没法判断是哪一项起作用
MAX_KEYS = 3
# 单键单轮最大相对变化
MAX_STEP = 0.25

SYSTEM_EVOLVER = """你是加密永续合约交易系统的参数调优助手。

你的唯一任务是：根据系统近期的真实表现，提出**少量、小幅**的参数调整建议。
约束（违反任何一条，整份提案会被代码直接丢弃）：
- 只能修改给定的参数，不能新增、不能改开关；
- 每个参数单次变化不超过 25%，整数参数最多 ±1；
- 一次最多改 3 个参数；
- 不得越出给定的上下界；
- 调整必须由数据支撑：先说清楚是哪一项统计指标触发了这个判断。

判断原则：
- 观望比例过高（flat_ratio > 0.8）且置信度中位数明显低于门槛 → 可小幅降门槛，
  但必须同时说明「用胜率换频率」的代价；
- 平均 R 为正但笔数极少 → 优先考虑降门槛，而不是加仓；
- 平均 R 为负 → 优先收紧（提门槛 / 降单笔风险），不要为了「看起来活跃」放水；
- 峰值捕获率低（< 0.5）→ 说明利润被回吐，应该放宽移动止损而非降低入场门槛。
"""

EVOLVE_SCHEMA = """只输出 JSON：
{
  "diagnosis": "一句话：数据说明了什么问题",
  "changes": [{"key": "参数名", "to": 数值, "why": "一句话理由（引用具体指标）"}],
  "expected": "一句话：预期影响",
  "risk": "一句话：这次调整的主要风险"
}
没有调整必要时 changes 返回空数组，并在 diagnosis 里说明原因。"""


def build_context(cfg, perf: dict | None = None, funnel: dict | None = None) -> dict:
    """给模型的上下文：当前值 + 近期绩效 + 决策漏斗。"""
    cur = {k: float(getattr(cfg.risk, k, 0.0) or 0.0) for k in
           ("min_confidence", "max_loss_per_trade_pct", "max_position_pct",
            "max_concurrent_positions", "max_margin_usage_pct")}
    cur["auto_top_k"] = float(getattr(cfg.market, "auto_top_k", 1) or 1)
    cur["rotate_min_score"] = float(getattr(cfg.market, "rotate_min_score", 0.0) or 0.0)
    return {
        "current": cur,
        "bounds": {k: list(v) for k, v in BOUNDS.items()},
        "performance": perf or {},
        "funnel": funnel or {},
        "max_step_pct": MAX_STEP,
        "max_keys": MAX_KEYS,
    }


def funnel_stats(decisions_dir: str | Path, limit: int = 80) -> dict:
    """统计最近 N 份决策快照的「观望比例」与置信度分布。

    这是判断「要不要降门槛」最关键的证据：只看盈亏看不出「有多少机会被门槛挡掉了」。
    """
    d = Path(decisions_dir)
    if not d.is_dir():
        return {}
    files = sorted(d.glob("*.json"))[-max(1, int(limit)):]
    if not files:
        return {}
    flat = 0
    total = 0
    confs: list[float] = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        total += 1
        rd = data.get("risk_decision") or {}
        if str(rd.get("action") or "").lower() in ("flat", "", "hold"):
            flat += 1
        prop = data.get("proposal") or {}
        c = prop.get("confidence")
        if isinstance(c, (int, float)):
            confs.append(float(c))
    if not total:
        return {}
    confs.sort()
    med = confs[len(confs) // 2] if confs else None
    return {
        "decisions": total,
        "flat": flat,
        "flat_ratio": round(flat / total, 3),
        "median_confidence": round(med, 3) if med is not None else None,
    }


def perf_stats(history: list[dict], limit: int = 60) -> dict:
    """最近平仓的绩效摘要（优先用 realized_r，缺失时按 pnl 方向近似胜率）。"""
    rows = [r for r in (history or []) if isinstance(r, dict)][-max(1, int(limit)):]
    if not rows:
        return {"trades": 0}
    rs = [float(r["realized_r"]) for r in rows if isinstance(r.get("realized_r"), (int, float))]
    wins = [x for x in rs if x > 0]
    caps: list[float] = []
    for r in rows:
        rr = r.get("realized_r")
        mf = r.get("mfe_r")
        if isinstance(rr, (int, float)) and isinstance(mf, (int, float)) and mf >= 0.3:
            caps.append(float(rr) / float(mf))
    caps.sort()
    out = {
        "trades": len(rows),
        "with_r": len(rs),
        "win_rate": round(len(wins) / len(rs), 3) if rs else None,
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
    }
    if caps:
        out["capture_median"] = round(caps[len(caps) // 2], 3)
    return out


def sanitize(proposal: dict, cfg, current: dict | None = None) -> dict:
    """代码侧裁决：白名单 / 步长 / 总边界 / 交叉校验。

    返回 `{"changes": [...], "rejected": [...], "ok": bool}`。
    任一改动越界只**拒绝该项**并记录原因；若改动后破坏保证金不等式，则整份作废。
    """
    cur = current or build_context(cfg)["current"]
    raw = (proposal or {}).get("changes") or []
    if not isinstance(raw, list):
        raw = []
    changes: list[dict] = []
    rejected: list[dict] = []
    seen: set[str] = set()

    for item in raw[: MAX_KEYS * 2]:  # 先截断，避免模型刷一堆重复项
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        to = num_or_none(item.get("to"))
        why = str(item.get("why") or "").strip()[:160]
        if key not in BOUNDS:
            rejected.append({"key": key or "?", "reason": "不在可调白名单"})
            continue
        if key in seen:
            rejected.append({"key": key, "reason": "重复项"})
            continue
        if to is None:
            rejected.append({"key": key, "reason": "值不是数字"})
            continue
        if len(changes) >= MAX_KEYS:
            rejected.append({"key": key, "reason": f"一轮最多改 {MAX_KEYS} 项"})
            continue
        lo, hi = BOUNDS[key]
        if not (lo - 1e-12 <= to <= hi + 1e-12):
            rejected.append({"key": key, "reason": f"越界，需在 {lo} ~ {hi}"})
            continue
        base = float(cur.get(key, 0.0) or 0.0)
        if key in INT_KEYS:
            base_i = int(round(base))
            to_i = int(round(to))
            if abs(to_i - base_i) > 1:
                rejected.append({"key": key, "reason": "整数参数单轮最多 ±1"})
                continue
            if to_i == base_i:
                continue
            changes.append({"key": key, "from": base_i, "to": to_i, "why": why})
        else:
            if base <= 0:
                rejected.append({"key": key, "reason": "当前值无效，无法按比例判断"})
                continue
            if abs(to - base) > abs(base) * MAX_STEP + 1e-9:
                rejected.append({"key": key, "reason": f"单轮变化超过 {int(MAX_STEP * 100)}%"})
                continue
            if abs(to - base) < 1e-9:
                continue
            changes.append({"key": key, "from": round(base, 6), "to": round(to, 6), "why": why})
        seen.add(key)

    # 交叉校验：把改动套到当前值上看保证金不等式是否仍成立
    merged = dict(cur)
    for c in changes:
        merged[c["key"]] = float(c["to"])
    pos = float(merged.get("max_position_pct") or 0.0)
    slots = max(1, int(round(float(merged.get("max_concurrent_positions") or 1))))
    margin = float(merged.get("max_margin_usage_pct") or 0.0)
    if pos * slots > margin + 1e-9:
        return {
            "changes": [],
            "rejected": rejected + [{
                "key": "*",
                "reason": f"改动后 {pos:.0%}×{slots} = {pos * slots:.0%} 超过保证金上限 {margin:.0%}，整份作废",
            }],
            "ok": False,
        }
    return {"changes": changes, "rejected": rejected, "ok": bool(changes)}


def propose(llm, ctx: dict) -> dict:
    """调用模型产出提案；失败返回空提案（调用方按「无调整」处理）。"""
    user = (
        "当前参数：\n" + json.dumps(ctx.get("current"), ensure_ascii=False, indent=1)
        + "\n\n近期平仓绩效：\n" + json.dumps(ctx.get("performance"), ensure_ascii=False, indent=1)
        + "\n\n决策漏斗（最近快照）：\n" + json.dumps(ctx.get("funnel"), ensure_ascii=False, indent=1)
        + "\n\n可调参数与上下界：\n" + json.dumps(ctx.get("bounds"), ensure_ascii=False, indent=1)
        + f"\n\n单轮最多改 {ctx.get('max_keys')} 项，单键最多变化 "
          f"{int(float(ctx.get('max_step_pct') or 0) * 100)}%。"
    )
    mock = {"diagnosis": "模型不可用，本轮不调整", "changes": [], "expected": "", "risk": ""}
    try:
        data, _resp = llm.json(SYSTEM_EVOLVER, user, EVOLVE_SCHEMA, mock)
        if not isinstance(data, dict):
            return mock
        return data
    except Exception:  # noqa: BLE001 - 调参是旁路，失败就等于不调整
        return mock
