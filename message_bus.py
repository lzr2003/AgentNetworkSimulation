#!/usr/bin/env python3
"""
消息总线 — 运行在 Host，路由 Agent 容器间的消息

每个 Agent 容器通过 HTTP POST /relay 发送消息
消息总线根据 target 转发到目标 Agent 容器

同时记录所有消息，用于通信分析
"""

import os
import sys
import json
import time
from typing import Dict, List, Any
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
import uvicorn
import requests

from agent_network.logger import get_logger, LogLevel
from agent_network.event_bus import PacketRecorder

app = FastAPI(title="Agent Message Bus")

# ── 全局日志器 ──
logger = get_logger()

# ── 可选的外部服务转发 ──
LOG_COLLECTOR_URL = os.environ.get("LOG_COLLECTOR_URL", "")
PACKET_MONITOR_URL = os.environ.get("PACKET_MONITOR_URL", "")


class RelayMessage(BaseModel):
    from_id: str
    from_name: str = ""
    to: str
    content: str
    reasoning: str = ""


# Agent 注册表: {agent_id: "http://host:port"}
agent_registry: Dict[str, str] = {}
stats = {
    "total_messages": 0,
    "by_source": {},
    "by_target": {},
    "start_time": datetime.now().isoformat(),
}


@app.get("/health")
async def health():
    return {"status": "ok", "agents": len(agent_registry)}


@app.post("/register")
async def register(agent_id: str, url: str, name: str = ""):
    """Agent 容器注册自己 (同时按ID和名称索引)"""
    agent_registry[agent_id] = url
    if name:
        agent_registry[name] = url  # 名称别名
    logger.system("agent_registered", f"[Bus] {agent_id} ({name}) @ {url}",
                  agent_id=agent_id, details={"url": url, "name": name, "total": len(agent_registry)})
    return {"registered": agent_id, "total": len(agent_registry)}


@app.post("/unregister")
async def unregister(agent_id: str):
    agent_registry.pop(agent_id, None)
    logger.system("agent_unregistered", f"[Bus] {agent_id} 已注销", agent_id=agent_id)
    return {"unregistered": agent_id}


@app.post("/relay")
async def relay(msg: RelayMessage, request: Request):
    """转发消息到目标 Agent — 双向记录报文"""
    relay_start = time.time()
    client_ip = request.client.host if request.client else "unknown"

    stats["total_messages"] += 1
    stats["by_source"][msg.from_id] = stats["by_source"].get(msg.from_id, 0) + 1
    stats["by_target"][msg.to] = stats["by_target"].get(msg.to, 0) + 1

    # ── 入站报文: Agent → Bus ──
    PacketRecorder.record(
        direction="inbound",
        src_ip=client_ip, dst_ip="bus", dst_port=9000,
        protocol="HTTP/1.1", method="POST", path="/relay",
        agent_from=msg.from_id, agent_to=msg.to,
        content=msg.content, reasoning=msg.reasoning,
    )

    # ── 记录所有通信报文（日志） ──
    logger.agent_message(
        from_id=msg.from_id, to=msg.to,
        content=msg.content, reasoning=msg.reasoning,
        latency_ms=(time.time() - relay_start) * 1000,
        status="relaying",
    )

    # ── 日志收集器转发 ──
    if LOG_COLLECTOR_URL:
        try:
            requests.post(f"{LOG_COLLECTOR_URL}/api/logs/ingest", json={
                "level": "INFO", "event": "message_relayed",
                "agent_id": msg.from_id,
                "index": "logs-agent",
                "message": msg.content[:500],
                "details": {"to": msg.to, "reasoning": msg.reasoning[:200]},
            }, timeout=1)
        except Exception:
            pass

    # ── 数据包监控器转发 ──
    if PACKET_MONITOR_URL:
        try:
            requests.post(f"{PACKET_MONITOR_URL}/api/packets/ingest", json={
                "from_id": msg.from_id, "from_name": msg.from_name,
                "to": msg.to, "content": msg.content,
                "reasoning": msg.reasoning,
                "type": "relay",
                "direction": "outbound",
                "latency": (time.time() - relay_start) * 1000,
            }, timeout=1)
        except Exception:
            pass

    # 广播模式
    if msg.to == "broadcast":
        results = {}
        for aid, url in agent_registry.items():
            if aid != msg.from_id:
                try:
                    resp = requests.post(f"{url}/message", json={
                        "from_id": msg.from_id, "from_name": msg.from_name,
                        "content": msg.content,
                    }, timeout=5)
                    results[aid] = resp.status_code
                except Exception as e:
                    results[aid] = str(e)
        for aid, status_code in results.items():
            PacketRecorder.record(
                direction="outbound", src_ip="bus", dst_ip=f"agent:{aid}",
                protocol="HTTP/1.1", method="POST", path="/message",
                status_code=status_code if isinstance(status_code, int) else 0,
                agent_from=msg.from_id, agent_to=aid,
                content=msg.content, reasoning=msg.reasoning,
            )
        logger.agent_message(from_id=msg.from_id, to="broadcast",
                             content=msg.content, reasoning=msg.reasoning,
                             status="broadcast", latency_ms=(time.time()-relay_start)*1000,
                             targets=len(results))
        return {"broadcast": True, "targets": len(results), "results": results}

    # 单播 — 先精确匹配ID，再匹配名称，再模糊匹配
    target_url = agent_registry.get(msg.to)
    if not target_url:
        # 尝试名称模糊匹配
        target_lower = msg.to.lower().strip()
        for key, url in agent_registry.items():
            if target_lower in key.lower() or key.lower() in target_lower:
                target_url = url
                break
    if not target_url:
        logger.error("message_target_not_found",
                     f"[Bus] 目标 '{msg.to}' 不在注册表中", agent_id=msg.from_id,
                     known_agents=list(agent_registry.keys()))
        return {"error": f"Target '{msg.to}' not found", "known": list(agent_registry.keys())}

    try:
        resp = requests.post(f"{target_url}/message", json={
            "from_id": msg.from_id, "from_name": msg.from_name,
            "content": msg.content,
        }, timeout=5)
        latency = (time.time() - relay_start) * 1000
        # ── 出站报文: Bus → 目标 Agent ──
        PacketRecorder.record(
            direction="outbound",
            src_ip="bus", dst_ip=target_url, dst_port=0,
            protocol="HTTP/1.1", method="POST", path="/message",
            status_code=resp.status_code, latency_ms=latency,
            agent_from=msg.from_id, agent_to=msg.to,
            content=msg.content, reasoning=msg.reasoning,
        )
        logger.agent_message(from_id=msg.from_id, to=msg.to,
                             content=msg.content, reasoning=msg.reasoning,
                             latency_ms=latency, status=f"delivered({resp.status_code})")
        return {"relayed": True, "to": msg.to, "status": resp.status_code, "latency_ms": round(latency, 1)}
    except Exception as e:
        latency = (time.time() - relay_start) * 1000
        PacketRecorder.record(
            direction="outbound", src_ip="bus", dst_ip=target_url,
            protocol="HTTP/1.1", method="POST", path="/message",
            status_code=0, latency_ms=latency,
            agent_from=msg.from_id, agent_to=msg.to,
            content=msg.content, reasoning=str(e),
        )
        logger.error("message_relay_failed",
                     f"[Bus] 转发给 {msg.to} 失败: {e}", agent_id=msg.from_id,
                     target=msg.to, error=str(e), latency_ms=latency)
        return {"error": str(e), "to": msg.to}


@app.get("/agents")
async def list_agents():
    return {"agents": agent_registry, "count": len(agent_registry)}


@app.get("/messages")
async def get_messages(limit: int = 50):
    """获取报文记录 (兼容旧API + 新格式)"""
    entries = logger.get_message_log(limit)
    return {"total": stats["total_messages"], "messages": entries,
            "stats": stats}


@app.get("/messages/raw")
async def get_raw_messages(limit: int = 50):
    """获取原始报文内容 (无过滤器)"""
    entries = logger.query(event="agent_message", limit=limit)
    return {"total": len(entries), "messages": entries}


@app.get("/packets")
async def get_packets(
    agent_id: str = None,
    direction: str = None,
    limit: int = 100,
):
    """获取 IP 包级别的通信报文"""
    records = PacketRecorder.get_records(agent_id=agent_id, direction=direction, limit=limit)
    return {
        "total": PacketRecorder.get_stats()["total_packets"],
        "packets": records,
        "stats": PacketRecorder.get_stats(),
    }


@app.get("/packets/stream")
async def get_packets_stream(
    agent_id: str = None,
    limit: int = 100,
):
    """Wireshark 风格的报文文本流"""
    lines = PacketRecorder.get_wireshark_view(agent_id=agent_id, limit=limit)
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("\n".join(lines), media_type="text/plain")


@app.get("/packets/stats")
async def get_packet_stats():
    """报文统计"""
    return PacketRecorder.get_stats()


@app.get("/stats")
async def get_stats():
    return {
        **stats,
        "agent_count": len(agent_registry),
        "log_stats": logger.get_index_stats(),
    }


if __name__ == "__main__":
    port = int(os.environ.get("BUS_PORT", 9000))
    logger.system("message_bus_start", f"Message Bus starting on :{port}",
                  details={"port": port})
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
