// MCP Agent Admin UI — app.js

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

function truncate(s, n = 50) {
  if (!s) return '—';
  s = String(s);
  return s.length > n ? s.slice(0, n) + '...' : s;
}

function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}

function statusBadge(status) {
  const map = {
    connected: 'badge-connected',
    disconnected: 'badge-disconnected',
    error: 'badge-error',
  };
  return `<span class="badge ${map[status] || ''}">${status}</span>`;
}

function enabledBadge(enabled) {
  return enabled
    ? '<span class="badge badge-enabled">enabled</span>'
    : '<span class="badge badge-disabled">disabled</span>';
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

let _activeTab = 'status';

function switchTab(name) {
  _activeTab = name;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === `tab-${name}`));
  loadActiveTab();
}

function loadActiveTab() {
  if (_activeTab === 'status') loadStatus();
  else if (_activeTab === 'servers') loadServers();
  else if (_activeTab === 'tools') loadTools();
  else if (_activeTab === 'setup') loadSetup();
  else if (_activeTab === 'logs') loadLogs();
}

document.querySelectorAll('.tab-btn').forEach(b => {
  b.addEventListener('click', () => switchTab(b.dataset.tab));
});

// ---------------------------------------------------------------------------
// Status Tab
// ---------------------------------------------------------------------------

async function loadStatus() {
  const r = await api('GET', '/ui/status');
  if (!r || !r.ok) return;
  const d = await r.json();

  const grid = document.getElementById('status-grid');
  grid.innerHTML = `
    <div class="status-card">
      <div class="label">Router</div>
      <div class="value ${d.router_connected ? 'ok' : 'bad'}">${d.router_connected ? 'Connected' : 'Disconnected'}</div>
      ${d.agent_id ? `<div style="font-size:11px;color:var(--text-dim);margin-top:.3rem" class="mono">${esc(d.agent_id)}</div>` : ''}
    </div>
    <div class="status-card">
      <div class="label">MCP Servers</div>
      <div class="value ${d.servers_connected === d.servers_total ? 'ok' : 'warn'}">${d.servers_connected} / ${d.servers_total}</div>
    </div>
    <div class="status-card">
      <div class="label">Total Tools</div>
      <div class="value">${d.total_tools}</div>
    </div>
  `;

  const infoBox = document.getElementById('status-agent-info');
  const info = d.agent_info || {};
  infoBox.innerHTML = `
    <p><strong>Description:</strong> ${esc(truncate(info.description, 200))}</p>
    <p><strong>Input Schema:</strong> <code>${esc(info.input_schema || '—')}</code></p>
    <p><strong>Output Schema:</strong> <code>${esc(info.output_schema || '—')}</code></p>
    <p><strong>Required Input:</strong> <code>${esc((info.required_input || []).join(', ') || '—')}</code></p>
  `;
}

// ---------------------------------------------------------------------------
// MCP Servers Tab
// ---------------------------------------------------------------------------

async function loadServers() {
  const r = await api('GET', '/ui/servers');
  if (!r || !r.ok) return;
  const servers = await r.json();
  const tbody = document.getElementById('servers-tbody');
  const names = Object.keys(servers);

  if (!names.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty-state">No servers configured. Add one above.</td></tr>';
    return;
  }

  tbody.innerHTML = names.map(name => {
    const s = servers[name];
    const cfg = s.config;
    const transport = cfg.transport_type || 'auto';
    const target = cfg.command
      ? `${cfg.command} ${(cfg.args || []).join(' ')}`
      : cfg.url || '—';
    return `<tr>
      <td class="mono">${esc(name)}</td>
      <td>${esc(transport)}</td>
      <td class="truncate" title="${esc(target)}" style="max-width:300px">${esc(truncate(target, 50))}</td>
      <td>${statusBadge(s.status)}${s.error ? ` <span style="font-size:11px;color:var(--red)" title="${esc(s.error)}">(!)</span>` : ''}</td>
      <td>${s.tool_count}</td>
      <td>${enabledBadge(cfg.enabled)}</td>
      <td style="white-space:nowrap">
        <button class="btn btn-secondary btn-sm" onclick="reconnectServer('${esc(name)}')">Reconnect</button>
        <button class="btn btn-secondary btn-sm" onclick="toggleServer('${esc(name)}')">${cfg.enabled ? 'Disable' : 'Enable'}</button>
        <button class="btn btn-secondary btn-sm btn-danger" onclick="removeServer('${esc(name)}')">Remove</button>
      </td>
    </tr>`;
  }).join('');
}

async function reconnectServer(name) {
  const r = await api('POST', `/ui/servers/${encodeURIComponent(name)}/reconnect`);
  if (r && r.ok) loadServers();
}

async function toggleServer(name) {
  const r = await api('POST', `/ui/servers/${encodeURIComponent(name)}/toggle`);
  if (r && r.ok) loadServers();
}

async function removeServer(name) {
  if (!confirm(`Remove MCP server "${name}"?`)) return;
  const r = await api('DELETE', `/ui/servers/${encodeURIComponent(name)}`);
  if (r && r.ok) loadServers();
}

window.reconnectServer = reconnectServer;
window.toggleServer = toggleServer;
window.removeServer = removeServer;

document.getElementById('btn-add-server').addEventListener('click', async () => {
  const name = document.getElementById('srv-name').value.trim();
  const res = document.getElementById('add-server-result');
  if (!name) { res.innerHTML = '<p class="error-msg">Name is required.</p>'; return; }

  let headers = {};
  const headersStr = document.getElementById('srv-headers').value.trim();
  if (headersStr) {
    try { headers = JSON.parse(headersStr); } catch { res.innerHTML = '<p class="error-msg">Invalid headers JSON.</p>'; return; }
  }

  let env = {};
  const envStr = document.getElementById('srv-env').value.trim();
  if (envStr) {
    try { env = JSON.parse(envStr); } catch { res.innerHTML = '<p class="error-msg">Invalid env JSON.</p>'; return; }
  }

  const argsStr = document.getElementById('srv-args').value.trim();
  const args = argsStr ? argsStr.split(',').map(s => s.trim()).filter(Boolean) : [];

  const enabledToolsStr = document.getElementById('srv-enabled-tools').value.trim();
  const enabled_tools = enabledToolsStr ? enabledToolsStr.split(',').map(s => s.trim()).filter(Boolean) : ['*'];

  const body = {
    name,
    transport_type: document.getElementById('srv-transport').value || null,
    command: document.getElementById('srv-command').value.trim(),
    args,
    env,
    url: document.getElementById('srv-url').value.trim(),
    headers,
    enabled_tools,
    tool_timeout: parseInt(document.getElementById('srv-timeout').value) || 30,
    enabled: true,
  };

  const r = await api('POST', '/ui/servers', body);
  if (!r) return;
  if (r.ok) {
    res.innerHTML = '<p style="color:var(--green);font-size:13px">Server added.</p>';
    document.getElementById('srv-name').value = '';
    document.getElementById('srv-command').value = '';
    document.getElementById('srv-args').value = '';
    document.getElementById('srv-url').value = '';
    document.getElementById('srv-headers').value = '';
    document.getElementById('srv-env').value = '';
    loadServers();
    setTimeout(() => { res.innerHTML = ''; }, 3000);
  } else {
    const err = await r.json().catch(() => ({}));
    res.innerHTML = `<p class="error-msg">${err.detail || 'Failed to add server.'}</p>`;
  }
});

document.getElementById('refresh-servers').addEventListener('click', loadServers);

// ---------------------------------------------------------------------------
// Tools Tab
// ---------------------------------------------------------------------------

let _allTools = [];
let _testToolNamespaced = '';

async function loadTools() {
  const r = await api('GET', '/ui/tools');
  if (!r || !r.ok) return;
  _allTools = await r.json();
  renderTools(_allTools);

  // Populate server filter.
  const servers = [...new Set(_allTools.map(t => t.server))];
  const sel = document.getElementById('tool-server-filter');
  const current = sel.value;
  sel.innerHTML = '<option value="">All servers</option>';
  servers.forEach(s => {
    const opt = document.createElement('option');
    opt.value = s;
    opt.textContent = s;
    sel.appendChild(opt);
  });
  sel.value = current;
}

function renderTools(tools) {
  const tbody = document.getElementById('tools-tbody');
  if (!tools.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-state">No tools available. Connect MCP servers first.</td></tr>';
    return;
  }

  tbody.innerHTML = tools.map((t, i) => {
    const schemaId = `schema-${i}`;
    const briefDisplay = t.brief ? esc(t.brief) : '<span style="color:var(--text-dim)">(none)</span>';
    const isOverridden = t.admin_brief ? ' <span title="Admin override" style="color:var(--accent);cursor:help">✎</span>' : '';
    const hasDoc = (t.admin_doc || t.llm_doc) ? ` <span title="${t.admin_doc ? 'Admin docs' : 'LLM-generated docs'}" style="color:${t.admin_doc ? 'var(--green)' : 'var(--text-dim)'};cursor:help">📄</span>` : '';
    return `<tr>
      <td class="mono" style="font-size:12px">${esc(t.name)}</td>
      <td class="mono" style="font-size:11px">${esc(t.server)}</td>
      <td style="font-size:12px">${briefDisplay}${isOverridden}${hasDoc}</td>
      <td>
        <span class="schema-toggle" onclick="toggleSchema('${schemaId}')">Show</span>
        <div class="schema-content" id="${schemaId}">${esc(JSON.stringify(t.input_schema, null, 2))}</div>
      </td>
      <td style="white-space:nowrap">
        <button class="btn btn-secondary btn-sm" onclick="openEditBrief('${esc(t.name)}')">Brief</button>
        <button class="btn btn-secondary btn-sm" onclick="openEditDoc('${esc(t.name)}')">Docs</button>
        <button class="btn btn-secondary btn-sm" onclick="openTestTool('${esc(t.server)}', '${esc(t.raw_name)}', '${esc(t.name)}')">Test</button>
      </td>
    </tr>`;
  }).join('');
}

function toggleSchema(id) {
  const el = document.getElementById(id);
  if (el) el.classList.toggle('open');
}

function openTestTool(server, rawName, namespacedName) {
  _testToolNamespaced = namespacedName;
  document.getElementById('test-tool-name').textContent = namespacedName;
  document.getElementById('test-tool-args').value = '';
  document.getElementById('test-result').style.display = 'none';
  document.getElementById('tool-test-area').style.display = 'block';
  document.getElementById('tool-test-area').scrollIntoView({ behavior: 'smooth' });
}

// ---------------------------------------------------------------------------
// Edit Brief
// ---------------------------------------------------------------------------

function openEditBrief(toolName) {
  const tool = _allTools.find(t => t.name === toolName);
  if (!tool) return;
  document.getElementById('edit-brief-tool-name').textContent = toolName;
  document.getElementById('edit-brief-llm').textContent = tool.llm_brief || '(none)';
  document.getElementById('edit-brief-input').value = tool.admin_brief || '';
  document.getElementById('edit-brief-result').innerHTML = '';
  document.getElementById('edit-brief-area').style.display = 'block';
  document.getElementById('edit-brief-area').scrollIntoView({ behavior: 'smooth' });
  document.getElementById('edit-brief-area').dataset.toolName = toolName;
}

document.getElementById('btn-save-brief').addEventListener('click', async () => {
  const toolName = document.getElementById('edit-brief-area').dataset.toolName;
  const input = document.getElementById('edit-brief-input').value.trim();
  const res = document.getElementById('edit-brief-result');

  const r = await api('POST', '/ui/tools/brief', {
    tool_name: toolName,
    brief: input || null,
  });
  if (r && r.ok) {
    res.innerHTML = '<span style="color:var(--green);font-size:12px">Saved. AgentInfo updated.</span>';
    setTimeout(() => { res.innerHTML = ''; }, 3000);
    loadTools();
  } else {
    const err = await r?.json().catch(() => ({}));
    res.innerHTML = `<span class="error-msg">${err?.detail || 'Save failed.'}</span>`;
  }
});

document.getElementById('btn-clear-brief').addEventListener('click', async () => {
  const toolName = document.getElementById('edit-brief-area').dataset.toolName;
  const res = document.getElementById('edit-brief-result');
  document.getElementById('edit-brief-input').value = '';

  const r = await api('POST', '/ui/tools/brief', { tool_name: toolName, brief: null });
  if (r && r.ok) {
    res.innerHTML = '<span style="color:var(--green);font-size:12px">Override cleared. Using LLM brief.</span>';
    setTimeout(() => { res.innerHTML = ''; }, 3000);
    loadTools();
  }
});

// ---------------------------------------------------------------------------
// Edit Documentation
// ---------------------------------------------------------------------------

function openEditDoc(toolName) {
  const tool = _allTools.find(t => t.name === toolName);
  if (!tool) return;
  document.getElementById('edit-doc-tool-name').textContent = toolName;
  document.getElementById('edit-doc-input').value = tool.admin_doc || '';
  document.getElementById('edit-doc-result').innerHTML = '';
  // Show LLM-generated doc preview if available.
  let llmDocEl = document.getElementById('edit-doc-llm-preview');
  if (!llmDocEl) {
    const container = document.getElementById('edit-doc-input').parentElement;
    const div = document.createElement('div');
    div.id = 'edit-doc-llm-preview';
    div.style.cssText = 'font-size:12px;color:var(--text-dim);margin-bottom:.5rem;max-height:200px;overflow-y:auto;white-space:pre-wrap;font-family:monospace;font-size:11px;background:var(--bg);padding:.5rem;border-radius:4px;border:1px solid var(--border)';
    container.insertBefore(div, container.firstChild);
    llmDocEl = div;
  }
  if (tool.llm_doc) {
    llmDocEl.style.display = 'block';
    llmDocEl.innerHTML = '<strong>LLM-generated:</strong>\n' + tool.llm_doc.replace(/</g, '&lt;').replace(/>/g, '&gt;');
  } else {
    llmDocEl.style.display = 'none';
  }
  document.getElementById('edit-doc-area').style.display = 'block';
  document.getElementById('edit-doc-area').scrollIntoView({ behavior: 'smooth' });
  document.getElementById('edit-doc-area').dataset.toolName = toolName;
}

document.getElementById('btn-save-doc').addEventListener('click', async () => {
  const toolName = document.getElementById('edit-doc-area').dataset.toolName;
  const input = document.getElementById('edit-doc-input').value.trim();
  const res = document.getElementById('edit-doc-result');

  const r = await api('POST', '/ui/tools/doc', {
    tool_name: toolName,
    doc: input || null,
  });
  if (r && r.ok) {
    res.innerHTML = '<span style="color:var(--green);font-size:12px">Documentation saved.</span>';
    setTimeout(() => { res.innerHTML = ''; }, 3000);
    loadTools();
  } else {
    const err = await r?.json().catch(() => ({}));
    res.innerHTML = `<span class="error-msg">${err?.detail || 'Save failed.'}</span>`;
  }
});

document.getElementById('btn-clear-doc').addEventListener('click', async () => {
  const toolName = document.getElementById('edit-doc-area').dataset.toolName;
  const res = document.getElementById('edit-doc-result');
  document.getElementById('edit-doc-input').value = '';

  const r = await api('POST', '/ui/tools/doc', { tool_name: toolName, doc: null });
  if (r && r.ok) {
    res.innerHTML = '<span style="color:var(--green);font-size:12px">Documentation cleared.</span>';
    setTimeout(() => { res.innerHTML = ''; }, 3000);
    loadTools();
  }
});

window.toggleSchema = toggleSchema;
window.openTestTool = openTestTool;
window.openEditBrief = openEditBrief;
window.openEditDoc = openEditDoc;

document.getElementById('btn-run-test').addEventListener('click', async () => {
  if (!_testToolNamespaced) return;

  let args = {};
  const argsStr = document.getElementById('test-tool-args').value.trim();
  if (argsStr) {
    try { args = JSON.parse(argsStr); } catch {
      document.getElementById('test-result').style.display = 'block';
      document.getElementById('test-result-status').textContent = 'Error';
      document.getElementById('test-result-content').textContent = 'Invalid JSON arguments.';
      return;
    }
  }

  const parts = _testToolNamespaced.split('__');
  const server = parts[0];
  const rawName = parts.slice(1).join('__');

  document.getElementById('test-result').style.display = 'block';
  document.getElementById('test-result-status').innerHTML = '<span class="spinner"></span> Running...';
  document.getElementById('test-result-content').textContent = '';

  const r = await api('POST', `/ui/servers/${encodeURIComponent(server)}/tools/${encodeURIComponent(rawName)}/test`, { arguments: args });
  if (!r) return;
  const d = await r.json();
  document.getElementById('test-result-status').textContent = d.status === 'ok' ? 'Success' : 'Error';
  document.getElementById('test-result-content').textContent = d.result || '(no output)';
});

document.getElementById('tool-server-filter').addEventListener('change', function () {
  const server = this.value;
  const filtered = server ? _allTools.filter(t => t.server === server) : _allTools;
  renderTools(filtered);
});

document.getElementById('refresh-tools').addEventListener('click', loadTools);

// ---------------------------------------------------------------------------
// Setup Tab
// ---------------------------------------------------------------------------

async function loadSetup() {
  // Onboarding status.
  const r1 = await api('GET', '/ui/onboarding');
  if (r1 && r1.ok) {
    const d = await r1.json();
    document.getElementById('router-status-info').innerHTML = `
      <p><strong>Router URL:</strong> <code>${esc(d.router_url)}</code></p>
      <p><strong>Agent ID:</strong> <code>${esc(d.agent_id) || '(not registered)'}</code></p>
      <p><strong>Status:</strong> ${d.connected
        ? '<span class="badge badge-connected">connected</span>'
        : '<span class="badge badge-disconnected">not connected</span>'}</p>
    `;
  }

  // Agent info.
  const r2 = await api('GET', '/ui/agent-info');
  if (r2 && r2.ok) {
    const d = await r2.json();
    document.getElementById('router-agent-info').textContent = JSON.stringify(d.agent_info, null, 2);
  }
}

document.getElementById('btn-register').addEventListener('click', async () => {
  const token = document.getElementById('router-inv-token').value.trim();
  const res = document.getElementById('register-result');
  if (!token) { res.innerHTML = '<p class="error-msg">Token is required.</p>'; return; }

  const r = await api('POST', '/ui/onboarding/register', { invitation_token: token });
  if (!r) return;
  if (r.ok) {
    const d = await r.json();
    res.innerHTML = `<p style="color:var(--green);font-size:13px">Registered as <code class="mono">${esc(d.agent_id)}</code></p>`;
    document.getElementById('router-inv-token').value = '';
    loadSetup();
  } else {
    const err = await r.json().catch(() => ({}));
    res.innerHTML = `<p class="error-msg">${err.detail || 'Registration failed.'}</p>`;
  }
});

document.getElementById('btn-refresh-info').addEventListener('click', async () => {
  const r = await api('POST', '/ui/agent-info/refresh');
  if (r && r.ok) {
    loadSetup();
  }
});

document.getElementById('btn-chpw').addEventListener('click', async () => {
  const cur = document.getElementById('chpw-current').value;
  const nw = document.getElementById('chpw-new').value;
  const res = document.getElementById('chpw-result');
  if (!cur || !nw) { res.innerHTML = '<p class="error-msg">Both fields required.</p>'; return; }
  const r = await api('POST', '/ui/change-password', { current_password: cur, new_password: nw });
  if (!r) return;
  if (r.ok) {
    res.innerHTML = '<p style="color:var(--green);font-size:13px">Password changed successfully.</p>';
    document.getElementById('chpw-current').value = '';
    document.getElementById('chpw-new').value = '';
  } else {
    const err = await r.json().catch(() => ({}));
    res.innerHTML = `<p class="error-msg">${err.detail || 'Error changing password.'}</p>`;
  }
});

// ---------------------------------------------------------------------------
// Logs Tab
// ---------------------------------------------------------------------------

async function loadLogs() {
  const r = await api('GET', '/ui/logs');
  if (!r || !r.ok) return;
  const logs = await r.json();
  const el = document.getElementById('log-view');
  el.textContent = logs.length ? logs.join('\n') : '(no logs yet)';
  el.scrollTop = el.scrollHeight;
}

document.getElementById('refresh-logs').addEventListener('click', loadLogs);

// Auto-refresh logs when active.
setInterval(() => { if (_activeTab === 'logs') loadLogs(); }, 5000);
// Auto-refresh status when active.
setInterval(() => { if (_activeTab === 'status') loadStatus(); }, 10000);

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

checkAuth();
