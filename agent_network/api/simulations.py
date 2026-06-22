import asyncio
import json
import uuid
import time
import random
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, Field

from agent_network import state
from agent_network.agent import AgentRegistry, Agent
from agent_network.logger import get_logger
from agent_network.event_bus import PacketRecorder
from agent_network.scene_def import get_api_config, SceneDefinition, AgentDef
from agent_network.container_runtime import get_runtime

router = APIRouter()
logger = get_logger()

_SCENES_DIR = Path("scenes")

# 保存最近的仿真结果
_simulation_results: List[Dict[str, Any]] = []

# LLM 配置缓存（运行时可通过 API 修改）
_llm_config: Dict[str, str] = {}

class SimulationRunRequest(BaseModel):
    """仿真运行请求"""
    scene: str = ""

def _get_effective_llm_config() -> Dict[str, str]:
    """获取有效的 LLM 配置：API 设置 > 环境变量"""
    config = get_api_config()
    config.update(_llm_config)
    return config

def _get_runtime_with_status_listener():
    runtime = get_runtime()
    if not hasattr(runtime, '_status_listener_set'):
        def on_status(agent_id, status):
            a = AgentRegistry.get(agent_id)
            if a:
                a.status = status
        runtime.on_status_change = on_status
        runtime._status_listener_set = True
    return runtime

def _control_agent_capture(created_cas: List[tuple], enabled: bool, requests_module) -> Dict[str, Any]:
    """启动/停止本轮已分配 Agent 容器内的 tcpdump 抓包。"""
    success = 0
    failed = 0
    for ca, _ in created_cas:
        if ca.status == "error":
            continue
        try:
            endpoint = "/capture/start" if enabled else "/capture/stop"
            resp = requests_module.post(f"{ca.url}{endpoint}", timeout=2)
            if resp.status_code == 200:
                success += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return {"success": success, "failed": failed}

def _force_layout(agents: List[Any], links: List[Dict],
                  width: float = 400, height: float = 400,
                  margin: float = 60, iterations: int = 80) -> Dict[str, tuple]:
    import math
    import random as _rnd
    n = len(agents)
    if n == 0:
        return {}

    pos = {}
    for a in agents:
        pos[a.agent_id] = [
            _rnd.uniform(margin, width - margin),
            _rnd.uniform(margin, height - margin),
        ]

    edges = set()
    for link in links:
        f = link.get("from", "").lower()
        t = link.get("to", "").lower()
        val = link.get("value", 0)
        edges.add((f, t, val))

    area = width * height
    k = math.sqrt(area / n) if n > 0 else 1

    for it in range(iterations):
        temp = 1.0 - it / iterations
        temp = max(0.02, temp * temp)

        disp = {aid: [0.0, 0.0] for aid in pos}
        ids = list(pos.keys())
        for i in range(n):
            for j in range(i + 1, n):
                aid_i, aid_j = ids[i], ids[j]
                dx = pos[aid_i][0] - pos[aid_j][0]
                dy = pos[aid_i][1] - pos[aid_j][1]
                dist = math.sqrt(dx * dx + dy * dy) or 0.01
                force = k * k / dist
                fx = (dx / dist) * force
                fy = (dy / dist) * force
                disp[aid_i][0] += fx
                disp[aid_i][1] += fy
                disp[aid_j][0] -= fx
                disp[aid_j][1] -= fy

        for f, t, val in edges:
            if f not in pos or t not in pos:
                continue
            dx = pos[t][0] - pos[f][0]
            dy = pos[t][1] - pos[f][1]
            dist = math.sqrt(dx * dx + dy * dy) or 0.01
            spring_force = (dist - k * 0.5) / k
            if val < 0:
                spring_force = -spring_force * 0.5
            else:
                spring_force = spring_force * (abs(val) / 100 + 0.3)
            fx = (dx / dist) * spring_force * k * 0.3
            fy = (dy / dist) * spring_force * k * 0.3
            disp[f][0] += fx
            disp[f][1] -= fy
            disp[t][0] -= fx
            disp[t][1] += fy

        for aid in ids:
            d = math.sqrt(disp[aid][0]**2 + disp[aid][1]**2) or 0.01
            capped = min(d, temp * k)
            pos[aid][0] += (disp[aid][0] / d) * capped
            pos[aid][1] += (disp[aid][1] / d) * capped
            pos[aid][0] = max(margin, min(width - margin, pos[aid][0]))
            pos[aid][1] = max(margin, min(height - margin, pos[aid][1]))

    for aid in pos:
        pos[aid][0] += _rnd.uniform(-12, 12)
        pos[aid][1] += _rnd.uniform(-12, 12)
        pos[aid][0] = max(margin, min(width - margin, pos[aid][0]))
        pos[aid][1] = max(margin, min(height - margin, pos[aid][1]))

    return pos

_pending_scene_def: Optional[SceneDefinition] = None
_pending_layout: Dict[str, tuple] = {}
_pending_config: Dict[str, str] = {}
_comm_matrix: Dict[str, set] = {}

def _setup_scene(scene_def: SceneDefinition) -> Dict[str, Any]:
    global _pending_scene_def, _pending_layout

    AgentRegistry.reset()
    PacketRecorder.reset()
    state.agent_logs.clear()
    logger.reset()

    from agent_network.comm import RemoteBus
    remote_bus = RemoteBus(message_bus_url=state.MESSAGE_BUS_URL)

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
    global _comm_matrix
    
    if scene_def is None:
        scene_def = _pending_scene_def
    if not scene_def:
        return {"error": "No scene setup. Call /api/simulations/setup first."}

    import requests as _req

    runtime = _get_runtime_with_status_listener()
    runtime.reset()

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
            agent = AgentRegistry.get(ca.agent_id)
            if agent:
                agent.container_url = ca.url

    assigned_count = sum(1 for ca, _ in created_cas if ca.status != "error")
    logger.system("container_pool",
        f"容器分配完成: {assigned_count}/{len(scene_def.agents)} Agent 分配成功",
        details={"total_agents": len(scene_def.agents), "assigned": assigned_count,
                  "errors": assign_errors})

    if assign_errors:
        created_cas = [(ca, tasks) for ca, tasks in created_cas if ca.status != "error"]
        logger.system("container_pool",
            f"警告: {len(assign_errors)} 个 Agent 分配失败，将被跳过",
            details={"skipped": [e["agent_id"] for e in assign_errors]})

    for ca, _ in created_cas:
        try:
            _req.post(f"{ca.url}/reset", timeout=3)
        except Exception:
            pass

    time.sleep(1)
    for ca, _ in created_cas:
        try:
            _req.post(f"{state.MESSAGE_BUS_URL}/register",
                      params={"agent_id": ca.agent_id, "url": ca.url, "name": ca.name}, timeout=3)
            runtime._set_status(ca, "idle", {"phase": "bus_register"})
        except Exception:
            runtime._set_status(ca, "error", {"phase": "bus_register", "error": "message_bus_register_failed"})

    event_triggers = getattr(scene_def, 'event_triggers', []) or []

    _comm_matrix.clear()
    for edge in (scene_def.workflow or []):
        src = edge.get("from", "").lower()
        dst = edge.get("to", "").lower()
        if src and dst:
            _comm_matrix.setdefault(src, set()).add(dst)
            _comm_matrix.setdefault(dst, set()).add(src)

    state.agent_logs.clear()
    logger.start_session(scene_def.scene_name)
    state.reset_token_usage_state(getattr(logger, "_session_id", ""))
    
    try:
        _req.post(f"{state.MESSAGE_BUS_URL}/session/start",
                  params={"session_dir": logger._session_dir}, timeout=3)
    except Exception:
        pass

    state.simulation_active = True
    capture_start = _control_agent_capture(created_cas, True, _req)
    logger.system("capture_control", "network_capture started",
                  details={"enabled": True, **capture_start})

    talk_id = f"talk-{uuid.uuid4().hex[:12]}-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    channel_map: Dict[str, str] = {}
    for edge in (scene_def.workflow or []):
        src = edge.get("from", "")
        dst = edge.get("to", "")
        ch = edge.get("channel_id", "")
        if src and dst:
            channel_map[f"{src}->{dst}"] = ch
            channel_map[f"{src.lower()}->{dst.lower()}"] = ch

    MAX_ROUNDS = state.termination_config.get("max_rounds", 20)
    stalemate_threshold = state.termination_config.get("stalemate_rounds", 3)
    results_log = []
    silent_rounds = 0
    stop_reason = "hard_limit"

    state.simulation_stop_requested = False
    try:
        for round_num in range(MAX_ROUNDS):
            if state.simulation_stop_requested:
                stop_reason = "user_stopped"
                logger.system("simulation_stopped", "用户手动停止仿真", details={"round": round_num + 1})
                break
            
            state.current_turn = round_num + 1

            for trigger in event_triggers:
                if trigger.get("turn") == state.current_turn:
                    event_payload = {
                        "event_name": trigger.get("event_name", "未知事件"),
                        "impact": trigger.get("impact", ""),
                        "turn": state.current_turn,
                    }
                    logger.event_trigger(state.current_turn, event_payload['event_name'], event_payload['impact'])
                    for ca, _ in created_cas:
                        try:
                            _req.post(f"{ca.url}/event", json=event_payload, timeout=5)
                        except Exception:
                            pass

            context = {
                "round": state.current_turn,
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

            if state.active_skills_module and hasattr(state.active_skills_module, '_engine'):
                eng = state.active_skills_module._engine
                registry = getattr(eng, 'round_action_registry', {})
                if registry:
                    latest_round = max(registry.keys())
                    for soldier_id, (gx, gy) in registry[latest_round].items():
                        agent = AgentRegistry.get(soldier_id)
                        if agent:
                            agent.x = float(gx)
                            agent.y = float(gy)

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
        state.simulation_active = False
        capture_stop = _control_agent_capture(created_cas, False, _req)
        logger.system("capture_control", "network_capture stopped",
                      details={"enabled": False, **capture_stop})
        final_status = "error" if stop_reason == "user_stopped" else "idle"
        for ca, _ in created_cas:
            if ca.status != "error":
                runtime._set_status(ca, final_status, {"phase": "simulation:finish", "stop_reason": stop_reason})

    state.current_relationships = scene_def.workflow
    registry_agents = [a.get_status() for a in AgentRegistry.list_all()]
    actual_rounds = len(results_log)
    runtime_agent_count = len(runtime.agents)

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
    folder = _SCENES_DIR / scene_name

    meta = json.loads((folder / "meta_and_roles.json").read_text(encoding='utf-8'))
    instances = json.loads((folder / "instances_and_skills.json").read_text(encoding='utf-8'))
    topology = json.loads((folder / "network_topology.json").read_text(encoding='utf-8'))

    smeta = meta.get("scenario_metadata", {})
    title = smeta.get("title", scene_name)
    bg = smeta.get("global_rules", "")
    
    if smeta.get("max_rounds"):
        state.termination_config["max_rounds"] = int(smeta["max_rounds"])
    if smeta.get("stalemate_rounds"):
        state.termination_config["stalemate_rounds"] = int(smeta["stalemate_rounds"])
        
    state.current_scene_name = scene_name
    state.current_max_rounds = state.termination_config.get("max_rounds", 20)
    
    roles = meta.get("roles", {})
    containers = instances.get("container_instances", {})

    skills_info = []
    skills_py = folder / "skills.py"
    if skills_py.exists():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(f"skills_{scene_name}", skills_py)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            state.active_skills_module = mod
            for name, func in mod.SkillRegistry._skills.items():
                schema = getattr(mod.SkillRegistry, '_params', {}).get(name, {})
                skills_info.append({
                    "name": name,
                    "desc": (func.__doc__ or "").strip(),
                    "params": schema.get("required", []),
                    "optional_params": schema.get("optional", []),
                })
        except Exception:
            state.active_skills_module = None

    agents: List[AgentDef] = []
    for role_id, role in roles.items():
        instance = containers.get(role_id, {})
        raw_skills = instance.get("skills") or instance.get("skill_bindings") or []
        if raw_skills and isinstance(raw_skills[0], dict):
            skills = [s["skill_name"] for s in raw_skills]
        else:
            skills = raw_skills
        backend = role.get("model_backbone", "brain")
        if backend == "claudecode":
            backend = "claude-code"

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
            tasks=skills[:6] if skills else [role.get("core_goal", "")],
            extra_meta={
                "identity": role.get("identity", ""),
                "core_goal": role.get("core_goal", ""),
                "hidden_secret": role.get("hidden_secret", ""),
                "initial_assets": role.get("initial_assets", {}),
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

    relationships = []
    for subnet in topology.get("sub_networks", []):
        for edge in subnet.get("edges", []):
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


@router.post("/simulations/run")
async def run_simulation(req: SimulationRunRequest):
    """运行仿真场景 — setup + launch 一体化"""
    if not req.scene or not (_SCENES_DIR / req.scene).is_dir():
        raise HTTPException(status_code=400, detail=f"Scene '{req.scene}' not found")
    scene_def = _build_scene_from_folder(req.scene)

    result = _setup_scene(scene_def)
    state.current_relationships = result["relationships"]

    config = _get_effective_llm_config()
    loop = asyncio.get_event_loop()
    launch = await loop.run_in_executor(None, _launch_containers, config)
    result.update(launch)

    state.service_state["simulations_run"] += 1
    return result


@router.post("/simulations/setup")
async def setup_simulation(req: SimulationRunRequest):
    """Step 1: 绘制场景 — 创建 Agent、返回布局和关系（不启动容器）"""
    global _pending_config
    if not req.scene or not (_SCENES_DIR / req.scene).is_dir():
        raise HTTPException(status_code=400, detail=f"Scene '{req.scene}' not found")
    scene_def = _build_scene_from_folder(req.scene)

    _pending_config = _get_effective_llm_config()
    result = _setup_scene(scene_def)
    state.current_relationships = result["relationships"]
    return result


@router.post("/simulations/launch")
async def launch_simulation():
    """Step 2: 拉起 Docker — 为已 setup 的 Agent 启动容器并运行仿真"""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _launch_containers, _pending_config, _pending_scene_def)
    return result


@router.post("/simulations/stop")
async def stop_simulation():
    """停止正在运行的仿真"""
    state.simulation_stop_requested = True
    return {"status": "stop_requested"}


@router.get("/simulations/results")
async def simulation_results():
    """获取最新仿真结果（简单缓存）"""
    return {"results": _simulation_results}


# ═══════════════════════════════════════════════
# 场景文件 API (挂载到 /scenes 或通过 router 调整路径前缀)
# ═══════════════════════════════════════════════

@router.get("/scenes")
async def list_scenes():
    """列出所有可用场景（文件夹格式）"""
    if not _SCENES_DIR.exists():
        return {"scenes": []}
    scenes = []
    for f in sorted(_SCENES_DIR.iterdir(), key=lambda n: n.name.lower()):
        if f.is_dir() and (f / "meta_and_roles.json").exists():
            scenes.append({"name": f.name, "format": "folder"})
    return {"scenes": scenes}


@router.get("/scenes/state")
async def scene_state_unified():
    """统一的场景面板数据端点"""
    agents = [a.get_status() for a in AgentRegistry.list_all()]
    custom = None
    if state.active_skills_module and hasattr(state.active_skills_module, 'get_panel_state'):
        try:
            custom = state.active_skills_module.get_panel_state()
        except Exception:
            custom = None
    return {
        "scene": state.current_scene_name,
        "running": state.simulation_active,
        "round": state.current_turn,
        "max_rounds": state.current_max_rounds,
        "agents": agents,
        "custom": custom,
    }


@router.get("/scenes/{scene_name}")
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


@router.get("/scenes/{scene_name}/panel", response_class=HTMLResponse)
async def scene_panel(scene_name: str):
    """返回场景自带的可视化面板 HTML"""
    folder = _SCENES_DIR / scene_name
    panel_path = folder / "panel.html"
    if panel_path.exists():
        return HTMLResponse(content=panel_path.read_text(encoding='utf-8'))
    raise HTTPException(status_code=404, detail="Panel not found")


@router.get("/scenes/{scene_name}/{filename:path}")
async def scene_asset(scene_name: str, filename: str):
    """提供场景文件夹中的静态资源（图片、CSS等）"""
    folder = _SCENES_DIR / scene_name
    file_path = folder / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"Asset '{filename}' not found")
    return FileResponse(str(file_path))


@router.get("/minesweeper/board")
async def minesweeper_board():
    """返回扫雷场景的棋盘状态（从 _engine 读取）"""
    if not state.active_skills_module or not hasattr(state.active_skills_module, '_engine'):
        return {"board": None, "error": "扫雷引擎未加载"}
    eng = state.active_skills_module._engine
    SIZE = getattr(eng, 'SIZE', 9)
    revealed = eng.revealed
    mines = eng.mines
    board_data = []
    for y in range(SIZE):
        row = []
        for x in range(SIZE):
            if (x, y) in revealed:
                cell = {"state": "revealed", "is_mine": (x, y) in mines, "adj": 0}
                if not cell["is_mine"]:
                    adj = sum(1 for dx in (-1,0,1) for dy in (-1,0,1) if (x+dx, y+dy) in mines)
                    cell["adj"] = adj
            else:
                cell = {"state": "hidden"}
            row.append(cell)
        board_data.append(row)
    
    registry = getattr(eng, 'round_action_registry', {})
    agents_pos = {}
    if registry:
        latest = max(registry.keys())
        agents_pos = registry[latest]
        
    return {"board": board_data, "size": SIZE, "agents": agents_pos}
