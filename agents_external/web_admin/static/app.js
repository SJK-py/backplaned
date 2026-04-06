// Agent Router Admin UI — app.js

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

async function api(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  if (r.status === 401) { showLogin(); return null; }
  return r;
}

function fmt(dt) {
  if (!dt) return '—';
  const d = new Date(dt);
  if (isNaN(d)) return dt;
  return d.toLocaleString();
}

function truncate(s, n = 40) {
  if (!s) return '—';
  s = String(s);
  return s.length > n ? s.slice(0, n) + '…' : s;
}

function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}

function statusBadge(status) {
  const map = { active: 'badge-active', completed: 'badge-completed', failed: 'badge-failed', timeout: 'badge-timeout' };
  return `<span class="badge ${map[status] || ''}">${esc(status)}</span>`;
}

function copyText(text) {
  navigator.clipboard.writeText(text).catch(() => {});
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

function showLogin() {
  document.getElementById('login-screen').style.display = 'flex';
  document.getElementById('app').classList.remove('visible');
}

function showApp() {
  document.getElementById('login-screen').style.display = 'none';
  document.getElementById('app').classList.add('visible');
  loadActiveTab();
}

async function checkAuth() {
  const r = await fetch('/ui/whoami');
  const d = await r.json();
  if (d.authenticated) showApp(); else showLogin();
}

document.getElementById('btn-login').addEventListener('click', async () => {
  const pw = document.getElementById('login-password').value;
  const r = await api('POST', '/ui/login', { password: pw });
  if (!r) return;
  if (r.ok) {
    document.getElementById('login-error').textContent = '';
    showApp();
  } else {
    document.getElementById('login-error').textContent = 'Invalid password.';
  }
});

document.getElementById('login-password').addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('btn-login').click();
});

document.getElementById('btn-logout').addEventListener('click', async () => {
  await api('POST', '/ui/logout');
  showLogin();
});

// ---------------------------------------------------------------------------
// Tab routing
// ---------------------------------------------------------------------------

let _activeTab = 'dashboard';

function switchTab(name) {
  _activeTab = name;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === `tab-${name}`));
  loadActiveTab();
}

function loadActiveTab() {
  if (_activeTab === 'dashboard') loadDashboard();
  else if (_activeTab === 'onboarding') { loadInvitations(); }
  else if (_activeTab === 'groups') loadGroupsTab();
  else if (_activeTab === 'acl') loadAclTab();
  else if (_activeTab === 'config') loadConfigTab();
  else if (_activeTab === 'agent') loadAgentTab();
  else if (_activeTab === 'log') loadLogTasks();
}

document.querySelectorAll('.tab-btn').forEach(b => {
  b.addEventListener('click', () => switchTab(b.dataset.tab));
});

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------

async function loadAgents() {
  const r = await api('GET', '/ui/agents');
  if (!r || !r.ok) return;
  const agents = await r.json();
  const tbody = document.getElementById('agents-tbody');
  if (!agents.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty-state">No agents registered.</td></tr>';
  } else {
    tbody.innerHTML = agents.map(a => {
      const info = a.agent_info || {};
      const desc = info.description || '—';
      const inG = (a.inbound_groups || []).join(', ') || '—';
      const outG = (a.outbound_groups || []).join(', ') || '—';
      const typeB = a.is_embedded
        ? '<span class="badge badge-embedded">embedded</span>'
        : a.is_alive === false
          ? '<span class="badge badge-danger">unavailable</span>'
          : '<span class="badge badge-external">external</span>';
      const disconnectBtn = a.is_embedded
        ? ''
        : `<button class="btn btn-secondary btn-sm" style="color:var(--danger,#e05)" onclick="disconnectAgent('${esc(a.agent_id)}')">Disconnect</button>`;
      const hasDoc = a.documentation_path ? '<span style="color:var(--green)">Yes</span>' : '<span style="color:var(--text-dim)">No</span>';
      const refreshBtn = `<button class="btn btn-secondary btn-sm" onclick="refreshAgentInfo('${esc(a.agent_id)}')" title="Re-fetch documentation from agent's documentation_url">Refresh Info</button>`;
      return `<tr>
        <td class="mono truncate" title="${esc(a.agent_id)}">${esc(a.agent_id)}</td>
        <td>${typeB}</td>
        <td>${esc(truncate(desc, 60))}</td>
        <td class="mono" style="font-size:11px">${esc(inG)} / ${esc(outG)}</td>
        <td><button class="btn btn-secondary btn-sm" onclick="openAgentDoc('${esc(a.agent_id)}')">${hasDoc}</button></td>
        <td>${fmt(a.registered_at)}</td>
        <td style="white-space:nowrap">${refreshBtn} ${disconnectBtn}</td>
      </tr>`;
    }).join('');
  }
  document.getElementById('agents-updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
}

async function loadTasks() {
  const status = document.getElementById('task-status-filter').value;
  const params = new URLSearchParams({ limit: 100 });
  if (status) params.set('status', status);
  const r = await api('GET', '/ui/tasks?' + params);
  if (!r || !r.ok) return;
  const tasks = await r.json();
  const tbody = document.getElementById('tasks-tbody');
  if (!tasks.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty-state">No tasks.</td></tr>';
  } else {
    tbody.innerHTML = tasks.map(t => `<tr>
      <td class="mono truncate" title="${esc(t.task_id)}">${esc(truncate(t.task_id, 20))}</td>
      <td class="mono truncate" title="${esc(t.origin_agent_id)}">${esc(truncate(t.origin_agent_id, 20))}</td>
      <td>${statusBadge(t.status)}</td>
      <td>${t.depth_count}</td>
      <td>${t.width_count}</td>
      <td>${fmt(t.created_at)}</td>
      <td>${fmt(t.timeout_at)}</td>
    </tr>`).join('');
  }
  document.getElementById('tasks-updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
}

async function loadProxyFiles() {
  const r = await api('GET', '/ui/proxy-files');
  if (!r || !r.ok) return;
  const files = await r.json();
  const tbody = document.getElementById('files-tbody');
  if (!files.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No proxy files.</td></tr>';
  } else {
    tbody.innerHTML = files.map(f => `<tr>
      <td>${esc(f.original_filename) || '—'}</td>
      <td class="mono" style="font-size:11px">${esc(f.file_key)}</td>
      <td class="mono truncate" title="${esc(f.task_id)}">${esc(truncate(f.task_id, 20))}</td>
      <td>${fmt(f.created_at)}</td>
      <td><button class="btn btn-secondary btn-sm" style="color:var(--danger,#e05)" onclick="deleteProxyFile('${esc(f.file_key)}')">Delete</button></td>
    </tr>`).join('');
  }
  document.getElementById('files-updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
}

function loadDashboard() {
  loadAgents();
  loadTasks();
  loadProxyFiles();
}

async function disconnectAgent(agentId) {
  if (!confirm(`Disconnect agent "${agentId}"?`)) return;
  const r = await api('DELETE', `/ui/agents/${encodeURIComponent(agentId)}`);
  if (r && r.ok) loadAgents();
}

async function deleteProxyFile(fileKey) {
  if (!confirm('Delete this proxy file?')) return;
  const r = await api('DELETE', `/ui/proxy-files/${encodeURIComponent(fileKey)}`);
  if (r && r.ok) loadProxyFiles();
}

async function deleteInvitationToken(token) {
  if (!confirm('Remove this token?')) return;
  const r = await api('DELETE', `/ui/invitation-tokens/${encodeURIComponent(token)}`);
  if (r && r.ok) loadInvitations();
}

async function openAgentDoc(agentId) {
  const area = document.getElementById('agent-doc-area');
  document.getElementById('doc-agent-id').textContent = agentId;
  document.getElementById('doc-save-result').innerHTML = '';
  area.dataset.agentId = agentId;

  // Fetch current doc content.
  const r = await api('GET', `/ui/agents/${encodeURIComponent(agentId)}/documentation`);
  if (!r || !r.ok) {
    document.getElementById('doc-content').value = '';
    document.getElementById('doc-source-url').textContent = '(unavailable)';
  } else {
    const d = await r.json();
    document.getElementById('doc-content').value = d.content || '';
    document.getElementById('doc-source-url').textContent = d.documentation_url || '(none)';
  }

  area.style.display = 'block';
  area.scrollIntoView({ behavior: 'smooth' });
}

document.getElementById('btn-save-doc').addEventListener('click', async () => {
  const agentId = document.getElementById('agent-doc-area').dataset.agentId;
  const content = document.getElementById('doc-content').value;
  const res = document.getElementById('doc-save-result');

  const r = await api('PUT', `/ui/agents/${encodeURIComponent(agentId)}/documentation`, { content });
  if (r && r.ok) {
    res.innerHTML = '<span style="color:var(--green);font-size:12px">Documentation saved.</span>';
    setTimeout(() => { res.innerHTML = ''; }, 3000);
    loadAgents();
  } else {
    const err = await r?.json().catch(() => ({}));
    res.innerHTML = `<span class="error-msg">${err?.detail || 'Save failed.'}</span>`;
  }
});

document.getElementById('btn-close-doc').addEventListener('click', () => {
  document.getElementById('agent-doc-area').style.display = 'none';
});

async function refreshAgentInfo(agentId) {
  const r = await api('POST', `/ui/agents/${encodeURIComponent(agentId)}/refresh-info`);
  if (!r) return;
  if (r.ok) {
    const d = await r.json();
    let msg;
    if (d.status === 'refreshed') {
      const parts = [];
      if (d.info_refreshed) parts.push('AgentInfo');
      if (d.doc_refreshed) parts.push('documentation');
      msg = `Refreshed: ${parts.join(' + ')}.`;
    } else {
      msg = 'No change.';
    }
    if (d.agent_signal_error) msg += `\nAgent signal error: ${d.agent_signal_error}`;
    alert(msg);
    loadAgents();
  } else {
    const err = await r.json().catch(() => ({}));
    alert(err.detail || 'Refresh failed.');
  }
}

window.openAgentDoc = openAgentDoc;
window.disconnectAgent = disconnectAgent;
window.deleteProxyFile = deleteProxyFile;
window.deleteInvitationToken = deleteInvitationToken;
window.refreshAgentInfo = refreshAgentInfo;

document.getElementById('refresh-agents').addEventListener('click', loadAgents);
document.getElementById('refresh-tasks').addEventListener('click', loadTasks);
document.getElementById('refresh-files').addEventListener('click', loadProxyFiles);
document.getElementById('task-status-filter').addEventListener('change', loadTasks);

// Auto-refresh dashboard every 15s when active
setInterval(() => { if (_activeTab === 'dashboard') loadDashboard(); }, 15000);

// ---------------------------------------------------------------------------
// Onboarding
// ---------------------------------------------------------------------------

async function loadInvitations() {
  const r = await api('GET', '/ui/invitations');
  if (!r || !r.ok) return;
  const invs = await r.json();
  const tbody = document.getElementById('invs-tbody');
  if (!invs.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty-state">No tokens yet.</td></tr>';
  } else {
    tbody.innerHTML = invs.map(inv => {
      const usedB = inv.used
        ? '<span class="badge badge-used">used</span>'
        : '<span class="badge badge-unused">available</span>';
      return `<tr>
        <td class="mono" style="font-size:11px;word-break:break-all;max-width:220px">${esc(inv.token)}</td>
        <td>${esc((inv.inbound_groups || []).join(', ')) || '—'}</td>
        <td>${esc((inv.outbound_groups || []).join(', ')) || '—'}</td>
        <td>${usedB}</td>
        <td>${fmt(inv.expires_at)}</td>
        <td>${fmt(inv.created_at)}</td>
        <td><button class="btn btn-secondary btn-sm" style="color:var(--danger,#e05)" onclick="deleteInvitationToken('${esc(inv.token)}')">Remove</button></td>
      </tr>`;
    }).join('');
  }
}

document.getElementById('btn-create-inv').addEventListener('click', async () => {
  const inbound = document.getElementById('inv-inbound').value.split(',').map(s => s.trim()).filter(Boolean);
  const outbound = document.getElementById('inv-outbound').value.split(',').map(s => s.trim()).filter(Boolean);
  const expires_in_hours = parseInt(document.getElementById('inv-expires').value) || 24;
  const r = await api('POST', '/ui/invitations', { inbound_groups: inbound, outbound_groups: outbound, expires_in_hours });
  if (!r || !r.ok) {
    document.getElementById('inv-result').innerHTML = '<p class="error-msg">Failed to create token.</p>';
    return;
  }
  const inv = await r.json();
  document.getElementById('inv-result').innerHTML = `
    <div class="token-box">
      <code id="new-token-value">${esc(inv.token)}</code>
      <button class="btn btn-secondary btn-sm" onclick="copyText(document.getElementById('new-token-value').textContent)">Copy</button>
    </div>
  `;
  loadInvitations();
});

document.getElementById('refresh-invs').addEventListener('click', loadInvitations);

// ---------------------------------------------------------------------------
// Groups Tab
// ---------------------------------------------------------------------------

async function loadGroupsTab() {
  const r = await api('GET', '/ui/agents');
  if (!r || !r.ok) return;
  const agents = await r.json();
  const tbody = document.getElementById('groups-tbody');
  if (!agents.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No agents registered.</td></tr>';
    return;
  }
  tbody.innerHTML = agents.map(a => {
    const typeB = a.is_embedded
      ? '<span class="badge badge-embedded">embedded</span>'
      : '<span class="badge badge-external">external</span>';
    const inG = (a.inbound_groups || []).join(', ');
    const outG = (a.outbound_groups || []).join(', ');
    return `<tr>
      <td class="mono" style="white-space:nowrap">${esc(a.agent_id)}</td>
      <td>${typeB}</td>
      <td><input type="text" class="groups-input" id="in-${esc(a.agent_id)}" value="${esc(inG)}" placeholder="group1, group2" style="width:100%" /></td>
      <td><input type="text" class="groups-input" id="out-${esc(a.agent_id)}" value="${esc(outG)}" placeholder="group1, group2" style="width:100%" /></td>
      <td><button class="btn btn-primary btn-sm" onclick="saveAgentGroups('${esc(a.agent_id)}')">Save</button></td>
    </tr>`;
  }).join('');
}

async function saveAgentGroups(agentId) {
  const inVal = document.getElementById(`in-${agentId}`).value;
  const outVal = document.getElementById(`out-${agentId}`).value;
  const inbound_groups = inVal.split(',').map(s => s.trim()).filter(Boolean);
  const outbound_groups = outVal.split(',').map(s => s.trim()).filter(Boolean);
  const r = await api('PATCH', `/ui/agents/${encodeURIComponent(agentId)}/groups`, { inbound_groups, outbound_groups });
  if (r && r.ok) {
    loadGroupsTab();
  } else {
    alert(`Failed to update groups for ${agentId}.`);
  }
}

window.saveAgentGroups = saveAgentGroups;
document.getElementById('refresh-groups').addEventListener('click', loadGroupsTab);

// ---------------------------------------------------------------------------
// ACL Tab
// ---------------------------------------------------------------------------

async function loadGroupAllowlist() {
  const r = await api('GET', '/ui/group-allowlist');
  if (!r || !r.ok) return;
  const rows = await r.json();
  const tbody = document.getElementById('acl-g-tbody');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="3" class="empty-state">No group rules.</td></tr>';
  } else {
    tbody.innerHTML = rows.map(row => `<tr>
      <td class="mono">${esc(row.inbound_group)}</td>
      <td class="mono">${esc(row.outbound_group)}</td>
      <td><button class="btn btn-secondary btn-sm" onclick="deleteGroupAcl('${esc(row.inbound_group)}','${esc(row.outbound_group)}')">Remove</button></td>
    </tr>`).join('');
  }
}

async function loadIndividualAllowlist() {
  const r = await api('GET', '/ui/individual-allowlist');
  if (!r || !r.ok) return;
  const rows = await r.json();
  const tbody = document.getElementById('acl-i-tbody');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="3" class="empty-state">No individual rules.</td></tr>';
  } else {
    tbody.innerHTML = rows.map(row => `<tr>
      <td class="mono truncate" title="${esc(row.agent_id)}">${esc(row.agent_id)}</td>
      <td class="mono truncate" title="${esc(row.destination_agent_id)}">${esc(row.destination_agent_id)}</td>
      <td><button class="btn btn-secondary btn-sm" onclick="deleteIndividualAcl('${esc(row.agent_id)}','${esc(row.destination_agent_id)}')">Remove</button></td>
    </tr>`).join('');
  }
}

function loadAclTab() {
  loadGroupAllowlist();
  loadIndividualAllowlist();
}

async function deleteGroupAcl(inbound_group, outbound_group) {
  const r = await api('DELETE', '/ui/group-allowlist', { inbound_group, outbound_group });
  if (r && r.ok) loadGroupAllowlist();
}

async function deleteIndividualAcl(agent_id, destination_agent_id) {
  const r = await api('DELETE', '/ui/individual-allowlist', { agent_id, destination_agent_id });
  if (r && r.ok) loadIndividualAllowlist();
}

window.deleteGroupAcl = deleteGroupAcl;
window.deleteIndividualAcl = deleteIndividualAcl;

document.getElementById('btn-add-group-acl').addEventListener('click', async () => {
  const inbound_group = document.getElementById('acl-g-inbound').value.trim();
  const outbound_group = document.getElementById('acl-g-outbound').value.trim();
  const res = document.getElementById('acl-g-result');
  if (!inbound_group || !outbound_group) {
    res.innerHTML = '<p class="error-msg">Both fields are required.</p>';
    return;
  }
  const r = await api('POST', '/ui/group-allowlist', { inbound_group, outbound_group });
  if (!r || !r.ok) {
    res.innerHTML = '<p class="error-msg">Failed to add rule.</p>';
    return;
  }
  res.innerHTML = '';
  document.getElementById('acl-g-inbound').value = '';
  document.getElementById('acl-g-outbound').value = '';
  loadGroupAllowlist();
});

document.getElementById('btn-add-ind-acl').addEventListener('click', async () => {
  const agent_id = document.getElementById('acl-i-agent').value.trim();
  const destination_agent_id = document.getElementById('acl-i-dest').value.trim();
  const res = document.getElementById('acl-i-result');
  if (!agent_id || !destination_agent_id) {
    res.innerHTML = '<p class="error-msg">Both fields are required.</p>';
    return;
  }
  const r = await api('POST', '/ui/individual-allowlist', { agent_id, destination_agent_id });
  if (!r || !r.ok) {
    res.innerHTML = '<p class="error-msg">Failed to add rule.</p>';
    return;
  }
  res.innerHTML = '';
  document.getElementById('acl-i-agent').value = '';
  document.getElementById('acl-i-dest').value = '';
  loadIndividualAllowlist();
});

// ---------------------------------------------------------------------------
// Config Tab
// ---------------------------------------------------------------------------

async function loadConfigTab() {
  const r = await api('GET', '/ui/agents');
  if (!r || !r.ok) return;
  const agents = await r.json();
  const sel = document.getElementById('config-agent-select');
  const current = sel.value;
  sel.innerHTML = '<option value="">— select agent —</option>';
  agents.filter(a => a.is_embedded).forEach(a => {
    const opt = document.createElement('option');
    opt.value = a.agent_id;
    opt.textContent = a.agent_id;
    sel.appendChild(opt);
  });
  if (current) {
    sel.value = current;
    if (sel.value === current) loadAgentConfig(current);
  }
}

async function loadAgentConfig(agentId) {
  const area = document.getElementById('config-editor-area');
  if (!agentId) { area.style.display = 'none'; return; }
  area.style.display = 'block';
  document.getElementById('config-save-result').innerHTML = '';

  const [cfgR, exR] = await Promise.all([
    api('GET', `/ui/agent-config/${encodeURIComponent(agentId)}`),
    api('GET', `/ui/agent-config/${encodeURIComponent(agentId)}/example`),
  ]);

  if (cfgR && cfgR.ok) {
    const cfg = await cfgR.json();
    document.getElementById('config-json-editor').value = JSON.stringify(cfg, null, 2);
  } else {
    document.getElementById('config-json-editor').value = '{}';
  }

  if (exR && exR.ok) {
    const ex = await exR.json();
    document.getElementById('config-example-display').textContent = JSON.stringify(ex, null, 2);
  } else {
    document.getElementById('config-example-display').textContent = '(no config.example available)';
  }
}

document.getElementById('config-agent-select').addEventListener('change', function () {
  loadAgentConfig(this.value);
});

document.getElementById('refresh-config-agents').addEventListener('click', loadConfigTab);

document.getElementById('btn-save-config').addEventListener('click', async () => {
  const agentId = document.getElementById('config-agent-select').value;
  if (!agentId) return;
  const editor = document.getElementById('config-json-editor');
  const res = document.getElementById('config-save-result');
  let parsed;
  try {
    parsed = JSON.parse(editor.value);
  } catch (e) {
    res.innerHTML = '<span class="error-msg">Invalid JSON.</span>';
    return;
  }
  const r = await api('PUT', `/ui/agent-config/${encodeURIComponent(agentId)}`, parsed);
  if (r && r.ok) {
    res.innerHTML = '<span style="color:var(--green);font-size:12px">Config saved. Restart router to apply.</span>';
    setTimeout(() => { res.innerHTML = ''; }, 4000);
  } else {
    const err = await r?.json().catch(() => ({}));
    res.innerHTML = `<span class="error-msg">${err?.detail || 'Save failed.'}</span>`;
  }
});

// ---------------------------------------------------------------------------
// Admin Agent Tab
// ---------------------------------------------------------------------------

// Mirrors the schema parser from helper.py, implemented in JS.

function parseInputSchema(schemaStr) {
  if (!schemaStr || !schemaStr.trim()) return [];
  const parts = [];
  let depth = 0, cur = '';
  for (const ch of schemaStr) {
    if (ch === '[') { depth++; cur += ch; }
    else if (ch === ']') { depth--; cur += ch; }
    else if (ch === ',' && depth === 0) { parts.push(cur.trim()); cur = ''; }
    else cur += ch;
  }
  if (cur.trim()) parts.push(cur.trim());

  return parts.map(part => {
    part = part.trim();
    if (!part) return null;
    if (!part.includes(':')) return { name: part, type: 'str', required: true };
    const colonIdx = part.indexOf(':');
    const name = part.slice(0, colonIdx).trim();
    const type = part.slice(colonIdx + 1).trim();
    const required = !/^Optional\[/.test(type);
    return { name, type, required };
  }).filter(Boolean);
}

function isLLMData(typeStr) {
  return typeStr === 'LLMData';
}

function renderDynamicFields(fields, required_input) {
  const container = document.getElementById('dynamic-fields');
  if (!fields.length) { container.innerHTML = ''; return; }

  const rows = fields.map(field => {
    const req = required_input.includes(field.name) || field.required;
    const reqMark = req ? ' <span class="required">*</span>' : '';

    if (isLLMData(field.type)) {
      return `
        <div class="dynamic-field">
          <label>${field.name}${reqMark} <span style="color:var(--text-dim);font-size:11px">(LLMData)</span></label>
          <div class="llmdata-group">
            <span class="sub-label">agent_instruction (optional)</span>
            <textarea data-field="${field.name}.agent_instruction" rows="2" placeholder="System-level instruction for the LLM…"></textarea>
            <span class="sub-label" style="margin-top:.5rem">context (optional)</span>
            <textarea data-field="${field.name}.context" rows="2" placeholder="Background context…"></textarea>
            <span class="sub-label" style="margin-top:.5rem">prompt <span class="required">*</span></span>
            <textarea data-field="${field.name}.prompt" rows="3" placeholder="The user-facing prompt…"></textarea>
          </div>
        </div>`;
    }

    // bool
    if (field.type === 'bool' || field.type === 'Optional[bool]') {
      return `<div class="dynamic-field">
        <label><input type="checkbox" data-field="${field.name}" data-type="bool" /> ${field.name}${reqMark}</label>
      </div>`;
    }

    // int / float / number
    if (/^(int|float|number)/.test(field.type)) {
      return `<div class="dynamic-field">
        <label>${field.name}${reqMark} <span style="color:var(--text-dim);font-size:11px">(${field.type})</span></label>
        <input type="number" data-field="${field.name}" data-type="number" placeholder="${field.name}" style="width:200px" />
      </div>`;
    }

    // default: text / textarea
    const multiline = /dict|object|str/.test(field.type) && !/List/.test(field.type);
    const input = multiline
      ? `<textarea data-field="${field.name}" rows="3" placeholder="${field.name}…"></textarea>`
      : `<input type="text" data-field="${field.name}" placeholder="${field.name}" style="min-width:280px" />`;

    return `<div class="dynamic-field">
      <label>${field.name}${reqMark} <span style="color:var(--text-dim);font-size:11px">(${field.type})</span></label>
      ${input}
    </div>`;
  }).join('');

  container.innerHTML = rows;
}

function collectPayload() {
  const payload = {};
  // Collect all regular fields
  document.querySelectorAll('#dynamic-fields [data-field]').forEach(el => {
    const key = el.dataset.field;
    if (key.includes('.')) {
      // Nested (LLMData sub-fields)
      const [parent, child] = key.split('.');
      if (!payload[parent]) payload[parent] = {};
      if (el.value.trim()) payload[parent][child] = el.value.trim();
    } else if (el.dataset.type === 'bool') {
      payload[key] = el.checked;
    } else if (el.dataset.type === 'number') {
      if (el.value.trim()) payload[key] = Number(el.value);
    } else {
      if (el.value.trim()) payload[key] = el.value.trim();
    }
  });
  return payload;
}

let _agentList = [];

async function loadAgentTab() {
  const r = await api('GET', '/ui/agents');
  if (!r || !r.ok) return;
  _agentList = await r.json();
  const sel = document.getElementById('agent-target-select');
  sel.innerHTML = '<option value="">— select agent —</option>';
  _agentList.forEach(a => {
    const info = a.agent_info || {};
    const label = info.description ? `${a.agent_id} — ${truncate(info.description, 40)}` : a.agent_id;
    const opt = document.createElement('option');
    opt.value = a.agent_id;
    opt.textContent = label;
    sel.appendChild(opt);
  });
}

document.getElementById('refresh-agent-list').addEventListener('click', loadAgentTab);

document.getElementById('agent-target-select').addEventListener('change', function () {
  const agentId = this.value;
  const infoDiv = document.getElementById('agent-schema-info');
  const sendArea = document.getElementById('send-task-area');
  const dynFields = document.getElementById('dynamic-fields');
  const resultArea = document.getElementById('task-result-area');
  resultArea.style.display = 'none';

  if (!agentId) {
    infoDiv.style.display = 'none';
    sendArea.style.display = 'none';
    dynFields.innerHTML = '';
    return;
  }

  const agent = _agentList.find(a => a.agent_id === agentId);
  const info = agent ? (agent.agent_info || {}) : {};

  document.getElementById('schema-description').textContent = info.description || '—';
  document.getElementById('schema-input').textContent = info.input_schema || '—';
  document.getElementById('schema-required').textContent = (info.required_input || []).join(', ') || '—';
  infoDiv.style.display = 'block';

  const fields = parseInputSchema(info.input_schema || '');
  renderDynamicFields(fields, info.required_input || []);
  sendArea.style.display = 'block';
});

document.getElementById('btn-send-task').addEventListener('click', async () => {
  const agentId = document.getElementById('agent-target-select').value;
  if (!agentId) return;

  const btn = document.getElementById('btn-send-task');
  btn.disabled = true;

  const payload = collectPayload();
  const resultArea = document.getElementById('task-result-area');
  const resultStatus = document.getElementById('task-result-status');
  const resultContent = document.getElementById('task-result-content');

  resultArea.style.display = 'block';
  resultStatus.innerHTML = '<span class="spinner"></span> Sending task…';
  resultContent.textContent = '';

  const r = await api('POST', '/ui/send-task', { target_agent_id: agentId, payload });
  if (!r || !r.ok) {
    resultStatus.textContent = 'Failed to send task.';
    btn.disabled = false;
    return;
  }
  const { task_id } = await r.json();
  resultStatus.innerHTML = `<span class="spinner"></span> Waiting for result… <span class="mono" style="font-size:11px">${esc(task_id)}</span>`;

  // Long-poll for result
  const pollR = await api('GET', `/ui/task-result/${task_id}?timeout=60`);
  if (!pollR.ok) {
    resultStatus.textContent = 'Error waiting for result.';
    btn.disabled = false;
    return;
  }
  const result = await pollR.json();
  if (result.status === 'pending') {
    resultStatus.textContent = 'Timed out waiting for result. The task is still active.';
    resultContent.textContent = JSON.stringify(result, null, 2);
    btn.disabled = false;
    return;
  }

  const statusCode = result.status_code;
  const output = result.payload || {};
  resultStatus.textContent = `Result received (status ${statusCode || '?'})`;
  resultContent.textContent = output.content || JSON.stringify(output, null, 2);
  btn.disabled = false;
});

// ---------------------------------------------------------------------------
// Log Tab
// ---------------------------------------------------------------------------

let _logSelectedTask = null;

async function loadLogTasks() {
  const r = await api('GET', '/ui/tasks?limit=200');
  if (!r || !r.ok) return;
  const tasks = await r.json();
  const tbody = document.getElementById('log-tasks-tbody');
  if (!tasks.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-state">No tasks.</td></tr>';
  } else {
    tbody.innerHTML = tasks.map(t => `<tr style="cursor:pointer" onclick="loadTaskEvents('${esc(t.task_id)}')">
      <td class="mono" style="font-size:11px"><a class="task-link">${esc(truncate(t.task_id, 30))}</a></td>
      <td class="mono" style="font-size:11px">${esc(truncate(t.origin_agent_id, 25))}</td>
      <td>${statusBadge(t.status)}</td>
      <td>${fmt(t.created_at)}</td>
    </tr>`).join('');
  }
}

async function loadTaskEvents(taskId) {
  _logSelectedTask = taskId;
  document.getElementById('log-selected-task').textContent = taskId;
  document.getElementById('log-events-section').style.display = 'block';

  const r = await api('GET', `/ui/events/${taskId}`);
  if (!r || !r.ok) {
    document.getElementById('log-events-list').innerHTML = '<div class="empty-state error-msg">Failed to load events.</div>';
    return;
  }
  const events = await r.json();
  const list = document.getElementById('log-events-list');
  if (!events.length) {
    list.innerHTML = '<div class="empty-state">No events for this task.</div>';
    return;
  }
  list.innerHTML = events.map(ev => {
    let agentLine = esc(ev.agent_id);
    if (ev.destination_agent_id) agentLine += ` → ${esc(ev.destination_agent_id)}`;
    let payloadStr = '';
    try {
      const p = typeof ev.payload === 'string' ? JSON.parse(ev.payload) : ev.payload;
      payloadStr = JSON.stringify(p, null, 2);
    } catch { payloadStr = String(ev.payload || ''); }

    return `<div class="event-item">
      <div class="event-header">
        <span class="event-type">${esc(ev.event_type)}</span>
        <span class="event-agents mono">${agentLine}</span>
        ${ev.status_code ? `<span class="badge ${ev.status_code < 400 ? 'badge-completed' : 'badge-failed'}">${ev.status_code}</span>` : ''}
        <span class="event-time">${fmt(ev.timestamp)}</span>
      </div>
      <div class="event-payload">${esc(payloadStr)}</div>
    </div>`;
  }).join('');
}

// Expose for inline onclick
window.loadTaskEvents = loadTaskEvents;

document.getElementById('refresh-log-tasks').addEventListener('click', loadLogTasks);
document.getElementById('refresh-log-events').addEventListener('click', () => {
  if (_logSelectedTask) loadTaskEvents(_logSelectedTask);
});

document.getElementById('btn-clear-log').addEventListener('click', async () => {
  if (!confirm('Clear all completed, failed, and timed-out tasks? Active tasks will not be removed.')) return;
  const r = await api('DELETE', '/ui/log');
  if (!r || !r.ok) return;
  const { deleted_tasks } = await r.json();
  _logSelectedTask = null;
  document.getElementById('log-events-section').style.display = 'none';
  loadLogTasks();
  alert(`Cleared ${deleted_tasks} task(s).`);
});

// Auto-refresh log every 10s when active
setInterval(() => {
  if (_activeTab === 'log') {
    loadLogTasks();
    if (_logSelectedTask) loadTaskEvents(_logSelectedTask);
  }
}, 10000);

// ---------------------------------------------------------------------------
// Change Password
// ---------------------------------------------------------------------------

document.getElementById('btn-chpw').addEventListener('click', async () => {
  const cur = document.getElementById('chpw-current').value;
  const nw = document.getElementById('chpw-new').value;
  const res = document.getElementById('chpw-result');
  if (!cur || !nw) { res.innerHTML = '<p style="color:var(--danger,#e05);font-size:13px">Both fields required.</p>'; return; }
  const r = await api('POST', '/ui/change-password', { current_password: cur, new_password: nw });
  if (!r) return;
  if (r.ok) {
    res.innerHTML = '<p style="color:var(--green,#0c6);font-size:13px">Password changed successfully.</p>';
    document.getElementById('chpw-current').value = '';
    document.getElementById('chpw-new').value = '';
  } else {
    const err = await r.json().catch(() => ({}));
    res.innerHTML = `<p style="color:var(--danger,#e05);font-size:13px">${err.detail || 'Error changing password.'}</p>`;
  }
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

checkAuth();
