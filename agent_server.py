#!/usr/bin/env python3
"""
Agent 容器运行时 — 每个 Docker 容器/子进程内运行的 HTTP 服务

接收消息 → Agent.decide() → 执行动作 → 通过 RemoteBus 发送消息

环境变量:
  AGENT_ID:         Agent ID
  AGENT_ROLE:       角色
  AGENT_NAME:       名称
  PORT:             监听端口
  MESSAGE_BUS_URL:  消息总线地址 (默认 localhost:9000)
  AGENT_CORE_GOAL:  核心目标 (可选)
  AGENT_HIDDEN_SECRET: 隐藏秘密 (可选)
  AGENT_ACTION_SPACE:  可用行动 JSON (可选)
  AGENT_INITIAL_ASSETS: 初始资产 JSON (可选)
  LLM_API_KEY:       LLM API Key (可选)
  LLM_MODEL:         模型名 (可选)

统一通信层: RemoteBus → Message Bus HTTP 中转
"""

import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import uvicorn
import requests

from agent_network.agent import Agent
from agent_network.comm import RemoteBus
from agent_network.logger import get_logger
from agent_network.event_bus import PacketRecorder


# ═══════════════════════════════════════════════
# Agent 身份 (环境变量)
# ═══════════════════════════════════════════════

AGENT_ID = os.environ.get("AGENT_ID", "agent-001")
AGENT_ROLE = os.environ.get("AGENT_ROLE", "generic")
AGENT_NAME = os.environ.get("AGENT_NAME", AGENT_ID)
AGENT_PORT = int(os.environ.get("PORT", "8000"))
MESSAGE_BUS = os.environ.get("MESSAGE_BUS_URL", "http://localhost:9000")
SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:8000")  # 统一日志收集

AGENT_CORE_GOAL = os.environ.get("AGENT_CORE_GOAL", "")
AGENT_HIDDEN_SECRET = os.environ.get("AGENT_HIDDEN_SECRET", "")
AGENT_ACTION_SPACE = json.loads(os.environ.get("AGENT_ACTION_SPACE", "[]"))
AGENT_INITIAL_ASSETS = json.loads(os.environ.get("AGENT_INITIAL_ASSETS", "{}"))
AGENT_SYSTEM_PROMPT = os.environ.get("AGENT_SYSTEM_PROMPT", "")  # from scene background_rules
AGENT_INTERACTION_PARADIGM = os.environ.get("AGENT_INTERACTION_PARADIGM", "")
AGENT_PARADIGM_HINT = os.environ.get("AGENT_PARADIGM_HINT", "")

LOG_COLLECTOR_URL = os.environ.get("LOG_COLLECTOR_URL", "")
PACKET_MONITOR_URL = os.environ.get("PACKET_MONITOR_URL", "")

# ═══════════════════════════════════════════════
# 初始化 Agent + 统一通信层
# ═══════════════════════════════════════════════

# 通信层: RemoteBus → Message Bus HTTP 中转
comm = RemoteBus(message_bus_url=MESSAGE_BUS, server_url=SERVER_URL)

# 创建 Agent 实例（和内存模式完全一致）
agent = Agent(
    agent_id=AGENT_ID,
    role=AGENT_ROLE,
    name=AGENT_NAME,
    skills=AGENT_ACTION_SPACE,
    tags=[AGENT_CORE_GOAL] if AGENT_CORE_GOAL else [],
)
agent.x = 0.0
agent.y = 0.0
agent.comm = comm
agent.pending_task_descs = [AGENT_CORE_GOAL] if AGENT_CORE_GOAL else []
agent.extra_meta = {
    "identity": AGENT_NAME,
    "core_goal": AGENT_CORE_GOAL,
    "hidden_secret": AGENT_HIDDEN_SECRET,
    "action_space": AGENT_ACTION_SPACE,
    "initial_assets": AGENT_INITIAL_ASSETS,
    "interaction_paradigm": AGENT_INTERACTION_PARADIGM,
    "paradigm_hint": AGENT_PARADIGM_HINT,
}

# 安装 Brain
brain_config = {}
if os.environ.get("LLM_API_KEY"):
    brain_config["api_key"] = os.environ["LLM_API_KEY"]
    brain_config["model"] = os.environ.get("LLM_MODEL", "")
    brain_config["provider"] = os.environ.get("LLM_PROVIDER", "auto")

_goals = []
if AGENT_CORE_GOAL:
    _goals.append(f"核心目标: {AGENT_CORE_GOAL}")
if AGENT_HIDDEN_SECRET:
    _goals.append(f"你的秘密: {AGENT_HIDDEN_SECRET}（可在关键时刻使用）")
if AGENT_ACTION_SPACE:
    _goals.append(f"可用行动: {', '.join(AGENT_ACTION_SPACE)}")
if AGENT_INITIAL_ASSETS:
    _goals.append(f"初始资产: {json.dumps(AGENT_INITIAL_ASSETS, ensure_ascii=False)}")
if AGENT_PARADIGM_HINT:
    _goals.append(f"行为模式: {AGENT_PARADIGM_HINT}")

# 构建完整系统提示词（注入范式提示）
_system_prompt = AGENT_SYSTEM_PROMPT
if AGENT_PARADIGM_HINT:
    _system_prompt = f"{_system_prompt}\n\n【行为模式指导】\n{AGENT_PARADIGM_HINT}"

agent.equip_brain(goals=_goals if _goals else None, config=brain_config,
                   system_prompt=_system_prompt)

# ═══════════════════════════════════════════════
# HTTP API
# ═══════════════════════════════════════════════

app = FastAPI(title=f"Agent {AGENT_NAME}")

turn = 0
last_action: Dict[str, Any] = {}

_agent_logger = get_logger()

def _log_agent(event: str, detail: str, **kw):
    """Agent 本地日志 + 上报到主服务器"""
    # 本地记录
    _agent_logger.system(event, detail, agent_id=AGENT_ID, details=kw or None)
    # 上报到主服务器
    try:
        requests.post(f"{SERVER_URL}/api/logs/agent", json={
            "agent_id": AGENT_ID,
            "agent_name": AGENT_NAME,
            "event": event,
            "detail": detail,
            "details": kw or {},
        }, timeout=2)
    except Exception:
        pass


class MessageIn(BaseModel):
    from_id: str
    from_name: str = ""
    content: str
    type: str = "message"


class DecideRequest(BaseModel):
    context: Dict[str, Any] = {}


@app.get("/status")
async def status():
    """Agent 状态"""
    return {
        "agent_id": AGENT_ID,
        "name": AGENT_NAME,
        "role": AGENT_ROLE,
        "turn": turn,
        "inbox_size": len(agent.inbox),
        "has_llm": bool(brain_config.get("api_key")),
        "core_goal": AGENT_CORE_GOAL or None,
        "hidden_secret": AGENT_HIDDEN_SECRET or None,
        "action_space": AGENT_ACTION_SPACE,
        "initial_assets": AGENT_INITIAL_ASSETS,
        "last_action": last_action,
    }


@app.post("/message")
async def receive_message(msg: MessageIn, request: Request):
    """接收来自其他 Agent 的消息 → 写入 Agent 收件箱"""
    client_ip = request.client.host if request.client else "unknown"
    agent.inbox.append({
        "from": msg.from_name or msg.from_id,
        "content": msg.content,
        "type": msg.type,
    })
    if len(agent.inbox) > 50:
        agent.inbox.pop(0)
    # 记录入站报文
    PacketRecorder.record_inbound(
        agent_id=AGENT_ID, src_ip=client_ip, method="POST", path="/message",
        content=msg.content, from_id=msg.from_id,
    )
    return {"received": True, "inbox_size": len(agent.inbox)}


# ── 全局事件队列 ──
_event_queue: List[Dict[str, Any]] = []


@app.post("/event")
async def receive_event(event: Dict[str, Any]):
    """接收来自调度中心的事件通知（如合规检查、媒体曝光等）"""
    event_name = event.get("event_name", "未知事件")
    impact = event.get("impact", "")
    turn = event.get("turn", 0)

    # 写入 Agent 收件箱（作为系统消息）
    agent.inbox.append({
        "from": "系统",
        "content": f"⚠️ 事件 [{event_name}]: {impact}",
        "type": "event",
    })

    # 同时写入事件队列（供决策时参考）
    _event_queue.append({
        "event_name": event_name,
        "impact": impact,
        "turn": turn,
    })

    _log_agent("event_received", f"事件: {event_name} — {impact}",
               event_name=event_name, impact=impact, turn=turn)
    return {"received": True, "event": event_name}


@app.get("/events")
async def list_events():
    """获取已触发的事件列表"""
    return {"agent_id": AGENT_ID, "events": _event_queue}


@app.post("/decide")
async def decide(req: DecideRequest = None):
    """触发 LLM 决策，返回 Action"""
    global turn, last_action
    turn += 1
    ctx = req.context if req else {}
    ctx["round"] = turn

    action = agent.decide(ctx)
    if action:
        last_action = action.to_dict() if hasattr(action, 'to_dict') else str(action)
        _log_agent("decide",
                   f"{action.type} → {getattr(action, 'target', '')}: {getattr(action, 'content', '')[:100]}",
                   action_type=action.type if hasattr(action, 'type') else "unknown",
                   target=getattr(action, 'target', ''),
                   content=getattr(action, 'content', '')[:300],
                   reasoning=getattr(action, 'reasoning', '')[:200],
                   round=turn)


    if action and hasattr(action, 'to_dict'):
        return {
            "agent_id": AGENT_ID, "agent_name": AGENT_NAME,
            "turn": turn, "has_llm": bool(brain_config.get("api_key")),
            **action.to_dict(),
        }
    return {
        "agent_id": AGENT_ID, "agent_name": AGENT_NAME,
        "turn": turn, "type": "wait", "target": "", "content": "",
        "reasoning": "no brain available",
    }


@app.post("/act")
async def act():
    """执行上一次 /decide 的决策（不再重复调用 LLM）"""
    global last_action
    result: Dict[str, Any] = {}

    if not last_action:
        return {"status": "no_decision_yet"}

    action_type = last_action.get("type", "wait")
    action_target = last_action.get("target", "")
    action_content = last_action.get("content", "")
    result["action"] = last_action
    _log_agent("act",
               f"{action_type} → {action_target}: {action_content[:100]}",
               action_type=action_type, target=action_target,
               content=action_content[:300])

    # 如果是发送消息，通过 RemoteBus 转发
    if action_type in ("send_message", "broadcast"):
        try:
            relay_start = time.time()
            if action_type == "send_message":
                ok = comm.send(AGENT_ID, AGENT_NAME, action_target, action_content)
            else:
                ok = comm.broadcast(AGENT_ID, AGENT_NAME, action_content)
            latency = (time.time() - relay_start) * 1000
            result["relayed"] = ok
            # 记录出站报文
            destination = action_target if action_type == "send_message" else "broadcast"
            PacketRecorder.record_outbound(
                agent_id=AGENT_ID, dst_ip=f"bus", dst_port=9000,
                method="POST", path="/relay", status=200 if ok else 0,
                latency_ms=latency, content=action_content,
                agent_to=destination,
            )

            # 转发到 Packet Monitor
            if PACKET_MONITOR_URL:
                try:
                    requests.post(f"{PACKET_MONITOR_URL}/api/packets/ingest", json={
                        "from_id": AGENT_ID, "from_name": AGENT_NAME,
                        "to": action_target if action_type == "send_message" else "broadcast",
                        "content": action_content, "type": action_type,
                        "direction": "outbound",
                    }, timeout=1)
                except Exception:
                    pass
        except Exception as e:
            result["relay_error"] = str(e)

    # 转发日志到 Log Collector
    if LOG_COLLECTOR_URL:
        try:
            requests.post(f"{LOG_COLLECTOR_URL}/api/logs/ingest", json={
                "level": "INFO", "event": "agent_act",
                "agent_id": AGENT_ID, "agent_name": AGENT_NAME,
                "index": "logs-agent",
                "message": f"Act: {str(action)[:200]}",
                "details": result,
            }, timeout=1)
        except Exception:
            pass

    return result


@app.get("/inbox")
async def get_inbox():
    """查看收件箱"""
    return {"inbox": agent.inbox[-20:]}


@app.post("/clear")
async def clear():
    """清空收件箱"""
    agent.inbox.clear()
    return {"cleared": True}


# ═══════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    # 向消息总线注册
    try:
        comm.register_agent(AGENT_ID, AGENT_NAME, f"http://localhost:{AGENT_PORT}")
        print(f"[Agent] Registered with message bus: {AGENT_ID} @ port {AGENT_PORT}")
    except Exception as e:
        print(f"[Agent] Failed to register with message bus: {e}")

    print(f"[Agent] {AGENT_NAME} ({AGENT_ROLE}) starting on port {AGENT_PORT}")
    print(f"[Agent] Goal: {AGENT_CORE_GOAL or 'N/A'}")
    print(f"[Agent] LLM: {'enabled' if brain_config.get('api_key') else 'disabled'}")
    uvicorn.run(app, host="0.0.0.0", port=AGENT_PORT, log_level="info")
