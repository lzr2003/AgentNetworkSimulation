// ============== Log (must be first) ==============
let logBuffer = [];
function logEntry(field, event) {
const now = new Date();
const ts = now.getFullYear() + '-' +
           (now.getMonth()+1).toString().padStart(2,'0') + '-' +
           now.getDate().toString().padStart(2,'0') + ' ' +
           now.getHours().toString().padStart(2,'0') + ':' +
           now.getMinutes().toString().padStart(2,'0') + ':' +
           now.getSeconds().toString().padStart(2,'0') + '.' +
           now.getMilliseconds().toString().padStart(3,'0');
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
let entries = logBuffer.filter(e => checked.includes(e.field));
const countEl = document.getElementById('log-count');
if (countEl) countEl.textContent = entries.length;
container.innerHTML = entries.slice(-200).map(e =>
'<div class=log-entry><span class=ts>' + e.timestamp + '</span> <span class="lv lv-' + e.field + '">' + e.field + '</span> <span class=ev>' + e.event + '</span></div>'
).join('') || '<div class=log-entry><span class=ts>--</span> <span class=ev>无日志</span></div>';
if (autoscroll && wasAtBottom) container.scrollTop = container.scrollHeight;
}
function clearLogs() { logBuffer = []; renderLogs(); }

// ============== State ==============
const API = '/api';
function $id(id) { return document.getElementById(id); }
let agents = [];
let connections = [];
let ws = null;
let hoveredAgent = null;
let simRunning = false;
let terrNainMap = null;
let tickRunning = false;
let tickInterval = null;

// ============== Agent forwarding → React iframe ==============
const iframe = document.getElementById('campus-iframe');
let _relationships = [];
let _lastLogCount = 0;
function forwardAgents() {
  if (iframe && iframe.contentWindow) {
    iframe.contentWindow.postMessage({ type: 'agents', data: agents, relationships: _relationships }, '*');
  }
}

// Receive hover events from iframe → show tooltip in parent
window.addEventListener('message', (e) => {
  if (e.data?.type === 'agent-hover') {
    const found = e.data.data;
    const tt = document.getElementById('tooltip');
    if (found) {
      hoveredAgent = found;
      const statusLabel = { idle:'空闲', running:'运行中', paused:'已暂停', stopped:'已停止', error:'异常', created:'已创建' };
      const roleLabel = { scout:'侦察兵', commander:'指挥官', analyst:'分析师', support:'支援', generic:'通用', observer:'观察员' };
      let html = '<div class=tt-name>' + (found.name || found.agent_id) + '</div>';
      html += '<div class=tt-role>' + (roleLabel[found.role] || found.role) + '</div>';
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
      // Use mouse position from iframe, relative to parent panel
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
if (msg.type === 'status' || msg.type === 'all') {
agents = msg.data.agents || [];
if (msg.data.relationships) _relationships = msg.data.relationships;
forwardAgents();
const logs = msg.data.agent_logs || [];
logs.slice(_lastLogCount).forEach(l => {
if (l.event === 'packet_send') logEntry('message', l.detail + ' [' + (l.agent_name || l.agent_id) + ']');
});
_lastLogCount = logs.length;
}
if (msg.type === 'packets' && msg.data) {
// packet stats received
}
if (msg.type === 'message') {
logEntry('message', '消息包: ' + (msg.data.payload?.action || msg.data.type) + ' [' + msg.data.source + ' → ' + msg.data.target + ']');
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
    sel.innerHTML = '<option value="">选择场景脚本</option>';
    data.scenes.forEach(s => {
      const opt = document.createElement('option');
      const val = typeof s === 'string' ? s : s.name;
      opt.value = val;
      opt.textContent = typeof s === 'string' ? val.replace('.json', '') : val;
      // Store format info
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

  // Folder format: pass scene name directly, backend parses the folder
  let body;
  if (format === 'folder') {
    body = { scene: name, name: name };
  } else {
    // Legacy .json file: load and extract script_json
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

  // ── Step 1: Setup — render agents on map immediately ──
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

  // ── Step 2: Launch — fire & forget, WebSocket pushes updates ──
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
