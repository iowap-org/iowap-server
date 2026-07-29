// dashboard.js — Community Portal (Phase 20, T-044).
//
// Renders the public cluster portal in the Community Mockup style:
// status bar (5 items from /cluster/overview), node profile cards
// (banner, avatar, name+id, status dot, caps, load-mini, meta),
// user profile cards (avatar, name, role, meta) and the activity feed
// (icon, highlight text, timestamp). All data comes from the public,
// unauthenticated /cluster/* endpoints.

// ===== helpers ==========================================================

function escHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function escAttr(s) {
  return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function fmt(d) {
  if (!d) return "-";
  const dt = new Date(d);
  return isNaN(dt) ? d : dt.toLocaleString();
}
function timeAgo(d) {
  if (!d) return "";
  const dt = new Date(d);
  if (isNaN(dt)) return "";
  const s = Math.floor((Date.now() - dt.getTime()) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return s + "s ago";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "m ago";
  const h = Math.floor(m / 60);
  if (h < 24) return h + "h ago";
  const d2 = Math.floor(h / 24);
  return d2 + "d ago";
}
function statusVar(name) {
  const map = { ok: "var(--ok)", warn: "var(--warn)", bad: "var(--bad)", info: "var(--info)", muted: "var(--muted)" };
  return map[name] || "var(--muted)";
}
function statusColor(entity) {
  if (entity && entity.status_color) return entity.status_color;
  const s = entity && entity.status;
  if (s === "online" || s === "active" || s === "idle" || s === "approved" || s === "completed") return "ok";
  if (s === "busy" || s === "running" || s === "claimed" || s === "maintenance") return "warn";
  if (s === "pending" || s === "accepted") return "info";
  if (s === "failed" || s === "timed_out" || s === "cancelled" || s === "offline" || s === "inactive") return "bad";
  return "muted";
}

// node avatar/banner/emoji heuristics (same as admin.js)
function nodeAvatarClass(nodeName) {
  const n = (nodeName || "").toLowerCase();
  if (n.includes("cyberfox") || n.includes("felix")) return "cyberfox";
  if (n.includes("mac") || n.includes("m4")) return "mac";
  if (n.includes("ssn")) return "ssn";
  if (n.includes("ct")) return "ct";
  return "default";
}
function nodeBannerClass(nodeName) {
  const n = (nodeName || "").toLowerCase();
  if (n.includes("mac") || n.includes("m4")) return "warn";
  if (n.includes("ssn")) return "ssn";
  if (n.includes("ct")) return "ct";
  return "";
}
function nodeAvatarEmoji(nodeName) {
  const n = (nodeName || "").toLowerCase();
  if (n.includes("cyberfox") || n.includes("felix")) return "🦊";
  if (n.includes("mac") || n.includes("m4")) return "💻";
  if (n.includes("ssn")) return "☁";
  if (n.includes("ct")) return "🌐";
  return (nodeName || "?").charAt(0).toUpperCase();
}

// ===== renderers ========================================================

function renderSummary(s) {
  const online = s.online_nodes ?? 0;
  const total = s.total_nodes ?? 0;
  const nodesClass = online > 0 ? "ok" : total > 0 ? "bad" : "";
  const stagesClass = (s.active_stages ?? 0) > 0 ? "warn" : "ok";
  const capsCount = s.capability_count ?? 0;
  const el = document.getElementById("summary");
  if (!el) return;
  el.innerHTML = `
    <div class="status-item"><div class="num ${nodesClass}">${online}/${total}</div><div class="lbl">nodes online</div></div>
    <div class="status-item"><div class="num">${s.total_tasks ?? 0}</div><div class="lbl">tasks processed</div></div>
    <div class="status-item"><div class="num ${stagesClass}">${s.active_stages ?? 0}</div><div class="lbl">active stages</div></div>
    <div class="status-item"><div class="num">${s.total_artifacts ?? 0}</div><div class="lbl">artifacts</div></div>
    <div class="status-item"><div class="num info">${capsCount}</div><div class="lbl">capabilities</div></div>
  `;
}

function renderHeroPill(s) {
  const pill = document.getElementById("heroPill");
  if (!pill) return;
  const online = s.online_nodes ?? 0;
  const total = s.total_nodes ?? 0;
  const cls = online > 0 ? "" : total > 0 ? "bad" : "warn";
  pill.className = "pill " + cls;
  const label = online > 0 ? "All systems operational" : total > 0 ? "Some nodes offline" : "No nodes registered";
  pill.innerHTML = `<span class="dot"></span> ${escHtml(label)}`;
}

function renderNodeCard(n) {
  const color = statusColor(n);
  const load = n.load;
  const loadPct = load == null ? null : Math.min(Math.max(load, 0), 100);
  const loadColor = loadPct == null ? "var(--muted)" : loadPct >= 85 ? "var(--bad)" : loadPct >= 60 ? "var(--warn)" : "var(--ok)";
  const loadText = load == null ? "?" : Math.round(load) + "%";
  const statusText = n.status == null ? "unknown" : String(n.status);
  const name = n.node_name || n.node_id;
  const caps = (n.capability_names || [])
    .slice(0, 6)
    .map((c) => `<span class="c">${escHtml(c)}</span>`)
    .join("");
  return `
    <a class="node-card" href="/relay/v2/dashboard/node/${escAttr(n.node_id)}">
      <div class="banner ${nodeBannerClass(n.node_name)}"></div>
      <div class="body">
        <div class="node-avatar ${nodeAvatarClass(n.node_name)}">${escHtml(nodeAvatarEmoji(n.node_name))}</div>
        <div class="node-name">${escHtml(name)}<span class="node-id">${escHtml(n.node_id)}</span></div>
        <div class="node-status"><span class="dot" style="background:${statusVar(color)}"></span>${escHtml(statusText)} · load ${loadText}</div>
        <div class="node-caps">${caps || '<span class="node-id">no caps</span>'}</div>
        <div class="load-mini"><div class="f" style="width:${loadPct == null ? 0 : loadPct}%;background:${loadColor}"></div></div>
        <div class="node-meta">
          <span>▲ ${n.task_count ?? 0} tasks</span>
          <span>queue: ${n.queue_depth ?? 0}</span>
          <span>${timeAgo(n.last_seen) || "—"}</span>
        </div>
      </div>
    </a>`;
}

function renderUserCard(u) {
  const initials = (u.username || u.user_id || "?").slice(0, 2).toUpperCase();
  const active = u.is_active !== false && u.status !== "inactive";
  const cls = active ? "" : "inactive";
  const statusText = active ? "active" : "inactive";
  const joined = u.created_at ? new Date(u.created_at).toLocaleDateString(undefined, { month: "short", year: "numeric" }) : "—";
  return `
    <a class="user-card ${cls}" href="/relay/v2/dashboard/user/${escAttr(u.user_id)}">
      <div class="top">
        <div class="u-avatar">${escHtml(initials)}</div>
        <div>
          <div class="u-name">${escHtml(u.username || u.user_id)}</div>
          <div class="u-role">${escHtml(u.role || "user")}</div>
        </div>
      </div>
      <div class="u-meta">
        <span>⏺ ${escHtml(statusText)}</span>
        <span>📅 joined ${escHtml(joined)}</span>
      </div>
    </a>`;
}

function eventIcon(type) {
  const t = (type || "").toLowerCase();
  if (t.includes("complet") || t.includes("success")) return "✅";
  if (t.includes("fail") || t.includes("error")) return "❌";
  if (t.includes("claim") || t.includes("assign")) return "🔄";
  if (t.includes("status") || t.includes("change")) return "⚡";
  if (t.includes("online") || t.includes("join") || t.includes("register")) return "🔗";
  if (t.includes("artifact") || t.includes("upload")) return "📦";
  if (t.includes("approve")) return "🛡";
  if (t.includes("delete")) return "🗑";
  if (t.includes("user")) return "👤";
  return "•";
}

function renderEvents(events) {
  const list = events || [];
  const el = document.getElementById("events");
  if (!el) return;
  el.innerHTML =
    list
      .map((e) => {
        const type = e.type || "event";
        const payload = e.payload || {};
        let body = `<span class="highlight">${escHtml(type)}</span>`;
        try {
          const ps = JSON.stringify(payload);
          if (ps && ps !== "{}") body += ` — <span class="ok">${escHtml(ps)}</span>`;
        } catch (_) {}
        return `
      <div class="activity-item">
        <div class="icon">${eventIcon(type)}</div>
        <div class="ev-body">${body}</div>
        <div class="ts">${timeAgo(e.timestamp) || fmt(e.timestamp)}</div>
      </div>`;
      })
      .join("") || '<div class="activity-item empty">No events yet.</div>';
}

// ===== data loading =====================================================

async function fetchJson(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function loadAll() {
  try {
    const [overview, usersReq, activityReq] = await Promise.all([
      fetchJson("/relay/v2/cluster/overview"),
      fetchJson("/relay/v2/cluster/users").catch(() => ({ users: [] })),
      fetchJson("/relay/v2/cluster/activity?limit=20").catch(() => ({ events: [] })),
    ]);

    const summary = overview.summary || {};
    renderHeroPill(summary);
    renderSummary(summary);

    const nodes = overview.nodes || [];
    document.getElementById("nodeCards").innerHTML =
      nodes.map(renderNodeCard).join("") || '<p class="empty">No nodes registered.</p>';

    const users = usersReq.users || [];
    document.getElementById("userCards").innerHTML =
      users.map(renderUserCard).join("") || '<p class="empty">No users registered.</p>';

    renderEvents(activityReq.events);

    const versionEl = document.getElementById("version");
    if (versionEl) {
      versionEl.textContent = `v2.0.0 · ${summary.total_nodes ?? 0} nodes · ${summary.total_tasks ?? 0} tasks`;
    }
  } catch (err) {
    const events = document.getElementById("events");
    if (events) events.innerHTML = `<div class="activity-item error">Failed to load cluster data: ${escHtml(err.message)}</div>`;
  }
}

document.addEventListener("DOMContentLoaded", loadAll);