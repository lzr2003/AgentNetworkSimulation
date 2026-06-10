#!/usr/bin/env python3
"""
Agent 容器运行时 — Claude Code 后端

每个 Agent 运行在独立 Docker 容器中。
决策流程: 构建 prompt → subprocess claude -p --print → 解析 Action → 执行

环境变量:
  AGENT_ID / AGENT_ROLE / AGENT_NAME / PORT
  MESSAGE_BUS_URL / SERVER_URL
  AGENT_CORE_GOAL / AGENT_HIDDEN_SECRET / AGENT_ACTION_SPACE / AGENT_INITIAL_ASSETS
  AGENT_SYSTEM_PROMPT (from scene background_rules)
  ANTHROPIC_API_KEY (Claude Code 会从环境变量读取)
"""

import os
import sys
import json
import time
import subprocess
import asyncio
from datetime import datetime, timezone
import re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any
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

# ── 通信层 ──
comm = RemoteBus(message_bus_url=MESSAGE_BUS, server_url=SERVER_URL)

# ── FastAPI ──
app = FastAPI(title=f"Agent {AGENT_NAME} (Claude Code)")

turn = 0
last_action: Dict[str, Any] = {}
inbox: list = []
_current_effective_id = AGENT_ID
_current_effective_name = AGENT_NAME
_allowed_targets: set = set()  # 通信权限矩阵


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
            "to_agent": kw.get("target", kw.get("to", "")) if kw.get("action_type") in ("send_message", "broadcast") else "",
            "action": (kw.get("target") or kw.get("to")) if kw.get("action_type") == "execute_skill" else "send_message" if kw.get("action_type") == "broadcast" else kw.get("action_type", event),
            "action_status": kw.get("status", "success"),
            "details": {k: v for k, v in kw.items() if k not in ("action_type", "target")},
        }, timeout=2)
    except Exception:
        pass


def _call_claude_code(prompt: str) -> str:
    """Call Claude Code CLI in print mode (non-interactive)."""
    env = os.environ.copy()
    # Ensure Claude Code doesn't try interactive mode
    result = subprocess.run(
        ["claude", "-p", prompt, "--print", "--output-format", "text"],
        capture_output=True, text=True, timeout=120,
        cwd="/app", env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Claude Code failed (exit {result.returncode}): {result.stderr}")
    return result.stdout.strip()


def _build_prompt(inbox_msgs: list, context: dict = None) -> str:
    """Build the prompt for Claude Code — 支持场景身份注入"""
    context = context or {}
    known = context.get("agents", context.get("known_agents", []))
    known_lines = []
    for a in known:
        aid = a.get('id', a.get('agent_id', '?'))
        nm = a.get('name', aid)
        known_lines.append(f"  - agent_id={aid}  名称={nm}")
    known_list = "\n".join(known_lines) if known_lines else "  none"

    # ── 收件箱分类 ──
    direct_msgs, broadcast_msgs, system_msgs = [], [], []
    for msg in inbox_msgs[-15:]:
        mtype = msg.get('type', 'direct')
        txt = f"  [{msg.get('from', '?')}]: {msg.get('content', '')}"
        if mtype == 'system':
            system_msgs.append(txt)
        elif mtype == 'broadcast':
            broadcast_msgs.append(txt)
        else:
            direct_msgs.append(txt)

    inbox_text = ""
    pending = len(direct_msgs)
    if direct_msgs:
        inbox_text += "## 📬 直接发给你的消息 — 必须回复！\n" + "\n".join(direct_msgs[-10:]) + "\n\n"
    if broadcast_msgs:
        inbox_text += "## 📢 广播消息\n" + "\n".join(broadcast_msgs[-5:]) + "\n\n"
    if system_msgs:
        inbox_text += "## ⚡ 系统通知\n" + "\n".join(system_msgs[-3:]) + "\n\n"
    if not inbox_text:
        inbox_text = "（收件箱为空 — 主动发起对话）\n"

    pending_warning = f"\n⚠️ 你有 {pending} 条未回复的直接消息！必须回复！" if pending > 0 else ""

    # 场景身份覆盖容器默认值
    identity = context.get("agent_name", _current_effective_name)
    role = context.get("agent_role", AGENT_ROLE)
    core_goal = context.get("core_goal") or AGENT_CORE_GOAL
    hidden_secret = context.get("hidden_secret") or AGENT_HIDDEN_SECRET
    skills_list = context.get("skills_list", [])
    background_rules = context.get("background_rules") or AGENT_SYSTEM_PROMPT

    skills_text = ""
    if skills_list:
        skills_text = "\n## 可用技能（使用 execute_skill 动作调用）\n" + "\n".join(
            f"  - execute_skill: {s.get('skill_name', s.get('name', '?'))} — {s.get('description', s.get('desc', ''))}"
            for s in skills_list
        ) + "\n"

    system = background_rules or "你是一个仿真场景中的角色，根据你的身份和目标做出决策。"

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


def _parse_response(text: str) -> dict:
    """Parse JSON action from Claude Code response."""
    json_match = re.search(r'\{[\s\S]*"action"[\s\S]*\}', text)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass
    return {"reasoning": "parse error", "action": "wait", "target": "", "content": text}


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
        "backend": "claude-code", "turn": turn,
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
    """触发 Claude Code 决策 — 支持场景身份注入"""
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

    try:
        prompt = _build_prompt(inbox, ctx)
        response = _call_claude_code(prompt)
        action = _parse_response(response)
        last_action = action
        act_type = action.get('action', 'unknown')
        act_target = action.get('target', '')
        act_content = action.get('content', '')
        act_reasoning = action.get('reasoning', '')
        _log_agent("decide", (act_content or act_reasoning),
                   action_type=act_type, target=act_target, status="decided",
                   content=act_content, reasoning=act_reasoning)
    except Exception as e:
        action = {"reasoning": str(e), "action": "wait", "target": "", "content": ""}
        last_action = action

    return {
        "agent_id": _current_effective_id, "agent_name": _current_effective_name,
        "turn": turn, "backend": "claude-code",
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
    result: Dict[str, Any] = {"action": last_action}

    if action_type in ("send_message", "broadcast"):
        # target=0.0.0.0 → 广播全员
        is_broadcast = (action_target == "0.0.0.0" or action_type == "broadcast")
        # 检查通信权限（广播跳过权限检查）
        if not is_broadcast and action_type == "send_message" and _allowed_targets and action_target not in _allowed_targets:
            result["relayed"] = False
            _log_agent("act", f"无通信权限: {action_target}（允许: {', '.join(sorted(_allowed_targets))}）",
                       action_type=action_type, target=action_target, status="failed")
        else:
            try:
                if is_broadcast:
                    ok = await asyncio.to_thread(comm.broadcast, _current_effective_id, _current_effective_name, action_content, _allowed_targets)
                else:
                    ok = await asyncio.to_thread(comm.send, _current_effective_id, _current_effective_name, action_target, action_content)
                result["relayed"] = ok
                _log_agent("act", action_content or action_type,
                           action_type=action_type, target=action_target,
                           content=action_content, status="success" if ok else "failed")
            except Exception as e:
                result["relay_error"] = str(e)
                _log_agent("act", f"发送异常: {e}",
                           action_type=action_type, target=action_target, status="failed")

            if PACKET_MONITOR_URL:
                try:
                    requests.post(f"{PACKET_MONITOR_URL}/api/packets/ingest", json={
                        "from_id": _current_effective_id, "from_name": _current_effective_name,
                        "to": "broadcast" if is_broadcast else action_target,
                        "content": action_content, "type": "send_message",
                        "direction": "outbound",
                    }, timeout=1)
                except Exception:
                    pass

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
    return {"inbox": inbox[-20:]}


@app.post("/clear")
async def clear():
    inbox.clear()
    return {"cleared": True}


@app.post("/reset")
async def reset_state():
    """重置容器状态 — 容器池复用时调用，清除跨仿真残留"""
    global turn, last_action, inbox, _allowed_targets
    global _current_effective_id, _current_effective_name
    turn = 0
    last_action = {}
    inbox = []
    _allowed_targets = set()
    _current_effective_id = AGENT_ID
    _current_effective_name = AGENT_NAME
    return {"status": "reset"}


if __name__ == "__main__":
    try:
        comm.register_agent(_current_effective_id, _current_effective_name, f"http://localhost:{AGENT_PORT}")
        print(f"[Claude Agent] Registered: {AGENT_ID} @ port {AGENT_PORT}")
    except Exception as e:
        print(f"[Claude Agent] Register failed: {e}")

    print(f"[Claude Agent] {AGENT_NAME} ({AGENT_ROLE}) starting on port {AGENT_PORT}")
    print(f"[Claude Agent] Goal: {AGENT_CORE_GOAL or 'N/A'}")
    uvicorn.run(app, host="0.0.0.0", port=AGENT_PORT, log_level="info")
