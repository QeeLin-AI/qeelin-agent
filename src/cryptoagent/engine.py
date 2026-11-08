"""交易引擎：把「决策」变成「持仓」，并持久化状态。

职责边界：
- 引擎只负责**执行**决策（下单/平仓/止损），不做任何分析。
- 模拟盘（paper）与实盘（live）共用同一套状态机，差别只在撮合来源。
- 实盘必须显式 arm，且 arm 状态**不落盘**——服务重启后自动回到 paper。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd

from .accounts import AccountStore, scope_of
from .agents.calibration import record_accuracy, summary as calibration_summary, weights_from_accuracy
from .agents.pipeline import Decision, TradingPipeline, render_markdown, round_price
from .agents.position_calibration import (
    BOUNDS as TUNE_BOUNDS,
    DEFAULTS as TUNE_DEFAULTS,
    L1_TUNABLE,
    RULE_PARAMS,
    TUNABLE as TUNING_KEYS,
    record_episode,
    learned_params,
    summary as tuning_summary,
)
from .agents.position_replay import evaluate as replay_evaluate
from .agents.position_evolver import propose as propose_evolution
from .agents.position_manager import manage_position, review_management
from .config import AppConfig
from .data import (
    KL_INTERVALS,
    fetch_klines,
    fetch_live_price,
    kl_disk_ttl,
    load_market,
    set_kl_cache_dir,
)
from .derivatives import enrich_derivatives
from .execution import BinanceFuturesClient, OKXClient, ExchangeError, account_equity, read_api_keys
from .notify import get_notifier
from .risk import AccountState


def _today() -> str:
    return time.strftime("%Y-%m-%d")


class TradingEngine:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.lock = threading.RLock()
        # 进程/引擎启动时刻：页面上显示「已运行多久」，用来一眼判断是不是刚重启过
        # （重启会丢 arm 状态、清空缓存，很多异常排查的第一步就是确认这个）。
        self.started_at = time.time()
        self.state_path = Path(cfg.runs_dir) / "engine_state.json"
        # 一账户一文件的账本（权益 / 成交 / 平仓历史）。engine_state.json 仍是
        # 「当前账户的工作副本 + 运行时状态」，账户文件才是长期账本。
        self.accounts = AccountStore(Path(cfg.runs_dir) / "accounts")
        self.state = self._default_state()
        self._load()
        self._init_account_store()
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
        # 多品种：移动止损的节流必须按品种独立，否则两个仓位会互相压制
        self._stop_adjust_at_by_symbol: dict[str, float] = dict(
            self.state.setdefault("last_stop_adjust_by_symbol", {}) or {}
        )
        self.last_management: dict = {}
        self.last_management_by_symbol: dict[str, dict] = {}
        # 持仓管理员 LLM：共享客户端、每仓冷却、最近一次动作、影子对照日志。
        # 后四项随 state 落盘，重启后解释/审计不会丢，也不会重复烧 token。
        self._position_llm = None
        self._position_llm_at: dict[str, float] = dict(
            self.state.setdefault("position_llm_at", {}) or {}
        )
        self.last_management_action: dict = dict(
            self.state.get("last_management_action") or {}
        )
        self.llm_shadow_log: list[dict] = list(
            self.state.get("llm_shadow_log") or []
        )[-50:]
        self.position_llm_usage: dict = dict(self.state.get("position_llm_usage") or {})
        for _k, _v in {"calls": 0, "errors": 0, "skipped_offpeak": 0,
                       "skipped_cooldown": 0}.items():
            self.position_llm_usage.setdefault(_k, _v)
        # 持仓管理员自迭代：绩效样本 + 当前生效参数 + 配置锚点。
        # 锚点用来识别「用户手动改过配置」——一旦改了，学习结果立即作废、以配置为准。
        _tune = self.state.get("position_tuning") or {}
        _llm_state = dict(_tune.get("llm") or {})
        for _k, _default in {"proposals": [], "applied": [], "shadow": [],
                             "rollbacks": [], "streak_loss": 0,
                             "cum_r": 0.0}.items():
            _llm_state.setdefault(_k, _default)
        self.position_tuning: dict = {
            "stats": dict(_tune.get("stats") or {}),
            "effective": dict(_tune.get("effective") or {}),
            "anchor": dict(_tune.get("anchor") or {}),
            "changes": list(_tune.get("changes") or [])[-10:],
            # L1：连续确认的待落地改动
            "pending": dict(_tune.get("pending") or {}),
            # L2：反事实回放的样本与最近一次门控结果
            "replay_cases": list(_tune.get("replay_cases") or [])[-200:],
            "replay": dict(_tune.get("replay") or {}),
            "gate": dict(_tune.get("gate") or {}),
            # L3：LLM 提案 / 影子 / 应用 / 回退
            "llm": _llm_state,
            "updated_at": float(_tune.get("updated_at") or 0.0),
        }
        # 各品种最新价（非活跃品种也要实时刷新，标记价/浮盈都读它）
        self._last_prices: dict[str, float] = {}
        self._price_ts: dict[str, float] = {}
        self._quote_cache: dict[str, tuple[float, float]] = {}
        self._frame_cache: dict[str, tuple[pd.DataFrame, float]] = {}
        # 自动循环末尾的钩子（WebApp 注册，用于「到点没到点」的自动调参检查）。
        # 引擎不反向依赖 WebApp，所以只留一个可空回调；未注册时什么也不做。
        self.on_cycle_hook = None
        # 持仓管理守护线程：不依赖「启动自动循环」，手动/半自动模式下开仓后
        # 也能做移动止损、时间止损与趋势反转退出。
        self._guard_lock = threading.Lock()
        self._guard_stop = threading.Event()
        self._guard_thread: threading.Thread | None = None
        self._guard_interval = 30
        # 报价线程：30 秒的管理轮询喂不动页面上的标记价，单独用毫秒级 ticker
        # 每 4 秒刷一次持仓品种的最新价（只看有仓的品种，空仓时零请求）。
        self._quote_stop = threading.Event()
        self._quote_thread: threading.Thread | None = None
        self._quote_interval = 4
        # 全品种缓存守护：高频档一轮要切多个品种，提前把每个品种的报价与 K 线
        # 焐热，切换时只读本地盘缓存（否则 load_market 会当场重下 6000 根）。
        self._warm_stop = threading.Event()
        self._warm_thread: threading.Thread | None = None
        self._warm_interval = 15
        self._warm_busy = False
        self._warm_main_at = 0.0
        self._warm_last = 0.0
        self._warm_last_result: dict = {}
        self._kl_short_warm_at: dict[str, float] = {}
        self._composite_cache: dict = {}
        # 行情 WebSocket：持仓品种最新价由交易所实时推送（断线自动回退 REST）。
        self._ws_stop = threading.Event()
        self._ws_thread = None
        self._ws_enabled = bool(getattr(self.cfg.market, "ws_quotes", True))
        self.start_position_guard()
        self.start_quote_guard()
        self.start_symbol_cache_guard()
        self.start_ws_guard()

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
            # positions 是唯一真相（key = 品种）；position 只是「当前品种」的兼容视图，
            # 老前端 / 老存档 / 单仓调用方都还在读它。
            "positions": {},
            "position": None,
            # 账户作用域（exchange:demo / exchange:live）：切换账户时据此重置账户级状态
            "account_scope": "",
            "fills": [],
            "position_history": [],
            "consecutive_losses": 0,
            # 重置账户的时间锚点：0=从未重置。交易所真实成交删不掉，重置靠
            # 记下「重置时刻 / 当时权益」，让 overlay 只显示这一刻之后的成交与盈亏。
            "account_reset_epoch": 0,
            "account_reset_equity": 0.0,
            # 资金费入账去重锚点（毫秒）：只入账该时间戳之后新产生的 FUNDING_FEE
            "funding_synced_until_ms": 0,
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
        # 旧存档只有单仓 state["position"]：迁进 positions 字典（多品种的唯一真相）。
        # 不迁移的话，升级后重启会把已有仓位当成空仓，等于凭空多出一张裸单。
        if not isinstance(self.state.get("positions"), dict):
            self.state["positions"] = {}
        legacy = self.state.get("position")
        if isinstance(legacy, dict) and not self.state["positions"]:
            sym = str(legacy.get("symbol") or self.cfg.market.symbol).upper()
            # 必须把品种写回仓位本体：老存档只有 map key、没有 symbol 字段，
            # 一旦 auto_rotate 换过品种，close()/撤单就会拿到「当前品种」而平错对象。
            legacy.setdefault("symbol", sym)
            legacy["symbol"] = sym
            self.state["positions"][sym] = legacy
        # 视图不能是 None：有仓位时 `position` 为 None 会让老前端显示「空仓」
        if self.state["positions"] and not self.state.get("position"):
            self.state["position"] = next(iter(self.state["positions"].values()))
        # 注意：这里不能给旧流水补账户标签。标签必须由 `_init_account_store()`
        # 在「按时间边界拆账」之后补，否则币安时期的历史会被先钉成当前账户。

    def _init_account_store(self) -> None:
        """初始化分账户账本。

        首次启动（runs/accounts 还没有 marker）把旧的单文件账本按账户拆开；
        之后每次启动只把当前账户的账本灌进 state。拆账失败就退回单文件模式，
        绝不因为迁移失败而清空流水。
        """
        try:
            result = self.accounts.migrate(self.state, self.state_path)
        except Exception as exc:  # noqa: BLE001 - 迁移失败不能挡住启动
            print(f"[accounts] 账本拆分失败，沿用单文件模式：{type(exc).__name__}: {exc}")
            return
        if result.get("migrated"):
            print(f"[accounts] 已按账户拆分旧流水：{result.get('accounts')}")
            self._save()
        scope = self._scope()
        try:
            self.accounts.apply_to_state(scope, self.state)
        except Exception as exc:  # noqa: BLE001
            print(f"[accounts] 加载 {scope} 账本失败：{type(exc).__name__}: {exc}")
            return
        # 兜底：拆账后仍无标签的行只能是当前账户运行时产生的
        self._tag_legacy_ledger()

    def _prune_decisions(self, out: Path) -> None:
        """清掉过期决策快照，每小时最多跑一次。

        自动循环 5 分钟一轮 = 每天 288 份、每个约 26KB。不清就是一年十万个
        文件、两三个 G：磁盘不会立刻满，但 ls、打包备份、迁移会越来越难受。
        只动 `*.json`，`latest.md` 和目录本身保留。
        """
        now = time.time()
        if now - getattr(self, "_prune_at", 0.0) < 3600:
            return
        self._prune_at = now
        keep_days = int(getattr(self.cfg, "decision_keep_days", 7) or 7)
        cutoff = now - keep_days * 86400
        try:
            for f in out.glob("*.json"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                except OSError:  # 单个文件删不掉就跳过，别中断决策
                    pass
        except Exception:  # noqa: BLE001
            pass

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(self.state_path)
        # 同步当前账户的长期账本：进程被 kill 也不会丢成交与权益曲线
        store = getattr(self, "accounts", None)
        if store is not None:
            try:
                store.save_state(self._scope(), self.state)
            except Exception:  # noqa: BLE001 - 账户账本写失败不影响主流程
                pass
        self._backup_state()

    def _backup_state(self) -> None:
        """每天留一份账本快照。

        state 里是持仓、权益、成交历史，写坏或被误删**没有第二份可回退**
        （当初从 Windows 迁过来时，这个文件是唯一必须手工搬运的账本）。
        按天留 14 份，整个目录不到 1MB。
        """
        try:
            day = time.strftime("%Y%m%d")
            if getattr(self, "_backup_day", None) == day:
                return
            self._backup_day = day
            if not self.state_path.exists():
                return
            dst = self.state_path.parent / "backup"
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.state_path, dst / f"engine_state-{day}.json")
            for old in sorted(dst.glob("engine_state-*.json"))[:-14]:
                old.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 - 备份失败绝不能影响正常记账
            pass

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
                from .data import _DEAD_UNTIL

                # 保留 K 线缓存：进程内缓存 key 带交易所，磁盘文件名带 .binance/.okx
                # 后缀，天然隔离，不需要清。只重置「失败熔断」，让新交易所重新探测。
                _DEAD_UNTIL.clear()
                self.refresh_data(force=True)
                print(f"[data] 行情源已切到 {exchange}：{self.data_source}")
                if exchange == "okx" and not str(self.data_source).startswith("okx"):
                    self._retry_okx_source()
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"切换行情源失败：{type(exc).__name__}: {exc}"
                print(f"[data] {self.last_error}")
            finally:
                self.data_loading = False
                self._reloading = False

        threading.Thread(target=_worker, daemon=True, name="reload-market").start()

    def _retry_okx_source(self, max_attempts: int = 4, base_delay: float = 6.0) -> None:
        """OKX 行情源 fallback 到别家后，后台退避重试直到切回 OKX。

        OKX 历史 K 线要翻页 6000 根（60 页串行），偶发超时会被 fetch_binance
        标记 dead 10 分钟（DEAD_COOLDOWN）；此后每次刷新都跳过 OKX、一直显示
        「币安永续」，用户切到 OKX 却看到币安标签会以为没切成功。这里在后台清
        熔断 + force 重拉，成功即恢复，避免来源标签长期挂错。
        """
        if self.state.get("exchange", "binance") != "okx":
            return

        def _worker() -> None:
            from .data import OKX, _DEAD_UNTIL

            for i in range(max_attempts):
                time.sleep(base_delay * (i + 1))
                try:
                    _DEAD_UNTIL.pop(OKX, None)
                    _df, src = self.refresh_data(force=True)
                    if str(src or "").startswith("okx"):
                        print(f"[data] 行情源已恢复 okx：{src}")
                        return
                except Exception:  # noqa: BLE001 - 单次失败继续下一次重试
                    pass

        threading.Thread(target=_worker, daemon=True, name="okx-source-retry").start()

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

    # ------------------------------------------------------------------
    # 账户作用域
    # ------------------------------------------------------------------
    @staticmethod
    def _account_scope(exchange: str, testnet: bool) -> str:
        """账户作用域：交易所 + 真假钱。

        demo 与 testnet 共用同一套「模拟盘」Key（`read_api_keys` 只分
        testnet / live 两套），因此归为同一作用域；主网是独立账户。
        """
        return f"{str(exchange or 'binance').lower()}:{'demo' if testnet else 'live'}"

    def _scope(self) -> str:
        """当前生效的账户作用域；旧存档没有该字段时按 exchange + testnet 推导。"""
        return str(self.state.get("account_scope") or "") or self._account_scope(
            self.state.get("exchange", "binance"), bool(self.state.get("testnet", True))
        )

    @staticmethod
    def _row_in_scope(row: dict, scope: str) -> bool:
        """流水行是否属于该账户。

        没带标签的行按「属于当前账户」处理：升级前的旧记录无法逐笔还原，
        加载时已由 `_tag_legacy_ledger()` 统一补标，这里只是运行时兜底。
        """
        if not scope:
            return True
        tag = str(row.get("account_scope") or "")
        return not tag or tag == scope

    def _scoped_rows(self, key: str, scope: str | None = None) -> list[dict]:
        """按账户取流水（fills / position_history），保持原有顺序。"""
        scope = self._scope() if scope is None else scope
        return [
            r for r in (self.state.get(key) or [])
            if isinstance(r, dict) and self._row_in_scope(r, scope)
        ]

    @classmethod
    def _counted_fees(cls, fills: list[dict]) -> float:
        """已发生、且需要单独计入「已实现盈亏」的手续费总额。

        口径（用户 2026-11-08 定）：浮动盈亏与平仓 pnl 都只反映价格变动（毛额），
        手续费**在发生那一刻**直接计入已实现盈亏——开仓费开仓就扣，平仓费平仓再扣，
        既不摊进浮盈，也不留到平仓时一次性从平仓盈亏里扣。

        兼容性：`fee_mode="gross"` 是本口径写下的标记。更早的旧流水里
        `position_history.pnl` 已经扣过平仓费（但**没扣过开仓费**），所以
        - OPEN 的 fee 一律计入（补上历史一直漏掉的开仓费，不是重复扣）；
        - 旧 CLOSE 的 fee 不计入（已在旧 pnl 里）；
        - 新 CLOSE 带 `fee_mode="gross"` 才计入（新 pnl 是毛额）。
        """
        return round(sum(float(f.get("fee") or 0.0) for f in cls._fee_rows(fills)), 6)

    @classmethod
    def _fee_rows(cls, fills: list[dict]):
        """筛出需要单独计入已实现盈亏的费用流水（累计与日历共用，别各写一套）。"""
        for f in fills:
            if isinstance(f, dict) and (
                str(f.get("action") or "").upper() == "OPEN"
                or f.get("fee_mode") == "gross"
            ):
                yield f

    @classmethod
    def _funding_rows(cls, fills: list[dict]):
        """筛出资金费流水（action=FUNDING）。fee 存「净资金费支出」：正=支付、负=收到。"""
        for f in fills:
            if isinstance(f, dict) and str(f.get("action") or "").upper() == "FUNDING":
                yield f

    @classmethod
    def _counted_funding(cls, fills: list[dict]) -> float:
        """已入账的资金费净支出（正=支付、负=收到）。只认 FUNDING 流水。"""
        return round(sum(float(f.get("fee") or 0.0) for f in cls._funding_rows(fills)), 6)

    def _pnl_summary(self, scope: str | None = None) -> dict:
        """当前账户的累计已实现盈亏 = Σ(平仓毛盈亏) − Σ(已发生手续费) − Σ(已发生资金费)。

        必须用全量 `position_history` 聚合，不能拿 `status()` 里那 30 条算：
        页面上写的是「累计」，只加最近 30 笔会随窗口滚动而变小。
        胜率/笔数仍只按平仓记录统计，手续费/资金费不是一笔「交易」。
        """
        scope = self._scope() if scope is None else scope
        rows = self._scoped_rows("position_history", scope)
        fills = self._scoped_rows("fills", scope)
        fees_paid = self._counted_fees(fills)
        funding_paid = self._counted_funding(fills)
        pnls = [float(r.get("pnl") or 0.0) for r in rows]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_pnl = round(sum(pnls), 6)
        return {
            "scope": scope,
            "realized": round(gross_pnl - fees_paid - funding_paid, 6),
            "gross_pnl": gross_pnl,
            "fees_paid": fees_paid,
            "funding_paid": funding_paid,
            "trades": len(pnls),
            "wins": len(wins),
            "losses": len(losses),
            "gross_win": round(sum(wins), 4),
            "gross_loss": round(abs(sum(losses)), 4),
            "first_at": str(rows[0].get("closed_at") or "") if rows else "",
            "last_at": str(rows[-1].get("closed_at") or "") if rows else "",
        }

    def _accounts_summary(self) -> dict:
        """跨账户摘要（每个账户一份 + 合计 + 按日），给页面和 LLM 共用。

        每 5 秒最多读一次盘：`status()` 每 3 秒被轮询，逐次读文件没必要。
        """
        now = time.time()
        hit = getattr(self, "_accounts_cache", None)
        if hit and now - float(hit[0]) < 5.0:
            return hit[1]
        try:
            data = self.accounts.summarize_all()
        except Exception:  # noqa: BLE001 - 摘要失败不能影响 status
            data = {"accounts": [], "combined": {}}
        cur = self._scope()
        for row in data.get("accounts") or []:
            if row.get("scope") != cur:
                continue
            # 当前账户的权益以内存为准：status 不写盘，文件可能落后几十秒
            eq = float(self.state.get("equity") or 0.0)
            peak = max(float(self.state.get("peak_equity") or 0.0), eq)
            row["equity"] = round(eq, 2)
            row["peak_equity"] = round(peak, 2)
            row["drawdown_pct"] = round((eq / peak - 1) * 100, 3) if peak > 0 else 0.0
            break
        self._accounts_cache = (now, data)
        return data

    def accounts_snapshot(self) -> dict:
        """跨账户数据快照：账户摘要 + 合计 + 按日合并。

        Web（/api/accounts）与 LLM 多账户分析共用这一个入口，避免各自拼文件。
        """
        return self._accounts_summary()

    def _tag_legacy_ledger(self) -> None:
        """给没有账户标签的旧流水补上「当时的账户」，并落盘一次。

        为什么必须钉住而不是每次按「当前账户」算：同一批旧记录如果没有标签，
        切到另一个交易所后会被新账户再算一遍，两个账户都显示同一份历史盈亏，
        永远归不了零。补标之后每笔只属于一个账户。
        局限：升级前如果已经跨交易所用过，旧记录无法逐笔还原，统一归给存档里
        最后使用的那个账户。
        """
        scope = self._scope()
        if not scope:
            return
        changed = 0
        for key in ("fills", "position_history"):
            for row in self.state.get(key) or []:
                if isinstance(row, dict) and not row.get("account_scope"):
                    row["account_scope"] = scope
                    changed += 1
        if changed:
            try:
                self._save()
            except Exception:  # noqa: BLE001 - 补标失败不能挡住启动
                pass

    # ------------------------------------------------------------------
    # 多品种持仓
    # ------------------------------------------------------------------
    @staticmethod
    def _pos_symbol(pos: dict | None, fallback: str = "") -> str:
        return str((pos or {}).get("symbol") or fallback or "").upper()

    @staticmethod
    def _pos_side(pos: dict | None) -> str:
        """统一方向读取：老存档可能没有 side、历史数据可能写成小写。

        返回值只可能是 "LONG" / "SHORT"，避免各处硬索引 `pos["side"]` 在缺字段时
        抛 KeyError 被外层宽 except 吞成「静默 hold」。
        """
        raw = str((pos or {}).get("side") or "LONG").strip().upper()
        return "SHORT" if raw.startswith("S") else "LONG"

    def _positions_map(self) -> dict[str, dict]:
        """全部持仓（key = 品种）。`state["positions"]` 是唯一真相。

        只做一件兼容：老存档（只有 `state["position"]`、没有 positions）首次读取时
        迁移进字典。**不做「视图被置空就删仓位」的猜测**——那是多线程下的竞态，
        一次误判就会把真实仓位从状态里抹掉。清仓一律走 `_drop_position()`。
        """
        m = self.state.get("positions")
        if not isinstance(m, dict):
            m = {}
            self.state["positions"] = m
        single = self.state.get("position")
        if not m and isinstance(single, dict):
            sym = self._pos_symbol(single, self.cfg.market.symbol)
            single["symbol"] = sym
            m[sym] = single
        return m

    def _sync_position_view(self) -> None:
        """把 `state["position"]` 同步成「当前品种」的仓（老前端只认单个）。"""
        m = self.state.get("positions")
        if not isinstance(m, dict):
            m = {}
            self.state["positions"] = m
        cur = m.get(str(self.cfg.market.symbol).upper())
        if cur is None and m:
            cur = next(iter(m.values()))
        self.state["position"] = cur

    def _get_position(self, symbol: str | None = None) -> dict | None:
        return self._positions_map().get(str(symbol or self.cfg.market.symbol).upper())

    def _resolve_position(self, symbol: str | None = None) -> dict | None:
        """定位要操作的仓位：显式品种优先；否则当前品种；再否则「只有一个仓」时用它。

        单仓时代 `/api/close` 不带 symbol，auto_rotate 换过品种后仍要能平掉唯一那笔。
        """
        m = self._positions_map()
        if symbol:
            return m.get(str(symbol).upper())
        cur = m.get(str(self.cfg.market.symbol).upper())
        if cur is not None:
            return cur
        return next(iter(m.values()), None) if len(m) == 1 else None

    def _put_position(self, pos: dict) -> None:
        m = self._positions_map()
        sym = self._pos_symbol(pos, self.cfg.market.symbol)
        pos["symbol"] = sym
        # 开仓时固化账户作用域：平仓写进流水时据此归户，旧存档缺该字段不补
        pos.setdefault("account_scope", self._scope())
        m[sym] = pos
        self._sync_position_view()

    def _drop_position(self, symbol: str | None = None) -> dict | None:
        m = self._positions_map()
        sym = str(symbol or self.cfg.market.symbol).upper()
        old = m.pop(sym, None)
        self._sync_position_view()
        return old

    def position_limit(self) -> int:
        """同时持仓的品种数上限（配置异常时退回单仓，绝不放开）。"""
        risk = getattr(self.cfg, "risk", None)
        try:
            n = int(getattr(risk, "max_concurrent_positions", 1) or 1)
        except (TypeError, ValueError):
            n = 1
        return max(1, n)

    def _quote_for(self, symbol: str, ttl: float = 10.0) -> float:
        """取某个品种的最新价。优先 ws 实时价，其次活跃品种 K 线 / REST ticker。

        多品种止损检查不能都拿「当前品种的价格」：BTC 的价套到 SOL 仓位上，
        止损/止盈会瞬间误触发。取不到价就返回 0，调用方跳过该品种本轮检查。

        ⚠️ 非活跃品种**必须**走 fetch_live_price（/ticker/price，毫秒级），不能
        用 fetch_klines 的最后一根收盘价顶替：盘缓存有效期是「半个 bar」，
        5m 周期最长 150 秒，页面上的标记价会像卡住一样每两三分钟才跳一次。
        K 线只作为 ticker 全源失败时的兜底。
        """
        sym = str(symbol).upper()
        # 优先 ws 实时价：只认 15s 内新鲜值，让非活跃品种的持仓管理员 / check_stops
        # 也吃到 <1s 价；ws 断线（>15s）自动回退到下面的 K 线 / REST，不会一直用旧价。
        ws_px = float((getattr(self, "_last_prices", None) or {}).get(sym) or 0.0)
        ws_ts = float((getattr(self, "_price_ts", None) or {}).get(sym) or 0.0)
        if ws_px > 0 and (time.time() - ws_ts) <= 15.0:
            return ws_px
        cur = str(self.cfg.market.symbol).upper()
        if sym == cur and self.df is not None:
            try:
                px = float(self.df["close"].iloc[-1])
                if px > 0:
                    self._stamp_price(sym, px)
                    return px
            except Exception:  # noqa: BLE001
                pass
        now = time.time()
        cached = self._quote_cache.get(sym)
        if cached and (now - cached[1]) < ttl:
            return cached[0]
        px = 0.0
        try:
            px, _src = fetch_live_price(sym, prefer=self.state.get("exchange", "binance"))
            px = float(px or 0.0)
        except Exception:  # noqa: BLE001 - ticker 挂了再退回 K 线
            px = 0.0
        if px <= 0:
            # 兜底：ticker 全源不可用时用 K 线收盘价（可能偏旧，总比没有强）
            try:
                interval = str(getattr(self.cfg.market, "short_interval", "5m") or "5m")
                # fetch_klines 返回 (df, 来源)；漏了这一步解包，df 会是个 tuple，
                # df["close"] 直接 TypeError —— 非活跃品种从此永远取不到价。
                df, _src = fetch_klines(sym, interval, 5, prefer=self.state.get("exchange", "binance"))
                if df is not None and len(df):
                    px = float(df["close"].iloc[-1])
            except Exception:  # noqa: BLE001 - 单个品种取价失败不影响其它仓位
                return 0.0
        if px <= 0:
            return 0.0
        self._quote_cache[sym] = (px, now)
        self._stamp_price(sym, px, now)
        return px

    def _frame_for(self, symbol: str, ttl: float = 60.0) -> pd.DataFrame | None:
        """取某个品种的 K 线，供持仓管理员算 ATR / 均线 / 持有根数。

        活跃品种用 self.df；其余品种走短周期 K 线并按品种缓存 60 秒——
        持仓管理 30 秒一轮，每轮重拉一遍全部品种纯属浪费。
        """
        sym = str(symbol).upper()
        if sym == str(self.cfg.market.symbol).upper() and self.df is not None:
            return self.df
        now = time.time()
        cached = self._frame_cache.get(sym)
        if cached and (now - cached[1]) < ttl:
            return cached[0]
        try:
            interval = str(getattr(self.cfg.market, "short_interval", "5m") or "5m")
            # 同 _quote_for：必须解包 (df, 来源)，否则拿到的是 tuple，
            # 下面的 len(df) 恒为 2，会把一个 tuple 当 K 线喂给持仓管理员。
            df, _src = fetch_klines(sym, interval, 200, prefer=self.state.get("exchange", "binance"))
        except Exception:  # noqa: BLE001 - 拿不到就退化成「本轮不管理」
            return None
        if df is not None and len(df):
            self._frame_cache[sym] = (df, now)
        return df

    def _positions_for_risk(self, price: float) -> list[dict]:
        """把全部持仓整理成风险引擎需要的格式（A3 组合层风控使用）。

        ⚠️ 名义 = 数量 × 开仓价，与杠杆无关（永续的名义就是币数×价格）。
        旧代码写成 `qty × entry × lev`，多乘了一次杠杆，导致组合敞口被放大 lev 倍
        ——30x 下几乎每笔都会被组合上限挡住，所以历史上才把上限一路抬到 27x。
        保证金 = 名义 / 杠杆，这才是交易所拒单时看的量。
        """
        rows: list[dict] = []
        for sym, pos in self._positions_map().items():
            entry = float(pos.get("entry") or price)
            qty = float(pos.get("qty") or 0.0)
            lev = float(pos.get("leverage") or 1.0) or 1.0
            # 敞口按**当前标记价**算：用开仓价会在浮盈时系统性低估组合风险，
            # 让组合层上限形同虚设。取不到标记价时才退回成本价。
            mark = self._mark_price(sym, price)
            notional = qty * (mark if mark > 0 else entry)
            rows.append({
                "symbol": self._pos_symbol(pos, self.cfg.market.symbol),
                "side": pos.get("side"),
                "notional": notional,
                "margin": notional / lev,
                "leverage": lev,
            })
        return rows

    def _roll_day(self, equity: float | None = None) -> None:
        """跨日重置「当日权益基准」。

        当日亏损限额（daily_loss_limit_pct）与页面 day_pnl_pct 都以
        day_start_equity 为分母。这个方法必须由 account_state / cycle 每轮调用，
        否则基准会永远停在首次启动那一天。
        """
        today = _today()
        if self.state.get("day") == today:
            return
        base = float(equity) if equity and float(equity) > 0 else float(self.state.get("equity") or 0.0)
        with self.lock:
            self.state["day"] = today
            self.state["day_start_equity"] = base

    def account_state(self, price: float | None = None) -> AccountState:
        price = price if price is not None else self.price()
        live = self._live_equity()
        if live is not None:
            # 只有「内部账本与交易所余额量级不符」时才对齐（如模拟期 10000 vs 实盘 4997，
            # 或切换了交易所/账户）。
            # 注意：不能拿 peak 与 live 比 —— 那样真实回撤超过 33%（peak/live > 1.5）就会
            # 把峰值重置成当前权益，回撤瞬间归零，max_drawdown_pct 熔断永远触发不了。
            ledger = float(self.state.get("equity") or 0.0)
            # 首次切换到实盘（还没有 live_baseline_at）时，旧 peak 属于模拟账本，
            # 重置成实盘权益是对的；已经对齐过之后的偏离属于手续费/资金费漂移，
            # 此时保留 peak，否则历史回撤会被抹掉、max_drawdown_pct 永远不触发。
            first_live = not bool(self.state.get("live_baseline_at"))
            if ledger <= 0:
                self._rebase_ledger(live, reset_peak=True)
            elif abs(ledger - live) / live > 0.5:
                self._rebase_ledger(live, reset_peak=first_live)
            equity = live
        else:
            equity = self._equity(price)
        # 跨日先滚动基准，再用同一口径返回 peak / day_start：
        # 实盘 equity 来自交易所，peak / day_start 若仍读内部账本，回撤与日亏
        # 会变成「交易所分子 ÷ 账本分母」的混合值，熔断可能误触发或漏触发。
        self._roll_day(equity)
        return AccountState(
            equity=equity,
            peak_equity=max(float(self.state.get("peak_equity") or 0.0), equity),
            day_start_equity=float(self.state.get("day_start_equity") or equity),
            position_notional=self._position_notional(price),
            consecutive_losses=self.state["consecutive_losses"],
        )

    def _position_pnl(self, price: float | None = None, symbol: str | None = None) -> float:
        """全部持仓的浮动盈亏。多品种按各自的最新价算，绝不共用当前品种的价。

        没有报价的品种（刚开仓、K 线还没拉到）按 0 计入——宁可少算浮盈，
        也不能拿别的品种的价格算出巨额假盈亏。
        """
        if symbol:
            pos = self._get_position(symbol)
            if not pos:
                return 0.0
            px = self._mark_price(symbol, price)
            if px <= 0:
                return 0.0
            direction = 1.0 if self._pos_side(pos) == "LONG" else -1.0
            return (px - float(pos["entry"])) * float(pos["qty"]) * direction
        total = 0.0
        for sym, pos in self._positions_map().items():
            px = self._mark_price(sym, price)
            if px <= 0:
                continue
            direction = 1.0 if pos.get("side") == "LONG" else -1.0
            total += (px - float(pos.get("entry") or 0.0)) * float(pos.get("qty") or 0.0) * direction
        return total

    def _latest_management(self, cur_sym: str = "") -> dict:
        """心跳行用的「最近一次巡检」——只认**当前仍有持仓**的品种。

        平仓不会清掉 `last_management_by_symbol` 里该品种的那条记录（它留着平仓
        那轮的 `action=close`，供「各品种巡检」回看），但直接把它当心跳会显示
        几十分钟前的 close，页面上看着像守护线程停在了平仓那一刻。所以按持仓过滤：

          1) 当前品种仍有仓 → 用那一份（用户最关心的就是它）；
          2) 否则在其它**持仓品种**里取 ts 最新的一条（守护线程本就逐仓轮转）；
          3) 完全空仓或持仓还没巡检过 → 返回 `{}`，前端显示空仓/等待首轮，
             绝不拿已平仓品种的历史记录冒充心跳。

        为什么不用 `self.last_management` 兜底：那也是「某品种某一轮的快照」，
        没有持仓约束，同样会冻结在历史 close 上。
        """
        try:
            open_syms = {str(s).upper() for s in self._positions_map()}
        except Exception:  # noqa: BLE001 - 存档损坏也不能把 status() 拖垮
            open_syms = set()
        if not open_syms:
            return {}
        m = getattr(self, "last_management_by_symbol", None) or {}
        # 快照成 list 再筛：守护线程可能在另一个线程往里塞新品种
        recs = [(str(k).upper(), v) for k, v in list(m.items())
                if isinstance(v, dict) and str(k).upper() in open_syms]
        if not recs:
            return {}
        cur = str(cur_sym or self.cfg.market.symbol).upper()
        for sym, rec in recs:
            if sym == cur:
                return rec
        return max(recs, key=lambda kv: float(kv[1].get("ts") or 0.0))[1]

    def _stamp_price(self, symbol: str, price: float, ts: float | None = None) -> None:
        """记下某品种的最新价与写入时刻（_last_prices 是页面读的唯一来源）。"""
        sym = str(symbol).upper()
        self._last_prices[sym] = float(price)
        book = getattr(self, "_price_ts", None)
        if book is None:      # 兼容用 object.__new__ 构造的测试实例
            book = self._price_ts = {}
        book[sym] = float(ts or time.time())

    def _mark_price(self, symbol: str, hint: float | None = None) -> float:
        """最新成交价（Last）：优先用报价线程的缓存，hint 只做兜底。

        这里**不发网络请求**——它会被 status() 每 3 秒调用一次，现场拉行情会把
        HTTP 轮询拖死。缓存由报价线程（4 秒一轮，含当前品种）维护。

        hint 一般是当前品种 K 线末根的收盘价。盘中 K 线不会每 4 秒刷新，
        直接拿它当标记价，浮盈会滞后几分钟——用户看到 +2U、点平仓却已经反向。
        所以只在缓存缺失/过期（>15s）时兜底，且**不覆盖缓存时间戳**，
        避免旧价把「新鲜」状态污染掉。
        """
        sym = str(symbol).upper()
        cached = float((getattr(self, "_last_prices", None) or {}).get(sym) or 0.0)
        ts = float((getattr(self, "_price_ts", None) or {}).get(sym) or 0.0)
        if cached > 0 and (time.time() - ts) <= 15.0:
            return cached
        if hint and float(hint) > 0 and sym == str(self.cfg.market.symbol).upper():
            return float(hint)
        return cached

    def _position_notional(self, price: float) -> float:
        """全部持仓的名义价值（币数 × 当前价），与杠杆无关。"""
        total = 0.0
        for sym, pos in self._positions_map().items():
            px = self._mark_price(sym, price)
            if px > 0:
                total += float(pos.get("qty") or 0.0) * px
        return total

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
        equity = account_equity(acct)
        if equity <= 0:
            return None
        self._live_eq_cache = (equity, now)
        return equity

    def _rebase_ledger(self, equity: float, reset_peak: bool = False) -> None:
        """把内部账本对齐到交易所真实权益（发现量级不符时调用）。

        reset_peak 只在「首次切到实盘 / 账户切换」时为 True：那时旧 peak 属于
        另一个账户，重置是必要的；运行期漂移校准必须保留历史峰值。
        """
        with self.lock:
            peak = float(equity) if reset_peak else max(
                float(self.state.get("peak_equity") or 0.0), float(equity)
            )
            self.state["equity"] = float(equity)
            self.state["peak_equity"] = peak
            self.state["day_start_equity"] = float(equity)
            self.state["day"] = _today()
            self.state["live_baseline_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._live_eq_cache = (float(equity), time.time())
            self._save()

    def _reset_account_scope(
        self, acct: dict | None = None, scope: str = "", old_scope: str = ""
    ) -> None:
        """切换账户：旧账户落盘、新账户换血、权益基准以交易所为准。

        peak_equity / day_start_equity / consecutive_losses 都是绑定在某个账户上的：
        换了账户还沿用旧值，会得到「旧账户峰值 vs 新账户余额」的伪回撤，以及旧账户
        留下的伪连亏（会一路写进风控官提示词并压低仓位）。

        流水（fills / position_history / 权益曲线）不再是「保留在同一个 state 里」，
        而是各自写进 `runs/accounts/<scope>.json`：切到 OKX 模拟盘后加载的是 OKX
        自己的账本，币安模拟盘的历史留在币安文件里，两边互不影响。
        K 线缓存与账户无关，完全不动。

        `old_scope` 必须由调用方在**改动 state 之前**取好：如果先写
        `state["account_scope"] = 新值` 再进来，这里会把「新账户文件」当成旧账户
        落盘，用当前（旧账户的）空流水覆盖掉它的历史。"""
        old_scope = (
            str(old_scope or "")
            or str(self.state.get("account_scope") or "")
            or self._scope()
        )
        new_scope = str(scope or "") or old_scope
        equity: float | None = None
        if acct:
            try:
                equity = account_equity(acct)
            except (TypeError, ValueError):
                equity = None
        store = getattr(self, "accounts", None)
        with self.lock:
            # 1) 旧账户先落盘（权益基准 + 权益曲线 + 成交/平仓历史）
            if store is not None and old_scope:
                try:
                    store.save_state(old_scope, self.state)
                except Exception:  # noqa: BLE001 - 落盘失败也不能阻断切换
                    pass
            if scope:
                self.state["account_scope"] = str(scope)
            # 2) 换血：只加载新账户自己的账本；新账户没有账本就是空历史
            existed = False
            if store is not None:
                try:
                    existed = store.apply_to_state(new_scope, self.state)
                except Exception:  # noqa: BLE001
                    self.state["fills"] = []
                    self.state["position_history"] = []
                    self.state["history"] = []
            else:
                # 分账户账本不可用（迁移失败）：退回「清空流水」的旧行为
                self.state["history"] = []
            # 3) 权益基准：能取到交易所余额就以它为准；新账户取不到就用初始权益
            if equity and equity > 0:
                self.state["equity"] = float(equity)
                # 新账户首连不能沿用上一个账户的峰值，否则回撤从第一天就是假的；
                # 已有账户则保留它自己的历史峰值（回撤要跨会话延续）。
                prev_peak = float(self.state.get("peak_equity") or 0.0) if existed else 0.0
                self.state["peak_equity"] = max(prev_peak, float(equity))
                self.state["day_start_equity"] = float(equity)
                self.state["day"] = _today()
            elif not existed:
                base_eq = float(self.cfg.backtest.initial_equity)
                self.state["equity"] = base_eq
                self.state["peak_equity"] = base_eq
                self.state["day_start_equity"] = base_eq
                self.state["day"] = _today()
            self.state["consecutive_losses"] = 0
            self.state["live_baseline_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._live_eq_cache = None
            # 账户摘要缓存必须失效：否则切完账户 5 秒内页面还读到旧账户的汇总
            self._accounts_cache = None
            self._save()

    def reset_account_records(self, scope: str | None = None) -> dict:
        """清空某个账户的权益曲线 / 成交记录 / 持仓历史。

        重置范围严格限定这三类数据：
        - 不动配置（风控、仓位、模型、通知都不变）；
        - 不动真实持仓（交易所仓位与本地 positions 都保留）；
        - 不动其它账户的文件；
        - 不动 K 线缓存、因子库、决策快照。

        当前账户还会把权益基准对齐到交易所余额（取不到就用当前权益），
        这样「重置后重新统计」的起点是真实权益，而不是旧账户累计出的数字。
        """
        target = str(scope or "").strip() or self._scope()
        before = self.accounts.reset(target)
        is_current = target == self._scope()
        if is_current:
            with self.lock:
                self.state["fills"] = []
                self.state["position_history"] = []
                self.state["history"] = []
                self.state["consecutive_losses"] = 0
                eq: float | None = None
                client = getattr(self, "live_client", None)
                if client is not None:
                    try:
                        acct = client.account()
                        eq = account_equity(acct)
                    except Exception:  # noqa: BLE001 - 取不到余额就用当前权益
                        eq = None
                if not eq or eq <= 0:
                    eq = float(self.state.get("equity") or self.cfg.backtest.initial_equity)
                self.state["equity"] = float(eq)
                self.state["peak_equity"] = float(eq)
                self.state["day_start_equity"] = float(eq)
                self.state["day"] = _today()
                # 记录「重置时刻 + 当时权益」：交易所真实成交 / 资金流水删不掉，
                # 只能让 overlay 从这个锚点起重新算「最近成交」与「当日盈亏」。
                self.state["account_reset_epoch"] = int(time.time() * 1000)
                self.state["account_reset_equity"] = float(eq)
                # 资金费入账锚点随重置清零：fills 已清空，下一轮资金费同步从
                # 重置时刻起重新累计，否则会漏掉重置之后新产生的资金费。
                self.state["funding_synced_until_ms"] = 0
                self._live_eq_cache = None
                self._accounts_cache = None
                self._save()
        else:
            self._accounts_cache = None
        return {
            **before,
            "current": is_current,
            "equity": round(float(self.state.get("equity") or 0.0), 2) if is_current else None,
            "reset_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

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
                    stances: list[float] | None = None,
                    factor_snapshot: dict | None = None) -> dict:
        slip = self.cfg.backtest.costs.slippage_bps / 10_000.0
        fill = price * (1 + slip) if side == "LONG" else price * (1 - slip)
        fee = qty * fill * self.cfg.backtest.costs.fee_bps / 10_000.0
        self.state["equity"] -= fee
        sym = self.cfg.market.symbol
        self._put_position({
            "side": side,
            "qty": qty,
            "entry": fill,
            "entry_fee": round(fee, 8),
            "leverage": lev,
            "stop": stop,
            "tp": tp,
            "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            # 持仓管理员用：R 必须在开仓时固化（止损后来会被移动），
            # epoch 用来算持有了多少根 K 线，避免本地时区歧义。
            "opened_epoch": time.time(),
            "initial_r_pct": abs(fill - stop) / fill if fill > 0 else 0.0,
            "symbol": sym,
            "analyst_stances": list(stances or []),
            # 开仓时的因子方向快照：平仓时用它把真实价格走向归因回因子
            "factor_snapshot": dict(factor_snapshot or {}),
            "mode": "paper",
        })
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
            "fee_mode": "gross",   # 本笔手续费单独计入已实现盈亏，不摊进平仓 pnl
            "reason": reason,
            "account_scope": self._scope(),
        }
        self.state["fills"].append(fill_rec)
        self.state["fills"] = self.state["fills"][-500:]
        return fill_rec

    def _record_close(self, pos: dict, exit_price: float, pnl: float, reason: str, mode: str,
                      rule: str = "", fee: float = 0.0) -> None:
        """把一次平仓写进持仓历史：持仓历史表 + 每日盈亏统计都靠它。

        `pnl` 是**价格变动毛额**，与平仓前的浮盈同口径；手续费由成交流水单独
        计入累计已实现盈亏，不在这里扣。但「是否真的赚了」必须看净额——
        所以同时落 `net_pnl` / `net_realized_r`（毛额 − 开仓费 − 平仓费），
        复盘官与绩效官优先读净额。高频小止盈下毛 R 会把胜率显著算高。
        """
        # 分析师权重校准：用「市场实际方向」而非这笔仓的盈亏评判立场
        # （开仓方向由交易员定，不该算到分析师头上）。
        stances = pos.get("analyst_stances") or []
        entry = float(pos.get("entry") or 0.0)
        if stances and entry > 0 and exit_price > 0 and abs(float(exit_price) - entry) > 1e-12:
            market_dir = 1.0 if float(exit_price) > entry else -1.0
            stats = self.state.setdefault("analyst_accuracy", {})
            record_accuracy(stats, stances, market_dir)

        # 因子归因：同样用「市场实际方向」评判因子当时的方向，而不是这笔仓的盈亏。
        # 一个仓位里多个因子共线，用 P&L 归因会把功劳/责任算到错的因子上；方向
        # 命中率没有这个问题。这份统计会被注入因子研究的提议器（见
        # research.build_live_evidence），构成「因子库 → 决策 → 真实成交 → 研究」闭环。
        frec = pos.get("factor_snapshot") or {}
        if frec and entry > 0 and exit_price > 0 and abs(float(exit_price) - entry) > 1e-12:
            market_dir = 1.0 if float(exit_price) > entry else -1.0
            fstats = self.state.setdefault("factor_accuracy", {})
            entries = [("__composite__", frec.get("composite"))]
            entries += [(i.get("name"), i.get("target")) for i in (frec.get("items") or [])]
            for name, val in entries:
                try:
                    name = str(name or "")
                    tgt = float(val or 0.0)
                except (TypeError, ValueError):
                    continue
                if not name or abs(tgt) < 0.05:
                    continue
                cur = fstats.get(name) or {"n": 0, "hit": 0}
                cur["n"] = int(cur.get("n") or 0) + 1
                if (tgt > 0) == (market_dir > 0):
                    cur["hit"] = int(cur.get("hit") or 0) + 1
                fstats[name] = cur
            # 因子库最多几十个；超了按样本量裁到 60，避免 state 无限膨胀
            if len(fstats) > 60:
                self.state["factor_accuracy"] = dict(
                    sorted(fstats.items(), key=lambda kv: -int(kv[1].get("n") or 0))[:60]
                )

        hist = self.state.setdefault("position_history", [])
        # R 口径与持仓管理员一致：开仓时的初始风险是分母，止损后来被移动也不改分母。
        # initial_r_pct 缺失时用当前止损近似，并标注 approx，避免和真值混算。
        entry_f = float(pos.get("entry") or 0.0)
        r_pct = float(pos.get("initial_r_pct") or 0.0)
        r_approx = False
        if r_pct <= 0 and entry_f > 0:
            stop_f = float(pos.get("stop") or 0.0)
            if stop_f > 0:
                r_pct = abs(entry_f - stop_f) / entry_f
                r_approx = True
        sign = 1.0 if str(pos.get("side") or "LONG").upper().startswith("L") else -1.0
        realized_r = None
        initial_stop = None
        if entry_f > 0 and r_pct > 0:
            realized_r = round((float(exit_price or 0.0) - entry_f) / entry_f * sign / r_pct, 4)
            initial_stop = round_price(entry_f * (1.0 - sign * r_pct))
        # 净额口径：毛额扣掉「开仓费 + 平仓费」。费用按开仓名义换算成 R，
        # 这样「净赚几个 R」可以直接和毛 R 对比。手续费占比在紧止损时会很大，
        # 高频小止盈的场景下毛 R 为正、净 R 可能已经接近 0。
        fee_total = float(fee or 0.0) + float(pos.get("entry_fee") or 0.0)
        net_pnl = round(float(pnl or 0.0) - fee_total, 4)
        net_realized_r = None
        if realized_r is not None and entry_f > 0 and r_pct > 0:
            notional_entry = entry_f * float(pos.get("qty") or 0.0)
            if notional_entry > 0:
                net_realized_r = round(
                    realized_r - fee_total / (notional_entry * r_pct), 4
                )
        hist.append(
            {
                "symbol": pos.get("symbol") or self.cfg.market.symbol,
                "side": pos.get("side"),
                "qty": round(float(pos.get("qty") or 0), 6),
                "entry": float(pos.get("entry") or 0),
                "exit": round_price(float(exit_price or 0)),
                "pnl": round(float(pnl or 0), 4),
                # 开仓费单列：pnl 是毛额，审计「毛 − 开仓费 − 平仓费」时靠它对账
                "fee_open": round(float(pos.get("entry_fee") or 0.0), 4),
                "fee_close": round(float(fee or 0.0), 4),
                "fee_total": round(fee_total, 4),
                # 净额：复盘/绩效统一读这两个字段，避免「毛赚实亏」被算成赢单
                "net_pnl": net_pnl,
                "net_realized_r": net_realized_r,
                "leverage": pos.get("leverage"),
                # 自迭代要用的量：没有这三项，事后连「赚了几个 R」都算不出来
                "initial_stop": initial_stop,
                "r_pct": round(r_pct, 6) if r_pct > 0 else None,
                "r_approx": r_approx,
                "realized_r": realized_r,
                "mfe_r": round(float(pos.get("mfe_r")), 4) if pos.get("mfe_r") is not None else None,
                "rule": str(rule or ""),
                # 账户标签：审计流水跨账户保留，但绩效统计要能按账户过滤
                "account_scope": pos.get("account_scope") or self._scope(),
                "opened_at": pos.get("opened_at", ""),
                "closed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "mode": mode,
                "reason": reason,
            }
        )
        # 复盘官：确定性归因（纯规则、不调 LLM），供绩效官与进化官消费。
        # 归因是旁路，任何异常都不能影响平仓记账。
        try:
            from .agents.reviewer import review_trade

            hist[-1]["review"] = review_trade(hist[-1])
        except Exception:  # noqa: BLE001
            pass
        self.state["position_history"] = hist[-200:]
        # 喂给持仓管理员的自校准（样本不足时内部会自动不动）
        self._learn_from_close(pos, float(exit_price or 0.0), rule)

    def performance_snapshot(self, force: bool = False) -> dict:
        """统一绩效快照（60 秒缓存）。

        所有进化系统都必须读这一份口径：各自统计会让「改完到底有没有变好」
        变成各说各话。按笔 R 计算，不看权益（权益受充值/切换账户/重置污染，
        不是策略能力）。
        """
        now = time.time()
        cached = getattr(self, "_perf_cache", None)
        if not force and cached and now - float(cached[0]) < 60.0:
            return cached[1]
        try:
            from .agents.scorekeeper import compute_kpis

            snap = compute_kpis(self._scoped_rows("position_history"))
        except Exception as exc:  # noqa: BLE001 - 绩效算不出来不能影响交易
            snap = {
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "error": f"{type(exc).__name__}: {exc}",
            }
        self._perf_cache = (now, snap)
        return snap

    def _paper_close(self, price: float, reason: str, symbol: str | None = None,
                     rule: str = "") -> dict | None:
        pos = self._resolve_position(symbol)
        if not pos:
            return None
        slip = self.cfg.backtest.costs.slippage_bps / 10_000.0
        direction = 1.0 if self._pos_side(pos) == "LONG" else -1.0
        fill = price * (1 - slip) if self._pos_side(pos) == "LONG" else price * (1 + slip)
        pnl = (fill - pos["entry"]) * pos["qty"] * direction
        fee = pos["qty"] * fill * self.cfg.backtest.costs.fee_bps / 10_000.0
        # 平仓 pnl 只记价格变动（毛额），与平仓前的「浮盈」同口径：
        # 开仓费开仓时已进已实现，平仓费由本条流水的 fee 单独进已实现，
        # 两者都不从这笔 pnl 里再扣一次。
        open_fee = float(pos.get("entry_fee") or 0.0)
        if open_fee <= 0 and float(pos.get("entry") or 0.0) > 0:
            open_fee = (
                float(pos["entry"]) * float(pos["qty"])
                * self.cfg.backtest.costs.fee_bps / 10_000.0
            )
        # 连亏风控看真实到手，仍按完整净额判断（只是不写进流水）
        net_for_risk = pnl - fee - open_fee
        self.state["equity"] += pnl - fee
        self.state["consecutive_losses"] = (
            self.state["consecutive_losses"] + 1 if net_for_risk < 0 else 0
        )
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "action": "CLOSE",
            "side": self._pos_side(pos),
            "symbol": self._pos_symbol(pos, self.cfg.market.symbol),
            "qty": round(pos["qty"], 6),
            "price": round_price(fill),
            "fee": round(fee, 4),
            "fee_open": round(open_fee, 4),
            "fee_mode": "gross",
            "pnl": round(pnl, 4),
            "reason": reason,
            "account_scope": self._scope(),
        }
        self.state["fills"].append(rec)
        self.state["fills"] = self.state["fills"][-500:]
        self._record_close(pos, fill, pnl, reason, "paper", rule=rule, fee=fee)
        self._drop_position(pos.get("symbol"))
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
            if not key or not secret or not passphrase:
                need = (
                    "OKX_DEMO_API_KEY / OKX_DEMO_API_SECRET / OKX_DEMO_PASSPHRASE"
                    if testnet
                    else "OKX_API_KEY / OKX_API_SECRET / OKX_PASSPHRASE"
                )
                raise ExchangeError(f"未设置 {need}（模拟盘与主网是两套独立 Key，OKX 必须三项齐全）")
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

    def arm(self, mode: str, confirm: str, testnet: bool = True,
            exchange: str | None = None, force: bool = False) -> dict:
        """解锁交易所下单。

        三种模式：
        - paper   ：本地模拟撮合，不碰交易所
        - testnet ：交易所**模拟盘**（假钱），只校验密钥，不需要确认文本
        - live    ：主网真钱，需要确认文本

        exchange 决定接币安还是 OKX（对应不同的 Key / 域名 / 签名方式）。

        force：账户作用域改变且仍有实盘持仓 / 自动交易在跑时，默认拒绝切换；
        只有显式 force 才放行，并保留告警（旧账户仓位将失去本地管理）。
        """
        if mode not in {"paper", "testnet", "demo", "live"}:
            raise ValueError("mode 只能是 paper / testnet / demo / live")
        exchange = (exchange or self.state.get("exchange", "binance")).lower()
        if exchange not in ("binance", "okx"):
            raise ValueError("exchange 只能是 binance / okx")
        prev_scope = str(self.state.get("account_scope") or "")
        # demo / testnet 都是假钱，共用一套模拟盘 Key，归为同一作用域
        new_scope = self._account_scope(exchange, mode in {"testnet", "demo"} or testnet)
        reset_needed = prev_scope != new_scope
        if mode == "paper":
            self.armed = False
            self.state["mode"] = "paper"
            self._save()
            return {"mode": "paper", "armed": False, "exchange": exchange}
        # 只在「已有一个明确的旧作用域，且新作用域不同」时拦截；
        # 首次解锁（旧存档没有 scope）无法判断是否同账户，不拦。
        if reset_needed and prev_scope and not force:
            live_positions = [
                p for p in self._positions_map().values() if p.get("mode") == "live"
            ]
            if live_positions:
                syms = "、".join(
                    str(p.get("symbol") or "?") for p in live_positions
                )
                raise PermissionError(
                    f"当前有 {len(live_positions)} 笔实盘持仓（{syms}）。切换账户后这些仓位"
                    "会失去本地的移动止损 / 时间止损管理，请先平仓；"
                    "确认仍要切换请传 force=true"
                )
            if (self.state.get("auto") or {}).get("running"):
                raise PermissionError(
                    "自动交易正在运行，请先停止再切换账户；确认仍要切换请传 force=true"
                )
        if mode in {"testnet", "demo"}:
            client = self.connect(testnet=True, env=("demo" if mode == "demo" else "testnet"), exchange=exchange)
            client.armed = True
            client.ping()
            acct = client.account()
            self.armed = True
            self.state["mode"] = "live"
            self.state["testnet"] = True
            self.state["exchange"] = exchange
            self._ws_restart()
            # 顺序很重要：先让 _reset_account_scope 用旧 scope 落盘再换血。
            # 提前写 account_scope 会让它把新账户文件当成旧账户覆盖掉。
            if reset_needed:
                self._reset_account_scope(acct, scope=new_scope, old_scope=prev_scope)
                self.reload_for_exchange()
            else:
                self.state["account_scope"] = new_scope
                self._save()
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
        client = self.connect(testnet=testnet, exchange=exchange)
        client.armed = True
        client.ping()
        acct = client.account()  # 验证签名
        self.armed = True
        self.state["mode"] = "live"
        self.state["testnet"] = testnet
        self.state["exchange"] = exchange
        self._ws_restart()
        if reset_needed:
            self._reset_account_scope(acct, scope=new_scope, old_scope=prev_scope)
            self.reload_for_exchange()
        else:
            self.state["account_scope"] = new_scope
            self._save()
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
            # 既没有开仓 orderId 命中的成交、opened_at 也解析失败：时间下界缺失，
            # 继续补记会把上一笔仓位的平仓成交算进来，宁可不补。
            return None
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
            direction = 1.0 if self._pos_side(pos) == "LONG" else -1.0
            gross = (exit_px - float(pos.get("entry") or 0.0)) * qty * direction
        # 与主动平仓同口径：pnl 只记价格变动毛额，平仓手续费由本条流水单独
        # 计入累计已实现盈亏，不在这里扣。
        pnl = gross
        net_for_risk = gross - fee
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
                    "fee_mode": "gross",
                    "pnl": round(pnl, 4),
                    "reason": "exchange",
                    "order_id": order_id,
                    "backfilled": True,
                    # 补记的是这笔仓位自己的账户：切过账户后不能按「当前」打标，
                    # 否则旧账户的平仓会被算进新账户的盈亏。
                    "account_scope": pos.get("account_scope") or self._scope(),
                }
            )
            self.state["fills"] = self.state["fills"][-500:]
            self.state["equity"] = float(self.state.get("equity") or 0.0) + net_for_risk
            self.state["consecutive_losses"] = (
                int(self.state.get("consecutive_losses") or 0) + 1
                if net_for_risk < 0
                else 0
            )
            self._record_close(pos, exit_px, pnl, "exchange", "live", rule="exchange", fee=fee)
            self._save()
        self._log_live("CLOSE", str(pos.get("side")), qty, exit_px, order_id)
        self._notify(
            "info" if net_for_risk >= 0 else "critical",
            f"{pos.get('side')} 已由交易所平仓（补记）",
            f"成交价 {round_price(exit_px)}，价格盈亏 {pnl:+,.2f} U，"
            f"平仓手续费 {fee:,.2f} U（费用直接计入累计已实现盈亏）",
            {"symbol": symbol, "pnl": round(pnl, 2), "reason": "exchange"},
        )
        return {"qty": qty, "price": exit_px, "fee": fee, "pnl": pnl}

    def _reconcile_position_with_exchange(self) -> None:
        """逐个核对本地实盘持仓与交易所真实持仓，清理「幽灵仓位」。

        本地 `state["positions"]` 只是缓存：当仓位在交易所网页端被手动平掉、或下单
        被拒但本地已记账时，会留下幽灵仓位，导致切换/删除品种、开仓被误拦截。
        只核对实盘（mode=live）仓位；模拟盘仓位不对应交易所，不能清。
        拿不到交易所数据时保守不动；确认交易所无持仓才清本地缓存，绝不主动平仓。
        """
        live_positions = [p for p in self._positions_map().values() if p.get("mode") == "live"]
        if not live_positions:
            return
        exchange = self.state.get("exchange", "binance")
        testnet = bool(self.state.get("testnet", True))
        for pos in live_positions:
            # 用仓位自己的品种去查：auto_rotate 换过品种后，拿当前品种去查只会得到
            # 空仓位，会把「另一个品种还在持仓」误判成幽灵仓位。
            symbol = str(pos.get("symbol") or self.cfg.market.symbol)
            try:
                client = self.live_client or self.connect(testnet=testnet, exchange=exchange)
                real = client.positions(symbol)
            except Exception:  # noqa: BLE001 - 拿不到交易所数据就保守不动
                continue
            if real:
                continue
            # 交易所已无持仓：先把残留的止损/止盈触发单撤掉。
            # OKX 的触发单不支持 reduceOnly（51205），仓位没了还留着触发单，
            # 价格碰到就会反手开出一笔没人管的仓位——这是必须堵住的口子。
            try:
                client.cancel_all(symbol)
            except Exception as exc:  # noqa: BLE001 - 撤单失败不阻断清缓存
                self.last_error = f"清理残留条件单失败：{type(exc).__name__}: {exc}"[:200]
            # 再把这次平仓的成交补记下来，最后清缓存。
            # 补记失败也只是少了流水，不能因此留下幽灵仓位。
            try:
                self._backfill_closed_from_exchange(pos)
            except Exception as exc:  # noqa: BLE001 - 补记是尽力而为
                self.last_error = f"平仓补记失败：{type(exc).__name__}: {exc}"[:200]
            with self.lock:
                self._drop_position(symbol)
                self._save()

    def _sync_funding_fees(self) -> None:
        """把持仓期间交易所真实产生的资金费同步进本地账本（fills 记 action=FUNDING）。

        资金费每 8 小时结算一次，只体现在 income / bills 的 FUNDING_FEE 流水里，
        本地下单回执拿不到它。这里周期拉取、按时间戳去重，把「上次已同步之后」
        新产生的资金费补记进 fills，让累计已实现盈亏与交易所余额对齐（不含资金费
        会系统性偏乐观）。

        只处理实盘（mode=live）且有持仓：模拟盘本地撮合不产生真实资金费。
        fee 存「净资金费支出」（= −income，正=支付、负=收到），与手续费同向入账。
        """
        if self.state.get("mode") != "live" or self.live_client is None:
            return
        if not self._positions_map():
            return
        # 资金费 8 小时才结算一次，而守护线程 30 秒一轮。不节流的话一天要拉
        # 2880 次 income（币安该接口权重不低），纯浪费额度还可能触发限流。
        now_ts = time.time()
        if now_ts - float(getattr(self, "_funding_sync_at", 0.0)) < 300.0:
            return
        self._funding_sync_at = now_ts
        # 去重下界：已同步锚点 → 账户重置锚点 → 最近 24h（首次不灌全量历史）
        floor = int(self.state.get("funding_synced_until_ms") or 0)
        if not floor:
            floor = int(self.state.get("account_reset_epoch") or 0)
        if not floor:
            floor = int((time.time() - 86400) * 1000)
        try:
            rows = self.live_client.income(floor, limit=1000)
        except Exception:  # noqa: BLE001 - 拿不到流水就不入账，不阻塞守护循环
            return
        funding = [
            r for r in (rows or [])
            if str(r.get("incomeType") or "").upper() == "FUNDING_FEE"
            and int(r.get("time") or 0) > floor
        ]
        if not funding:
            return
        funding.sort(key=lambda r: int(r.get("time") or 0))
        scope = self._scope()
        added = 0
        with self.lock:
            for r in funding:
                ts_ms = int(r.get("time") or 0)
                income = float(r.get("income") or 0.0)
                if income == 0.0 or ts_ms <= 0:
                    continue
                sym = str(r.get("symbol") or "").strip().upper() or str(self.cfg.market.symbol).upper()
                self.state["fills"].append({
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_ms / 1000)),
                    "action": "FUNDING",
                    "symbol": sym,
                    "fee": round(-income, 6),   # 收到为正的 income → 负支出；支付为负 → 正支出
                    "account_scope": scope,
                    "funding_ts_ms": ts_ms,
                })
                added += 1
            if added:
                self.state["fills"] = self.state["fills"][-500:]
                self.state["funding_synced_until_ms"] = max(int(r.get("time") or 0) for r in funding)
                self._save()

    def switch_symbol(self, symbol: str) -> dict:
        """切换活跃品种（只影响「看哪个品种 / 对谁做开仓决策」，不影响已有仓位）。

        多品种持仓后不再有「有仓不能切」的限制：每个仓位自己带着 symbol，
        行情、止损检查、移动止损都按仓位自己的品种走，切走不会让仓位失管。
        """
        symbol = symbol.strip().upper()
        if not symbol:
            raise ValueError("品种不能为空")
        if symbol == self.cfg.market.symbol:
            return {"symbol": symbol, "changed": False}
        # 切到当前环境不存在的合约，后续下单必然失败（OKX 模拟盘尤甚）。在这里
        # 就拒绝，并把「当前环境能交易的品种」一并给出，避免切过去再撞 51001。
        verdict = self.symbol_tradable(symbol)
        if not verdict["ok"]:
            raise ValueError(verdict["reason"])
        # 先与交易所核对，清理幽灵仓位（本地记着、但交易所实际已平）
        self._reconcile_position_with_exchange()
        with self.lock:
            self.cfg.market.symbol = symbol
            self.state["symbol"] = symbol
            self.df = None
            self._sync_position_view()
            self._save()
        df, src = self.refresh_data()
        if self.state.get("exchange", "binance") == "okx" and not str(src or "").startswith("okx"):
            self._retry_okx_source()
        if df is None or not len(df):
            # 品种已经切过去了，但行情没拿到：宁可 price 返回 None，也不要拿
            # self.df 里的旧品种价格冒充新品种（那是错价下单的源头）。
            return {"symbol": symbol, "changed": True, "data_source": src,
                    "price": None, "bars": 0}
        # 切完之后，原持仓品种就成了「非活跃品种」，它的行情不再由 self.df 提供，
        # 只靠 _last_prices。这里立刻补一次报价，否则页面上的标记价会显示 --，
        # 直到守护线程下一轮（最长 30 秒）才恢复。
        for sym in list(self._positions_map()):
            if sym != symbol:
                try:
                    self._quote_for(sym, ttl=0.0)
                except Exception:  # noqa: BLE001 - 补价失败不影响切换本身
                    pass
        return {
            "symbol": symbol,
            "changed": True,
            "data_source": src,
            "price": round(float(df["close"].iloc[-1]), 4),
            "bars": int(len(df)),
        }

    def disarm(self) -> dict:
        """断开交易所：停止自动交易、切断真钱，回到本地模拟。

        只切 `armed` 不够——自动循环还会继续跑并在 paper 账本里开模拟仓，
        用户看到的「已断开」与实际行为不一致。这里一并停掉自动交易。
        """
        if (self.state.get("auto") or {}).get("running"):
            self.stop_auto()
        self.armed = False
        if self.live_client:
            self.live_client.armed = False
        self.state["mode"] = "paper"
        self._live_eq_cache = None
        self._save()
        return {"mode": "paper", "armed": False}

    def _okx_demo_env(self) -> bool:
        """当前 OKX 连接是模拟盘还是主网。

        客户端自己的 testnet 标记最可信；未连接交易所时退回账户作用域
        （okx:demo / okx:live）。OKX 用请求头 `x-simulated-trading: 1` 区分
        模拟盘，且**品种清单与主网不是同一份**，必须按环境判断。
        """
        client = getattr(self, "live_client", None)
        if client is not None and hasattr(client, "testnet"):
            try:
                return bool(client.testnet)
            except Exception:  # noqa: BLE001 - 读不到就按作用域推导
                pass
        try:
            return self._scope().endswith(":demo")
        except Exception:  # noqa: BLE001
            return True

    def _okx_swap_symbols(self) -> set[str]:
        """OKX 永续（SWAP）白名单（内部 symbol 集合），**按当前环境**拉取并缓存。

        ⚠️ 必须分环境：OKX 模拟盘的永续清单与主网并不相同（实测主网 543 个、
        模拟盘仅 191 个，ZEC / HYPE 只在主网）。拿主网清单去校验模拟盘账户，
        会放行实际不存在的合约，下单时在 set_leverage 处撞
        51001「Instrument ID doesn't exist」——这正是手动执行决策报错的根因。

        缓存按 demo / live 分键，切换账户后不会串用另一套清单。拉取失败缓存
        空集合，调用方据此退回「不过滤」，绝不因网络问题阻断轮动。
        """
        demo = self._okx_demo_env()
        cache = getattr(self, "_okx_swap_cache", None)
        if cache is None:
            cache = self._okx_swap_cache = {}
        hit = cache.get(demo)
        if hit is not None:
            return hit
        out: set[str] = set()
        try:
            import json
            import urllib.request
            from .netutil import ssl_context
            headers = {"User-Agent": "cryptoagent"}
            if demo:
                headers["x-simulated-trading"] = "1"
            req = urllib.request.Request(
                "https://www.okx.com/api/v5/public/instruments?instType=SWAP",
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=10, context=ssl_context()) as resp:
                js = json.loads(resp.read().decode("utf-8"))
            for it in (js.get("data") or []):
                # 只收正常在交易的合约：suspend / preopen 状态下单同样会被拒
                if str(it.get("state") or "live").lower() != "live":
                    continue
                inst = str(it.get("instId") or "")
                if inst.endswith("-USDT-SWAP"):
                    out.add(f"{inst[: -len('-USDT-SWAP')]}USDT")
        except Exception:  # noqa: BLE001 - 白名单拉不到就不过滤，不能阻断轮动
            out = set()
        cache[demo] = out
        return out

    def symbol_tradable(self, symbol: str) -> dict:
        """这个品种在「当前交易所 + 当前环境」能不能交易。

        专门拦 OKX 模拟盘与主网品种清单不一致的情况：让调用方拿到一句能读懂的
        原因，而不是把交易所的 51001 原样抛给用户。非 OKX，或白名单拉取失败时
        一律放行——宁可在下单时由交易所报错，也不因公共接口抖动误拒真实下单。
        """
        sym = str(symbol or "").strip().upper()
        if str((getattr(self, "state", None) or {}).get("exchange") or "") != "okx":
            return {"ok": True, "reason": ""}
        universe = self._okx_swap_symbols()
        if not universe:
            return {"ok": True, "reason": ""}
        if sym in universe:
            return {"ok": True, "reason": ""}
        env = "模拟盘" if self._okx_demo_env() else "主网"
        alts = [str(s).upper() for s in (self.cfg.market.symbols or [])
                if str(s).upper() in universe and str(s).upper() != sym]
        hint = ("；当前环境可交易：" + " / ".join(alts[:6])) if alts else ""
        return {
            "ok": False,
            "reason": f"{sym} 在 OKX {env}没有永续合约{hint}",
            "code": "symbol_unsupported",
        }

    def _scan_opportunities(self) -> list[dict]:
        """扫全部品种的因子复合信号，返回按机会分降序的 [{symbol, composite, score, active}]。

        纯本地计算、不下单、不调 LLM；结果缓存到 self.opportunities 供页面排行榜展示。
        """
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
            # 用 dirty 标志循环：品种增删发生在扫描进行中时，扫完旧列表再补扫一次
            # 基于最新 market.symbols 的结果，避免机会榜一直挂着已删/缺新品种。
            while True:
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
                    if self.scan_state.get("dirty"):
                        self.scan_state["dirty"] = False
                        self.scan_state["scanning"] = True
                        continue
                    break

        threading.Thread(target=_work, name="opportunity-scan", daemon=True).start()
        return dict(self.scan_state)

    def invalidate_scan_cache(self) -> None:
        """品种增删后失效机会榜缓存；若有扫描在跑，标记脏、扫完自动重扫。"""
        with self.lock:
            self.opportunities = []
            self.scan_state["last_ts"] = 0.0
            self.scan_state["dirty"] = True

    def _top_symbols(self, k: int | None = None) -> list[str]:
        """按机会分返回前 K 个「未持仓且达阈值」的品种，供一轮内依次决策。

        `auto_top_k=1` 时等价于旧的「一轮切到最强的一个」。调大后同一轮会评估多个
        品种——这是提高交易频率最直接的一步：机会数随 K 线性增加，而不是靠调快循环
        （循环再快，一轮也只看得见一个品种）。
        """
        try:
            kk = int(k if k is not None else getattr(self.cfg.market, "auto_top_k", 1))
        except (TypeError, ValueError):
            kk = 1
        kk = max(0, min(kk, 8))
        if kk <= 0:
            return []
        fresh = (time.time() - float(self.scan_state.get("last_ts") or 0.0)) < 300.0
        rows = self.opportunities if (fresh and self.opportunities) else self._scan_opportunities()
        if not rows:
            return []
        held = set(self._positions_map().keys())
        thr = float(getattr(self.cfg.market, "rotate_min_score", 0.0) or 0.0)
        ranked: list[tuple[float, str]] = []
        okx_only = self._okx_swap_symbols() if str(
            (getattr(self, "state", None) or {}).get("exchange") or "") == "okx" else None
        for r in rows:
            if r.get("error"):
                continue
            sym = str(r.get("symbol") or "").upper()
            if not sym or sym in held:
                continue
            # okx 下只保留交易所确实支持的永续品种，从源头避开 51001
            if okx_only and sym not in okx_only:
                continue
            try:
                score = float(r.get("score") or 0.0)
            except (TypeError, ValueError):
                continue
            if score < thr:
                continue
            ranked.append((score, sym))
        ranked.sort(key=lambda x: -x[0])
        return [s for _, s in ranked[:kk]]

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
        # 已持仓的品种不再作为轮动目标：切过去只会被「已有持仓」跳过，
        # 白白浪费一次决策机会，也挡住了真正的第二个仓位。
        held = set(self._positions_map().keys())
        rows_all = rows
        # okx 下只保留交易所确实支持的永续品种，从源头避开 51001
        okx_only = self._okx_swap_symbols() if str(
            (getattr(self, "state", None) or {}).get("exchange") or "") == "okx" else None
        rows = [
            r for r in rows
            if str(r.get("symbol", "")).upper() not in held
            and (not okx_only or str(r.get("symbol", "")).upper() in okx_only)
        ]
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
            (float(r.get("score") or 0.0) for r in rows_all if r.get("symbol") == current), 0.0
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

    def uptime_seconds(self) -> int:
        """进程已运行秒数。取不到启动时刻时返回 0，绝不抛错（要进 status）。"""
        try:
            return int(max(0.0, time.time() - float(self.started_at)))
        except (AttributeError, TypeError, ValueError):
            return 0

    # ------------------------------------------------------------------
    # 持仓管理员参数自迭代（与上面的分析师权重校准同一套机制）
    # ------------------------------------------------------------------
    def _tuning_base(self) -> dict[str, float]:
        """当前配置里的可调参数原值——学习的锚点，也是「用户改配置」的比对基准。"""
        cfg = getattr(self.cfg, "position", None)
        out: dict[str, float] = {}
        for k in TUNING_KEYS:
            try:
                out[k] = float(getattr(cfg, k))
            except (AttributeError, TypeError, ValueError):
                out[k] = 0.0
        return out

    def effective_position_config(self):
        """返回带自校准覆盖的持仓配置**副本**；未启用/异常时返回原对象。

        用户手动改过配置（与锚点不一致）时，学习结果立即作废、以配置为准——
        绝不出现「我改了他又给我悄悄改回去」。
        """
        cfg = getattr(self.cfg, "position", None)
        if cfg is None or not bool(getattr(cfg, "auto_tune", True)):
            return cfg
        try:
            base = self._tuning_base()
            anchor = self.position_tuning.get("anchor") or {}
            eff = self.position_tuning.get("effective") or {}
            stale = (not eff) or any(
                abs(float(anchor.get(k, base[k])) - base[k]) > 1e-9 for k in TUNING_KEYS
            )
            if stale:
                eff = dict(base)
                self.position_tuning["anchor"] = dict(base)
                self.position_tuning["effective"] = dict(eff)
            else:
                eff = {k: float(eff.get(k, base[k])) for k in TUNING_KEYS}
            return replace(cfg, **eff)
        except Exception:  # noqa: BLE001 - 校准是旁路，出问题就用原配置
            return cfg

    @staticmethod
    def _cfg_num(value, default: float) -> float:
        """配置数值：None 取默认；0 是合法值，不能被 `or` 吞掉。"""
        return float(default if value is None else value)

    def _evolution_mode(self) -> str:
        """当前演进层级；非法值退回 stats，绝不因配置写错而关闭保护。"""
        mode = str(getattr(self.cfg.position, "evolution_mode", "stats") or "stats").lower()
        return mode if mode in ("off", "stats", "replay", "llm") else "stats"

    def _sync_tuning_state(self) -> None:
        """把运行态完整写回 state（避免各处手写 dict 漏字段）。"""
        self.state["position_tuning"] = {
            "stats": dict(self.position_tuning.get("stats") or {}),
            "effective": dict(self.position_tuning.get("effective") or {}),
            "anchor": dict(self.position_tuning.get("anchor") or {}),
            "changes": list(self.position_tuning.get("changes") or []),
            "pending": dict(self.position_tuning.get("pending") or {}),
            "replay_cases": list(self.position_tuning.get("replay_cases") or [])[-200:],
            "replay": dict(self.position_tuning.get("replay") or {}),
            "gate": dict(self.position_tuning.get("gate") or {}),
            "llm": dict(self.position_tuning.get("llm") or {}),
            "updated_at": float(self.position_tuning.get("updated_at") or 0.0),
        }

    def _confirm_tuning(self, changes: list[dict]) -> list[dict]:
        """L1 连续确认：同一方向连续 N 轮才落地，单轮噪声只记 pending。"""
        need = max(1, int(getattr(self.cfg.position, "evolution_confirm_rounds", 2) or 2))
        if not changes:
            self.position_tuning["pending"] = {}
            return []
        pending = self.position_tuning.get("pending") or {}
        prev = {c.get("param"): c for c in (pending.get("changes") or [])}
        same = bool(prev) and all(
            c.get("param") in prev
            and (float(c.get("to", 0)) - float(c.get("from", 0)))
            * (float(prev[c["param"]].get("to", 0)) - float(prev[c["param"]].get("from", 0))) > 0
            for c in changes
        )
        rounds = (int(pending.get("rounds") or 0) + 1) if same else 1
        if rounds >= need:
            # 确认一次只落地一次；下一轮重新计数，避免连续 10 轮调 10 次
            self.position_tuning["pending"] = {}
            return changes
        self.position_tuning["pending"] = {"changes": changes, "rounds": rounds}
        return []

    def _replay_gate(self, base: dict, cand: dict) -> tuple[bool, dict]:
        """L2：候选参数在最近平仓样本上的反事实回放。"""
        cfg = self.cfg.position
        try:
            base_cfg = replace(cfg, **{
                k: float(base.get(k, TUNE_DEFAULTS.get(k, 0.0))) for k in TUNING_KEYS
            })
            cand_cfg = replace(cfg, **{
                k: float(cand.get(k, TUNE_DEFAULTS.get(k, 0.0))) for k in TUNING_KEYS
            })
        except Exception as exc:  # noqa: BLE001
            return False, {"passed": False, "reason": f"参数不合法：{type(exc).__name__}"}
        out = replay_evaluate(
            self.position_tuning.get("replay_cases") or [],
            self._frame_for,
            base_cfg,
            cand_cfg,
            window=int(getattr(cfg, "evolution_replay_window", 100) or 100),
            min_cases=int(getattr(cfg, "evolution_replay_min_cases", 20) or 20),
            min_median_delta=float(getattr(cfg, "evolution_replay_min_median_delta", 0.0) or 0.0),
            max_worst_delta=self._cfg_num(
                getattr(cfg, "evolution_replay_max_worst_delta", 0.2), 0.2),
        )
        out["checked_at"] = time.time()
        return bool(out.get("passed")), out

    def _recompute_position_tuning(self) -> None:
        """按演进层级重算生效参数：L1 归因 → L2 回放 → 落地。"""
        cfg = getattr(self.cfg, "position", None)
        mode = self._evolution_mode()
        if cfg is None or mode == "off" or not bool(getattr(cfg, "auto_tune", True)):
            return
        try:
            base = self._tuning_base()
            anchor = self.position_tuning.get("anchor") or {}
            prev = self.position_tuning.get("effective") or {}
            if not prev or any(
                abs(float(anchor.get(k, base[k])) - base[k]) > 1e-9 for k in TUNING_KEYS
            ):
                prev = dict(base)  # 用户改过配置：从新配置重新开始学
                self.position_tuning["pending"] = {}
            eff, _ok, changes = learned_params(
                prev, self.position_tuning.get("stats") or {},
                rule_min_samples=int(getattr(cfg, "evolution_rule_min_samples", 8) or 8),
            )
            changes = self._confirm_tuning(changes)
            now = time.time()
            if mode in ("replay", "llm"):
                gate = dict(self.position_tuning.get("gate") or {})
                if changes:
                    passed, replay = self._replay_gate(prev, eff)
                    self.position_tuning["replay"] = replay
                    gate["l2_checks"] = replay.get("checks") or {}
                    gate["l2_cases"] = int(replay.get("cases") or 0)
                    gate["l2_median_delta"] = replay.get("median_delta")
                    gate["l2_worst_delta"] = replay.get("worst_delta")
                    # 只有「有候选且回放通过」才允许置位；无候选不改变已通过状态
                    gate["l2_passed"] = bool(gate.get("l2_passed")) or bool(passed)
                    if passed:
                        gate["l2_passed_at"] = gate.get("l2_passed_at") or now
                        gate["l2_stable_since"] = gate.get("l2_stable_since") or now
                    else:
                        # 门控未过：丢弃本轮改动，保持当前生效值
                        eff = dict(prev)
                        changes = []
                else:
                    # 没有候选：回放没有可比对象，不能凭「空跑」通过门控。
                    # 否则只要样本够、哪怕参数完全没动，也会把 L2 置为通过，
                    # 进而错误地放行 L3。
                    self.position_tuning["replay"] = {
                        "passed": False, "cases": 0,
                        "reason": "无待验证的候选改动",
                        "checks": {"has_candidate": False},
                    }
                    gate["l2_checks"] = {"has_candidate": False}
                    gate.setdefault("l2_passed", False)
                self.position_tuning["gate"] = gate
            # 进化账本：L1 校准的改动同样要留痕，否则「进化采纳率」永远算不出来。
            # 走到这里的 changes 已过「连续确认 + L2 回放闸门」，是真正生效的一批。
            # 去重：同一参数 1 小时内且目标值相同就不再记，避免参数震荡把账本刷爆。
            try:
                from .evolution import get_governor

                gov = get_governor(self.cfg.runs_dir)
                perf_snap = self.performance_snapshot()
                seen = dict(getattr(self, "_calib_ledger", None) or {})
                for ch in changes:
                    key = str(ch.get("param") or "")
                    to = ch.get("to")
                    if not key:
                        continue
                    prev_rec = seen.get(key) or {}
                    if (now - float(prev_rec.get("at") or 0.0) < 3600.0
                            and abs(float(prev_rec.get("to") or 0.0)
                                    - float(to or 0.0)) < 1e-9):
                        continue
                    gov.record_applied(
                        kind="rule",
                        target=f"position.{key}",
                        before=ch.get("from"),
                        after=to,
                        source="position_calibration",
                        rationale=str(ch.get("why") or ""),
                        kpi=perf_snap,
                        evidence=({"kind": "replay_gate",
                                   "gate": dict(self.position_tuning.get("gate") or {})}
                                  if self._evolution_mode() != "stats" else {}),
                        note=f"rule={ch.get('rule')}",
                    )
                    seen[key] = {"at": now, "to": to}
                self._calib_ledger = seen
            except Exception:  # noqa: BLE001 - 记账失败不影响已生效的参数
                pass
            log = list(self.position_tuning.get("changes") or [])
            for ch in changes:
                log.append({**ch, "ts": now})
            self.position_tuning.update({
                "anchor": dict(base),
                "effective": dict(eff),
                "changes": log[-10:],
                "updated_at": now,
            })
            self._sync_tuning_state()
            self._save()
        except Exception:  # noqa: BLE001 - 学习失败绝不影响交易
            pass

    def position_tuning_status(self) -> dict:
        """给状态接口/设置页的自迭代摘要（不触发任何重算，纯读）。"""
        try:
            cfg = getattr(self.cfg, "position", None)
            llm = dict(self.position_tuning.get("llm") or {})
            return {
                **tuning_summary(self.position_tuning.get("stats") or {},
                                 self.position_tuning.get("effective") or {}),
                "auto_tune": bool(getattr(cfg, "auto_tune", True)) if cfg else False,
                "mode": self._evolution_mode(),
                "base": self._tuning_base(),
                "changes": list(self.position_tuning.get("changes") or [])[-5:],
                "pending": dict(self.position_tuning.get("pending") or {}),
                "replay": dict(self.position_tuning.get("replay") or {}),
                "gate": dict(self.position_tuning.get("gate") or {}),
                "llm": {
                    "enabled": self._llm_evolution_allowed() if cfg else False,
                    "allow_action": bool(getattr(cfg, "evolution_llm_allow_action", False)) if cfg else False,
                    "allow_loosen": bool(getattr(cfg, "evolution_llm_allow_loosen", False)) if cfg else False,
                    "shadow_cases": int(getattr(cfg, "evolution_llm_shadow_cases", 20) or 20) if cfg else 20,
                    "applied_count": len(llm.get("applied") or []),
                    "proposals": list(llm.get("proposals") or [])[-5:],
                    "applied": list(llm.get("applied") or [])[-5:],
                    "shadow": list(llm.get("shadow") or [])[-5:],
                    "rollbacks": list(llm.get("rollbacks") or [])[-3:],
                    "streak_loss": int(llm.get("streak_loss") or 0),
                    "cum_r": round(float(llm.get("cum_r") or 0.0), 3),
                },
                "updated_at": float(self.position_tuning.get("updated_at") or 0.0),
            }
        except Exception:  # noqa: BLE001
            return {"samples": 0, "min_samples": 0, "calibrated": False, "auto_tune": False}

    def _learn_from_close(self, pos: dict, exit_price: float, rule: str) -> None:
        """把一笔平仓换算成 R，喂给校准器。带移动止损的仓位统一归到 trail 组。"""
        cfg = getattr(self.cfg, "position", None)
        if cfg is None or not bool(getattr(cfg, "auto_tune", True)):
            return
        try:
            entry = float(pos.get("entry") or 0.0)
            exit_px = float(exit_price or 0.0)
            if entry <= 0 or exit_px <= 0:
                return
            r_pct = float(pos.get("initial_r_pct") or 0.0)
            if r_pct <= 0:
                stop = float(pos.get("stop") or 0.0)
                if stop <= 0:
                    return
                r_pct = abs(entry - stop) / entry
            if r_pct <= 0:
                return
            sign = 1.0 if str(pos.get("side") or "LONG").upper().startswith("L") else -1.0
            realized_r = (exit_px - entry) / entry * sign / r_pct
            # 被动过移动止损的仓位，其出场本质上就是移动止损的结果
            tag = "trail" if (pos.get("trailed") and rule in ("stop_loss", "trail")) else (rule or "unknown")
            stats = self.position_tuning.setdefault("stats", {})
            record_episode(stats, tag, realized_r, pos.get("mfe_r"))
            self._remember_replay_case(pos, realized_r)
            self._update_llm_streak(pos, realized_r)
            self._recompute_position_tuning()
        except Exception:  # noqa: BLE001
            pass

    def _remember_replay_case(self, pos: dict, realized_r: float) -> None:
        """L2：保存一笔可回放的平仓元数据（不存 K 线，回放时按品种取现成行情）。"""
        try:
            opened = float(pos.get("opened_epoch") or 0.0)
            entry = float(pos.get("entry") or 0.0)
            stop = float(pos.get("stop") or 0.0)
            if opened <= 0 or entry <= 0 or stop <= 0:
                return
            case = {
                "symbol": self._pos_symbol(pos, self.cfg.market.symbol),
                "side": self._pos_side(pos),
                "entry": entry,
                "stop": stop,
                "tp": float(pos.get("tp") or 0.0),
                "opened_epoch": opened,
                "closed_epoch": time.time(),
                "initial_r_pct": float(pos.get("initial_r_pct") or abs(entry - stop) / entry),
                "realized_r": round(float(realized_r), 4),
                "mfe_r": pos.get("mfe_r"),
            }
            cases = list(self.position_tuning.get("replay_cases") or [])
            cases.append(case)
            self.position_tuning["replay_cases"] = cases[-200:]
        except Exception:  # noqa: BLE001 - 回放样本只是旁路
            pass

    def _llm_evolution_allowed(self) -> bool:
        """L3 门控：L2 通过 + 用户显式开启，二者缺一不可。

        稳定等待期（evolution_l2_stable_days）默认 0：L2 门控通过后立即允许进入 L3；
        仅在显式配置 > 0 时才要求 L2 通过后继续稳定运行 N 天（保留可逆的观察期）。
        """
        cfg = getattr(self.cfg, "position", None)
        if cfg is None or self._evolution_mode() != "llm":
            return False
        if not bool(getattr(cfg, "evolution_llm_enabled", False)):
            return False
        gate = self.position_tuning.get("gate") or {}
        if not bool(gate.get("l2_passed")):
            return False
        days = int(getattr(cfg, "evolution_l2_stable_days", 0) or 0)
        since = float(gate.get("l2_stable_since") or 0.0)
        if days > 0 and since and (time.time() - since) < days * 86400:
            return False
        return True

    def _apply_llm_evolution(self, dec, ev: dict, pos: dict) -> dict:
        """把 LLM 提案交给代码裁决：方向只许更保守、参数受规则与步长限制。"""
        cfg = getattr(self.cfg, "position", None)
        if cfg is None:
            return {"applied": False, "phase": "disabled"}
        llm = self.position_tuning.setdefault("llm", {})
        rec = {
            "ts": time.time(),
            "symbol": self._pos_symbol(pos, self.cfg.market.symbol),
            "rule": dec.rule,
            "rule_action": dec.action,
            "llm_action": ev.get("action"),
            "confidence": float(ev.get("confidence") or 0.0),
            "params": dict(ev.get("params") or {}),
            "reason": str(ev.get("reason") or "")[:160],
            "source": ev.get("source"),
        }
        llm["proposals"] = (list(llm.get("proposals") or []) + [rec])[-50:]
        applied_n = len(llm.get("applied") or [])
        shadow_n = max(0, int(self._cfg_num(
            getattr(cfg, "evolution_llm_shadow_cases", 20), 20)))
        if len(llm.get("shadow") or []) < shadow_n:
            # 影子期：只记录，不应用；攒够 shadow_cases 条后才进入 live。
            # 用 shadow 计数而不是 applied 计数——影子期 applied 永远是 0，
            # 用 applied 判断会永远出不了影子期。
            llm["shadow"] = (list(llm.get("shadow") or []) + [rec])[-50:]
            llm["last"] = rec
            self.position_tuning["llm"] = llm
            self._sync_tuning_state()
            return {"applied": False, "phase": "shadow", "shadow_count": len(llm["shadow"])}
        order = {"hold": 0, "tighten": 1, "close": 2}
        cur = order.get(dec.action, 0)
        new = order.get(str(ev.get("action") or dec.action), cur)
        allow_loosen = bool(getattr(cfg, "evolution_llm_allow_loosen", False))
        if new < cur and not allow_loosen:
            new = cur
        changed = False
        if new > cur:
            if new == 2:
                dec.action = "close"
                dec.reason = f"LLM 进化提案：{rec['reason']}"
                dec.rule = dec.rule or "llm_evolve"
                dec.details["llm_override"] = rec
                changed = True
            elif new == 1 and dec.new_stop:
                dec.action = "tighten"
                dec.details["llm_override"] = rec
                changed = True
        # 参数：只允许命中规则的相关参数，且单轮相对步长受限
        allowed = set(RULE_PARAMS.get(dec.rule, ()))
        step = max(0.0, self._cfg_num(
            getattr(cfg, "evolution_llm_max_param_step", 0.15), 0.15))
        eff = dict(self.position_tuning.get("effective") or {})
        base = self._tuning_base()
        for key, value in (ev.get("params") or {}).items():
            if key not in allowed or key not in TUNE_BOUNDS:
                continue
            try:
                cur_v = float(eff.get(key, base.get(key, TUNE_DEFAULTS.get(key, 0.0))))
                target = float(value)
            except (TypeError, ValueError):
                continue
            lo, hi = TUNE_BOUNDS[key]
            target = min(hi, max(lo, target))
            if step > 0:
                # 先把 target 夹进「当前值 ± step」区间，再夹硬边界
                target = max(cur_v * (1 - step), min(cur_v * (1 + step), target))
            if abs(target - cur_v) < 1e-9:
                continue
            eff[key] = round(float(target), 6)
            changed = True
        if changed:
            self.position_tuning["effective"] = eff
            llm["applied"] = (list(llm.get("applied") or []) + [rec])[-50:]
            llm["streak_loss"] = 0
            llm["cum_r"] = 0.0
            pos["llm_evolution"] = {"ts": time.time(), "action": dec.action,
                                    "params": rec["params"]}
        llm["last"] = rec
        self.position_tuning["llm"] = llm
        self._sync_tuning_state()
        if changed:
            self._save()
        return {"applied": changed, "phase": "live"}

    def _update_llm_streak(self, pos: dict, realized_r: float) -> None:
        """L3 回退：LLM 覆盖后连续亏损或累计 R 触发阈值，自动退回 stats。"""
        if not pos.get("llm_evolution"):
            return
        try:
            cfg = self.cfg.position
            llm = self.position_tuning.setdefault("llm", {})
            if realized_r < 0:
                llm["streak_loss"] = int(llm.get("streak_loss") or 0) + 1
            else:
                llm["streak_loss"] = 0
            llm["cum_r"] = round(float(llm.get("cum_r") or 0.0) + float(realized_r), 6)
            self.position_tuning["llm"] = llm
            max_losses = max(1, int(getattr(cfg, "evolution_rollback_after_losses", 3) or 3))
            max_dd = abs(float(getattr(cfg, "evolution_rollback_drawdown_r", 2.0) or 2.0))
            if llm["streak_loss"] >= max_losses or llm["cum_r"] <= -max_dd:
                self._rollback_llm_evolution(
                    f"LLM 覆盖后连续亏损 {llm['streak_loss']} 笔 / "
                    f"累计 {llm['cum_r']:+.2f}R，自动回退"
                )
        except Exception:  # noqa: BLE001
            pass

    def _rollback_llm_evolution(self, reason: str) -> None:
        """回退：关掉 L3、清掉学习覆盖、保留统计与审计记录。"""
        try:
            self.cfg.position.evolution_mode = "stats"
            base = self._tuning_base()
            self.position_tuning["effective"] = dict(base)
            self.position_tuning["anchor"] = dict(base)
            self.position_tuning["pending"] = {}
            llm = self.position_tuning.setdefault("llm", {})
            llm["rollbacks"] = (list(llm.get("rollbacks") or [])
                                + [{"ts": time.time(), "reason": reason}])[-20:]
            llm["streak_loss"] = 0
            llm["cum_r"] = 0.0
            self._sync_tuning_state()
            self._save()
            self._notify("warn", "持仓管理员 LLM 进化已回退", reason, {"reason": reason})
        except Exception:  # noqa: BLE001
            pass

    def _seed_replay_cases_from_history(self) -> None:
        """用已有平仓历史补齐 L2 回放样本（老版本没有 replay_cases 字段）。

        只取当前账户：回放样本决定参数怎么调，混入另一个交易所的行情与
        成交，会把参数往不合适的方向带。
        """
        hist = self._scoped_rows("position_history")
        cases: list[dict] = []
        for h in hist[-200:]:
            try:
                opened = time.mktime(time.strptime(str(h.get("opened_at")), "%Y-%m-%d %H:%M:%S"))
                entry = float(h.get("entry") or 0.0)
                stop = float(h.get("initial_stop") or 0.0)
                r_pct = float(h.get("r_pct") or 0.0)
                if opened <= 0 or entry <= 0 or stop <= 0 or r_pct <= 0:
                    continue
                cases.append({
                    "symbol": str(h.get("symbol") or "").upper(),
                    "side": str(h.get("side") or "LONG").upper(),
                    "entry": entry, "stop": stop, "tp": 0.0,
                    "opened_epoch": opened,
                    "closed_epoch": time.mktime(time.strptime(str(h.get("closed_at")), "%Y-%m-%d %H:%M:%S")),
                    "initial_r_pct": r_pct,
                    "realized_r": float(h.get("realized_r") or 0.0),
                    "mfe_r": h.get("mfe_r"),
                })
            except Exception:  # noqa: BLE001
                continue
        if cases:
            self.position_tuning["replay_cases"] = cases[-200:]

    def start_replay_validation(self) -> dict:
        """手动触发一次 L2 验收（后台线程，不阻塞交易）。

        候选来自对关键参数的 ±12% 小步探索；每个候选都要过回放门槛，
        只有中位 R 有改善且最差单笔不恶化的才落地并置 gate。
        """
        th = getattr(self, "_replay_thread", None)
        if th is not None and th.is_alive():
            return {"running": True, "started_at": getattr(self, "_replay_started_at", 0.0)}
        self._replay_result = {"running": True, "started_at": time.time(),
                               "finished_at": 0.0, "passed": False}
        self._replay_started_at = time.time()
        self._replay_thread = threading.Thread(target=self._replay_validation_job, daemon=True)
        self._replay_thread.start()
        return dict(self._replay_result)

    def replay_validation_status(self) -> dict:
        return dict(getattr(self, "_replay_result", {}) or {})

    def _replay_validation_job(self) -> None:
        try:
            cfg = self.cfg.position
            base = self._tuning_base()
            prev = dict(self.position_tuning.get("effective") or base)
            min_cases = int(getattr(cfg, "evolution_replay_min_cases", 20) or 20)
            if len(self.position_tuning.get("replay_cases") or []) < min_cases:
                self._seed_replay_cases_from_history()
            candidates: list[dict] = []
            # reversal_ema_bars 是主要出场规则（趋势反转）的敏感度旋钮；
            # reversal_composite 在回放里没有历史 composite，无法公平比较，故不探索。
            for key in ("reversal_ema_bars", "trail_atr_mult", "max_hold_bars"):
                lo, hi = TUNE_BOUNDS[key]
                cur = float(prev.get(key, TUNE_DEFAULTS[key]))
                for factor in (1 - 0.12, 1 + 0.12):
                    cand = dict(prev)
                    cand[key] = float(min(hi, max(lo, cur * factor)))
                    if abs(cand[key] - cur) < 1e-9:
                        continue
                    passed, replay = self._replay_gate(prev, cand)
                    candidates.append({
                        "param": key, "factor": round(factor, 2),
                        "from": round(cur, 4), "to": round(cand[key], 4),
                        "passed": bool(passed), "replay": replay,
                    })
            # 验收标准是「不劣于」而不是「必须改善」：只要候选通过回放门槛
            # （中位 R ≥ 下限、最差单笔不恶化），就可以落地；同等条件下优先
            # 中位 R 更高、最差单笔更好的候选。
            ok = [c for c in candidates if c["passed"]]
            best = max(
                ok,
                key=lambda c: (
                    float((c["replay"] or {}).get("median_delta") or 0.0),
                    float((c["replay"] or {}).get("worst_delta") or 0.0),
                ),
            ) if ok else None
            gate = dict(self.position_tuning.get("gate") or {})
            if best is not None:
                eff = dict(prev)
                eff[best["param"]] = float(best["to"])
                self.position_tuning["effective"] = eff
                self.position_tuning["anchor"] = dict(base)
                log = list(self.position_tuning.get("changes") or [])
                log.append({
                    "param": best["param"], "from": best["from"], "to": best["to"],
                    "rule": "replay",
                    "why": f"L2 回放验收：中位 R {best['replay'].get('median_delta'):+.2f}",
                    "ts": time.time(),
                })
                self.position_tuning["changes"] = log[-10:]
                gate["l2_passed"] = True
                gate["l2_passed_at"] = gate.get("l2_passed_at") or time.time()
                gate["l2_stable_since"] = gate.get("l2_stable_since") or time.time()
            else:
                gate["l2_passed"] = False
            gate["l2_candidates"] = len(candidates)
            gate["l2_checked_at"] = time.time()
            self.position_tuning["gate"] = gate
            self.position_tuning["replay"] = (best or (candidates[0] if candidates else {})).get("replay") or {}
            self._sync_tuning_state()
            self._save()
            self._replay_result = {
                "running": False, "finished_at": time.time(), "passed": bool(best),
                "candidates": candidates, "best": best, "gate": gate,
            }
        except Exception as exc:  # noqa: BLE001 - 验收失败不影响交易
            self._replay_result = {
                "running": False, "finished_at": time.time(), "passed": False,
                "error": f"{type(exc).__name__}: {exc}",
            }


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
        detail = self._factor_detail(symbol)
        if detail is None:
            return None
        return float(detail.get("composite") or 0.0)

    def _factor_detail(self, symbol: str) -> dict | None:
        """该品种当前的因子明细 {"composite": float, "signals": [...]}（90 秒缓存）。

        算一次 `compute_factor_signal` 要起子进程（~1.25s），所以必须缓存。开仓时
        用它固化「各因子当时的方向」，平仓时才能把真实价格走向归因回因子。
        与 `_opportunity_composite` 共用同一份缓存，不额外付出计算成本。
        """
        cache = getattr(self, "_composite_cache", None)
        if cache is None:
            cache = self._composite_cache = {}
        now = time.time()
        hit = cache.get(symbol)
        if hit and now - float(hit[0]) < 90.0 and isinstance(hit[1], dict):
            return hit[1]
        # 必须用「这个品种自己的行情」：self.df 是当前活跃品种，多品种持仓时
        # 拿它算另一个品种的 composite，会按错误的因子值触发反向退出平仓。
        df = self._frame_for(symbol)
        if df is None or not len(df):
            return None
        try:
            from .factors.signal import compute_factor_signal

            raw = compute_factor_signal(self.cfg.research.factor_library, df)
        except Exception:  # noqa: BLE001 - 因子算不出来就退回纯价格规则
            return None
        detail = {
            "composite": float(raw.get("composite") or 0.0),
            "signals": [
                {
                    "name": str(s.get("name") or ""),
                    "target": float(s.get("target") or 0.0),
                    "weight": float(s.get("weight") or 0.0),
                    "kind": str(s.get("kind") or ""),
                }
                for s in (raw.get("signals") or [])
                if s.get("name")
            ],
        }
        cache[symbol] = (now, detail)
        return detail

    def _factor_record(self, symbol: str) -> dict:
        """开仓时固化因子快照：合成值 + 各因子方向（最多 12 个，控制体积）。

        平仓时用「市场实际方向」评判每个因子当时的方向，而不是用这笔仓的盈亏——
        一个仓位里多个因子共线，用 P&L 归因会把功劳/责任算到错的因子上。
        """
        try:
            detail = self._factor_detail(symbol)
        except Exception:  # noqa: BLE001 - 快照失败绝不能影响开仓
            return {}
        if not detail:
            return {}
        items = []
        for s in detail.get("signals") or []:
            tgt = float(s.get("target") or 0.0)
            if abs(tgt) < 0.05:      # 微弱信号不参与归因，避免噪音
                continue
            items.append({
                "name": s["name"],
                "target": round(tgt, 4),
                "weight": round(float(s.get("weight") or 0.0), 4),
                "kind": s.get("kind") or "",
            })
        items.sort(key=lambda x: -abs(x["target"]) * max(x["weight"], 1e-9))
        return {
            "composite": round(float(detail.get("composite") or 0.0), 4),
            "items": items[:12],
        }

    def _attach_factor_snapshot(self, symbol: str) -> None:
        """开仓成交后补因子快照（后台线程，绝不阻塞下单与全局锁）。

        因子计算要起子进程（~1.25s，命中 90 秒缓存则零成本）。execute 全程持
        `self.lock`，在这里同步算会卡住持仓守护/其它品种的开平仓，所以放到一次性
        后台线程里做。失败或拿不到行情就当作没有快照：平仓时只是少统计一笔，
        不影响任何交易逻辑；下一次 `_save()` 会把它一起落盘。
        """
        def _work() -> None:
            try:
                pos = self._get_position(symbol)
                if pos is None:
                    return
                rec = self._factor_record(symbol)
                if rec:
                    pos["factor_snapshot"] = rec
            except Exception:  # noqa: BLE001 - 快照失败不影响持仓
                pass

        try:
            threading.Thread(target=_work, name="factor-snap", daemon=True).start()
        except Exception:  # noqa: BLE001 - 起不了线程也不能影响交易
            pass

    def _sync_live_stops(self, pos: dict, new_stop: float) -> bool:
        """实盘按新止损重挂条件单。

        同方向 closePosition 条件单只能有一张，所以必须「先撤后挂」。
        万一新止损挂不上，立刻回退挂回旧止损并发 critical 告警 —— 绝不能留下裸仓。
        """
        sym = pos.get("symbol") or self.cfg.market.symbol
        client = self.live_client
        if client is None or not self.armed:
            return False
        close_side = "SELL" if self._pos_side(pos) == "LONG" else "BUY"
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
        close_side = "SELL" if self._pos_side(pos) == "LONG" else "BUY"
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
        """停止持仓守护线程。线程是 daemon，产品路径不调用；仅供测试清理。"""
        self._guard_stop.set()

    def _guard_loop(self) -> None:
        while not self._guard_stop.wait(self._guard_interval):
            try:
                if not self._positions_map():
                    continue
                self._reconcile_position_with_exchange()
                # 资金费同步：实盘持仓期间交易所每 8h 结算一次，本地回执拿不到，
                # 这里周期性补记进账本，让累计已实现盈亏与交易所余额对齐。
                self._sync_funding_fees()
                # 符号感知取当前活跃品种实时价（ws 优先），不再用全局标量，
                # 避免 auto_rotate 切品种瞬间滞后的标量污染持仓管理。
                v = self._quote_for(self.cfg.market.symbol)
                live_px = v if v > 0 else None
                # 逐仓管理：每个品种用自己的行情、自己的移动止损节流。
                # 一个品种报错不能连累其它仓位（以前单个仓位异常就整轮中断）。
                for pos in list(self._positions_map().values()):
                    try:
                        self._retry_pending_take_profit(pos)
                        sym = self._pos_symbol(pos, self.cfg.market.symbol)
                        px = live_px if sym == str(self.cfg.market.symbol).upper() else None
                        self._manage_open_position(pos, px)
                    except Exception as exc:  # noqa: BLE001 - 单仓异常不影响其它仓
                        self.last_error = f"持仓守护异常（{self._pos_symbol(pos)}）：{type(exc).__name__}: {str(exc)[:100]}"
            except Exception as exc:  # noqa: BLE001 - 守护线程绝不能因单次异常退出
                self.last_error = f"持仓守护异常：{type(exc).__name__}: {str(exc)[:120]}"

    # ---- 持仓报价线程 --------------------------------------------------
    def start_quote_guard(self, interval_sec: int = 4) -> None:
        """启动/确认持仓报价线程。幂等，重复调用不会创建第二个线程。

        存在的理由：页面每 3 秒拉一次 /api/status，但持仓管理 30 秒才跑一轮，
        期间标记价、浮盈全是旧值，看起来像「行情卡住了」。这里用毫秒级的
        /ticker/price 单独刷价，只刷「已持仓且不是当前品种」的，空仓时零请求。
        """
        self._quote_interval = max(2, int(interval_sec))
        if getattr(self, "_quote_thread", None) and self._quote_thread.is_alive():
            return
        self._quote_stop.clear()
        self._quote_thread = threading.Thread(
            target=self._quote_loop, name="position-quote", daemon=True
        )
        self._quote_thread.start()

    def stop_quote_guard(self) -> None:
        """停止报价线程。线程是 daemon，产品路径不调用；仅供测试清理。"""
        self._quote_stop.set()

    def _quote_loop(self) -> None:
        while not self._quote_stop.wait(self._quote_interval):
            try:
                self.refresh_position_quotes()
            except Exception:  # noqa: BLE001 - 报价线程绝不能因单次异常退出
                pass

    def refresh_position_quotes(self) -> dict[str, float]:
        """把所有持仓品种的最新成交价刷进 _last_prices（毫秒级 ticker）。

        **当前品种也刷**：页面价格读的是 K 线末根收盘，盘中不会每 4 秒更新，
        只刷「其它品种」会让当前仓的标记价/浮盈滞后几分钟，看起来像行情卡住、
        平仓时才发现价格早已反向。这里每轮对全部持仓品种各发一次轻量请求。

        取价失败保持旧值不动，绝不清零——清掉会让页面从价格变成「—」。
        """
        prefer = self.state.get("exchange", "binance")
        now = time.time()
        # ws 推送窗口：ws 刚推过的品种跳过 REST，别把毫秒级实时价又覆盖回 4 秒快照。
        # ws 断线后 3 秒内 REST 自动接管，价格不会卡死。
        ws_fresh = 3.0 if getattr(self, "_ws_enabled", True) else 0.0
        out: dict[str, float] = {}
        for sym in list(self._positions_map().keys()):
            ts = float((getattr(self, "_price_ts", None) or {}).get(sym) or 0.0)
            if ts and (now - ts) < ws_fresh:
                continue
            try:
                px, _src = fetch_live_price(sym, prefer=prefer)
            except Exception:  # noqa: BLE001 - 单品种失败不影响其它品种
                continue
            px = float(px or 0.0)
            if px <= 0:
                continue
            self._quote_cache[sym] = (px, time.time())
            self._stamp_price(sym, px)
            out[sym] = px
        return out

    # ---- 行情 WebSocket 推送 --------------------------------------------
    def start_ws_guard(self) -> None:
        """启动行情 WebSocket 推送线程（幂等）。订阅持仓品种 + 当前活跃品种。"""
        if not getattr(self, "_ws_enabled", True):
            return
        thread = getattr(self, "_ws_thread", None)
        if thread is not None and getattr(thread, "alive", False):
            return
        from .wsfeed import PriceStream

        self._ws_stop.clear()
        self._ws_thread = PriceStream(
            exchange=self.state.get("exchange", "binance"),
            get_exchange=lambda: self.state.get("exchange", "binance"),
            get_symbols=self._ws_symbols,
            on_price=self._on_ws_price,
        )
        self._ws_thread.start()

    def stop_ws_guard(self) -> None:
        """停止行情推送线程（供测试清理；产品路径由 daemon 随进程退出）。"""
        thread = getattr(self, "_ws_thread", None)
        if thread is not None:
            thread.stop()
            self._ws_thread = None

    def _ws_restart(self) -> None:
        """交易所切换后重连行情流（binance ↔ okx 端点不同）。"""
        if not hasattr(self, "_ws_stop"):
            return  # object.__new__ 构造的轻量测试实例未跑 __init__，跳过
        self.stop_ws_guard()
        self.start_ws_guard()

    def _on_ws_price(self, sym: str, px: float) -> None:
        if px and px > 0:
            self._stamp_price(str(sym).upper(), float(px))

    def _ws_symbols(self) -> list[str]:
        """行情流订阅的品种：全部持仓 + 当前活跃品种（顶栏价格也要实时）。"""
        syms = list(self._positions_map().keys())
        cur = str(self.cfg.market.symbol).upper()
        if cur not in syms:
            syms.append(cur)
        return syms

    # ---- 全品种缓存预热 --------------------------------------------------
    def start_symbol_cache_guard(self, interval_sec: int | None = None) -> None:
        """启动全品种缓存守护线程（幂等）。

        存在的理由：`auto_top_k > 1` 后一轮会依次切多个品种，而切换走的是
        `load_market`——它按品种落盘、盘缓存过期就当场重下 6000 根 K 线。
        不预热的话，一轮 3 个品种可能多花十几秒，且行情越不活跃越容易过期。
        """
        mkt = self.cfg.market
        if interval_sec is None:
            interval_sec = int(getattr(mkt, "cache_warm_interval_sec", 15) or 15)
        self._warm_interval = max(5, int(interval_sec))
        if getattr(self, "_warm_thread", None) and self._warm_thread.is_alive():
            return
        self._warm_stop.clear()
        self._warm_thread = threading.Thread(
            target=self._warm_loop, name="symbol-cache", daemon=True
        )
        self._warm_thread.start()

    def _warm_loop(self) -> None:
        while not self._warm_stop.wait(self._warm_interval):
            try:
                self.warm_symbol_cache()
            except Exception:  # noqa: BLE001 - 预热线程绝不能因单次异常退出
                pass

    def warm_symbol_cache(self) -> dict:
        """把「全部可交易品种」的报价与 K 线缓存焐热，返回本轮做了多少事。

        三档节流，避免和决策/页面抢带宽：
        - 报价：每 `cache_warm_interval_sec`（默认 15s）一轮，每个品种一次轻量请求；
        - 短周期 K 线：按盘缓存 TTL（半个 bar）节流，默认 5m 图约 2.5 分钟一次；
        - 主周期 K 线：每 `cache_warm_main_sec`（默认 300s）一轮，且跳过当前品种
          （它的行情由 refresh_data 负责，重复拉是浪费）。
        """
        mkt = self.cfg.market
        if not bool(getattr(mkt, "cache_all_symbols", True)):
            return {}
        if getattr(self, "_warm_busy", False):
            return {}
        syms = [str(s).upper() for s in (getattr(mkt, "symbols", None) or [])][:12]
        if not syms:
            return {}

        cur = str(mkt.symbol).upper()
        prefer = self.state.get("exchange", "binance")
        real = str(getattr(mkt, "source", "synthetic")) == "binance"
        short_iv = str(getattr(mkt, "short_interval", "5m") or "5m")
        short_bars = max(20, min(int(getattr(mkt, "short_bars", 200) or 200), 500))
        now = time.time()
        every_main = max(60, int(getattr(mkt, "cache_warm_main_sec", 300) or 300))
        do_main = (now - float(self._warm_main_at or 0.0)) >= every_main
        short_ttl = kl_disk_ttl(short_iv)

        out = {"symbols": len(syms), "quotes": 0, "main": 0, "short": 0}
        self._warm_busy = True
        try:
            for sym in syms:
                # 1) 报价：页面标记价、机会榜、非活跃持仓都读它
                try:
                    px, _src = fetch_live_price(sym, prefer=prefer)
                    px = float(px or 0.0)
                    if px > 0:
                        self._quote_cache[sym] = (px, time.time())
                        self._stamp_price(sym, px)
                        out["quotes"] += 1
                except Exception:  # noqa: BLE001 - 单品种取价失败不影响其它
                    pass
                if not real or sym == cur:
                    continue
                # 2) 主周期 K 线：用副本 cfg 只换 symbol，避免动到全局配置
                if do_main:
                    try:
                        load_market(replace(mkt, symbol=sym), prefer=prefer)
                        out["main"] += 1
                    except Exception:  # noqa: BLE001
                        pass
                # 3) 短周期 K 线：持仓管理员的 ATR/均线要它
                try:
                    if now - float(self._kl_short_warm_at.get(sym, 0.0)) >= short_ttl:
                        fetch_klines(sym, short_iv, short_bars, prefer=prefer)
                        self._kl_short_warm_at[sym] = now
                        out["short"] += 1
                except Exception:  # noqa: BLE001
                    pass
            if do_main:
                self._warm_main_at = now
        finally:
            self._warm_busy = False
        self._warm_last = now
        self._warm_last_result = out
        return out

    # ---- 持仓管理员的 LLM 闸门 ----------------------------------------
    def _position_llm_client(self):
        """持仓管理员专用共享 LLMClient：配置变更后由 invalidate 清掉再重建。"""
        if self._position_llm is None:
            from .agents.llm import LLMClient
            self._position_llm = LLMClient(self.cfg.llm)
        return self._position_llm

    def invalidate_position_llm(self) -> None:
        """LLM provider/model/Key 改变后调用，避免旧客户端继续用老配置。"""
        self._position_llm = None

    def _position_llm_allowed(self, sym: str, offpeak_gate: bool = True) -> bool:
        """谷时优先 + 每仓冷却。任一不满足就跳过本次调用，绝不因 LLM 拖慢退出。

        `offpeak_gate=False` 表示本次调用不受峰谷约束（仅保留冷却频率上限）：
        L3 进化提案是持仓管理的重要决策，不该被「峰时省一半钱」的成本优化静默关掉。
        """
        cfg = getattr(self.cfg, "position", None)
        if cfg is None:
            return False
        now = time.time()
        if offpeak_gate and bool(getattr(cfg, "llm_offpeak_only", True)):
            from .agents.llm import is_offpeak
            if not is_offpeak(self.cfg.llm):
                self.position_llm_usage["skipped_offpeak"] += 1
                return False
        gap = max(0, int(getattr(cfg, "llm_min_interval_sec", 900) or 0))
        last = float(self._position_llm_at.get(sym, 0.0) or 0.0)
        if gap and now - last < gap:
            self.position_llm_usage["skipped_cooldown"] += 1
            return False
        # 先占位：即使调用失败，也不会在同一秒被反复放行；
        # _commit_position_llm_call 会在成功后把时间戳刷新、失败时回拨到
        # 「gap - 60」，即失败只冷却 60 秒而不是整段 15 分钟。
        self._position_llm_at[sym] = now
        self.state["position_llm_at"] = dict(self._position_llm_at)
        return True

    def _commit_position_llm_call(self, sym: str, ok: bool, n: int = 1) -> None:
        """LLM 调用结束后才落冷却与用量：失败不该白锁一整段冷却，也不该计成功。

        `n` 表示本轮实际发生的 LLM 调用次数（解释/影子与 L3 进化各算一次），
        避免「同一轮调了两次却只计一次」导致用量与频率被低估。
        """
        now = time.time()
        # 用基础配置而不是 effective_position_config()：后者依赖实例状态，
        # 测试里用 object.__new__ 构造的轻量实例没有该方法。
        gap = max(0, int(getattr(getattr(self.cfg, "position", None),
                                 "llm_min_interval_sec", 900) or 0))
        n = max(1, int(n or 1))
        if ok:
            self._position_llm_at[sym] = now
            self.position_llm_usage["calls"] += n
        else:
            # 失败只冷却 60 秒，避免网关抖动时立刻重试，也不把 15 分钟全浪费掉
            self._position_llm_at[sym] = now - max(0, gap - 60)
            self.position_llm_usage["errors"] += n
        self.state["position_llm_at"] = dict(self._position_llm_at)
        self.state["position_llm_usage"] = dict(self.position_llm_usage)

    def _manage_open_position(self, pos: dict, price: float | None = None) -> dict:
        """包装：抢锁防重入（自动循环与守护线程可能同时进入）。"""
        lock = getattr(self, "_guard_lock", None)
        if lock is None:      # 兼容用 object.__new__ 构造的测试实例
            out = self._manage_open_position_inner(pos, price)
            out["ts"] = time.time()
            return out
        if not lock.acquire(blocking=False):
            # 抢锁失败不是「巡检结论」，必须显式标记 skipped，否则 run_decision
            # 会把这条假 hold 当成最新巡检写进页面，掩盖真正的心跳。
            sym = self._pos_symbol(pos, self.cfg.market.symbol)
            out = {"action": "hold", "reason": "持仓管理已在另一线程执行",
                   "details": {"contended": True}, "skipped": True,
                   "ts": time.time(), "symbol": sym}
            self.last_management_by_symbol[sym] = out
            return out
        try:
            out = self._manage_open_position_inner(pos, price)
            # 每轮都刷新时间戳。hold 不产生动作，没有时间戳就分不清
            # 「刚刚巡检过」和「线程早就停了」——页面靠它显示上次巡检时刻。
            out["ts"] = time.time()
            sym = self._pos_symbol(pos, self.cfg.market.symbol)
            out["symbol"] = sym
            self.last_management_by_symbol[sym] = out
            # last_management 保留「当前品种」那一份，老前端只读这一个字段
            if sym == str(self.cfg.market.symbol).upper() or not self.last_management:
                self.last_management = out
            # 最近一次「有动作」单独留档：下一轮 hold 会覆盖 last_management，
            # 但页面上这条（含 LLM 解释）必须继续展示，否则用户根本看不到它。
            if out.get("action") not in (None, "hold"):
                slim = {k: out[k] for k in ("action", "reason", "new_stop",
                                            "explanation", "ts", "symbol") if k in out}
                det = dict(out.get("details") or {})
                det.pop("close_result", None)   # 订单回执可能很大，不进状态存档
                slim["details"] = det
                self.last_management_action = slim
                self.state["last_management_action"] = slim
                self._save()
            return out
        finally:
            lock.release()

    def _manage_open_position_inner(self, pos: dict, price: float | None = None) -> dict:
        """在「已有持仓、跳过开仓决策」的这一轮里做退出管理。"""
        # 用带自校准覆盖的配置副本：样本够时参数会随实盘结果缓慢演进，
        # 样本不足时它与原配置完全等价（calibrated=False）。
        cfg = self.effective_position_config()
        if cfg is None or not cfg.enabled:
            return {"action": "hold", "reason": "持仓管理员未启用", "details": {}}
        try:
            sym = self._pos_symbol(pos, self.cfg.market.symbol)
            # K 线必须按仓位自己的品种取：拿当前品种的行情去管另一个品种的仓，
            # ATR / 均线 / 持有根数全是错的。
            df = self._frame_for(sym)
            if df is None or not len(df):
                return {"action": "hold", "reason": "行情暂不可用，本轮不管理", "details": {}}
            px = float(price) if price and price > 0 else self._quote_for(sym)
            if px <= 0:
                return {"action": "hold", "reason": "取不到最新价，本轮不管理", "details": {}}
            composite = self._opportunity_composite(sym)
            # composite 来自机会扫描/短缓存，可能已经过期；把时间戳一起交给规则层做新鲜度校验
            comp_ts = None
            hit = (getattr(self, "_composite_cache", None) or {}).get(sym)
            if hit:
                comp_ts = float(hit[0])
            elif any(r.get("symbol") == sym and not r.get("error")
                     for r in (self.opportunities or [])):
                comp_ts = float(self.scan_state.get("last_ts") or 0) or None
            dec = manage_position(pos, df, px, cfg, composite=composite,
                                  composite_ts=comp_ts)

            # 记录持仓期峰值（以 R 计）：自校准判断「冲高后回吐」的唯一依据。
            # 用现价算即可，不依赖 K 线重放；每 30 秒采样一次，只会略微低估峰值。
            try:
                _entry = float(pos.get("entry") or 0.0)
                _r = float(pos.get("initial_r_pct") or 0.0)
                if _entry > 0 and _r > 0:
                    _sign = 1.0 if str(pos.get("side") or "LONG").upper().startswith("L") else -1.0
                    _fav_r = (px - _entry) / _entry * _sign / _r
                    if _fav_r > float(pos.get("mfe_r") if pos.get("mfe_r") is not None else -99.0):
                        pos["mfe_r"] = round(_fav_r, 4)
                        self._save()
            except Exception:  # noqa: BLE001 - 峰值记录失败不影响退出判断
                pass

            if dec.action == "close":
                res = self.close(px, reason=f"持仓管理员：{dec.reason}", symbol=sym,
                                 rule=dec.rule or "position_manager")
                dec.details["close_result"] = res
                self._notify("info", f"{sym} 持仓管理员提前退出", dec.reason,
                             {"symbol": sym, "action": "close", "details": dec.details})

            elif dec.action == "tighten" and dec.new_stop is not None:
                now = time.time()
                last_at = float(self._stop_adjust_at_by_symbol.get(sym, 0.0))
                if now - last_at < cfg.min_adjust_interval_sec:
                    dec.details["throttled"] = True
                    return dec.to_dict()
                live = pos.get("mode") == "live" and self.state.get("mode") == "live"
                if live and cfg.live_stop_adjust and not self._sync_live_stops(pos, dec.new_stop):
                    return {"action": "hold", "reason": "交易所条件单未更新，本地止损保持不变",
                            "details": dec.details}
                with self.lock:
                    if self._positions_map().get(sym) is pos:
                        pos["stop"] = float(dec.new_stop)
                        # 标记「这笔仓的止损被移动过」：它最终多半以 stop_loss 出场，
                        # 自校准要能把这类出场归到移动止损组，而不是普通止损。
                        pos["trailed"] = True
                        self._save()
                self._stop_adjust_at_by_symbol[sym] = now
                self.state["last_stop_adjust_by_symbol"] = dict(self._stop_adjust_at_by_symbol)
                if sym == str(self.cfg.market.symbol).upper():
                    self._stop_adjust_at = now
                    self.state["last_stop_adjust_at"] = now
                self._save()
                self._notify("info", f"{sym} 移动止损已更新", dec.reason,
                             {"symbol": sym, "new_stop": dec.new_stop})

            # 解释 / 影子复核：共用一次 LLM 调用，受谷时 + 每仓冷却闸门约束
            # （旁路，成本敏感）。影子意见只写日志，绝不参与 action。
            want_review = (
                (bool(getattr(cfg, "llm_explain", False)) and dec.action != "hold")
                or bool(getattr(cfg, "llm_shadow", False))
            )
            # L3 进化提案：独立于解释旁路。峰谷（llm_offpeak_only）只是成本优化，
            # 不该让 L3 静默失效，因此 L3 开启时跳过峰谷检查、只保留冷却频率上限。
            want_evolve = self._llm_evolution_allowed()
            if (want_review or want_evolve) and self._position_llm_allowed(
                    sym, offpeak_gate=not want_evolve):
                ok = False
                n_calls = 0
                try:
                    if want_review:
                        review = review_management(self._position_llm_client(), cfg, dec, pos)
                        n_calls += 1
                        dec.explanation = review.get("explanation") or ""
                        sh = review.get("shadow")
                        if sh:
                            rec = {
                                "ts": time.time(), "symbol": sym,
                                "rule_action": dec.action, "rule_reason": dec.reason,
                                "llm_action": sh.get("action"),
                                "confidence": sh.get("confidence"),
                                "reason": sh.get("reason"),
                                "source": sh.get("source"),
                                "degraded": sh.get("degraded"),
                            }
                            self.llm_shadow_log.append(rec)
                            self.llm_shadow_log = self.llm_shadow_log[-50:]
                            self.state["llm_shadow_log"] = list(self.llm_shadow_log)
                            dec.details["shadow_action"] = sh.get("action")
                    if want_evolve:
                        ev = propose_evolution(
                            self._position_llm_client(), cfg, pos, dec,
                            self.position_tuning_status(),
                            allowed_params=RULE_PARAMS.get(dec.rule, ()),
                        )
                        n_calls += 1
                        min_conf = self._cfg_num(
                            getattr(cfg, "evolution_llm_min_confidence", 0.65), 0.65)
                        if (ev and not ev.get("degraded")
                                and float(ev.get("confidence") or 0.0) >= min_conf):
                            self._apply_llm_evolution(dec, ev, pos)
                    ok = True
                except Exception:  # noqa: BLE001 - LLM 是旁路，绝不阻断退出动作
                    ok = False
                finally:
                    # 按实际调用次数记账：解释/影子与 L3 进化各算一次，不再只计一次
                    self._commit_position_llm_call(sym, ok, n=n_calls)
                if ok:
                    self._save()
            return dec.to_dict()
        except Exception as exc:  # noqa: BLE001 - 持仓管理绝不阻断主流程
            return {"action": "hold", "reason": f"持仓管理异常，保守不动：{type(exc).__name__}",
                    "details": {"error": str(exc)[:160]}}

    def run_decision(self, rounds: int = 2, price: float | None = None) -> dict:
        # 当前品种已有持仓时不再跑开仓决策：既省掉每轮 LLM 调用，也不会每 5 分钟推一条
        # 「决策被风控拦截」。先与交易所对账（实盘），避免交易所侧止损/止盈已成交、
        # 本地缓存还挂着，导致一直等不到「平仓后再决策」。
        # 注意：别的品种持仓不影响本品种决策——组合层敞口由 _positions_for_risk 汇总。
        # 当前品种在当前环境不可交易时不跑决策：白花一轮 LLM 调用，结论也执行不了。
        # 典型场景是 OKX 模拟盘（只上线部分永续），主网有的币种模拟盘没有。
        probe = self.symbol_tradable(str(self.cfg.market.symbol).upper())
        if not probe["ok"]:
            return {
                "skipped": True,
                "reason": probe["reason"],
                "symbol": self.cfg.market.symbol,
                "code": probe.get("code") or "symbol_unsupported",
                "management": {},
            }
        self._reconcile_position_with_exchange()
        held = self._get_position()
        if held:
            return {
                "skipped": True,
                "reason": f"{self.cfg.market.symbol} 已有持仓，等平仓完成后再做开仓决策",
                "symbol": self.cfg.market.symbol,
                "position": held,
                # 持仓期间不是完全放手：由持仓管理员做移动止损 / 时间止损 / 趋势反转退出
                "management": self._manage_open_position(held, price),
            }
        cap = self.position_limit()
        held_count = len(self._positions_map())
        if held_count >= cap:
            return {
                "skipped": True,
                "reason": f"同时持仓已达上限 {cap} 个品种（当前 {held_count} 个），本轮不开新仓",
                "symbol": self.cfg.market.symbol,
                "position": self.state.get("position"),
                "management": {},
            }
        df = self.df if self.df is not None else self.refresh_data()[0]
        # 开仓基准价优先用 ws 实时价（<1s 新鲜），拿不到再退回 REST 实时价 / K 线末根。
        # 决策的「最新价」也随 live_price 注入 LLM，消除 K 线末根收盘的滞后。
        sym = str(self.cfg.market.symbol).upper()
        live_px = float(self._mark_price(sym) or 0.0)
        if live_px <= 0 and price and price > 0:
            live_px = float(price)
        ref_price = live_px if live_px > 0 else float(df["close"].iloc[-1])
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
            live_price=live_px if live_px > 0 else None,
        )
        self.last_decision = decision

        # A2：决策被风控引擎拒绝时告警（approved=False 且 action=flat 才是真拒绝）
        # 同一品种 + 同一原因 30 分钟内只提醒一次：否则每轮都会刷一条相同的拦截通知。
        if (not decision.risk_decision["approved"] and decision.risk_decision["action"] == "flat") \
                or any(c.get("check") == "risk_officer" and not c.get("passed")
                       for c in (decision.risk_decision.get("checks") or [])):
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
        self._prune_decisions(out)

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
        price = price if (price is not None and float(price) > 0) else self.price()
        if not price or float(price) <= 0:
            return {"ok": False, "error": "拿不到有效的最新价，拒绝下单"}

        if not decision and not order.get("quantity"):
            return {"ok": False, "error": "当前决策没有订单（风控未通过或策略观望）"}

        # 下单前先确认这个品种在当前环境真的存在：OKX 模拟盘只上线部分永续，
        # 不在清单里的品种会在 set_leverage 第一步就撞 51001，用户只看到
        # 「Instrument ID doesn't exist」完全无从下手。这里给一句能读懂的拒绝。
        verdict = self.symbol_tradable(str(self.cfg.market.symbol).upper())
        if not verdict["ok"]:
            return {"ok": False, "error": f"{verdict['reason']}，无法下单",
                    "code": verdict.get("code") or "symbol_unsupported"}

        # 先与交易所核对，清理幽灵仓位，避免误判「已有持仓」而拒绝开仓
        self._reconcile_position_with_exchange()
        with self.lock:
            if self.state.get("halt"):
                return {"ok": False, "error": "熔断开关已打开"}
            sym = str(self.cfg.market.symbol).upper()
            # 同一品种只允许一笔：加仓不做，避免把单品种风险叠成两倍
            if self._get_position(sym):
                return {"ok": False, "error": f"{sym} 已有持仓，拒绝重复开仓（请先平仓）"}
            cap = self.position_limit()
            held_count = len(self._positions_map())
            if held_count >= cap:
                return {"ok": False,
                        "error": f"同时持仓已达上限 {cap} 个品种（当前 {held_count} 个），"
                                 "先平掉一个再开仓"}

            # 把这次决策时三位分析师的立场带进仓位，平仓时用于方向命中率校准
            stances = list(self.state.get("pending_stances") or [])
            side = "LONG" if order["side"] == "BUY" else "SHORT"
            qty = float(order["quantity"])
            lev = float(order["leverage"])
            stop = float(order["stop_loss_price"])
            tp = float(order["take_profit_price"])

            # 方向兜底：空单止损必须高于现价、多单必须低于现价，否则交易所直接 -2021
            # （历史计划里存着的老价格也可能已经穿越现价，执行前统一校正一次）。
            # ⚠️ 必须用订单自带的决策参考价 entry_ref_price，不能用 self.price()：
            # self.price() 取的是 self.df 末根收盘，而 self.df 会被扫描/切换品种覆盖成
            # 别的主力品种（如 BTC）；一旦它返回 BTC 量级价格，SOL 的止损就会被错算到
            # 76000+ 而永远触发不了，把仓位「焊死」在线上。entry_ref_price 是订单生成时
            # 该品种的正确参考价，天然不会被串台。仅在它缺失时才回退到当前价。
            gap = 0.003
            ref = float(order.get("entry_ref_price") or 0.0) or float(price)
            if side == "SHORT":
                if stop <= ref * (1 + 0.0005):
                    stop = round_price(ref * (1 + gap))
                if tp >= ref * (1 - 0.0005):
                    tp = round_price(ref * (1 - gap))
            else:
                if stop >= ref * (1 - 0.0005):
                    stop = round_price(ref * (1 - gap))
                if tp <= ref * (1 + 0.0005):
                    tp = round_price(ref * (1 + gap))

            if self.state["mode"] == "live":
                if not self.armed or self.live_client is None:
                    return {"ok": False, "error": "实盘未解锁"}
                try:
                    # 顺序不能反：OKX 在该品种还有未成交的条件单（止盈/止损/追踪）时
                    # 会拒绝调整杠杆（59669），先 set_leverage 会让整个执行直接失败。
                    # 而且 closePosition 的止损单一个方向只能有一张，上一次执行留下的
                    # 孤儿单会让新止损报 -4130，所以两个交易所都先撤干净再设杠杆。
                    try:
                        self.live_client.cancel_all(self.cfg.market.symbol)
                    except Exception:  # noqa: BLE001 - 没有旧单时会报错，忽略
                        pass
                    self.live_client.set_leverage(self.cfg.market.symbol, int(round(lev)))
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
                            # 主单已成交、止损与紧急平仓都失败：这是真正的裸仓。
                            # 不能只写一行 last_error 就当作处理完了——落一条待处置记录，
                            # 由守护线程/人工在下一轮看到（_reconcile 也会核对交易所持仓）。
                            self.state["unmanaged_exposure"] = {
                                "symbol": sym, "side": side, "qty": qty,
                                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "detail": str(exc2)[:200],
                            }
                            self._notify("critical", f"{sym} 出现无保护敞口",
                                         f"止损与紧急平仓均失败：{exc2}", {"symbol": sym})
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
                    # 成交明细可能有索引延迟：拿不到就用下单回执里的手续费（OKX 订单详情已经带回）
                    fee = float(detail["fee"]) if detail else float(res.fee or 0.0)
                    # 数量必须用交易所回读的成交量：下单量会被 stepSize 量化，
                    # 用未量化的 qty 记账会让本地仓位与交易所长期偏离。
                    fill_qty = float(detail["qty"]) if detail and detail.get("qty") else float(res.qty or qty)
                    plan_px = float(order.get("entry_ref_price") or 0.0) or None
                    self._put_position({
                        "symbol": sym,
                        "side": side,
                        "qty": fill_qty,
                        "entry": fill_px,
                        # 开仓手续费随仓固化：供审计与连亏风控使用。费用本身在开仓时
                        # 已由这条 OPEN 流水单独计入累计已实现盈亏，平仓不再重复扣。
                        "entry_fee": round(fee, 8),
                        "leverage": lev,
                        "stop": stop,
                        "tp": tp,
                        "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "opened_epoch": time.time(),
                        "initial_r_pct": abs(fill_px - stop) / fill_px if fill_px > 0 else 0.0,
                        "analyst_stances": stances,
                        "mode": "live",
                        "order_id": res.order_id,
                    })
                    # 成交之后再补因子快照：因子计算要起子进程（~1.25s），
                    # 放在下单前会推迟成交、放大滑点，所以绝不能前移。
                    self._attach_factor_snapshot(sym)
                    self.state["fills"].append(
                        {
                            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "action": "OPEN",
                            "side": side,
                            "symbol": sym,
                            "qty": fill_qty,
                            "price": round_price(fill_px),
                            "plan_price": round_price(plan_px),
                            "slippage_bps": round(
                                (fill_px - plan_px) / plan_px * 10_000.0, 2
                            ) if plan_px else None,
                            "fee": round(fee, 6),
                            "fee_mode": "gross",
                            "reason": f"live:{res.order_id}",
                            "account_scope": self._scope(),
                        }
                    )
                    self._save()
                    self._log_live("OPEN", side, fill_qty, res.price or price, res.order_id)
                    self._consume_plan()
                    return {"ok": True, "mode": "live", "order_id": res.order_id}
                except ExchangeError as exc:
                    self.last_error = str(exc)
                    return {"ok": False, "error": str(exc)}
            else:
                rec = self._paper_open(side, qty, price, lev, stop, tp, reason,
                                       plan_price=order.get("entry_ref_price"),
                                       stances=stances)
                # 同上：成交后再补因子快照，不拖慢撮合
                self._attach_factor_snapshot(str(self.cfg.market.symbol).upper())
                self._consume_plan()
                self._save()
                return {"ok": True, "mode": "paper", "fill": rec}

    def _consume_plan(self) -> None:
        """订单计划一旦执行就作废，避免重复下单。"""
        self.pending_order = None
        # 进程内的 last_decision 也必须清掉：execute() 优先读它，只清 pending_order
        # 的话，交易所侧快速平仓 + 本地对账清掉幽灵仓后，同一份订单会被二次执行。
        self.last_decision = None
        # 立场已经写进仓位，清掉待办避免下一笔误用旧立场
        self.state["pending_stances"] = []
        ld = self.state.get("last_decision")
        if isinstance(ld, dict) and isinstance(ld.get("order"), dict):
            ld["order"]["status"] = "FILLED"

    def close(self, price: float | None = None, reason: str = "manual",
              symbol: str | None = None, rule: str = "") -> dict:
        """平仓。`symbol` 不传时平「当前品种」；只有一个仓时也兼容不传的老调用。"""
        # 先解析目标仓位的品种（不持锁），以便按「仓位自己的品种」取符号感知实时价。
        # 放在 self.lock 之外：_quote_for 在 ws 断线时会回退 REST（最长 ~10s），
        # 不应在锁内阻塞其它交易线程。
        _pos = self._resolve_position(symbol)
        _sym = ""
        if _pos is not None:
            _sym = self._pos_symbol(_pos, "")
            if not _sym:
                for _k, _v in self._positions_map().items():
                    if _v is _pos:
                        _sym = _k
                        break
        _sym = _sym or str(self.cfg.market.symbol).upper()
        # 平仓参考价：优先调用方显式传入；否则按仓位品种取符号感知实时价（ws 优先，
        # 其次该品种 REST ticker）。绝不拿当前品种 K 线末根或全局标量冒充其它品种——
        # 多仓手动平仓若误用错价，会把错误成交价套到目标仓位上、算错盈亏。
        if price is None:
            price = self._quote_for(_sym) if _sym else self.price()
            # ws/rest 都拿不到价时的兜底：仅当前品种退回 K 线末根（符号正确）；
            # 非当前品种宁可为 0 暴露错误，也不用一个错价静默算错盈亏。
            if (not price) and _sym == str(self.cfg.market.symbol).upper():
                price = self.price()
        with self.lock:
            pos = self._resolve_position(symbol)
            if not pos:
                if symbol:
                    return {"ok": False, "error": f"{symbol} 当前没有持仓"}
                return {"ok": False, "error": "当前没有持仓"}
            # 用仓位自己的品种。优先读 pos["symbol"]；老存档可能没有该字段，
            # 此时反查 map 的 key（而不是退回 cfg.market.symbol —— 切过品种后
            # 那会指向另一个品种，实盘会平错对象、模拟盘会删错 key 留幽灵仓）。
            sym = self._pos_symbol(pos, "")
            if not sym:
                for key, val in self._positions_map().items():
                    if val is pos:
                        sym = key
                        break
            sym = sym or str(self.cfg.market.symbol).upper()
            pos_side = self._pos_side(pos)
            if pos.get("mode") == "live" and self.state["mode"] == "live":
                if not self.armed or self.live_client is None:
                    return {"ok": False, "error": "实盘未解锁"}
                try:
                    self.live_client.cancel_all(sym)
                    res = self.live_client.place_market(
                        sym,
                        "SELL" if self._pos_side(pos) == "LONG" else "BUY",
                        float(pos["qty"]),
                        reduce_only=True,
                    )
                    detail = self._live_fill(sym, res.order_id)
                    exit_px = float(detail["price"]) if detail and detail.get("price") else float(res.price or price)
                    direction = 1.0 if self._pos_side(pos) == "LONG" else -1.0
                    gross = (exit_px - float(pos["entry"])) * float(pos["qty"]) * direction
                    if detail is not None:
                        gross = float(detail["pnl"])  # 交易所口径的已实现盈亏（不含手续费）
                    # 成交明细可能有索引延迟：拿不到就用下单回执里的手续费（OKX 订单详情已经带回）
                    fee = float(detail["fee"]) if detail else float(res.fee or 0.0)
                    open_fee = float(pos.get("entry_fee") or 0.0)
                    if open_fee <= 0 and float(pos.get("entry") or 0.0) > 0:
                        open_fee = (
                            float(pos["entry"]) * float(pos["qty"])
                            * float(self.cfg.backtest.costs.fee_bps or 0.0) / 10_000.0
                        )
                    # 平仓 pnl = 价格变动毛额，与平仓前浮盈同口径。开仓费开仓时已入账，
                    # 平仓费由本条 CLOSE 流水单独入账，都不从 pnl 里扣。
                    pnl = gross
                    self.state["fills"].append(
                        {
                            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "action": "CLOSE",
                            "side": self._pos_side(pos),
                            "symbol": pos.get("symbol") or self.cfg.market.symbol,
                            "qty": round(float(pos["qty"]), 6),
                            "price": round_price(exit_px),
                            "fee": round(fee, 6),
                            "fee_open": round(open_fee, 6),
                            "fee_mode": "gross",
                            "pnl": round(pnl, 4),
                            "reason": reason,
                            "order_id": res.order_id,
                            "account_scope": self._scope(),
                        }
                    )
                    self.state["fills"] = self.state["fills"][-500:]
                    # 账本只加「毛盈亏 − 平仓费」：开仓费在交易所侧已扣，本地从未记账，
                    # 再加一次会让 state.equity 与交易所余额偏离一笔开仓费。
                    self.state["equity"] += gross - fee
                    self._record_close(pos, exit_px, pnl, reason, "live", rule=rule, fee=fee)
                    self._drop_position(sym)
                    self._save()
                    self._log_live("CLOSE", self._pos_side(pos), pos["qty"], exit_px, res.order_id)
                    # A2：平仓后报告已实现盈亏。环境标注必须按 testnet 区分：
                    # mode==live 只表示「走交易所接口」，demo（testnet=True）也是 live，
                    # 不能据此标「实盘」——否则币安模拟盘的平仓通知会误标成主网实盘。
                    env_label = "模拟盘" if self.state.get("testnet", True) else "主网实盘"
                    self._notify("info" if pnl >= 0 else "critical",
                                 f"{pos['side']} 平仓（{env_label}）",
                                 f"价格盈亏 {pnl:+,.2f} U，平仓手续费 {fee:,.2f} U"
                                 f"（费用直接计入累计已实现盈亏；{reason}）",
                                 {"symbol": sym, "pnl": round(pnl, 2), "reason": reason})
                    return {"ok": True, "mode": "live", "order_id": res.order_id}
                except ExchangeError as exc:
                    return {"ok": False, "error": str(exc)}
            rec = self._paper_close(price, reason, symbol=sym, rule=rule)
            if rec is None:
                return {"ok": False, "error": f"{sym} 平仓失败：仓位记录已不存在"}
            self._save()
            # A2：平仓后报告已实现盈亏（paper = 本地模拟撮合，不进交易所）
            self._notify("info" if rec["pnl"] >= 0 else "critical",
                         f"{pos['side']} 平仓（本地模拟）",
                         f"价格盈亏 {rec['pnl']:+,.2f} U，平仓手续费 {rec['fee']:,.2f} U"
                         f"（费用直接计入累计已实现盈亏；{reason}）",
                         {"symbol": sym, "pnl": rec["pnl"], "reason": reason})
            return {"ok": True, "mode": "paper", "fill": rec}

    def check_stops(self, price: float | None = None) -> list[dict]:
        """模拟盘里逐品种检查止损/止盈。实盘由交易所的 STOP_MARKET 单负责。

        多品种时每个仓必须用自己品种的价格：拿当前品种的价去比另一个品种的止损，
        不是误触发就是永远不触发。取不到价的品种本轮跳过。
        """
        hits: list[dict] = []
        cur = str(self.cfg.market.symbol).upper()
        for sym, pos in list(self._positions_map().items()):
            if pos.get("mode") == "live":
                continue
            px = float(price) if (price and sym == cur) else self._quote_for(sym)
            if px <= 0:
                continue
            hit = None
            if self._pos_side(pos) == "LONG":
                if px <= pos["stop"]:
                    hit = "stop_loss"
                elif px >= pos["tp"]:
                    hit = "take_profit"
            else:
                if px >= pos["stop"]:
                    hit = "stop_loss"
                elif px <= pos["tp"]:
                    hit = "take_profit"
            if not hit:
                continue
            with self.lock:
                rec = self._paper_close(px, hit, symbol=sym, rule=hit)
            if rec:
                hits.append(rec)
        return hits

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
        interval_sec = max(30, int(interval_sec))
        # 参数永远先落定：线程已在跑时直接 return 会让「自动下单」勾选改了却不生效——
        # 用户看到「已启动」的提示，实际只决策不下单，平仓样本永远是 0。
        self.auto_interval = interval_sec
        self.auto_execute = bool(execute)
        if self._auto_thread and self._auto_thread.is_alive():
            self.state["auto"].update({
                "running": True,
                "interval_sec": interval_sec,
                "execute": self.auto_execute,
            })
            self._save()
            return self.state["auto"]
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
            # 跨日先把「当日权益基准」滚动到今天：daily_loss_limit_pct 与
            # 页面 day_pnl_pct 都以它为分母，不滚动就永远是启动那天的基准。
            self._roll_day()
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
            # 选品种：按机会分取前 auto_top_k 个「未持仓且达标」的品种，本轮依次评估。
            # top_k=1 时与旧行为完全一致（切到最强的一个、开一个仓就结束）。
            targets: list[str] = []
            self._rotate_cycle_count += 1
            every = max(1, int(getattr(self.cfg.market, "rotate_every_cycles", 1) or 1))
            if self.cfg.market.auto_rotate \
                    and len(self._positions_map()) < self.position_limit() \
                    and self._rotate_cycle_count % every == 0:
                try:
                    targets = self._top_symbols()
                    if not targets:
                        # 全体弱机会时的老逻辑：退而求其次切到「明显更强」的那个
                        best = self._pick_best_symbol()
                        if best:
                            targets = [best]
                except Exception as exc:  # noqa: BLE001 - 选品种失败不阻断决策
                    self.last_error = f"自主选品种失败：{exc}"

            df, src = self.refresh_data()
            # 符号感知实时价：优先 ws 推送（<1s，按当前品种取），其次 K 线末根兜底。
            # 不再用全局 live_price_provider 标量——它只反映「当前活跃品种」，
            # auto_rotate 切品种后会滞后，把别的品种价套到当前仓上（串台）。
            price = self._quote_for(self.cfg.market.symbol)
            if price <= 0 and self.df is not None:
                price = float(df["close"].iloc[-1])
            stopped = self.check_stops(price)

            plan = targets or [str(self.cfg.market.symbol).upper()]
            first_payload: dict | None = None
            first_executed: dict | None = None
            evaluated: list[str] = []
            for sym in plan:
                # 每开一仓都重新看额度：一轮内打满上限就停，不再白烧 LLM
                if len(self._positions_map()) >= self.position_limit():
                    break
                sym_u = str(sym).upper()
                if sym_u in self._positions_map():
                    continue
                if sym_u != str(self.cfg.market.symbol).upper():
                    try:
                        sw = self.switch_symbol(sym_u)
                    except Exception as exc:  # noqa: BLE001 - 单个品种切换失败不影响其它
                        self.last_error = f"切换 {sym_u} 失败：{exc}"
                        continue
                    # 符号感知实时价：switch 后按切换目标的品种取（ws 优先），
                    # 缺失时退回 switch_symbol 自己算出的 price。杜绝用「当前品种」
                    # 全局标量把价套错对象。
                    sw_price = float(sw.get("price") or 0.0)
                    lp = self._quote_for(sym_u)
                    price = lp if lp > 0 else sw_price
                    if price <= 0:
                        continue
                payload = self.run_decision(price=price)
                executed = None
                if self.auto_execute and payload.get("order"):
                    executed = self.execute(price, reason="auto")
                evaluated.append(sym_u)
                if first_payload is None:
                    first_payload, first_executed = payload, executed
                # 没开出仓就继续看下一个品种；开了仓则下一轮再评估（避免同轮重复加仓）
                if executed is None:
                    continue

            self.state["auto"]["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._save()
            hook = getattr(self, "on_cycle_hook", None)
            if callable(hook):
                try:
                    hook()
                except Exception:  # noqa: BLE001 - 钩子（自动调参）失败绝不能影响交易循环
                    pass
            return {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "price": price,
                "stopped": stopped,
                "decision": (first_payload or {}).get("risk_decision"),
                "executed": first_executed,
                "evaluated": evaluated,
            }

    # ------------------------------------------------------------------
    # 状态输出
    # ------------------------------------------------------------------
    def _daily_pnl(self, days: int = 120, scope: str | None = None) -> list[dict]:
        """按日期聚合已实现盈亏 = 平仓毛盈亏（按平仓日） − 手续费 − 资金费（按发生日）。

        与累计盈亏共用 `_fee_rows` / `_funding_rows`：口径必须一致，且 Σdaily 必须
        等于 `_pnl_summary()["realized"]`，否则会出现「累计 +120 / 日历 -30」。
        开仓当日就会有一条负的费用，这正符合「费用发生即计入已实现」。
        """
        scope = self._scope() if scope is None else scope
        agg: dict[str, dict] = {}
        for h in self._scoped_rows("position_history", scope):
            day = str(h.get("closed_at") or "")[:10]
            if len(day) != 10:
                continue
            row = agg.setdefault(day, {"date": day, "pnl": 0.0, "trades": 0, "wins": 0})
            pnl = float(h.get("pnl") or 0.0)
            row["pnl"] = round(row["pnl"] + pnl, 6)
            row["trades"] += 1
            if pnl > 0:
                row["wins"] += 1
        for f in self._fee_rows(self._scoped_rows("fills", scope)):
            fee = float(f.get("fee") or 0.0)
            day = str(f.get("ts") or "")[:10]
            if not fee or len(day) != 10:
                continue
            row = agg.setdefault(day, {"date": day, "pnl": 0.0, "trades": 0, "wins": 0})
            row["pnl"] = round(row["pnl"] - fee, 6)
        for f in self._funding_rows(self._scoped_rows("fills", scope)):
            funding = float(f.get("fee") or 0.0)   # 正=支付、负=收到
            day = str(f.get("ts") or "")[:10]
            if not funding or len(day) != 10:
                continue
            row = agg.setdefault(day, {"date": day, "pnl": 0.0, "trades": 0, "wins": 0})
            row["pnl"] = round(row["pnl"] - funding, 6)
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
            # 展示口径必须与风控一致：实盘直接读交易所权益（_live_equity 自带
            # 20 秒缓存且只读）。这里**不能**调 account_state()——它会触发账本
            # 重基（写 state），展示接口不应有交易副作用。
            live_eq = self._live_equity()
            if live_eq is not None:
                eq = live_eq
            elif price:
                eq = self._equity(price)
            else:
                eq = self.state["equity"]
            self._roll_day(eq)
            peak_eq = max(float(self.state.get("peak_equity") or 0.0), float(eq))
            day_base = float(self.state.get("day_start_equity") or eq) or float(eq)
            # 逐仓算标记价与浮盈。多品种下「当前价」只对当前品种成立，
            # 其它品种用守护线程缓存的报价；没报价的 unrealized 给 None（前端显示 —），
            # 绝不拿当前品种的价格去套。
            positions: list[dict] = []
            fee_rate = float(self.cfg.backtest.costs.fee_bps or 0.0) / 10_000.0
            for sym, raw in self._positions_map().items():
                mark = self._mark_price(sym, price)
                entry = float(raw.get("entry") or 0.0)
                qty = float(raw.get("qty") or 0.0)
                direction = 1.0 if raw.get("side") == "LONG" else -1.0
                gross = (mark - entry) * qty * direction if (mark > 0 and entry > 0) else None
                # 浮动盈亏**只反映价格变动（毛额）**：手续费不摊进来。
                # 开仓费在开仓时、平仓费在平仓时各自直接计入「累计已实现盈亏」，
                # 所以浮盈不会一开仓就是负的双边手续费，平仓 pnl 也是同一个毛额口径。
                # fees_est 只作为「这笔仓已付 / 将要付多少手续费」的参考，不参与浮盈计算。
                open_fee = float(raw.get("entry_fee") or 0.0)
                if open_fee <= 0 and entry > 0 and qty > 0:
                    open_fee = entry * qty * fee_rate
                close_fee = mark * qty * fee_rate if mark > 0 else 0.0
                fees_est = open_fee + close_fee
                positions.append({
                    **raw,
                    "symbol": sym,
                    "mark": round(mark, 8) if mark > 0 else None,
                    "mark_ts": (getattr(self, "_price_ts", None) or {}).get(sym),
                    "unrealized": round(gross, 2) if gross is not None else None,
                    "unrealized_gross": round(gross, 2) if gross is not None else None,
                    "fees_est": round(fees_est, 2) if gross is not None else None,
                    "notional": round(qty * mark, 2) if mark > 0 else None,
                })
            cur_sym = str(self.cfg.market.symbol).upper()
            pos = next((p for p in positions if p["symbol"] == cur_sym), None)
            if pos is None and positions:
                pos = positions[0]
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
                "peak_equity": round(peak_eq, 2),
                "day_pnl_pct": round((eq / day_base - 1) * 100, 3) if day_base else 0.0,
                "drawdown_pct": round((eq / max(peak_eq, 1e-9) - 1) * 100, 3),
                "position": pos,
                # 多品种：完整持仓列表（前端阶段化改造中，position 仍是「当前品种」兼容视图）
                "positions": positions,
                "position_limit": self.position_limit(),
                "fills": [
                    {**f, "symbol": f.get("symbol") or self.cfg.market.symbol}
                    for f in self._scoped_rows("fills")[-30:][::-1]
                ],
                # 持仓历史（最近 30 笔平仓，最新在前）+ 每日已实现盈亏（最近 14 天）
                # 只回当前账户的：切到 OKX 模拟盘后不能还显示币安模拟盘的成交与平仓。
                "position_history": sorted(
                    self._scoped_rows("position_history")[-200:],
                    key=lambda x: str(x.get("closed_at") or ""),
                    reverse=True,
                )[:30],
                # 累计已实现盈亏：全量聚合当前账户，不受上面 30 条窗口影响
                "pnl": self._pnl_summary(),
                # 跨账户汇总：页面「账户对比」与 LLM 多账户分析都读这一份
                "accounts": self._accounts_summary(),
                "daily": self._daily_pnl(scope=self._scope()),
                "consecutive_losses": self.state["consecutive_losses"],
                # 统一绩效口径（60s 缓存）：复盘归因 + 按规则/品种/方向分组 KPI
                "performance": self.performance_snapshot(),
                "last_decision": self.state.get("last_decision"),
                "auto": {
                    **self.state.get("auto", {}),
                    # 每轮评估几个品种：调大即提频，页面上要能看到当前值
                    "top_k": int(getattr(self.cfg.market, "auto_top_k", 1) or 1),
                },
                # 全品种缓存预热状态：高频档一轮切多品种时，靠它确认「切换只读本地」
                "cache_warm": {
                    "enabled": bool(getattr(self.cfg.market, "cache_all_symbols", True)),
                    "symbols": len(getattr(self.cfg.market, "symbols", None) or []),
                    "interval_sec": int(getattr(self, "_warm_interval", 0) or 0),
                    "last_at": float(getattr(self, "_warm_last", 0.0) or 0.0),
                    "last_result": dict(getattr(self, "_warm_last_result", {}) or {}),
                },
                "halt": bool(self.state.get("halt")),
                "data_source": self.data_source,
                "data_loading": bool(self.data_loading),
                # 进程运行时长：前端在侧栏常驻显示，用于判断是否刚重启过
                "uptime_sec": self.uptime_seconds(),
                "started_at": time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(float(getattr(self, "started_at", time.time()))),
                ),
                "llm": {"provider": self.cfg.llm.provider, "model": self.cfg.llm.model},
                # 分析师权重校准状态（样本不足时 calibrated=False，权重仍是默认值）
                "analyst_calibration": calibration_summary(self.state.get("analyst_accuracy")),
                # 持仓管理员最近一次判断 + 守护线程状态（手动模式下也能看到退出决策）
                # position_management 只给「当前仍有持仓的品种」里最新的一轮，全量在
                # position_managements（含已平仓品种的历史记录，供回看）
                "position_management": self._latest_management(cur_sym),
                "position_managements": dict(self.last_management_by_symbol),
                # 最近一次「有动作」（含 LLM 解释）单独保留，不会被下一轮 hold 冲掉
                "position_management_last": dict(self.last_management_action),
                # 持仓管理员 LLM 的用量与影子对照（只读审计，不参与决策）
                "position_llm_usage": dict(self.position_llm_usage),
                "position_llm_shadow": list(self.llm_shadow_log[-10:]),
                # 持仓管理员自迭代：样本数、捕获率、当前生效参数与最近改动
                "position_tuning": self.position_tuning_status(),
                "position_guard": {
                    "running": bool(getattr(self, "_guard_thread", None)
                                    and self._guard_thread.is_alive()),
                    "interval_sec": int(getattr(self, "_guard_interval", 30)),
                },
                # 报价线程：标记价的新鲜度靠它，不是靠上面 30 秒一轮的管理轮询
                "quote_guard": {
                    "running": bool(getattr(self, "_quote_thread", None)
                                    and self._quote_thread.is_alive()),
                    "interval_sec": int(getattr(self, "_quote_interval", 4)),
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