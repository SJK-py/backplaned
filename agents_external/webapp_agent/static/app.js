import{h,render,Component}from'https://esm.sh/preact@10.25.4';
import{useState,useEffect,useRef,useCallback}from'https://esm.sh/preact@10.25.4/hooks';
import htm from'https://esm.sh/htm@3.1.1';
const html=htm.bind(h);

// ---------------------------------------------------------------------------
// API helper
// ---------------------------------------------------------------------------
async function api(method,path,body){
  const opts={method,credentials:'same-origin',headers:{}};
  if(body){opts.headers['Content-Type']='application/json';opts.body=JSON.stringify(body)}
  const r=await fetch(path,opts);
  if(r.status===401){location.reload();return null}
  return r;
}

function esc(s){return s||''}

// ---------------------------------------------------------------------------
// Login Page
// ---------------------------------------------------------------------------
function LoginPage({onLogin}){
  const[tab,setTab]=useState('password');
  const[uid,setUid]=useState('');
  const[pw,setPw]=useState('');
  const[token,setToken]=useState('');
  const[newPw,setNewPw]=useState('');
  const[error,setError]=useState('');
  const[step,setStep]=useState('login'); // login | set_password
  const[loading,setLoading]=useState(false);

  async function doPasswordLogin(){
    if(!uid||!pw)return;
    setLoading(true);setError('');
    const r=await api('POST','/api/login',{user_id:uid,password:pw});
    setLoading(false);
    if(!r)return;
    if(r.ok){onLogin(uid)}
    else{const d=await r.json();setError(d.detail||'Login failed')}
  }

  async function doTokenLogin(){
    if(!uid||!token)return;
    setLoading(true);setError('');
    const r=await api('POST','/api/login-token',{user_id:uid,token});
    setLoading(false);
    if(!r)return;
    const d=await r.json();
    if(r.ok&&d.status==='set_password'){setStep('set_password')}
    else{setError(d.detail||'Token validation failed')}
  }

  async function doSetPassword(){
    if(!newPw)return;
    setLoading(true);setError('');
    const r=await api('POST','/api/set-password',{user_id:uid,password:newPw,token});
    setLoading(false);
    if(!r)return;
    if(r.ok){onLogin(uid)}
    else{const d=await r.json();setError(d.detail||'Failed to set password')}
  }

  if(step==='set_password'){
    return html`<div class="login-page"><div class="login-box">
      <h1>Set Password</h1>
      <p class="subtitle">Token validated. Set a password for <strong>${esc(uid)}</strong>.</p>
      <div class="form-group"><label>New Password</label>
        <input type="password" value=${newPw} onInput=${e=>setNewPw(e.target.value)}
          onKeyDown=${e=>e.key==='Enter'&&doSetPassword()} placeholder="Choose a password"/></div>
      ${error&&html`<div class="error">${error}</div>`}
      <button class="btn" style="width:100%;margin-top:8px" onClick=${doSetPassword} disabled=${loading}>
        ${loading?'Setting...':'Set Password & Login'}</button>
    </div></div>`;
  }

  return html`<div class="login-page"><div class="login-box">
    <h1>Backplaned</h1>
    <p class="subtitle">Sign in to continue</p>
    <div class="tab-row">
      <button class=${tab==='password'?'active':''} onClick=${()=>setTab('password')}>Password</button>
      <button class=${tab==='token'?'active':''} onClick=${()=>setTab('token')}>Token</button>
    </div>
    <div class="form-group"><label>User ID</label>
      <input type="text" value=${uid} onInput=${e=>setUid(e.target.value)} placeholder="your_user_id"/></div>
    ${tab==='password'?html`
      <div class="form-group"><label>Password</label>
        <input type="password" value=${pw} onInput=${e=>setPw(e.target.value)}
          onKeyDown=${e=>e.key==='Enter'&&doPasswordLogin()} placeholder="Password"/></div>
      ${error&&html`<div class="error">${error}</div>`}
      <button class="btn" style="width:100%;margin-top:8px" onClick=${doPasswordLogin} disabled=${loading}>
        ${loading?html`<span class="spinner"></span>`:'Sign In'}</button>
    `:html`
      <div class="form-group"><label>Login Token</label>
        <input type="text" value=${token} onInput=${e=>setToken(e.target.value)}
          onKeyDown=${e=>e.key==='Enter'&&doTokenLogin()} placeholder="Token from /webapp command"/></div>
      ${error&&html`<div class="error">${error}</div>`}
      <button class="btn" style="width:100%;margin-top:8px" onClick=${doTokenLogin} disabled=${loading}>
        ${loading?html`<span class="spinner"></span>`:'Validate Token'}</button>
    `}
  </div></div>`;
}

// ---------------------------------------------------------------------------
// Main App
// ---------------------------------------------------------------------------
function App(){
  const[user,setUser]=useState(null);
  const[checking,setChecking]=useState(true);
  const[sessions,_setSessions]=useState(()=>{
    try{const s=localStorage.getItem('wa_sessions');return s?JSON.parse(s):[]}catch(e){return[]}
  });
  const setSessions=useCallback(fn=>{
    _setSessions(prev=>{
      const next=typeof fn==='function'?fn(prev):fn;
      try{localStorage.setItem('wa_sessions',JSON.stringify(next))}catch(e){}
      return next;
    });
  },[]);
  const[archived,setArchived]=useState([]);
  const[currentSid,_setCurrentSid]=useState(()=>{
    try{return localStorage.getItem('wa_currentSid')||null}catch(e){return null}
  });
  const setCurrentSid=useCallback(v=>{
    _setCurrentSid(v);
    try{if(v)localStorage.setItem('wa_currentSid',v);else localStorage.removeItem('wa_currentSid')}catch(e){}
  },[]);
  const[messages,setMessages]=useState([]);
  const[agents,setAgents]=useState('');
  const[sending,setSending]=useState(false);
  const[attachedFiles,setAttachedFiles]=useState([]);
  const[leftOpen,setLeftOpen]=useState(true);
  const[rightOpen,setRightOpen]=useState(false);
  const[modal,setModal]=useState(null);
  const inputRef=useRef(null);
  const chatRef=useRef(null);

  // Check auth on mount
  useEffect(()=>{
    (async()=>{
      const r=await fetch('/api/me',{credentials:'same-origin'});
      if(r.ok){const d=await r.json();setUser(d.user_id)}
      setChecking(false);
    })();
  },[]);

  // Load agents
  const loadAgents=useCallback(async()=>{
    if(!user)return;
    const r=await api('GET','/api/agents');
    if(r&&r.ok){const d=await r.json();setAgents(d.content||'')}
  },[user]);

  // Load archived
  const loadArchived=useCallback(async()=>{
    if(!user)return;
    const r=await api('GET','/api/archived-sessions');
    if(r&&r.ok){const d=await r.json();setArchived(Array.isArray(d)?d:[])}
  },[user]);

  // Load agents and archived once on login
  useEffect(()=>{
    if(!user)return;
    loadAgents();loadArchived();
  },[user,loadAgents,loadArchived]);

  // Load history when currentSid changes (including initial restore from localStorage)
  useEffect(()=>{
    if(!currentSid||!user)return;
    (async()=>{
      try{
        const r=await api('GET',`/api/sessions/${currentSid}/history`);
        if(r&&r.ok){const hist=await r.json();if(Array.isArray(hist)&&hist.length)setMessages(hist);else setMessages([])}
      }catch(e){setMessages([])}
    })();
  },[currentSid,user]);

  // Auto-scroll chat
  useEffect(()=>{
    if(chatRef.current)chatRef.current.scrollTop=chatRef.current.scrollHeight;
  },[messages]);

  // Create new session
  async function newSession(){
    const r=await api('POST','/api/sessions/new');
    if(!r||!r.ok)return;
    const d=await r.json();
    const sid=d.session_id;
    setSessions(prev=>[{session_id:sid,title:'New session'},...prev]);
    setCurrentSid(sid);
    setMessages([]);
  }

  // Send message with SSE progress streaming
  async function sendMessage(){
    const el=inputRef.current;if(!el)return;
    const msg=el.value.trim();if(!msg||!currentSid)return;
    el.value='';
    const isFirstMsg=messages.length===0;
    setMessages(prev=>[...prev,{role:'user',content:msg}]);
    setSending(true);

    // Upload attached files first, collect ProxyFile dicts
    let fileDicts=null;
    if(attachedFiles.length>0){
      fileDicts=[];
      for(const f of attachedFiles){
        try{
          const fd=new FormData();fd.append('file',f);
          const ur=await fetch('/api/upload',{method:'POST',credentials:'same-origin',body:fd});
          if(ur.ok){fileDicts.push(await ur.json())}
        }catch(e){/* skip failed uploads */}
      }
      if(fileDicts.length===0)fileDicts=null;
    }

    try{
      const r=await fetch('/api/chat-stream',{
        method:'POST',credentials:'same-origin',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({session_id:currentSid,message:msg,files:fileDicts}),
      });
      if(!r.ok){
        setMessages(prev=>[...prev,{role:'system',content:'Error sending message.'}]);
        setSending(false);setAttachedFiles([]);el.focus();return;
      }
      const reader=r.body.getReader();
      const decoder=new TextDecoder();
      let buf='';
      while(true){
        const{done,value}=await reader.read();
        if(done)break;
        buf+=decoder.decode(value,{stream:true});
        const lines=buf.split('\n');
        buf=lines.pop()||'';
        for(const line of lines){
          if(!line.startsWith('data: '))continue;
          try{
            const ev=JSON.parse(line.slice(6));
            if(ev.type==='thinking'||ev.type==='status'){
              setMessages(prev=>{
                const last=prev[prev.length-1];
                if(last&&last.role==='progress')return[...prev.slice(0,-1),{role:'progress',content:ev.content||ev.type}];
                return[...prev,{role:'progress',content:ev.content||ev.type}];
              });
            }else if(ev.type==='tool_call'){
              setMessages(prev=>{
                const last=prev[prev.length-1];
                const text=ev.content?`${ev.content}`:`🔧 Calling ${ev.metadata?.tool||'tool'}...`;
                if(last&&last.role==='progress')return[...prev.slice(0,-1),{role:'progress',content:text}];
                return[...prev,{role:'progress',content:text}];
              });
            }else if(ev.type==='tool_result'){
              setMessages(prev=>{
                const last=prev[prev.length-1];
                const text=ev.content?`✅ ${ev.metadata?.tool||'Result'}: ${ev.content.slice(0,200)}`:'✅ Tool completed';
                if(last&&last.role==='progress')return[...prev.slice(0,-1),{role:'progress',content:text}];
                return[...prev,{role:'progress',content:text}];
              });
            }else if(ev.type==='result'){
              let reply=ev.content||'(no response)';
              const rfiles=ev.files;
              if(rfiles&&rfiles.length){
                const names=rfiles.map(f=>f.original_name||f.name||'file').join(', ');
                reply+=`\n\n📎 Files received: ${names} (check inbox to download)`;
              }
              setMessages(prev=>{
                const filtered=prev.filter(m=>m.role!=='progress');
                return[...filtered,{role:'assistant',content:reply}];
              });
            }else if(ev.type==='error'){
              setMessages(prev=>[...prev.filter(m=>m.role!=='progress'),{role:'system',content:ev.content||'Error'}]);
            }
          }catch(e){/* ignore parse errors */}
        }
      }
      // Lazy title fetch after first response (core generates title async)
      if(isFirstMsg){
        setTimeout(async()=>{
          const ir=await api('GET',`/api/sessions/${currentSid}/info`);
          if(ir&&ir.ok){
            const info=await ir.json();
            if(info.title)setSessions(prev=>prev.map(s=>s.session_id===currentSid?{...s,title:info.title}:s));
          }
        },15000);
      }
    }catch(e){
      setMessages(prev=>[...prev,{role:'system',content:`Error: ${e.message}`}]);
    }
    setSending(false);
    setAttachedFiles([]);
    el.focus();
  }

  // Archive session
  async function archiveSession(sid){
    await api('POST',`/api/sessions/${sid}/archive`);
    setSessions(prev=>prev.filter(s=>s.session_id!==sid));
    if(currentSid===sid){setCurrentSid(null);setMessages([])}
    loadArchived();
  }

  // Unarchive
  async function unarchiveSession(sid,title){
    const r=await api('POST',`/api/sessions/${sid}/unarchive`);
    if(r&&r.ok){
      loadArchived();
      setSessions(prev=>[...prev,{session_id:sid,title:title||sid}]);
    }
  }

  // Delete archived
  async function deleteSession(sid){
    if(!confirm(`Delete session ${sid} permanently?`))return;
    await api('DELETE',`/api/sessions/${sid}`);
    loadArchived();
  }

  // Rename
  async function renameSession(sid){
    const name=prompt('New session name:');
    if(!name)return;
    await api('POST',`/api/sessions/${sid}/rename`,{name});
    setSessions(prev=>prev.map(s=>s.session_id===sid?{...s,title:name}:s));
  }

  // Set default
  async function setDefault(sid){
    await api('POST',`/api/sessions/${sid}/default`);
    setSessions(prev=>{
      const item=prev.find(s=>s.session_id===sid);
      if(!item)return prev;
      return[item,...prev.filter(s=>s.session_id!==sid)];
    });
  }

  // Link/unlink
  async function linkAgent(agentId){
    if(!currentSid)return;
    await api('POST',`/api/sessions/${currentSid}/link/${agentId}`);
  }
  async function unlinkAgent(){
    if(!currentSid)return;
    await api('POST',`/api/sessions/${currentSid}/unlink`);
  }

  // Select session — load history from backend
  function selectSession(sid){
    if(sid===currentSid)return;
    setCurrentSid(sid);
    setMessages([]);
    if(window.innerWidth<=768)setLeftOpen(false);
  }

  // Logout
  async function logout(){
    await api('POST','/api/logout');
    try{localStorage.removeItem('wa_sessions');localStorage.removeItem('wa_currentSid')}catch(e){}
    _setSessions([]);_setCurrentSid(null);
    setMessages([]);setArchived([]);setAgents('');setAttachedFiles([]);
    setUser(null);
  }

  if(checking)return html`<div class="login-page"><span class="spinner"></span></div>`;
  if(!user)return html`<${LoginPage} onLogin=${u=>setUser(u)}/>`;

  // Ensure at least one session
  if(sessions.length===0&&!currentSid){
    newSession();
    return html`<div class="login-page"><span class="spinner"></span></div>`;
  }

  return html`<div class="app-layout">
    <!-- LEFT PANE: Sessions -->
    <div class=${"pane-left"+(leftOpen?'':' collapsed')}>
      <div style="padding:12px 12px 8px;display:flex;justify-content:space-between;align-items:center">
        <span style="font-weight:600;font-size:14px">Sessions</span>
        <button class="icon-btn" onClick=${()=>setLeftOpen(false)}>✕</button>
      </div>
      <div class="session-list">
        <div class="session-section-title">Active</div>
        ${sessions.map(s=>html`
          <div class=${"session-item"+(s.session_id===currentSid?' active':'')}
            onClick=${()=>selectSession(s.session_id)}>
            <span class="title">${esc(s.title)||esc(s.session_id)}</span>
            <div class="session-actions">
              <button onClick=${e=>{e.stopPropagation();renameSession(s.session_id)}} title="Rename">✎</button>
              <button onClick=${e=>{e.stopPropagation();setDefault(s.session_id)}} title="Set default">★</button>
              <button onClick=${e=>{e.stopPropagation();archiveSession(s.session_id)}} title="Archive">📦</button>
            </div>
          </div>
        `)}
        <div class="new-session-btn" onClick=${newSession}>+ New session</div>

        <div class="session-section-title" style="margin-top:12px">Archived <button class="icon-btn" style="font-size:12px" title="Refresh" onClick=${e=>{e.stopPropagation();loadArchived()}}>↻</button></div>
        ${archived.length>0?archived.map(a=>html`
          <div class="session-item">
            <span class="title">${esc(a.title)||esc(a.session_id)}</span>
            <div class="session-actions" style="opacity:1">
              <button onClick=${()=>unarchiveSession(a.session_id,a.title)} title="Unarchive">↩</button>
              <button onClick=${()=>deleteSession(a.session_id)} title="Delete">🗑</button>
            </div>
          </div>
        `):html`<div style="padding:4px 10px;font-size:12px;color:var(--dim)">No archived sessions</div>`}
      </div>
    </div>

    <!-- CENTER PANE: Chat -->
    <div class="pane-center">
      <div class="topbar">
        <div class="topbar-left">
          ${!leftOpen&&html`<button class="icon-btn" onClick=${()=>setLeftOpen(true)}>☰</button>`}
          <span class="brand">Backplaned</span>
        </div>
        <div class="topbar-right">
          <button class="icon-btn" title="Config" onClick=${()=>setModal('config')}>⚙</button>
          <button class="icon-btn" title="Files" onClick=${()=>setModal('inbox')}>📁</button>
          <span class="user-badge">${esc(user)}</span>
          ${!rightOpen&&html`<button class="icon-btn" onClick=${()=>setRightOpen(true)}>🤖</button>`}
          <button class="icon-btn" title="Logout" onClick=${logout}>⏻</button>
        </div>
      </div>

      <div class="chat-area" ref=${chatRef}>
        ${messages.length===0&&html`<div class="msg system">Start a conversation...</div>`}
        ${messages.map(m=>html`<div class=${"msg "+m.role}>${esc(m.content)}</div>`)}
        ${sending&&html`<div class="typing-indicator"><span/><span/><span/></div>`}
      </div>

      <div class="input-area">
        <input type="file" id="file-input" style="display:none" multiple
          onChange=${e=>{if(e.target.files.length)setAttachedFiles(Array.from(e.target.files))}}/>
        <button class="icon-btn" title="Attach files" style="flex-shrink:0;width:40px;height:40px;display:flex;align-items:center;justify-content:center;font-size:18px"
          onClick=${()=>document.getElementById('file-input').click()}>📎</button>
        <div style="flex:1;display:flex;flex-direction:column;gap:2px">
          ${attachedFiles.length>0&&html`<div style="font-size:11px;color:var(--dim);padding:0 4px">
            ${attachedFiles.map((f,i)=>html`<span style="margin-right:6px">${esc(f.name)} <button style="background:none;border:none;color:var(--danger);cursor:pointer;font-size:10px" onClick=${()=>setAttachedFiles(prev=>prev.filter((_,j)=>j!==i))}>✕</button></span>`)}
          </div>`}
          <textarea ref=${inputRef} rows="1" placeholder="Type a message..."
            onKeyDown=${e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMessage()}}}
            disabled=${sending||!currentSid}/>
        </div>
        <button class="send-btn" onClick=${sendMessage} disabled=${sending||!currentSid}>↑</button>
      </div>
    </div>

    <!-- RIGHT PANE: Agents -->
    <div class=${"pane-right"+(rightOpen?'':' collapsed')}>
      <div style="padding:12px 12px 8px;display:flex;justify-content:space-between;align-items:center">
        <span style="font-weight:600;font-size:14px">Agents</span>
        <div style="display:flex;gap:4px">
          <button class="icon-btn" title="Refresh" onClick=${loadAgents}>↻</button>
          <button class="icon-btn" onClick=${()=>setRightOpen(false)}>✕</button>
        </div>
      </div>
      <div class="agent-list">
        ${(typeof agents==='string'&&agents)?agents.split('\n').filter(l=>l.trim()).map(line=>{
          const m=line.match(/^\*\*([^*]+)\*\*:\s*(.*)/);
          if(m)return html`<div class="agent-item">
            <div class="name">${esc(m[1])}</div>
            <div class="desc">${esc(m[2])}</div>
            <div class="actions">
              <button class="btn-sm btn-outline" onClick=${()=>linkAgent(m[1])}>Link</button>
              <button class="btn-sm btn-outline" onClick=${()=>unlinkAgent()}>Unlink</button>
            </div>
          </div>`;
          return line.trim()?html`<div style="padding:4px 10px;font-size:12px;color:var(--dim)">${esc(line)}</div>`:null;
        }):html`<div style="padding:16px;color:var(--dim);font-size:13px">Loading agents...</div>`}
      </div>
    </div>

    <!-- MODALS -->
    ${modal==='config'&&html`<${ConfigModal} onClose=${()=>setModal(null)}/>`}
    ${modal==='inbox'&&html`<${InboxModal} onClose=${()=>setModal(null)} userId=${user}/>`}
  </div>`;
}

// ---------------------------------------------------------------------------
// Config Modal
// ---------------------------------------------------------------------------
function ConfigModal({onClose}){
  const[content,setContent]=useState('Loading...');
  const[instruction,setInstruction]=useState('');
  const[saving,setSaving]=useState(false);

  useEffect(()=>{
    (async()=>{
      const r=await api('GET','/api/config');
      if(r&&r.ok){const d=await r.json();setContent(d.content||'(empty)')}
    })();
  },[]);

  async function save(){
    if(!instruction)return;
    setSaving(true);
    const r=await api('POST','/api/config',{instruction});
    if(r&&r.ok){const d=await r.json();setContent(d.content||content)}
    setSaving(false);setInstruction('');
  }

  return html`<div class="modal-overlay" onClick=${e=>e.target===e.currentTarget&&onClose()}>
    <div class="modal">
      <h2>Configuration</h2>
      <pre style="background:var(--bg2);padding:12px;border-radius:8px;font-size:12px;overflow:auto;max-height:300px;margin-bottom:12px;white-space:pre-wrap">${content}</pre>
      <div class="form-group"><label>Modify config (natural language)</label>
        <input type="text" value=${instruction} onInput=${e=>setInstruction(e.target.value)}
          onKeyDown=${e=>e.key==='Enter'&&save()} placeholder="e.g. set timezone to Asia/Seoul"/>
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="btn-outline btn-sm" onClick=${onClose}>Close</button>
        <button class="btn btn-sm" onClick=${save} disabled=${saving}>${saving?'Saving...':'Apply'}</button>
      </div>
    </div>
  </div>`;
}

// ---------------------------------------------------------------------------
// Inbox Modal
// ---------------------------------------------------------------------------
function InboxModal({onClose,userId}){
  const[files,setFiles]=useState([]);

  async function load(){
    const r=await api('GET','/api/inbox');
    if(r&&r.ok)setFiles(await r.json());
  }
  useEffect(()=>{load()},[]);

  async function deleteFile(name){
    if(!confirm(`Delete ${name}?`))return;
    await api('DELETE',`/api/inbox/${encodeURIComponent(name)}`);
    load();
  }

  return html`<div class="modal-overlay" onClick=${e=>e.target===e.currentTarget&&onClose()}>
    <div class="modal">
      <h2>File Inbox</h2>
      ${files.length===0?html`<p style="color:var(--dim)">No files in inbox.</p>`:html`
        <div style="max-height:400px;overflow-y:auto">
          ${files.map(f=>html`
            <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border)">
              <div>
                <div style="font-size:13px;font-weight:500">${esc(f.name)}</div>
                <div style="font-size:11px;color:var(--dim)">${(f.size/1024).toFixed(1)} KB</div>
              </div>
              <div style="display:flex;gap:4px">
                <a href="/api/inbox/${encodeURIComponent(f.name)}" download class="btn-sm btn-outline">↓</a>
                <button class="btn-sm btn-danger" onClick=${()=>deleteFile(f.name)}>✕</button>
              </div>
            </div>
          `)}
        </div>
      `}
      <div style="display:flex;justify-content:flex-end;margin-top:12px">
        <button class="btn-outline btn-sm" onClick=${onClose}>Close</button>
      </div>
    </div>
  </div>`;
}

// ---------------------------------------------------------------------------
// Mount
// ---------------------------------------------------------------------------
render(html`<${App}/>`,document.getElementById('app'));
