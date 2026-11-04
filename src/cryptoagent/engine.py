"""交易引擎：把「决策」变成「持仓」，并持久化状态。

职责边界：
- 引擎只负责**执行**决策（下单/平仓/止损），不做任何分析。
- 模拟盘（paper）与实盘（live）共用同一套状态机，差别只在撮合来源。
- 实盘必须显式 arm，且 arm 状态**不落盘**——服务重启后自动回到 paper。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from .agents.calibration import record_accuracy, summary as calibration_summary, weights_from_accuracy
from .agents.pipeline import Decision, TradingPipeline, render_markdown, round_price
from .agents.position_manager import explain_management, manage_position
from .config import AppConfig
from .data import KL_INTERVALS, fetch_klines, kl_disk_ttl, load_market, set_kl_cache_dir
from .derivatives import enrich_derivatives
from .execution import BinanceFuturesClient, OKXClient, build_client, ExchangeError, read_api_keys
from .notify import get_notifier
from .risk import AccountState


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _parse_ts_ms(value: object) -> int:
    """把本地时间字符串（"%Y-%m-%d %H:%M:%S"）转成毫秒时间戳；解析不了返回 0。"""
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S")) * 1000)
    except (ValueError, OverflowError, OSError):
        return 0


class TradingEngine:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.lock = threading.RLock()
        self.state_path = Path(cfg.runs_dir) / "engine_state.json"
        self.state = self._default_state()
        self._load()
        # 打开/重启后默认不自动运行交易循环：线程只在用户点「启动」时由 start_auto 创建。
        # 先把「上次是否是运行中」记下来，供 CRYPTOAGENT_AUTO_RESUME=on 时在模拟盘下恢复。
        _prev_auto = dict(self.state.get("auto") or {})
        self.auto_was_running = bool(_prev_auto.get("running"))
        self.state.setdefault("auto", {})["running"] = False

        self.df: pd.DataFrame | None = None
        self.data_source = ""
        self.data_loading = False      # 启动后 K 线在后台下载时为 True，前端据此显示「加载中」
        self.last_decision: Decision | None = None
        self.last_error = ""
        # 恢复上次选择的品种：set_symbol 只写 state 不写 config，重启要还原，
        # 否则会悄悄退回 config/default.json 里的默认品种（BTCUSDT）。
        if self.state.get("symbol"):
            self.cfg.market.symbol = self.state["symbol"]
        # 重启后仍然可以从 state 里恢复「上次决策的订单计划」，否则执行按钮会失灵
        self.pending_order: dict | None = (self.state.get("last_decision") or {}).get("order")
        self.armed = False                 # 不持久化
        self.live_client: BinanceFuturesClient | OKXClient | None = None
        self.state.setdefault("exchange", "binance")
        self.live_price_provider = None    # 由 Web 层注入：返回最新成交价

        self._auto_thread: threading.Thread | None = None
        self._auto_stop = threading.Event()
        # 短周期 K 线预热节流：预热只是为了把盘缓存焐热，失败不影响任何决策
        self._kl_prewarm_at = 0.0
        self._kl_prewarm_busy = False
        self.auto_interval = int(self.state.get("auto", {}).get("interval_sec", 300))
        self.auto_execute = bool(self.state.get("auto", {}).get("execute", False))
        self._cycle_lock = threading.Lock()
        # 机会信号排行榜：最近一次全品种扫描结果（供页面展示，扫描是重操作，避免每次轮询重扫）
        self.opportunities: list[dict] = []
        self.scan_state: dict = {
            "scanning": False,
            "started_at": None,
            "last_at": None,
            "last_ts": 0.0,
            "error": "",
        }
        self._rotate_cycle_count = 0
        # 风控拦截通知去重：同品种同原因在冷却期内只报一次，避免每轮刷屏。
        # 落盘保存，服务重启后不会立刻重复推送同样内容。
        self._reject_alert_at: dict[str, float] = dict(
            self.state.setdefault("alert_dedupe", {}) or {}
        )
        self._stop_adjust_at = float(self.state.get("last_stop_adjust_at", 0.0) or 0.0)
        self.last_management: dict = {}
        # 持仓管理守护线程：不依赖「启动自动循环」，手动/半自动模式下开仓后
        # 也能做移动止损、时间止损与趋势反转退出。
        self._guard_lock = threading.Lock()
        self._guard_stop = threading.Event()
        self._guard_thread: threading.Thread | None = None
        self._guard_interval = 30
        self._composite_cache: dict = {}
        self.start_position_guard()

    # ------------------------------------------------------------------
    def _default_state(self) -> dict:
        eq = self.cfg.backtest.initial_equity
        return {
            "mode": "paper",
            "testnet": True,
            "symbol": self.cfg.market.symbol,
            "equity": eq,
            "peak_equity": eq,
            "day_start_equity": eq,
            "day": _today(),
            "position": None,
            "fills": [],
            "position_history": [],
            "consecutive_losses": 0,
            "last_decision": None,
            "auto": {"running": False, "interval_sec": 300, "execute": False, "last_run": ""},
            "halt": False,
            "history": [],
        }

    def _load(self) -> None:
        if self.state_path.exists():
            try:
                saved = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.state.update(saved)
            except Exception:  # noqa: BLE001 - 状态文件损坏时重建，不阻塞启动
                pass

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(self.state_path)

    # ------------------------------------------------------------------
    def refresh_data(self, force: bool = False) -> tuple[pd.DataFrame, str]:
        # 行情源跟着「当前选的交易所」走：选 OKX 就用 OKX 永续的 K 线
        set_kl_cache_dir(getattr(self.cfg.market, "cache_dir", "data/cache"))
        df, src = load_market(
            self.cfg.market, force_download=force, prefer=self.state.get("exchange", "binance")
        )
        # C 档衍生品因子的数据：基差 / OI / taker 多空比 / 账户多空比。
        # 拿不到就原样返回（signal 层会自动跳过依赖这些列的因子），绝不阻断行情加载。
        mkt = self.cfg.market
        df = enrich_derivatives(
            df, mkt.symbol, mkt.interval,
            enabled=bool(getattr(mkt, "derivatives_enabled", True)),
            ttl=float(getattr(mkt, "derivatives_ttl_sec", 300) or 300),
        )
        with self.lock:
            self.df = df
            self.data_source = src
        self._prewarm_klines()
        return df, src

    def _prewarm_klines(self) -> None:
        """后台预热短周期 K 线盘缓存（方案 A）。

        决策里的中频突破信号、页面切 15m 图，走的都是 fetch_klines。没有预热时
        它们只能现场打交易所：慢几百毫秒，而且哪次拉失败就静默退化成「无信号」。
        这里在主行情刷新后顺手把 short_interval（以及额外配置的 15m）拉到盘里，
        之后两边都读本地。

        三条纪律：
        - 只在主行情刷新时触发，机会扫描（6 品种 × 4 请求）不预热；
        - 按盘缓存有效期节流，不跟页面轮询抢带宽；
        - 任何异常静默，预热失败不能影响决策。
        """
        mkt = self.cfg.market
        if str(getattr(mkt, "source", "synthetic")) != "binance":
            return
        if not getattr(mkt, "klines_cache", True) or not getattr(mkt, "klines_prewarm", True):
            return
        # short_interval + 额外周期（默认 15m）：中频信号和图表各要一个粒度，
        # 两个周期各一次请求，比让它们各自在请求线程里现场拉省得多。
        wanted = [str(getattr(mkt, "short_interval", "5m") or "5m")]
        wanted += [str(x or "") for x in (getattr(mkt, "klines_prewarm_intervals", ["15m"]) or [])]
        intervals: list[str] = []
        for iv in wanted:
            iv = iv.strip()
            if iv in KL_INTERVALS and iv not in intervals:
                intervals.append(iv)
        if not intervals:
            return
        every = kl_disk_ttl(intervals[0])
        now = time.time()
        with self.lock:
            if self._kl_prewarm_busy or (now - self._kl_prewarm_at) < every:
                return
            self._kl_prewarm_busy = True
            self._kl_prewarm_at = now

        symbol = str(mkt.symbol)
        bars = max(20, min(int(getattr(mkt, "short_bars", 200) or 200), 500))
        exchange = self.state.get("exchange", "binance")

        def _worker() -> None:
            try:
                for iv in intervals:
                    try:
                        # force 不设为 True：盘里还新鲜就直接跳过，不跟页面/决策抢同一份数据。
                        # 盘过期时它自己会走网络并覆盖，这就是「预热」的本意。
                        fetch_klines(symbol, iv, bars, prefer=exchange)
                    except Exception:  # noqa: BLE001 - 单个周期失败不影响其他周期
                        continue
            finally:
                self._kl_prewarm_busy = False   # 任何情况都要解锁，否则预热永久停摆

        threading.Thread(target=_worker, daemon=True, name="kl-prewarm").start()

    def reload_for_exchange(self) -> None:
        """切换交易所后把行情源也切过去：后台重拉，不卡住 HTTP 请求。"""
        if getattr(self, "_reloading", False):
            return
        self._reloading = True
        exchange = self.state.get("exchange", "binance")

        def _worker() -> None:
            try:
                self.data_loading = True
                from .data import _DEAD_UNTIL, _KL_CACHE

                _KL_CACHE.clear()          # 短周期图也别再吐旧交易所的缓存
                _DEAD_UNTIL.clear()        # 手动切换要重新探测，别被 10 分钟熔断挡住
                self.refresh_data(force=True)
                print(f"[data] 行情源已切到 {exchange}：{self.data_source}")
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"切换行情源失败：{type(exc).__name__}: {exc}"
                print(f"[data] {self.last_error}")
            finally:
                self.data_loading = False
                self._reloading = False

        threading.Thread(target=_worker, daemon=True, name="reload-market").start()

    def start_data_load(self) -> None:
        """后台线程加载 K 线，不阻塞 Web 服务启动（窗口秒开）。

        refresh_data 在 fapi 被墙时会依次 fallback 到 vision 镜像 / gate 永续，
        首次全量下载可能耗时 30~40 秒。把这一步挪到后台，HTTP 服务立即就绪，
        前端展示「K 线加载中」，加载完成后自动刷新。
        """
        if self.data_loading or self.df is not None:
            return
        self.data_loading = True
        self.last_error = ""

        def _worker() -> None:
            try:
                self.refresh_data()
            except Exception as exc:  # noqa: BLE001 - 后台失败只记错误，不崩进程
                self.last_error = f"行情加载失败：{type(exc).__name__} {exc}"
            finally:
                self.data_loading = False

        threading.Thread(target=_worker, daemon=True).start()

    def price(self) -> float:
        if self.df is None:
            self.refresh_data()
        assert self.df is not None
        return float(self.df["close"].iloc[-1])

    # ------------------------------------------------------------------
    # 通知（A2）：多通道、fail-safe，绝不因通知失败中断交易
    # ------------------------------------------------------------------
    def _notify(self, level: str, title: str, message: str, meta: dict | None = None) -> None:
        try:
            if not hasattr(self, "_notifier"):
                self._notifier = get_notifier(self.cfg.notify)
            self._notifier.send(level, title, message, meta)
        except Exception:  # noqa: BLE001 - 通知是旁路，任何异常都不应影响主流程
            pass

    def _positions_for_risk(self, price: float) -> list[dict]:
        """把当前持仓整理成风险引擎需要的格式（A3 组合层风控使用）。"""
        pos = self.state.get("position")
        if not pos:
            return []
        entry = float(pos.get("entry") or price)
        qty = float(pos.get("qty") or 0.0)
        lev = float(pos.get("leverage") or 1.0)
        return [{
            "symbol": self.cfg.market.symbol,
            "side": pos.get("side"),
            "notional": qty * entry * lev,
        }]

    def _roll_day(self) -> None:
        if self.state["day"] != _today():
            self.state["day"] = _today()
            self.state["day_start_equity"] = self.state["equity"]
            self.state["position"] = self.state.get("position")

    def account_state(self, price: float | None = None) -> AccountState:
        price = price if price is not None else self.price()
        live = self._live_equity()
        if live is not None:
            # 只有「内部账本与交易所余额量级不符」时才对齐（如模拟期 10000 vs 实盘 4997，
            # 或切换了交易所/账户）。
            # 注意：不能拿 peak 与 live 比 —— 那样真实回撤超过 33%（peak/live > 1.5）就会
            # 把峰值重置成当前权益，回撤瞬间归零，max_drawdown_pct 熔断永远触发不了。
            ledger = float(self.state.get("equity") or 0.0)
            if ledger <= 0 or abs(ledger - live) / live > 0.5:
                self._rebase_ledger(live)
            equity = live
        else:
            equity = self._equity(price)
        return AccountState(
            equity=equity,
            peak_equity=max(self.state["peak_equity"], equity),
            day_start_equity=self.state["day_start_equity"] or equity,
            position_notional=self._position_notional(price),
            consecutive_losses=self.state["consecutive_losses"],
        )

    def _position_pnl(self, price: float) -> float:
        pos = self.state.get("position")
        if not pos:
            return 0.0
        direction = 1.0 if pos["side"] == "LONG" else -1.0
        return (price - pos["entry"]) * pos["qty"] * direction

    def _position_notional(self, price: float) -> float:
        pos = self.state.get("position")
        return pos["qty"] * price if pos else 0.0

    def _equity(self, price: float) -> float:
        return float(self.state["equity"] + self._position_pnl(price))

    # ---- 实盘权益 / 成交回读 ------------------------------------------
    def _live_equity(self, ttl: float = 20.0) -> float | None:
        """实盘模式下取交易所真实权益（钱包余额 + 未实现盈亏），带短缓存。

        内部 state["equity"] 从 backtest.initial_equity 起算，和交易所真实余额不是一个
        口径（实例：本机 10000 vs 币安 demo 4997）。风控若拿内部值算仓位，实盘会按幻影
        本金放大仓位，回撤/日亏限额也会失真。所以实盘一律以交易所为准。
        """
        if self.state.get("mode") != "live" or not self.armed or self.live_client is None:
            return None
        now = time.time()
        cache = getattr(self, "_live_eq_cache", None)
        if cache and (now - cache[1]) < ttl:
            return cache[0]
        try:
            acct = self.live_client.account()
        except Exception:  # noqa: BLE001 - 拿不到就退回本地账本，绝不阻塞决策
            return None
        equity = float(acct.get("balance") or 0.0) + float(acct.get("unrealized_pnl") or 0.0)
        if equity <= 0:
            return None
        self._live_eq_cache = (equity, now)
        return equity

    def _rebase_ledger(self, equity: float) -> None:
        """把内部账本对齐到交易所真实权益（发现量级不符时调用）。"""
        with self.lock:
            self.state["equity"] = float(equity)
            self.state["peak_equity"] = float(equity)
            self.state["day_start_equity"] = float(equity)
            self.state["day"] = _today()
            self.state["live_baseline_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._live_eq_cache = (float(equity), time.time())
            self._save()

    def _reset_account_scope(self, acct: dict | None = None) -> None:
        """切换交易所/账户后重置「账户级」状态。

        peak_equity / day_start_equity / consecutive_losses 都是绑定在某个账户上的：
        换了账户还沿用旧值，会得到「旧账户峰值 vs 新账户余额」的伪回撤，以及旧账户
        留下的伪连亏（会一路写进风控官提示词并压低仓位）。
        position_history 属于审计流水，保留不动。
        """
        equity: float | None = None
        if acct:
            try:
                equity = float(acct.get("balance") or 0.0) + float(acct.get("unrealized_pnl") or 0.0)
            except (TypeError, ValueError):
                equity = None
        with self.lock:
            if equity and equity > 0:
                self.state["equity"] = float(equity)
                self.state["peak_equity"] = float(equity)
                self.state["day_start_equity"] = float(equity)
                self.state["day"] = _today()
            self.state["consecutive_losses"] = 0
            self.state["live_baseline_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._live_eq_cache = None
            self._save()

    def _live_fill(self, symbol: str, order_id: str) -> dict | None:
        """按 orderId 回读交易所成交，拿真实成交均价 / 手续费 / 已实现盈亏。

        市价单返回的 avgPrice 常为 0，手续费也不在订单响应里；不回读就只能把 fee 记成
        0，已实现盈亏会偏乐观，内部权益与交易所余额越跑越偏。
        """
        if not order_id or self.live_client is None:
            return None
        try:
            rows = self.live_client.trades(symbol, limit=50)
        except Exception:  # noqa: BLE001
            return None
        hits = [r for r in rows if str(r.get("order_id")) == str(order_id)]
        if not hits:
            return None
        qty = sum(float(r.get("qty") or 0.0) for r in hits)
        notional = sum(float(r.get("qty") or 0.0) * float(r.get("price") or 0.0) for r in hits)
        return {
            "qty": qty,
            "price": (notional / qty) if qty > 0 else 0.0,
            "fee": sum(float(r.get("fee") or 0.0) for r in hits),
            "pnl": sum(float(r.get("pnl") or 0.0) for r in hits),
        }

    # ------------------------------------------------------------------
    # 模拟撮合
    # ------------------------------------------------------------------
    def _paper_open(self, side: str, qty: float, price: float, lev: float,
                    stop: float, tp: float, reason: str, plan_price: float | None = None,
                    stances: list[float] | None = None) -> dict:
        slip = self.cfg.backtest.costs.slippage_bps / 10_000.0
        fill = price * (1 + slip) if side == "LONG" else price * (1 - slip)
        fee = qty * fill * self.cfg.backtest.costs.fee_bps / 10_000.0
        self.state["equity"] -= fee
        self.state["position"] = {
            "side": side,
            "qty": qty,
            "entry": fill,
            "leverage": lev,
            "stop": stop,
            "tp": tp,
            "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            # 持仓管理员用：R 必须在开仓时固化（止损后来会被移动），
            # epoch 用来算持有了多少根 K 线，避免本地时区歧义。
            "opened_epoch": time.time(),
            "initial_r_pct": abs(fill - stop) / fill if fill > 0 else 0.0,
            "symbol": self.cfg.market.symbol,
            "analyst_stances": list(stances or []),
            "mode": "paper",
        }
        # A4：记录计划价与滑点，便于事后审计「计划 vs 成交」偏差
        slip_bps = None
        if plan_price:
            slip_bps = round((fill - plan_price) / plan_price * 10_000.0, 2)
        fill_rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "action": "OPEN",
            "side": side,
            "symbol": self.cfg.market.symbol,
            "qty": round(qty, 6),
            "price": round_price(fill),
            "plan_price": round_price(plan_price) if plan_price else None,
            "slippage_bps": slip_bps,
            "fee": round(fee, 4),
            "reason": reason,
        }
        self.state["fills"].append(fill_rec)
        self.state["fills"] = self.state["fills"][-500:]
        return fill_rec

    def _record_close(self, pos: dict, exit_price: float, pnl: float, reason: str, mode: str) -> None:
        """把一次平仓写进持仓历史：持仓历史表 + 每日盈亏统计都靠它。"""
        # 分析师权重校准：用「市场实际方向」而非这笔仓的盈亏评判立场
        # （开仓方向由交易员定，不该算到分析师头上）。
        stances = pos.get("analyst_stances") or []
        entry = float(pos.get("entry") or 0.0)
        if stances and entry > 0 and exit_price > 0 and abs(float(exit_price) - entry) > 1e-12:
            market_dir = 1.0 if float(exit_price) > entry else -1.0
            stats = self.state.setdefault("analyst_accuracy", {})
            record_accuracy(stats, stances, market_dir)

        hist = self.state.setdefault("position_history", [])
        hist.append(
            {
                "symbol": pos.get("symbol") or self.cfg.market.symbol,
                "side": pos.get("side"),
                "qty": round(float(pos.get("qty") or 0), 6),
                "entry": float(pos.get("entry") or 0),
                "exit": round_price(float(exit_price or 0)),
                "pnl": round(float(pnl or 0), 4),
                "leverage": pos.get("leverage"),
                "opened_at": pos.get("opened_at", ""),
                "closed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "mode": mode,
                "reason": reason,
            }
        )
        self.state["position_history"] = hist[-200:]

    def _paper_close(self, price: float, reason: str) -> dict | None:
        pos = self.state.get("position")
        if not pos:
            return None
        slip = self.cfg.backtest.costs.slippage_bps / 10_000.0
        direction = 1.0 if pos["side"] == "LONG" else -1.0
        fill = price * (1 - slip) if pos["side"] == "LONG" else price * (1 + slip)
        pnl = (fill - pos["entry"]) * pos["qty"] * direction
        fee = pos["qty"] * fill * self.cfg.backtest.costs.fee_bps / 10_000.0
        self.state["equity"] += pnl - fee
        self.state["consecutive_losses"] = (
            self.state["consecutive_losses"] + 1 if pnl < 0 else 0
        )
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "action": "CLOSE",
            "side": pos["side"],
            "qty": round(pos["qty"], 6),
            "price": round_price(fill),
            "fee": round(fee, 4),
            "pnl": round(pnl - fee, 4),
            "reason": reason,
        }
        self.state["fills"].append(rec)
        self.state["fills"] = self.state["fills"][-500:]
        self._record_close(pos, fill, pnl - fee, reason, "paper")
        self.state["position"] = None
        return rec

    # ------------------------------------------------------------------
    # 实盘
    # ------------------------------------------------------------------
    def connect(
        self, testnet: bool = True, env: str | None = None, exchange: str | None = None
    ) -> BinanceFuturesClient | OKXClient:
        exchange = exchange or self.state.get("exchange", "binance")
        key, secret, passphrase = read_api_keys(exchange, testnet)
        if exchange == "okx":
            if not key or not secret:
                need = "OKX_DEMO_API_KEY / OKX_DEMO_API_SECRET" if testnet else "OKX_API_KEY / OKX_API_SECRET"
                raise ExchangeError(f"未设置 {need}（模拟盘与主网是两套独立 Key）")
            self.live_client = OKXClient(
                key, secret, passphrase, testnet=testnet, armed=self.armed, env=env
            )
        else:
            if not key or not secret:
                need = (
                    "BINANCE_DEMO_API_KEY / BINANCE_DEMO_API_SECRET"
                    if testnet
                    else "BINANCE_API_KEY / BINANCE_API_SECRET"
                )
                raise ExchangeError(f"未设置 {need}（模拟盘与主网是两套独立 Key）")
            self.live_client = BinanceFuturesClient(
                key, secret, testnet=testnet, armed=self.armed, env=env
            )
        return self.live_client

    def arm(self, mode: str, confirm: str, testnet: bool = True, exchange: str | None = None) -> dict:
        """解锁交易所下单。

        三种模式：
        - paper   ：本地模拟撮合，不碰交易所
        - testnet ：交易所**模拟盘**（假钱），只校验密钥，不需要确认文本
        - live    ：主网真钱，需要确认文本 + 环境变量（主网还要 ack 文件）

        exchange 决定接币安还是 OKX（对应不同的 Key / 域名 / 签名方式）。
        """
        if mode not in {"paper", "testnet", "demo", "live"}:
            raise ValueError("mode 只能是 paper / testnet / demo / live")
        exchange = (exchange or self.state.get("exchange", "binance")).lower()
        if exchange not in ("binance", "okx"):
            raise ValueError("exchange 只能是 binance / okx")
        prev_exchange = self.state.get("exchange", "binance")
        if mode == "paper":
            self.armed = False
            self.state["mode"] = "paper"
            self._save()
            return {"mode": "paper", "armed": False, "exchange": exchange}
        if mode in {"testnet", "demo"}:
            client = self.connect(testnet=True, env=("demo" if mode == "demo" else "testnet"), exchange=exchange)
            client.armed = True
            client.ping()
            acct = client.account()
            self.armed = True
            self.state["mode"] = "live"
            self.state["testnet"] = True
            self.state["exchange"] = exchange
            self._save()
            if prev_exchange != exchange:
                self._reset_account_scope(acct)
                self.reload_for_exchange()
            return {
                "mode": "testnet",
                "armed": True,
                "testnet": True,
                "exchange": exchange,
                "balance": acct.get("balance"),
                "available": acct.get("available"),
            }
        if confirm.strip() != "我确认使用真实资金":
            raise PermissionError("确认文本不匹配，拒绝解锁")
        if not testnet and os.getenv("CRYPTOAGENT_I_UNDERSTAND_THE_RISK") != "yes":
            raise PermissionError(
                "主网交易需要环境变量 CRYPTOAGENT_I_UNDERSTAND_THE_RISK=yes"
            )
        client = self.connect(testnet=testnet, exchange=exchange)
        client.armed = True
        client.ping()
        acct = client.account()  # 验证签名
        self.armed = True
        self.state["mode"] = "live"
        self.state["testnet"] = testnet
        self.state["exchange"] = exchange
        self._save()
        if prev_exchange != exchange:
            self._reset_account_scope(acct)
            self.reload_for_exchange()
        return {"mode": "live", "armed": True, "testnet": testnet, "exchange": exchange}

    def _backfill_closed_from_exchange(self, pos: dict) -> dict | None:
        """交易所侧已平仓时，回读 userTrades 把这次平仓补记进 fills / 持仓历史。

        实盘开仓有本地回执，平仓却常常发生在交易所侧：止损 / 止盈是挂上去的条件单，
        成交后本地收不到任何回调。只清 state["position"] 会让「最近成交」缺一笔平仓、
        「持仓历史」缺记录，已实现盈亏和每日盈亏统计也跟着少记。

        做法：以开仓成交（orderId 命中）为时间下界，取之后的反向成交，从最新往回
        累计到本地持仓量，聚合出真实平仓价 / 手续费 / 已实现盈亏。
        只补记账、绝不主动下单；取不到可信成交时返回 None，让调用方直接清缓存。
        """
        if self.live_client is None:
            return None
        symbol = str(pos.get("symbol") or self.cfg.market.symbol)
        try:
            rows = self.live_client.trades(symbol, limit=100)
        except Exception:  # noqa: BLE001 - 回读失败只是不补记，不影响清缓存
            return None
        if not rows:
            return None
        # 平仓方向：多头平仓是「卖出」，空头平仓是「买入」。
        # 这里必须用 action 而不是 side —— OKX 的 side 表示持仓方向（posSide），
        # 币安的 side 表示成交方向，两者语义不一致。
        close_action = "卖出" if str(pos.get("side")) == "LONG" else "买入"
        closes = [r for r in rows if r.get("action") == close_action]
        if not closes:
            return None
        # 只有开仓之后的成交才算本次平仓；开仓回执超出回看窗口时退回本地开仓时间
        open_ts = 0
        open_oid = str(pos.get("order_id") or "")
        if open_oid:
            hits = [r for r in rows if str(r.get("order_id")) == open_oid]
            if hits:
                open_ts = max(int(r.get("ts_ms") or 0) for r in hits)
        if not open_ts:
            open_ts = _parse_ts_ms(pos.get("opened_at"))
        closes = [r for r in closes if int(r.get("ts_ms") or 0) >= open_ts]
        if not closes:
            return None
        closes.sort(key=lambda r: -int(r.get("ts_ms") or 0))   # 最新在前，便于从后往前累计
        # 从最新往回累计到本地持仓量：更早那几笔仓位的平仓不会被误算进这一笔
        pos_qty = abs(float(pos.get("qty") or 0.0))
        picked: list[dict] = []
        total = 0.0
        for row in closes:                     # closes 按时间倒序
            picked.append(row)
            total += abs(float(row.get("qty") or 0.0))
            if pos_qty <= 0 or total >= pos_qty * 0.999:
                break
        qty = sum(abs(float(r.get("qty") or 0.0)) for r in picked)
        if qty <= 0:
            return None
        if pos_qty > 0 and qty < pos_qty * 0.98:
            return None                        # 成交量凑不齐，宁可空着也不记错账
        notional = sum(
            abs(float(r.get("qty") or 0.0)) * float(r.get("price") or 0.0) for r in picked
        )
        exit_px = notional / qty
        fee = sum(float(r.get("fee") or 0.0) for r in picked)
        gross = sum(float(r.get("pnl") or 0.0) for r in picked)
        if not gross:
            # OKX 部分场景 pnl 为空：退回按开仓价自己算价格差
            direction = 1.0 if str(pos.get("side")) == "LONG" else -1.0
            gross = (exit_px - float(pos.get("entry") or 0.0)) * qty * direction
        pnl = gross - fee
        ts_ms = max(int(r.get("ts_ms") or 0) for r in picked)
        ts = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_ms / 1000))
            if ts_ms
            else time.strftime("%Y-%m-%d %H:%M:%S")
        )
        order_id = str(picked[0].get("order_id") or "")
        with self.lock:
            self.state["fills"].append(
                {
                    "ts": ts,
                    "action": "CLOSE",
                    "side": pos.get("side"),
                    "symbol": symbol,
                    "qty": round(qty, 6),
                    "price": round_price(exit_px),
                    "fee": round(fee, 6),
                    "pnl": round(pnl, 4),
                    "reason": "exchange",
                    "order_id": order_id,
                    "backfilled": True,
                }
            )
            self.state["fills"] = self.state["fills"][-500:]
            self.state["equity"] = float(self.state.get("equity") or 0.0) + pnl
            self.state["consecutive_losses"] = (
                int(self.state.get("consecutive_losses") or 0) + 1
                if pnl < 0
                else 0
            )
            self._record_close(pos, exit_px, pnl, "exchange", "live")
            self._save()
        self._log_live("CLOSE", str(pos.get("side")), qty, exit_px, order_id)
        self._notify(
            "info" if pnl >= 0 else "critical",
            f"{pos.get('side')} 已由交易所平仓（补记）",
            f"成交价 {round_price(exit_px)}，已实现盈亏 {pnl:+,.2f} U（止损/止盈或手动平仓）",
            {"symbol": symbol, "pnl": round(pnl, 2), "reason": "exchange"},
        )
        return {"qty": qty, "price": exit_px, "fee": fee, "pnl": pnl}

    def _reconcile_position_with_exchange(self) -> None:
        """核对本地实盘持仓与交易所真实持仓，清理「幽灵仓位」。

        本地 state["position"] 只是缓存：当仓位在交易所网页端被手动平掉、或下单被拒
        但本地已记账时，会留下幽灵仓位，导致切换/删除品种、开仓被误拦截。
        只核对实盘（mode=live）仓位；模拟盘仓位不对应交易所，不能清。
        拿不到交易所数据时保守不动；确认交易所无持仓才清本地缓存，绝不主动平仓。
        """
        pos = self.state.get("position")
        if not pos or pos.get("mode") != "live":
            return
        exchange = self.state.get("exchange", "binance")
        testnet = bool(self.state.get("testnet", True))
        # 用仓位自己的品种去查：auto_rotate 换过品种后，拿当前品种去查只会得到空仓位，
        # 会把「另一个品种还在持仓」误判成幽灵仓位。
        symbol = str(pos.get("symbol") or self.cfg.market.symbol)
        try:
            client = self.live_client or self.connect(testnet=testnet, exchange=exchange)
            real = client.positions(symbol)
        except Exception:  # noqa: BLE001 - 拿不到交易所数据就保守不动
            return
        if not real:
            # 交易所已无持仓：先把这次平仓的成交补记下来，再清缓存。
            # 补记失败也只是少了流水，不能因此留下幽灵仓位。
            try:
                self._backfill_closed_from_exchange(pos)
            except Exception as exc:  # noqa: BLE001 - 补记是尽力而为
                self.last_error = f"平仓补记失败：{type(exc).__name__}: {exc}"[:200]
            with self.lock:
                self.state["position"] = None
                self._save()

    def switch_symbol(self, symbol: str) -> dict:
        """切换活跃品种（有持仓时拒绝，避免仓位和行情对不上）。"""
        symbol = symbol.strip().upper()
        if not symbol:
            raise ValueError("品种不能为空")
        if symbol == self.cfg.market.symbol:
            return {"symbol": symbol, "changed": False}
        # 先与交易所核对，清理幽灵仓位（本地记着、但交易所实际已平）
        self._reconcile_position_with_exchange()
        with self.lock:
            if self.state.get("position"):
                raise ValueError("还有未平仓位，先平仓再换品种")
            self.cfg.market.symbol = symbol
            self.state["symbol"] = symbol
            self.df = None
            self._save()
        df, src = self.refresh_data()
        return {
            "symbol": symbol,
            "changed": True,
            "data_source": src,
            "price": round(float(df["close"].iloc[-1]), 4),
            "bars": int(len(df)),
        }

    def disarm(self) -> dict:
        self.armed = False
        if self.live_client:
            self.live_client.armed = False
        self.state["mode"] = "paper"
        self._save()
        return {"mode": "paper", "armed": False}

    def _scan_opportunities(self) -> list[dict]:
        """扫全部品种的因子复合信号，返回按机会分降序的 [{symbol, composite, score, active}]。

        纯本地计算、不下单、不调 LLM；结果缓存到 self.opportunities 供页面排行榜展示。
        """
        from dataclasses import replace

        from .data import load_market
        from .factors.signal import compute_factor_signal

        rows: list[dict] = []
        for sym in self.cfg.market.symbols:
            entry = {"symbol": sym, "composite": 0.0, "score": 0.0, "active": sym == self.cfg.market.symbol}
            try:
                mkt = replace(self.cfg.market, symbol=sym)
                df, _ = load_market(mkt, prefer=self.state.get("exchange", "binance"))
                # 机会榜只需最近一段历史：因子最长回看 336 + 目标窗口 200 ≈ 536 根，
                # 取 600 根足够；否则在 6000 根上逐因子计算，单品种要 10-30 秒。
                df = df.tail(600)
                fsig = compute_factor_signal(self.cfg.research.factor_library, df)
                composite = float(fsig.get("composite", 0.0) or 0.0)
                entry["composite"] = round(composite, 4)
                entry["score"] = round(abs(composite), 4)
            except Exception:  # noqa: BLE001 - 单个品种失败不影响整体
                entry["error"] = True
            rows.append(entry)
        rows.sort(key=lambda r: -(r["score"] or 0))
        self.opportunities = rows
        self.scan_state["last_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.scan_state["last_ts"] = time.time()
        return rows

    def start_scan_async(self) -> dict:
        """后台线程扫描全品种，立即返回状态。

        一次扫描要跑 6 个品种的因子计算（约 10 秒）。如果同步跑在 HTTP 处理里，
        页面会一直卡在「扫描中」，还会把同进程的其它接口一起拖住。
        """
        with self.lock:
            if self.scan_state.get("scanning"):
                return dict(self.scan_state)
            self.scan_state["scanning"] = True
            self.scan_state["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.scan_state["error"] = ""

        def _work() -> None:
            rows: list[dict] = []
            err = ""
            try:
                rows = self._scan_opportunities()
            except Exception as exc:  # noqa: BLE001 - 扫描失败不影响交易主链路
                err = f"{type(exc).__name__}: {exc}"[:200]
            with self.lock:
                self.opportunities = rows
                self.scan_state["scanning"] = False
                self.scan_state["last_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self.scan_state["error"] = err

        threading.Thread(target=_work, name="opportunity-scan", daemon=True).start()
        return dict(self.scan_state)

    def _pick_best_symbol(self, min_score: float | None = None) -> str | None:
        """自主选品种：扫全品种，返回机会分最大且超阈值的品种。

        全部低于阈值时不再「原地不动」——否则会一直死守当前品种（可能还是评分最低
        的那个），每次决策都在最弱的信号上打转。此时退而求其次切到相对最强品种，
        但要求它明显优于当前品种（分差 ≥ 0.02），并加 30 分钟冷却，避免弱机会区间
        反复横跳。
        """
        threshold = float(min_score if min_score is not None else self.cfg.market.rotate_min_score)
        # 5 分钟内的扫描结果直接复用：自动轮动每轮都扫一遍会和页面扫描重复，白烧 CPU
        fresh = (time.time() - float(self.scan_state.get("last_ts") or 0.0)) < 300.0
        rows = self.opportunities if (fresh and self.opportunities) else self._scan_opportunities()
        if not rows:
            return None
        top = rows[0]
        if top.get("error"):
            return None
        score = float(top.get("score") or 0.0)
        if score >= threshold:
            return top["symbol"]

        # —— 全体弱机会：切到相对最强，但要「明显更好」且限频 ——
        current = str(self.cfg.market.symbol or "")
        if top["symbol"] == current:
            return None
        cur_score = next(
            (float(r.get("score") or 0.0) for r in rows if r.get("symbol") == current), 0.0
        )
        if score - cur_score < 0.02:
            return None
        now = time.time()
        if now - float(getattr(self, "_last_fallback_rotate_at", 0.0)) < 1800.0:
            return None
        self._last_fallback_rotate_at = now
        return top["symbol"]

    # ------------------------------------------------------------------
    # 决策与执行
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 分析师权重事后校准
    # ------------------------------------------------------------------
    def analyst_weights(self) -> tuple[list[float], bool]:
        """从历史平仓的方向命中率生成辩论权重；样本不足则沿用默认 0.5/0.3/0.2。"""
        try:
            return weights_from_accuracy(self.state.get("analyst_accuracy"))
        except Exception:  # noqa: BLE001 - 校准失败不影响决策
            return [0.5, 0.3, 0.2], False

    # ------------------------------------------------------------------
    # 持仓管理员：开仓之后的退出决策（移动止损 / 时间止损 / 趋势反转）
    # ------------------------------------------------------------------
    def _opportunity_composite(self, symbol: str) -> float | None:
        """先复用机会扫描缓存；没有缓存时就地算一次（90 秒短缓存）。

        机会扫描只在 auto_rotate 开启或前端打开机会榜时触发，否则持仓管理员
        的「因子强反向退出」永远拿不到 composite。这里做一次按需兜底。
        """
        for r in (getattr(self, "opportunities", None) or []):
            if r.get("symbol") == symbol and not r.get("error"):
                try:
                    return float(r.get("composite") or 0.0)
                except (TypeError, ValueError):
                    return None
        cache = getattr(self, "_composite_cache", None)
        if cache is None:
            cache = self._composite_cache = {}
        now = time.time()
        hit = cache.get(symbol)
        if hit and now - float(hit[0]) < 90.0:
            return float(hit[1])
        if self.df is None:
            return None
        try:
            from .factors.signal import compute_factor_signal

            val = float(
                compute_factor_signal(self.cfg.research.factor_library, self.df)
                .get("composite", 0.0) or 0.0
            )
        except Exception:  # noqa: BLE001 - 因子算不出来就退回纯价格规则
            return None
        cache[symbol] = (now, val)
        return val

    def _sync_live_stops(self, pos: dict, new_stop: float) -> bool:
        """实盘按新止损重挂条件单。

        同方向 closePosition 条件单只能有一张，所以必须「先撤后挂」。
        万一新止损挂不上，立刻回退挂回旧止损并发 critical 告警 —— 绝不能留下裸仓。
        """
        sym = pos.get("symbol") or self.cfg.market.symbol
        client = self.live_client
        if client is None or not self.armed:
            return False
        close_side = "SELL" if pos["side"] == "LONG" else "BUY"
        try:
            client.cancel_all(sym)
            try:
                client.place_stop_market(sym, close_side, float(new_stop), order_type="STOP_MARKET")
            except Exception as exc:  # noqa: BLE001
                # 旧单已撤、新单没挂上：恢复旧止损，本次调整作废
                restored = False
                try:
                    client.place_stop_market(sym, close_side, float(pos["stop"]),
                                             order_type="STOP_MARKET")
                    restored = True
                except Exception:  # noqa: BLE001
                    pass
                self.last_error = f"移动止损挂单失败，已回退旧止损：{exc}"
                self._notify("critical", f"{sym} 移动止损挂单失败",
                             f"旧止损恢复={restored}；错误：{str(exc)[:120]}",
                             {"symbol": sym, "restored": restored})
                return False
            try:
                client.place_stop_market(sym, close_side, float(pos["tp"]),
                                         order_type="TAKE_PROFIT_MARKET")
                pos.pop("tp_pending", None)      # 本次重挂成功，清掉待补标记
            except Exception as exc:  # noqa: BLE001 - 止损已生效，但止盈缺失必须告警
                pos["tp_pending"] = True
                self.last_error = f"移动止损后止盈单重挂失败：{exc}"
                self._notify(
                    "critical", f"{sym} 止盈单重挂失败",
                    f"仓位目前只有止损、没有止盈；守护线程会在下一轮补挂。错误：{str(exc)[:120]}",
                    {"symbol": sym, "tp": pos.get("tp")},
                )
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"移动止损撤单失败：{exc}"
            return False

    def _retry_pending_take_profit(self, pos: dict) -> None:
        """止盈单曾重挂失败时，守护线程每轮尝试单独补挂（不动其它条件单）。"""
        if not pos.get("tp_pending"):
            return
        client = self.live_client
        if client is None or not self.armed or pos.get("mode") != "live":
            return
        sym = pos.get("symbol") or self.cfg.market.symbol
        close_side = "SELL" if pos["side"] == "LONG" else "BUY"
        try:
            client.place_stop_market(sym, close_side, float(pos["tp"]),
                                     order_type="TAKE_PROFIT_MARKET")
            with self.lock:
                pos.pop("tp_pending", None)
                self._save()
            self._notify("info", f"{sym} 止盈单已补挂",
                         f"触发价 {pos.get('tp')}", {"symbol": sym})
        except Exception:  # noqa: BLE001 - 下轮继续重试，不刷屏
            pass

    # ------------------------------------------------------------------
    # 持仓管理守护线程：脱离自动循环，手动模式下也生效
    # ------------------------------------------------------------------
    def start_position_guard(self, interval_sec: int = 30) -> None:
        """启动/确认持仓守护线程。幂等，重复调用不会创建第二个线程。"""
        self._guard_interval = max(5, int(interval_sec))
        if getattr(self, "_guard_thread", None) and self._guard_thread.is_alive():
            return
        self._guard_stop.clear()
        self._guard_thread = threading.Thread(
            target=self._guard_loop, name="position-guard", daemon=True
        )
        self._guard_thread.start()

    def stop_position_guard(self) -> None:
        self._guard_stop.set()

    def _guard_loop(self) -> None:
        while not self._guard_stop.wait(self._guard_interval):
            try:
                if self.df is None or not self.state.get("position"):
                    continue
                self._reconcile_position_with_exchange()
                pos = self.state.get("position")
                if not pos:
                    continue
                self._retry_pending_take_profit(pos)
                px = None
                if self.live_price_provider is not None:
                    try:
                        v = float(self.live_price_provider() or 0.0)
                        if v > 0:
                            px = v
                    except Exception:  # noqa: BLE001
                        pass
                self._manage_open_position(pos, px)
            except Exception as exc:  # noqa: BLE001 - 守护线程绝不能因单次异常退出
                self.last_error = f"持仓守护异常：{type(exc).__name__}: {str(exc)[:120]}"

    def _manage_open_position(self, pos: dict, price: float | None = None) -> dict:
        """包装：抢锁防重入（自动循环与守护线程可能同时进入）。"""
        lock = getattr(self, "_guard_lock", None)
        if lock is None:      # 兼容用 object.__new__ 构造的测试实例
            return self._manage_open_position_inner(pos, price)
        if not lock.acquire(blocking=False):
            return {"action": "hold", "reason": "持仓管理已在另一线程执行", "details": {}}
        try:
            out = self._manage_open_position_inner(pos, price)
            # 每轮都刷新时间戳。hold 不产生动作，没有时间戳就分不清
            # 「刚刚巡检过」和「线程早就停了」——页面靠它显示上次巡检时刻。
            out["ts"] = time.time()
            self.last_management = out
            return out
        finally:
            lock.release()

    def _manage_open_position_inner(self, pos: dict, price: float | None = None) -> dict:
        """在「已有持仓、跳过开仓决策」的这一轮里做退出管理。"""
        cfg = getattr(self.cfg, "position", None)
        if cfg is None or not cfg.enabled:
            return {"action": "hold", "reason": "持仓管理员未启用", "details": {}}
        try:
            df = self.df if self.df is not None else self.refresh_data()[0]
            px = float(price) if price and price > 0 else self.price()
            sym = pos.get("symbol") or self.cfg.market.symbol
            dec = manage_position(pos, df, px, cfg,
                                  composite=self._opportunity_composite(sym))

            if dec.action == "close":
                res = self.close(px, reason=f"持仓管理员：{dec.reason}")
                dec.details["close_result"] = res
                self._notify("info", f"{sym} 持仓管理员提前退出", dec.reason,
                             {"symbol": sym, "action": "close", "details": dec.details})

            elif dec.action == "tighten" and dec.new_stop is not None:
                now = time.time()
                if now - float(getattr(self, "_stop_adjust_at", 0.0)) < cfg.min_adjust_interval_sec:
                    dec.details["throttled"] = True
                    return dec.to_dict()
                live = pos.get("mode") == "live" and self.state.get("mode") == "live"
                if live and cfg.live_stop_adjust and not self._sync_live_stops(pos, dec.new_stop):
                    return {"action": "hold", "reason": "交易所条件单未更新，本地止损保持不变",
                            "details": dec.details}
                with self.lock:
                    if self.state.get("position") is pos:
                        pos["stop"] = float(dec.new_stop)
                        self._save()
                self._stop_adjust_at = now
                self.state["last_stop_adjust_at"] = now
                self._save()
                self._notify("info", f"{sym} 移动止损已更新", dec.reason,
                             {"symbol": sym, "new_stop": dec.new_stop})

            if cfg.llm_explain and dec.action != "hold":
                try:
                    from .agents.llm import LLMClient
                    dec.explanation = explain_management(LLMClient(self.cfg.llm), dec, pos)
                except Exception:  # noqa: BLE001 - 解释是旁路
                    pass
            return dec.to_dict()
        except Exception as exc:  # noqa: BLE001 - 持仓管理绝不阻断主流程
            return {"action": "hold", "reason": f"持仓管理异常，保守不动：{type(exc).__name__}",
                    "details": {"error": str(exc)[:160]}}

    def run_decision(self, rounds: int = 2, price: float | None = None) -> dict:
        # 已有持仓时不再跑开仓决策：既省掉每轮 LLM 调用，也不会每 5 分钟推一条
        # 「决策被风控拦截」。先与交易所对账（实盘），避免交易所侧止损/止盈已成交、
        # 本地缓存还挂着，导致一直等不到「平仓后再决策」。
        self._reconcile_position_with_exchange()
        held = self.state.get("position")
        if held:
            return {
                "skipped": True,
                "reason": "已有持仓，等平仓完成后再做开仓决策",
                "symbol": self.cfg.market.symbol,
                "position": held,
                # 持仓期间不是完全放手：由持仓管理员做移动止损 / 时间止损 / 趋势反转退出
                "management": self._manage_open_position(held, price),
            }
        df = self.df if self.df is not None else self.refresh_data()[0]
        # 开仓链仍以 K 线收盘价为基准（与改造前一致）；传入的 price 只供持仓管理员
        # 用实时价判断，避免顺带改变 account_state / 组合敞口的计算口径。
        ref_price = float(df["close"].iloc[-1])
        pipe = TradingPipeline(self.cfg)
        # 杠杆上限以交易所支持的最大杠杆为准；未连交易所用配置兜底（默认 3x 保守）
        ex_max = None
        if self.armed and self.live_client is not None:
            try:
                ex_max = float(self.live_client.max_leverage(self.cfg.market.symbol))
            except Exception:  # noqa: BLE001 - 拿不到就退回配置兜底
                ex_max = None
        aw, calibrated = self.analyst_weights()
        decision = pipe.run(
            df, state=self.account_state(ref_price), debate_rounds=rounds, persist=False,
            exchange_max_leverage=ex_max, positions=self._positions_for_risk(ref_price),
            analyst_weights=aw, analyst_calibrated=calibrated,
            exchange=self.state.get("exchange", "binance"),
        )
        self.last_decision = decision

        # A2：决策被风控引擎拒绝时告警（approved=False 且 action=flat 才是真拒绝）
        # 同一品种 + 同一原因 30 分钟内只提醒一次：否则每轮都会刷一条相同的拦截通知。
        if not decision.risk_decision["approved"] and decision.risk_decision["action"] == "flat":
            reasons = decision.risk_decision.get("reasons", [])
            sig = f"{decision.symbol}|{(reasons[0] if reasons else '未通过风控检查')[:60]}"
            now = time.time()
            alerts = getattr(self, "_reject_alert_at", None)
            if alerts is None:
                alerts = self._reject_alert_at = {}
            if now - float(alerts.get(sig, 0.0)) >= 1800.0:
                alerts[sig] = now
                if len(alerts) > 50:      # 防止长期运行无限增长
                    for k in sorted(alerts, key=alerts.get)[:25]:
                        alerts.pop(k, None)
                self.state["alert_dedupe"] = alerts
                self._save()
                self._notify("warn", f"{decision.symbol} 决策被风控拦截",
                             "；".join(reasons[:3]) or "未通过风控检查",
                             {"symbol": decision.symbol, "checks": decision.risk_decision.get("checks")})

        out = Path(self.cfg.runs_dir) / "decisions"
        out.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        payload = asdict(decision)
        payload["markdown"] = render_markdown(decision)
        (out / f"{stamp}_{decision.symbol}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        (out / "latest.md").write_text(payload["markdown"], encoding="utf-8")

        with self.lock:
            self.pending_order = decision.order
            self.state["last_decision"] = {
                "ts": decision.ts,
                "action": decision.risk_decision["action"],
                "size_pct": decision.risk_decision["size_pct"],
                "approved": decision.risk_decision["approved"],
                "order": decision.order,
                "file": str(out / f"{stamp}_{decision.symbol}.json"),
            }
            # 供下一笔开仓带上：平仓时据此评判每个分析师看对方向没有
            self.state["pending_stances"] = [
                float(a.get("stance") or 0.0) for a in decision.analysts[:3]
            ]
            self._save()
        return payload

    def execute(self, price: float | None = None, reason: str = "manual") -> dict:
        """执行最近一次决策里的订单计划（进程重启后用 state 里恢复的计划）。"""
        decision = self.last_decision
        order = decision.order if decision is not None else self.pending_order
        if order is None:
            return {"ok": False, "error": "还没有决策，请先运行一次决策"}
        price = price if price is not None else self.price()

        if not decision and not order.get("quantity"):
            return {"ok": False, "error": "当前决策没有订单（风控未通过或策略观望）"}

        # 先与交易所核对，清理幽灵仓位，避免误判「已有持仓」而拒绝开仓
        self._reconcile_position_with_exchange()
        with self.lock:
            if self.state.get("halt"):
                return {"ok": False, "error": "熔断开关已打开"}
            if self.state.get("position"):
                return {"ok": False, "error": "已有持仓，拒绝重复开仓（请先平仓）"}

            # 把这次决策时三位分析师的立场带进仓位，平仓时用于方向命中率校准
            stances = list(self.state.get("pending_stances") or [])
            side = "LONG" if order["side"] == "BUY" else "SHORT"
            qty = float(order["quantity"])
            lev = float(order["leverage"])
            stop = float(order["stop_loss_price"])
            tp = float(order["take_profit_price"])

            # 方向兜底：空单止损必须高于现价、多单必须低于现价，否则交易所直接 -2021
            # （历史计划里存着的老价格也可能已经穿越现价，执行前统一校正一次）
            gap = 0.003
            if side == "SHORT":
                if stop <= price * (1 + 0.0005):
                    stop = round_price(price * (1 + gap))
                if tp >= price * (1 - 0.0005):
                    tp = round_price(price * (1 - gap))
            else:
                if stop >= price * (1 - 0.0005):
                    stop = round_price(price * (1 - gap))
                if tp <= price * (1 + 0.0005):
                    tp = round_price(price * (1 + gap))

            if self.state["mode"] == "live":
                if not self.armed or self.live_client is None:
                    return {"ok": False, "error": "实盘未解锁"}
                try:
                    self.live_client.set_leverage(self.cfg.market.symbol, int(round(lev)))
                    # 先清掉这个品种的残留条件单：closePosition 的止损单一个方向只能有一张，
                    # 上一次执行留下的孤儿单会让新止损直接报 -4130
                    try:
                        self.live_client.cancel_all(self.cfg.market.symbol)
                    except Exception:  # noqa: BLE001 - 没有旧单时会报错，忽略
                        pass
                    res = self.live_client.place_market(
                        self.cfg.market.symbol, "BUY" if side == "LONG" else "SELL", qty
                    )
                    close_side = "SELL" if side == "LONG" else "BUY"
                    # 止损单（保命）：触发价在开仓价亏损侧。
                    # 止损是刚开仓后最重要的保护：一旦下失败，绝不能留下"裸仓"——
                    # 立即反向平掉刚开的仓位并作废本计划，避免无保护敞口 / 下次循环重复开仓。
                    try:
                        self.live_client.place_stop_market(
                            self.cfg.market.symbol, close_side, stop,
                            order_type="STOP_MARKET",
                        )
                    except ExchangeError as exc:
                        self.last_error = f"止损单下失败：{exc}"
                        try:
                            self.live_client.place_market(
                                self.cfg.market.symbol, close_side, qty, reduce_only=True
                            )
                            self.last_error += "；已立即反向平仓保护（不留裸仓）"
                        except ExchangeError as exc2:
                            self.last_error += f"；紧急平仓也失败：{exc2}"
                        self._consume_plan()
                        return {"ok": False, "error": self.last_error}
                    # 止盈单（锁利）：触发价在开仓价盈利侧；失败不影响持仓与止损
                    try:
                        self.live_client.place_stop_market(
                            self.cfg.market.symbol, close_side, tp,
                            order_type="TAKE_PROFIT_MARKET",
                        )
                    except ExchangeError as exc:
                        self.last_error = f"止盈单下失败（持仓与止损已生效）：{exc}"
                    # 回读交易所成交：市价单的 avgPrice 常为 0，手续费也不在订单响应里
                    detail = self._live_fill(self.cfg.market.symbol, res.order_id)
                    fill_px = float(detail["price"]) if detail and detail.get("price") else float(res.price or price)
                    fee = float(detail["fee"]) if detail else 0.0
                    plan_px = float(order.get("entry_ref_price") or 0.0) or None
                    self.state["position"] = {
                        "symbol": self.cfg.market.symbol,
                        "side": side,
                        "qty": qty,
                        "entry": fill_px,
                        "leverage": lev,
                        "stop": stop,
                        "tp": tp,
                        "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "opened_epoch": time.time(),
                        "initial_r_pct": abs(fill_px - stop) / fill_px if fill_px > 0 else 0.0,
                        "analyst_stances": stances,
                        "mode": "live",
                        "order_id": res.order_id,
                    }
                    self.state["fills"].append(
                        {
                            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "action": "OPEN",
                            "side": side,
                            "symbol": self.cfg.market.symbol,
                            "qty": qty,
                            "price": round_price(fill_px),
                            "plan_price": round_price(plan_px),
                            "slippage_bps": round(
                                (fill_px - plan_px) / plan_px * 10_000.0, 2
                            ) if plan_px else None,
                            "fee": round(fee, 6),
                            "reason": f"live:{res.order_id}",
                        }
                    )
                    self._save()
                    self._log_live("OPEN", side, qty, res.price or price, res.order_id)
                    self._consume_plan()
                    return {"ok": True, "mode": "live", "order_id": res.order_id}
                except ExchangeError as exc:
                    self.last_error = str(exc)
                    return {"ok": False, "error": str(exc)}
            else:
                rec = self._paper_open(side, qty, price, lev, stop, tp, reason,
                                       plan_price=order.get("entry_ref_price"),
                                       stances=stances)
                self._consume_plan()
                self._save()
                return {"ok": True, "mode": "paper", "fill": rec}

    def _consume_plan(self) -> None:
        """订单计划一旦执行就作废，避免重复下单。"""
        self.pending_order = None
        # 立场已经写进仓位，清掉待办避免下一笔误用旧立场
        self.state["pending_stances"] = []
        ld = self.state.get("last_decision")
        if isinstance(ld, dict) and isinstance(ld.get("order"), dict):
            ld["order"]["status"] = "FILLED"

    def close(self, price: float | None = None, reason: str = "manual") -> dict:
        price = price if price is not None else self.price()
        with self.lock:
            pos = self.state.get("position")
            if not pos:
                return {"ok": False, "error": "当前没有持仓"}
            # 用仓位自己的品种：auto_rotate 切过品种后，cfg.market.symbol 已经变了，
            # 拿当前品种去撤单/平仓会平错对象（与「幽灵仓位」同源的坑）。
            sym = pos.get("symbol") or self.cfg.market.symbol
            if pos.get("mode") == "live" and self.state["mode"] == "live":
                if not self.armed or self.live_client is None:
                    return {"ok": False, "error": "实盘未解锁"}
                try:
                    self.live_client.cancel_all(sym)
                    res = self.live_client.place_market(
                        sym,
                        "SELL" if pos["side"] == "LONG" else "BUY",
                        float(pos["qty"]),
                        reduce_only=True,
                    )
                    detail = self._live_fill(sym, res.order_id)
                    exit_px = float(detail["price"]) if detail and detail.get("price") else float(res.price or price)
                    direction = 1.0 if pos["side"] == "LONG" else -1.0
                    gross = (exit_px - float(pos["entry"])) * float(pos["qty"]) * direction
                    if detail is not None:
                        gross = float(detail["pnl"])  # 交易所口径的已实现盈亏（不含手续费）
                    fee = float(detail["fee"]) if detail else 0.0
                    pnl = gross - fee
                    self.state["fills"].append(
                        {
                            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "action": "CLOSE",
                            "side": pos["side"],
                            "symbol": pos.get("symbol") or self.cfg.market.symbol,
                            "qty": round(float(pos["qty"]), 6),
                            "price": round_price(exit_px),
                            "fee": round(fee, 6),
                            "pnl": round(pnl, 4),
                            "reason": reason,
                            "order_id": res.order_id,
                        }
                    )
                    self.state["fills"] = self.state["fills"][-500:]
                    self.state["equity"] += pnl
                    self._record_close(pos, exit_px, pnl, reason, "live")
                    self.state["position"] = None
                    self._save()
                    self._log_live("CLOSE", pos["side"], pos["qty"], exit_px, res.order_id)
                    # A2：平仓后报告已实现盈亏
                    self._notify("info" if pnl >= 0 else "critical",
                                 f"{pos['side']} 平仓（实盘）",
                                 f"已实现盈亏 {pnl:+,.2f} U（{reason}）",
                                 {"symbol": sym, "pnl": round(pnl, 2), "reason": reason})
                    return {"ok": True, "mode": "live", "order_id": res.order_id}
                except ExchangeError as exc:
                    return {"ok": False, "error": str(exc)}
            rec = self._paper_close(price, reason)
            self._save()
            # A2：平仓后报告已实现盈亏
            self._notify("info" if rec["pnl"] >= 0 else "critical",
                         f"{pos['side']} 平仓（模拟）",
                         f"已实现盈亏 {rec['pnl']:+,.2f} U（{reason}）",
                         {"symbol": sym, "pnl": rec["pnl"], "reason": reason})
            return {"ok": True, "mode": "paper", "fill": rec}

    def check_stops(self, price: float | None = None) -> dict | None:
        """模拟盘里用最新价检查止损/止盈。实盘由交易所的 STOP_MARKET 单负责。"""
        price = price if price is not None else self.price()
        pos = self.state.get("position")
        if not pos or pos.get("mode") == "live":
            return None
        hit = None
        if pos["side"] == "LONG":
            if price <= pos["stop"]:
                hit = "stop_loss"
            elif price >= pos["tp"]:
                hit = "take_profit"
        else:
            if price >= pos["stop"]:
                hit = "stop_loss"
            elif price <= pos["tp"]:
                hit = "take_profit"
        if hit:
            with self.lock:
                return self._paper_close(price, hit)
        return None

    def _log_live(self, action: str, side: str, qty: float, price: float, order_id: str) -> None:
        path = Path(self.cfg.runs_dir) / "live_orders.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "action": action,
                        "side": side,
                        "qty": qty,
                        "price": price,
                        "order_id": order_id,
                        "exchange": self.state.get("exchange", "binance"),
                        "testnet": self.state.get("testnet", True),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    # ------------------------------------------------------------------
    # 自动循环
    # ------------------------------------------------------------------
    def start_auto(self, interval_sec: int = 300, execute: bool = False) -> dict:
        if self._auto_thread and self._auto_thread.is_alive():
            return {"running": True, "interval_sec": self.auto_interval}
        interval_sec = max(30, int(interval_sec))
        self.auto_interval = interval_sec
        self.auto_execute = bool(execute)
        self._auto_stop.clear()
        self._auto_thread = threading.Thread(target=self._auto_loop, daemon=True)
        self._auto_thread.start()
        self.state["auto"] = {
            "running": True,
            "interval_sec": interval_sec,
            "execute": self.auto_execute,
            "last_run": self.state["auto"].get("last_run", ""),
        }
        self._save()
        return self.state["auto"]

    def stop_auto(self) -> dict:
        self._auto_stop.set()
        self.state["auto"]["running"] = False
        self._save()
        return self.state["auto"]

    def _auto_loop(self) -> None:
        while not self._auto_stop.is_set():
            try:
                self.cycle()
            except Exception as exc:  # noqa: BLE001 - 自动循环不能因单次异常退出
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._auto_stop.wait(self.auto_interval)

    def cycle(self) -> dict:
        with self._cycle_lock:
            # 数据还没加载好（后台 K 线下载中）时拒绝决策，返回友好提示，
            # 不触发同步 refresh_data 把请求阻塞几十秒。
            if self.df is None:
                return {
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "price": None,
                    "stopped": False,
                    "decision": None,
                    "executed": None,
                    "error": "行情数据加载中，请稍候再启动",
                }
            # 自主选品种：无持仓时每 N 轮扫一次全品种，切到因子信号最强的（有持仓则跳过）
            rotated = False
            self._rotate_cycle_count += 1
            every = max(1, int(getattr(self.cfg.market, "rotate_every_cycles", 1) or 1))
            if self.cfg.market.auto_rotate and not self.state.get("position") \
                    and self._rotate_cycle_count % every == 0:
                try:
                    best = self._pick_best_symbol()
                    if best and best != self.cfg.market.symbol:
                        self.switch_symbol(best)
                        rotated = True
                except Exception as exc:  # noqa: BLE001 - 选品种失败不阻断决策
                    self.last_error = f"自主选品种失败：{exc}"

            df, src = self.refresh_data()
            price = float(df["close"].iloc[-1])
            # 有实时报价时优先用实时价，模拟盘止损才跟得上秒级行情；
            # 刚换品种时跳过实时价（它还是旧品种的价）
            if self.live_price_provider is not None and not rotated:
                try:
                    lp = float(self.live_price_provider() or 0.0)
                    if lp > 0:
                        price = lp
                except Exception:  # noqa: BLE001
                    pass
            stopped = self.check_stops(price)
            # 持仓管理员要用同一份实时价判断（模拟盘 check_stops 也用它）
            payload = self.run_decision(price=price)
            executed = None
            if self.auto_execute and payload.get("order"):
                executed = self.execute(price, reason="auto")
            self.state["auto"]["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._save()
            return {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "price": price,
                "stopped": stopped,
                "decision": payload.get("risk_decision"),
                "executed": executed,
            }

    # ------------------------------------------------------------------
    # 状态输出
    # ------------------------------------------------------------------
    def _daily_pnl(self, days: int = 120) -> list[dict]:
        """按平仓日期聚合已实现盈亏，返回最近 N 天（有交易的日子）。"""
        agg: dict[str, dict] = {}
        for h in self.state.get("position_history", []):
            day = str(h.get("closed_at") or "")[:10]
            if len(day) != 10:
                continue
            row = agg.setdefault(day, {"date": day, "pnl": 0.0, "trades": 0, "wins": 0})
            pnl = float(h.get("pnl") or 0.0)
            row["pnl"] = round(row["pnl"] + pnl, 4)
            row["trades"] += 1
            if pnl > 0:
                row["wins"] += 1
        return [agg[k] for k in sorted(agg)][-days:]

    def status(self, price_override: float | None = None) -> dict:
        with self.lock:
            price = None
            # 数据还没加载好（启动后 K 线在后台下载）时不调 self.price()，
            # 否则会同步触发 refresh_data 把 status 请求阻塞几十秒，窗口打开即卡死。
            if self.df is not None:
                try:
                    price = price_override if price_override else self.price()
                except Exception:  # noqa: BLE001
                    pass
            eq = self._equity(price) if price else self.state["equity"]
            pos = self.state.get("position")
            if pos and price:
                pos = {
                    **pos,
                    "mark": price,
                    "unrealized": round(self._position_pnl(price), 2),
                    "notional": round(pos["qty"] * price, 2),
                }
            if pos:
                pos = {**pos, "symbol": pos.get("symbol") or self.cfg.market.symbol}
            hist = self.state.get("history", [])
            if price:
                hist = (hist + [{"ts": time.strftime("%H:%M:%S"), "equity": round(eq, 2), "price": price}])[-300:]
                self.state["history"] = hist
            return {
                "mode": self.state["mode"],
                "armed": self.armed,
                "testnet": self.state.get("testnet", True),
                "symbol": self.cfg.market.symbol,
                "interval": self.cfg.market.interval,
                "auto_rotate": bool(self.cfg.market.auto_rotate),
                "rotate_min_score": float(self.cfg.market.rotate_min_score),
                "rotate_every_cycles": int(self.cfg.market.rotate_every_cycles),
                "opportunities": self.opportunities,
                "scan_state": dict(self.scan_state),
                "price": price,
                "equity": round(eq, 2),
                "cash_equity": round(self.state["equity"], 2),
                "peak_equity": round(self.state["peak_equity"], 2),
                "day_pnl_pct": round((eq / self.state["day_start_equity"] - 1) * 100, 3)
                if self.state["day_start_equity"]
                else 0.0,
                "drawdown_pct": round((eq / max(self.state["peak_equity"], 1e-9) - 1) * 100, 3),
                "position": pos,
                "fills": [
                    {**f, "symbol": f.get("symbol") or self.cfg.market.symbol}
                    for f in self.state["fills"][-30:][::-1]
                ],
                # 持仓历史（最近 30 笔平仓，最新在前）+ 每日已实现盈亏（最近 14 天）
                "position_history": sorted(
                    self.state.get("position_history", [])[-200:],
                    key=lambda x: str(x.get("closed_at") or ""),
                    reverse=True,
                )[:30],
                "daily": self._daily_pnl(),
                "consecutive_losses": self.state["consecutive_losses"],
                "last_decision": self.state.get("last_decision"),
                "auto": self.state.get("auto", {}),
                "halt": bool(self.state.get("halt")),
                "data_source": self.data_source,
                "data_loading": bool(self.data_loading),
                "llm": {"provider": self.cfg.llm.provider, "model": self.cfg.llm.model},
                # 分析师权重校准状态（样本不足时 calibrated=False，权重仍是默认值）
                "analyst_calibration": calibration_summary(self.state.get("analyst_accuracy")),
                # 持仓管理员最近一次判断 + 守护线程状态（手动模式下也能看到退出决策）
                "position_management": getattr(self, "last_management", {}),
                "position_guard": {
                    "running": bool(getattr(self, "_guard_thread", None)
                                    and self._guard_thread.is_alive()),
                    "interval_sec": int(getattr(self, "_guard_interval", 30)),
                },
                "history": hist,
                "last_error": self.last_error,
                "exchange": self.state.get("exchange", "binance"),
                "keys_binance": bool(os.getenv("BINANCE_DEMO_API_KEY")) or bool(os.getenv("BINANCE_API_KEY")),
                "keys_okx": bool(os.getenv("OKX_DEMO_API_KEY")) or bool(os.getenv("OKX_API_KEY")),
                "live_keys": bool(os.getenv("BINANCE_API_KEY")) or bool(os.getenv("OKX_API_KEY")),
                # 模拟盘 / 主网 是四套独立 Key，分开报告，绝不互相回退
                "keys": {
                    "binance": {
                        "demo": bool(os.getenv("BINANCE_DEMO_API_KEY")),
                        "live": bool(os.getenv("BINANCE_API_KEY")),
                    },
                    "okx": {
                        "demo": bool(os.getenv("OKX_DEMO_API_KEY")),
                        "live": bool(os.getenv("OKX_API_KEY")),
                    },
                },
            }

    def set_halt(self, halt: bool) -> dict:
        self.state["halt"] = bool(halt)
        if halt:
            self.stop_auto()
            # A2：触发熔断时立即告警
            self._notify("critical", "已触发熔断开关", "交易已停止，请人工排查后手动解除",
                         {"symbol": self.cfg.market.symbol})
        self._save()
        return {"halt": halt}