// bootstrap.js — IOWAP Bootstrap (Create First Admin) + chained password set
// Keine Inline-Scripts — Logik hier ausgelagert

async function getCsrfToken() {
  const m = document.cookie.split(';').map(c => c.trim()).find(c => c.startsWith('relay_csrf='));
  return m ? decodeURIComponent(m.split('=')[1]) : '';
}

document.addEventListener('DOMContentLoaded', () => {
  const msg = document.getElementById('message');
  const msg2 = document.getElementById('message2');
  const btn = document.getElementById('submit');

  document.getElementById('form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const username = document.getElementById('username').value;
    const email = document.getElementById('email').value;
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
        // Chain: the response already carries the new admin's session
        // cookies. Wait for the operator's explicit Continue before the
        // password form; the temp password lives ONLY in the hidden
        // field there (no copy/logout/re-login).
        document.getElementById('continue').style.display = 'inline-block';
        document.getElementById('greeting').textContent = `Admin "${username}" created.`;
        document.getElementById('temp_password').value = body.temporary_password;
        msg.className = 'success';
        msg.textContent = 'Now set your final password.';
      } else {
        msg.className = 'error';
        msg.textContent = body.detail || `Failed (${res.status})`;
      }
    } finally { btn.disabled = false; }
  });

  document.getElementById('continue').addEventListener('click', () => {
    document.getElementById('form').style.display = 'none';
    document.getElementById('continue').style.display = 'none';
    document.getElementById('step2').style.display = 'block';
    document.getElementById('newpw').focus();
  });

  document.getElementById('form2').addEventListener('submit', async (e) => {
    e.preventDefault();
    const newpw = document.getElementById('newpw').value;
    const conf = document.getElementById('conf').value;
    msg2.textContent = '';
    if (newpw !== conf) {
      msg2.className = 'error';
      msg2.textContent = 'Passwords do not match.';
      return;
    }
    if (newpw.length < 12) {
      msg2.className = 'error';
      msg2.textContent = 'Minimum 12 characters.';
      return;
    }
    const btn2 = document.getElementById('setpw');
    btn2.disabled = true;
    try {
      const res = await fetch('/relay/v2/dashboard/api/me/password', {
        method: 'POST',
        headers: { 'X-CSRF-Token': await getCsrfToken() },
        body: new URLSearchParams({
          current_password: document.getElementById('temp_password').value,
          new_password: newpw,
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (res.ok) {
        // Final password set - the refreshed cookie is the day-to-day
        // admin session; go straight to the dashboard.
        location.href = '/relay/v2/dashboard/';
      } else {
        msg2.className = 'error';
        msg2.textContent = body.detail || `Failed (${res.status})`;
        btn2.disabled = false;
      }
    } catch (err) {
      msg2.className = 'error';
      msg2.textContent = 'Network error - try again.';
      btn2.disabled = false;
    }
  });
});