// user-profile.js — public user profile page (Phase 20, T-044).

function fmt(d) {
  if (!d) return "-";
  const dt = new Date(d);
  return isNaN(dt) ? d : dt.toLocaleString();
}
function escHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

const params = new URLSearchParams(location.search);
const userId = params.get("id") || (location.pathname.split("/").pop() || "");

async function load() {
  if (!userId) {
    document.querySelector("h1").textContent = "No user id given";
    return;
  }
  try {
    const u = await fetch(`/relay/v2/cluster/users/${encodeURIComponent(userId)}`, {
      headers: { Accept: "application/json" },
    }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))));
    render(u);
  } catch (err) {
    document.querySelector("h1").textContent = "User not found";
    document.getElementById("statusPill").innerHTML = `<span class="status-dot bad"></span> ${err.message}`;
  }
}

function render(u) {
  document.getElementById("userName").textContent = u.username || u.user_id;
  document.getElementById("userRole").textContent = u.role;
  document.querySelector(".avatar").textContent = (u.username || "?").charAt(0).toUpperCase();

  const pill = document.getElementById("statusPill");
  pill.className = "status-pill " + (u.is_active ? "" : "bad");
  pill.innerHTML = `<span class="status-dot ${u.status_color}"></span> ${escHtml(u.status)}`;

  document.getElementById("stats").innerHTML = `
    <div class="stat"><div class="label">Status</div><div class="value" style="font-size:1.2rem;">${escHtml(u.status)}</div></div>
    <div class="stat"><div class="label">Role</div><div class="value" style="font-size:1.2rem;">${escHtml(u.role)}</div></div>
    <div class="stat"><div class="label">Groups</div><div class="value">${(u.groups || []).length}</div></div>
    <div class="stat"><div class="label">Joined</div><div class="value" style="font-size:1rem;">${fmt(u.created_at)}</div></div>
  `;

  const groupsEl = document.getElementById("groups");
  groupsEl.innerHTML =
    (u.groups || [])
      .map((g) => `<span class="tag">${escHtml(g)}</span>`)
      .join("") || '<span class="empty">No groups.</span>';

  const activity = u.activity || [];
  document.querySelector("#activity tbody").innerHTML =
    activity
      .map(
        (a) => `
      <tr>
        <td><span class="tag">${escHtml(a.action)}</span></td>
        <td>${escHtml(a.resource_type || "-")} ${a.resource_id ? '<span class="mono">' + escHtml(a.resource_id) + "</span>" : ""}</td>
        <td>${fmt(a.created_at)}</td>
      </tr>`
      )
      .join("") || '<tr><td colspan="3" class="empty">No activity recorded.</td></tr>';
}

document.addEventListener("DOMContentLoaded", load);