// AlgoEdge shared pagination control, used by the dashboard and the
// administration page. Two modes:
//   - AlgoPager.create(...)      controls only; the caller fetches each page
//                                (for APIs that already paginate server-side).
//   - AlgoPager.paginateRows(..) client-side paging of the rows a table
//                                already rendered - no extra requests, rows
//                                are only hidden, never reordered or changed.
// DOM only (no innerHTML, no inline styles) so it works under a strict CSP.
(() => {
  'use strict';

  const PAGE_SIZES = [10, 25, 50, 100];
  const DEFAULT_PAGE_SIZE = 25;
  let uid = 0;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  // Page numbers to show: first, last, current ±1, with gaps as null.
  function pageWindow(page, pages) {
    const wanted = new Set([1, pages, page - 1, page, page + 1]);
    const list = [...wanted].filter((n) => n >= 1 && n <= pages).sort((a, b) => a - b);
    const out = [];
    list.forEach((n, i) => {
      if (i > 0 && n - list[i - 1] > 1) out.push(null);
      out.push(n);
    });
    return out;
  }

  function create({ label = 'Table', pageSize = DEFAULT_PAGE_SIZE, pageSizes = PAGE_SIZES, onChange } = {}) {
    uid += 1;
    const state = { page: 1, pageSize, total: 0 };
    const root = el('nav', 'ae-pager');
    root.setAttribute('aria-label', `${label} pagination`);

    const summary = el('p', 'ae-pager-summary');
    summary.setAttribute('aria-live', 'polite');

    const sizeWrap = el('label', 'ae-pager-size');
    const sizeId = `aePagerSize${uid}`;
    sizeWrap.htmlFor = sizeId;
    sizeWrap.append(el('span', 'ae-pager-size-label', 'Rows per page'));
    const select = el('select');
    select.id = sizeId;
    pageSizes.forEach((size) => select.append(new Option(String(size), String(size))));
    select.value = String(pageSize);
    sizeWrap.append(select);

    const controls = el('div', 'ae-pager-controls');
    const prev = el('button', 'ae-pager-button ae-pager-step', 'Previous');
    prev.type = 'button';
    prev.setAttribute('aria-label', 'Previous page');
    const pagesList = el('div', 'ae-pager-pages');
    const next = el('button', 'ae-pager-button ae-pager-step', 'Next');
    next.type = 'button';
    next.setAttribute('aria-label', 'Next page');
    controls.append(prev, pagesList, next);

    const meta = el('div', 'ae-pager-meta');
    meta.append(summary, sizeWrap);
    root.append(meta, controls);

    const pageCount = () => Math.max(1, Math.ceil(state.total / state.pageSize));

    function go(page, focusSelector) {
      const target = Math.min(Math.max(1, page), pageCount());
      if (target === state.page) return;
      state.page = target;
      render();
      if (onChange) onChange({ page: state.page, pageSize: state.pageSize });
      // Keep keyboard focus on the control that was used; if it is now
      // disabled (first/last page), move it to the current page number.
      if (!focusSelector) return;
      const again = root.querySelector(focusSelector);
      const fallback = root.querySelector('.ae-pager-page[aria-current="page"]');
      const focusTo = again && !again.disabled ? again : fallback;
      if (focusTo && focusTo.offsetParent !== null) focusTo.focus();
      else if (again && !again.disabled) again.focus();
    }

    function render() {
      const pages = pageCount();
      if (state.page > pages) state.page = pages;
      const first = state.total === 0 ? 0 : (state.page - 1) * state.pageSize + 1;
      const last = Math.min(state.total, state.page * state.pageSize);
      summary.textContent = state.total === 0
        ? 'No rows'
        : `Showing ${first}–${last} of ${state.total} · Page ${state.page} of ${pages}`;
      prev.disabled = state.page <= 1;
      next.disabled = state.page >= pages;
      pagesList.replaceChildren();
      pageWindow(state.page, pages).forEach((n) => {
        if (n === null) {
          const gap = el('span', 'ae-pager-gap', '…');
          gap.setAttribute('aria-hidden', 'true');
          pagesList.append(gap);
          return;
        }
        const button = el('button', 'ae-pager-button ae-pager-page', String(n));
        button.type = 'button';
        button.dataset.page = String(n);
        button.setAttribute('aria-label', `Page ${n}`);
        if (n === state.page) button.setAttribute('aria-current', 'page');
        button.addEventListener('click', () => go(n, `[data-page="${n}"]`));
        pagesList.append(button);
      });
    }

    prev.addEventListener('click', () => go(state.page - 1, '.ae-pager-step[aria-label="Previous page"]'));
    next.addEventListener('click', () => go(state.page + 1, '.ae-pager-step[aria-label="Next page"]'));
    select.addEventListener('change', () => {
      state.pageSize = Number(select.value);
      state.page = 1;
      render();
      if (onChange) onChange({ page: state.page, pageSize: state.pageSize });
    });

    render();
    return {
      element: root,
      get page() { return state.page; },
      get pageSize() { return state.pageSize; },
      // Called with the server's (or the table's) row count; page optional.
      update(total, page) {
        state.total = Math.max(0, Number(total) || 0);
        if (page) state.page = page;
        render();
      },
    };
  }

  const attached = new Map(); // tbody -> { pager, observer }

  // Client-side paging of an already-rendered tbody. Placeholder rows
  // (.empty-row) are never counted or hidden. Re-renders of the tbody (the
  // dashboard refreshes on timers) re-apply the current page automatically.
  function paginateRows(tbody, { label, pageSize = DEFAULT_PAGE_SIZE, mount } = {}) {
    if (attached.has(tbody)) return attached.get(tbody).pager;
    const table = tbody.closest('table');
    const anchor = mount || tbody.closest('.table-wrap, .api-data') || table;
    let applying = false;

    const pager = create({ label: label || tableLabel(table), pageSize, onChange: apply });
    const existing = anchor.nextElementSibling;
    if (existing && existing.classList.contains('ae-pager')) existing.remove();
    anchor.after(pager.element);

    function apply() {
      if (applying) return;
      applying = true;
      const rows = [...tbody.rows].filter((row) => !row.classList.contains('empty-row'));
      pager.update(rows.length);
      const start = (pager.page - 1) * pager.pageSize;
      const end = start + pager.pageSize;
      rows.forEach((row, index) => { row.hidden = index < start || index >= end; });
      // Nothing to page through: keep the control out of the way.
      pager.element.hidden = rows.length <= PAGE_SIZES[0];
      applying = false;
    }

    const observer = new MutationObserver(apply);
    observer.observe(tbody, { childList: true });
    attached.set(tbody, { pager, observer });
    apply();
    return pager;
  }

  // The closest heading above the table (inside its nearest ancestor that
  // has one), ignoring count badges nested in the heading.
  function tableLabel(table) {
    const own = table && (table.getAttribute('aria-label') || (table.caption && table.caption.textContent.trim()));
    if (own) return own.slice(0, 60);
    for (let node = table && table.parentElement; node && node !== document.body; node = node.parentElement) {
      const before = [...node.querySelectorAll('h1, h2, h3, h4')]
        .filter((h) => h.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING);
      const heading = before[before.length - 1];
      if (!heading) continue;
      const direct = [...heading.childNodes].filter((n) => n.nodeType === Node.TEXT_NODE).map((n) => n.textContent).join(' ');
      const text = (direct.trim() || heading.textContent).trim().replace(/\s+/g, ' ');
      if (text) return text.slice(0, 60);
    }
    return 'Table';
  }

  // Attach client-side paging to every data table under root, including
  // tables rendered later (e.g. backtest results). Tables without a <thead>
  // (such as the chart library's internal layout table) are left alone.
  function autoPaginate(root, { skip = () => false } = {}) {
    let queued = false;
    function scan() {
      queued = false;
      for (const [tbody, entry] of attached) {
        if (!tbody.isConnected) {
          entry.observer.disconnect();
          entry.pager.element.remove();
          attached.delete(tbody);
        }
      }
      root.querySelectorAll('table > tbody').forEach((tbody) => {
        const table = tbody.parentElement;
        if (attached.has(tbody) || !table.tHead || skip(tbody)) return;
        paginateRows(tbody);
      });
    }
    new MutationObserver(() => {
      if (queued) return;
      queued = true;
      window.requestAnimationFrame(scan);
    }).observe(root, { childList: true, subtree: true });
    scan();
  }

  window.AlgoPager = { create, paginateRows, autoPaginate, PAGE_SIZES, DEFAULT_PAGE_SIZE };
})();
