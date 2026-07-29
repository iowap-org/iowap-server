// user-profile.js — public user profile page (Phase 20, T-044).
//
// Fetches /cluster/users/{id} and renders the user profile in the
// Community Mockup style: profile hero (avatar, name, role, status
// pill), stats grid (status, role, groups, joined), groups chip row
// and the recent activity feed (icon, action, resource, timestamp).

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
  if (s === "active" || s === "online" || s === "approved") return "ok";
  if (s === "busy" || s === "running") return "warn";
  if (s === "pending" || s === "accepted") return "info";
  if (s === "inactive" || s === "offline" || s === "failed") return "bad";
  return "muted";
}

function actionIcon(action) {
  const a = (action || "").toLowerCase();
  if (a.includes("create")) return "✨";
  if (a.includes("delete")) return "🗑";
  if (a.includes("update") || a.includes("change")) return "✏️";
  if (a.includes("approve")) return "🛡";
  if (a.includes("login") || a.includes("auth")) return "🔐";
  if (a.includes("node")) return "🖥";
  if (a.includes("task")) return "📋";
  return "•";
}

// ===== bootstrap ========================================================

const userId =
  new URLSearchParams(location.search).get("id") ||
  (location.pathname.split("/").pop() || "");

async function load() {
  if (!userId) {
    document.getElementById("userName").textContent = "No user id given";
    return;
  }
  try {
    const u = await fetch(`/relay/v2/cluster/users/${encodeURIComponent(userId)}`, {
      headers: { Accept: "application/json" },
    }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.statusText || r.status))));
    render(u);
  } catch (err) {
    document.getElementById("userName").textContent = "User not found";
    const pill = document.getElementById("statusPill");
    pill.className = "status-pill bad";
    pill.innerHTML = `<span class="status-dot"></span> ${escHtml(err.message || "not found")}`;
  }
}

// ===== render ===========================================================

function render(u) {
  const name = u.username || u.user_id;
  document.getElementById("userName").textContent = name;
  document.getElementById("userRole").textContent = u.role || "user";
  const avatar = document.getElementById("userAvatar");
  avatar.textContent = (name || "?").slice(0, 2).toUpperCase();

  const color = statusColor(u);
  const pill = document.getElementById("statusPill");
  pill.className = "status-pill " + (color === "ok" ? "" : color);
  pill.innerHTML = `<span class="status-dot"></span> ${escHtml(u.status || "unknown")}`;

  const groups = u.groups || [];
  document.getElementById("stats").innerHTML = `
    <div class="meta"><div class="lbl">Status</div><div class="val ${color === "ok" ? "ok" : color}">${escHtml(u.status || "-")}</div></div>
    <div class="meta"><div class="lbl">Role</div><div class="val sm">${escHtml(u.role || "-")}</div></div>
    <div class="meta"><div class="lbl">Groups</div><div class="val info">${groups.length}</div></div>
    <div class="meta"><div class="lbl">Joined</div><div class="val sm">${fmt(u.created_at)}</div></div>
  `;

  const groupsEl = document.getElementById("groups");
  groupsEl.innerHTML =
    groups
      .map((g) => `<span class="tag info">${escHtml(g)}</span>`)
      .join("") || '<span class="empty">No groups.</span>';

  const activity = u.activity || [];
  const el = document.getElementById("activity");
  el.innerHTML =
    activity
      .map((a) => {
        const res = [a.resource_type, a.resource_id].filter(Boolean).map(escHtml).join(" ");
        return `
      <div class="activity-item">
        <div class="icon">${actionIcon(a.action)}</div>
        <div class="ev-body"><span class="highlight">${escHtml(a.action)}</span>${res ? ` — <span class="mono">${res}</span>` : ""}</div>
        <div class="ts">${timeAgo(a.created_at) || fmt(a.created_at)}</div>
      </div>`;
      })
      .join("") || '<div class="activity-item empty">No activity recorded.</div>';
}

document.addEventListener("DOMContentLoaded", load);