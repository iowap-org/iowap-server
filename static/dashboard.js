// dashboard.js — Community Portal (Phase 20, T-044).
//
// Public, read-only cluster view. Fetches the public /cluster/* endpoints
// (no auth, no CSRF). Admin logic has moved to admin.js / admin.html.
// Auto-refreshes every 10s. Clicking a node/user card opens the public
// profile page; the Capabilities tab opens the SSN-hosted page (still
// public via the SSN dynamic route).

let refreshTimer = null;
let currentView = "overview";

function fmt(d) {
  if (!d) return "-";
  const dt = new Date(d);
  return isNaN(dt) ? d : dt.toLocaleString();
}

function escHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function escAttr(s) {
  return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Status colour mapping using the server-supplied status_color field.
function statusTag(entity) {
  const cls = (entity && entity.status_color) || "muted";
  const label = (entity && entity.status) || "-";
  return `<span class="tag ${cls}">${escHtml(label)}</span>`;
}

function statusDot(entity) {
  const cls = (entity && entity.status_color) || "muted";
  return `<span class="status-dot ${cls}"></span>`;
}

async function fetchJson(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status} ${text || res.statusText}`);
  }
  return res.json();
}

function showView(view) {
  currentView = view;
  const views = ["overview", "nodes", "users", "capabilities", "events"];
  views.forEach((v) => {
    const el = document.getElementById("view" + v.charAt(0).toUpperCase() + v.slice(1));
    if (el) el.classList.toggle("hidden", v !== view);
  });
  document.querySelectorAll(".nav-link").forEach((a) => {
    a.classList.toggle("active", a.dataset.view === view);
  });
  if (view === "capabilities") loadCapabilities();
  if (view === "events") loadEvents();
  if (view === "nodes") renderNodesTable();
  if (view === "users") renderUsersTable();
}

// --- Overview ------------------------------------------------------------

let lastOverview = null;

async function loadOverview() {
  try {
    const data = await fetchJson("/relay/v2/cluster/overview");
    lastOverview = data;
    renderSummary(data.summary, data.generated_at);
    renderNodeCards(data.nodes || []);
    renderUserCards();
    renderActivity(data.activity || []);
    renderNodesTable();
    renderUsersTable();
  } catch (err) {
    document.getElementById("statusPill").innerHTML =
      `<span class="status-dot bad"></span> error: ${escHtml(err.message)}`;
    console.error(err);
  }
}

function renderSummary(s, generatedAt) {
  const pill = document.getElementById("statusPill");
  const cls = s.online_nodes > 0 ? "ok" : "bad";
  pill.className = "status-pill " + (s.online_nodes > 0 ? "" : "bad");
  pill.innerHTML = `<span class="status-dot ${cls}"></span> ${s.online_nodes}/${s.total_nodes} nodes online · updated ${fmt(generatedAt)}`;

  const taskStatText =
    Object.entries(s.task_stats || {})
      .map(([k, v]) => k + ": " + v)
      .join(" · ") || "-";
  document.getElementById("summary").innerHTML = `
    <div class="stat"><div class="label">Nodes online</div><div class="value ${cls}">${s.online_nodes}/${s.total_nodes}</div><div class="sub">cluster nodes</div></div>
    <div class="stat"><div class="label">Tasks</div><div class="value">${s.total_tasks}</div><div class="sub">${taskStatText}</div></div>
    <div class="stat"><div class="label">Active stages</div><div class="value ${s.active_stages > 0 ? "warn" : "ok"}">${s.active_stages}</div><div class="sub">in progress</div></div>
    <div class="stat"><div class="label">Artifacts</div><div class="value">${s.total_artifacts}</div><div class="sub">stored</div></div>
    <div class="stat"><div class="label">Capabilities</div><div class="value">${s.capability_count}</div><div class="sub">advertised</div></div>
  `;
}

function renderNodeCards(nodes) {
  const container = document.getElementById("nodeCards");
  if (!nodes.length) {
    container.innerHTML = '<p class="empty">No nodes registered.</p>';
    return;
  }
  container.innerHTML = nodes
    .map((n) => {
      const caps = (n.capability_names || []).slice(0, 4);
      const more = (n.capability_names || []).length - caps.length;
      const loadPct = Math.max(0, Math.min(100, Math.round((n.load || 0) * 100)));
      return `
      <div class="card clickable" data-node-id="${escAttr(n.node_id)}">
        <div class="card-banner"></div>
        <div class="card-head">
          <div class="avatar">${escHtml((n.node_name || n.node_id || "?").charAt(0).toUpperCase())}</div>
          <div>
            <div class="card-title">${escHtml(n.node_name || n.node_id)}</div>
            <div class="card-sub mono">${escHtml(n.node_id)}</div>
          </div>
        </div>
        <div>${statusDot(n)} ${statusTag(n)} <span class="tag">${escHtml(n.role)}</span></div>
        <div class="cap-list">
          ${caps.map((c) => `<span class="tag">${escHtml(c)}</span>`).join("")}
          ${more > 0 ? `<span class="tag muted">+${more}</span>` : ""}
        </div>
        <div class="load-row"><span class="card-sub">load</span><div class="load-bar"><span style="width:${loadPct}%"></span></div><span class="card-sub">${loadPct}%</span></div>
        <div class="card-sub">queue ${n.queue_depth ?? "-"} · last seen ${fmt(n.last_seen)}</div>
      </div>`;
    })
    .join("");
}

async function renderUserCards() {
  try {
    const data = await fetchJson("/relay/v2/cluster/users");
    const users = data.users || [];
    const container = document.getElementById("userCards");
    if (!users.length) {
      container.innerHTML = '<p class="empty">No human users.</p>';
      return;
    }
    container.innerHTML = users
      .map(
        (u) => `
      <div class="card clickable" data-user-id="${escAttr(u.user_id)}">
        <div class="card-head">
          <div class="avatar">${escHtml((u.username || "?").charAt(0).toUpperCase())}</div>
          <div>
            <div class="card-title">${escHtml(u.username)}</div>
            <div class="card-sub">${escHtml(u.role)}</div>
          </div>
        </div>
        <div>${statusDot(u)} ${statusTag(u)}</div>
        <div class="card-sub">joined ${fmt(u.created_at)}</div>
      </div>`
      )
      .join("");
  } catch (err) {
    console.error("renderUserCards failed:", err);
  }
}

function renderActivity(events) {
  const feed = document.getElementById("activityFeed");
  if (!events.length) {
    feed.innerHTML = '<li class="empty">No events yet.</li>';
    return;
  }
  feed.innerHTML = events
    .map(
      (e) => `
      <li><span class="ev-type">${escHtml(e.type)}</span>
        <span class="ev-time">${fmt(e.timestamp)}</span>
        <div class="card-sub">${escHtml(JSON.stringify(e.payload || {}))}</div>
      </li>`
    )
    .join("");
}

// --- Nodes / Users tables (public) --------------------------------------

function renderNodesTable() {
  const nodes = (lastOverview && lastOverview.nodes) || [];
  const tbody = document.querySelector("#nodesTable tbody");
  if (!tbody) return;
  tbody.innerHTML =
    nodes
      .map(
        (n) => `
      <tr class="clickable" data-node-id="${escAttr(n.node_id)}">
        <td class="mono">${escHtml(n.node_id)}</td>
        <td>${escHtml(n.node_name || n.node_id)}</td>
        <td><span class="tag">${escHtml(n.role)}</span></td>
        <td>${statusTag(n)}</td>
        <td>${(n.capability_names || []).join(", ") || "-"}</td>
        <td>${n.load ?? "-"}</td>
        <td>${n.queue_depth ?? "-"}</td>
        <td>${fmt(n.last_seen)}</td>
      </tr>`
      )
      .join("") || '<tr><td colspan="8" class="empty">No nodes.</td></tr>';
}

async function renderUsersTable() {
  const tbody = document.querySelector("#usersTable tbody");
  if (!tbody) return;
  try {
    const data = await fetchJson("/relay/v2/cluster/users");
    const users = data.users || [];
    tbody.innerHTML =
      users
        .map(
          (u) => `
        <tr class="clickable" data-user-id="${escAttr(u.user_id)}">
          <td>${escHtml(u.username)}</td>
          <td><span class="tag">${escHtml(u.role)}</span></td>
          <td>${statusTag(u)}</td>
          <td>${fmt(u.created_at)}</td>
        </tr>`
        )
        .join("") || '<tr><td colspan="4" class="empty">No users.</td></tr>';
  } catch (err) {
    console.error("renderUsersTable failed:", err);
  }
}

async function loadEvents() {
  try {
    const data = await fetchJson("/relay/v2/cluster/activity?limit=50");
    renderActivity(data.events || []);
  } catch (err) {
    console.error("loadEvents failed:", err);
  }
}

// --- Capabilities (public list, SSN-hosted pages open in profile) --------

async function loadCapabilities() {
  try {
    const data = await fetchJson("/relay/v2/cluster/nodes");
    // Aggregate capability -> nodes from the public node list.
    const capMap = new Map();
    for (const n of data.nodes || []) {
      for (const c of n.capability_names || []) {
        if (!capMap.has(c)) capMap.set(c, { name: c, nodes: [] });
        capMap.get(c).nodes.push({ node_id: n.node_id, node_name: n.node_name });
      }
    }
    const caps = Array.from(capMap.values());
    const container = document.getElementById("capabilityCards");
    if (!caps.length) {
      container.innerHTML = '<p class="empty">No capabilities advertised.</p>';
      return;
    }
    container.innerHTML = caps
      .map(
        (c) => `
      <div class="card clickable" data-capability="${escAttr(c.name)}">
        <div class="card-title">${escHtml(c.name)}</div>
        <div class="card-sub">${c.nodes.length} node(s)</div>
      </div>`
      )
      .join("");
  } catch (err) {
    console.error("loadCapabilities failed:", err);
  }
}

// --- Navigation ----------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".nav-link").forEach((a) => {
    a.addEventListener("click", (e) => {
      const view = a.dataset.view;
      if (!view) return;
      e.preventDefault();
      showView(view);
    });
  });

  // Event delegation: open profile pages.
  document.addEventListener("click", (e) => {
    const nodeCard = e.target.closest("[data-node-id]");
    if (nodeCard && nodeCard.dataset.nodeId) {
      location.href = `/relay/v2/dashboard/node/${encodeURIComponent(nodeCard.dataset.nodeId)}`;
      return;
    }
    const userCard = e.target.closest("[data-user-id]");
    if (userCard && userCard.dataset.userId) {
      location.href = `/relay/v2/dashboard/user/${encodeURIComponent(userCard.dataset.userId)}`;
      return;
    }
    const capCard = e.target.closest("[data-capability]");
    if (capCard && capCard.dataset.capability) {
      // Capability pages are hosted by the SSN; the public portal just
      // shows the node list — clicking a capability has no public page
      // yet, so we keep it as a no-op visual cue.
      capCard.style.opacity = "0.6";
      return;
    }
  });

  loadOverview();
  refreshTimer = setInterval(loadOverview, 10000);
});