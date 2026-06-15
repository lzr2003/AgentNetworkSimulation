// ============== Log (must be first) ==============
// ── 归一化：将新旧格式统一为前端展示模型 ──
const _seenLogKeys = new Set();  // 去重：session_id+seq 或复合 key
const MAX_SEEN_KEYS = 2000;

function normalizeLogRecord(record, origin) {
  // origin: 'frontend' (本地产生) | 'ws_agent_log' | 'ws_log_entries' | 'ws_packets'
  const ts = (record.timestamp || '').slice(11, 24) || '';
  const cat = record.category || '';
  const src = record.source || '';
  const evt = record.event || '';

  // ── 生成去重 key ──
  let dedupKey = null;
  if (record.session_id && record.seq) {
    dedupKey = record.session_id + '|' + record.seq;
  }
  if (!dedupKey && origin !== 'frontend') {
    // 兼容旧格式：时间+来源+事件+actor+message 组合
    const actorId = (record.actor || {}).id || record.agent_id || '';
    dedupKey = [ts, src || origin, evt, actorId, (record.message || '').slice(0, 40)].join('|');
  }
  if (dedupKey && _seenLogKeys.has(dedupKey)) return null;
  if (dedupKey) {
    _seenLogKeys.add(dedupKey);
    if (_seenLogKeys.size > MAX_SEEN_KEYS) {
      const iter = _seenLogKeys.values();
      for (let i = 0; i < 500; i++) _seenLogKeys.delete(iter.next().value);
    }
  }

  // ── 统一展示模型 ──
  const result = { timestamp: ts, raw: record, origin };

  // 分类映射
  if (cat === 'frontend' || src === 'frontend' || origin === 'frontend') {
    result.field = 'frontend';
  } else if (cat === 'llm_api') {
    result.field = 'llm_api';
  } else if (cat === 'network_capture') {
    result.field = 'network_capture';
  } else if (cat === 'communication' || evt === 'agent_message') {
    result.field = 'message';
  } else if (cat === 'agent_behavior' || evt === 'decide' || evt === 'act' || evt === 'agent_action' || evt === 'agent_decide') {
    result.field = 'agent';
  } else if (evt === 'event_trigger' || evt === 'session_start' || evt === 'simulation_complete' || cat === 'lifecycle') {
    result.field = 'scene';
  } else if (record.level === 'ERROR') {
    result.field = 'system';
  } else {
    result.field = 'system';
  }

  result.level = record.level || 'INFO';
  result.source = src || origin || 'unknown';
  result.event = evt || '';
  result.actor = (record.actor || {}).id || record.agent_id || record.from_agent || '';
  result.target = (record.target || {}).id || record.to_agent || '';
  result.action = (record.action || {}).name || record.action || '';
  result.status = (record.action || {}).status || record.action_status || '';

  // ── 生成展示文案 ──
  const payload = record.payload || {};
  const details = record.details || {};
  const network = record.network || {};

  if (result.field === 'message') {
    const from = result.actor || (record.details || {}).from || (record.network || {}).agent_from || '';
    const to = result.target || (record.details || {}).to || (record.network || {}).agent_to || '';
    const content = payload.content || (record.details || {}).content || record.message || '';
    result.eventText = (from ? from + ' → ' + to : record.message || '');
    result.detailText = content.length > 200 ? content.slice(0, 200) + '…' : content;
  } else if (result.field === 'llm_api') {
    const tgt = record.target || {};
    const pl = record.payload || {};
    const net = record.network || {};
    result.eventText = 'LLM ' + (tgt.provider || '') + '/' + (tgt.model || '') + ' → ' + (record.action?.status || '');
    result.detailText = (net.latency_ms ? net.latency_ms + 'ms ' : '') + (pl.prompt_chars ? pl.prompt_chars + '→' + pl.response_chars + 'ch' : '');
  } else if (result.field === 'network_capture') {
    const tgt = record.target || {};
    const net = record.network || {};
    const pl = record.payload || {};
    result.eventText = 'CAP ' + (record.actor?.id || '') + ' → ' + (tgt.host||tgt.ip||'?') + ':' + (tgt.port||'');
    result.detailText = (pl.line_summary || '') + ' ' + (net.protocol||'');
  } else if (result.field === 'agent') {
    const st = result.status;
    const stIcon = st === 'success' ? '✅' : st === 'failed' ? '❌' : st === 'decided' ? '💭' : '➡️';
    result.eventText = result.actor + ' ' + stIcon + ' ' + (result.action || result.event);
    result.detailText = record.message || payload.content || (record.details || {}).detail || '';
  } else if (result.field === 'scene') {
    result.eventText = (evt === 'event_trigger' ? '⚡ ' : '') + (record.message || '');
    result.detailText = '';
  } else if (result.field === 'system' && result.level === 'ERROR') {
    result.eventText = '❌ ' + (result.actor || '') + ' | ' + (record.message || '');
    result.detailText = '';
  } else {
    result.eventText = record.message || evt || '';
    result.detailText = '';
  }

  return result;
}

// ── 本地前端日志（写入内存 + 上报后端） ──
let logBuffer = [];
let _logIngestQueue = [];
let _logIngestTimer = null;
function logEntry(field, event, overrideTs) {
const now = new Date(Date.now() + _serverTimeOffset);
const ts = overrideTs || (now.getHours().toString().padStart(2,'0') + ':' +
           now.getMinutes().toString().padStart(2,'0') + ':' +
           now.getSeconds().toString().padStart(2,'0') + '.' +
           now.getMilliseconds().toString().padStart(3,'0'));
const rec = { timestamp: ts, field, event, source: 'frontend', origin: 'frontend',
              eventText: event, detailText: '', level: 'INFO', actor: '', target: '',
              action: '', status: '', raw: null };
logBuffer.push(rec);
if (logBuffer.length > 500) logBuffer.shift();
if (_logFlushTimer) clearTimeout(_logFlushTimer);
_logFlushTimer = setTimeout(renderLogs, 16);
// 异步上报到后端全局日志
_logIngestQueue.push({
  timestamp: now.toISOString(),
  source: 'frontend',
  component: 'dashboard',
  category: 'frontend',
  event: field,
  message: event,
});
if (!_logIngestTimer) {
  _logIngestTimer = setTimeout(() => {
    const batch = _logIngestQueue.splice(0);
    _logIngestTimer = null;
    for (const r of batch) {
      fetch('/api/logs/ingest', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(r),
      }).catch(() => {});
    }
  }, 2000);
}
}
let _logFlushTimer = null;
function renderLogs() {
const checked = [...document.querySelectorAll('#log-fields input:checked')].map(cb => cb.dataset.field);
const container = document.getElementById('log-entries');
const autoscroll = document.getElementById('log-autoscroll')?.checked;
if (!container) return;
const wasAtBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 30;
const expandedTexts = new Set();
container.querySelectorAll('.log-entry.expanded').forEach(el => {
  const evEl = el.querySelector('.ev');
  const tsEl = el.querySelector('.ts');
  if (evEl && tsEl) expandedTexts.add(evEl.textContent + '|' + tsEl.textContent);
});
let entries = logBuffer.filter(e => checked.includes(e.field));
const countEl = document.getElementById('log-count');
if (countEl) countEl.textContent = entries.length;

// 使用 DOM 构建，避免 innerHTML XSS
container.replaceChildren();
const fragment = document.createDocumentFragment();
const displayEntries = entries.slice(-200);
if (!displayEntries.length) {
  const div = document.createElement('div');
  div.className = 'log-entry';
  const tsSpan = document.createElement('span');
  tsSpan.className = 'ts';
  tsSpan.textContent = '--';
  const evSpan = document.createElement('span');
  evSpan.className = 'ev';
  evSpan.textContent = '无日志';
  div.appendChild(tsSpan);
  div.appendChild(evSpan);
  fragment.appendChild(div);
} else {
  displayEntries.forEach(e => {
    const div = document.createElement('div');
    div.className = 'log-entry';
    div.addEventListener('click', function() { this.classList.toggle('expanded'); });

    const tsSpan = document.createElement('span');
    tsSpan.className = 'ts';
    tsSpan.textContent = e.timestamp;

    const lvlClass = 'lv lv-' + e.field + (e.level === 'ERROR' ? ' lv-err' : e.level === 'WARN' ? ' lv-warn' : '');
    const lvSpan = document.createElement('span');
    lvSpan.className = lvlClass;
    lvSpan.textContent = e.field;

    const evSpan = document.createElement('span');
    evSpan.className = 'ev';
    evSpan.textContent = e.eventText || '';

    div.appendChild(tsSpan);
    div.appendChild(lvSpan);
    div.appendChild(evSpan);

    if (e.detailText) {
      const detailSpan = document.createElement('span');
      detailSpan.className = 'ev-detail';
      detailSpan.textContent = ' | ' + e.detailText;
      evSpan.appendChild(detailSpan);
    }

    // 恢复展开状态
    const key = evSpan.textContent + '|' + tsSpan.textContent;
    if (expandedTexts.has(key)) div.classList.add('expanded');

    fragment.appendChild(div);
  });
}
container.appendChild(fragment);
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
let _serverTimeOffset = 0; // ms, 服务端与浏览器时差

// ============== Persistent Viewport State ==============
const viewport = {
  scale: 1,
  offsetX: 0,    // world coordinate at canvas centre
  offsetY: 0,
  initialized: false,
  userControlled: false,
  MIN_SCALE: 0.15,
  MAX_SCALE: 4.0,
};

// ============== Coordinate Conversion ==============
// All coords are CSS-pixel based; ctx.scale(dpr,dpr) is applied in render().
function canvasCSS() {
  const dpr = window.devicePixelRatio || 1;
  return { w: canvas.width / dpr, h: canvas.height / dpr };
}

function worldToScreen(wx, wy) {
  const { w, h } = canvasCSS();
  const cx = w / 2, cy = h / 2;
  return {
    sx: cx + (wx - viewport.offsetX) * viewport.scale,
    sy: cy + (wy - viewport.offsetY) * viewport.scale,
  };
}

function screenToWorld(sx, sy) {
  const { w, h } = canvasCSS();
  const cx = w / 2, cy = h / 2;
  return {
    wx: viewport.offsetX + (sx - cx) / viewport.scale,
    wy: viewport.offsetY + (sy - cy) / viewport.scale,
  };
}

function fitViewportToAgents() {
  if (!agents.length) {
    viewport.initialized = false;
    return;
  }
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  agents.forEach(a => {
    if (a.x == null || a.y == null) return;
    const sp = _simState.get(a.agent_id);
    const ax = sp ? sp.x : a.x;
    const ay = sp ? sp.y : a.y;
    minX = Math.min(minX, ax); maxX = Math.max(maxX, ax);
    minY = Math.min(minY, ay); maxY = Math.max(maxY, ay);
  });
  if (!isFinite(minX)) { viewport.initialized = false; return; }

  const { w, h } = canvasCSS();
  const margin = 60;
  const rangeX = maxX - minX || 1;
  const rangeY = maxY - minY || 1;
  viewport.scale = Math.min((w - margin * 2) / rangeX, (h - margin * 2) / rangeY) * 0.85;
  viewport.offsetX = (minX + maxX) / 2;
  viewport.offsetY = (minY + maxY) / 2;
  viewport.initialized = true;
}

// ============== Communication Trajectories ==============
// Each message (send_message / broadcast success) produces one trajectory.
const _trajectories = [];
const TRAJECTORY_DURATION = 2200;   // travel time (ms)
const TRAJECTORY_FADE = 500;        // fade-out after arrival (ms)
const TRAJECTORY_MAX_AGE = TRAJECTORY_DURATION + TRAJECTORY_FADE + 200;
const MAX_TRAJECTORIES = 80;

// Per-edge message counter for parallel-offset (cycling 0..4)
const _edgeMsgIdx = {};

function pushCommEvent(fromId, toId, isBroadcast) {
  const key = fromId + '→' + toId;
  _edgeMsgIdx[key] = ((_edgeMsgIdx[key] || 0) + 1) % 5;
  _trajectories.push({
    from: fromId,
    to: toId,
    startTime: performance.now(),
    offsetIndex: _edgeMsgIdx[key],
    isBroadcast: !!isBroadcast,
  });
  // Trim old events when over capacity
  const now = performance.now();
  while (_trajectories.length > 0 && now - _trajectories[0].startTime > TRAJECTORY_MAX_AGE) {
    _trajectories.shift();
  }
  if (_trajectories.length > MAX_TRAJECTORIES) {
    _trajectories.splice(0, _trajectories.length - MAX_TRAJECTORIES);
  }
}

function getAgentWorldPos(agentId) {
  const sp = _simState.get(agentId);
  if (sp) return sp;
  const a = agents.find(ag => ag.agent_id === agentId);
  return (a && a.x != null) ? { x: a.x, y: a.y } : null;
}

function hasRelationship(fromId, toId) {
  return _relationships.some(r => {
    const f = r.from.toLowerCase();
    const t = r.to.toLowerCase();
    return (f === fromId && t === toId) || (f === toId && t === fromId);
  });
}

// ============== Client-side Force Simulation State ==============
let _simState = new Map(); // agent_id → { x, y } in world coords

// ============== Canvas Agent Rendering ==============
const STATUS_COLORS = {
  idle: '#38D5FF', running: '#2F8CFF', thinking: '#BFEAFF', acting: '#58F0C2', paused: '#FFBF5A',
  stopped: '#FF4E5E', error: '#FF4E5E', created: '#7B8EA5',
  decided: '#8EA7FF', messaged: '#58F0C2', send_failed: '#FF4E5E', analyzed: '#BFEAFF',
};

const canvas = document.getElementById('agent-canvas');
const ctx = canvas?.getContext('2d');
const REDUCED_MOTION = window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches || false;

function resizeCanvas() {
  const container = document.getElementById('canvas-stage');
  if (!canvas || !container) return;
  const rect = container.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  canvas.style.width = rect.width + 'px';
  canvas.style.height = rect.height + 'px';
  // Re-fit if user hasn't taken manual control
  if (!viewport.userControlled && agents.length > 0) {
    fitViewportToAgents();
  }
}

window.addEventListener('resize', resizeCanvas);
// 初始尺寸：等待两次布局帧确保 CSS 布局完成后获取准确尺寸
requestAnimationFrame(() => requestAnimationFrame(resizeCanvas));
// 监听容器布局变化（侧边栏折叠/展开等不触发 window.resize 的场景）
if (window.ResizeObserver) {
  const _ro = new ResizeObserver(() => resizeCanvas());
  const _stage = document.getElementById('canvas-stage');
  if (_stage) _ro.observe(_stage);
}
// 后台标签页暂停渲染循环
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) { resizeCanvas(); render(); }
});

function drawCommandBackground(canvasW, canvasH, now) {
  const pulse = REDUCED_MOTION ? 0 : now * 0.001;
  const bg = ctx.createLinearGradient(0, 0, canvasW, canvasH);
  bg.addColorStop(0, '#020815');
  bg.addColorStop(0.48, '#07182B');
  bg.addColorStop(1, '#020611');
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, canvasW, canvasH);

  const glow1 = ctx.createRadialGradient(canvasW * 0.18, canvasH * 0.10, 0, canvasW * 0.18, canvasH * 0.10, canvasW * 0.78);
  glow1.addColorStop(0, 'rgba(47,140,255,0.20)');
  glow1.addColorStop(0.44, 'rgba(47,140,255,0.045)');
  glow1.addColorStop(1, 'rgba(47,140,255,0)');
  ctx.fillStyle = glow1;
  ctx.fillRect(0, 0, canvasW, canvasH);

  const glow2 = ctx.createRadialGradient(canvasW * 0.82, canvasH * 0.42, 0, canvasW * 0.82, canvasH * 0.42, canvasW * 0.68);
  glow2.addColorStop(0, 'rgba(56,213,255,0.15)');
  glow2.addColorStop(0.50, 'rgba(56,213,255,0.035)');
  glow2.addColorStop(1, 'rgba(56,213,255,0)');
  ctx.fillStyle = glow2;
  ctx.fillRect(0, 0, canvasW, canvasH);

  const grid = 48;
  ctx.save();
  ctx.lineWidth = 0.7;
  ctx.strokeStyle = 'rgba(95,187,255,0.105)';
  ctx.beginPath();
  for (let x = -grid; x <= canvasW + grid; x += grid) {
    ctx.moveTo(x, 0);
    ctx.lineTo(x, canvasH);
  }
  for (let y = -grid; y <= canvasH + grid; y += grid) {
    ctx.moveTo(0, y);
    ctx.lineTo(canvasW, y);
  }
  ctx.stroke();
  ctx.restore();
}

// ── Draw permanent relationship lines ──
function drawRelationshipLines(relationships, agents) {
  if (!ctx || !relationships.length || !agents.length) return;

  ctx.save();
  ctx.lineCap = 'round';

  for (const rel of relationships) {
    const fromPos = getAgentWorldPos(rel.from.toLowerCase());
    const toPos = getAgentWorldPos(rel.to.toLowerCase());
    if (!fromPos || !toPos) continue;

    const from = worldToScreen(fromPos.x, fromPos.y);
    const to = worldToScreen(toPos.x, toPos.y);

    const dx = to.sx - from.sx;
    const dy = to.sy - from.sy;
    const len = Math.sqrt(dx * dx + dy * dy);
    if (len === 0) continue;

    const isCooperative = (rel.value || 0) > 0;
    const alpha = Math.min(1, Math.abs(rel.value || 50) / 100 + 0.22);
    const color = isCooperative
      ? `rgba(56,213,255,${alpha.toFixed(2)})`
      : `rgba(255,78,94,${alpha.toFixed(2)})`;
    const glow = isCooperative ? 'rgba(47,140,255,0.24)' : 'rgba(255,78,94,0.24)';

    ctx.beginPath();
    ctx.moveTo(from.sx, from.sy);
    ctx.lineTo(to.sx, to.sy);
    ctx.setLineDash([10, 7]);
    ctx.lineWidth = 3.4;
    ctx.strokeStyle = glow;
    ctx.shadowColor = glow;
    ctx.shadowBlur = 14;
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(from.sx, from.sy);
    ctx.lineTo(to.sx, to.sy);
    ctx.lineWidth = 1;
    ctx.strokeStyle = color;
    ctx.shadowBlur = 8;
    ctx.stroke();
  }
  ctx.setLineDash([]);
  ctx.shadowBlur = 0;
  ctx.restore();
}

// ── Draw a single trajectory (message envelope) ──
function drawOneTrajectory(traj, fromScr, toScr, now) {
  const age = now - traj.startTime;
  if (age > TRAJECTORY_MAX_AGE) return;

  const dx = toScr.sx - fromScr.sx;
  const dy = toScr.sy - fromScr.sy;
  const len = Math.sqrt(dx * dx + dy * dy);
  if (len < 1) return;

  // Perpendicular normal for offset
  const nx = -dy / len;
  const ny = dx / len;
  const off = (traj.offsetIndex - 2) * 5.5; // spread -11..11 px

  const progress = Math.min(1, age / TRAJECTORY_DURATION);
  const fadeOut = 1 - Math.max(0, (age - TRAJECTORY_DURATION) / TRAJECTORY_FADE);
  if (fadeOut <= 0.01) return;

  const headX = fromScr.sx + dx * progress + nx * off;
  const headY = fromScr.sy + dy * progress + ny * off;

  const packetColor = traj.isBroadcast ? '255,191,90' : '56,213,255';

  // ── High-energy packet trail ──
  const tailAlpha = fadeOut * 0.9;
  ctx.save();
  ctx.shadowColor = `rgba(${packetColor},0.9)`;
  ctx.shadowBlur = 14;
  for (let t = 0; t < 7; t++) {
    const tp = Math.max(0, progress - (t + 1) * 0.032);
    const tx = fromScr.sx + dx * tp + nx * off;
    const ty = fromScr.sy + dy * tp + ny * off;
    const tr = Math.max(0.7, 3.2 - t * 0.36);
    const ta = tailAlpha * (1 - t / 8);
    ctx.fillStyle = `rgba(${packetColor},${ta.toFixed(3)})`;
    ctx.beginPath();
    ctx.arc(tx, ty, tr, 0, Math.PI * 2);
    ctx.fill();
  }
  const trailStart = Math.max(0, progress - 0.18);
  ctx.beginPath();
  ctx.moveTo(fromScr.sx + dx * trailStart + nx * off, fromScr.sy + dy * trailStart + ny * off);
  ctx.lineTo(headX, headY);
  ctx.strokeStyle = `rgba(${packetColor},${(fadeOut * 0.62).toFixed(3)})`;
  ctx.lineWidth = 1.8;
  ctx.stroke();

  // ── Head (bright dot) ──
  ctx.fillStyle = `rgba(${packetColor},${fadeOut.toFixed(3)})`;
  ctx.beginPath();
  ctx.arc(headX, headY, traj.isBroadcast ? 4.2 : 3.6, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = `rgba(255,255,255,${(fadeOut * 0.8).toFixed(3)})`;
  ctx.beginPath();
  ctx.arc(headX, headY, 1.3, 0, Math.PI * 2);
  ctx.fill();

  // ── Destination pulse ring (after arrival) ──
  if (progress >= 1 && age - TRAJECTORY_DURATION < 700) {
    const pulseAge = age - TRAJECTORY_DURATION;
    const pulseR = 4 + pulseAge * 0.04;
    const pulseAlpha = Math.max(0, (1 - pulseAge / 700)) * fadeOut * 0.7;
    if (pulseAlpha > 0.01) {
      ctx.strokeStyle = `rgba(${packetColor},${pulseAlpha.toFixed(3)})`;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(toScr.sx + nx * off, toScr.sy + ny * off, pulseR, 0, Math.PI * 2);
      ctx.stroke();
    }
  }
  ctx.restore();
}

// ── Draw temporary link line for agents without permanent relationship ──
function drawTempLink(fromScr, toScr) {
  ctx.save();
  ctx.setLineDash([3, 7]);
  ctx.lineWidth = 1;
  ctx.strokeStyle = 'rgba(95,187,255,0.34)';
  ctx.shadowColor = 'rgba(56,213,255,0.24)';
  ctx.shadowBlur = 10;
  ctx.beginPath();
  ctx.moveTo(fromScr.sx, fromScr.sy);
  ctx.lineTo(toScr.sx, toScr.sy);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.restore();
}

// ── Draw all communication trajectories + optional temp links ──
function drawTrajectories(now) {
  if (!ctx || !_trajectories.length || !agents.length) return;

  // Purge expired
  let cut = 0;
  while (cut < _trajectories.length && now - _trajectories[cut].startTime > TRAJECTORY_MAX_AGE) {
    cut++;
  }
  if (cut > 0) _trajectories.splice(0, cut);

  if (!_trajectories.length) return;

  // Determine temp links needed (agents communicating without explicit relationship)
  const tempLinks = new Map(); // "from→to" → true
  const seenEdges = new Set();

  ctx.save();

  for (const traj of _trajectories) {
    const fromPos = getAgentWorldPos(traj.from);
    const toPos = getAgentWorldPos(traj.to);
    if (!fromPos || !toPos) continue;

    const from = worldToScreen(fromPos.x, fromPos.y);
    const to = worldToScreen(toPos.x, toPos.y);

    const edgeKey = traj.from + '→' + traj.to;
    if (!hasRelationship(traj.from, traj.to) && !tempLinks.has(edgeKey)) {
      tempLinks.set(edgeKey, { from, to });
    }

    drawOneTrajectory(traj, from, to, now);
  }

  // Draw temp links behind trajectories (draw after trajectories so they're visible underneath)
  // Actually we need to draw them before trajectories. Let's reorder:
  // This is handled in the main drawRelationshipsComposite which draws lines first,
  // so we just collect them here.

  ctx.restore();

  return tempLinks;
}

// ── Composite: relationships + trajectories ──
function drawRelationshipsComposite(agents, relationships, now) {
  if (!ctx || !agents.length) return;

  // Auto-fit on first render with agents
  if (!viewport.initialized) {
    fitViewportToAgents();
  }

  // 1. Permanent relationship lines
  drawRelationshipLines(relationships, agents);

  // 2. Collect temp links and draw trajectories
  if (!_trajectories.length) return;

  // Purge expired
  let cut = 0;
  while (cut < _trajectories.length && now - _trajectories[cut].startTime > TRAJECTORY_MAX_AGE) {
    cut++;
  }
  if (cut > 0) _trajectories.splice(0, cut);
  if (!_trajectories.length) return;

  // Gather temp links
  const tempLinks = [];
  const drawnTemp = new Set();

  ctx.save();

  for (const traj of _trajectories) {
    const fromPos = getAgentWorldPos(traj.from);
    const toPos = getAgentWorldPos(traj.to);
    if (!fromPos || !toPos) continue;

    const from = worldToScreen(fromPos.x, fromPos.y);
    const to = worldToScreen(toPos.x, toPos.y);

    const edgeKey = traj.from + '→' + traj.to;
    if (!hasRelationship(traj.from, traj.to) && !drawnTemp.has(edgeKey)) {
      drawnTemp.add(edgeKey);
      drawTempLink(from, to);
    }

    drawOneTrajectory(traj, from, to, now);
  }

  ctx.restore();
}

// ── Draw agents ──
function drawAgents(agents, hoveredId, time) {
  if (!ctx || !agents.length) return;

  const now = time || performance.now();
  const { w: canvasW, h: canvasH } = canvasCSS();
  const r = Math.max(6, Math.min(16, Math.min(canvasW, canvasH) / 40));

  // ── Client-side force simulation (prevent overlap) ──
  // Convert screen-pixel thresholds to world-space using current scale
  const worldR = r / viewport.scale;
  const minDistWorld = worldR * 10;
  const damping = 0.12;

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

  // Compute world bounds for clamping
  let minWX = Infinity, maxWX = -Infinity, minWY = Infinity, maxWY = -Infinity;
  for (const [, p] of entries) {
    minWX = Math.min(minWX, p.x); maxWX = Math.max(maxWX, p.x);
    minWY = Math.min(minWY, p.y); maxWY = Math.max(maxWY, p.y);
  }
  const padX = Math.max((maxWX - minWX) * 0.5, 100 / viewport.scale);
  const padY = Math.max((maxWY - minWY) * 0.5, 100 / viewport.scale);

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

      if (d < minDistWorld) {
        const force = (minDistWorld - d) / minDistWorld * (isLinked ? 2 : 1);
        fx += (dx / d) * force;
        fy += (dy / d) * force;
      } else if (isLinked && d < minDistWorld * 4) {
        fx -= dx * 0.02;
        fy -= dy * 0.02;
      }
    }

    pi.x += fx * damping;
    pi.y += fy * damping;
    pi.x = Math.max(minWX - padX, Math.min(maxWX + padX, pi.x));
    pi.y = Math.max(minWY - padY, Math.min(maxWY + padY, pi.y));
  }

  // ── Draw agents ──
  for (const a of agents) {
    if (a.x == null || a.y == null) continue;
    const sp = _simState.get(a.agent_id);
    const wx = sp ? sp.x : a.x;
    const wy = sp ? sp.y : a.y;
    const p = worldToScreen(wx, wy);
    const color = STATUS_COLORS[a.status] || '#7B8EA5';
    const pulse = REDUCED_MOTION ? 0.5 : (Math.sin(now / 360 + p.sx * 0.01) + 1) / 2;
    const active = ['thinking', 'acting', 'running', 'decided', 'messaged', 'analyzed'].includes(a.status);

    ctx.save();
    ctx.shadowColor = color;
    ctx.shadowBlur = active ? 20 + pulse * 10 : 10;

    if (hoveredId === a.agent_id) {
      const lock = r + 15;
      const notch = 7;
      ctx.strokeStyle = color + 'ff';
      ctx.lineWidth = 1.8;
      ctx.beginPath();
      ctx.moveTo(p.sx - lock, p.sy - lock + notch); ctx.lineTo(p.sx - lock, p.sy - lock); ctx.lineTo(p.sx - lock + notch, p.sy - lock);
      ctx.moveTo(p.sx + lock - notch, p.sy - lock); ctx.lineTo(p.sx + lock, p.sy - lock); ctx.lineTo(p.sx + lock, p.sy - lock + notch);
      ctx.moveTo(p.sx - lock, p.sy + lock - notch); ctx.lineTo(p.sx - lock, p.sy + lock); ctx.lineTo(p.sx - lock + notch, p.sy + lock);
      ctx.moveTo(p.sx + lock - notch, p.sy + lock); ctx.lineTo(p.sx + lock, p.sy + lock); ctx.lineTo(p.sx + lock, p.sy + lock - notch);
      ctx.stroke();
    }

    // Core
    const core = ctx.createRadialGradient(p.sx - r * 0.3, p.sy - r * 0.35, 0, p.sx, p.sy, r);
    core.addColorStop(0, '#FFFFFF');
    core.addColorStop(0.22, color);
    core.addColorStop(1, color + '88');
    ctx.fillStyle = core;
    ctx.beginPath();
    ctx.arc(p.sx, p.sy, r, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = '#BFEAFF';
    ctx.lineWidth = 0.8;
    ctx.stroke();

    // Active sweep arc (only when thinking)
    if (a.status === 'thinking') {
      const spinAngle = REDUCED_MOTION ? 0 : (now / 220) % (Math.PI * 2);
      ctx.setLineDash([4, 6]);
      ctx.beginPath();
      ctx.arc(p.sx, p.sy, r + 3, spinAngle, spinAngle + Math.PI * 1.35);
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.setLineDash([]);
    }
    ctx.restore();

    // Label
    ctx.save();
    ctx.fillStyle = '#EAF7FF';
    const labelSize = Math.max(9, Math.min(14, canvasW / 60));
    ctx.font = labelSize + 'px Inter, IBM Plex Sans, system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.strokeStyle = 'rgba(3,8,18,0.86)';
    ctx.lineWidth = 3;
    ctx.strokeText(a.name || a.agent_id, p.sx, p.sy - r - 4);
    ctx.fillText(a.name || a.agent_id, p.sx, p.sy - r - 4);
    ctx.textAlign = 'start';
    ctx.restore();
  }
}

function render() {
  if (!document.hidden) requestAnimationFrame(render);
  if (!ctx) return;
  const dpr = window.devicePixelRatio || 1;
  ctx.save();
  ctx.scale(dpr, dpr);

  const { w: canvasW, h: canvasH } = canvasCSS();
  const now = performance.now();

  drawCommandBackground(canvasW, canvasH, now);

  if (agents.length > 0) {
    drawRelationshipsComposite(agents, _relationships, now);
    drawAgents(agents, hoveredAgent?.agent_id, now);
  } else {
    ctx.save();
    ctx.fillStyle = 'rgba(191,234,255,0.76)';
    ctx.shadowColor = 'rgba(56,213,255,0.72)';
    ctx.shadowBlur = 18;
    ctx.font = '12px JetBrains Mono, IBM Plex Mono, monospace';
    ctx.textAlign = 'center';
    ctx.fillText('WAITING FOR BLUE FORCE TELEMETRY', canvasW / 2, canvasH / 2);
    ctx.restore();
  }

  ctx.restore();
}
render();

// ============== Canvas Mouse Events ==============
const statusLabel = { idle:'空闲', running:'运行中', thinking:'思考中', acting:'执行中', paused:'已暂停', stopped:'已停止', error:'异常', created:'已创建', decided:'已决策', messaged:'已发送', send_failed:'发送失败', analyzed:'已分析' };
const roleLabel = { scout:'侦察兵', commander:'指挥官', analyst:'分析师', support:'支援', brain:'Brain', 'claude-code':'Claude Code', openclaw:'OpenClaw', observer:'观察员' };
const backendLabel = { brain:'Brain', 'claude-code':'Claude Code', openclaw:'OpenClaw' };

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

function showTooltip(agent, mx, my) {
  const tt = document.getElementById('tooltip');
  if (!agent) {
    hoveredAgent = null;
    tt.style.display = 'none';
    return;
  }
  hoveredAgent = agent;
  let html = '<div class=tt-name>' + escapeHtml(agent.name || agent.agent_id) + '</div>';
  const backend = (agent.extra_meta || {}).backend || '';
  html += '<div class=tt-role>' + escapeHtml(roleLabel[agent.role] || backendLabel[backend] || agent.role) + '</div>';
  html += '<div class=tt-row><span class=lbl>ID</span><span class=val>' + escapeHtml(agent.agent_id) + '</span></div>';
  html += '<div class=tt-row><span class=lbl>状态</span><span class=val>' + escapeHtml(statusLabel[agent.status] || agent.status) + '</span></div>';
  if (agent.x !== undefined) {
    html += '<div class=tt-row><span class=lbl>坐标</span><span class=val>(' + agent.x.toFixed(0) + ', ' + agent.y.toFixed(0) + ')</span></div>';
  }
  const tasks = agent.pending_task_descs || [];
  if (tasks.length > 0) { html += '<div class=tt-section>任务</div>'; tasks.forEach((t, i) => { html += '<div class=tt-task><span class=tt-task-n>' + (i+1) + '.</span> ' + escapeHtml(t) + '</div>'; }); }
  const meta = agent.extra_meta || {};
  if (meta.core_goal) { html += '<div class=tt-section>目标</div><div class=tt-task>' + escapeHtml(meta.core_goal) + '</div>'; }
  if (meta.hidden_secret) { html += '<div class=tt-section>秘密</div><div class="tt-task tt-secret">' + escapeHtml(meta.hidden_secret) + '</div>'; }
  if (meta.action_space && meta.action_space.length) {
    html += '<div class=tt-section>行动</div><div class=tt-skills>' + meta.action_space.map(a => '<span class=tt-tag>' + escapeHtml(a) + '</span>').join('') + '</div>';
  }
  tt.innerHTML = html;
  tt.style.display = 'block';
  const panel = document.getElementById('canvas-stage');
  const panelRect = panel?.getBoundingClientRect();
  const panelW = panelRect?.width || window.innerWidth;
  const panelH = panelRect?.height || window.innerHeight;
  const ttW = tt.offsetWidth || 300;
  const ttH = tt.offsetHeight || 160;
  let left = mx + 16;
  let top = my - 10;
  if (left + ttW > panelW - 8) left = mx - ttW - 16;
  if (top + ttH > panelH - 8) top = panelH - ttH - 8;
  tt.style.left = Math.max(8, Math.min(left, panelW - ttW - 8)) + 'px';
  tt.style.top = Math.max(8, Math.min(top, panelH - ttH - 8)) + 'px';
}

// ── Find agent at screen (CSS-pixel) position ──
function findAgentAtScreen(mx, my) {
  if (!agents.length) return null;
  const { w: canvasW, h: canvasH } = canvasCSS();
  const rPx = Math.max(6, Math.min(16, Math.min(canvasW, canvasH) / 40)) + 3;

  for (let i = agents.length - 1; i >= 0; i--) {
    const a = agents[i];
    if (a.x == null || a.y == null) continue;
    const sp = _simState.get(a.agent_id);
    const wx = sp ? sp.x : a.x;
    const wy = sp ? sp.y : a.y;
    const p = worldToScreen(wx, wy);
    const dx = mx - p.sx, dy = my - p.sy;
    if (Math.sqrt(dx * dx + dy * dy) < rPx) return a;
  }
  return null;
}

// ── Panning state ──
let isPanning = false;
let panStartMX = 0, panStartMY = 0;
let panStartOffX = 0, panStartOffY = 0;

// ── Mouse down ──
canvas?.addEventListener('mousedown', function(e) {
  const rect = canvas.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;
  const found = findAgentAtScreen(mx, my);

  if (found) {
    // Drag agent
    draggingId = found.agent_id;
    canvas.style.cursor = 'grabbing';
    const panel = document.getElementById('canvas-panel');
    if (panel) { panel.classList.add('dragging'); panel.classList.remove('panning'); }
  } else {
    // Pan canvas (left button only)
    if (e.button === 0) {
      isPanning = true;
      panStartMX = mx;
      panStartMY = my;
      panStartOffX = viewport.offsetX;
      panStartOffY = viewport.offsetY;
      const panel = document.getElementById('canvas-panel');
      if (panel) { panel.classList.add('panning'); panel.classList.remove('dragging'); }
    }
  }
  e.preventDefault();
});

// ── Mouse move ──
canvas?.addEventListener('mousemove', function(e) {
  const rect = canvas.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;

  if (draggingId) {
    // 拖动 Agent：更新 _simState 位置（世界坐标）
    const world = screenToWorld(mx, my);
    _simState.set(draggingId, { x: world.wx, y: world.wy });
    return;
  }

  if (isPanning) {
    const dx = mx - panStartMX;
    const dy = my - panStartMY;
    viewport.offsetX = panStartOffX - dx / viewport.scale;
    viewport.offsetY = panStartOffY - dy / viewport.scale;
    viewport.userControlled = true;
    return;
  }

  // Hover detection
  const found = findAgentAtScreen(mx, my);
  canvas.style.cursor = found ? 'grab' : '';
  if (found !== hoveredAgent) {
    showTooltip(found, mx, my);
  }
});

// ── Mouse up ──
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
    const panel = document.getElementById('canvas-panel');
    if (panel) panel.classList.remove('dragging');
  }

  if (isPanning) {
    isPanning = false;
    const panel = document.getElementById('canvas-panel');
    if (panel) panel.classList.remove('panning');
  }
});

// ── Mouse leave ──
canvas?.addEventListener('mouseleave', function() {
  if (draggingId) {
    const pos = _simState.get(draggingId);
    if (pos) {
      const agent = agents.find(a => a.agent_id === draggingId);
      if (agent) { agent.x = Math.round(pos.x); agent.y = Math.round(pos.y); }
    }
    draggingId = null;
    canvas.style.cursor = '';
    const panel = document.getElementById('canvas-panel');
    if (panel) panel.classList.remove('dragging');
  }
  if (isPanning) {
    isPanning = false;
    const panel = document.getElementById('canvas-panel');
    if (panel) panel.classList.remove('panning');
  }
  showTooltip(null, 0, 0);
});

// ── Mouse wheel: zoom at pointer ──
canvas?.addEventListener('wheel', function(e) {
  e.preventDefault();
  const rect = canvas.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;

  // World point under cursor before zoom
  const worldBefore = screenToWorld(mx, my);

  // Apply zoom
  const factor = e.deltaY < 0 ? 1.12 : 0.88;
  const newScale = Math.max(viewport.MIN_SCALE, Math.min(viewport.MAX_SCALE, viewport.scale * factor));
  if (newScale === viewport.scale) return; // clamped — no change
  viewport.scale = newScale;

  // World point under cursor after zoom — adjust offset so cursor stays on same world point
  const worldAfter = screenToWorld(mx, my);
  viewport.offsetX += worldBefore.wx - worldAfter.wx;
  viewport.offsetY += worldBefore.wy - worldAfter.wy;
  viewport.userControlled = true;
}, { passive: false });

// ── Double-click on empty space: reset to auto-fit ──
canvas?.addEventListener('dblclick', function(e) {
  const rect = canvas.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;
  const found = findAgentAtScreen(mx, my);
  if (!found) {
    viewport.userControlled = false;
    fitViewportToAgents();
  }
});

// ============== WebSocket ==============
function connectWS() {
const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
ws = new WebSocket(proto + '//' + location.host + '/ws');
ws.onopen = () => { ws.send('all'); logEntry('frontend', 'WebSocket 已连接'); };
ws.onmessage = (e) => {
const msg = JSON.parse(e.data);
// ── 实时推送的单条日志（兼容旧 agent_log 缓冲） ──
if (msg.type === 'agent_log' && msg.data) {
    const l = msg.data;
    if (!_serverTimeOffset && l.timestamp) {
        _serverTimeOffset = new Date(l.timestamp).getTime() - Date.now();
    }
    // 兼容旧格式 → 归一化
    const norm = normalizeLogRecord({
      timestamp: l.timestamp || '',
      level: l.level || 'INFO',
      source: 'agent',
      category: (l.event === 'act' || l.event === 'decide') ? 'agent_behavior' : 'system',
      event: l.event || '',
      actor: { id: l.from_agent || l.agent_id || '' },
      target: { id: l.to_agent || '' },
      action: { name: l.action || '', status: l.action_status || '' },
      message: l.detail || '',
      agent_id: l.agent_id,
      from_agent: l.from_agent,
      to_agent: l.to_agent,
      details: { detail: l.detail, action: l.action, action_status: l.action_status },
    }, 'ws_agent_log');
    if (norm) {
      logBuffer.push(norm);
      if (logBuffer.length > 500) logBuffer.shift();
      if (_logFlushTimer) clearTimeout(_logFlushTimer);
      _logFlushTimer = setTimeout(renderLogs, 16);
      _lastLogCount++;
    }
    // ── 记录通信事件，用于报文轨迹动画 ──
    const status = l.action_status || '';
    const from = l.from_agent || l.agent_id || '?';
    const to = l.to_agent || '';
    const action = l.action || l.event || '?';
    if (status === 'success' && from !== '?' && to) {
      if (action === 'send_message') {
        pushCommEvent(from.toLowerCase(), to.toLowerCase(), false);
      } else if (action === 'broadcast') {
        if (to === '0.0.0.0') {
          for (const rel of _relationships) {
            const rf = rel.from.toLowerCase();
            const rt = rel.to.toLowerCase();
            if (rf === from.toLowerCase()) {
              pushCommEvent(from.toLowerCase(), rt, true);
            } else if (rt === from.toLowerCase()) {
              pushCommEvent(from.toLowerCase(), rf, true);
            }
          }
        } else {
          pushCommEvent(from.toLowerCase(), to.toLowerCase(), true);
        }
      }
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
    if (msg.type === 'all') {
      _simState = new Map();
      viewport.initialized = false;
      viewport.userControlled = false;
    }
    agents = msg.data.agents || [];
    if (msg.data.relationships !== undefined && (msg.data.relationships.length > 0 || agents.length === 0)) _relationships = msg.data.relationships;
    // ── agent_logs 旧缓冲区（兼容） ──
    if (_lastLogCount === 0) {
    const logs = msg.data.agent_logs || [];
    logs.slice(_lastLogCount).forEach(l => {
        const norm = normalizeLogRecord({
          timestamp: l.timestamp || '',
          level: l.level || 'INFO',
          source: 'agent',
          category: (l.event === 'act' || l.event === 'decide') ? 'agent_behavior' : 'system',
          event: l.event || '',
          actor: { id: l.from_agent || l.agent_id || '' },
          target: { id: l.to_agent || '' },
          action: { name: l.action || '', status: l.action_status || '' },
          message: l.detail || '',
          agent_id: l.agent_id,
          from_agent: l.from_agent,
          to_agent: l.to_agent,
          details: { detail: l.detail, action: l.action, action_status: l.action_status },
        }, 'ws_agent_log');
        if (norm) { logBuffer.push(norm); if (logBuffer.length > 500) logBuffer.shift(); }
    });
    _lastLogCount = logs.length;
    }
    // ── 新 log_entries（v2 统一 schema，优先使用） ──
    const logEntries = msg.data.log_entries || [];
    logEntries.forEach(e => {
        const norm = normalizeLogRecord(e, 'ws_log_entries');
        if (norm) { logBuffer.push(norm); if (logBuffer.length > 500) logBuffer.shift(); }
    });
    if (logEntries.length > 0 && _logFlushTimer) clearTimeout(_logFlushTimer);
    if (logEntries.length > 0) _logFlushTimer = setTimeout(renderLogs, 16);
}
// ── 通信报文 ──
if (msg.type === 'packets' && msg.data) {
    (msg.data.packets || []).forEach(p => {
        const norm = normalizeLogRecord({
          timestamp: p.timestamp || '',
          level: 'INFO',
          source: 'bus',
          category: 'communication',
          event: 'agent_message',
          actor: { id: p.agent_from || '' },
          target: { id: p.agent_to || '' },
          network: {
            src_ip: p.src_ip || '', src_port: p.src_port || 0,
            dst_ip: p.dst_ip || '', dst_port: p.dst_port || 0,
            protocol: p.protocol || 'TCP',
            packet_len: p.total_size || p.size_bytes || 0,
            tcp_flags: p.tcp_flags || '',
            channel_id: p.channel_id || '',
            message_type: p.message_type || p.method || '',
          },
          payload: { content: p.content || '' },
          message: (p.agent_from||'') + '→' + (p.agent_to||'') + ' | ' + (p.content||'').slice(0, 80),
        }, 'ws_packets');
        if (norm) { logBuffer.push(norm); if (logBuffer.length > 500) logBuffer.shift(); }
    });
    if (_logFlushTimer) clearTimeout(_logFlushTimer);
    _logFlushTimer = setTimeout(renderLogs, 16);
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

// ============== Scene Panel ==============
function loadScenePanel(name) {
  const iframe = document.getElementById('fp-iframe');
  const titleEl = document.getElementById('fp-title');
  if (iframe && name) {
    iframe.src = API + '/scenes/' + encodeURIComponent(name) + '/panel';
    sessionStorage.setItem('lastScenePanel', name);
  }
  if (titleEl) titleEl.textContent = name || '场景面板';
}

function toggleScenePanel() {
  document.getElementById('scene-panel')?.classList.toggle('collapsed');
  document.getElementById('canvas-panel')?.classList.toggle('scene-collapsed');
}

async function runSelectedScene() {
  const sel = document.getElementById('scene-selector');
  const name = sel?.value;
  if (!name) { logEntry('scene', '请先选择场景脚本'); return; }

	// 清空上轮仿真的前端日志和状态
	clearLogs();
	_lastLogCount = 0;
  _simState = new Map();
  _trajectories.length = 0;
  // Reset viewport for new scene
  viewport.initialized = false;
  viewport.userControlled = false;

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

async function stopSimulation() {
  if (!simRunning) { logEntry('scene', '没有正在运行的仿真'); return; }
  logEntry('scene', '⏹ 正在停止仿真...');
  try {
    const r = await fetch(API + '/simulations/stop', { method: 'POST' });
    if (r.ok) logEntry('scene', '已发送停止请求，当前轮次结束后停止');
  } catch(e) {
    logEntry('scene', '停止请求失败: ' + e.message);
  }
}

function togglePanel(id) { document.getElementById(id).classList.toggle('minimized'); }

// ============== Start ==============
logEntry('system', '控制台就绪');
loadSceneList();
// 恢复上次加载的场景面板
const lastScene = sessionStorage.getItem('lastScenePanel');
if (lastScene) loadScenePanel(lastScene);
setTimeout(() => { if (ws && ws.readyState === WebSocket.OPEN) ws.send('all'); }, 500);
