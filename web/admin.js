// AlgoEdge administration page (ADMIN only - the server refuses everyone
// else; hiding nothing here is relied on). All values are rendered with
// textContent, never innerHTML. The CSRF token is kept in memory only.
(() => {
  'use strict';

  const state = { page: 1, pageSize: 50, total: 0, csrf: null, me: null };
  const $ = (id) => document.getElementById(id);
  const message = $('adminMessage');

  function show(text, kind = 'error') {
    message.textContent = text;
    message.dataset.kind = kind;
    message.hidden = !text;
  }

  async function api(path, options = {}) {
    const method = (options.method || 'GET').toUpperCase();
    const headers = { Accept: 'application/json', ...(options.headers || {}) };
    if (method !== 'GET') {
      headers['Content-Type'] = 'application/json';
      headers['X-CSRF-Token'] = state.csrf;
    }
    const response = await fetch(path, { ...options, method, headers, cache: 'no-store' });
    if (response.status === 401) {
      window.location.replace('/login.html');
      throw new Error('signed out');
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Request failed.');
    return data;
  }

  function cell(text, className) {
    const td = document.createElement('td');
    td.textContent = text == null || text === '' ? '—' : String(text);
    if (className) td.className = className;
    return td;
  }

  function badge(ok, yes, no) {
    const td = document.createElement('td');
    const span = document.createElement('span');
    span.className = `badge ${ok ? 'ok' : 'fail'}`;
    span.textContent = ok ? yes : no;
    td.appendChild(span);
    return td;
  }

  const when = (iso) => (iso ? iso.replace('T', ' ').slice(0, 19) : null);

  async function loadActivity() {
    const params = new URLSearchParams({ page: String(state.page), page_size: String(state.pageSize) });
    for (const [key, id] of [['start', 'fStart'], ['end', 'fEnd'], ['success', 'fSuccess'],
      ['identifier_type', 'fType'], ['failure_reason', 'fReason'], ['user_id', 'fUser']]) {
      if ($(id).value) params.set(key, $(id).value);
    }
    const data = await api(`/api/admin/login-activity?${params}`);
    state.total = data.total;
    const body = $('activityBody');
    body.replaceChildren();
    for (const item of data.items) {
      const row = document.createElement('tr');
      row.append(cell(when(item.attemptAt), 'mono'), cell(item.user), cell(item.identifier), cell(item.identifierType),
        badge(item.success, 'Success', 'Failure'), cell(item.failureReason), cell(item.ipAddress, 'mono'),
        cell(item.userAgent, 'wrap'), cell(item.sessionId ? item.sessionId.slice(0, 12) : null, 'mono'));
      body.appendChild(row);
    }
    if (!data.items.length) {
      const row = document.createElement('tr');
      const td = cell('No login activity matches these filters.');
      td.colSpan = 9;
      row.appendChild(td);
      body.appendChild(row);
    }
    const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
    $('activityCount').textContent = `${state.total} attempt(s) · page ${state.page} of ${pages}`;
    $('prevPage').disabled = state.page <= 1;
    $('nextPage').disabled = state.page >= pages;
  }

  function actionButton(label, handler, className = 'ghost-button') {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = className;
    button.textContent = label;
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        await handler();
      } catch (error) {
        show(error.message);
      } finally {
        button.disabled = false;
      }
    });
    return button;
  }

  function passwordResetForm(user, container) {
    container.replaceChildren();
    const input = document.createElement('input');
    input.type = 'password';
    input.autocomplete = 'new-password';
    input.placeholder = 'New password (min 8)';
    input.setAttribute('aria-label', `New password for user ${user.id}`);
    const save = actionButton('Save', async () => {
      await api(`/api/admin/users/${user.id}/password`, { method: 'POST', body: JSON.stringify({ password: input.value }) });
      input.value = '';
      show('Password updated. The user has been signed out of any open sessions.', 'success');
      await loadUsers();
    }, 'primary-button');
    container.append(input, save, actionButton('Cancel', loadUsers));
    input.focus();
  }

  async function loadUsers() {
    const data = await api('/api/admin/users');
    const body = $('usersBody');
    const filter = $('fUser');
    const selected = filter.value;
    filter.replaceChildren(new Option('All users', ''));
    body.replaceChildren();
    for (const user of data.users) {
      filter.appendChild(new Option(user.email || user.mobileNo, String(user.id)));
      const row = document.createElement('tr');
      const actions = document.createElement('td');
      const wrap = document.createElement('div');
      wrap.className = 'row-actions';
      const self = state.me && state.me.id === user.id;
      if (!self) {
        wrap.appendChild(actionButton(user.isActive ? 'Deactivate' : 'Activate', async () => {
          await api(`/api/admin/users/${user.id}/active`, { method: 'POST', body: JSON.stringify({ active: !user.isActive }) });
          await loadUsers();
        }, user.isActive ? 'danger-button' : 'ghost-button'));
        const nextRole = user.role === 'ADMIN' ? 'USER' : 'ADMIN';
        wrap.appendChild(actionButton(`Make ${nextRole}`, async () => {
          await api(`/api/admin/users/${user.id}/role`, { method: 'POST', body: JSON.stringify({ role: nextRole }) });
          await loadUsers();
        }));
      }
      wrap.appendChild(actionButton('Reset password', async () => passwordResetForm(user, wrap)));
      actions.appendChild(wrap);
      row.append(cell(user.id, 'mono'), cell(user.email), cell(user.mobileNo), cell(user.role),
        badge(user.isActive, 'Active', 'Disabled'), cell(when(user.lastLoginAt), 'mono'),
        cell(when(user.lockedUntil), 'mono'), actions);
      body.appendChild(row);
    }
    filter.value = selected;
  }

  $('activityFilters').addEventListener('submit', (event) => {
    event.preventDefault();
    state.page = 1;
    loadActivity().catch((error) => show(error.message));
  });
  $('prevPage').addEventListener('click', () => { state.page -= 1; loadActivity().catch((e) => show(e.message)); });
  $('nextPage').addEventListener('click', () => { state.page += 1; loadActivity().catch((e) => show(e.message)); });

  $('createUserForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await api('/api/admin/users', {
        method: 'POST',
        body: JSON.stringify({ email: $('newEmail').value.trim(), mobileNo: $('newMobile').value.trim(),
          role: $('newRole').value, password: $('newPassword').value }),
      });
      event.target.reset();
      show('User added.', 'success');
      await loadUsers();
    } catch (error) {
      show(error.message);
    }
  });

  $('logoutButton').addEventListener('click', async () => {
    try {
      await api('/api/auth/logout', { method: 'POST', body: '{}' });
    } finally {
      window.location.replace('/login.html');
    }
  });

  (async () => {
    try {
      const me = await api('/api/auth/me');
      state.csrf = me.csrfToken;
      state.me = me.user;
      $('whoami').textContent = `${me.user.email || me.user.mobileNo} · ${me.user.role}`;
      if (me.user.role !== 'ADMIN') {
        window.location.replace('/');
        return;
      }
      await loadUsers();
      await loadActivity();
    } catch (error) {
      show(error.message);
    }
  })();
})();
