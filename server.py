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
import time

# 导入平台核心模块
from agent_network.agent import Agent, AgentRegistry, Message
from agent_network.agent_hub import AgentHub
from agent_network.llm_parser import parse_script, get_api_config, SceneDefinition, AgentDef
from agent_network.container_runtime import get_runtime, ContainerRuntime
from agent_network.container_controller import ContainerController
from agent_network.logger import SimulationLogger, LogLevel
from agent_network.event_bus import PacketRecorder
from agent_network.tool import ToolRegistry
from agent_network.skill import SkillRegistry
from agent_network.workflow import WorkflowEngine

# ── 统一 Agent 日志缓冲区 ──
_agent_logs: List[Dict[str, Any]] = []  # 内存/容器模式共用日志

# ═══════════════════════════════════════════════
# Lifespan — 替代已弃用的 on_event
# ═══════════════════════════════════════════════

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
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


# 全局服务状态
service_state = {
    "started_at": datetime.now().isoformat(timespec="seconds"),
    "simulations_run": 0,
    "active_engine": None,
}

_current_map: Optional[Dict[str, Any]] = None  # 当前地形地图 (TerrainMap.to_dict())
_current_map_obj: Optional[Any] = None           # TerrainMap 实例 (用于寻路等操作)


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
    scene: str = "auto"  # "battlefield" | "fleet" | "auto"
    name: str = ""
    script: Optional[str] = None  # 自然语言剧本（scene=auto 时使用 LLM 解析）
    script_json: Optional[Dict[str, Any]] = None  # 结构化场景定义（直接映射，不经过 LLM）


class SettingsRequest(BaseModel):
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None


class ScriptParseRequest(BaseModel):
    script: str
    use_llm: bool = True


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
    return HTMLResponse(content=_load_dashboard())


# ═══════════════════════════════════════════════
# Agent 管理 API — 对应架构文档第六节
# ═══════════════════════════════════════════════

@app.get("/api/agents", response_model=List[AgentStatus])
async def list_agents():
    """列出所有已注册 Agent"""
    return [a.get_status() for a in AgentRegistry.list_all()]


@app.get("/api/agents/{agent_id}", response_model=AgentStatus)
async def get_agent(agent_id: str):
    """获取指定 Agent 详情"""
    agent = AgentRegistry.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return agent.get_status()


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


@app.post("/api/skills/execute")
async def execute_skill(req: SkillExecuteRequest):
    """执行技能"""
    try:
        result = SkillRegistry.execute(req.skill_name, **req.params)
        return {"skill": req.skill_name, "result": result}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


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


def _run_scene_definition(scene_def: SceneDefinition, engine_name: str) -> Dict[str, Any]:
    """根据 SceneDefinition 创建并运行场景（容器模式）"""
    config = _get_effective_llm_config()
    _agent_logs.clear()
    return _run_with_containers(scene_def, engine_name, config)


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

    return pos


def _run_with_containers(scene_def: SceneDefinition, engine_name: str,
                         config: Dict[str, str]) -> Dict[str, Any]:
    """
    容器化运行场景：每个 Agent 运行在独立子进程中，通过 HTTP 通信。
    Docker 可用时自动切换为 Docker 容器模式。
    """
    # 检测 Docker 是否可用
    mode = "process"
    try:
        import docker
        docker.from_env()
        mode = "docker"
        print(f"[Container] Docker detected, using container mode")
    except Exception:
        print(f"[Container] Docker unavailable, using subprocess mode")

    runtime = get_runtime(mode=mode)
    runtime.stop_all()  # 清理旧的容器/进程
    AgentRegistry.reset()

    # 统一通信层（容器模式 — 注册用的 stub，实际通信在子进程内）
    from agent_network.comm import RemoteBus
    remote_bus = RemoteBus(message_bus_url="http://localhost:9000")

    # 力导向布局：根据 relationship 关系优化 Agent 位置
    layout_pos = _force_layout(scene_def.agents, scene_def.workflow)

    created_cas = []
    for ad in scene_def.agents:
        # 注册到 AgentRegistry（供 WebSocket 和 API 查询）
        agent = Agent(
            agent_id=ad.agent_id,
            role=ad.role,
            name=ad.name,
            skills=ad.skills,
            tags=ad.tags,
        )
        agent.set_comm(remote_bus)
        # 用力导向布局位置
        lx, ly = layout_pos.get(ad.agent_id, (random.uniform(50, 350), random.uniform(50, 350)))
        agent.x = lx
        agent.y = ly
        agent.pending_task_descs = ad.tasks
        agent.extra_meta = ad.extra_meta
        AgentRegistry.register(agent)
        agent.start()

        ca = runtime.create_agent(
            agent_id=ad.agent_id,
            role=ad.role,
            name=ad.name,
            llm_config=config if config.get("api_key") else None,
            extra_meta=ad.extra_meta if ad.extra_meta else None,
        )
        created_cas.append((ca, agent, ad.tasks))

    # 等待所有 Agent 启动（子进程并行启动）
    time.sleep(2)
    # 批量向消息总线注册
    for ca, agent, _ in created_cas:
        try:
            import requests as _req
            _req.post("http://localhost:9000/register",
                      params={"agent_id": ca.agent_id, "url": ca.url, "name": ca.name}, timeout=3)
            ca.status = "running"
        except Exception:
            ca.status = "error"

    # 运行仿真轮次
    agent_count = len(created_cas)
    rounds = max(2, min(5, agent_count // 2))
    results_log = []

    for round_num in range(rounds):
        context = {
            "round": round_num + 1,
            "total_rounds": rounds,
            "scene": scene_def.scene_name,
            "agents": [{"id": ca.agent_id, "role": ca.role, "name": ca.name}
                       for ca, _, _ in created_cas],
            "tasks": {ca.agent_id: tasks for ca, _, tasks in created_cas},
        }
        round_result = runtime.run_round(context)
        results_log.append(round_result)
        time.sleep(0.3)

    # 收集最终状态 — 与内存模式 engine._collect_results() 统一格式
    registry_agents = [a.get_status() for a in AgentRegistry.list_all()]

    return {
        "simulation_name": engine_name,
        "duration_seconds": round(rounds * 1.5, 2),
        "agents": registry_agents,
        "agent_stats": AgentRegistry.get_stats(),
        "packet_stats": PacketRecorder.get_stats(),
        "rounds": rounds,
        "results_log": results_log,
        "container_mode": mode,
    }


def _build_scene_from_script_json(sj: Dict[str, Any]) -> SceneDefinition:
    """将 script_json 格式直接映射为 SceneDefinition（不需要 LLM）。"""
    meta = sj.get("scenario_metadata", {})
    title = meta.get("title", "自定义场景")
    bg = meta.get("background_rules", "")
    role_slots = sj.get("role_slots", [])

    agents: List[AgentDef] = []
    for slot in role_slots:
        slot_id = slot.get("slot_id", "").lower()  # ROLE_A → role-a
        identity = slot.get("identity", slot_id)
        core_goal = slot.get("core_goal", "")
        action_space = slot.get("action_space", [])
        hidden_secret = slot.get("hidden_secret", "")
        initial_assets = slot.get("initial_assets", {})

        agent = AgentDef(
            agent_id=slot_id,
            role="generic",           # 非军事角色，统一 generic
            name=identity,
            skills=action_space[:4],   # 前4个action作为skill
            tags=[core_goal[:30]],     # core_goal 截断为 tag
            tasks=[core_goal],         # core_goal 作为主任务
            extra_meta={
                "identity": identity,
                "core_goal": core_goal,
                "hidden_secret": hidden_secret,
                "initial_assets": initial_assets,
                "action_space": action_space,
                "background_rules": bg,  # scene system prompt
            },
        )
        agents.append(agent)

    # 关系矩阵 → workflow（每个关系作为一个消息步骤）
    relationships = sj.get("initial_relationship_matrix", {}).get("links", [])
    event_triggers = sj.get("event_triggers", [])

    return SceneDefinition(
        scene_name=title,
        description=bg,
        agents=agents,
        workflow=relationships,       # 保留原始关系数据
        event_triggers=event_triggers,  # type: ignore[call-arg]
    )


@app.post("/api/simulations/run")
async def run_simulation(req: SimulationRunRequest):
    """运行仿真场景 — 支持 auto/battlefield/fleet + LLM 解析 + script_json 直映"""
    global service_state

    name = req.name or f"{req.scene}-{service_state['simulations_run'] + 1}"

    # ── 优先：script_json 直接映射（不需要 LLM） ──
    if req.script_json:
        scene_def = _build_scene_from_script_json(req.script_json)
        use_llm = False

    # ── 其次：自然语言 script → LLM/模板解析 ──
    elif req.scene == "auto" or req.script:
        script_text = req.script or req.scene
        config = _get_effective_llm_config()
        use_llm = bool(config.get("api_key"))
        scene_def = parse_script(script_text, use_llm=use_llm, config=config)

    # ── 执行场景 ──
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _run_scene_definition, scene_def, name)
    result["llm_parsed"] = use_llm
    result["scene_definition"] = {
        "scene_name": scene_def.scene_name,
        "description": scene_def.description,
        "agents": [{"agent_id": a.agent_id, "role": a.role, "name": a.name, "tasks": a.tasks}
                   for a in scene_def.agents],
        "relationships": scene_def.workflow,
    }
    result["relationships"] = scene_def.workflow

    _simulation_results.append(result)
    if len(_simulation_results) > 20:
        _simulation_results.pop(0)

    service_state["simulations_run"] += 1
    return result


# ═══════════════════════════════════════════════
# 脚本解析 API
# ═══════════════════════════════════════════════

@app.post("/api/scripts/parse")
async def parse_script_endpoint(req: ScriptParseRequest):
    """
    LLM 解析自然语言剧本 → 返回结构化场景定义

    用于前端预览：用户在编辑器中输入自然语言，点"解析"预览 Agent 配置
    """
    config = _get_effective_llm_config()
    use_llm = req.use_llm and bool(config.get("api_key"))
    scene_def = parse_script(req.script, use_llm=use_llm, config=config)
    return {
        "llm_used": use_llm,
        "scene_name": scene_def.scene_name,
        "description": scene_def.description,
        "agents": [
            {
                "agent_id": a.agent_id,
                "role": a.role,
                "name": a.name,
                "skills": a.skills,
                "tags": a.tags,
                "tasks": a.tasks,
            }
            for a in scene_def.agents
        ],
        "workflow": scene_def.workflow,
    }


# ═══════════════════════════════════════════════
# 场景文件 API
# ═══════════════════════════════════════════════

_SCENES_DIR = None  # initialized after pathlib import below


@app.get("/api/scenes")
async def list_scenes():
    """列出所有可用的 .md 场景文件"""
    if not _SCENES_DIR.exists():
        return {"scenes": []}
    files = sorted(
        [f.name for f in _SCENES_DIR.iterdir() if f.suffix == '.json'],
        key=lambda n: n.lower()
    )
    return {"scenes": files}


@app.get("/api/scenes/{filename}")
async def read_scene(filename: str):
    """读取指定 .json 场景文件内容"""
    path = (_SCENES_DIR / filename).resolve()
    # Prevent path traversal
    if not str(path).startswith(str(_SCENES_DIR.resolve())):
        raise HTTPException(status_code=403, detail="Invalid path")
    if not path.exists() or path.suffix != '.json':
        raise HTTPException(status_code=404, detail=f"Scene '{filename}' not found")
    content = path.read_text(encoding='utf-8')
    name = path.stem
    return {"filename": filename, "name": name, "content": content}


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

# 全局日志收集器
_global_logger = SimulationLogger("api-server")


@app.get("/api/logs")
async def query_logs(
    agent_id: Optional[str] = Query(None),
    level: Optional[str] = Query(None),
    level_type: Optional[str] = Query(None),
    event: Optional[str] = Query(None),
    index: Optional[str] = Query(None),
    limit: int = Query(default=50, le=500),
):
    """日志查询 API"""
    # 内存查询
    log_level = None
    if level:
        try:
            log_level = LogLevel[level.upper()]
        except KeyError:
            pass

    entries = _global_logger.query(
        agent_id=agent_id,
        level=log_level,
        level_type=level_type,
        event_contains=event,
        index=index,
        limit=limit,
    )
    return {
        "backend": "memory",
        "total": len(entries),
        "entries": [e.to_dict() for e in entries],
    }


@app.get("/api/logs/stats")
async def log_stats():
    """日志索引统计"""
    return _global_logger.get_index_stats()


# ═══════════════════════════════════════════════
# Packet 查询 API — 对应架构文档第九节
# ═══════════════════════════════════════════════

@app.get("/api/packets")
async def query_packets(agent_id: Optional[str] = Query(None)):
    """查询收发包记录"""
    records = PacketRecorder.get_records(agent_id=agent_id)
    return {
        "total": len(records),
        "records": [r.to_dict() for r in records],
    }


@app.get("/api/packets/stats")
async def packet_stats():
    """收发包统计"""
    return PacketRecorder.get_stats()


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
        "logs": _global_logger.get_index_stats(),
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
    runtime = get_runtime(mode="process")
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
    runtime = get_runtime()
    runtime.stop_agent(agent_id)
    return {"stopped": agent_id}


@app.post("/api/containers/stop-all")
async def container_stop_all():
    """停止所有容器"""
    runtime = get_runtime()
    runtime.stop_all()
    return {"stopped": "all"}


@app.get("/api/containers/status")
async def container_status():
    """获取所有容器状态"""
    runtime = get_runtime()
    return runtime.get_all_status()


@app.post("/api/containers/decide-all")
async def container_decide_all():
    """触发所有容器 Agent 决策"""
    runtime = get_runtime()
    return runtime.decide_all()


@app.post("/api/containers/act-all")
async def container_act_all():
    """触发所有容器 Agent 执行"""
    runtime = get_runtime()
    return runtime.act_all()


@app.post("/api/containers/round")
async def container_round():
    """执行一轮：决策 → 执行"""
    runtime = get_runtime()
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
        runtime = get_runtime(mode="process")
        _controller.set_runtime(runtime)
    return _controller


@app.get("/api/controller/health")
async def controller_health():
    """获取所有 Agent 健康状态摘要"""
    ctrl = _get_controller()
    runtime = get_runtime()

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
# 地图 API — 地形生成、Agent 部署、移动
# ═══════════════════════════════════════════════

class MapGenerateRequest(BaseModel):
    size: int = Field(default=16, ge=4, le=64, description="地图尺寸 (4-64)")
    use_llm: bool = Field(default=False, description="是否使用 LLM 生成")


class MapPlaceRequest(BaseModel):
    agent_ids: List[str]
    positions: List[List[float]]  # [[col, row], ...]


class MapPlaceRandomRequest(BaseModel):
    agent_ids: List[str]


class MapMoveRequest(BaseModel):
    agent_id: str
    target_x: float
    target_y: float


@app.post("/api/map/generate")
async def map_generate(req: MapGenerateRequest):
    """
    生成地形地图

    - **size**: 网格尺寸 (4-64)
    - **use_llm**: 是否使用 LLM 智能生成 (需要配置 API Key)
    """
    global _current_map, _current_map_obj

    config = _get_effective_llm_config()
    tm = generate_map_with_llm(size=req.size, config=config, use_llm=req.use_llm)
    _current_map_obj = tm
    _current_map = tm.to_dict()
    return _current_map


@app.get("/api/map/state")
async def map_state():
    """获取当前地图状态"""
    if _current_map is None:
        raise HTTPException(status_code=404, detail="尚未生成地图，请先 POST /api/map/generate")
    return _current_map


@app.post("/api/map/agents/place")
async def map_place_agents(req: MapPlaceRequest):
    """将 Agent 部署到指定坐标"""
    if _current_map is None:
        raise HTTPException(status_code=400, detail="请先生成地图")

    placed = 0
    for agent_id, (col, row) in zip(req.agent_ids, req.positions):
        agent = AgentRegistry.get(agent_id)
        if agent:
            if not is_passable(_current_map, int(col), int(row)):
                continue
            agent.x = float(col)
            agent.y = float(row)
            placed += 1
    return {"placed": placed, "total": len(req.agent_ids)}


@app.post("/api/map/agents/place-random")
async def map_place_random(req: MapPlaceRandomRequest):
    """随机部署 Agent 到地图可通行格"""
    if _current_map is None:
        raise HTTPException(status_code=400, detail="请先生成地图")

    tm = _current_map_obj
    if tm is None:
        raise HTTPException(status_code=500, detail="地图对象不可用")

    passable = tm.find_passable_cells()
    if not passable:
        raise HTTPException(status_code=400, detail="地图无可用通行格")

    placed = 0
    for agent_id in req.agent_ids:
        agent = AgentRegistry.get(agent_id)
        if agent and passable:
            col, row = random.choice(passable)
            agent.x = float(col)
            agent.y = float(row)
            placed += 1
    return {"placed": placed, "total": len(req.agent_ids)}


@app.post("/api/map/agents/move")
async def map_move_agent(req: MapMoveRequest):
    """设置 Agent 移动目标"""
    if _current_map is None:
        raise HTTPException(status_code=400, detail="请先生成地图")

    agent = AgentRegistry.get(req.agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")

    target_col, target_row = int(req.target_x), int(req.target_y)
    if not is_passable(_current_map, target_col, target_row):
        raise HTTPException(status_code=400, detail="目标格不可通行")

    agent._target_x = float(req.target_x)
    agent._target_y = float(req.target_y)
    return {
        "agent_id": req.agent_id,
        "from": [agent.x, agent.y],
        "target": [req.target_x, req.target_y],
    }


@app.post("/api/map/tick")
async def map_tick():
    """执行一次仿真 tick，更新所有 Agent 位置"""
    if _current_map is None:
        raise HTTPException(status_code=400, detail="请先生成地图")

    import math

    updated = []
    for agent in AgentRegistry.list_all():
        if agent._target_x is not None and agent._target_y is not None:
            dx = agent._target_x - agent.x
            dy = agent._target_y - agent.y
            dist = math.sqrt(dx * dx + dy * dy)
            if dist < 0.1:
                agent.x = agent._target_x
                agent.y = agent._target_y
                agent._target_x = None
                agent._target_y = None
            else:
                step = agent.speed * 0.3  # 每 tick 移动 0.3 格
                agent.x += (dx / dist) * min(step, dist)
                agent.y += (dy / dist) * min(step, dist)
            updated.append(agent)

    return {
        "tick": "completed",
        "agents": [a.get_status() for a in updated],
        "total_updated": len(updated),
    }


@app.get("/api/map/path")
async def map_find_path(
    from_x: float = Query(...),
    from_y: float = Query(...),
    to_x: float = Query(...),
    to_y: float = Query(...),
):
    """计算两点之间的路径 (A*)"""
    if _current_map is None:
        raise HTTPException(status_code=400, detail="请先生成地图")

    grid = _current_map.get("grid", [])
    if not grid:
        raise HTTPException(status_code=500, detail="地图网格不可用")

    start = (int(from_x), int(from_y))
    goal = (int(to_x), int(to_y))
    path = astar(grid, start, goal)
    return {
        "path": path,
        "length": len(path),
        "reachable": len(path) > 0,
        "from": list(start),
        "to": list(goal),
    }


# ═══════════════════════════════════════════════
# 统一 Agent 日志 — 内存/容器模式共用
# ═══════════════════════════════════════════════

@app.post("/api/logs/agent")
async def agent_log_ingest(req: Request):
    """容器模式 Agent 提交决策/执行日志"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    _agent_logs.append({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "agent_id": body.get("agent_id", "?"),
        "agent_name": body.get("agent_name", "?"),
        "event": body.get("event", "act"),
        "detail": body.get("detail", ""),
    })
    if len(_agent_logs) > 500:
        _agent_logs.pop(0)
    return {"status": "ok", "total_logs": len(_agent_logs)}


@app.get("/api/logs/agent")
async def agent_logs_get():
    """获取 Agent 日志"""
    return {"logs": _agent_logs[-200:], "total": len(_agent_logs)}


# ═══════════════════════════════════════════════
# WebSocket — 实时仿真状态推送
# ═══════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket 端点 — 实时推送 Agent 状态、日志和消息"""
    await websocket.accept()

    # 注册到 EventBus 全局广播 — 消息实时推送
    received_messages = []

    def on_message(msg: Message):
        received_messages.append(msg.to_dict())

    # 给 EventBus 注册广播回调
    event_bus = None
    for agent in AgentRegistry.list_all():
        if agent.event_bus:
            event_bus = agent.event_bus
            break
    if not event_bus:
        event_bus = EventBus("ws-bus")
    event_bus.on_broadcast(on_message)

    # 状态推送循环
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
                            "map": _current_map,
                            "agent_logs": _agent_logs[-50:],
                        },
                    })
                elif data == "packets":
                    await websocket.send_json({
                        "type": "packets",
                        "data": {
                            "records": [r.to_dict() for r in PacketRecorder.get_records()],
                            "stats": PacketRecorder.get_stats(),
                        },
                    })
                elif data == "logs":
                    await websocket.send_json({
                        "type": "logs",
                        "data": {
                            "entries": [e.to_dict() for e in _global_logger.get_entries()[-50:]],
                            "stats": _global_logger.get_index_stats(),
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
                            "logs": _global_logger.get_index_stats(),
                            "map": _current_map,
                            "agent_logs": _agent_logs[-50:],
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
                        "map": _current_map,
                    },
                }
                # 推送积压的消息
                if received_messages:
                    payload["data"]["messages"] = received_messages[-10:]
                    received_messages.clear()
                await websocket.send_json(payload)
    except WebSocketDisconnect:
        pass


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
_DASHBOARD_PATH = pathlib.Path(__file__).parent / 'web' / 'dist' / 'dashboard.html'

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
app.mount('/static', StaticFiles(directory=str(pathlib.Path(__file__).parent / 'web' / 'dist')), name='static')


# ═══════════════════════════════════════════════
# 战术地图 (Procedural Tactical Situation Map)
# ═══════════════════════════════════════════════

_TACTICAL_PATH = pathlib.Path(__file__).parent / 'web' / 'dist'

def _load_tactical_map() -> str:
    try:
        return (_TACTICAL_PATH / 'index.html').read_text(encoding='utf-8')
    except Exception:
        return '<h1>Tactical Map not built — run: cd web && npm run build</h1>'

TACTICAL_HTML = None  # reloaded on each request to pick up rebuilds

@app.get("/tactical-map", response_class=HTMLResponse)
async def tactical_map():
    """程序化战术态势地图 — Procedural Tactical Situation Map"""
    html = _load_tactical_map()
    return HTMLResponse(content=html)

if _TACTICAL_PATH.exists():
    # Mount the entire dist directory to serve JS, CSS, favicon, icons, etc.
    # Route /tactical-map (above) takes priority over this mount for exact matches.
    app.mount('/tactical-map', StaticFiles(directory=str(_TACTICAL_PATH), html=True), name='tactical-map')


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
    print(f'║  战术地图: http://{args.host}:{args.port}/tactical-map          ║')
    print('╚══════════════════════════════════════════════════════════════╝')
    print()

    uvicorn.run(
        'server:app',
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level='info',
    )
