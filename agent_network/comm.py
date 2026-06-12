"""
统一通信层 — 抽象 Agent 间通信，内存/容器模式共用同一接口。

LocalBus:  内存模式，直接通过 EventBus 通信（零网络开销）
RemoteBus: 容器模式，通过消息总线 /relay 中转
"""

import os
import json
import time
import requests
from typing import Dict, List, Optional, Any
from abc import ABC, abstractmethod


class CommLayer(ABC):
    """Agent 通信层抽象基类"""

    inbox: List[Dict[str, Any]]

    @abstractmethod
    def send(self, from_id: str, from_name: str, target: str, content: str,
             channel_id: str = "", talk: str = "") -> bool:
        """发送消息给目标 Agent"""
        ...

    @abstractmethod
    def broadcast(self, from_id: str, from_name: str, content: str, allowed: set = None,
                  channel_id: str = "", talk: str = "") -> bool:
        """广播消息给所有 Agent（可选通信权限过滤）"""
        ...

    @abstractmethod
    def register_agent(self, agent_id: str, name: str, url: str = "") -> None:
        """向通信层注册 Agent"""
        ...


class RemoteBus(CommLayer):
    """容器模式 — 通过消息总线 /relay 中转，自动记录出站流量"""

    def __init__(self, message_bus_url: str = "http://localhost:9000",
                 server_url: str = "http://localhost:8000"):
        self._bus_url = message_bus_url.rstrip("/")
        self._server_url = server_url.rstrip("/")
        self.inbox: List[Dict[str, Any]] = []
        # 流量日志
        self._traffic = os.environ.get("LOG_TRAFFIC", "0") == "1"
        self._traffic_component = os.environ.get("AGENT_ID", "agent")

    def _post(self, path: str, json_data: dict = None, params: dict = None,
              timeout: float = 10) -> tuple:
        """内部 POST，流量启用时自动记录"""
        url = f"{self._bus_url}{path}"
        if self._traffic and self._server_url:
            from agent_network.traffic_log import traffic_post_json
            data = json_data if json_data else {}
            traffic_post_json(url, data, component=self._traffic_component,
                              server_url=self._server_url, timeout=timeout)
        try:
            resp = requests.post(url, json=json_data, params=params, timeout=timeout)
            return resp.ok, (resp.json() if resp.ok else None)
        except Exception:
            return False, None

    def register_agent(self, agent_id: str, name: str, url: str = "") -> None:
        """向消息总线注册"""
        try:
            self._post("/register", params={"agent_id": agent_id, "url": url, "name": name}, timeout=3)
        except Exception:
            pass

    def send(self, from_id: str, from_name: str, target: str, content: str,
             channel_id: str = "", talk: str = "") -> bool:
        """通过消息总线转发给目标 Agent"""
        ok, _ = self._post("/relay", json_data={
            "from_id": from_id, "from_name": from_name,
            "to": target, "content": content,
            "channel_id": channel_id, "talk": talk,
        }, timeout=10)
        return ok

    def broadcast(self, from_id: str, from_name: str, content: str, allowed: set = None,
                  channel_id: str = "", talk: str = "") -> bool:
        """通过消息总线广播（消息总线根据注册表转发）"""
        payload = {
            "from_id": from_id, "from_name": from_name,
            "to": "broadcast", "content": content,
            "channel_id": channel_id, "talk": talk,
        }
        if allowed:
            payload["allowed"] = list(allowed)
        ok, _ = self._post("/relay", json_data=payload, timeout=15)
        return ok
