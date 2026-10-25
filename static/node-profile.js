// node-profile.js — public node profile page (Phase 20, T-044).
//
// Fetches /cluster/nodes/{id} and renders the node profile in the
// Community Mockup style: profile hero (avatar, name, id, status pill),
// stats grid (load, queue, caps, last seen), capability cards (name,
// type, description, input_schema), CSS-only load sparkline, recent
// tasks table and the node-scoped activity feed.

// ===== helpers ==========================================================

// Capability-Descriptions sind einzeilig; die Marker Parameter:/notable: werden visuell als Zeilenanfang gerendert.
function richDesc(s) {
  return String(s).replace(/ (Parameter|notable): /g, "<br>$1: ");
}
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
  if (n.includes("ct")) return "ct";
  return "default";
}
function nodeAvatarEmoji(nodeName) {
  const n = (nodeName || "").toLowerCase();
  if (n.includes("cyberfox") || n.includes("felix")) return "🦊";
  if (n.includes("mac") || n.includes("m4")) return "💻";
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

let nodeId =
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
    // Forward-mapping endpoints resolve by node_id only — canonicalize here
    // so name-based URLs (node profile opens via /node/<name>) don't 404.
    nodeId = n.node_id || nodeId;
    render(n, activityReq.events || []);
    loadForwardMappings();
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
  document.getElementById("nodeDesc").textContent = n.description || "";
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
        <p class="card-sub">${richDesc(escHtml(c.description || "No description"))}</p>
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

  // Recent tasks (T-248: sender + worker nodes by NAME, no raw IDs).
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
        <td>${escHtml(t.sender_node_name || "—")}</td>
        <td>${escHtml((t.worker_node_names || []).join(", ") || "—")}</td>
        <td>${fmt(t.created_at)}</td>
        <td>${fmt(t.completed_at)}</td>
      </tr>`
      )
      .join('') || '<tr><td colspan="8" class="empty">No tasks.</td></tr>';

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

// ===== forward mappings (T-Forward) =====================================

function getCsrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)relay_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

function fwdMessage(text, cls) {
  const el = document.getElementById("fwdMsg");
  el.hidden = !text;
  el.textContent = text || "";
  el.className = "empty " + (cls || "");
}

function fwdMutedNote(td, note) {
  td.innerHTML = `<td colspan="8" class="empty">${escHtml(note)}</td>`;
}

// ===== T-246: provider pin (mapping target_node_id) =====================
// The capabilities payload is fetched ONCE per page load and shared by the
// target picker (fillForwardTargetPicker), the pin picker
// (fillForwardPinPicker) and the pin column's node-name resolution — no new
// endpoint, no per-change refetch (design.md §7.2.2).
let fwdCapabilities = null;
const nodeNames = new Map(); // node_id → node_name (fallback: raw id)
// T-247: the submitter's declared slot rows (input_schema rides parsed) —
// the binding editor's SOURCE chip universe (§7.4.2, D-09 cache form).
let fwdSlotRows = [];

async function fetchCapabilitiesPayload() {
  if (fwdCapabilities) return fwdCapabilities;
  try {
    const capsReq = await fetch("/relay/v2/dashboard/api/capabilities", {
      headers: { Accept: "application/json" },
    });
    if (!capsReq.ok) return null;
    fwdCapabilities = (await capsReq.json()).capabilities || [];
    for (const c of fwdCapabilities) {
      for (const n of c.nodes || []) {
        if (n && n.node_id) nodeNames.set(n.node_id, n.node_name || n.node_id);
      }
    }
  } catch (_) {}
  return fwdCapabilities;
}

// T-246: fill the pin picker from the CACHED capabilities payload — one
// option per node advertising `target` (offline nodes included: the pin is
// an address, availability is runtime — design D-10). Resets the selection.
function fillForwardPinPicker(target) {
  const pin = document.getElementById("fwd-pin");
  if (!pin) return;
  while (pin.options.length > 1) pin.remove(1);
  const cap = (fwdCapabilities || []).find((c) => c && c.name === target);
  ((cap && cap.nodes) || []).forEach((n) => {
    if (!n || !n.node_id) return;
    const opt = document.createElement("option");
    opt.value = n.node_id;
    opt.textContent = n.node_name || n.node_id;
    pin.appendChild(opt);
  });
}

// T-245: typed forward slots — the Type column renders the declared slot
// type with a compact value/ref indicator (read-only; the binding editor
// is T-247). Slot rows from an old relay carry no slot_type → neutral
// "forward". Display rules (design.md §8): forward → neutral badge;
// text → badge + the value truncated to 80 chars; bool → "true"/"false"
// badge; declared ref → muted "→ <ref_slot>.<ref_field>".
function fwdTypeCell(decl) {
  if (!decl) return "<td>—</td>";
  const t = String(decl.slot_type || "forward");
  let badge;
  if (t === "text") {
    const v = String(decl.value ?? "");
    badge = `<span class="tag info">text</span>`
      + (v ? ` <small class="muted mono">${escHtml(v.slice(0, 80))}</small>` : "");
  } else if (t === "bool") {
    badge = typeof decl.value === "boolean"
      ? `<span class="tag info">${decl.value ? "true" : "false"}</span>`
      : `<small class="muted">—</small>`;
  } else {
    badge = `<span class="tag">forward</span>`;
  }
  const ref = decl.ref_slot
    ? ` <small class="muted">→ ${escHtml(String(decl.ref_slot))}.${escHtml(String(decl.ref_field || ""))}</small>`
    : "";
  return `<td>${badge}${ref}</td>`;
}

async function loadForwardMappings() {
  const tbody = document.querySelector("#forwardMappingsTable tbody");
  if (!tbody || !nodeId) return;

  // Best-effort identity probe: anonymous visitors keep the table read-only.
  let canManage = false;
  try {
    const meReq = await fetch("/relay/v2/dashboard/api/me", {
      headers: { Accept: "application/json" },
    });
    if (meReq.ok) {
      const me = await meReq.json();
      canManage = me.is_master === true || me.role === "admin";
    }
  } catch (_) {}

  let res;
  try {
    res = await fetch(
      `/relay/v2/forward-mappings?node=${encodeURIComponent(nodeId)}`,
      { headers: { Accept: "application/json" } },
    );
  } catch (_) {
    fwdMutedNote(tbody, "Forward mappings are unavailable.");
    return;
  }
  if (res.status === 401 || res.status === 403) {
    fwdMutedNote(tbody, "sign in as admin to manage forward mappings");
    return;
  }
  if (!res.ok) {
    fwdMutedNote(tbody, "forward mappings are currently unavailable");
    return;
  }
  let mappings;
  try {
    mappings = (await res.json()).mappings || [];
  } catch (_) {
    fwdMutedNote(tbody, "forward mappings are currently unavailable");
    return;
  }

  // T-Forward-2: node-declared forward slots (node.yaml) from the relay.
  // Optional — a relay without the table/endpoint returns 404/401 and the
  // mapping table falls back to the pre-T2 shape (slot → target only).
  let slots = [];
  try {
    const slotsRes = await fetch(
      `/relay/v2/forward-mappings/slots?node=${encodeURIComponent(nodeId)}`,
      { headers: { Accept: "application/json" } },
    );
    if (slotsRes.ok) {
      slots = (await slotsRes.json()).slots || [];
    }
  } catch (_) {}

  const bySlot = new Map(slots.map((s) => [String(s.slot), s]));
  // T-247: the binding editor reads the submitter's DECLARED slot schema
  // (source chip universe — §7.4.2) from the same rows.
  fwdSlotRows = slots;
  const mappedSlots = new Set(mappings.map((m) => String(m.slot)));
  const declaredOnly = slots.filter((s) => !mappedSlots.has(String(s.slot)));

  // T-246: the Provider column resolves pin node ids to names — one shared
  // capabilities GET per page load (cached; anonymous visitors keep the
  // raw-id fallback).
  await fetchCapabilitiesPayload();

  const rowsHtml = mappings.map(
      (m) => {
        const declared = bySlot.get(String(m.slot));
        const labelCell = declared?.label
          ? `${escHtml(declared.label)}${declared?.description
              ? `<br><small class="muted">${escHtml(declared.description)}</small>`
              : ""}`
          : "—";
        const typeCell = fwdTypeCell(declared);
        // T-246: the pin as node NAME (raw id fallback = deleted node);
        // unpinned shows the visible "Provider variabel" badge.
        const pinCell = m.target_node_id
          ? `<span class="tag info">${escHtml(nodeNames.get(m.target_node_id) || m.target_node_id)}</span>`
          : `<span class="tag">variabel</span>`;
        // T-247: the binding state is VISIBLE per row (C7 §7.4.1) — a stale
        // binding shows up as "N rule(s)" and the dashboard editor is the
        // correction path (no auto-unbind). Rules count display-only; a
        // surprising shape falls back to "1+".
        const ruleCount = m.binding && Array.isArray(m.binding.rules)
          ? m.binding.rules.length
          : 0;
        const bindingCell = m.binding
          ? `<span class="tag info">${ruleCount ? ruleCount + (ruleCount === 1 ? " rule" : " rules") : "1+"}</span>`
          : `<span class="tag">legacy</span>`;
        return `
      <tr>
        <td class="mono">${escHtml(m.slot)}</td>
        <td>${labelCell}</td>
        ${typeCell}
        <td><span class="tag info">${escHtml(m.target_capability)}</span></td>
        <td>${pinCell}</td>
        <td>${bindingCell}</td>
        <td>${fmt(m.updated_at)}</td>
        <td>${(
          canManage
            ? `<button type="button" class="fwd-del" data-slot="${escAttr(m.slot)}">delete</button>`
            : ""
        )}</td>
      </tr>`;
      }
    )
    .concat(
      declaredOnly.map(
        (s) => {
          const typeCell = fwdTypeCell(s);
          return `
      <tr>
        <td class="mono">${escHtml(s.slot)}</td>
        <td>${s.label ? escHtml(s.label) : "—"}${s.description
          ? `<br><small class="muted">${escHtml(s.description)}</small>`
          : ""}</td>
        ${typeCell}
        <td><span class="tag">unset</span></td>
        <td>—</td>
        <td>—</td>
        <td>${fmt(s.updated_at)}</td>
        <td>${canManage
          ? `<button type="button" class="fwd-add-declared" data-slot="${escAttr(s.slot)}" data-label="${escAttr(s.label || "")}">map…</button>`
          : ""}</td>
      </tr>`;
        },
      ),
    );

  tbody.innerHTML = rowsHtml.join('') || '<tr><td colspan="8" class="empty">No forward mappings or declared slots.</td></tr>';

  if (canManage) {
    const panel = document.getElementById("forwardMappings");
    document
      .querySelectorAll("#forwardMappingsTable .fwd-del")
      .forEach((btn) => {
        btn.addEventListener("click", () => deleteForwardMapping(btn.dataset.slot));
      });
    document
      .querySelectorAll("#forwardMappingsTable .fwd-add-declared")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const input = document.getElementById("fwd-slot");
          const targetEl = document.getElementById("fwd-target");
          const pinEl = document.getElementById("fwd-pin");
          if (input) input.value = btn.dataset.slot || "";
          // T-246: a prefilled mapping starts unpinned (Provider variabel).
          if (pinEl) pinEl.value = "";
          // T-247: re-render the binding editor for the prefilled slot
          // (programmatic value set fires no change event).
          fwdRenderBindingEditor();
          if (!targetEl || !targetEl.options.length) fillForwardTargetPicker();
          input?.focus?.();
        });
      });
    if (!document.getElementById("fwdAddBtn")) {
      const addRow = document.createElement("div");
      addRow.innerHTML = `
        <div style="display:flex;gap:.5rem;align-items:center;margin-top:.75rem;">
          <input id="fwd-slot" type="text" placeholder="slot (1, a, s-2)" maxlength="64" />
          <select id="fwd-target"><option value="">target capability…</option></select>
          <select id="fwd-pin"><option value="">Provider variabel…</option></select>
          <button type="button" id="fwdAddBtn">Add mapping</button>
        </div>`;
      panel.appendChild(addRow);
      // T-247: the binding editor's container (hidden until a target
      // capability is selected — the change listener builds its rule rows;
      // design §7.4.2 "the binding section appears when the target
      // capability is selected").
      const bindSection = document.createElement("div");
      bindSection.id = "fwdBindingSection";
      bindSection.style.display = "none";
      panel.appendChild(bindSection);
      document.getElementById("fwdAddBtn").addEventListener("click", addForwardMapping);
      // T-246: repopulate the pin picker whenever the capability changes —
      // cached payload, no refetch, selection resets to "variabel".
      // T-247: the same change also (re)renders the binding editor (the
      // target chip universe rides the same cached payload).
      document.getElementById("fwd-target").addEventListener("change", (e) => {
        fillForwardPinPicker(e.target.value);
        fwdRenderBindingEditor();
      });
      // T-247: the source chips follow the declared slot's input_schema.
      document.getElementById("fwd-slot").addEventListener("change", () => {
        fwdRenderBindingEditor();
      });
      fillForwardTargetPicker();
    }
  }
}

async function fillForwardTargetPicker() {
  const select = document.getElementById("fwd-target");
  if (!select || select.options.length > 1) return;
  const caps = await fetchCapabilitiesPayload();
  if (!caps) return;
  const names = Array.from(
    new Set(
      caps
        .map((c) => c && c.name)
        .filter(
          (n) => n && n !== "forward" // the pseudo-capability is never a mapping target
        )
    ),
  ).sort();
  names.forEach((n) => {
    const opt = document.createElement("option");
    opt.value = n;
    opt.textContent = n;
    select.appendChild(opt);
  });
}

function validateForwardSlot(slot) {
  // Mirrors the server rules (^/^[A-Za-z0-9_-]{1,64}$/); server stays authoritative.
  return /^[A-Za-z0-9_-]{1,64}$/.test(slot);
}

// ===== T-247: payload binding editor (C6, design §7.4) =====================
// One section under the mapping add-row: per-rule SOURCE chips (from the
// submitter's declared slot input_schema) + a target field (datalist of the
// selected capability's aggregate schema field names) + the binding-level
// separator. One source chip in a rule = getrennt (copy); several = 
// zusammengeführt (join) — THE rule-row shape IS the modes (plan C6).
// No new endpoint: both chip universes come from the page's existing GETs
// (the capabilities cache + forward-mappings/slots, D-09/T-246 form). The
// aggregate is a DISPLAY source only — the server PUT validates against
// the exact pin/slot branch (§4.2) and divergence fails loud in the editor
// via fwdErrorText.

// Both canonical schema forms (models/capability.py from_dict: nested
// {"fields": {name: spec}} and flat {name: spec}; list-valued field sets
// keyed by an explicit name attr or their position) → the field NAMES.
function fwdSchemaFieldNames(schema) {
  if (!schema || typeof schema !== "object" || Array.isArray(schema)) return [];
  const raw = schema.fields !== undefined ? schema.fields : schema;
  if (!raw || typeof raw !== "object") return [];
  const names = [];
  if (Array.isArray(raw)) {
    raw.forEach((entry, i) => {
      const nm = entry && typeof entry === "object" && entry.name
        ? entry.name
        : "field_" + i;
      names.push(nm);
    });
    return names;
  }
  for (const [key, entry] of Object.entries(raw)) {
    names.push(entry && typeof entry === "object" && entry.name ? entry.name : key);
  }
  return names;
}

function fwdSlotSchemaNames(slotVal) {
  const declared = (fwdSlotRows || []).find((s) => String(s.slot) === slotVal);
  return fwdSchemaFieldNames(declared && declared.input_schema);
}

function fwdCapabilitySchemaNames(target) {
  const cap = (fwdCapabilities || []).find((c) => c && c.name === target);
  return fwdSchemaFieldNames(cap ? cap.input_schema : null);
}

// One rule row: source chips (a declared slot schema exists — click = select)
// or a free text input (no declared schema — the unvalidated-sources branch
// of §4.2 is a supported form), the target field with the capability schema
// datalist, and the remove button.
function fwdAddRuleRow(rule) {
  const rulesBox = document.getElementById("fwdRules");
  if (!rulesBox) return;
  const slotVal = (document.getElementById("fwd-slot")?.value || "").trim();
  const sourceNames = fwdSlotSchemaNames(slotVal);
  const hasSourceChips = sourceNames.length > 0;
  const sources = (rule && rule.sources) || [];
  const chips = hasSourceChips
    ? sourceNames
        .map((n) => {
          const on = sources.includes(n) ? " on" : "";
          return `<button type="button" class="fwd-chip${on}" data-name="${escAttr(n)}">${escHtml(n)}</button>`;
        })
        .join(" ")
    : `<input type="text" class="fwd-sources-free" placeholder="source fields (comma-separated)" value="${escAttr(sources.join(", "))}" />`;
  const hasSlotsSchema = hasSourceChips
    ? ""
    : `<small class="muted">slot '${escHtml(slotVal || "…")}' declares no input_schema — sources free-typed (payload is the universe at submit)</small>`;
  const div = document.createElement("div");
  div.className = "fwd-rule";
  div.style.cssText = "display:flex;gap:.35rem;align-items:center;flex-wrap:wrap;margin-top:.4rem;";
  div.innerHTML = `
    <span class="muted">rule ${rulesBox.querySelectorAll(".fwd-rule").length + 1}</span>
    <span class="fwd-chips" style="display:inline-flex;gap:.25rem;flex-wrap:wrap;">${chips}</span>
    <span class="muted">→</span>
    <input type="text" class="fwd-rule-target" list="fwdTargetFields" placeholder="target field…" style="width:16ch;" value="${escAttr((rule && rule.target) || "")}" />
    <button type="button" class="fwd-rule-del" title="remove rule">✕</button>
    <br>${hasSlotsSchema}`;
  rulesBox.appendChild(div);
  div.querySelectorAll(".fwd-chip").forEach((b) => {
    b.addEventListener("click", () => b.classList.toggle("on"));
  });
  div.querySelector(".fwd-rule-del")?.addEventListener("click", () => div.remove());
}

// The "match by name" helper (board T-247): 1:1 rules from the intersection
// of the declared source names and the target field names — the Specialist
// case without hand work. No other wizard logic.
function fwdMatchByName() {
  const slotVal = (document.getElementById("fwd-slot")?.value || "").trim();
  const target = document.getElementById("fwd-target")?.value || "";
  const sourceNames = fwdSlotSchemaNames(slotVal);
  const targetNames = fwdCapabilitySchemaNames(target);
  const box = document.getElementById("fwdRules");
  if (!box) return;
  if (!sourceNames.length || !targetNames.length) {
    fwdMessage("match by name needs a declared slot schema AND a target schema.", "bad");
    return;
  }
  const targetSet = new Set(targetNames);
  const matched = Array.from(new Set(sourceNames.filter((n) => targetSet.has(n))));
  if (!matched.length) {
    fwdMessage("No 1:1 overlap between the slot schema fields and the target's fields.", "bad");
    return;
  }
  box.innerHTML = "";
  matched.forEach((n) => fwdAddRuleRow({ sources: [n], target: n }));
}

// Rebuild the whole binding section for the CURRENT slot + target pair
// (the rule rows reset — the chip universes change with both).
function fwdRenderBindingEditor() {
  const section = document.getElementById("fwdBindingSection");
  if (!section) return;
  const target = document.getElementById("fwd-target")?.value || "";
  if (!target) {
    section.style.display = "none";
    section.innerHTML = "";
    return;
  }
  const targetNames = fwdCapabilitySchemaNames(target);
  section.style.display = "";
  section.innerHTML = `
    <div style="margin-top:.75rem;padding:.5rem;border:1px solid rgba(127,127,127,.35);border-radius:.5rem;">
      <div class="muted" style="font-size:.9rem;">
        Payload binding — per rule: select the source field(s) → one target field
        (1 source = getrennt/copy, several = zusammengeführt/join). No rules = legacy passthrough
        (unbound fields fly except the reserved prompt fields task/prompt/question/message/input).
      </div>
      <div id="fwdRules"></div>
      <div style="display:flex;gap:.5rem;align-items:center;margin-top:.5rem;flex-wrap:wrap;">
        <button type="button" id="fwdAddRule">add rule</button>
        <button type="button" id="fwdMatchName">match by name</button>
        <label>separator
          <input type="text" id="fwd-separator" value="\\n\\n" style="width:10ch;"
                 title="\\n = newline (the default two-newline join); empty = adjacent join" />
        </label>
      </div>
      <datalist id="fwdTargetFields">${targetNames.map((n) => `<option value="${escAttr(n)}"></option>`).join("")}</datalist>
      <small class="muted">${targetNames.length
        ? "Target fields from the capability schema (" + escHtml(targetNames.join(", ")) + ") — display source: the aggregate; the pinned node's own schema decides the PUT."
        : "The capability declares no input_schema — target free-typed (the PUT validates hard; unpinned bindings need the slot schema)."}</small>
    </div>`;
  document.getElementById("fwdAddRule").addEventListener("click", () => fwdAddRuleRow());
  document.getElementById("fwdMatchName").addEventListener("click", fwdMatchByName);
  fwdAddRuleRow();
}

// Harvest the editor's rule rows (DOM is the state while editing). Every rule
// with a target AND ≥1 source is sendable; half-filled rules are surfaced,
// never silently skipped.
function fwdHarvestBinding() {
  const rules = [];
  let incomplete = false;
  document.querySelectorAll("#fwdRules .fwd-rule").forEach((div) => {
    const freeInput = div.querySelector(".fwd-sources-free");
    const sources = freeInput
      ? freeInput.value.split(",").map((s) => s.trim()).filter(Boolean)
      : Array.from(div.querySelectorAll(".fwd-chip.on")).map((b) => b.dataset.name);
    const target = (div.querySelector(".fwd-rule-target")?.value || "").trim();
    if ((target && !sources.length) || (!target && sources.length)) {
      incomplete = true;
    }
    if (target && sources.length) {
      rules.push({ sources, target });
    }
  });
  return { rules, incomplete };
}

// The editor's separator value: "\n\n" shown as the escape text (input
// values cannot hold raw newlines) is de-escaped on send; empty = adjacent
// join (§7.4.2).
function fwdSeparatorValue() {
  return (document.getElementById("fwd-separator")?.value || "").replace(/\\n/g, "\n");
}

async function addForwardMapping() {
  const input = document.getElementById("fwd-slot");
  const targetEl = document.getElementById("fwd-target");
  const slot = (input.value || "").trim();
  if (!validateForwardSlot(slot)) {
    fwdMessage("Slot must be 1-64 chars: letters, digits, '_' or '-'.", "bad");
    return;
  }
  const target = targetEl.value;
  if (!target) {
    fwdMessage("Pick a target capability.", "bad");
    return;
  }
  const { rules, incomplete } = fwdHarvestBinding();
  if (incomplete) {
    fwdMessage("Each binding rule needs a target and at least one source field.", "bad");
    return;
  }
  const res = await fetch(
    `/relay/v2/forward-mappings/${encodeURIComponent(nodeId)}/${encodeURIComponent(slot)}`,
    {
      method: "PUT",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-CSRF-Token": getCsrfToken(),
      },
      body: JSON.stringify({
        target_capability: target,
        // T-246: the pin ALWAYS rides explicitly ("" = Provider variabel) —
        // the PUT is a full-row replace (design D-02: omitting the pin
        // would unpin).
        target_node_id: document.getElementById("fwd-pin")?.value || "",
        // T-247: the binding rides ALWAYS explicitly (D-02 analog) —
        // completed rules or null (legacy passthrough = the editor's
        // "remove binding" path).
        binding: rules.length ? { rules, separator: fwdSeparatorValue() } : null,
      }),
    },
  );
  if (res.status === 401 || res.status === 403) {
    location.href = "/relay/v2/dashboard/login";
    return;
  }
  if (!res.ok) {
    fwdMessage(await fwdErrorText(res), "bad");
    return;
  }
  fwdMessage("Forward mapping saved.", "ok");
  loadForwardMappings();
}

async function deleteForwardMapping(slot) {
  const res = await fetch(
    `/relay/v2/forward-mappings/${encodeURIComponent(nodeId)}/${encodeURIComponent(slot)}`,
    {
      method: "DELETE",
      headers: { Accept: "application/json", "X-CSRF-Token": getCsrfToken() },
    },
  );
  if (res.status === 401 || res.status === 403) {
    location.href = "/relay/v2/dashboard/login";
    return;
  }
  if (!res.ok) {
    fwdMessage(await fwdErrorText(res), "bad");
    return;
  }
  fwdMessage(`Forward mapping '${slot}' deleted.`, "ok");
  loadForwardMappings();
}

async function fwdErrorText(res) {
  try {
    const body = await res.json();
    return body.detail || `${res.status} ${res.statusText}`;
  } catch (_) {}
  return res.statusText || `HTTP ${res.status}`;
}

document.addEventListener("DOMContentLoaded", load);