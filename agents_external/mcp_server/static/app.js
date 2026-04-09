// Router-as-MCP-Server Admin UI — app.js

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

function truncate(s, n = 60) {
  if (!s) return '—';
  s = String(s);
  return s.length > n ? s.slice(0, n) + '...' : s;
}

function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}

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
// Tabs
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
  else if (_activeTab === 'tools') loadTools();
  else if (_activeTab === 'logs') loadLogs();
  else if (_activeTab === 'config') loadConfig();
  else if (_activeTab === 'setup') loadSetup();
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

  document.getElementById('status-grid').innerHTML = `
    <div class="status-card">
      <div class="label">Router</div>
      <div class="value ${d.router_connected ? 'ok' : 'bad'}">${d.router_connected ? 'Connected' : 'Disconnected'}</div>
      ${d.agent_id ? `<div style="font-size:11px;color:var(--text-dim);margin-top:.3rem" class="mono">${esc(d.agent_id)}</div>` : ''}
    </div>
    <div class="status-card">
      <div class="label">MCP Transport</div>
      <div class="value">${d.mcp_transport.toUpperCase()}</div>
      <div style="font-size:11px;color:var(--text-dim);margin-top:.3rem">Port ${d.mcp_port}</div>
    </div>
    <div class="status-card">
      <div class="label">Agents Exposed</div>
      <div class="value">${d.agent_count}</div>
    </div>
    <div class="status-card">
      <div class="label">Poll / Timeout</div>
      <div class="value" style="font-size:1rem">${d.poll_interval}s / ${d.tool_timeout}s</div>
    </div>
  `;

  document.getElementById('mcp-url-box').innerHTML = `
    <p>Point your MCP client (Claude Desktop, Cursor, etc.) to:</p>
    <code style="font-size:14px;color:var(--accent);user-select:all">${esc(d.mcp_url)}</code>
    <p style="margin-top:.5rem;font-size:11px">Transport: ${d.mcp_transport.toUpperCase()}</p>
  `;

}

// ---------------------------------------------------------------------------
// Setup Tab
// ---------------------------------------------------------------------------

async function loadSetup() {
  const r = await api('GET', '/ui/onboarding');
  if (r && r.ok) {
    const d = await r.json();
    document.getElementById('setup-onboard-info').innerHTML = `
      <p><strong>Router URL:</strong> <code>${esc(d.router_url)}</code></p>
      <p><strong>Agent ID:</strong> <code>${esc(d.agent_id) || '(not registered)'}</code></p>
      <p><strong>Status:</strong> ${d.connected
        ? '<span class="badge badge-connected">connected</span>'
        : '<span class="badge badge-disconnected">not connected</span>'}</p>
    `;
  }
}

document.getElementById('btn-setup-register').addEventListener('click', async () => {
  const token = document.getElementById('setup-inv-token').value.trim();
  const res = document.getElementById('setup-register-result');
  if (!token) { res.innerHTML = '<p class="error-msg">Token is required.</p>'; return; }
  const r = await api('POST', '/ui/onboarding/register', { invitation_token: token });
  if (!r) return;
  if (r.ok) {
    const d = await r.json();
    res.innerHTML = `<p style="color:var(--green);font-size:13px">Registered as <code class="mono">${esc(d.agent_id)}</code></p>`;
    document.getElementById('setup-inv-token').value = '';
    loadSetup();
  } else {
    const err = await r.json().catch(() => ({}));
    res.innerHTML = `<p class="error-msg">${err.detail || 'Registration failed.'}</p>`;
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
// Tools Tab
// ---------------------------------------------------------------------------

let _excludeSet = new Set();

async function loadTools() {
  // Load tools.
  const r = await api('GET', '/ui/tools');
  if (!r || !r.ok) return;
  const tools = await r.json();

  // Load exclude config.
  const r2 = await api('GET', '/ui/config');
  if (r2 && r2.ok) {
    const cfg = await r2.json();
    _excludeSet = new Set(cfg.exclude_agents || []);
    document.getElementById('exclude-input').value = (cfg.exclude_agents || []).join(', ');
  }

  const tbody = document.getElementById('tools-tbody');
  if (!tools.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No tools. Agents will appear after polling.</td></tr>';
    return;
  }

  tbody.innerHTML = tools.map((t, i) => {
    const schemaId = `schema-${i}`;
    return `<tr>
      <td class="mono" style="font-size:12px">${esc(t.tool_name)}</td>
      <td class="mono" style="font-size:12px">${esc(t.agent_id)}</td>
      <td>${esc(truncate(t.description, 80))}</td>
      <td>
        <span class="schema-toggle" onclick="toggleSchema('${schemaId}')">Show</span>
        <div class="schema-content" id="${schemaId}">${esc(JSON.stringify(t.input_schema, null, 2))}</div>
      </td>
      <td>
        <button class="btn btn-secondary btn-sm ${_excludeSet.has(t.agent_id) ? 'btn-danger' : ''}"
                onclick="toggleExclude('${esc(t.agent_id)}')"
                title="${_excludeSet.has(t.agent_id) ? 'Click to re-expose' : 'Click to exclude'}">
          ${_excludeSet.has(t.agent_id) ? 'Excluded' : 'Exposed'}
        </button>
      </td>
    </tr>`;
  }).join('');
}

function toggleSchema(id) {
  const el = document.getElementById(id);
  if (el) el.classList.toggle('open');
}

async function toggleExclude(agentId) {
  if (_excludeSet.has(agentId)) {
    _excludeSet.delete(agentId);
  } else {
    _excludeSet.add(agentId);
  }
  const r = await api('POST', '/ui/config/exclude', { exclude_agents: [..._excludeSet] });
  if (r && r.ok) loadTools();
}

window.toggleSchema = toggleSchema;
window.toggleExclude = toggleExclude;

document.getElementById('refresh-tools').addEventListener('click', async () => {
  const r = await api('POST', '/ui/agents/refresh');
  if (r && r.ok) loadTools();
});

document.getElementById('btn-save-exclude').addEventListener('click', async () => {
  const input = document.getElementById('exclude-input').value;
  const agents = input.split(',').map(s => s.trim()).filter(Boolean);
  const res = document.getElementById('exclude-result');
  const r = await api('POST', '/ui/config/exclude', { exclude_agents: agents });
  if (r && r.ok) {
    res.innerHTML = '<p style="color:var(--green);font-size:13px">Saved.</p>';
    setTimeout(() => { res.innerHTML = ''; }, 3000);
    loadTools();
  } else {
    res.innerHTML = '<p class="error-msg">Failed to save.</p>';
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

// Auto-refresh.
setInterval(() => { if (_activeTab === 'logs') loadLogs(); }, 5000);
setInterval(() => { if (_activeTab === 'status') loadStatus(); }, 10000);

// ---------------------------------------------------------------------------
// Config Tab
// ---------------------------------------------------------------------------

async function loadConfig() {
  try {
    const r = await api('GET', '/ui/config');
    const d = await r.json();
    document.getElementById('cfg-editor').value = JSON.stringify(d.config || {}, null, 2);
    document.getElementById('cfg-example').textContent = JSON.stringify(d.example || {}, null, 2);
  } catch (e) {
    document.getElementById('cfg-editor').value = '// Failed to load';
  }
}

async function saveConfig() {
  const msg = document.getElementById('cfg-msg');
  try {
    const config = JSON.parse(document.getElementById('cfg-editor').value);
    await api('PUT', '/ui/config', { config });
    msg.style.color = '#4caf50'; msg.textContent = 'Saved';
    setTimeout(() => msg.textContent = '', 2000);
  } catch (e) {
    msg.style.color = '#f44336'; msg.textContent = e.message || 'Invalid JSON';
  }
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

checkAuth();
