/* QeeLin-Agent 控制台前端：零依赖，纯 fetch + canvas */
const $ = (id) => document.getElementById(id);
const fmt = (n, d = 2) => (n === null || n === undefined || Number.isNaN(n)) ? '--' : Number(n).toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d });
// 价格按量级取小数位（DOGE 0.07 这种不能只留两位）
const fmtPx = (n) => {
  if (n === null || n === undefined || Number.isNaN(n)) return '--';
  const v = Math.abs(Number(n));
  const d = v >= 1000 ? 2 : v >= 100 ? 3 : v >= 1 ? 4 : v >= 0.1 ? 5 : v >= 0.01 ? 6 : 8;
  return fmt(n, d);
};
// 数量：大数保留 2 位，小数最多 6 位
const fmtQty = (n) => {
  if (n === null || n === undefined || Number.isNaN(n)) return '--';
  const v = Math.abs(Number(n));
  return fmt(n, v >= 1000 ? 2 : v >= 1 ? 4 : 6);
};
const pct = (n) => (n === null || n === undefined) ? '--' : `${n >= 0 ? '+' : ''}${Number(n).toFixed(2)}%`;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
// 持仓方向中文：多 / 空（保留英文便于对照）
const sideCn = (s) => (s === 'LONG' ? '多' : s === 'SHORT' ? '空' : (s || ''));
// 成交动作中文：开仓 / 平仓（交易所原始成交已带 买入/卖出 则原样保留）
const actionCn = (a) => (a === 'OPEN' ? '开仓' : a === 'CLOSE' ? '平仓' : (a || ''));
// 数据源标签：把 `binance-vision:BTCUSDT 1h（无资金费率）` 压成 `币安镜像 · 1h`
const shortSource = (src) => {
  if (!src) return '--';
  const s = String(src);
  const [kindRaw, rest = ''] = s.split(/:(.+)/);
  const kind = kindRaw.trim();
  const pair = /([A-Z]+)USDT\s+(\w+)/.exec(rest);
  const sym = pair ? `${pair[1]} ${pair[2]}` : '';
  if (kind.startsWith('synthetic')) return '合成假数据';
  if (kind.startsWith('stale-cache')) return `旧缓存 ${sym}`.trim();
  if (kind === 'cache') return `本地缓存 ${sym}`.trim();
  if (kind === 'okx-cache') return `OKX缓存 ${sym}`.trim();
  const label = { 'binance-vision': '币安镜像', 'binance-spot': '币安现货', 'binance-fapi': '币安永续', 'binance-fapi-demo': '币安合约(模拟)', 'gate-futures': 'Gate 永续', 'okx-swap': 'OKX永续' }[kind] || kind;
  return `${label} ${sym}`.trim();
};

let lastStatus = null;
let latestDecision = null;
let testnetAutoShown = false;
let liveWarned = false;

async function api(path, method = 'GET', body = null) {
  const opt = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opt.body = JSON.stringify(body);
  const res = await fetch(path, opt);
  const json = await res.json().catch(() => ({ ok: false, error: '响应不是 JSON' }));
  if (!res.ok || json.ok === false) throw new Error(json.error || `HTTP ${res.status}`);
  return json.data;
}

let toastTimer = null;
function toast(msg, type = '') {
  const el = $('toast');
  el.textContent = msg;
  el.className = `toast show ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, 4200);
}

/* ---------------- 标签页 ---------------- */
function showTab(name, push = true) {
  const btn = document.querySelector(`.tab[data-tab="${name}"]`);
  if (!btn) return;
  document.querySelectorAll('.tab').forEach((b) => b.classList.remove('active'));
  document.querySelectorAll('.panel').forEach((p) => p.classList.remove('active'));
  btn.classList.add('active');
  $(`tab-${name}`).classList.add('active');
  if (push && location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);
  if (name === 'overview') renderPnl();
  if (name === 'settings') loadLlmSettings();
}

document.querySelectorAll('.tab').forEach((btn) => {
  btn.addEventListener('click', () => showTab(btn.dataset.tab));
});

/* ---------------- 状态轮询 ---------------- */
async function poll() {
  try {
    const s = await api('/api/status');
    lastStatus = s;
    renderStatus(s);
  } catch (e) {
    console.error(e);
  }
}

/* ---------------- LLM 设置 ---------------- */
async function loadLlmSettings() {
  try {
    const d = await api('/api/settings/llm');
    $('llmProvider').value = d.provider || 'mock';
    $('llmModel').value = d.model || '';
    $('llmBaseUrl').value = d.base_url || '';
    $('llmApiKey').value = '';
    $('llmApiKey').placeholder = d.api_key_set
      ? `已设置（尾号 ${d.api_key_tail}），留空保持原值`
      : 'sk-...';
    $('llmStatus').textContent = `当前 ${d.provider} · ${d.model}${d.api_key_set ? '（Key 已配置）' : '（未配 Key）'}`;
  } catch (e) { console.error(e); }
}

async function saveLlmSettings() {
  const payload = {
    provider: $('llmProvider').value,
    model: $('llmModel').value.trim(),
    base_url: $('llmBaseUrl').value.trim(),
  };
  const key = $('llmApiKey').value.trim();
  if (key) payload.api_key = key;
  try {
    await api('/api/settings/llm', 'POST', payload);
    toast('LLM 设置已保存', 'ok');
    $('llmApiKey').value = '';
    loadLlmSettings();
    poll();
  } catch (e) { toast(e.message, 'err'); }
}

async function testLlmSettings() {
  await saveLlmSettings();
  toast('正在跑一次决策验证 LLM…', 'ok');
  try {
    const d = await api('/api/decision', 'POST', { rounds: 1 });
    const degraded = d && d.degraded;
    toast(degraded ? 'LLM 调用有降级（限流/失败），规则已兜底' : 'LLM 正常生效', degraded ? 'err' : 'ok');
  } catch (e) { toast('测试失败：' + e.message, 'err'); }
}

/* ---------------- 多品种 ---------------- */
let symbolList = [];

async function loadSymbols() {
  try {
    const list = await api('/api/symbols');
    symbolList = list;
    const sel = $('symbolSelect');
    sel.innerHTML = list.map((x) => {
      const mark = x.cached ? '' : (x.synthetic_only ? '（合成）' : '（未下载）');
      return `<option value="${esc(x.symbol)}">${esc(x.symbol.replace('USDT', '/USDT'))}${mark}</option>`;
    }).join('');
    const active = list.find((x) => x.active);
    if (active) sel.value = active.symbol;
    sel.title = list.map((x) => `${x.symbol}${x.cached ? ' 缓存到 ' + x.last : ' 未下载'}`).join(' ｜ ');
  } catch (e) { console.error(e); }
}

function syncSymbolSelect(symbol) {
  const sel = $('symbolSelect');
  if (!sel || !symbol) return;
  if (!symbolList.some((x) => x.symbol === symbol)) {
    sel.insertAdjacentHTML('beforeend', `<option value="${esc(symbol)}">${esc(symbol)}</option>`);
  }
  if (sel.value !== symbol) sel.value = symbol;
}

$('symbolSelect').addEventListener('change', async () => {
  const sym = $('symbolSelect').value;
  const sel = $('symbolSelect');
  sel.disabled = true;
  try {
    const d = await api('/api/symbol', 'POST', { symbol: sym });
    toast(d.changed ? `已切到 ${sym}（${fmt(d.price)}）` : `${sym} 已是当前品种`, 'ok');
    await loadSymbols();
    await poll();
  } catch (e) {
    toast(`切换失败：${e.message}`, 'err');
    poll();
  } finally {
    sel.disabled = false;
  }
});

function factorSigHtml(p) {
  const c = p.factor_composite;
  if (c === undefined || c === null) return '';
  const active = (p.factor_signals || []).filter((s) => s.weight > 0);
  const names = active.map((s) => `${s.name}(${s.target >= 0 ? '+' : ''}${s.target.toFixed(2)})`).join('、') || '无（全部被留出集淘汰）';
  const cls = c > 0.05 ? 'up' : c < -0.05 ? 'down' : 'muted';
  const dir = c > 0.05 ? '偏多' : c < -0.05 ? '偏空' : '中性';
  return `<div class="thesis">因子信号 <b class="${cls}">${c >= 0 ? '+' : ''}${Number(c).toFixed(2)}</b>（${dir}）
    · ${esc(names)}。留出集为负的因子已自动淘汰，不参与决策。</div>`;
}

function renderResearchAuto(a) {
  const chk = $('autoResearchChk');
  const el = $('autoResearchState');
  if (!chk || !el || !a) return;
  chk.checked = !!a.enabled;
  if (a.enabled) {
    el.textContent = a.status === 'running'
      ? '正在研究…'
      : (a.status === 'ok' ? `已开启 · 每 ${a.interval_sec}s · 上次 ${a.last_result || '--'}` : `已开启 · ${a.status || ''}`);
  } else {
    el.textContent = '已关闭。开启后定时重跑研究、更新因子库，决策自动用新因子。';
  }
}

$('autoResearchChk').addEventListener('change', async () => {
  const on = $('autoResearchChk').checked;
  const interval = Number($('autoResearchInterval').value) || 86400;
  try {
    const d = await api('/api/research/auto', 'POST', { on, interval_sec: interval });
    renderResearchAuto(d.research_auto);
    toast(on ? '自动研究已开启（因子会定时进化）' : '自动研究已关闭', 'ok');
    poll();
  } catch (e) {
    $('autoResearchChk').checked = !on;
    toast(`设置失败：${e.message}`, 'err');
  }
});

function renderRisk(r) {
  if (!r) return;
  const ov = Number(r.leverage_override || 0);
  const sel = $('levSelect');
  if (sel && document.activeElement !== sel) sel.value = String(ov);
  const cap = (Number(r.max_loss_per_trade_pct) * 100).toFixed(1);
  const rt = $('riskTotal');
  if (rt && document.activeElement !== rt) rt.value = cap;
  const rpc = $('riskPosCapInput');
  if (rpc && document.activeElement !== rpc) rpc.value = (Number(r.max_position_pct) * 100).toFixed(0);
  const rd = $('riskDaily');
  if (rd && document.activeElement !== rd) rd.value = (Number(r.daily_loss_limit_pct) * 100).toFixed(1);
  const rdd = $('riskDrawdown');
  if (rdd && document.activeElement !== rdd) rdd.value = (Number(r.max_drawdown_pct) * 100).toFixed(0);
}

$('levSelect').addEventListener('change', async () => {
  const v = Number($('levSelect').value);
  try {
    const d = await api('/api/risk', 'POST', { leverage_override: v });
    renderRisk(d.risk);
    toast(v > 0
      ? `杠杆已固定为 ${v}x，已写进配置（下次决策生效）`
      : '杠杆恢复自动（跟随决策，上限以交易所为准）', 'ok');
  } catch (e) {
    toast(`设置失败：${e.message}`, 'err');
    poll();
  }
});

$('riskTotal').addEventListener('change', async () => {
  const v = Number($('riskTotal').value);
  if (!v || v < 0.1 || v > 5) { toast('总持仓风险需在 0.1% ~ 5% 之间', 'err'); poll(); return; }
  try {
    const d = await api('/api/risk', 'POST', { max_loss_per_trade_pct: v / 100 });
    renderRisk(d.risk);
    toast(`总持仓风险已设为 ${v}%`, 'ok');
  } catch (e) { toast(`设置失败：${e.message}`, 'err'); poll(); }
});

$('riskPosCapInput').addEventListener('change', async () => {
  const v = Number($('riskPosCapInput').value);
  if (!v || v < 1 || v > 100) { toast('仓位持仓上限需在 1% ~ 100% 之间', 'err'); poll(); return; }
  try {
    const d = await api('/api/risk', 'POST', { max_position_pct: v / 100 });
    renderRisk(d.risk);
    toast(`仓位持仓上限已设为 ${v}%`, 'ok');
  } catch (e) { toast(`设置失败：${e.message}`, 'err'); poll(); }
});

$('riskDaily').addEventListener('change', async () => {
  const v = Number($('riskDaily').value);
  if (!v || v < 1 || v > 30) { toast('当日亏损上限需在 1% ~ 30% 之间', 'err'); poll(); return; }
  try {
    const d = await api('/api/risk', 'POST', { daily_loss_limit_pct: v / 100 });
    renderRisk(d.risk);
    toast(`当日亏损上限已设为 ${v}%`, 'ok');
  } catch (e) { toast(`设置失败：${e.message}`, 'err'); poll(); }
});

$('riskDrawdown').addEventListener('change', async () => {
  const v = Number($('riskDrawdown').value);
  if (!v || v < 5 || v > 50) { toast('最大回撤需在 5% ~ 50% 之间', 'err'); poll(); return; }
  try {
    const d = await api('/api/risk', 'POST', { max_drawdown_pct: v / 100 });
    renderRisk(d.risk);
    toast(`最大回撤已设为 ${v}%`, 'ok');
  } catch (e) { toast(`设置失败：${e.message}`, 'err'); poll(); }
});

function renderAutoResume(a) {
  const chk = $('autoResumeChk');
  const el = $('autoResumeState');
  if (!chk || !el) return;
  const on = !!(a && a.enabled);
  chk.checked = on;
  if (!a) { el.textContent = ''; return; }
  if (!on) { el.textContent = '重启后自动恢复交易：关闭。开了之后服务重启会自动接着跑（模拟盘）。'; return; }
  if (a.resumed) {
    el.textContent = `重启后自动恢复交易：已开启 · 已恢复于 ${a.at || ''}（每 ${a.interval_sec || 300}s，自动下单=${a.execute ? '是' : '否'}）`;
  } else if (a.error) {
    el.textContent = `重启后自动恢复交易：已开启 · 本次未恢复：${a.error}`;
  } else {
    el.textContent = '重启后自动恢复交易：已开启（仅模拟盘）';
  }
}

function renderAutoArm(a) {
  const chk = $('autoArmChk');
  const el = $('autoArmState');
  if (!chk || !el) return;
  const on = !!(a && a.enabled);
  chk.checked = on;
  if (!a) { el.textContent = ''; return; }
  if (!on) {
    el.textContent = '重启后自动解锁：关闭。开了之后重启服务会自动连上，不用手点。';
    return;
  }
  const env = a.env || 'demo';
  if (a.ok) {
    el.textContent = `重启后自动解锁：已开启（${env}）· 上次成功 ${a.at || ''}${a.balance != null ? ` · 余额 ${fmt(a.balance)} USDT` : ''}`;
  } else if (a.error) {
    el.textContent = `重启后自动解锁：已开启（${env}）· 还没成功（第 ${a.attempts || 0} 次）：${a.error}`;
  } else {
    el.textContent = `重启后自动解锁：已开启（${env}）`;
  }
}

$('autoResumeChk').addEventListener('change', async () => {
  const on = $('autoResumeChk').checked;
  try {
    const d = await api('/api/trading/auto-resume', 'POST', { on });
    renderAutoResume(d.auto_resume);
    toast(on ? '已开启：服务重启后会自动恢复自动交易（仅模拟盘）' : '已关闭自动恢复', 'ok');
    poll();
  } catch (e) {
    $('autoResumeChk').checked = !on;
    toast(`设置失败：${e.message}`, 'err');
  }
});

$('autoArmChk').addEventListener('change', async () => {
  const on = $('autoArmChk').checked;
  const ex = currentExchange('testnet');
  try {
    const d = await api('/api/trading/auto-arm', 'POST', { on, env: 'demo', exchange: ex });
    renderAutoArm(d.auto_arm);
    toast(on ? `已开启：下次重启会自动解锁 ${ex === 'okx' ? 'OKX' : '币安'} demo` : '已关闭自动解锁', 'ok');
    poll();
  } catch (e) {
    $('autoArmChk').checked = !on;
    toast(`设置失败：${e.message}`, 'err');
  }
});

function renderScan(data) {
  $('scanMeta').textContent = `${data.rows.length} 个品种 · ${data.ts}`;
  const tb = $('scanTable').querySelector('tbody');
  if (!data.rows.length) { tb.innerHTML = '<tr><td colspan="7" class="muted center">没有结果</td></tr>'; return; }
  tb.innerHTML = data.rows.map((r) => {
    if (!r.ok) return `<tr><td class="mono">${esc(r.symbol)}</td><td colspan="6" class="muted">${esc(r.error)}</td></tr>`;
    const cls = r.action === 'long' ? 'up' : r.action === 'short' ? 'down' : 'muted';
    const verdict = r.approved ? '<span class="up">通过</span>' : '<span class="down">否决</span>';
    return `<tr>
      <td class="mono">${esc(r.symbol)}</td>
      <td>${fmt(r.price)}</td>
      <td class="${cls}">${esc(r.action)}</td>
      <td>${(r.size_pct * 100).toFixed(1)}%</td>
      <td>${fmt(r.leverage, 1)}x</td>
      <td>${verdict}</td>
      <td class="muted">${esc((r.reasons || []).join('；') || '—')}</td>
    </tr>`;
  }).join('');
}

$('runScan').addEventListener('click', async () => {
  const btn = $('runScan');
  btn.disabled = true; btn.textContent = '扫描中…';
  $('scanMeta').textContent = '正在逐个跑决策（没缓存的要先下载）…';
  try {
    const d = await api('/api/scan', 'POST', {});
    renderScan(d);
    const best = d.rows.find((r) => r.ok && r.action !== 'flat' && r.approved);
    toast(best ? `最有信号的是 ${best.symbol}：${best.action} ${(best.size_pct * 100).toFixed(1)}%` : '全部品种都没信号，观望', best ? 'ok' : '');
    await loadSymbols();
  } catch (e) {
    toast(`扫描失败：${e.message}`, 'err');
    $('scanMeta').textContent = '';
  } finally {
    btn.disabled = false; btn.textContent = '扫一遍所有品种';
  }
});

/* ---------------- 实时报价 / 数据源告警 ---------------- */
const LIVE_SOURCE_LABEL = { 'binance-fapi': '币安永续', 'binance-spot': '币安现货', 'binance-vision': '币安镜像', 'gate-futures': 'Gate 永续', 'okx': 'OKX永续', 'bybit': 'Bybit' };

function renderLive(s) {
  const live = s.live || {};
  const dot = $('liveDot');
  const ts = $('liveTs');
  if (live.price) {
    dot.className = 'live-dot on';
    const isSpot = live.source === 'binance-spot';
    dot.title = isSpot
      ? '当前是现货价（永续源不可达），与合约持仓标记价有 funding 基差'
      : '实时成交价（永续）';
    const lag = live.ts ? (Date.now() - new Date(live.ts.replace(/-/g, '/')).getTime()) / 1000 : 0;
    ts.textContent = `${LIVE_SOURCE_LABEL[live.source] || live.source || '实时'} · ${live.ts ? live.ts.slice(11) : ''}${lag > 60 ? `（${Math.round(lag / 60)} 分钟前）` : ''}`;
    // 顶栏涨跌幅：用后端的 24h 涨跌幅（不再依赖 K 线）
    if (live.change_pct != null) {
      const el = $('pct');
      el.textContent = pct(live.change_pct);
      el.className = `pct ${live.change_pct >= 0 ? 'up' : 'down'}`;
      el.title = '24 小时涨跌幅';
      $('pctSpan').textContent = '24h';
    }
  } else if (live.error) {
    dot.className = 'live-dot err';
    // 把第一行原因显示出来，别只丢一句「不可用」
    const first = String(live.error).split('\n').map((x) => x.trim()).filter(Boolean)[0] || '未知错误';
    const brief = first.replace(/^实时报价获取失败：/, '').slice(0, 90);
    ts.textContent = `实时价不可用 · 用 K 线收盘价（${brief}）`;
    ts.title = live.error;
    if (!liveWarned) {
      liveWarned = true;
      toast(`实时报价全挂：${brief}\n在服务器上跑 run.cmd netcheck 看哪个源不通`, 'err');
    }
  } else {
    dot.className = 'live-dot';
    ts.textContent = '连接中…';
  }
}

function renderDataWarn(s) {
  const src = String(s.data_source || '');
  const el = $('dataWarn');
  if (src.startsWith('synthetic')) {
    el.classList.remove('hidden');
    el.innerHTML = `⚠️ <b>当前是合成假数据</b>（${esc(src)}），价格和涨跌都不是真实市场。
      这里的回测/决策结果只能验证流程能跑通，不能用来判断策略好坏。
      切真实行情：把 <code>config/default.json</code> 里 <code>market.source</code> 改成 <code>binance</code>，再点「刷新行情」。`;
  } else if (src.startsWith('stale-cache')) {
    el.classList.remove('hidden');
    el.innerHTML = `⚠️ 行情刷新失败，正在用旧缓存（${esc(src)}）。价格可能已经过时。`;
  } else {
    el.classList.add('hidden');
    el.innerHTML = '';
  }
}

function renderStatus(s) {
  syncSymbolSelect(s.symbol);
  const loading = !!s.data_loading;
  $('loadingBar').classList.toggle('hidden', !loading);
  // 行情尚未就绪：价格区用小号字体 + 动态转圈，避免沿用 22px 大价格字号显得突兀
  const priceEl = $('price');
  if (loading) {
    priceEl.className = 'price is-loading';
    priceEl.innerHTML = '<span class="spin sm"></span> 加载中';
  } else {
    priceEl.className = 'price';
    priceEl.textContent = fmt(s.price);
  }
  $('dataSource').textContent = `数据源 ${loading ? '加载中…' : shortSource(s.data_source)}`;
  $('dataSource').title = s.data_source || '';
  $('llmTag').textContent = `LLM ${s.llm.provider}`;
  if (s.server && s.server.version) $('verTag').innerHTML = `<span class="ver-v">V</span> ${esc(s.server.version)}`;
  renderLive(s);
  renderDataWarn(s);

  const badge = $('modeBadge');
  const exName = (s.exchange === 'okx') ? 'OKX' : '币安';
  const modeLabel = s.mode !== 'live'
    ? '本地模拟'
    : (s.testnet ? `模拟盘·${exName}` : `主网实盘·${exName}`);
  badge.textContent = modeLabel;
  badge.className = `badge ${s.mode === 'live' ? (s.testnet ? 'paper' : 'live') : 'paper'}`;

  $('haltBtn').classList.toggle('selected', s.halt);
  $('haltBtn').textContent = s.halt ? '已熔断' : '熔断';

  // 数据来源：解锁后自动切到交易所账户，不再显示本地模拟账本
  const ex = s.account_view === 'exchange';
  const exOk = ex && !s.account_error;
  const chip = $('equitySrc');
  chip.textContent = s.account_error
    ? '本地（交易所不可达）'
    : (ex ? (s.testnet ? '交易所 demo' : '交易所主网') : '本地模拟');
  chip.className = `src-chip${exOk ? ' ex' : ''}`;
  chip.title = s.account_error
    ? `交易所接口报错，暂时显示本地模拟账本：${s.account_error}`
    : (ex ? '数据来自交易所账户接口（/fapi/v2/account + /fapi/v1/userTrades）'
          : '数据来自本地模拟账本 runs/engine_state.json');

  $('equity').textContent = fmt(s.equity);
  $('equitySub').textContent = exOk
    ? `可用 ${fmt(s.cash_equity)} · 钱包 ${fmt(s.wallet_balance)}`
    : `可用 ${fmt(s.cash_equity)}`;
  $('dayPnl').textContent = pct(s.day_pnl_pct);
  $('dayPnl').className = s.day_pnl_pct >= 0 ? 'up' : 'down';
  $('dayPnl').title = exOk && s.day_pnl_abs != null
    ? '今日资金流水合计：已实现盈亏 + 手续费 + 资金费'
    : '';
  // 百分比太小时看不出金额（比如 -0.003%），交易所模式下把绝对值也标出来
  $('dayPnlSub').textContent = exOk && s.day_pnl_abs != null
    ? `${s.day_pnl_abs >= 0 ? '+' : ''}${fmt(s.day_pnl_abs, 4)} USDT`
    : '';
  $('drawdown').textContent = pct(s.drawdown_pct);

  // 交易所读取失败的警告条
  const aw = $('accountWarn');
  if (s.account_error) {
    aw.classList.remove('hidden');
    // 把币安原始报错截短，别让一整串英文撑爆横幅
    const brief = String(s.account_error).replace(/\s+/g, ' ').slice(0, 160);
    aw.innerHTML = `⚠️ <b>交易所账户读取失败</b>：<code>${esc(brief)}</code>。
      下面显示的权益/持仓/成交是<b>本地模拟账本</b>的数字，不是交易所账户。`;
  } else {
    aw.classList.add('hidden');
    aw.innerHTML = '';
  }
  const posChip = $('posSrc');
  posChip.classList.toggle('hidden', !exOk);
  if (exOk) posChip.textContent = '交易所';

  if (s.position) {
    const p = s.position;
    $('position').textContent = `${sideCn(p.side)} ${fmt(p.qty, 4)}`;
    $('position').className = p.side === 'LONG' ? 'up' : 'down';
    $('posSub').textContent = `开仓 ${fmtPx(p.entry)} · 浮盈亏 ${fmt(p.unrealized)}`;
  } else {
    $('position').textContent = '空仓';
    $('position').className = '';
    $('posSub').textContent = '';
  }

  // 成交表
  const tb = $('fillsTable').querySelector('tbody');
  if (s.fills && s.fills.length) {
    tb.innerHTML = s.fills.map((f) => {
      const cls = f.side === 'LONG' ? 'up' : 'down';
      const fee = f.fee ? `<span class="muted"> 费${fmt(Math.abs(f.fee), 4)}</span>` : '';
      const tip = f.reason ? ` title="${esc(f.reason)}"` : '';
      return `<tr${tip}>
      <td class="mono">${esc(f.symbol || s.symbol)}</td>
      <td class="muted">${esc(f.ts)}</td>
      <td class="${cls}">${esc(actionCn(f.action))}</td>
      <td class="${cls}">${esc(sideCn(f.side))}</td>
      <td>${fmtQty(f.qty)}</td>
      <td>${fmtPx(f.price)}</td>
      <td class="${(f.pnl || 0) >= 0 ? 'up' : 'down'}">${f.pnl !== undefined ? fmt(f.pnl) : '--'}${fee}</td>
    </tr>`;
    }).join('');
  } else {
    tb.innerHTML = '<tr><td colspan="7" class="muted center">暂无成交</td></tr>';
  }

  // 持仓表
  const pt = $('posTable').querySelector('tbody');
  if (s.position) {
    const p = s.position;
    pt.innerHTML = `<tr>
      <td class="mono">${esc(p.symbol || s.symbol)}<span class="cell-tag">${esc(p.mode || (exOk ? '交易所' : 'paper'))}</span></td>
      <td class="${p.side === 'LONG' ? 'up' : 'down'}">${esc(sideCn(p.side))}</td>
      <td>${fmtQty(p.qty)}</td><td>${fmtPx(p.entry)}</td><td>${fmtPx(p.mark)}</td>
      <td>${fmt(p.leverage, 1)}x</td>
      <td class="col-2line"><span class="up">${fmtPx(p.stop)}</span><span class="down">${fmtPx(p.tp)}</span></td>
      <td class="${p.unrealized >= 0 ? 'up' : 'down'}">${fmt(p.unrealized)}</td></tr>`;
  } else {
    pt.innerHTML = '<tr><td colspan="8" class="muted center">空仓</td></tr>';
  }
  if ($('posMeta')) $('posMeta').textContent = s.position ? '持仓中' : '空仓';

  // 交易页状态
  $('armState').textContent = modeLabel;
  $('armState').className = `badge ${s.mode === 'live' ? (s.testnet ? 'paper' : 'live') : 'paper'}`;
  // 标题右侧直接写清「现在连的是谁」，省得点开面板才知道
  if ($('exNow')) {
    $('exNow').textContent = s.mode === 'live'
      ? `${exName} · ${s.testnet ? '模拟盘' : '主网实盘'}${s.armed ? '' : '（未连接）'}`
      : '';
  }
  // 交易页右侧连接状态卡
  if ($('connBar')) {
    const on = s.mode === 'live' && s.armed;
    const tag = on ? (s.testnet ? '模拟盘' : '主网实盘') : '本地模拟';
    $('connBar').classList.toggle('on', on);
    $('connDot').classList.toggle('on', on);
    $('connStateText').textContent = on ? '已连接' : '未连接';
    $('connModeTag').textContent = tag;
    $('connBarText').textContent = on ? `${exName} · ${tag}` : '本地模拟';
    $('connBarSub').textContent = on
      ? `${s.testnet ? 'Demo 假钱' : '真钱'} · 权益 ${fmt(s.equity)} USDT`
      : '未接交易所，下单只在本地撮合';
    $('connDisarmBtn').classList.toggle('hidden', !on);
    $('connSwitchBtn').textContent = on ? '切换交易所' : '去连接交易所';
  }
  // 三个模式按钮的高亮跟着真实状态走
  const connected = s.mode === 'live' && s.armed;
  // 交易模式按钮高亮：展开某个模式面板时该按钮立刻变绿（选哪个绿哪个）；
  // 没有面板展开时，绿色跟随真实模式
  const panelOpen = !$('testnetBox').classList.contains('hidden') || !$('liveBox').classList.contains('hidden');
  if (panelOpen) {
    $('paperBtn').classList.toggle('selected', false);
    $('testnetBtn').classList.toggle('selected', !$('testnetBox').classList.contains('hidden'));
    $('liveBtn').classList.toggle('selected', !$('liveBox').classList.contains('hidden'));
  } else {
    $('paperBtn').classList.toggle('selected', s.mode !== 'live');
    $('testnetBtn').classList.toggle('selected', s.mode === 'live' && s.testnet);
    $('liveBtn').classList.toggle('selected', s.mode === 'live' && !s.testnet);
  }
  // 已连上就在按钮上打个勾，不用再靠面板里的文字判断
  $('testnetBtn').classList.toggle('ok', connected && s.testnet);
  $('liveBtn').classList.toggle('ok', connected && !s.testnet);
  // 面板开着的时候，状态行和切换按钮要跟着实时刷新
  if (!$('testnetBox').classList.contains('hidden')) renderExchangePanel('testnet');
  if (!$('liveBox').classList.contains('hidden')) renderExchangePanel('live');
  // 四套 Key 分开显示，别再笼统写「已配置」
  if ($('keysPresent')) {
    const K = s.keys || {};
    const tags = [];
    if (K.binance && K.binance.demo) tags.push('币安模拟盘');
    if (K.binance && K.binance.live) tags.push('币安主网');
    if (K.okx && K.okx.demo) tags.push('OKX模拟盘');
    if (K.okx && K.okx.live) tags.push('OKX主网');
    $('keysPresent').textContent = tags.length ? tags.join(' / ') : '未配置';
  }
  renderRisk(s.risk);
  renderResearchAuto(s.research_auto);
  // 交易页的四项状态跟总览页保持一致，别一个说已连接一个说未连接
  if (connected) {
    $('connState').textContent = s.testnet ? `已连接（模拟盘·${exName}）` : `已连接（主网实盘·${exName}）`;
    $('connState').className = 'up';
    $('liveBalance').textContent = `${fmt(s.equity)} USDT`;
    $('livePos').textContent = s.position
      ? `${sideCn(s.position.side)} ${fmt(s.position.qty, 4)} @ ${fmt(s.position.entry)}`
      : '无';
  } else {
    $('connState').textContent = '未连接';
    $('connState').className = '';
    $('liveBalance').textContent = '--';
    $('livePos').textContent = '--';
  }
  renderAutoArm(s.auto_arm);
  renderAutoResume(s.auto_resume);
  $('autoState').textContent = s.auto.running ? '运行中' : '停止';
  $('autoState').className = `badge ${s.auto.running ? 'on' : 'off'}`;
  $('autoMeta').textContent = s.auto.running
    ? `每 ${s.auto.interval_sec}s 跑一次，自动下单：${s.auto.execute ? '是' : '否'}，上次：${s.auto.last_run || '--'}`
    : '未运行';
  // 状态指示配色：运行中→启动绿/停止灰；停止→启动灰/停止红
  if ($('autoStart')) { $('autoStart').classList.toggle('active', s.auto.running); }
  if ($('autoStop')) { $('autoStop').classList.toggle('active', !s.auto.running); }
  if (s.auto.running) { $('autoInterval').value = s.auto.interval_sec; $('autoExec').checked = s.auto.execute; }
  if (s.last_decision) {
    $('execBtn').disabled = !s.last_decision.order;
    $('execBtn').title = s.last_decision.order ? '' : '上次决策没有产生订单';
  }
  renderPnl();
  renderPosHistory();
  renderDaily();
  renderOpportunities();
}

/* ---------------- K 线图 ---------------- */
function setupCanvas(cv) {
  const dpr = window.devicePixelRatio || 1;
  const rect = cv.getBoundingClientRect();
  const w = Math.max(320, rect.width);
  const h = parseInt(cv.getAttribute('height'), 10) || 300;
  cv.width = w * dpr;
  cv.height = h * dpr;
  cv.style.height = `${h}px`;
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}

function renderPosHistory() {
  const tb = $('posHistBody');
  if (!tb) return;
  const hist = (lastStatus && lastStatus.position_history) || [];
  if ($('histMeta')) $('histMeta').textContent = hist.length ? `共 ${hist.length} 笔` : '最近平仓';
  if (!hist.length) {
    tb.innerHTML = '<tr><td colspan="7" class="muted center">暂无平仓记录</td></tr>';
    return;
  }
  const reasonCn = { manual: '手动', stop: '止损', tp: '止盈', auto: '自动', kill: '熔断' };
  tb.innerHTML = hist.map((h) => {
    const up = (h.pnl || 0) >= 0;
    const dir = h.side === 'LONG' ? '多' : '空';
    return `<tr>
      <td class="muted">${esc(String(h.closed_at || '').slice(5))}</td>
      <td>${esc(h.symbol || '')}<span class="cell-tag">${h.mode === 'live' ? '交易所' : '本地模拟'}</span></td>
      <td class="${h.side === 'LONG' ? 'up' : 'down'}">${dir}</td>
      <td>${fmtQty(h.qty)}</td>
      <td class="muted">${fmtPx(h.entry)} → ${fmtPx(h.exit)}</td>
      <td class="${up ? 'up' : 'down'}">${up ? '+' : ''}${fmt(h.pnl, 2)}</td>
      <td class="muted">${esc(reasonCn[h.reason] || h.reason || '')}</td>
    </tr>`;
  }).join('');
}

let calCursor = null;   // {y, m} 当前日历月份

function renderDaily() {
  const grid = $('calGrid');
  if (!grid) return;
  const days = (lastStatus && lastStatus.daily) || [];
  const now = new Date();
  if (!calCursor) calCursor = { y: now.getFullYear(), m: now.getMonth() };

  const byDay = new Map(days.map((d) => [d.date, d]));
  const { y, m } = calCursor;
  const pad = (n) => String(n).padStart(2, '0');
  const monthPrefix = `${y}-${pad(m + 1)}`;
  const monthRows = days.filter((d) => String(d.date || '').startsWith(monthPrefix));
  const maxAbs = Math.max(...monthRows.map((d) => Math.abs(d.pnl || 0)), 1e-9);

  const firstDow = (new Date(y, m, 1).getDay() + 6) % 7;   // 周一为一周起点
  const total = new Date(y, m + 1, 0).getDate();
  const todayKey = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;

  let html = '';
  for (let i = 0; i < firstDow; i += 1) html += '<div class="cal-cell empty"></div>';

  let monthPnl = 0;
  let wins = 0;
  let losses = 0;
  for (let d = 1; d <= total; d += 1) {
    const key = `${monthPrefix}-${pad(d)}`;
    const rec = byDay.get(key);
    let cls = 'cal-cell';
    let style = '';
    let label = '';
    let tip = `${key}`;
    if (rec) {
      const pnl = rec.pnl || 0;
      monthPnl += pnl;
      if (pnl > 0) wins += 1;
      else if (pnl < 0) losses += 1;
      const a = 0.10 + 0.42 * Math.min(1, Math.abs(pnl) / maxAbs);
      cls += pnl >= 0 ? ' up' : ' down';
      style = `background: rgba(${pnl >= 0 ? '0,224,143' : '255,77,94'},${a.toFixed(3)})`;
      label = `<span class="v">${pnl >= 0 ? '+' : ''}${fmt(pnl, 2)}</span>`;
      tip = `${key} · ${rec.trades} 笔（胜 ${rec.wins}）· ${pnl >= 0 ? '+' : ''}${fmt(pnl, 2)} U`;
    }
    if (key === todayKey) cls += ' today';
    html += `<div class="${cls}" style="${style}" title="${esc(tip)}"><span class="d">${d}</span>${label}</div>`;
  }
  grid.innerHTML = html;

  $('calMonth').textContent = `${y} 年 ${m + 1} 月`;
  $('calSummary').textContent = monthRows.length
    ? `本月 ${monthPnl >= 0 ? '+' : ''}${fmt(monthPnl, 2)} U · 盈 ${wins} 天 / 亏 ${losses} 天`
    : '本月还没有平仓记录';

  // 翻月：最早只能到有数据的那一月，最晚到本月
  const earliest = days.length ? String(days[0].date || '').slice(0, 7) : monthPrefix;
  const curMonth = `${now.getFullYear()}-${pad(now.getMonth() + 1)}`;
  const cursorKey = monthPrefix;
  $('calPrev').disabled = cursorKey <= earliest;
  $('calNext').disabled = cursorKey >= curMonth;
}

function shiftMonth(delta) {
  if (!calCursor) return;
  const d = new Date(calCursor.y, calCursor.m + delta, 1);
  calCursor = { y: d.getFullYear(), m: d.getMonth() };
  renderDaily();
}

function renderPnl() {
  const totalEl = $('pnlTotal');
  const statsEl = $('pnlStats');
  const tradesEl = $('pnlTrades');
  if (!totalEl || !statsEl || !tradesEl) return;

  // 已平仓记录：优先用「持仓历史」（新数据源），没有才退回成交里的 pnl
  const hist = (lastStatus?.position_history || []).slice()
    .sort((a, b) => String(a.closed_at).localeCompare(String(b.closed_at)));
  const fills = hist.length
    ? hist.map((h) => ({ ts: h.closed_at, pnl: h.pnl, action: 'CLOSE' }))
    : (lastStatus?.fills || []).filter((f) => Math.abs(f.pnl || 0) > 1e-9)
      .slice().sort((a, b) => String(a.ts).localeCompare(String(b.ts)));

  const total = fills.reduce((s, f) => s + (f.pnl || 0), 0);
  totalEl.textContent = `${total >= 0 ? '+' : ''}${fmt(total, 2)} U`;
  totalEl.className = total >= 0 ? 'up' : 'down';

  const wins = fills.filter((f) => (f.pnl || 0) > 0);
  const losses = fills.filter((f) => (f.pnl || 0) < 0);
  const winRate = fills.length ? `${Math.round(wins.length / fills.length * 100)}%` : '--';
  const grossWin = wins.reduce((s, f) => s + (f.pnl || 0), 0);
  const grossLoss = Math.abs(losses.reduce((s, f) => s + (f.pnl || 0), 0));
  const pf = grossLoss > 0 ? fmt(grossWin / grossLoss, 2) : (grossWin > 0 ? '∞' : '--');
  statsEl.innerHTML = `
    <div class="pnl-stat"><label>平仓笔数</label><b>${fills.length}</b></div>
    <div class="pnl-stat"><label>胜率</label><b>${winRate}</b></div>
    <div class="pnl-stat"><label>盈亏比</label><b>${pf}</b></div>`;

  if (!fills.length) {
    tradesEl.innerHTML = '<div class="muted center">暂无平仓成交</div>';
    return;
  }

  const maxAbs = Math.max(...fills.map((f) => Math.abs(f.pnl || 0)), 1e-9);
  const rows = fills.slice(-20).reverse().map((f) => {
    const up = (f.pnl || 0) >= 0;
    const w = Math.max(4, Math.round((Math.abs(f.pnl || 0) / maxAbs) * 100));
    return `<div class="pnl-row">
      <span class="pnl-time">${esc(f.ts || '')}</span>
      <span class="pnl-act muted">${esc(actionCn(f.action))}</span>
      <div class="pnl-bar-track"><div class="pnl-bar ${up ? 'up' : 'down'}" style="width:${w}%"></div></div>
      <span class="pnl-val ${up ? 'up' : 'down'}">${up ? '+' : ''}${fmt(f.pnl, 2)}</span>
    </div>`;
  }).join('');
  tradesEl.innerHTML = rows;
}

/* ---------------- 决策 ---------------- */
function renderDecision(d, switchTab = true) {
  if (!d || !d.analysts) return;
  latestDecision = d;
  const a = d.analysts || [];
  const analysts = a.map((x) => {
    const cls = x.stance > 0.1 ? 'up' : x.stance < -0.1 ? 'down' : 'muted';
    return `<div class="analyst">
      <h3>${esc(x.role)} <span class="stance ${cls}">${x.stance >= 0 ? '+' : ''}${Number(x.stance).toFixed(2)}</span></h3>
      <p>${esc(x.summary)}</p>
      ${(x.data_flags || []).map((f) => `<div class="flag">⚑ ${esc(f)}</div>`).join('')}
    </div>`;
  }).join('');

  const p = d.proposal, r = d.risk_decision, o = d.order;
  const orderHtml = o
    ? `<div class="order-box">
        <b>${esc(sideCn(o.side))} ${fmt(o.quantity, 4)} ${esc(o.symbol)}</b> · 名义 ${fmt(o.notional_usd)} USDT · ${fmt(o.leverage, 1)}x<br>
        止损 ${fmt(o.stop_loss_price)} · 止盈 ${fmt(o.take_profit_price)} · 单笔最大亏损 ${fmt(o.risk_usd)} USDT（权益 ${(o.max_loss_pct_of_equity * 100).toFixed(2)}%）<br>
        <span class="muted">状态 ${esc(o.status)}，需要在「交易」页手动执行</span>
      </div>`
    : `<div class="order-box rejected">本次没有订单：${esc(r.reasons.join('；'))}</div>`;

  $('decisionBody').className = 'decision-body';
  $('decisionBody').innerHTML = `
    <div class="card">
      <div class="card-head"><h2>① 分析师团队</h2><span class="muted">${esc(d.ts)}</span></div>
      <div class="analyst-grid">${analysts}</div>
    </div>
    <div class="card">
      <div class="card-head"><h2>② 研究辩论</h2><span class="muted">综合评分 ${Number(d.debate.score).toFixed(2)}</span></div>
      <div class="debate">
        <div class="bull"><h3>多头</h3><p>${esc(d.debate.bull_case)}</p></div>
        <div class="bear"><h3>空头</h3><p>${esc(d.debate.bear_case)}</p></div>
      </div>
      <div class="thesis">${esc(d.debate.thesis)}</div>
    </div>
    <div class="grid-2">
      <div class="card">
        <div class="card-head"><h2>③ 交易员提案</h2></div>
        <div class="proposal-grid">
          <div class="item"><span>方向</span><b class="${p.action === 'long' ? 'up' : p.action === 'short' ? 'down' : ''}">${esc(p.action)}</b></div>
          <div class="item"><span>仓位</span><b>${(p.size_pct * 100).toFixed(1)}%</b></div>
          <div class="item"><span>杠杆</span><b>${fmt(p.leverage, 1)}x</b></div>
          <div class="item"><span>止损</span><b>${(p.stop_loss_pct * 100).toFixed(2)}%</b></div>
          <div class="item"><span>止盈</span><b>${(p.take_profit_pct * 100).toFixed(2)}%</b></div>
          <div class="item"><span>置信度</span><b>${Number(p.confidence).toFixed(2)}</b></div>
        </div>
        <p class="muted">${esc(p.rationale)}</p>
        ${factorSigHtml(p)}
      </div>
      <div class="card">
        <div class="card-head"><h2>④ 风控</h2></div>
        <p>风控官：<b>${esc(d.risk_officer.verdict)}</b>（仓位乘数 ${Number(d.risk_officer.size_multiplier).toFixed(2)}）</p>
        <ul class="muted">${(d.risk_officer.reasons || []).map((x) => `<li>${esc(x)}</li>`).join('')}</ul>
        <p>风控引擎：<b class="${r.approved ? 'up' : 'down'}">${r.approved ? '通过' : '拒绝'}</b>
           · ${esc(r.action)} · 仓位 ${(r.size_pct * 100).toFixed(2)}% · ${fmt(r.leverage, 1)}x</p>
        <ul class="muted">${(r.reasons || []).map((x) => `<li>${esc(x)}</li>`).join('')}</ul>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h2>⑤ 订单计划</h2></div>
      ${orderHtml}
    </div>`;
  if (switchTab) showTab('decision');
}

$('runDecision').addEventListener('click', async () => {
  const btn = $('runDecision');
  btn.disabled = true; btn.textContent = '运行中…';
  try {
    const data = await api('/api/decision', 'POST', { rounds: Number($('rounds').value) || 2 });
    renderDecision(data);
    toast('决策完成', 'ok');
  } catch (e) {
    toast(`决策失败：${e.message}`, 'err');
  } finally {
    btn.disabled = false; btn.textContent = '运行决策';
    poll();
  }
});

/* ---------------- 研究 ---------------- */
function renderResearch(r, announce = false) {
  const rows = r.rows || [];
  $('researchMeta').textContent = `迭代 ${r.iterations} 次 · 通过 ${r.accepted} 个 · ${r.generated_at}`;
  // 冠军因子（best 是 proposal：name/family/hypothesis/code）
  const best = r.best;
  if (best && best.code) {
    $('bestCard').classList.remove('hidden');
    $('bestMeta').textContent = `${best.name || ''} · ${best.family || ''}`;
    $('bestHypo').textContent = best.hypothesis || '';
    $('bestCode').textContent = best.code;
  }
  const tb = $('researchTable').querySelector('tbody');
  if (!rows.length) { tb.innerHTML = '<tr><td colspan="7" class="muted center">没有结果</td></tr>'; return; }
  tb.innerHTML = rows.map((x) => {
    if (!x.ok) return `<tr><td>${esc(x.name)}</td><td>${esc(x.family)}</td><td colspan="4" class="muted">${esc(x.error)}</td><td class="down">失败</td></tr>`;
    const cls = x.accepted ? 'up' : 'down';
    return `<tr>
      <td class="mono">${esc(x.name)}</td><td class="muted">${esc(x.family)}</td>
      <td>${x.is_sharpe.toFixed(2)}</td><td>${x.oos_sharpe.toFixed(2)}</td>
      <td class="${x.oos_dd <= -20 ? 'down' : ''}">${x.oos_dd.toFixed(1)}%</td>
      <td>${x.score === null ? '--' : x.score.toFixed(2)}</td>
      <td class="${cls}">${x.accepted ? '通过' : esc(x.reject || '淘汰')}</td></tr>`;
  }).join('');
  if (announce && r.holdout && r.best) {
    toast(`最优因子 ${r.best.name} 留出集夏普 ${Number(r.holdout.sharpe).toFixed(2)}`, 'ok');
  }
}

$('runResearch').addEventListener('click', async () => {
  const btn = $('runResearch');
  btn.disabled = true; btn.textContent = '挖掘中…';
  try {
    const data = await api('/api/research', 'POST', { iterations: Number($('iterations').value) || 6 });
    renderResearch(data, true);
    toast(`研究完成，通过 ${data.accepted} 个`, 'ok');
  } catch (e) {
    toast(`研究失败：${e.message}`, 'err');
  } finally {
    btn.disabled = false; btn.textContent = '开始挖掘';
  }
});

/* ---------------- 交易 ---------------- */
function exCn(ex) { return ex === 'okx' ? 'OKX' : '币安'; }
// 自动交易跑着的时候禁止换交易所 / 断开，否则循环会拿着半截状态下单
function autoRunning() {
  return !!(lastStatus && lastStatus.auto && lastStatus.auto.running);
}
function blockIfAutoRunning(what) {
  if (!autoRunning()) return false;
  toast(`自动交易正在运行，先点「停止」再${what}`, 'err');
  return true;
}
// 当前选中的交易所（币安 / OKX）：按面板 scope 读取分段控件
function currentExchange(scope) {
  const box = scope === 'live' ? $('liveBox') : $('testnetBox');
  const sel = box.querySelector('.ex-choice .seg.sel');
  return sel ? sel.dataset.ex : 'binance';
}
// 把某个面板的分段控件指到指定交易所
function selectExchange(scope, ex) {
  const box = scope === 'live' ? $('liveBox') : $('testnetBox');
  box.querySelectorAll('.ex-choice .seg').forEach((b) => {
    b.classList.toggle('sel', b.dataset.ex === ex);
  });
  syncPassRow(scope);
}
// 切到 OKX 时显示 Passphrase 行，切到币安时隐藏
function syncPassRow(scope) {
  const ex = currentExchange(scope);
  const row = scope === 'live' ? $('livePassRow') : $('tnPassRow');
  if (row) row.classList.toggle('show', ex === 'okx');
}
// 某交易所在该环境下有没有 Key（模拟盘 / 主网四套 Key 各自独立，互不回退）
function hasKeys(ex, mode) {
  const k = lastStatus && lastStatus.keys && lastStatus.keys[ex];
  return !!(k && k[mode]);
}
// 这个交易所此刻是否就是正连着的那个
function isConnectedExchange(ex, scope) {
  if (!lastStatus || lastStatus.mode !== 'live' || !lastStatus.armed) return false;
  const wantDemo = scope !== 'live';
  return lastStatus.exchange === ex && !!lastStatus.testnet === wantDemo;
}
// 面板顶部那行「币安：已配 Key ｜ OKX：未配 Key」，顺便同步切换按钮
function renderExchangePanel(scope) {
  const mode = scope === 'live' ? 'live' : 'demo';
  const el = $(scope === 'live' ? 'liveExStatus' : 'tnExStatus');
  if (!el) return;
  el.innerHTML = ['binance', 'okx'].map((ex) => {
    const own = hasKeys(ex, mode);
    const cur = isConnectedExchange(ex, scope);
    const label = own ? '已配 Key' : '未配 Key';
    const cls = cur ? 'cur' : (own ? 'has' : 'none');
    return `<span class="${cls}">${exCn(ex)}：${label}${cur ? ' · 当前连接' : ''}</span>`;
  }).join('<span class="sep">｜</span>');

  const btn = $(scope === 'live' ? 'liveSwitchBtn' : 'tnSwitchBtn');
  if (!btn) return;
  const ex = currentExchange(scope);
  const ready = hasKeys(ex, mode);
  if (isConnectedExchange(ex, scope)) { btn.textContent = `已连${exCn(ex)}`; btn.disabled = true; }
  else if (ready) { btn.textContent = `切换到${exCn(ex)}`; btn.disabled = false; }
  else { btn.textContent = `先填${exCn(ex)}的 Key`; btn.disabled = true; }
}
// 交易所分段控件：单选高亮 + 同步 Passphrase 行 + 刷新状态行
document.querySelectorAll('.ex-choice').forEach((choice) => {
  const scope = choice.dataset.scope;
  choice.querySelectorAll('.seg').forEach((btn) => {
    btn.addEventListener('click', () => {
      choice.querySelectorAll('.seg').forEach((b) => b.classList.remove('sel'));
      btn.classList.add('sel');
      syncPassRow(scope);
      renderExchangePanel(scope);
    });
  });
  syncPassRow(scope);
});

$('paperBtn').addEventListener('click', async () => {
  try {
    await api('/api/trading/disarm', 'POST', {});
    closeModePanels();
    toast('已切回本地模拟', 'ok');
    poll();
  }
  catch (e) { toast(e.message, 'err'); }
});

// 一次只开一个配置面板，切换模式时把另一个收起来
function openPanel(boxId, btnId) {
  const box = $(boxId), btn = $(btnId);
  const opening = box.classList.contains('hidden');
  ['testnetBox', 'liveBox'].forEach((b) => $(b).classList.add('hidden'));
  ['testnetBtn', 'liveBtn'].forEach((b) => $(b).classList.remove('open'));
  // .open 表示「当前展开的是这个面板」；.selected 留给真实模式高亮，两者互不干扰
  if (opening) { box.classList.remove('hidden'); btn.classList.add('open'); }
}

function closeModePanels() {
  ['testnetBox', 'liveBox'].forEach((b) => $(b).classList.add('hidden'));
  ['testnetBtn', 'liveBtn'].forEach((b) => $(b).classList.remove('open'));
}

// 「模拟盘」：已连着 → 再点一次展开设置（换交易所/断开）；没连 → 有 Key 就直接连，没 Key 才开表单
$('testnetBtn').addEventListener('click', async () => {
  const already = lastStatus && lastStatus.mode === 'live' && lastStatus.testnet && lastStatus.armed;
  if (already) {
    openPanel('testnetBox', 'testnetBtn');
    selectExchange('testnet', lastStatus.exchange || 'binance');
    renderExchangePanel('testnet');
    return;
  }
  const ex = currentExchange('testnet');
  const hasKey = hasKeys(ex, 'demo');
  if (hasKey) {
    if (blockIfAutoRunning('连接交易所')) return;
    const btn = $('testnetBtn');
    btn.disabled = true;
    try {
      const res = await api('/api/trading/arm', 'POST', { mode: 'testnet', exchange: ex });
      toast(`已连上${exCn(ex)}模拟盘（Demo），余额 ${fmt(res.balance)} USDT（假钱）`, 'ok');
      closeModePanels();
      poll();
    } catch (e) {
      toast(`切换失败：${e.message}`, 'err');
      openPanel('testnetBox', 'testnetBtn');
      renderExchangePanel('testnet');
      poll();
    } finally { btn.disabled = false; }
  } else {
    openPanel('testnetBox', 'testnetBtn');
    renderExchangePanel('testnet');
  }
});

$('liveBtn').addEventListener('click', () => {
  openPanel('liveBox', 'liveBtn');
  if (lastStatus && lastStatus.mode === 'live' && !lastStatus.testnet) {
    selectExchange('live', lastStatus.exchange || 'binance');
  }
  renderExchangePanel('live');
});

// 面板里的「切换/连接」：直接用已存好的 Key 换交易所，不用重新输
$('tnSwitchBtn').addEventListener('click', async () => {
  if (blockIfAutoRunning('切换交易所')) return;
  const btn = $('tnSwitchBtn');
  const ex = currentExchange('testnet');
  const prev = btn.textContent;
  btn.disabled = true; btn.textContent = '连接中…';
  try {
    const res = await api('/api/trading/arm', 'POST', { mode: 'testnet', exchange: ex });
    toast(`已切到${exCn(ex)}模拟盘（Demo），余额 ${fmt(res.balance)} USDT（假钱）`, 'ok');
    closeModePanels();
    poll();
  } catch (e) {
    toast(`切换失败：${e.message}`, 'err');
    btn.disabled = false; btn.textContent = prev;
  }
});

// 断开：回到本地模拟（两个面板各一个按钮）
['tnDisarmBtn', 'liveDisarmBtn'].forEach((id) => {
  const el = $(id);
  if (!el) return;
  el.addEventListener('click', async () => {
    if (blockIfAutoRunning('断开连接')) return;
    try {
      await api('/api/trading/disarm', 'POST', {});
      closeModePanels();
      toast('已断开交易所，回到本地模拟', 'ok');
      poll();
    } catch (e) { toast(e.message, 'err'); }
  });
});

// 状态条上的「切换交易所」：直接展开当前模式对应的面板
$('connSwitchBtn').addEventListener('click', () => {
  // 交易所/API 设置已挪到「设置」页：先切页再展开对应面板
  showTab('settings');
  const scope = (lastStatus && lastStatus.mode === 'live' && !lastStatus.testnet) ? 'live' : 'testnet';
  const boxId = scope === 'live' ? 'liveBox' : 'testnetBox';
  openPanel(boxId, scope === 'live' ? 'liveBtn' : 'testnetBtn');
  selectExchange(scope, (lastStatus && lastStatus.exchange) || 'binance');
  renderExchangePanel(scope);
  $(boxId).scrollIntoView({ behavior: 'smooth', block: 'center' });
});

// 交易页那行「已移到设置页」的链接
const goSettings = $('goSettings');
if (goSettings) {
  goSettings.addEventListener('click', (e) => {
    e.preventDefault();
    showTab('settings');
  });
}

// 状态条上的「断开」
$('connDisarmBtn').addEventListener('click', async () => {
  if (blockIfAutoRunning('断开连接')) return;
  try {
    await api('/api/trading/disarm', 'POST', {});
    closeModePanels();
    toast('已断开交易所，回到本地模拟', 'ok');
    poll();
  } catch (e) { toast(e.message, 'err'); }
});

// 保存模拟盘 Key → 写 .env → 立刻自检
$('tnSaveBtn').addEventListener('click', async () => {
  const btn = $('tnSaveBtn');
  const ex = currentExchange('testnet');
  const pass = ex === 'okx' ? $('tnPass').value.trim() : '';
  btn.disabled = true; btn.textContent = '保存中…';
  try {
    const d = await api('/api/exchange/keys', 'POST', {
      exchange: ex, mode: 'demo', key: $('tnKey').value.trim(), secret: $('tnSecret').value.trim(), passphrase: pass,
    });
    $('tnKey').value = ''; $('tnSecret').value = ''; $('tnPass').value = '';
    $('tnStatus').textContent = formatSelftest(d);
    if (d.ok) {
      const res = await api('/api/trading/arm', 'POST', { mode: 'testnet', exchange: ex });
      toast(`已连上${ex === 'okx' ? 'OKX' : '币安'}模拟盘（Demo），余额 ${fmt(res.balance)} USDT（假钱）`, 'ok');
      closeModePanels();     // 连上了就把配置框收起来，只留徽章状态
      poll();
    } else {
      toast(`连接失败：${d.error || '未知错误'}`, 'err');
    }
  } catch (e) { toast(e.message, 'err'); }
  finally { btn.disabled = false; btn.textContent = '保存到 .env 并连接'; }
});

$('tnTestBtn').addEventListener('click', () => runSelftest($('tnStatus')));
$('selftestBtn').addEventListener('click', () => runSelftest($('tnStatus')));

function formatSelftest(d) {
  if (!d || !d.ok) return `✗ ${d?.error || '未知错误'}`;
  return `✓ ${d.base} · 延迟 ${d.latency_ms}ms · 余额 ${fmt(d.balance)} USDT（可用 ${fmt(d.available)}）`
    + ` · 精度 tick ${d.filters.tick} / step ${d.filters.step}`;
}

async function runSelftest(el) {
  const prev = el.textContent;
  el.textContent = '自检中…';
  try {
    const d = await api('/api/exchange/test');
    el.textContent = formatSelftest(d);
    toast(d.ok ? '连接自检通过' : `自检失败：${d.error}`, d.ok ? 'ok' : 'err');
    if (d.ok) {
      $('connState').textContent = '已连接（测试网）';
      $('liveBalance').textContent = `${fmt(d.balance)} USDT`;
    }
  } catch (e) {
    el.textContent = prev;
    toast(e.message, 'err');
  }
}

$('armBtn').addEventListener('click', async () => {
  if (blockIfAutoRunning('切换实盘')) return;
  const ex = currentExchange('live');
  const key = $('liveKey').value.trim();
  const secret = $('liveSecret').value.trim();
  const pass = ex === 'okx' ? $('livePass').value.trim() : '';
  try {
    // 主网面板里填了 Key 就先落盘（只写 .env + 自检，不联网下单）
    if (key && secret) {
      const d = await api('/api/exchange/keys', 'POST', { exchange: ex, mode: 'live', key, secret, passphrase: pass });
      if (!d.ok) { toast(`Key 校验失败：${d.error || '未知错误'}`, 'err'); return; }
    }
    await api('/api/trading/arm', 'POST', {
      mode: 'live', exchange: ex,
      confirm: $('confirmText').value,
      testnet: $('testnetChk').checked,
    });
    toast(`${ex === 'okx' ? 'OKX' : '币安'}主网实盘已解锁（重启服务会自动回到模拟）`, 'ok');
    closeModePanels();
    $('confirmText').value = ''; $('liveKey').value = ''; $('liveSecret').value = ''; $('livePass').value = '';
    poll();
  } catch (e) { toast(`解锁失败：${e.message}`, 'err'); }
});

$('syncBtn').addEventListener('click', async () => {
  try {
    const d = await api('/api/exchange/sync', 'POST', {});
    $('connState').textContent = d.connected ? '已连接' : `未连接${d.error ? '：' + d.error : ''}`;
    if (d.account) $('liveBalance').textContent = `${fmt(d.account.balance)} USDT`;
    $('livePos').textContent = d.positions.length ? d.positions.map((p) => `${sideCn(p.side)} ${p.qty}`).join(' / ') : '无';
    toast(d.connected ? '同步成功' : `同步失败：${d.error}`, d.connected ? 'ok' : 'err');
  } catch (e) { toast(e.message, 'err'); }
});

$('autoStart').addEventListener('click', async () => {
  try {
    await api('/api/trading/auto', 'POST', {
      running: true,
      interval_sec: Number($('autoInterval').value) || 300,
      execute: $('autoExec').checked,
    });
    toast('自动循环已启动', 'ok'); poll();
  } catch (e) { toast(e.message, 'err'); }
});
$('autoStop').addEventListener('click', async () => {
  try { await api('/api/trading/auto', 'POST', { running: false }); toast('已停止', 'ok'); poll(); }
  catch (e) { toast(e.message, 'err'); }
});

$('execBtn').addEventListener('click', async () => {
  if (!confirm('确认执行当前决策的订单计划？')) return;
  try {
    const d = await api('/api/trading/execute', 'POST', {});
    toast(d.ok ? `执行成功（${d.mode}）` : `执行失败：${d.error}`, d.ok ? 'ok' : 'err');
    poll();
  } catch (e) { toast(e.message, 'err'); }
});

$('closeBtn').addEventListener('click', async () => {
  if (!confirm('确认平掉当前持仓？')) return;
  try {
    const d = await api('/api/trading/close', 'POST', {});
    toast(d.ok ? '已平仓' : `平仓失败：${d.error}`, d.ok ? 'ok' : 'err');
    poll();
  } catch (e) { toast(e.message, 'err'); }
});

$('haltBtn').addEventListener('click', async () => {
  const on = !(lastStatus && lastStatus.halt);
  if (on && !confirm('打开熔断会立刻停止自动交易，并阻止新的决策与下单。确定？')) return;
  try {
    await api('/api/killswitch', 'POST', { on });
    toast(on ? '熔断已打开' : '熔断已解除', on ? 'err' : 'ok');
    poll();
  } catch (e) { toast(e.message, 'err'); }
});

$('clearCacheBtn').addEventListener('click', () => {
  if (window.desktop) { poll(); toast('已刷新', 'ok'); return; }
  // 强制重新加载界面，绕过浏览器/代理缓存的旧版 JS/CSS
  try { window.location.reload(true); } catch (e) { window.location.reload(); }
});

$('llmSaveBtn').addEventListener('click', saveLlmSettings);
$('llmTestBtn').addEventListener('click', testLlmSettings);

/* ---------------- 桌面窗口控制（Electron） ---------------- */
if (window.desktop) {
  document.body.classList.add('desktop');
  const wc = $('winControls');
  if (wc) {
    wc.classList.remove('hidden');
    $('tbMin').addEventListener('click', () => window.desktop.minimize());
    $('tbMax').addEventListener('click', () => window.desktop.maximize());
    $('tbClose').addEventListener('click', () => window.desktop.close());
  }
}

/* ---------------- 机会信号排行榜 ---------------- */
function renderOpportunities(rows) {
  const listEl = $('oppList');
  if (!listEl) return;
  const data = rows || (lastStatus?.opportunities || []);
  if (!data.length) {
    listEl.innerHTML = '<div class="muted center">扫描中…（自动循环会周期性扫描）</div>';
    return;
  }
  const maxScore = Math.max(...data.map((r) => r.score || 0), 0.0001);
  listEl.innerHTML = data.map((r) => {
    const w = Math.max(2, Math.round(((r.score || 0) / maxScore) * 100));
    const sig = r.error ? '—' : ((r.composite || 0) >= 0 ? '看多 ' : '看空 ') + fmt(r.composite, 3);
    const cls = r.active ? 'opp-row active' : 'opp-row';
    return `<div class="${cls}">
      <span class="opp-sym">${esc(r.symbol)}${r.active ? ' ●' : ''}</span>
      <span class="opp-sig muted">${sig}</span>
      <div class="opp-bar-track"><div class="opp-bar" style="width:${w}%"></div></div>
      <span class="opp-score">${r.error ? '—' : fmt(r.score, 3)}</span>
    </div>`;
  }).join('');

  if ($('rotateToggle')) $('rotateToggle').checked = !!lastStatus?.auto_rotate;
  if ($('rotateMinScore')) $('rotateMinScore').value = lastStatus?.rotate_min_score ?? 0.2;
  if ($('rotateEvery')) $('rotateEvery').value = lastStatus?.rotate_every_cycles ?? 1;
}

$('rotateToggle').addEventListener('change', async () => {
  try {
    await api('/api/rotate', 'POST', { auto_rotate: $('rotateToggle').checked });
    toast($('rotateToggle').checked ? '已开启自动选品种' : '已关闭自动选品种', 'ok');
  } catch (e) { toast(e.message, 'err'); }
});
$('rotateMinScore').addEventListener('change', async () => {
  try {
    const v = parseFloat($('rotateMinScore').value);
    await api('/api/rotate', 'POST', { min_score: v });
    toast(`阈值已更新为 ${v}`, 'ok');
  } catch (e) { toast(e.message, 'err'); }
});
$('rotateEvery').addEventListener('change', async () => {
  try {
    const v = parseInt($('rotateEvery').value, 10);
    await api('/api/rotate', 'POST', { every_cycles: v });
    toast(`扫描频率已更新为每 ${v} 轮`, 'ok');
  } catch (e) { toast(e.message, 'err'); }
});
$('rotateRefresh').addEventListener('click', async () => {
  try {
    toast('正在扫描全品种…', 'ok');
    const d = await api('/api/opportunity', 'POST', { refresh: true });
    renderOpportunities(d.rows);
    toast(`扫描完成，共 ${d.rows.length} 个品种`, 'ok');
  } catch (e) { toast(e.message, 'err'); }
});

window.addEventListener('resize', () => { renderPnl(); });
$('calPrev').addEventListener('click', () => shiftMonth(-1));
$('calNext').addEventListener('click', () => shiftMonth(1));

/* ---------------- 启动 ---------------- */
(async function init() {
  await loadSymbols();
  const hash = location.hash.replace('#', '');
  if (['overview', 'decision', 'research', 'trading', 'settings'].includes(hash)) showTab(hash, false);
  await poll();
  try {
    const r = await api('/api/research/latest');
    if (r) renderResearch({ ...r, rows: r.rows || [] });
  } catch (_) { /* 首次运行没有历史，忽略 */ }
  try {
    const d = await api('/api/decisions/latest');
    if (d && d.analysts) renderDecision(d, false);
  } catch (_) { /* 同上 */ }
  try {
    const sc = await api('/api/scan');
    if (sc && sc.rows) renderScan(sc);
  } catch (_) { /* 还没扫描过 */ }
  setInterval(poll, 8000);          // 状态轮询降到 8 秒，别把币安签名接口刷爆（429/418 封 IP）
})();