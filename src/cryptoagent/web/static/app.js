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
// 日历格子的窄屏数值：只保留到个位（去掉小数与千分位），手机上一格仅约 42px，
// "+1,024.55" 会把所在列撑宽、同一行 7 格宽度不一。完整精度仍在 title 里。
// 宽屏 .v-full 仍是两位小数，不受影响。
const fmtCalShort = (n) => {
  const v = Math.round(Number(n) || 0);
  return v > 0 ? `+${v}` : (v < 0 ? `${v}` : '0');
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
// 持仓方向中文：多 / 空（保留英文便于对照）
const sideCn = (s) => (s === 'LONG' ? '多' : s === 'SHORT' ? '空' : (s || ''));
// 成交动作中文：开仓 / 平仓（交易所原始成交已带 买入/卖出 则原样保留）
const actionCn = (a) => (a === 'OPEN' ? '开仓' : a === 'CLOSE' ? '平仓' : (a || ''));
// 决策方向中文：后端 proposal.action / risk_decision.action 是 long/short/flat，
// 直接输出会在 ③ 交易员提案、④ 风控、机会榜里露出英文，统一走这里。
const dirCn = (a) => ({ long: '做多', short: '做空', flat: '观望' }[String(a || '').toLowerCase()] || (a || ''));
// 风控官结论中文：approve / reduce / reject
const verdictCn = (v) => ({ approve: '通过', reduce: '减仓', reject: '拒绝' }[String(v || '').toLowerCase()] || (v || ''));
// 订单状态中文。注意：决策快照（/api/decisions/latest）写入时 status 固定是 PLANNED，
// 真实状态在 /api/status 的 last_decision.order（execute() 成功后后端会改成 FILLED）。
const ORDER_STATUS_CN = {
  PLANNED: '待执行', SENT: '已发送', FILLED: '已成交',
  REJECTED: '已拒绝', CANCELED: '已撤销', CANCELLED: '已撤销',
};
const orderStatusCn = (s) => ORDER_STATUS_CN[String(s || '').toUpperCase()] || s || '';
// 同一笔订单的身份：品种 + 方向 + 数量（快照自带的 status 不可信，靠它去实时数据里认领）
const orderKey = (o) => [o.symbol, String(o.position_side || o.side || '').toUpperCase(), Number(o.quantity || 0)].join('|');
// 合并实时状态：快照里的 order 只提供计划内容，状态优先取 /api/status 的同一笔订单
function liveOrderStatus(o) {
  if (!o) return '';
  const lo = (lastStatus && lastStatus.last_decision && lastStatus.last_decision.order) || null;
  if (lo && orderKey(lo) === orderKey(o) && lo.status) return lo.status;
  return o.status;
}
// 状态行文案：待执行才提示去交易页手动执行，已成交不再提示
function orderNote(o) {
  const st = liveOrderStatus(o);
  const hint = String(st || '').toUpperCase() === 'PLANNED' ? '，需要在「交易」页手动执行' : '';
  return `状态 ${orderStatusCn(st)}${hint}`;
}
// 轮询时只刷这一行，不整块重渲染（否则滚动位置与「展开更多」状态会被重置）
function paintOrderStatus() {
  const note = document.querySelector('#orderDock .df-note');
  if (!note || !latestDecision || !latestDecision.order) return;
  const text = orderNote(latestDecision.order);
  if (note.textContent !== text) note.textContent = text;
}
// 卡片右上角时间戳：本地记录是 %Y-%m-%d %H:%M:%S(19位)，裁掉年份保留 MM-DD HH:MM:SS；
// 交易所回读已是 %m-%d %H:%M:%S(14位) 则原样保留。
const shortTs = (v) => {
  const s = String(v ?? '').trim();
  if (!s) return '';
  return s.length > 14 ? s.slice(5) : s;
};
// 通知时间戳是 UTC ISO（带 +00:00 和微秒），直接截断会得到
// 「11-05T17:07:19.848546+00:00」这种超长串，把通知正文挤成一字一行。
// 这里转成本地时间并压到 MM-DD HH:MM；解析失败退回原截断逻辑。
const notifTs = (v) => {
  const s = String(v ?? '').trim();
  if (!s) return '';
  const d = new Date(s.replace(/\.(\d{3})\d+/, '.$1'));
  if (Number.isNaN(d.getTime())) return shortTs(s);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
// 账户模式中文（用户约定）：本地 = 本地模拟撮合，模拟 = 交易所 demo，实盘 = 交易所主网。
// mode=live 只表示「通过交易所接口下的单」，持仓记录本身不带 testnet 标记，
// 必须结合全局 s.testnet 才能区分模拟盘与主网，否则币安 demo 会被误标成实盘。
const modeCn = (m, testnet) => {
  const v = String(m ?? '').trim().toLowerCase();
  if (v === 'paper') return '本地';
  if (v === 'live' || v === 'testnet') return testnet ? '模拟' : '实盘';
  return '本地';
};
/* 「最近成交」展示合并：同一时间段（沿用 f.ts 现有秒级展示粒度）+ 同品种 + 同方向
   的多笔成交合并成一条展示记录。仅作用于渲染结果：s.fills / lastStatus 原始明细
   不做任何修改，fillMeta 与平均滑点仍基于原始明细计算。
   - 组内只有 1 笔：原样返回，完全按原规则展示（含 title、动作、涨跌色等），零影响。
   - 多笔：数量/手续费/盈亏求和，成交价/计划价/滑点按数量加权平均，动作与原因去重并列，
     并用 __n 标记笔数（品种列渲染成「N 笔合并」），完整体现该组汇总信息。
   - 输出顺序按每组首次出现的位置（Map 保序），与接口返回的最新在前顺序一致。 */
const mergeFillsForDisplay = (fills) => {
  const groups = new Map();
  fills.forEach((f) => {
    const key = `${f.ts || ''}\u0001${f.symbol || ''}\u0001${f.side || ''}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(f);
  });
  const num = (v) => (typeof v === 'number' ? v : Number(v));
  const valid = (v) => v !== null && v !== undefined && v !== '' && Number.isFinite(num(v));
  return Array.from(groups.values()).map((rows) => {
    if (rows.length === 1) return rows[0];
    const sum = (k) => rows.reduce((a, f) => a + (valid(f[k]) ? num(f[k]) : 0), 0);
    const wavg = (k) => {
      let w = 0, acc = 0;
      rows.forEach((f) => {
        const q = valid(f.qty) ? num(f.qty) : 0;
        if (q > 0 && valid(f[k])) { w += q; acc += q * num(f[k]); }
      });
      return w > 0 ? acc / w : null;
    };
    const acts = [...new Set(rows.map((f) => actionCn(f.action)).filter(Boolean))];
    const reasons = [...new Set(rows.map((f) => f.reason).filter(Boolean))];
    const base = rows[0];
    return {
      ...base,
      qty: sum('qty'),
      price: wavg('price') ?? base.price,
      plan_price: wavg('plan_price'),
      slippage_bps: wavg('slippage_bps'),
      fee: rows.some((f) => valid(f.fee)) ? sum('fee') : undefined,
      pnl: rows.some((f) => valid(f.pnl)) ? sum('pnl') : undefined,
      reason: reasons.join(' / ') || undefined,
      __n: rows.length,
      __acts: acts.join(' / ') || '--',
    };
  });
};
// 数据源标签：把 `cache:BNBUSDT 1h 末根 …` 压成 `缓存 BNB 1h`，完整原串留在 title
const shortSource = (src) => {
  if (!src) return '--';
  const s = String(src);
  const [kindRaw, rest = ''] = s.split(/:(.+)/);
  const kind = kindRaw.trim();
  const pair = /([A-Z]+)USDT\s+(\w+)/.exec(rest);
  const sym = pair ? `${pair[1]} ${pair[2]}` : '';
  if (kind.startsWith('synthetic')) return '合成假数据';
  if (kind.startsWith('stale-cache')) return `旧缓存 ${sym}`.trim();
  if (kind === 'cache') return `缓存 ${sym}`.trim();
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
  // 长任务（扫描/研究/决策）单独放宽；其余 30 秒超时。没有超时的话，
  // 后端一旦卡住，按钮会永远停在「扫描中…」，用户只能刷新页面。
  const longTask = /\/(scan|research|decision)$/.test(path);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), longTask ? 180000 : 30000);
  opt.signal = ctrl.signal;
  let res;
  try {
    res = await fetch(path, opt);
  } catch (e) {
    if (e && e.name === 'AbortError') throw new Error('请求超时，请稍后重试');
    throw e;
  } finally {
    clearTimeout(timer);
  }
  const json = await res.json().catch(() => ({ ok: false, error: '响应不是 JSON' }));
  if (!res.ok || json.ok === false) throw new Error(json.error || `HTTP ${res.status}`);
  return json.data;
}

// 账户切换：后端在「有实盘持仓 / 自动交易运行中」时会拒绝（403）。
// 这里让用户显式确认后带 force 重试；取消则把原始错误继续抛给调用方。
async function armAccount(payload) {
  try {
    return await api('/api/trading/arm', 'POST', payload);
  } catch (e) {
    const msg = String(e.message || '');
    if (!/实盘持仓|自动交易正在运行/.test(msg)) throw e;
    if (!window.confirm(`${msg}\n\n确认继续切换？旧账户的仓位将不再由本程序管理。`)) throw e;
    return await api('/api/trading/arm', 'POST', { ...payload, force: true });
  }
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
  if (name === 'overview') { renderPnl(); }
  if (name === 'decision') { requestAnimationFrame(fitScanReasons); requestAnimationFrame(syncFactorMoreVisibility); }
  if (name === 'settings') { loadLlmSettings(); loadSentimentSettings(); loadNotifySettings(); loadPositionLlmSettings(); loadProfiles(); loadEvolveState(); }
  if (name === 'evolution') { loadEvolution(); }
}

document.querySelectorAll('.tab').forEach((btn) => {
  btn.addEventListener('click', () => showTab(btn.dataset.tab));
});

// 手机端点击顶部 LOGO / 品牌名回到总览页。
// 仅在移动布局（≤860px）生效，PC 端侧栏品牌区保持纯展示、不响应也无手型光标。
// 复用 showTab 以同步激活态与 hash；顺带把页面滚回顶部，符合「回到首页」的预期。
const brandEl = document.querySelector('.side-brand');
if (brandEl) {
  brandEl.addEventListener('click', () => {
    if (!window.matchMedia('(max-width: 860px)').matches) return;
    showTab('overview');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });
}

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
  // 确保停在「当前持仓」分段，并从持仓卡顶部开始看（不是滚到表格中间）
  const cur = document.querySelector('[data-pos-tab="cur"]');
  if (cur && !cur.classList.contains('sel')) cur.click();
  const panel = document.getElementById('posPanel');
  if (!panel) return;
  // 等一帧再滚：panel 刚从 display:none 变 block，同一帧里量到的位置不准
  requestAnimationFrame(() => {
    const topH = document.querySelector('.topbar')?.offsetHeight || 64;
    const y = panel.getBoundingClientRect().top + window.scrollY - topH - 12;
    window.scrollTo({ top: Math.max(0, y), behavior: 'smooth' });
  });
});

/* ---------------- 进化页 ---------------- */
const EVO_VERDICT_CN = { good: '好交易', bad_entry: '入场错', bad_exit: '退出早', gave_back: '浮盈回吐', neutral: '中性' };
const EVO_MISTAKE_CN = {
  entry_immediately_wrong: '入场即反向',
  entry_wrong_side: '方向做反',
  exit_too_early: '退出过早',
  gave_back_profit: '浮盈回吐',
  stop_too_tight: '止损过紧',
};
const EVO_STATUS_CN = {
  proposed: '待审', applied: '已应用', confirmed: '已证实',
  degraded: '变差', rejected: '已拒绝', rolled_back: '已回滚',
};
const EVO_KIND_CN = { config: '参数', rule: '规则', factor: '因子' };
// 复盘判定的固定顺序与配色：堆叠条和图例共用同一份，避免两处顺序/颜色不一致
const EVO_VERDICT_KEYS = ['good', 'bad_entry', 'bad_exit', 'gave_back', 'neutral'];
const EVO_VERDICT_COLOR = {
  good: 'var(--ok)',
  bad_entry: 'var(--up)',
  bad_exit: 'var(--amber)',
  gave_back: 'var(--purple)',
  neutral: 'var(--faint)',
};

const evoSigned = (v) => {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '--';
  const n = Number(v);
  return `${n >= 0 ? '+' : ''}${n.toFixed(2)}`;
};
// 参数值的可读化：5.0000 → 5、1.3000 → 1.3，避免表格里出现一长串零
const evoVal = (v) => {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'number') return Number(v).toFixed(4).replace(/\.?0+$/, '');
  return String(v);
};
// 账本 target 是「回滚时要定位的对象路径」（factor_library.momentum_168 /
// position.trail_atr_mult），数据层必须保留完整值；表格只显示末段名字，
// 完整路径挂 title。缩写只发生在展示层，回滚用的仍是原始 target。
const evoShortTarget = (t) => {
  const s = String(t || '');
  const i = s.lastIndexOf('.');
  return i >= 0 ? s.slice(i + 1) : s;
};
// 动作短语归一化：「新建因子文件」是早期文案，统一显示为「新建因子」。
// 历史记录已经落盘、账本不删历史，所以只能在展示层抹平。
const evoPhrase = (v) => String(v == null ? '' : v).replace('新建因子文件', '新建因子');

async function loadEvolution() {
  // 绩效数据随 status 轮询已有；这里只补「方案」与「账本」两个接口
  if (!lastStatus) {
    try { lastStatus = await api('/api/status'); } catch (e) { /* 用空数据渲染 */ }
  }
  renderEvoPerf((lastStatus && lastStatus.performance) || {});
  renderEvoLessons();
  renderEvoProfile();
  try {
    const d = await api('/api/evolution/changes', 'POST', { limit: 30 });
    renderEvoChanges(d || {});
  } catch (e) {
    const tb = $('evoChangesTable')?.querySelector('tbody');
    if (tb) tb.innerHTML = `<tr><td colspan="6" class="muted center">读取失败：${esc(e.message)}</td></tr>`;
  }
}

async function renderEvoProfile() {
  const badge = $('evoProfileBadge');
  const summary = $('evoProfileSummary');
  const meta = $('evoProfileMeta');
  if (!badge) return;
  try {
    const d = await api('/api/settings/profiles');
    const cur = d.current || '';
    const list = d.profiles || [];
    const prof = list.find((p) => p.name === cur);
    if (cur && prof) {
      badge.textContent = prof.label || cur;
      badge.className = 'badge on';
      summary.textContent = prof.summary || '';
      meta.innerHTML = '<span>当前参数与方案完全一致，差异 <b>0</b> 项</span>';
      return;
    }
    // 不匹配任何方案时给出「最接近的那一套」，比只写「自定义」更有用
    badge.textContent = '自定义组合';
    badge.className = 'badge';
    summary.textContent = '当前参数不匹配任何预设方案，属于自定义组合。';
    const nearest = list.slice().sort((a, b) => (a.diff || []).length - (b.diff || []).length)[0];
    meta.innerHTML = nearest
      ? `<span>最接近 <b>${esc(nearest.label)}</b>，差异 <b>${(nearest.diff || []).length}</b> 项</span>`
      : '';
  } catch (e) {
    badge.textContent = '读取失败';
    badge.className = 'badge';
    summary.textContent = e.message || '无法读取配置方案';
  }
}

function renderEvoPerf(p) {
  p = p || {};
  const o = p.overall || {};
  const n = Number(o.n_with_r || 0);
  const need = Number(p.min_trades_for_verdict || 100);

  const fill = $('evoProgressFill');
  if (fill) fill.style.width = `${need > 0 ? Math.min(100, (n / need) * 100) : 0}%`;
  const ptext = $('evoProgressText');
  if (ptext) ptext.textContent = `${n} / ${need} 笔平仓`;

  const st = $('evoSampleState');
  if (st) {
    if (p.verdict_ready) { st.textContent = '可评估'; st.className = 'badge on'; }
    else if (p.sample_ok) { st.textContent = '样本可用'; st.className = 'badge paper'; }
    else { st.textContent = '观察期'; st.className = 'badge'; }
  }

  const num = (v, d = 2) => (v === null || v === undefined || Number.isNaN(Number(v))) ? '--' : Number(v).toFixed(d);
  const kpis = [
    ['期望值', o.expectancy_r == null ? '--' : `${evoSigned(o.expectancy_r)}R`, '每笔平均 R'],
    ['按笔夏普', num(o.r_sharpe), '均值 / 波动'],
    ['最大回撤', o.max_dd_r == null ? '--' : `${evoSigned(o.max_dd_r)}R`, '累计 R 曲线'],
    ['胜率', o.win_rate == null ? '--' : `${(Number(o.win_rate) * 100).toFixed(0)}%`, `${n} 笔样本`],
  ];
  const kp = $('evoKpis');
  if (kp) {
    kp.innerHTML = kpis.map(([k, v, s]) =>
      `<div class="evo-kpi"><label>${k}</label><b>${v}</b><small>${s}</small></div>`).join('');
  }

  const rs = p.review_summary || {};
  const total = Number(rs.n || 0);
  const counts = EVO_VERDICT_KEYS.map((k) => Number((rs.by_verdict || {})[k] || 0));
  const sum = counts.reduce((a, b) => a + b, 0);
  const vd = $('evoVerdicts');
  if (vd) {
    // 一条堆叠分布条 + 五格图例：先看比例再读数字，比五个孤立方块更快看懂
    const bar = sum
      ? `<div class="evo-dist-bar">${EVO_VERDICT_KEYS.map((k, i) => (counts[i]
        ? `<i style="width:${((counts[i] / sum) * 100).toFixed(2)}%;background:${EVO_VERDICT_COLOR[k]}"`
          + ` title="${EVO_VERDICT_CN[k]} ${counts[i]} 笔"></i>`
        : '')).join('')}</div>`
      : '';
    const legend = `<div class="evo-dist-legend">${EVO_VERDICT_KEYS.map((k, i) => {
      const share = sum ? Math.round((counts[i] / sum) * 100) : 0;
      // 无样本时不显示「—」占位：只有数字 0 更干净
      const pct = sum ? `<span>${share}%</span>` : '';
      return `<div class="evo-legend"><label><i class="dot" style="background:${EVO_VERDICT_COLOR[k]}"></i>${EVO_VERDICT_CN[k]}</label>`
        + `<b>${counts[i]}${pct}</b></div>`;
    }).join('')}</div>`;
    vd.innerHTML = bar + legend;
  }
  const mis = Object.entries(rs.by_mistake || {}).slice(0, 6);
  const mc = $('evoMistakes');
  if (mc) {
    mc.innerHTML = mis.length
      ? mis.map(([k, v]) => `<span class="evo-chip bad">${esc(EVO_MISTAKE_CN[k] || k)} ×${v}</span>`).join('')
      : '<span class="muted">暂无错误标签</span>';
  }
  const rm = $('evoReviewMeta');
  if (rm) {
    rm.textContent = total
      ? `${total} 笔已复盘 · 好交易占比 ${(Number(rs.good_ratio || 0) * 100).toFixed(0)}%`
      : '产生了平仓后自动生成';
  }
  renderEvoBench(p, o);
}

function renderEvoBench(p, o) {
  const tb = $('evoBenchTable')?.querySelector('tbody');
  if (!tb) return;
  const b = p.benchmarks || {};
  const rows = [
    ['期望值 (R/笔)', 'expectancy_r', 2],
    ['按笔夏普', 'r_sharpe', 2],
    ['最大回撤 (R)', 'max_dd_r', 2],
  ];
  tb.innerHTML = rows.map(([label, key, d]) => {
    const cell = (v) => (v === null || v === undefined || Number.isNaN(Number(v)))
      ? '—' : Number(v).toFixed(d);
    return `<tr><td>${label}</td><td class="num"><b>${cell(o[key])}</b></td>`
      + `<td class="num muted">${cell((b.pass || {})[key])}</td>`
      + `<td class="num muted">${cell((b.near_human || {})[key])}</td>`
      + `<td class="num muted">${cell((b.beyond || {})[key])}</td></tr>`;
  }).join('');
}

function renderEvoChanges(d) {
  const tb = $('evoChangesTable')?.querySelector('tbody');
  if (!tb) return;
  const items = (d && d.items) || [];
  const meta = $('evoChangesMeta');
  if (meta) {
    const s = (d && d.summary) || {};
    const bs = s.by_status || {};
    meta.textContent = items.length
      ? `共 ${s.total || items.length} 条 · 已证实 ${bs.confirmed || 0} · 变差 ${bs.degraded || 0}`
      : '';
  }
  if (!items.length) {
    tb.innerHTML = '<tr><td colspan="6" class="muted center">还没有自动改动。L1 参数校准需 20 笔平仓样本，因子研究需先通过三闸。</td></tr>';
    return;
  }
  tb.innerHTML = items.map((it) => {
    const st = EVO_STATUS_CN[it.status] || it.status || '';
    const cls = it.status === 'degraded' ? 'bad'
      : it.status === 'confirmed' ? 'ok'
        : it.status === 'rejected' ? 'warn'
          : it.status === 'rolled_back' ? 'muted' : '';
    const a = it.attribution || {};
    let att = '观察中';
    if (a.ready) {
      const dl = evoSigned(a.delta_expectancy_r);
      att = a.verdict === 'improved' ? `改善 ${dl}R`
        : a.verdict === 'degraded' ? `变差 ${dl}R`
          : `无明显变化 ${dl}R`;
    } else if (a.need) {
      att = `${a.have || 0}/${a.need} 笔`;
    }
    // 新建类改动没有「之前的值」，直接显示结果，避免「— → 新建因子」这种冗余
    const hasBefore = it.before !== null && it.before !== undefined && it.before !== '';
    const change = hasBefore
      ? `${evoVal(it.before)} → ${evoVal(it.after)}`
      : evoPhrase(evoVal(it.after));
    const ts = String(it.applied_at || it.created_at || '');
    return `<tr>
      <td class="muted">${esc(ts.slice(5, 16))}</td>
      <td>${esc(EVO_KIND_CN[it.kind] || it.kind || '')}</td>
      <td title="${esc(it.target || '')}">${esc(evoShortTarget(it.target))}</td>
      <td>${esc(change)}</td>
      <td><span class="evo-state ${cls}">${esc(st)}</span></td>
      <td class="muted">${esc(att)}</td>
    </tr>`;
  }).join('');
}

function renderEvoLessons() {
  const el = $('evoLessons');
  if (!el) return;
  const hist = (lastStatus && Array.isArray(lastStatus.position_history)) ? lastStatus.position_history : [];
  const rows = hist.filter((h) => h && h.review && h.review.lesson).slice(0, 5);
  if (!rows.length) {
    el.innerHTML = '<div class="evo-empty">暂无复盘记录。平仓后复盘官会给出「赚在哪、亏在哪」的结构化结论。</div>';
    return;
  }
  el.innerHTML = rows.map((h) => {
    const v = (h.review && h.review.verdict) || '';
    const cls = v === 'good' ? 'good' : (v === 'gave_back' || v === 'bad_exit') ? 'warn' : 'bad';
    return `<div class="evo-lesson ${cls}">${esc(h.symbol || '')} ${esc(h.review.lesson)}</div>`;
  }).join('');
}

/* ---------------- 状态轮询 ---------------- */
async function poll() {
  try {
    const s = await api('/api/status');
    lastStatus = s;
    renderStatus(s);
    // api() 已经把 {ok,data} 解包成 data，这里不能再取 .data——多写一层会让整张
    // 「持仓管理员」卡片永远拿到 undefined、一直 hidden（曾经的实际故障）。
    renderPositionManagement(s);
    renderDecisionShadow(s);
    // 订单计划的状态行跟实时订单走（决策快照里永远写着 PLANNED）
    paintOrderStatus();
  } catch (e) {
    console.error(e);
  }
  try {
    renderNotifications();
  } catch (e) {
    console.error(e);
  }
  // 进化页可见时就地刷新绩效与复盘（数据来自 lastStatus，不再发额外请求）
  if ($('tab-evolution')?.classList.contains('active') && lastStatus) {
    try {
      renderEvoPerf(lastStatus.performance || {});
      renderEvoLessons();
    } catch (e) {
      console.error(e);
    }
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
    return;
  }
  // 3 秒一次轮询，内容没变就不要重建 DOM：innerHTML 一清空，列表的滚动位置就被顶回顶部，
  // 用户正在翻历史消息时会一直被拽回去。
  const sig = items.map((n) => `${n.ts}|${n.level}|${n.title}|${n.message || ''}`).join('~');
  if (el.dataset.sig === sig) return;
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
        <div class="notif-ts muted">${esc(notifTs(n.ts))}</div>
      </div>
    </div>`;
  }).join('');
  el.scrollTop = keepTop;
}

/* 总览右栏定高：通知 / 机会榜 / 持仓管理员三张卡合起来恰好等于左侧「盈亏」卡高度，
   超出的内容各自在卡片内部滚动（CSS 负责把这段高度按比例分给三张卡），绝不撑高页面。
   不用固定像素——左卡高度随平仓笔数、日历行数变化，窗口缩放也会改变换行，只能实测。
   写完高度不会再触发重排循环：右栏高度对左卡自身的布局没有影响。 */
let _sideFitRaf = 0;

function fitOverviewSide() {
  const side = document.querySelector('.ov-side');
  const main = document.querySelector('.pnl-card');
  if (!side || !main) return;
  // ≤1180px 总览是单列，右栏与左卡不再左右并排，「底部对齐」不成立，交还自然高度
  if (window.matchMedia('(max-width: 1180px)').matches) {
    if (side.style.height) side.style.height = '';
    return;
  }
  // 切到别的页签时元素 display:none，rect 全是 0，量出来会把整栏压扁
  if (!side.offsetParent || !main.offsetParent) return;
  const h = Math.round(main.getBoundingClientRect().height);
  // 高度没变就别写：这段会被 3 秒轮询反复调用，避免无谓的样式重算
  const next = h > 0 ? `${h}px` : '';
  if (side.style.height !== next) side.style.height = next;
}

/* 合并同一帧内的多次请求：renderPnl 里在 DOM 改完之后调用，rAF 保证量到的是最终布局 */
function scheduleSideFit() {
  if (_sideFitRaf) return;
  _sideFitRaf = requestAnimationFrame(() => {
    _sideFitRaf = 0;
    try { fitOverviewSide(); } catch (_) { /* 布局异常不能连累状态轮询 */ }
  });
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
  // 只认布尔 true：直接把本函数当 click 处理器时，首参会被浏览器塞成 MouseEvent，
  // truthy 会把成功提示静默吞掉（历史 bug）。「测试连接」里传 true 才是真的静默。
  const silent = quiet === true;
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
    if (!silent) toast('模型设置已保存', 'ok');
    $('llmApiKey').value = '';
    loadLlmSettings();
    poll();
    return true;
  } catch (e) {
    toast('模型设置保存失败：' + e.message, 'err');
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
  const silent = quiet === true;   // 见 saveLlmSettings：事件对象不能当 quiet 用
  const payload = {
    enabled: $('sentimentEnabled').checked,
    source: $('sentimentSource').value,
  };
  const key = $('sentimentApiKey').value.trim();
  if (key) payload.cryptopanic_api_key = key;
  try {
    await api('/api/settings/sentiment', 'POST', payload);
    if (!silent) toast('情绪新闻设置已保存', 'ok');
    $('sentimentApiKey').value = '';
    loadSentimentSettings();
    return true;
  } catch (e) {
    toast('情绪新闻设置保存失败：' + e.message, 'err');
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
  const silent = quiet === true;   // 见 saveLlmSettings：事件对象不能当 quiet 用
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
    if (!silent) toast('通知告警设置已保存', 'ok');
    loadNotifySettings();
    return true;
  } catch (e) {
    toast('通知告警设置保存失败：' + e.message, 'err');
    return false;
  }
}

/* ---------------- 持仓管理员 · LLM 接入设置 ---------------- */
async function loadPositionLlmSettings() {
  try {
    const d = await api('/api/settings/position');
    $('posLlmExplain').checked = !!d.llm_explain;
    $('posLlmShadow').checked = !!d.llm_shadow;
    $('posLlmOffpeakOnly').checked = !!d.llm_offpeak_only;
    $('posBarsFromIndex').checked = !!d.bars_from_index;
    $('posPeakDrawdownEnabled').checked = !!d.peak_drawdown_exit_enabled;
    if ($('posAutoTune')) $('posAutoTune').checked = d.auto_tune !== false;
    if ($('posEvolutionMode')) $('posEvolutionMode').value = d.evolution_mode || 'stats';
    if ($('posEvolutionLlmEnabled')) $('posEvolutionLlmEnabled').checked = !!d.evolution_llm_enabled;
    if ($('posEvolutionAllowAction')) $('posEvolutionAllowAction').checked = !!d.evolution_llm_allow_action;
    if ($('posEvolutionAllowLoosen')) $('posEvolutionAllowLoosen').checked = !!d.evolution_llm_allow_loosen;
    if ($('posEnabled')) $('posEnabled').checked = d.enabled !== false;
    const tn = d.tuning || {};
    if ($('posTuneDesc')) {
      const n = Number(tn.samples || 0);
      const need = Number(tn.min_samples || 20);
      $('posTuneDesc').textContent = tn.auto_tune === false
        ? '已关闭：参数完全按你设置的值运行'
        : (n >= need
          ? `已启用 · 已学 ${n} 笔样本，参数会随实盘结果缓慢演进（单轮最多 12%，有硬边界）`
          : `已启用 · 已攒 ${n}/${need} 笔样本，攒够后开始自动微调；改配置即重置`);
    }
    $('posLlmMinInterval').value = d.llm_min_interval_sec;
    $('posReversalEmaBars').value = d.reversal_ema_bars;
    $('posCompositeMaxAge').value = d.composite_max_age_sec;
    $('posPeakDrawdownR').value = d.peak_drawdown_r;
    if ($('posEvolutionShadowCases')) $('posEvolutionShadowCases').value = d.evolution_llm_shadow_cases ?? 20;
    if ($('posEvolutionMinConfidence')) $('posEvolutionMinConfidence').value = d.evolution_llm_min_confidence ?? 0.65;
    // 实际生效值提示：L1/L3 学习会覆盖 config 里的值，两者不一致时明确提示
    const eff = d.effective || {};
    if (eff.reversal_ema_bars != null && Number($('posReversalEmaBars').value) !== eff.reversal_ema_bars) {
      $('posReversalEmaBars').title = `实际生效 ${eff.reversal_ema_bars}（已被 L1/L3 学习调整；改配置即重置学习）`;
    }
    if (eff.peak_drawdown_r != null && Number($('posPeakDrawdownR').value) !== eff.peak_drawdown_r) {
      $('posPeakDrawdownR').title = `实际生效 ${eff.peak_drawdown_r}（已被 L1/L3 学习调整；改配置即重置学习）`;
    }
    const off = d.offpeak || {};
    $('posLlmOffpeak').textContent = off.label || '--';
    const rj = d.replay_job || {};
    if ($('posReplayState')) {
      $('posReplayState').textContent = rj.running
        ? 'L2 验收运行中…'
        : (rj.error ? `L2 验收出错：${rj.error}`
          : (rj.finished_at ? (rj.passed ? 'L2 已通过' : 'L2 未通过') : 'L2 未运行'));
    }
    const u = d.usage || {};
    $('posLlmUsage').textContent =
      `LLM 调用 ${u.calls ?? 0} 次 · 峰时跳过 ${u.skipped_offpeak ?? 0}` +
      ` · 冷却跳过 ${u.skipped_cooldown ?? 0} · 失败 ${u.errors ?? 0}`;
  } catch (e) { console.error(e); }
}

async function savePositionLlmSettings() {
  const payload = {
    enabled: $('posEnabled') ? $('posEnabled').checked : true,
    llm_explain: $('posLlmExplain').checked,
    llm_shadow: $('posLlmShadow').checked,
    llm_offpeak_only: $('posLlmOffpeakOnly').checked,
    bars_from_index: $('posBarsFromIndex').checked,
    peak_drawdown_exit_enabled: $('posPeakDrawdownEnabled').checked,
    auto_tune: $('posAutoTune') ? $('posAutoTune').checked : true,
    evolution_mode: $('posEvolutionMode') ? $('posEvolutionMode').value : 'stats',
    evolution_llm_enabled: $('posEvolutionLlmEnabled') ? $('posEvolutionLlmEnabled').checked : false,
    evolution_llm_allow_action: $('posEvolutionAllowAction') ? $('posEvolutionAllowAction').checked : false,
    evolution_llm_allow_loosen: $('posEvolutionAllowLoosen') ? $('posEvolutionAllowLoosen').checked : false,
    llm_min_interval_sec: Number($('posLlmMinInterval').value),
    reversal_ema_bars: Number($('posReversalEmaBars').value),
    composite_max_age_sec: Number($('posCompositeMaxAge').value),
    peak_drawdown_r: Number($('posPeakDrawdownR').value),
    evolution_llm_shadow_cases: Number($('posEvolutionShadowCases') ? $('posEvolutionShadowCases').value : 20),
    evolution_llm_min_confidence: Number($('posEvolutionMinConfidence') ? $('posEvolutionMinConfidence').value : 0.65),
  };
  try {
    await api('/api/settings/position', 'POST', payload);
    toast('持仓管理员设置已保存', 'ok');
    loadPositionLlmSettings();
    return true;
  } catch (e) {
    toast('持仓管理员设置保存失败：' + e.message, 'err');
    return false;
  }
}

/* ---------------- 配置方案（预设 / 恢复默认 / 撤销） ---------------- */
let profilesCache = null;

async function loadProfiles() {
  try {
    const d = await api('/api/settings/profiles');
    profilesCache = d;
    const sel = $('profileSelect');
    if (!sel) return;
    const keep = sel.value;
    sel.innerHTML = (d.profiles || []).map((p) =>
      `<option value="${esc(p.name)}">${esc(p.label)}${p.matches ? ' · 当前' : ''}</option>`
    ).join('');
    const cur = (d.profiles || []).find((p) => p.matches);
    $('profileCurrent').value = cur ? cur.label : '自定义';
    // 当前配置不匹配任何方案时，下拉显示「自定义」并选中它；
    // 此时不展示任何方案的改动预览（收起），用户主动选方案后才显示。
    sel.innerHTML = (cur ? '' : '<option value="">自定义</option>')
      + (d.profiles || []).map((p) =>
        `<option value="${esc(p.name)}">${esc(p.label)}${p.matches ? ' · 当前' : ''}</option>`
      ).join('');
    if (cur) {
      sel.value = (keep && (d.profiles || []).some((p) => p.name === keep)) ? keep : cur.name;
    } else {
      sel.value = '';
    }
    renderProfileDiff();
  } catch (e) { console.error(e); }
}

// 配置键 → 中文说明。差异预览里把英文键名替换成中文，方便一眼看懂会改什么；
// 名称、单位与下方「交易参数 / 持仓管理员 / 选品」设置表单保持一致。
const SETTING_LABELS = {
  // —— 风控 risk（与「交易参数」表单标签一致）——
  min_confidence: '置信度门槛',
  max_loss_per_trade_pct: '单笔风险上限',
  max_position_pct: '单笔仓位上限',
  max_concurrent_positions: '同时持仓品种数',
  max_margin_usage_pct: '保证金占用上限',
  max_leverage: '杠杆上限',
  min_leverage: '杠杆下限',
  daily_loss_limit_pct: '当日亏损上限',
  max_drawdown_pct: '最大回撤',
  max_portfolio_notional_pct: '组合名义敞口上限',
  correlated_exposure_pct: '相关组敞口上限',
  sizing_mode: '仓位大小',
  leverage_override: '杠杆',
  // —— 持仓管理员 position（与「持仓管理员 · LLM 接入」表单标签一致）——
  auto_tune: '退出参数自校准',
  llm_explain: '规则动作解释',
  llm_shadow: '独立复核',
  llm_offpeak_only: '峰时跳过 LLM',
  bars_from_index: '持有根数按 K 线计数',
  peak_drawdown_exit_enabled: '峰值回撤退出',
  peak_drawdown_r: '峰值回撤阈值（R）',
  reversal_ema_bars: '反转 EMA 跨度（根）',
  composite_max_age_sec: '因子信号有效期（秒）',
  llm_min_interval_sec: 'LLM 冷却（秒 / 每品种）',
  // —— 选品 market ——
  auto_rotate: '自动选品种',
  auto_top_k: '每轮评估品种数',
  min_score: '选品分数门槛',
  rotate_min_score: '选品分数门槛',
  // —— 模型设置 llm（仅峰谷计价策略进方案）——
  offpeak_research_only: '因子研究仅谷时调用',
  peak_mode: '峰时决策',
};
// 单位：与表单输入框后缀一致。比例类 _pct 已在 _fmtSetting 里显示成「X%」，
// 所以这里只补「权益」二字，避免叠成「X% % 权益」。
const SETTING_UNITS = {
  max_loss_per_trade_pct: '权益',
  max_position_pct: '权益',
  max_margin_usage_pct: '权益',
  daily_loss_limit_pct: '权益',
  max_drawdown_pct: '权益',
  max_portfolio_notional_pct: 'x 权益',
  correlated_exposure_pct: 'x 权益',
  max_concurrent_positions: '个',
  auto_top_k: '个',
  max_leverage: 'x',
  min_leverage: 'x',
  llm_min_interval_sec: '秒',
  reversal_ema_bars: '根',
  composite_max_age_sec: '秒',
  peak_drawdown_r: 'R',
};
// 只有这些键是「0~1 的比例」，显示时才乘 100。
// 组合层两个 *_pct 的语义是「倍权益」（例如 16x），不能按百分比渲染。
const PCT_KEYS = new Set([
  'max_loss_per_trade_pct', 'max_position_pct', 'max_margin_usage_pct',
  'daily_loss_limit_pct', 'max_drawdown_pct',
]);
function _settingLabel(key) {
  return SETTING_LABELS[key] || key;
}
// 把文本里的英文参数键换成中文。按长度降序替换，避免 rotate_min_score 被
// 更短的 min_score 先截断成 rotate_选品分数门槛。
// 用途：兼容旧版本写进 state 的 last_result（那时后端直接拼了内部键名）。
const _LABEL_KEYS_BY_LEN = Object.keys(SETTING_LABELS).sort((a, b) => b.length - a.length);
function _localizeKeys(text) {
  if (!text) return text;
  let out = String(text);
  for (const k of _LABEL_KEYS_BY_LEN) {
    if (out.includes(k)) out = out.split(k).join(SETTING_LABELS[k]);
  }
  return out;
}
function _settingUnit(key) {
  return SETTING_UNITS[key] || '';
}
// 枚举型取值 → 中文（与表单选项一致）
function _settingValue(key, v) {
  if (key === 'sizing_mode') return v === 'risk' ? '按风险上限反推' : (v === 'llm' ? '听交易员' : String(v));
  if (key === 'leverage_override') return (Number(v) || 0) === 0 ? '自动（跟随决策）' : `${Number(v)}x`;
  if (key === 'peak_mode') return v === 'thrift' ? '省钱模式（峰时规则兜底）' : (v === 'full' ? '照常使用 LLM' : String(v));
  return null;
}

function _fmtSetting(key, v) {
  const mapped = _settingValue(key, v);
  if (mapped != null) return mapped;
  if (typeof v === 'boolean') return v ? '开' : '关';
  if (typeof v === 'number') {
    // 比例类键（白名单内且值在 0~1）按百分数显示；倍权益类保持原值。
    if (PCT_KEYS.has(String(key || '')) && Math.abs(v) <= 1) {
      return `${Number((v * 100).toFixed(1))}%`;
    }
    return Math.abs(v) < 1 ? String(Number(v.toFixed(4))) : String(v);
  }
  return String(v);
}

// 差异预览里的一行：中文名 + 当前/目标值 + 单位。
function _diffRow(key, current, target) {
  const u = _settingUnit(key);
  const suf = u ? ` ${u}` : '';
  return `· ${_settingLabel(key)}：${esc(_fmtSetting(key, current))}${suf} → <b>${esc(_fmtSetting(key, target))}${suf}</b>`;
}

function renderProfileDiff() {
  const sel = $('profileSelect');
  const box = $('profileDiff');
  if (!sel || !box || !profilesCache) return;
  const p = (profilesCache.profiles || []).find((x) => x.name === sel.value);
  if (!p) { box.hidden = true; box.textContent = ''; return; }
  box.hidden = false;
  const diff = p.diff || [];
  if (!diff.length) {
    box.innerHTML = `<b>${esc(p.summary)}</b><br>当前配置已与该方案一致。`;
    return;
  }
  const head = `<b>${esc(p.summary)}</b><br>将改动 ${diff.length} 项：`;
  const rows = diff.slice(0, 12).map((x) =>
    `<br>${_diffRow(x.key, x.current, x.target)}`).join('');
  box.innerHTML = head + rows + (diff.length > 12 ? `<br>…另有 ${diff.length - 12} 项` : '');
}

async function applyProfile(name) {
  if (!name) return;
  try {
    const r = await api('/api/settings/profile', 'POST', { name });
    toast(`已应用「${r.label}」，改动 ${r.changed} 项`, 'ok');
    await refreshSettingsPanels();
  } catch (e) {
    toast('应用方案失败：' + e.message, 'err');
  }
}

async function rollbackSettings() {
  try {
    const r = await api('/api/settings/rollback', 'POST', {});
    toast(`已撤销：${r.rolled_back || '上一次改动'}`, 'ok');
    await refreshSettingsPanels();
  } catch (e) {
    toast('撤销失败：' + e.message, 'err');
  }
}

/* ---------------- LLM 自主调参 ---------------- */
async function loadEvolveState() {
  try {
    const d = await api('/api/settings/evolve');
    if ($('evolveAuto')) $('evolveAuto').checked = !!d.enabled;
    if ($('evolveInterval')) $('evolveInterval').value = d.interval_hours || 24;
    if ($('evolveState')) {
      $('evolveState').textContent = d.enabled
        ? `已开启 · 每 ${d.interval_hours} 小时 · ${_localizeKeys(d.last_result) || '尚未运行'}`
        : '已关闭（仅手动）';
    }
  } catch (e) { console.error(e); }
}

async function runEvolve(apply) {
  const btn = apply ? $('evolveApplyBtn') : $('evolveDryBtn');
  if (!btn) return;
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '评估中…';
  try {
    const d = await api('/api/settings/evolve', 'POST', { apply: !!apply });
    renderEvolveResult(d);
    if (d.applied) {
      toast('已应用模型建议', 'ok');
      await refreshSettingsPanels();
    } else if (!d.ok) {
      toast('建议未通过代码校验，未应用', 'err');
    } else {
      toast('评估完成（未应用）', 'ok');
    }
  } catch (e) {
    toast('评估失败：' + e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

function renderEvolveResult(d) {
  const box = $('evolveResult');
  if (!box) return;
  const p = d.proposal || {};
  const lines = [];
  if (p.diagnosis) lines.push(`<b>诊断</b>：${esc(p.diagnosis)}`);
  if (p.expected) lines.push(`<b>预期</b>：${esc(p.expected)}`);
  if (p.risk) lines.push(`<b>风险</b>：${esc(p.risk)}`);
  const ch = d.changes || [];
  if (ch.length) {
    lines.push('<b>改动</b>：' + ch.map((c) =>
      `${_settingLabel(c.key)} ${esc(_fmtSetting(c.key, c.from))} → <b>${esc(_fmtSetting(c.key, c.to))}</b>`).join('；'));
  }
  const rj = d.rejected || [];
  if (rj.length) {
    lines.push('<b>被拒</b>：' + rj.map((r) => `${_settingLabel(r.key)}（${esc(r.reason)}）`).join('；'));
  }
  if (!ch.length && !rj.length) lines.push('本轮没有调整建议。');
  if (d.applied) lines.push('<b>已应用</b>，可在「交易参数」页撤销。');
  box.innerHTML = lines.join('<br>');
}

async function refreshSettingsPanels() {
  await loadProfiles();
  await loadPositionLlmSettings();
  await loadEvolveState();
  try { poll(); } catch (e) { /* 状态轮询自己会重试 */ }
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

// 交易对展示与拆分：BTCUSDT → { pair: 'BTC/USDT', base: 'BTC' }。
// base 用作数量的单位（9.0115 BTC），pair 用于标题行；计价资产从长到短匹配，避免 USD 抢先命中 USDT。
const PAIR_QUOTES = ['USDT', 'USDC', 'FDUSD', 'BUSD', 'TUSD', 'USD', 'BTC', 'ETH'];
function splitPair(sym) {
  const s = String(sym || '').toUpperCase();
  for (const q of PAIR_QUOTES) {
    if (s.endsWith(q) && s.length > q.length) {
      const base = s.slice(0, -q.length);
      return { pair: `${base}/${q}`, base };
    }
  }
  return { pair: s, base: '' };
}
// BTCUSDT → BTC/USDT，BTCUSDC → BTC/USDC
function prettySymbol(sym) {
  return splitPair(sym).pair;
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
  // 因子贡献度条形——哪些因子真正在「贡献信号」
  const contribs = (p.factor_contributions || []).filter((x) => x.weight > 0)
    .sort((a, b) => Math.abs(b.share) - Math.abs(a.share)).slice(0, 8);
  let detail = '';
  if (contribs.length) {
    const maxAbs = Math.max(...contribs.map((x) => Math.abs(x.share)), 0.0001);
    detail = `<div class="df-detail">`
      + contribs.map((x) => {
        const w = (Math.abs(x.share) / maxAbs) * 100;
        const d = x.share >= 0 ? 'up' : 'down';
        return `<div class="df-row">
          <span class="df-name">${esc(x.name)}</span>
          <span class="df-track"><span class="df-fill ${d}" style="width:${w.toFixed(1)}%"></span></span>
          <span class="df-num ${d}">${x.share >= 0 ? '+' : ''}${(x.share * 100).toFixed(1)}%</span>
        </div>`;
      }).join('')
      + `</div>`;
  }
  const hasMore = active.length > 3;
  const clampCls = hasMore && !_decisionMore.factors ? ' clamped' : '';
  const more = hasMore
    ? `<button class="df-more" type="button" data-more="factors" aria-expanded="${_decisionMore.factors}">${_decisionMore.factors ? '收起' : '展开更多'}</button>`
    : '';
  return `<div class="df-block">
    <div class="df-head"><span class="df-title">因子信号</span><span class="df-count">${active.length} 个有效因子</span></div>
    <div class="df-conclusion"><b class="df-key ${cls}">${c >= 0 ? '+' : ''}${Number(c).toFixed(2)}</b><span class="df-dir">${dir}</span></div>
    <div class="df-fields">
      <div class="df-field wide${hasMore ? ' df-field-more' : ''}"><span class="df-label">有效因子</span><span class="df-val text${clampCls}">${esc(names)}</span></div>
    </div>
    ${more}
    ${detail}
    <p class="df-note">留出集为负的因子已自动淘汰，不参与决策。</p>
  </div>`;
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

// 运行时长本地每秒自增：以服务器返回的 uptime_sec 为锚点，用本地墙钟推算已运行秒数。
// 用 requestAnimationFrame 逐帧检查、只在「整秒翻滚」那一刻改写文字——既不依赖
// setTimeout 的抖动，也不会一秒内反复重绘；轮询回来只在漂移超过 2 秒时才重新锚定，
// 避免每次轮询都因服务器/本地时钟微小偏差而偶发跳秒或重复。
let _uptimeAnchor = null; // { sec, wall }
let _uptimeLastSec = -1;
let _uptimeRAF = 0;
function _renderUptimeFrame() {
  if (_uptimeAnchor) {
    const total = _uptimeAnchor.sec + Math.floor((Date.now() - _uptimeAnchor.wall) / 1000);
    if (total !== _uptimeLastSec) {
      _uptimeLastSec = total;
      const txt = `已运行 ${fmtUptime(total)}`;
      for (const id of ['uptimeTag', 'uptimeTagM']) {
        const el = $(id);
        if (el) el.textContent = txt;
      }
    }
  }
  _uptimeRAF = requestAnimationFrame(_renderUptimeFrame);
}
// 服务器返回 uptime_sec 时调用：首次建立锚点；之后仅在漂移过大时纠正。
function _anchorUptime(sec, startedAt) {
  const wall = Date.now();
  if (_uptimeAnchor) {
    const expected = _uptimeAnchor.sec + Math.floor((wall - _uptimeAnchor.wall) / 1000);
    if (Math.abs(expected - sec) <= 2) return; // 漂移很小，保持当前平滑计数，不跳秒
  }
  _uptimeAnchor = { sec, wall };
  _uptimeLastSec = -1;
  if (!_uptimeRAF) _uptimeRAF = requestAnimationFrame(_renderUptimeFrame);
  if (startedAt) {
    for (const id of ['uptimeTag', 'uptimeTagM']) {
      const el = $(id);
      if (el) el.title = `启动于 ${startedAt}`;
    }
  }
}

function fmtUptime(sec) {
  // 只保留两级单位：天/小时、小时/分、分/秒——侧栏位置窄，写全「1天2小时3分4秒」会折行。
  const n = Math.max(0, Math.floor(Number(sec) || 0));
  const d = Math.floor(n / 86400);
  const h = Math.floor((n % 86400) / 3600);
  const m = Math.floor((n % 3600) / 60);
  const s = n % 60;
  if (d) return `${d} 天 ${h} 小时`;
  if (h) return `${h} 小时 ${m} 分`;
  if (m) return `${m} 分 ${s} 秒`;
  return `${s} 秒`;
}

function renderPositionManagement(d) {
  const card = $('posMgmtCard');
  if (!card) return;
  const m = (d && d.position_management) || {};
  const last = (d && d.position_management_last) || {};
  const all = (d && d.position_managements) || {};
  const shadow = (d && d.position_llm_shadow) || [];
  const guard = (d && d.position_guard) || {};
  const tune = (d && d.position_tuning) || {};
  // 最近一次动作 / 独立复核记录常驻，不再被 30 秒后的 hold 巡检冲掉
  const has = (m.action && m.action !== 'hold') || !!last.action || shadow.length > 0
    || Number(tune.samples || 0) > 0;
  card.classList.toggle('hidden', !has);
  if (!has) return;

  const ago = (ts) => {
    const t = Number(ts) || 0;
    if (!t) return '--';
    const s = Math.max(0, Math.round(Date.now() / 1000 - t));
    return s < 60 ? `${s}s 前` : (s < 3600 ? `${Math.floor(s / 60)}m 前` : `${Math.floor(s / 3600)}h 前`);
  };
  const actCn = (a) => ({ hold: '持有', tighten: '收紧止损', close: '平仓' }[a] || a || '--');
  let html = '';

  if (last.action) {
    const cls = last.action === 'close' ? 'down' : 'up';
    html += `<div class="pm-block">
      <div class="pm-block-title">最近一次动作 <span class="muted">${esc(ago(last.ts))}</span></div>
      <p><b class="${cls}">${esc(actCn(last.action))}</b>${last.symbol ? ' · ' + esc(last.symbol) : ''}
        · ${esc(last.reason || '')}</p>
      ${last.new_stop ? `<p class="muted">新止损 ${fmt(last.new_stop)}</p>` : ''}
      ${last.explanation ? `<p class="pm-explain">${esc(last.explanation)}</p>` : ''}
    </div>`;
  }

  const rows = Object.entries(all).sort((a, b) => (Number(b[1].ts) || 0) - (Number(a[1].ts) || 0));
  if (rows.length) {
    html += `<div class="pm-block"><div class="pm-block-title">各品种巡检</div><div class="pm-rows">`;
    for (const [sym, o] of rows.slice(0, 6)) {
      const r = o && o.details && o.details.floating_r;
      html += `<span class="pm-row"><b>${esc(sym)}</b>
        <span class="${o.action === 'close' ? 'down' : (o.action === 'tighten' ? 'up' : 'muted')}">${esc(actCn(o.action))}</span>
        ${r != null ? `<span class="muted">${Number(r) >= 0 ? '+' : ''}${Number(r).toFixed(2)}R</span>` : ''}
        <span class="muted">${esc(ago(o.ts))}</span></span>`;
    }
    html += `</div></div>`;
  }

  if (shadow.length) {
    html += `<div class="pm-block"><div class="pm-block-title">LLM 独立复核 <span class="muted">只读对照</span></div><div class="pm-rows">`;
    for (const r of shadow.slice(-5).reverse()) {
      html += `<span class="pm-row"><b>${esc(r.symbol || '')}</b>
        <span>规则 ${esc(actCn(r.rule_action))}</span>
        <span class="${r.llm_action === 'close' ? 'down' : ''}">LLM ${esc(actCn(r.llm_action))}</span>
        <span class="muted">${esc(ago(r.ts))}</span></span>`;
    }
    html += `</div></div>`;
  }

  // 演进状态：L1/L2/L3 的当前层级、门控与按规则归因
  const byRule = tune.by_rule || {};
  const gate = tune.gate || {};
  const llm = tune.llm || {};
  const modeCn = { off: '关闭', stats: 'L1 统计', replay: 'L2 回放', llm: 'L3 LLM' }[tune.mode] || tune.mode || '--';
  html += `<div class="pm-block">
    <div class="pm-block-title">演进状态 <span class="muted">${esc(modeCn)}</span></div>
    <p class="muted">${gate.l2_passed ? 'L2 回放已通过' : (gate.l2_cases ? `L2 未通过（${gate.l2_cases} 笔样本）` : 'L2 未评估')}
      ${gate.l2_median_delta != null ? ` · 中位差 ${Number(gate.l2_median_delta) >= 0 ? '+' : ''}${Number(gate.l2_median_delta).toFixed(2)}R` : ''}
      ${llm.enabled ? ' · L3 已启用' : ''}</p>
    ${Object.keys(byRule).length ? `<div class="pm-rows">${Object.entries(byRule).slice(0, 5).map(([k, v]) =>
      `<span class="pm-row"><b>${esc(v.label || k)}</b>
       <span class="muted">${Number(v.n || 0)} 笔 · 均 ${Number(v.avg_r || 0) >= 0 ? '+' : ''}${Number(v.avg_r || 0).toFixed(2)}R</span>
       <span class="muted">捕获 ${v.capture == null ? '--' : Number(v.capture).toFixed(2)}</span></span>`).join('')}</div>` : ''}
    ${(llm.rollbacks || []).length ? `<p class="down">已回退：${esc((llm.rollbacks[llm.rollbacks.length - 1] || {}).reason || '')}</p>` : ''}
  </div>`;

  // 自迭代：攒样本 → 自动微调参数。只展示「已经变了的」参数，避免刷屏。
  if (Number(tune.samples || 0) > 0) {
    const base = tune.base || {};
    const eff = tune.effective || {};
    const moved = Object.keys(eff)
      .filter((k) => base[k] != null && Math.abs(Number(eff[k]) - Number(base[k])) > 1e-9);
    const cap = tune.capture_median;
    html += `<div class="pm-block">
      <div class="pm-block-title">参数自校准
        <span class="muted">${tune.calibrated ? '已启用' : `攒样本 ${tune.samples}/${tune.min_samples || 20}`}</span>
      </div>
      <p class="muted">已学 ${Number(tune.samples)} 笔
        ${cap != null ? ` · 峰值捕获率 ${Number(cap).toFixed(2)}` : ''}
        ${tune.auto_tune === false ? ' · 当前关闭（按你设置的值运行）' : ''}</p>
      ${moved.length
        ? `<div class="pm-rows">${moved.slice(0, 4).map((k) =>
            `<span class="pm-row"><b>${esc(k)}</b>
             <span class="muted">${Number(base[k]).toFixed(3)} → ${Number(eff[k]).toFixed(3)}</span></span>`).join('')}</div>`
        : `<p class="muted">参数暂未调整${tune.calibrated ? '（当前表现已在目标区间）' : ''}。</p>`}
    </div>`;
  }

  html += `<p class="muted">守护线程${guard.running ? '运行中' : '未运行'}，
    每 ${guard.interval_sec || 30} 秒检查一次，不依赖自动交易循环。</p>`;
  $('posMgmtBody').innerHTML = html;
}

function renderDecisionShadow(d) {
  const box = $('decisionShadowBox');
  if (!box) return;
  // 与设置页「独立复核」开关同源（status.position_llm_shadow = llm_shadow_log[-10:]）：
  // 记录规则动作与 LLM 独立意见对照，纯只读留档，不改 action。移到决策页后由状态轮询刷新。
  const rows = (d && d.position_llm_shadow) || [];
  // 动作枚举统一显示中文（与持仓管理员卡片同一套译名）
  const actCn = (a) => ({ hold: '持有', tighten: '收紧止损', close: '平仓' }[a] || a || '--');
  const hasMore = rows.length > 2;
  const collapsed = hasMore && !_decisionMore.shadow;
  const more = hasMore
    ? `<button class="df-more" type="button" data-more="shadow" aria-expanded="${_decisionMore.shadow}">${_decisionMore.shadow ? '收起' : '展开更多'}</button>`
    : '';
  box.innerHTML = rows.length
    ? `<div class="df-head"><span class="df-title">独立复核</span><span class="df-count">最近 ${rows.length} 条</span></div>
       <div class="shadow-list${collapsed ? ' collapsed' : ''}">`
      + rows.slice().reverse().map((r) => {
        const same = r.rule_action === r.llm_action;
        return `<div class="df-item">
          <div class="df-head">
            <span class="df-sym">${esc(r.symbol || '')}</span>
            <span class="df-flags">
              ${r.degraded ? '<span class="df-flag warn">降级</span>' : ''}
              <span class="df-flag ${same ? 'same' : 'diff'}">${same ? '一致' : '不同'}</span>
            </span>
          </div>
          <div class="df-fields three">
            <div class="df-field"><span class="df-label">规则</span><span class="df-val">${esc(actCn(r.rule_action))}</span></div>
            <div class="df-field"><span class="df-label">LLM</span><span class="df-val${same ? '' : ' df-em'}">${esc(actCn(r.llm_action))}</span></div>
            <div class="df-field"><span class="df-label">置信</span><span class="df-val">${Number(r.confidence || 0).toFixed(2)}</span></div>
          </div>
          ${r.reason ? `<p class="df-note">${esc(r.reason)}</p>` : ''}
        </div>`;
      }).join('')
      + `</div>${more}`
    : `<div class="muted center">暂无独立复核记录（开启「独立复核」并产生持仓动作后显示）</div>`;
}

function renderResearchAuto(a) {
  const chk = $('autoResearchChk');
  const el = $('autoResearchState');
  if (!chk || !el || !a) return;
  // 用户正在操作这个开关时不要回写：3 秒轮询会把刚点的开关弹回旧状态
  if (document.activeElement !== chk) chk.checked = !!a.enabled;
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
  if (document.activeElement !== chk) chk.checked = on;
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
  if (document.activeElement !== chk) chk.checked = on;
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
  // 时间戳拆成「日期 + 时间」两段：PC 显示完整，手机端只留时间（CSS 隐藏日期）
  const ts = String(data.ts || '');
  const sp = ts.indexOf(' ');
  const datePart = sp > 0 ? ts.slice(0, sp) : '';
  const timePart = sp > 0 ? ts.slice(sp + 1) : ts;
  $('scanMeta').innerHTML = `${data.rows.length} 个品种 · `
    + (datePart ? `<span class="scan-meta-date">${esc(datePart)}</span> ` : '')
    + `<span class="scan-meta-time">${esc(timePart)}</span>`;
  const tb = $('scanTable').querySelector('tbody');
  if (!data.rows.length) { tb.innerHTML = '<tr><td colspan="7" class="muted center">没有结果</td></tr>'; return; }
  tb.innerHTML = data.rows.map((r) => {
    if (!r.ok) return `<tr><td class="mono">${esc(r.symbol)}</td><td colspan="6" class="muted">${esc(r.error)}</td></tr>`;
    const cls = r.action === 'long' ? 'up' : r.action === 'short' ? 'down' : 'muted';
    const verdict = r.approved ? '<span class="up">通过</span>' : '<span class="down">否决</span>';
    return `<tr>
      <td class="mono">${esc(r.symbol)}</td>
      <td>${fmt(r.price)}</td>
      <td class="${cls}">${esc(dirCn(r.action))}</td>
      <td>${(r.size_pct * 100).toFixed(1)}%</td>
      <td>${fmt(r.leverage, 1)}x</td>
      <td>${verdict}</td>
      <td class="muted scan-reason"><span class="reason-text">${esc((r.reasons || []).join('；') || '—')}</span><button class="reason-toggle" type="button" onclick="const c=this.closest('.scan-reason');const ex=c.classList.toggle('expanded');this.textContent=ex?'收起':'展开'">展开</button></td>
    </tr>`;
  }).join('');
  fitScanReasons();
}

// 按实际渲染行数决定「理由」是否折叠（仅移动端有折叠样式，PC 端 clamp 未生效、按钮本就隐藏）：
//   ≤3 行 → 完整展示、隐藏展开按钮（加 .fit）
//   >3 行 → 默认折叠 3 行、显示展开按钮
// 元素处于隐藏 tab（clientHeight=0）时跳过，待切到决策页由 showTab 再次测量。
function fitScanReasons() {
  document.querySelectorAll('#scanTable .scan-reason').forEach((td) => {
    const rt = td.querySelector('.reason-text');
    if (!rt) return;
    if (rt.clientHeight === 0) return; // 未布局（隐藏 tab），跳过
    const overflows = rt.scrollHeight - rt.clientHeight > 1;
    td.classList.toggle('fit', !overflows);
    if (!overflows) {
      td.classList.remove('expanded');
      const b = td.querySelector('.reason-toggle');
      if (b) b.textContent = '展开';
    }
  });
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
  $('dataSource').textContent = loading ? '加载中…' : shortSource(s.data_source);
  $('dataSource').title = s.data_source || '';
  $('llmTag').textContent = `LLM ${s.llm.provider}`;
  if (s.server && s.server.version) $('verTag').innerHTML = `<span class="ver-v">V</span>${esc(s.server.version)}`;
  if (s.uptime_sec != null) {
    // 以服务器返回为锚点自增；仅在漂移过大时纠正，避免每次轮询都跳秒。
    _anchorUptime(s.uptime_sec, s.started_at);
  }
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
  $('haltBtn').textContent = s.halt ? '解除熔断' : '紧急熔断';

  // 数据来源：解锁后自动切到交易所账户，不再显示本地模拟账本
  const ex = s.account_view === 'exchange';
  const exOk = ex && !s.account_error;
  const chip = $('equitySrc');
  // 标注要具体：哪个交易所 + Demo/实盘；不可达时只显示「本地」。
  // 用短词 Demo 代替「模拟盘」压缩标注框宽度，具体报错留在 chip.title 与告警条里。
  const srcTag = s.account_error
    ? '本地'
    : (ex ? `${exName}·${s.testnet ? 'Demo' : '实盘'}` : '本地');
  chip.textContent = srcTag;
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
  if (exOk) posChip.textContent = srcTag;   // 与权益卡一致：具体交易所 + Demo/实盘

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
      // 单笔：大字放单笔浮动盈亏（下方小字再放开仓价与开仓数量）
      const u = p.unrealized == null ? null : Number(p.unrealized);
      $('position').textContent = u == null ? '—' : `${u >= 0 ? '+' : ''}${fmt(u)}`;
      $('position').className = u == null ? '' : (u >= 0 ? 'up' : 'down');
      posSym.textContent = shortSym(p.symbol || s.symbol) || '—';
    } else {
      // 多笔：大字放综合浮盈亏（下方小字放品种名称，点卡片跳交易页）
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
    // 后端 position_management 已按「当前仍有持仓的品种」过滤（已平仓品种留下的
    // action=close 不再冒充心跳）。下面这段只在持仓品种里重取一次最新，纯兜底：
    // 兜底后端字段缺失/版本较旧的情况。（完全空仓走 else 分支，整块不渲染。）
    const mgMap = s.position_managements || {};
    let pm = s.position_management || {};
    let pmTs = Number(pm.ts) || 0;
    for (const _p of posAll) {
      const _sym = String(_p.symbol || s.symbol || '').toUpperCase();
      const _e = mgMap[_sym];
      const _t = _e ? (Number(_e.ts) || 0) : 0;
      if (_e && _t > pmTs) { pm = _e; pmTs = _t; }
    }
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
    // 单笔：下方小字放「开仓价 + 开仓数量」；多笔：下方小字放「品种名称」
    let head;
    if (posAll.length === 1) {
      const q = Math.abs(Number(p.qty) || 0);
      const qtyTxt = q >= 1000 ? fmt(p.qty, 0) : q >= 1 ? fmt(p.qty, 2) : fmt(p.qty, 4);
      head = `开仓价 ${esc(fmtPx(p.entry))} · 开仓量 ${esc(qtyTxt)}`;
    } else {
      head = posAll.slice(0, 4).map((x) => esc(shortSym(x.symbol))).join(' / ')
        + (posAll.length > 4 ? ` +${posAll.length - 4}` : '')
        + (known ? '' : ' · 部分待报价');
    }
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
    // 滑点统一成「成本口径」：正数 = 成交价劣于计划价（吃亏），负数 = 优于计划价。
    // 原始 slippage_bps 是「成交价 − 计划价」，方向没归一化——买入价高是吃亏、
    // 卖出价低也是吃亏，直接用符号判红绿必然有一半是错的。
    const slipOf = (f) => {
      if (f.slippage_bps === null || f.slippage_bps === undefined) return null;
      const raw = Number(f.slippage_bps);
      if (!Number.isFinite(raw)) return null;
      return String(f.side || '').toUpperCase() === 'SHORT' ? -raw : raw;
    };
    const avgSlip = (() => {
      const xs = s.fills.map(slipOf).filter((x) => x !== null);
      return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
    })();
    // 展示层合并：仅改变渲染出的行，s.fills 原始明细与上面的平均滑点统计均不受影响
    const shown = mergeFillsForDisplay(s.fills);
    if ($('fillMeta')) {
      const mergeNote = shown.length < s.fills.length ? ` · 合并显示为 ${shown.length} 条` : '';
      $('fillMeta').textContent = `开仓 / 平仓流水（最近 ${s.fills.length} 条${mergeNote}）`
        + (avgSlip !== null ? ` · 平均滑点 ${avgSlip >= 0 ? '+' : ''}${avgSlip.toFixed(2)}bps` : '');
    }
    tb.innerHTML = shown.map((f) => {
      const cls = f.side === 'LONG' ? 'up' : 'down';
      const slipVal = slipOf(f);
      // bps 做主口径：跨品种可比、与手续费同单位（fee 也是 5bps 这种量级）。
      // 绝对价差放进 title——「差 6.5U」在 BTC 上只有 8.6bps，BNB 差 1.11U 却是
      // 15.95bps，单看价差会把严重程度判反，所以不做主显示。
      const slipTitle = (() => {
        if (slipVal === null) return '';
        const plan = Number(f.plan_price), fill = Number(f.price);
        if (!Number.isFinite(plan) || !Number.isFinite(fill) || plan <= 0 || fill <= 0) {
          return `成本滑点 ${slipVal >= 0 ? '+' : ''}${slipVal.toFixed(2)}bps`;
        }
        const short = String(f.side || '').toUpperCase() === 'SHORT';
        const diffCost = short ? plan - fill : fill - plan;
        const sgn = diffCost > 0 ? '+' : (diffCost < 0 ? '-' : '');
        // 价差按自身量级取 2~6 位：fmtPx 是按绝对价格定精度的，
        // 直接套会把 1.11 打成「1.1100」，读起来像精度噪声。
        const diffAbs = Math.abs(diffCost);
        const dDigits = diffAbs >= 1 ? 2 : diffAbs >= 0.1 ? 3 : diffAbs >= 0.01 ? 4 : 6;
        return `计划 ${fmtPx(plan)} → 成交 ${fmtPx(fill)}，价差 ${sgn}${diffAbs.toFixed(dDigits)}`
          + `（${slipVal >= 0 ? '+' : ''}${slipVal.toFixed(2)}bps）。正数 = 吃亏，负数 = 占便宜`;
      })();
      const slip = slipVal === null
        ? '--'
        : `<span class="${slipVal > 0 ? 'up' : (slipVal < 0 ? 'down' : 'muted')}" title="${esc(slipTitle)}">${slipVal >= 0 ? '+' : ''}${slipVal.toFixed(2)}bps</span>`;
      const tip = f.reason ? ` title="${esc(f.reason)}"` : '';
      const mergeTag = f.__n ? `<span class="merge-tag">${f.__n} 笔合并</span>` : '';
      // 开仓成交没有已实现盈亏：盈亏列开仓显示「--」，平仓才显示实际盈亏。
      // 本地成交带 action=OPEN/CLOSE 可直接判；但线上(armed+live)展示的是交易所回读成交，
      // 其 action 为「买入/卖出」、pnl 为 realizedPnl（开仓=0），没有 OPEN 标记，
      // 因此统一用「是否有已实现盈亏」来识别开仓：pnl≈0 且非明确平仓动作 → 开仓。
      const realized = f.pnl !== undefined && Math.abs(f.pnl) > 1e-6;
      const actLabel = f.__acts || actionCn(f.action) || (f.action || '');
      const isExplicitClose = f.action === 'CLOSE' || f.action === '平仓' || actLabel.includes('平仓');
      const isOpenOnly = !isExplicitClose && (
        f.action === 'OPEN' || actLabel === '开仓'
        || (!realized && (actLabel.includes('买入') || actLabel.includes('卖出')))
      );
      // 动作列：统一标识「开仓 / 平仓」，不再把交易所的「买入/卖出」当成开平仓动作
      const actText = isOpenOnly ? '开仓' : '平仓';
      const pnlCell = isOpenOnly
        ? `<td class="muted">--</td>`
        : `<td class="${(f.pnl || 0) >= 0 ? 'up' : 'down'}">${f.pnl !== undefined ? fmt(f.pnl) : '--'}</td>`;
      return `<tr${tip}>
      <td class="mono">${esc(f.symbol || s.symbol)}<span class="dir-flag ${cls}">${esc(sideCn(f.side))}</span>${mergeTag}<span class="card-ts">${esc(shortTs(f.ts))}</span></td>
      <td class="muted">${esc(f.ts)}</td>
      <td class="${cls}">${esc(actText)}</td>
      <td class="${cls}">${esc(sideCn(f.side))}</td>
      <td>${fmtQty(f.qty)}</td>
      <td>${f.plan_price ? fmtPx(f.plan_price) : '--'}</td>
      <td>${fmtPx(f.price)}</td>
      <td>${slip}</td>
      <td class="muted">${f.fee !== undefined ? fmt(f.fee, 4) : '--'}</td>
      ${pnlCell}
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
      <td class="mono">${esc(sym)}<span class="cell-tag">${esc(modeCn(p.mode || (exOk ? 'live' : 'paper'), s.testnet))}</span><span class="dir-flag ${p.side === 'LONG' ? 'up' : 'down'}">${esc(sideCn(p.side))}</span><span class="card-ts">${esc(shortTs(p.opened_at))}</span></td>
      <td class="${p.side === 'LONG' ? 'up' : 'down'}">${esc(sideCn(p.side))}</td>
      <td>${fmtQty(p.qty)}</td><td>${fmtPx(p.entry)}</td><td>${fmtPx(p.mark)}</td>
      <td>${fmt(p.leverage, 1)}x</td>
      <td class="col-2line"><span class="up">${fmtPx(p.stop)}</span><span class="down">${fmtPx(p.tp)}</span></td>
      <td class="${cls}"${unreal == null ? '' : ` title="预估双边手续费 ${p.fees_est != null ? fmt(p.fees_est) : '--'} U"`}>${unreal == null ? '--' : fmt(unreal)}</td>
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
  // 四套 Key 分 Demo / 主网 两行展示；仅当已配置凭据时才显示这两行
  if ($('keysPresent')) {
    const K = s.keys || {};
    const demo = [];
    if (K.binance && K.binance.demo) demo.push('币安');
    if (K.okx && K.okx.demo) demo.push('OKX');
    const live = [];
    if (K.binance && K.binance.live) live.push('币安');
    if (K.okx && K.okx.live) live.push('OKX');
    const kp = $('keysPresent');
    if (demo.length || live.length) {
      kp.classList.remove('hidden');
      kp.innerHTML = `<span class="k-line">Demo：${demo.length ? demo.join('/') : '无'}</span>` +
                     `<span class="k-line">主网：${live.length ? live.join('/') : '无'}</span>`;
    } else {
      kp.classList.add('hidden');
    }
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
    ? `每 ${s.auto.interval_sec}s 跑一次，每轮评估 ${s.auto.top_k || 1} 个品种，自动下单：${s.auto.execute ? '是' : '否'}，上次：${s.auto.last_run || '--'}`
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
      <td>${esc(h.symbol || '')}<span class="cell-tag">${esc(modeCn(h.mode, lastStatus && lastStatus.testnet))}</span><span class="dir-flag ${h.side === 'LONG' ? 'up' : 'down'}">${dir}</span><span class="card-ts">${esc(shortTs(h.closed_at))}</span></td>
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
  // 盈亏卡是右栏高度的基准：本函数会改成交条数与日历行数，改完（rAF 后）重算右栏
  scheduleSideFit();
  const totalEl = $('pnlTotal');
  const statsEl = $('pnlStats');
  const tradesEl = $('pnlTrades');
  if (!totalEl || !statsEl || !tradesEl) return;

  // 已平仓记录：优先用「持仓历史」（新数据源），没有才退回成交里的 pnl
  // 后端已按当前账户过滤，切到 OKX 模拟盘后不会混进币安模拟盘的记录。
  const hist = (lastStatus?.position_history || []).slice()
    .sort((a, b) => String(a.closed_at).localeCompare(String(b.closed_at)));
  const fills = hist.length
    ? hist.map((h) => ({ ts: h.closed_at, pnl: h.pnl, action: 'CLOSE' }))
    : (lastStatus?.fills || []).filter((f) => Math.abs(f.pnl || 0) > 1e-9)
      .slice().sort((a, b) => String(a.ts).localeCompare(String(b.ts)));

  // 累计值用后端按账户聚合的全量摘要（不受 status 只回 30 条的窗口限制）。
  // 只有「摘要为空但成交里有 pnl」时才退回前端累加。
  const summary = lastStatus?.pnl || null;
  // 只有开仓费、还没有任何平仓时也要用后端摘要：否则会退回前端只加 pnl，
  // 把「开仓即计入已实现」的手续费漏掉，累计盈亏显示成 0。
  const useSummary = !!(summary && (Number(summary.trades) > 0 || Number(summary.fees_paid) > 0 || Number(summary.funding_paid) !== 0 || !fills.length));
  const trades = useSummary ? Number(summary.trades || 0) : fills.length;
  const total = useSummary
    ? Number(summary.realized || 0)
    : fills.reduce((s, f) => s + (f.pnl || 0), 0);
  totalEl.textContent = `${total >= 0 ? '+' : ''}${fmt(total, 2)} U`;
  totalEl.className = total >= 0 ? 'up' : 'down';
  if (useSummary) {
    const gross = Number(summary.gross_pnl || 0);
    const fees = Number(summary.fees_paid || 0);
    const funding = Number(summary.funding_paid || 0);
    const fundingStr = funding !== 0
      ? `${funding > 0 ? ' − 已发生资金费' : ' + 收到资金费'} ${fmt(Math.abs(funding), 2)} U`
      : '';
    totalEl.title = `平仓毛盈亏 ${gross >= 0 ? '+' : ''}${fmt(gross, 2)} U − 已发生手续费 ${fmt(fees, 2)} U${fundingStr}`
      + ` = ${total >= 0 ? '+' : ''}${fmt(total, 2)} U；开仓费、平仓费、资金费在发生时即计入`;
  }

  const winCount = useSummary ? Number(summary.wins || 0) : fills.filter((f) => (f.pnl || 0) > 0).length;
  const winRate = trades ? `${Math.round(winCount / trades * 100)}%` : '--';
  const grossWin = useSummary
    ? Number(summary.gross_win || 0)
    : fills.filter((f) => (f.pnl || 0) > 0).reduce((s, f) => s + (f.pnl || 0), 0);
  const grossLoss = useSummary
    ? Number(summary.gross_loss || 0)
    : Math.abs(fills.filter((f) => (f.pnl || 0) < 0).reduce((s, f) => s + (f.pnl || 0), 0));
  const pf = grossLoss > 0 ? fmt(grossWin / grossLoss, 2) : (grossWin > 0 ? '∞' : '--');
  statsEl.innerHTML = `
    <div class="pnl-stat"><label>平仓笔数</label><b>${trades}</b></div>
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
      <span class="pnl-time" title="${esc(f.ts || '')}">${esc(shortTs(f.ts))}</span>
      <span class="pnl-act muted">${esc(actionCn(f.action))}</span>
      <div class="pnl-bar-track"><div class="pnl-bar ${up ? 'up' : 'down'}" style="width:${w}%"></div></div>
      <span class="pnl-val ${up ? 'up' : 'down'}">${up ? '+' : ''}${fmt(f.pnl, 2)}</span>
    </div>`;
  }).join('');
  tradesEl.innerHTML = rows;
}

/* ---------------- 决策 ---------------- */
// 移动端两处默认折叠的状态放在模块级：独立复核由 3 秒轮询反复渲染，
// 状态外置才能让用户点开的「展开更多」不被下一次轮询收回。
const _decisionMore = { shadow: false, factors: false };
// 较宽的手机屏上有效因子可能本身不足 3 行，此时没有「其余行」，
// 展开按钮就没有可展开的内容——仅在 clamp 真的截断了内容时才出现。
function syncFactorMoreVisibility() {
  const val = document.querySelector('.df-block .df-val.text');
  const btn = document.querySelector('.df-more[data-more="factors"]');
  if (!val || !btn || !val.offsetParent) return;
  const overflowing = val.classList.contains('clamped') && val.scrollHeight > val.clientHeight + 2;
  btn.classList.toggle('df-more-hidden', !overflowing && !_decisionMore.factors);
}
window.addEventListener('resize', syncFactorMoreVisibility);
document.addEventListener('click', (e) => {
  const btn = e.target.closest && e.target.closest('.df-more');
  if (!btn) return;
  const key = btn.dataset.more;
  if (key !== 'shadow' && key !== 'factors') return;
  _decisionMore[key] = !_decisionMore[key];
  btn.textContent = _decisionMore[key] ? '收起' : '展开更多';
  btn.setAttribute('aria-expanded', String(_decisionMore[key]));
  if (key === 'shadow') {
    const list = document.querySelector('#decisionShadowBox .shadow-list');
    if (list) list.classList.toggle('collapsed', !_decisionMore.shadow);
  } else {
    const val = document.querySelector('.df-block .df-val.text');
    if (val) val.classList.toggle('clamped', !_decisionMore.factors);
  }
});

// 决策被跳过（当前品种有持仓 / 持仓已满）时给一条常驻说明。
// 后端在跑 LLM 之前就直接返回，所以是「瞬间完成」——不是模型没调用或功能失效。
// 后端 reason 形如「BNBUSDT 已有持仓，等平仓完成后再做开仓决策」，取首句即可
const skipHead = (d) => String((d && d.reason) || '').split(/[，,。]/)[0].trim();
function renderDecisionSkip(d) {
  const box = $('decisionSkipNote');
  if (!box) return;
  if (!d || !d.skipped) {
    box.classList.add('hidden');
    box.innerHTML = '';
    return;
  }
  box.innerHTML = `<b>未运行决策</b>：${esc(skipHead(d))}。`
    + '<span class="ds-sub">持仓期间不重复开仓（节省 LLM 调用），平仓后可再次运行。</span>';
  box.classList.remove('hidden');
}

function renderDecision(d, switchTab = true) {
  if (d && d.skipped) { renderDecisionSkip(d); return; }
  if (!d || !d.analysts) return;
  renderDecisionSkip(null);   // 真正跑完一次决策后，清掉上一次的「跳过」说明
  latestDecision = d;
  const a = d.analysts || [];
  const analysts = a.map((x) => {
    const cls = x.stance > 0.1 ? 'up' : x.stance < -0.1 ? 'down' : 'muted';
    const flags = x.data_flags || [];
    return `<div class="analyst ${cls}">
      <h3>${esc(x.role)} <span class="stance ${cls}">${x.stance >= 0 ? '+' : ''}${Number(x.stance).toFixed(2)}</span></h3>
      <div class="analyst-body"><p>${esc(x.summary)}</p></div>
      ${flags.length ? `<div class="analyst-flags">${flags.map((f) => `<span class="flag">⚑ ${esc(f)}</span>`).join('')}</div>` : ''}
    </div>`;
  }).join('');

  const p = d.proposal, r = d.risk_decision, o = d.order;
  // 订单方向中文：后端 order.side 是 BUY/SELL、position_side 是 LONG/SHORT。
  // 原来用 sideCn(o.side)，它只认 LONG/SHORT，会把 "SELL" 原样吐出来（第一行显示英文）；
  // 且 o.side === 'short' 永远不成立，颜色也恒为红。这里两者都按 BUY/SELL + LONG/SHORT 判。
  const oSideRaw = String((o && (o.position_side || o.side)) || '').toUpperCase();
  const oIsSell = oSideRaw === 'SHORT' || oSideRaw === 'SELL';
  const oIsBuy = oSideRaw === 'LONG' || oSideRaw === 'BUY';
  const oSideCn = oIsSell ? '做空' : (oIsBuy ? '做多' : '');
  const oSideCls = oIsSell ? 'down' : 'up';
  // 首行：BNB/USDT 做空 9.0115 BNB（品种 动作 数量 基础资产）
  const oPair = splitPair(o && o.symbol);
  const oUnit = oPair.base ? ` ${esc(oPair.base)}` : '';
  // 只有「做多/做空」上红绿，品种、数量、单位保持正文色（整行染色会让其他信息也带上涨跌语义）
  const orderHtml = o
    ? `<div class="order-lead"><b>${esc(oPair.pair)} <span class="${oSideCls}">${oSideCn}</span> ${fmt(o.quantity, 4)}${oUnit}</b></div>
      <div class="df-fields three">
        <div class="df-field"><span class="df-label">名义</span><span class="df-val">${fmt(o.notional_usd)} USDT</span></div>
        <div class="df-field"><span class="df-label">杠杆</span><span class="df-val">${fmt(o.leverage, 1)}x</span></div>
        <div class="df-field"><span class="df-label">止损</span><span class="df-val">${fmt(o.stop_loss_price)}</span></div>
        <div class="df-field"><span class="df-label">止盈</span><span class="df-val">${fmt(o.take_profit_price)}</span></div>
        <div class="df-field"><span class="df-label">最大亏损</span><span class="df-val">${fmt(o.risk_usd)} USDT</span></div>
        <div class="df-field"><span class="df-label">权益占比</span><span class="df-val">${(o.max_loss_pct_of_equity * 100).toFixed(2)}%</span></div>
      </div>
      <p class="df-note">${esc(orderNote(o))}</p>`
    : `<div class="order-reject"><b>本次没有订单</b><span>${esc(r.reasons.join('；'))}</span></div>`;

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
          <div class="item"><span>方向</span><b class="${p.action === 'long' ? 'up' : p.action === 'short' ? 'down' : ''}">${esc(dirCn(p.action))}</b></div>
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
        <p>风控官：<b>${esc(verdictCn(d.risk_officer.verdict))}</b>（仓位乘数 ${Number(d.risk_officer.size_multiplier).toFixed(2)}）</p>
        <ul class="muted">${(d.risk_officer.reasons || []).map((x) => `<li>${esc(x)}</li>`).join('')}</ul>
        <p>风控引擎：<b class="${r.approved ? 'up' : 'down'}">${r.approved ? '通过' : '拒绝'}</b>
           · ${esc(dirCn(r.action))} · 仓位 ${(r.size_pct * 100).toFixed(2)}% · ${fmt(r.leverage, 1)}x</p>
        <ul class="muted">${(r.reasons || []).map((x) => `<li>${esc(x)}</li>`).join('')}</ul>
      </div>
    </div>
    `;
  // 订单计划回到决策页主内容末尾，随页面正常滚动。
  const dock = $('orderDock');
  dock.classList.remove('hidden');
  dock.innerHTML = `<div class="card order-dock-card">
      <div class="card-head"><h2>⑤ 订单计划</h2><span class="muted dock-hint">滚动时保持可见</span></div>
      <div class="order-dock-body" role="region" aria-label="订单计划详情" tabindex="0">${orderHtml}</div>
    </div>`;
  if (switchTab) showTab('decision');
  requestAnimationFrame(syncFactorMoreVisibility);
}

$('runDecision').addEventListener('click', async () => {
  const btn = $('runDecision');
  btn.disabled = true; btn.textContent = '运行中…';
  try {
    const data = await api('/api/decision', 'POST', { rounds: Number($('rounds').value) || 2 });
    if (data && data.skipped) {
      // 跳过不等于失败：说明原因，别弹「决策完成」让用户以为瞬间跑完了整条 LLM 链
      renderDecisionSkip(data);
      toast(`未运行决策：${skipHead(data)}。`, 'err');
      return;
    }
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

// 研究已在后端异步跑，这里轮询 status.research_state 直到完成。
async function waitResearchDone(timeoutSec = 900) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutSec * 1000) {
    const s = await api('/api/status');
    const rs = s.research_state || {};
    if (rs.error) throw new Error(rs.error);
    if (!rs.running) return;
    await new Promise((r) => setTimeout(r, 3000));
  }
  throw new Error('研究超时，请稍后在研究页刷新查看结果');
}

$('runResearch').addEventListener('click', async () => {
  const btn = $('runResearch');
  btn.disabled = true; btn.textContent = '挖掘中…';
  try {
    const started = await api('/api/research', 'POST', { iterations: Number($('iterations').value) || 6 });
    if (!started || !started.started) throw new Error('研究未启动');
    await waitResearchDone();
    const r = await api('/api/research/latest');
    if (r) {
      renderResearch(r, true);
      toast(`研究完成，通过 ${r.accepted} 个`, 'ok');
    }
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

  // 主网面板没有「切换交易所」按钮：状态行已足够，切换靠解锁时选择。
  if (scope === 'live') return;
  const btn = $('tnSwitchBtn');
  if (!btn) return;
  const ex = currentExchange(scope);
  const ready = hasKeys(ex, mode);
  if (isConnectedExchange(ex, scope)) {
    btn.textContent = `已连${exCn(ex)}`; btn.disabled = true; btn.classList.add('is-connected');
  } else if (ready) {
    btn.innerHTML = '<svg class="btn-switch-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/></svg>连接';
    btn.disabled = false; btn.classList.remove('is-connected');
  } else {
    btn.textContent = `先填${exCn(ex)}的 Key`; btn.disabled = true; btn.classList.remove('is-connected');
  }
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
      const res = await armAccount({ mode: 'testnet', exchange: ex });
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
  btn.disabled = true; btn.textContent = '连接中…'; btn.classList.add('is-loading');
  try {
    const res = await api('/api/trading/arm', 'POST', { mode: 'testnet', exchange: ex });
    toast(`已切到${exCn(ex)}模拟盘（Demo），余额 ${fmt(res.balance)} USDT（假钱）`, 'ok');
    btn.classList.remove('is-loading');
    closeModePanels();
    poll();
  } catch (e) {
    toast(`切换失败：${e.message}`, 'err');
    btn.classList.remove('is-loading'); renderExchangePanel('testnet');
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
      toast('已断开交易所并停止自动交易，回到本地模拟', 'ok');
      poll();
    } catch (e) { toast(e.message, 'err'); }
  });
});

// 状态条上的「切换交易所」：弹出已保存凭据的账户列表，直接点选切换
(function () {
  const menu = document.createElement('div');
  menu.id = 'acctMenu'; menu.className = 'acct-menu'; menu.hidden = true;
  document.body.appendChild(menu);
})();

const ACCT_EX_CN = { binance: '币安', okx: 'OKX' };
const ACCT_ENV_CN = { demo: '模拟盘', live: '主网实盘' };

function acctCurrentScope() {
  if (lastStatus && lastStatus.mode === 'live' && lastStatus.armed)
    return `${lastStatus.exchange}:${lastStatus.testnet ? 'demo' : 'live'}`;
  return 'local';
}
function acctItemHtml(type, scope, name, tag, isCur, ex, env) {
  const right = isCur
    ? '<span class="acct-check">✓ 当前</span>'
    : `<span class="acct-tag">${tag}</span>`;
  const cls = `acct-item${isCur ? ' is-current' : ''}${type === 'live' ? ' acct-live' : ''}`;
  let inner = `<button class="acct-head" data-type="${type}" data-scope="${scope}"${ex ? ` data-ex="${ex}"` : ''}${env ? ` data-env="${env}"` : ''}><span class="acct-dot"></span><span class="acct-name">${name}</span>${right}</button>`;
  if (type === 'live') {
    inner += `<div class="acct-confirm"><input class="acct-confirm-input" type="text" placeholder="输入：我确认使用真实资金" autocomplete="off" onpaste="return false" /><button class="acct-confirm-btn" data-ex="${ex}" data-env="${env}">确认切换</button></div>`;
  }
  return `<div class="${cls}">${inner}</div>`;
}
function acctBuildMenu() {
  const menu = $('acctMenu');
  const cur = acctCurrentScope();
  const keys = (lastStatus && lastStatus.keys) || {};
  let html = `<div class="acct-menu-head"><span>切换交易账户</span><button class="acct-menu-close" id="acctMenuClose" title="关闭">×</button></div>`;
  html += acctItemHtml('local', 'local', '本地模拟', '本地撮合 · 不联网', cur === 'local');
  const saved = [];
  for (const ex of ['binance', 'okx']) for (const env of ['demo', 'live']) {
    if (keys[ex] && keys[ex][env]) saved.push({ ex, env });
  }
  if (saved.length) {
    html += `<div class="acct-sec-label">已保存凭据</div>`;
    for (const { ex, env } of saved) {
      const scope = `${ex}:${env}`;
      html += acctItemHtml(env === 'live' ? 'live' : 'demo', scope,
        `${ACCT_EX_CN[ex]} · ${ACCT_ENV_CN[env]}`, env === 'live' ? '真钱 · 需确认' : '假钱',
        cur === scope, ex, env);
    }
  } else {
    html += `<div class="acct-sec-label">还没有已保存的凭据</div>`;
  }
  html += `<div class="acct-menu-foot"><button class="acct-head acct-manage" data-action="manage"><span class="acct-name">＋ 管理 / 添加凭据</span></button></div>`;
  menu.innerHTML = html;
  menu.querySelectorAll('.acct-head[data-scope]').forEach((b) => {
    b.addEventListener('click', () => acctOnPick(b.dataset.type, b.dataset.scope, b));
  });
  menu.querySelectorAll('.acct-confirm-btn').forEach((b) => {
    b.addEventListener('click', (ev) => { ev.stopPropagation(); acctLiveConfirm(b); });
  });
  const mg = menu.querySelector('.acct-manage');
  if (mg) mg.addEventListener('click', () => { acctCloseMenu(); acctOpenPanel('testnet'); });
  const close = $('acctMenuClose'); if (close) close.addEventListener('click', acctCloseMenu);
}
function acctOpenMenu(anchor) {
  acctBuildMenu();
  const menu = $('acctMenu');
  menu.hidden = false; menu._anchor = anchor;
  const r = anchor.getBoundingClientRect();
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  let top = r.bottom + 8;
  if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 8);
  let left = r.left;
  if (left + mw > window.innerWidth - 8) left = window.innerWidth - mw - 8;
  menu.style.top = top + 'px'; menu.style.left = left + 'px';
}
function acctCloseMenu() { const m = $('acctMenu'); if (m) { m.hidden = true; m._anchor = null; } }
function acctOpenPanel(scope) {
  const boxId = scope === 'live' ? 'liveBox' : 'testnetBox';
  showTab('settings');
  openPanel(boxId, scope === 'live' ? 'liveBtn' : 'testnetBtn');
  selectExchange(scope, (lastStatus && lastStatus.exchange) || 'binance');
  renderExchangePanel(scope);
  $(boxId).scrollIntoView({ behavior: 'smooth', block: 'center' });
}
async function acctOnPick(type, scope, headBtn) {
  if (type === 'local') {
    acctCloseMenu();
    if (blockIfAutoRunning('断开连接')) return;
    try { await api('/api/trading/disarm', 'POST', {}); toast('已回到本地模拟', 'ok'); poll(); }
    catch (e) { toast(e.message, 'err'); }
    return;
  }
  if (type === 'demo') {
    const ex = scope.split(':')[0];
    acctCloseMenu();
    if (blockIfAutoRunning('切换交易所')) return;
    const prev = headBtn.textContent;
    headBtn.disabled = true; headBtn.textContent = '连接中…';
    try {
      const res = await armAccount({ mode: 'testnet', exchange: ex });
      toast(`已切到${exCn(ex)}模拟盘（Demo），余额 ${fmt(res.balance)} USDT（假钱）`, 'ok');
      closeModePanels(); poll();
    } catch (e) { toast(`切换失败：${e.message}`, 'err'); headBtn.disabled = false; headBtn.textContent = prev; }
    return;
  }
  if (type === 'live') {
    const item = headBtn.closest('.acct-item');
    $('acctMenu').querySelectorAll('.acct-live.open').forEach((o) => { if (o !== item) o.classList.remove('open'); });
    item.classList.toggle('open');
    const inp = item.querySelector('.acct-confirm-input');
    if (inp) inp.focus();
  }
}
async function acctLiveConfirm(btn) {
  if (blockIfAutoRunning('切换交易所')) return;
  const ex = btn.dataset.ex, env = btn.dataset.env;
  const input = btn.previousElementSibling;
  const phrase = input.value.trim();
  btn.disabled = true; btn.textContent = '切换中…';
  try {
    const res = await armAccount({ mode: 'live', exchange: ex, testnet: false, confirm: phrase });
    toast(`${exCn(ex)}主网实盘已解锁`, 'ok');
    acctCloseMenu(); poll();
  } catch (e) {
    toast(`解锁失败：${e.message}`, 'err');
    btn.disabled = false; btn.textContent = '确认切换';
  }
}
function acctSwitchClick(anchor) {
  const m = $('acctMenu');
  if (!m.hidden && m._anchor === anchor) { acctCloseMenu(); return; }
  acctOpenMenu(anchor);
}
const _acctCs = $('connSwitchBtn');
if (_acctCs) _acctCs.addEventListener('click', (e) => { e.stopPropagation(); acctSwitchClick(_acctCs); });
const _acctSs = $('setConnSwitch');
if (_acctSs) _acctSs.addEventListener('click', (e) => { e.stopPropagation(); acctSwitchClick(_acctSs); });
document.addEventListener('click', (e) => {
  const m = $('acctMenu');
  if (!m || m.hidden) return;
  if (m.contains(e.target)) return;
  if (e.target.closest && e.target.closest('#connSwitchBtn, #setConnSwitch')) return;
  acctCloseMenu();
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') acctCloseMenu(); });

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
    toast('已断开交易所并停止自动交易，回到本地模拟', 'ok');
    poll();
  } catch (e) { toast(e.message, 'err'); }
});

// 设置页连接状态卡：切换交易所 = 弹出账户列表（见上方 acctSwitchClick）

// 设置页连接状态卡：断开
const setConnDisarm = $('setConnDisarm');
if (setConnDisarm) {
  setConnDisarm.addEventListener('click', async () => {
    if (blockIfAutoRunning('断开连接')) return;
    try {
      await api('/api/trading/disarm', 'POST', {});
      closeModePanels();
      toast('已断开交易所并停止自动交易，回到本地模拟', 'ok');
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
      const res = await armAccount({ mode: 'testnet', exchange: ex });
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

// 主网确认文本必须逐字手动输入，禁止复制粘贴（含拖拽），防止「无脑粘贴」解锁真钱。
['paste', 'drop'].forEach((ev) => $('confirmText').addEventListener(ev, (e) => e.preventDefault()));

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
    await armAccount({
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

$('resetRecordsBtn').addEventListener('click', async () => {
  // scope 为空时由后端按当前账户处理；label 只用于提示文案
  const scope = (lastStatus?.pnl || {}).scope || '';
  const label = scope || '当前账户';
  const trades = Number(lastStatus?.pnl?.trades || 0);
  const fills = (lastStatus?.fills || []).length;
  // 两次确认：第一次说明范围，第二次确认不可撤销，避免误点
  if (!confirm(
    `将清空【${label}】的权益盈亏记录、交易记录与持仓历史。\n` +
    `当前约有 ${trades} 笔平仓记录、${fills} 条成交流水。\n\n` +
    '只清这三类数据：风控 / 仓位 / 模型等设置不变，真实持仓也不会被平掉，其它账户不受影响。\n\n继续？'
  )) return;
  if (!confirm(`再次确认：清空后无法恢复。\n\n确定重置【${label}】的记录吗？`)) return;
  try {
    const d = await api('/api/accounts/reset', 'POST', scope ? { scope } : {});
    if ($('resetRecordsState')) {
      $('resetRecordsState').textContent =
        `已重置 ${d.scope || label}：清空 ${d.fills} 条流水 / ${d.position_history} 笔平仓 / ` +
        `${d.equity_history} 个权益点，基准权益 ${fmt(d.equity, 2)} U`;
    }
    toast('已重置当前账户记录', 'ok');
    poll();
  } catch (e) { toast(`重置失败：${e.message}`, 'err'); }
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

// 必须包一层：直接传函数当 click 处理器时，事件对象会成为第一个实参（quiet），
// 把「保存成功」提示静默吞掉。这里显式无参调用，让 save* 走默认 quiet=false。
$('llmSaveBtn').addEventListener('click', () => saveLlmSettings());
$('llmTestBtn').addEventListener('click', testLlmSettings);
$('sentimentSaveBtn').addEventListener('click', () => saveSentimentSettings());
$('notifySaveBtn').addEventListener('click', () => saveNotifySettings());
$('posLlmSaveBtn').addEventListener('click', () => savePositionLlmSettings());
$('posReplayBtn')?.addEventListener('click', async () => {
  try {
    await api('/api/settings/position/replay', 'POST', {});
    toast('L2 验收已在后台运行，完成后状态会更新', 'ok');
    setTimeout(loadPositionLlmSettings, 6000);
  } catch (e) {
    toast('L2 验收启动失败：' + e.message, 'err');
  }
});

$('profileSelect').addEventListener('change', renderProfileDiff);
$('profileApplyBtn').addEventListener('click', () => applyProfile($('profileSelect').value));
$('profileResetBtn').addEventListener('click', () => applyProfile((profilesCache && profilesCache.default) || 'balanced'));
$('profileRollbackBtn').addEventListener('click', rollbackSettings);

$('evolveDryBtn').addEventListener('click', () => runEvolve(false));
$('evolveApplyBtn').addEventListener('click', () => runEvolve(true));

async function saveEvolveAuto() {
  try {
    await api('/api/settings/evolve/auto', 'POST', {
      enabled: $('evolveAuto').checked,
      interval_hours: Number($('evolveInterval').value) || 24,
    });
    loadEvolveState();
    toast('自动调参设置已保存', 'ok');
  } catch (e) {
    toast('保存失败：' + e.message, 'err');
  }
}
$('evolveAuto').addEventListener('change', saveEvolveAuto);
$('evolveInterval').addEventListener('change', saveEvolveAuto);
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
      listEl.innerHTML = '<div class="muted center">暂无扫描结果，点「重扫」查看机会信号</div>';
      // 首次进页面自动触发一次后台扫描，避免排行榜一直空着
      if (!window.__oppAsked) {
        window.__oppAsked = true;
        api('/api/opportunity', 'POST', {}).catch(() => {});
      }
    }
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

  if ($('rotateToggle') && document.activeElement !== $('rotateToggle')) {
    $('rotateToggle').checked = !!lastStatus?.auto_rotate;
  }
  if ($('rotateMinScore') && document.activeElement !== $('rotateMinScore')) {
    $('rotateMinScore').value = lastStatus?.rotate_min_score ?? 0.2;
  }
  if ($('rotateEvery') && document.activeElement !== $('rotateEvery')) {
    $('rotateEvery').value = lastStatus?.rotate_every_cycles ?? 1;
  }
  scheduleSideFit();   // 榜单行数变了，三卡的高度分配跟着重算
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

window.addEventListener('resize', () => { renderPnl(); scheduleSideFit(); });
$('calPrev').addEventListener('click', () => shiftMonth(-1));
$('calNext').addEventListener('click', () => shiftMonth(1));

/* ---------------- 主题切换 ---------------- */
const THEME_KEY = 'qeelin-theme';
function applyTheme(mode) {
  const light = mode === 'light';
  document.documentElement.dataset.theme = light ? 'light' : 'dark';
  const btn = $('themeBtn');
  if (btn) {
    // 日 / 月 SVG 由 CSS 按 data-theme 切换，这里只更新文字与可访问名称，
    // 不要用 textContent 覆盖 .theme-ico（会把两个 SVG 抹掉）。
    const txt = btn.querySelector('.theme-text');
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
  if (['overview', 'decision', 'research', 'evolution', 'trading', 'settings'].includes(hash)) showTab(hash, false);
  await poll();
  // 字体加载完字宽会变（盈亏卡可能换行），右栏高度要再校一次
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => scheduleSideFit()).catch(() => {});
  }
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
  // 2 秒一轮：最新价来自交易所行情流（WebSocket）实时推送，后端缓存始终 <1s；
  // /api/status 不现场打交易所——其余全是内存数据，所以这个频率不会把接口刷爆。
  setInterval(poll, 2000);
  // 只在循环尚未启动时启动：poll() 里的 _anchorUptime 通常已经起了一个，
  // 这里再无条件赋值会造成两个 rAF 循环并行（且旧句柄丢失、无法停止）。
  if (!_uptimeRAF) _uptimeRAF = requestAnimationFrame(_renderUptimeFrame);
})();
