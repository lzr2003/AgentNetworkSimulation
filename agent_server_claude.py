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
            "to_agent": kw.get("target", kw.get("to", "")),
            "action": kw.get("action_type", event),
            "action_status": kw.get("status", "success"),
            "details": kw or {},
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
        raise RuntimeError(f"Claude Code failed (exit {result.returncode}): {result.stderr[:500]}")
    return result.stdout.strip()


def _build_prompt(inbox_msgs: list, context: dict = None) -> str:
    """Build the prompt for Claude Code — 支持场景身份注入"""
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

    # 场景身份覆盖容器默认值
    identity = context.get("agent_name", _current_effective_name)
    role = context.get("agent_role", AGENT_ROLE)
    core_goal = context.get("core_goal") or AGENT_CORE_GOAL
    hidden_secret = context.get("hidden_secret") or AGENT_HIDDEN_SECRET
    action_space = context.get("action_space") or AGENT_ACTION_SPACE
    skills_list = context.get("skills_list", [])
    background_rules = context.get("background_rules") or AGENT_SYSTEM_PROMPT

    actions_text = ", ".join(action_space) if action_space else "send_message, broadcast, wait"
    skills_text = ""
    if skills_list:
        skills_text = "\n- 可用技能:\n" + "\n".join(
            f"    {s['name']}: {s.get('desc','')}" for s in skills_list
        )

    system = background_rules or "你是一个仿真场景中的角色，根据你的身份和目标做出决策。"

    return f"""{system}

## 你的身份
- 名字: {identity}
- 角色: {role}
- 核心目标: {core_goal or '完成场景任务'}
- 隐藏秘密: {hidden_secret or '无'}
- 可用行动: {actions_text}{skills_text}

## 当前回合: {turn}
## 已知其它 Agent:
{known_list}

## 收件箱:
{inbox_text}

## 指令
必须立即采取具体行动，绝对不能wait！
做出本轮决策。用 JSON 回复（只输出 JSON）：
```json
{{"reasoning": "推理", "action": "send_message|broadcast|wait", "target": "目标agent_id", "content": "内容"}}
```
注意: target 必须用 agent_id（如 tel_a_ceo），不是中文名。"""


def _parse_response(text: str) -> dict:
    """Parse JSON action from Claude Code response."""
    json_match = re.search(r'\{[\s\S]*"action"[\s\S]*\}', text)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass
    return {"reasoning": "parse error", "action": "wait", "target": "", "content": text[:200]}


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
        _log_agent("decide", (act_content or act_reasoning)[:100],
                   action_type=act_type, target=act_target, status="decided",
                   content=act_content[:200], reasoning=act_reasoning[:200])
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

            if PACKET_MONITOR_URL:
                try:
                    requests.post(f"{PACKET_MONITOR_URL}/api/packets/ingest", json={
                        "from_id": _current_effective_id, "from_name": _current_effective_name,
                        "to": action_target if action_type == "send_message" else "broadcast",
                        "content": action_content, "type": action_type,
                        "direction": "outbound",
                    }, timeout=1)
                except Exception:
                    pass

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
        comm.register_agent(_current_effective_id, _current_effective_name, f"http://localhost:{AGENT_PORT}")
        print(f"[Claude Agent] Registered: {AGENT_ID} @ port {AGENT_PORT}")
    except Exception as e:
        print(f"[Claude Agent] Register failed: {e}")

    print(f"[Claude Agent] {AGENT_NAME} ({AGENT_ROLE}) starting on port {AGENT_PORT}")
    print(f"[Claude Agent] Goal: {AGENT_CORE_GOAL or 'N/A'}")
    uvicorn.run(app, host="0.0.0.0", port=AGENT_PORT, log_level="info")
