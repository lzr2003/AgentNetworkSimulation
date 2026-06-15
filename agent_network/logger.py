"""
统一结构化日志 — 所有模块共用

日志条目格式 (v2 统一 schema):
{
  "timestamp": "2026-06-11T19:14:02.501",
  "seq": 1,
  "session_id": "minesweeper_20260611_191402_501302",
  "level": "INFO",
  "source": "backend",          # backend | agent | bus | frontend
  "component": "srv",           # srv | bus | ag-c1 | dashboard
  "category": "communication",  # communication | agent_behavior | system | frontend | lifecycle
  "event": "agent_message",

  "actor": {"id": "cmdr_01"},
  "target": {"id": "soldier_07"},

  "action": {"name": "send_message", "status": "success"},
  "message": "cmdr_01 → soldier_07",   # 短摘要，仅用于人类快速阅读

  "payload": {                         # 业务数据
    "content": "报告指挥官...",
    "reasoning": "...",
    "skill_params": {},
    "skill_result": {}
  },

  "network": {                         # 通信层数据
    "src_ip": "172.19.0.4", "src_port": 0,
    "dst_ip": "172.19.0.8", "dst_port": 0,
    "protocol": "TCP/HTTP",
    "latency_ms": 8.4,
    "packet_len": 317, "header_len": 200, "payload_len": 117,
    "tcp_flags": "PSH,ACK",
    "channel_id": "",
    "message_type": "relay"
  },

  "trace": {                           # 追踪信息
    "round": 3,
    "talk": "",
    "correlation_id": ""
  }
}

路由规则:
  - category == "communication"  → global.jsonl + communication.jsonl
  - category == "agent_behavior" → global.jsonl + behavior.jsonl
  - 其他                         → global.jsonl
"""

import json
import os
import time
import threading
from enum import Enum
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
from collections import deque


class LogLevel(Enum):
    INFO = 0
    WARN = 1
    ERROR = 2


# ── 统一日志记录 schema ──

_LOG_TZ = timezone(timedelta(hours=8))


def _format_log_time(dt: datetime, timespec: str = "milliseconds") -> str:
    """Format timestamps as Beijing time without a timezone suffix."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_LOG_TZ)
    dt = dt.astimezone(_LOG_TZ)
    if timespec == "seconds":
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def current_log_timestamp(timespec: str = "milliseconds") -> str:
    """Current Beijing-time timestamp for global logs."""
    return _format_log_time(datetime.now(_LOG_TZ), timespec=timespec)


def normalize_log_timestamp(value: Any = "", timespec: str = "milliseconds") -> str:
    """
    Normalize incoming timestamps to the global log timezone.

    Naive timestamps in this project are already Beijing-local log time. Browser
    clients may send explicit UTC timestamps with a Z suffix; those are converted
    to the same Beijing-local display format.
    """
    if not value:
        return current_log_timestamp(timespec=timespec)
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except Exception:
            return current_log_timestamp(timespec=timespec)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_LOG_TZ)
    return _format_log_time(dt, timespec=timespec)

def _base_record(level: str, source: str, component: str, category: str,
                 event: str, message: str = "") -> Dict:
    """构造一条统一 schema 日志记录的骨架"""
    return {
        "timestamp": current_log_timestamp(),
        "seq": 0,
        "session_id": "",
        "level": level,
        "source": source,
        "component": component,
        "category": category,
        "event": event,
        "actor": {},
        "target": {},
        "action": {},
        "message": message,
        "payload": {},
        "network": {},
        "trace": {},
    }


class SimulationLogger:
    """全局单例日志 — 线程安全环形缓冲 + JSONL 持久化"""

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
        self._entries: deque[Dict] = deque(maxlen=max_entries)
        self._stats = {
            "total": 0,
            "by_level": {},
            "by_event": {},
            "by_agent": {},
            "start_time": current_log_timestamp(timespec="seconds"),
        }
        self._seq = 0
        self._session_id = ""
        # 持久化目录
        self._log_dir = log_dir or os.environ.get("LOG_DIR", "./data/logs")
        self._file_path = ""            # global.jsonl
        self._session_dir = ""
        self._session_comm_path = ""    # communication.jsonl
        self._session_behavior_path = ""  # behavior.jsonl
        self._session_active = False
        self._file_lock = threading.Lock()
        self._init_file()
        self._initialized = True

    # ═══════════════════════════════════════════
    # 文件持久化
    # ═══════════════════════════════════════════

    def _init_file(self):
        if not self._log_dir:
            return
        os.makedirs(self._log_dir, exist_ok=True)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def start_session(self, scene_name: str):
        """开始新的仿真会话 — 创建 {场景名}_{时间戳}/ 文件夹"""
        ts = datetime.now(_LOG_TZ).strftime("%Y%m%d_%H%M%S_%f")
        safe_name = scene_name.replace("/", "_").replace("\\", "_").replace(" ", "_")
        self._session_id = f"{safe_name}_{ts}"
        self._session_dir = os.path.join(self._log_dir, self._session_id)
        os.makedirs(self._session_dir, exist_ok=True)
        self._set_session_paths()
        self._session_active = True
        self._seq = 0
        # 写入 session 元信息
        record = _base_record("INFO", "backend", "srv", "lifecycle", "session_start",
                              f"Session started: {scene_name}")
        record["session_id"] = self._session_id
        record["seq"] = self._next_seq()
        record["payload"] = {"scene_name": scene_name, "session_dir": self._session_dir}
        self._write_file(record)
        self._append_memory(record)

    def set_session_dir(self, session_dir: str):
        """复用已有 session 文件夹（供跨容器同步，由 message_bus 调用）"""
        self._session_dir = session_dir
        self._session_id = os.path.basename(session_dir)
        self._set_session_paths()
        self._session_active = True

    def _set_session_paths(self):
        self._file_path = os.path.join(self._session_dir, "global.jsonl")
        self._session_comm_path = os.path.join(self._session_dir, "communication.jsonl")
        self._session_behavior_path = os.path.join(self._session_dir, "behavior.jsonl")

    def _append_file(self, filepath: str, entry: Dict):
        if not filepath:
            return
        try:
            with self._file_lock:
                with open(filepath, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _write_file(self, record: Dict):
        """按 category 路由写入文件"""
        if not self._file_path:
            return
        # global.jsonl: 全部写入
        self._append_file(self._file_path, record)
        # communication.jsonl: 仅通信类
        if record.get("category") == "communication" and self._session_comm_path:
            self._append_file(self._session_comm_path, record)
        # behavior.jsonl: 仅 agent 行为类
        if record.get("category") == "agent_behavior" and self._session_behavior_path:
            self._append_file(self._session_behavior_path, record)

    # ═══════════════════════════════════════════
    # 核心 emit 方法 — 唯一写入入口
    # ═══════════════════════════════════════════

    def emit(self, record: Dict):
        """写入一条统一 schema 日志到内存缓冲区 + 持久化文件"""
        record["seq"] = self._next_seq()
        record["session_id"] = self._session_id
        if not record.get("timestamp"):
            record["timestamp"] = current_log_timestamp()

        self._append_memory(record)
        self._write_file(record)

    def ingest(self, record: Dict):
        """接收外部日志（前端、外部服务），直接写入文件不做转换"""
        record.setdefault("source", "external")
        record.setdefault("component", "unknown")
        record.setdefault("category", "system")
        record.setdefault("level", "INFO")
        record["seq"] = self._next_seq()
        record["session_id"] = self._session_id
        record["timestamp"] = normalize_log_timestamp(record.get("timestamp", ""))
        self._append_memory(record)
        self._write_file(record)

    def _append_memory(self, record: Dict):
        with self._lock:
            self._entries.append(record)
            self._stats["total"] += 1
            lvl = record.get("level", "INFO")
            evt = record.get("event", "")
            aid = (record.get("actor") or {}).get("id", "")
            self._stats["by_level"][lvl] = self._stats["by_level"].get(lvl, 0) + 1
            self._stats["by_event"][evt] = self._stats["by_event"].get(evt, 0) + 1
            if aid:
                self._stats["by_agent"][aid] = self._stats["by_agent"].get(aid, 0) + 1

    # ═══════════════════════════════════════════
    # 便捷方法 — 内部构造新 schema 后调用 emit
    # ═══════════════════════════════════════════

    def system(self, event: str, message: str = "", level: LogLevel = LogLevel.INFO,
               agent_id: str = "", details: Dict = None, **kw):
        """系统事件（server / message_bus）"""
        rec = _base_record(level.name, "backend", "srv", "system", event, message)
        if agent_id:
            rec["actor"] = {"id": agent_id}
        if details or kw:
            rec["payload"] = {**(details or {}), **kw}
        self.emit(rec)

    def agent_action(self, agent_id: str, action: str, result: Dict = None, **kw):
        """Agent 动作执行"""
        rec = _base_record("INFO", "agent", agent_id, "agent_behavior",
                           "agent_action", f"[{agent_id}] {action}")
        rec["actor"] = {"id": agent_id}
        rec["action"] = {"name": action, "status": "success"}
        rec["payload"] = kw
        if result:
            rec["payload"]["result"] = result
        self.emit(rec)

    def agent_decide(self, agent_id: str, prompt_snippet: str, decision: Dict = None):
        """Agent 决策"""
        rec = _base_record("INFO", "agent", agent_id, "agent_behavior",
                           "agent_decide", f"[{agent_id}] 决策: {prompt_snippet}")
        rec["actor"] = {"id": agent_id}
        rec["action"] = {"name": "decide", "status": "decided"}
        rec["payload"] = {
            "prompt_snippet": prompt_snippet,
            "decision": decision or {},
        }
        self.emit(rec)

    def agent_message(self, from_id: str, to: str, content: str, reasoning: str = "",
                      latency_ms: float = 0, status: str = "success",
                      src_ip: str = "", src_port: int = 0,
                      dst_ip: str = "", dst_port: int = 0,
                      protocol: str = "TCP/HTTP",
                      packet_len: int = 0, header_len: int = 0, payload_len: int = 0,
                      tcp_flags: str = "",
                      channel_id: str = "",
                      message_type: str = "relay",
                      talk: str = ""):
        """Agent 间通信报文 — 正文进 payload，网络层进 network"""
        rec = _base_record("INFO", "bus", "bus", "communication",
                           "agent_message", f"{from_id} → {to}")
        rec["actor"] = {"id": from_id}
        rec["target"] = {"id": to}
        rec["action"] = {"name": message_type, "status": status}
        rec["payload"] = {
            "content": content,
            "reasoning": reasoning,
        }
        rec["network"] = {
            "src_ip": src_ip, "src_port": src_port,
            "dst_ip": dst_ip, "dst_port": dst_port,
            "protocol": protocol,
            "latency_ms": round(latency_ms, 1),
            "packet_len": packet_len, "header_len": header_len, "payload_len": payload_len,
            "tcp_flags": tcp_flags,
            "channel_id": channel_id,
            "message_type": message_type,
        }
        rec["trace"]["talk"] = talk
        self.emit(rec)

    def container_event(self, agent_id: str, event: str, message: str = "", **kw):
        """容器生命周期事件"""
        rec = _base_record("INFO", "backend", "srv", "lifecycle",
                           f"container_{event}", f"[{agent_id}] {message or event}")
        rec["actor"] = {"id": agent_id}
        if kw:
            rec["payload"] = kw
        self.emit(rec)

    def event_trigger(self, turn: int, event_name: str, impact: str):
        """场景事件触发"""
        rec = _base_record("INFO", "backend", "srv", "system",
                           "event_trigger", f"Round {turn}: {event_name} — {impact}")
        rec["payload"] = {"turn": turn, "event_name": event_name, "impact": impact}
        rec["trace"]["round"] = turn
        self.emit(rec)

    def dag_step(self, step_id: str, agent_id: str, action: str, round_num: int, status: str = "started"):
        """DAG 工作流步骤"""
        rec = _base_record("INFO", "backend", "srv", "system",
                           "dag_step", f"Round {round_num}, Step {step_id}: [{agent_id}] {action} ({status})")
        rec["actor"] = {"id": agent_id}
        rec["action"] = {"name": action, "status": status}
        rec["payload"] = {"step_id": step_id}
        rec["trace"]["round"] = round_num
        self.emit(rec)

    def error(self, event: str, message: str = "", agent_id: str = "", **kw):
        """错误事件"""
        rec = _base_record("ERROR", "backend", "srv", "system", event, message)
        if agent_id:
            rec["actor"] = {"id": agent_id}
        if kw:
            rec["payload"] = kw
        self.emit(rec)

    # ═══════════════════════════════════════════
    # 查询 & 导出
    # ═══════════════════════════════════════════

    def get_entries(self, limit: int = 100) -> List[Dict]:
        with self._lock:
            return list(self._entries)[-limit:]

    def query(self, agent_id: str = None, event: str = None, level: str = None,
              keyword: str = None, limit: int = 50) -> List[Dict]:
        with self._lock:
            results = list(self._entries)
        if agent_id:
            results = [e for e in results
                       if (e.get("actor") or {}).get("id") == agent_id]
        if event:
            results = [e for e in results if e.get("event") == event]
        if level:
            results = [e for e in results if e.get("level") == level.upper()]
        if keyword:
            k = keyword.lower()
            results = [e for e in results
                       if k in (e.get("message") or "").lower()
                       or k in json.dumps(e.get("payload", {})).lower()
                       or k in json.dumps(e.get("network", {})).lower()]
        return results[-limit:]

    def get_index_stats(self) -> Dict:
        with self._lock:
            return dict(self._stats)

    def get_agent_timeline(self, agent_id: str, limit: int = 50) -> List[Dict]:
        return self.query(agent_id=agent_id, limit=limit)

    def get_message_log(self, limit: int = 50) -> List[Dict]:
        """获取 Agent 间通信报文（兼容旧 API）"""
        with self._lock:
            results = [e for e in self._entries if e.get("category") == "communication"]
        return results[-limit:]

    def export(self, fmt: str = "jsonl", limit: int = 0) -> str:
        entries = list(self._entries)[-limit:] if limit > 0 else list(self._entries)
        if fmt == "json":
            return json.dumps(entries, ensure_ascii=False, indent=2)
        elif fmt == "csv":
            import io
            import csv
            buf = io.StringIO()
            fieldnames = ["timestamp", "seq", "session_id", "level", "source",
                          "component", "category", "event", "message"]
            writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            for d in entries:
                row = {k: d.get(k, "") for k in fieldnames}
                row["payload"] = json.dumps(d.get("payload", {}), ensure_ascii=False)
                row["network"] = json.dumps(d.get("network", {}), ensure_ascii=False)
                writer.writerow(row)
            return buf.getvalue()
        else:
            return "\n".join(json.dumps(d, ensure_ascii=False) for d in entries)

    def export_file(self, filepath: str, fmt: str = "jsonl", limit: int = 0):
        content = self.export(fmt=fmt, limit=limit)
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return filepath

    def list_log_files(self) -> List[Dict]:
        if not self._log_dir or not os.path.isdir(self._log_dir):
            return []
        sessions = []
        for entry in sorted(os.listdir(self._log_dir), reverse=True):
            entry_path = os.path.join(self._log_dir, entry)
            if os.path.isdir(entry_path):
                files = []
                for f in sorted(os.listdir(entry_path)):
                    if f.endswith(".jsonl"):
                        fp = os.path.join(entry_path, f)
                        files.append({
                            "name": f, "size_bytes": os.path.getsize(fp), "path": fp,
                        })
                if files:
                    sessions.append({
                        "session": entry, "path": entry_path, "files": files,
                    })
            elif entry.endswith(".jsonl"):
                sessions.append({
                    "session": None, "path": None,
                    "files": [{
                        "name": entry, "size_bytes": os.path.getsize(entry_path), "path": entry_path,
                    }],
                })
        return sessions

    def reset(self):
        with self._lock:
            self._entries.clear()
            self._stats = {
                "total": 0, "by_level": {}, "by_event": {}, "by_agent": {},
                "start_time": current_log_timestamp(timespec="seconds"),
            }
        self._seq = 0
        self._session_id = ""
        return self


# ── 全局实例 ──
_logger = SimulationLogger("AgentNetwork")
system_log = _logger.system
agent_log = _logger.agent_action
message_log = _logger.agent_message
get_logger = lambda: _logger
