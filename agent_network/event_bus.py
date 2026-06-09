"""
EventBus + PacketRecorder — Agent 间通信报文记录

每条 PacketRecord 记录 OSI L3-L7 层级信息：
  L3: src_ip / dst_ip (容器 IP)
  L4: src_port / dst_port / protocol (HTTP)
  L7: method / path / status / agent_from / agent_to / content / reasoning
"""

import json
import threading
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional
from collections import deque


class PacketRecord:
    """单条报文记录 — 模拟 IP 包 + HTTP 请求"""
    __slots__ = (
        "timestamp", "direction",
        "src_ip", "src_port", "dst_ip", "dst_port", "protocol",
        "method", "path", "status_code", "latency_ms",
        "agent_from", "agent_to", "content", "reasoning", "content_length",
    )

    def __init__(self, **kw):
        self.timestamp = kw.get("timestamp", datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
        self.direction = kw.get("direction", "relay")  # inbound | outbound | relay
        # 网络层
        self.src_ip = kw.get("src_ip", "")
        self.src_port = kw.get("src_port", 0)
        self.dst_ip = kw.get("dst_ip", "")
        self.dst_port = kw.get("dst_port", 0)
        self.protocol = kw.get("protocol", "HTTP/1.1")
        # 传输层
        self.method = kw.get("method", "POST")
        self.path = kw.get("path", "/")
        self.status_code = kw.get("status_code", 0)
        self.latency_ms = round(kw.get("latency_ms", 0), 2)
        # 应用层
        self.agent_from = kw.get("agent_from", "")
        self.agent_to = kw.get("agent_to", "")
        self.content = kw.get("content", "")
        self.reasoning = kw.get("reasoning", "")
        self.content_length = len(self.content)

    def to_dict(self) -> Dict:
        return {
            "timestamp": self.timestamp,
            "direction": self.direction,
            # L3
            "src": f"{self.src_ip}:{self.src_port}" if self.src_ip else "",
            "dst": f"{self.dst_ip}:{self.dst_port}" if self.dst_ip else "",
            "protocol": self.protocol,
            # L4-L7
            "method": self.method,
            "path": self.path,
            "status": self.status_code,
            "latency_ms": self.latency_ms,
            # 应用层
            "agent_from": self.agent_from,
            "agent_to": self.agent_to,
            "content": self.content[:500],
            "reasoning": self.reasoning[:200],
            "size_bytes": self.content_length,
        }

    def to_wireshark_style(self) -> str:
        """Wireshark 风格的摘要行"""
        return (
            f"{self.timestamp}  {self.src_ip}:{self.src_port} → {self.dst_ip}:{self.dst_port}  "
            f"{self.protocol}  {self.method} {self.path}  [{self.status_code}]  "
            f"{self.latency_ms}ms  {self.content_length}B  "
            f"{self.agent_from}→{self.agent_to}"
        )


class PacketRecorder:
    """全局单例报文记录器 — 线程安全环形缓冲"""

    _instance: Optional["PacketRecorder"] = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init()
        return cls._instance

    def _init(self, max_packets: int = 1000):
        self._records: deque[PacketRecord] = deque(maxlen=max_packets)
        self._stats = {
            "total_packets": 0,
            "total_bytes": 0,
            "by_direction": {},
            "by_agent": {},
            "by_status": {},
            "avg_latency_ms": 0.0,
        }

    # ── 记录方法 ──

    @classmethod
    def record(cls, **kw):
        """记录一条报文"""
        rec = PacketRecord(**kw)
        inst = cls()
        with cls._lock:
            inst._records.append(rec)
            st = inst._stats
            st["total_packets"] += 1
            st["total_bytes"] += rec.content_length
            st["by_direction"][rec.direction] = st["by_direction"].get(rec.direction, 0) + 1
            st["by_agent"][rec.agent_from] = st["by_agent"].get(rec.agent_from, 0) + 1
            st["by_status"][str(rec.status_code)] = st["by_status"].get(str(rec.status_code), 0) + 1
            # 滑动平均延迟
            n = st["total_packets"]
            st["avg_latency_ms"] = round(
                (st["avg_latency_ms"] * (n - 1) + rec.latency_ms) / n, 2
            )

    @classmethod
    def record_inbound(cls, agent_id: str, src_ip: str, method: str = "POST",
                       path: str = "/message", status: int = 200, latency_ms: float = 0,
                       content: str = "", from_id: str = "", **kw):
        """记录入站报文 (Agent 接收)"""
        cls.record(
            direction="inbound", agent_to=agent_id, src_ip=src_ip,
            agent_from=from_id if from_id else kw.pop("agent_from", ""),
            method=method, path=path, status_code=status, latency_ms=latency_ms,
            content=content, **kw,
        )

    @classmethod
    def record_outbound(cls, agent_id: str, dst_ip: str, dst_port: int = 9000,
                        method: str = "POST", path: str = "/relay",
                        status: int = 200, latency_ms: float = 0,
                        content: str = "", reasoning: str = "", **kw):
        """记录出站报文 (Agent 发送)"""
        cls.record(
            direction="outbound", agent_from=agent_id, dst_ip=dst_ip, dst_port=dst_port,
            method=method, path=path, status_code=status, latency_ms=latency_ms,
            content=content, reasoning=reasoning, **kw,
        )

    # ── 查询方法 ──

    @classmethod
    def get_records(cls, agent_id: str = None, direction: str = None,
                    limit: int = 100) -> List[Dict]:
        inst = cls()
        with cls._lock:
            records = list(inst._records)
        if agent_id:
            records = [r for r in records if r.agent_from == agent_id or r.agent_to == agent_id]
        if direction:
            records = [r for r in records if r.direction == direction]
        return [r.to_dict() for r in records[-limit:]]

    @classmethod
    def get_wireshark_view(cls, agent_id: str = None, limit: int = 100) -> List[str]:
        """Wireshark 风格的文本摘要列表"""
        records = cls.get_records(agent_id=agent_id, limit=limit)
        inst = cls()
        with cls._lock:
            all_records = list(inst._records)
        # 找到匹配的原始记录对象
        result = []
        for d in records:
            for r in all_records:
                if r.timestamp == d["timestamp"] and r.content[:20] == d["content"][:20]:
                    result.append(r.to_wireshark_style())
                    break
        return result

    @classmethod
    def get_stats(cls) -> Dict:
        inst = cls()
        with cls._lock:
            return dict(inst._stats)

    @classmethod
    def reset(cls):
        inst = cls()
        with cls._lock:
            inst._init()


class EventBus:
    """事件总线 — 用于内部事件发布/订阅"""
    def __init__(self, name="default"):
        self.name = name
        self._subscribers: Dict[str, List] = {}

    def subscribe(self, event: str, callback):
        if event not in self._subscribers:
            self._subscribers[event] = []
        self._subscribers[event].append(callback)

    def publish(self, event: str, data: Dict = None) -> int:
        count = 0
        for cb in self._subscribers.get(event, []):
            try:
                cb(data)
                count += 1
            except Exception:
                pass
        return count

    def reset(self):
        self._subscribers.clear()
