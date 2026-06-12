"""
Agent 容器内 tcpdump 网络抓包 — 捕获外部 LLM API 访问的 TCP/TLS 元数据。

写入 global.jsonl (category: network_capture)，由 LOG_LLM_API=1 控制。
不做 HTTPS 解密，不记录 payload 明文。

用法（agent_server.py 启动时调用一次）:
  from agent_network.packet_capture import start_capture, stop_capture
  start_capture(agent_id=AGENT_ID, server_url=SERVER_URL)
  # ... Agent 运行 ...
  stop_capture()
"""

import os
import re
import json
import time
import socket
import subprocess
import threading
from datetime import datetime
from typing import Optional, Dict, List

# ── 配置 ──

# 内部流量排除：bus、srv 容器名和 IP 模式
INTERNAL_HOSTS = {"bus", "srv", "localhost", "127.0.0.1"}
INTERNAL_PORTS = {8000, 9000, 6379}  # Agent/srv 内网端口
# 聚合窗口：同一连接在此时间内合并为一条日志
AGGREGATION_WINDOW = 5.0  # seconds

# tcpdump 输出行正则，兼容：
#   12:34:56.123456 IP 172.18.0.2.53000 > 1.2.3.4.443: Flags [S], ... length 0
#   1718170000.123456 eth0 Out IP 172.18.0.2.53000 > 1.2.3.4.443: Flags [P.], ... length 123
TCPDUMP_LINE = re.compile(
    r'^(?P<time>\S+)\s+'
    r'(?:(?P<iface>\S+)\s+(?P<iface_dir>In|Out|in|out)\s+)?'
    r'IP\s+'
    r'(?P<src>.+?)\.(?P<src_port>\d+)\s+>\s+'
    r'(?P<dst>.+?)\.(?P<dst_port>\d+):'
    r'.*?Flags\s+\[(?P<flags>[^\]]+)\].*?length\s+(?P<length>\d+)'
)

_capture_process: Optional[subprocess.Popen] = None
_capture_thread: Optional[threading.Thread] = None
_running = False


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _resolve_llm_hosts() -> List[str]:
    """从环境变量解析 LLM API 目标 host"""
    hosts = set()
    for key in ("LLM_API_BASE", "ANTHROPIC_BASE_URL", "OPENAI_API_BASE"):
        url = os.environ.get(key, "")
        if url:
            try:
                from urllib.parse import urlparse
                parsed = urlparse(url)
                if parsed.hostname:
                    hosts.add(parsed.hostname)
            except Exception:
                pass
    # DeepSeek 默认
    if not hosts:
        hosts.add("api.deepseek.com")
    return list(hosts)


def _send_record(server_url: str, record: dict):
    """异步发送抓包记录到 /api/logs/ingest"""
    try:
        import requests as _r
        _r.post(f"{server_url}/api/logs/ingest", json=record, timeout=2)
    except Exception:
        pass


def _llm_capture_enabled() -> bool:
    """LLM 网络层抓包开关；LOG_TRAFFIC 仅作为旧配置兼容。"""
    return os.environ.get("LOG_LLM_API", os.environ.get("LOG_TRAFFIC", "0")) == "1"


def _parse_tcpdump_line(line: str) -> Optional[dict]:
    """解析一行 tcpdump 输出"""
    m = TCPDUMP_LINE.match(line.strip())
    if not m:
        return None
    gd = m.groupdict()
    return {
        "time": gd["time"],
        "interface": gd.get("iface") or "any",
        "interface_direction": (gd.get("iface_dir") or "").lower(),
        "src_ip": gd["src"],
        "src_port": int(gd["src_port"]),
        "dst_ip": gd["dst"],
        "dst_port": int(gd["dst_port"]),
        "tcp_flags": gd["flags"],
        "length": int(gd["length"]),
    }


def _is_llm_traffic(parsed: dict, llm_hosts: List[str]) -> bool:
    """判断是否为外部 LLM API 流量"""
    # 排除内部
    dst = parsed["dst_ip"]
    src = parsed["src_ip"]
    if dst in INTERNAL_HOSTS or src in INTERNAL_HOSTS:
        return False
    if dst.startswith("172.") or dst.startswith("10.") or dst.startswith("192.168."):
        return False
    if parsed["dst_port"] in INTERNAL_PORTS or parsed["src_port"] in INTERNAL_PORTS:
        return False
    # 匹配已知 LLM host
    for host in llm_hosts:
        if host in dst or host in src:
            return True
    # 外部 443/80 流量
    if parsed["dst_port"] in (443, 80) and not dst.startswith("172."):
        return True
    return False


def _flush_aggregated(agent_id: str, server_url: str, connections: dict):
    """将聚合的连接数据写入日志"""
    now = time.time()
    expired = []
    for key, data in list(connections.items()):
        if now - data["last_time"] < AGGREGATION_WINDOW:
            continue
        expired.append(key)
        record = {
            "timestamp": _now_iso(),
            "level": "INFO",
            "source": "agent",
            "component": agent_id,
            "category": "network_capture",
            "event": "llm_api_packet",
            "actor": {"id": agent_id},
            "target": {
                "host": data["host"],
                "ip": data["dst_ip"],
                "port": data["dst_port"],
            },
            "action": {"name": "CAPTURE", "status": f"{data['count']} packets"},
            "message": f"CAPTURE {agent_id} → {data['host']}:{data['dst_port']} {data['count']}pkts {data['total_bytes']}B",
            "payload": {
                "line_summary": f"{data['count']} packets, {data['total_bytes']} bytes total",
                "capture_source": "tcpdump",
                "body_logged": False,
            },
            "network": {
                "direction": "outbound",
                "protocol": "TCP/TLS",
                "src_ip": data["src_ip"],
                "src_port": data["src_port"],
                "dst_ip": data["dst_ip"],
                "dst_port": data["dst_port"],
                "tcp_flags": data.get("last_flags", ""),
                "packet_len": data["total_bytes"],
                "capture_interface": data.get("interface", "any"),
                "interface_direction": data.get("interface_direction", ""),
                "external": True,
            },
            "trace": {},
        }
        threading.Thread(target=_send_record, args=(server_url, record), daemon=True).start()
    for key in expired:
        del connections[key]


def _capture_loop(agent_id: str, server_url: str):
    """后台抓包线程 — 运行 tcpdump 并解析输出"""
    global _running, _capture_process
    llm_hosts = _resolve_llm_hosts()

    # 构建 tcpdump 命令
    # -i any: 所有接口
    # -nn: 不解析 hostname/port name，保证端口是数字
    # tcp port 443 or tcp port 80: 只抓外部 HTTPS/HTTP
    cmd = ["tcpdump", "-i", "any", "-nn", "-l",
           "tcp port 443 or tcp port 80"]

    try:
        _capture_process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
    except FileNotFoundError:
        record = {
            "timestamp": _now_iso(), "level": "WARN",
            "source": "agent", "component": agent_id,
            "category": "system", "event": "tcpdump_missing",
            "message": f"[{agent_id}] tcpdump not found, packet capture disabled",
        }
        _send_record(server_url, record)
        return
    except PermissionError:
        record = {
            "timestamp": _now_iso(), "level": "WARN",
            "source": "agent", "component": agent_id,
            "category": "system", "event": "tcpdump_permission_denied",
            "message": f"[{agent_id}] No permission for tcpdump (NET_RAW/NET_ADMIN needed)",
        }
        _send_record(server_url, record)
        return

    _running = True
    _send_record(server_url, {
        "timestamp": _now_iso(),
        "level": "INFO",
        "source": "agent",
        "component": agent_id,
        "category": "system",
        "event": "tcpdump_started",
        "message": f"[{agent_id}] tcpdump started for LLM API network capture",
        "payload": {"command": " ".join(cmd), "llm_hosts": llm_hosts},
    })
    # 聚合缓冲区：connection_key → {count, bytes, timestamps}
    connections: Dict[str, dict] = {}
    last_flush = time.time()
    parse_miss = 0

    while _running:
        line = _capture_process.stdout.readline()
        if not line and _capture_process.poll() is not None:
            break

        parsed = _parse_tcpdump_line(line)
        if not parsed:
            parse_miss += 1
            if parse_miss in (10, 100, 1000):
                _send_record(server_url, {
                    "timestamp": _now_iso(),
                    "level": "WARN",
                    "source": "agent",
                    "component": agent_id,
                    "category": "system",
                    "event": "tcpdump_parse_miss",
                    "message": f"[{agent_id}] tcpdump output parse miss x{parse_miss}",
                    "payload": {"sample": line.strip()[:300]},
                })
            continue
        if not _is_llm_traffic(parsed, llm_hosts):
            continue

        # 聚合：按 (src_ip, dst_ip, dst_port) 分组
        key = f"{parsed['src_ip']}:{parsed['dst_ip']}:{parsed['dst_port']}"
        if key not in connections:
            # 尝试 DNS 解析 host
            host = parsed["dst_ip"]
            for h in llm_hosts:
                try:
                    ips = socket.getaddrinfo(h, 443, proto=socket.IPPROTO_TCP)
                    if any(addr[4][0] == parsed["dst_ip"] for addr in ips):
                        host = h
                        break
                except Exception:
                    pass
            connections[key] = {
                "host": host,
                "src_ip": parsed["src_ip"],
                "src_port": parsed["src_port"],
                "dst_ip": parsed["dst_ip"],
                "dst_port": parsed["dst_port"],
                "interface": parsed.get("interface", "any"),
                "interface_direction": parsed.get("interface_direction", ""),
                "count": 0,
                "total_bytes": 0,
                "last_time": time.time(),
                "last_flags": "",
            }
        conn = connections[key]
        conn["count"] += 1
        conn["total_bytes"] += parsed["length"]
        conn["last_time"] = time.time()
        conn["last_flags"] = parsed["tcp_flags"]

        # 定期刷新聚合数据
        if time.time() - last_flush > AGGREGATION_WINDOW:
            _flush_aggregated(agent_id, server_url, connections)
            last_flush = time.time()

    # 退出前清空
    _flush_aggregated(agent_id, server_url, connections)
    stderr = ""
    returncode = _capture_process.poll() if _capture_process else None
    try:
        stderr = (_capture_process.stderr.read() if _capture_process and _capture_process.stderr else "") or ""
    except Exception:
        stderr = ""
    _send_record(server_url, {
        "timestamp": _now_iso(),
        "level": "WARN" if returncode not in (0, None) else "INFO",
        "source": "agent",
        "component": agent_id,
        "category": "system",
        "event": "tcpdump_exited",
        "message": f"[{agent_id}] tcpdump exited rc={returncode}",
        "payload": {"returncode": returncode, "stderr": stderr[-500:]},
    })


def start_capture(agent_id: str = "", server_url: str = "http://srv:8000"):
    """启动后台抓包（由 agent_server main 调用）"""
    global _capture_thread
    if not _llm_capture_enabled():
        return
    _capture_thread = threading.Thread(
        target=_capture_loop,
        args=(agent_id, server_url),
        daemon=True,
    )
    _capture_thread.start()


def stop_capture():
    """停止抓包"""
    global _running, _capture_process
    _running = False
    if _capture_process:
        try:
            _capture_process.terminate()
            _capture_process.wait(timeout=2)
        except Exception:
            _capture_process.kill()
        _capture_process = None
