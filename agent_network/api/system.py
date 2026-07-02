import os
import json
from datetime import datetime
from typing import Dict, Any
from fastapi import APIRouter, HTTPException, Request

try:
    import psutil
except ImportError:
    psutil = None

from agent_network import state
from agent_network.agent_model import AgentRegistry
from agent_network.logger import get_logger
from agent_network.real_packet_store import packet_stats

router = APIRouter()
logger = get_logger()


@router.get("/stats")
async def system_stats():
    if psutil:
        mem = psutil.virtual_memory()
        mem_stats = {"total_mb": mem.total // (1024 * 1024), "used_mb": mem.used // (1024 * 1024), "percent": mem.percent}
    else:
        mem_stats = {"total_mb": 0, "used_mb": 0, "percent": 0}

    token_usage = state.get_token_usage_snapshot()
    totals = token_usage.get("totals", {})
    return {
        "memory": mem_stats,
        "simulation": {
            "started_at": state.service_state["started_at"],
            "uptime_seconds": (datetime.now() - datetime.fromisoformat(state.service_state["started_at"])).total_seconds() if "started_at" in state.service_state else 0,
            "simulations_run": state.service_state["simulations_run"],
        },
        "agents": AgentRegistry.get_stats(),
        "tools": {
            "registered": len(state.active_tools_module.ToolRegistry.list_tools()) if state.active_tools_module and hasattr(state.active_tools_module, "ToolRegistry") else 0,
            "stats": {"total_calls": 0},
        },
        "packets": packet_stats(),
        "logs": logger.get_index_stats(),
        "tokens": {"total_calls": totals.get("events", 0), "total_tokens": totals.get("total", 0), "provider_total": totals.get("provider_total", 0)},
        "network_mode": "direct",
    }


@router.get("/settings")
async def get_settings():
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


@router.post("/settings")
async def update_settings(req: Request):
    data = await req.json()
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        config = {}
    config.update(data)
    with open("config.json", "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    return {"status": "success"}


@router.get("/tools")
async def list_tools():
    if state.active_tools_module and hasattr(state.active_tools_module, "ToolRegistry"):
        return {"tools": state.active_tools_module.ToolRegistry.list_tools()}
    return {"tools": []}


@router.post("/tools/execute")
async def execute_tool(req: Request):
    if os.environ.get("ENABLE_DEBUG_TOOL_EXECUTE") != "1":
        raise HTTPException(status_code=403, detail="Direct server-side tool execution is disabled.")
    data = await req.json()
    tool_name = data.get("tool_name")
    params = data.get("params", {})
    if state.active_tools_module and hasattr(state.active_tools_module, "ToolRegistry"):
        try:
            result = state.active_tools_module.ToolRegistry.execute(tool_name, **params)
            return {"tool": tool_name, "result": result}
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found. Active scene has no tools.")


@router.get("/tools/stats")
async def tool_stats():
    return {"total_calls": 0}
