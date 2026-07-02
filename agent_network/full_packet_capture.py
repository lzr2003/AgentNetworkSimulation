import os
import signal
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

_capture_process: Optional[subprocess.Popen] = None
_capture_lock = threading.Lock()


def _safe(value: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in str(value or "")) or "unknown"


def start_full_capture(
    agent_id: str,
    session_id: str = "",
    pcap_dir: str = "/app/data/pcap",
    interface: str = "any",
):
    """Start full tcpdump capture for the Agent container.

    This intentionally captures everything visible inside the container network
    namespace: Agent-to-Agent HTTP, LLM API, DNS, external websites, srv access,
    and any other TCP/UDP/IP traffic. PacketRecorder simulation is not used.
    """
    global _capture_process

    if os.environ.get("LOG_FULL_PCAP", "1") != "1":
        return {"status": "disabled", "reason": "LOG_FULL_PCAP!=1"}

    with _capture_lock:
        if _capture_process and _capture_process.poll() is None:
            return {"status": "running", "pid": _capture_process.pid}

        agent_id = _safe(agent_id or os.environ.get("AGENT_ID", "agent"))
        session_id = _safe(session_id or datetime.now().strftime("%Y%m%d_%H%M%S"))
        out_dir = Path(pcap_dir) / session_id
        out_dir.mkdir(parents=True, exist_ok=True)
        pcap_path = out_dir / f"{agent_id}.pcap"

        cmd = ["tcpdump", "-i", interface, "-nn", "-s", "0", "-w", str(pcap_path)]
        _capture_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        return {
            "status": "started",
            "agent_id": agent_id,
            "session_id": session_id,
            "pcap_path": str(pcap_path),
            "interface": interface,
            "pid": _capture_process.pid,
            "cmd": " ".join(cmd),
        }


def stop_full_capture():
    global _capture_process

    with _capture_lock:
        if not _capture_process:
            return {"status": "not_running"}

        if _capture_process.poll() is None:
            _capture_process.send_signal(signal.SIGTERM)
            try:
                _capture_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _capture_process.kill()

        rc = _capture_process.returncode
        _capture_process = None
        return {"status": "stopped", "returncode": rc}
