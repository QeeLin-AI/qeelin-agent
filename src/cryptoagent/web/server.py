"""本地 Web 服务：零依赖 HTTP 服务器 + JSON API。

默认只监听 127.0.0.1；绑定非回环地址时**强制**要求 CRYPTOAGENT_WEB_TOKEN
（见 serve() 的启动闸门），请求通过 Authorization / Cookie / ?token= 任一方式鉴权。
任何写操作（下单/解锁）都必须由浏览器显式触发，且服务端再校验一次。
"""
from __future__ import annotations

import functools
import hmac
import html as _html
import json
import math
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
from ..agents.llm import LLMClient, is_offpeak, offpeak_info, seconds_until_offpeak

STATIC_DIR = Path(__file__).resolve().parent / "static"


def normalize_symbol(raw: str) -> str:
    """把用户输入的品种写法归一成 SOLUSDT 这种形式。

    支持 SOLUSDT / solusdt / SOL/USDT / SOL-USDT / SOL-USDT-SWAP / SOLUSDT.P 等写法。
    """
    s = str(raw or "").strip().upper().replace(" ", "")
    for sep in ("/", "-", "_", ":"):
        s = s.replace(sep, "")
    for suffix in ("SWAP", "PERP", "P", "UMCBL"):
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
            break
    if not s.endswith("USDT") and s.endswith("USD"):
        s = s[:-3] + "USDT"
    if not (3 <= len(s) <= 20) or not s.isalnum():
        raise ApiError("品种代码不合法，例如 SOLUSDT", 400)
    return s


def normalize_query(raw: str) -> str:
    """候选搜索用：宽松处理，SEARCH 输入一半也能匹配。"""
    s = str(raw or "").strip().upper().replace(" ", "")
    for sep in ("/", "-", "_", ":"):
        s = s.replace(sep, "")
    return s


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _as_int(value, name: str, default: int,
            lo: int | None = None, hi: int | None = None) -> int:
    """路由层整数参数：非法值返回 400，而不是让 ValueError 冒泡成 500。"""
    try:
        out = int(value if value not in (None, "") else default)
    except (TypeError, ValueError) as exc:
        raise ApiError(f"{name} 必须是整数", 400) from exc
    if lo is not None:
        out = max(lo, out)
    if hi is not None:
        out = min(hi, out)
    return out


def _serialized(fn):
    """配置写操作串行化：同一时刻只允许一个请求改 cfg 并落盘。"""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self.cfg_lock:
            return fn(self, *args, **kwargs)
    return wrapper


class WebApp:
    def __init__(self, cfg: AppConfig, live: bool = False, live_interval: float = 5.0) -> None:
        self.cfg = cfg
        self.engine = TradingEngine(cfg)
        self.research_lock = threading.Lock()
        # 配置写锁：ThreadingHTTPServer 的多个请求线程 + 自动 evolve 线程共享
        # 同一个 AppConfig。逐字段 mutate 后全量 save 必须串行，否则会观察到
        # 「仓位上限已改、保证金上限没改」的中间态，回滚也会抹掉并发写入。
        self.cfg_lock = threading.RLock()
        self.last_research: dict | None = None
        self.last_decision: dict | None = None
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.exchange_env = "testnet"      # 自检探测到的可用环境：testnet | demo
        self.last_scan: dict | None = None
        self.token = os.getenv("CRYPTOAGENT_WEB_TOKEN", "").strip()
        # 重启后自动解锁（只允许假钱环境）。CRYPTOAGENT_AUTO_ARM = <exchange>:<env>，
        # 例如 okx:demo / binance:testnet；没有交易所前缀时跟随当前活跃交易所。
        self.auto_arm = os.getenv("CRYPTOAGENT_AUTO_ARM", "").strip().lower()
        _aa = self.auto_arm.split(":", 1)
        self.auto_arm_exchange = (
            _aa[0] if len(_aa) == 2 else self.engine.state.get("exchange", "binance")
        )
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
        # 重启后自动恢复「自动交易」：CRYPTOAGENT_AUTO_RESUME=on 才生效，且只在模拟盘（testnet/demo）。
        self.auto_resume = os.getenv("CRYPTOAGENT_AUTO_RESUME", "off").strip().lower()
        self.auto_resume_state: dict = {
            "enabled": self.auto_resume == "on",
            "resumed": False,
            "at": "",
            "interval_sec": 0,
            "execute": False,
            "error": "",
        }
        # 自定义品种：添加后在后台预下载历史（6000 根），这里记录进行中/失败的
        self._prefetching: set[str] = set()
        self._prefetch_error: dict[str, str] = {}
        # 配置方案 / LLM 自主调参：改动历史 + 互斥锁（自动调参在 cycle 钩子里跑）
        self._evolve_lock = threading.Lock()
        # 自动调参挂在自动循环末尾：cycle 每 N 秒一次，这里只做「到点没到点」的便宜判断
        try:
            self.engine.on_cycle_hook = self._auto_evolve_tick
        except Exception:  # noqa: BLE001 - 挂不上就只保留手动触发
            pass
        # 交易所可交易合约清单（给「添加品种」做候选提示，10 分钟缓存）
        self._sym_catalog: list[dict] = []
        self._sym_catalog_at = 0.0
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
        if live and self.auto_resume == "on":
            threading.Thread(target=self._auto_resume_loop, name="auto-resume", daemon=True).start()
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
        # Key 都没配就没必要空重试 6 次，直接给出明确原因
        from ..execution import read_api_keys

        k, s, p = read_api_keys(exchange, True)
        if not (k and s and (p if exchange == "okx" else True)):
            self.auto_arm_state.update({
                "ok": False,
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "error": f"未配置 {exchange} 模拟盘 API Key，已跳过自动解锁",
            })
            print(f"[auto-arm] {self.auto_arm_state['error']}")
            return
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

    def set_auto_resume(self, payload: dict) -> dict:
        """开关「重启后自动恢复自动交易」。只对模拟盘生效，主网实盘永不自动恢复。"""
        on = bool(payload.get("on", False))
        value = "on" if on else "off"
        update_dotenv({"CRYPTOAGENT_AUTO_RESUME": value})
        self.auto_resume = value
        self.auto_resume_state["enabled"] = on
        if not on:
            self.auto_resume_state["resumed"] = False
        return {"auto_resume": self.auto_resume_state, "saved": value}

    def _auto_resume_loop(self, max_wait: int = 300) -> None:
        """重启后恢复自动交易。三个条件都满足才动手：
        1) .env 里 CRYPTOAGENT_AUTO_RESUME=on；2) 重启前确实在跑；3) 当前是模拟盘且已解锁。
        主网实盘永不自动恢复——真钱必须每次手动确认。
        """
        eng = self.engine
        if not getattr(eng, "auto_was_running", False):
            self.auto_resume_state["error"] = "上次退出时未在运行，跳过"
            return
        waited = 0
        while waited < 90 and not self.auto_arm_state.get("ok"):
            time.sleep(2)
            waited += 2
        if not (eng.armed and eng.state.get("mode") == "live" and eng.state.get("testnet")):
            self.auto_resume_state["error"] = "交易所未解锁或不是模拟盘，跳过自动恢复"
            return
        if eng.state.get("halt"):
            self.auto_resume_state["error"] = "熔断开关打开，跳过"
            return
        waited = 0
        while waited < max_wait and eng.df is None:
            time.sleep(2)
            waited += 2
        prev = eng.state.get("auto") or {}
        interval = int(prev.get("interval_sec") or 300)
        execute = bool(prev.get("execute"))
        eng.start_auto(interval, execute)
        self.auto_resume_state.update(
            {
                "resumed": True,
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "interval_sec": interval,
                "execute": execute,
                "error": "",
            }
        )
        print(f"[auto-resume] 已恢复自动交易：每 {interval}s，自动下单={execute}")

    # ------------------------------------------------------------------
    def status(self) -> dict:
        # 有实时价就传给引擎，权益/浮盈亏/权益曲线都按实时价算
        st = self.engine.status(price_override=self.live_price)
        st["live"] = self.live_status()
        st["auto_arm"] = self.auto_arm_state
        st["auto_resume"] = self.auto_resume_state
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

    def notifications(self, limit: int = 50) -> dict:
        """A2：返回应用内通知列表（由 notify.Notifier 写入 JSONL）。"""
        from ..notify import load_notifications

        items = load_notifications(self.cfg.notify.inapp_path, limit=limit)
        return {"items": items, "channels": self.cfg.notify.channels}

    def clear_notifications(self) -> dict:
        """清空应用内通知列表（JSONL 是追加日志，这里直接截断）。"""
        from ..notify import clear_notifications

        clear_notifications(self.cfg.notify.inapp_path)
        return {"cleared": True, "items": [], "channels": self.cfg.notify.channels}

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

        # 成交：跨品种拉取（切了品种也要能看到上一笔），缓存 15 秒让新成交尽快出现
        if self._ex_trades is None or (now - self._ex_trades_at) >= 15:
            rows = None
            try:
                rows = client.trades(None, limit=30)
            except Exception:  # noqa: BLE001 - 个别交易所要求指定 symbol，退回当前品种
                try:
                    rows = client.trades(symbol, limit=30)
                except Exception:  # noqa: BLE001 - 成交拿不到不影响余额
                    rows = None
            if rows is not None:
                # 重置过账户后，只显示「重置时刻」之后的成交：交易所的真实成交
                # 是客观存在的删不掉，重置只能让视图从这个锚点起重新累计。
                reset_epoch = int(self.engine.state.get("account_reset_epoch") or 0)
                self._ex_trades = [
                    self._fill_with_plan({**t, "symbol": t.get("symbol") or symbol}) for t in rows
                    if reset_epoch <= 0 or int(t.get("ts_ms") or 0) >= reset_epoch
                ]
                self._ex_trades_at = now
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
        """当日盈亏 = 资金流水里的交易类收入（排除划转）。

        重置过账户后，只汇总「重置时刻」之后的流水，基准用重置时的权益；
        否则按 UTC 日界（今日起点）汇总。"""
        now = time.time()
        if self._ex_pnl_cache is not None and (now - self._ex_pnl_at) < ttl:
            return self._ex_pnl_cache

        reset_epoch = int(self.engine.state.get("account_reset_epoch") or 0)
        if reset_epoch:
            start_ms = reset_epoch
        else:
            # 币安按 UTC 日界结算；epoch 本身就是 UTC 基准，直接按整天取整
            start_ms = int(time.time() // 86400) * 86400 * 1000
        rows = client.income(start_ms, limit=1000)
        skip = {"TRANSFER", "INTERNAL_TRANSFER", "INSURANCE_CLEAR", "CROSS_COLLATERAL_TRANSFER"}
        total = 0.0
        for r in rows or []:
            if str(r.get("incomeType", "")).upper() in skip:
                continue
            # 币安 income 自带 time（毫秒），OKX 已统一补上；再按锚点过滤一次兜底
            if reset_epoch and int(r.get("time") or 0) and int(r.get("time")) < reset_epoch:
                continue
            try:
                total += float(r.get("income", 0.0))
            except (TypeError, ValueError):
                continue
        if reset_epoch:
            base = float(self.engine.state.get("account_reset_equity") or 0.0)
            if base <= 0:
                acct = client.account()
                base = acct["balance"] - total
        else:
            acct = client.account()
            base = acct["balance"] - total
        pct = (total / base * 100) if base else 0.0
        self._ex_pnl_cache = {"abs": round(total, 4), "pct": round(pct, 3)}
        self._ex_pnl_at = now
        return self._ex_pnl_cache

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
            # 峰时省钱：自动研究可延后，峰时不跑；等到下一个谷时窗口再执行
            if getattr(self.cfg.llm, "offpeak_research_only", True) and not is_offpeak(self.cfg.llm):
                wait = max(60, min(interval, seconds_until_offpeak(self.cfg.llm) or interval))
                self.research_auto_state["status"] = f"等待谷时（约 {wait // 60} 分钟）"
                self._research_auto_stop.wait(wait)
                continue
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
        interval = _as_int(payload.get("interval_sec", 86400), "interval_sec", 86400, 60, 604800)
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
            vals = [v for v in probe.values() if v]
            if not vals or all(v.startswith("未设置") for v in vals):
                stage, error = "keys", "未设置 OKX 模拟盘/主网 API Key（可在本页填，或写进 .env 后重启）"
            else:
                detail = next((v for v in vals if not v.startswith("未设置")), vals[0])
                stage, error = "network", f"OKX 连接失败：{detail}"
            return {"ok": False, "stage": stage, "exchange": "okx", "probe": probe, "error": error}

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
        vals = [v for v in probe.values() if v]
        if not vals or all(v.startswith("未设置") for v in vals):
            stage, error = "keys", "未设置币安模拟盘/主网 API Key（可在本页填，或写进 .env 后重启）"
        else:
            detail = next((v for v in vals if not v.startswith("未设置")), vals[0])
            stage, error = "network", f"币安连接失败：{detail}"
        return {"ok": False, "stage": stage, "exchange": "binance", "probe": probe, "error": error}

    # -- 多品种 ---------------------------------------------------------
    def symbols(self) -> list[dict]:
        """配置里列出的品种 + 本地缓存情况（置顶的排最前）。"""
        cache_dir = Path(self.cfg.market.cache_dir)
        stem = f"{self.cfg.market.interval}_{self.cfg.market.bars}"
        pinned = [x for x in (getattr(self.cfg.market, "pinned", None) or [])
                  if x in self.cfg.market.symbols]
        order = pinned + [x for x in self.cfg.market.symbols if x not in pinned]
        out = []
        for sym in order:
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
            entry["pinned"] = sym in pinned
            entry["exchanges"] = (self.cfg.market.symbol_exchanges or {}).get(sym, [])
            entry["prefetching"] = sym in self._prefetching
            entry["error"] = self._prefetch_error.get(sym, "")
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
        # 切到列表里没有的品种（历史缓存手动加过的）时补进列表并落盘，别让下拉框丢项
        if symbol not in self.cfg.market.symbols:
            from ..config import config_path, save_config

            self.cfg.market.symbols.append(symbol)
            try:
                save_config(self.cfg, config_path())
            except OSError:
                pass
        # 换了品种，交易所视图和权益曲线都得重来
        self.reset_exchange_view()
        return res

    # ---- 自定义交易品种 ---------------------------------------------
    @staticmethod
    def _exchange_has_symbol(sym: str, exchange: str) -> tuple[bool, str]:
        """这家交易所有没有上线该 USDT 本位永续？返回 (是否有, 没有的原因)。"""
        import urllib.error

        from ..data import FAPI, OKX, _get, okx_inst

        if exchange == "okx":
            inst = okx_inst(sym)
            try:
                data = _get(
                    f"{OKX}/api/v5/public/instruments",
                    {"instType": "SWAP", "instId": inst},
                    timeout=10,
                )
            except Exception as exc:  # noqa: BLE001
                return False, f"查询 OKX 合约列表失败（{type(exc).__name__}），稍后再试"
            if data.get("data"):
                return True, ""
            if sym.endswith("USDC"):
                return (
                    False,
                    f"OKX 只有 USDT 本位和币本位（USD）永续，没有 USDC 本位合约；"
                    f"{sym} 请切换到「币安」添加",
                )
            return False, f"OKX 没有上线 {sym} 的永续合约（{inst}）"

        try:
            _get(f"{FAPI}/fapi/v1/ticker/price", {"symbol": sym}, timeout=10)
            return True, ""
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8") or "{}")
            except Exception:  # noqa: BLE001
                body = {}
            if body.get("code") in (-1121, -1100) or "Invalid symbol" in str(body.get("msg", "")):
                return False, f"币安合约没有上线 {sym}（symbol 不存在）"
            return False, f"查询币安合约失败：{body.get('msg') or exc.code}"
        except Exception as exc:  # noqa: BLE001
            return False, f"查询币安合约失败（{type(exc).__name__}），稍后再试"

    def add_symbol(self, payload: dict) -> dict:
        """添加一个交易品种（USDT 本位永续）。

        先用当前交易所校验行情能不能拿到，再写进 config/default.json，
        最后后台预下载历史 K 线（6000 根要几秒，不能卡住 HTTP 请求）。
        """
        from ..config import config_path, save_config
        from ..data import fetch_klines

        raw = str(payload.get("symbol", "")).strip()
        if not raw:
            raise ApiError("请填写品种代码，例如 SOLUSDT", 400)
        sym = normalize_symbol(raw)

        # 用户选的交易所（不传就用当前连接的交易所）
        exchange = str(
            payload.get("exchange") or self.engine.state.get("exchange", "binance")
        ).strip().lower()
        if exchange not in ("binance", "okx"):
            raise ApiError("exchange 只能是 binance / okx", 400)
        ex_cn = "OKX" if exchange == "okx" else "币安"

        if sym in self.cfg.market.symbols:
            return {
                "symbol": sym,
                "added": False,
                "exchange": exchange,
                "symbols": self.symbols(),
            }

        # 1) 这家交易所到底有没有上线这个永续合约（没上线的别加进来）
        listed, why = self._exchange_has_symbol(sym, exchange)
        if not listed:
            raise ApiError(why, 400)

        # 2) 行情能不能真的拉到（且必须来自这家交易所，别拿别家的数据充数）
        #    use_disk=False：这是连通性校验，必须真的打一次交易所，不能读盘缓存
        try:
            df, src = fetch_klines(sym, self.cfg.market.interval, 60,
                                   prefer=exchange, use_disk=False, force=True)
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"{ex_cn} 行情拉取失败（{type(exc).__name__}），稍后再试", 502) from exc
        if df is None or df.empty:
            raise ApiError(f"{ex_cn} 没有返回任何 K 线，换个代码试试", 400)
        if exchange == "okx" and not str(src).startswith("okx"):
            raise ApiError(f"{ex_cn} 行情拉不到（当前只能拿到 {src} 的数据），稍后再试", 502)
        if exchange == "binance" and not str(src).startswith("binance"):
            raise ApiError(f"{ex_cn} 行情拉不到（当前只能拿到 {src} 的数据），稍后再试", 502)

        self.cfg.market.symbols.append(sym)
        ex_map = dict(self.cfg.market.symbol_exchanges or {})
        ex_map[sym] = [exchange]
        self.cfg.market.symbol_exchanges = ex_map
        self._prefetch_error.pop(sym, None)
        saved = True
        try:
            save_config(self.cfg, config_path())
        except OSError:
            saved = False          # 只读文件系统：内存里仍生效，重启会丢

        threading.Thread(target=self._prefetch_symbol, args=(sym,), daemon=True).start()

        return {
            "symbol": sym,
            "added": True,
            "saved": saved,
            "source": src,
            "price": round(float(df["close"].iloc[-1]), 8),
            "bars": int(len(df)),
            "exchange": exchange,
            "note": "",
            "symbols": self.symbols(),
        }

    def remove_symbol(self, payload: dict) -> dict:
        """从列表里删掉一个品种（当前活跃品种、或有持仓的品种不许删）。"""
        from ..config import config_path, save_config

        sym = normalize_symbol(str(payload.get("symbol", "")).strip())
        if sym not in self.cfg.market.symbols:
            raise ApiError(f"{sym} 不在品种列表里", 404)
        if sym == self.cfg.market.symbol:
            raise ApiError("这是当前正在用的品种，先切到别的品种再删", 409)
        # 先与交易所核对，清理幽灵仓位，避免误拦截删除
        self.engine._reconcile_position_with_exchange()
        # 多品种：只要 dict 里有这个品种就不能删（以前只比对了单个仓位）
        if self.engine._get_position(sym):
            raise ApiError("该品种还有未平仓位，先平仓再删", 409)
        self.cfg.market.symbols.remove(sym)
        self.cfg.market.pinned = [x for x in (self.cfg.market.pinned or []) if x != sym]
        if self.cfg.market.symbol_exchanges:
            self.cfg.market.symbol_exchanges.pop(sym, None)
        self._prefetch_error.pop(sym, None)
        saved = True
        try:
            save_config(self.cfg, config_path())
        except OSError:
            saved = False
        return {"symbol": sym, "removed": True, "saved": saved, "symbols": self.symbols()}

    def set_symbol_pin(self, payload: dict) -> dict:
        """置顶 / 取消置顶某个品种（置顶 = 排到列表最前面，可多个）。"""
        from ..config import config_path, save_config

        sym = normalize_symbol(str(payload.get("symbol", "")).strip())
        if sym not in self.cfg.market.symbols:
            raise ApiError(f"{sym} 不在品种列表里", 404)
        pinned = [x for x in (self.cfg.market.pinned or []) if x != sym]
        desire = payload.get("pinned")
        if desire is None:
            desire = sym not in (self.cfg.market.pinned or [])   # 没传就切换
        if bool(desire):
            pinned.insert(0, sym)          # 新置顶的排在最前
        self.cfg.market.pinned = pinned
        saved = True
        try:
            save_config(self.cfg, config_path())
        except OSError:
            saved = False
        return {
            "symbol": sym,
            "pinned": bool(desire),
            "saved": saved,
            "symbols": self.symbols(),
        }

    def symbol_candidates(self, q: str = "") -> list[dict]:
        """交易所永续合约清单，给「添加品种」做候选（避免手打错代码）。"""
        now = time.time()
        if not self._sym_catalog or (now - self._sym_catalog_at) > 600:
            catalog = self._load_symbol_catalog()
            if catalog:                      # 抓不到就保留旧缓存，别把候选清空
                self._sym_catalog, self._sym_catalog_at = catalog, now
        cat = self._sym_catalog
        key = normalize_query(q)
        if not key:
            return cat[:40]
        return [x for x in cat if key in x["symbol"]][:30]

    @staticmethod
    def _load_symbol_catalog() -> list[dict]:
        from ..data import FAPI, OKX, _get

        found: dict[str, dict] = {}
        try:
            data = _get(f"{FAPI}/fapi/v1/exchangeInfo", {}, timeout=12)
            for item in data.get("symbols", []):
                if (
                    item.get("quoteAsset") in ("USDT", "USDC")
                    and item.get("contractType") == "PERPETUAL"
                    and item.get("status") == "TRADING"
                ):
                    found[item["symbol"]] = {"symbol": item["symbol"], "exchanges": ["binance"]}
        except Exception:  # noqa: BLE001
            pass
        try:
            data = _get(f"{OKX}/api/v5/public/instruments", {"instType": "SWAP"}, timeout=12)
            for item in data.get("data", []):
                inst = str(item.get("instId", ""))
                quote = next((q for q in ("USDT", "USDC") if inst.endswith(f"-{q}-SWAP")), "")
                if not quote:
                    continue
                sym = inst[: -len(f"-{quote}-SWAP")].replace("-", "") + quote
                row = found.setdefault(sym, {"symbol": sym, "exchanges": []})
                if "okx" not in row["exchanges"]:
                    row["exchanges"].append("okx")
        except Exception:  # noqa: BLE001
            pass
        return sorted(found.values(), key=lambda x: x["symbol"])

    def _prefetch_symbol(self, sym: str) -> None:
        """后台把这个品种的历史 K 线拉全，页面上会显示「缓存中」。"""
        from dataclasses import replace

        from ..data import load_market

        if sym in self._prefetching:
            return
        self._prefetching.add(sym)
        try:
            mkt = replace(self.cfg.market, symbol=sym)
            load_market(
                mkt, force_download=True, prefer=self.engine.state.get("exchange", "binance")
            )
            self._prefetch_error.pop(sym, None)
        except Exception as exc:  # noqa: BLE001
            self._prefetch_error[sym] = f"{type(exc).__name__}: {str(exc)[:120]}"
        finally:
            self._prefetching.discard(sym)

    def invalidate_exchange_cache(self) -> None:
        """只让下一次 status 重新拉数（下单/平仓后调用，曲线保留）。"""
        self._ex_cache = None
        self._ex_cache_at = 0.0
        self._ex_pnl_cache = None
        self._ex_pnl_at = 0.0

    def reset_exchange_view(self) -> None:
        """清账户级视图（快照 / 当日盈亏 / 成交 / 条件单 / 权益曲线）。

        切品种与切账户都调用；K 线缓存不在这里，账户切换不会影响行情数据。
        """
        self.invalidate_exchange_cache()
        self._ex_trades = None
        self._ex_trades_at = 0.0
        self._ex_orders = None
        self._ex_orders_at = 0.0
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
                df, src = load_market(mkt, prefer=self.engine.state.get("exchange", "binance"))
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
            # 仓位怎么定：llm=听交易员 / risk=按单笔风险上限反推（旧值 max 已废弃）
            "sizing_mode": r.sizing_mode if r.sizing_mode in ("llm", "risk") else "llm",
            "min_leverage": r.min_leverage,
            "max_portfolio_notional_pct": r.max_portfolio_notional_pct,
            "correlated_exposure_pct": r.correlated_exposure_pct,
            # 多品种并发：品种数上限 + 保证金占用上限（交易所真正卡的那道闸）
            "max_concurrent_positions": int(getattr(r, "max_concurrent_positions", 1) or 1),
            "max_margin_usage_pct": float(getattr(r, "max_margin_usage_pct", 1.0) or 1.0),
        }

    @_serialized
    def set_risk(self, payload: dict) -> dict:
        """改杠杆/风险上限并写进 config/default.json（重启后仍生效）。"""
        from dataclasses import fields as dc_fields

        from ..config import config_path, save_config

        r = self.cfg.risk
        # 逐字段赋值，任何一步校验失败都要整体回滚，否则会留下「改了一半」的配置
        # （比如仓位上限已改成 50%、后面才因为保证金不够被拒，内存里就是 50%）。
        _snap = {f.name: getattr(r, f.name) for f in dc_fields(r)}
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
        if "max_leverage" in payload:
            try:
                v = float(payload["max_leverage"])
            except (TypeError, ValueError) as exc:
                raise ApiError("max_leverage 必须是数字", 400) from exc
            if not (1.0 <= v <= 125.0):
                raise ApiError("杠杆上限只能在 1 ~ 125 之间", 400)
            r.max_leverage = v
        if "max_loss_per_trade_pct" in payload:
            try:
                v = float(payload["max_loss_per_trade_pct"])
            except (TypeError, ValueError) as exc:
                raise ApiError("max_loss_per_trade_pct 必须是数字", 400) from exc
            if not (0.001 <= v <= 1.0):
                raise ApiError("单笔风险上限只能在 0.1% ~ 100% 之间", 400)
            r.max_loss_per_trade_pct = v
        if "max_position_pct" in payload:
            try:
                v = float(payload["max_position_pct"])
            except (TypeError, ValueError) as exc:
                raise ApiError("max_position_pct 必须是数字", 400) from exc
            if not (0.001 <= v <= 1.0):
                raise ApiError("仓位上限只能在 0.1% ~ 100% 之间", 400)
            r.max_position_pct = v
        if "max_concurrent_positions" in payload:
            try:
                v = int(float(payload["max_concurrent_positions"]))
            except (TypeError, ValueError) as exc:
                raise ApiError("max_concurrent_positions 必须是整数", 400) from exc
            if not (1 <= v <= 20):
                raise ApiError("同时持仓品种数只能在 1 ~ 20 之间", 400)
            r.max_concurrent_positions = v
        if "max_margin_usage_pct" in payload:
            try:
                v = float(payload["max_margin_usage_pct"])
            except (TypeError, ValueError) as exc:
                raise ApiError("max_margin_usage_pct 必须是数字", 400) from exc
            if not (0.01 <= v <= 1.0):
                raise ApiError("保证金占用上限只能在 1% ~ 100% 之间", 400)
            r.max_margin_usage_pct = v
        if "daily_loss_limit_pct" in payload:
            try:
                v = float(payload["daily_loss_limit_pct"])
            except (TypeError, ValueError) as exc:
                raise ApiError("daily_loss_limit_pct 必须是数字", 400) from exc
            if not (0.001 <= v <= 1.0):
                raise ApiError("当日亏损上限只能在 0.1% ~ 100% 之间", 400)
            r.daily_loss_limit_pct = v
        if "max_drawdown_pct" in payload:
            try:
                v = float(payload["max_drawdown_pct"])
            except (TypeError, ValueError) as exc:
                raise ApiError("max_drawdown_pct 必须是数字", 400) from exc
            if not (0.001 <= v <= 1.0):
                raise ApiError("最大回撤只能在 0.1% ~ 100% 之间", 400)
            r.max_drawdown_pct = v
        if "min_confidence" in payload:
            try:
                v = float(payload["min_confidence"])
            except (TypeError, ValueError) as exc:
                raise ApiError("min_confidence 必须是数字", 400) from exc
            if not (0.0 <= v <= 1.0):
                raise ApiError("置信度门槛只能在 0 ~ 1 之间", 400)
            r.min_confidence = v
        if "sizing_mode" in payload:
            mode = str(payload["sizing_mode"]).strip().lower()
            if mode == "max":      # 旧值：两者取大已废弃，回落到「听交易员」
                mode = "llm"
            if mode not in ("llm", "risk"):
                raise ApiError("sizing_mode 只能是 llm / risk", 400)
            r.sizing_mode = mode
        if "min_leverage" in payload:
            try:
                v = float(payload["min_leverage"])
            except (TypeError, ValueError) as exc:
                raise ApiError("min_leverage 必须是数字", 400) from exc
            if not (1.0 <= v <= 50.0):
                raise ApiError("杠杆下限只能在 1 ~ 50 之间", 400)
            r.min_leverage = v
        if "max_portfolio_notional_pct" in payload:
            try:
                v = float(payload["max_portfolio_notional_pct"])
            except (TypeError, ValueError) as exc:
                raise ApiError("max_portfolio_notional_pct 必须是数字", 400) from exc
            if not (0.1 <= v <= 100.0):
                raise ApiError("组合名义敞口上限只能在 0.1 ~ 100 倍权益之间", 400)
            r.max_portfolio_notional_pct = v
        if "correlated_exposure_pct" in payload:
            try:
                v = float(payload["correlated_exposure_pct"])
            except (TypeError, ValueError) as exc:
                raise ApiError("correlated_exposure_pct 必须是数字", 400) from exc
            if not (0.1 <= v <= 100.0):
                raise ApiError("相关组敞口上限只能在 0.1 ~ 100 倍权益之间", 400)
            r.correlated_exposure_pct = v
        # 单笔仓位上限 × 同时持仓品种数 = 所有仓位打满要占的保证金。
        # 它必须 ≤ 保证金占用上限，否则「允许开 N 仓」是假承诺：按上限开满时，
        # 第 N 仓一定被组合层保证金闸拒掉。这里硬拦，前端只是先提示一道。
        _pos = float(r.max_position_pct)
        _slots = max(1, int(r.max_concurrent_positions or 1))
        _margin = float(r.max_margin_usage_pct)
        if _pos * _slots > _margin + 1e-9:
            need = _pos * _slots
            for _k, _v in _snap.items():
                setattr(r, _k, _v)
            raise ApiError(
                f"单笔仓位上限 {_pos:.1%} × 同时持仓 {_slots} 个 = {need:.1%} 权益，"
                f"超过保证金占用上限 {_margin:.1%}，本次修改已撤销。"
                f"请先把「保证金占用上限」提高到 {math.ceil(need * 1000) / 10:.1f}% 以上",
                400,
            )

        # 风控硬顶由用户自主设定，系统不擅自改写任何配置值。
        # 仅在「单笔最大名义（仓位上限 × 杠杆上限）」超出组合上限时给出警告，
        # 提示这类单会被组合层拒绝，由用户自己决定是否调整。
        warnings = []
        _need = float(r.max_position_pct) * float(r.max_leverage)
        if _need > 0:
            if r.max_portfolio_notional_pct < _need:
                warnings.append(
                    f"单笔最大名义 {_need:.1f}x 权益 超过组合名义敞口上限 "
                    f"{r.max_portfolio_notional_pct:.1f}x，超过该值的单会被组合层拒绝"
                )
            if r.correlated_exposure_pct < _need:
                warnings.append(
                    f"单笔最大名义 {_need:.1f}x 权益 超过相关组敞口上限 "
                    f"{r.correlated_exposure_pct:.1f}x，同向集中持仓会被拒绝"
                )
        try:
            save_config(self.cfg, config_path())
            saved = True
        except OSError:
            # 保存失败（只读文件系统）时回滚内存：否则页面显示新值、重启后又
            # 变回旧值，用户无法判断到底有没有生效。
            for _k, _v in _snap.items():
                setattr(r, _k, _v)
            saved = False
            warnings.append("配置写入失败（文件系统只读？），本次修改已回滚")
        return {"risk": self.risk_config(), "saved": saved, "warnings": warnings}

    # ------------------------------------------------------------------
    # 配置方案：预设 / 一键恢复默认 / 回滚
    # ------------------------------------------------------------------
    _SETTINGS_GROUPS = ("risk", "position", "market", "llm")

    def _settings_snapshot(self) -> dict:
        from dataclasses import asdict
        return {g: asdict(getattr(self.cfg, g)) for g in self._SETTINGS_GROUPS}

    def _settings_restore(self, snap: dict) -> None:
        """把三组配置整体写回快照，并落盘（失败也只影响持久化，内存已是旧值）。"""
        from ..config import config_path, save_config
        for g in self._SETTINGS_GROUPS:
            obj = getattr(self.cfg, g)
            for k, v in (snap.get(g) or {}).items():
                if hasattr(obj, k):
                    setattr(obj, k, v)
        try:
            save_config(self.cfg, config_path())
        except OSError:
            pass

    def _settings_history(self) -> list:
        h = self.engine.state.setdefault("settings_history", [])
        if not isinstance(h, list):
            h = []
            self.engine.state["settings_history"] = h
        return h

    def _push_settings_history(self, rec: dict) -> None:
        h = self._settings_history()
        h.append(rec)
        del h[:-20]
        try:
            self.engine._save()
        except Exception:  # noqa: BLE001 - 历史写盘失败不影响已生效的配置
            pass

    def settings_profiles(self) -> dict:
        """列出全部方案 + 与当前配置的差异；`current` 为空表示当前是自定义组合。"""
        from ..settings_profiles import DEFAULT_PROFILE, PROFILES, matches_profile, profile_diff
        return {
            "default": DEFAULT_PROFILE,
            "current": next((n for n in PROFILES if matches_profile(self.cfg, n)), ""),
            "profiles": [
                {
                    "name": n,
                    "label": p["label"],
                    "summary": p["summary"],
                    "diff": profile_diff(self.cfg, n),
                    "matches": matches_profile(self.cfg, n),
                }
                for n, p in PROFILES.items()
            ],
            "history": self._settings_history()[-5:],
        }

    @_serialized
    def apply_settings_profile(self, payload: dict) -> dict:
        """应用一套方案。走既有 setter，因此沿用它们的校验与「失败整体回滚」。"""
        from ..settings_profiles import PROFILES, assert_profile_safe, profile_diff
        name = str(payload.get("name") or "").strip()
        if name not in PROFILES:
            raise ApiError(f"未知方案：{name or '(空)'}", 400)
        # 二次确认：方案不得包含 API Key / provider / model（导入时已校验，这里防运行期被改）
        assert_profile_safe(name)
        prof = PROFILES[name]
        before = profile_diff(self.cfg, name)
        snap = self._settings_snapshot()
        try:
            if prof.get("risk"):
                self.set_risk(dict(prof["risk"]))
            if prof.get("position"):
                self.set_position_settings(dict(prof["position"]))
            if prof.get("market"):
                self.set_rotate(dict(prof["market"]))
            if prof.get("llm"):
                self.set_llm_settings(dict(prof["llm"]))
        except Exception:
            self._settings_restore(snap)
            raise
        prev = {f"{d['group']}.{d['key']}": d["current"] for d in before}
        self._push_settings_history({
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": "profile", "name": name, "label": prof["label"],
            "prev": prev,
        })
        return {
            "applied": name,
            "label": prof["label"],
            "changed": len(before),
            "risk": self.risk_config(),
            "remaining": profile_diff(self.cfg, name),
        }

    def rollback_settings(self) -> dict:
        """把上一次（方案或自动调参）改动回滚。回滚后仍要满足保证金不等式。"""
        from ..config import config_path, save_config
        h = self._settings_history()
        if not h:
            raise ApiError("没有可回滚的改动", 400)
        rec = h[-1]
        snap = self._settings_snapshot()
        for path, val in (rec.get("prev") or {}).items():
            grp, _, key = str(path).partition(".")
            obj = getattr(self.cfg, grp, None)
            if obj is not None and hasattr(obj, key):
                setattr(obj, key, val)
        pos = float(self.cfg.risk.max_position_pct)
        slots = max(1, int(self.cfg.risk.max_concurrent_positions or 1))
        margin = float(self.cfg.risk.max_margin_usage_pct)
        if pos * slots > margin + 1e-9:
            self._settings_restore(snap)
            raise ApiError(
                f"回滚后 {pos:.0%}×{slots} 超过保证金上限 {margin:.0%}，已撤销本次回滚", 400
            )
        try:
            save_config(self.cfg, config_path())
        except OSError:
            pass
        h.pop()
        try:
            self.engine._save()
        except Exception:  # noqa: BLE001
            pass
        return {"rolled_back": rec.get("label") or rec.get("name") or rec.get("kind"),
                "risk": self.risk_config()}

    # ------------------------------------------------------------------
    # LLM 自主调参（提案由模型出，裁决由代码做）
    # ------------------------------------------------------------------
    @_serialized
    def settings_evolve(self, payload: dict) -> dict:
        """让模型基于近期表现提一份参数调整建议；apply=True 时通过裁决就落盘。"""
        from ..agents.settings_evolver import (
            POSITION_KEYS, build_context, funnel_stats, perf_stats, propose, sanitize,
        )
        apply_now = bool(payload.get("apply"))
        # 只拿当前账户的平仓样本：切到 OKX 模拟盘后，不能再用币安模拟盘的
        # 战绩去推断参数该往哪调。
        perf = perf_stats(self.engine._scoped_rows("position_history"))
        funnel = funnel_stats(Path(self.cfg.runs_dir) / "decisions")
        ctx = build_context(self.cfg, perf, funnel)
        prop = propose(self.engine._position_llm_client(), ctx)
        verdict = sanitize(prop, self.cfg, ctx["current"])
        out = {
            "proposal": prop,
            "changes": verdict["changes"],
            "rejected": verdict["rejected"],
            "ok": verdict["ok"],
            "applied": False,
            "context": {"performance": perf, "funnel": funnel},
        }
        if not (apply_now and verdict["ok"]):
            return out

        snap = self._settings_snapshot()
        risk_payload: dict = {}
        rotate_payload: dict = {}
        position_payload: dict = {}
        prev: dict = {}
        for c in verdict["changes"]:
            if c["key"] == "auto_top_k":
                rotate_payload["auto_top_k"] = int(c["to"])
                prev[f"market.{c['key']}"] = c["from"]
            elif c["key"] == "rotate_min_score":
                rotate_payload["min_score"] = float(c["to"])
                prev[f"market.rotate_min_score"] = c["from"]
            elif c["key"] in POSITION_KEYS:
                # 持仓管理员·LLM 接入里的数值项（冷却间隔），走它自己的 setter
                position_payload[c["key"]] = int(c["to"])
                prev[f"position.{c['key']}"] = c["from"]
            else:
                risk_payload[c["key"]] = c["to"]
                prev[f"risk.{c['key']}"] = c["from"]
        try:
            if risk_payload:
                self.set_risk(risk_payload)
            if position_payload:
                self.set_position_settings(position_payload)
            if rotate_payload:
                self.set_rotate(rotate_payload)
        except Exception:
            self._settings_restore(snap)
            raise
        self._push_settings_history({
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": "evolve", "name": "llm",
            "label": "LLM 自动调参",
            "diagnosis": str((prop or {}).get("diagnosis") or "")[:160],
            "prev": prev,
        })
        out["applied"] = True
        out["risk"] = self.risk_config()
        return out

    def settings_evolve_state(self) -> dict:
        st = self.engine.state.setdefault("settings_evolve", {})
        return {
            "enabled": bool(st.get("enabled")),
            "interval_hours": float(st.get("interval_hours") or 24),
            "last_at": float(st.get("last_at") or 0),
            "last_result": st.get("last_result") or "",
        }

    def set_settings_evolve(self, payload: dict) -> dict:
        """开关「每日自动调参」。开启后由自动循环末尾的钩子按间隔触发。"""
        st = self.engine.state.setdefault("settings_evolve", {})
        if "enabled" in payload:
            st["enabled"] = bool(payload["enabled"])
        if "interval_hours" in payload:
            try:
                v = float(payload["interval_hours"])
            except (TypeError, ValueError) as exc:
                raise ApiError("interval_hours 必须是数字", 400) from exc
            if not (1.0 <= v <= 168.0):
                raise ApiError("自动调参间隔只能在 1 ~ 168 小时之间", 400)
            st["interval_hours"] = v
        self.engine.state["settings_evolve"] = st
        try:
            self.engine._save()
        except Exception:  # noqa: BLE001
            pass
        return self.settings_evolve_state()

    def _auto_evolve_tick(self) -> None:
        """自动循环末尾调用：只做便宜的「到点了没」，到点才真跑（在独立线程里）。"""
        try:
            st = self.engine.state.get("settings_evolve") or {}
            if not st.get("enabled"):
                return
            every = float(st.get("interval_hours") or 24) * 3600.0
            if time.time() - float(st.get("last_at") or 0) < every:
                return
            if not self._evolve_lock.acquire(blocking=False):
                return
        except Exception:  # noqa: BLE001
            return

        def _run() -> None:
            try:
                stt = self.engine.state.setdefault("settings_evolve", {})
                stt["last_at"] = time.time()
                stt["last_result"] = "运行中"
                self.engine.state["settings_evolve"] = stt
                res = self.settings_evolve({"apply": True})
                stt["last_result"] = (
                    "已应用：" + "、".join(c["key"] for c in res.get("changes") or [])
                    if res.get("applied") else
                    ("无调整" if not (res.get("changes") or []) else "提案被拒：" +
                     "；".join(r.get("reason") or "" for r in res.get("rejected") or [])[:100])
                )
                self.engine.state["settings_evolve"] = stt
                self.engine._save()
            except Exception as exc:  # noqa: BLE001 - 自动调参失败不影响交易
                stt = self.engine.state.setdefault("settings_evolve", {})
                stt["last_result"] = f"失败：{type(exc).__name__}"
                self.engine.state["settings_evolve"] = stt
            finally:
                try:
                    self._evolve_lock.release()
                except RuntimeError:
                    pass

        threading.Thread(target=_run, daemon=True).start()

    def opportunities(self, payload: dict) -> dict:
        """机会信号排行榜：立即返回缓存结果，扫描放到后台线程（一次扫描约 10 秒）。

        同步扫描会把页面卡在「扫描中」，还会阻塞同进程的其它接口，所以这里只负责
        「确保有一次扫描在跑」，结果由前端轮询 status 里的 opportunities 拿到。
        """
        scan = self.engine.scan_state
        rows = self.engine.opportunities
        if (payload.get("refresh") or not rows) and not scan.get("scanning"):
            scan = self.engine.start_scan_async()
        return {
            "rows": rows,
            "scanning": bool(scan.get("scanning")),
            "last_scan_at": scan.get("last_at"),
            "scan_error": scan.get("error") or "",
            "auto_rotate": bool(self.cfg.market.auto_rotate),
            "min_score": float(self.cfg.market.rotate_min_score),
            "every_cycles": int(self.cfg.market.rotate_every_cycles),
            "current": self.cfg.market.symbol,
        }

    @_serialized
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
        if "auto_top_k" in payload:
            try:
                v = int(payload["auto_top_k"])
            except (TypeError, ValueError) as exc:
                raise ApiError("auto_top_k 必须是整数", 400) from exc
            if not (1 <= v <= 8):
                raise ApiError("每轮评估品种数只能在 1 ~ 8 之间", 400)
            m.auto_top_k = v
        try:
            save_config(self.cfg, config_path())
            saved = True
        except OSError:
            saved = False
        return {
            "auto_rotate": bool(m.auto_rotate),
            "min_score": float(m.rotate_min_score),
            "every_cycles": int(m.rotate_every_cycles),
            "auto_top_k": int(getattr(m, "auto_top_k", 1) or 1),
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
            "offpeak_research_only": bool(getattr(c, "offpeak_research_only", True)),
            "peak_mode": getattr(c, "peak_mode", "full"),
            "peak_holidays": list(getattr(c, "peak_holidays", None) or []),
            "offpeak": offpeak_info(c),
        }

    @_serialized
    def set_llm_settings(self, payload: dict) -> dict:
        """写 LLM 配置：provider/model/base_url/api_key 进 .env，峰谷策略进 config。"""
        from ..config import config_path, save_config

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

        policy_keys = ("offpeak_research_only", "peak_mode", "peak_holidays")
        want_policy = any(k in payload for k in policy_keys)
        if not updates and not want_policy:
            raise ApiError("没有需要保存的配置", 400)
        if updates:
            update_dotenv(updates)
            # 同步内存配置：TradingPipeline 每次决策时用 self.cfg.llm 新建，改这里即时生效
            if "CRYPTOAGENT_LLM_PROVIDER" in updates:
                self.cfg.llm.provider = updates["CRYPTOAGENT_LLM_PROVIDER"]
            if "CRYPTOAGENT_LLM_MODEL" in updates:
                self.cfg.llm.model = updates["CRYPTOAGENT_LLM_MODEL"]
            if "CRYPTOAGENT_LLM_BASE_URL" in updates:
                self.cfg.llm.base_url = updates["CRYPTOAGENT_LLM_BASE_URL"]

        saved = True
        if want_policy:
            if "offpeak_research_only" in payload:
                self.cfg.llm.offpeak_research_only = bool(payload["offpeak_research_only"])
            if "peak_mode" in payload:
                pm = str(payload["peak_mode"]).strip().lower()
                self.cfg.llm.peak_mode = pm if pm in ("full", "thrift") else "full"
            if "peak_holidays" in payload:
                raw = payload["peak_holidays"]
                if isinstance(raw, str):
                    raw = raw.replace("，", ",").split(",")
                self.cfg.llm.peak_holidays = [
                    str(x).strip() for x in (raw or []) if str(x).strip()
                ][:80]
            try:
                save_config(self.cfg, config_path())
            except OSError:
                saved = False
        # provider/model/Key 变了，持仓管理员的共享客户端也要按新配置重建
        self.engine.invalidate_position_llm()
        data = self.llm_settings()
        data["saved"] = saved
        return data

    def position_settings(self) -> dict:
        """读持仓管理员配置：LLM 接入开关 + 退出规则 + 用量/影子对照（只读诊断）。"""
        p = self.cfg.position
        # 实际生效值：L1/L3 学习会覆盖 config（只写 effective、不回写 config），
        # 设置页必须能展示两者差异，否则用户改的值与实际生效值脱节。
        try:
            eff = self.engine.effective_position_config()
            effective = {
                "reversal_ema_bars": int(eff.reversal_ema_bars),
                "peak_drawdown_r": float(eff.peak_drawdown_r),
            }
        except Exception:  # noqa: BLE001 - 轻量测试实例没有该方法
            effective = {}
        return {
            "enabled": bool(getattr(p, "enabled", True)),
            "effective": effective,
            "llm_explain": bool(p.llm_explain),
            "llm_shadow": bool(p.llm_shadow),
            "llm_offpeak_only": bool(p.llm_offpeak_only),
            "llm_min_interval_sec": int(p.llm_min_interval_sec),
            "auto_tune": bool(getattr(p, "auto_tune", True)),
            "evolution_mode": str(getattr(p, "evolution_mode", "stats")),
            "evolution_rule_min_samples": int(getattr(p, "evolution_rule_min_samples", 8)),
            "evolution_confirm_rounds": int(getattr(p, "evolution_confirm_rounds", 2)),
            "evolution_replay_min_cases": int(getattr(p, "evolution_replay_min_cases", 20)),
            "evolution_replay_window": int(getattr(p, "evolution_replay_window", 100)),
            "evolution_replay_min_median_delta": float(getattr(p, "evolution_replay_min_median_delta", 0.0)),
            "evolution_replay_max_worst_delta": float(getattr(p, "evolution_replay_max_worst_delta", 0.2)),
            "evolution_l2_stable_days": int(getattr(p, "evolution_l2_stable_days", 0)),
            "evolution_llm_enabled": bool(getattr(p, "evolution_llm_enabled", False)),
            "evolution_llm_allow_action": bool(getattr(p, "evolution_llm_allow_action", False)),
            "evolution_llm_allow_loosen": bool(getattr(p, "evolution_llm_allow_loosen", False)),
            "evolution_llm_min_confidence": float(getattr(p, "evolution_llm_min_confidence", 0.65)),
            "evolution_llm_max_param_step": float(getattr(p, "evolution_llm_max_param_step", 0.15)),
            "evolution_llm_shadow_cases": int(getattr(p, "evolution_llm_shadow_cases", 20)),
            "evolution_rollback_after_losses": int(getattr(p, "evolution_rollback_after_losses", 3)),
            "evolution_rollback_drawdown_r": float(getattr(p, "evolution_rollback_drawdown_r", 2.0)),
            "tuning": self.engine.position_tuning_status(),
            "replay_job": self.engine.replay_validation_status(),
            "bars_from_index": bool(p.bars_from_index),
            "reversal_ema_bars": int(p.reversal_ema_bars),
            "composite_max_age_sec": int(p.composite_max_age_sec),
            "peak_drawdown_exit_enabled": bool(p.peak_drawdown_exit_enabled),
            "peak_drawdown_r": float(p.peak_drawdown_r),
            "offpeak": offpeak_info(self.cfg.llm),
            "usage": dict(self.engine.position_llm_usage),
            "shadow_recent": list(self.engine.llm_shadow_log[-10:]),
        }

    @_serialized
    def set_position_settings(self, payload: dict) -> dict:
        """热更新持仓管理员配置：WebApp 与 Engine 共享同一个 AppConfig，改完立即生效。

        LLM 配置变更顺带失效共享客户端；所有数值都做范围校验，畸形输入整体不动。
        """
        from ..config import config_path, save_config

        p = self.cfg.position
        bool_keys = ("enabled", "llm_explain", "llm_shadow", "llm_offpeak_only",
                     "bars_from_index", "peak_drawdown_exit_enabled", "auto_tune",
                     "evolution_llm_enabled", "evolution_llm_allow_action",
                     "evolution_llm_allow_loosen")
        int_keys = {
            "llm_min_interval_sec": (0, 86400),
            "reversal_ema_bars": (16, 96),
            "composite_max_age_sec": (0, 3600),
            "evolution_rule_min_samples": (5, 100),
            "evolution_confirm_rounds": (1, 10),
            "evolution_replay_min_cases": (5, 200),
            "evolution_replay_window": (20, 500),
            "evolution_l2_stable_days": (0, 365),
            "evolution_llm_shadow_cases": (0, 200),
            "evolution_rollback_after_losses": (1, 20),
        }
        float_keys = {
            "peak_drawdown_r": (0.5, 2.0),
            "evolution_replay_min_median_delta": (-1.0, 1.0),
            "evolution_replay_max_worst_delta": (0.0, 5.0),
            "evolution_llm_min_confidence": (0.0, 1.0),
            "evolution_llm_max_param_step": (0.01, 0.5),
            "evolution_rollback_drawdown_r": (0.1, 20.0),
        }
        if not any(k in payload for k in (*bool_keys, *int_keys, *float_keys, "evolution_mode")):
            raise ApiError("没有需要保存的配置", 400)

        staged: dict = {}
        if "evolution_mode" in payload:
            mode = str(payload["evolution_mode"]).strip().lower()
            if mode not in ("off", "stats", "replay", "llm"):
                raise ApiError("evolution_mode 只能是 off / stats / replay / llm", 400)
            staged["evolution_mode"] = mode
        # 门控：L3 必须先过 L2 回放；稳定等待期默认 0（L2 通过即放行），不满足直接 400
        target_mode = staged.get("evolution_mode", str(getattr(p, "evolution_mode", "stats")))
        if target_mode == "llm":
            gate = self.engine.position_tuning.get("gate") or {}
            if not gate.get("l2_passed"):
                raise ApiError(
                    "L3 需要先通过 L2 反事实回放门控：请先切到 replay、积累平仓样本并让门控通过",
                    400,
                )
            enabled = bool(payload.get(
                "evolution_llm_enabled",
                staged.get("evolution_llm_enabled", getattr(p, "evolution_llm_enabled", False)),
            ))
            if not enabled:
                raise ApiError("L3 需要显式开启 evolution_llm_enabled", 400)
            days = int(payload.get(
                "evolution_l2_stable_days",
                staged.get("evolution_l2_stable_days", getattr(p, "evolution_l2_stable_days", 0)),
            ))
            since = float(gate.get("l2_stable_since") or 0.0)
            if days > 0 and since and (time.time() - since) < days * 86400:
                raise ApiError(f"L2 需稳定运行 {days} 天后再评估 L3", 400)
        if target_mode == "replay" and not (self.engine.position_tuning.get("replay_cases") or []):
            raise ApiError("L2 需要至少一笔已平仓样本；先运行一段时间再切换", 400)
        for k in bool_keys:
            if k in payload:
                staged[k] = bool(payload[k])
        for k, (lo, hi) in int_keys.items():
            if k in payload:
                try:
                    v = int(payload[k])
                except (TypeError, ValueError):
                    raise ApiError(f"{k} 必须是整数", 400)
                if not lo <= v <= hi:
                    raise ApiError(f"{k} 需在 {lo} ~ {hi} 之间", 400)
                staged[k] = v
        for k, (lo, hi) in float_keys.items():
            if k in payload:
                try:
                    v = float(payload[k])
                except (TypeError, ValueError):
                    raise ApiError(f"{k} 必须是数字", 400)
                if not lo <= v <= hi:
                    raise ApiError(f"{k} 需在 {lo} ~ {hi} 之间", 400)
                staged[k] = v
        for k, v in staged.items():
            setattr(p, k, v)
        self.engine.invalidate_position_llm()
        saved = True
        try:
            save_config(self.cfg, config_path())
        except OSError:
            saved = False
        data = self.position_settings()
        data["saved"] = saved
        return data

    def sentiment_settings(self) -> dict:
        """读当前情绪/新闻源配置（A1）。api_key 脱敏，只回显是否已设置 + 尾号。"""
        s = self.cfg.sentiment
        key = os.getenv(s.cryptopanic_api_key_env, "")
        return {
            "enabled": s.enabled,
            "source": s.source,
            "cryptopanic_api_key_env": s.cryptopanic_api_key_env,
            "cryptopanic_key_set": bool(key),
            "cryptopanic_key_tail": key[-4:] if key else "",
        }

    @_serialized
    def set_sentiment_settings(self, payload: dict) -> dict:
        """写情绪/新闻源配置（A1）：开关、源选择进 config/default.json，Key 进 .env。"""
        from ..config import config_path, save_config

        s = self.cfg.sentiment
        changed = False
        if "enabled" in payload:
            s.enabled = bool(payload["enabled"])
            changed = True
        if "source" in payload:
            src = str(payload["source"]).strip().lower()
            if src not in ("price_action", "cryptopanic"):
                raise ApiError("不支持的新闻源：只能是 price_action 或 cryptopanic", 400)
            s.source = src
            changed = True

        updates: dict[str, str] = {}
        api_key = str(payload.get("cryptopanic_api_key", "")).strip()
        if api_key:
            updates[s.cryptopanic_api_key_env] = api_key
        if updates:
            update_dotenv(updates)

        saved = bool(updates)
        if changed:
            try:
                save_config(self.cfg, config_path())
                saved = True
            except OSError:
                saved = False  # 只读文件系统：内存里仍然生效
        return {**self.sentiment_settings(), "saved": saved}

    def notify_settings(self) -> dict:
        """读当前通知/告警配置（A2）。外部通道凭据只回显是否已设置。"""
        n = self.cfg.notify
        return {
            "enabled": n.enabled,
            "channels": list(n.channels),
            "webhook_url_env": n.webhook_url_env,
            "telegram_token_env": n.telegram_token_env,
            "telegram_chat_env": n.telegram_chat_env,
            "email_smtp_host_env": n.email_smtp_host_env,
            "email_smtp_port_env": n.email_smtp_port_env,
            "email_user_env": n.email_user_env,
            "email_pass_env": n.email_pass_env,
            "email_to_env": n.email_to_env,
            "webhook_set": bool(os.getenv(n.webhook_url_env, "")),
            "telegram_set": bool(os.getenv(n.telegram_token_env, "") and os.getenv(n.telegram_chat_env, "")),
            "email_set": bool(os.getenv(n.email_smtp_host_env, "")),
        }

    @_serialized
    def set_notify_settings(self, payload: dict) -> dict:
        """写通知/告警配置（A2）：主开关、通道进 config/default.json，外部凭据进 .env。"""
        from ..config import config_path, save_config

        n = self.cfg.notify
        valid = {"console", "inapp", "webhook", "telegram", "email"}
        changed = False
        if "enabled" in payload:
            n.enabled = bool(payload["enabled"])
            changed = True
        if "channels" in payload:
            channels = [str(c).strip() for c in payload["channels"] if str(c).strip() in valid]
            # 至少保留一个本地通道，避免通知全关后彻底无迹可查
            if not any(c in ("console", "inapp") for c in channels):
                channels.append("inapp")
            n.channels = channels
            changed = True

        updates: dict[str, str] = {}
        for key, env in (
            ("webhook_url", n.webhook_url_env),
            ("telegram_token", n.telegram_token_env),
            ("telegram_chat", n.telegram_chat_env),
            ("email_smtp_host", n.email_smtp_host_env),
            ("email_smtp_port", n.email_smtp_port_env),
            ("email_user", n.email_user_env),
            ("email_pass", n.email_pass_env),
            ("email_to", n.email_to_env),
        ):
            v = str(payload.get(key, "")).strip()
            if v:
                updates[env] = v
        if updates:
            update_dotenv(updates)

        saved = bool(updates)
        if changed:
            try:
                save_config(self.cfg, config_path())
                saved = True
            except OSError:
                saved = False  # 只读文件系统：内存里仍然生效
        return {**self.notify_settings(), "saved": saved}

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
            return self.engine.arm(
                mode, confirm, testnet=testnet, exchange=exchange,
                force=bool(payload.get("force", False)),
            )
        except PermissionError as exc:
            raise ApiError(str(exc), 403) from exc
        except ExchangeError as exc:
            raise ApiError(str(exc), 502) from exc

    def _fill_with_plan(self, row: dict) -> dict:
        """交易所成交流水没有「计划价 / 滑点」，用本地 fills 按订单号补上。"""
        oid = str(row.get("order_id") or "")
        if not oid:
            return row
        for f in reversed(self.engine.state.get("fills") or []):
            if str(f.get("order_id") or "") == oid or str(f.get("reason") or "").endswith(oid):
                if f.get("plan_price") is not None:
                    row["plan_price"] = f["plan_price"]
                if f.get("slippage_bps") is not None:
                    row["slippage_bps"] = f["slippage_bps"]
                break
        return row

    def exchange_status(self) -> dict:
        from ..execution import read_api_keys

        e = self.engine
        exchange = e.state.get("exchange", "binance")
        testnet = bool(e.state.get("testnet", True))
        k, s, p = read_api_keys(exchange, testnet)
        keys_present = bool(k and s and (p if exchange == "okx" else True))
        out = {
            "exchange": exchange,
            "keys_binance": bool(os.getenv("BINANCE_API_KEY")),
            "keys_okx": bool(os.getenv("OKX_API_KEY")),
            "keys_present": keys_present,
            "keys_binance_demo": bool(os.getenv("BINANCE_DEMO_API_KEY")) or bool(os.getenv("BINANCE_API_KEY")),
            "keys_okx_demo": bool(os.getenv("OKX_DEMO_API_KEY")) or bool(os.getenv("OKX_API_KEY")),
            "keys_binance_live": bool(os.getenv("BINANCE_API_KEY")),
            "keys_okx_live": bool(os.getenv("OKX_API_KEY")),
            "armed": e.armed,
            "mode": e.state["mode"],
            "testnet": testnet,
            "connected": False,
            "account": None,
            "positions": [],
            "error": "",
        }
        if not keys_present or not e.armed:
            if not keys_present:
                out["error"] = f"未设置 {exchange} {'模拟盘' if testnet else '主网'} API Key"
            return out
        try:
            client = e.live_client or e.connect(testnet=testnet, exchange=exchange)
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
    server_version = "QeeLin-Agent/1.2.1"
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
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except (TypeError, ValueError) as exc:
            raise ApiError("Content-Length 非法", 400) from exc
        # 本地工具也要有上限：畸形/超大 body 会把进程内存吃满
        if length < 0 or length > 1024 * 1024:
            raise ApiError("请求体过大（上限 1 MiB）", 413)
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
        比较用 compare_digest：普通 == 会在首个不同字节处提前返回，理论上
        可以被响应时间差推断令牌前缀。
        """
        token = self._token()
        if not token:
            return True

        def _same(value: str) -> bool:
            return hmac.compare_digest(str(value).encode("utf-8"), token.encode("utf-8"))

        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer ") and _same(header[7:].strip()):
            return True
        cookie = self.headers.get("Cookie", "") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "ca_token" and _same(v):
                return True
        if query and _same(query.get("token", [""])[0]):
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
            f"<pre style='background:#121722;border:1px solid #222b3a;border-radius:8px;padding:12px;overflow:auto'>{_html.escape(str(env_path))}</pre>"
            "<p>Windows 上查看：</p>"
            "<pre style='background:#121722;border:1px solid #222b3a;border-radius:8px;padding:12px'>"
            "Get-Content C:\\crypto-agent\\.env | Select-String TOKEN</pre>"
            "<p>Linux 上查看：</p>"
            "<pre style='background:#121722;border:1px solid #222b3a;border-radius:8px;padding:12px'>"
            "grep TOKEN /opt/crypto-agent/.env</pre>"
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

    def _origin_ok(self) -> bool:
        """拒绝跨站写请求。

        浏览器对「简单请求」（text/plain、无自定义头）不做预检，同源策略只阻止
        读响应、并不阻止请求送达 127.0.0.1——不校验 Origin 的话，任意网页都能
        对着本机 8000 端口发 POST 触发下单/平仓/熔断。
        """
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True                      # 非浏览器客户端（curl / 脚本）
        if origin == "null":
            return False
        try:
            netloc = urllib.parse.urlsplit(origin).netloc
        except ValueError:
            return False
        return netloc == (self.headers.get("Host") or "").strip()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        # 与 GET 保持一致：POST 也接受 ?token=（脚本/接口调用常用）
        if not self._auth_ok(urllib.parse.parse_qs(parsed.query)):
            return self._deny()
        if not self._origin_ok():
            return self._json({"ok": False, "error": "跨站写请求被拒绝"}, 403)
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
        if path == "/api/accounts":
            # 每个账户一份摘要 + 跨账户合计（LLM 与「账户对比」共用）
            return self._json({"ok": True, "data": app.engine.accounts_snapshot()})
        if path == "/api/accounts/trades":
            # 明细查询：?scope=okx:demo&kind=fills&since=2026-11-01&symbol=BTCUSDT
            scope = (query.get("scope", [""])[0] or "").strip()
            kind = (query.get("kind", ["position_history"])[0] or "position_history").strip()
            rows = app.engine.accounts.query(
                scope=scope or None,
                kind=kind,
                since=(query.get("since", [""])[0] or "").strip(),
                until=(query.get("until", [""])[0] or "").strip(),
                symbol=(query.get("symbol", [""])[0] or "").strip(),
                limit=_as_int(query.get("limit", ["200"])[0], "limit", 200, 1, 1000),
            )
            return self._json(
                {
                    "ok": True,
                    "data": {"scope": scope or "*", "kind": kind, "count": len(rows), "rows": rows},
                }
            )
        if path == "/api/klines":
            return self._json(
                {
                    "ok": True,
                    "data": app.klines(
                        query.get("interval", [""])[0],
                        _as_int(query.get("limit", ["240"])[0], "limit", 240, 1, 1000),
                    ),
                }
            )
        if path == "/api/research/latest":
            return self._json({"ok": True, "data": app.latest_research()})
        if path == "/api/exchange/status":
            return self._json({"ok": True, "data": app.exchange_status()})
        if path == "/api/exchange/test":
            exchange = query.get("exchange", ["binance"])[0]
            mode = query.get("mode", [""])[0] or None
            return self._json({"ok": True, "data": app.exchange_test(exchange, mode)})
        if path == "/api/live":
            return self._json({"ok": True, "data": app.live_status()})
        if path == "/api/symbols":
            return self._json({"ok": True, "data": app.symbols()})
        if path == "/api/symbols/candidates":
            return self._json(
                {"ok": True, "data": app.symbol_candidates(query.get("q", [""])[0])}
            )
        if path == "/api/settings/llm":
            return self._json({"ok": True, "data": app.llm_settings()})
        if path == "/api/settings/sentiment":
            return self._json({"ok": True, "data": app.sentiment_settings()})
        if path == "/api/settings/notify":
            return self._json({"ok": True, "data": app.notify_settings()})
        if path == "/api/settings/position":
            return self._json({"ok": True, "data": app.position_settings()})
        if path == "/api/settings/profiles":
            return self._json({"ok": True, "data": app.settings_profiles()})
        if path == "/api/settings/evolve":
            return self._json({"ok": True, "data": app.settings_evolve_state()})
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
            data = app.decision(_as_int(payload.get("rounds", 2), "rounds", 2, 1, 5))
            return self._json({"ok": True, "data": data})
        if path == "/api/research":
            data = app.research(_as_int(payload.get("iterations", 6), "iterations", 6, 1, 50))
            return self._json({"ok": True, "data": data})
        if path == "/api/research/auto":
            return self._json({"ok": True, "data": app.set_research_auto(payload)})
        if path == "/api/trading/arm":
            prev_scope = str(eng.state.get("account_scope") or "")
            res = app.arm(payload)
            app.invalidate_exchange_cache()   # 刚解锁：总览页要立刻改读交易所
            if str(eng.state.get("account_scope") or "") != prev_scope:
                # 账户作用域变了（含首次解锁）：旧账户的权益曲线不能接在新账户上
                app.reset_exchange_view()
            return self._json({"ok": True, "data": res})
        if path == "/api/trading/disarm":
            res = eng.disarm()
            app.reset_exchange_view()
            return self._json({"ok": True, "data": res})
        if path == "/api/accounts/reset":
            # 只清当前账户（或指定账户）的权益曲线 / 成交 / 持仓历史
            scope = str(payload.get("scope") or "").strip()
            if scope and (scope.count(":") != 1 or scope.split(":")[1] not in ("demo", "live")):
                raise ApiError("scope 格式应为 exchange:demo 或 exchange:live", 400)
            res = eng.reset_account_records(scope or None)
            # 交易所视图（成交 / 当日盈亏）也要跟着锚点重算，先失效缓存
            app.reset_exchange_view()
            return self._json({"ok": True, "data": res})
        if path == "/api/trading/execute":
            res = eng.execute(price=app.live_price, reason="web")
            app.invalidate_exchange_cache()
            return self._json({"ok": True, "data": res})
        if path == "/api/trading/close":
            # 多品种：不传 symbol 时平「当前品种」（单仓时自动落到唯一那笔）
            res = eng.close(price=app.live_price, reason="web",
                            symbol=(payload.get("symbol") or None))
            app.invalidate_exchange_cache()
            return self._json({"ok": True, "data": res})
        if path == "/api/risk":
            return self._json({"ok": True, "data": app.set_risk(payload)})
        if path == "/api/trading/auto-arm":
            return self._json({"ok": True, "data": app.set_auto_arm(payload)})
        if path == "/api/trading/auto-resume":
            return self._json({"ok": True, "data": app.set_auto_resume(payload)})
        if path == "/api/trading/auto":
            if payload.get("running"):
                data = eng.start_auto(
                    _as_int(payload.get("interval_sec", 300), "interval_sec", 300, 30, 86400),
                    bool(payload.get("execute", False)),
                )
            else:
                data = eng.stop_auto()
            return self._json({"ok": True, "data": data})
        if path == "/api/killswitch":
            return self._json({"ok": True, "data": eng.set_halt(bool(payload.get("on", False)))})
        if path == "/api/notifications":
            return self._json({"ok": True, "data": app.notifications(
                _as_int(payload.get("limit", 50), "limit", 50, 1, 500))})
        if path == "/api/notifications/clear":
            return self._json({"ok": True, "data": app.clear_notifications()})
        if path == "/api/exchange/sync":
            return self._json({"ok": True, "data": app.exchange_status()})
        if path == "/api/exchange/keys":
            return self._json({"ok": True, "data": app.save_keys(payload)})
        if path == "/api/settings/llm":
            return self._json({"ok": True, "data": app.set_llm_settings(payload)})
        if path == "/api/settings/sentiment":
            return self._json({"ok": True, "data": app.set_sentiment_settings(payload)})
        if path == "/api/settings/notify":
            return self._json({"ok": True, "data": app.set_notify_settings(payload)})
        if path == "/api/settings/position/replay":
            return self._json({"ok": True, "data": app.engine.start_replay_validation()})
        if path == "/api/settings/position":
            return self._json({"ok": True, "data": app.set_position_settings(payload)})
        if path == "/api/settings/profile":
            return self._json({"ok": True, "data": app.apply_settings_profile(payload)})
        if path == "/api/settings/rollback":
            return self._json({"ok": True, "data": app.rollback_settings()})
        if path == "/api/settings/evolve":
            return self._json({"ok": True, "data": app.settings_evolve(payload)})
        if path == "/api/settings/evolve/auto":
            return self._json({"ok": True, "data": app.set_settings_evolve(payload)})
        if path == "/api/live":
            data = app.start_live() if payload.get("on", True) else app.stop_live()
            return self._json({"ok": True, "data": data})
        if path == "/api/symbol":
            return self._json({"ok": True, "data": app.set_symbol(payload)})
        if path == "/api/symbols/add":
            return self._json({"ok": True, "data": app.add_symbol(payload)})
        if path == "/api/symbols/remove":
            return self._json({"ok": True, "data": app.remove_symbol(payload)})
        if path == "/api/symbols/pin":
            return self._json({"ok": True, "data": app.set_symbol_pin(payload)})
        if path == "/api/scan":
            return self._json({"ok": True, "data": app.scan(payload)})
        if path == "/api/opportunity":
            return self._json({"ok": True, "data": app.opportunities(payload)})
        if path == "/api/rotate":
            return self._json({"ok": True, "data": app.set_rotate(payload)})
        raise ApiError(f"未知接口 {path}", 404)

    # -- 静态文件 -----------------------------------------------------
    def _static(self, path: str) -> None:
        if path in ("/", ""):
            rel = "index.html"
        elif path.startswith("/static/"):
            rel = path[len("/static/"):]
        else:
            rel = path.lstrip("/")
        base = STATIC_DIR.resolve()
        target = (base / rel).resolve()
        # 用路径包含关系而不是字符串前缀：字符串 startswith 会被
        # 「../static_evil/x」这类同前缀兄弟目录绕过。
        if not target.is_relative_to(base) or not target.is_file():
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