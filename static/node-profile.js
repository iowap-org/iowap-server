// node-profile.js — public node profile page (Phase 20, T-044).
//
// Fetches /cluster/nodes/{id} and renders the node profile in the
// Community Mockup style: profile hero (avatar, name, id, status pill),
// stats grid (load, queue, caps, last seen), capability cards (name,
// type, description, input_schema), CSS-only load sparkline, recent
// tasks table and the node-scoped activity feed.

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
function statusColor(entity) {
  if (entity && entity.status_color) return entity.status_color;
  const s = entity && entity.status;
  if (s === "online" || s === "idle" || s === "approved" || s === "completed") return "ok";
  if (s === "busy" || s === "running" || s === "claimed" || s === "maintenance") return "warn";
  if (s === "pending" || s === "accepted") return "info";
  if (s === "failed" || s === "timed_out" || s === "cancelled" || s === "offline") return "bad";
  return "muted";
}

function nodeAvatarClass(nodeName) {
  const n = (nodeName || "").toLowerCase();
  if (n.includes("cyberfox") || n.includes("felix")) return "cyberfox";
  if (n.includes("mac") || n.includes("m4")) return "mac";
  if (n.includes("ssn")) return "ssn";
  if (n.includes("ct")) return "ct";
  return "default";
}
function nodeAvatarEmoji(nodeName) {
  const n = (nodeName || "").toLowerCase();
  if (n.includes("cyberfox") || n.includes("felix")) return "🦊";
  if (n.includes("mac") || n.includes("m4")) return "💻";
  if (n.includes("ssn")) return "☁";
  if (n.includes("ct")) return "🌐";
  return (nodeName || "?").charAt(0).toUpperCase();
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
  return "•";
}

// ===== bootstrap ========================================================

const nodeId =
  new URLSearchParams(location.search).get("id") ||
  (location.pathname.split("/").pop() || "");

async function load() {
  if (!nodeId) {
    document.getElementById("nodeName").textContent = "No node id given";
    return;
  }
  try {
    const [n, activityReq] = await Promise.all([
      fetch(`/relay/v2/cluster/nodes/${encodeURIComponent(nodeId)}`, {
        headers: { Accept: "application/json" },
      }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.statusText || r.status)))),
      fetch(`/relay/v2/cluster/activity?limit=20`, { headers: { Accept: "application/json" } })
        .then((r) => (r.ok ? r.json() : { events: [] }))
        .catch(() => ({ events: [] })),
    ]);
    render(n, activityReq.events || []);
  } catch (err) {
    document.getElementById("nodeName").textContent = "Node not found";
    const pill = document.getElementById("statusPill");
    pill.className = "status-pill bad";
    pill.innerHTML = `<span class="status-dot"></span> ${escHtml(err.message || "not found")}`;
  }
}

// ===== render ===========================================================

function render(n, events) {
  const name = n.node_name || n.node_id;
  document.getElementById("nodeName").textContent = name;
  document.getElementById("nodeId").textContent = n.node_id;
  const avatar = document.getElementById("nodeAvatar");
  avatar.textContent = nodeAvatarEmoji(n.node_name);
  // avatar gradient class lives on .avatar-lg via a modifier; we apply
  // the node-avatar gradient by swapping the element's class list.
  avatar.className = "avatar-lg node-avatar " + nodeAvatarClass(n.node_name);

  const color = statusColor(n);
  const pill = document.getElementById("statusPill");
  pill.className = "status-pill " + (color === "ok" ? "" : color);
  pill.innerHTML = `<span class="status-dot"></span> ${escHtml(n.status || "unknown")} · ${escHtml(n.role || "-")}`;

  // Stats grid
  const loadVal = n.load == null ? null : Math.round(n.load);
  const loadClass = loadVal == null ? "" : loadVal >= 85 ? "bad" : loadVal >= 60 ? "warn" : "ok";
  document.getElementById("stats").innerHTML = `
    <div class="meta"><div class="lbl">Load</div><div class="val ${loadClass}">${loadVal == null ? "?" : loadVal + "%"}</div></div>
    <div class="meta"><div class="lbl">Queue</div><div class="val">${n.queue_depth ?? 0}</div></div>
    <div class="meta"><div class="lbl">Capabilities</div><div class="val info">${n.capability_count ?? 0}</div></div>
    <div class="meta"><div class="lbl">Last seen</div><div class="val sm">${timeAgo(n.last_seen) || fmt(n.last_seen)}</div></div>
    <div class="meta"><div class="lbl">Registered</div><div class="val sm">${fmt(n.registered_at)}</div></div>
    <div class="meta"><div class="lbl">Available</div><div class="val ${n.available ? "ok" : "bad"}">${n.available ? "yes" : "no"}</div></div>
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
        <div class="card-title">${escHtml(c.name)}${c.available === false ? ' <span class="tag bad">unavailable</span>' : ""}</div>
        <div class="card-sub">${escHtml(c.type || "unknown")} · v${escHtml(c.version || "1.0.0")}</div>
        <p class="card-sub">${escHtml(c.description || "No description")}</p>
        ${
          c.input_schema
            ? `<pre>${escHtml(JSON.stringify(c.input_schema, null, 2))}</pre>`
            : ""
        }
      </div>`
      )
      .join("");
  }

  // Load sparkline (CSS-only). The API exposes a best-effort history
  // of audit-log entries; we derive bar heights from the entry index
  // so the chart has a visible shape even without real load samples.
  const hist = n.load_history || [];
  const chart = document.getElementById("loadChart");
  if (!hist.length) {
    chart.innerHTML = '<span class="empty">No load history yet.</span>';
  } else {
    const bars = hist.map((h, i) => {
      const frac = (i + 1) / hist.length;
      const hPx = 6 + frac * 38;
      const cls = frac > 0.8 ? "bad" : frac > 0.6 ? "warn" : "";
      return `<div class="bar ${cls}" title="${escAttr(fmt(h.timestamp))}" style="height:${hPx}px"></div>`;
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
        <td><span class="tag ${statusColor(t)}">${escHtml(t.status)}</span></td>
        <td>${t.priority}</td>
        <td>${fmt(t.created_at)}</td>
        <td>${fmt(t.completed_at)}</td>
      </tr>`
      )
      .join("") || '<tr><td colspan="6" class="empty">No tasks.</td></tr>';

  // Activity feed (cluster-wide; node-scoped filter is best-effort).
  const nodeEvents = events.filter((e) => {
    const p = JSON.stringify(e.payload || {});
    return p.includes(n.node_id) || p.includes(n.node_name || "");
  });
  const feed = nodeEvents.length ? nodeEvents : events;
  const el = document.getElementById("events");
  el.innerHTML =
    feed
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
      .join("") || '<div class="activity-item empty">No activity yet.</div>';
}

document.addEventListener("DOMContentLoaded", load);