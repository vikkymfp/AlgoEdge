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
  // Means "last successful broker API request" (account/positions/orders
  // polling) - deliberately not "Live", which would suggest a live
  // market-data stream even when Market Data is unavailable.
  pill.title = 'Last successful broker API request (account, positions, orders). Not a live market-data stream.';
  if (lastBrokerSyncAt == null) {
    pill.innerHTML = '<i></i>Broker API · checking&hellip;';
    return;
  }
  const seconds = Math.max(0, Math.round((Date.now() - lastBrokerSyncAt) / 1000));
  pill.innerHTML = `<i></i>Last API success · ${seconds === 0 ? 'just now' : `${seconds}s ago`}`;
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

function kvGridHtml(entries) {
  return `<dl class="kv-grid">${entries.map(([key, item]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(formatDataValue(item))}</dd></div>`).join('')}</dl>`;
}

function keyValuesHtml(value) {
  const entries = Object.entries(value || {});
  return entries.length
    ? `<div class="api-data">${kvGridHtml(entries)}</div>`
    : '<div class="api-data"><p class="data-empty">No data returned.</p></div>';
}

// ---- Funds & Margin: compact summary + the unchanged raw response ----
// Field names are Groww's own get_available_margin_details() keys. A field
// Groww doesn't return is shown as "—", never guessed.
const MARGIN_SUMMARY_FIELDS = [
  ['clear_cash', 'Available Cash'],
  ['net_margin_used', 'Net Margin Used'],
  ['collateral_available', 'Collateral Available'],
  ['brokerage_and_charges', 'Brokerage & Charges'],
];
const MARGIN_SEGMENTS = [
  ['fno_margin_details', 'F&O Margin'],
  ['equity_margin_details', 'Equity Margin'],
  ['commodity_margin_details', 'Commodity Margin'],
];

function fieldCaseInsensitive(object, key) {
  if (!object || typeof object !== 'object') return undefined;
  const match = Object.keys(object).find((candidate) => candidate.toLowerCase() === key);
  return match === undefined ? undefined : object[match];
}

function formatMarginValue(value) {
  const numeric = typeof value === 'number' ? value : typeof value === 'string' && value.trim() !== '' ? Number(value) : NaN;
  if (Number.isFinite(numeric)) {
    const formatted = `₹${Math.abs(numeric).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    return numeric < 0 ? `-${formatted}` : formatted;
  }
  return formatDataValue(value);
}

function humanizeFieldName(key) {
  const words = String(key).replace(/_/g, ' ').toLowerCase()
    .replace(/\bfno\b/g, 'F&O').replace(/\b(cnc|mis|mtf|nrml)\b/g, (acronym) => acronym.toUpperCase());
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function marginSummaryHtml(margin, rawView, fetchedAt) {
  const metrics = MARGIN_SUMMARY_FIELDS.map(([key, label]) => `
    <div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(formatMarginValue(fieldCaseInsensitive(margin, key)))}</dd></div>`).join('');
  const segments = MARGIN_SEGMENTS.map(([key, label]) => {
    const details = fieldCaseInsensitive(margin, key);
    const entries = details && typeof details === 'object' ? Object.entries(details) : [];
    const body = entries.length
      ? `<dl class="margin-segment-list">${entries.map(([field, value]) => `<div><dt title="${escapeHtml(humanizeFieldName(field))}">${escapeHtml(humanizeFieldName(field))}</dt><dd>${escapeHtml(formatMarginValue(value))}</dd></div>`).join('')}</dl>`
      : '<p class="margin-segment-empty">Not returned by Groww</p>';
    return `<section class="margin-segment"><h4>${escapeHtml(label)}</h4>${body}</section>`;
  }).join('');
  const rawEntries = Object.entries(rawView || {});
  const snapshotNote = fetchedAt
    ? `Broker snapshot fetched at ${fetchedAt}. Values don't update live — reload the page to fetch a new snapshot.`
    : 'Broker snapshot. Values don\'t update live — reload the page to fetch a new snapshot.';
  return `<div class="api-data margin-summary">
    <p class="snapshot-note">${escapeHtml(snapshotNote)}</p>
    <dl class="margin-metrics">${metrics}</dl>
    <div class="margin-segments">${segments}</div>
    <details class="raw-response">
      <summary>Raw broker response <span>${number(rawEntries.length)} fields</span></summary>
      ${rawEntries.length ? kvGridHtml(rawEntries) : '<p class="data-empty">No data returned.</p>'}
    </details>
  </div>`;
}

// ---- Market Data: the actual backend error, and only the methods the
// backend reports for this capability (marketData.availableMethods) ----
const MARKET_DATA_METHOD_LABELS = { get_ltp: 'LTP', get_quote: 'Quote', get_ohlc: 'OHLC' };

function marketDataUnavailableHtml(marketData) {
  const affected = (marketData.availableMethods || []).map((method) => MARKET_DATA_METHOD_LABELS[method] || method);
  const reason = marketData.error || 'No market data request has succeeded yet.';
  return `<div class="api-data"><div class="capability-alert">
    <p class="capability-alert-title">Market data — Unavailable</p>
    <p class="capability-alert-error">${escapeHtml(reason)}</p>
    <dl class="capability-alert-meta">
      ${affected.length ? `<div><dt>Affected</dt><dd>${escapeHtml(affected.join(' · '))}</dd></div>` : ''}
      <div><dt>Impact</dt><dd>Live market quotes cannot currently be retrieved.</dd></div>
    </dl>
  </div></div>`;
}

function dataTableHtml(rows) {
  if (!rows || rows.length === 0) {
    return '<div class="api-data"><p class="data-empty">No data returned.</p></div>';
  }
  const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))];
  return `<div class="api-data"><table class="data-table"><thead><tr>${columns.map((column) => `<th>${escapeHtml(column)}</th>`).join('')}</tr></thead><tbody>${rows.map((row) => `<tr>${columns.map((column) => `<td title="${escapeHtml(formatDataValue(row[column]))}">${escapeHtml(formatDataValue(row[column]))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}

// Distinct from the table/kv-grid's own "No data returned" (which means the
// call succeeded and genuinely has nothing) - this means the call itself
// didn't work, and says which capability and (when known) why, rather than
// showing an empty table that looks identical to "no data" either way.
function capabilityUnavailableHtml(reason) {
  return `<div class="api-data"><p class="data-empty diagnostic-unavailable">${escapeHtml(reason)}</p></div>`;
}

function renderKeyValues(target, value) {
  target.innerHTML = keyValuesHtml(value);
}

function renderDataTable(target, rows) {
  target.innerHTML = dataTableHtml(rows);
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
    ['Session status', status.sessionStatus || '—'],
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

// Small, generic pieces shared by the top 4 diagnostic cards and the 5
// section tags on Broker Diagnostics - the backend (live_grid.py's
// account_snapshot()) is the sole source of truth for connected/available/
// error; this only ever formats what it already returned.
function renderDiagnosticCard(id, level, label, detail) {
  const item = document.querySelector(`#${id}`);
  if (!item) return;
  item.querySelector('strong').innerHTML = `<i class="status-dot"></i>${escapeHtml(label)}`;
  item.className = `system-status-item ${level}`;
  const detailEl = document.querySelector(`#${id}Detail`);
  if (detailEl) detailEl.textContent = detail || '';
}

function renderDiagnosticTag(id, level, label) {
  const tag = document.querySelector(`#${id}`);
  if (!tag) return;
  tag.textContent = label;
  tag.className = `status-tag ${level === 'ok' ? '' : level === 'warning' ? 'warning' : 'danger'}`;
}

// The two Diagnostics cards that depend on the broker's current connection
// state. Re-rendered from BOTH renderAccount() and renderBrokerStatus(), so
// the API Connection card always agrees with the header pill instead of
// waiting for the next account refresh when broker status lands second.
function renderDiagnosticConnectionCards() {
  if (!document.querySelector('#diagApiConnection')) return;
  const account = lastAccountPayload || {};
  const profile = account.profile || {};
  const marginStatus = account.marginStatus || {};
  const holdingsStatus = account.holdingsStatus || {};
  const positionsStatus = account.positionsStatus || {};
  const ordersStatus = account.ordersStatus || {};
  const brokerStatus = lastBrokerStatusPayload?.connectionStatus;
  if (brokerStatus === 'CONNECTED') {
    renderDiagnosticCard('diagApiConnection', 'ok', 'CONNECTED', 'Groww session is active');
  } else if (brokerStatus) {
    renderDiagnosticCard('diagApiConnection', 'warning', 'UNAVAILABLE',
      brokerStatus === 'TOKEN_EXPIRED'
        ? (lastBrokerStatusPayload?.autoReauthAvailable
          ? 'Groww session expired — a new one is generated automatically from your API key & secret'
          : 'Groww session expired — add an API key & secret on the Broker page')
        : `Session status: ${brokerStatus}`);
  } else {
    renderDiagnosticCard('diagApiConnection', 'warning', 'UNAVAILABLE', 'Checking broker connection…');
  }

  const accountChecks = [
    ['Profile', profile.connected, profile.error],
    ['Margin', marginStatus.available, marginStatus.error],
    ['Holdings', holdingsStatus.available, holdingsStatus.error],
    ['Positions', positionsStatus.available, positionsStatus.error],
    ['Orders', ordersStatus.available, ordersStatus.error],
  ];
  const failedChecks = accountChecks.filter(([, available]) => !available);
  if (brokerStatus !== 'CONNECTED') {
    renderDiagnosticCard('diagAccountData', 'warning', 'UNAVAILABLE', 'Requires an active Groww connection');
  } else if (failedChecks.length === 0) {
    renderDiagnosticCard('diagAccountData', 'ok', 'CONNECTED', 'Profile, margin, holdings, positions and orders all responded');
  } else {
    const firstError = failedChecks.find(([, , error]) => error)?.[2];
    renderDiagnosticCard('diagAccountData', 'danger', 'ERROR',
      `${failedChecks.map(([label]) => label).join(', ')} unavailable${firstError ? ` — ${firstError}` : ''}`);
  }
}

function renderAccount(account, fetchSucceeded = true) {
  lastAccountPayload = account;
  renderPositionsBrokerCard();

  // /api/account is fetched once per page load (loadAccount) - this is a
  // point-in-time broker snapshot, not a live-updating feed, so it is
  // labelled with when it was fetched rather than as "live".
  const fetchedAt = fetchSucceeded ? formatClockTime(new Date().toISOString()) : null;
  const sourceLabel = account.source === 'LIVE BROKER DATA' ? 'BROKER SNAPSHOT' : (account.source || 'Account data unavailable');
  document.querySelector('#accountSource').textContent =
    `${sourceLabel}${fetchedAt ? ` · Fetched: ${fetchedAt}` : ''}`;

  const profile = account.profile || {};
  const margin = account.margin || {};
  const marginStatus = account.marginStatus || {};
  const holdingsStatus = account.holdingsStatus || {};
  const positionsStatus = account.positionsStatus || {};
  const ordersStatus = account.ordersStatus || {};
  const instrumentMaster = account.instrumentMaster || {};
  const marketData = account.marketData || {};
  const holdings = account.holdings || [];
  const positions = account.positions || [];
  const orders = account.orders || [];

  // ---- Top 4 status cards ----
  renderDiagnosticConnectionCards();

  // Endpoint-level: a market data 403 marks only this card unavailable and
  // never changes the broker connection card above.
  if (marketData.status === 'AVAILABLE') {
    renderDiagnosticCard('diagMarketData', 'ok', 'CONNECTED', 'Live quotes available');
  } else if (marketData.status === 'PERMISSION_DENIED_OR_UNAVAILABLE') {
    renderDiagnosticCard('diagMarketData', 'warning', 'UNAVAILABLE',
      marketData.error || 'No market data request has succeeded yet');
  } else {
    renderDiagnosticCard('diagMarketData', 'warning', 'UNAVAILABLE', 'Status unknown');
  }

  if (instrumentMaster.available) {
    renderDiagnosticCard('diagInstrumentMaster', 'ok', 'CONNECTED', `${number(instrumentMaster.count)} instruments loaded`);
  } else {
    renderDiagnosticCard('diagInstrumentMaster', 'danger', 'ERROR', instrumentMaster.error || 'Could not load instrument master');
  }

  // ---- Connection & Permissions ----
  renderDiagnosticTag('diagProfileTag', profile.connected ? 'ok' : 'danger', profile.connected ? 'Available' : 'Unavailable');
  document.querySelector('#profileData').innerHTML = profile.connected
    ? keyValuesHtml(profile)
    : capabilityUnavailableHtml(profile.error ? `Profile unavailable: ${profile.error}` : 'Profile data is currently unavailable.');

  // ---- Funds & Margin ----
  renderDiagnosticTag('diagMarginTag', marginStatus.available ? 'ok' : 'danger', marginStatus.available ? 'Available' : 'Unavailable');
  if (marginStatus.available) {
    const fnoMargin = margin.fno_margin_details || {};
    const equityMargin = margin.equity_margin_details || {};
    // Summary first; the raw section keeps exactly the fields this panel
    // always showed, collapsed.
    document.querySelector('#marginData').innerHTML = marginSummaryHtml(margin, { ...margin, ...fnoMargin, ...equityMargin }, fetchedAt);
  } else {
    document.querySelector('#marginData').innerHTML =
      capabilityUnavailableHtml(marginStatus.error ? `Margin data unavailable: ${marginStatus.error}` : 'Margin data is currently unavailable.');
  }

  // ---- Holdings & Positions (combined section) ----
  document.querySelector('#holdingsPositionsCount').textContent = `${number(holdings.length)} / ${number(positions.length)}`;
  const bothAvailable = holdingsStatus.available && positionsStatus.available;
  const eitherAvailable = holdingsStatus.available || positionsStatus.available;
  renderDiagnosticTag('diagHoldingsPositionsTag', bothAvailable ? 'ok' : eitherAvailable ? 'warning' : 'danger', bothAvailable ? 'Available' : eitherAvailable ? 'Partial' : 'Unavailable');
  document.querySelector('#holdingsPositionsData').innerHTML = `
    <div class="api-subsection">
      <h4>Holdings</h4>
      ${holdingsStatus.available ? dataTableHtml(holdings) : capabilityUnavailableHtml(holdingsStatus.error ? `Holdings unavailable: ${holdingsStatus.error}` : 'Holdings are currently unavailable.')}
    </div>
    <div class="api-subsection">
      <h4>Positions</h4>
      ${positionsStatus.available ? dataTableHtml(positions) : capabilityUnavailableHtml(positionsStatus.error ? `Positions unavailable: ${positionsStatus.error}` : 'Positions are currently unavailable.')}
    </div>`;

  // ---- Orders & Trades ----
  document.querySelector('#ordersDataCount').textContent = number(orders.length);
  renderDiagnosticTag('diagOrdersTag', ordersStatus.available ? 'ok' : 'danger', ordersStatus.available ? 'Available' : 'Unavailable');
  document.querySelector('#ordersData').innerHTML = ordersStatus.available
    ? dataTableHtml(orders)
    : capabilityUnavailableHtml(ordersStatus.error ? `Orders unavailable: ${ordersStatus.error}` : 'Orders are currently unavailable.');

  // ---- Instrument Master & Market Data (combined section) ----
  // Market data is AVAILABLE only once a real market data call succeeded
  // (see marketData.status above); denied or not yet exercised counts as
  // unavailable, so this tag never claims both sub-capabilities work.
  const marketDataAvailable = marketData.status === 'AVAILABLE';
  renderDiagnosticTag(
    'diagInstrumentTag',
    instrumentMaster.available && marketDataAvailable ? 'ok' : instrumentMaster.available ? 'warning' : 'danger',
    instrumentMaster.available && marketDataAvailable ? 'Available' : instrumentMaster.available ? 'Partial' : 'Unavailable',
  );
  document.querySelector('#instrumentMarketData').innerHTML = `
    <div class="api-subsection">
      <h4>Instrument master</h4>
      ${instrumentMaster.available
        ? keyValuesHtml({ count: instrumentMaster.count, fields: instrumentMaster.fields })
        : capabilityUnavailableHtml(instrumentMaster.error ? `Instrument master unavailable: ${instrumentMaster.error}` : 'Instrument master is currently unavailable.')}
    </div>
    <div class="api-subsection">
      <h4>Market data</h4>
      ${marketDataAvailable
        ? '<div class="api-data"><p class="data-empty data-empty-left">Live quotes available.</p></div>'
        : marketData.status === 'PERMISSION_DENIED_OR_UNAVAILABLE'
          ? marketDataUnavailableHtml(marketData)
          : capabilityUnavailableHtml('Market data status unknown.')}
    </div>`;
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

function formatClockTime(iso) {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true });
}

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

  // Database gets its own detail line - a real backend health check result
  // (algoedge.db.check_connection(), a live SELECT 1), never inferred from
  // application state, and never a raw driver error (no credentials/
  // connection-string fragments reach the browser).
  const db = health.database || {};
  const dbCheckTime = formatClockTime(db.lastSuccessfulCheckAt);
  document.querySelector('#healthDatabaseDetail').textContent = db.status === 'CONNECTED'
    ? `${db.databaseName || 'AlgoEdge'} · SQL Server · Last check: ${dbCheckTime || '—'}`
    : `${db.error || 'Unable to connect to database'}${dbCheckTime ? ` · Last successful check: ${dbCheckTime}` : ''}`;

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
    renderAccount({ source: 'ACCOUNT DATA UNAVAILABLE', profile: { connected: false }, holdings: [], positions: [], orders: [], margin: {}, instrumentMaster: {}, marketData: {} }, false);
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

const TOKEN_STATUS_CLASS = {
  ACTIVE: 'positive', EXPIRING_SOON: 'warning', RENEWAL_DUE: 'warning', EXPIRED: 'warning', INVALID: 'warning', UNAVAILABLE: '',
};

// Groww doesn't publish a real expiry timestamp (see token_service.py's
// _estimate_token_expiry) - this is always an estimate based on the known
// ~6am IST daily reset, computed client-side purely from the already-
// fetched sessionExpiresAt so it stays live between the 20s broker-status
// refreshes without a new request.
function formatTimeRemaining(expiryAtIso) {
  if (!expiryAtIso) return null;
  const target = new Date(expiryAtIso).getTime();
  if (Number.isNaN(target)) return null;
  const diffMs = target - Date.now();
  if (diffMs <= 0) {
    const overdueMinutes = Math.round(-diffMs / 60000);
    // Only the ESTIMATED reset has passed - whether the session actually
    // expired is sessionStatus's call (EXPIRED only after a real 401).
    const ago = overdueMinutes < 60 ? `${overdueMinutes}m` : `${Math.round(overdueMinutes / 60)}h`;
    return `Estimated reset passed ${ago} ago`;
  }
  const totalMinutes = Math.round(diffMs / 60000);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
}

const BROKER_AUTH_MODE_LABEL = {
  API_KEY_SECRET: 'Generated from API key & secret — renewed automatically',
  MANUAL_TOKEN: 'Pasted session token — not renewed automatically',
  NONE: 'Not configured — add an API key & secret',
};

function renderBrokerStatus(status) {
  lastBrokerStatusPayload = status;
  renderPositionsBrokerCard();
  document.querySelector('#brokerApiKeyMasked').textContent = status.apiKeyMasked || 'Not configured';
  document.querySelector('#brokerApiSecretMasked').textContent = status.apiSecretMasked || 'Not configured';
  // The access token is session state generated from the key/secret, so
  // only where the current session comes from is shown - never the token.
  document.querySelector('#brokerAuthMode').textContent = BROKER_AUTH_MODE_LABEL[status.authMode] || '—';
  document.querySelector('#brokerReauthButton').hidden = !status.autoReauthAvailable;

  const connectionTag = document.querySelector('#brokerConnectionTag');
  connectionTag.textContent = status.connectionStatus;
  connectionTag.className = `status-tag ${brokerConnectionTagClass(status.connectionStatus)}`;

  const tokenStatusEl = document.querySelector('#brokerTokenStatus');
  tokenStatusEl.textContent = (status.sessionStatus || '—').replace(/_/g, ' ');
  tokenStatusEl.className = TOKEN_STATUS_CLASS[status.sessionStatus] || '';

  document.querySelector('#brokerTokenCreatedAt').textContent = formatTimestamp(status.sessionCreatedAt);
  const renewalNote = status.autoReauthAvailable
    ? '; a new session is generated automatically from your API key & secret'
    : '; a pasted session token is not renewed automatically';
  document.querySelector('#brokerTokenExpiryAt').textContent = status.sessionExpiresAt
    ? `${formatTimestamp(status.sessionExpiresAt)}${status.sessionExpiryIsEstimated ? ` (estimated — Groww sessions reset daily ~6:00 AM IST${renewalNote})` : ''}`
    : 'Not published by Groww for this auth method';

  // No usable token right now - a countdown would imply one exists.
  const remaining = status.sessionStatus === 'UNAVAILABLE' ? null : formatTimeRemaining(status.sessionExpiresAt);
  const remainingEl = document.querySelector('#brokerTokenTimeRemaining');
  remainingEl.textContent = remaining || '—';
  remainingEl.className = remaining && remaining.startsWith('Estimated reset passed') ? 'warning' : '';

  document.querySelector('#brokerLastValidatedAt').textContent = formatTimestamp(status.lastValidatedAt);
  document.querySelector('#brokerLastSuccessAt').textContent = formatTimestamp(status.lastSuccessfulRequestAt);
  document.querySelector('#brokerLastError').textContent = status.lastError || 'None';
  // Endpoint-level denials (e.g. market data 403) - listed here, never
  // reflected in the connection status/pill above.
  const unavailable = Object.entries(status.capabilities || {})
    .filter(([, capability]) => capability.status === 'UNAVAILABLE')
    .map(([name, capability]) => `${name.replace(/_/g, ' ')}${capability.error ? ` (${capability.error})` : ''}`);
  document.querySelector('#brokerUnavailableCapabilities').textContent = unavailable.join('; ') || 'None';
  document.querySelector('#brokerPersisted').textContent = status.credentialsPersisted ? 'Yes (encrypted)' : 'No (in-memory only this session)';

  // This pill is the header's global "Groww Connected" indicator, driven
  // purely by connectionStatus - a value that now only ever says CONNECTED
  // when the last real API request actually succeeded (token_service.py's
  // _TrackedClient marks it down the instant any real call fails,
  // anywhere in the app - not just on an explicit Test Connection click).
  const pill = document.querySelector('#brokerStatusPill');
  pill.classList.remove('warning', 'danger');
  if (status.connectionStatus === 'CONNECTED') {
    pill.innerHTML = '<i></i>Groww Connected';
  } else if (status.connectionStatus === 'TOKEN_EXPIRED') {
    pill.classList.add('warning');
    pill.innerHTML = '<i></i>Session Expired';
  } else if (status.connectionStatus === 'TOKEN_INVALID') {
    pill.classList.add('danger');
    pill.innerHTML = '<i></i>Authentication Failed';
  } else if (status.connectionStatus === 'MISSING') {
    pill.classList.add('warning');
    pill.innerHTML = '<i></i>Groww Not Configured';
  } else {
    pill.classList.add('danger');
    pill.innerHTML = '<i></i>Groww Disconnected';
  }

  systemStatusState.broker.connected = status.connectionStatus === 'CONNECTED';
  renderBrokerActionResult();
  if (lastAccountPayload) renderDiagnosticConnectionCards();
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
    body.innerHTML = '<tr class="empty-row"><td colspan="5">No credential/session events recorded yet.</td></tr>';
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
    document.querySelector('#brokerHistoryBody').innerHTML = '<tr class="empty-row"><td colspan="5">Could not load credential/session history.</td></tr>';
  }
}

// The result of the last API Management action (update token/credentials,
// test connection) - kept separate from the current connection state and
// re-rendered against it on every status refresh, so a success message can
// never keep claiming "Connected" once the broker reports otherwise.
// Shape: { kind: 'update' | 'test' | 'error', label, message, persisted, lostSinceAction }
let brokerActionResult = null;

function setBrokerActionResult(result) {
  brokerActionResult = result ? { ...result, lostSinceAction: false } : null;
  renderBrokerActionResult();
}

function brokerActionResultView(result, connectionStatus) {
  if (!result) return { text: '', tone: '' };
  if (result.kind === 'error') return { text: result.message, tone: 'warning' };

  const connected = connectionStatus === 'CONNECTED';
  // Any non-CONNECTED status seen after the action (CONNECTION_LOST,
  // TOKEN_EXPIRED, TOKEN_INVALID, an API error) permanently retires the
  // green "Connected" confirmation for that action - the action's own
  // validation is no longer the current truth.
  if (!connected) result.lostSinceAction = true;

  if (result.kind === 'test') {
    if (connected && !result.lostSinceAction) return { text: `Connected — ${result.message}`, tone: 'positive' };
    return connected
      ? { text: '', tone: '' }
      : { text: 'Broker connection is currently unavailable — see Current API status below.', tone: 'warning' };
  }

  // kind === 'update'
  if (connected && !result.lostSinceAction) {
    return {
      text: result.persisted
        ? `${result.label} validated and saved. Connected.`
        : `${result.label} validated but not saved (kept until the app restarts). Connected.`,
      tone: 'positive',
    };
  }
  if (!connected) {
    return {
      text: result.persisted
        ? 'Credentials saved, but broker connection is currently unavailable.'
        : 'Credentials applied until the app restarts (not saved), but broker connection is currently unavailable.',
      tone: 'warning',
    };
  }
  // Reconnected later, but not by this action's validation.
  return { text: result.persisted ? 'Credentials saved.' : 'Credentials applied until the app restarts (not saved).', tone: '' };
}

function renderBrokerActionResult() {
  const el = document.querySelector('#brokerActionResult');
  if (!el) return;
  const view = brokerActionResultView(brokerActionResult, lastBrokerStatusPayload?.connectionStatus);
  el.textContent = view.text;
  el.className = `broker-result ${view.tone}`.trim();
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
      input.value = '';
      tokenForm.hidden = true;
      renderBrokerStatus(payload);
      setBrokerActionResult({ kind: 'update', label: 'Session token', persisted: Boolean(payload.update?.persisted) });
      await Promise.all([loadBrokerStatus(), loadBrokerHistory()]);
    } catch (error) {
      setBrokerActionResult({ kind: 'error', message: error.message || 'Token update failed.' });
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
      keyInput.value = '';
      secretInput.value = '';
      credentialsForm.hidden = true;
      renderBrokerStatus(payload);
      setBrokerActionResult({ kind: 'update', label: 'API key/secret', persisted: Boolean(payload.update?.persisted) });
      await Promise.all([loadBrokerStatus(), loadBrokerHistory()]);
    } catch (error) {
      setBrokerActionResult({ kind: 'error', message: error.message || 'Credential update failed.' });
    } finally {
      submitButton.disabled = false;
    }
  });

  document.querySelector('#brokerReauthButton').addEventListener('click', async () => {
    const button = document.querySelector('#brokerReauthButton');
    button.disabled = true;
    try {
      const response = await fetch('/api/broker/reauthenticate', { method: 'POST' });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || 'Re-authentication failed');
      renderBrokerStatus(payload);
      setBrokerActionResult({ kind: 'update', label: 'New session', persisted: Boolean(payload.credentialsPersisted) });
      await loadBrokerHistory();
    } catch (error) {
      setBrokerActionResult({ kind: 'error', message: error.message || 'Re-authentication failed.' });
    } finally {
      button.disabled = false;
    }
  });

  document.querySelector('#brokerTestConnectionButton').addEventListener('click', async () => {
    const button = document.querySelector('#brokerTestConnectionButton');
    button.disabled = true;
    try {
      const response = await fetch('/api/broker/test-connection', { method: 'POST' });
      const payload = await response.json();
      setBrokerActionResult(payload.connected
        ? { kind: 'test', message: payload.message }
        : { kind: 'error', message: `Connection failed — ${payload.message}` });
      await Promise.all([loadBrokerStatus(), loadBrokerHistory()]);
    } catch {
      setBrokerActionResult({ kind: 'error', message: 'Test connection failed. Try again.' });
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
let lastBacktestParams = null;
let backtestChartMountQueue = [];

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

function backtestKpiCard(label, value, className) {
  return `<div class="backtest-kpi-card"><span>${escapeHtml(label)}</span><strong class="${className || ''}">${value}</strong></div>`;
}

// The 7 primary KPIs the spec calls out, kept visually prominent and
// separate from the secondary detail row below - "much easier to
// understand at a glance" means a short, curated list up top, not all 12
// computed metrics competing for attention at once.
function renderBacktestKpis(metrics) {
  const pnlClass = metrics.netPoints >= 0 ? 'positive' : 'warning';
  const expectancyClass = metrics.expectancyPoints == null ? '' : metrics.expectancyPoints >= 0 ? 'positive' : 'warning';
  return `<div class="backtest-kpi-grid">
    ${backtestKpiCard('Trades', number(metrics.totalTrades))}
    ${backtestKpiCard('Win rate', metrics.winRate == null ? '—' : `${metrics.winRate.toFixed(1)}%`)}
    ${backtestKpiCard('Profit factor', metrics.profitFactor == null ? '—' : metrics.profitFactor.toFixed(2))}
    ${backtestKpiCard('Net P&L (pts)', metrics.netPoints.toFixed(1), pnlClass)}
    ${backtestKpiCard('Expectancy (pts)', metrics.expectancyPoints == null ? '—' : metrics.expectancyPoints.toFixed(1), expectancyClass)}
    ${backtestKpiCard('Max drawdown (pts)', metrics.maxDrawdownPoints.toFixed(1), metrics.maxDrawdownPoints > 0 ? 'warning' : '')}
    ${backtestKpiCard('Max consec. losses', number(metrics.maxConsecutiveLosses), metrics.maxConsecutiveLosses > 0 ? 'warning' : '')}
  </div>`;
}

function renderBacktestSecondary(metrics) {
  return `<div class="backtest-metric-grid">
    ${renderMetricCard('Wins', number(metrics.wins), 'positive')}
    ${renderMetricCard('Losses', number(metrics.losses), metrics.losses > 0 ? 'warning' : '')}
    ${renderMetricCard('Avg trade (pts)', metrics.averageTradePoints.toFixed(1), metrics.averageTradePoints >= 0 ? 'positive' : 'warning')}
    ${renderMetricCard('Largest win (pts)', metrics.largestWinPoints == null ? '—' : metrics.largestWinPoints.toFixed(1), 'positive')}
    ${renderMetricCard('Largest loss (pts)', metrics.largestLossPoints == null ? '—' : metrics.largestLossPoints.toFixed(1), 'warning')}
  </div>`;
}

function renderDirectionCard(label, perf, cssClass) {
  return `<div class="backtest-direction-card ${cssClass}">
    <h3>${escapeHtml(label)}</h3>
    <div class="backtest-metric-grid">
      ${renderMetricCard('Trades', number(perf.trades))}
      ${renderMetricCard('Win rate', perf.win_rate == null ? '—' : `${perf.win_rate.toFixed(1)}%`)}
      ${renderMetricCard('Net points', perf.net_points.toFixed(1), perf.net_points >= 0 ? 'positive' : 'warning')}
    </div>
  </div>`;
}

function backtestBucketRows(buckets) {
  return buckets.map((b) => `
    <tr>
      <td class="mono">${escapeHtml(b.label)}</td>
      <td class="mono">${number(b.trades)}</td>
      <td class="mono">${b.win_rate == null ? '—' : `${b.win_rate.toFixed(1)}%`}</td>
      <td class="mono ${b.net_points >= 0 ? 'positive' : 'warning'}">${b.net_points.toFixed(1)}</td>
    </tr>`).join('');
}

// Cumulative-points equity curve, built from the exact trade ledger the
// backend returned - never a rupee figure (see backtest.py's module
// docstring: this account has no historical option-premium data, so a
// rupee P&L would have to be invented). Chart containers are recreated on
// every run via innerHTML, so the chart instance itself is too - mounted
// after the HTML lands in the DOM via backtestChartMountQueue below.
function mountEquityChart(containerId, trades) {
  const container = document.querySelector(`#${containerId}`);
  if (!container || !trades || trades.length === 0) return;
  const colors = chartColors();
  const chart = LightweightCharts.createChart(container, {
    layout: { background: { color: colors.bg }, textColor: colors.text, fontFamily: "'JetBrains Mono', monospace", fontSize: 10 },
    grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
    rightPriceScale: { borderColor: colors.border },
    timeScale: { borderColor: colors.border, timeVisible: true, secondsVisible: false },
    autoSize: true,
  });
  const series = chart.addLineSeries({ color: '#4f46e5', lineWidth: 2 });
  const sorted = [...trades].sort((a, b) => new Date(a.exitTime) - new Date(b.exitTime));
  const seenTimes = new Set();
  let cumulative = 0;
  const data = [];
  for (const trade of sorted) {
    cumulative += trade.points;
    let time = Math.floor(new Date(trade.exitTime).getTime() / 1000);
    if (!Number.isFinite(time)) continue;
    while (seenTimes.has(time)) time += 1; // lightweight-charts needs strictly ascending, unique times
    seenTimes.add(time);
    data.push({ time, value: cumulative });
  }
  series.setData(data);
  chart.timeScale().fitContent();
}

function renderEquitySection(containerId, trades) {
  if (!trades || trades.length === 0) {
    return '';
  }
  backtestChartMountQueue.push({ id: containerId, trades });
  return `<div class="backtest-equity-section">
    <div class="backtest-equity-heading">
      <h4>Equity curve</h4>
      <small>Cumulative points — not rupee P&amp;L</small>
    </div>
    <div class="backtest-equity-chart" id="${containerId}"></div>
  </div>`;
}

function renderSegmentResult(label, segmentKey, candleCount, metrics, trades) {
  const header = `<div class="backtest-segment-header"><h3>${escapeHtml(label)}</h3><span>${number(candleCount)} candles</span></div>`;
  if (!metrics) {
    return `<div class="backtest-segment segment-${segmentKey}">
      ${header}
      <p class="data-empty">Not enough candles in this window.</p>
    </div>`;
  }
  if (metrics.totalTrades === 0) {
    return `<div class="backtest-segment segment-${segmentKey}">
      ${header}
      <p class="data-empty">No trades were generated in this window.</p>
    </div>`;
  }
  return `<div class="backtest-segment segment-${segmentKey}">
    ${header}
    ${renderBacktestKpis(metrics)}
    ${renderBacktestSecondary(metrics)}
    ${renderEquitySection(`backtestEquityChart-${segmentKey}`, trades)}
    <div class="backtest-direction-grid">
      ${renderDirectionCard('CALL performance', metrics.callPerformance, 'call')}
      ${renderDirectionCard('PUT performance', metrics.putPerformance, 'put')}
    </div>
    <div class="backtest-split-card">
      <h3>Time-of-day performance</h3>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Hour</th><th>Trades</th><th>Win rate</th><th>Net points</th></tr></thead>
          <tbody>${backtestBucketRows(metrics.timeOfDayPerformance) || '<tr class="empty-row"><td colspan="4">No data</td></tr>'}</tbody>
        </table>
      </div>
    </div>
    <div class="backtest-split-card">
      <h3>Market-regime performance <small class="muted">(price above/below its own 50-period SMA — a simple proxy, not an authoritative regime classifier)</small></h3>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Regime</th><th>Trades</th><th>Win rate</th><th>Net points</th></tr></thead>
          <tbody>${backtestBucketRows(metrics.marketRegimePerformance) || '<tr class="empty-row"><td colspan="4">No data</td></tr>'}</tbody>
        </table>
      </div>
    </div>
  </div>`;
}

async function runBacktest() {
  const button = document.querySelector('#backtestRunButton');
  const resultEl = document.querySelector('#backtestResult');
  const interval = document.querySelector('#backtestInterval').value;
  const slippage = document.querySelector('#backtestSlippage').value || 0;
  button.disabled = true;
  document.querySelector('#backtestExportBar').hidden = true;
  document.querySelector('#backtestMetaRow').hidden = true;
  resultEl.innerHTML = '<p class="data-empty">Running backtest against historical data&hellip;</p>';
  backtestChartMountQueue = [];
  try {
    const query = `index_id=${backtestIndexId}&interval=${interval}&assumed_slippage_points=${slippage}&split=${backtestMode === 'split'}`;
    const response = await fetch(`/api/backtest/run?${query}`, { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error('Backtest failed');
    const payload = await response.json();
    lastBacktestParams = {
      index_id: backtestIndexId, interval, assumed_slippage_points: String(slippage), split: String(backtestMode === 'split'),
    };

    document.querySelector('#backtestDisclaimer').textContent = payload.disclaimer;
    document.querySelector('#backtestMetaSymbol').textContent = payload.indexName || payload.indexId;
    document.querySelector('#backtestMetaRange').textContent = `${payload.period} (${interval})`;
    document.querySelector('#backtestMetaCandles').textContent = number(payload.candleCount);
    document.querySelector('#backtestMetaSlippage').textContent = `${payload.assumedSlippagePoints} pts`;
    document.querySelector('#backtestMetaRow').hidden = false;
    document.querySelector('#backtestExportBar').hidden = false;

    const executionStatus = `<div class="backtest-execution-status ok">
      <strong>Backtest execution: ✓ Completed</strong>
      <p>This confirms the run finished without error — it says nothing about whether the strategy is profitable. Review the metrics below before drawing any conclusion.</p>
    </div>`;

    if (backtestMode === 'split') {
      const segments = [['train', 'Train'], ['validation', 'Validation'], ['out_of_sample', 'Out-of-sample']];
      resultEl.innerHTML = executionStatus + segments.map(([key, label]) => {
        const split = payload.splits[key];
        return renderSegmentResult(label, key, split.candleCount, split.metrics, split.trades);
      }).join('');
    } else {
      resultEl.innerHTML = executionStatus + renderSegmentResult('Full period', 'full', payload.candleCount, payload.metrics, payload.trades);
    }
    backtestChartMountQueue.forEach(({ id, trades }) => mountEquityChart(id, trades));
  } catch {
    resultEl.innerHTML = '<div class="backtest-execution-status fail"><strong>Backtest execution: ✗ Failed</strong><p>Try a different index/timeframe or try again.</p></div>';
    document.querySelector('#backtestExportBar').hidden = true;
    lastBacktestParams = null;
  } finally {
    button.disabled = false;
  }
}

function triggerDownload(url) {
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.rel = 'noopener';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

function downloadBacktestExport(kind) {
  if (!lastBacktestParams) return;
  const params = new URLSearchParams(lastBacktestParams);
  triggerDownload(`/api/backtest/export/${kind}?${params}`);
}

// Purely client-side: wipes displayed results/charts/tables and the export
// state back to the page's clean pre-run state. Never touches the control
// bar's own selections (index/timeframe/slippage/mode) and never issues a
// network request - the backend, strategy engine and export endpoints are
// completely uninvolved.
function clearBacktestResults() {
  document.querySelector('#backtestResult').innerHTML = '<p class="data-empty">Run a backtest to see results.</p>';
  document.querySelector('#backtestDisclaimer').textContent = '';
  document.querySelector('#backtestMetaRow').hidden = true;
  document.querySelector('#backtestExportBar').hidden = true;
  backtestChartMountQueue = [];
  lastBacktestParams = null;
}

function initBacktestPanel() {
  renderBacktestIndexTabs();
  document.querySelectorAll('#backtestModeToggle .segmented-option').forEach((button) => button.addEventListener('click', () => {
    backtestMode = button.dataset.mode;
    document.querySelectorAll('#backtestModeToggle .segmented-option').forEach((option) => option.classList.toggle('active', option === button));
  }));
  document.querySelector('#backtestRunButton').addEventListener('click', runBacktest);
  document.querySelector('#backtestClearButton').addEventListener('click', clearBacktestResults);
  document.querySelector('#backtestExportXlsx').addEventListener('click', () => downloadBacktestExport('xlsx'));
  document.querySelector('#backtestExportPdf').addEventListener('click', () => downloadBacktestExport('pdf'));
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
