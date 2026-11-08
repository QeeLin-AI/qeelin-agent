"""进化治理层：所有自动改动的统一账本、验证闸门与效果归因。

为什么需要它：参数、因子、规则各自都在「自己决定要不要改」，没有共同裁判。
结果是改了之后变差也无人知晓，更无法回到上一次真正更好的版本。

三层职责（刻意做得简单、可审计）：
1. **账本 Ledger**：append-only JSONL。回滚是「记一条新记录」，绝不删除历史——
   删掉历史等于毁掉「为什么变成现在这样」的唯一证据。
2. **闸门 Gate**：没有验证证据一律拒绝（fail-closed），样本不足也拒绝。
   宁可不动，也不要在没证据的情况下改生产参数。
3. **归因 Attribution**：应用之后只看新产生的平仓，与基线对比，给出这次改动
   到底改善还是变差；变差超过阈值标 `degraded`，供自动回滚。

设计边界：本模块**只记账、只裁决，不直接改配置**。真正的写入仍走
server 现有的 setter（它们带校验与失败回滚），本模块只在其前后记录与判定。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .agents.scorekeeper import stats_of

# 应用前所需的最小样本：少于这个数，任何「改完更好」都只是噪声
MIN_SAMPLE_FOR_APPLY = 20
# 应用后归因所需的新增平仓笔数
MIN_TRADES_FOR_ATTRIBUTION = 20
# 归因判定阈值（单位 R）
DEGRADE_EXPECTANCY_R = 0.10
IMPROVE_EXPECTANCY_R = 0.05

KINDS = ("config", "rule", "factor")
STATUSES = ("proposed", "applied", "rejected", "degraded", "confirmed", "rolled_back")


@dataclass
class Change:
    id: str
    kind: str                  # config | rule | factor
    target: str                # 例："risk.min_confidence" / "exit_rule.trail_atr_mult"
    before: Any
    after: Any
    source: str                # settings_evolver / position_calibration / manual
    rationale: str
    created_at: str
    status: str = "proposed"
    evidence: dict = field(default_factory=dict)
    # 应用当时的基线：之后只用「新增的平仓」做归因，避免被更早的历史稀释
    kpi_at_apply: dict = field(default_factory=dict)
    n_at_apply: int = 0
    applied_at: str = ""
    closed_at: str = ""
    note: str = ""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def new_id(kind: str) -> str:
    return f"{time.strftime('%Y%m%d%H%M%S')}-{kind}-{uuid.uuid4().hex[:6]}"


class ChangeLedger:
    """append-only 变更账本（JSONL）。线程安全，写入用临时文件替换。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows: list[dict] = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("id"):
                    rows.append(rec)
        except OSError:
            return []
        return rows

    def _write(self, rows: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{time.time_ns()}.tmp")
        tmp.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def append(self, change: Change) -> Change:
        with self._lock:
            rows = self._read()
            rows.append(asdict(change))
            self._write(rows)
        return change

    def all(self) -> list[dict]:
        with self._lock:
            return self._read()

    def get(self, change_id: str) -> dict | None:
        for r in self.all():
            if r.get("id") == change_id:
                return r
        return None

    def update(self, change_id: str, **fields) -> dict | None:
        with self._lock:
            rows = self._read()
            hit = None
            for r in rows:
                if r.get("id") == change_id:
                    r.update({k: v for k, v in fields.items() if v is not None})
                    hit = r
                    break
            if hit is not None:
                self._write(rows)
            return hit


def validate_change(evidence: dict | None, kpi: dict | None,
                    min_delta_r: float = 0.0) -> dict:
    """应用前的闸门。没有证据或样本不足，一律不通过（fail-closed）。

    `evidence` 约定字段（回放/模拟结果）：
      kind: "replay" | "shadow" | "backtest"
      delta_expectancy_r: float   候选相对基线的期望值变化
      delta_max_dd_r: float       回撤变化（更负=更差）
      n: int                      验证样本数
    """
    e = evidence or {}
    checks: dict[str, Any] = {}
    n = int(((kpi or {}).get("overall") or {}).get("n_with_r") or 0)

    checks["sample_ok"] = n >= MIN_SAMPLE_FOR_APPLY
    checks["evidence_kind"] = str(e.get("kind") or "")
    checks["has_evidence"] = bool(checks["evidence_kind"])
    passed = checks["sample_ok"] and checks["has_evidence"]

    if passed:
        delta_e = float(e.get("delta_expectancy_r") or 0.0)
        delta_dd = float(e.get("delta_max_dd_r") or 0.0)
        checks["improved"] = delta_e > float(min_delta_r)
        # 回撤允许 0.5R 噪声；更负说明风险变大，不许上线
        checks["risk_not_worse"] = delta_dd >= -0.5
        checks["delta_expectancy_r"] = round(delta_e, 4)
        checks["delta_max_dd_r"] = round(delta_dd, 4)
        passed = checks["improved"] and checks["risk_not_worse"]

    return {"passed": bool(passed), "checks": checks, "min_delta_r": float(min_delta_r)}


def attribute_change(change: dict, rows: list[dict], window: int = 50) -> dict:
    """应用后归因：只比「应用之后新产生的平仓」与「应用之前的基线窗口」。

    不能用整体 KPI 比，那会被更早的历史稀释，看不出这次改动的真实效果。
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    n0 = max(0, int(change.get("n_at_apply") or 0))
    before = rows[max(0, n0 - window):n0]
    after = rows[n0:n0 + window]
    need = MIN_TRADES_FOR_ATTRIBUTION

    if len(after) < need:
        return {
            "ready": False,
            "have": len(after),
            "need": need,
            "before_n": len(before),
            "status": change.get("status"),
        }

    kb, ka = stats_of(before), stats_of(after)
    de = (ka.get("expectancy_r") or 0.0) - (kb.get("expectancy_r") or 0.0)
    d_dd = (ka.get("max_dd_r") or 0.0) - (kb.get("max_dd_r") or 0.0)
    if de >= IMPROVE_EXPECTANCY_R and d_dd >= -0.5:
        verdict = "improved"
    elif de <= -DEGRADE_EXPECTANCY_R:
        verdict = "degraded"
    else:
        verdict = "neutral"
    return {
        "ready": True,
        "verdict": verdict,
        "before_n": kb.get("n_with_r", 0),
        "after_n": ka.get("n_with_r", 0),
        "expectancy_r_before": kb.get("expectancy_r"),
        "expectancy_r_after": ka.get("expectancy_r"),
        "delta_expectancy_r": round(de, 4),
        "delta_max_dd_r": round(d_dd, 4),
        "win_rate_before": kb.get("win_rate"),
        "win_rate_after": ka.get("win_rate"),
        "status": change.get("status"),
    }


class Governor:
    """账本 + 闸门 + 归因的统一入口。"""

    def __init__(self, runs_dir: str | Path) -> None:
        self.ledger = ChangeLedger(Path(runs_dir) / "evolution" / "changes.jsonl")

    # -- 记账 ----------------------------------------------------------
    def propose(self, kind: str, target: str, before: Any, after: Any,
                source: str, rationale: str, evidence: dict | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError(f"未知改动类型 {kind}，允许：{KINDS}")
        ch = Change(
            id=new_id(kind), kind=kind, target=str(target),
            before=before, after=after, source=str(source),
            rationale=str(rationale)[:400], created_at=_now(),
            status="proposed", evidence=dict(evidence or {}),
        )
        return asdict(self.ledger.append(ch))

    def record_applied(self, kind: str, target: str, before: Any, after: Any,
                       source: str, rationale: str, kpi: dict | None,
                       evidence: dict | None = None, note: str = "") -> dict:
        """已经生效的改动（例如现有 setter 直接应用后）补记一条账。

        这类记录不强求 evidence——它记的是「事实」而不是「提案」，
        但 `n_at_apply` 必须准确，否则后续归因会算错区间。
        """
        if kind not in KINDS:
            raise ValueError(f"未知改动类型 {kind}，允许：{KINDS}")
        n = int(((kpi or {}).get("overall") or {}).get("n_with_r") or 0)
        ch = Change(
            id=new_id(kind), kind=kind, target=str(target),
            before=before, after=after, source=str(source),
            rationale=str(rationale)[:400], created_at=_now(),
            status="applied", evidence=dict(evidence or {}),
            kpi_at_apply=dict((kpi or {}).get("overall") or {}),
            n_at_apply=n, applied_at=_now(), note=str(note)[:200],
        )
        return asdict(self.ledger.append(ch))

    def reject(self, change_id: str, reason: str) -> dict | None:
        return self.ledger.update(
            change_id, status="rejected", closed_at=_now(), note=str(reason)[:200])

    def rollback(self, change_id: str) -> dict | None:
        """只标记状态；真正的配置写回由调用方用 `before` 走 setter 完成。"""
        rec = self.ledger.get(change_id)
        if not rec:
            return None
        return self.ledger.update(change_id, status="rolled_back", closed_at=_now())

    # -- 查询 ----------------------------------------------------------
    def changes_with_attribution(self, rows: list[dict], limit: int = 50) -> dict:
        recs = self.ledger.all()[-(max(1, int(limit))):]
        recs = list(reversed(recs))
        out = []
        for r in recs:
            att = attribute_change(r, rows)
            # 归因结论回写账本：degraded 是「该回滚」的信号，必须持久化
            st = r.get("status")
            if att.get("ready") and st == "applied":
                if att.get("verdict") == "degraded":
                    self.ledger.update(r["id"], status="degraded", closed_at=_now(),
                                       note=f"应用后期望值下降 {att.get('delta_expectancy_r')}R")
                    r["status"] = "degraded"
                elif att.get("verdict") == "improved":
                    self.ledger.update(r["id"], status="confirmed", closed_at=_now(),
                                       note=f"应用后期望值改善 {att.get('delta_expectancy_r')}R")
                    r["status"] = "confirmed"
            out.append({**r, "attribution": att})
        return {"items": out, "summary": self.summary()}

    def summary(self) -> dict:
        recs = self.ledger.all()
        counts: dict[str, int] = {}
        for r in recs:
            counts[str(r.get("status") or "")] = counts.get(str(r.get("status") or ""), 0) + 1
        applied = sum(v for k, v in counts.items() if k in ("applied", "confirmed", "degraded"))
        confirmed = counts.get("confirmed", 0)
        return {
            "total": len(recs),
            "by_status": counts,
            "applied": applied,
            # 进化采纳率：被证实改善的比例（用户标准里的「学习能力」指标）
            "confirm_rate": round(confirmed / applied, 4) if applied else None,
        }


def get_governor(runs_dir: str | Path) -> Governor:
    """按 runs_dir 复用同一个 Governor（账本文件句柄很轻，但避免重复建锁）。"""
    key = str(Path(runs_dir).resolve())
    cache = getattr(get_governor, "_cache", None)
    if cache is None:
        cache = get_governor._cache = {}
    if key not in cache:
        cache[key] = Governor(runs_dir)
    return cache[key]
