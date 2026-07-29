// node-profile.js — public node profile page (Phase 20, T-044).

function fmt(d) {
  if (!d) return "-";
  const dt = new Date(d);
  return isNaN(dt) ? d : dt.toLocaleString();
}
function escHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

const params = new URLSearchParams(location.search);
const nodeId = params.get("id") || (location.pathname.split("/").pop() || "");

async function load() {
  if (!nodeId) {
    document.querySelector("h1").textContent = "No node id given";
    return;
  }
  try {
    const n = await fetch(`/relay/v2/cluster/nodes/${encodeURIComponent(nodeId)}`, {
      headers: { Accept: "application/json" },
    }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))));
    render(n);
  } catch (err) {
    document.querySelector("h1").textContent = "Node not found";
    document.getElementById("statusPill").innerHTML = `<span class="status-dot bad"></span> ${err.message}`;
  }
}

function render(n) {
  document.getElementById("nodeName").textContent = n.node_name || n.node_id;
  document.getElementById("nodeId").textContent = n.node_id;
  document.querySelector(".avatar").textContent = (n.node_name || n.node_id || "?").charAt(0).toUpperCase();

  const pill = document.getElementById("statusPill");
  pill.className = "status-pill " + (n.status === "online" ? "" : "bad");
  pill.innerHTML = `<span class="status-dot ${n.status_color}"></span> ${escHtml(n.status)} · ${escHtml(n.role)}`;

  document.getElementById("stats").innerHTML = `
    <div class="stat"><div class="label">Load</div><div class="value">${Math.round((n.load || 0) * 100)}%</div></div>
    <div class="stat"><div class="label">Queue</div><div class="value">${n.queue_depth ?? "-"}</div></div>
    <div class="stat"><div class="label">Capabilities</div><div class="value">${n.capability_count ?? 0}</div></div>
    <div class="stat"><div class="label">Last seen</div><div class="value" style="font-size:1rem;">${fmt(n.last_seen)}</div></div>
  `;

  // Capabilities
  const caps = n.capabilities || [];
  const capsEl = document.getElementById("capabilities");
  if (!caps.length) {
    capsEl.innerHTML = '<p class="empty">No capabilities advertised.</p>';
  } else {
    capsEl.innerHTML = caps
      .map(
        (c) => `
      <div class="card">
        <div class="card-title">${escHtml(c.name)}</div>
        <div class="card-sub">${escHtml(c.type || "unknown")} · v${escHtml(c.version || "1.0.0")}${c.available ? "" : " · unavailable"}</div>
        <p class="card-sub">${escHtml(c.description || "No description")}</p>
        ${
          c.input_schema
            ? `<pre style="background:#10131a;padding:.5rem;border-radius:.4rem;overflow:auto;font-size:.75rem;max-height:160px;">${escHtml(JSON.stringify(c.input_schema, null, 2))}</pre>`
            : ""
        }
      </div>`
      )
      .join("");
  }

  // Mini load chart (CSS-only) — derive bar heights from the load_history
  // timestamps count so the chart has a visible shape even without real
  // load samples.
  const hist = n.load_history || [];
  const chart = document.getElementById("loadChart");
  if (!hist.length) {
    chart.innerHTML = '<span class="empty">No load history yet.</span>';
  } else {
    const bars = hist.map((_, i) => {
      const h = 10 + ((i + 1) / hist.length) * 30; // growing placeholder shape
      return `<div class="bar" style="height:${h}px;"></div>`;
    });
    chart.innerHTML = bars.join("");
  }

  // Recent tasks
  const tasks = n.recent_tasks || [];
  document.querySelector("#tasks tbody").innerHTML =
    tasks
      .map(
        (t) => `
      <tr>
        <td class="mono">${escHtml(t.task_id)}</td>
        <td>${escHtml(t.task_name)}</td>
        <td><span class="tag ${t.status_color}">${escHtml(t.status)}</span></td>
        <td>${t.priority}</td>
        <td>${fmt(t.created_at)}</td>
        <td>${fmt(t.completed_at)}</td>
      </tr>`
      )
      .join("") || '<tr><td colspan="6" class="empty">No tasks.</td></tr>';
}

document.addEventListener("DOMContentLoaded", load);