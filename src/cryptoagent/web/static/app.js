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
// 日历格子的窄屏版本：手机上一格只有约 42px，必须去掉千分位并按量级降精度，
// 否则 "+1,024.55" 会把所在列撑宽，同一行 7 个格子就变成七种宽度
const fmtCalShort = (n) => {
  const a = Math.abs(Number(n) || 0);
  return `${n >= 0 ? '+' : '-'}${a.toFixed(a >= 100 ? 0 : 1)}`;
};
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

// opts.sticky = true：常驻不自动消失，直到下一次 toast() 顶掉它（用于「正在测试…」这类长任务）
function toast(msg, type = '', opts = {}) {
  const el = $('toast');
  const sticky = !!opts.sticky;
  // 已经在常驻态又来了新的常驻文案（例如每秒刷新秒数）：只改文字，
  // 不要重建 spinner，否则转圈动画每秒重置一次，看着像卡住。
  if (sticky && el.classList.contains('sticky')) {
    const txt = el.querySelector('.toast-text');
    if (txt) {
      txt.textContent = msg;
      el.className = `toast show ${type} sticky`;
      return;
    }
  }
  el.innerHTML = sticky
    ? `<span class="toast-spinner"></span><span class="toast-text">${esc(msg)}</span>`
    : esc(msg);
  el.className = `toast show ${type}${sticky ? ' sticky' : ''}`;
  clearTimeout(toastTimer);
  toastTimer = null;
  if (!sticky) toastTimer = setTimeout(() => { el.className = 'toast'; }, 4200);
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
  if (name === 'overview') { renderPnl(); fitNotifHeight(); }
  if (name === 'settings') { loadLlmSettings(); loadSentimentSettings(); loadNotifySettings(); }
}

document.querySelectorAll('.tab').forEach((btn) => {
  btn.addEventListener('click', () => showTab(btn.dataset.tab));
});

// 顶部导航横向滚动时，两端加渐隐遮罩，避免硬切半个图标（仅移动端生效，类由 CSS 媒体查询限定）
(function () {
  const nav = document.querySelector('.side-scroll');
  if (!nav) return;
  const updateNavMask = () => {
    const max = nav.scrollWidth - nav.clientWidth;
    nav.classList.toggle('nav-more', nav.scrollLeft < max - 1);
    nav.classList.toggle('nav-prev', nav.scrollLeft > 1);
  };
  nav.addEventListener('scroll', updateNavMask, { passive: true });
  window.addEventListener('resize', updateNavMask);
  // 切 tab 后若激活项被遮在边缘，滚到可见并刷新遮罩
  document.querySelectorAll('.tab').forEach((btn) => btn.addEventListener('click', () => {
    requestAnimationFrame(() => {
      const active = nav.querySelector('.tab.active');
      if (active) active.scrollIntoView({ block: 'nearest', inline: 'nearest' });
      updateNavMask();
    });
  }));
  updateNavMask();
})();

// 多仓时点总览的「持仓」卡 → 跳到交易页并定位到持仓表。
// 单仓不响应：那一档已经把方向和开仓价都写在卡上了，没有「明细」可看，
// 让整张卡可点反而会变成误触。
document.getElementById('posCard')?.addEventListener('click', () => {
  const list = (lastStatus && Array.isArray(lastStatus.positions)) ? lastStatus.positions : [];
  if (list.length < 2) return;
  showTab('trading');
  const tbl = document.getElementById('posTable');
  if (!tbl) return;
  // 等一帧再滚：panel 刚从 display:none 变 block，同一帧里量到的位置不准
  requestAnimationFrame(() => {
    tbl.scrollIntoView({ behavior: 'smooth', block: 'center' });
    tbl.classList.remove('flash');
    void tbl.offsetWidth;          // 强制重排，否则同一张表连续点两次不会重放动画
    tbl.classList.add('flash');
    setTimeout(() => tbl.classList.remove('flash'), 1800);
  });
});

/* ---------------- 状态轮询 ---------------- */
async function poll() {
  try {
    const s = await api('/api/status');
    lastStatus = s;
    renderStatus(s);
    renderPositionManagement(s && s.data);
  } catch (e) {
    console.error(e);
  }
  try {
    renderNotifications();
  } catch (e) {
    console.error(e);
  }
}

/* A2：通知 / 告警面板 */
async function renderNotifications() {
  const d = await api('/api/notifications', 'POST', {});
  const items = (d && d.items) || [];
  const chs = (d && d.channels) || [];
  const chEl = $('notifChannels');
  if (chEl) chEl.textContent = '渠道：' + chs.join(' / ');
  const el = $('notifList');
  if (!el) return;
  if (!items.length) {
    if (el.dataset.sig !== '') {
      el.dataset.sig = '';
      el.innerHTML = '<div class="muted center">暂无通知。风控拦截、平仓盈亏、熔断都会推送到这里（也可配置 Telegram / 邮件）。</div>';
    }
    fitNotifHeight();
    return;
  }
  // 3 秒一次轮询，内容没变就不要重建 DOM：innerHTML 一清空，列表的滚动位置就被顶回顶部，
  // 用户正在翻历史消息时会一直被拽回去。
  const sig = items.map((n) => `${n.ts}|${n.level}|${n.title}|${n.message || ''}`).join('~');
  if (el.dataset.sig === sig) { fitNotifHeight(); return; }
  el.dataset.sig = sig;
  const keepTop = el.scrollTop;
  const lvlCls = { critical: 'down', warn: 'amber', info: 'muted' };
  el.innerHTML = items.slice().reverse().map((n) => {
    const c = lvlCls[n.level] || 'muted';
    return `<div class="notif-item">
      <span class="notif-lvl ${c}">${esc(n.level)}</span>
      <div class="notif-body">
        <div class="notif-title">${esc(n.title)}</div>
        <div class="notif-msg">${esc(n.message)}</div>
      </div>
      <span class="notif-ts muted">${esc((n.ts || '').slice(5))}</span>
    </div>`;
  }).join('');
  el.scrollTop = keepTop;
  fitNotifHeight();
}

/* 通知卡片定高：底端与左侧「盈亏」卡片对齐，超出的消息在卡片内部滚动。
   不用固定像素——左侧卡片高度随成交条数、日历行数变化，这里用「机会榜底端 − 盈亏卡底端」
   的差值反推列表还能用多高：差值为正说明已经探出左卡底部，按超出量缩；
   为负说明还有空间，放到内容自然高度为止（所以消息少时卡片不会凭空变高）。
   scrollHeight 必须先把 max-height 摘掉再读，否则它会被 clientHeight 顶住、永远缩不回去；
   读完立刻写回，同一帧内完成，用户看不到跳动，滚动位置也会还原。 */
const NOTIF_MIN_H = 120;   // 再挤也留一条消息的高度

function fitNotifHeight() {
  const list = $('notifList');
  const card = list && list.closest('.notif-card');
  const board = document.querySelector('.opp-panel');
  const left = document.querySelector('.pnl-card');
  if (!list || !card || !board || !left) return;
  // ≤1180px 时总览是单列、机会榜另起一行，「对齐左卡底部」不成立，交还给自然高度
  if (window.matchMedia('(max-width: 1180px)').matches) {
    list.style.maxHeight = '';
    return;
  }
  // 切到别的页签时元素 display:none，rect 全是 0，算出来的高度会把卡片压扁
  if (!card.offsetParent) return;
  const cur = list.getBoundingClientRect().height;
  if (!cur) return;
  const overflow = board.getBoundingClientRect().bottom - left.getBoundingClientRect().bottom;
  const keepTop = list.scrollTop;
  list.style.maxHeight = 'none';
  const natural = list.scrollHeight;
  const height = Math.max(NOTIF_MIN_H, Math.min(natural, cur - overflow));
  list.style.maxHeight = `${Math.round(height)}px`;
  list.scrollTop = keepTop;   // 摘掉 max-height 时会被夹成 0，这里还原
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
    $('llmPeakMode').value = d.peak_mode || 'full';
    $('llmOffpeakResearch').checked = d.offpeak_research_only !== false;
    $('llmPeakHolidays').value = (d.peak_holidays || []).join(',');
    const op = d.offpeak || {};
    $('llmOffpeak').textContent = op.label || '--';
    $('llmPeakHint').textContent = op.peak_windows || '';
    $('llmStatus').textContent = [`当前 ${d.provider}`, d.model, op.label].filter(Boolean).join(' · ');
  } catch (e) { console.error(e); }
}

async function saveLlmSettings(quiet = false) {
  const payload = {
    provider: $('llmProvider').value,
    model: $('llmModel').value.trim(),
    base_url: $('llmBaseUrl').value.trim(),
    offpeak_research_only: $('llmOffpeakResearch').checked,
    peak_mode: $('llmPeakMode').value,
    peak_holidays: $('llmPeakHolidays').value.trim(),
  };
  const key = $('llmApiKey').value.trim();
  if (key) payload.api_key = key;
  try {
    await api('/api/settings/llm', 'POST', payload);
    if (!quiet) toast('LLM 设置已保存', 'ok');
    $('llmApiKey').value = '';
    loadLlmSettings();
    poll();
    return true;
  } catch (e) {
    toast(e.message, 'err');
    return false;
  }
}

/* ---------------- A1 情绪 / 新闻源设置 ---------------- */
async function loadSentimentSettings() {
  try {
    const d = await api('/api/settings/sentiment');
    $('sentimentEnabled').checked = !!d.enabled;
    $('sentimentSource').value = d.source || 'price_action';
    $('sentimentApiKey').value = '';
    $('sentimentApiKey').placeholder = d.cryptopanic_key_set
      ? `已设置（尾号 ${d.cryptopanic_key_tail}），留空保持原值`
      : '留空则不修改';
    $('sentimentStatus').textContent = `当前 ${d.source === 'cryptopanic' ? 'CryptoPanic' : '价格行为代理'}${d.enabled ? '' : '（已关闭）'}`;
    toggleCpKeyRow();
  } catch (e) { console.error(e); }
}

function toggleCpKeyRow() {
  const row = $('cpKeyRow');
  if (!row) return;
  row.style.display = $('sentimentSource').value === 'cryptopanic' ? '' : 'none';
}

async function saveSentimentSettings(quiet = false) {
  const payload = {
    enabled: $('sentimentEnabled').checked,
    source: $('sentimentSource').value,
  };
  const key = $('sentimentApiKey').value.trim();
  if (key) payload.cryptopanic_api_key = key;
  try {
    await api('/api/settings/sentiment', 'POST', payload);
    if (!quiet) toast('情绪 / 新闻源设置已保存', 'ok');
    $('sentimentApiKey').value = '';
    loadSentimentSettings();
    return true;
  } catch (e) {
    toast(e.message, 'err');
    return false;
  }
}

/* ---------------- A2 通知 / 告警设置 ---------------- */
async function loadNotifySettings() {
  try {
    const d = await api('/api/settings/notify');
    $('notifyEnabled').checked = !!d.enabled;
    const chs = d.channels || [];
    document.querySelectorAll('#notifyChannels input[type="checkbox"]').forEach((cb) => {
      cb.checked = chs.includes(cb.value);
    });
    $('notifyWebhook').value = '';
    $('notifyTgToken').value = '';
    $('notifyTgChat').value = '';
    $('notifySmtpHost').value = '';
    $('notifySmtpPort').value = '';
    $('notifySmtpUser').value = '';
    $('notifySmtpPass').value = '';
    $('notifySmtpTo').value = '';
    const setHint = (id, ok, tail, label) => {
      const el = $(id);
      if (!el) return;
      el.placeholder = ok ? `${label}已配置（尾号 ${tail}）` : `${label}未配置`;
    };
    setHint('notifyWebhook', d.webhook_set, '', 'Webhook URL');
    setHint('notifyTgToken', d.telegram_set, '', 'Telegram');
    setHint('notifySmtpHost', d.email_set, '', 'SMTP');
    $('notifyStatus').textContent = `当前通道：${(chs.length ? chs : ['无']).join(' / ')}${d.enabled ? '' : '（主开关已关）'}`;
  } catch (e) { console.error(e); }
}

async function saveNotifySettings(quiet = false) {
  const channels = [];
  document.querySelectorAll('#notifyChannels input[type="checkbox"]').forEach((cb) => {
    if (cb.checked) channels.push(cb.value);
  });
  const payload = {
    enabled: $('notifyEnabled').checked,
    channels,
    webhook_url: $('notifyWebhook').value.trim(),
    telegram_token: $('notifyTgToken').value.trim(),
    telegram_chat: $('notifyTgChat').value.trim(),
    email_smtp_host: $('notifySmtpHost').value.trim(),
    email_smtp_port: $('notifySmtpPort').value.trim(),
    email_user: $('notifySmtpUser').value.trim(),
    email_pass: $('notifySmtpPass').value.trim(),
    email_to: $('notifySmtpTo').value.trim(),
  };
  try {
    await api('/api/settings/notify', 'POST', payload);
    if (!quiet) toast('通知 / 告警设置已保存', 'ok');
    loadNotifySettings();
    return true;
  } catch (e) {
    toast(e.message, 'err');
    return false;
  }
}

async function testLlmSettings() {
  const btn = $('llmTestBtn');
  const label = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '测试中…'; }

  // 长任务提示：常驻 + 计时，测试结束（成功或失败）才被结果 toast 顶掉
  let secs = 0;
  const showProgress = () => {
    toast(`正在跑一次决策验证 LLM…${secs ? `（已 ${secs}s）` : ''}`, 'ok', { sticky: true });
  };

  try {
    // 先保存配置；保存失败就不要拿旧配置去测
    if (!(await saveLlmSettings(true))) return;
    showProgress();
    const timer = setInterval(() => { secs += 1; showProgress(); }, 1000);
    try {
      const d = await api('/api/decision', 'POST', { rounds: 1 });
      const degraded = d && d.degraded;
      const spent = secs >= 1 ? `（用时 ${secs}s）` : '';
      toast(
        degraded ? `LLM 调用有降级（限流/失败），规则已兜底${spent}` : `LLM 正常生效${spent}`,
        degraded ? 'err' : 'ok'
      );
    } finally {
      clearInterval(timer);
    }
  } catch (e) {
    toast('测试失败：' + e.message, 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
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
      const star = x.pinned ? '★ ' : '';
      return `<option value="${esc(x.symbol)}">${star}${esc(prettySymbol(x.symbol))}${mark}</option>`;
    }).join('');
    const active = list.find((x) => x.active);
    if (active) sel.value = active.symbol;
    sel.title = list.map((x) => `${x.symbol}${x.cached ? ' 缓存到 ' + x.last : ' 未下载'}`).join(' ｜ ');
  } catch (e) { console.error(e); }
}

// BTCUSDT → BTC/USDT，BTCUSDC → BTC/USDC
function prettySymbol(sym) {
  const s = String(sym || '');
  for (const q of ['USDT', 'USDC']) {
    if (s.endsWith(q) && s.length > q.length) return `${s.slice(0, -q.length)}/${q}`;
  }
  return s;
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

/* ---------------- 交易品种管理（自主添加） ---------------- */
let symExchange = 'binance';
let symDelArmed = null;
let symDelTimer = null;
let symSearchTimer = null;
let symPollTimer = null;

function setSymExchange(ex) {
  symExchange = ex === 'okx' ? 'okx' : 'binance';
  document.querySelectorAll('#symExChoice .seg').forEach((b) => {
    b.classList.toggle('sel', b.dataset.symEx === symExchange);
  });
}

function symExLabel(ex) {
  return ex === 'okx' ? 'OKX' : '币安';
}

function symTag(x) {
  if (x.pinned) return '<span class="sym-tag pin">★ 置顶</span>';
  if (x.active) return '<span class="sym-tag cur">当前</span>';
  if (x.prefetching) return '<span class="sym-tag wait">缓存中…</span>';
  if (x.error) return `<span class="sym-tag err" title="${esc(x.error)}">拉取失败</span>`;
  if (x.cached) return `<span class="sym-tag ok">已缓存 ${esc(String(x.last || '').slice(5, 16))}</span>`;
  if (x.synthetic_only) return '<span class="sym-tag wait">合成数据</span>';
  return '<span class="sym-tag">未下载</span>';
}

function renderSymList() {
  const box = $('symList');
  if (!box) return;
  const list = symbolList || [];
  if ($('symCount')) $('symCount').textContent = `${list.length} 个`;
  if (!list.length) {
    box.innerHTML = '<div class="muted center">还没有品种</div>';
    return;
  }
  box.innerHTML = list.map((x) => {
    const armed = symDelArmed === x.symbol;
    return `<div class="sym-row">
      <span class="sym-name">${esc(x.symbol)}</span>
      ${symTag(x)}
      ${(x.exchanges || []).length ? `<span class="sym-ex">${(x.exchanges || []).map(symExLabel).join(' / ')}</span>` : ''}
      <span class="sym-actions">
        <button class="btn ghost sm" data-sym-pin="${esc(x.symbol)}" data-pin-to="${x.pinned ? '0' : '1'}" title="${x.pinned ? '取消置顶' : '置顶到最前'}">${x.pinned ? '取消置顶' : '置顶'}</button>
        ${x.active ? '<span class="muted">使用中</span>' : `
        <button class="btn ghost sm" data-sym-switch="${esc(x.symbol)}">切换</button>
        <button class="btn ghost sm${armed ? ' danger' : ''}" data-sym-del="${esc(x.symbol)}">${armed ? '确认删除' : '删除'}</button>`}</span>
    </div>`;
  }).join('');
}

function startSymPoll() {
  clearInterval(symPollTimer);
  symPollTimer = setInterval(async () => {
    if (!$('symModal') || $('symModal').classList.contains('hidden')) {
      clearInterval(symPollTimer);
      symPollTimer = null;
      return;
    }
    try {
      await loadSymbols();
      renderSymList();
      if (!(symbolList || []).some((x) => x.prefetching)) {
        clearInterval(symPollTimer);
        symPollTimer = null;
      }
    } catch (e) { /* 网络抖动忽略，下次再试 */ }
  }, 2500);
}

async function openSymModal() {
  if (!$('symModal')) return;
  $('symModal').classList.remove('hidden');
  $('symMsg').textContent = '';
  $('symInput').value = '';
  $('symSuggest').classList.add('hidden');
  setSymExchange((lastStatus && lastStatus.exchange) || 'binance');
  renderSymList();
  setTimeout(() => $('symInput').focus(), 60);
  try {
    await loadSymbols();
    renderSymList();
    if ((symbolList || []).some((x) => x.prefetching)) startSymPoll();
  } catch (e) { /* 列表拉不到就先显示缓存 */ }
}

function closeSymModal() {
  if (!$('symModal')) return;
  $('symModal').classList.add('hidden');
  symDelArmed = null;
  clearInterval(symPollTimer);
  symPollTimer = null;
}

async function loadSymSuggest(q) {
  const box = $('symSuggest');
  const key = (q || '').trim();
  if (!key) {
    box.classList.add('hidden');
    box.innerHTML = '';
    return;
  }
  try {
    const list = await api('/api/symbols/candidates?q=' + encodeURIComponent(key));
    if (!list.length) {
      box.innerHTML = '<span class="muted">交易所里没匹配到，可以直接点「添加」试一下</span>';
    } else {
      box.innerHTML = list.slice(0, 12).map((c) => {
        const ex = (c.exchanges || []).join(' / ');
        return `<button class="sym-chip" data-sym-pick="${esc(c.symbol)}">${esc(c.symbol)}${ex ? `<span class="chip-ex">${esc(ex)}</span>` : ''}</button>`;
      }).join('');
    }
    box.classList.remove('hidden');
  } catch (e) {
    box.classList.add('hidden');
  }
}

async function addSymbolFromModal() {
  const raw = ($('symInput').value || '').trim();
  if (!raw) {
    toast('先填一个品种代码，例如 SOLUSDT', 'err');
    return;
  }
  const btn = $('symAddBtn');
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = '校验中…';
  toast(`正在向${symExLabel(symExchange)}校验 ${raw.toUpperCase()} 的永续合约…`, 'ok', { sticky: true });
  try {
    const d = await api('/api/symbols/add', 'POST', { symbol: raw, exchange: symExchange });
    symbolList = d.symbols || symbolList;
    renderSymList();
    await loadSymbols();
    renderSymList();
    $('symInput').value = '';
    $('symSuggest').classList.add('hidden');
    if (!d.added) {
      toast(`${d.symbol} 已经在列表里了`, 'ok');
      return;
    }
    const note = d.note ? `；${d.note}` : '';
    toast(
      `${d.symbol} 已添加到${symExLabel(d.exchange || symExchange)}（${fmtPx(d.price)}）`
        + `${d.saved ? '' : '，配置只读、重启会丢'}，正在预下载历史…${note}`,
      note ? 'err' : 'ok'
    );
    startSymPoll();
  } catch (e) {
    toast(`添加失败：${e.message}`, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

async function toggleSymbolPin(sym, pinned) {
  try {
    const d = await api('/api/symbols/pin', 'POST', { symbol: sym, pinned });
    symbolList = d.symbols || symbolList;
    await loadSymbols();
    renderSymList();
    toast(pinned ? `${sym} 已置顶，下拉框里也排到最前` : `${sym} 已取消置顶`, 'ok');
  } catch (e) {
    toast(`${pinned ? '置顶' : '取消置顶'}失败：${e.message}`, 'err');
  }
}

async function switchSymbolTo(sym) {
  try {
    const d = await api('/api/symbol', 'POST', { symbol: sym });
    toast(d.changed ? `已切到 ${sym}（${fmt(d.price)}）` : `${sym} 已是当前品种`, 'ok');
    await loadSymbols();
    renderSymList();
    await poll();
    closeSymModal();
  } catch (e) {
    toast(`切换失败：${e.message}`, 'err');
  }
}

async function removeSymbolFlow(sym) {
  // 两次点击确认，避免误删（不用原生 confirm，样式统一）
  if (symDelArmed !== sym) {
    symDelArmed = sym;
    renderSymList();
    clearTimeout(symDelTimer);
    symDelTimer = setTimeout(() => {
      if (symDelArmed === sym) {
        symDelArmed = null;
        renderSymList();
      }
    }, 4000);
    return;
  }
  symDelArmed = null;
  clearTimeout(symDelTimer);
  try {
    const d = await api('/api/symbols/remove', 'POST', { symbol: sym });
    symbolList = d.symbols || symbolList;
    await loadSymbols();
    renderSymList();
    toast(`${sym} 已从列表移除${d.saved ? '' : '（配置只读，重启会回来）'}`, 'ok');
  } catch (e) {
    toast(e.message, 'err');
    renderSymList();
  }
}

if ($('symbolAddBtn')) {
  $('symbolAddBtn').addEventListener('click', openSymModal);
  document.querySelectorAll('#symExChoice .seg').forEach((b) => {
    b.addEventListener('click', () => setSymExchange(b.dataset.symEx));
  });
  $('symClose').addEventListener('click', closeSymModal);
  $('symAddBtn').addEventListener('click', addSymbolFromModal);
  $('symInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      addSymbolFromModal();
    }
  });
  $('symInput').addEventListener('input', () => {
    clearTimeout(symSearchTimer);
    const q = $('symInput').value;
    symSearchTimer = setTimeout(() => loadSymSuggest(q), 260);
  });
  $('symSuggest').addEventListener('click', (e) => {
    const chip = e.target.closest('[data-sym-pick]');
    if (!chip) return;
    $('symInput').value = chip.dataset.symPick;
    $('symSuggest').classList.add('hidden');
    $('symInput').focus();
  });
  $('symList').addEventListener('click', (e) => {
    const pin = e.target.closest('[data-sym-pin]');
    if (pin) {
      toggleSymbolPin(pin.dataset.symPin, pin.dataset.pinTo === '1');
      return;
    }
    const sw = e.target.closest('[data-sym-switch]');
    if (sw) {
      switchSymbolTo(sw.dataset.symSwitch);
      return;
    }
    const del = e.target.closest('[data-sym-del]');
    if (del) removeSymbolFlow(del.dataset.symDel);
  });
  $('symModal').addEventListener('click', (e) => {
    if (e.target === $('symModal')) closeSymModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('symModal').classList.contains('hidden')) closeSymModal();
  });
}

function factorSigHtml(p) {
  const c = p.factor_composite;
  if (c === undefined || c === null) return '';
  const active = (p.factor_signals || []).filter((s) => s.weight > 0);
  const names = active.map((s) => `${s.name}(${s.target >= 0 ? '+' : ''}${s.target.toFixed(2)})`).join('、') || '无（全部被留出集淘汰）';
  const cls = c > 0.05 ? 'up' : c < -0.05 ? 'down' : 'muted';
  const dir = c > 0.05 ? '偏多' : c < -0.05 ? '偏空' : '中性';
  // B4：因子贡献度条形——哪些因子真正在「贡献信号」
  let bars = '';
  const contribs = (p.factor_contributions || []).filter((x) => x.weight > 0)
    .sort((a, b) => Math.abs(b.share) - Math.abs(a.share)).slice(0, 8);
  if (contribs.length) {
    const maxAbs = Math.max(...contribs.map((x) => Math.abs(x.share)), 0.0001);
    bars = `<div class="factor-bars">`
      + contribs.map((x) => {
        const w = (Math.abs(x.share) / maxAbs) * 100;
        const d = x.share >= 0 ? 'up' : 'down';
        return `<div class="fb-row">
          <span class="fb-name">${esc(x.name)}</span>
          <span class="fb-track"><span class="fb-fill ${d}" style="width:${w.toFixed(1)}%"></span></span>
          <span class="fb-val ${d}">${x.share >= 0 ? '+' : ''}${(x.share * 100).toFixed(1)}%</span>
        </div>`;
      }).join('')
      + `</div>`;
  }
  return `<div class="thesis">因子信号 <b class="${cls}">${c >= 0 ? '+' : ''}${Number(c).toFixed(2)}</b>（${dir}）
    · ${esc(names)}。留出集为负的因子已自动淘汰，不参与决策。</div>${bars}`;
}

function sizingTraceHtml(p) {
  const t = p.sizing_trace || [];
  if (!t.length) return '';
  const rows = t.map((x) => `<div class="st-row">
      <span>${esc(x.step)}</span>
      <b>${(Number(x.size_pct || 0) * 100).toFixed(2)}%</b>
      <span class="muted">${esc(x.detail || '')}</span>
    </div>`).join('');
  return `<div class="sizing-trace"><b>仓位归因</b>${rows}</div>`;
}

function costGateHtml(p) {
  const g = p.cost_gate;
  if (!g) return '';
  const cls = g.approved ? 'up' : 'down';
  return `<div class="cost-gate">执行成本闸 <b class="${cls}">${g.approved ? '通过' : '拒绝'}</b>
    · 成本 ${(Number(g.cost_pct || 0) * 100).toFixed(3)}% / 止盈 ${(Number(g.expected_pct || 0) * 100).toFixed(2)}%
    · 净边际 ${(Number(g.net_edge_pct || 0) * 100).toFixed(2)}%
    · 参与率 ${(Number(g.participation || 0) * 100).toFixed(2)}%
    <div class="muted">${esc((g.reasons || []).join('；'))}</div></div>`;
}

function renderPositionManagement(d) {
  const card = $('posMgmtCard');
  if (!card) return;
  const m = d && d.position_management;
  const has = m && m.action && m.action !== 'hold';
  card.classList.toggle('hidden', !has);
  if (!has) return;
  const cls = m.action === 'close' ? 'down' : 'up';
  $('posMgmtBody').innerHTML = `<p>最近一次判断：<b class="${cls}">${esc(m.action)}</b>
    · ${esc(m.reason || '')}</p>
    ${m.new_stop ? `<p class="muted">新止损 ${fmt(m.new_stop)}</p>` : ''}
    ${m.explanation ? `<p class="muted">${esc(m.explanation)}</p>` : ''}
    <p class="muted">守护线程${(d.position_guard || {}).running ? '运行中' : '未运行'}，
    每 ${(d.position_guard || {}).interval_sec || 30} 秒检查一次，不依赖自动交易循环。</p>`;
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

// 服务端当前生效的三个值。校验与回滚都以它为基准：
// change 事件触发时输入框里已经是用户改后的值，不能拿来当「原值」。
let riskLast = { pos: 0, slots: 1, margin: 0 };

function marginRuleMsg(posPct, slots, marginPct) {
  const need = Math.round(posPct * slots * 100) / 100;
  if (!(need > marginPct + 1e-6)) return '';
  const fix = Math.ceil(need * 10) / 10;
  return `单笔仓位上限 ${posPct}% × ${slots} 个品种 = ${need}% 权益，`
    + `超过保证金占用上限 ${marginPct}%。请先把「保证金占用上限」提到 ${fix}% 以上`;
}

function renderRisk(r) {
  if (!r) return;
  const ov = Number(r.leverage_override || 0);
  const sel = $('levSelect');
  if (sel && document.activeElement !== sel) sel.value = String(ov);
  const cap = (Number(r.max_loss_per_trade_pct) * 100).toFixed(1);
  const rt = $('riskTotal');
  if (rt && document.activeElement !== rt) rt.value = cap;
  const rpc = $('riskPosCapInput');
  if (rpc && document.activeElement !== rpc) rpc.value = String(Math.round(Number(r.max_position_pct) * 1000) / 10);
  const rd = $('riskDaily');
  if (rd && document.activeElement !== rd) rd.value = (Number(r.daily_loss_limit_pct) * 100).toFixed(1);
  const rdd = $('riskDrawdown');
  if (rdd && document.activeElement !== rdd) rdd.value = String(Math.round(Number(r.max_drawdown_pct) * 1000) / 10);
  const sm = $('riskSizingMode');
  if (sm && document.activeElement !== sm) sm.value = r.sizing_mode || 'llm';
  const rmlv = $('riskMaxLev');
  if (rmlv && document.activeElement !== rmlv) rmlv.value = Number(r.max_leverage || 20).toFixed(0);
  const rml = $('riskMinLev');
  if (rml && document.activeElement !== rml) rml.value = Number(r.min_leverage || 1).toFixed(0);
  const rport = $('riskPortfolioCap');
  if (rport && document.activeElement !== rport) rport.value = Number(r.max_portfolio_notional_pct || 1.2).toFixed(1);
  const rcorr = $('riskCorrCap');
  if (rcorr && document.activeElement !== rcorr) rcorr.value = Number(r.correlated_exposure_pct || 0.9).toFixed(1);
  // 多品种并发：品种数上限 + 保证金占用上限
  const rmp = $('riskMaxPos');
  if (rmp && document.activeElement !== rmp) rmp.value = String(Number(r.max_concurrent_positions || 1));
  const rmc = $('riskMarginCap');
  if (rmc && document.activeElement !== rmc) {
    rmc.value = String(Math.round(Number(r.max_margin_usage_pct || 1) * 100));
  }
  renderMarginRule(r);
}

// 把「单笔仓位 × 品种数 vs 保证金合计」的关系摆在设置项下面：
// 合规时灰字说明能开满，超限时整条变红并写清要提到多少。
function renderMarginRule(r) {
  const box = $('riskMarginRule');
  const txt = $('riskMarginRuleText');
  if (!box || !txt) return;
  const pos = Math.round(Number(r.max_position_pct || 0) * 1000) / 10;
  const slots = Math.max(1, Math.round(Number(r.max_concurrent_positions || 1)));
  const margin = Math.round(Number(r.max_margin_usage_pct || 0) * 1000) / 10;
  const bad = !!marginRuleMsg(pos, slots, margin);
  const need = Math.round(pos * slots * 100) / 100;
  riskLast = { pos, slots, margin };
  box.classList.toggle('bad', bad);
  box.querySelector('.rl-ico').textContent = bad ? '!' : '✓';
  txt.innerHTML = bad
    ? `单笔仓位 <b>${pos}%</b> × <b>${slots}</b> 个品种 = <b>${need}%</b> 权益，`
      + `已超过保证金占用上限 <b>${margin}%</b>，需先把合计上限提到 <b>${Math.ceil(need * 10) / 10}%</b>`
    : `单笔仓位 <b>${pos}%</b> × <b>${slots}</b> 个品种 = <b>${need}%</b> 权益 `
      + `≤ 保证金占用上限 <b>${margin}%</b>，按仓位上限可以开满`;
}

// 风控保存后的非阻塞提醒：硬顶由用户自主设定，系统只提示、不改值
function riskWarn(d) {
  const w = (d && d.warnings) || [];
  if (!w.length) return;
  setTimeout(() => toast(w[0], 'err'), 1400);
}

$('levSelect').addEventListener('change', async () => {
  const v = Number($('levSelect').value);
  try {
    const d = await api('/api/risk', 'POST', { leverage_override: v });
    renderRisk(d.risk); riskWarn(d);
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
  if (!v || v < 0.1 || v > 100) { toast('单笔风险上限需在 0.1% ~ 100% 之间', 'err'); poll(); return; }
  try {
    const d = await api('/api/risk', 'POST', { max_loss_per_trade_pct: v / 100 });
    renderRisk(d.risk); riskWarn(d);
    toast(`单笔风险上限已设为 ${v}%`, 'ok');
  } catch (e) { toast(`设置失败：${e.message}`, 'err'); poll(); }
});

$('riskPosCapInput').addEventListener('change', async () => {
  const v = Number($('riskPosCapInput').value);
  if (!v || v < 0.1 || v > 100) { toast('单笔仓位上限需在 0.1% ~ 100% 之间', 'err'); poll(); return; }
  const blocked = marginRuleMsg(v, riskLast.slots, riskLast.margin);
  if (blocked) {
    $('riskPosCapInput').value = String(riskLast.pos);     // 立即回滚，别等轮询
    toast(blocked, 'err');
    return;
  }
  try {
    const d = await api('/api/risk', 'POST', { max_position_pct: v / 100 });
    renderRisk(d.risk); riskWarn(d);
    toast(`单笔仓位上限已设为 ${v}%`, 'ok');
  } catch (e) { toast(`设置失败：${e.message}`, 'err'); poll(); }
});

$('riskMaxPos').addEventListener('change', async () => {
  const v = Math.round(Number($('riskMaxPos').value));
  if (!v || v < 1 || v > 20) { toast('同时持仓品种数需在 1 ~ 20 之间', 'err'); poll(); return; }
  const blocked = marginRuleMsg(riskLast.pos, v, riskLast.margin);
  if (blocked) {
    $('riskMaxPos').value = String(riskLast.slots);
    toast(blocked, 'err');
    return;
  }
  try {
    const d = await api('/api/risk', 'POST', { max_concurrent_positions: v });
    renderRisk(d.risk); riskWarn(d);
    toast(v === 1 ? '已设为单品种持仓' : `最多同时持有 ${v} 个品种`, 'ok');
  } catch (e) { toast(`设置失败：${e.message}`, 'err'); poll(); }
});

$('riskMarginCap').addEventListener('change', async () => {
  const v = Number($('riskMarginCap').value);
  if (!v || v < 1 || v > 100) { toast('保证金占用上限需在 1% ~ 100% 之间', 'err'); poll(); return; }
  const blocked = marginRuleMsg(riskLast.pos, riskLast.slots, v);
  if (blocked) {
    $('riskMarginCap').value = String(riskLast.margin);
    toast(blocked, 'err');
    return;
  }
  try {
    const d = await api('/api/risk', 'POST', { max_margin_usage_pct: v / 100 });
    renderRisk(d.risk); riskWarn(d);
    toast(`保证金占用上限已设为 ${v}% 权益`, 'ok');
  } catch (e) { toast(`设置失败：${e.message}`, 'err'); poll(); }
});

$('riskDaily').addEventListener('change', async () => {
  const v = Number($('riskDaily').value);
  if (!v || v < 0.1 || v > 100) { toast('当日亏损上限需在 0.1% ~ 100% 之间', 'err'); poll(); return; }
  try {
    const d = await api('/api/risk', 'POST', { daily_loss_limit_pct: v / 100 });
    renderRisk(d.risk); riskWarn(d);
    toast(`当日亏损上限已设为 ${v}%`, 'ok');
  } catch (e) { toast(`设置失败：${e.message}`, 'err'); poll(); }
});

$('riskDrawdown').addEventListener('change', async () => {
  const v = Number($('riskDrawdown').value);
  if (!v || v < 0.1 || v > 100) { toast('最大回撤需在 0.1% ~ 100% 之间', 'err'); poll(); return; }
  try {
    const d = await api('/api/risk', 'POST', { max_drawdown_pct: v / 100 });
    renderRisk(d.risk); riskWarn(d);
    toast(`最大回撤已设为 ${v}%`, 'ok');
  } catch (e) { toast(`设置失败：${e.message}`, 'err'); poll(); }
});

$('riskPortfolioCap').addEventListener('change', async () => {
  const v = Number($('riskPortfolioCap').value);
  if (!v || v < 0.1 || v > 100) { toast('组合名义敞口上限需在 0.1 ~ 100 倍权益之间', 'err'); poll(); return; }
  try {
    const d = await api('/api/risk', 'POST', { max_portfolio_notional_pct: v });
    renderRisk(d.risk); riskWarn(d);
    toast(`组合名义敞口上限已设为 ${v}x 权益`, 'ok');
  } catch (e) { toast(`设置失败：${e.message}`, 'err'); poll(); }
});

$('riskCorrCap').addEventListener('change', async () => {
  const v = Number($('riskCorrCap').value);
  if (!v || v < 0.1 || v > 100) { toast('相关组敞口上限需在 0.1 ~ 100 倍权益之间', 'err'); poll(); return; }
  try {
    const d = await api('/api/risk', 'POST', { correlated_exposure_pct: v });
    renderRisk(d.risk); riskWarn(d);
    toast(`相关组敞口上限已设为 ${v}x 权益`, 'ok');
  } catch (e) { toast(`设置失败：${e.message}`, 'err'); poll(); }
});

$('riskSizingMode').addEventListener('change', async () => {
  const v = $('riskSizingMode').value;
  try {
    const d = await api('/api/risk', 'POST', { sizing_mode: v });
    renderRisk(d.risk); riskWarn(d);
    const label = { llm: '听交易员', risk: '按风险上限反推' }[v] || v;
    toast(`仓位方式已改为「${label}」`, 'ok');
  } catch (e) { toast(`设置失败：${e.message}`, 'err'); poll(); }
});

$('riskMaxLev').addEventListener('change', async () => {
  const v = Number($('riskMaxLev').value);
  if (!v || v < 1 || v > 125) { toast('杠杆上限需在 1 ~ 125 之间', 'err'); poll(); return; }
  try {
    const d = await api('/api/risk', 'POST', { max_leverage: v });
    renderRisk(d.risk); riskWarn(d);
    toast(`杠杆上限已设为 ${v}x（实际杠杆取 min(交易所上限, 此值)）`, 'ok');
  } catch (e) { toast(`设置失败：${e.message}`, 'err'); poll(); }
});

$('riskMinLev').addEventListener('change', async () => {
  const v = Number($('riskMinLev').value);
  if (!v || v < 1 || v > 50) { toast('杠杆下限需在 1 ~ 50 之间', 'err'); poll(); return; }
  try {
    const d = await api('/api/risk', 'POST', { min_leverage: v });
    renderRisk(d.risk); riskWarn(d);
    toast(`杠杆下限已设为 ${v}x`, 'ok');
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
  if (s.server && s.server.version) $('verTag').innerHTML = `<span class="ver-v">v</span>${esc(s.server.version)}`;
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

  // 总览卡只有一行大字的位置，按持仓数量分两档，避免挤：
  //  · 单仓 —— 大字放「方向 数量」，品种名挂在标题旁（原来只有方向+数量，看不出是哪个品种）
  //  · 多仓 —— 大字放「合计浮盈亏」，品种数量挂在标题旁，明细统一去交易页看；
  //            整张卡可点，点一下直接跳到交易页的持仓表
  const posAll = Array.isArray(s.positions) ? s.positions : (s.position ? [s.position] : []);
  const posCard = $('posCard');
  const posSym = $('posSym');
  const shortSym = (v) => String(v || '').replace(/USDT$/, '');
  if (posAll.length) {
    const p = s.position || posAll[0];
    // 有仓位还没报价时合计会偏小，用 known 标记并在数字后加 *，别让残缺合计数冒充真值
    const known = posAll.every((x) => x.unrealized != null);
    const totalUnreal = posAll.reduce(
      (a, x) => a + (x.unrealized == null ? 0 : Number(x.unrealized)), 0);
    if (posAll.length === 1) {
      // 数量按量级取精度：144.4000 这种在 26px 下会把卡片撑满，
      // 144.40 / 14440 / 0.0432 才是人真正想看的位数
      const q = Math.abs(Number(p.qty) || 0);
      const qtyTxt = q >= 1000 ? fmt(p.qty, 0) : q >= 1 ? fmt(p.qty, 2) : fmt(p.qty, 4);
      $('position').textContent = `${sideCn(p.side)} ${qtyTxt}`;
      $('position').className = p.side === 'LONG' ? 'up' : 'down';
      posSym.textContent = shortSym(p.symbol || s.symbol) || '—';
    } else {
      $('position').textContent =
        `${totalUnreal >= 0 ? '+' : ''}${fmt(totalUnreal)}${known ? '' : '*'}`;
      $('position').className = totalUnreal >= 0 ? 'up' : 'down';
      posSym.textContent = `${posAll.length} 个品种`;
    }
    posSym.title = posAll.length === 1
      ? String(p.symbol || s.symbol || '')
      : posAll.map((x) => x.symbol).join(' / ');
    posSym.classList.remove('hidden');
    posCard.classList.toggle('clickable', posAll.length > 1);
    posCard.title = posAll.length > 1 ? '点击查看持仓明细' : '';
    // 持仓管理员的常态是 hold——不产生任何动作，而「持仓管理员」卡片只在真改止损/
    // 平仓时才出现，于是页面上长期看不到它在工作。这里把「上一轮巡检」常态化显示：
    // 状态点 + 高亮时间戳徽标 + 距今秒数。守护线程 30 秒一轮，秒数不涨即说明停了。
    const pm = s.position_management || {};
    const gd = s.position_guard || {};
    const ts = Number(pm.ts) || 0;
    let guardLine;
    if (!gd.running) {
      guardLine = '<span class="pm-line off"><i class="pm-dot"></i>持仓管理员未运行</span>';
    } else if (!ts) {
      guardLine = '<span class="pm-line warn"><i class="pm-dot"></i>运行中 · 等待首轮巡检</span>';
    } else {
      const at = new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour12: false });
      const age = Math.max(0, Math.round(Date.now() / 1000 - ts));
      // 正常一轮 30 秒，超过 90 秒没更新基本可判定线程卡住
      const state = age > 90 ? 'warn' : 'live';
      const r = pm.details && pm.details.floating_r;
      let tail = `<span class="pm-act">${esc(pm.action || 'hold')}</span>`;
      if (r != null) {
        tail += ` · <span class="pm-r">${Number(r) >= 0 ? '+' : ''}${Number(r).toFixed(2)}R</span>`;
      }
      // 多仓时心跳行可能来自「别的品种」那一轮（当前品种空仓时取全场最新），
      // 把品种塞进 title，避免看到 hold/+0.55R 却不知道说的是哪个仓
      const who = pm.symbol && posAll.length > 1 ? ` · 最近巡检 ${esc(pm.symbol)}` : '';
      guardLine = `<span class="pm-line ${state}" title="持仓管理员每 30 秒巡检一次${who}">`
        + `<i class="pm-dot"></i>巡检 <span class="pm-time">${esc(at)}</span>`
        + ` <span class="pm-age">${age}s 前</span> · ${tail}</span>`;
    }
    // 合计已经放在大字里了，这行只报「哪几个品种」，超过 3 个折成 +N，不再拼一遍盈亏
    const head = posAll.length === 1
      ? `开仓 ${esc(fmtPx(p.entry))} · 浮盈亏 ${esc(fmt(p.unrealized))}`
      : posAll.slice(0, 3).map((x) => esc(shortSym(x.symbol))).join(' / ')
        + (posAll.length > 3 ? ` +${posAll.length - 3}` : '')
        + (known ? '' : ' · 部分待报价');
    $('posSub').innerHTML = `${head}<br>${guardLine}`;
  } else {
    $('position').textContent = '空仓';
    $('position').className = '';
    $('posSub').textContent = '';
    posSym.classList.add('hidden');
    posSym.textContent = '';
    posCard.classList.remove('clickable');
    posCard.title = '';
  }

  // 成交表
  const tb = $('fillsTable').querySelector('tbody');
  if (s.fills && s.fills.length) {
    // A4：计划价 vs 成交价偏差、滑点、手续费、盈亏，全部回流展示
    const avgSlip = (() => {
      const xs = s.fills.map((f) => f.slippage_bps).filter((x) => x !== null && x !== undefined);
      return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
    })();
    if ($('fillMeta')) {
      $('fillMeta').textContent = `开仓 / 平仓流水（最近 ${s.fills.length} 条）`
        + (avgSlip !== null ? ` · 平均滑点 ${avgSlip >= 0 ? '+' : ''}${avgSlip.toFixed(2)}bps` : '');
    }
    tb.innerHTML = s.fills.map((f) => {
      const cls = f.side === 'LONG' ? 'up' : 'down';
      const slip = f.slippage_bps !== null && f.slippage_bps !== undefined
        ? `<span class="${f.slippage_bps > 0 ? 'down' : 'up'}">${f.slippage_bps >= 0 ? '+' : ''}${Number(f.slippage_bps).toFixed(2)}</span>` : '--';
      const tip = f.reason ? ` title="${esc(f.reason)}"` : '';
      return `<tr${tip}>
      <td class="mono">${esc(f.symbol || s.symbol)}</td>
      <td class="muted">${esc(f.ts)}</td>
      <td class="${cls}">${esc(actionCn(f.action))}</td>
      <td class="${cls}">${esc(sideCn(f.side))}</td>
      <td>${fmtQty(f.qty)}</td>
      <td>${f.plan_price ? fmtPx(f.plan_price) : '--'}</td>
      <td>${fmtPx(f.price)}</td>
      <td>${slip}</td>
      <td class="muted">${f.fee !== undefined ? fmt(f.fee, 4) : '--'}</td>
      <td class="${(f.pnl || 0) >= 0 ? 'up' : 'down'}">${f.pnl !== undefined ? fmt(f.pnl) : '--'}</td>
    </tr>`;
    }).join('');
  } else {
    tb.innerHTML = '<tr><td colspan="10" class="muted center">暂无成交</td></tr>';
  }

  // 持仓表：多品种逐行渲染，每行一个独立的平仓按钮（多仓时不能只给「平当前」）
  const pt = $('posTable').querySelector('tbody');
  const posList = posAll;
  if (posList.length) {
    pt.innerHTML = posList.map((p) => {
      const sym = p.symbol || s.symbol;
      const unreal = p.unrealized;
      const cls = unreal == null ? 'muted' : (unreal >= 0 ? 'up' : 'down');
      return `<tr>
      <td class="mono">${esc(sym)}<span class="cell-tag">${esc(p.mode || (exOk ? '交易所' : 'paper'))}</span></td>
      <td class="${p.side === 'LONG' ? 'up' : 'down'}">${esc(sideCn(p.side))}</td>
      <td>${fmtQty(p.qty)}</td><td>${fmtPx(p.entry)}</td><td>${fmtPx(p.mark)}</td>
      <td>${fmt(p.leverage, 1)}x</td>
      <td class="col-2line"><span class="up">${fmtPx(p.stop)}</span><span class="down">${fmtPx(p.tp)}</span></td>
      <td class="${cls}">${unreal == null ? '--' : fmt(unreal)}</td>
      <td><button class="btn ghost danger xs" data-close-pos="${esc(sym)}">平仓</button></td></tr>`;
    }).join('');
  } else {
    pt.innerHTML = '<tr><td colspan="9" class="muted center">空仓</td></tr>';
  }
  if ($('posMeta')) {
    $('posMeta').textContent = posList.length
      ? `${posList.length}/${Number(s.position_limit) || posList.length} 个`
      : '空仓';
  }

  // 交易页状态
  $('armState').textContent = modeLabel;
  $('armState').className = `badge ${s.mode === 'live' ? (s.testnet ? 'paper' : 'live') : 'paper'}`;
  // 标题右侧直接写清「现在连的是谁」，省得点开面板才知道
  if ($('exNow')) {
    $('exNow').textContent = s.mode === 'live'
      ? `${exName} · ${s.testnet ? '模拟盘' : '主网实盘'}${s.armed ? '' : '（未连接）'}`
      : '';
  }
  // 连接状态卡：交易页右侧 + 设置页右侧共用同一份渲染，避免两处文案打架
  const connOn = s.mode === 'live' && s.armed;
  const connTag = connOn ? (s.testnet ? '模拟盘' : '主网实盘') : '本地模拟';
  const connCards = [
    ['connBar', 'connDot', 'connStateText', 'connModeTag', 'connBarText', 'connBarSub', 'connDisarmBtn', 'connSwitchBtn'],
    ['setConnPanel', 'setConnDot', 'setConnState', 'setConnTag', 'setConnMain', 'setConnSub', 'setConnDisarm', 'setConnSwitch'],
  ];
  for (const [panelId, dotId, stateId, tagId, mainId, subId, disarmId, switchId] of connCards) {
    const panel = $(panelId);
    if (!panel) continue;
    panel.classList.toggle('on', connOn);
    $(dotId).classList.toggle('on', connOn);
    $(stateId).textContent = connOn ? '已连接' : '未连接';
    $(tagId).textContent = connTag;
    $(mainId).textContent = connOn ? `${exName} · ${connTag}` : '本地模拟';
    $(subId).textContent = connOn
      ? `${s.testnet ? 'Demo 假钱' : '真钱'} · 权益 ${fmt(s.equity)} USDT`
      : '未接交易所，下单只在本地撮合';
    $(disarmId).classList.toggle('hidden', !connOn);
    $(switchId).textContent = connOn ? '切换交易所' : '去连接交易所';
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
  // 设置页「账户状态」里的余额与持仓
  if (connected) {
    $('liveBalance').textContent = `${fmt(s.equity)} USDT`;
    $('livePos').textContent = s.position
      ? `${sideCn(s.position.side)} ${fmt(s.position.qty, 4)} @ ${fmt(s.position.entry)}`
      : '无';
  } else {
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

/* 持仓 / 持仓历史：标签页，选中项记在浏览器里 */
function showPosTab(name, persist = true) {
  const hist = name === 'hist';
  document.querySelectorAll('[data-pos-tab]').forEach((b) => {
    b.classList.toggle('sel', (b.dataset.posTab === 'hist') === hist);
  });
  if ($('posPaneCur')) $('posPaneCur').classList.toggle('hidden', hist);
  if ($('posPaneHist')) $('posPaneHist').classList.toggle('hidden', !hist);
  // 「一键平仓」只在当前持仓页有意义，历史页收起来
  if ($('posActions')) $('posActions').classList.toggle('hidden', hist);
  if (persist) {
    try { localStorage.setItem('qeelin-postab', hist ? 'hist' : 'cur'); } catch (e) { /* 隐私模式 */ }
  }
}

if ($('posTabs')) {
  $('posTabs').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-pos-tab]');
    if (btn) showPosTab(btn.dataset.posTab);
  });
  let savedPosTab = null;
  try { savedPosTab = localStorage.getItem('qeelin-postab'); } catch (e) { /* 隐私模式 */ }
  showPosTab(savedPosTab === 'hist' ? 'hist' : 'cur', false);
}

function renderPosHistory() {
  const tb = $('posHistBody');
  if (!tb) return;
  const hist = (lastStatus && lastStatus.position_history) || [];
  if ($('histMeta')) $('histMeta').textContent = hist.length ? `共 ${hist.length} 笔` : '0 笔';
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
    let label = '';
    let tip = `${key}`;
    if (rec) {
      const pnl = rec.pnl || 0;
      monthPnl += pnl;
      if (pnl > 0) wins += 1;
      else if (pnl < 0) losses += 1;
      // 不再铺底色，只用红绿数字表示盈亏，画面更干净
      cls += pnl >= 0 ? ' up' : ' down';
      // 宽屏用完整数值，窄屏换成紧凑写法，由 CSS 决定显示哪一个
      label = `<span class="v v-full">${pnl >= 0 ? '+' : ''}${fmt(pnl, 2)}</span>`
        + `<span class="v v-sm">${fmtCalShort(pnl)}</span>`;
      tip = `${key} · ${rec.trades} 笔（胜 ${rec.wins}）· ${pnl >= 0 ? '+' : ''}${fmt(pnl, 2)} U`;
    }
    if (key === todayKey) cls += ' today';
    html += `<div class="${cls}" title="${esc(tip)}"><span class="d">${d}</span>${label}</div>`;
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
      <div class="analyst-body collapsed"><p>${esc(x.summary)}</p></div>
      <button class="analyst-toggle" type="button" onclick="const a=this.closest('.analyst');a.classList.toggle('open');this.textContent=a.classList.contains('open')?'收起':'展开全文'">展开全文</button>
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
        ${sizingTraceHtml(p)}
        ${costGateHtml(p)}
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
  // data-label 供窄屏卡片式布局用（thead 隐藏后由 ::before 还原字段名）
  tb.innerHTML = rows.map((x) => {
    if (!x.ok) return `<tr><td class="cell-name" data-label="因子">${esc(x.name)}</td><td data-label="家族">${esc(x.family)}</td><td colspan="4" class="muted" data-label="错误">${esc(x.error)}</td><td class="down" data-label="结论">失败</td></tr>`;
    const cls = x.accepted ? 'up' : 'down';
    return `<tr>
      <td class="mono cell-name" data-label="因子">${esc(x.name)}</td><td class="muted" data-label="家族">${esc(x.family)}</td>
      <td data-label="IS 夏普">${x.is_sharpe.toFixed(2)}</td><td data-label="OOS 夏普">${x.oos_sharpe.toFixed(2)}</td>
      <td data-label="OOS 回撤" class="${x.oos_dd <= -20 ? 'down' : ''}">${x.oos_dd.toFixed(1)}%</td>
      <td data-label="评分">${x.score === null ? '--' : x.score.toFixed(2)}</td>
      <td data-label="结论" class="${cls}">${x.accepted ? '通过' : esc(x.reject || '淘汰')}</td></tr>`;
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

// 设置页连接状态卡：切换交易所 = 展开当前模式的凭据面板
const setConnSwitch = $('setConnSwitch');
if (setConnSwitch) {
  setConnSwitch.addEventListener('click', () => {
    const scope = (lastStatus && lastStatus.mode === 'live' && !lastStatus.testnet) ? 'live' : 'testnet';
    const boxId = scope === 'live' ? 'liveBox' : 'testnetBox';
    openPanel(boxId, scope === 'live' ? 'liveBtn' : 'testnetBtn');
    selectExchange(scope, (lastStatus && lastStatus.exchange) || 'binance');
    renderExchangePanel(scope);
    $(boxId).scrollIntoView({ behavior: 'smooth', block: 'center' });
  });
}

// 设置页连接状态卡：断开
const setConnDisarm = $('setConnDisarm');
if (setConnDisarm) {
  setConnDisarm.addEventListener('click', async () => {
    if (blockIfAutoRunning('断开连接')) return;
    try {
      await api('/api/trading/disarm', 'POST', {});
      closeModePanels();
      toast('已断开交易所，回到本地模拟', 'ok');
      poll();
    } catch (e) { toast(e.message, 'err'); }
  });
}

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

$('tnTestBtn').addEventListener('click', () => runSelftest($('tnStatus'), currentExchange('testnet'), 'demo'));
$('selftestBtn').addEventListener('click', () => {
  // 底部全局「连接自检」：测当前正连着的交易所；还没连交易所时退回模拟盘面板当前选中项
  const ex = (lastStatus && lastStatus.exchange) || currentExchange('testnet');
  const mode = (lastStatus && lastStatus.testnet === false) ? 'live' : 'demo';
  runSelftest($('tnStatus'), ex, mode);
});

function formatSelftest(d) {
  if (!d || !d.ok) return `✗ ${d?.error || '未知错误'}`;
  return `✓ ${d.base} · 延迟 ${d.latency_ms}ms · 余额 ${fmt(d.balance)} USDT（可用 ${fmt(d.available)}）`
    + ` · 精度 tick ${d.filters.tick} / step ${d.filters.step}`;
}

async function runSelftest(el, exchange, mode) {
  const prev = el.textContent;
  el.textContent = '自检中…';
  try {
    let url = '/api/exchange/test';
    if (exchange) url += `?exchange=${encodeURIComponent(exchange)}&mode=${encodeURIComponent(mode || '')}`;
    const d = await api(url);
    el.textContent = formatSelftest(d);
    toast(d.ok ? '连接自检通过' : `自检失败：${d.error}`, d.ok ? 'ok' : 'err');
    if (d.ok) {
      if ($('setConnState')) $('setConnState').textContent = '已连接';
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
    // 这个面板只负责真钱：testnet 显式传 false（服务端默认是 true，不传会静默进模拟盘）
    await api('/api/trading/arm', 'POST', {
      mode: 'live', exchange: ex,
      confirm: $('confirmText').value,
      testnet: false,
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
    if ($('setConnState')) $('setConnState').textContent = d.connected ? '已连接' : `未连接${d.error ? '：' + d.error : ''}`;
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

// 持仓表行内平仓：多品种时逐个平，避免一不留神把全部仓位平掉
$('posTable').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-close-pos]');
  if (!btn) return;
  const sym = btn.dataset.closePos;
  if (!confirm(`确认平掉 ${sym} 这笔持仓？`)) return;
  btn.disabled = true;
  try {
    const d = await api('/api/trading/close', 'POST', { symbol: sym });
    toast(d.ok ? `${sym} 已平仓` : `平仓失败：${d.error}`, d.ok ? 'ok' : 'err');
    poll();
  } catch (err) {
    toast(err.message, 'err');
  } finally {
    btn.disabled = false;
  }
});

$('closeBtn').addEventListener('click', async () => {
  const list = (lastStatus
    && (Array.isArray(lastStatus.positions)
      ? lastStatus.positions
      : (lastStatus.position ? [lastStatus.position] : []))) || [];
  if (!list.length) { toast('当前没有持仓', 'err'); return; }
  const names = list.map((p) => p.symbol).join('、');
  const ask = list.length > 1
    ? `确认平掉全部 ${list.length} 笔持仓（${names}）？`
    : `确认平掉 ${names}？`;
  if (!confirm(ask)) return;
  let ok = 0;
  const errs = [];
  for (const p of list) {
    try {
      const d = await api('/api/trading/close', 'POST', { symbol: p.symbol });
      if (d.ok) ok += 1;
      else errs.push(`${p.symbol}: ${d.error}`);
    } catch (e) {
      errs.push(`${p.symbol}: ${e.message}`);
    }
  }
  toast(errs.length
    ? `平仓 ${ok} 笔，失败 ${errs.length} 笔（${errs[0]}）`
    : `已平仓 ${ok} 笔`, errs.length ? 'err' : 'ok');
  poll();
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

/* 通知 / 告警：一键清除（截断 inapp JSONL），清完立即重渲染 */
const notifClearBtn = $('notifClearBtn');
if (notifClearBtn) {
  notifClearBtn.addEventListener('click', async () => {
    if (!confirm('清空全部通知？')) return;
    try {
      await api('/api/notifications/clear', 'POST', {});
      toast('通知已清除', 'ok');
      renderNotifications();
    } catch (e) { toast(e.message, 'err'); }
  });
}

$('llmSaveBtn').addEventListener('click', saveLlmSettings);
$('llmTestBtn').addEventListener('click', testLlmSettings);
$('sentimentSaveBtn').addEventListener('click', saveSentimentSettings);
$('notifySaveBtn').addEventListener('click', saveNotifySettings);
$('sentimentSource').addEventListener('change', toggleCpKeyRow);

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
  const scan = lastStatus?.scan_state || {};
  if (!data.length) {
    if (scan.scanning) {
      listEl.innerHTML = '<div class="muted center">正在扫描全品种…（约 10 秒，完成后自动刷新）</div>';
    } else {
      listEl.innerHTML = '<div class="muted center">暂无扫描结果，点右上角「重扫」</div>';
      // 首次进页面自动触发一次后台扫描，避免排行榜一直空着
      if (!window.__oppAsked) {
        window.__oppAsked = true;
        api('/api/opportunity', 'POST', {}).catch(() => {});
      }
    }
    fitNotifHeight();
    return;
  }
  const maxScore = Math.max(...data.map((r) => r.score || 0), 0.0001);
  listEl.innerHTML = data.map((r) => {
    const w = Math.max(2, Math.round(((r.score || 0) / maxScore) * 100));
    const sig = r.error ? '—' : ((r.composite || 0) >= 0 ? '看多 ↑' : '看空 ↓');
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
  fitNotifHeight();   // 榜单高度变了，通知卡的上限要跟着重算
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
    const d = await api('/api/opportunity', 'POST', { refresh: true });
    if (d.scanning) {
      renderOpportunities(d.rows);
      toast('已开始扫描，完成后自动刷新', 'ok');
    } else {
      renderOpportunities(d.rows);
      toast(`扫描完成，共 ${(d.rows || []).length} 个品种`, 'ok');
    }
  } catch (e) { toast(e.message, 'err'); }
});

window.addEventListener('resize', () => { renderPnl(); fitNotifHeight(); });
$('calPrev').addEventListener('click', () => shiftMonth(-1));
$('calNext').addEventListener('click', () => shiftMonth(1));

/* ---------------- 主题切换 ---------------- */
const THEME_KEY = 'qeelin-theme';
function applyTheme(mode) {
  const light = mode === 'light';
  document.documentElement.dataset.theme = light ? 'light' : 'dark';
  const btn = $('themeBtn');
  if (btn) {
    // 图标与文字分开存：窄屏只留图标，改 textContent 会把图标一起抹掉
    const ico = btn.querySelector('.theme-ico');
    const txt = btn.querySelector('.theme-text');
    if (ico) ico.textContent = light ? '🌙' : '☀';
    if (txt) txt.textContent = light ? '暗色主题' : '亮色主题';
    btn.title = light ? '切换到暗色主题' : '切换到亮色主题';
    btn.setAttribute('aria-label', btn.title);
  }
}
// 有本地偏好就听本地；没有则尊重 <html data-theme>（默认暗色）
let savedTheme = null;
try { savedTheme = localStorage.getItem(THEME_KEY); } catch (e) { /* 隐私模式 */ }
applyTheme(
  savedTheme === 'light' || (!savedTheme && document.documentElement.dataset.theme === 'light')
    ? 'light'
    : 'dark'
);
$('themeBtn').addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* 忽略隐私模式 */ }
  applyTheme(next);
  toast(next === 'light' ? '已切换到亮色主题' : '已切换到暗色主题', 'ok');
});

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
  // 3 秒一轮：标记价要跟着报价线程（4 秒）走，8 秒会明显发木。
  // /api/status 不现场打交易所——实时价来自 ticker 线程，实盘权益 20 秒缓存，
  // 其余全是内存数据，所以这个频率不会把签名接口刷爆。
  setInterval(poll, 3000);
})();