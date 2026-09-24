const demoGrids = [
  {
    id: 'delta-01', name: 'Delta 01', symbol: 'RELIANCE', description: 'RELIANCE · NSE · Cash delivery', status: 'RUNNING', source: 'PAPER SNAPSHOT',
    range: '₹2,740 — ₹3,020', spacing: '₹20.00', realizedPnl: 8460, size: 320, side: 'LONG', averageEntry: 2864.50, markPrice: 2918.25, unrealizedPnl: 17200, liquidationPrice: 2486.00, utilization: 64, health: 82, nextTrigger: 2940,
    orders: [
      { side: 'BUY', quantity: 80, actualPrice: 2842.10, gridLevel: 2840, status: 'OPEN' },
      { side: 'SELL', quantity: 80, actualPrice: 2949.80, gridLevel: 2950, status: 'OPEN' },
      { side: 'BUY', quantity: 80, actualPrice: 2801.25, gridLevel: 2800, status: 'OPEN' }
    ]
  },
  {
    id: 'delta-02', name: 'Delta 02', symbol: 'TCS', description: 'TCS · NSE · Cash delivery', status: 'RUNNING', source: 'PAPER SNAPSHOT',
    range: '₹3,220 — ₹3,540', spacing: '₹25.00', realizedPnl: 12980, size: 180, side: 'LONG', averageEntry: 3378.20, markPrice: 3412.60, unrealizedPnl: 6192, liquidationPrice: 2994.00, utilization: 48, health: 91, nextTrigger: 3435,
    orders: [
      { side: 'BUY', quantity: 45, actualPrice: 3349.50, gridLevel: 3350, status: 'OPEN' },
      { side: 'SELL', quantity: 45, actualPrice: 3441.20, gridLevel: 3450, status: 'OPEN' }
    ]
  },
  {
    id: 'delta-03', name: 'Delta 03', symbol: 'INFY', description: 'INFY · NSE · Cash delivery', status: 'PAUSED', source: 'PAPER SNAPSHOT',
    range: '₹1,420 — ₹1,620', spacing: '₹15.00', realizedPnl: 3840, size: 0, side: 'FLAT', averageEntry: 0, markPrice: 1538.40, unrealizedPnl: 0, liquidationPrice: null, utilization: 0, health: 76, nextTrigger: 1550,
    orders: [{ side: 'BUY', quantity: 60, actualPrice: 1521.75, gridLevel: 1520, status: 'OPEN' }]
  }
];

const CHART_INDICES = [
  { id: 'nifty-50', name: 'NIFTY 50' },
  { id: 'bank-nifty', name: 'BANK NIFTY' },
  { id: 'sensex', name: 'SENSEX' }
];
const CHART_TIMEFRAMES = [
  { id: '1m', label: '1m' },
  { id: '5m', label: '5m' },
  { id: '15m', label: '15m' },
  { id: '1h', label: '1H' },
  { id: '1d', label: '1D' }
];
const MARKET_SYNC_INTERVAL_MS = 20000;

let grids = demoGrids;
let activeGridId = grids[0].id;
let isRefreshing = false;
let marketRefreshing = false;
let autoSyncTimer;
let brokerSyncTimer;
let marketSyncTimer;
let chart;
let candleSeries;
let activeIndexId = CHART_INDICES[0].id;
let activeTimeframe = '5m';

const money = (value) => value == null ? '—' : `₹${Math.abs(value).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const signedMoney = (value) => value == null ? '—' : value >= 0 ? `+${money(value)}` : `-${money(value)}`;
const signedPercent = (value) => `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
const number = (value) => value.toLocaleString('en-IN');

// Decouples the 1s UI heartbeat (just re-renders "Xs ago" from a timestamp)
// from actually hitting Groww: broker-data fetches (positions/orders/grids)
// run on their own slower brokerSyncTimer (see bottom of file) and only
// update lastBrokerSyncAt/brokerLive here - the label never implies Groww
// is being polled every second, because it isn't.
let brokerLive = false;
let lastBrokerSyncAt = null;

function markBrokerSynced(success) {
  brokerLive = success;
  if (success) lastBrokerSyncAt = Date.now();
  renderSyncHeartbeat();
}

function renderSyncHeartbeat() {
  const pill = document.querySelector('#connectionPill');
  if (!pill) return;
  if (!brokerLive) {
    pill.innerHTML = '<i></i>Demo feed';
    return;
  }
  if (lastBrokerSyncAt == null) {
    pill.innerHTML = '<i></i>Live · syncing&hellip;';
    return;
  }
  const seconds = Math.max(0, Math.round((Date.now() - lastBrokerSyncAt) / 1000));
  pill.innerHTML = `<i></i>Live · ${seconds === 0 ? 'just now' : `${seconds}s ago`}`;
}

function getActiveGrid() { return grids.find((grid) => grid.id === activeGridId) || grids[0]; }

function escapeHtml(value) {
  return String(value ?? '—').replace(/[&<>'"]/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[character]);
}

function formatDataValue(value) {
  if (Array.isArray(value)) return value.join(', ') || '—';
  if (value && typeof value === 'object') return JSON.stringify(value);
  return value == null ? '—' : value;
}

function renderKeyValues(target, value) {
  const entries = Object.entries(value || {});
  target.innerHTML = entries.length ? `<div class="api-data"><dl class="kv-grid">${entries.map(([key, item]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(formatDataValue(item))}</dd></div>`).join('')}</dl></div>` : '<div class="api-data"><p class="data-empty">No data returned.</p></div>';
}

function renderDataTable(target, rows) {
  if (!rows || rows.length === 0) {
    target.innerHTML = '<div class="api-data"><p class="data-empty">No data returned.</p></div>';
    return;
  }
  const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))];
  target.innerHTML = `<div class="api-data"><table class="data-table"><thead><tr>${columns.map((column) => `<th>${escapeHtml(column)}</th>`).join('')}</tr></thead><tbody>${rows.map((row) => `<tr>${columns.map((column) => `<td title="${escapeHtml(formatDataValue(row[column]))}">${escapeHtml(formatDataValue(row[column]))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}

// Cached so the compact broker card on the Positions page can reuse data
// already fetched by loadAccount()/loadBrokerStatus() - no extra API calls.
let lastAccountPayload = null;
let lastBrokerStatusPayload = null;
let positionsBrokerCardExpanded = false;

function renderPositionsBrokerCard() {
  const toggle = document.querySelector('#positionsBrokerToggle');
  if (!toggle) return;
  const status = lastBrokerStatusPayload || {};
  const account = lastAccountPayload || {};
  const profile = account.profile || {};
  const margin = account.margin || {};

  const statusEl = document.querySelector('#posBrokerConnLabel').parentElement;
  const connected = status.connectionStatus === 'CONNECTED';
  statusEl.className = `broker-mini-status ${connected ? 'ok' : 'danger'}`;
  document.querySelector('#posBrokerConnLabel').textContent =
    status.connectionStatus ? status.connectionStatus.replace('_', ' ') : 'Checking…';
  document.querySelector('#posBrokerSourceTag').textContent = 'LIVE BROKER DATA';
  document.querySelector('#posBrokerLastSync').textContent =
    `Last sync ${status.lastSuccessfulRequestAt ? formatTimestamp(status.lastSuccessfulRequestAt) : '—'}`;

  document.querySelector('#posBrokerDetailsGrid').innerHTML = [
    ['Connection', status.connectionStatus || '—'],
    ['Token status', status.tokenStatus || '—'],
    ['Active segments', (profile.activeSegments || []).join(', ') || '—'],
    ['Holdings', (account.holdings || []).length],
    ['Available margin (equity)', margin.equity_margin_details?.clear_cash != null ? money(margin.equity_margin_details.clear_cash) : '—'],
    ['Available margin (F&O)', margin.fno_margin_details?.clear_cash != null ? money(margin.fno_margin_details.clear_cash) : '—'],
    ['API key', status.apiKeyMasked || 'Not configured'],
    ['Last error', status.lastError || 'None'],
  ].map(([label, value]) => `<div class="account-stat"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join('');
}

document.querySelector('#positionsBrokerToggle')?.addEventListener('click', () => {
  positionsBrokerCardExpanded = !positionsBrokerCardExpanded;
  document.querySelector('#positionsBrokerToggle').setAttribute('aria-expanded', String(positionsBrokerCardExpanded));
  document.querySelector('#positionsBrokerDetails').hidden = !positionsBrokerCardExpanded;
});

function renderAccount(account) {
  lastAccountPayload = account;
  renderPositionsBrokerCard();
  document.querySelector('#accountSource').textContent = account.source || 'Account data unavailable';
  const profile = account.profile || {};
  const margin = account.margin || {};
  const fnoMargin = margin.fno_margin_details || {};
  const equityMargin = margin.equity_margin_details || {};
  document.querySelector('#accountSummary').innerHTML = [
    ['Connection', profile.connected ? 'Authenticated' : 'Unavailable'],
    ['Active segments', (profile.activeSegments || []).join(', ') || '—'],
    ['Holdings', (account.holdings || []).length],
    ['Instrument master', account.instrumentMaster?.available ? 'Available' : 'Unavailable']
  ].map(([label, value]) => `<div class="account-stat"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join('');
  renderKeyValues(document.querySelector('#profileData'), profile);
  renderKeyValues(document.querySelector('#marginData'), { ...margin, ...fnoMargin, ...equityMargin });
  document.querySelector('#holdingsCount').textContent = (account.holdings || []).length;
  document.querySelector('#positionsCount').textContent = (account.positions || []).length;
  document.querySelector('#ordersDataCount').textContent = (account.orders || []).length;
  renderDataTable(document.querySelector('#holdingsData'), account.holdings);
  renderDataTable(document.querySelector('#positionsData'), account.positions);
  renderDataTable(document.querySelector('#ordersData'), account.orders);
  renderKeyValues(document.querySelector('#instrumentData'), {
    ...account.instrumentMaster,
    marketDataStatus: account.marketData?.status,
    marketDataMethods: account.marketData?.availableMethods
  });
}

function renderMarket(indices, source) {
  document.querySelector('#marketSource').textContent = source === 'YAHOO FINANCE (FREE FEED)' ? 'Yahoo Finance feed (independent of broker connection)' : 'Market data feed unavailable';
  systemStatusState.market.live = indices.some((index) => index.status === 'LIVE');
  updatePulseStatus();
  updateAutoGates();
  updateCriticalBanner();
  document.querySelector('#marketCards').innerHTML = indices.map((index) => `
    <article class="market-card ${index.status === 'LIVE' ? 'live' : ''}">
      <div class="market-card-top">
        <div><strong>${index.name}</strong><small>${index.status === 'LIVE' ? 'Live · Yahoo Finance' : 'Market data unavailable'}</small></div>
        <div class="market-change ${index.change >= 0 ? 'up' : 'down'}"><strong>${index.change == null ? '—' : `${index.change >= 0 ? '↗' : '↘'} ${signedPercent(index.changePercent || 0)}`}</strong><span>${index.change == null ? '—' : signedMoney(index.change)}</span></div>
      </div>
      <strong class="market-price">${money(index.price)}</strong>
      <div class="market-chart">${sparkline(index.sparkline, index.change >= 0)}</div>
    </article>
  `).join('');
}

function sparkline(points, positive) {
  if (!points || points.length < 2) return '<span class="market-status">UNAVAILABLE</span>';
  const minimum = Math.min(...points);
  const maximum = Math.max(...points);
  const width = 250;
  const height = 48;
  const spread = maximum - minimum || 1;
  const polyline = points.map((point, index) => `${(index / (points.length - 1)) * width},${height - ((point - minimum) / spread) * height}`).join(' ');
  return `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true"><polyline class="${positive ? 'line-up' : 'line-down'}" points="${polyline}" /></svg>`;
}

function renderTabs() {
  document.querySelector('#gridTabs').innerHTML = grids.map((grid) => `
    <button class="grid-tab ${grid.id === activeGridId ? 'active' : ''}" type="button" aria-selected="${grid.id === activeGridId}" data-grid-id="${grid.id}">${grid.name}</button>
  `).join('');
  document.querySelectorAll('.grid-tab').forEach((button) => button.addEventListener('click', () => {
    activeGridId = button.dataset.gridId;
    render();
  }));
}

function renderPositionsSummaryStrip(positions) {
  const exposure = positions.reduce((sum, p) => sum + Math.abs(p.quantity || 0) * (p.averagePrice || 0), 0);
  const unrealizedTotal = positions.reduce((sum, p) => sum + (p.unrealizedPnl || 0), 0);
  document.querySelector('#summaryOpenPositions').textContent = number(positions.length);
  document.querySelector('#summaryExposure').textContent = money(exposure);
  const unrealizedEl = document.querySelector('#summaryUnrealizedPnl');
  unrealizedEl.textContent = positions.length === 0 ? '—' : signedMoney(unrealizedTotal);
  unrealizedEl.className = positions.length === 0 ? '' : unrealizedTotal >= 0 ? 'positive' : 'warning';
}

function renderPositionsTable(positions) {
  document.querySelector('#openPositionsCount').textContent = positions.length;
  renderPositionsSummaryStrip(positions);
  const tableWrap = document.querySelector('#positionsTableWrap');
  const emptyState = document.querySelector('#positionsEmptyState');
  if (positions.length === 0) {
    tableWrap.hidden = true;
    emptyState.hidden = false;
    return;
  }
  tableWrap.hidden = false;
  emptyState.hidden = true;
  document.querySelector('#positionsBody').innerHTML = positions.map((position) => `
    <tr>
      <td class="mono">${escapeHtml(position.symbol)}</td>
      <td><span class="side ${position.side === 'LONG' ? 'buy' : position.side === 'SHORT' ? 'sell' : ''}">${position.side}</span></td>
      <td class="mono">${number(position.quantity)}</td>
      <td class="mono">${money(position.averagePrice)}</td>
      <td class="mono">${money(position.ltp)}</td>
      <td class="mono ${position.unrealizedPnl >= 0 ? 'positive' : ''}">${signedMoney(position.unrealizedPnl)}</td>
      <td class="mono ${position.realizedPnl >= 0 ? 'positive' : ''}">${signedMoney(position.realizedPnl)}</td>
    </tr>
  `).join('');
}

async function loadPositions() {
  try {
    const response = await fetch('/api/positions', { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Positions API unavailable');
    const payload = await response.json();
    document.querySelector('#positionsSource').textContent = payload.source || 'LIVE BROKER DATA';
    renderPositionsTable(payload.positions || []);
    markBrokerSynced(true);
  } catch {
    document.querySelector('#positionsSource').textContent = 'POSITIONS DATA UNAVAILABLE';
    renderPositionsTable([]);
    markBrokerSynced(false);
  }
}

function renderPnl(payload) {
  const realized = payload.realized || {};
  const unrealized = payload.unrealized || {};

  const totalEl = document.querySelector('#pnlTotalRealized');
  totalEl.textContent = signedMoney(realized.total);
  totalEl.className = realized.total >= 0 ? 'positive' : 'warning';

  const summaryRealizedEl = document.querySelector('#summaryRealizedPnl');
  summaryRealizedEl.textContent = signedMoney(realized.total);
  summaryRealizedEl.className = realized.total >= 0 ? 'positive' : 'warning';

  document.querySelector('#pnlLiveRealized').textContent = signedMoney(realized.live);
  document.querySelector('#pnlPaperRealized').textContent = signedMoney(realized.paper);

  const paperUnrealizedEl = document.querySelector('#pnlPaperUnrealized');
  if (unrealized.paper == null) {
    paperUnrealizedEl.textContent = '—';
    paperUnrealizedEl.className = '';
  } else {
    paperUnrealizedEl.textContent = signedMoney(unrealized.paper);
    paperUnrealizedEl.className = unrealized.paper >= 0 ? 'positive' : 'warning';
  }

  document.querySelector('#pnlLiveUnrealizedNote').textContent =
    `Live unrealized P&L: ${unrealized.liveUnavailableReason || 'unavailable'}`;

  const netEl = document.querySelector('#pnlNetTotal');
  netEl.textContent = signedMoney(realized.netTotal);
  netEl.className = realized.netTotal >= 0 ? 'positive' : 'warning';
  document.querySelector('#pnlLiveCosts').textContent = money(realized.liveCosts);

  document.querySelector('#pnlCostModelNote').textContent = realized.costModelConfigured
    ? 'Net P&L includes configured brokerage/STT/exchange/GST/stamp-duty costs for live trades.'
    : 'Cost model not configured (all rates default to 0) - net total currently equals gross. Set ALGOEDGE_COST_* in .env once you have your account\'s real published rates.';
}

async function loadPnl() {
  try {
    const response = await fetch('/api/pnl', { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('P&L API unavailable');
    renderPnl(await response.json());
  } catch {
    document.querySelector('#pnlTotalRealized').textContent = 'UNAVAILABLE';
    document.querySelector('#summaryRealizedPnl').textContent = '—';
    document.querySelector('#pnlLiveRealized').textContent = '—';
    document.querySelector('#pnlPaperRealized').textContent = '—';
    document.querySelector('#pnlPaperUnrealized').textContent = '—';
    document.querySelector('#pnlNetTotal').textContent = '—';
    document.querySelector('#pnlLiveCosts').textContent = '—';
  }
}

function renderTagBreakdown(counts) {
  const entries = Object.entries(counts || {});
  if (entries.length === 0) return '<span class="summary-tag">none</span>';
  return `<div class="summary-tags">${entries.map(([key, count]) => `<span class="summary-tag">${escapeHtml(key)} ${count}</span>`).join('')}</div>`;
}

function renderDailySummaryTable(days) {
  const body = document.querySelector('#dailySummaryBody');
  if (days.length === 0) {
    body.innerHTML = '<tr class="empty-row"><td colspan="5">No signals or orders recorded yet.</td></tr>';
    return;
  }
  body.innerHTML = days.map((day) => `
    <tr>
      <td class="mono">${escapeHtml(day.date)}</td>
      <td>${renderTagBreakdown(day.signalsByAction)}</td>
      <td class="mono">${number(day.ordersLive)} / ${number(day.ordersPaper)}</td>
      <td>${renderTagBreakdown(day.ordersByOutcome)}</td>
      <td class="mono ${day.realizedPnl >= 0 ? 'positive' : 'warning'}">${signedMoney(day.realizedPnl)}</td>
    </tr>
  `).join('');
}

function renderConversionSummary(days) {
  const totalSignals = days.reduce((sum, day) => sum + (day.signalsTotal || 0), 0);
  const totalOrders = days.reduce((sum, day) => sum + (day.ordersPlaced || 0), 0);
  const totalFilled = days.reduce((sum, day) => sum + ((day.ordersByOutcome || {}).SUCCESS || 0), 0);
  const signalToOrder = totalSignals === 0 ? null : (totalOrders / totalSignals) * 100;
  const orderToFill = totalOrders === 0 ? null : (totalFilled / totalOrders) * 100;
  document.querySelector('#convSignals').textContent = number(totalSignals);
  document.querySelector('#convOrders').textContent = number(totalOrders);
  document.querySelector('#convFilled').textContent = number(totalFilled);
  document.querySelector('#convSignalToOrder').textContent = signalToOrder == null ? '—' : `${signalToOrder.toFixed(1)}%`;
  document.querySelector('#convOrderToFill').textContent = orderToFill == null ? '—' : `${orderToFill.toFixed(1)}%`;
}

async function loadDailySummary() {
  try {
    const response = await fetch('/api/reports/daily-summary?days=30', { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Daily summary API unavailable');
    const payload = await response.json();
    const days = payload.days || [];
    renderDailySummaryTable(days);
    renderConversionSummary(days);
  } catch {
    document.querySelector('#dailySummaryBody').innerHTML = '<tr class="empty-row"><td colspan="5">Could not load daily summary.</td></tr>';
  }
}

let periodSummaryMode = 'weekly';

function renderPeriodSummaryTable(buckets) {
  const body = document.querySelector('#periodSummaryBody');
  if (buckets.length === 0) {
    body.innerHTML = '<tr class="empty-row"><td colspan="4">No activity recorded yet.</td></tr>';
    return;
  }
  body.innerHTML = buckets.map((bucket) => `
    <tr>
      <td class="mono">${escapeHtml(bucket.label)} <small class="muted">(${bucket.start} to ${bucket.end})</small></td>
      <td class="mono">${number(bucket.signalsTotal)}</td>
      <td class="mono">${number(bucket.ordersLive)} / ${number(bucket.ordersPaper)}</td>
      <td class="mono ${bucket.realizedPnl >= 0 ? 'positive' : 'warning'}">${signedMoney(bucket.realizedPnlLive)} / ${signedMoney(bucket.realizedPnlPaper)}</td>
    </tr>
  `).join('');
}

async function loadPeriodSummary() {
  try {
    const response = await fetch(`/api/reports/period-summary?period=${periodSummaryMode}&periods=12`, { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Period summary API unavailable');
    const payload = await response.json();
    renderPeriodSummaryTable(payload.buckets || []);
  } catch {
    document.querySelector('#periodSummaryBody').innerHTML = '<tr class="empty-row"><td colspan="4">Could not load period summary.</td></tr>';
  }
}

document.querySelectorAll('#periodSummaryToggle .segmented-option').forEach((button) => button.addEventListener('click', () => {
  periodSummaryMode = button.dataset.period;
  document.querySelectorAll('#periodSummaryToggle .segmented-option').forEach((option) => option.classList.toggle('active', option === button));
  loadPeriodSummary();
}));

function renderStrategyPerformanceTable(strategies) {
  const body = document.querySelector('#strategyPerformanceBody');
  if (strategies.length === 0) {
    body.innerHTML = '<tr class="empty-row"><td colspan="7">No closed trades recorded yet.</td></tr>';
    return;
  }
  body.innerHTML = strategies.map((strategy) => `
    <tr>
      <td class="mono">${escapeHtml(strategy.source)}</td>
      <td class="mono">${number(strategy.tradesClosed)} <small class="muted">(${strategy.wins}W / ${strategy.losses}L)</small></td>
      <td class="mono">${strategy.winRate == null ? '—' : `${strategy.winRate.toFixed(1)}%`}</td>
      <td class="mono ${strategy.totalPnl >= 0 ? 'positive' : 'warning'}">${signedMoney(strategy.totalPnl)}</td>
      <td class="mono">${strategy.tradesClosed === 0 ? '—' : signedMoney(strategy.averagePnl)}</td>
      <td class="mono positive">${strategy.bestTrade == null ? '—' : signedMoney(strategy.bestTrade)}</td>
      <td class="mono warning">${strategy.worstTrade == null ? '—' : signedMoney(strategy.worstTrade)}</td>
    </tr>
  `).join('');
}

async function loadStrategyPerformance() {
  try {
    const response = await fetch('/api/reports/strategy-performance', { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Strategy performance API unavailable');
    const payload = await response.json();
    renderStrategyPerformanceTable(payload.strategies || []);
  } catch {
    document.querySelector('#strategyPerformanceBody').innerHTML = '<tr class="empty-row"><td colspan="7">Could not load strategy performance.</td></tr>';
  }
}

function renderAlertsTable(alertRows) {
  const body = document.querySelector('#alertsBody');
  if (alertRows.length === 0) {
    body.innerHTML = '<tr class="empty-row"><td colspan="6">No alerts recorded yet.</td></tr>';
    return;
  }
  body.innerHTML = alertRows.map((alert) => `
    <tr>
      <td class="mono">${formatTimestamp(alert.createdAt)}</td>
      <td class="mono ${alert.severity === 'CRITICAL' ? 'warning' : ''}">${escapeHtml(alert.severity)}</td>
      <td class="mono">${escapeHtml(alert.category)}</td>
      <td>${escapeHtml(alert.message)}</td>
      <td class="mono">${escapeHtml(alert.source)}</td>
      <td>${alert.acknowledged
        ? '<span class="side buy">ACK</span>'
        : `<button class="text-button" type="button" data-ack-id="${alert.id}">Acknowledge</button>`}</td>
    </tr>
  `).join('');
  document.querySelectorAll('#alertsBody button[data-ack-id]').forEach((button) => button.addEventListener('click', async () => {
    await fetch(`/api/alerts/${button.dataset.ackId}/acknowledge`, { method: 'POST' });
    await Promise.all([loadAlerts(), loadAlertsPill()]);
  }));
}

async function loadAlerts() {
  try {
    const response = await fetch('/api/alerts?limit=50', { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Alerts API unavailable');
    const payload = await response.json();
    renderAlertsTable(payload.alerts || []);
  } catch {
    document.querySelector('#alertsBody').innerHTML = '<tr class="empty-row"><td colspan="6">Could not load alerts.</td></tr>';
  }
}

async function loadAlertsPill() {
  try {
    const response = await fetch('/api/alerts?unacknowledged_only=true&limit=50', { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Alerts API unavailable');
    const payload = await response.json();
    const count = (payload.alerts || []).length;
    const pill = document.querySelector('#alertsPill');
    const hasCritical = (payload.alerts || []).some((alert) => alert.severity === 'CRITICAL');
    pill.hidden = count === 0;
    document.querySelector('#alertsPillCount').textContent = count;
    pill.classList.toggle('danger', hasCritical);
    pill.classList.toggle('warning', !hasCritical && count > 0);
  } catch {
    /* leave last known state on screen */
  }
}

async function acknowledgeAllAlerts() {
  await fetch('/api/alerts/acknowledge-all', { method: 'POST' });
  await Promise.all([loadAlerts(), loadAlertsPill()]);
}

document.querySelector('#acknowledgeAllAlertsButton').addEventListener('click', acknowledgeAllAlerts);
document.querySelector('#alertsPill').addEventListener('click', () => navigateTo('reports-panel'));

const RECONCILE_STATUS_LABELS = {
  OK: 'ALL MATCH',
  MISMATCH: 'POSITION MISMATCH',
  UNCONFIRMED: 'UNKNOWN',
  UNKNOWN: 'RECONCILIATION REQUIRED',
};

// Shared cross-page state so Pulse, Auto's gates checklist, and the global
// critical banner all agree on broker/market/reconciliation/auto status
// instead of each page re-deriving its own answer.
const systemStatusState = {
  broker: { connected: false },
  market: { live: false },
  reconciliation: { status: 'UNKNOWN', blocking: false },
  auto: { enabled: false, killSwitch: false, consecutiveLossHalt: false },
};

function updatePulseStatus() {
  const brokerEl = document.querySelector('#pulseBrokerStatus');
  const marketEl = document.querySelector('#pulseMarketStatus');
  if (!brokerEl || !marketEl) return;
  brokerEl.innerHTML = `<i class="status-dot"></i>${systemStatusState.broker.connected ? 'Connected' : 'Not connected'}`;
  brokerEl.closest('.system-status-item').className = `system-status-item ${systemStatusState.broker.connected ? 'ok' : 'danger'}`;
  marketEl.innerHTML = `<i class="status-dot"></i>${systemStatusState.market.live ? 'Live' : 'Unavailable'}`;
  marketEl.closest('.system-status-item').className = `system-status-item ${systemStatusState.market.live ? 'ok' : 'warning'}`;
}

function updateAutoGates() {
  const s = systemStatusState;
  const gates = {
    autoGateBroker: { ok: s.broker.connected, label: s.broker.connected ? 'PASS' : 'FAIL — not connected' },
    autoGateRisk: {
      ok: !s.auto.killSwitch && !s.auto.consecutiveLossHalt,
      label: s.auto.killSwitch ? 'FAIL — kill switch armed' : s.auto.consecutiveLossHalt ? 'FAIL — loss halt active' : 'PASS',
    },
    autoGateReconciliation: { ok: !s.reconciliation.blocking, label: s.reconciliation.blocking ? `FAIL — ${RECONCILE_STATUS_LABELS[s.reconciliation.status] || s.reconciliation.status}` : 'PASS' },
    autoGateMarketData: { ok: s.market.live, label: s.market.live ? 'PASS' : 'FAIL — unavailable' },
    autoGateSignalEngine: { ok: s.auto.enabled, label: s.auto.enabled ? 'PASS' : 'FAIL — scheduler disabled' },
  };
  Object.entries(gates).forEach(([id, gate]) => {
    const item = document.querySelector(`#${id}`);
    if (!item) return;
    item.querySelector('strong').innerHTML = `<i class="status-dot"></i>${gate.label}`;
    item.className = `system-status-item ${gate.ok ? 'ok' : 'danger'}`;
  });
}

function updateCriticalBanner() {
  const banner = document.querySelector('#criticalBanner');
  if (!banner) return;
  const s = systemStatusState;
  let level = null;
  let text = null;
  let target = null;
  if (s.auto.killSwitch) {
    level = 'critical'; target = 'auto-trading-panel';
    text = 'CRITICAL ERROR — Kill switch is armed. Auto Trading is halted.';
  } else if (s.reconciliation.blocking) {
    level = 'critical'; target = 'positions-panel';
    text = `TRADING BLOCKED — Position reconciliation: ${RECONCILE_STATUS_LABELS[s.reconciliation.status] || s.reconciliation.status}. New live orders are blocked.`;
  } else if (s.auto.consecutiveLossHalt) {
    level = 'warning'; target = 'auto-trading-panel';
    text = 'RISK WARNING — Consecutive-loss halt is active.';
  } else if (s.reconciliation.status && s.reconciliation.status !== 'OK') {
    level = 'warning'; target = 'positions-panel';
    text = `RECONCILIATION WARNING — ${RECONCILE_STATUS_LABELS[s.reconciliation.status] || s.reconciliation.status}.`;
  } else if (!s.broker.connected) {
    level = 'warning'; target = 'broker-panel';
    text = 'BROKER WARNING — Not connected to Groww. Live orders are unavailable.';
  }
  if (!level) {
    banner.hidden = true;
    updatePulseFitHeight();
    return;
  }
  banner.hidden = false;
  banner.className = `critical-banner ${level === 'warning' ? 'warning' : ''}`;
  banner.textContent = text;
  banner.dataset.target = target;
  updatePulseFitHeight();
}

const HEALTH_DOT_CLASS = { HEALTHY: 'ok', CONNECTED: 'ok', OK: 'ok', ACTIVE: 'ok' };

function renderSystemHealth(health) {
  const rows = [
    ['healthDatabase', health.database?.status],
    ['healthBroker', health.broker?.status],
    ['healthReconciliation', health.reconciliation?.status],
    ['healthRisk', health.riskEngine?.status],
  ];
  rows.forEach(([id, status]) => {
    const item = document.querySelector(`#${id}`);
    if (!item) return;
    const ok = HEALTH_DOT_CLASS[status] === 'ok';
    item.querySelector('strong').innerHTML = `<i class="status-dot"></i>${escapeHtml(status || 'UNKNOWN')}`;
    item.className = `system-status-item ${ok ? 'ok' : 'danger'}`;
  });
  document.querySelector('#healthLastSignal').textContent = health.lastSignal ? formatTimestamp(health.lastSignal.createdAt) : 'No signals recorded';
  document.querySelector('#healthLastBrokerSync').textContent = formatTimestamp(health.lastBrokerSync);
  document.querySelector('#healthLastDbWrite').textContent = formatTimestamp(health.lastDatabaseWrite);
  document.querySelector('#healthLastReconciliation').textContent = formatTimestamp(health.lastReconciliation);
  const errorEl = document.querySelector('#healthLastError');
  errorEl.hidden = !health.lastError;
  errorEl.textContent = health.lastError ? `Last error/warning: ${health.lastError}` : '';
}

async function loadSystemHealth() {
  try {
    const response = await fetch('/api/system/health', { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('System health API unavailable');
    renderSystemHealth(await response.json());
  } catch {
    /* leave last known state on screen */
  }
}

function renderReconciliation(payload) {
  const comparisons = payload.comparisons || [];
  const tag = document.querySelector('#reconcileStatusTag');
  const gate = payload.gate || {};
  const mismatches = comparisons.filter((row) => !row.matches).length;
  const gateStatus = gate.status || (mismatches === 0 ? 'OK' : 'MISMATCH');
  tag.textContent = RECONCILE_STATUS_LABELS[gateStatus] || gateStatus;
  tag.className = `status-tag ${gateStatus === 'OK' ? '' : 'danger'}`;

  systemStatusState.reconciliation = { status: gateStatus, blocking: !!gate.blocking };
  updateAutoGates();
  updateCriticalBanner();

  const banner = document.querySelector('#reconcileGateBanner');
  const overrideControls = document.querySelector('#reconcileOverrideControls');
  if (gate.blocking) {
    banner.hidden = false;
    banner.className = 'broker-result warning';
    const statusLabel = RECONCILE_STATUS_LABELS[gate.status] || gate.status;
    banner.textContent = gate.overrideReason
      ? `${statusLabel} — NEW ORDERS BLOCKED (${gate.reason || 'no reason recorded'}) — overridden until ${formatTimestamp(gate.overriddenUntil)}: ${gate.overrideReason}`
      : `${statusLabel} — NEW ORDERS BLOCKED. ${gate.reason || 'No reason recorded'}. Positions are never auto-corrected; resolve manually or override with a recorded reason.`;
    overrideControls.hidden = false;
  } else {
    banner.hidden = true;
    overrideControls.hidden = true;
  }

  const tableWrap = document.querySelector('#reconcileTableWrap');
  const allMatchEl = document.querySelector('#reconcileAllMatch');
  const showCompactAllMatch = comparisons.length > 0 && mismatches === 0 && !gate.blocking;
  tableWrap.hidden = showCompactAllMatch;
  allMatchEl.hidden = !showCompactAllMatch;
  if (showCompactAllMatch) {
    document.querySelector('#reconcileAllMatchNote').textContent =
      `${comparisons.length} symbol${comparisons.length === 1 ? '' : 's'} checked against live Groww data`;
  }

  const body = document.querySelector('#reconcileBody');
  body.innerHTML = comparisons.length === 0
    ? '<tr class="empty-row"><td colspan="4">No real orders on record yet - nothing to reconcile.</td></tr>'
    : comparisons.map((row) => `
      <tr class="${row.matches ? '' : 'reconcile-mismatch-row'}">
        <td class="mono">${escapeHtml(row.tradingSymbol)}</td>
        <td class="mono">${number(row.expectedQuantity)}</td>
        <td class="mono">${number(row.actualQuantity)}</td>
        <td><span class="side ${row.matches ? 'buy' : 'sell'}">${row.matches ? 'MATCH' : 'MISMATCH'}</span></td>
      </tr>
    `).join('');

  const unconfirmedEl = document.querySelector('#reconcileUnconfirmed');
  const unconfirmed = payload.unconfirmedOrders || [];
  if (unconfirmed.length === 0) {
    unconfirmedEl.hidden = true;
    return;
  }
  unconfirmedEl.hidden = false;
  unconfirmedEl.innerHTML = `<p class="data-empty data-empty-left">${unconfirmed.length} order(s) with unconfirmed fill status - not counted either way, check the Groww app manually:</p>
    <dl>${unconfirmed.map((order) => `
      <dt>${escapeHtml(order.tradingSymbol || '—')}</dt>
      <dd>${escapeHtml(order.side || '')} ${number(order.quantity || 0)} &middot; ${escapeHtml(order.outcome || '')} &middot; ${escapeHtml(order.growwOrderId || 'no order id')}</dd>
    `).join('')}</dl>`;
}

async function loadReconciliation() {
  try {
    const response = await fetch('/api/reconciliation', { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Reconciliation API unavailable');
    renderReconciliation(await response.json());
  } catch {
    document.querySelector('#reconcileStatusTag').textContent = 'RECONCILIATION REQUIRED';
    document.querySelector('#reconcileStatusTag').className = 'status-tag danger';
    document.querySelector('#reconcileAllMatch').hidden = true;
    document.querySelector('#reconcileTableWrap').hidden = false;
    document.querySelector('#reconcileBody').innerHTML = '<tr class="empty-row"><td colspan="4">Could not load reconciliation data — treat as unreconciled until this is resolved.</td></tr>';
  }
}

async function overrideReconciliationBlock() {
  const reason = window.prompt('Reason for overriding the reconciliation block (shown in the audit trail):', '');
  if (reason === null || !reason.trim()) return;
  const response = await fetch(`/api/reconciliation/override?reason=${encodeURIComponent(reason.trim())}&duration_minutes=60`, { method: 'POST' });
  if (response.ok) renderReconciliation(await response.json());
}

async function clearReconciliationOverride() {
  const response = await fetch('/api/reconciliation/override/clear', { method: 'POST' });
  if (response.ok) renderReconciliation(await response.json());
}

document.querySelector('#reconcileOverrideButton').addEventListener('click', overrideReconciliationBlock);
document.querySelector('#reconcileOverrideClearButton').addEventListener('click', clearReconciliationOverride);

function formatTimestamp(iso) {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
}

// For a pure calendar date (e.g. a contract expiry, "2026-09-29") with no
// time component - anchors to IST noon so no UTC/IST rollover shifts it to
// the wrong day, and never renders a bogus time-of-day.
function formatDateOnly(isoDate) {
  if (!isoDate) return '—';
  const date = new Date(`${isoDate}T12:00:00+05:30`);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' });
}

function renderLedgerTable(orders) {
  const body = document.querySelector('#ledgerBody');
  if (orders.length === 0) {
    body.innerHTML = '<tr class="empty-row"><td colspan="9">No trades recorded yet.<br><small class="muted">Orders placed via Manual trading, Auto trading, or the F&amp;O scanner will appear here.</small></td></tr>';
    return;
  }
  body.innerHTML = orders.map((order) => {
    const outcomeClass = order.outcome === 'SUCCESS' ? 'positive' : (order.outcome === 'FAILED' || order.outcome === 'TIMEOUT') ? 'warning' : '';
    const hasFillDetail = order.filledQuantity != null || order.remainingQuantity != null;
    const filledRemaining = hasFillDetail
      ? `${order.filledQuantity == null ? '—' : number(order.filledQuantity)} / ${order.remainingQuantity == null ? '—' : number(order.remainingQuantity)}`
      : '—';
    return `<tr>
      <td class="mono">${formatTimestamp(order.createdAt)}</td>
      <td class="mono">${escapeHtml(order.source || '—')}${order.live ? ' <span class="ledger-live-tag">LIVE</span>' : ''}</td>
      <td class="mono">${escapeHtml(order.tradingSymbol || order.indexId || '—')}</td>
      <td><span class="side ${(order.side || '').toLowerCase() === 'sell' ? 'sell' : 'buy'}">${escapeHtml(order.side || '—')}</span></td>
      <td class="mono">${order.quantity == null ? '—' : number(order.quantity)}</td>
      <td class="mono">${filledRemaining}</td>
      <td class="mono">${money(order.expectedPrice != null ? order.expectedPrice : order.price)}</td>
      <td class="mono">${money(order.price)}</td>
      <td class="mono ${outcomeClass}">${escapeHtml(order.outcome || order.orderStatus || '—')}</td>
    </tr>`;
  }).join('');
}

function updateExecutionHealth(orders) {
  const startOfToday = new Date();
  startOfToday.setHours(0, 0, 0, 0);
  const todays = orders.filter((order) => order.createdAt && new Date(order.createdAt) >= startOfToday);
  const filled = todays.filter((order) => order.outcome === 'SUCCESS').length;
  const failed = todays.filter((order) => ['FAILED', 'REJECTED', 'TIMEOUT'].includes(order.outcome)).length;
  const pending = todays.length - filled - failed;
  const successPct = todays.length === 0 ? null : Math.round((filled / todays.length) * 100);

  document.querySelector('#execOrdersToday').textContent = number(todays.length);
  document.querySelector('#execFilled').textContent = number(filled);
  document.querySelector('#execPending').textContent = number(pending);
  document.querySelector('#execFailed').textContent = number(failed);
  document.querySelector('#execHealthScore').textContent = successPct == null ? '—' : successPct;
  document.querySelector('#execHealthBar').style.width = successPct == null ? '0%' : `${successPct}%`;

  const note = document.querySelector('#execHealthNote');
  const message = document.querySelector('#execHealthMessage');
  if (todays.length === 0) {
    note.hidden = true;
  } else if (failed > 0) {
    note.hidden = false;
    message.textContent = `${failed} order${failed === 1 ? '' : 's'} rejected or failed today. Review the trade ledger before relying on Auto Trading.`;
  } else if (pending > 0) {
    note.hidden = false;
    message.textContent = `${pending} order${pending === 1 ? '' : 's'} still pending or unconfirmed.`;
  } else {
    note.hidden = true;
  }
}

async function loadLedger() {
  const source = document.querySelector('#ledgerSourceFilter').value;
  const live = document.querySelector('#ledgerLiveFilter').value;
  const params = new URLSearchParams();
  if (source) params.set('source', source);
  if (live) params.set('live', live);
  try {
    const response = await fetch(`/api/trade-ledger?${params}`, { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Trade ledger API unavailable');
    const payload = await response.json();
    const orders = payload.orders || [];
    renderLedgerTable(orders);
    updateExecutionHealth(orders);
  } catch {
    document.querySelector('#ledgerBody').innerHTML = '<tr class="empty-row"><td colspan="9">Could not load trade ledger.</td></tr>';
  }
}

function renderOrdersTable(orders) {
  document.querySelector('#orderCount').textContent = orders.length;
  if (orders.length === 0) {
    document.querySelector('#ordersBody').innerHTML =
      '<tr class="empty-row"><td colspan="5">No orders recorded.<br><small class="muted">Groww currently reports zero orders for this account.</small></td></tr>';
    return;
  }
  document.querySelector('#ordersBody').innerHTML = orders.map((order) => `<tr>
      <td class="mono">${escapeHtml(order.symbol)}</td>
      <td><span class="side ${order.side.toLowerCase()}">${order.side}</span></td>
      <td class="mono">${number(order.quantity)}</td>
      <td class="mono"><strong>${money(order.actualPrice)}</strong></td>
      <td><span class="order-status">● ${escapeHtml(order.status)}</span></td>
    </tr>`).join('');
}

async function loadOrders() {
  try {
    const response = await fetch('/api/orders', { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Orders API unavailable');
    const payload = await response.json();
    renderOrdersTable(payload.orders || []);
    markBrokerSynced(true);
  } catch {
    renderOrdersTable([]);
    markBrokerSynced(false);
  }
}

function render() {
  const grid = getActiveGrid();
  const hasMark = typeof grid.markPrice === 'number';
  const entryMove = hasMark && grid.averageEntry ? ((grid.markPrice - grid.averageEntry) / grid.averageEntry) * 100 : null;
  const liqBuffer = hasMark && grid.liquidationPrice ? ((grid.markPrice - grid.liquidationPrice) / grid.markPrice) * 100 : null;
  document.querySelector('#gridName').textContent = grid.name;
  document.querySelector('#gridDescription').textContent = grid.description;
  document.querySelector('#gridStatus').textContent = grid.status;
  document.querySelector('#gridRange').textContent = grid.range;
  document.querySelector('#gridSpacing').textContent = grid.spacing;
  document.querySelector('#realizedPnl').textContent = signedMoney(grid.realizedPnl);
  document.querySelector('#sourceLabel').textContent = grid.source;
  document.querySelector('#positionSize').innerHTML = `${number(grid.size)} <small>shares</small>`;
  document.querySelector('#positionSide').textContent = grid.side;
  document.querySelector('#averageEntry').textContent = money(grid.averageEntry);
  document.querySelector('#markPrice').textContent = money(grid.markPrice);
  document.querySelector('#markMove').textContent = entryMove == null ? 'Live mark unavailable' : `${signedPercent(entryMove)} from entry`;
  document.querySelector('#unrealizedPnl').textContent = signedMoney(grid.unrealizedPnl);
  document.querySelector('#pnlPercent').textContent = entryMove == null ? 'Live mark unavailable' : signedPercent(entryMove);
  document.querySelector('#liquidationPrice').textContent = money(grid.liquidationPrice);
  document.querySelector('#liqDistance').textContent = liqBuffer == null ? 'Not supplied by broker' : `${liqBuffer.toFixed(2)}% buffer to mark`;
  document.querySelector('#healthScore').textContent = grid.health == null ? '—' : grid.health;
  document.querySelector('#healthBar').style.width = grid.health == null ? '0%' : `${grid.health}%`;
  document.querySelector('#utilization').textContent = grid.utilization == null ? 'Unavailable' : `${grid.utilization}%`;
  document.querySelector('#orderNotional').textContent = money(grid.orders.reduce((total, order) => total + order.quantity * order.actualPrice, 0) / 100000) + 'L';
  document.querySelector('#nextTrigger').textContent = money(grid.nextTrigger);
  document.querySelector('#riskMessage').textContent = grid.size === 0 ? 'No position is open. The grid is waiting for its first fill.' : `Mark is inside the active grid. ${grid.orders.length} orders are waiting for execution.`;
  renderTabs();
}

async function loadGrids() {
  setRefreshing(true);
  try {
    const response = await fetch('/api/grids', { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Grid API unavailable');
    const payload = await response.json();
    if (!Array.isArray(payload.grids) || payload.grids.length === 0) throw new Error('No grids returned');
    grids = payload.grids;
    activeGridId = grids[0].id;
    markBrokerSynced(true);
    document.querySelector('#sourceLabel').textContent = 'LIVE BROKER DATA';
    document.querySelector('#footerMode').textContent = 'Groww position and order feed';
  } catch {
    markBrokerSynced(false);
    document.querySelector('#footerMode').textContent = 'Paper data until API feed is connected';
  }
  render();
  setRefreshing(false);
}

function setRefreshing(active) {
  isRefreshing = active;
  const button = document.querySelector('#refreshButton');
  button.disabled = active;
  button.classList.toggle('is-loading', active);
  button.setAttribute('aria-label', active ? 'Refreshing live data' : 'Refresh live data');
}

async function loadMarket() {
  if (marketRefreshing) return;
  marketRefreshing = true;
  try {
    const response = await fetch('/api/market', { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Market API unavailable');
    const payload = await response.json();
    renderMarket(payload.indices || [], payload.source);
  } catch {
    renderMarket([
      { name: 'NIFTY 50', price: null, status: 'UNAVAILABLE' },
      { name: 'S&P BSE Sensex', price: null, status: 'UNAVAILABLE' },
      { name: 'Nifty Bank Index', price: null, status: 'UNAVAILABLE' }
    ], 'UNAVAILABLE');
  } finally {
    marketRefreshing = false;
  }
}

async function loadAccount() {
  try {
    const response = await fetch('/api/account', { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Account API unavailable');
    renderAccount(await response.json());
  } catch {
    renderAccount({ source: 'ACCOUNT DATA UNAVAILABLE', profile: { connected: false }, holdings: [], positions: [], orders: [], margin: {}, instrumentMaster: {}, marketData: {} });
  }
}

let autoTradingState = { enabled: false, killSwitch: false };
let autoRunIndexId = CHART_INDICES[0].id;
let autoEquityChart;
let autoEquitySeries;
let optionMarketIndexId = CHART_INDICES[0].id;

function renderOptionMarketIndexTabs() {
  document.querySelector('#optionMarketIndexTabs').innerHTML = CHART_INDICES.map((index) => `
    <button type="button" class="${index.id === optionMarketIndexId ? 'active' : ''}" data-index-id="${index.id}">${index.name}</button>
  `).join('');
  document.querySelectorAll('#optionMarketIndexTabs button').forEach((button) => button.addEventListener('click', () => {
    optionMarketIndexId = button.dataset.indexId;
    renderOptionMarketIndexTabs();
    loadOptionMarket();
  }));
}

function renderOptionLeg(prefix, leg, side) {
  const symbolEl = document.querySelector(`#opt${prefix}Symbol`);
  const signalEl = document.querySelector(`#opt${prefix}Signal`);
  const quoteNoteEl = document.querySelector(`#opt${prefix}QuoteNote`);
  if (!leg?.available) {
    symbolEl.textContent = leg?.reason || 'Unavailable';
    ['Premium', 'Bid', 'Ask', 'Ltp'].forEach((field) => { document.querySelector(`#opt${prefix}${field}`).textContent = '—'; });
    document.querySelector(`#opt${prefix}LotSize`).textContent = '—';
    document.querySelector(`#opt${prefix}LotValue`).textContent = '—';
    quoteNoteEl.textContent = '';
    signalEl.textContent = side || '—';
    signalEl.className = 'option-leg-signal';
    return;
  }
  symbolEl.textContent = leg.tradingSymbol;
  document.querySelector(`#opt${prefix}Premium`).textContent = leg.premium == null ? 'Unavailable' : money(leg.premium);
  document.querySelector(`#opt${prefix}Bid`).textContent = leg.bid == null ? '—' : money(leg.bid);
  document.querySelector(`#opt${prefix}Ask`).textContent = leg.ask == null ? '—' : money(leg.ask);
  document.querySelector(`#opt${prefix}Ltp`).textContent = leg.ltp == null ? '—' : money(leg.ltp);
  document.querySelector(`#opt${prefix}LotSize`).textContent = number(leg.lotSize);
  document.querySelector(`#opt${prefix}LotValue`).textContent = leg.premium == null ? 'Unavailable (needs live premium)' : money(leg.premium * leg.lotSize);
  quoteNoteEl.textContent = leg.quoteUnavailableReason || '';
  signalEl.textContent = side || 'HOLD';
  signalEl.className = `option-leg-signal ${side && side !== 'HOLD' ? 'active' : ''}`;
}

function renderOptionMarket(payload) {
  document.querySelector('#optUnderlyingName').textContent = payload.underlyingName || '—';
  document.querySelector('#optSpot').textContent = payload.spot == null ? 'Unavailable' : money(payload.spot);
  document.querySelector('#optAtmStrike').textContent = payload.atmStrike == null ? '—' : number(payload.atmStrike);
  document.querySelector('#optExpiry').textContent = payload.call?.available ? formatDateOnly(payload.call.expiryDate) : '—';
  document.querySelector('#optSignalAsOf').textContent = payload.signal?.asOf
    ? `Setup condition as of the latest closed candle (${formatTimestamp(payload.signal.asOf)}) — signal only, no order is placed.`
    : 'Signal unavailable — could not evaluate the latest candle.';

  renderOptionLeg('Call', payload.call, payload.signal?.call);
  renderOptionLeg('Put', payload.put, payload.signal?.put);

  const availableMargin = lastAccountPayload?.margin?.equity_margin_details?.clear_cash;
  document.querySelector('#optAvailableMargin').textContent = availableMargin == null ? '—' : money(availableMargin);
  document.querySelector('#optMaxLots').textContent = 'Unavailable — requires live premium to compute margin-based lot sizing';
}

async function loadOptionMarket() {
  try {
    const response = await fetch(`/api/auto-trading/option-context/${optionMarketIndexId}`, { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Option market API unavailable');
    renderOptionMarket(await response.json());
  } catch {
    document.querySelector('#optUnderlyingName').textContent = 'Unavailable';
    document.querySelector('#optSpot').textContent = '—';
    document.querySelector('#optAtmStrike').textContent = '—';
    document.querySelector('#optExpiry').textContent = '—';
    document.querySelector('#optSignalAsOf').textContent = 'Could not load option market context.';
  }
}

function renderAutoTradingStatus(status) {
  autoTradingState = { enabled: status.enabled, killSwitch: status.killSwitch };
  const blocked = status.enabled && (status.killSwitch || status.consecutiveLossHalt);
  const enabledTag = document.querySelector('#autoEnabledTag');
  enabledTag.textContent = blocked ? 'BLOCKED' : status.enabled ? 'ENABLED' : 'DISABLED';
  enabledTag.className = `status-tag ${status.enabled && !blocked ? '' : 'danger'}`;

  const statusDot = document.querySelector('#autoStatusDot');
  statusDot.className = `auto-status-dot ${status.enabled && !blocked ? 'ok' : blocked ? 'danger' : 'warning'}`;

  const killArmedCard = document.querySelector('#autoKillArmedCard');
  killArmedCard.hidden = !status.killSwitch;

  const pill = document.querySelector('#autoStatusPill');
  pill.classList.remove('warning', 'danger');
  if (status.killSwitch) {
    pill.classList.add('danger');
    pill.innerHTML = '<i></i>KILL SWITCH ARMED';
  } else if (blocked) {
    pill.classList.add('danger');
    pill.innerHTML = '<i></i>AUTO TRADING BLOCKED';
  } else if (status.enabled) {
    pill.innerHTML = '<i></i>AUTO TRADING ACTIVE';
  } else {
    pill.classList.add('warning');
    pill.innerHTML = '<i></i>AUTO DISABLED';
  }

  systemStatusState.auto = { enabled: status.enabled, killSwitch: status.killSwitch, consecutiveLossHalt: status.consecutiveLossHalt };
  updateAutoGates();
  updateCriticalBanner();
  document.querySelector('#autoToggleButton').textContent = status.enabled ? 'Disable Auto Trading' : 'Enable Auto Trading';
  document.querySelector('#autoKillButton').textContent = status.killSwitch ? 'Reset Kill Switch' : 'Kill Switch';

  document.querySelector('#autoKillReasonNote').innerHTML =
    `<strong>Reason:</strong> ${escapeHtml(status.killSwitchReason || 'No reason recorded')}`;

  document.querySelector('#autoLossHaltTag').hidden = !status.consecutiveLossHalt;
  document.querySelector('#autoLossHaltResetButton').hidden = !status.consecutiveLossHalt;

  document.querySelector('#autoTradesToday').textContent = status.tradesToday;
  document.querySelector('#autoRealizedPnl').textContent = signedMoney(status.realizedPnlToday);
  document.querySelector('#autoMaxOpenPositions').textContent = number(status.limits.maxOpenPositions);
  document.querySelector('#autoDailyLossLimit').textContent = money(status.limits.dailyLossLimit);
  document.querySelector('#autoEntryCutoff').textContent = status.limits.entryCutoff || '—';
  document.querySelector('#autoSquareOffTime').textContent = status.limits.squareOffTime || '—';
  document.querySelector('#autoCooldownMinutes').textContent = `${status.limits.cooldownMinutes} min`;
  document.querySelector('#autoConsecutiveLosses').textContent =
    `${status.consecutiveLosses} / ${status.limits.maxConsecutiveLosses}`;
  document.querySelector('#autoSchedulerNote').textContent =
    `Background scheduler runs every ${Math.round(status.scheduler.tickSeconds / 60)} minutes for all indices while enabled. `
    + 'Paper execution only — no live order is ever placed by Auto Trading.';

  const accountsBody = document.querySelector('#autoAccountsBody');
  const accountEntries = Object.entries(status.accounts || {});
  accountsBody.innerHTML = accountEntries.length === 0
    ? '<tr class="empty-row"><td colspan="4">No index accounts configured.</td></tr>'
    : accountEntries.map(([indexId, account]) => `
      <tr>
        <td class="mono">${escapeHtml(account.indexName || indexId)}</td>
        <td class="mono">${account.quantity === 0 ? 'Flat' : `${account.quantity > 0 ? 'LONG' : 'SHORT'} ${number(Math.abs(account.quantity))}`}</td>
        <td class="mono">${money(account.averagePrice)}</td>
        <td class="mono">${money(account.cash)}</td>
      </tr>
    `).join('');
}

async function loadAutoTradingStatus() {
  try {
    const response = await fetch('/api/auto-trading/status', { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Auto trading API unavailable');
    renderAutoTradingStatus(await response.json());
  } catch {
    /* leave last known state on screen */
  }
}

async function toggleAutoTrading() {
  const endpoint = autoTradingState.enabled ? '/api/auto-trading/disable' : '/api/auto-trading/enable';
  const response = await fetch(endpoint, { method: 'POST' });
  if (response.ok) renderAutoTradingStatus(await response.json());
}

async function toggleKillSwitch() {
  if (autoTradingState.killSwitch) {
    const response = await fetch('/api/auto-trading/kill-switch/reset', { method: 'POST' });
    if (response.ok) renderAutoTradingStatus(await response.json());
    return;
  }
  const reason = window.prompt('Reason for engaging the kill switch (shown in the audit trail):', '');
  if (reason === null) return; // cancelled
  const query = reason.trim() ? `?reason=${encodeURIComponent(reason.trim())}` : '';
  const response = await fetch(`/api/auto-trading/kill-switch${query}`, { method: 'POST' });
  if (response.ok) renderAutoTradingStatus(await response.json());
}

async function resetConsecutiveLossHalt() {
  const response = await fetch('/api/auto-trading/consecutive-loss-halt/reset', { method: 'POST' });
  if (response.ok) renderAutoTradingStatus(await response.json());
}

function renderAutoRunIndexTabs() {
  document.querySelector('#autoRunIndexTabs').innerHTML = CHART_INDICES.map((index) => `
    <button type="button" class="${index.id === autoRunIndexId ? 'active' : ''}" data-index-id="${index.id}">${index.name}</button>
  `).join('');
  document.querySelectorAll('#autoRunIndexTabs button').forEach((button) => button.addEventListener('click', () => {
    autoRunIndexId = button.dataset.indexId;
    renderAutoRunIndexTabs();
  }));
}

async function runAutoTradingCycle() {
  const button = document.querySelector('#autoRunButton');
  const resultEl = document.querySelector('#autoResult');
  button.disabled = true;
  try {
    const response = await fetch(`/api/auto-trading/run/${autoRunIndexId}?interval=5m`, { method: 'POST' });
    if (!response.ok) throw new Error('Auto trading run failed');
    const payload = await response.json();
    resultEl.innerHTML = `<dl>
      <dt>Index</dt><dd>${escapeHtml(payload.indexId)} · ${escapeHtml(payload.interval)}</dd>
      <dt>Signal</dt><dd>${escapeHtml(payload.signal.action)} — ${escapeHtml(payload.signal.reason)}</dd>
      <dt>Risk check</dt><dd>${payload.risk.allowed ? 'Passed' : 'Blocked'} — ${escapeHtml(payload.risk.reason)}</dd>
      <dt>Order</dt><dd>${payload.order ? `${escapeHtml(payload.order.status)} — ${escapeHtml(payload.order.detail)}` : 'Not placed'}</dd>
    </dl>`;
    await Promise.all([loadAutoTradingStatus(), loadAutoTradingSignals(), loadAutoTradingPerformance()]);
  } catch {
    resultEl.innerHTML = '<p class="data-empty">Auto trading run failed. Try again.</p>';
  } finally {
    button.disabled = false;
  }
}

function renderAutoSignalsTable(signals) {
  const body = document.querySelector('#autoSignalsBody');
  if (signals.length === 0) {
    body.innerHTML = '<tr class="empty-row"><td colspan="5">No signals generated yet.</td></tr>';
    return;
  }
  body.innerHTML = signals.map((signal) => `
    <tr>
      <td class="mono">${formatTimestamp(signal.createdAt)}</td>
      <td class="mono">${escapeHtml(signal.indexId || '—')}</td>
      <td><span class="side ${signal.action === 'SELL' ? 'sell' : 'buy'}">${escapeHtml(signal.action || '—')}</span></td>
      <td class="mono">${escapeHtml(signal.reason || '—')}</td>
      <td class="mono">${money(signal.price)}</td>
    </tr>
  `).join('');
}

async function loadAutoTradingSignals() {
  try {
    const response = await fetch('/api/auto-trading/signals?limit=20', { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Auto trading signals API unavailable');
    const payload = await response.json();
    renderAutoSignalsTable(payload.signals || []);
  } catch {
    document.querySelector('#autoSignalsBody').innerHTML = '<tr class="empty-row"><td colspan="5">Could not load signal log.</td></tr>';
  }
}

async function loadAutoTradingPerformance() {
  try {
    const response = await fetch('/api/reports/strategy-performance', { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Strategy performance API unavailable');
    const payload = await response.json();
    const perf = (payload.strategies || []).find((strategy) => strategy.source === 'algoedge.auto_trader');
    document.querySelector('#autoPerfTrades').textContent = perf ? number(perf.tradesClosed) : '0';
    document.querySelector('#autoPerfWinRate').textContent = !perf || perf.winRate == null ? '—' : `${perf.winRate.toFixed(1)}%`;
    const totalPnlEl = document.querySelector('#autoPerfTotalPnl');
    totalPnlEl.textContent = signedMoney(perf ? perf.totalPnl : 0);
    totalPnlEl.className = (perf ? perf.totalPnl : 0) >= 0 ? 'positive' : 'warning';
  } catch {
    /* leave last known state on screen */
  }
}

function initAutoEquityChart() {
  const container = document.querySelector('#autoEquityChart');
  const colors = chartColors();
  autoEquityChart = LightweightCharts.createChart(container, {
    layout: { background: { color: colors.bg }, textColor: colors.text, fontFamily: "'JetBrains Mono', monospace", fontSize: 11 },
    grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
    rightPriceScale: { borderColor: colors.border },
    timeScale: { borderColor: colors.border, timeVisible: true, secondsVisible: false },
    autoSize: true
  });
  autoEquitySeries = autoEquityChart.addLineSeries({ color: colors.up, lineWidth: 2 });
}

async function loadAutoTradingEquityCurve() {
  const emptyMessage = document.querySelector('#autoEquityEmpty');
  try {
    const response = await fetch('/api/auto-trading/equity-curve', { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Equity curve API unavailable');
    const payload = await response.json();
    const points = (payload.points || [])
      .map((point) => ({ time: Math.floor(new Date(point.closedAt).getTime() / 1000), value: point.cumulativePnl }))
      .filter((point) => Number.isFinite(point.time));
    if (points.length === 0) throw new Error('No closed paper trades yet');
    autoEquitySeries.setData(points);
    autoEquityChart.timeScale().fitContent();
    emptyMessage.hidden = true;
  } catch {
    autoEquitySeries.setData([]);
    emptyMessage.hidden = false;
  }
}

document.querySelector('#autoToggleButton').addEventListener('click', toggleAutoTrading);
document.querySelector('#autoKillButton').addEventListener('click', toggleKillSwitch);
document.querySelector('#autoLossHaltResetButton').addEventListener('click', resetConsecutiveLossHalt);
document.querySelector('#autoRunButton').addEventListener('click', runAutoTradingCycle);
renderAutoRunIndexTabs();
initAutoEquityChart();

function refreshAll() {
  if (isRefreshing) return;
  loadGrids();
  loadPositions();
  loadOrders();
  loadAutoTradingStatus();
  loadMarket();
  loadCandles();
  loadStrategySignal();
}

function chartColors() {
  const dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  return dark
    ? { bg: '#12151f', text: '#9098ac', grid: '#1b1f2d', border: '#232838', up: '#34d399', down: '#f87171' }
    : { bg: '#ffffff', text: '#6b7280', grid: '#f1f2f8', border: '#e6e8f0', up: '#12875a', down: '#d9364a' };
}

function initChart() {
  const container = document.querySelector('#chartContainer');
  const colors = chartColors();
  chart = LightweightCharts.createChart(container, {
    layout: { background: { color: colors.bg }, textColor: colors.text, fontFamily: "'JetBrains Mono', monospace", fontSize: 11 },
    grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
    rightPriceScale: { borderColor: colors.border },
    timeScale: { borderColor: colors.border, timeVisible: true, secondsVisible: false },
    autoSize: true
  });
  candleSeries = chart.addCandlestickSeries({
    upColor: colors.up,
    downColor: colors.down,
    borderVisible: false,
    wickUpColor: colors.up,
    wickDownColor: colors.down
  });
}

function renderIndexTabs() {
  document.querySelector('#indexTabs').innerHTML = CHART_INDICES.map((index) => `
    <button type="button" class="${index.id === activeIndexId ? 'active' : ''}" data-index-id="${index.id}">${index.name}</button>
  `).join('');
  document.querySelectorAll('#indexTabs button').forEach((button) => button.addEventListener('click', () => {
    activeIndexId = button.dataset.indexId;
    renderIndexTabs();
    loadCandles();
    loadStrategySignal();
  }));
}

function renderTimeframeTabs() {
  document.querySelector('#timeframeTabs').innerHTML = CHART_TIMEFRAMES.map((frame) => `
    <button type="button" class="${frame.id === activeTimeframe ? 'active' : ''}" data-timeframe="${frame.id}">${frame.label}</button>
  `).join('');
  document.querySelectorAll('#timeframeTabs button').forEach((button) => button.addEventListener('click', () => {
    activeTimeframe = button.dataset.timeframe;
    renderTimeframeTabs();
    loadCandles();
    loadStrategySignal();
  }));
}

async function loadCandles() {
  const emptyMessage = document.querySelector('#chartEmpty');
  try {
    const response = await fetch(`/api/market/candles/${activeIndexId}?interval=${activeTimeframe}`, { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Candle API unavailable');
    const payload = await response.json();
    const candles = payload.candles || [];
    if (!candles.length) throw new Error('No candle data');
    candleSeries.setData(candles);
    chart.timeScale().fitContent();
    emptyMessage.hidden = true;
  } catch {
    candleSeries.setData([]);
    emptyMessage.hidden = false;
  }
}

async function loadStrategySignal() {
  const actionEl = document.querySelector('#strategyAction');
  const reasonEl = document.querySelector('#strategyReason');
  try {
    const response = await fetch(`/api/strategy/signal/${activeIndexId}?interval=${activeTimeframe}`, { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Strategy API unavailable');
    const payload = await response.json();
    const signal = payload.signal || {};
    actionEl.textContent = signal.action || '—';
    actionEl.className = `strategy-action ${signal.action === 'BUY' ? 'buy' : signal.action === 'SELL' ? 'sell' : ''}`;
    reasonEl.textContent = signal.reason || '—';
    document.querySelector('#strategyRsi').textContent = signal.rsi == null ? '—' : signal.rsi.toFixed(1);
    document.querySelector('#strategyEma').textContent = signal.ema == null ? '—' : money(signal.ema);
  } catch {
    actionEl.textContent = '—';
    actionEl.className = 'strategy-action';
    reasonEl.textContent = 'Signal unavailable';
    document.querySelector('#strategyRsi').textContent = '—';
    document.querySelector('#strategyEma').textContent = '—';
  }
}

let tradeIndexId = CHART_INDICES[0].id;
let tradeRight = 'CE';
let tradeSide = 'BUY';
let tradePreviewValid = false;

function renderTradeIndexTabs() {
  document.querySelector('#tradeIndexTabs').innerHTML = CHART_INDICES.map((index) => `
    <button type="button" class="${index.id === tradeIndexId ? 'active' : ''}" data-index-id="${index.id}">${index.name}</button>
  `).join('');
  document.querySelectorAll('#tradeIndexTabs button').forEach((button) => button.addEventListener('click', () => {
    tradeIndexId = button.dataset.indexId;
    renderTradeIndexTabs();
    resetTradePreview();
    loadTradeExpiries();
  }));
}

function initSegmented(containerId, onSelect) {
  const container = document.querySelector(containerId);
  container.querySelectorAll('.segmented-option').forEach((button) => button.addEventListener('click', () => {
    container.querySelectorAll('.segmented-option').forEach((b) => b.classList.remove('active'));
    button.classList.add('active');
    onSelect(button.dataset.value);
    resetTradePreview();
  }));
}

function updateOrderTypeFields() {
  const orderType = document.querySelector('#tradeOrderType').value;
  document.querySelector('#tradePriceField').hidden = !(orderType === 'LIMIT' || orderType === 'SL');
  document.querySelector('#tradeTriggerField').hidden = !(orderType === 'SL' || orderType === 'SL_M');
}

async function loadTradeExpiries() {
  const select = document.querySelector('#tradeExpiry');
  select.innerHTML = '<option value="">Loading&hellip;</option>';
  try {
    const response = await fetch(`/api/manual-trading/expiries/${tradeIndexId}`, { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Expiries unavailable');
    const payload = await response.json();
    const expiries = payload.expiries || [];
    if (!expiries.length) throw new Error('No expiries returned');
    select.innerHTML = expiries.map((expiry) => `<option value="${expiry}">${expiry}</option>`).join('');
    await loadTradeStrikes();
  } catch {
    select.innerHTML = '<option value="">Unavailable</option>';
    document.querySelector('#tradeStrike').innerHTML = '<option value="">Unavailable</option>';
  }
}

async function loadTradeStrikes() {
  const expiry = document.querySelector('#tradeExpiry').value;
  const select = document.querySelector('#tradeStrike');
  if (!expiry) { select.innerHTML = '<option value="">Select expiry first</option>'; return; }
  select.innerHTML = '<option value="">Loading&hellip;</option>';
  try {
    const response = await fetch(`/api/manual-trading/strikes/${tradeIndexId}?expiry=${expiry}`, { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Strikes unavailable');
    const payload = await response.json();
    const strikes = payload.strikes || [];
    if (!strikes.length) throw new Error('No strikes returned');
    select.innerHTML = strikes.map((strike) => `<option value="${strike}">${number(strike)}</option>`).join('');
  } catch {
    select.innerHTML = '<option value="">Unavailable</option>';
  }
}

function resetTradePreview() {
  tradePreviewValid = false;
  document.querySelector('#tradeConfirmBlock').hidden = true;
  document.querySelector('#tradeConfirmCheckbox').checked = false;
  document.querySelector('#tradePlaceButton').disabled = true;
  document.querySelector('#tradePreview').innerHTML = '<p class="data-empty">Preview an order to see the resolved contract before placing anything.</p>';
}

function tradeQueryParams() {
  const params = new URLSearchParams({
    index_id: tradeIndexId,
    expiry: document.querySelector('#tradeExpiry').value,
    strike: document.querySelector('#tradeStrike').value,
    right: tradeRight,
    side: tradeSide,
    order_type: document.querySelector('#tradeOrderType').value,
    lots: document.querySelector('#tradeLots').value || '1',
    product: document.querySelector('#tradeProduct').value,
  });
  const price = document.querySelector('#tradePrice').value;
  const trigger = document.querySelector('#tradeTrigger').value;
  if (price) params.set('price', price);
  if (trigger) params.set('trigger_price', trigger);
  return params;
}

async function previewTradeOrder() {
  const expiry = document.querySelector('#tradeExpiry').value;
  const strike = document.querySelector('#tradeStrike').value;
  const previewEl = document.querySelector('#tradePreview');
  if (!expiry || !strike) {
    previewEl.innerHTML = '<p class="data-empty">Select an expiry and strike first.</p>';
    return;
  }
  try {
    const response = await fetch(`/api/manual-trading/preview?${tradeQueryParams()}`, { headers: { Accept: 'application/json' } });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || 'Preview failed');
    const contract = payload.contract;
    document.querySelector('#tradeLiveTag').textContent = payload.liveTradingEnabled ? 'LIVE TRADING ENABLED' : 'DRY RUN';
    document.querySelector('#tradeLiveTag').className = `status-tag ${payload.liveTradingEnabled ? 'danger' : ''}`;
    previewEl.innerHTML = `<dl>
      <dt>Contract</dt><dd>${escapeHtml(contract.tradingSymbol)}</dd>
      <dt>Exchange</dt><dd>${escapeHtml(contract.exchange)}</dd>
      <dt>Expiry</dt><dd>${escapeHtml(contract.expiryDate)}</dd>
      <dt>Lot size</dt><dd>${number(contract.lotSize)}</dd>
      <dt>Quantity</dt><dd>${number(payload.quantity)} (${document.querySelector('#tradeLots').value || 1} lot(s))</dd>
    </dl>`;
    tradePreviewValid = true;
    const confirmBlock = document.querySelector('#tradeConfirmBlock');
    confirmBlock.hidden = false;
    document.querySelector('#tradeConfirmCheckbox').checked = false;
    document.querySelector('#tradePlaceButton').disabled = true;
    if (!payload.liveTradingEnabled) {
      document.querySelector('#tradePlaceButton').disabled = true;
      confirmBlock.querySelector('.trade-confirm-check').innerHTML =
        '<input type="checkbox" disabled /> Live trading is disabled (set ALGOEDGE_LIVE_TRADING=true) - preview only.';
    } else {
      confirmBlock.querySelector('.trade-confirm-check').innerHTML =
        '<input type="checkbox" id="tradeConfirmCheckbox" /> I have reviewed this order and want to place it live.';
      document.querySelector('#tradeConfirmCheckbox').addEventListener('change', (event) => {
        document.querySelector('#tradePlaceButton').disabled = !event.target.checked;
      });
    }
  } catch (error) {
    resetTradePreview();
    previewEl.innerHTML = `<p class="data-empty">${escapeHtml(error.message || 'Preview failed')}</p>`;
  }
}

async function placeTradeOrder() {
  if (!tradePreviewValid) return;
  const button = document.querySelector('#tradePlaceButton');
  const resultEl = document.querySelector('#tradeResult');
  button.disabled = true;
  try {
    const response = await fetch(`/api/manual-trading/order?${tradeQueryParams()}`, { method: 'POST', headers: { Accept: 'application/json' } });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || 'Order failed');
    const outcomeClass = payload.outcome === 'SUCCESS' ? 'positive' : payload.outcome === 'CANCELLED' ? 'warning' : '';
    resultEl.innerHTML = `<dl>
      <dt>Outcome</dt><dd class="${outcomeClass}">${escapeHtml(payload.outcome)}</dd>
      <dt>Order status</dt><dd>${escapeHtml(payload.orderStatus || '—')}</dd>
      <dt>Groww order ID</dt><dd>${escapeHtml(payload.growwOrderId || '—')}</dd>
      <dt>Reason</dt><dd>${escapeHtml(payload.reason || '—')}</dd>
      <dt>Checks</dt><dd>${payload.attempts == null ? '—' : number(payload.attempts)}</dd>
    </dl>`;
  } catch (error) {
    resultEl.innerHTML = `<dl><dt>Error</dt><dd>${escapeHtml(error.message || 'Order failed')}</dd></dl>`;
  } finally {
    resetTradePreview();
    loadManualTrades();
  }
}

function renderManualTrades(trades) {
  const body = document.querySelector('#manualTradesBody');
  if (trades.length === 0) {
    body.innerHTML = '<tr class="empty-row"><td colspan="7">No manual trades recorded yet.</td></tr>';
    return;
  }
  body.innerHTML = trades.map((trade) => `
    <tr>
      <td class="mono">${escapeHtml(trade.tradingSymbol)}</td>
      <td><span class="side ${trade.side === 'LONG' ? 'buy' : 'sell'}">${escapeHtml(trade.side)}</span></td>
      <td class="mono">${number(trade.quantity)}</td>
      <td class="mono">${money(trade.entryPrice)}</td>
      <td class="mono">${trade.exitPrice == null ? '—' : money(trade.exitPrice)}</td>
      <td><span class="order-status">● ${escapeHtml(trade.status)}</span></td>
      <td class="mono ${trade.pnl == null ? '' : trade.pnl >= 0 ? 'positive' : 'warning'}">${trade.pnl == null ? '—' : signedMoney(trade.pnl)}</td>
    </tr>
  `).join('');
}

async function loadManualTrades() {
  try {
    const response = await fetch('/api/manual-trading/trades', { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Manual trades API unavailable');
    const payload = await response.json();
    renderManualTrades(payload.trades || []);
  } catch {
    document.querySelector('#manualTradesBody').innerHTML = '<tr class="empty-row"><td colspan="7">Could not load manual trades.</td></tr>';
  }
}

function initTradePanel() {
  renderTradeIndexTabs();
  initSegmented('#tradeRight', (value) => { tradeRight = value; });
  initSegmented('#tradeSide', (value) => { tradeSide = value; });
  document.querySelector('#tradeOrderType').addEventListener('change', () => { updateOrderTypeFields(); resetTradePreview(); });
  document.querySelector('#tradeExpiry').addEventListener('change', () => { loadTradeStrikes(); resetTradePreview(); });
  document.querySelector('#tradeStrike').addEventListener('change', resetTradePreview);
  document.querySelector('#tradeLots').addEventListener('change', resetTradePreview);
  document.querySelector('#tradeProduct').addEventListener('change', resetTradePreview);
  document.querySelector('#tradePreviewButton').addEventListener('click', previewTradeOrder);
  document.querySelector('#tradePlaceButton').addEventListener('click', placeTradeOrder);
  updateOrderTypeFields();
  loadTradeExpiries();
  loadManualTrades();
}

// Pulse is the one page that fits to the viewport instead of scrolling (see
// the .pulse-mode CSS) - every other page keeps normal document scroll.
// --pulse-header-h is measured, never hardcoded, so it stays correct whether
// or not the critical banner is currently showing.
function updatePulseFitHeight() {
  const topbar = document.querySelector('.topbar');
  const banner = document.querySelector('#criticalBanner');
  const footer = document.querySelector('.shell > footer');
  const headerHeight = (topbar?.offsetHeight || 0)
    + (banner && !banner.hidden ? banner.offsetHeight : 0)
    + (footer?.offsetHeight || 0);
  document.documentElement.style.setProperty('--pulse-chrome-h', `${headerHeight}px`);
}

function navigateTo(pageId) {
  const railLinks = [...document.querySelectorAll('.rail-link')];
  const isKnownPage = railLinks.some((link) => link.getAttribute('href') === `#${pageId}`);
  if (!isKnownPage) return;
  railLinks.forEach((link) => {
    const isTarget = link.getAttribute('href') === `#${pageId}`;
    link.classList.toggle('active', isTarget);
    const page = document.querySelector(link.getAttribute('href'));
    if (page) page.hidden = !isTarget;
    if (isTarget) link.scrollIntoView({ block: 'nearest' });
  });
  if (`#${pageId}` !== window.location.hash) {
    history.replaceState(null, '', `#${pageId}`);
  }
  document.querySelector('main').classList.toggle('pulse-mode', pageId === 'market-pulse');
  if (pageId === 'market-pulse') updatePulseFitHeight();
  document.querySelector('main').scrollTo({ top: 0 });
  window.scrollTo({ top: 0 });
}

function initNav() {
  document.querySelectorAll('.rail-link').forEach((link) => {
    link.addEventListener('click', (event) => {
      event.preventDefault();
      navigateTo(link.getAttribute('href').slice(1));
    });
  });
  // Route persistence: a refresh (or a shared link) lands back on whichever
  // page was open, derived from the URL hash rather than click history alone.
  // Always run through navigateTo (even for the default page) so pulse-mode
  // and its fit-to-viewport height get applied on first load too.
  navigateTo(window.location.hash ? window.location.hash.slice(1) : 'market-pulse');
  window.addEventListener('hashchange', () => navigateTo(window.location.hash.slice(1)));
  window.addEventListener('resize', () => {
    if (document.querySelector('main').classList.contains('pulse-mode')) updatePulseFitHeight();
  });
}

document.querySelector('#refreshButton').addEventListener('click', refreshAll);
function brokerConnectionTagClass(connectionStatus) {
  if (connectionStatus === 'CONNECTED') return '';
  if (connectionStatus === 'MISSING') return 'danger';
  return 'danger';
}

function renderBrokerStatus(status) {
  lastBrokerStatusPayload = status;
  renderPositionsBrokerCard();
  document.querySelector('#brokerApiKeyMasked').textContent = status.apiKeyMasked || 'Not configured';
  document.querySelector('#brokerApiSecretMasked').textContent = status.apiSecretMasked || 'Not configured';
  document.querySelector('#brokerAccessTokenMasked').textContent = status.accessTokenMasked || 'Not configured';

  const connectionTag = document.querySelector('#brokerConnectionTag');
  connectionTag.textContent = status.connectionStatus;
  connectionTag.className = `status-tag ${brokerConnectionTagClass(status.connectionStatus)}`;

  document.querySelector('#brokerTokenStatus').textContent = status.tokenStatus;
  document.querySelector('#brokerTokenCreatedAt').textContent = formatTimestamp(status.tokenCreatedAt);
  document.querySelector('#brokerTokenExpiryAt').textContent = status.tokenExpiryAt
    ? formatTimestamp(status.tokenExpiryAt)
    : 'Not published by Groww for this auth method';
  document.querySelector('#brokerLastValidatedAt').textContent = formatTimestamp(status.lastValidatedAt);
  document.querySelector('#brokerLastSuccessAt').textContent = formatTimestamp(status.lastSuccessfulRequestAt);
  document.querySelector('#brokerLastError').textContent = status.lastError || 'None';
  document.querySelector('#brokerPersisted').textContent = status.credentialsPersisted ? 'Yes (encrypted)' : 'No (in-memory only this session)';

  const pill = document.querySelector('#brokerStatusPill');
  pill.classList.remove('warning', 'danger');
  if (status.connectionStatus === 'CONNECTED') {
    pill.innerHTML = '<i></i>Groww Connected';
  } else if (status.connectionStatus === 'TOKEN_EXPIRED') {
    pill.classList.add('warning');
    pill.innerHTML = '<i></i>Token Update Required';
  } else {
    pill.classList.add('danger');
    pill.innerHTML = '<i></i>Groww Disconnected';
  }

  systemStatusState.broker.connected = status.connectionStatus === 'CONNECTED';
  updatePulseStatus();
  updateAutoGates();
  updateCriticalBanner();
}

async function loadBrokerStatus() {
  try {
    const response = await fetch('/api/broker/status', { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Broker status API unavailable');
    renderBrokerStatus(await response.json());
  } catch {
    document.querySelector('#brokerConnectionTag').textContent = 'UNAVAILABLE';
    document.querySelector('#brokerConnectionTag').className = 'status-tag danger';
  }
}

function renderBrokerHistoryTable(events) {
  const body = document.querySelector('#brokerHistoryBody');
  if (events.length === 0) {
    body.innerHTML = '<tr class="empty-row"><td colspan="5">No token/credential events recorded yet.</td></tr>';
    return;
  }
  body.innerHTML = events.map((event) => `
    <tr>
      <td class="mono">${formatTimestamp(event.createdAt)}</td>
      <td class="mono">${escapeHtml(event.event || '—')}</td>
      <td class="mono ${event.status === 'SUCCESS' ? 'positive' : 'warning'}">${escapeHtml(event.status || '—')}</td>
      <td class="mono">${escapeHtml(event.tokenReference || '—')}</td>
      <td class="mono">${escapeHtml(event.errorMessage || '—')}</td>
    </tr>
  `).join('');
}

async function loadBrokerHistory() {
  try {
    const response = await fetch('/api/broker/history?limit=20', { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Broker history API unavailable');
    const payload = await response.json();
    renderBrokerHistoryTable(payload.events || []);
  } catch {
    document.querySelector('#brokerHistoryBody').innerHTML = '<tr class="empty-row"><td colspan="5">Could not load token history.</td></tr>';
  }
}

function showBrokerResult(message, isError) {
  const el = document.querySelector('#brokerActionResult');
  el.textContent = message;
  el.className = `broker-result ${isError ? 'warning' : 'positive'}`;
}

function initBrokerPanel() {
  const tokenForm = document.querySelector('#brokerTokenForm');
  const credentialsForm = document.querySelector('#brokerCredentialsForm');

  document.querySelector('#brokerUpdateTokenButton').addEventListener('click', () => {
    credentialsForm.hidden = true;
    tokenForm.hidden = !tokenForm.hidden;
  });
  document.querySelector('#brokerTokenCancel').addEventListener('click', () => { tokenForm.hidden = true; });

  document.querySelector('#brokerUpdateCredentialsButton').addEventListener('click', () => {
    tokenForm.hidden = true;
    credentialsForm.hidden = !credentialsForm.hidden;
  });
  document.querySelector('#brokerCredentialsCancel').addEventListener('click', () => { credentialsForm.hidden = true; });

  tokenForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const input = document.querySelector('#brokerAccessTokenInput');
    const submitButton = document.querySelector('#brokerTokenSubmit');
    submitButton.disabled = true;
    try {
      const response = await fetch('/api/broker/access-token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ accessToken: input.value }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || 'Token update failed');
      showBrokerResult('Access token validated and saved. Connected.', false);
      input.value = '';
      tokenForm.hidden = true;
      await Promise.all([loadBrokerStatus(), loadBrokerHistory()]);
    } catch (error) {
      showBrokerResult(error.message || 'Token update failed.', true);
    } finally {
      submitButton.disabled = false;
    }
  });

  credentialsForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const keyInput = document.querySelector('#brokerApiKeyInput');
    const secretInput = document.querySelector('#brokerApiSecretInput');
    const submitButton = document.querySelector('#brokerCredentialsSubmit');
    submitButton.disabled = true;
    try {
      const response = await fetch('/api/broker/credentials', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ apiKey: keyInput.value, apiSecret: secretInput.value }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || 'Credential update failed');
      showBrokerResult('API key/secret validated and saved. Connected.', false);
      keyInput.value = '';
      secretInput.value = '';
      credentialsForm.hidden = true;
      await Promise.all([loadBrokerStatus(), loadBrokerHistory()]);
    } catch (error) {
      showBrokerResult(error.message || 'Credential update failed.', true);
    } finally {
      submitButton.disabled = false;
    }
  });

  document.querySelector('#brokerTestConnectionButton').addEventListener('click', async () => {
    const button = document.querySelector('#brokerTestConnectionButton');
    button.disabled = true;
    try {
      const response = await fetch('/api/broker/test-connection', { method: 'POST' });
      const payload = await response.json();
      showBrokerResult(payload.connected ? `Connected — ${payload.message}` : `Connection failed — ${payload.message}`, !payload.connected);
      await Promise.all([loadBrokerStatus(), loadBrokerHistory()]);
    } catch {
      showBrokerResult('Test connection failed. Try again.', true);
    } finally {
      button.disabled = false;
    }
  });

  document.querySelector('#brokerStatusPill').addEventListener('click', () => navigateTo('broker-panel'));
  document.querySelector('#autoStatusPill').addEventListener('click', () => navigateTo('auto-trading-panel'));
  document.querySelector('#criticalBanner').addEventListener('click', () => {
    const target = document.querySelector('#criticalBanner').dataset.target;
    if (target) navigateTo(target);
  });
}

let backtestIndexId = CHART_INDICES[0].id;
let backtestMode = 'single';

function renderBacktestIndexTabs() {
  document.querySelector('#backtestIndexTabs').innerHTML = CHART_INDICES.map((index) => `
    <button type="button" class="${index.id === backtestIndexId ? 'active' : ''}" data-index-id="${index.id}">${index.name}</button>
  `).join('');
  document.querySelectorAll('#backtestIndexTabs button').forEach((button) => button.addEventListener('click', () => {
    backtestIndexId = button.dataset.indexId;
    renderBacktestIndexTabs();
  }));
}

function renderMetricCard(label, value, className) {
  return `<div class="backtest-metric-card"><span>${escapeHtml(label)}</span><strong class="${className || ''}">${value}</strong></div>`;
}

function renderBacktestMetrics(metrics) {
  if (metrics.totalTrades === 0) {
    return '<p class="data-empty">No trades were generated in this window.</p>';
  }
  const pnlClass = metrics.netPoints >= 0 ? 'positive' : 'warning';
  const breakdown = (label, perf) => `
    <div class="backtest-split-card">
      <h3>${escapeHtml(label)}</h3>
      <div class="backtest-metric-grid">
        ${renderMetricCard('Trades', number(perf.trades))}
        ${renderMetricCard('Win rate', perf.win_rate == null ? '—' : `${perf.win_rate.toFixed(1)}%`)}
        ${renderMetricCard('Net points', perf.net_points.toFixed(1), perf.net_points >= 0 ? 'positive' : 'warning')}
      </div>
    </div>`;
  const bucketRows = (buckets) => buckets.map((b) => `
    <tr>
      <td class="mono">${escapeHtml(b.label)}</td>
      <td class="mono">${number(b.trades)}</td>
      <td class="mono">${b.win_rate == null ? '—' : `${b.win_rate.toFixed(1)}%`}</td>
      <td class="mono ${b.net_points >= 0 ? 'positive' : 'warning'}">${b.net_points.toFixed(1)}</td>
    </tr>`).join('');

  return `
    <div class="backtest-metric-grid">
      ${renderMetricCard('Total trades', number(metrics.totalTrades))}
      ${renderMetricCard('Win rate', metrics.winRate == null ? '—' : `${metrics.winRate.toFixed(1)}%`)}
      ${renderMetricCard('Profit factor', metrics.profitFactor == null ? '—' : metrics.profitFactor.toFixed(2))}
      ${renderMetricCard('Net points', metrics.netPoints.toFixed(1), pnlClass)}
      ${renderMetricCard('Avg trade (pts)', metrics.averageTradePoints.toFixed(1))}
      ${renderMetricCard('Expectancy (pts)', metrics.expectancyPoints == null ? '—' : metrics.expectancyPoints.toFixed(1))}
      ${renderMetricCard('Largest win (pts)', metrics.largestWinPoints == null ? '—' : metrics.largestWinPoints.toFixed(1), 'positive')}
      ${renderMetricCard('Largest loss (pts)', metrics.largestLossPoints == null ? '—' : metrics.largestLossPoints.toFixed(1), 'warning')}
      ${renderMetricCard('Max consecutive losses', number(metrics.maxConsecutiveLosses))}
      ${renderMetricCard('Max drawdown (pts)', metrics.maxDrawdownPoints.toFixed(1))}
    </div>
    ${breakdown('CALL performance', metrics.callPerformance)}
    ${breakdown('PUT performance', metrics.putPerformance)}
    <div class="backtest-split-card">
      <h3>Time-of-day performance</h3>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Hour</th><th>Trades</th><th>Win rate</th><th>Net points</th></tr></thead>
          <tbody>${bucketRows(metrics.timeOfDayPerformance) || '<tr class="empty-row"><td colspan="4">No data</td></tr>'}</tbody>
        </table>
      </div>
    </div>
    <div class="backtest-split-card">
      <h3>Market-regime performance <small class="muted">(price above/below its own 50-period SMA — a simple proxy, not an authoritative regime classifier)</small></h3>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Regime</th><th>Trades</th><th>Win rate</th><th>Net points</th></tr></thead>
          <tbody>${bucketRows(metrics.marketRegimePerformance) || '<tr class="empty-row"><td colspan="4">No data</td></tr>'}</tbody>
        </table>
      </div>
    </div>
  `;
}

async function runBacktest() {
  const button = document.querySelector('#backtestRunButton');
  const resultEl = document.querySelector('#backtestResult');
  const interval = document.querySelector('#backtestInterval').value;
  const slippage = document.querySelector('#backtestSlippage').value || 0;
  button.disabled = true;
  resultEl.innerHTML = '<p class="data-empty">Running backtest against historical data&hellip;</p>';
  try {
    const query = `index_id=${backtestIndexId}&interval=${interval}&assumed_slippage_points=${slippage}&split=${backtestMode === 'split'}`;
    const response = await fetch(`/api/backtest/run?${query}`, { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Backtest failed');
    const payload = await response.json();
    document.querySelector('#backtestDisclaimer').textContent =
      `${payload.disclaimer} (${payload.candleCount} candles, period ${payload.period}.)`;
    const executionStatus = `<div class="broker-result">
      <strong>Backtest execution: ✓ Completed</strong>
      <p class="muted">This confirms the run finished without error — it says nothing about whether the strategy is profitable. Review the metrics below before drawing any conclusion.</p>
    </div>`;
    if (backtestMode === 'split') {
      resultEl.innerHTML = executionStatus + ['train', 'validation', 'out_of_sample'].map((name) => {
        const split = payload.splits[name];
        const title = name === 'out_of_sample' ? 'Out-of-sample' : name.charAt(0).toUpperCase() + name.slice(1);
        if (!split.metrics) return `<div class="backtest-split-card"><h3>${title}</h3><p class="data-empty">Not enough candles in this window.</p></div>`;
        return `<div class="backtest-split-card"><h3>${title} (${split.candleCount} candles)</h3>${renderBacktestMetrics(split.metrics)}</div>`;
      }).join('');
    } else {
      resultEl.innerHTML = executionStatus + renderBacktestMetrics(payload.metrics);
    }
  } catch {
    resultEl.innerHTML = '<div class="broker-result warning"><strong>Backtest execution: ✗ Failed</strong><p>Try a different index/timeframe or try again.</p></div>';
  } finally {
    button.disabled = false;
  }
}

function initBacktestPanel() {
  renderBacktestIndexTabs();
  document.querySelectorAll('#backtestModeToggle .segmented-option').forEach((button) => button.addEventListener('click', () => {
    backtestMode = button.dataset.mode;
    document.querySelectorAll('#backtestModeToggle .segmented-option').forEach((option) => option.classList.toggle('active', option === button));
  }));
  document.querySelector('#backtestRunButton').addEventListener('click', runBacktest);
}

initNav();
initTradePanel();
initBrokerPanel();
initBacktestPanel();
renderIndexTabs();
renderTimeframeTabs();
renderOptionMarketIndexTabs();
initChart();
loadGrids();
loadPositions();
loadOrders();
loadAutoTradingStatus();
loadMarket();
loadAccount();
loadCandles();
loadStrategySignal();
loadReconciliation();
loadLedger();
loadPnl();
loadDailySummary();
loadPeriodSummary();
loadStrategyPerformance();
loadAutoTradingSignals();
loadAutoTradingPerformance();
loadAutoTradingEquityCurve();
loadBrokerStatus();
loadBrokerHistory();
loadAlerts();
loadAlertsPill();
loadSystemHealth();
loadOptionMarket();
document.querySelector('#ledgerSourceFilter').addEventListener('change', loadLedger);
document.querySelector('#ledgerLiveFilter').addEventListener('change', loadLedger);
// UI heartbeat only: cheap, local, no Groww calls (loadAutoTradingStatus
// reads the paper-only risk manager's in-memory state) plus a pure re-render
// of the "Live · Xs ago" label from the timestamp brokerSyncTimer maintains
// below - this never itself polls the broker.
autoSyncTimer = window.setInterval(() => {
  loadAutoTradingStatus();
  renderSyncHeartbeat();
}, 1000);
// Actual Groww polling (positions/orders/grids all call real broker
// endpoints - see live_grid.py) - paced at 3s, inside the 2-5s range that
// keeps this well clear of Groww's rate limits while still feeling live.
brokerSyncTimer = window.setInterval(() => {
  if (isRefreshing) return;
  loadGrids();
  loadPositions();
  loadOrders();
}, 3000);
marketSyncTimer = window.setInterval(() => {
  loadMarket();
  loadCandles();
  loadStrategySignal();
  loadReconciliation();
  loadPnl();
  loadLedger();
  loadDailySummary();
  loadPeriodSummary();
  loadStrategyPerformance();
  loadManualTrades();
  loadAutoTradingSignals();
  loadAutoTradingPerformance();
  loadAutoTradingEquityCurve();
  loadBrokerStatus();
  loadBrokerHistory();
  loadAlerts();
  loadAlertsPill();
  loadSystemHealth();
  loadOptionMarket();
}, MARKET_SYNC_INTERVAL_MS);
