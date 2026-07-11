// dashboard.js — AI-Relay Dashboard
// Keine onclick-Handler im HTML — alles via Event Delegation

let currentUser = null;
let allPermissions = [];
let groupsData = [];
let editingGroupId = null;

function fmt(d) {
  if (!d) return '-';
  const dt = new Date(d);
  return isNaN(dt) ? d : dt.toLocaleString();
}

function getCsrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)relay_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : '';
}

async function fetchJson(path, opts={}) {
  const res = await fetch(path, {
    headers: {
      ...opts.headers,
      'Accept': 'application/json',
      'X-CSRF-Token': getCsrfToken(),
    },
    ...opts
  });
  if (res.status === 401 || res.status === 403) {
    location.href = '/relay/v2/dashboard/login';
    return new Promise(() => {});
  }
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status} ${text || res.statusText}`);
  }
  return res.json();
}

async function postForm(path, formData) {
  return fetchJson(path, { method: 'POST', body: formData });
}

async function delJson(path) {
  return fetchJson(path, { method: 'DELETE' });
}

function showToken(nodeId, tokenValue) {
  document.getElementById('tokenPath').textContent = `~/.relay/${nodeId}.token`;
  document.getElementById('tokenValue').textContent = tokenValue;
  document.getElementById('tokenOverlay').classList.remove('hidden');
  document.getElementById('tokenBox').classList.remove('hidden');
}

function hideToken() {
  document.getElementById('tokenOverlay').classList.add('hidden');
  document.getElementById('tokenBox').classList.add('hidden');
}

function copyToken() {
  navigator.clipboard.writeText(document.getElementById('tokenValue').textContent).then(() => alert('Token copied.'));
}

function showTab(mode) {
  document.getElementById('viewDashboard').classList.toggle('hidden', mode !== 'dashboard');
  document.getElementById('viewAdmin').classList.toggle('hidden', mode !== 'admin');
  document.getElementById('tabDashboard').classList.toggle('active', mode === 'dashboard');
  document.getElementById('tabAdmin').classList.toggle('active', mode === 'admin');
}

function can(perm) {
  return currentUser && (currentUser.is_master || (currentUser.permissions || []).includes(perm));
}

function adminMsg(text, isError) {
  const el = document.getElementById('adminMsg');
  el.textContent = text;
  el.className = isError ? 'error' : 'ok';
  setTimeout(() => { el.textContent = ''; el.className = ''; }, 5000);
}

async function loadMe() {
  try {
    currentUser = await fetchJson('/relay/v2/dashboard/api/me');
    if (can('users:manage') || can('groups:manage')) {
      document.getElementById('tabAdmin').classList.remove('hidden');
    } else {
      document.getElementById('tabAdmin').classList.add('hidden');
    }
    document.getElementById('usersSection').classList.toggle('hidden', !can('users:manage'));
    document.getElementById('groupsSection').classList.toggle('hidden', !can('groups:manage'));
  } catch (err) {
    console.error(err);
  }
}

async function approveNode(nodeId) {
  const role = document.getElementById(`role-${nodeId}`).value;
  const caps = document.getElementById(`caps-${nodeId}`).value.split(',').map(s => s.trim()).filter(Boolean).map(name => ({ name, version: '1.0' }));
  const data = await fetchJson(`/relay/v2/admin/nodes/${encodeURIComponent(nodeId)}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role, capabilities: caps })
  });
  showToken(nodeId, data.token);
  loadAll();
}

async function newToken(nodeId) {
  const data = await fetchJson(`/relay/v2/admin/nodes/${encodeURIComponent(nodeId)}/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
  showToken(nodeId, data.token);
  loadAll();
}

async function deleteNode(nodeId, nodeName) {
  if (!confirm(`Delete node ${nodeName} (${nodeId})? This cannot be undone.`)) return;
  try {
    await fetchJson(`/relay/v2/admin/nodes/${encodeURIComponent(nodeId)}`, { method: 'DELETE' });
    loadAll();
  } catch (err) {
    alert('Delete failed: ' + err.message);
  }
}

function renderAction(n) {
  let actions = '';
  if (n.status === 'pending') {
    const caps = (n.capability_names || []).join(', ') || 'vault';
    actions = `
      <select id="role-${n.node_id}" class="inline-select">
        <option value="service" selected>service</option>
        <option value="worker">worker</option>
      </select>
      <input id="caps-${n.node_id}" class="inline-input" value="${caps}" />
      <button class="approve-btn" data-node-id="${n.node_id}">Approve</button>
    `;
  } else {
    actions = `<button class="token-btn" data-node-id="${n.node_id}">New Token</button>`;
  }
  if (can('nodes:delete')) {
    actions += ` <button class="refresh danger delete-btn" data-node-id="${n.node_id}" data-node-name="${(n.node_name || '').replace(/"/g, '&quot;')}">Delete</button>`;
  }
  return actions;
}

async function loadAdmin() {
  if (!can('users:manage') && !can('groups:manage')) return;
  try {
    const reqs = [];
    if (can('users:manage')) reqs.push(fetchJson('/relay/v2/dashboard/api/users'));
    else reqs.push(Promise.resolve({users: []}));
    if (can('groups:manage')) {
      reqs.push(fetchJson('/relay/v2/dashboard/api/groups'));
      reqs.push(fetchJson('/relay/v2/dashboard/api/permissions'));
    } else {
      reqs.push(Promise.resolve({groups: []}));
      reqs.push(Promise.resolve({permissions: []}));
    }
    const [usersData, groupsDataRaw, permsData] = await Promise.all(reqs);
    allPermissions = permsData.permissions || [];
    groupsData = groupsDataRaw.groups || [];
    renderUsers(usersData.users || []);
    renderGroups(groupsData);
  } catch (err) {
    adminMsg('Admin load failed: ' + err.message, true);
    console.error(err);
  }
}

function renderUsers(users) {
  document.querySelector('#users tbody').innerHTML = users.map(u => `
    <tr>
      <td>${u.username}</td>
      <td>${u.email || '-'}</td>
      <td><input id="groups-${u.user_id}" class="inline-input" value="${(u.groups || []).join(', ')}" /></td>
      <td><span class="tag ${u.is_active ? 'badge-active' : 'badge-inactive'}">${u.is_active ? 'active' : 'inactive'}</span></td>
      <td>${fmt(u.created_at)}</td>
      <td class="admin-actions">
        <button class="token-btn save-groups-btn" data-user-id="${u.user_id}">Save Groups</button>
        <button class="token-btn reset-pw-btn" data-user-id="${u.user_id}">Reset Password</button>
        <button class="approve-btn toggle-active-btn" data-user-id="${u.user_id}" data-active="${!u.is_active}">${u.is_active ? 'Deactivate' : 'Activate'}</button>
        <button class="refresh danger delete-user-btn" data-user-id="${u.user_id}">Delete</button>
      </td>
    </tr>
  `).join('') || '<tr><td colspan="6">No users found.</td></tr>';
}

function renderGroups(groups) {
  document.querySelector('#groups tbody').innerHTML = groups.map(g => `
    <tr>
      <td>${g.group_name}</td>
      <td>${g.description || '-'}</td>
      <td>${(g.permissions || []).map(p => `<span class="tag">${p}</span>`).join(' ')}</td>
      <td class="admin-actions">
        <button class="token-btn edit-perms-btn" data-group-id="${g.group_id}" data-group-name="${(g.group_name || '').replace(/"/g, '&quot;')}">Edit Permissions</button>
      </td>
    </tr>
  `).join('') || '<tr><td colspan="4">No groups found.</td></tr>';
}

async function createUser(e) {
  e.preventDefault();
  const form = e.target;
  const data = new FormData(form);
  try {
    await postForm('/relay/v2/dashboard/api/users', data);
    adminMsg('User created.');
    form.reset();
    form.groups.value = 'user';
    loadAdmin();
  } catch (err) {
    adminMsg('Create user failed: ' + err.message, true);
  }
  return false;
}

async function updateUserGroups(userId) {
  const groups = document.getElementById(`groups-${userId}`).value;
  try {
    await postForm(`/relay/v2/dashboard/api/users/${encodeURIComponent(userId)}/groups`,
      new URLSearchParams({ groups }));
    adminMsg('Groups updated.');
    loadAdmin();
  } catch (err) {
    adminMsg('Update groups failed: ' + err.message, true);
  }
}

async function resetPassword(userId) {
  const password = prompt('Enter new password (min 8 chars):');
  if (!password) return;
  try {
    await postForm(`/relay/v2/dashboard/api/users/${encodeURIComponent(userId)}/password`,
      new URLSearchParams({ password }));
    adminMsg('Password reset.');
  } catch (err) {
    adminMsg('Password reset failed: ' + err.message, true);
  }
}

async function toggleActive(userId, active) {
  try {
    await postForm(`/relay/v2/dashboard/api/users/${encodeURIComponent(userId)}/active`,
      new URLSearchParams({ active: active ? 'true' : 'false' }));
    adminMsg(`User ${active ? 'activated' : 'deactivated'}.`);
    loadAdmin();
  } catch (err) {
    adminMsg('Toggle active failed: ' + err.message, true);
  }
}

async function deleteUser(userId) {
  if (!confirm('Delete this user?')) return;
  try {
    await delJson(`/relay/v2/dashboard/api/users/${encodeURIComponent(userId)}`);
    adminMsg('User deleted.');
    loadAdmin();
  } catch (err) {
    adminMsg('Delete user failed: ' + err.message, true);
  }
}

function editGroupPerms(groupId, groupName) {
  editingGroupId = groupId;
  document.getElementById('permGroupName').textContent = groupName;
  const group = groupsData.find(g => g.group_id === groupId) || { permissions: [] };
  const currentPerms = group.permissions || [];
  document.getElementById('permCheckboxes').innerHTML = allPermissions.map(p => `
    <label><input type="checkbox" value="${p.permission_name}" ${currentPerms.includes(p.permission_name) ? 'checked' : ''}>
      <span>${p.permission_name}</span>
    </label>
  `).join('');
  document.getElementById('permOverlay').classList.remove('hidden');
  document.getElementById('permBox').classList.remove('hidden');
}

function hidePermModal() {
  document.getElementById('permOverlay').classList.add('hidden');
  document.getElementById('permBox').classList.add('hidden');
  editingGroupId = null;
}

async function saveGroupPerms() {
  if (!editingGroupId) return;
  const checked = Array.from(document.querySelectorAll('#permCheckboxes input:checked')).map(i => i.value);
  try {
    await postForm(`/relay/v2/dashboard/api/groups/${encodeURIComponent(editingGroupId)}/permissions`,
      new URLSearchParams({ permissions: checked.join(',') }));
    adminMsg('Group permissions saved.');
    hidePermModal();
    loadAdmin();
  } catch (err) {
    adminMsg('Save permissions failed: ' + err.message, true);
  }
}

async function loadAll() {
  const btn = document.getElementById('btnRefresh');
  btn.disabled = true;
  document.getElementById('status').textContent = 'loading...';
  try {
    const [overview, endpoints, events] = await Promise.all([
      fetchJson('/relay/v2/dashboard/api/overview'),
      fetchJson('/relay/v2/dashboard/api/endpoints'),
      fetchJson('/relay/v2/dashboard/api/events/recent?limit=50'),
    ]);

    const userNodes = (overview.nodes || []).filter(n => n.node_id !== '__dashboard_admin__');

    const s = overview.summary;
    const taskStatText = Object.entries(s.task_stats || {}).map(([k,v]) => k + ': ' + v).join(' · ') || '-';
    document.getElementById('summary').innerHTML = `
      <div class="card"><h2>Nodes</h2><div class="big ${s.online_nodes > 0 ? 'ok' : 'bad'}">${s.online_nodes}/${s.total_nodes}</div><div>online</div></div>
      <div class="card"><h2>Tasks</h2><div class="big">${s.total_tasks}</div><div>${taskStatText}</div></div>
      <div class="card"><h2>Active Stages</h2><div class="big ${s.active_stages > 0 ? 'warn' : 'ok'}">${s.active_stages}</div></div>
      <div class="card"><h2>Artifacts</h2><div class="big">${s.total_artifacts}</div></div>
    `;

    document.querySelector('#nodes tbody').innerHTML = userNodes.map(n => `
      <tr class="${n.status === 'pending' ? 'pending-row' : ''}">
        <td class="mono">${n.node_id}</td>
        <td>${n.node_name}</td>
        <td><span class="tag">${n.role}</span></td>
        <td><span class="tag ${n.status === 'online' ? 'ok' : 'bad'}">${n.status}</span></td>
        <td>${(n.capability_names || []).join(', ')}</td>
        <td>${n.load ?? '-'}</td>
        <td>${n.queue_depth ?? '-'}</td>
        <td>${fmt(n.last_seen)}</td>
        <td>${renderAction(n)}</td>
      </tr>
    `).join('');

    document.querySelector('#tasks tbody').innerHTML = (overview.tasks || []).map(t => `
      <tr>
        <td class="mono">${t.task_id}</td>
        <td>${t.task_name}</td>
        <td><span class="tag">${t.status}</span></td>
        <td>${t.priority}</td>
        <td>${fmt(t.created_at)}</td>
      </tr>
    `).join('');

    document.querySelector('#stages tbody').innerHTML = (overview.active_stages || []).map(st => `
      <tr>
        <td class="mono">${st.stage_id}</td>
        <td class="mono">${st.task_id}</td>
        <td>${st.capability}</td>
        <td><span class="tag ${st.status === 'claimed' ? 'warn' : 'ok'}">${st.status}</span></td>
        <td>${st.claimed_by || '-'}</td>
      </tr>
    `).join('');

    document.getElementById('events').textContent = (events.events || []).map(e =>
      `[${fmt(e.timestamp)}] ${e.type} ${JSON.stringify(e.payload)}`
    ).join('\n') || 'no events yet';

    document.querySelector('#endpoints tbody').innerHTML = (endpoints.endpoints || []).map(ep => `
      <tr>
        <td><span class="tag">${ep.method}</span></td>
        <td class="mono">${ep.path}</td>
        <td>${ep.auth}</td>
        <td>${ep.description}</td>
      </tr>
    `).join('');

    document.getElementById('status').textContent = 'updated ' + fmt(overview.generated_at);
  } catch (err) {
    document.getElementById('status').innerHTML = `<span class="error">error: ${err.message}</span>`;
    console.error(err);
  } finally {
    btn.disabled = false;
  }
}

document.addEventListener('DOMContentLoaded', () => {
    // Tabs
    document.querySelectorAll('.tab').forEach(el => {
        el.addEventListener('click', () => showTab(el.dataset.tab));
    });

    // Refresh
    document.getElementById('btnRefresh')?.addEventListener('click', loadAll);

    // Token overlay
    document.querySelector('.copy-token-btn')?.addEventListener('click', copyToken);
    document.querySelector('.close-token-btn')?.addEventListener('click', hideToken);
    document.getElementById('tokenOverlay')?.addEventListener('click', hideToken);

    // Permissions overlay
    document.querySelector('.save-perms-btn')?.addEventListener('click', saveGroupPerms);
    document.querySelector('.cancel-perms-btn')?.addEventListener('click', hidePermModal);
    document.getElementById('permOverlay')?.addEventListener('click', hidePermModal);

    // Create user form
    document.getElementById('createUserForm')?.addEventListener('submit', createUser);

    // Node actions (Event Delegation auf Container)
    document.addEventListener('click', (e) => {
        const btn = e.target.closest('.approve-btn');
        if (btn && btn.dataset.nodeId) { approveNode(btn.dataset.nodeId); return; }

        const tokenBtn = e.target.closest('.token-btn');
        if (tokenBtn && tokenBtn.dataset.nodeId) { newToken(tokenBtn.dataset.nodeId); return; }

        const delBtn = e.target.closest('.delete-btn');
        if (delBtn) { deleteNode(delBtn.dataset.nodeId, delBtn.dataset.nodeName); return; }
    });

    // User actions (Event Delegation)
    document.addEventListener('click', (e) => {
        const groupsBtn = e.target.closest('.save-groups-btn');
        if (groupsBtn) { updateUserGroups(groupsBtn.dataset.userId); return; }

        const resetBtn = e.target.closest('.reset-pw-btn');
        if (resetBtn) { resetPassword(resetBtn.dataset.userId); return; }

        const toggleBtn = e.target.closest('.toggle-active-btn');
        if (toggleBtn) { toggleActive(toggleBtn.dataset.userId, toggleBtn.dataset.active === 'true'); return; }

        const delUserBtn = e.target.closest('.delete-user-btn');
        if (delUserBtn) { deleteUser(delUserBtn.dataset.userId); return; }
    });

    // Group permissions
    document.addEventListener('click', (e) => {
        const editBtn = e.target.closest('.edit-perms-btn');
        if (editBtn) { editGroupPerms(editBtn.dataset.groupId, editBtn.dataset.groupName); return; }
    });

    // Initial load
    loadMe().then(() => { loadAll(); loadAdmin(); });
    setInterval(() => { loadAll(); if (!document.getElementById('viewAdmin').classList.contains('hidden')) loadAdmin(); }, 10000);
});