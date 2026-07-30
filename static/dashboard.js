// dashboard.js — Portal teaser page
// Fetches cluster overview for the status pill and version.

document.addEventListener('DOMContentLoaded', async () => {
  try {
    const r = await fetch('/relay/v2/cluster/overview');
    if (!r.ok) return;
    const data = await r.json();
    const s = data.summary;
    const pill = document.getElementById('heroPill');
    if (pill) {
      const allOk = s.online_nodes === s.total_nodes;
      pill.className = allOk ? 'pill' : 'pill warn';
      pill.innerHTML = `<span class="dot"></span> ${s.online_nodes}/${s.total_nodes} nodes online · ${s.total_tasks} tasks processed`;
    }
    const ver = document.getElementById('version');
    if (ver) ver.textContent = 'v2.0.0 · ' + s.total_nodes + ' nodes';
  } catch {
    // silent
  }
});
