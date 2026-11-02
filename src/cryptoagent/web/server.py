"""本地 Web 服务：零依赖 HTTP 服务器 + JSON API。

只监听 127.0.0.1（默认），没有登录鉴权——这是本机工具，不是公网服务。
任何写操作（下单/解锁）都必须由浏览器显式触发，且服务端再校验一次。
"""
from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..config import AppConfig, update_dotenv
from ..data import fetch_24h, fetch_live_price
from ..engine import TradingEngine
from .. import __version__
from ..execution import ExchangeError
from ..research import ResearchLoop, build_proposer
from ..agents.llm import LLMClient

STATIC_DIR = Path(__file__).resolve().parent / "static"


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class WebApp:
    def __init__(self, cfg: AppConfig, live: bool = False, live_interval: float = 5.0) -> None:
        self.cfg = cfg
        self.engine = TradingEngine(cfg)
        self.research_lock = threading.Lock()
        self.last_research: dict | None = None
        self.last_decision: dict | None = None
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.exchange_env = "testnet"      # 自检探测到的可用环境：testnet | demo
        self.last_scan: dict | None = None
        self.token = os.getenv("CRYPTOAGENT_WEB_TOKEN", "").strip()
        # 重启后自动解锁（只允许假钱环境）。CRYPTOAGENT_AUTO_ARM = <exchange>:<env>，
        # 例如 okx:demo / binance:testnet；没有交易所前缀时默认 binance（兼容旧配置）。
        self.auto_arm = os.getenv("CRYPTOAGENT_AUTO_ARM", "").strip().lower()
        _aa = self.auto_arm.split(":", 1)
        self.auto_arm_exchange = _aa[0] if len(_aa) == 2 else "binance"
        self.auto_arm_env = _aa[-1]
        self.auto_arm_state: dict = {
            "enabled": self.auto_arm_env in ("demo", "testnet"),
            "env": self.auto_arm_env,
            "exchange": self.auto_arm_exchange,
            "ok": False,
            "at": "",
            "attempts": 0,
            "error": "",
        }
        # 自动研究（因子进化）：按 research.auto_interval_sec 定时重跑研究，更新因子库
        self.research_auto_state: dict = {
            "enabled": self.cfg.research.auto_interval_sec > 0,
            "interval_sec": int(self.cfg.research.auto_interval_sec),
            "iterations": int(self.cfg.research.iterations),
            "last_run": "",
            "last_result": "",
            "status": "",
        }
        self._research_auto_stop = threading.Event()
        self._research_auto_thread: threading.Thread | None = None
        # 实时报价（后台线程轮询，单测里 live=False 不联网）
        self.live_price: float | None = None
        self.live_source = ""
        self.live_ts = ""
        self.live_error = ""
        self._live_interval = live_interval
        self.live_change: float | None = None     # 24h 涨跌幅%（顶栏显示）
        self._live_change_at = 0.0
        # 交易所账户视图（实盘/demo 解锁后，总览页改读这里，不再读本地模拟账本）
        self._ex_cache: dict | None = None
        self._ex_cache_at = 0.0
        self._ex_history: list[dict] = []      # 交易所权益曲线（本地账本那条在切模式时会失真）
        self._ex_hist_at = 0.0
        self._ex_pnl_cache: dict | None = None
        self._ex_pnl_at = 0.0
        # 币安 429/418 限流熔断：被 ban 后 5 分钟内不再打签名接口（否则封禁会越拉越长）
        self._ex_banned_until = 0.0
        # 低频数据单独缓存（成交/条件单变化慢，不必跟着账户快照刷）
        self._ex_trades: list | None = None
        self._ex_trades_at = 0.0
        self._ex_orders: list | None = None
        self._ex_orders_at = 0.0
        # 交易所权益曲线落盘：否则每次重启都从「数据积累中」重新开始
        self._ex_hist_file = Path(cfg.runs_dir) / "exchange_equity.json"
        try:
            if self._ex_hist_file.exists():
                self._ex_history = json.loads(self._ex_hist_file.read_text(encoding="utf-8"))[-300:]
        except Exception:  # noqa: BLE001
            self._ex_history = []
        self._live_stop = threading.Event()
        self._live_thread: threading.Thread | None = None
        # 让引擎在自动循环里用实时价（模拟盘止损/成交更贴近真实）
        self.engine.live_price_provider = lambda: self.live_price
        if live:
            self.start_live()
        if live and self.auto_arm_state["enabled"]:
            self.auto_arm_state["error"] = "启动中，等待网络…"
            threading.Thread(target=self._auto_arm_loop, name="auto-arm", daemon=True).start()
        if live and self.research_auto_state["enabled"]:
            self.start_research_auto()

    # -- 实时报价 ------------------------------------------------------
    def start_live(self) -> dict:
        if self._live_thread and self._live_thread.is_alive():
            return self.live_status()
        self._live_stop.clear()
        self._live_thread = threading.Thread(target=self._live_loop, name="live-ticker", daemon=True)
        self._live_thread.start()
        return self.live_status()

    def stop_live(self) -> dict:
        self._live_stop.set()
        return self.live_status()

    def _live_loop(self) -> None:
        while not self._live_stop.is_set():
            try:
                price, src = fetch_live_price(
                    self.cfg.market.symbol, prefer=self.engine.state.get("exchange", "binance")
                )
                self.live_price = price
                self.live_source = src
                self.live_ts = time.strftime("%Y-%m-%d %H:%M:%S")
                self.live_error = ""
            except Exception as exc:  # noqa: BLE001
                self.live_error = str(exc)[:200]
            # 24h 涨跌幅低频刷新（fapi ticker/24hr 权重高，60 秒一次就够）
            now = time.time()
            if now - self._live_change_at >= 60:
                try:
                    _last, chg = fetch_24h(
                        self.cfg.market.symbol, prefer=self.engine.state.get("exchange", "binance")
                    )
                    self.live_change = round(chg, 3)
                    self._live_change_at = now
                except Exception:  # noqa: BLE001
                    pass
            self._live_stop.wait(self._live_interval)

    def live_status(self) -> dict:
        return {
            "price": self.live_price,
            "source": self.live_source,
            "ts": self.live_ts,
            "error": self.live_error,
            "running": bool(self._live_thread and self._live_thread.is_alive()),
            "interval_sec": self._live_interval,
            "change_pct": self.live_change,
        }

    # -- 重启后自动解锁 --------------------------------------------------
    def _auto_arm_loop(self, max_attempts: int = 6) -> None:
        """开机自启时网络往往还没好，所以带退避重试。只对 demo/testnet 生效。"""
        env = self.auto_arm_env
        exchange = self.auto_arm_exchange
        for attempt in range(1, max_attempts + 1):
            self.auto_arm_state["attempts"] = attempt
            try:
                res = self.engine.arm(env, confirm="", testnet=True, exchange=exchange)
                self.auto_arm_state.update(
                    {
                        "ok": True,
                        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "error": "",
                        "balance": res.get("balance"),
                    }
                )
                print(f"[auto-arm] 已自动解锁 {exchange}:{env}，余额 {res.get('balance')}")
                # 注意：这里只自动「解锁交易所」，不自动启动交易循环。
                # 交易循环由用户在前端点「启动」显式开启，避免打开页面就自动下单。
                return
            except Exception as exc:  # noqa: BLE001
                self.auto_arm_state["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
                print(f"[auto-arm] 第 {attempt} 次失败：{self.auto_arm_state['error']}")
                if attempt < max_attempts:
                    time.sleep(min(60, 5 * attempt))
        self.auto_arm_state["at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    def set_auto_arm(self, payload: dict) -> dict:
        """开关「重启后自动解锁」，并写进 .env 持久化。只接受 demo / testnet。"""
        on = bool(payload.get("on", False))
        env = str(payload.get("env", "demo")).strip().lower()
        exchange = str(payload.get("exchange", "binance")).strip().lower()
        if on and env not in ("demo", "testnet"):
            raise ApiError("自动解锁只支持 demo 或 testnet（真钱必须手动确认）", 400)
        if on and exchange not in ("binance", "okx"):
            raise ApiError("自动解锁只支持 binance 或 okx", 400)
        value = f"{exchange}:{env}" if on else "off"
        update_dotenv({"CRYPTOAGENT_AUTO_ARM": value})
        self.auto_arm = value
        self.auto_arm_exchange = exchange
        self.auto_arm_env = env
        self.auto_arm_state["enabled"] = on
        self.auto_arm_state["env"] = env
        self.auto_arm_state["exchange"] = exchange
        if on:
            # 立刻生效一次，不用等重启
            try:
                res = self.engine.arm(env, confirm="", testnet=True, exchange=exchange)
                self.auto_arm_state.update(
                    {"ok": True, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "error": "", "balance": res.get("balance")}
                )
                self.exchange_env = f"{exchange}:{env}"
            except Exception as exc:  # noqa: BLE001
                self.auto_arm_state["ok"] = False
                self.auto_arm_state["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return {"auto_arm": self.auto_arm_state, "saved": value}

    # ------------------------------------------------------------------
    def status(self) -> dict:
        # 有实时价就传给引擎，权益/浮盈亏/权益曲线都按实时价算
        st = self.engine.status(price_override=self.live_price)
        st["live"] = self.live_status()
        st["auto_arm"] = self.auto_arm_state
        st["research_auto"] = self.research_auto_state
        st["risk"] = self.risk_config()
        st["account_view"] = "local"
        st["account_error"] = ""
        # 解锁后总览页要显示交易所的真实账户，而不是本地模拟账本（否则永远是 10000 空仓）
        if self.engine.armed and self.engine.state.get("mode") == "live":
            st["account_view"] = "exchange"
            try:
                snap = self.exchange_snapshot()
                st.update(snap["overlay"])
                st["history"] = snap["history"]
                st["account_error"] = snap["error"]
            except Exception as exc:  # noqa: BLE001
                # 拉不到就直说原因，别静默退回本地数字（那个数字会骗人）
                st["account_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        st["server"] = {
            "started_at": self.started_at,
            "data_source": self.engine.data_source or "未加载",
            "llm_provider": self.cfg.llm.provider,
            "version": __version__,
        }
        return st

    # -- 交易所账户视图 --------------------------------------------------
    def exchange_snapshot(self, ttl: float = 20.0) -> dict:
        """交易所真实余额/持仓/成交，覆盖总览页的本地账本字段。

        币安签名接口有 IP 级权重上限（2400/min），status 每几秒就被轮询一次，
        不缓存会把权重刷爆、IP 被 429/418 封禁。所以：
        - 账户/持仓快照：20 秒缓存
        - 成交(userTrades)/条件单(openAlgoOrders)：60 秒缓存
        - 资金流水(income)：300 秒缓存
        被限流后熔断 5 分钟，期间不再打签名接口。
        """
        now = time.time()
        # 限流熔断：有旧数据就用旧的（标注一下），没有才报错
        if now < self._ex_banned_until:
            if self._ex_cache is not None:
                snap = dict(self._ex_cache)
                snap["error"] = "交易所接口被限流（429/418），已暂停请求，约 5 分钟后自动恢复"
                return snap
            raise RuntimeError("交易所接口被限流（429/418），请稍候几分钟再试")

        if self._ex_cache is not None and (now - self._ex_cache_at) < ttl:
            return self._ex_cache

        client = self.engine.live_client
        if client is None:
            raise RuntimeError("还没连接交易所（armed 状态丢失，请重新解锁）")
        symbol = self.cfg.market.symbol

        try:
            acct = client.account()
            positions = client.positions(symbol)
        except ExchangeError as exc:
            msg = str(exc)
            if "418" in msg or "429" in msg or "-1003" in msg or "banned" in msg.lower():
                self._ex_banned_until = now + 300.0
            raise

        # 成交：低频缓存 60 秒
        if self._ex_trades is None or (now - self._ex_trades_at) >= 60:
            try:
                self._ex_trades = [{**t, "symbol": symbol} for t in client.trades(symbol, limit=30)]
                self._ex_trades_at = now
            except Exception:  # noqa: BLE001 - 成交拿不到不影响余额
                pass
        trades = self._ex_trades or []

        # 条件单：只有有持仓才查，60 秒缓存
        if positions and (self._ex_orders is None or (now - self._ex_orders_at) >= 60):
            try:
                self._ex_orders = client.open_algo_orders(symbol)
                self._ex_orders_at = now
            except Exception:  # noqa: BLE001
                pass

        # 持仓：本地结构（side/qty/entry/mark/unrealized/leverage/stop/tp/mode）
        position = None
        if positions:
            p = positions[0]
            stop = tp = None
            for o in self._ex_orders or []:
                try:
                    price = float(o.get("triggerPrice") or o.get("stopPrice") or 0) or None
                except (TypeError, ValueError):
                    price = None
                if not price:
                    continue
                # 按触发价相对开仓价判断：低于开仓价=止损，高于=止盈（做空反之），
                # 不能用当前浮盈浮亏判断，否则浮亏时止损单会被误标成止盈。
                if p["side"] == "LONG":
                    if price < p["entry"]:
                        stop = price
                    elif price > p["entry"]:
                        tp = price
                else:
                    if price > p["entry"]:
                        stop = price
                    elif price < p["entry"]:
                        tp = price
            position = {
                "symbol": symbol,
                "side": p["side"],
                "qty": p["qty"],
                "entry": p["entry"],
                "mark": p["mark"],
                "unrealized": round(p["unrealized"], 4),
                "notional": round(p["qty"] * p["mark"], 2),
                "leverage": p["leverage"],
                "stop": stop,
                "tp": tp,
                "mode": "模拟" if self.engine.state.get("testnet", True) else "实盘",
            }

        equity = round(acct["balance"] + acct["unrealized_pnl"], 4)
        # 权益曲线：每 30 秒记一个点，切换模式后不要把本地曲线接上去
        if not self._ex_history or (now - self._ex_hist_at) >= 30:
            self._ex_hist_at = now
            self._ex_history.append(
                {"ts": time.strftime("%m-%d %H:%M", ), "equity": round(equity, 2), "price": self.live_price}
            )
            self._ex_history = self._ex_history[-300:]
            try:
                self._ex_hist_file.parent.mkdir(parents=True, exist_ok=True)
                self._ex_hist_file.write_text(
                    json.dumps(self._ex_history[-300:], ensure_ascii=False), encoding="utf-8"
                )
            except OSError:
                pass

        overlay = {
            "equity": round(equity, 2),
            "cash_equity": round(acct["available"], 2),
            "wallet_balance": round(acct["balance"], 2),
            "unrealized_pnl": round(acct["unrealized_pnl"], 2),
            "position": position,
            "fills": trades,
            "peak_equity": round(max([h["equity"] for h in self._ex_history] + [equity]), 2),
            "drawdown_pct": round(
                (equity / max(max([h["equity"] for h in self._ex_history] + [equity]), 1e-9) - 1) * 100, 3
            ),
        }
        # 当日盈亏：用资金流水汇总，60 秒缓存（income 权重 30，不适合跟着轮询）
        try:
            pnl = self.exchange_day_pnl(client)
            overlay["day_pnl_pct"] = pnl["pct"]
            overlay["day_pnl_abs"] = pnl["abs"]
        except Exception:  # noqa: BLE001
            overlay["day_pnl_pct"] = None
            overlay["day_pnl_abs"] = None

        snap = {"overlay": overlay, "history": self._ex_history, "error": ""}
        self._ex_cache = snap
        self._ex_cache_at = now
        return snap

    def exchange_day_pnl(self, client, ttl: float = 300.0) -> dict:
        """当日盈亏 = 今日资金流水里的交易类收入（排除划转）。"""
        now = time.time()
        if self._ex_pnl_cache is not None and (now - self._ex_pnl_at) < ttl:
            return self._ex_pnl_cache

        # 币安按 UTC 日界结算；epoch 本身就是 UTC 基准，直接按整天取整
        start = int(time.time() // 86400) * 86400
        rows = client.income(start * 1000, limit=1000)
        skip = {"TRANSFER", "INTERNAL_TRANSFER", "INSURANCE_CLEAR", "CROSS_COLLATERAL_TRANSFER"}
        total = 0.0
        for r in rows or []:
            if str(r.get("incomeType", "")).upper() in skip:
                continue
            try:
                total += float(r.get("income", 0.0))
            except (TypeError, ValueError):
                continue
        acct = client.account()
        base = acct["balance"] - total
        pct = (total / base * 100) if base else 0.0
        self._ex_pnl_cache = {"abs": round(total, 4), "pct": round(pct, 3)}
        self._ex_pnl_at = now
        return self._ex_pnl_cache

    def refresh_data(self) -> dict:
        """强制重新下载行情并刷新引擎里的 df（「刷新行情」按钮用）。"""
        df, src = self.engine.refresh_data(force=True)
        return {
            "rows": int(len(df)),
            "source": src,
            "first": str(df.index[0]),
            "last": str(df.index[-1]),
            "last_close": round(float(df["close"].iloc[-1]), 2),
        }

    def klines(self, interval: str = "", limit: int = 240) -> dict:
        """实时 K 线：直接问交易所，支持 5m/15m/1h 等周期（本地 CSV 只有一种周期）。"""
        from ..data import DataError, fetch_klines

        interval = (interval or self.cfg.market.interval).strip()
        limit = max(20, min(int(limit), 1000))
        symbol = self.cfg.market.symbol
        try:
            df, src = fetch_klines(
                symbol, interval, limit, prefer=self.engine.state.get("exchange", "binance")
            )
        except DataError as exc:
            raise ApiError(str(exc), 502) from exc
        return {
            "symbol": symbol,
            "interval": interval,
            "source": src,
            "bars": [
                {
                    "t": int(ts.timestamp()),
                    "o": round(float(o), 2),
                    "h": round(float(h), 2),
                    "l": round(float(lo), 2),
                    "c": round(float(c), 2),
                    "v": round(float(v), 2),
                }
                for ts, o, h, lo, c, v in zip(
                    df.index, df["open"], df["high"], df["low"], df["close"], df["volume"]
                )
            ],
        }

    def candles(self, limit: int = 240) -> dict:
        df = self.engine.df
        if df is None:
            df, _ = self.engine.refresh_data()
        tail = df.tail(max(20, min(limit, 1500)))
        return {
            "symbol": self.cfg.market.symbol,
            "interval": self.cfg.market.interval,
            "bars": [
                {
                    "t": int(ts.timestamp()),
                    "o": round(float(o), 2),
                    "h": round(float(h), 2),
                    "l": round(float(lo), 2),
                    "c": round(float(c), 2),
                    "v": round(float(v), 2),
                }
                for ts, o, h, lo, c, v in zip(
                    tail.index, tail["open"], tail["high"], tail["low"], tail["close"], tail["volume"]
                )
            ],
        }

    def decision(self, rounds: int = 2) -> dict:
        if self.engine.state.get("halt"):
            raise ApiError("熔断开关已打开，禁止运行决策", 409)
        data = self.engine.run_decision(rounds=rounds)
        self.last_decision = data
        return data

    @staticmethod
    def _rows_from_history(history: list) -> list:
        """把研究循环的 history 压成排行榜表格行（前端直接用）。"""
        rows = []
        for ev in history or []:
            prop = ev.get("proposal") or {}
            if not ev.get("ok"):
                rows.append(
                    {
                        "name": prop.get("name"),
                        "family": prop.get("family"),
                        "ok": False,
                        "error": (ev.get("error") or "")[:120],
                    }
                )
                continue
            train = ev.get("train") or {}
            valid = ev.get("valid") or {}
            rows.append(
                {
                    "name": prop.get("name"),
                    "family": prop.get("family"),
                    "ok": True,
                    "hypothesis": prop.get("hypothesis"),
                    "is_sharpe": round(train.get("sharpe", 0.0), 3),
                    "oos_sharpe": round(valid.get("sharpe", 0.0), 3),
                    "oos_dd": round(valid.get("max_drawdown", 0.0) * 100, 2),
                    "score": round(ev["score"], 3) if ev.get("score") is not None else None,
                    "accepted": bool(ev.get("accepted")),
                    "reject": ev.get("reject_reason"),
                }
            )
        return rows

    def research(self, iterations: int = 6) -> dict:
        if not self.research_lock.acquire(blocking=False):
            raise ApiError("上一轮研究还在跑，请稍候", 409)
        try:
            return self._run_research_once(iterations)
        finally:
            self.research_lock.release()

    def _run_research_once(self, iterations: int = 6) -> dict:
        """跑一轮研究并更新 last_research（调用方需已持有 research_lock）。"""
        df = self.engine.df
        if df is None:
            df, _ = self.engine.refresh_data()
        # 让 LLM 真正参与因子提议与优化；LLM 不可用/输出非法时回退到模板提议器
        proposer = None
        try:
            llm = LLMClient(self.cfg.llm)
            proposer = build_proposer(self.cfg, llm, verbose=False)
        except Exception:  # noqa: BLE001 - 研究不能因 LLM 初始化失败而中断
            proposer = None
        loop = ResearchLoop(self.cfg, df, proposer=proposer, verbose=False)
        report = loop.run(max(1, min(iterations, 30)))
        rows = self._rows_from_history(report["history"])
        self.last_research = {
            "generated_at": report["generated_at"],
            "accepted": report["accepted"],
            "iterations": report["iterations"],
            "best": report["best"]["proposal"] if report.get("best") else None,
            "holdout": report.get("holdout"),
            "rows": rows,
            "report_path": str((Path(self.cfg.research.out_dir) / "report.md").resolve()),
        }
        self._record_live_performance(report)
        return self.last_research

    def _record_live_performance(self, report: dict) -> None:
        """复盘：把实盘（模拟盘）累计盈亏快照记进 runs/research/live_performance.json。

        这是「实盘盈亏回灌」的数据源——等模拟盘跑出足够多成交，就能拿它做因子归因。
        """
        try:
            p = Path(self.cfg.research.out_dir) / "live_performance.json"
            data = {}
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
            fills = self.engine.state.get("fills", [])
            realized = sum(float(f.get("pnl") or 0.0) for f in fills)
            holdout = report.get("holdout") or {}
            rec = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "realized_pnl": round(realized, 4),
                "equity": round(float(self.engine.state.get("equity", 0.0)), 2),
                "fills_count": len(fills),
                "best_factor": (report.get("best") or {}).get("proposal", {}).get("name"),
                "best_holdout": holdout.get("sharpe"),
            }
            hist = data.setdefault("history", [])
            hist.append(rec)
            data["history"] = hist[-500:]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001 - 复盘记录失败不影响主流程
            pass

    # -- 自动研究（因子自动进化） ----------------------------------------
    def start_research_auto(self, interval_sec: int | None = None, iterations: int | None = None) -> dict:
        if interval_sec is not None:
            self.research_auto_state["interval_sec"] = max(300, int(interval_sec))
        if iterations is not None:
            self.research_auto_state["iterations"] = max(1, min(int(iterations), 30))
        self.research_auto_state["enabled"] = True
        if self._research_auto_thread and self._research_auto_thread.is_alive():
            return self.research_auto_state
        self._research_auto_stop.clear()
        self._research_auto_thread = threading.Thread(
            target=self._research_auto_loop, name="research-auto", daemon=True
        )
        self._research_auto_thread.start()
        return self.research_auto_state

    def stop_research_auto(self) -> dict:
        self.research_auto_state["enabled"] = False
        self._research_auto_stop.set()
        return self.research_auto_state

    def _research_auto_loop(self) -> None:
        while not self._research_auto_stop.is_set():
            interval = max(300, int(self.research_auto_state.get("interval_sec", 0) or 0))
            if interval <= 0:
                break
            try:
                if self.research_lock.acquire(blocking=False):
                    try:
                        self.research_auto_state["status"] = "running"
                        self.research_auto_state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        self._run_research_once(int(self.research_auto_state.get("iterations", 6)))
                        self.research_auto_state["status"] = "ok"
                        self.research_auto_state["last_result"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    finally:
                        self.research_lock.release()
                else:
                    self.research_auto_state["status"] = "跳过：上一轮还在跑"
            except Exception as exc:  # noqa: BLE001
                self.research_auto_state["status"] = f"error: {type(exc).__name__} {str(exc)[:120]}"
            self._research_auto_stop.wait(interval)

    def set_research_auto(self, payload: dict) -> dict:
        """开关自动研究并写进 config（重启后仍生效）。"""
        from ..config import config_path, save_config

        on = bool(payload.get("on", False))
        interval = int(payload.get("interval_sec", 86400))
        self.cfg.research.auto_interval_sec = max(300, interval) if on else 0
        if "iterations" in payload:
            self.cfg.research.iterations = max(1, min(int(payload["iterations"]), 30))
        try:
            save_config(self.cfg, config_path())
            saved = True
        except OSError:
            saved = False
        if on:
            self.start_research_auto(interval, payload.get("iterations"))
        else:
            self.stop_research_auto()
        return {"research_auto": self.research_auto_state, "saved": saved}

    def latest_research(self) -> dict | None:
        if self.last_research:
            return self.last_research
        path = Path(self.cfg.research.out_dir) / "leaderboard.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return {
                    "generated_at": data.get("generated_at"),
                    "accepted": data.get("accepted"),
                    "iterations": data.get("iterations"),
                    "best": (data.get("best") or {}).get("proposal"),
                    "holdout": data.get("holdout"),
                    "rows": self._rows_from_history(data.get("history") or []),
                    "report_path": str((Path(self.cfg.research.out_dir) / "report.md").resolve()),
                }
            except Exception:  # noqa: BLE001
                return None
        return None

    def latest_decision(self) -> dict | None:
        """最近一次决策的完整结构化结果（含 markdown），供页面打开时回填。"""
        if self.last_decision:
            return self.last_decision
        d = Path(self.cfg.runs_dir) / "decisions"
        files = sorted(d.glob("*.json"))
        for f in reversed(files[-5:]):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if isinstance(data, dict) and data.get("analysts"):
                return data
        return None

    def exchange_test(self, exchange: str = "binance", mode: str | None = None) -> dict:
        """连接自检：探测所选交易所的模拟盘 / 主网，报告哪个通、延迟、余额、精度。只读。

        mode 指定只测 'demo' 或 'live' 一套 Key；不传则向后兼容：demo 优先、再 live。
        模拟盘与主网是两套独立 Key，按环境分别读取。
        """
        from ..execution import OKXClient, BinanceFuturesClient, read_api_keys

        exchange = (exchange or "binance").lower()
        probe: dict[str, str] = {}
        if mode == "live":
            envs = ["live"]
        elif mode == "demo":
            envs = ["demo"]
        elif exchange == "okx":
            envs = ["demo", "live"]
        else:
            envs = ["demo", "testnet"]

        if exchange == "okx":
            for env in envs:
                testnet = env != "live"
                key, secret, passphrase = read_api_keys("okx", testnet=testnet)
                if not key or not secret:
                    probe[env] = "未设置 OKX " + ("模拟盘" if testnet else "主网") + " API Key"
                    continue
                try:
                    t0 = time.time()
                    client = OKXClient(key, secret, passphrase, env=env, timeout=8)
                    client.ping()
                    latency = int((time.time() - t0) * 1000)
                    st = client.server_time()
                    acct = client.account()
                    tick, step, _ = client.filters(self.cfg.market.symbol)
                    self.exchange_env = f"okx:{env}"
                    return {
                        "ok": True,
                        "stage": "done",
                        "exchange": "okx",
                        "env": env,
                        "base": client.base,
                        "latency_ms": latency,
                        "server_time": st,
                        "balance": acct["balance"],
                        "available": acct["available"],
                        "filters": {"tick": tick, "step": step, "min_notional": 5.0},
                        "probe": probe,
                    }
                except Exception as exc:  # noqa: BLE001
                    probe[env] = f"{type(exc).__name__}: {str(exc)[:160]}"
            return {
                "ok": False,
                "stage": "network" if any(probe.values()) else "keys",
                "exchange": "okx",
                "probe": probe,
                "error": "OKX 模拟盘/主网都连不上（多半是网络被墙，需要代理）"
                if any(probe.values())
                else "未设置 OKX 模拟盘/主网 API Key（可在本页填，或写进 .env 后重启）",
            }

        # 币安路径
        for env in envs:
            testnet = env != "live"
            key, secret, _ = read_api_keys("binance", testnet=testnet)
            if not key or not secret:
                probe[env] = "未设置币安 " + ("Demo" if testnet else "主网") + " API Key"
                continue
            try:
                t0 = time.time()
                client = BinanceFuturesClient(key, secret, env=env, timeout=8)
                client.ping()
                latency = int((time.time() - t0) * 1000)
                st = client.server_time()
                acct = client.account()
                pos = client.positions(self.cfg.market.symbol)
                tick, step, min_notional = client.filters(self.cfg.market.symbol)
                self.exchange_env = env
                return {
                    "ok": True,
                    "stage": "done",
                    "exchange": "binance",
                    "env": env,
                    "base": client.base,
                    "latency_ms": latency,
                    "server_time": st,
                    "balance": acct["balance"],
                    "available": acct["available"],
                    "positions": pos,
                    "filters": {"tick": tick, "step": step, "min_notional": min_notional},
                    "probe": probe,
                }
            except Exception as exc:  # noqa: BLE001
                probe[env] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return {
            "ok": False,
            "stage": "network" if any(probe.values()) else "keys",
            "exchange": "binance",
            "probe": probe,
            "error": "测试网/Demo 和主网都连不上（多半是网络被墙，需要代理）"
            if any(probe.values())
            else "未设置币安模拟盘/主网 API Key（可在本页填，或写进 .env 后重启）",
        }

    # -- 多品种 ---------------------------------------------------------
    def symbols(self) -> list[dict]:
        """配置里列出的品种 + 本地缓存情况。"""
        cache_dir = Path(self.cfg.market.cache_dir)
        stem = f"{self.cfg.market.interval}_{self.cfg.market.bars}"
        out = []
        for sym in self.cfg.market.symbols:
            real = cache_dir / f"{sym}_{stem}.real.csv"
            synth = cache_dir / f"{sym}_{stem}_s{self.cfg.market.seed}.synthetic.csv"
            entry = {
                "symbol": sym,
                "active": sym == self.cfg.market.symbol,
                "cached": real.exists(),
                "synthetic_only": (not real.exists()) and synth.exists(),
                "last": None,
                "size_kb": round(real.stat().st_size / 1024) if real.exists() else 0,
            }
            if real.exists():
                try:
                    with real.open("r", encoding="utf-8") as fh:
                        last = fh.readlines()[-1]
                    entry["last"] = last.split(",")[0][:16]
                except Exception:  # noqa: BLE001
                    pass
            out.append(entry)
        return out

    def set_symbol(self, payload: dict) -> dict:
        symbol = str(payload.get("symbol", "")).strip().upper()
        if not symbol:
            raise ApiError("symbol 不能为空", 400)
        try:
            res = self.engine.switch_symbol(symbol)
        except ValueError as exc:
            raise ApiError(str(exc), 409) from exc
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"切换失败：{str(exc)[:200]}", 502) from exc
        # 实时价是上一个品种的，清掉免得顶栏显示错价
        self.live_price = None
        self.live_ts = ""
        # 换了品种，交易所视图和权益曲线都得重来
        self.reset_exchange_view()
        return res

    def invalidate_exchange_cache(self) -> None:
        """只让下一次 status 重新拉数（下单/平仓后调用，曲线保留）。"""
        self._ex_cache = None
        self._ex_cache_at = 0.0
        self._ex_pnl_cache = None
        self._ex_pnl_at = 0.0

    def reset_exchange_view(self) -> None:
        """连权益曲线一起清（切品种 / 切回模拟盘时调用）。"""
        self.invalidate_exchange_cache()
        self._ex_history = []
        self._ex_hist_at = 0.0
        try:
            self._ex_hist_file.unlink(missing_ok=True)
        except OSError:
            pass

    def scan(self, payload: dict) -> dict:
        """把所有品种各跑一遍决策（纯本地、不下单），出一张对比表。"""
        from dataclasses import replace

        from ..agents.pipeline import TradingPipeline
        from ..data import load_market

        wanted = payload.get("symbols") or self.cfg.market.symbols
        rows = []
        for sym in wanted:
            sym = str(sym).strip().upper()
            try:
                mkt = replace(self.cfg.market, symbol=sym)
                df, src = load_market(mkt)
                price = float(df["close"].iloc[-1])
                sub = replace(self.cfg, market=mkt)
                decision = TradingPipeline(sub).run(
                    df,
                    state=self.engine.account_state(price),
                    debate_rounds=1,
                    persist=False,
                )
                rd = decision.risk_decision
                rows.append(
                    {
                        "symbol": sym,
                        "ok": True,
                        "price": round(price, 4),
                        "action": rd.get("action"),
                        "size_pct": round(float(rd.get("size_pct", 0.0)), 4),
                        "leverage": rd.get("leverage"),
                        "approved": bool(rd.get("approved")),
                        "reasons": (rd.get("reasons") or [])[:2],
                        "order": decision.order,
                        "data_source": src,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                rows.append({"symbol": sym, "ok": False, "error": f"{type(exc).__name__}: {str(exc)[:140]}"})
        rows.sort(key=lambda r: (not r.get("ok"), -(r.get("size_pct") or 0)))
        self.last_scan = {"rows": rows, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        return self.last_scan

    # -- 交易参数 --------------------------------------------------------
    def risk_config(self) -> dict:
        r = self.cfg.risk
        return {
            "leverage_override": r.leverage_override,
            "max_leverage": r.max_leverage,
            "max_position_pct": r.max_position_pct,
            "max_loss_per_trade_pct": r.max_loss_per_trade_pct,
            "min_confidence": r.min_confidence,
            "max_drawdown_pct": r.max_drawdown_pct,
            "daily_loss_limit_pct": r.daily_loss_limit_pct,
        }

    def set_risk(self, payload: dict) -> dict:
        """改杠杆/风险上限并写进 config/default.json（重启后仍生效）。"""
        from ..config import config_path, save_config

        r = self.cfg.risk
        if "leverage_override" in payload:
            try:
                lev = float(payload["leverage_override"])
            except (TypeError, ValueError) as exc:
                raise ApiError("leverage_override 必须是数字", 400) from exc
            if lev < 0 or lev > 125:
                raise ApiError("杠杆只能在 0（自动）到 125 之间", 400)
            r.leverage_override = lev
            # 不再抬 max_leverage：它只是「未连交易所时的保守兜底」，
            # 实际杠杆上限由交易所支持的最大杠杆决定（连上交易所就按交易所的来）
        if "max_loss_per_trade_pct" in payload:
            try:
                v = float(payload["max_loss_per_trade_pct"])
            except (TypeError, ValueError) as exc:
                raise ApiError("max_loss_per_trade_pct 必须是数字", 400) from exc
            if not (0.001 <= v <= 0.05):
                raise ApiError("单笔风险上限只能在 0.1% ~ 5% 之间", 400)
            r.max_loss_per_trade_pct = v
        if "max_position_pct" in payload:
            try:
                v = float(payload["max_position_pct"])
            except (TypeError, ValueError) as exc:
                raise ApiError("max_position_pct 必须是数字", 400) from exc
            if not (0.01 <= v <= 1.0):
                raise ApiError("仓位上限只能在 1% ~ 100% 之间", 400)
            r.max_position_pct = v
        if "daily_loss_limit_pct" in payload:
            try:
                v = float(payload["daily_loss_limit_pct"])
            except (TypeError, ValueError) as exc:
                raise ApiError("daily_loss_limit_pct 必须是数字", 400) from exc
            if not (0.01 <= v <= 0.30):
                raise ApiError("当日亏损上限只能在 1% ~ 30% 之间", 400)
            r.daily_loss_limit_pct = v
        if "max_drawdown_pct" in payload:
            try:
                v = float(payload["max_drawdown_pct"])
            except (TypeError, ValueError) as exc:
                raise ApiError("max_drawdown_pct 必须是数字", 400) from exc
            if not (0.05 <= v <= 0.50):
                raise ApiError("最大回撤只能在 5% ~ 50% 之间", 400)
            r.max_drawdown_pct = v
        try:
            save_config(self.cfg, config_path())
            saved = True
        except OSError:
            saved = False          # 只读文件系统：内存里仍然生效
        return {"risk": self.risk_config(), "saved": saved}

    def opportunities(self, payload: dict) -> dict:
        """机会信号排行榜：返回最近一次全品种扫描结果；refresh=true 时强制重扫。"""
        rows = self.engine.opportunities
        if payload.get("refresh") or not rows:
            rows = self.engine._scan_opportunities()
        return {
            "rows": rows,
            "auto_rotate": bool(self.cfg.market.auto_rotate),
            "min_score": float(self.cfg.market.rotate_min_score),
            "every_cycles": int(self.cfg.market.rotate_every_cycles),
            "current": self.cfg.market.symbol,
        }

    def set_rotate(self, payload: dict) -> dict:
        """改自主选品种的开关/阈值/频率，并写进 config/default.json。"""
        from ..config import config_path, save_config

        m = self.cfg.market
        if "auto_rotate" in payload:
            m.auto_rotate = bool(payload["auto_rotate"])
        if "min_score" in payload:
            try:
                v = float(payload["min_score"])
            except (TypeError, ValueError) as exc:
                raise ApiError("min_score 必须是数字", 400) from exc
            if not (0.0 <= v <= 1.0):
                raise ApiError("信号阈值只能在 0 ~ 1 之间", 400)
            m.rotate_min_score = v
        if "every_cycles" in payload:
            try:
                v = int(payload["every_cycles"])
            except (TypeError, ValueError) as exc:
                raise ApiError("every_cycles 必须是整数", 400) from exc
            if not (1 <= v <= 100):
                raise ApiError("扫描频率只能在 1 ~ 100 轮之间", 400)
            m.rotate_every_cycles = v
        try:
            save_config(self.cfg, config_path())
            saved = True
        except OSError:
            saved = False
        return {
            "auto_rotate": bool(m.auto_rotate),
            "min_score": float(m.rotate_min_score),
            "every_cycles": int(m.rotate_every_cycles),
            "saved": saved,
        }

    def save_keys(self, payload: dict) -> dict:
        """把所选交易所、所选环境（demo/live）的 Key 存进项目根 .env（不回显、不落日志），然后立刻自检。

        模拟盘与主网用两套独立 Key，分开存储、互不覆盖：
        - 币安  demo → BINANCE_DEMO_* / live → BINANCE_*
        - OKX   demo → OKX_DEMO_*     / live → OKX_*
        """
        exchange = str(payload.get("exchange", "binance")).strip().lower()
        if exchange not in ("binance", "okx"):
            raise ApiError("exchange 只能是 binance / okx", 400)
        mode = str(payload.get("mode", "demo")).strip().lower()
        if mode not in ("demo", "live"):
            raise ApiError("mode 只能是 demo / live", 400)
        key = str(payload.get("key", "")).strip()
        secret = str(payload.get("secret", "")).strip()
        passphrase = str(payload.get("passphrase", "")).strip()
        if not key or not secret:
            raise ApiError("API Key / Secret 都不能为空", 400)
        if len(key) < 20 or len(secret) < 20:
            raise ApiError("Key 或 Secret 长度不对，检查是否复制完整", 400)
        if exchange == "okx" and len(passphrase) < 4:
            raise ApiError("OKX 需要 Passphrase（开户时设置的，不是登录密码）", 400)

        if exchange == "okx":
            env_keys = (
                {"OKX_DEMO_API_KEY": key, "OKX_DEMO_API_SECRET": secret, "OKX_DEMO_PASSPHRASE": passphrase}
                if mode == "demo"
                else {"OKX_API_KEY": key, "OKX_API_SECRET": secret, "OKX_PASSPHRASE": passphrase}
            )
        else:
            env_keys = (
                {"BINANCE_DEMO_API_KEY": key, "BINANCE_DEMO_API_SECRET": secret}
                if mode == "demo"
                else {"BINANCE_API_KEY": key, "BINANCE_API_SECRET": secret}
            )
        update_dotenv(env_keys)
        res = self.exchange_test(exchange, mode)
        res["saved"] = True
        res["key_tail"] = key[-4:]
        res["exchange"] = exchange
        res["mode"] = mode
        return res

    def llm_settings(self) -> dict:
        """读当前 LLM 配置（api_key 脱敏，只回显尾号）。"""
        c = self.cfg.llm
        key = os.getenv(c.api_key_env, "")
        return {
            "provider": c.provider,
            "model": c.model,
            "base_url": c.base_url,
            "api_key_env": c.api_key_env,
            "api_key_set": bool(key),
            "api_key_tail": key[-4:] if key else "",
            "temperature": c.temperature,
            "max_tokens": c.max_tokens,
        }

    def set_llm_settings(self, payload: dict) -> dict:
        """写 LLM 配置到 .env（provider/model/base_url/api_key），同步内存配置，下次决策即时生效。"""
        updates: dict[str, str] = {}
        provider = str(payload.get("provider", "")).strip()
        model = str(payload.get("model", "")).strip()
        base_url = str(payload.get("base_url", "")).strip()
        api_key = str(payload.get("api_key", "")).strip()
        if provider:
            updates["CRYPTOAGENT_LLM_PROVIDER"] = provider
        if model:
            updates["CRYPTOAGENT_LLM_MODEL"] = model
        if base_url:
            updates["CRYPTOAGENT_LLM_BASE_URL"] = base_url
        if api_key:
            updates[self.cfg.llm.api_key_env] = api_key
        if not updates:
            raise ApiError("没有需要保存的配置", 400)
        update_dotenv(updates)
        # 同步内存配置：TradingPipeline 每次决策时用 self.cfg.llm 新建，改这里即时生效
        if "CRYPTOAGENT_LLM_PROVIDER" in updates:
            self.cfg.llm.provider = updates["CRYPTOAGENT_LLM_PROVIDER"]
        if "CRYPTOAGENT_LLM_MODEL" in updates:
            self.cfg.llm.model = updates["CRYPTOAGENT_LLM_MODEL"]
        if "CRYPTOAGENT_LLM_BASE_URL" in updates:
            self.cfg.llm.base_url = updates["CRYPTOAGENT_LLM_BASE_URL"]
        return self.llm_settings()

    def arm(self, payload: dict) -> dict:
        mode = str(payload.get("mode", "paper"))
        confirm = str(payload.get("confirm", ""))
        testnet = bool(payload.get("testnet", True))
        exchange = str(payload.get("exchange", "binance")).strip().lower()
        # 老期货测试网（testnet.binancefuture.com）已并入 Demo Trading，共享 CDN 出口 IP
        # 极容易被限流封禁（418）。「模拟盘」一律走 demo，不再碰老测试网。
        if mode == "testnet":
            mode = "demo"
        try:
            return self.engine.arm(mode, confirm, testnet=testnet, exchange=exchange)
        except PermissionError as exc:
            raise ApiError(str(exc), 403) from exc
        except ExchangeError as exc:
            raise ApiError(str(exc), 502) from exc

    def exchange_status(self) -> dict:
        import os

        e = self.engine
        keys_binance = bool(os.getenv("BINANCE_API_KEY"))
        keys_okx = bool(os.getenv("OKX_API_KEY"))
        out = {
            "exchange": e.state.get("exchange", "binance"),
            "keys_binance": keys_binance,
            "keys_okx": keys_okx,
            "keys_present": keys_binance or keys_okx,
            "keys_binance_demo": bool(os.getenv("BINANCE_DEMO_API_KEY")) or keys_binance,
            "keys_okx_demo": bool(os.getenv("OKX_DEMO_API_KEY")) or keys_okx,
            "keys_binance_live": keys_binance,
            "keys_okx_live": keys_okx,
            "armed": e.armed,
            "mode": e.state["mode"],
            "testnet": e.state.get("testnet", True),
            "connected": False,
            "account": None,
            "positions": [],
            "error": "",
        }
        if not (keys_binance or keys_okx) or not e.armed:
            out["error"] = "" if (keys_binance or keys_okx) else "未设置 API Key"
            return out
        try:
            client = e.live_client or e.connect(
                testnet=e.state.get("testnet", True), exchange=e.state.get("exchange", "binance")
            )
            client.armed = True
            client.ping()
            out["connected"] = True
            out["account"] = client.account()
            out["positions"] = client.positions(self.cfg.market.symbol)
        except ExchangeError as exc:
            out["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"{type(exc).__name__}: {exc}"
        return out


class Handler(BaseHTTPRequestHandler):
    server_version = "QeeLin-Agent/0.2"
    app: WebApp  # 由 serve() 注入

    # -- 基础 ---------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return  # 静默，避免刷屏

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"请求体不是合法 JSON: {exc}", 400) from exc

    # -- 鉴权 ---------------------------------------------------------
    def _token(self) -> str:
        return getattr(self.app, "token", "") or ""

    def _auth_ok(self, query: dict | None = None) -> bool:
        """没设 token 就放行（只监听 127.0.0.1 的本地用法）。

        设了 token 时接受三种携带方式：Authorization: Bearer、Cookie、?token=。
        """
        token = self._token()
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer ") and header[7:].strip() == token:
            return True
        cookie = self.headers.get("Cookie", "") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "ca_token" and v == token:
                return True
        if query and query.get("token", [""])[0] == token:
            return True
        return False

    def _deny(self) -> None:
        if self.path.startswith("/api/"):
            return self._json({"ok": False, "error": "未授权：需要 token"}, 401)
        env_path = Path(__file__).resolve().parents[3] / ".env"  # 项目根
        html = (
            "<!doctype html><meta charset=utf-8><title>需要访问令牌</title>"
            "<body style='background:#0b0e14;color:#d7dce5;font:14px/1.8 system-ui;padding:40px;max-width:760px'>"
            "<h2 style='color:#d7dce5'>QeeLin-Agent · 需要访问令牌</h2>"
            "<p>这个控制台能直接下单，所以开了令牌保护。</p>"
            "<p>在地址后面加 <code style='background:#161d2b;padding:2px 6px;border-radius:4px'>?token=你的令牌</code> "
            "打开一次，之后浏览器会记住 30 天。</p>"
            "<h3 style='font-size:15px;margin-top:28px'>令牌在哪？</h3>"
            "<p>在<b>服务器上</b>这个文件里（安装脚本最后也会打印一次）：</p>"
            f"<pre style='background:#121722;border:1px solid #222b3a;border-radius:8px;padding:12px;overflow:auto'>{env_path}</pre>"
            "<p>Windows 上查看：</p>"
            "<pre style='background:#121722;border:1px solid #222b3a;border-radius:8px;padding:12px'>"
            "Get-Content C:\\crypto-agent\\.env | Select-String TOKEN</pre>"
            "<p>Linux 上查看：</p>"
            "<pre style='background:#121722;border:1px solid #222b3a;border-radius:8px;padding:12px'>"
            "grep TOKEN /etc/crypto-agent.env</pre>"
            "<p style='color:#7c8798'>懒得找也可以直接跑 <code>run.cmd token</code>，它会把完整地址打出来。</p>"
            "</body>"
        )
        self._send(401, html.encode("utf-8"), "text/html; charset=utf-8")

    # -- 路由 ---------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if not self._auth_ok(query):
            return self._deny()
        # ?token=xxx 首次访问：种 cookie，然后跳回不带 token 的地址
        if (
            self._token()
            and query.get("token", [""])[0] == self._token()
            and not path.startswith("/api/")
        ):
            self.send_response(302)
            self.send_header("Location", path or "/")
            self.send_header(
                "Set-Cookie", f"ca_token={self._token()}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            if path.startswith("/api/"):
                return self._api_get(path, query)
            return self._static(path)
        except ApiError as exc:
            self._json({"ok": False, "error": str(exc)}, exc.status)
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._auth_ok():
            return self._deny()
        try:
            payload = self._read_json()
            return self._api_post(path, payload)
        except ApiError as exc:
            self._json({"ok": False, "error": str(exc)}, exc.status)
        except PermissionError as exc:
            self._json({"ok": False, "error": str(exc)}, 403)
        except ExchangeError as exc:
            self._json({"ok": False, "error": str(exc)}, 502)
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    # -- GET ----------------------------------------------------------
    def _api_get(self, path: str, query: dict) -> None:
        app = self.app
        if path == "/api/status":
            return self._json({"ok": True, "data": app.status()})
        if path == "/api/candles":
            limit = int(query.get("limit", ["240"])[0])
            return self._json({"ok": True, "data": app.candles(limit)})
        if path == "/api/klines":
            return self._json(
                {
                    "ok": True,
                    "data": app.klines(
                        query.get("interval", [""])[0],
                        int(query.get("limit", ["240"])[0]),
                    ),
                }
            )
        if path == "/api/research/latest":
            return self._json({"ok": True, "data": app.latest_research()})
        if path == "/api/exchange/status":
            return self._json({"ok": True, "data": app.exchange_status()})
        if path == "/api/exchange/test":
            return self._json({"ok": True, "data": app.exchange_test()})
        if path == "/api/live":
            return self._json({"ok": True, "data": app.live_status()})
        if path == "/api/symbols":
            return self._json({"ok": True, "data": app.symbols()})
        if path == "/api/settings/llm":
            return self._json({"ok": True, "data": app.llm_settings()})
        if path == "/api/scan":
            return self._json({"ok": True, "data": app.last_scan})
        if path == "/api/decisions/latest":
            data = app.latest_decision()
            if data is None:
                md = Path(app.cfg.runs_dir) / "decisions" / "latest.md"
                text = md.read_text(encoding="utf-8") if md.exists() else ""
                return self._json({"ok": True, "data": {"markdown": text}})
            return self._json({"ok": True, "data": data})
        raise ApiError(f"未知接口 {path}", 404)

    # -- POST ---------------------------------------------------------
    def _api_post(self, path: str, payload: dict) -> None:
        app = self.app
        eng = app.engine
        if path == "/api/decision":
            data = app.decision(int(payload.get("rounds", 2)))
            return self._json({"ok": True, "data": data})
        if path == "/api/research":
            data = app.research(int(payload.get("iterations", 6)))
            return self._json({"ok": True, "data": data})
        if path == "/api/research/auto":
            return self._json({"ok": True, "data": app.set_research_auto(payload)})
        if path == "/api/trading/arm":
            res = app.arm(payload)
            app.invalidate_exchange_cache()   # 刚解锁：总览页要立刻改读交易所
            return self._json({"ok": True, "data": res})
        if path == "/api/trading/disarm":
            res = eng.disarm()
            app.reset_exchange_view()
            return self._json({"ok": True, "data": res})
        if path == "/api/trading/execute":
            res = eng.execute(price=app.live_price, reason="web")
            app.invalidate_exchange_cache()
            return self._json({"ok": True, "data": res})
        if path == "/api/trading/close":
            res = eng.close(price=app.live_price, reason="web")
            app.invalidate_exchange_cache()
            return self._json({"ok": True, "data": res})
        if path == "/api/risk":
            return self._json({"ok": True, "data": app.set_risk(payload)})
        if path == "/api/trading/auto-arm":
            return self._json({"ok": True, "data": app.set_auto_arm(payload)})
        if path == "/api/trading/auto":
            if payload.get("running"):
                data = eng.start_auto(
                    int(payload.get("interval_sec", 300)), bool(payload.get("execute", False))
                )
            else:
                data = eng.stop_auto()
            return self._json({"ok": True, "data": data})
        if path == "/api/killswitch":
            return self._json({"ok": True, "data": eng.set_halt(bool(payload.get("on", False)))})
        if path == "/api/exchange/sync":
            return self._json({"ok": True, "data": app.exchange_status()})
        if path == "/api/exchange/keys":
            return self._json({"ok": True, "data": app.save_keys(payload)})
        if path == "/api/settings/llm":
            return self._json({"ok": True, "data": app.set_llm_settings(payload)})
        if path == "/api/live":
            data = app.start_live() if payload.get("on", True) else app.stop_live()
            return self._json({"ok": True, "data": data})
        if path == "/api/symbol":
            return self._json({"ok": True, "data": app.set_symbol(payload)})
        if path == "/api/scan":
            return self._json({"ok": True, "data": app.scan(payload)})
        if path == "/api/opportunity":
            return self._json({"ok": True, "data": app.opportunities(payload)})
        if path == "/api/rotate":
            return self._json({"ok": True, "data": app.set_rotate(payload)})
        if path == "/api/data/refresh":
            return self._json({"ok": True, "data": app.refresh_data()})
        raise ApiError(f"未知接口 {path}", 404)

    # -- 静态文件 -----------------------------------------------------
    def _static(self, path: str) -> None:
        if path in ("/", ""):
            rel = "index.html"
        elif path.startswith("/static/"):
            rel = path[len("/static/"):]
        else:
            rel = path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)


def serve(cfg: AppConfig, host: str = "127.0.0.1", port: int = 8000, open_browser: bool = True) -> None:
    loopback = host in ("127.0.0.1", "localhost", "::1")
    token = os.getenv("CRYPTOAGENT_WEB_TOKEN", "").strip()
    if not loopback and not token:
        raise SystemExit(
            "拒绝启动：绑定到 %s 必须设置 CRYPTOAGENT_WEB_TOKEN。" % host
            + "\n  这个控制台能直接下单，暴露到公网又没有鉴权 = 谁都能花你的钱。"
            + "\n  生成令牌：openssl rand -hex 24  →  export CRYPTOAGENT_WEB_TOKEN=<令牌>"
            + "\n  或者保持默认 127.0.0.1，用 SSH 隧道访问（推荐）。"
        )
    app = WebApp(cfg, live=True)
    # 启动即打开窗口：K 线在后台线程下载（fapi 被墙时会 fallback 到 gate，首次约 30~40 秒），
    # 不再同步阻塞 HTTP 服务启动。前端通过 status.data_loading 展示「K 线加载中」，加载完自动刷新。
    app.engine.start_data_load()

    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    url = f"http://{host}:{port}"
    if token:
        url = f"{url}/?token={token}"
        open_browser = open_browser and loopback
    print(f"\n  QeeLin-Agent 控制台已启动：{url}")
    print(f"  数据源：后台加载中（K 线下载完自动刷新）   模式：{app.engine.state['mode']}")
    print("  按 Ctrl+C 停止\n")
    if open_browser:
        threading.Thread(target=lambda: (time.sleep(0.6), webbrowser.open(url)), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[web] 已停止")
    finally:
        httpd.server_close()