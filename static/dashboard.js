// dashboard.js — Community Portal welcome page (Phase 20, T-044)
// Minimal JS: just display the version from the API.

document.addEventListener('DOMContentLoaded', async () => {
  try {
    const r = await fetch('/relay/v2/cluster/overview');
    if (r.ok) {
      const data = await r.json();
      const el = document.getElementById('version');
      if (el) el.textContent = 'v2.0.0 · ' + data.summary.total_nodes + ' nodes';
    }
  } catch {
    // silent — version fallback is fine
  }
});
