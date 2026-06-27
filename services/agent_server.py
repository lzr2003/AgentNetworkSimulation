#!/usr/bin/env python3
"""
Agent 容器运行时 — 统一 HTTP 服务 (OpenCLAW / Claude Code / Direct LLM)

AgentNetwork 只负责容器化运行入口、消息收件箱和控制面上下文注入。
单 Agent 内部 ReAct、记忆和 Tool 选择交给 Claude Code / OpenCLAW。
Direct LLM 是显式降级后端，不能伪装成 OpenCLAW。
"""

import os
import sys
import json
import asyncio
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request
from pydantic import BaseModel
from typing import List, Dict, Any
import uvicorn
import requests

from agent_network.logger import get_logger
from agent_network.comm import RemoteBus
from agent_network.packet_capture import start_capture, stop_capture
from agent_network.adapters.base import AgentContext
from agent_network.adapters.claude_code import ClaudeCodeAdapter
from agent_network.adapters.direct_llm import DirectLLMAdapter
from agent_network.adapters.openclaw import OpenCLAWAdapter

AGENT_ID = os.environ.get("AGENT_ID", "agent-001")
AGENT_ROLE = os.environ.get("AGENT_ROLE", "generic")
AGENT_NAME = os.environ.get("AGENT_NAME", AGENT_ID)
AGENT_PORT = int(os.environ.get("PORT", "8000"))
MESSAGE_BUS = os.environ.get("MESSAGE_BUS_URL", "http://localhost:9000")
SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:8000")

AGENT_CORE_GOAL = os.environ.get("AGENT_CORE_GOAL", "")
AGENT_ACTION_SPACE = json.loads(os.environ.get("AGENT_ACTION_SPACE", "[]"))
AGENT_INITIAL_ASSETS = json.loads(os.environ.get("AGENT_INITIAL_ASSETS", "{}"))
AGENT_SYSTEM_PROMPT = os.environ.get("AGENT_SYSTEM_PROMPT", "")
AGENT_INTERACTION_PARADIGM = os.environ.get("AGENT_INTERACTION_PARADIGM", "")
AGENT_PARADIGM_HINT = os.environ.get("AGENT_PARADIGM_HINT", "")

BACKEND = os.environ.get("AGENT_BACKEND", "openclaw")
if BACKEND == "claudecode":
    BACKEND = "claude-code"
if BACKEND in {"direct-llm", "directllm"}:
    BACKEND = "direct_llm"

SUPPORTED_BACKENDS = {"openclaw", "claude-code", "direct_llm"}
if BACKEND not in SUPPORTED_BACKENDS:
    raise RuntimeError(
        f"Unsupported AGENT_BACKEND={BACKEND!r}. "
        "Use 'openclaw', 'claude-code', or explicit 'direct_llm'."
    )

API_KEY = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LLM_API_KEY", "")
MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")

comm = RemoteBus(message_bus_url=MESSAGE_BUS, server_url=SERVER_URL)
logger = get_logger()
backend_label = {
    "openclaw": "OpenCLAW",
    "claude-code": "Claude Code",
    "direct_llm": "Direct LLM",
}.get(BACKEND, BACKEND)
app = FastAPI(title=f"Agent {AGENT_NAME} ({backend_label})")

from agent_network.traffic_log import TrafficMiddleware, traffic_enabled
if traffic_enabled():
    app.add_middleware(TrafficMiddleware, component=AGENT_ID, server_url=f"{SERVER_URL}")

turn = 0
inbox: list = []
_event_queue: list = []


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _safe_post_json(url: str, json_data: dict, timeout: float = 3) -> bool:
    try:
        requests.post(url, json=json_data, timeout=timeout)
        return True
    except Exception:
        return False


def _append_inbox(from_agent: str, content: str, msg_type: str = "direct"):
    inbox.append({"from": from_agent, "content": content, "type": msg_type})
    if len(inbox) > 50:
        inbox.pop(0)


def _clear_inbox():
    inbox.clear()


def _inbox_size() -> int:
    return len(inbox)


def _log_agent(event: str, detail: str, **kw):
    action_type = kw.get("action_type", event)
    target = kw.get("target", kw.get("to", ""))
    _safe_post_json(f"{SERVER_URL}/api/logs/agent", {
        "agent_id": AGENT_ID,
        "agent_name": AGENT_NAME,
        "event": event,
        "detail": detail,
        "timestamp": _now_iso(),
        "from_agent": AGENT_ID,
        "to_agent": target if action_type in ("send_message", "broadcast") else "",
        "action": action_type,
        "action_status": kw.get("status", "success"),
        "details": {k: v for k, v in kw.items() if k not in ("action_type", "target")},
    }, timeout=2)


def _skill_names_from_legacy(skills: List[Dict[str, Any]]) -> List[str]:
    names = []
    for item in skills or []:
        if isinstance(item, dict):
            names.append(item.get("name") or item.get("skill_name") or "")
        elif isinstance(item, str):
            names.append(item)
    return list(dict.fromkeys([name for name in names if name]))


class MessageIn(BaseModel):
    from_id: str
    from_name: str = ""
    content: str
    type: str = "message"


class RunRequest(BaseModel):
    trace_id: str = ""
    agent_id: str = ""
    agent_name: str = ""
    role: str = ""
    core_goal: str = ""
    task: str = ""
    messages: List[Dict[str, Any]] = []
    skills: List[Dict[str, Any]] = []
    allowed_skills: List[str] = []
    allowed_tools: List[str] = []
    permissions: Dict[str, Any] = {}
    state_snapshot: Dict[str, Any] = {}
    tick: int = 0
    timeout_seconds: int = 60
    max_turns: int = 10
    scene_key: str = "default"


def _make_adapter():
    if BACKEND == "claude-code":
        return ClaudeCodeAdapter()
    if BACKEND == "direct_llm":
        return DirectLLMAdapter()
    return OpenCLAWAdapter()


@app.post("/run")
async def run_agent(req: RunRequest):
    """Run one backend-native Agent task.

    The full ReAct loop is delegated to Claude Code / OpenCLAW. This endpoint
    only converts AgentNetwork context into the BackendAdapter contract.
    """
    allowed_skills = req.allowed_skills or _skill_names_from_legacy(req.skills)
    context = AgentContext(
        trace_id=req.trace_id,
        agent_id=req.agent_id or AGENT_ID,
        agent_name=req.agent_name or AGENT_NAME,
        role=req.role or AGENT_ROLE,
        core_goal=req.core_goal or AGENT_CORE_GOAL,
        task=req.task,
        messages=req.messages or inbox,
        skills=req.skills or [],
        allowed_tools=req.allowed_tools,
        permissions=req.permissions,
        state_snapshot=req.state_snapshot,
        tick=req.tick,
        timeout_seconds=req.timeout_seconds,
        max_turns=req.max_turns,
        scene_key=req.scene_key or os.environ.get("AGENT_SCENE_KEY", "default"),
        allowed_skills=allowed_skills,
    )

    adapter = _make_adapter()
    result = await asyncio.to_thread(adapter.run_agent_task, context)

    for event in getattr(result, "application_events", []) or []:
        logger.emit_application_event(
            event=event.get("event", "agent_event"),
            actor=event.get("actor", {"agent_id": context.agent_id}),
            target=event.get("target", {}),
            task=event.get("task", {"goal": context.task}),
            conversation=event.get("conversation", {}),
            action=event.get("action", {}),
            content=event.get("content", {}),
            decision=event.get("decision", {}),
            skill=event.get("skill", {}),
            tool=event.get("tool", {}),
            state_change=event.get("state_change", {}),
            policy=event.get("policy", {}),
            result=event.get("result", {}),
            metrics=event.get("metrics", {}),
            links=event.get("links", {}),
            trace_id=event.get("trace_id", context.trace_id),
            tick=context.tick,
            component=context.agent_id,
            source="agent",
            debug={"schema_version": "application.v1", "emitter": "agent_server.run_agent"},
        )

    return result.__dict__


@app.get("/status")
async def status():
    return {
        "agent_id": AGENT_ID,
        "name": AGENT_NAME,
        "role": AGENT_ROLE,
        "backend": BACKEND,
        "turn": turn,
        "inbox_size": _inbox_size(),
        "has_llm": bool(API_KEY),
        "core_goal": AGENT_CORE_GOAL or None,
        "action_space": AGENT_ACTION_SPACE,
        "initial_assets": AGENT_INITIAL_ASSETS,
    }


@app.post("/message")
async def receive_message(msg: MessageIn, request: Request = None):
    _append_inbox(msg.from_id, msg.content, msg.type or "direct")
    return {"received": True, "inbox_size": _inbox_size()}


@app.post("/event")
async def receive_event(event: Dict[str, Any]):
    event_name = event.get("event_name", "未知事件")
    impact = event.get("impact", "")
    t = event.get("turn", 0)
    _append_inbox("系统", f"⚠️ 事件 [{event_name}]: {impact}", "system")
    _event_queue.append({"event_name": event_name, "impact": impact, "turn": t})
    _log_agent("event_received", f"事件: {event_name} — {impact}", event_name=event_name, impact=impact, turn=t)
    return {"received": True, "event": event_name}


@app.get("/events")
async def list_events():
    return {"agent_id": AGENT_ID, "events": _event_queue}


@app.get("/inbox")
async def get_inbox():
    return {"inbox": inbox[-20:]}


@app.post("/clear")
async def clear():
    _clear_inbox()
    return {"cleared": True}


@app.post("/capture/start")
async def capture_start(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    capture_agent_id = body.get("agent_id") or AGENT_ID
    capture_agent_name = body.get("agent_name") or AGENT_NAME
    return start_capture(agent_id=capture_agent_id, agent_name=capture_agent_name, server_url=SERVER_URL)


@app.post("/capture/stop")
async def capture_stop():
    return stop_capture()


@app.post("/reset")
async def reset_state():
    global turn, _event_queue
    stop_capture()
    turn = 0
    _event_queue = []
    inbox.clear()
    return {"status": "reset", "brain_cleared": False}


if __name__ == "__main__":
    try:
        comm.register_agent(AGENT_ID, AGENT_NAME, f"http://localhost:{AGENT_PORT}")
        print(f"[Agent {backend_label}] Registered: {AGENT_ID} @ port {AGENT_PORT}")
    except Exception as e:
        print(f"[Agent {backend_label}] Register failed: {e}")

    print(f"[Agent {backend_label}] {AGENT_NAME} ({AGENT_ROLE}) starting on port {AGENT_PORT}")
    print(f"[Agent {backend_label}] Backend: {BACKEND} | Model: {MODEL} | Goal: {AGENT_CORE_GOAL or 'N/A'}")

    try:
        uvicorn.run(app, host="0.0.0.0", port=AGENT_PORT, log_level="info")
    finally:
        stop_capture()
