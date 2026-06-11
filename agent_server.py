#!/usr/bin/env python3
"""
Agent 容器运行时 — 统一 HTTP 服务 (Brain / OpenCLAW / Claude Code)

通过 AGENT_BACKEND 环境变量选择 LLM 后端:
  brain       → agent.decide() (Brain 抽象层, OpenAI-compatible API)
  openclaw    → Anthropic SDK tool-use API
  claude-code → subprocess claude -p CLI

环境变量:
  AGENT_ID / AGENT_ROLE / AGENT_NAME / PORT
  MESSAGE_BUS_URL / SERVER_URL
  AGENT_CORE_GOAL / AGENT_HIDDEN_SECRET / AGENT_ACTION_SPACE / AGENT_INITIAL_ASSETS
  AGENT_SYSTEM_PROMPT (scene background_rules)
  ANTHROPIC_API_KEY / LLM_API_KEY / LLM_MODEL
  AGENT_BACKEND (brain | openclaw | claude-code, default: brain)
"""

import os, sys, json, time, asyncio, re, subprocess
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import uvicorn, requests

from agent_network.comm import RemoteBus

# ═══════════════════════════════════════════════
# Agent 身份 (环境变量)
# ═══════════════════════════════════════════════

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
AGENT_INTERACTION_PARADIGM = os.environ.get("AGENT_INTERACTION_PARADIGM", "")
AGENT_PARADIGM_HINT = os.environ.get("AGENT_PARADIGM_HINT", "")

LOG_COLLECTOR_URL = os.environ.get("LOG_COLLECTOR_URL", "")
PACKET_MONITOR_URL = os.environ.get("PACKET_MONITOR_URL", "")

BACKEND = os.environ.get("AGENT_BACKEND", "brain")
API_KEY = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LLM_API_KEY", "")
MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")

# ═══════════════════════════════════════════════
# 通信层
# ═══════════════════════════════════════════════

comm = RemoteBus(message_bus_url=MESSAGE_BUS, server_url=SERVER_URL)

# ═══════════════════════════════════════════════
# Agent 实例 (brain 后端专用)
# ═══════════════════════════════════════════════

_agent = None          # brain Agent 对象
_brain_config = {}
_event_queue: List[Dict[str, Any]] = []
_channel_map: Dict[str, str] = {}
_current_talk: str = ""

if BACKEND == "brain":
    from agent_network.agent import Agent
    from agent_network.event_bus import PacketRecorder

    _agent = Agent(
        agent_id=AGENT_ID, role=AGENT_ROLE, name=AGENT_NAME,
        skills=AGENT_ACTION_SPACE,
        tags=[AGENT_CORE_GOAL] if AGENT_CORE_GOAL else [],
    )
    _agent.x = 0.0
    _agent.y = 0.0
    _agent.comm = comm
    _agent.pending_task_descs = [AGENT_CORE_GOAL] if AGENT_CORE_GOAL else []
    _agent.extra_meta = {
        "identity": AGENT_NAME, "core_goal": AGENT_CORE_GOAL,
        "hidden_secret": AGENT_HIDDEN_SECRET, "action_space": AGENT_ACTION_SPACE,
        "initial_assets": AGENT_INITIAL_ASSETS,
        "interaction_paradigm": AGENT_INTERACTION_PARADIGM,
        "paradigm_hint": AGENT_PARADIGM_HINT,
    }

    if os.environ.get("LLM_API_KEY"):
        _brain_config["api_key"] = os.environ["LLM_API_KEY"]
        _brain_config["model"] = os.environ.get("LLM_MODEL", "")
        _brain_config["provider"] = os.environ.get("LLM_PROVIDER", "auto")

    _goals = []
    if AGENT_CORE_GOAL: _goals.append(f"核心目标: {AGENT_CORE_GOAL}")
    if AGENT_HIDDEN_SECRET: _goals.append(f"你的秘密: {AGENT_HIDDEN_SECRET}（可在关键时刻使用）")
    if AGENT_ACTION_SPACE: _goals.append(f"可用行动: {', '.join(AGENT_ACTION_SPACE)}")
    if AGENT_INITIAL_ASSETS: _goals.append(f"初始资产: {json.dumps(AGENT_INITIAL_ASSETS, ensure_ascii=False)}")
    if AGENT_PARADIGM_HINT: _goals.append(f"行为模式: {AGENT_PARADIGM_HINT}")

    _sys_prompt = AGENT_SYSTEM_PROMPT
    if AGENT_PARADIGM_HINT:
        _sys_prompt = f"{_sys_prompt}\n\n【行为模式指导】\n{AGENT_PARADIGM_HINT}"

    _agent.equip_brain(goals=_goals if _goals else None, config=_brain_config, system_prompt=_sys_prompt)

elif BACKEND == "openclaw":
    # OpenCLAW tool definitions
    _TOOLS = [
        {
            "name": "send_message",
            "description": "向 Agent 发送消息。target 填 agent_id（如 ceo），填 0.0.0.0 表示向全体 Agent 广播。",
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
            "name": "analyze_situation",
            "description": "分析当前局势，评估各方立场和策略。",
            "input_schema": {
                "type": "object",
                "properties": {"focus": {"type": "string", "description": "分析重点"}},
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
                "properties": {"reason": {"type": "string", "description": "等待原因"}},
                "required": ["reason"],
            },
        },
        {
            "name": "execute_skill",
            "description": "执行一个可用技能。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string", "description": "技能名称"},
                    "params": {"type": "object", "description": "技能参数"},
                    "reasoning": {"type": "string", "description": "执行理由"},
                },
                "required": ["skill_name"],
            },
        },
    ]

# ═══════════════════════════════════════════════
# FastAPI
# ═══════════════════════════════════════════════

backend_label = {"brain": "Brain", "openclaw": "OpenCLAW", "claude-code": "Claude Code"}.get(BACKEND, BACKEND)
app = FastAPI(title=f"Agent {AGENT_NAME} ({backend_label})")

turn = 0
last_action: Dict[str, Any] = {}
inbox: list = []                  # openclaw / claude-code 用的独立收件箱
_current_effective_id = AGENT_ID
_current_effective_name = AGENT_NAME
_allowed_targets: set = set()

if BACKEND == "brain":
    _effective_id = AGENT_ID
    _effective_name = AGENT_NAME


# ═══════════════════════════════════════════════
# 日志上报
# ═══════════════════════════════════════════════

def _log_agent(event: str, detail: str, **kw):
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    effective_id = kw.pop("from_id", _current_effective_id)
    effective_name = kw.pop("from_name", _current_effective_name)
    try:
        requests.post(f"{SERVER_URL}/api/logs/agent", json={
            "agent_id": effective_id, "agent_name": effective_name,
            "event": event, "detail": detail, "timestamp": timestamp,
            "from_agent": effective_id,
            "to_agent": kw.get("target", kw.get("to", "")) if kw.get("action_type") in ("send_message", "broadcast") else "",
            "action": (kw.get("target") or kw.get("to")) if kw.get("action_type") == "execute_skill" else "send_message" if kw.get("action_type") == "broadcast" else kw.get("action_type", event),
            "action_status": kw.get("status", "success"),
            "details": {k: v for k, v in kw.items() if k not in ("action_type", "target")},
        }, timeout=2)
    except Exception:
        pass


# ═══════════════════════════════════════════════
# 后端决策 (openclaw / claude-code)
# ═══════════════════════════════════════════════

def _build_system_prompt(context: dict = None) -> str:
    ctx = context or {}
    identity = ctx.get("agent_name", _current_effective_name)
    role = ctx.get("agent_role", AGENT_ROLE)
    core_goal = ctx.get("core_goal") or AGENT_CORE_GOAL
    hidden_secret = ctx.get("hidden_secret") or AGENT_HIDDEN_SECRET
    skills_list = ctx.get("skills_list", [])
    background_rules = ctx.get("background_rules") or AGENT_SYSTEM_PROMPT

    system = background_rules or "你是一个仿真场景中的角色，根据你的身份和目标做出合理决策。"

    skills_text = ""
    if skills_list:
        skills_text = "\n## 可用技能\n使用 execute_skill 工具调用：\n" + "\n".join(
            f"  - execute_skill: {s.get('skill_name', s.get('name', '?'))} — {s.get('description', s.get('desc', ''))}"
            for s in skills_list
        ) + "\n"

    return f"""{system}

## 你的身份
- 名字: {identity}
- 角色: {role}
- 核心目标: {core_goal or '完成场景任务'}
- 隐藏秘密: {hidden_secret or '无'}
{skills_text}
行为准则：
- 必须立即采取具体行动，绝对不能wait！
- 有直接消息必须回复！优先 send_message
- send_message 的 target 必须用 agent_id（如 ceo、cto）
- 有技能时积极使用 execute_skill
- 用中文回复"""


def _build_user_message(inbox_msgs: list, context: dict = None) -> str:
    context = context or {}
    known = context.get("agents", context.get("known_agents", []))
    known_lines = ["  - agent_id=" + (a.get('id', a.get('agent_id', '?'))) + "  名称=" + (a.get('name', a.get('id', a.get('agent_id', '?')))) for a in known]
    known_list = "\n".join(known_lines) if known_lines else "  none"

    direct_msgs, broadcast_msgs, system_msgs = [], [], []
    for msg in inbox_msgs[-15:]:
        mtype = msg.get('type', 'direct')
        txt = f"  [{msg.get('from', '?')}]: {msg.get('content', '')}"
        if mtype == 'system': system_msgs.append(txt)
        elif mtype == 'broadcast': broadcast_msgs.append(txt)
        else: direct_msgs.append(txt)

    inbox_text = ""
    pending = len(direct_msgs)
    if direct_msgs: inbox_text += "## 📬 直接发给你的消息 — 必须回复！\n" + "\n".join(direct_msgs[-10:]) + "\n\n"
    if broadcast_msgs: inbox_text += "## 📢 广播消息\n" + "\n".join(broadcast_msgs[-5:]) + "\n\n"
    if system_msgs: inbox_text += "## ⚡ 系统通知\n" + "\n".join(system_msgs[-3:]) + "\n\n"
    if not inbox_text: inbox_text = "（收件箱为空 — 主动发起对话或分析局势）\n"
    pending_warning = f"\n⚠️ 你有 {pending} 条未回复的直接消息！本轮必须回复！" if pending > 0 else ""

    return f"""## 当前回合: {turn}

## 已知其它 Agent（发消息时 target 必须用 agent_id）
{known_list}

{inbox_text}{pending_warning}

请做出本轮决策。如有直接消息必须回复，有技能时积极使用 execute_skill。"""


def _call_openclaw(system_prompt: str, user_message: str) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=API_KEY)
    response = client.messages.create(
        model=MODEL, max_tokens=1024, system=system_prompt,
        tools=_TOOLS, messages=[{"role": "user", "content": user_message}],
    )
    tool_uses = [b for b in response.content if b.type == "tool_use"]
    if tool_uses:
        tb = tool_uses[0]
        ti = tb.input if isinstance(tb.input, dict) else json.loads(str(tb.input))
        if tb.name == "send_message":
            return {"action": "send_message", "target": ti.get("target", ""), "content": ti.get("content", ""), "reasoning": ti.get("reasoning", "")}
        elif tb.name == "execute_skill":
            return {"action": "execute_skill", "target": ti.get("skill_name", ""), "content": ti.get("params", {}), "reasoning": ti.get("reasoning", "")}
        elif tb.name == "wait":
            return {"action": "wait", "target": "", "content": ti.get("reason", "waiting"), "reasoning": ti.get("reason", "waiting")}
        else:
            return {"action": tb.name.replace("_", ""), "target": "", "content": str(ti), "reasoning": str(ti)}
    text_blocks = [b for b in response.content if b.type == "text"]
    text = text_blocks[0].text if text_blocks else ""
    return {"action": "wait", "target": "", "content": "", "reasoning": text}


def _build_claude_prompt(inbox_msgs: list, context: dict = None) -> str:
    ctx = context or {}
    identity = ctx.get("agent_name", _current_effective_name)
    role = ctx.get("agent_role", AGENT_ROLE)
    core_goal = ctx.get("core_goal") or AGENT_CORE_GOAL
    hidden_secret = ctx.get("hidden_secret") or AGENT_HIDDEN_SECRET
    skills_list = ctx.get("skills_list", [])
    background_rules = ctx.get("background_rules") or AGENT_SYSTEM_PROMPT
    system = background_rules or "你是一个仿真场景中的角色，根据你的身份和目标做出决策。"

    known = ctx.get("agents", ctx.get("known_agents", []))
    known_lines = ["  - agent_id=" + (a.get('id', a.get('agent_id', '?'))) + "  名称=" + (a.get('name', a.get('id', a.get('agent_id', '?')))) for a in known]
    known_list = "\n".join(known_lines) if known_lines else "  none"

    skills_text = ""
    if skills_list:
        skills_text = "\n## 可用技能（使用 execute_skill 动作调用）\n" + "\n".join(
            f"  - execute_skill: {s.get('skill_name', s.get('name', '?'))} — {s.get('description', s.get('desc', ''))}"
            for s in skills_list) + "\n"

    direct_msgs, broadcast_msgs, system_msgs = [], [], []
    for msg in inbox_msgs[-15:]:
        mtype = msg.get('type', 'direct')
        txt = f"  [{msg.get('from', '?')}]: {msg.get('content', '')}"
        if mtype == 'system': system_msgs.append(txt)
        elif mtype == 'broadcast': broadcast_msgs.append(txt)
        else: direct_msgs.append(txt)
    inbox_text = ""
    pending = len(direct_msgs)
    if direct_msgs: inbox_text += "## 📬 直接发给你的消息 — 必须回复！\n" + "\n".join(direct_msgs[-10:]) + "\n\n"
    if broadcast_msgs: inbox_text += "## 📢 广播消息\n" + "\n".join(broadcast_msgs[-5:]) + "\n\n"
    if system_msgs: inbox_text += "## ⚡ 系统通知\n" + "\n".join(system_msgs[-3:]) + "\n\n"
    if not inbox_text: inbox_text = "（收件箱为空 — 主动发起对话）\n"
    pending_warning = f"\n⚠️ 你有 {pending} 条未回复的直接消息！必须回复！" if pending > 0 else ""

    return f"""{system}

## 你的身份
- 名字: {identity}
- 角色: {role}
- 核心目标: {core_goal or '完成场景任务'}
- 隐藏秘密: {hidden_secret or '无'}
{skills_text}
## 已知其它 Agent（发消息时 target 必须用 agent_id）
{known_list}

## 当前回合: {turn}

{inbox_text}{pending_warning}

## 指令
基于以上信息，决定你这一轮要做什么。用 JSON 回复（只输出 JSON）：
```json
{{"reasoning": "推理", "action": "send_message|execute_skill|wait", "target": "目标agent_id或技能名", "content": "消息内容或技能参数"}}
```
重要规则:
- 必须立即采取具体行动！如果收件箱有直接发给你的消息，本轮必须回复
- target 必须用 agent_id（如 ceo、cto），不能用中文名
- 向全体 Agent 广播消息时，target 填 "0.0.0.0"
- 有技能时积极使用 execute_skill
- 用中文回复"""


def _call_claude_code(prompt: str) -> str:
    env = os.environ.copy()
    result = subprocess.run(
        ["claude", "-p", prompt, "--print", "--output-format", "text"],
        capture_output=True, text=True, timeout=120, cwd="/app", env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Claude Code failed (exit {result.returncode}): {result.stderr[:200]}")
    return result.stdout.strip()


def _parse_claude_response(text: str) -> dict:
    json_match = re.search(r'\{[\s\S]*"action"[\s\S]*\}', text)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass
    return {"reasoning": "parse error", "action": "wait", "target": "", "content": text}


# ═══════════════════════════════════════════════
# Pydantic 模型
# ═══════════════════════════════════════════════

class MessageIn(BaseModel):
    from_id: str
    from_name: str = ""
    content: str
    type: str = "message"


class DecideRequest(BaseModel):
    context: Dict[str, Any] = {}


# ═══════════════════════════════════════════════
# 端点
# ═══════════════════════════════════════════════

@app.get("/status")
async def status():
    inbox_size = len(_agent.inbox) if _agent else len(inbox)
    has_llm = bool(_brain_config.get("api_key") if _agent else API_KEY)
    return {
        "agent_id": AGENT_ID, "name": AGENT_NAME, "role": AGENT_ROLE,
        "backend": BACKEND, "turn": turn,
        "inbox_size": inbox_size, "has_llm": has_llm,
        "core_goal": AGENT_CORE_GOAL or None,
        "hidden_secret": AGENT_HIDDEN_SECRET or None,
        "action_space": AGENT_ACTION_SPACE,
        "initial_assets": AGENT_INITIAL_ASSETS,
        "last_action": last_action,
    }


@app.post("/message")
async def receive_message(msg: MessageIn, request: Request = None):
    if _agent:
        client_ip = request.client.host if request and request.client else "unknown"
        _agent._add_to_inbox(from_agent=msg.from_name or msg.from_id, content=msg.content, msg_type=msg.type or "direct")
        if BACKEND == "brain":
            PacketRecorder.record_inbound(agent_id=AGENT_ID, src_ip=client_ip, method="POST", path="/message", content=msg.content, from_id=msg.from_id)
    else:
        inbox.append({"from": msg.from_name or msg.from_id, "content": msg.content, "type": msg.type})
        if len(inbox) > 50:
            inbox.pop(0)
    return {"received": True, "inbox_size": len(_agent.inbox) if _agent else len(inbox)}


@app.post("/event")
async def receive_event(event: Dict[str, Any]):
    if not _agent:
        return {"received": False, "reason": "only brain backend supports events"}
    event_name = event.get("event_name", "未知事件")
    impact = event.get("impact", "")
    t = event.get("turn", 0)
    _agent._add_to_inbox(from_agent="系统", content=f"⚠️ 事件 [{event_name}]: {impact}", msg_type="system")
    _event_queue.append({"event_name": event_name, "impact": impact, "turn": t})
    _log_agent("event_received", f"事件: {event_name} — {impact}", event_name=event_name, impact=impact, turn=t)
    return {"received": True, "event": event_name}


@app.get("/events")
async def list_events():
    return {"agent_id": AGENT_ID, "events": _event_queue}


@app.post("/decide")
async def decide(req: DecideRequest = None):
    global turn, last_action, _current_effective_id, _current_effective_name
    turn += 1
    ctx = req.context if req else {}
    if "round" not in ctx:
        ctx["round"] = turn

    effective_id = ctx.get("agent_id", AGENT_ID)
    effective_name = ctx.get("agent_name", AGENT_NAME)
    _current_effective_id = effective_id
    _current_effective_name = effective_name

    global _allowed_targets
    _allowed_targets = set(ctx.get("comm_matrix", {}).get(effective_id, []))

    if BACKEND == "brain":
        global _channel_map, _current_talk, _effective_id, _effective_name
        _channel_map = ctx.get("channel_map", {})
        _current_talk = ctx.get("talk", "")
        _effective_id = effective_id
        _effective_name = effective_name

        if effective_id != AGENT_ID:
            injected_goals = []
            injected_prompt = AGENT_SYSTEM_PROMPT
            if ctx.get("core_goal"): injected_goals.append(f"核心目标: {ctx['core_goal']}")
            if ctx.get("hidden_secret"): injected_goals.append(f"隐藏秘密: {ctx['hidden_secret']}")
            if ctx.get("action_space"): injected_goals.append(f"可用行动: {', '.join(ctx['action_space'])}")
            if ctx.get("skills_list"):
                skills_text = "\n".join(f"  - {s['name']}: {s.get('desc','')}" for s in ctx["skills_list"])
                injected_goals.append(f"可用技能:\n{skills_text}")
            if ctx.get("background_rules"): injected_prompt = ctx["background_rules"]
            if injected_goals and effective_id != getattr(_agent, '_last_injected_id', None):
                _agent._last_injected_id = effective_id
                _agent.equip_brain(goals=injected_goals, config=_brain_config, system_prompt=injected_prompt)

        action = _agent.decide(ctx)
        if action:
            last_action = action.to_dict() if hasattr(action, 'to_dict') else str(action)
            act_type = action.type if hasattr(action, 'type') else "unknown"
            act_target = getattr(action, 'target', '')
            content_text = getattr(action, 'content', '') or getattr(action, 'reasoning', '')
            _log_agent("decide", content_text, from_id=effective_id, target=act_target,
                       action_type=act_type, content=getattr(action, 'content', ''),
                       reasoning=getattr(action, 'reasoning', ''), round=turn, status="decided")

        if action and hasattr(action, 'to_dict'):
            return {"agent_id": effective_id, "agent_name": effective_name, "turn": turn,
                    "has_llm": bool(_brain_config.get("api_key")), **action.to_dict()}
        return {"agent_id": effective_id, "agent_name": effective_name, "turn": turn,
                "type": "wait", "target": "", "content": "", "reasoning": "no brain available"}

    elif BACKEND == "openclaw":
        if not API_KEY:
            action = {"reasoning": "no API key configured", "action": "wait", "target": "", "content": ""}
        else:
            try:
                system = _build_system_prompt(ctx)
                user = _build_user_message(inbox, ctx)
                action = _call_openclaw(system, user)
                last_action = action
                _log_agent("decide", action.get('content', '') or action.get('reasoning', ''),
                           action_type=action.get('action'), target=action.get('target', ''),
                           content=action.get('content', ''), reasoning=action.get('reasoning', ''), status="decided")
            except Exception as e:
                action = {"reasoning": str(e), "action": "wait", "target": "", "content": ""}
        return {"agent_id": effective_id, "agent_name": effective_name, "turn": turn, "backend": "openclaw",
                "type": action.get("action", "wait"), "target": action.get("target", ""),
                "content": action.get("content", ""), "reasoning": action.get("reasoning", "")}

    elif BACKEND == "claude-code":
        try:
            prompt = _build_claude_prompt(inbox, ctx)
            response = _call_claude_code(prompt)
            action = _parse_claude_response(response)
            last_action = action
            _log_agent("decide", action.get('content', '') or action.get('reasoning', ''),
                       action_type=action.get('action'), target=action.get('target', ''),
                       content=action.get('content', ''), reasoning=action.get('reasoning', ''), status="decided")
        except Exception as e:
            action = {"reasoning": str(e), "action": "wait", "target": "", "content": ""}
        return {"agent_id": effective_id, "agent_name": effective_name, "turn": turn, "backend": "claude-code",
                "type": action.get("action", "wait"), "target": action.get("target", ""),
                "content": action.get("content", ""), "reasoning": action.get("reasoning", "")}


@app.post("/act")
async def act():
    global last_action
    if not last_action:
        return {"status": "no_decision_yet"}

    action_type = last_action.get("type", last_action.get("action", "wait"))
    action_target = last_action.get("target", "")
    action_content = last_action.get("content", "")
    result: Dict[str, Any] = {"action": last_action, "backend": BACKEND}

    # ── send_message / broadcast ──
    if action_type in ("send_message", "broadcast"):
        is_broadcast = (action_target == "0.0.0.0" or action_type == "broadcast")
        if not is_broadcast and action_type == "send_message" and _allowed_targets and action_target not in _allowed_targets:
            result["relayed"] = False
            _log_agent("act", f"无通信权限: {action_target}（允许: {', '.join(sorted(_allowed_targets))}）",
                       action_type=action_type, target=action_target, status="failed")
        else:
            try:
                relay_start = time.time()
                if BACKEND == "brain":
                    chan_id = _channel_map.get(f"{_effective_id}->{action_target}", "") or \
                              _channel_map.get(f"{_effective_id.lower()}->{action_target.lower()}", "")
                else:
                    chan_id = ""
                    talk = ""
                talk = _current_talk if BACKEND == "brain" else ""
                if is_broadcast:
                    ok = await asyncio.to_thread(comm.broadcast, _current_effective_id, _current_effective_name, action_content, _allowed_targets, chan_id, talk)
                else:
                    ok = await asyncio.to_thread(comm.send, _current_effective_id, _current_effective_name, action_target, action_content, chan_id, talk)
                latency = (time.time() - relay_start) * 1000
                result["relayed"] = ok
                _log_agent("act", action_content or action_type, action_type=action_type, target=action_target,
                           content=action_content, status="success" if ok else "failed")
                if BACKEND == "brain":
                    destination = "broadcast" if is_broadcast else action_target
                    PacketRecorder.record_outbound(agent_id=_current_effective_id, dst_ip="bus", dst_port=9000,
                                                   method="POST", path="/relay", status=200 if ok else 0,
                                                   latency_ms=latency, content=action_content, agent_to=destination)
                if PACKET_MONITOR_URL:
                    try:
                        requests.post(f"{PACKET_MONITOR_URL}/api/packets/ingest", json={
                            "from_id": _current_effective_id, "from_name": _current_effective_name,
                            "to": "broadcast" if is_broadcast else action_target,
                            "content": action_content, "type": action_type, "direction": "outbound",
                        }, timeout=1)
                    except Exception:
                        pass
            except Exception as e:
                result["relay_error"] = str(e)
                _log_agent("act", f"发送异常: {e}", action_type=action_type, target=action_target, status="failed")

    # ── execute_skill ──
    elif action_type == "execute_skill":
        skill_name = last_action.get("skill", action_target)
        skill_params = last_action.get("params", action_content if isinstance(action_content, dict) else {})
        if not isinstance(skill_params, dict):
            skill_params = {}
        try:
            r = requests.post(f"{SERVER_URL}/api/skills/execute", json={
                "skill_name": skill_name, "params": skill_params,
            }, timeout=10)
            result["skill_result"] = r.json() if r.ok else {"error": r.text[:500]}
            _log_agent("act", f"技能调用: {skill_name} | 参数: {json.dumps(skill_params, ensure_ascii=False)}",
                       action_type="execute_skill", target=skill_name, status="success" if r.ok else "failed")
        except Exception as e:
            result["skill_error"] = str(e)
            _log_agent("act", f"技能调用异常: {skill_name} | {e}",
                       action_type="execute_skill", target=skill_name, status="failed")

    # ── LOG_COLLECTOR ──
    if LOG_COLLECTOR_URL:
        try:
            requests.post(f"{LOG_COLLECTOR_URL}/api/logs/ingest", json={
                "level": "INFO", "event": "agent_act",
                "agent_id": _current_effective_id, "agent_name": _current_effective_name,
                "index": "logs-agent", "message": f"Act: {str(last_action)}",
                "details": result,
            }, timeout=1)
        except Exception:
            pass

    return result


@app.get("/inbox")
async def get_inbox():
    items = _agent.inbox if _agent else inbox
    return {"inbox": items[-20:]}


@app.post("/clear")
async def clear():
    if _agent:
        _agent.inbox.clear()
    else:
        inbox.clear()
    return {"cleared": True}


@app.post("/reset")
async def reset_state():
    global turn, last_action, _allowed_targets, _current_effective_id, _current_effective_name
    turn = 0
    last_action = {}
    _allowed_targets = set()
    _current_effective_id = AGENT_ID
    _current_effective_name = AGENT_NAME
    if _agent:
        global _channel_map, _current_talk, _event_queue
        _channel_map = {}
        _current_talk = ""
        _event_queue = []
        _agent.inbox.clear()
        if BACKEND == "brain":
            global _effective_id, _effective_name
            _effective_id = AGENT_ID
            _effective_name = AGENT_NAME
        if hasattr(_agent, '_last_injected_id'):
            del _agent._last_injected_id
        if hasattr(_agent, 'brain') and _agent.brain:
            _agent.brain.memory = []
            _agent.brain.turn = 0
    else:
        inbox.clear()
    return {"status": "reset", "brain_cleared": bool(_agent and hasattr(_agent, 'brain') and _agent.brain)}


# ═══════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    try:
        comm.register_agent(AGENT_ID, AGENT_NAME, f"http://localhost:{AGENT_PORT}")
        print(f"[Agent {backend_label}] Registered: {AGENT_ID} @ port {AGENT_PORT}")
    except Exception as e:
        print(f"[Agent {backend_label}] Register failed: {e}")

    print(f"[Agent {backend_label}] {AGENT_NAME} ({AGENT_ROLE}) starting on port {AGENT_PORT}")
    print(f"[Agent {backend_label}] Backend: {BACKEND} | Model: {MODEL} | Goal: {AGENT_CORE_GOAL or 'N/A'}")
    uvicorn.run(app, host="0.0.0.0", port=AGENT_PORT, log_level="info")
