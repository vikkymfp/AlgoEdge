// AlgoEdge sign-in page. Holds nothing secret: the CAPTCHA answer lives only
// on the server, the session is an HttpOnly cookie, and nothing is written to
// localStorage/sessionStorage. Every message is set with textContent.
(() => {
  'use strict';

  const form = document.getElementById('loginForm');
  const identifier = document.getElementById('identifier');
  const password = document.getElementById('password');
  const toggle = document.getElementById('togglePassword');
  const captchaInput = document.getElementById('captcha');
  const captchaImage = document.getElementById('captchaImage');
  const refresh = document.getElementById('refreshCaptcha');
  const message = document.getElementById('formMessage');
  const button = document.getElementById('loginButton');
  const label = button.querySelector('.button-label');
  let captchaId = null;
  let busy = false;

  function showMessage(text, kind = 'error') {
    message.textContent = text;
    message.dataset.kind = kind;
    message.hidden = !text;
  }

  async function loadCaptcha() {
    captchaId = null;
    captchaInput.value = '';
    refresh.disabled = true;
    try {
      const response = await fetch('/api/auth/captcha', { headers: { Accept: 'application/json' }, cache: 'no-store' });
      if (response.status === 429) {
        showMessage('Too many requests. Please wait a moment and refresh the security code.');
        return;
      }
      if (!response.ok) throw new Error('captcha unavailable');
      const data = await response.json();
      captchaId = data.captchaId;
      captchaImage.src = data.image;
    } catch (error) {
      showMessage('Could not load the security code. Please refresh it.');
    } finally {
      refresh.disabled = false;
    }
  }

  function setBusy(value) {
    busy = value;
    button.disabled = value;
    button.setAttribute('aria-busy', String(value));
    label.textContent = value ? 'Signing in…' : 'Sign in';
    [identifier, password, captchaInput, refresh, toggle].forEach((element) => { element.disabled = value; });
  }

  function validate() {
    if (!identifier.value.trim()) return { field: identifier, text: 'Enter your email or mobile number.' };
    if (!password.value) return { field: password, text: 'Enter your password.' };
    if (!/^[A-Za-z0-9]{6}$/.test(captchaInput.value.trim())) {
      return { field: captchaInput, text: 'Enter the 6-character security code.' };
    }
    if (!captchaId) return { field: captchaInput, text: 'The security code is still loading. Please try again.' };
    return null;
  }

  // Eye = password hidden (click to show); eye-off = visible (click to hide).
  // Only the input's type changes - the value is never copied anywhere.
  const eye = toggle.querySelector('.icon-eye');
  const eyeOff = toggle.querySelector('.icon-eye-off');
  toggle.addEventListener('click', () => {
    const show = password.type === 'password';
    password.type = show ? 'text' : 'password';
    const text = show ? 'Hide password' : 'Show password';
    toggle.setAttribute('aria-label', text);
    toggle.title = text;
    toggle.setAttribute('aria-pressed', String(show));
    eye.hidden = show;
    eyeOff.hidden = !show;
    password.focus();
  });

  refresh.addEventListener('click', () => { showMessage(''); loadCaptcha(); });
  captchaInput.addEventListener('input', () => { captchaInput.value = captchaInput.value.toUpperCase(); });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy) return;
    const problem = validate();
    if (problem) {
      showMessage(problem.text);
      problem.field.focus();
      return;
    }
    showMessage('');
    setBusy(true);
    let signedIn = false;
    try {
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        cache: 'no-store',
        body: JSON.stringify({
          identifier: identifier.value,
          password: password.value,
          captchaId,
          captcha: captchaInput.value.trim().toUpperCase(),
        }),
      });
      if (response.ok) {
        signedIn = true;
        password.value = '';
        password.type = 'password';
        window.location.replace('/');
        return;
      }
      const data = await response.json().catch(() => ({}));
      if (response.status === 400 && data.code === 'INVALID_CAPTCHA') {
        showMessage('The security code was incorrect or expired. Please try the new one.');
        captchaInput.focus();
      } else if (response.status === 429) {
        showMessage('Too many attempts. Please wait a few minutes and try again.');
      } else if (response.status === 503) {
        showMessage('Sign-in is temporarily unavailable. Please try again later.');
      } else {
        showMessage('Invalid login credentials.');
        password.value = '';
        password.focus();
      }
    } catch (error) {
      showMessage('Could not reach the server. Please try again.');
    } finally {
      if (!signedIn) {
        setBusy(false);
        loadCaptcha(); // a CAPTCHA is single-use: always fetch a fresh one after an attempt
      }
    }
  });

  // Already signed in? Go straight to the dashboard.
  fetch('/api/auth/me', { headers: { Accept: 'application/json' }, cache: 'no-store' })
    .then((response) => { if (response.ok) window.location.replace('/'); })
    .catch(() => {});

  loadCaptcha();
  identifier.focus();
})();
