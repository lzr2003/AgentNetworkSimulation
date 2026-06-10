"""
统一结构化日志 — 所有模块共用

日志条目格式:
{
  "timestamp": "2026-06-09T03:20:00.123",
  "level": "INFO" | "DEBUG" | "WARN" | "ERROR",
  "event": "message_relayed" | "agent_decide" | "agent_act" | "container_start" | ...,
  "agent_id": "agent-001" (可选),
  "message": "人类可读的描述",
  "details": { ... 结构化数据 ... }
}
"""

import json
import os
import time
import threading
from enum import Enum
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from collections import deque


class LogLevel(Enum):
    TRACE = 0
    DEBUG = 1
    INFO = 2
    WARN = 3
    ERROR = 4
    FATAL = 5
    AUDIT = 6


class LogEntry:
    def __init__(self, level: LogLevel, event: str, message: str = "",
                 agent_id: str = "", details: Dict = None):
        self.timestamp = datetime.now().isoformat(timespec="milliseconds")
        self.level = level.name
        self.event = event
        self.agent_id = agent_id
        self.message = message
        self.details = details or {}

    def to_dict(self) -> Dict:
        return {
            "timestamp": self.timestamp,
            "level": self.level,
            "event": self.event,
            "agent_id": self.agent_id,
            "message": self.message,
            "details": self.details,
        }


class SimulationLogger:
    """全局单例日志 — 线程安全环形缓冲"""

    _instance: Optional["SimulationLogger"] = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kw):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, name: str = "", max_entries: int = 2000, log_dir: str = ""):
        if self._initialized:
            return
        self.name = name or "AgentNetwork"
        self._max = max_entries
        self._entries: deque[LogEntry] = deque(maxlen=max_entries)
        self._stats = {
            "total": 0,
            "by_level": {},
            "by_event": {},
            "by_agent": {},
            "start_time": datetime.now().isoformat(timespec="seconds"),
        }
        # 持久化目录
        self._log_dir = log_dir or os.environ.get("LOG_DIR", "./data/logs")
        self._file_path = ""           # 向后兼容：指向当前 session 的 global.jsonl
        self._session_dir = ""         # 当前 session 文件夹
        self._session_comm_path = ""   # communication.jsonl
        self._session_behavior_path = ""  # behavior.jsonl
        self._session_active = False
        self._file_lock = threading.Lock()
        self._init_file()
        self._initialized = True

    # ── 文件持久化 ──

    def _init_file(self):
        """初始化日志目录（文件路径在 start_session / set_session_dir 时设置）"""
        if not self._log_dir:
            return
        os.makedirs(self._log_dir, exist_ok=True)
        # 不再设置日期回退文件 — 所有日志必须在 session 内写入 global.jsonl

    def start_session(self, scene_name: str):
        """开始新的仿真会话 — 创建 {剧本名}_{时间戳}/ 文件夹"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")  # 精确到微秒
        safe_name = scene_name.replace("/", "_").replace("\\", "_").replace(" ", "_")
        self._session_dir = os.path.join(self._log_dir, f"{safe_name}_{ts}")
        os.makedirs(self._session_dir, exist_ok=True)
        self._set_session_paths()
        self._session_active = True
        # 写入 session 元信息
        meta = {
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "level": "INFO",
            "event": "session_start",
            "agent_id": "",
            "message": f"Session started: {scene_name}",
            "details": {"scene_name": scene_name, "session_dir": self._session_dir},
        }
        self._append_file(self._file_path, meta)

    def set_session_dir(self, session_dir: str):
        """复用已有 session 文件夹（供跨容器同步，由 message_bus 调用）"""
        self._session_dir = session_dir
        self._set_session_paths()
        self._session_active = True

    def _set_session_paths(self):
        """根据 _session_dir 设置三个日志文件路径"""
        self._file_path = os.path.join(self._session_dir, "global.jsonl")
        self._session_comm_path = os.path.join(self._session_dir, "communication.jsonl")
        self._session_behavior_path = os.path.join(self._session_dir, "behavior.jsonl")

    def _append_file(self, filepath: str, entry: Dict):
        """向指定文件追加一行 JSON"""
        if not filepath:
            return
        try:
            with self._file_lock:
                with open(filepath, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _write_file(self, entry: Dict):
        """追加日志到对应文件 — 全局/通信/行为三路写入"""
        if not self._file_path:
            return  # session 未启动，仅保留在内存缓冲区
        event = entry.get("event", "")
        # 全局日志：全部写入
        self._append_file(self._file_path, entry)
        # 通信日志：仅 agent_message
        if event == "agent_message" and self._session_comm_path:
            self._append_file(self._session_comm_path, entry)
        # 行为日志：agent_action, agent_decide, decide, act
        if event in ("agent_action", "agent_decide", "decide", "act") and self._session_behavior_path:
            self._append_file(self._session_behavior_path, entry)

    def export(self, fmt: str = "jsonl", limit: int = 0) -> str:
        """
        导出日志为字符串
        - jsonl: JSON Lines 格式
        - json: JSON 数组格式
        - csv: 逗号分隔值
        """
        entries = list(self._entries)[-limit:] if limit > 0 else list(self._entries)
        data = [e.to_dict() for e in entries]

        if fmt == "json":
            return json.dumps(data, ensure_ascii=False, indent=2)
        elif fmt == "csv":
            import io
            import csv
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=["timestamp", "level", "event", "agent_id", "message", "details"])
            writer.writeheader()
            for d in data:
                writer.writerow({
                    "timestamp": d["timestamp"], "level": d["level"],
                    "event": d["event"], "agent_id": d["agent_id"],
                    "message": d["message"], "details": json.dumps(d["details"], ensure_ascii=False),
                })
            return buf.getvalue()
        else:  # jsonl
            return "\n".join(json.dumps(d, ensure_ascii=False) for d in data)

    def export_file(self, filepath: str, fmt: str = "jsonl", limit: int = 0):
        """导出日志到文件"""
        content = self.export(fmt=fmt, limit=limit)
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return filepath

    def list_log_files(self) -> List[Dict]:
        """列出所有持久化的日志文件（支持 session 文件夹 + 旧版单文件）"""
        if not self._log_dir or not os.path.isdir(self._log_dir):
            return []
        sessions = []
        for entry in sorted(os.listdir(self._log_dir), reverse=True):
            entry_path = os.path.join(self._log_dir, entry)
            if os.path.isdir(entry_path):
                # Session 文件夹
                files = []
                for f in sorted(os.listdir(entry_path)):
                    if f.endswith(".jsonl"):
                        fp = os.path.join(entry_path, f)
                        files.append({
                            "name": f,
                            "size_bytes": os.path.getsize(fp),
                            "path": fp,
                        })
                if files:
                    sessions.append({
                        "session": entry,
                        "path": entry_path,
                        "files": files,
                    })
            elif entry.endswith(".jsonl"):
                # 旧版单文件（兼容）
                sessions.append({
                    "session": None,
                    "path": None,
                    "files": [{
                        "name": entry,
                        "size_bytes": os.path.getsize(entry_path),
                        "path": entry_path,
                    }],
                })
        return sessions

    # ── 便捷方法 ──

    def system(self, event: str, message: str = "", level: LogLevel = LogLevel.INFO,
               agent_id: str = "", details: Dict = None, **kw):
        """记录系统事件 (server / message_bus)"""
        self._log(level, event, message, agent_id, details or kw or None)

    def agent_action(self, agent_id: str, action: str, result: Dict = None, **kw):
        """记录 Agent 动作"""
        details = kw
        if result:
            details["result"] = result
        self._log(LogLevel.INFO, "agent_action", f"[{agent_id}] {action}",
                  agent_id=agent_id, details=details)

    def agent_decide(self, agent_id: str, prompt_snippet: str, decision: Dict = None):
        """记录 Agent 决策"""
        self._log(LogLevel.INFO, "agent_decide",
                  f"[{agent_id}] 决策: {prompt_snippet}",
                  agent_id=agent_id,
                  details={"prompt_snippet": prompt_snippet, "decision": decision or {}})

    def agent_message(self, from_id: str, to: str, content: str, reasoning: str = "",
                      latency_ms: float = 0, status: str = "success",
                      # ── 网络层字段 ──
                      src_ip: str = "", src_port: int = 0,
                      dst_ip: str = "", dst_port: int = 0,
                      protocol: str = "TCP/HTTP",
                      packet_len: int = 0, header_len: int = 0, payload_len: int = 0,
                      tcp_flags: str = "",
                      channel_id: str = "",
                      message_type: str = "relay",
                      talk: str = ""):
        """记录 Agent 间通信报文（含完整网络层元数据）"""
        self._log(LogLevel.INFO, "agent_message",
                  f"{from_id} → {to}: {content}",
                  agent_id=from_id,
                  details={
                      "from": from_id, "to": to,
                      "content": content,
                      "reasoning": reasoning,
                      "latency_ms": round(latency_ms, 1),
                      "status": status,
                      # ── 网络层 ──
                      "src_ip": src_ip, "src_port": src_port,
                      "dst_ip": dst_ip, "dst_port": dst_port,
                      "protocol": protocol,
                      "packet_len": packet_len, "header_len": header_len, "payload_len": payload_len,
                      "tcp_flags": tcp_flags,
                      "channel_id": channel_id,
                      "message_type": message_type,
                      "talk": talk,
                  })

    def container_event(self, agent_id: str, event: str, message: str = "", **kw):
        """记录容器生命周期事件"""
        self._log(LogLevel.INFO, f"container_{event}",
                  f"[{agent_id}] {message or event}", agent_id=agent_id, details=kw or None)

    def event_trigger(self, turn: int, event_name: str, impact: str):
        """记录场景事件触发"""
        self._log(LogLevel.AUDIT, "event_trigger",
                  f"Round {turn}: {event_name} — {impact}",
                  details={"turn": turn, "event_name": event_name, "impact": impact})

    def dag_step(self, step_id: str, agent_id: str, action: str, round_num: int, status: str = "started"):
        """记录 DAG 工作流步骤"""
        self._log(LogLevel.INFO, "dag_step",
                  f"Round {round_num}, Step {step_id}: [{agent_id}] {action} ({status})",
                  agent_id=agent_id,
                  details={"step_id": step_id, "round": round_num, "action": action, "status": status})

    def error(self, event: str, message: str = "", agent_id: str = "", **kw):
        """记录错误"""
        self._log(LogLevel.ERROR, event, message, agent_id, kw or None)

    # ── 核心方法 ──

    def _log(self, level: LogLevel, event: str, message: str = "",
             agent_id: str = "", details: Dict = None):
        entry = LogEntry(level, event, message, agent_id, details)
        with self._lock:
            self._entries.append(entry)
            self._stats["total"] += 1
            self._stats["by_level"][level.name] = self._stats["by_level"].get(level.name, 0) + 1
            self._stats["by_event"][event] = self._stats["by_event"].get(event, 0) + 1
            if agent_id:
                self._stats["by_agent"][agent_id] = self._stats["by_agent"].get(agent_id, 0) + 1
        # 异步持久化
        self._write_file(entry.to_dict())

    def get_entries(self, limit: int = 100) -> List[Dict]:
        with self._lock:
            items = list(self._entries)[-limit:]
            return [e.to_dict() for e in items]

    def query(self, agent_id: str = None, event: str = None, level: str = None,
              keyword: str = None, limit: int = 50) -> List[Dict]:
        """按条件过滤日志"""
        with self._lock:
            results = list(self._entries)
        if agent_id:
            results = [e for e in results if e.agent_id == agent_id]
        if event:
            results = [e for e in results if e.event == event]
        if level:
            results = [e for e in results if e.level == level.upper()]
        if keyword:
            k = keyword.lower()
            results = [e for e in results if k in e.message.lower() or k in json.dumps(e.details).lower()]
        return [e.to_dict() for e in results[-limit:]]

    def get_index_stats(self) -> Dict:
        with self._lock:
            return dict(self._stats)

    def get_agent_timeline(self, agent_id: str, limit: int = 50) -> List[Dict]:
        """获取某个 Agent 的完整动作时间线"""
        return self.query(agent_id=agent_id, limit=limit)

    def get_message_log(self, limit: int = 50) -> List[Dict]:
        """获取 Agent 间通信报文"""
        return self.query(event="agent_message", limit=limit)

    def reset(self):
        with self._lock:
            self._entries.clear()
            self._stats = {
                "total": 0, "by_level": {}, "by_event": {}, "by_agent": {},
                "start_time": datetime.now().isoformat(timespec="seconds"),
            }
        return self


# ── 全局实例 ──
_logger = SimulationLogger("AgentNetwork")
system_log = _logger.system
agent_log = _logger.agent_action
message_log = _logger.agent_message
get_logger = lambda: _logger
