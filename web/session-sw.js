// AlgoEdge session service worker - close-tab sign-out only (see
// session-guard.js). It has NO fetch handler: it never sees, caches or
// changes any request the pages make, and it stores nothing.
'use strict';

// Long enough for a reload or a Dashboard <-> Admin navigation to create its
// new tab; short enough that a closed tab's session ends promptly.
const GRACE_MS = 8000;
const APP_PAGES = new Set(['/', '/index.html', '/admin.html']);

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

function isAppPage(client) {
  try {
    const url = new URL(client.url);
    return url.origin === self.location.origin && APP_PAGES.has(url.pathname);
  } catch {
    return false;
  }
}

self.addEventListener('message', (event) => {
  const data = event.data || {};
  if (data.type !== 'tab-closing' || typeof data.csrf !== 'string' || !data.csrf) return;
  const closingId = event.source && event.source.id;
  event.waitUntil((async () => {
    await new Promise((resolve) => setTimeout(resolve, GRACE_MS));
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    if (windows.some((client) => client.id !== closingId && isAppPage(client))) return;
    try {
      await fetch('/api/auth/logout', {
        method: 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { Accept: 'application/json', 'X-CSRF-Token': data.csrf },
      });
    } catch { /* best effort: server expiry still applies */ }
  })());
});
