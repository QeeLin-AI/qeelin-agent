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

from .agents.pipeline import Decision, TradingPipeline, render_markdown, round_price
from .config import AppConfig
from .data import load_market
from .execution import BinanceFuturesClient, OKXClient, build_client, ExchangeError, read_api_keys
from .notify import get_notifier
from .risk import AccountState


def _today() -> str:
    return time.strftime("%Y-%m-%d")


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
        self.auto_interval = int(self.state.get("auto", {}).get("interval_sec", 300))
        self.auto_execute = bool(self.state.get("auto", {}).get("execute", False))
        self._cycle_lock = threading.Lock()
        # 机会信号排行榜：最近一次全品种扫描结果（供页面展示，扫描是重操作，避免每次轮询重扫）
        self.opportunities: list[dict] = []
        self._rotate_cycle_count = 0

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
        df, src = load_market(
            self.cfg.market, force_download=force, prefer=self.state.get("exchange", "binance")
        )
        with self.lock:
            self.df = df
            self.data_source = src
        return df, src

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
        equity = self._equity(price)
        return AccountState(
            equity=equity,
            peak_equity=max(self.state["peak_equity"], equity),
            day_start_equity=self.state["day_start_equity"],
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

    # ------------------------------------------------------------------
    # 模拟撮合
    # ------------------------------------------------------------------
    def _paper_open(self, side: str, qty: float, price: float, lev: float,
                    stop: float, tp: float, reason: str, plan_price: float | None = None) -> dict:
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
        client.account()  # 验证签名
        self.armed = True
        self.state["mode"] = "live"
        self.state["testnet"] = testnet
        self.state["exchange"] = exchange
        self._save()
        if prev_exchange != exchange:
            self.reload_for_exchange()
        return {"mode": "live", "armed": True, "testnet": testnet, "exchange": exchange}

    def switch_symbol(self, symbol: str) -> dict:
        """切换活跃品种（有持仓时拒绝，避免仓位和行情对不上）。"""
        symbol = symbol.strip().upper()
        if not symbol:
            raise ValueError("品种不能为空")
        if symbol == self.cfg.market.symbol:
            return {"symbol": symbol, "changed": False}
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
                df, _ = load_market(mkt)
                fsig = compute_factor_signal(self.cfg.research.factor_library, df)
                composite = float(fsig.get("composite", 0.0) or 0.0)
                entry["composite"] = round(composite, 4)
                entry["score"] = round(abs(composite), 4)
            except Exception:  # noqa: BLE001 - 单个品种失败不影响整体
                entry["error"] = True
            rows.append(entry)
        rows.sort(key=lambda r: -(r["score"] or 0))
        self.opportunities = rows
        return rows

    def _pick_best_symbol(self, min_score: float | None = None) -> str | None:
        """自主选品种：扫全品种，返回机会分最大且超阈值的品种（无则 None）。"""
        threshold = float(min_score if min_score is not None else self.cfg.market.rotate_min_score)
        rows = self._scan_opportunities()
        if not rows:
            return None
        top = rows[0]
        if top.get("error") or (top["score"] or 0) < threshold:
            return None
        return top["symbol"]

    # ------------------------------------------------------------------
    # 决策与执行
    # ------------------------------------------------------------------
    def run_decision(self, rounds: int = 2) -> dict:
        df = self.df if self.df is not None else self.refresh_data()[0]
        price = float(df["close"].iloc[-1])
        pipe = TradingPipeline(self.cfg)
        # 杠杆上限以交易所支持的最大杠杆为准；未连交易所用配置兜底（默认 3x 保守）
        ex_max = None
        if self.armed and self.live_client is not None:
            try:
                ex_max = float(self.live_client.max_leverage(self.cfg.market.symbol))
            except Exception:  # noqa: BLE001 - 拿不到就退回配置兜底
                ex_max = None
        decision = pipe.run(
            df, state=self.account_state(price), debate_rounds=rounds, persist=False,
            exchange_max_leverage=ex_max, positions=self._positions_for_risk(price),
        )
        self.last_decision = decision

        # A2：决策被风控引擎拒绝时告警（approved=False 且 action=flat 才是真拒绝）
        if not decision.risk_decision["approved"] and decision.risk_decision["action"] == "flat":
            reasons = decision.risk_decision.get("reasons", [])
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

        with self.lock:
            if self.state.get("halt"):
                return {"ok": False, "error": "熔断开关已打开"}
            if self.state.get("position"):
                return {"ok": False, "error": "已有持仓，拒绝重复开仓（请先平仓）"}

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
                    self.state["position"] = {
                        "side": side,
                        "qty": qty,
                        "entry": res.price or price,
                        "leverage": lev,
                        "stop": stop,
                        "tp": tp,
                        "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
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
                            "price": res.price or price,
                            "plan_price": round_price(order.get("entry_ref_price")),
                            "slippage_bps": round(
                                ((res.price or price) - float(order.get("entry_ref_price") or price))
                                / float(order.get("entry_ref_price") or price) * 10_000.0, 2
                            ) if order.get("entry_ref_price") else None,
                            "fee": 0.0,
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
                                       plan_price=order.get("entry_ref_price"))
                self._consume_plan()
                self._save()
                return {"ok": True, "mode": "paper", "fill": rec}

    def _consume_plan(self) -> None:
        """订单计划一旦执行就作废，避免重复下单。"""
        self.pending_order = None
        ld = self.state.get("last_decision")
        if isinstance(ld, dict) and isinstance(ld.get("order"), dict):
            ld["order"]["status"] = "FILLED"

    def close(self, price: float | None = None, reason: str = "manual") -> dict:
        price = price if price is not None else self.price()
        with self.lock:
            pos = self.state.get("position")
            if not pos:
                return {"ok": False, "error": "当前没有持仓"}
            if pos.get("mode") == "live" and self.state["mode"] == "live":
                if not self.armed or self.live_client is None:
                    return {"ok": False, "error": "实盘未解锁"}
                try:
                    self.live_client.cancel_all(self.cfg.market.symbol)
                    res = self.live_client.place_market(
                        self.cfg.market.symbol,
                        "SELL" if pos["side"] == "LONG" else "BUY",
                        float(pos["qty"]),
                        reduce_only=True,
                    )
                    exit_px = res.price or price
                    direction = 1.0 if pos["side"] == "LONG" else -1.0
                    pnl = (float(exit_px) - float(pos["entry"])) * float(pos["qty"]) * direction
                    self._record_close(pos, exit_px, pnl, reason, "live")
                    self.state["position"] = None
                    self._save()
                    self._log_live("CLOSE", pos["side"], pos["qty"], exit_px, res.order_id)
                    # A2：平仓后报告已实现盈亏
                    self._notify("info" if pnl >= 0 else "critical",
                                 f"{pos['side']} 平仓（实盘）",
                                 f"已实现盈亏 {pnl:+,.2f} U（{reason}）",
                                 {"symbol": self.cfg.market.symbol, "pnl": round(pnl, 2), "reason": reason})
                    return {"ok": True, "mode": "live", "order_id": res.order_id}
                except ExchangeError as exc:
                    return {"ok": False, "error": str(exc)}
            rec = self._paper_close(price, reason)
            self._save()
            # A2：平仓后报告已实现盈亏
            self._notify("info" if rec["pnl"] >= 0 else "critical",
                         f"{pos['side']} 平仓（模拟）",
                         f"已实现盈亏 {rec['pnl']:+,.2f} U（{reason}）",
                         {"symbol": self.cfg.market.symbol, "pnl": rec["pnl"], "reason": reason})
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
            payload = self.run_decision()
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