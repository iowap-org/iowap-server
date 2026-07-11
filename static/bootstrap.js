// bootstrap.js — AI-Relay Bootstrap (Create First Admin)
// Keine Inline-Scripts — Logik hier ausgelagert

async function getCsrfToken() {
  const m = document.cookie.split(';').map(c => c.trim()).find(c => c.startsWith('relay_csrf='));
  return m ? decodeURIComponent(m.split('=')[1]) : '';
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const username = document.getElementById('username').value;
    const email = document.getElementById('email').value;
    const msg = document.getElementById('message');
    const btn = document.getElementById('submit');
    msg.textContent = '';
    btn.disabled = true;
    try {
      const res = await fetch('/relay/v2/dashboard/api/bootstrap', {
        method: 'POST',
        headers: { 'X-CSRF-Token': await getCsrfToken() },
        body: new URLSearchParams({ username, email }),
      });
      const body = await res.json().catch(() => ({}));
      if (res.ok) {
        msg.className = 'success';
        msg.innerHTML = `Admin account created.\n\nTemporary password:\n${body.temporary_password}\n\nYou must now log in as ${username} and change the password. Master-seed login will be disabled once you do.`;
        setTimeout(() => location.href = '/relay/v2/dashboard/login', 30000);
      } else {
        msg.className = 'error';
        msg.textContent = body.detail || `Failed (${res.status})`;
      }
    } finally { btn.disabled = false; }
  });
});