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
let draggingId = null;
let _relationships = [];
let _lastLogCount = 0;

// ============== Communication Events (data flow animation) ==============
const _commEvents = [];
const COMM_EVENT_TTL = 2000; // 粒子动画持续 2 秒

function purgeCommEvents(now) {
  for (let i = _commEvents.length - 1; i >= 0; i--) {
    if (now - _commEvents[i].timestamp > COMM_EVENT_TTL) {
      _commEvents.splice(i, 1);
    }
  }
}

// ============== Client-side Force Simulation State ==============
let _simState = new Map(); // agent_id → { x, y } in world coords

// ============== Canvas Agent Rendering ==============
const STATUS_COLORS = {
  idle: '#6B8A5E', running: '#5A7A9A', paused: '#B8783A',
  stopped: '#C0392B', error: '#C0392B', created: '#8B8475',
  decided: '#7A6A8A', messaged: '#5A7A9A', send_failed: '#C0392B', analyzed: '#B8783A',
};

const canvas = document.getElementById('agent-canvas');
const ctx = canvas?.getContext('2d');

function resizeCanvas() {
  const container = document.getElementById('canvas-panel');
  if (!canvas || !container) return;
  const rect = container.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  canvas.style.width = rect.width + 'px';
  canvas.style.height = rect.height + 'px';
}

window.addEventListener('resize', resizeCanvas);
setTimeout(resizeCanvas, 100);

function getScreenMapping(agents) {
  const dpr = window.devicePixelRatio || 1;
  const canvasW = canvas.width / dpr;
  const canvasH = canvas.height / dpr;
  const margin = 60;
  const worldW = canvasW - margin * 2;
  const worldH = canvasH - margin * 2;

  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  agents.forEach(a => {
    if (a.x == null) return;
    const sp = _simState.get(a.agent_id);
    const ax = sp ? sp.x : a.x;
    const ay = sp ? sp.y : a.y;
    minX = Math.min(minX, ax); maxX = Math.max(maxX, ax);
    minY = Math.min(minY, ay); maxY = Math.max(maxY, ay);
  });

  const rangeX = maxX - minX || 1;
  const rangeY = maxY - minY || 1;
  const scale = Math.min(worldW / rangeX, worldH / rangeY) * 0.85;
  const offsetX = margin + (worldW - rangeX * scale) / 2;
  const offsetY = margin + (worldH - rangeY * scale) / 2;

  return {
    valid: isFinite(minX),
    // When force-simulated position exists, use it instead of raw agent position
    getPos: function(agentId) {
      const sp = _simState.get(agentId);
      if (sp) return sp;
      const a = agents.find(a => a.agent_id === agentId);
      return (a && a.x != null) ? { x: a.x, y: a.y } : null;
    },
    toScreen: function(x, y) {
      return {
        sx: offsetX + (x - minX) * scale,
        sy: offsetY + (y - minY) * scale,
      };
    },
    toWorld: function(sx, sy) {
      return {
        wx: minX + (sx - offsetX) / scale,
        wy: minY + (sy - offsetY) / scale,
      };
    },
  };
}

function drawRelationships(agents, relationships, time) {
  if (!ctx || !relationships.length || !agents.length) return;
  const agentMap = {};
  agents.forEach(a => { agentMap[a.agent_id.toLowerCase()] = a; });

  const mapping = getScreenMapping(agents);
  if (!mapping.valid) return;

  const now = time || performance.now();
  purgeCommEvents(now);

  ctx.save();
  ctx.lineCap = 'round';

  for (const rel of relationships) {
    const fromPos = mapping.getPos(rel.from.toLowerCase());
    const toPos = mapping.getPos(rel.to.toLowerCase());
    if (!fromPos || !toPos) continue;

    const from = mapping.toScreen(fromPos.x, fromPos.y);
    const to = mapping.toScreen(toPos.x, toPos.y);

    const dx = to.sx - from.sx;
    const dy = to.sy - from.sy;
    const len = Math.sqrt(dx * dx + dy * dy);
    if (len === 0) continue;

    // ── 静态基线 ──
    const isCooperative = (rel.value || 0) > 0;
    const alpha = Math.min(1, Math.abs(rel.value || 50) / 100 + 0.15);
    const color = isCooperative
      ? `rgba(107,138,94,${alpha.toFixed(2)})`
      : `rgba(192,57,43,${alpha.toFixed(2)})`;

    ctx.beginPath();
    ctx.moveTo(from.sx, from.sy);
    ctx.lineTo(to.sx, to.sy);
    ctx.setLineDash([4, 3]);
    ctx.lineWidth = 0.6;
    ctx.strokeStyle = color;
    ctx.stroke();

    // ── 数据流动画：沿连线绘制流动粒子 ──
    const fromKey = rel.from.toLowerCase();
    const toKey = rel.to.toLowerCase();
    const commOnEdge = _commEvents.find(c =>
      c.from === fromKey && c.to === toKey
    );
    if (commOnEdge) {
      const age = now - commOnEdge.timestamp;
      if (age < COMM_EVENT_TTL) {
        const fadeAlpha = 1 - age / COMM_EVENT_TTL;
        const flowSpeed = 0.04; // px/ms
        ctx.fillStyle = `rgba(90,122,154,${fadeAlpha.toFixed(2)})`;
        // 绘制 3 个流动粒子，均匀分布
        for (let p = 0; p < 3; p++) {
          const offset = ((age * flowSpeed + p * len / 3) % len + len) % len;
          const px = from.sx + (dx / len) * offset;
          const py = from.sy + (dy / len) * offset;
          ctx.beginPath();
          ctx.arc(px, py, 2.2, 0, Math.PI * 2);
          ctx.fill();
        }
      }
    }
  }
  ctx.setLineDash([]);
  ctx.restore();
}

function drawAgents(agents, hoveredId, time) {
  if (!ctx || !agents.length) return;

  const mapping = getScreenMapping(agents);
  if (!mapping.valid) return;

  const dpr = window.devicePixelRatio || 1;
  const canvasW = canvas.width / dpr;
  const r = Math.max(6, Math.min(16, canvasW / 40));

  // ── Client-side force simulation (prevent overlap) ──
  const now = time || performance.now();
  const maxX = 400;
  const margin = r * 2;

  // Init new agents
  for (const a of agents) {
    if (a.x == null || a.y == null) continue;
    if (!_simState.has(a.agent_id)) {
      _simState.set(a.agent_id, { x: a.x, y: a.y });
    }
  }
  // Remove stale agents
  const activeIds = new Set(agents.map(a => a.agent_id));
  for (const id of _simState.keys()) { if (!activeIds.has(id)) _simState.delete(id); }

  // Build adjacency from relationships
  const adj = new Map();
  for (const rel of _relationships) {
    const f = rel.from.toLowerCase();
    const t = rel.to.toLowerCase();
    if (!adj.has(f)) adj.set(f, new Set());
    if (!adj.has(t)) adj.set(t, new Set());
    adj.get(f).add(t); adj.get(t).add(f);
  }

  const entries = Array.from(_simState.entries());
  const n = entries.length;
  const minDist = r * 10;
  const damping = 0.12;

  for (let i = 0; i < n; i++) {
    const [id, pi] = entries[i];
    // 拖动中的 agent 不参与力模拟
    if (id === draggingId) continue;
    let fx = 0, fy = 0;
    const neighbors = adj.get(id);

    for (let j = 0; j < n; j++) {
      if (i === j) continue;
      const [jid, pj] = entries[j];
      const dx = pi.x - pj.x;
      const dy = pi.y - pj.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const isLinked = neighbors?.has(jid);

      if (d < minDist) {
        const force = (minDist - d) / minDist * (isLinked ? 2 : 1);
        fx += (dx / d) * force;
        fy += (dy / d) * force;
      } else if (isLinked && d < minDist * 4) {
        fx -= dx * 0.02;
        fy -= dy * 0.02;
      }
    }

    pi.x += fx * damping;
    pi.y += fy * damping;
    pi.x = Math.max(margin, Math.min(maxX - margin, pi.x));
    pi.y = Math.max(margin, Math.min(maxX - margin, pi.y));
  }

  // ── Draw agents ──
  for (const a of agents) {
    if (a.x == null || a.y == null) continue;
    const sp = _simState.get(a.agent_id);
    const wx = sp ? sp.x : a.x;
    const wy = sp ? sp.y : a.y;
    const p = mapping.toScreen(wx, wy);
    const color = STATUS_COLORS[a.status] || '#8B8475';

    // Selection ring for hovered agent
    if (hoveredId === a.agent_id) {
      ctx.beginPath(); ctx.arc(p.sx, p.sy, r + 3, 0, Math.PI * 2);
      ctx.strokeStyle = color; ctx.lineWidth = 2;
      ctx.setLineDash([2, 2]); ctx.stroke(); ctx.setLineDash([]);
    }

    // Body
    ctx.beginPath(); ctx.arc(p.sx, p.sy, r, 0, Math.PI * 2);
    ctx.fillStyle = color + 'cc';
    ctx.fill();
    ctx.strokeStyle = color;
    ctx.lineWidth = 0.8;
    ctx.stroke();

    // ── 思考动画：decided / analyzed 状态时绘制旋转弧线 ──
    if (a.status === 'decided' || a.status === 'analyzed') {
      const spinAngle = (now / 600) % (Math.PI * 2); // 每 600ms 转一圈
      const arcLen = Math.PI * 1.4; // 约 252°
      ctx.save();
      ctx.beginPath();
      ctx.arc(p.sx, p.sy, r + 3.5, spinAngle, spinAngle + arcLen);
      ctx.strokeStyle = color + '99';
      ctx.lineWidth = 1.5;
      ctx.setLineDash([4, 3]);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.restore();
    }

    // Label
    ctx.fillStyle = '#2A2A2A';
    ctx.font = '10px Inter, IBM Plex Sans, system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(a.name || a.agent_id, p.sx, p.sy - r - 4);
    ctx.textAlign = 'start';
  }
}

function render() {
  requestAnimationFrame(render);
  if (!ctx) return;
  const dpr = window.devicePixelRatio || 1;
  ctx.save();
  ctx.scale(dpr, dpr);

  const canvasW = canvas.width / dpr;
  const canvasH = canvas.height / dpr;
  const now = performance.now();

  // Clear canvas with paper background
  ctx.fillStyle = '#ECE8DF';
  ctx.fillRect(0, 0, canvasW, canvasH);

  if (agents.length > 0) {
    drawRelationships(agents, _relationships, now);
    drawAgents(agents, hoveredAgent?.agent_id, now);
  }

  ctx.restore();
}
render();

// ============== Canvas Mouse Events → Tooltip ==============
const statusLabel = { idle:'空闲', running:'运行中', paused:'已暂停', stopped:'已停止', error:'异常', created:'已创建', decided:'已决策', messaged:'已发送', send_failed:'发送失败', analyzed:'分析中' };
const roleLabel = { scout:'侦察兵', commander:'指挥官', analyst:'分析师', support:'支援', brain:'Brain', 'claude-code':'Claude Code', openclaw:'OpenClaw', observer:'观察员' };
const backendLabel = { brain:'Brain', 'claude-code':'Claude Code', openclaw:'OpenClaw' };

function showTooltip(agent, mx, my) {
  const tt = document.getElementById('tooltip');
  if (!agent) {
    hoveredAgent = null;
    tt.style.display = 'none';
    return;
  }
  hoveredAgent = agent;
  let html = '<div class=tt-name>' + (agent.name || agent.agent_id) + '</div>';
  const backend = (agent.extra_meta || {}).backend || '';
  html += '<div class=tt-role>' + (roleLabel[agent.role] || backendLabel[backend] || agent.role) + '</div>';
  html += '<div class=tt-row><span class=lbl>ID</span><span class=val>' + agent.agent_id + '</span></div>';
  html += '<div class=tt-row><span class=lbl>状态</span><span class=val>' + (statusLabel[agent.status] || agent.status) + '</span></div>';
  if (agent.x !== undefined) {
    html += '<div class=tt-row><span class=lbl>坐标</span><span class=val>(' + agent.x.toFixed(0) + ', ' + agent.y.toFixed(0) + ')</span></div>';
  }
  const tasks = agent.pending_task_descs || [];
  if (tasks.length > 0) { html += '<div class=tt-section>任务</div>'; tasks.forEach((t, i) => { html += '<div class=tt-task><span class=tt-task-n>' + (i+1) + '.</span> ' + t + '</div>'; }); }
  const meta = agent.extra_meta || {};
  if (meta.core_goal) { html += '<div class=tt-section>目标</div><div class=tt-task>' + meta.core_goal + '</div>'; }
  if (meta.hidden_secret) { html += '<div class=tt-section>秘密</div><div class=tt-task style=color:#C0392B>' + meta.hidden_secret + '</div>'; }
  if (meta.action_space && meta.action_space.length) {
    html += '<div class=tt-section>行动</div><div class=tt-skills>' + meta.action_space.map(a => '<span class=tt-tag>' + a + '</span>').join('') + '</div>';
  }
  tt.innerHTML = html;
  tt.style.display = 'block';
  const panelRect = document.getElementById('canvas-panel')?.getBoundingClientRect();
  const tx = mx + (panelRect?.left || 0);
  const ty = my + (panelRect?.top || 0);
  tt.style.left = Math.min(tx + 16, window.innerWidth - 300) + 'px';
  tt.style.top = Math.max(4, ty - 10) + 'px';
}

// ── Find agent at screen position ──
function findAgentAtScreen(mx, my) {
  const mapping = getScreenMapping(agents);
  if (!mapping.valid) return null;
  const dpr = window.devicePixelRatio || 1;
  const canvasW = canvas.width / dpr;
  const rPx = Math.max(6, Math.min(16, canvasW / 40)) + 3;

  for (let i = agents.length - 1; i >= 0; i--) {
    const a = agents[i];
    if (a.x == null) continue;
    const sp = _simState.get(a.agent_id);
    const wx = sp ? sp.x : a.x;
    const wy = sp ? sp.y : a.y;
    const p = mapping.toScreen(wx, wy);
    const dx = mx - p.sx, dy = my - p.sy;
    if (Math.sqrt(dx * dx + dy * dy) < rPx) return a;
  }
  return null;
}

canvas?.addEventListener('mousedown', function(e) {
  const rect = canvas.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;
  const found = findAgentAtScreen(mx, my);
  if (found) {
    draggingId = found.agent_id;
    canvas.style.cursor = 'grabbing';
    e.preventDefault();
  }
});

canvas?.addEventListener('mousemove', function(e) {
  const rect = canvas.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;

  if (draggingId) {
    // 拖动中：更新 _simState 中的位置
    const mapping = getScreenMapping(agents);
    if (mapping.valid) {
      const world = mapping.toWorld(mx, my);
      _simState.set(draggingId, { x: world.wx, y: world.wy });
    }
    return;
  }

  const found = findAgentAtScreen(mx, my);
  canvas.style.cursor = found ? 'grab' : '';
  if (found !== hoveredAgent) {
    showTooltip(found, mx, my);
  }
});

canvas?.addEventListener('mouseup', function(e) {
  if (draggingId) {
    const pos = _simState.get(draggingId);
    if (pos) {
      // 同步位置到 agents 数组
      const agent = agents.find(a => a.agent_id === draggingId);
      if (agent) { agent.x = Math.round(pos.x); agent.y = Math.round(pos.y); }
    }
    draggingId = null;
    canvas.style.cursor = '';
  }
});

canvas?.addEventListener('mouseleave', function() {
  if (draggingId) {
    const pos = _simState.get(draggingId);
    if (pos) {
      const agent = agents.find(a => a.agent_id === draggingId);
      if (agent) { agent.x = Math.round(pos.x); agent.y = Math.round(pos.y); }
    }
    draggingId = null;
    canvas.style.cursor = '';
  }
  showTooltip(null, 0, 0);
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
    _lastLogCount++;
    // ── 记录通信事件，用于数据流动画 ──
    if ((action === 'send_message' || action === 'broadcast') && status === 'success' && from !== '?' && to) {
      _commEvents.push({ from: from.toLowerCase(), to: to.toLowerCase(), timestamp: Date.now() });
      if (_commEvents.length > 20) _commEvents.shift();
    }
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
    // Reset simState on full sync (new simulation)
    if (msg.type === 'all') { _simState = new Map(); }
    agents = msg.data.agents || [];
    if (msg.data.relationships !== undefined && (msg.data.relationships.length > 0 || agents.length === 0)) _relationships = msg.data.relationships;
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
    if (sel) sel.innerHTML = '';
    data.scenes.forEach(s => {
      const val = typeof s === 'string' ? s : s.name;
      const label = typeof s === 'string' ? val.replace('.json', '') : val;
      if (sel) {
        const opt = document.createElement('option');
        opt.value = val; opt.textContent = label;
        if (typeof s !== 'string') opt.dataset.format = s.format;
        sel.appendChild(opt);
      }
    });
  } catch(e) { console.error('loadSceneList', e); }
}

function onSceneSelect() {}

// ============== Float Panel ==============
function loadScenePanel(name) {
  const iframe = document.getElementById('fp-iframe');
  const titleEl = document.getElementById('fp-title');
  if (iframe && name) {
    iframe.src = API + '/scenes/' + encodeURIComponent(name) + '/panel';
  }
  if (titleEl) titleEl.textContent = name || '场景面板';
}

function toggleFloatPanel() {
  document.getElementById('float-panel').classList.toggle('collapsed');
}

async function runSelectedScene() {
  const sel = document.getElementById('scene-selector');
  const name = sel?.value;
  if (!name) { logEntry('scene', '请先选择场景脚本'); return; }

	// 清空上轮仿真的前端日志和状态
	clearLogs();
	_lastLogCount = 0;
  _simState = new Map();
  _commEvents.length = 0;

  logEntry('scene', '=== ' + name + ' ===');

  // 同步悬浮窗面板
  loadScenePanel(name);

  const body = { scene: name, name: name };

  try {
    const r1 = await fetch(API + '/simulations/setup', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    });
    if (!r1.ok) throw new Error((await r1.text()).slice(0, 200));
    const d1 = await r1.json();
    if (d1.relationships) { _relationships = d1.relationships; }
    if (ws && ws.readyState === WebSocket.OPEN) ws.send('all');
    logEntry('scene', '场景就绪: ' + (d1.agent_stats?.total_agents || 0) + ' Agent');
  } catch(e) { logEntry('scene', '场景构建失败: ' + e.message); return; }

  simRunning = true;
  fetch(API + '/simulations/launch', { method: 'POST' })
    .then(r => r.ok ? r.json() : r.text().then(t => { throw new Error(t.slice(0, 200)); }))
    .then(d => {
      if (d.error) { logEntry('scene', '容器: ' + d.error); return; }
      logEntry('scene', '仿真完成: ' + (d.duration_seconds||0) + 's | ' + (d.agent_stats?.total_agents||0) + ' Agent');
      if (d.relationships) { _relationships = d.relationships; }
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
