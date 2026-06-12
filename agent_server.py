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
  AGENT_INTERACTION_PARADIGM / AGENT_PARADIGM_HINT
  ANTHROPIC_API_KEY / LLM_API_KEY / LLM_MODEL
  AGENT_BACKEND (brain | openclaw | claude-code, default: brain)
  PACKET_MONITOR_URL
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

_agent = None
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

# ── 流量监管 middleware（LOG_TRAFFIC=1 时启用）──
from agent_network.traffic_log import TrafficMiddleware, traffic_enabled
if traffic_enabled():
    app.add_middleware(TrafficMiddleware, component=AGENT_ID, server_url=f"{SERVER_URL}")

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
# 基础 helper
# ═══════════════════════════════════════════════

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _safe_post_json(url: str, json_data: dict, timeout: float = 3) -> bool:
    """安全 POST，失败静默返回 False"""
    try:
        requests.post(url, json=json_data, timeout=timeout)
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════
# 收件箱 helper（统一 brain / 非 brain）
# ═══════════════════════════════════════════════

def _get_inbox() -> list:
    return _agent.inbox if _agent else inbox


def _append_inbox(from_agent: str, content: str, msg_type: str = "direct"):
    if _agent:
        _agent._add_to_inbox(from_agent=from_agent, content=content, msg_type=msg_type)
    else:
        inbox.append({"from": from_agent, "content": content, "type": msg_type})
        if len(inbox) > 50:
            inbox.pop(0)


def _clear_inbox():
    if _agent:
        _agent.inbox.clear()
    else:
        inbox.clear()


def _inbox_size() -> int:
    return len(_agent.inbox) if _agent else len(inbox)


def _recent_direct_sender() -> Optional[str]:
    """从收件箱倒序查找最近一条 direct 消息的发件人"""
    src = _get_inbox()
    for msg in reversed(src):
        if msg.get("type") in ("direct",) and msg.get("from"):
            sender = msg["from"]
            if sender not in ("系统", "self", _current_effective_name, _current_effective_id):
                return sender
    return None


# ═══════════════════════════════════════════════
# Action 归一化
# ═══════════════════════════════════════════════

def _normalize_action(raw) -> dict:
    """归一化 brain Action 对象 / openclaw dict / claude dict → 统一 dict，保留所有扩展字段"""
    if hasattr(raw, 'to_dict'):
        d = raw.to_dict()
        result = {
            "type": d.get("type", "wait"),
            "target": d.get("target", ""),
            "content": d.get("content", ""),
            "reasoning": d.get("reasoning", ""),
        }
        # brain Action 对象的扩展字段（skill, params, target_x, target_y 等）
        for k in ("skill", "params", "target_x", "target_y", "focus", "objective", "steps"):
            if k in d and k not in result:
                result[k] = d[k]
        return result
    if isinstance(raw, dict):
        result = {
            "type": raw.get("action", raw.get("type", "wait")),
            "target": raw.get("target", ""),
            "content": raw.get("content", ""),
            "reasoning": raw.get("reasoning", ""),
        }
        # 保留 openclaw/claude 返回的扩展字段（execute_skill 的 skill_name→skill, params）
        for k in ("skill", "params", "skill_name", "focus", "objective", "steps"):
            val = raw.get(k, "")
            if val and k not in result:
                result["skill" if k == "skill_name" else k] = val
        return result
    return {"type": "wait", "target": "", "content": "", "reasoning": str(raw)}


def _decision_response(action: dict, effective_id: str, effective_name: str, extra: dict = None) -> dict:
    """构造 /decide 统一返回结构"""
    base = {
        "agent_id": effective_id, "agent_name": effective_name, "turn": turn,
        "type": action.get("type", "wait"),
        "target": action.get("target", ""),
        "content": action.get("content", ""),
        "reasoning": action.get("reasoning", ""),
    }
    if extra:
        base.update(extra)
    return base


# ═══════════════════════════════════════════════
# Prompt 构建
# ═══════════════════════════════════════════════

def _format_known_agents(known: list) -> str:
    lines = ["  - agent_id=" + (a.get('id', a.get('agent_id', '?')))
             + "  名称=" + (a.get('name', a.get('id', a.get('agent_id', '?'))))
             for a in known]
    return "\n".join(lines) if lines else "  none"


def _format_skills(skills_list: list) -> str:
    if not skills_list:
        return ""
    return "\n## 可用技能（使用 execute_skill 动作调用）\n" + "\n".join(
        f"  - execute_skill: {s.get('skill_name', s.get('name', '?'))} — {s.get('description', s.get('desc', ''))}"
        for s in skills_list
    ) + "\n"


def _partition_inbox_messages(inbox_msgs: list):
    """将收件箱消息按类型分成 direct/broadcast/system 三组"""
    direct, broadcast, system = [], [], []
    for msg in inbox_msgs[-15:]:
        mtype = msg.get('type', 'direct')
        txt = f"  [{msg.get('from', '?')}]: {msg.get('content', '')}"
        if mtype == 'system':
            system.append(txt)
        elif mtype == 'broadcast':
            broadcast.append(txt)
        else:
            direct.append(txt)
    return direct, broadcast, system


def _format_inbox_sections(direct: list, broadcast: list, system: list) -> str:
    """将分好组的收件箱消息格式化为 prompt 文本"""
    parts = []
    pending = len(direct)
    if direct:
        parts.append("## 📬 直接发给你的消息\n" + "\n".join(direct[-10:]) + "\n")
    if broadcast:
        parts.append("## 📢 广播消息\n" + "\n".join(broadcast[-5:]) + "\n")
    if system:
        parts.append("## ⚡ 系统通知\n" + "\n".join(system[-3:]) + "\n")
    if not parts:
        parts.append("（收件箱为空 — 主动发起对话或分析局势）\n")
    if pending > 0:
        parts.append(f"\n⚠️ 你有 {pending} 条未回复的直接消息。执行 skill 后会自动回复。")
    return "\n".join(parts)


def _build_identity_block(ctx: dict, skills_list: list = None) -> str:
    """构建身份信息块，brain/openclaw/claude 共用"""
    identity = ctx.get("agent_name", _current_effective_name)
    role = ctx.get("agent_role", AGENT_ROLE)
    core_goal = ctx.get("core_goal") or AGENT_CORE_GOAL
    hidden_secret = ctx.get("hidden_secret") or AGENT_HIDDEN_SECRET
    skills_text = _format_skills(skills_list or ctx.get("skills_list", []))
    return f"""## 你的身份
- 名字: {identity}
- 角色: {role}
- 核心目标: {core_goal or '完成场景任务'}
- 隐藏秘密: {hidden_secret or '无'}
{skills_text}"""


def _build_user_message(inbox_msgs: list, ctx: dict = None) -> str:
    """OpenCLAW 用户消息"""
    ctx = ctx or {}
    known = ctx.get("agents", ctx.get("known_agents", []))
    direct, broadcast, system = _partition_inbox_messages(inbox_msgs)
    inbox_text = _format_inbox_sections(direct, broadcast, system)
    return f"""## 当前回合: {turn}

## 已知其它 Agent（发消息时 target 必须用 agent_id）
{_format_known_agents(known)}

{inbox_text}

请做出本轮决策。收到任务指令直接用 execute_skill 执行，结果会自动回复发件人。"""


def _build_claude_prompt(inbox_msgs: list, ctx: dict = None) -> str:
    """Claude Code prompt"""
    ctx = ctx or {}
    identity = ctx.get("agent_name", _current_effective_name)
    role = ctx.get("agent_role", AGENT_ROLE)
    core_goal = ctx.get("core_goal") or AGENT_CORE_GOAL
    hidden_secret = ctx.get("hidden_secret") or AGENT_HIDDEN_SECRET
    skills_list = ctx.get("skills_list", [])
    background_rules = ctx.get("background_rules") or AGENT_SYSTEM_PROMPT
    known = ctx.get("agents", ctx.get("known_agents", []))
    system = background_rules or "你是一个仿真场景中的角色，根据你的身份和目标做出决策。"

    direct, broadcast, system_msgs = _partition_inbox_messages(inbox_msgs)
    inbox_text = _format_inbox_sections(direct, broadcast, system_msgs)

    return f"""{system}

{_build_identity_block(ctx, skills_list)}
## 已知其它 Agent（发消息时 target 必须用 agent_id）
{_format_known_agents(known)}

## 当前回合: {turn}

{inbox_text}

## 指令
基于以上信息，决定你这一轮要做什么。用 JSON 回复（只输出 JSON）：
```json
{{"reasoning": "推理", "action": "send_message|execute_skill|wait", "target": "目标agent_id或技能名", "content": "消息内容或技能参数"}}
```
重要规则:
- 必须立即采取具体行动！有直接消息时推荐 execute_skill，结果会自动回复发件人
- target 必须用 agent_id（如 ceo、cto），不能用中文名
- 向全体 Agent 广播消息时，target 填 "0.0.0.0"
- 有技能时积极使用 execute_skill
- 用中文回复"""


# ═══════════════════════════════════════════════
# 后端调用
# ═══════════════════════════════════════════════

def _build_system_prompt(ctx: dict = None) -> str:
    """OpenCLAW system prompt"""
    ctx = ctx or {}
    background_rules = ctx.get("background_rules") or AGENT_SYSTEM_PROMPT
    system = background_rules or "你是一个仿真场景中的角色，根据你的身份和目标做出合理决策。"
    identity_block = _build_identity_block(ctx, ctx.get("skills_list", []))
    return f"""{system}

{identity_block}
行为准则：
- 必须立即采取具体行动，绝对不能wait！
- 有直接消息时直接用 execute_skill 执行任务，结果会自动回复发件人
- send_message 的 target 必须用 agent_id（如 ceo、cto）
- 有技能时积极使用 execute_skill
- 用中文回复"""


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
# 日志
# ═══════════════════════════════════════════════

def _log_agent(event: str, detail: str, **kw):
    """向 SERVER_URL/api/logs/agent 上报，server 转换为统一 global 日志"""
    effective_id = kw.pop("from_id", _current_effective_id)
    effective_name = kw.pop("from_name", _current_effective_name)
    action_type = kw.get("action_type", event)
    target = kw.get("target", kw.get("to", ""))
    _safe_post_json(f"{SERVER_URL}/api/logs/agent", {
        "agent_id": effective_id, "agent_name": effective_name,
        "event": event, "detail": detail, "timestamp": _now_iso(),
        "from_agent": effective_id,
        "to_agent": target if action_type in ("send_message", "broadcast") else "",
        "action": target if action_type == "execute_skill"
                  else "send_message" if action_type == "broadcast"
                  else action_type,
        "action_status": kw.get("status", "success"),
        "details": {k: v for k, v in kw.items() if k not in ("action_type", "target")},
    }, timeout=2)


def _notify_packet_monitor(from_id: str, from_name: str, to: str, content: str,
                           action_type: str):
    """通知外部报文监控器（仅在 PACKET_MONITOR_URL 配置时生效）"""
    if PACKET_MONITOR_URL:
        _safe_post_json(f"{PACKET_MONITOR_URL}/api/packets/ingest", {
            "from_id": from_id, "from_name": from_name,
            "to": to, "content": content, "type": action_type, "direction": "outbound",
        }, timeout=1)


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
# /decide — 决策端点
# ═══════════════════════════════════════════════

def _prepare_decision_context(ctx: dict):
    """准备决策上下文：递增回合、设置身份、通信权限、channel/talk"""
    global turn, _current_effective_id, _current_effective_name, _allowed_targets
    turn += 1
    if "round" not in ctx:
        ctx["round"] = turn

    effective_id = ctx.get("agent_id", AGENT_ID)
    effective_name = ctx.get("agent_name", AGENT_NAME)
    _current_effective_id = effective_id
    _current_effective_name = effective_name
    _allowed_targets = set(ctx.get("comm_matrix", {}).get(effective_id, []))
    return effective_id, effective_name


async def _decide_with_brain(ctx: dict, effective_id: str):
    """Brain 后端决策"""
    global _channel_map, _current_talk, _effective_id, _effective_name
    _channel_map = ctx.get("channel_map", {})
    _current_talk = ctx.get("talk", "")
    _effective_id = effective_id
    _effective_name = _current_effective_name

    # 动态身份切换时注入 goals/prompt
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

    action = await asyncio.to_thread(_agent.decide, ctx)
    if not action:
        return {"type": "wait", "target": "", "content": "", "reasoning": "no brain available"}
    return _normalize_action(action)


async def _decide_with_openclaw(ctx: dict):
    """OpenCLAW 后端决策"""
    if not API_KEY:
        return {"type": "wait", "target": "", "content": "", "reasoning": "no API key configured"}
    system = _build_system_prompt(ctx)
    user = _build_user_message(inbox, ctx)
    action_raw = await asyncio.to_thread(_call_openclaw, system, user)
    return _normalize_action(action_raw)


async def _decide_with_claude_code(ctx: dict):
    """Claude Code 后端决策"""
    prompt = _build_claude_prompt(inbox, ctx)
    response = await asyncio.to_thread(_call_claude_code, prompt)
    action_raw = _parse_claude_response(response)
    return _normalize_action(action_raw)


@app.post("/decide")
async def decide(req: DecideRequest = None):
    ctx = req.context if req else {}
    effective_id, effective_name = _prepare_decision_context(ctx)

    try:
        if BACKEND == "brain":
            action = await _decide_with_brain(ctx, effective_id)
        elif BACKEND == "openclaw":
            action = await _decide_with_openclaw(ctx)
        else:  # claude-code
            action = await _decide_with_claude_code(ctx)
    except Exception as e:
        action = {"type": "wait", "target": "", "content": "", "reasoning": str(e)}

    global last_action
    last_action = action
    _log_agent("decide", action.get("content", "") or action.get("reasoning", ""),
               action_type=action.get("type"), target=action.get("target", ""),
               content=action.get("content", ""), reasoning=action.get("reasoning", ""),
               round=turn, status="decided")

    extra = {}
    if BACKEND == "brain":
        extra["has_llm"] = bool(_brain_config.get("api_key"))
    else:
        extra["backend"] = BACKEND
    return _decision_response(action, effective_id, effective_name, extra)


# ═══════════════════════════════════════════════
# /act — 执行端点
# ═══════════════════════════════════════════════

async def _handle_message_action(action_type: str, action_target: str, action_content: str) -> dict:
    """处理 send_message / broadcast 动作"""
    is_broadcast = (action_target == "0.0.0.0" or action_type == "broadcast")
    result: Dict[str, Any] = {}
    if not is_broadcast and action_type == "send_message" and _allowed_targets and action_target not in _allowed_targets:
        _log_agent("act", f"无通信权限: {action_target}（允许: {', '.join(sorted(_allowed_targets))}）",
                   action_type=action_type, target=action_target, status="failed")
        return {"relayed": False}

    try:
        relay_start = time.time()
        if BACKEND == "brain":
            chan_id = _channel_map.get(f"{_effective_id}->{action_target}", "") or \
                      _channel_map.get(f"{_effective_id.lower()}->{action_target.lower()}", "")
        else:
            chan_id = ""
        talk = _current_talk if BACKEND == "brain" else ""
        if is_broadcast:
            ok = await asyncio.to_thread(comm.broadcast, _current_effective_id, _current_effective_name,
                                         action_content, _allowed_targets, chan_id, talk)
        else:
            ok = await asyncio.to_thread(comm.send, _current_effective_id, _current_effective_name,
                                         action_target, action_content, chan_id, talk)
        latency = (time.time() - relay_start) * 1000
        result["relayed"] = ok
        _log_agent("act", action_content or action_type, action_type=action_type,
                   target=action_target, content=action_content,
                   status="success" if ok else "failed")
        if BACKEND == "brain":
            destination = "broadcast" if is_broadcast else action_target
            PacketRecorder.record_outbound(agent_id=_current_effective_id, dst_ip="bus",
                                           dst_port=9000, method="POST", path="/relay",
                                           status=200 if ok else 0, latency_ms=latency,
                                           content=action_content, agent_to=destination)
        _notify_packet_monitor(_current_effective_id, _current_effective_name,
                               "broadcast" if is_broadcast else action_target,
                               action_content, action_type)
    except Exception as e:
        result["relay_error"] = str(e)
        _log_agent("act", f"发送异常: {e}", action_type=action_type, target=action_target,
                   status="failed")
    return result


async def _handle_skill_action(action_target: str, action_content, la: dict) -> dict:
    """处理 execute_skill 动作"""
    skill_name = la.get("skill", action_target)
    skill_params = la.get("params", action_content if isinstance(action_content, dict) else {})
    if not isinstance(skill_params, dict):
        skill_params = {}
    result: Dict[str, Any] = {}
    try:
        r = await asyncio.to_thread(
            requests.post,
            f"{SERVER_URL}/api/skills/execute",
            json={"skill_name": skill_name, "params": skill_params},
            timeout=10,
        )
        skill_ret = r.json() if r.ok else {"error": r.text[:500]}
        result["skill_result"] = skill_ret
        # 写入收件箱
        ret_str = json.dumps(skill_ret, ensure_ascii=False)
        _append_inbox("系统", f"[技能 {skill_name} 执行结果]\n{ret_str}", "system")
        _log_agent("act", f"技能调用: {skill_name} | 返回: {ret_str}",
                   action_type="execute_skill", target=skill_name,
                   status="success" if r.ok else "failed",
                   skill_params=skill_params, skill_result=skill_ret)
    except Exception as e:
        result["skill_error"] = str(e)
        _log_agent("act", f"技能调用异常: {skill_name} | {e}",
                   action_type="execute_skill", target=skill_name, status="failed")
    return result


async def _auto_reply_skill_result(skill_ret: dict, skill_name: str):
    """技能执行成功后，自动回复最近直接发件人"""
    if not isinstance(skill_ret, dict) or skill_ret.get("status") == "error":
        return
    recent_sender = _recent_direct_sender()
    if not recent_sender:
        return
    ret_str = json.dumps(skill_ret, ensure_ascii=False)
    auto_msg = f"[{skill_name} 执行结果]\n{ret_str}"
    ok = await asyncio.to_thread(comm.send, _current_effective_id, _current_effective_name,
                                 recent_sender, auto_msg, "", "")
    _log_agent("act", auto_msg, action_type="send_message", target=recent_sender,
               content=auto_msg, status="success" if ok else "failed")


@app.post("/act")
async def act():
    global last_action
    if not last_action:
        return {"status": "no_decision_yet"}

    action_type = last_action.get("type", last_action.get("action", "wait"))
    action_target = last_action.get("target", "")
    action_content = last_action.get("content", "")
    result: Dict[str, Any] = {"action": last_action, "backend": BACKEND}

    if action_type in ("send_message", "broadcast"):
        result.update(await _handle_message_action(action_type, action_target, action_content))
    elif action_type == "execute_skill":
        result.update(await _handle_skill_action(action_target, action_content, last_action))
        # 技能成功后自动回复最近发件人
        if result.get("skill_result"):
            await _auto_reply_skill_result(result["skill_result"],
                                           last_action.get("skill", action_target))

    return result


# ═══════════════════════════════════════════════
# 其他端点
# ═══════════════════════════════════════════════

@app.get("/status")
async def status():
    has_llm = bool(_brain_config.get("api_key") if _agent else API_KEY)
    return {
        "agent_id": AGENT_ID, "name": AGENT_NAME, "role": AGENT_ROLE,
        "backend": BACKEND, "turn": turn,
        "inbox_size": _inbox_size(), "has_llm": has_llm,
        "core_goal": AGENT_CORE_GOAL or None,
        "hidden_secret": AGENT_HIDDEN_SECRET or None,
        "action_space": AGENT_ACTION_SPACE,
        "initial_assets": AGENT_INITIAL_ASSETS,
        "last_action": last_action,
    }


@app.post("/message")
async def receive_message(msg: MessageIn, request: Request = None):
    if _agent and BACKEND == "brain":
        client_ip = request.client.host if request and request.client else "unknown"
        PacketRecorder.record_inbound(agent_id=AGENT_ID, src_ip=client_ip,
                                      method="POST", path="/message",
                                      content=msg.content, from_id=msg.from_id)
    _append_inbox(msg.from_id, msg.content, msg.type or "direct")
    return {"received": True, "inbox_size": _inbox_size()}


@app.post("/event")
async def receive_event(event: Dict[str, Any]):
    if not _agent:
        return {"received": False, "reason": "only brain backend supports events"}
    event_name = event.get("event_name", "未知事件")
    impact = event.get("impact", "")
    t = event.get("turn", 0)
    _append_inbox("系统", f"⚠️ 事件 [{event_name}]: {impact}", "system")
    _event_queue.append({"event_name": event_name, "impact": impact, "turn": t})
    _log_agent("event_received", f"事件: {event_name} — {impact}",
               event_name=event_name, impact=impact, turn=t)
    return {"received": True, "event": event_name}


@app.get("/events")
async def list_events():
    return {"agent_id": AGENT_ID, "events": _event_queue}


@app.get("/inbox")
async def get_inbox():
    items = _agent.inbox if _agent else inbox
    return {"inbox": items[-20:]}


@app.post("/clear")
async def clear():
    _clear_inbox()
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
    return {"status": "reset",
            "brain_cleared": bool(_agent and hasattr(_agent, 'brain') and _agent.brain)}


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
