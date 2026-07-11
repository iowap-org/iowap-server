// change-password.js — AI-Relay Change Password
// Keine Inline-Scripts — Logik hier ausgelagert

async function getCsrfToken() {
  const m = document.cookie.split(';').map(c => c.trim()).find(c => c.startsWith('relay_csrf='));
  return m ? decodeURIComponent(m.split('=')[1]) : '';
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const current = document.getElementById('current').value;
    const next = document.getElementById('new').value;
    const confirm = document.getElementById('confirm').value;
    const msg = document.getElementById('message');
    const btn = document.getElementById('submit');
    msg.className = 'error';
    msg.textContent = '';
    if (next !== confirm) { msg.textContent = 'Passwords do not match.'; return; }
    if (next.length < 12) { msg.textContent = 'Password must be at least 12 characters.'; return; }
    const csrf = await getCsrfToken();
    if (!csrf) { msg.textContent = 'Missing CSRF cookie. Please reload the page.'; return; }
    btn.disabled = true;
    try {
      const res = await fetch('/relay/v2/dashboard/api/me/password', {
        method: 'POST',
        headers: { 'X-CSRF-Token': csrf },
        body: new URLSearchParams({ current_password: current, new_password: next }),
      });
      if (res.ok) {
        const body = await res.json().catch(() => ({}));
        msg.className = 'success';
        msg.textContent = body.message || 'Password changed. Redirecting...';
        location.href = body.redirect_url || '/relay/v2/dashboard/';
      } else {
        const body = await res.json().catch(() => ({}));
        msg.textContent = body.detail || `Failed (${res.status})`;
      }
    } catch (err) {
      msg.textContent = 'Network or browser error: ' + err.message;
    } finally { btn.disabled = false; }
  });
  document.getElementById('logout').addEventListener('click', async () => {
    await fetch('/relay/v2/dashboard/logout', { method: 'POST', headers: { 'X-CSRF-Token': await getCsrfToken() } });
    location.href = '/relay/v2/dashboard/login';
  });
});