#!/usr/bin/env python3
"""
AI Agent 仿真运行平台 — Web API 服务
======================================

基于 FastAPI 的调度中心，对应架构文档第十二节技术栈：
调度中心: FastAPI

提供 RESTful API：
- Agent 管理（注册、发现、状态查询）
- 仿真场景运行
- Tool/Skill 执行
- 日志查询与搜索
- Packet 记录查询
- 系统统计监控

启动方式:
    python server.py              # 默认 http://localhost:8000
    python server.py --port 9000  # 自定义端口
    uvicorn server:app --reload   # 开发模式热重载
"""

import sys
import os
import json
import math
import random
import importlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows UTF-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime
import uvicorn
import asyncio
import uuid
import time

# 导入平台核心模块
from agent_network.agent import Agent, AgentRegistry, Message
from agent_network.agent_hub import AgentHub, RoutingStrategy, ScalingPolicy
from agent_network.llm_parser import get_api_config, SceneDefinition, AgentDef
from agent_network.container_runtime import get_runtime, ContainerRuntime
from agent_network.container_controller import ContainerController
from agent_network.workflow import WorkflowEngine, WorkflowDAG, WorkflowStep
from agent_network.agent_scheduler import TaskPriority, TaskStatus
from agent_network.logger import SimulationLogger, LogLevel, get_logger, normalize_log_timestamp
from agent_network.event_bus import PacketRecorder
from agent_network.tool import ToolRegistry
from agent_network.skill import SkillRegistry

# ── 统一日志器 ──
logger = get_logger()

def _beijing_time(utc_str: str = "") -> str:
    """将 UTC ISO 时间戳转为北京时间 ISO 格式: YYYY-MM-DDTHH:MM:SS.sss"""
    return normalize_log_timestamp(utc_str)

# ── 服务发现 ──
_MESSAGE_BUS_URL = os.environ.get("MESSAGE_BUS_URL", "http://bus:9000")

# ── WebSocket 连接池 ──
_ws_clients: set = set()
_server_loop: Optional[asyncio.AbstractEventLoop] = None

# ── 统一 Agent 日志缓冲区 ──
_agent_logs: List[Dict[str, Any]] = []  # 内存/容器模式共用日志

# ═══════════════════════════════════════════════
# Lifespan — 替代已弃用的 on_event
# ═══════════════════════════════════════════════

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _server_loop
    _server_loop = asyncio.get_running_loop()
    yield

# ═══════════════════════════════════════════════
# FastAPI 应用初始化
# ═══════════════════════════════════════════════

app = FastAPI(
    title="AI Agent 仿真运行平台",
    description="企业级 AI Agent 仿真、推演与编排平台 API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Docker HTTP middleware（LOG_DOCKER_HTTP=1 时启用）──
from agent_network.traffic_log import TrafficMiddleware, traffic_enabled
if traffic_enabled():
    app.add_middleware(TrafficMiddleware, component="srv", server_url="http://localhost:8000")


# 全局服务状态
service_state = {
    "started_at": datetime.now().isoformat(timespec="seconds"),
    "simulations_run": 0,
    "active_engine": None,
}
_simulation_stop_requested = False  # 仿真停止标志
_simulation_active = False          # 仅仿真运行期间接收 network_capture 包日志

_current_relationships: List[Dict[str, Any]] = []  # 当前关系链
_termination_config: Dict[str, int] = {"max_rounds": 10, "stalemate_rounds": 3}  # 终止条件默认值


def _control_agent_capture(created_cas: List[tuple], enabled: bool, requests_module) -> Dict[str, Any]:
    """启动/停止本轮已分配 Agent 容器内的 tcpdump 抓包。"""
    path = "/capture/start" if enabled else "/capture/stop"
    results = {"requested": 0, "ok": 0, "failed": 0}
    for ca, _ in created_cas:
        if not getattr(ca, "url", "") or ca.status == "error":
            continue
        results["requested"] += 1
        try:
            resp = requests_module.post(f"{ca.url}{path}", timeout=3)
            if resp.ok:
                results["ok"] += 1
            else:
                results["failed"] += 1
        except Exception:
            results["failed"] += 1
    return results


def _runtime_status_listener(ca, status: str, detail: Dict[str, Any] = None):
    agent = AgentRegistry.get(ca.agent_id)
    if agent:
        agent.status = status
        agent.container_url = ca.url
    if not _ws_clients:
        return

    payload = {
        "type": "agent_status",
        "data": [a.get_status() for a in AgentRegistry.list_all()],
    }
    loop = _server_loop
    if loop and loop.is_running():
        loop.call_soon_threadsafe(lambda: asyncio.create_task(_ws_broadcast(payload)))


def _get_runtime_with_status_listener() -> ContainerRuntime:
    runtime = get_runtime()
    runtime.set_status_listener(_runtime_status_listener)
    return runtime


# ═══════════════════════════════════════════════
# Pydantic 模型
# ═══════════════════════════════════════════════

class AgentCreateRequest(BaseModel):
    agent_id: Optional[str] = None
    role: str = "generic"
    name: str = ""
    skills: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    capability_scores: Dict[str, float] = Field(default_factory=dict)


class AgentStatus(BaseModel):
    agent_id: str
    name: str
    role: str
    status: str
    skills: List[str]
    tags: List[str]
    capability_scores: Dict[str, float]
    pending_tasks: int
    completed_tasks: int
    x: Optional[float] = None
    y: Optional[float] = None


class TaskRequest(BaseModel):
    action: str
    target_agent_id: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)


class ToolExecuteRequest(BaseModel):
    tool_name: str
    params: Dict[str, Any] = Field(default_factory=dict)


class SkillExecuteRequest(BaseModel):
    skill_name: str
    params: Dict[str, Any] = Field(default_factory=dict)


class SimulationRunRequest(BaseModel):
    scene: str = ""  # 场景文件夹名（scenes/ 下的子目录）
    name: str = ""


class SettingsRequest(BaseModel):
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None


class LogQueryRequest(BaseModel):
    agent_id: Optional[str] = None
    level: Optional[str] = None
    level_type: Optional[str] = None
    event_contains: Optional[str] = None
    index: Optional[str] = None
    limit: int = 50


# ═══════════════════════════════════════════════
# 首页 — 控制台 Dashboard
# ═══════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def index():
    """仿真平台控制台首页"""
    from fastapi.responses import Response
    resp = HTMLResponse(content=_load_dashboard())
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ═══════════════════════════════════════════════
# Agent 管理 API — 对应架构文档第六节
# ═══════════════════════════════════════════════

def _inject_runtime_url(status: dict) -> dict:
    """注入容器运行时的实际 URL（get_status 已包含 url 字段）"""
    return status  # url 已在 Agent.get_status() 中通过 container_url 返回


@app.get("/api/agents", response_model=List[AgentStatus])
async def list_agents():
    """列出所有已注册 Agent"""
    return [_inject_runtime_url(a.get_status()) for a in AgentRegistry.list_all()]


@app.get("/api/agents/{agent_id}", response_model=AgentStatus)
async def get_agent(agent_id: str):
    """获取指定 Agent 详情"""
    agent = AgentRegistry.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return _inject_runtime_url(agent.get_status())


@app.post("/api/agents", response_model=AgentStatus)
async def create_agent(req: AgentCreateRequest):
    """注册新 Agent"""
    agent = Agent(
        agent_id=req.agent_id,
        role=req.role,
        name=req.name or req.agent_id or "",
        skills=req.skills,
        tags=req.tags,
        capability_scores=req.capability_scores,
    )
    AgentRegistry.register(agent)
    agent.start()
    return agent.get_status()


@app.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: str):
    """注销 Agent"""
    agent = AgentRegistry.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    agent.stop()
    AgentRegistry.unregister(agent_id)
    return {"deleted": agent_id}


@app.get("/api/agents/discover")
async def discover_agents(
    role: Optional[str] = Query(None),
    skill: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
):
    """
    Agent 发现 API

    对应架构文档第六节：
    - /api/agents/discover?role=commander    按角色
    - /api/agents/discover?skill=planning    按技能
    - /api/agents/discover?tag=blue_force    按标签
    """
    agents = AgentRegistry.find_agent(role=role, skill=skill, tag=tag)
    return [a.get_status() for a in agents]


@app.get("/api/agents/discover/best")
async def discover_best_agent(skill: str = Query(...)):
    """按能力评分找最优 Agent"""
    agent = AgentRegistry.find_best_agent(skill=skill)
    if not agent:
        raise HTTPException(status_code=404, detail=f"No agent with skill '{skill}'")
    return agent.get_status()


@app.post("/api/agents/{agent_id}/task")
async def send_task(agent_id: str, req: TaskRequest):
    """向 Agent 发送任务"""
    agent = AgentRegistry.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

    target = None
    if req.target_agent_id:
        target = AgentRegistry.get(req.target_agent_id)

    msg = agent.send_task(req.action, target=target, **req.params)

    # 立即触发执行（简化：单Agent直接执行）
    if agent.task_queue:
        task_msg = agent.task_queue.pop(0)
        result = agent.execute_task(task_msg)
        return {"message": msg.to_dict(), "result": result}

    return {"message": msg.to_dict(), "result": "task_queued"}


# ═══════════════════════════════════════════════
# Agent 统计 API
# ═══════════════════════════════════════════════

@app.get("/api/agents/stats")
async def agent_stats():
    """Agent 注册中心统计信息"""
    return AgentRegistry.get_stats()


# ═══════════════════════════════════════════════
# Tool 管理 API — 对应架构文档第四节
# ═══════════════════════════════════════════════

@app.get("/api/tools")
async def list_tools():
    """列出所有已注册工具"""
    return {"tools": ToolRegistry.list_tools()}


@app.post("/api/tools/execute")
async def execute_tool(req: ToolExecuteRequest):
    """执行工具"""
    try:
        result = ToolRegistry.execute(req.tool_name, **req.params)
        return {"tool": req.tool_name, "result": result}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/tools/stats")
async def tool_stats():
    """工具调用统计"""
    return ToolRegistry.get_stats()


# ═══════════════════════════════════════════════
# Skill 管理 API — 对应架构文档第五节
# ═══════════════════════════════════════════════

@app.get("/api/skills")
async def list_skills():
    """列出所有已注册技能"""
    return {"skills": SkillRegistry.list_skills()}


_active_skills_module = None  # 当前场景的 skills 模块

@app.post("/api/skills/execute")
async def execute_skill(req: SkillExecuteRequest):
    """执行技能 — 优先用场景 skills，回退到默认 SkillRegistry"""
    if _active_skills_module:
        try:
            result = _active_skills_module.SkillRegistry.execute(req.skill_name, **req.params)
            return {"skill": req.skill_name, "result": result}
        except Exception:
            pass
    try:
        result = SkillRegistry.execute(req.skill_name, **req.params)
        return {"skill": req.skill_name, "result": result}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/minesweeper/board")
async def minesweeper_board():
    """返回扫雷场景的棋盘状态（从 _engine 读取）"""
    if not _active_skills_module or not hasattr(_active_skills_module, '_engine'):
        return {"board": None, "error": "扫雷引擎未加载"}
    eng = _active_skills_module._engine
    SIZE = getattr(eng, 'SIZE', 9)
    revealed = eng.revealed
    board = []
    for y in range(SIZE):
        row = []
        for x in range(SIZE):
            if revealed[y][x]:
                if (x, y) in eng.discovered_mines:
                    row.append(-1)  # mine
                else:
                    row.append(eng.count_adjacent_mines(x, y))  # adjacent mine count
            else:
                row.append(None)
        board.append(row)
    safe_count = sum(1 for y in range(SIZE) for x in range(SIZE) if revealed[y][x] and (x, y) not in eng.discovered_mines)
    # 收集士兵当前位置
    soldiers = {}
    registry = getattr(eng, 'round_action_registry', {})
    if registry:
        latest_round = max(registry.keys())
        for sid, (gx, gy) in registry[latest_round].items():
            soldiers[sid] = {"x": gx, "y": gy}
    return {
        "board": board,
        "soldiers": soldiers,
        "discovered_mines": [{"x": k[0], "y": k[1]} for k in eng.discovered_mines.keys()],
        "game_state": "RUNNING" if safe_count < (SIZE * SIZE - eng.TOTAL_MINES) else "VICTORY",
        "cells_revealed": safe_count,
        "completion_percentage": f"{(safe_count / (SIZE * SIZE - eng.TOTAL_MINES)) * 100:.1f}%",
    }


# ═══════════════════════════════════════════════
# 仿真运行 API — 对应架构文档第三节
# ═══════════════════════════════════════════════

# 保存最近的仿真结果
_simulation_results: List[Dict[str, Any]] = []


# LLM 配置缓存（运行时可通过 API 修改）
_llm_config: Dict[str, str] = {}

def _get_effective_llm_config() -> Dict[str, str]:
    """获取有效的 LLM 配置：API 设置 > 环境变量"""
    config = get_api_config()
    config.update(_llm_config)
    return config


def _force_layout(agents: List[Any], links: List[Dict],
                  width: float = 400, height: float = 400,
                  margin: float = 60, iterations: int = 80) -> Dict[str, tuple]:
    """
    力导向布局：根据关系矩阵计算 Agent 位置。
    - 有连线的 Agent 相互吸引（合作近、竞争远）
    - 所有 Agent 互相排斥避免重叠
    """
    import math, random as _rnd
    n = len(agents)
    if n == 0:
        return {}

    # 初始化随机位置
    pos = {}
    for a in agents:
        pos[a.agent_id] = [
            _rnd.uniform(margin, width - margin),
            _rnd.uniform(margin, height - margin),
        ]

    # 构建邻接表
    edges = set()
    for link in links:
        f = link.get("from", "").lower()
        t = link.get("to", "").lower()
        val = link.get("value", 0)
        edges.add((f, t, val))

    area = width * height
    k = math.sqrt(area / n)  # 理想间距

    # 温度逐步降低
    for it in range(iterations):
        temp = 1.0 - it / iterations  # 1.0 → 0.0
        temp = max(0.02, temp * temp)

        # 计算排斥力
        disp = {aid: [0.0, 0.0] for aid in pos}
        ids = list(pos.keys())
        for i in range(n):
            for j in range(i + 1, n):
                aid_i, aid_j = ids[i], ids[j]
                dx = pos[aid_i][0] - pos[aid_j][0]
                dy = pos[aid_i][1] - pos[aid_j][1]
                dist = math.sqrt(dx * dx + dy * dy) or 0.01
                # 库仑排斥力
                force = k * k / dist
                fx = (dx / dist) * force
                fy = (dy / dist) * force
                disp[aid_i][0] += fx
                disp[aid_i][1] += fy
                disp[aid_j][0] -= fx
                disp[aid_j][1] -= fy

        # 计算吸引力（连线）
        for f, t, val in edges:
            if f not in pos or t not in pos:
                continue
            dx = pos[t][0] - pos[f][0]
            dy = pos[t][1] - pos[f][1]
            dist = math.sqrt(dx * dx + dy * dy) or 0.01
            # 胡克弹簧力：值越大（越合作）吸引力越强，负值（竞争）推远
            spring_force = (dist - k * 0.5) / k  # 目标距离 = 0.5 * ideal
            if val < 0:
                spring_force = -spring_force * 0.5  # 竞争关系推远
            else:
                spring_force = spring_force * (abs(val) / 100 + 0.3)
            fx = (dx / dist) * spring_force * k * 0.3
            fy = (dy / dist) * spring_force * k * 0.3
            disp[f][0] += fx
            disp[f][1] -= fy
            disp[t][0] -= fx
            disp[t][1] += fy

        # 应用位移
        for aid in ids:
            d = math.sqrt(disp[aid][0]**2 + disp[aid][1]**2) or 0.01
            capped = min(d, temp * k)
            pos[aid][0] += (disp[aid][0] / d) * capped
            pos[aid][1] += (disp[aid][1] / d) * capped
            # 边界约束
            pos[aid][0] = max(margin, min(width - margin, pos[aid][0]))
            pos[aid][1] = max(margin, min(height - margin, pos[aid][1]))

    # Break overlaps: add small random jitter to all positions
    for aid in pos:
        pos[aid][0] += _rnd.uniform(-12, 12)
        pos[aid][1] += _rnd.uniform(-12, 12)
        pos[aid][0] = max(margin, min(width - margin, pos[aid][0]))
        pos[aid][1] = max(margin, min(height - margin, pos[aid][1]))

    return pos


# 全局场景状态（setup → launch 分离）
_pending_scene_def: Optional[SceneDefinition] = None
_pending_layout: Dict[str, tuple] = {}
_pending_config: Dict[str, str] = {}

# 通信基础设施（关系矩阵 → 通信权限）
_comm_matrix: Dict[str, set] = {}  # agent_id → {allowed_target_ids}


def _setup_scene(scene_def: SceneDefinition) -> Dict[str, Any]:
    """Step 1: 绘制场景 — 创建 Agent、计算布局、注册，不启动容器"""
    global _pending_scene_def, _pending_layout

    AgentRegistry.reset()
    PacketRecorder.reset()
    _agent_logs.clear()  # 清空上轮仿真日志
    logger.reset()       # 清空结构化日志

    from agent_network.comm import RemoteBus
    remote_bus = RemoteBus(message_bus_url=_MESSAGE_BUS_URL)

    # 力导向布局
    layout_pos = _force_layout(scene_def.agents, scene_def.workflow)

    for ad in scene_def.agents:
        agent = Agent(
            agent_id=ad.agent_id,
            role=ad.role,
            name=ad.name,
            skills=ad.skills,
            tags=ad.tags,
        )
        agent.set_comm(remote_bus)
        lx, ly = layout_pos.get(ad.agent_id, (random.uniform(50, 350), random.uniform(50, 350)))
        agent.x = lx
        agent.y = ly
        agent.pending_task_descs = ad.tasks
        agent.extra_meta = ad.extra_meta
        AgentRegistry.register(agent)
        agent.start()

    _pending_scene_def = scene_def
    _pending_layout = layout_pos

    return {
        "agents": [a.get_status() for a in AgentRegistry.list_all()],
        "agent_stats": AgentRegistry.get_stats(),
        "relationships": scene_def.workflow,
        "scene_name": scene_def.scene_name,
    }


def _launch_containers(config: Dict[str, str], scene_def=None) -> Dict[str, Any]:
    """Step 2: 拉起 Agent 并运行仿真 — Docker 不可用时回退到直连模式"""
    global _current_relationships, _simulation_active

    if scene_def is None:
        scene_def = _pending_scene_def
    if not scene_def:
        return {"error": "No scene setup. Call /api/simulations/setup first."}

    import requests as _req

    runtime = _get_runtime_with_status_listener()
    runtime.reset()  # 清理上一轮的容器占用标记，释放池容器

    created_cas = []
    assign_errors = []
    for ad in scene_def.agents:
        ca = runtime.assign_agent(
            agent_id=ad.agent_id, role=ad.role, name=ad.name,
            extra_meta=ad.extra_meta if ad.extra_meta else None,
        )
        created_cas.append((ca, ad.tasks))
        if ca.status == "error":
            assign_errors.append({
                "agent_id": ca.agent_id, "name": ca.name,
                "error": getattr(ca, '_assign_error', 'unknown'),
            })
        else:
            # 直接给 AgentRegistry 中的 Agent 设置容器 URL
            agent = AgentRegistry.get(ca.agent_id)
            if agent:
                agent.container_url = ca.url

    # 容器分配结果日志
    assigned_count = sum(1 for ca, _ in created_cas if ca.status != "error")
    logger.system("container_pool",
        f"容器分配完成: {assigned_count}/{len(scene_def.agents)} Agent 分配成功",
        details={"total_agents": len(scene_def.agents), "assigned": assigned_count,
                  "errors": assign_errors})

    if assign_errors:
        # 过滤掉分配失败的 Agent，不参与后续轮次
        created_cas = [(ca, tasks) for ca, tasks in created_cas if ca.status != "error"]
        logger.system("container_pool",
            f"警告: {len(assign_errors)} 个 Agent 分配失败，将被跳过",
            details={"skipped": [e["agent_id"] for e in assign_errors]})

    # ── 容器状态重置（在分配后、注册前，确保上一轮仿真残留已清除）──
    for ca, _ in created_cas:
        try:
            _req.post(f"{ca.url}/reset", timeout=3)
        except Exception:
            pass

    time.sleep(1)
    for ca, _ in created_cas:
        try:
            _req.post(f"{_MESSAGE_BUS_URL}/register",
                      params={"agent_id": ca.agent_id, "url": ca.url, "name": ca.name}, timeout=3)
            runtime._set_status(ca, "idle", {"phase": "bus_register"})
        except Exception:
            runtime._set_status(ca, "error", {"phase": "bus_register", "error": "message_bus_register_failed"})

    # 获取事件触发器
    event_triggers = getattr(scene_def, 'event_triggers', []) or []

    # ── 构建通信矩阵（从 workflow edges，双向）──
    global _comm_matrix
    _comm_matrix.clear()
    for edge in (scene_def.workflow or []):
        src = edge.get("from", "").lower()
        dst = edge.get("to", "").lower()
        if src and dst:
            _comm_matrix.setdefault(src, set()).add(dst)
            _comm_matrix.setdefault(dst, set()).add(src)

    # ── 清空上一轮仿真日志 ──
    _agent_logs.clear()

    # ── 初始化 session 日志文件夹（server + message_bus 同步）──
    logger.start_session(scene_def.scene_name)
    try:
        _req.post(f"{_MESSAGE_BUS_URL}/session/start",
                  params={"session_dir": logger._session_dir}, timeout=3)
    except Exception:
        pass  # message_bus 不可达时不阻塞仿真

    _simulation_active = True
    capture_start = _control_agent_capture(created_cas, True, _req)
    logger.system("capture_control", "network_capture started",
                  details={"enabled": True, **capture_start})

    # ── 生成会话 ID ──
    talk_id = f"talk-{uuid.uuid4().hex[:12]}-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    # ── 构建信道映射: {(from_id, to_id): channel_id} ──
    channel_map: Dict[str, str] = {}
    for edge in (scene_def.workflow or []):
        src = edge.get("from", "")
        dst = edge.get("to", "")
        ch = edge.get("channel_id", "")
        if src and dst:
            channel_map[f"{src}->{dst}"] = ch
            channel_map[f"{src.lower()}->{dst.lower()}"] = ch  # 小写别名，方便查找

    # ── 运行仿真轮次: 广播模式 ──
    workflow_steps = scene_def.workflow if scene_def.workflow else []
    MAX_ROUNDS = _termination_config.get("max_rounds", 20)
    stalemate_threshold = _termination_config.get("stalemate_rounds", 3)
    results_log = []
    silent_rounds = 0
    stop_reason = "hard_limit"

    global _simulation_stop_requested
    _simulation_stop_requested = False
    try:
        for round_num in range(MAX_ROUNDS):
            if _simulation_stop_requested:
                stop_reason = "user_stopped"
                logger.system("simulation_stopped", "用户手动停止仿真", details={"round": round_num + 1})
                break
            current_turn = round_num + 1

            # 检查事件触发
            for trigger in event_triggers:
                if trigger.get("turn") == current_turn:
                    event_payload = {
                        "event_name": trigger.get("event_name", "未知事件"),
                        "impact": trigger.get("impact", ""),
                        "turn": current_turn,
                    }
                    logger.event_trigger(current_turn, event_payload['event_name'], event_payload['impact'])
                    for ca, _ in created_cas:
                        try:
                            _req.post(f"{ca.url}/event", json=event_payload, timeout=5)
                        except Exception:
                            pass

            context = {
                "round": current_turn,
                "total_rounds": MAX_ROUNDS,
                "scene": scene_def.scene_name,
                "agents": [{"id": ca.agent_id, "role": ca.role, "name": ca.name}
                           for ca, _ in created_cas],
                "tasks": {ca.agent_id: tasks for ca, tasks in created_cas},
                "comm_matrix": {k: list(v) for k, v in _comm_matrix.items()},
                "channel_map": channel_map,
                "talk": talk_id,
            }
            round_result = runtime.run_round(context)
            results_log.append(round_result)

            # 同步扫雷引擎中的士兵位置到 AgentRegistry
            if _active_skills_module and hasattr(_active_skills_module, '_engine'):
                eng = _active_skills_module._engine
                registry = getattr(eng, 'round_action_registry', {})
                if registry:
                    latest_round = max(registry.keys())
                    for soldier_id, (gx, gy) in registry[latest_round].items():
                        agent = AgentRegistry.get(soldier_id)
                        if agent:
                            agent.x = float(gx)
                            agent.y = float(gy)

            # 僵局检测：本轮是否有实际消息产生
            decisions = round_result.get("decisions", [])
            messages_sent = sum(
                1 for d in decisions
                if d.get("type") in ("send_message", "broadcast", "execute_skill")
            )
            if messages_sent == 0:
                silent_rounds += 1
            else:
                silent_rounds = 0

            if silent_rounds >= stalemate_threshold:
                stop_reason = f"stalemate_{stalemate_threshold}_silent_rounds"
                break

            time.sleep(0.3)
        else:
            stop_reason = "hard_limit"
    finally:
        _simulation_active = False
        capture_stop = _control_agent_capture(created_cas, False, _req)
        logger.system("capture_control", "network_capture stopped",
                      details={"enabled": False, **capture_stop})
        final_status = "stopped" if stop_reason == "user_stopped" else "idle"
        for ca, _ in created_cas:
            if ca.status != "error":
                runtime._set_status(ca, final_status, {"phase": "simulation:finish", "stop_reason": stop_reason})

    _current_relationships = scene_def.workflow
    registry_agents = [a.get_status() for a in AgentRegistry.list_all()]
    actual_rounds = len(results_log)
    runtime_agent_count = len(runtime.agents)

    # 写入仿真完成日志
    logger.system("simulation_complete",
        f"仿真完成: {scene_def.scene_name} | {actual_rounds}轮 | "
        f"{runtime_agent_count}/{len(scene_def.agents)} Agent | {stop_reason}",
        details={"scene": scene_def.scene_name, "rounds": actual_rounds,
                  "agent_count": runtime_agent_count, "agent_defined": len(scene_def.agents),
                  "stop_reason": stop_reason})

    return {
        "simulation_name": scene_def.scene_name,
        "duration_seconds": round(len(results_log) * 1.5 if results_log else 1.5, 2),
        "agents": registry_agents,
        "agent_stats": AgentRegistry.get_stats(),
        "packet_stats": PacketRecorder.get_stats(),
        "max_rounds": MAX_ROUNDS,
        "rounds": actual_rounds,
        "stop_reason": stop_reason,
        "results_log": results_log,
        "relationships": scene_def.workflow,
        "container_mode": "pool",
    }


def _build_scene_from_folder(scene_name: str) -> SceneDefinition:
    """将文件夹格式（3个JSON）映射为 SceneDefinition。"""
    from pathlib import Path
    folder = _SCENES_DIR / scene_name

    # Load 3 files
    meta = json.loads((folder / "meta_and_roles.json").read_text(encoding='utf-8'))
    instances = json.loads((folder / "instances_and_skills.json").read_text(encoding='utf-8'))
    topology = json.loads((folder / "network_topology.json").read_text(encoding='utf-8'))

    smeta = meta.get("scenario_metadata", {})
    title = smeta.get("title", scene_name)
    bg = smeta.get("global_rules", "")
    # 从场景元数据读取终止条件
    if smeta.get("max_rounds"):
        _termination_config["max_rounds"] = int(smeta["max_rounds"])
    if smeta.get("stalemate_rounds"):
        _termination_config["stalemate_rounds"] = int(smeta["stalemate_rounds"])
    roles = meta.get("roles", {})
    containers = instances.get("container_instances", {})

    # 加载场景 skills.py
    global _active_skills_module
    skills_info = []
    skills_py = folder / "skills.py"
    if skills_py.exists():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(f"skills_{scene_name}", skills_py)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _active_skills_module = mod
            for name, func in mod.SkillRegistry._skills.items():
                skills_info.append({"name": name, "desc": (func.__doc__ or "").strip()})
        except Exception:
            _active_skills_module = None

    agents: List[AgentDef] = []
    for role_id, role in roles.items():
        instance = containers.get(role_id, {})
        # 兼容两种技能格式: "skills" (字符串列表) 或 "skill_bindings" (对象列表)
        raw_skills = instance.get("skills") or instance.get("skill_bindings") or []
        if raw_skills and isinstance(raw_skills[0], dict):
            skills = [s["skill_name"] for s in raw_skills]
        else:
            skills = raw_skills
        backend = role.get("model_backbone", "brain")
        # Map model_backbone to our backend names
        if backend == "claudecode":
            backend = "claude-code"

        # 交互范式的 prompt 修饰
        paradigm = role.get("primary_interaction_paradigm", "")
        paradigm_hints = {
            "EXTERNAL_NEGOTIATION": "你处于对外谈判模式，需要在合作与竞争之间寻找平衡。",
            "COMPETITIVE_AGGRESSIVE": "你采取进攻性市场竞争策略，优先扩大份额而非短期利润。",
            "INTERNAL_COLLABORATION": "你注重内部协作，通过团队配合提升整体效率。",
            "REGULATORY_COMPLIANCE": "你需要确保所有行动符合监管要求，违规将带来严重后果。",
        }

        agent = AgentDef(
            agent_id=role_id.lower(),
            role="generic",
            name=role.get("name", role_id),
            skills=skills[:4],
            tags=[paradigm] if paradigm else [],
            tasks=skills[:6] if skills else [role.get("core_goal", "")],  # 技能绑定作为具体任务
            extra_meta={
                "identity": role.get("identity", ""),
                "core_goal": role.get("core_goal", ""),
                "hidden_secret": role.get("hidden_secret", ""),      # 从 JSON 读取，不再硬编码空值
                "initial_assets": role.get("initial_assets", {}),    # 从 JSON 读取，不再硬编码空值
                "action_space": ["send_message"] + skills,
                "background_rules": bg,
                "backend": backend,
                "interaction_paradigm": paradigm,
                "paradigm_hint": paradigm_hints.get(paradigm, ""),
                "pip_packages": instance.get("pip_packages", []),
                "runtime_engine": instance.get("runtime_engine", ""),
                "skills_list": [s for s in skills_info if s["name"] in skills],
            },
        )
        agents.append(agent)

    # Build relationships from topology edges
    relationships = []
    for subnet in topology.get("sub_networks", []):
        for edge in subnet.get("edges", []):
            # 权重: 优先读 edge.weight，否则根据 paradigm 推断
            weight = edge.get("weight")
            if weight is None:
                weight = 70 if edge.get("paradigm") == "COLLABORATION" else -50
            relationships.append({
                "from": edge["source"].lower(),
                "to": edge["target"].lower(),
                "relation_type": edge.get("paradigm", ""),
                "value": weight,
                "can_direct_chat": edge.get("direct_chat", True),
                "channel_id": edge.get("channel_id", ""),
            })

    return SceneDefinition(
        scene_name=title,
        description=bg,
        agents=agents,
        workflow=relationships,
        event_triggers=[],
    )


@app.post("/api/simulations/run")
async def run_simulation(req: SimulationRunRequest):
    """运行仿真场景 — setup + launch 一体化"""
    global service_state, _current_relationships

    # ── Step 1: Build scene (folder format only) ──
    if not req.scene or not (_SCENES_DIR / req.scene).is_dir():
        raise HTTPException(status_code=400, detail=f"Scene '{req.scene}' not found")
    scene_def = _build_scene_from_folder(req.scene)

    # ── Step 2: Setup agents ──
    result = _setup_scene(scene_def)
    _current_relationships = result["relationships"]

    # ── Step 3: Launch containers ──
    config = _get_effective_llm_config()
    loop = asyncio.get_event_loop()
    launch = await loop.run_in_executor(None, _launch_containers, config)
    result.update(launch)

    service_state["simulations_run"] += 1
    return result


@app.post("/api/simulations/setup")
async def setup_simulation(req: SimulationRunRequest):
    """Step 1: 绘制场景 — 创建 Agent、返回布局和关系（不启动容器）"""
    global _current_relationships, _pending_config

    if not req.scene or not (_SCENES_DIR / req.scene).is_dir():
        raise HTTPException(status_code=400, detail=f"Scene '{req.scene}' not found")
    scene_def = _build_scene_from_folder(req.scene)

    _pending_config = _get_effective_llm_config()
    result = _setup_scene(scene_def)
    _current_relationships = result["relationships"]
    return result


@app.post("/api/simulations/launch")
async def launch_simulation():
    """Step 2: 拉起 Docker — 为已 setup 的 Agent 启动容器并运行仿真"""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _launch_containers, _pending_config, _pending_scene_def)
    return result


@app.post("/api/simulations/stop")
async def stop_simulation():
    """停止正在运行的仿真"""
    global _simulation_stop_requested
    _simulation_stop_requested = True
    return {"status": "stop_requested"}


# ═══════════════════════════════════════════════
# 场景文件 API
# ═══════════════════════════════════════════════

_SCENES_DIR = None  # initialized after pathlib import below


@app.get("/api/scenes")
async def list_scenes():
    """列出所有可用场景（文件夹格式）"""
    if not _SCENES_DIR.exists():
        return {"scenes": []}
    scenes = []
    for f in sorted(_SCENES_DIR.iterdir(), key=lambda n: n.name.lower()):
        if f.is_dir() and (f / "meta_and_roles.json").exists():
            scenes.append({"name": f.name, "format": "folder"})
    return {"scenes": scenes}


@app.get("/api/scenes/{scene_name}")
async def read_scene(scene_name: str):
    """读取场景内容（文件夹格式）"""
    folder = _SCENES_DIR / scene_name
    if not folder.is_dir():
        raise HTTPException(status_code=404, detail=f"Scene '{scene_name}' not found")
    files = {}
    for key in ["meta_and_roles", "instances_and_skills", "network_topology"]:
        fpath = folder / f"{key}.json"
        if fpath.exists():
            files[key] = json.loads(fpath.read_text(encoding='utf-8'))
    if "meta_and_roles" not in files:
        raise HTTPException(status_code=404, detail=f"Folder scene '{scene_name}' missing meta_and_roles.json")
    title = files["meta_and_roles"].get("scenario_metadata", {}).get("title", scene_name)
    return {"name": scene_name, "title": title, "format": "folder", "files": files}


@app.get("/api/scenes/{scene_name}/panel", response_class=HTMLResponse)
async def scene_panel(scene_name: str):
    """返回场景自带的可视化面板 HTML（scenes/{name}/panel.html）"""
    folder = _SCENES_DIR / scene_name
    panel_path = folder / "panel.html"
    if panel_path.exists():
        return HTMLResponse(content=panel_path.read_text(encoding='utf-8'))
    return HTMLResponse(content='<html><body style="background:#ECE8DF;color:#6A665F;display:flex;align-items:center;justify-content:center;height:100%;font-family:Inter,sans-serif;font-size:12px">无可视化面板</body></html>')


# ═══════════════════════════════════════════════
# API 配置管理
# ═══════════════════════════════════════════════

@app.get("/api/settings")
async def get_settings():
    """获取当前 LLM API 配置（敏感信息脱敏）"""
    config = _get_effective_llm_config()
    key = config.get("api_key", "")
    return {
        "provider": config.get("provider", "auto"),
        "api_key": key[:8] + "..." + key[-4:] if len(key) > 12 else ("***" if key else ""),
        "has_key": bool(key),
        "api_base": config.get("api_base", ""),
        "model": config.get("model", ""),
    }


@app.post("/api/settings")
async def update_settings(req: SettingsRequest):
    """更新 LLM API 配置（运行时生效）"""
    if req.api_key is not None:
        _llm_config["api_key"] = req.api_key
    if req.api_base is not None:
        _llm_config["api_base"] = req.api_base
    if req.model is not None:
        _llm_config["model"] = req.model
    if req.provider is not None:
        _llm_config["provider"] = req.provider

    # 自动检测 provider
    key = _llm_config.get("api_key", "")
    if key.startswith("sk-ant-"):
        _llm_config["provider"] = "anthropic"
    elif key.startswith("sk-"):
        _llm_config["provider"] = "openai"

    return await get_settings()


@app.get("/api/simulations/results")
async def get_simulation_results(limit: int = Query(default=5, le=20)):
    """获取最近的仿真结果"""
    return _simulation_results[-limit:]


# ═══════════════════════════════════════════════
# 日志查询 API — 对应架构文档第十一节
# ═══════════════════════════════════════════════
# 日志查询 & 导出 API
# ═══════════════════════════════════════════════


@app.get("/api/logs")
async def query_logs(
    agent_id: Optional[str] = Query(None),
    level: Optional[str] = Query(None),
    event: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    limit: int = Query(default=100, le=1000),
):
    """日志查询 API — 支持按 agent/level/event/keyword 过滤"""
    entries = logger.query(
        agent_id=agent_id,
        level=level,
        event=event,
        keyword=keyword,
        limit=limit,
    )
    return {
        "backend": "memory",
        "total": len(entries),
        "entries": entries,
    }


@app.get("/api/logs/stats")
async def log_stats():
    """日志统计"""
    return logger.get_index_stats()


@app.get("/api/logs/agent/{agent_id}")
async def agent_logs(agent_id: str, limit: int = 50):
    """获取某个 Agent 的完整时间线（decide/act/agent_action/agent_decide）"""
    return {
        "agent_id": agent_id,
        "entries": logger.get_agent_timeline(agent_id, limit),
    }


@app.get("/api/logs/messages")
async def message_logs(limit: int = 50):
    """获取 Agent 间通信报文 + Docker HTTP 流量"""
    with logger._lock:
        comm_entries = [e for e in logger._entries if e.get("category") == "communication"]
    total = len(comm_entries)
    return {
        "total": total,
        "entries": comm_entries[-limit:],
    }


@app.get("/api/logs/export")
async def export_logs(fmt: str = Query(default="jsonl", pattern="^(jsonl|json|csv)$"),
                      limit: int = Query(default=0)):
    """导出日志（jsonl / json / csv）"""
    from fastapi.responses import PlainTextResponse
    content = logger.export(fmt=fmt, limit=limit)
    media_types = {"jsonl": "application/x-ndjson", "json": "application/json", "csv": "text/csv"}
    return PlainTextResponse(content, media_type=media_types.get(fmt, "text/plain"))


@app.get("/api/logs/export/file")
async def export_logs_file(fmt: str = Query(default="jsonl", pattern="^(jsonl|json|csv)$"),
                           limit: int = Query(default=0)):
    """导出日志到文件并返回下载链接"""
    import tempfile
    filename = f"agent_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{fmt if fmt != 'jsonl' else 'jsonl'}"
    filepath = os.path.join(tempfile.gettempdir(), filename)
    logger.export_file(filepath, fmt=fmt, limit=limit)
    from fastapi.responses import FileResponse
    return FileResponse(filepath, filename=filename)


@app.get("/api/logs/files")
async def list_log_files():
    """列出持久化的日志文件"""
    return {"files": logger.list_log_files()}


@app.get("/api/logs/download/{filename:path}")
async def download_log_file(filename: str):
    """下载指定日志文件（支持 session 子目录路径，如 session_name/global.jsonl）"""
    from fastapi.responses import FileResponse
    log_dir = os.path.realpath(logger._log_dir)
    filepath = os.path.realpath(os.path.join(log_dir, filename))
    # 安全检查：防止路径穿越
    if not filepath.startswith(log_dir + os.sep) and filepath != log_dir:
        raise HTTPException(403, "Path traversal denied")
    if not os.path.isfile(filepath):
        raise HTTPException(404, f"Log file '{filename}' not found")
    return FileResponse(filepath, filename=os.path.basename(filename))


# ═══════════════════════════════════════════════
# Packet 查询 API — 对应架构文档第九节
# ═══════════════════════════════════════════════

@app.get("/api/packets")
async def query_packets(
    agent_id: Optional[str] = Query(None),
    direction: Optional[str] = Query(None),
    limit: int = Query(default=100, le=500),
):
    """查询 IP 包级别通信报文 — 含源/目标IP、延迟、内容"""
    records = PacketRecorder.get_records(agent_id=agent_id, direction=direction, limit=limit)
    return {
        "total": PacketRecorder.get_stats()["total_packets"],
        "packets": records,
        "stats": PacketRecorder.get_stats(),
    }


@app.get("/api/packets/stats")
async def packet_stats():
    """收发包统计 — 总量/字节/平均延迟/按方向分布"""
    return PacketRecorder.get_stats()


@app.get("/api/packets/stream")
async def packet_stream(agent_id: Optional[str] = Query(None), limit: int = 100):
    """Wireshark 风格报文文本流"""
    lines = PacketRecorder.get_wireshark_view(agent_id=agent_id, limit=limit)
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("\n".join(lines), media_type="text/plain")


# ═══════════════════════════════════════════════
# 系统统计 API
# ═══════════════════════════════════════════════

@app.get("/api/stats")
async def system_stats():
    """系统综合统计"""
    return {
        "service": {
            "version": "0.1.0",
            "started_at": service_state["started_at"],
            "uptime_seconds": (
                datetime.now() - datetime.fromisoformat(service_state["started_at"])
            ).total_seconds(),
            "simulations_run": service_state["simulations_run"],
        },
        "agents": AgentRegistry.get_stats(),
        "tools": {"registered": len(ToolRegistry.list_tools()), "stats": ToolRegistry.get_stats()},
        "skills": {"registered": len(SkillRegistry.list_skills())},
        "packets": PacketRecorder.get_stats(),
        "logs": logger.get_index_stats(),
    }


# ═══════════════════════════════════════════════
# 容器运行时 API — Docker Agent 管理
# ═══════════════════════════════════════════════

class ContainerAgentRequest(BaseModel):
    agent_id: str
    role: str = "scout"
    name: str = ""
    port: int = 0


@app.post("/api/containers/create")
async def container_create(req: ContainerAgentRequest):
    """创建并启动一个 Agent 容器/进程"""
    runtime = _get_runtime_with_status_listener()
    config = _get_effective_llm_config()
    ca = runtime.create_agent(
        agent_id=req.agent_id, role=req.role,
        name=req.name or req.agent_id, port=req.port,
        llm_config=config if config.get("api_key") else None,
    )
    return ca.to_dict()


@app.post("/api/containers/{agent_id}/stop")
async def container_stop(agent_id: str):
    """停止 Agent 容器"""
    runtime = _get_runtime_with_status_listener()
    runtime.stop_agent(agent_id)
    return {"stopped": agent_id}


@app.post("/api/containers/stop-all")
async def container_stop_all():
    """停止所有容器"""
    runtime = _get_runtime_with_status_listener()
    runtime.stop_all()
    return {"stopped": "all"}


@app.get("/api/containers/status")
async def container_status():
    """获取所有容器状态"""
    runtime = _get_runtime_with_status_listener()
    return runtime.get_all_status()


@app.post("/api/containers/decide-all")
async def container_decide_all():
    """触发所有容器 Agent 决策"""
    runtime = _get_runtime_with_status_listener()
    return runtime.decide_all()


@app.post("/api/containers/act-all")
async def container_act_all():
    """触发所有容器 Agent 执行"""
    runtime = _get_runtime_with_status_listener()
    return runtime.act_all()


@app.post("/api/containers/round")
async def container_round():
    """执行一轮：决策 → 执行"""
    runtime = _get_runtime_with_status_listener()
    return runtime.run_round()


# ═══════════════════════════════════════════════
# 容器控制器 API — 对应架构文档第六节 Container Controller
# ═══════════════════════════════════════════════

_controller: Optional[ContainerController] = None


def _get_controller() -> ContainerController:
    """懒加载容器控制器"""
    global _controller
    if _controller is None:
        _controller = ContainerController()
        runtime = _get_runtime_with_status_listener()
        _controller.set_runtime(runtime)
    return _controller


@app.get("/api/controller/health")
async def controller_health():
    """获取所有 Agent 健康状态摘要"""
    ctrl = _get_controller()
    runtime = _get_runtime_with_status_listener()

    # 触发一次健康检查
    import asyncio
    for agent_id in list(runtime.agents.keys()):
        await ctrl.check_health(agent_id)

    return ctrl.get_health_summary()


@app.get("/api/controller/health/{agent_id}")
async def controller_agent_health(agent_id: str):
    """获取单个 Agent 健康状态"""
    ctrl = _get_controller()
    status = await ctrl.check_health(agent_id)
    return status.to_dict()


@app.get("/api/controller/resources")
async def controller_resources(agent_id: Optional[str] = Query(None)):
    """获取 Agent 资源使用情况"""
    ctrl = _get_controller()
    await ctrl.get_resource_usage(agent_id)
    if agent_id:
        usage = ctrl._resource_usage.get(agent_id)
        return usage.to_dict() if usage else {"error": "No data"}
    return ctrl.get_resource_summary()


@app.post("/api/controller/restart/{agent_id}")
async def controller_restart(agent_id: str):
    """重启 Agent 容器"""
    ctrl = _get_controller()
    return await ctrl.restart_agent(agent_id)


@app.post("/api/controller/scale/{role}")
async def controller_scale(role: str, action: str = Query("up", regex="^(up|down)$"),
                           count: int = Query(1, ge=1, le=10)):
    """扩缩容指定角色的 Agent"""
    ctrl = _get_controller()
    if action == "up":
        return await ctrl.scale_up(role, count)
    else:
        return await ctrl.scale_down(role, count)


class ScalingPolicyRequest(BaseModel):
    role: str
    metric: str = "cpu"
    min_instances: int = 1
    max_instances: int = 10
    scale_up_threshold: float = 80.0
    scale_down_threshold: float = 20.0
    cooldown_seconds: int = 30
    scale_step: int = 1


@app.post("/api/controller/policies")
async def controller_set_policy(req: ScalingPolicyRequest):
    """设置扩缩容策略"""
    ctrl = _get_controller()
    policy = ScalingPolicy(
        role=req.role,
        metric=req.metric,
        min_instances=req.min_instances,
        max_instances=req.max_instances,
        scale_up_threshold=req.scale_up_threshold,
        scale_down_threshold=req.scale_down_threshold,
        cooldown_seconds=req.cooldown_seconds,
        scale_step=req.scale_step,
    )
    ctrl.set_scaling_policy(policy)
    return {"set": policy.to_dict()}


@app.get("/api/controller/policies")
async def controller_get_policies():
    """获取所有扩缩容策略"""
    ctrl = _get_controller()
    return ctrl.get_scaling_policies()


@app.get("/api/controller/status")
async def controller_full_status():
    """获取容器控制器完整状态"""
    ctrl = _get_controller()
    return ctrl.get_full_status()


# ═══════════════════════════════════════════════
# Agent Hub API — 统一管理层 API（对应架构文档第六节）
# ═══════════════════════════════════════════════

# 全局 AgentHub 实例
_hub: Optional[AgentHub] = None


def _get_hub() -> AgentHub:
    """懒加载 AgentHub 单例"""
    global _hub
    if _hub is None:
        _hub = get_hub()
        _hub.start()
    return _hub


class HubTaskRequest(BaseModel):
    action: str
    target_agent_id: str = ""
    priority: str = "normal"       # critical / high / normal / low / background
    delay_seconds: float = 0.0
    params: Dict[str, Any] = Field(default_factory=dict)
    max_retries: int = 3


class HubRoutingRequest(BaseModel):
    strategy: str = "round_robin"  # round_robin / least_loaded / affinity / random / best_capability


@app.get("/api/hub/status")
async def hub_status():
    """AgentHub 综合状态"""
    hub = _get_hub()
    return hub.get_status()


@app.get("/api/hub/stats")
async def hub_stats():
    """AgentHub 所有子系统统计"""
    hub = _get_hub()
    return hub.get_stats()


# ── 任务管理 ──────────────────────────────────

@app.post("/api/hub/tasks")
async def hub_submit_task(req: HubTaskRequest):
    """
    向 AgentHub 提交任务

    支持优先级、延迟执行、路由策略参数
    """
    hub = _get_hub()
    task_id = hub.schedule_task(
        action=req.action,
        target_agent_id=req.target_agent_id,
        priority=TaskPriority.from_string(req.priority),
        delay_seconds=req.delay_seconds,
        params=req.params,
        max_retries=req.max_retries,
    )
    return {
        "task_id": task_id,
        "status": "submitted",
        "priority": req.priority,
        "delay_seconds": req.delay_seconds,
    }


@app.get("/api/hub/tasks")
async def hub_list_tasks(
    status: Optional[str] = Query(None, description="pending / running / completed / failed / cancelled"),
    limit: int = Query(default=50, le=200),
):
    """列出所有任务（可按状态筛选）"""
    hub = _get_hub()

    if status:
        status_upper = status.upper()
        if status_upper == "PENDING":
            tasks = hub.scheduler.get_pending_tasks()
        elif status_upper == "RUNNING":
            tasks = hub.scheduler.get_running_tasks()
        else:
            tasks = hub.scheduler.get_recent_tasks(limit=limit)
            try:
                ts = TaskStatus[status_upper]
                tasks = [t for t in tasks if t.status == ts]
            except KeyError:
                pass
    else:
        tasks = hub.list_tasks()

    return {
        "total": len(tasks),
        "tasks": [t if isinstance(t, dict) else t.to_dict() for t in tasks][:limit],
    }


@app.get("/api/hub/tasks/{task_id}")
async def hub_get_task(task_id: str):
    """获取单个任务详情"""
    hub = _get_hub()
    task = hub.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return task.to_dict()


@app.delete("/api/hub/tasks/{task_id}")
async def hub_cancel_task(task_id: str):
    """取消任务（仅 PENDING 状态可取消）"""
    hub = _get_hub()
    ok = hub.cancel_task(task_id)
    if not ok:
        raise HTTPException(status_code=409, detail=f"Cannot cancel task '{task_id}' (not pending or not found)")
    return {"cancelled": task_id}


# ── 分发与路由 ────────────────────────────────

@app.post("/api/hub/dispatch")
async def hub_dispatch(strategy: Optional[str] = Query(None, description="路由策略，不指定则使用默认")):
    """手动触发分发所有待处理任务"""
    hub = _get_hub()
    if strategy:
        hub.set_routing_strategy(RoutingStrategy.from_string(strategy))
    records = hub.dispatch_all()
    return {
        "total": len(records),
        "succeeded": sum(1 for r in records if r.success),
        "failed": sum(1 for r in records if not r.success),
        "records": [r.to_dict() for r in records],
    }


@app.get("/api/hub/routing")
async def hub_get_routing():
    """获取当前路由策略"""
    hub = _get_hub()
    return {
        "strategy": hub.get_routing_strategy(),
        "available": [s.value for s in RoutingStrategy],
    }


@app.put("/api/hub/routing")
async def hub_set_routing(req: HubRoutingRequest):
    """切换路由策略"""
    try:
        strategy = RoutingStrategy.from_string(req.strategy)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Unknown strategy '{req.strategy}'. Available: {[s.value for s in RoutingStrategy]}")
    hub = _get_hub()
    hub.set_routing_strategy(strategy)
    return {
        "strategy": hub.get_routing_strategy(),
        "dispatcher_stats": hub.dispatcher.get_stats(),
    }


# ── 持久化 ────────────────────────────────────

@app.post("/api/hub/tasks/persist")
async def hub_persist_tasks():
    """持久化当前任务队列到磁盘"""
    hub = _get_hub()
    count = hub.persist_tasks()
    return {"persisted": count, "path": hub.dispatcher.persistence_path}


@app.post("/api/hub/tasks/restore")
async def hub_restore_tasks():
    """从磁盘恢复任务队列"""
    hub = _get_hub()
    count = hub.restore_tasks()
    return {"restored": count, "queue_depth": hub.scheduler.queue_depth}


# ── Tick（调度周期） ───────────────────────────

@app.post("/api/hub/tick")
async def hub_tick():
    """执行一个完整调度周期：出队 → 路由 → 分发 → 执行"""
    hub = _get_hub()
    records = hub.tick()
    return {
        "tick_complete": True,
        "dispatched": len(records),
        "succeeded": sum(1 for r in records if r.success),
        "failed": sum(1 for r in records if not r.success),
        "scheduler": hub.scheduler.get_stats(),
    }


# ═══════════════════════════════════════════════
# Workflow API — 工作流运行时 API（对应架构文档第三节）
# ═══════════════════════════════════════════════

# 全局 WorkflowEngine 实例
_wf_engine: Optional[WorkflowEngine] = None
_wf_history: List[Dict[str, Any]] = []


def _get_wf_engine() -> WorkflowEngine:
    global _wf_engine
    if _wf_engine is None:
        _wf_engine = WorkflowEngine(max_workers=8)
    return _wf_engine


class WorkflowRunRequest(BaseModel):
    """工作流运行请求"""
    name: str = ""
    steps: List[Dict[str, Any]] = Field(default_factory=list)
    agents: List[Dict[str, Any]] = Field(default_factory=list)  # 可选：动态创建 Agent


@app.post("/api/workflows/run")
async def workflow_run(req: WorkflowRunRequest):
    """
    运行自定义工作流

    请求体示例:
    {
      "name": "侦察-分析-决策",
      "agents": [{"agent_id": "s1", "role": "scout", "name": "侦察兵"}],
      "steps": [
        {"step_id": "1", "type": "task", "agent_id": "s1", "action": "侦察区域A"},
        {"step_id": "2", "type": "task", "agent_id": "s1", "action": "分析情报", "depends_on": ["1"]}
      ]
    }
    """
    # 动态创建 Agent（如果提供）
    if req.agents:
        from agent_network.agent import Agent
        for ad in req.agents:
            agent = Agent(
                agent_id=ad.get("agent_id", f"agent-{len(AgentRegistry.list_all())}"),
                role=ad.get("role", "generic"),
                name=ad.get("name", ad.get("agent_id", "")),
                skills=ad.get("skills", []),
                tags=ad.get("tags", []),
                capability_scores=ad.get("capability_scores", {}),
            )
            AgentRegistry.register(agent)
            agent.start()

    # 构建 WorkflowStep 列表
    try:
        steps = [WorkflowStep.from_dict(s) for s in req.steps]
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid step definition: {e}")

    # 运行
    engine = _get_wf_engine()
    context = {"registry": AgentRegistry}
    result = engine.run(steps, context=context, name=req.name or "api-workflow")

    # 保留历史
    _wf_history.append({
        "name": req.name,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        **{k: v for k, v in result.__dict__.items() if k != "logs"},
    })
    if len(_wf_history) > 50:
        _wf_history.pop(0)

    return {
        "workflow_name": result.workflow_name,
        "total_steps": result.total_steps,
        "completed": result.completed,
        "failed": result.failed,
        "skipped": result.skipped,
        "duration_seconds": result.duration_seconds,
        "steps": result.steps,
        "logs": result.logs[-30:],
    }


@app.get("/api/workflows/status")
async def workflow_status():
    """当前 WorkflowEngine 执行状态"""
    engine = _get_wf_engine()
    return engine.get_status()


@app.get("/api/workflows/history")
async def workflow_history(limit: int = Query(default=10, le=50)):
    """历史工作流执行记录"""
    return {
        "total": len(_wf_history),
        "history": _wf_history[-limit:],
    }


class WorkflowValidateRequest(BaseModel):
    steps: List[Dict[str, Any]] = Field(default_factory=list)


@app.post("/api/workflows/validate")
async def workflow_validate(req: WorkflowValidateRequest):
    """验证工作流 DAG（检测循环依赖等）"""
    try:
        wf_steps = [WorkflowStep.from_dict(s) for s in req.steps]
        dag = WorkflowDAG()
        layers = dag.build(wf_steps)
        return {
            "valid": True,
            "total_steps": len(wf_steps),
            "layers": len(layers),
            "parallelism": [len(layer) for layer in layers],
            "detail": {s.step_id: {"layer": i + 1, "depends_on": s.depends_on}
                       for i, layer in enumerate(layers) for s in layer},
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))




# ═══════════════════════════════════════════════
# 统一 Agent 日志 — 内存/容器模式共用
# ═══════════════════════════════════════════════

@app.post("/api/logs/agent")
async def agent_log_ingest(req: Request):
    """容器模式 Agent 提交决策/执行日志 — 写入统一日志器"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    agent_id = body.get("agent_id", "?")
    event = body.get("event", "act")
    detail = body.get("detail", "")
    details = body.get("details", {})

    # 同时写入旧缓冲（兼容）+ 统一日志器
    action_status = body.get("action_status", "success")
    _agent_logs.append({
        "timestamp": _beijing_time(body.get("timestamp", "")),
        "level": "ERROR" if action_status == "failed" else "INFO",
        "agent_id": agent_id,
        "agent_name": body.get("agent_name", "?"),
        "event": event,
        "detail": detail,
        "from_agent": body.get("from_agent", agent_id),
        "to_agent": body.get("to_agent", ""),
        "action": body.get("action", event),
        "action_status": action_status,
    })
    if len(_agent_logs) > 500:
        _agent_logs.pop(0)

    # 写入结构化日志 — 使用统一 schema
    action_name = body.get("action", event)
    from_agent = body.get("from_agent", agent_id)
    to_agent = body.get("to_agent", "")
    record = {
        "timestamp": _beijing_time(body.get("timestamp", "")) or datetime.now().isoformat(timespec="milliseconds"),
        "level": "ERROR" if action_status == "failed" else "INFO",
        "source": "agent",
        "component": agent_id,
        "category": "agent_behavior",
        "event": event,
        "actor": {"id": from_agent},
        "target": {"id": to_agent} if to_agent else {},
        "action": {"name": action_name, "status": action_status},
        "message": detail or f"[{agent_id}] {action_name}",
        "payload": {
            "content": body.get("content", details.get("content", "")),
            "reasoning": body.get("reasoning", details.get("reasoning", "")),
            "skill_params": body.get("skill_params", details.get("skill_params", {})),
            "skill_result": body.get("skill_result", details.get("skill_result", {})),
            **(details or {}),
        },
        "network": {},
        "trace": {},
    }
    logger.emit(record)

    # 实时推送给前端（仅日志；Agent 状态由 ContainerRuntime 的 API 生命周期驱动）
    if _ws_clients:
        asyncio.create_task(_ws_broadcast({
            "type": "agent_log", "data": _agent_logs[-1]
        }))
    return {"status": "ok", "total_logs": len(_agent_logs)}


@app.post("/api/logs/ingest")
async def log_ingest(req: Request):
    """通用日志接入 — 接收前端/外部服务的日志，写入统一日志器"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    if (
        not _simulation_active
        and body.get("category") == "network_capture"
        and body.get("event") == "llm_api_packet"
    ):
        return {"status": "dropped", "reason": "simulation_inactive"}
    record = {
        "timestamp": body.get("timestamp", ""),
        "level": body.get("level", "INFO"),
        "source": body.get("source", "external"),
        "component": body.get("component", "unknown"),
        "category": body.get("category", "system"),
        "event": body.get("event", "log"),
        "actor": body.get("actor", {}),
        "target": body.get("target", {}),
        "action": body.get("action", {}),
        "message": body.get("message", ""),
        "payload": body.get("payload", {}),
        "network": body.get("network", {}),
        "trace": body.get("trace", {}),
    }
    # 如果 message 为空但 payload.content 存在，自动填充
    if not record["message"] and record["payload"].get("content"):
        record["message"] = str(record["payload"]["content"])[:120]
    logger.ingest(record)
    return {"status": "ok"}


@app.get("/api/logs/agent")
async def agent_logs_get(limit: int = Query(default=200)):
    """获取 Agent 动作日志（从统一日志器）"""
    entries = logger.query(event="agent_action", limit=limit)
    return {"logs": entries, "total": len(entries)}


# ═══════════════════════════════════════════════
# WebSocket — 实时仿真状态推送
# ═══════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket 端点 — 实时推送 Agent 状态、日志和消息"""
    await websocket.accept()
    _ws_clients.add(websocket)

    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                if data == "status":
                    agents_data = [a.get_status() for a in AgentRegistry.list_all()]
                    await websocket.send_json({
                        "type": "status",
                        "data": {
                            "agents": agents_data,
                            "stats": AgentRegistry.get_stats(),
                            "agent_logs": _agent_logs[-50:], "log_entries": logger.get_entries(50),
                            "relationships": _current_relationships,
                        },
                    })
                elif data == "packets":
                    await websocket.send_json({
                        "type": "packets",
                        "data": {
                            "packets": PacketRecorder.get_records(limit=50),
                            "stats": PacketRecorder.get_stats(),
                        },
                    })
                elif data == "logs":
                    await websocket.send_json({
                        "type": "logs",
                        "data": {
                            "entries": logger.get_entries(50),
                            "stats": logger.get_index_stats(),
                        },
                    })
                elif data == "all":
                    agents_data = [a.get_status() for a in AgentRegistry.list_all()]
                    await websocket.send_json({
                        "type": "all",
                        "data": {
                            "agents": agents_data,
                            "stats": AgentRegistry.get_stats(),
                            "packets": PacketRecorder.get_stats(),
                            "logs": {"stats": logger.get_index_stats(), "entries": logger.get_entries(20)},
                            "agent_logs": _agent_logs[-50:], "log_entries": logger.get_entries(50),
                            "relationships": _current_relationships,
                        },
                    })
            except asyncio.TimeoutError:
                # 自动推送 agent 状态和最新消息
                agents_data = [a.get_status() for a in AgentRegistry.list_all()]
                payload = {
                    "type": "status",
                    "data": {
                        "agents": agents_data,
                        "stats": AgentRegistry.get_stats(),
                        "relationships": _current_relationships,
                    },
                }
                await websocket.send_json(payload)
    except WebSocketDisconnect:
        _ws_clients.discard(websocket)
    except Exception:
        _ws_clients.discard(websocket)


async def _ws_broadcast(data: dict):
    """向所有连接的 WebSocket 客户端广播"""
    gone = set()
    for ws in _ws_clients:
        try:
            await ws.send_json(data)
        except Exception:
            gone.add(ws)
    _ws_clients.difference_update(gone)


# ═══════════════════════════════════════════════
# 健康检查
# ═══════════════════════════════════════════════

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "version": "0.1.0",
        "timestamp": datetime.now().isoformat(),
    }


# ═══════════════════════════════════════════════
# Dashboard HTML
# ═══════════════════════════════════════════════


# ═══════════════════════════════════════════════
# Dashboard — 从外部文件加载
# ═══════════════════════════════════════════════

import pathlib
_SCENES_DIR = pathlib.Path(__file__).parent / 'scenes'
_WEB_PUBLIC_DIR = pathlib.Path(__file__).parent / 'web' / 'public'
_WEB_DIST_DIR = pathlib.Path(__file__).parent / 'web' / 'dist'
_WEB_STATIC_DIR = _WEB_DIST_DIR if (_WEB_DIST_DIR / 'dashboard.html').exists() else _WEB_PUBLIC_DIR
_DASHBOARD_PATH = _WEB_STATIC_DIR / 'dashboard.html'

def _load_dashboard() -> str:
    try:
        return _DASHBOARD_PATH.read_text(encoding='utf-8')
    except Exception:
        return '<h1>Dashboard not found</h1>'

# Dashboard HTML is loaded per-request to pick up edits without restart


# ═══════════════════════════════════════════════
# 静态文件挂载
# ═══════════════════════════════════════════════

from fastapi.staticfiles import StaticFiles
app.mount('/static', StaticFiles(directory=str(_WEB_STATIC_DIR)), name='static')


# ═══════════════════════════════════════════════
# 启动入口
# ═══════════════════════════════════════════════

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='AI Agent 仿真运行平台 - Web API 服务')
    parser.add_argument('--port', type=int, default=8000, help='服务端口 (默认: 8000)')
    parser.add_argument('--host', type=str, default='127.0.0.1', help='监听地址 (默认: 127.0.0.1)')
    parser.add_argument('--reload', action='store_true', help='开发模式热重载')
    args = parser.parse_args()

    print()
    print('╔══════════════════════════════════════════════════════════════╗')
    print('║   AI Agent 仿真运行平台 - Web API 服务 v0.1.0                ║')
    print('╠══════════════════════════════════════════════════════════════╣')
    print(f'║  地址: http://{args.host}:{args.port}                          ║')
    print(f'║  API:  http://{args.host}:{args.port}/docs                      ║')
    print(f'║  控制台: http://{args.host}:{args.port}/                        ║')
    print('╚══════════════════════════════════════════════════════════════╝')
    print()

    uvicorn.run(
        'server:app',
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level='info',
    )
