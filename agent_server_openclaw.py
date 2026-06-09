#!/usr/bin/env python3
"""
Agent 容器运行时 — OpenCLAW 后端 (Anthropic Tool-Use API)

每个 Agent 运行在独立 Docker 容器中。
决策: Anthropic Messages API with tool definitions → Tool call → 解析 Action → 执行

环境变量:
  AGENT_ID / AGENT_ROLE / AGENT_NAME / PORT
  MESSAGE_BUS_URL / SERVER_URL
  AGENT_CORE_GOAL / AGENT_HIDDEN_SECRET / AGENT_ACTION_SPACE / AGENT_INITIAL_ASSETS
  AGENT_SYSTEM_PROMPT (from scene background_rules)
  ANTHROPIC_API_KEY / LLM_API_KEY / LLM_MODEL
"""

import os
import sys
import json
import time
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn
import requests

from agent_network.comm import RemoteBus

# ── Agent 身份 ──
AGENT_ID = os.environ.get("AGENT_ID", "agent-001")
AGENT_ROLE = os.environ.get("AGENT_ROLE", "generic")
AGENT_NAME = os.environ.get("AGENT_NAME", AGENT_ID)
AGENT_PORT = int(os.environ.get("PORT", "8000"))
MESSAGE_BUS = os.environ.get("MESSAGE_BUS_URL", "http://localhost:9000")
SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:8000")

AGENT_CORE_GOAL = os.environ.get("AGENT_CORE_GOAL", "")
AGENT_HIDDEN_SECRET = os.environ.get("AGENT_HIDDEN_SECRET", "")
AGENT_ACTION_SPACE = json.loads(os.environ.get("AGENT_ACTION_SPACE", "[]"))
AGENT_INITIAL_ASSETS = json.loads(os.environ.get("AGENT_INITIAL_ASSETS", "{}"))
AGENT_SYSTEM_PROMPT = os.environ.get("AGENT_SYSTEM_PROMPT", "")

LOG_COLLECTOR_URL = os.environ.get("LOG_COLLECTOR_URL", "")
PACKET_MONITOR_URL = os.environ.get("PACKET_MONITOR_URL", "")

# ── API Key ──
API_KEY = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LLM_API_KEY", "")
MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")

# ── 通信层 ──
comm = RemoteBus(message_bus_url=MESSAGE_BUS, server_url=SERVER_URL)

# ── FastAPI ──
app = FastAPI(title=f"Agent {AGENT_NAME} (OpenCLAW)")

turn = 0
last_action: Dict[str, Any] = {}
inbox: list = []
_current_effective_id = AGENT_ID
_current_effective_name = AGENT_NAME
_allowed_targets: set = set()  # 通信权限矩阵

# ── Tool definitions (match Agent action space) ──
_TOOLS = [
    {
        "name": "send_message",
        "description": "向另一个 Agent 发送消息。target 必须用 agent_id（如 role_a），不是中文名。",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "目标 Agent 的 agent_id"},
                "content": {"type": "string", "description": "消息内容"},
                "reasoning": {"type": "string", "description": "发送此消息的推理原因"},
            },
            "required": ["target", "content"],
        },
    },
    {
        "name": "broadcast",
        "description": "向所有 Agent 广播消息。",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "广播内容"},
                "reasoning": {"type": "string", "description": "广播的推理原因"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "analyze_situation",
        "description": "分析当前局势，评估各方立场和策略。",
        "input_schema": {
            "type": "object",
            "properties": {
                "focus": {"type": "string", "description": "分析重点（如某个 Agent、某个事件）"},
            },
            "required": ["focus"],
        },
    },
    {
        "name": "plan_strategy",
        "description": "制定下一步行动计划。",
        "input_schema": {
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "计划目标"},
                "steps": {"type": "string", "description": "具体步骤"},
            },
            "required": ["objective"],
        },
    },
    {
        "name": "wait",
        "description": "等待观望，收集更多信息后再行动。",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "等待原因"},
            },
            "required": ["reason"],
        },
    },
]


def _log_agent(event: str, detail: str, **kw):
    """结构化动作日志上报 — 使用注入的场景身份"""
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    effective_id = kw.pop("from_id", _current_effective_id)
    effective_name = kw.pop("from_name", _current_effective_name)
    try:
        requests.post(f"{SERVER_URL}/api/logs/agent", json={
            "agent_id": effective_id, "agent_name": effective_name,
            "event": event, "detail": detail,
            "timestamp": timestamp,
            "from_agent": effective_id,
            "to_agent": kw.get("target", kw.get("to", "")),
            "action": kw.get("action_type", event),
            "action_status": kw.get("status", "success"),
            "details": kw or {},
        }, timeout=2)
    except Exception:
        pass


def _build_system_prompt(context: dict = None) -> str:
    """构建系统提示词 — 支持场景身份注入"""
    ctx = context or {}
    identity = ctx.get("agent_name", _current_effective_name)
    role = ctx.get("agent_role", AGENT_ROLE)
    core_goal = ctx.get("core_goal") or AGENT_CORE_GOAL
    hidden_secret = ctx.get("hidden_secret") or AGENT_HIDDEN_SECRET
    action_space = ctx.get("action_space") or AGENT_ACTION_SPACE
    skills_list = ctx.get("skills_list", [])
    background_rules = ctx.get("background_rules") or AGENT_SYSTEM_PROMPT

    system = background_rules or "你是一个仿真场景中的角色，根据你的身份和目标做出合理决策。"
    actions_text = ", ".join(action_space) if action_space else "send_message, broadcast, analyze, plan, wait"

    skills_text = ""
    if skills_list:
        skills_text = "\n- 可用技能:\n" + "\n".join(
            f"    {s['name']}: {s.get('desc','')}" for s in skills_list
        )

    return f"""{system}

## 你的身份
- 名字: {identity}
- 角色: {role}
- 核心目标: {core_goal or '完成场景任务'}
- 隐藏秘密: {hidden_secret or '无'}
- 可用行动: {actions_text}{skills_text}

行为准则：
- 必须立即采取具体行动，绝对不能wait！
- 始终围绕核心目标和秘密行动
- 用 send_message 主动与相关 Agent 通信（target 必须用 agent_id）
- 合理使用 analyze_situation 和 plan_strategy
- 用中文回复"""


def _build_user_message(inbox_msgs: list, context: dict = None) -> str:
    context = context or {}
    known = context.get("agents", context.get("known_agents", []))
    known_list = "\n".join(
        f"  - {a.get('name', a.get('agent_id', '?'))} (agent_id={a.get('id', a.get('agent_id', '?'))})"
        for a in known) if known else "  none"

    inbox_text = "（空）"
    if inbox_msgs:
        inbox_text = "\n".join(
            f"  [{msg.get('from', '?')}]: {msg.get('content', '')}"
            for msg in inbox_msgs[-5:]
        )

    return f"""## 当前回合: {turn}

## 已知其它 Agent:
{known_list}

## 收件箱:
{inbox_text}

请做出本轮决策。必要时使用工具（如 send_message 与其他 Agent 通信）。"""


def _call_anthropic_with_tools(system_prompt: str, user_message: str) -> dict:
    """Call Anthropic API with tool definitions, handle tool use if needed."""
    import anthropic
    client = anthropic.Anthropic(api_key=API_KEY)

    messages = [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system_prompt,
        tools=_TOOLS,
        messages=messages,
    )

    # Check for tool use blocks
    tool_uses = [b for b in response.content if b.type == "tool_use"]
    if tool_uses:
        # Execute the first tool use
        tool_block = tool_uses[0]
        tool_name = tool_block.name
        tool_input = tool_block.input if isinstance(tool_block.input, dict) else json.loads(str(tool_block.input))

        if tool_name == "send_message":
            return {
                "action": "send_message",
                "target": tool_input.get("target", ""),
                "content": tool_input.get("content", ""),
                "reasoning": tool_input.get("reasoning", ""),
            }
        elif tool_name == "broadcast":
            return {
                "action": "broadcast",
                "target": "broadcast",
                "content": tool_input.get("content", ""),
                "reasoning": tool_input.get("reasoning", ""),
            }
        elif tool_name == "analyze_situation":
            return {
                "action": "analyze",
                "target": "",
                "content": tool_input.get("focus", ""),
                "reasoning": tool_input.get("focus", ""),
            }
        elif tool_name == "plan_strategy":
            return {
                "action": "plan",
                "target": "",
                "content": f"{tool_input.get('objective', '')}: {tool_input.get('steps', '')}",
                "reasoning": tool_input.get("objective", ""),
            }
        elif tool_name == "wait":
            return {
                "action": "wait",
                "target": "",
                "content": tool_input.get("reason", "waiting"),
                "reasoning": tool_input.get("reason", "waiting"),
            }

    # Fallback: extract text
    text_blocks = [b for b in response.content if b.type == "text"]
    text = text_blocks[0].text if text_blocks else ""
    return {"action": "wait", "target": "", "content": "", "reasoning": text[:200]}


# ── HTTP API ──

class MessageIn(BaseModel):
    from_id: str
    from_name: str = ""
    content: str
    type: str = "message"


class DecideRequest(BaseModel):
    context: Dict[str, Any] = {}


@app.get("/status")
async def status():
    return {
        "agent_id": AGENT_ID, "name": AGENT_NAME, "role": AGENT_ROLE,
        "backend": "openclaw", "turn": turn,
        "inbox_size": len(inbox), "core_goal": AGENT_CORE_GOAL or None,
        "hidden_secret": AGENT_HIDDEN_SECRET or None,
        "action_space": AGENT_ACTION_SPACE, "initial_assets": AGENT_INITIAL_ASSETS,
        "last_action": last_action,
    }


@app.post("/message")
async def receive_message(msg: MessageIn):
    inbox.append({"from": msg.from_name or msg.from_id, "content": msg.content, "type": msg.type})
    if len(inbox) > 50:
        inbox.pop(0)
    return {"received": True, "inbox_size": len(inbox)}


@app.post("/decide")
async def decide(req: DecideRequest = None):
    """触发 LLM 决策 — 支持场景身份注入"""
    global turn, last_action, _current_effective_id, _current_effective_name
    turn += 1
    ctx = req.context if req else {}
    ctx["round"] = turn

    # 使用注入的身份（场景 Agent），覆盖容器自身的身份
    _current_effective_id = ctx.get("agent_id", AGENT_ID)
    _current_effective_name = ctx.get("agent_name", AGENT_NAME)

    # 存储通信权限矩阵
    global _allowed_targets
    _allowed_targets = set(ctx.get("comm_matrix", {}).get(_current_effective_id, []))

    if not API_KEY:
        return {
            "agent_id": _current_effective_id, "agent_name": _current_effective_name,
            "turn": turn, "backend": "openclaw",
            "type": "wait", "target": "", "content": "",
            "reasoning": "no API key configured",
        }

    try:
        system = _build_system_prompt(ctx)
        user = _build_user_message(inbox, ctx)
        action = _call_anthropic_with_tools(system, user)
        last_action = action
        act_content = action.get('content', '')
        act_reasoning = action.get('reasoning', '')
        _log_agent("decide", (act_content or act_reasoning)[:100],
                   action_type=action.get('action'), target=action.get('target', ''),
                   content=act_content[:200], reasoning=act_reasoning[:200], status="decided")
    except Exception as e:
        action = {"reasoning": str(e), "action": "wait", "target": "", "content": ""}
        last_action = action

    return {
        "agent_id": _current_effective_id, "agent_name": _current_effective_name,
        "turn": turn, "backend": "openclaw",
        "type": action.get("action", "wait"),
        "target": action.get("target", ""),
        "content": action.get("content", ""),
        "reasoning": action.get("reasoning", ""),
    }


@app.post("/act")
async def act():
    global last_action
    if not last_action:
        return {"status": "no_decision_yet"}

    action_type = last_action.get("action", "wait")
    action_target = last_action.get("target", "")
    action_content = last_action.get("content", "")
    result: Dict[str, Any] = {"action": last_action, "backend": "openclaw"}

    if action_type in ("send_message", "broadcast"):
        # 检查通信权限
        if action_type == "send_message" and _allowed_targets and action_target not in _allowed_targets:
            result["relayed"] = False
            _log_agent("act", f"无通信权限: {action_target}（允许: {', '.join(sorted(_allowed_targets))}）",
                       action_type=action_type, target=action_target, status="failed")
        else:
            try:
                if action_type == "send_message":
                    ok = await asyncio.to_thread(comm.send, _current_effective_id, _current_effective_name, action_target, action_content)
                else:
                    ok = await asyncio.to_thread(comm.broadcast, _current_effective_id, _current_effective_name, action_content, _allowed_targets)
                result["relayed"] = ok
                _log_agent("act", action_content[:100] or action_type,
                           action_type=action_type, target=action_target,
                           content=action_content[:300], status="success" if ok else "failed")
            except Exception as e:
                result["relay_error"] = str(e)
                _log_agent("act", f"发送异常: {e}",
                           action_type=action_type, target=action_target, status="failed")

    if LOG_COLLECTOR_URL:
        try:
            requests.post(f"{LOG_COLLECTOR_URL}/api/logs/ingest", json={
                "level": "INFO", "event": "agent_act",
                "agent_id": _current_effective_id, "agent_name": _current_effective_name,
                "index": "logs-agent", "message": f"Act: {str(last_action)[:200]}",
                "details": result,
            }, timeout=1)
        except Exception:
            pass

    return result


@app.get("/inbox")
async def get_inbox():
    return {"inbox": inbox[-20:]}


@app.post("/clear")
async def clear():
    inbox.clear()
    return {"cleared": True}


if __name__ == "__main__":
    try:
        comm.register_agent(AGENT_ID, AGENT_NAME, f"http://localhost:{AGENT_PORT}")
        print(f"[OpenCLAW Agent] Registered: {AGENT_ID} @ port {AGENT_PORT}")
    except Exception as e:
        print(f"[OpenCLAW Agent] Register failed: {e}")

    print(f"[OpenCLAW Agent] {AGENT_NAME} ({AGENT_ROLE}) starting on port {AGENT_PORT}")
    print(f"[OpenCLAW Agent] Model: {MODEL} | Goal: {AGENT_CORE_GOAL or 'N/A'}")
    uvicorn.run(app, host="0.0.0.0", port=AGENT_PORT, log_level="info")
