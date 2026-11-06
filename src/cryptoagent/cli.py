"""命令行入口：

    python -m cryptoagent demo                 # 端到端：研究 → 回测 → 多智能体决策
    python -m cryptoagent research -n 8        # 只跑因子研究循环
    python -m cryptoagent backtest --factor momentum_24
    python -m cryptoagent agents               # 只跑一次决策
    python -m cryptoagent download             # 下载币安行情到缓存
    python -m cryptoagent factors              # 列出内置因子
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

from .agents.llm import LLMClient
from .agents.pipeline import TradingPipeline, render_markdown
from .backtest import buy_and_hold, factor_to_target, run_backtest
from .config import DEFAULT_CONFIG_PATH, AppConfig, load_config
from .data import load_market
from .factors.library import LIBRARY, get_factor
from .research import ResearchLoop, build_proposer


def _ensure_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def _load(args) -> tuple[AppConfig, pd.DataFrame, str]:
    cfg = load_config(args.config or DEFAULT_CONFIG_PATH)
    if getattr(args, "source", None):
        cfg.market.source = args.source
    if getattr(args, "bars", None):
        cfg.market.bars = args.bars
    if getattr(args, "symbol", None):
        cfg.market.symbol = args.symbol
    cfg.llm.provider = getattr(args, "llm", None) or cfg.llm.provider
    df, src = load_market(cfg.market, force_download=getattr(args, "refresh", False))
    return cfg, df, src


def _header(title: str) -> None:
    print("\n" + "=" * 66)
    print(f"  {title}")
    print("=" * 66)


# --------------------------------------------------------------------------
def cmd_demo(args) -> int:
    cfg, df, src = _load(args)
    llm = LLMClient(cfg.llm)
    mode = "规则引擎兜底（离线，不调用模型）" if llm.provider == "mock" else f"真实模型 {cfg.llm.model}"
    _header(f"QeeLin-Agent demo  |  {cfg.market.symbol} {cfg.market.interval}  |  数据源 {src}")
    print(f"智能体模式：{mode}")
    print(f"K 线数量：{len(df)}  区间：{df.index[0]} → {df.index[-1]}")
    print(f"最新价：{df['close'].iloc[-1]:,.1f}")

    _header("阶段 1/3：Agent 自动写因子并回测迭代")
    loop = ResearchLoop(cfg, df, proposer=build_proposer(cfg, llm))
    report = loop.run(args.iterations)
    best = report.get("best")

    _header("阶段 2/3：冠军因子全样本回测（含训练集，仅用于展示；真实评估看上面的留出集）")
    if best:
        code = best["proposal"]["code"]
        name = best["proposal"]["name"]
    else:
        print("本轮没有因子通过验证集门槛，回退到内置 momentum_24。")
        bf = get_factor("momentum_24")
        code, name = bf.code, bf.name
    from .factors.sandbox import run_factor

    values, _ = run_factor(code, df)
    target = factor_to_target(values, window=200, cap=1.0)
    res = run_backtest(df, target, cfg.backtest, cfg.market.interval)
    bh = buy_and_hold(df, cfg.backtest, cfg.market.interval)
    print(f"因子：{name}")
    print(res.summary())
    print(f"\n基准（买入持有）：收益 {bh.metrics['total_return']:+.1%}  "
          f"夏普 {bh.metrics['sharpe']:.2f}  回撤 {bh.metrics['max_drawdown']:.1%}")

    _header("阶段 3/3：多智能体决策流水线")
    pipe = TradingPipeline(cfg, llm=llm)
    decision = pipe.run(df)
    md = render_markdown(decision)
    print(md)

    out = Path(cfg.runs_dir) / "demo_report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"\n报告已保存：{out.resolve()}")
    print(f"因子排行榜：{(Path(cfg.research.out_dir) / 'report.md').resolve()}")
    return 0


def cmd_research(args) -> int:
    cfg, df, src = _load(args)
    _header(f"因子研究循环 | {cfg.market.symbol} | {src}")
    llm = LLMClient(cfg.llm)
    loop = ResearchLoop(cfg, df, proposer=build_proposer(cfg, llm))
    report = loop.run(args.iterations)
    print(f"\n通过 {report['accepted']} / {report['iterations']}")
    print(f"榜单：{(Path(cfg.research.out_dir) / 'report.md').resolve()}")
    return 0


def cmd_backtest(args) -> int:
    cfg, df, src = _load(args)
    _header(f"回测 | {cfg.market.symbol} | {src}")
    if args.code:
        code = Path(args.code).read_text(encoding="utf-8")
        name = Path(args.code).stem
    else:
        bf = get_factor(args.factor)
        code, name = bf.code, bf.name
    from .factors.sandbox import check_lookahead, run_factor

    values, meta = run_factor(code, df)
    problems = check_lookahead(meta)
    if problems:
        print(f"⚠ 前视偏差：{problems[0]}")
    target = factor_to_target(values, window=args.window, cap=1.0)
    res = run_backtest(df, target, cfg.backtest, cfg.market.interval)
    print(f"因子：{name}\n")
    print(res.summary())
    bh = buy_and_hold(df, cfg.backtest, cfg.market.interval)
    print(f"\n买入持有：收益 {bh.metrics['total_return']:+.1%}  夏普 {bh.metrics['sharpe']:.2f}")
    return 0


def cmd_agents(args) -> int:
    cfg, df, src = _load(args)
    _header(f"多智能体决策 | {cfg.market.symbol} | LLM={cfg.llm.provider}")
    decision = TradingPipeline(cfg).run(df, debate_rounds=args.rounds)
    md = render_markdown(decision)
    print(md)
    out = Path(cfg.runs_dir) / "decisions" / "latest.md"
    print(f"\n已保存：{out.resolve()}")
    return 0


def cmd_web(args) -> int:
    cfg = load_config(args.config or DEFAULT_CONFIG_PATH)
    if getattr(args, "source", None):
        cfg.market.source = args.source
    if getattr(args, "symbol", None):
        cfg.market.symbol = args.symbol
    if getattr(args, "bars", None):
        cfg.market.bars = args.bars
    if getattr(args, "llm", None):
        cfg.llm.provider = args.llm
    from .web import serve

    serve(cfg, host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_download(args) -> int:
    cfg = load_config(args.config or DEFAULT_CONFIG_PATH)
    cfg.market.source = "binance"
    if args.symbol:
        cfg.market.symbol = args.symbol
    if args.bars:
        cfg.market.bars = args.bars
    _header(f"下载 {cfg.market.symbol} {cfg.market.interval} 到本地缓存")
    from .data import DataError

    try:
        df, src = load_market(cfg.market, force_download=True, strict=True)
    except DataError as exc:
        print(f"下载失败：{exc}")
        print("提示：本项目不会在你不知情时把 synthetic 数据当成真实行情。")
        return 2
    print(f"来源：{src}  行数：{len(df)}  区间：{df.index[0]} → {df.index[-1]}")
    return 0


_HINTS = {
    "-2014": "Key 格式不对：多是少复制、前后有空格、或者拿的是别的环境的 Key（主网 Key 不能用于测试网，反之亦然）",
    "-2015": "Key 无效/权限不对/IP 不在白名单：检查是否只勾了「允许合约交易」、是否绑定了这台服务器的 IP",
    "-1021": "本机时间偏差太大：跑 w32tm /resync（Windows）或 chronyc makestep（Linux）",
    "-1109": "账号不在这个环境里：Demo Trading 生成的 Key 拿到老测试网用会报这个",
    "-1003": "IP 被限流/封禁（418）。老测试网走 CDN，共享出口 IP 常被别人刷爆；换 demo 环境或过几小时再试",
    "-1001": "交易所内部错误，稍后重试",
    "-4164": "下单名义价值低于交易所最小值（BTCUSDT 一般 50~100 USDT）",
    "451": "地区限制：这个机房的 IP 被交易所挡了（换地区或走代理）",
    "403": "被 WAF 拦了：多半是 IP 段问题，或请求头异常",
    "401": "签名/Key 没通过校验",
}


def _explain(err: str) -> str:
    for code, hint in _HINTS.items():
        if code in err:
            return hint
    if "timed out" in err or "URLError" in err or "getaddrinfo" in err:
        return "网络不通：这台机器连不上这个域名（DNS 或出口被限），先跑 netcheck 确认"
    return ""


def cmd_exchtest(args) -> int:
    """交易所连通性体检：用 .env 里的 Key 分别打 testnet / demo / live，把错误码翻译成人话。"""
    import os
    import time as _t

    from .config import load_dotenv
    from .execution import BinanceFuturesClient

    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env")

    _header("交易所体检")
    key = os.getenv("BINANCE_API_KEY", "").strip()
    secret = os.getenv("BINANCE_API_SECRET", "").strip()
    if not key or not secret:
        print("  没读到 Key。检查顺序：")
        print(f"    1) {root / '.env'} 里有没有 BINANCE_API_KEY / BINANCE_API_SECRET 两行")
        print("    2) 改完 .env 后**必须重启服务**（服务只在启动时读一次）")
        print("    3) Windows 计划任务跑的服务：Stop-ScheduledTask/Start-ScheduledTask crypto-agent")
        return 3
    env_file = root / ".env"
    in_file = env_file.exists() and "BINANCE_API_KEY" in env_file.read_text(encoding="utf-8", errors="ignore")
    print(f"  Key：{key[:6]}…{key[-4:]}（长度 {len(key)}）")
    print(f"  来源：{env_file if in_file else '系统环境变量（不在 .env 里）'}")

    sym = args.symbol
    for env in args.envs:
        print(f"\n  ── {env} ──")
        try:
            t0 = _t.time()
            c = BinanceFuturesClient(key, secret, env=env, timeout=12)
            c.ping()
            print(f"    ping        OK   {int((_t.time() - t0) * 1000)}ms  {c.base}")
        except Exception as exc:  # noqa: BLE001
            print(f"    ping        FAIL {type(exc).__name__}: {str(exc)[:120]}")
            hint = _explain(str(exc))
            if hint:
                print(f"                → {hint}")
            continue

        # 时钟偏差
        try:
            st = c.server_time()
            skew = st / 1000 - _t.time()
            flag = "OK" if abs(skew) < 5 else "偏差大（先校时）"
            print(f"    时钟        {flag}  服务器-本机 = {skew:+.1f}s")
        except Exception as exc:  # noqa: BLE001
            print(f"    时钟        FAIL {str(exc)[:80]}")

        # 签名接口（真正验证 Key）
        try:
            acct = c.account()
            print(f"    签名/余额   OK   余额 {acct['balance']:,.2f} USDT（可用 {acct['available']:,.2f}）")
        except Exception as exc:  # noqa: BLE001
            print(f"    签名/余额   FAIL {str(exc)[:160]}")
            hint = _explain(str(exc))
            if hint:
                print(f"                → {hint}")
            continue

        try:
            pos = c.positions(sym)
            print(f"    持仓        OK   {sym} " + ("无" if not pos else str(pos)))
            tick, step, min_notional = c.filters(sym)
            print(f"    精度        tick {tick} / step {step} / 最小名义 {min_notional} USDT")
        except Exception as exc:  # noqa: BLE001
            print(f"    持仓/精度   FAIL {str(exc)[:120]}")

    print("\n  说明：ping 通但签名失败 = Key 问题；签名通但下单报 -4164 = 数量太小；")
    print("        ping 就失败 = 网络/地区问题，跟 Key 无关。")
    return 0


def cmd_token(args) -> int:
    """打印带访问令牌的完整地址（令牌在 .env 里，忘了就用这个）。"""
    import os

    from .config import load_dotenv

    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env")
    token = os.getenv("CRYPTOAGENT_WEB_TOKEN", "").strip()

    _header("访问地址")
    if not token:
        print("  没有设置 CRYPTOAGENT_WEB_TOKEN —— 说明这台机器的控制台不需要令牌。")
        print(f"  直接打开：http://127.0.0.1:{args.port}/")
        print("\n  想加一道锁的话，在 .env 里加一行：")
        print("    CRYPTOAGENT_WEB_TOKEN=随便一串长随机字符")
        return 0

    env_file = root / ".env"
    print(f"  令牌：{token}")
    print(f"  来源：{env_file if env_file.exists() else '系统环境变量'}")
    print(f"\n  本机直接打开：")
    print(f"    http://127.0.0.1:{args.port}/?token={token}")
    print(f"\n  从别的电脑（先开 SSH 隧道 ssh -N -L {args.port}:127.0.0.1:{args.port} user@server）：")
    print(f"    http://127.0.0.1:{args.port}/?token={token}")
    print("\n  打开一次之后浏览器会记 30 天，不用每次都带。")
    return 0


def cmd_llmtest(args) -> int:
    """接真模型前的体检：一次文本、一次 JSON、一次完整决策，报延迟和 token。"""
    import os
    import time as _t

    from .agents.llm import LLMClient
    from .agents.pipeline import TradingPipeline
    from .config import AppConfig
    from .data import load_market

    _header("LLM 体检")
    cfg: AppConfig = load_config(args.config or DEFAULT_CONFIG_PATH)
    if getattr(args, "provider", None):
        cfg.llm.provider = args.provider
    if getattr(args, "model", None):
        cfg.llm.model = args.model
    if getattr(args, "base_url", None):
        cfg.llm.base_url = args.base_url

    key = os.getenv(cfg.llm.api_key_env, "")
    print(f"  provider : {cfg.llm.provider}")
    print(f"  model    : {cfg.llm.model}")
    print(f"  base_url : {cfg.llm.base_url}")
    print(f"  api key  : {cfg.llm.api_key_env} = {'已设置（' + key[-4:] + '）' if key else '未设置'}")

    if cfg.llm.provider == "mock":
        print("当前是 mock 模式（内置规则引擎，不调用模型）。要接真模型：")
        print("  .env 里写：")
        print("    CRYPTOAGENT_LLM_PROVIDER=openai")
        print("    CRYPTOAGENT_LLM_BASE_URL=https://api.deepseek.com/v1")
        print("    CRYPTOAGENT_LLM_MODEL=deepseek-chat")
        print("    OPENAI_API_KEY=sk-xxx")
        print("  然后重跑：run.cmd llmtest")
        return 3

    llm = LLMClient(cfg.llm)
    if llm.provider == "mock":
        print(f"[x] Key 没读到（{llm._degrade_reason}），已自动降级成 mock。")
        return 3

    # 1) 文本调用
    t0 = _t.time()
    r1 = llm.text("你是一个简洁的助手。", "用一句话说明你在测试。", "mock")
    d1 = (_t.time() - t0) * 1000
    print(f"  [1/3] 文本调用   {'OK' if not r1.degraded else '降级'}  {d1:.0f}ms")
    if r1.degraded:
        print(f"        失败原因：{r1.reason[:160]}")
        return 2
    print(f"        返回：{r1.content[:80]}")

    # 2) JSON 调用
    t0 = _t.time()
    data, r2 = llm.json(
        "你是交易员，只输出 JSON。",
        "BTC 现价 78000，24h 涨 0.8%，波动率分位 30%。给个方向。",
        '- "action": "long|short|flat"\n- "size_pct": 0~0.3\n- "rationale": "一句话"',
        {"action": "flat", "size_pct": 0.0, "rationale": "mock"},
    )
    d2 = (_t.time() - t0) * 1000
    print(f"  [2/3] JSON 调用   {'OK' if not r2.degraded else '降级'}  {d2:.0f}ms  -> {data}")
    if r2.degraded:
        print(f"        失败原因：{r2.reason[:200]}")
        return 2

    # 3) 完整决策（这一轮才是真实成本）
    std = load_market(cfg.market)
    df, src = std
    print(f"  [3/3] 完整决策   行情 {cfg.market.symbol} {len(df)} 根（{src.split(':')[0]}）…")
    t0 = _t.time()
    pipe = TradingPipeline(cfg)
    decision = pipe.run(df, debate_rounds=args.rounds, persist=False)
    d3 = _t.time() - t0
    u = pipe.llm.usage
    print(f"        耗时 {d3:.1f}s ｜ 调用 {u['calls']} 次 ｜ 输入 {u['prompt_tokens']} tok ｜ 输出 {u['completion_tokens']} tok")
    print(f"        结论 {decision.risk_decision['action']} ｜ 降级 {'是' if decision.degraded else '否'}")

    per_day = 86400 / max(30, args.interval)
    print(f"\n  按每 {args.interval}s 跑一轮：{per_day:.0f} 轮/天 ≈ "
          f"{u['calls'] * per_day:,.0f} 次调用，"
          f"{(u['prompt_tokens'] + u['completion_tokens']) * per_day / 1e6:.2f} M tok/天")
    print("  单价按你的网关实际计价算；DeepSeek 这类便宜模型通常一天不到 1 元。")
    return 0


def cmd_netcheck(args) -> int:
    """体检：哪些币安域名当前网络能通，决定能不能用真钱/假钱下单。"""
    import os
    import socket
    import ssl
    import urllib.error
    import urllib.request

    _header("网络体检")
    proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or ""
    print(f"代理(HTTPS_PROXY)：{proxy or '未设置'}")

    targets = [
        ("行情-币安镜像", "https://data-api.binance.vision/api/v3/ping"),
        ("行情-主网 fapi", "https://fapi.binance.com/fapi/v1/ping"),
        ("下单-测试网(老)", "https://testnet.binancefuture.com/fapi/v1/ping"),
        ("下单-Demo Trading", "https://demo-fapi.binance.com/fapi/v1/ping"),
        ("网页-Demo 注册", "https://demo.binance.com/en/futures/BTCUSDT"),
        ("备用-Gate 永续", "https://api.gateio.ws/api/v4/futures/usdt/contracts/BTC_USDT"),
    ]
    ok_count = 0
    for name, url in targets:
        t0 = __import__("time").time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "crypto-agent/netcheck"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                code = resp.status
            ms = int((__import__("time").time() - t0) * 1000)
            ok_count += 1
            print(f"  [OK]   {name:<18} {code}  {ms}ms")
        except urllib.error.HTTPError as exc:
            print(f"  [HTTP] {name:<18} {exc.code}（能连上，返回了错误码）")
            ok_count += 1
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            print(f"  [FAIL] {name:<18} {type(exc).__name__}: {str(reason)[:60]}")

    print()
    if ok_count == 0:
        print("结论：全都不通，这台机器现在没有外网或全被墙。")
    else:
        print("结论：能通就能下单；「下单-测试网(老)」或「下单-Demo Trading」任一为 OK，")
        print("      配上对应 API Key 就能跑假钱实盘；「行情-币安镜像」OK 就够看真实行情。")

    # ---- 实时报价（页面顶栏那个绿点靠它）----
    from .data import LIVE_SOURCES, fetch_live_price

    print("\n实时报价源（每 5 秒拉一次的那个）：")
    for name, fn, _host in LIVE_SOURCES:
        t0 = time.time()
        try:
            price, _src = fn(args.symbol, 6)
            print(f"  [OK]   {name:<18} {price:>12,.2f}  {int((time.time() - t0) * 1000)}ms")
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {name:<18} {type(exc).__name__}: {str(exc)[:60]}")

    try:
        price, src = fetch_live_price(args.symbol, timeout=6)
        print(f"\n  实时价可用：{args.symbol} = {price:,.2f}（来源 {src}）")
    except Exception as exc:  # noqa: BLE001
        print("\n  [x] 实时价全挂，页面会显示「实时报价不可用」，价格改用 K 线收盘价。")
        print(f"      {str(exc)[:400]}")
        print("      常见原因：机房出口被限（451/403）、DNS 污染、或临时网络故障。")
    return 0


def cmd_factors(args) -> int:
    _header("内置因子库")
    for name, f in LIBRARY.items():
        print(f"- {name:<18} [{f.family}] {f.hypothesis}")
    return 0


def cmd_accounts(args) -> int:
    """按账户查看权益 / 盈亏 / 流水，支持跨账户汇总与明细查询。

    `--json` 输出结构化结果，供 LLM / 脚本直接消费：
      accounts                         → 每个账户摘要 + 合计 + 按日合并
      accounts --scope okx:demo        → 单个账户
      accounts --kind fills --since 2026-11-01 --json
    """
    from .accounts import AccountStore

    cfg = load_config(args.config or DEFAULT_CONFIG_PATH)
    store = AccountStore(Path(cfg.runs_dir) / "accounts")
    detail = bool(args.scope or args.since or args.until or args.symbol)
    if detail:
        rows = store.query(
            scope=args.scope or None,
            kind=args.kind,
            since=args.since,
            until=args.until,
            symbol=args.symbol,
            limit=args.limit,
        )
        if args.json:
            print(json.dumps({"count": len(rows), "rows": rows}, ensure_ascii=False, indent=2))
            return 0
        _header(f"{args.kind}（{args.scope or '全部账户'}，{len(rows)} 条）")
        for r in rows:
            stamp = r.get("closed_at") or r.get("ts") or ""
            pnl = r.get("pnl")
            pnl_txt = f"{pnl:+.2f}" if isinstance(pnl, (int, float)) else "   —"
            print(f"{stamp}  {r.get('account_scope',''):<14} {r.get('symbol',''):<10} {pnl_txt}")
        return 0

    snap = store.summarize_all()
    if args.json:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        return 0
    _header("按账户汇总")
    for a in snap["accounts"]:
        print(
            f"{a['scope']:<14} 权益 {a['equity']:>10.2f}  峰值 {a['peak_equity']:>10.2f}  "
            f"回撤 {a['drawdown_pct']:>7.2f}%  已实现 {a['realized']:>+9.2f}  "
            f"笔数 {a['trades']:>3}  胜率 {a['win_rate'] if a['win_rate'] is not None else '—'}"
        )
    c = snap["combined"]
    print(f"\n合计：已实现 {c['realized']:+.2f} · {c['trades']} 笔 · 胜率 {c['win_rate'] or '—'}")
    for ex, v in (c.get("by_exchange") or {}).items():
        print(f"  {ex:<10} 已实现 {v['realized']:+.2f} · {v['trades']} 笔")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cryptoagent", description="币圈合约多智能体交易与研究框架")
    p.add_argument("--config", help="配置文件路径（默认 config/default.json）")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--source", choices=["synthetic", "binance"], help="数据源")
        sp.add_argument("--bars", type=int, help="K 线数量")
        sp.add_argument("--symbol", help="交易对，如 BTCUSDT")
        sp.add_argument("--llm", choices=["mock", "openai"], help="LLM 提供方")
        sp.add_argument("--refresh", action="store_true", help="忽略缓存重新下载")

    d = sub.add_parser("demo", help="端到端演示：研究 → 回测 → 决策")
    common(d)
    d.add_argument("-n", "--iterations", type=int, default=4)
    d.set_defaults(func=cmd_demo)

    r = sub.add_parser("research", help="因子研究循环")
    common(r)
    r.add_argument("-n", "--iterations", type=int, default=8)
    r.set_defaults(func=cmd_research)

    b = sub.add_parser("backtest", help="回测单个因子")
    common(b)
    b.add_argument("--factor", default="momentum_24", help="内置因子名")
    b.add_argument("--code", help="自定义因子文件路径")
    b.add_argument("--window", type=int, default=200)
    b.set_defaults(func=cmd_backtest)

    a = sub.add_parser("agents", help="跑一次多智能体决策")
    common(a)
    a.add_argument("--rounds", type=int, default=2)
    a.set_defaults(func=cmd_agents)

    w = sub.add_parser("web", help="启动本地 Web 控制台")
    common(w)
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)
    w.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    w.set_defaults(func=cmd_web)

    dl = sub.add_parser("download", help="下载币安行情")
    dl.add_argument("--symbol")
    dl.add_argument("--bars", type=int)
    dl.set_defaults(func=cmd_download)

    ex = sub.add_parser("exchtest", help="体检：用 .env 里的 Key 试连交易所，翻译错误码")
    ex.add_argument("--symbol", default="BTCUSDT")
    ex.add_argument("--envs", default=["demo", "testnet"], nargs="+", choices=["testnet", "demo", "live"])
    ex.set_defaults(func=cmd_exchtest)

    tk = sub.add_parser("token", help="打印带访问令牌的控制台地址")
    tk.add_argument("--port", type=int, default=8000)
    tk.set_defaults(func=cmd_token)

    lt = sub.add_parser("llmtest", help="体检：真模型能不能用、多快、多少 token")
    lt.add_argument("--provider", choices=["mock", "openai"])
    lt.add_argument("--model")
    lt.add_argument("--base-url", dest="base_url")
    lt.add_argument("--rounds", type=int, default=2)
    lt.add_argument("--interval", type=int, default=300, help="自动循环间隔秒数，用于估日成本")
    lt.set_defaults(func=cmd_llmtest)

    nc = sub.add_parser("netcheck", help="体检：行情/下单/实时价各域名当前能不能连")
    nc.add_argument("--symbol", default="BTCUSDT", help="测实时价用的品种")
    nc.set_defaults(func=cmd_netcheck)

    f = sub.add_parser("factors", help="列出内置因子")
    f.set_defaults(func=cmd_factors)

    ac = sub.add_parser("accounts", help="按账户查看权益/盈亏/流水（支持跨账户汇总）")
    ac.add_argument("--scope", default="", help="账户作用域，如 okx:demo；留空看全部")
    ac.add_argument("--kind", default="position_history",
                    choices=["position_history", "fills"], help="查平仓历史还是成交流水")
    ac.add_argument("--since", default="", help="起始时间（YYYY-MM-DD 或完整时间）")
    ac.add_argument("--until", default="", help="结束时间")
    ac.add_argument("--symbol", default="", help="只看某个品种，如 BTCUSDT")
    ac.add_argument("--limit", type=int, default=50, help="明细条数上限")
    ac.add_argument("--json", action="store_true", help="输出 JSON（给 LLM / 脚本用）")
    ac.set_defaults(func=cmd_accounts)
    return p


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130