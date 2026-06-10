// ============== Log (must be first) ==============
let logBuffer = [];
function logEntry(field, event, overrideTs) {
const now = new Date();
const ts = overrideTs || (now.getHours().toString().padStart(2,'0') + ':' +
           now.getMinutes().toString().padStart(2,'0') + ':' +
           now.getSeconds().toString().padStart(2,'0') + '.' +
           now.getMilliseconds().toString().padStart(3,'0'));
logBuffer.push({ timestamp: ts, source: 'frontend', field, event });
if (logBuffer.length > 500) logBuffer.shift();
renderLogs();
}
function renderLogs() {
const checked = [...document.querySelectorAll('#log-fields input:checked')].map(cb => cb.dataset.field);
const container = document.getElementById('log-entries');
const autoscroll = document.getElementById('log-autoscroll')?.checked;
if (!container) return;
const wasAtBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 30;
// 保存已展开的日志索引（通过匹配文本内容）
const expandedTexts = new Set();
container.querySelectorAll('.log-entry.expanded').forEach(el => {
  expandedTexts.add(el.querySelector('.ev')?.textContent + '|' + el.querySelector('.ts')?.textContent);
});
let entries = logBuffer.filter(e => checked.includes(e.field));
const countEl = document.getElementById('log-count');
if (countEl) countEl.textContent = entries.length;
container.innerHTML = entries.slice(-200).map(e =>
'<div class=log-entry onclick="this.classList.toggle(\'expanded\')"><span class=ts>' + e.timestamp + '</span> <span class="lv lv-' + e.field + '">' + e.field + '</span> <span class=ev>' + e.event + '</span></div>'
).join('') || '<div class=log-entry><span class=ts>--</span> <span class=ev>无日志</span></div>';
// 恢复已展开的日志条目
container.querySelectorAll('.log-entry').forEach(el => {
  const key = el.querySelector('.ev')?.textContent + '|' + el.querySelector('.ts')?.textContent;
  if (expandedTexts.has(key)) el.classList.add('expanded');
});
if (autoscroll && wasAtBottom) container.scrollTop = container.scrollHeight;
}
function clearLogs() { logBuffer = []; renderLogs(); }

// ============== State ==============
const API = '/api';
function $id(id) { return document.getElementById(id); }
let agents = [];
let ws = null;
let hoveredAgent = null;
let simRunning = false;
let _relationships = [];
let _lastLogCount = 0;

// ============== Agent forwarding → React iframe ==============
const iframe = document.getElementById('campus-iframe');
function forwardAgents() {
  if (iframe && iframe.contentWindow) {
    iframe.contentWindow.postMessage({ type: 'agents', data: agents, relationships: _relationships }, '*');
  }
}

// Receive hover events from iframe → show tooltip in parent
window.addEventListener('message', (e) => {
  if (e.data?.type === 'agent-move') {
    // 中继坐标更新到 server
    fetch(API + '/agents/' + e.data.agent_id + '/move?x=' + e.data.x + '&y=' + e.data.y, { method: 'POST' })
      .catch(() => {});
  }
  if (e.data?.type === 'agent-hover') {
    const found = e.data.data;
    const tt = document.getElementById('tooltip');
    if (found) {
      hoveredAgent = found;
      const statusLabel = { idle:'空闲', running:'运行中', paused:'已暂停', stopped:'已停止', error:'异常', created:'已创建', decided:'已决策', messaged:'已发送', send_failed:'发送失败', analyzed:'分析中' };
      const roleLabel = { scout:'侦察兵', commander:'指挥官', analyst:'分析师', support:'支援', brain:'Brain', 'claude-code':'Claude Code', openclaw:'OpenClaw', observer:'观察员' };
const backendLabel = { brain:'Brain', 'claude-code':'Claude Code', openclaw:'OpenClaw' };
      let html = '<div class=tt-name>' + (found.name || found.agent_id) + '</div>';
      const backend = (found.extra_meta||{}).backend || '';
html += '<div class=tt-role>' + (roleLabel[found.role] || backendLabel[backend] || found.role) + '</div>';
      html += '<div class=tt-row><span class=lbl>ID</span><span class=val>' + found.agent_id + '</span></div>';
      html += '<div class=tt-row><span class=lbl>状态</span><span class=val>' + (statusLabel[found.status] || found.status) + '</span></div>';
      if (found.x !== undefined) {
        html += '<div class=tt-row><span class=lbl>坐标</span><span class=val>(' + found.x.toFixed(0) + ', ' + found.y.toFixed(0) + ')</span></div>';
      }
      const tasks = found.pending_task_descs || [];
      if (tasks.length > 0) { html += '<div class=tt-section>任务</div>'; tasks.forEach((t, i) => { html += '<div class=tt-task><span class=tt-task-n>' + (i+1) + '.</span> ' + t + '</div>'; }); }
      const meta = found.extra_meta || {};
      if (meta.core_goal) { html += '<div class=tt-section>目标</div><div class=tt-task>' + meta.core_goal + '</div>'; }
      if (meta.hidden_secret) { html += '<div class=tt-section>秘密</div><div class=tt-task style=color:#C0392B>' + meta.hidden_secret + '</div>'; }
      if (meta.action_space && meta.action_space.length) {
        html += '<div class=tt-section>行动</div><div class=tt-skills>' + meta.action_space.map(a => '<span class=tt-tag>' + a + '</span>').join('') + '</div>';
      }
      tt.innerHTML = html;
      tt.style.display = 'block';
      const panelRect = document.getElementById('canvas-panel')?.getBoundingClientRect();
      const tx = (e.data.mx || 0) - (panelRect?.left || 0);
      const ty = (e.data.my || 0) - (panelRect?.top || 0);
      tt.style.left = Math.min(tx + 16, window.innerWidth - 300) + 'px';
      tt.style.top = Math.max(4, ty - 10) + 'px';
    } else {
      hoveredAgent = null;
      tt.style.display = 'none';
    }
  }
});

// ============== WebSocket ==============
function connectWS() {
const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
ws = new WebSocket(proto + '//' + location.host + '/ws');
ws.onopen = () => { ws.send('all'); logEntry('frontend', 'WebSocket 已连接'); };
ws.onmessage = (e) => {
const msg = JSON.parse(e.data);
// ── 实时推送的单条日志 ──
if (msg.type === 'agent_log' && msg.data) {
    const l = msg.data;
    const ts = (l.timestamp||'').slice(11,23);
    const from = l.from_agent || l.agent_id || '?';
    const to = l.to_agent || '';
    const action = l.action || l.event || '?';
    const status = l.action_status || '';
    const stIcon = status === 'success' ? '✅' : status === 'failed' ? '❌' : status === 'decided' ? '💭' : '➡️';
    const msgText = from + ' ' + stIcon + ' ' + action + (to && to !== '-' ? ' → ' + to : '') + ' | ' + (l.detail||'');
    logEntry('agent', msgText, ts);
    _lastLogCount++; // 同步计数，避免 batch all 响应重复渲染
    return;
    }
// ── Agent 状态实时更新 ──
if (msg.type === 'agent_status' && msg.data) {
    msg.data.forEach(s => {
        const existing = agents.find(a => a.agent_id === s.agent_id);
        if (existing) { existing.status = s.status; }
    });
    return;
}
if (msg.type === 'status' || msg.type === 'all') {
    agents = msg.data.agents || [];
    if (msg.data.relationships) _relationships = msg.data.relationships;
    forwardAgents();
    // ── Agent 动作日志 ──
    if (_lastLogCount === 0) {
    const logs = msg.data.agent_logs || [];
    logs.slice(_lastLogCount).forEach(l => {
        const ts = (l.timestamp||'').slice(11,23);
        const from = l.from_agent || l.agent_id || '?';
        const to = l.to_agent || '';
        const action = l.action || l.event || '?';
        const status = l.action_status || '';
        const stIcon = status === 'success' ? '✅' : status === 'failed' ? '❌' : status === 'decided' ? '💭' : '➡️';
        const msgText = from + ' ' + stIcon + ' ' + action + (to && to !== '-' ? ' → ' + to : '') + ' | ' + (l.detail||'');
        logEntry('agent', msgText, ts);
    });
    _lastLogCount = logs.length;
    }
    // ── 结构化的 logger 条目 (去重) ──
    const logEntries = msg.data.log_entries || [];
    logEntries.forEach(e => {
        const ts = (e.timestamp||'').slice(11,23);
        const d = e.details || {};
        const from = d.from_agent || e.agent_id || '';
        const to = d.to_agent || '';
        if (e.event === 'agent_message') {
            logEntry('message', from + ' → ' + to + ' | ' + ((d.content||'')), ts);
        } else if (e.event === 'event_trigger') {
            logEntry('scene', '⚡ ' + (e.message||''), ts);
        } else if (e.level === 'ERROR') {
            logEntry('system', '❌ ' + from + ' | ' + (e.message||''), ts);
        }
    });
}
// ── 通信报文 ──
if (msg.type === 'packets' && msg.data) {
    (msg.data.packets || []).forEach(p => {
        const ts = (p.timestamp||'').slice(11,23);
        const src = [p.src_ip||'', p.src_port||''].filter(Boolean).join(':') || '?';
        const dst = [p.dst_ip||'', p.dst_port||''].filter(Boolean).join(':') || '?';
        const text = [
            src + ' → ' + dst,
            p.protocol || 'TCP',
            (p.total_size||p.size_bytes||'?') + 'B',
            '[' + (p.tcp_flags||'') + ']',
            p.channel_id ? 'ch:' + p.channel_id : '',
            p.message_type || p.method || '',
            (p.agent_from||'') + '→' + (p.agent_to||''),
            (p.content||'')
        ].filter(Boolean).join(' | ');
        logEntry('message', text, ts);
    });
}
};
ws.onclose = () => { setTimeout(connectWS, 3000); };
}
connectWS();

// ============== Scene Selector ==============
async function loadSceneList() {
  try {
    const r = await fetch(API + '/scenes');
    const data = await r.json();
    const sel = document.getElementById('scene-selector');
    if (!sel) return;
    sel.innerHTML = '';
    data.scenes.forEach(s => {
      const opt = document.createElement('option');
      const val = typeof s === 'string' ? s : s.name;
      opt.value = val;
      opt.textContent = typeof s === 'string' ? val.replace('.json', '') : val;
      if (typeof s !== 'string') opt.dataset.format = s.format;
      sel.appendChild(opt);
    });
  } catch(e) { console.error('loadSceneList', e); }
}

function onSceneSelect() {}

async function runSelectedScene() {
  const sel = document.getElementById('scene-selector');
  const name = sel?.value;
  const format = sel?.selectedOptions[0]?.dataset?.format || 'file';
  if (!name) { logEntry('scene', '请先选择场景脚本'); return; }

  logEntry('scene', '=== ' + name + ' ===');

  let body;
  if (format === 'folder') {
    body = { scene: name, name: name };
  } else {
    try {
      const r = await fetch(API + '/scenes/' + encodeURIComponent(name));
      const data = await r.json();
      const content = data.content?.trim();
      if (!content) { logEntry('scene', '场景文件内容为空'); return; }
      const json = JSON.parse(content);
      let sceneName, scriptJson = null, script = null;
      if (json.script_json) {
        sceneName = json.script_json.scenario_metadata?.title || data.name;
        scriptJson = json.script_json;
      } else if (json.script) {
        sceneName = json.name || data.name; script = json.script;
      } else { logEntry('scene', '场景格式不支持'); return; }
      body = scriptJson
        ? { scene: 'auto', script_json: scriptJson, name: sceneName }
        : { scene: 'auto', script: script, name: sceneName };
    } catch(e) {
      logEntry('scene', (e instanceof SyntaxError ? 'JSON解析失败: ' : '读取失败: ') + e.message);
      return;
    }
  }

  try {
    const r1 = await fetch(API + '/simulations/setup', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    });
    if (!r1.ok) throw new Error((await r1.text()).slice(0, 200));
    const d1 = await r1.json();
    if (d1.relationships) { _relationships = d1.relationships; forwardAgents(); }
    if (ws && ws.readyState === WebSocket.OPEN) ws.send('all');
    logEntry('scene', '场景就绪: ' + (d1.agent_stats?.total_agents || 0) + ' Agent');
  } catch(e) { logEntry('scene', '场景构建失败: ' + e.message); return; }

  simRunning = true;
  fetch(API + '/simulations/launch', { method: 'POST' })
    .then(r => r.ok ? r.json() : r.text().then(t => { throw new Error(t.slice(0, 200)); }))
    .then(d => {
      if (d.error) { logEntry('scene', '容器: ' + d.error); return; }
      logEntry('scene', '仿真完成: ' + (d.duration_seconds||0) + 's | ' + (d.agent_stats?.total_agents||0) + ' Agent');
      if (d.relationships) { _relationships = d.relationships; forwardAgents(); }
      if (ws && ws.readyState === WebSocket.OPEN) ws.send('all');
    })
    .catch(e => logEntry('scene', '容器启动失败: ' + e.message))
    .finally(() => { simRunning = false; });
}

function togglePanel(id) { document.getElementById(id).classList.toggle('minimized'); }

// ============== Start ==============
logEntry('system', '控制台就绪');
loadSceneList();
setTimeout(() => { if (ws && ws.readyState === WebSocket.OPEN) ws.send('all'); }, 500);
