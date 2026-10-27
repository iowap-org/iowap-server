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
let fwdMappingCache = []; // T-254-r: edit-button prefill source (slot/target/pin/binding)
// T-235/T-250: mirror of the server's KNOWN_FLOW_PROMPT_FIELDS
// (core/forward.py:53, incl. the split channel systemprompt/taskprompt) —
// datalist suggestions for free-typed sources (payload is the universe at
// submit; the server stays authoritative on every rule).
const FWD_KNOWN_PROMPT_FIELDS = [
  "task", "prompt", "question", "message", "input", "systemprompt", "taskprompt",
];

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
  // T-254-r: the rows cache for the edit button (the editor prefills from
  // the exact mapping object: slot/target/pin/binding).
  fwdMappingCache = mappings;
  const mappedSlots = new Set(mappings.map((m) => String(m.slot)));
  const declaredOnly = slots.filter((s) => !mappedSlots.has(String(s.slot)));

  // T-246: the Provider column resolves pin node ids to names — one shared
  // capabilities GET per page load (cached; anonymous visitors keep the
  // raw-id fallback).
  await fetchCapabilitiesPayload();

  const fwdMappingRows = mappings.map(
      (m) => {
        const declared = bySlot.get(String(m.slot));
        const labelCell = declared?.label
          ? `${escHtml(declared.label)}${declared?.description
              ? `<br><small class="muted">${escHtml(declared.description)}</small>`
              : ""}`
          : "—";
        const typeCell = fwdTypeCell(declared);
        // T-246: the pin as node NAME (raw id fallback = deleted node);
        // unpinned shows the visible "variable provider" badge.
        const pinCell = m.target_node_id
          ? `<span class="tag info">${escHtml(nodeNames.get(m.target_node_id) || m.target_node_id)}</span>`
          : `<span class="tag">variable</span>`;
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
            ? `<button type="button" class="fwd-edit" data-slot="${escAttr(m.slot)}">edit</button> <button type="button" class="fwd-del" data-slot="${escAttr(m.slot)}">delete</button>`
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

  tbody.innerHTML = fwdMappingRows.join('') || '<tr><td colspan="8" class="empty">No forward mappings or declared slots.</td></tr>';

  if (canManage) {
    const panel = document.getElementById("forwardMappings");
    document
      .querySelectorAll("#forwardMappingsTable .fwd-del")
      .forEach((btn) => {
        btn.addEventListener("click", () => deleteForwardMapping(btn.dataset.slot));
      });
    document
      .querySelectorAll("#forwardMappingsTable .fwd-edit")
      .forEach((btn) => {
        const m = fwdMappingCache.find(
          (row) => String(row.slot) === String(btn.dataset.slot));
        if (!m) return;
        const input = document.getElementById("fwd-slot");
        const targetEl = document.getElementById("fwd-target");
        const pinEl = document.getElementById("fwd-pin");
        if (input) input.value = m.slot || "";
        if (targetEl) targetEl.value = m.target_capability || "";
        if (pinEl) pinEl.value = m.target_node_id || "";
        // T-254-r: open the editor preloaded with the EXISTING binding
        // (rules + separator) — the variable-wiring form, edit not rebuild.
        fillForwardTargetPicker();
        targetEl.value = m.target_capability || "";
        fwdRenderBindingEditor(m.binding && Array.isArray(m.binding.rules)
          ? m.binding : undefined);
        input?.focus?.();
      });
    document
      .querySelectorAll("#forwardMappingsTable .fwd-add-declared")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const input = document.getElementById("fwd-slot");
          const targetEl = document.getElementById("fwd-target");
          const pinEl = document.getElementById("fwd-pin");
          if (input) input.value = btn.dataset.slot || "";
          // T-246: a prefilled mapping starts unpinned (variable provider).
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
          <select id="fwd-pin"><option value="">variable provider…</option></select>
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
      // cached payload, no refetch, selection resets to "variable".
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
// separator. One source chip in a rule = copy; several = join — THE rule-row shape IS the modes (plan C6).
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

function fwdTargetFieldSpec(target, fieldName) {
  // T-252: the capability field spec for a const-value field — rides
  // parsed on the capabilities cache (same form the server PUT checks).
  const cap = (fwdCapabilities || []).find((c) => c && c.name === target);
  if (!cap || !cap.input_schema) return null;
  const schema = cap.input_schema;
  const fields =
    schema && schema.fields && typeof schema.fields === "object"
      ? schema.fields
      : schema;
  return fields && typeof fields === "object" ? fields[fieldName] || null : null;
}

// One rule row: source-side **select** (known prompt fields + slot-schema
// names + "(const)" for a fixed-value rule, + "other…" swaps to free
// text), the target **dropdown** (capability schema fields), and for
// const rules the value control typed by the TARGET field spec
// (boolean→checkbox with its description as title, else text input).
// 1 source = copy; several via "source2+" select = join.
function fwdAddRuleRow(rule) {
  const rulesBox = document.getElementById("fwdRules");
  if (!rulesBox) return;
  const slotVal = (document.getElementById("fwd-slot")?.value || "").trim();
  const targetVal = document.getElementById("fwd-target")?.value || "";
  const sourceNames = fwdSlotSchemaNames(slotVal);
  const targetNames = fwdCapabilitySchemaNames(targetVal);
  const rules = (rule && rule.sources && rule.sources.length) ? rule.sources : [];
  const isConst = rule && rule.sources == null && (rule.target || null);
  const isFreeText = rule && rule.freeText === true;
  const options = Array.from(new Set(FWD_KNOWN_PROMPT_FIELDS.concat(sourceNames)));
  const sourceCell = hasSourceChips(sourceNames) && !isConst
    ? chipCell(rules, sourceNames)
    : selectCell(options, isConst, rules, isFreeText);
  const valueHTML = isConst ? fwdConstValueHTML(rule, targetVal, (rule && rule.target) || "", rules) : "";
  const div = document.createElement("div");
  div.className = "fwd-rule";
  div.style.cssText = "display:flex;gap:.35rem;align-items:center;flex-wrap:wrap;margin-top:.4rem;";
  div.innerHTML = `
    <span class="muted">rule ${rulesBox.querySelectorAll(".fwd-rule").length + 1}</span>
    <span class="fwd-chips" style="display:inline-flex;gap:.25rem;flex-wrap:wrap;">${sourceCell}${
      // T-254-r2: the (const) option rides the chips case too — the editor
      // hides the select only when the slot schema provides chips, but a
      // const rule needs a path from there as well (schema-case).
      hasSourceChips(sourceNames) && !isConst
        ? `<button type="button" class="fwd-chip-btn" title="const rule — writes a fixed value">(const)</button>`
        : ""
    }</span>
    <span class="muted">→</span>
    ${targetNames.length
      ? `<select class="fwd-rule-target" style="width:auto;">${targetNames
          .map(
            (n) =>
              `<option value="${escAttr(n)}"${(rule && rule.target) === n ? " selected" : ""}>${escHtml(n)}</option>`
          )
          .join("")}
        <option value=""${ !(rule && rule.target) ? " selected" : ""}>target…</option></select>`
      : `<input type="text" class="fwd-rule-target" list="fwdTargetFields" placeholder="target field…" style="width:16ch;" value="${escAttr((rule && rule.target) || "")}" />`}
    ${valueHTML}
    <button type="button" class="fwd-rule-del" title="remove rule">✕</button>`;
  rulesBox.appendChild(div);
  div.querySelectorAll(".fwd-chip").forEach((b) => {
    b.addEventListener("click", () => { b.classList.toggle("on"); fwdUpdatePreview(); });
  });
  // T-254-r2: the (const) chip swaps the row to a const row (the same swap
  // the select-form "__const__" option makes below): chips vanish, the
  // (const) marker + a value control appear — then the target dropdown
  // re-types the value control.
  div.querySelector(".fwd-chip-btn")?.addEventListener("click", () => {
    const chips = div.querySelector(".fwd-chips");
    if (!chips) return;
    chips.innerHTML = `<span class="muted" title="const rule — writes a fixed value">(const)</span>
      <span class="fwd-const-value"></span>`;
    chips.className = "fwd-chips"; // keep the layout hook
    const holder = chips.querySelector(".fwd-const-value");
    const tgtVal = (div.querySelector(".fwd-rule-target")?.value || "");
    holder.innerHTML = fwdConstValueHTML(null, document.getElementById("fwd-target")?.value || "", tgtVal, []);
    holder.querySelector(".fwd-const")?.addEventListener("input", fwdUpdatePreview);
    constTargetWatch(div);
    fwdUpdatePreview();
  });
  div.querySelector(".fwd-rule-del")?.addEventListener("click", () => { div.remove(); fwdUpdatePreview(); });
  const srcSel = div.querySelector(".fwd-source-select");
  if (srcSel && !isConst) {
    srcSel.addEventListener("change", () => {
      if (srcSel.value === "__const__") {
        // T-252: const row — source select swaps to the (const) marker
        // and the value control (typed by the picked target's spec) appears.
        const span = document.createElement("span");
        span.className = "muted";
        span.title = "const rule — writes a fixed value";
        span.textContent = "(const)";
        srcSel.replaceWith(span);
        const holder = document.createElement("span");
        holder.className = "fwd-const-value";
        div.insertBefore(holder, div.querySelector(".fwd-rule-del"));
        holder.innerHTML = fwdConstValueHTML(null, targetVal, (div.querySelector(".fwd-rule-target")?.value || ""), []);
        const vInp2 = holder.querySelector("input");
        if (vInp2) vInp2.addEventListener("input", fwdUpdatePreview);
        if (div.querySelector(".fwd-rule-target")) constTargetWatch(div);
        fwdUpdatePreview();
      } else if (srcSel.value === "__other__") {
        const inp = document.createElement("input");
        inp.type = "text"; inp.className = "fwd-sources-free f-source-single";
        inp.setAttribute("list", "fwdSourceFields"); inp.placeholder = "source field name…";
        inp.style.width = "20ch"; inp.dataset.role = "extra";
        srcSel.replaceWith(inp); inp.focus();
      } else if (srcSel.value !== "") {
        const b = document.createElement("button");
        b.type = "button"; b.className = "fwd-chip on"; b.dataset.name = srcSel.value;
        b.textContent = srcSel.value;
        srcSel.replaceWith(b);
        b.addEventListener("click", () => { b.classList.toggle("on"); fwdUpdatePreview(); });
      } else fwdUpdatePreview();
    });
  }
  const tgtSel = div.querySelector(".fwd-rule-target");
  if (tgtSel) constTargetWatch(div);
}

// T-252: the target dropdown of a rule row watches for const-value swaps —
// when the target field changes on a const row, the value control re-types
// to the new field's spec (checkbox/string/number).
function constTargetWatch(div) {
  const tgtSel = div.querySelector(".fwd-rule-target");
  if (!tgtSel) return;
  tgtSel.addEventListener("change", () => {
    const holder = div.querySelector(".fwd-const-value");
    if (holder) {
      const capName = (document.getElementById("fwd-target")?.value || "");
      holder.innerHTML = fwdConstValueHTML(null, capName, tgtSel.value || "", []);
      const vInp = holder.querySelector("input");
      if (vInp) vInp.addEventListener("input", fwdUpdatePreview);
    }
    fwdUpdatePreview();
  });
}

function hasSourceChips(sourceNames) { return Array.isArray(sourceNames) && sourceNames.length > 0; }
function chipCell(sources, names) {
  return names.map((n) => {
    const on = sources.includes(n) ? " on" : "";
    return `<button type="button" class="fwd-chip${on}" data-name="${escAttr(n)}">${escHtml(n)}</button>`;
  }).join(" ");
}
function selectCell(options, isConst, sources, isFreeText) {
  const o = options.map((n) => `<option value="${escAttr(n)}"${sources.includes(n) ? " selected" : ""}>${escHtml(n)}</option>`).join("");
  return isConst
    ? `<span class="muted" title="const rule — writes a fixed value">(const)</span>`
    : (isFreeText
      ? `<input type="text" class="fwd-sources-free f-source-single" list="fwdSourceFields" placeholder="source…" style="width:18ch;" value="${escAttr(sources[0] || "")}" />`
      : `<select class="fwd-source-select">
           <option value=""${sources.length ? "" : " selected"}>source…</option>${o}
           <option value="__const__">(const)</option>
           <option value="__other__">other…</option>
         </select>`);
}
function fwdConstValueHTML(rule, capName, fieldName, _unused) {
  const spec = fwdTargetFieldSpec(capName, fieldName);
  const type = spec && spec.type ? String(spec.type) : "string";
  const title = spec && spec.description ? ` title="${escAttr(String(spec.description).slice(0, 200))}"` : "";
  const val = rule && rule.value !== undefined ? rule.value : (type === "boolean" ? false : "");
  if (type === "boolean")
    return `<span class="fwd-const-value"><label><input type="checkbox" class="fwd-const" ${val === true || val === "true" ? "checked" : ""}${title} />(const)</label></span>`;
  // T-254-r2: object-typed consts (schema case) get a textarea + JSON hint —
  // the value rides verbatim as a JSON object on the PUT (a string would 422
  // against the field spec).
  if (type === "object")
    return `<span class="fwd-const-value"><textarea class="fwd-const" spellcheck="false" style="width:26rem;min-height:5rem;font-family:var(--mono,monospace);font-size:.8rem;" placeholder='{"type": "object", …}(JSON)'></textarea></span>`;
  return `<span class="fwd-const-value"><input type="${type === "integer" || type === "number" ? "number" : "text"}" class="fwd-const" style="width:12ch;" value="${escAttr(val === null || val === undefined ? "" : String(val))}"${title} placeholder="const…"/></span>`;
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
function fwdRenderBindingEditor(prefillBinding) {
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
        (1 source = copy, several = join). No rules = legacy passthrough
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
      <datalist id="fwdSourceFields">${Array.from(new Set(FWD_KNOWN_PROMPT_FIELDS.concat(targetNames)))
        .map((n) => `<option value="${escAttr(n)}"></option>`).join("")}</datalist>
      <small class="muted">${targetNames.length
        ? "Target = dropdown (capability schema). Sources: chips (slot schema) or datalist-suggested free text (known prompt fields)."
        : "The capability declares no input_schema — target free-typed (the PUT validates hard; unpinned bindings need the slot schema)."}</small>
      <div id="fwdPreviewCard" style="margin-top:.6rem;padding:.5rem;border:1px dashed rgba(127,127,127,.4);border-radius:.4rem;">
        <div id="fwdCapSpec"></div>
        <span class="muted" style="font-size:.9rem;">preview — flow submit → node payload (live simulator, mirrors <code>apply_binding</code>)</span>
        <div style="display:flex;gap:.7rem;align-items:stretch;flex-wrap:wrap;margin-top:.35rem;">
          <div style="flex:1 1 21rem;min-width:18rem;">
            <small class="muted">flow submits (edit freely):</small>
            <textarea id="fwdPreviewSample" spellcheck="false"
              style="width:100%;min-height:6rem;font-family:var(--mono,monospace);font-size:.85rem;"></textarea>
          </div>
          <div style="flex:1 1 21rem;min-width:18rem;">
            <small class="muted">node receives:</small>
            <div id="fwdPreviewOut" style="font-size:.85rem;"></div>
          </div>
        </div>
      </div>
    </div>`;
  document.getElementById("fwdAddRule").addEventListener("click", () => {
    fwdAddRuleRow();
    fwdUpdatePreview();
  });
  document.getElementById("fwdMatchName").addEventListener("click", () => {
    fwdMatchByName();
    fwdUpdatePreview();
  });
  document.getElementById("fwdPreviewSample").value = FWD_PREVIEW_SAMPLE_DEFAULT;
  // T-251: delegated live re-render — any typing/chip-toggle/target change
  // inside the section refreshes the preview (remove buttons included).
  section.addEventListener("input", (e) => {
    if (e.target.id === "fwd-separator" || e.target.closest("#fwdPreviewCard") || e.target.closest("#fwdRules")) {
      fwdUpdatePreview();
    }
  });
  section.addEventListener("click", (e) => {
    if (e.target.closest("#fwdRules")) fwdUpdatePreview();
  });
  // T-254-r: edit mode — existing binding prefill (rules + separator) or the
  // empty one-row default (add flow). The rule rows build AFTER the datalists.
  if (prefillBinding && Array.isArray(prefillBinding.rules)) {
    document.getElementById("fwd-separator").value =
      typeof prefillBinding.separator === "string" ? prefillBinding.separator : "\\n\\n";
    prefillBinding.rules.forEach((r) => fwdAddRuleRow(r));
  } else {
    fwdAddRuleRow();
  }
  fwdRenderCapSpec(target);
  fwdUpdatePreview();
}

// T-251: client mirror of core/forward.py apply_binding (\u00a73-4, T-247) \u2014 the
// live preview simulator. Byte-order-faithful: rules in order over a working
// copy; \u22651 present source fires (1 = copy verbatim natively; several = join
// with separator, non-str \u2192 json.dumps); fired rule consumes sources except
// its own target; dormant rule writes/consumes nothing; final sweep keeps
// written targets + the \u00a710 reserved prompt set, everything else flies.
function fwdApplyBindingPreview(payload, rules, separator) {
  const working = Object.assign({}, payload || {});
  const written = new Set();
  for (const rule of rules || []) {
    // T-252 const-rule: no 'sources' key → write the fixed value natively,
    // consume nothing (mirror of core/forward.py apply_binding).
    if (!("sources" in (rule || {}))) {
      working[rule.target] = rule.value;
      written.add(rule.target);
      continue;
    }
    const sources = rule.sources || [];
    const present = sources.filter((s) =>
      Object.prototype.hasOwnProperty.call(working, s)
    );
    if (!present.length) continue;
    let value;
    if (present.length === 1) {
      value = working[present[0]];
    } else {
      value = present
        .map((s) =>
          typeof working[s] === "string" ? working[s] : JSON.stringify(working[s])
        )
        .join(separator);
    }
    working[rule.target] = value;
    written.add(rule.target);
    present.forEach((s) => {
      if (s !== rule.target) delete working[s];
    });
  }
  const out = {};
  Object.keys(working).forEach((k) => {
    if (written.has(k) || FWD_KNOWN_PROMPT_FIELDS.includes(k)) out[k] = working[k];
  });
  return out;
}

// T-251 preview-state helpers: the sample payload (editable) + the live
// before/after render. Parser is lenient: broken JSON surfaces as a message,
// never as a dead panel.
const FWD_PREVIEW_SAMPLE_DEFAULT = JSON.stringify(
  {
    systemprompt: "Answer ONLY with the plan JSON (schema below).",
    taskprompt: "Plan the task; capabilities snapshot follows.",
    original_request: "the user's original request",
  },
  null,
  2
);

// T-251/T-250: render the SELECTED capability **so wie immer** (Ronny
// 2026-10-26) — the IDENTICAL card form the node profile renders for
// every capability (card-title / card-sub type·version / card-sub
// description via richDesc incl. the Parameter:/notable: line markers /
// the full input_schema <pre> — the same rows the hard PUT validates
// against). Display-only, no re-styling of its own.
function fwdRenderCapSpec(capName) {
  const el = document.getElementById("fwdCapSpec");
  if (!el) return;
  const cap = (fwdCapabilities || []).find((c) => c && c.name === capName);
  if (!cap) {
    el.innerHTML = `<p class="empty">Capability not in the cluster catalog.</p>`;
    return;
  }
  el.innerHTML = `
    <div class="card">
      <div class="card-title">${escHtml(cap.name || capName)}${cap.available === false ? ' <span class="tag bad">unavailable</span>' : ""}</div>
      <div class="card-sub">${escHtml(cap.type || "unknown")} · v${escHtml(cap.version || "1.0.0")}</div>
      <p class="card-sub">${richDesc(escHtml(cap.description || "No description"))}</p>
      ${cap.input_schema ? `<pre>${escHtml(JSON.stringify(cap.input_schema, null, 2))}</pre>` : ""}
    </div>`;
}

function fwdUpdatePreview() {
  const outEl = document.getElementById("fwdPreviewOut");
  if (!outEl) return;
  let sample;
  try {
    sample = JSON.parse(document.getElementById("fwdPreviewSample")?.value || "{}");
  } catch (e) {
    outEl.innerHTML = `<span class="bad">sample JSON invalid: ${escHtml(String(e.message).slice(0, 80))}</span>`;
    return;
  }
  const { rules } = fwdHarvestBinding();
  const separator = fwdSeparatorValue();
  const result = fwdApplyBindingPreview(sample, rules, separator);
  const before = Object.keys(sample).sort();
  const after = result;
  const afterKeys = Object.keys(after);
  const rulesView = (rules || [])
    .map((r) => {
      const isConstRule = !("sources" in r);
      const kind = isConstRule ? "const" : r.sources.length > 1 ? `join (${separator.length ? "sep" : "adjacent"})` : "copy";
      const left = isConstRule ? `(const) ${JSON.stringify(r.value)}` : r.sources.join(" + ");
      return `&nbsp;&nbsp;${escHtml(left)} \u2192 <b>${escHtml(r.target)}</b> (${kind})`;
    })
    .join("<br>") || "<i>no rules \u2014 legacy passthrough (reserved fields survive)</i>";
  const fliesList = before.filter((k) => !(k in after));
  outEl.innerHTML =
    `<b>rules in effect:</b><br>${rulesView}<br><br>` +
    `<b>node receives:</b><pre style="margin:.3rem 0;white-space:pre-wrap;">${escHtml(JSON.stringify(after, null, 2))}</pre>` +
    (fliesList.length
      ? `<span class="muted">dropped (unbound): ${escHtml(fliesList.join(", "))}</span>`
      : `<span class="muted">nothing dropped</span>`);
}

// Harvest the editor's rule rows (DOM is the state while editing). Every rule
// with a target AND (≥1 source OR a const value) is sendable; half-filled
// rules are surfaced, never silently skipped. Const rows (T-252) carry no
// source select — their value control is the typing evidence.
function fwdHarvestBinding() {
  const rules = [];
  let incomplete = false;
  document.querySelectorAll("#fwdRules .fwd-rule").forEach((div, idx) => {
    const target = (div.querySelector(".fwd-rule-target")?.value || "").trim();
    const srcSel = div.querySelector(".fwd-source-select");
    const chipOns = Array.from(div.querySelectorAll(".fwd-chip.on")).map((b) => b.dataset.name);
    const freeInp = div.querySelector(".fwd-sources-free");
    let sources = [];
    let isConst = false;
    let value = undefined;
    if (srcSel) {
      // select form: the picked option (or more, via added chips) — an
      // empty pick on a fresh row = nothing yet (the placeholder row)
      if (chipOns.length) sources = chipOns;
      else if (srcSel.value && srcSel.value !== "__other__") sources = [srcSel.value];
      if (!chipOns.length && !freeInp && (!srcSel.value || srcSel.value === "__other__")) {
        return; // empty row — ignore
      }
    } else if (chipOns.length) {
      sources = chipOns;
    } else if (freeInp) {
      sources = [freeInp.value].map((s) => s.trim()).filter(Boolean);
      if (!sources.length) return; // empty free row
    } else if (div.querySelector(".fwd-const-value")) {
      isConst = true; // (const) row
    } else {
      return;
    }
    if (isConst) {
      const box = div.querySelector(".fwd-const");
      if (!target) { incomplete = true; return; }
      if (!box) { incomplete = true; return; }
      if (box.type === "checkbox") value = !!box.checked;
      else {
        const raw = (box.value || "").trim();
        if (raw === "") { incomplete = true; return; }
        // T-254-r2: an object-typed target (e.g. schema) needs a REAL object —
        // the textarea JSON parses on harvest; a broken JSON is incomplete.
        if (box.type === "textarea") {
          try {
            const parsed = JSON.parse(raw);
            if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) { incomplete = true; return; }
            value = parsed;
          } catch (_) { incomplete = true; return; }
        } else {
          value = box.type === "number" ? Number(raw) : raw;
        }
      }
      rules.push({ target, value });
      return;
    }
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
        // T-246: the pin ALWAYS rides explicitly ("" = variable provider) —
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