"""按账户隔离的账本存储：权益基准、权益曲线、成交流水、平仓历史。

为什么要按账户分文件：`engine_state.json` 是单文件账本，`fills` /
`position_history` 混着币安模拟盘、OKX 模拟盘、主网三家数据。只加一个
`account_scope` 字段做过滤，历史数据一旦没有标签就会跟着「当前账户」漂移
（币安的旧记录在切到 OKX 后被算成 OKX 的盈亏）。分文件后每个账户各有一份，
物理隔离，切账户只换加载的文件，不存在「谁算进谁」的问题。

统一结构（所有账户同一 schema，便于跨账户汇总）：

    {
      "version": 1,
      "scope": "okx:demo",          # exchange:环境（demo / live）
      "exchange": "okx",
      "testnet": true,
      "account": {                  # 账户级权益基准
        "equity": 5000.0,
        "peak_equity": 5100.0,
        "day_start_equity": 5000.0,
        "day": "2026-11-06",
        "consecutive_losses": 0,
        "baseline_at": "2026-11-06 23:00:00"
      },
      "equity_history": [{"ts", "equity", "price"}],
      "fills": [...],               # 成交流水（开仓 + 平仓）
      "position_history": [...],    # 平仓历史
      "updated_at": "..."
    }

访问入口只有 `AccountStore`：引擎、Web、LLM 分析都走它，避免各处自己拼路径。
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

VERSION = 1
# 单账户保留上限：与原先全局账本一致，分账户后每个账户各有一份
MAX_FILLS = 500
MAX_HISTORY = 300
MAX_POSITIONS = 200


def scope_of(exchange: str, testnet: bool) -> str:
    """账户作用域：交易所 + 真假钱。demo 与 testnet 共用同一套模拟盘 Key。"""
    return f"{str(exchange or 'binance').lower()}:{'demo' if testnet else 'live'}"


def split_scope(scope: str) -> tuple[str, str]:
    """'okx:demo' -> ('okx', 'demo')；非法输入回退到币安模拟盘。"""
    text = str(scope or "").strip().lower()
    if ":" not in text:
        return "binance", "demo"
    ex, env = text.split(":", 1)
    return (ex or "binance"), ("live" if env == "live" else "demo")


def _num(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default  # 排除 NaN


def _pnl_rows(rows: list[dict]) -> dict:
    pnls = [_num(r.get("pnl")) for r in rows if isinstance(r, dict)]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    return {
        "realized": round(sum(pnls), 4),
        "trades": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "gross_win": round(sum(wins), 4),
        "gross_loss": round(abs(sum(losses)), 4),
    }


def _daily(rows: list[dict]) -> list[dict]:
    agg: dict[str, dict] = {}
    for h in rows:
        day = str(h.get("closed_at") or "")[:10]
        if len(day) != 10:
            continue
        row = agg.setdefault(day, {"date": day, "pnl": 0.0, "trades": 0, "wins": 0})
        pnl = _num(h.get("pnl"))
        row["pnl"] = round(row["pnl"] + pnl, 4)
        row["trades"] += 1
        if pnl > 0:
            row["wins"] += 1
    return [agg[k] for k in sorted(agg)]


def _empty(scope: str) -> dict:
    exchange, env = split_scope(scope)
    return {
        "version": VERSION,
        "scope": scope,
        "exchange": exchange,
        "testnet": env != "live",
        "account": {
            "equity": 0.0,
            "peak_equity": 0.0,
            "day_start_equity": 0.0,
            "day": "",
            "consecutive_losses": 0,
            "baseline_at": "",
        },
        "equity_history": [],
        "fills": [],
        "position_history": [],
        "updated_at": "",
    }


class AccountStore:
    """runs/accounts/ 下的一账户一文件账本。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- 基础读写 ------------------------------------------------------
    def path(self, scope: str) -> Path:
        name = str(scope or "binance:demo").replace(":", "-").replace("/", "_")
        return self.root / f"{name}.json"

    def _read(self, scope: str) -> dict:
        p = self.path(scope)
        if not p.exists():
            return _empty(scope)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _empty(scope)
        if not isinstance(data, dict):
            return _empty(scope)
        base = _empty(scope)
        base.update(data)
        base["scope"] = scope
        for key in ("fills", "position_history", "equity_history"):
            if not isinstance(base.get(key), list):
                base[key] = []
        if not isinstance(base.get("account"), dict):
            base["account"] = _empty(scope)["account"]
        return base

    def _write(self, scope: str, data: dict) -> None:
        data["scope"] = scope
        data["version"] = VERSION
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        p = self.path(scope)
        tmp = p.with_suffix(f".{time.time_ns()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(p)

    def scopes(self) -> list[str]:
        out = []
        for p in sorted(self.root.glob("*.json")):
            if p.name == "migrated.json":
                continue
            scope = p.stem.replace("-demo", ":demo").replace("-live", ":live")
            if ":" in scope:
                out.append(scope)
        return out

    def exists(self, scope: str) -> bool:
        return self.path(scope).exists()

    # -- 与引擎 state 同步 ---------------------------------------------
    def save_state(self, scope: str, state: dict) -> None:
        """把 state 里属于该账户的部分落盘。

        state 是「当前账户」的工作副本，账户文件是长期账本；每次 `_save()`
        顺手同步一次，保证进程被 kill 也不丢成交与权益曲线。
        """
        if not scope:
            return
        data = self._read(scope)
        data["account"] = {
            "equity": round(_num(state.get("equity")), 6),
            "peak_equity": round(_num(state.get("peak_equity")), 6),
            "day_start_equity": round(_num(state.get("day_start_equity")), 6),
            "day": str(state.get("day") or ""),
            "consecutive_losses": int(_num(state.get("consecutive_losses"))),
            "baseline_at": str(state.get("live_baseline_at") or ""),
            # 重置账户的时间锚点：交易所真实成交/资金流水是删不掉的，
            # 只能记下「重置时刻」，让 overlay 只显示这一刻之后的成交与盈亏。
            "reset_epoch": int(_num(state.get("account_reset_epoch"))),
            "reset_equity": round(_num(state.get("account_reset_equity")), 6),
        }
        data["equity_history"] = list(state.get("history") or [])[-MAX_HISTORY:]
        data["fills"] = list(state.get("fills") or [])[-MAX_FILLS:]
        # 旧记录可能还没标签：落盘时统一补上本账户，避免以后又漂移
        rows = []
        for row in list(state.get("position_history") or [])[-MAX_POSITIONS:]:
            if isinstance(row, dict):
                row = {**row, "account_scope": row.get("account_scope") or scope}
            rows.append(row)
        data["position_history"] = rows
        self._write(scope, data)

    def apply_to_state(self, scope: str, state: dict) -> bool:
        """把该账户的账本灌进 state（切账户时调用）。返回账户是否已存在。

        不存在时把三类流水清空：新账户不能继承上一个账户的历史。
        """
        existed = self.exists(scope)
        data = self._read(scope)
        acct = data.get("account") or {}
        state["fills"] = list(data.get("fills") or [])[-MAX_FILLS:]
        state["position_history"] = list(data.get("position_history") or [])[-MAX_POSITIONS:]
        state["history"] = list(data.get("equity_history") or [])[-MAX_HISTORY:]
        # 重置锚点跟着账户走：切回一个「重置过」的账户要恢复它的锚点，
        # 切到一个从没重置过的账户要清空（否则会用上一个账户的锚点去过滤）。
        state["account_reset_epoch"] = int(_num(acct.get("reset_epoch")))
        state["account_reset_equity"] = _num(acct.get("reset_equity"))
        if existed and _num(acct.get("equity")) > 0:
            state["equity"] = _num(acct.get("equity"))
            state["peak_equity"] = _num(acct.get("peak_equity")) or _num(acct.get("equity"))
            state["day_start_equity"] = _num(acct.get("day_start_equity")) or _num(acct.get("equity"))
            state["day"] = str(acct.get("day") or state.get("day") or "")
            state["consecutive_losses"] = int(_num(acct.get("consecutive_losses")))
            if acct.get("baseline_at"):
                state["live_baseline_at"] = str(acct["baseline_at"])
        return existed

    # -- 迁移 ----------------------------------------------------------
    def migrate(self, state: dict, state_path: str | Path | None = None) -> dict:
        """把旧单文件账本按账户拆开，只做一次。

        归因规则（按可信度从高到低）：
        1. 记录自带 `account_scope` → 按标签；
        2. 无标签且早于最近一次账户切换（`live_baseline_at`）→ 归「上一个账户」：
           记录 `mode` 与当前环境不同 → 同交易所的另一环境；否则另一交易所的同环境；
        3. 其余 → 归当前账户。
        迁移前会把 engine_state.json 复制一份到 runs/backup/，可人工回退。
        """
        marker = self.root / "migrated.json"
        if marker.exists():
            return {"migrated": False, "reason": "already"}

        cur_scope = str(state.get("account_scope") or "") or scope_of(
            state.get("exchange", "binance"), bool(state.get("testnet", True))
        )
        cutoff = _parse_ts(state.get("live_baseline_at"))
        prev_scope = _guess_prev_scope(cur_scope, state)

        buckets: dict[str, dict] = {}

        def bucket(scope: str) -> dict:
            if scope not in buckets:
                buckets[scope] = _empty(scope)
            return buckets[scope]

        def attribute(row: dict) -> str:
            tag = str(row.get("account_scope") or "")
            if tag:
                return tag
            if cutoff:
                ts = _parse_ts(row.get("closed_at") or row.get("ts"))
                if ts and ts < cutoff:
                    # 只能按「切换时间」判断归属。不能用 row["mode"]：那个字段
                    # 表示「走交易所 / 本地模拟」，模拟盘同样写 live，会把
                    # 币安模拟盘的历史误判成主网。
                    return prev_scope
            return cur_scope

        for key in ("fills", "position_history"):
            for row in state.get(key) or []:
                if not isinstance(row, dict):
                    continue
                scope = attribute(row)
                item = {**row, "account_scope": scope}
                if not item.get("attribution"):
                    item["attribution"] = (
                        "tagged" if row.get("account_scope") else
                        ("inferred_time" if cutoff and _parse_ts(row.get("closed_at") or row.get("ts")) else "current")
                    )
                bucket(scope)[key].append(item)

        # 当前账户的权益基准与权益曲线跟着当前账户走
        cur = bucket(cur_scope)
        cur["account"] = {
            "equity": round(_num(state.get("equity")), 6),
            "peak_equity": round(_num(state.get("peak_equity")), 6),
            "day_start_equity": round(_num(state.get("day_start_equity")), 6),
            "day": str(state.get("day") or ""),
            "consecutive_losses": int(_num(state.get("consecutive_losses"))),
            "baseline_at": str(state.get("live_baseline_at") or ""),
        }
        cur["equity_history"] = list(state.get("history") or [])[-MAX_HISTORY:]

        stats: dict[str, dict] = {}
        for scope, data in buckets.items():
            data["fills"] = data["fills"][-MAX_FILLS:]
            data["position_history"] = data["position_history"][-MAX_POSITIONS:]
            self._write(scope, data)
            stats[scope] = {
                "fills": len(data["fills"]),
                "position_history": len(data["position_history"]),
                "realized": round(sum(_num(r.get("pnl")) for r in data["position_history"]), 4),
            }

        # state 只保留当前账户，旧账户已各自落盘
        self.apply_to_state(cur_scope, state)

        backup = None
        src = Path(state_path) if state_path else None
        if src and src.exists():
            try:
                dst_dir = self.root.parent / "backup"
                dst_dir.mkdir(parents=True, exist_ok=True)
                backup = dst_dir / f"engine_state.pre-accounts-{time.strftime('%Y%m%d-%H%M%S')}.json"
                shutil.copy2(src, backup)
            except OSError:
                backup = None

        marker.write_text(
            json.dumps(
                {
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "from": cur_scope,
                    "cutoff": str(state.get("live_baseline_at") or ""),
                    "accounts": stats,
                    "backup": str(backup) if backup else "",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return {"migrated": True, "accounts": stats, "backup": str(backup) if backup else ""}

    # -- 重置（只清三类记录） -------------------------------------------
    def reset(self, scope: str) -> dict:
        """清空该账户的权益曲线、成交记录、持仓历史。

        只动这三类数据：账户是否存在、配置、真实持仓、其它账户的文件都不碰。
        权益基准（equity / peak / day_start）由调用方决定是否重置——引擎会把
        它对齐到当前交易所余额，让「重新开始统计」有一个真实的起点。
        """
        data = self._read(scope)
        counts = {
            "fills": len(data.get("fills") or []),
            "position_history": len(data.get("position_history") or []),
            "equity_history": len(data.get("equity_history") or []),
        }
        # 从未落过盘的账户不凭空建文件：重置一个不存在的账户应当是空操作
        if not self.path(scope).exists():
            return {"scope": scope, **counts}
        data["fills"] = []
        data["position_history"] = []
        data["equity_history"] = []
        self._write(scope, data)
        return {"scope": scope, **counts}

    # -- 汇总与查询（LLM 用） -------------------------------------------
    def summarize(self, scope: str) -> dict:
        data = self._read(scope)
        acct = data.get("account") or {}
        rows = [r for r in (data.get("position_history") or []) if isinstance(r, dict)]
        stat = _pnl_rows(rows)
        equity = _num(acct.get("equity"))
        peak = _num(acct.get("peak_equity")) or equity
        gross_loss = stat["gross_loss"]
        return {
            "scope": scope,
            "exchange": data.get("exchange"),
            "testnet": bool(data.get("testnet")),
            "equity": round(equity, 2),
            "peak_equity": round(peak, 2),
            "drawdown_pct": round((equity / peak - 1) * 100, 3) if peak > 0 else 0.0,
            "day_start_equity": round(_num(acct.get("day_start_equity")), 2),
            "consecutive_losses": int(_num(acct.get("consecutive_losses"))),
            **stat,
            "win_rate": round(stat["wins"] / stat["trades"] * 100, 1) if stat["trades"] else None,
            "profit_factor": round(stat["gross_win"] / gross_loss, 3) if gross_loss > 0 else None,
            "first_at": str(rows[0].get("closed_at") or "") if rows else "",
            "last_at": str(rows[-1].get("closed_at") or "") if rows else "",
            "daily": _daily(rows),
            "updated_at": str(data.get("updated_at") or ""),
        }

    def summarize_all(self) -> dict:
        """跨账户汇总：每个账户一份摘要 + 合计 + 按日合并。

        LLM 分析多账户时读这一个结构即可，不需要自己拼文件。
        """
        accounts = [self.summarize(s) for s in self.scopes()]
        by_day: dict[str, dict] = {}
        by_exchange: dict[str, dict] = {}
        total = {"realized": 0.0, "trades": 0, "wins": 0, "gross_win": 0.0, "gross_loss": 0.0}
        for a in accounts:
            total["realized"] = round(total["realized"] + a["realized"], 4)
            total["trades"] += a["trades"]
            total["wins"] += a["wins"]
            total["gross_win"] = round(total["gross_win"] + a["gross_win"], 4)
            total["gross_loss"] = round(total["gross_loss"] + a["gross_loss"], 4)
            ex = by_exchange.setdefault(
                a["exchange"] or "unknown", {"realized": 0.0, "trades": 0, "accounts": 0}
            )
            ex["realized"] = round(ex["realized"] + a["realized"], 4)
            ex["trades"] += a["trades"]
            ex["accounts"] += 1
            for d in a["daily"]:
                row = by_day.setdefault(d["date"], {"date": d["date"], "pnl": 0.0, "trades": 0, "wins": 0})
                row["pnl"] = round(row["pnl"] + d["pnl"], 4)
                row["trades"] += d["trades"]
                row["wins"] += d["wins"]
        return {
            "accounts": accounts,
            "combined": {
                **total,
                "win_rate": round(total["wins"] / total["trades"] * 100, 1) if total["trades"] else None,
                "profit_factor": round(total["gross_win"] / total["gross_loss"], 3)
                if total["gross_loss"] > 0 else None,
                "by_exchange": by_exchange,
                "by_day": [by_day[k] for k in sorted(by_day)],
            },
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def query(
        self,
        scope: str | None = None,
        kind: str = "position_history",
        since: str = "",
        until: str = "",
        symbol: str = "",
        limit: int = 200,
    ) -> list[dict]:
        """按账户 / 时间 / 品种查明细。scope 为空表示跨账户。"""
        key = "fills" if kind == "fills" else "position_history"
        scopes = [scope] if scope else self.scopes()
        stamp_key = "ts" if key == "fills" else "closed_at"
        rows: list[dict] = []
        for s in scopes:
            for row in self._read(s).get(key) or []:
                if not isinstance(row, dict):
                    continue
                ts = str(row.get(stamp_key) or "")
                if since and ts and ts < since:
                    continue
                if until and ts and ts > until:
                    continue
                if symbol and str(row.get("symbol") or "").upper() != symbol.upper():
                    continue
                rows.append({**row, "account_scope": row.get("account_scope") or s})
        rows.sort(key=lambda r: str(r.get(stamp_key) or ""))
        return rows[-max(1, int(limit)):]


def _parse_ts(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(text[: len(time.strftime(fmt))], fmt))
        except (ValueError, OverflowError, OSError):
            continue
    return 0.0


def _guess_prev_scope(cur_scope: str, state: dict) -> str:
    """推断「上一次用过的账户」。

    优先看存档里已有的账户标签：如果历史记录里出现过 `binance:demo` 而当前是
    `okx:demo`，那些没标签的旧记录大概率也属于 `binance:demo`。这比按「另一
    交易所同环境」硬猜准得多，也能正确处理同交易所换环境的场景。
    没有任何标签时，才退回到「另一交易所的同环境」。
    """
    counts: dict[str, int] = {}
    for key in ("fills", "position_history"):
        for row in state.get(key) or []:
            if not isinstance(row, dict):
                continue
            tag = str(row.get("account_scope") or "")
            if tag and tag != cur_scope:
                counts[tag] = counts.get(tag, 0) + 1
    if counts:
        return max(counts, key=lambda k: counts[k])
    exchange, env = split_scope(cur_scope)
    other = "okx" if exchange == "binance" else "binance"
    return f"{other}:{env}"
