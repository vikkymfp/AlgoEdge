// AlgoEdge best-effort sign-out when the last AlgoEdge tab is closed.
//
// The server session stays authoritative: this only *asks* the existing
// POST /api/auth/logout endpoint to end the session, with the same CSRF
// header every other state-changing request carries. If the request never
// happens (browser quit, crash, mobile tab discarded) the server's own idle /
// absolute expiry ends the session exactly as before.
//
// A page cannot tell a tab close from a reload or a navigation when it
// unloads, and a logout sent straight from the unloading page races the
// reload (the new page's CSS/JS then fail with 401). So the unloading page
// only tells a small service worker (session-sw.js, which outlives the page)
// "this tab is going away". The worker waits a short grace period and signs
// out only if no AlgoEdge dashboard/admin tab exists by then - a reload or a
// Dashboard <-> Admin navigation has already created its new tab by then, and
// any other open AlgoEdge tab keeps the shared session alive.
//
// Service workers need a secure context (HTTPS, or localhost). On a plain-
// HTTP deployment this is inactive and sessions end by the server's timeouts
// or the Sign out button only. The CSRF token is read from page memory at
// unload time and never written to any storage.
(() => {
  'use strict';

  let getCsrf = () => null;
  let worker = null;
  let installed = false;

  function install(csrfGetter) {
    if (installed) return;
    installed = true;
    getCsrf = csrfGetter;
    if (!window.isSecureContext || !('serviceWorker' in navigator)) return;

    navigator.serviceWorker.register('/session-sw.js', { scope: '/', updateViaCache: 'none' })
      .then(() => navigator.serviceWorker.ready)
      .then((registration) => { worker = registration.active; })
      .catch(() => { worker = null; });
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      worker = navigator.serviceWorker.controller || worker;
    });

    window.addEventListener('pagehide', () => {
      const target = worker || navigator.serviceWorker.controller;
      const csrf = getCsrf();
      if (!target || !csrf) return;
      try {
        target.postMessage({ type: 'tab-closing', csrf });
      } catch { /* best effort */ }
    });
  }

  window.AlgoSessionGuard = { install };
})();
