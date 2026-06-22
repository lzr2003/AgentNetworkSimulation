"""
外部 LLM API 调用元数据记录 — 不记录 prompt/response 正文，只记录元数据。

写入 agent_application 应用层日志，由 LOG_LLM_API=1 开关控制。

用法:
  from agent_network.llm_metrics import log_llm_call, log_llm_cli

  # HTTP / SDK 调用
  log_llm_call(provider="deepseek", model="deepseek-chat", method="POST",
               path="/v1/chat/completions", status="200", latency_ms=1234.5,
               prompt_chars=3500, response_chars=800, max_tokens=512,
               messages_count=1, component="ag-b1")

  # Claude CLI 调用
  log_llm_cli(exit_code=0, latency_ms=2345.6, component="ag-c1")
"""

import os
import json
import time
import threading
from datetime import datetime
from typing import Optional, Dict, Any


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _send(record: dict):
    """异步发送到 SERVER_URL/api/logs/ingest"""
    server_url = os.environ.get("SERVER_URL", "http://srv:8000")
    try:
        import requests as _r
        _r.post(f"{server_url}/api/logs/ingest", json=record, timeout=2)
    except Exception:
        pass


def llm_api_enabled() -> bool:
    """LLM 应用层日志开关；LOG_TRAFFIC 仅作为旧配置兼容。"""
    return os.environ.get("LOG_LLM_API", os.environ.get("LOG_TRAFFIC", "0")) == "1"


def _infer_source(component: str, actor_id: str = "") -> str:
    if actor_id:
        return "agent"
    if "bus" in component:
        return "bus"
    if component in ("srv", "server", "backend"):
        return "backend"
    return "agent" if component not in ("unknown", "") else "backend"


def _actor(actor_id: str = "", actor_name: str = "") -> dict:
    if not actor_id:
        return {}
    actor = {"id": actor_id}
    if actor_name:
        actor["name"] = actor_name
    return actor


def _usage_number(usage: Dict[str, Any], key: str):
    value = usage.get(key)
    return value if isinstance(value, (int, float)) else None


def _usage_nested_number(usage: Dict[str, Any], parent: str, key: str):
    nested = usage.get(parent)
    if isinstance(nested, dict):
        value = nested.get(key)
        return value if isinstance(value, (int, float)) else None
    return None


def _first_number(*values):
    for value in values:
        if value is not None:
            return value
    return None


def log_llm_call(*,
                 provider: str = "",
                 model: str = "",
                 method: str = "POST",
                 path: str = "",
                 host: str = "",
                 status: str = "200",
                 latency_ms: float = 0,
                 prompt_chars: int = 0,
                 response_chars: int = 0,
                 max_tokens: int = 0,
                 messages_count: int = 0,
                 component: str = "unknown",
                 actor_id: str = "",
                 actor_name: str = "",
                 usage: Optional[Dict[str, Any]] = None,
                 error: str = ""):
    """记录一次外部 LLM HTTP/SDK 调用的元数据"""
    if not llm_api_enabled():
        return

    usage = usage or {}
    prompt_cache_hit = _first_number(
        _usage_number(usage, "prompt_cache_hit_tokens"),
        _usage_nested_number(usage, "prompt_tokens_details", "cached_tokens"),
        _usage_number(usage, "cache_read_input_tokens"),
    )
    prompt_cache_miss = _usage_number(usage, "prompt_cache_miss_tokens")
    cache_hit_ratio = None
    if isinstance(prompt_cache_hit, (int, float)) and isinstance(prompt_cache_miss, (int, float)):
        cache_total = prompt_cache_hit + prompt_cache_miss
        if cache_total > 0:
            cache_hit_ratio = round(prompt_cache_hit / cache_total, 4)

    payload = {
        "prompt_chars": prompt_chars,
        "messages_count": messages_count,
        "max_tokens": max_tokens,
        "response_chars": response_chars,
        "error_type": error[:200] if error else "",
        "body_logged": False,
        "estimated": True,
    }
    for key in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens",
                "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
                "cache_read_input_tokens", "cache_creation_input_tokens"):
        if usage.get(key) is not None:
            payload[key] = usage[key]
            payload["estimated"] = False
    for key in ("prompt_tokens_details", "completion_tokens_details"):
        if isinstance(usage.get(key), dict):
            payload[key] = usage[key]
            payload["estimated"] = False
    if cache_hit_ratio is not None:
        payload["cache_hit_ratio"] = cache_hit_ratio

    record = {
        "timestamp": _now_iso(),
        "level": "ERROR" if error or (status and not status.startswith("2")) else "INFO",
        "source": _infer_source(component, actor_id),
        "component": component,
        "category": "agent_application",
        "layer": "agent_application",
        "event": "llm_api_call",
        "actor": _actor(actor_id, actor_name),
        "target": {
            "provider": provider,
            "model": model,
            "host": host,
            "path": path,
        },
        "action": {"name": method, "status": f"error:{error}" if error else status},
        "message": f"LLM {method} {provider}/{model} → {status} {latency_ms:.0f}ms",
        "payload": payload,
        "network": {
            "direction": "outbound",
            "latency_ms": round(latency_ms, 1),
            "external": True,
        },
        "trace": {},
    }
    threading.Thread(target=_send, args=(record,), daemon=True).start()


def log_llm_cli(*,
                exit_code: int = 0,
                latency_ms: float = 0,
                component: str = "unknown",
                actor_id: str = "",
                actor_name: str = "",
                error: str = ""):
    """记录一次 Claude CLI 调用的元数据（不记录 prompt/response）"""
    if not llm_api_enabled():
        return

    record = {
        "timestamp": _now_iso(),
        "level": "ERROR" if exit_code != 0 else "INFO",
        "source": _infer_source(component, actor_id),
        "component": component,
        "category": "agent_application",
        "layer": "agent_application",
        "event": "llm_cli_call",
        "actor": _actor(actor_id, actor_name),
        "target": {
            "provider": "anthropic",
            "method": "cli",
        },
        "action": {"name": "CLI", "status": f"exit_{exit_code}" if exit_code == 0 else f"exit_{exit_code}:{error[:80]}"},
        "message": f"LLM CLI claude → exit {exit_code} {latency_ms:.0f}ms",
        "payload": {
            "error_type": error[:200] if error else "",
            "body_logged": False,
        },
        "network": {
            "direction": "outbound",
            "latency_ms": round(latency_ms, 1),
            "external": True,
        },
        "trace": {},
    }
    threading.Thread(target=_send, args=(record,), daemon=True).start()


# ── 上下文管理器：包装 LLM 调用并自动记录 ──

class LLMCallTracker:
    """包装一次 LLM 调用，自动计时并记录元数据"""

    def __init__(self, provider: str, model: str, method: str = "POST",
                 path: str = "", host: str = "", component: str = "unknown",
                 prompt_chars: int = 0, messages_count: int = 0, max_tokens: int = 0,
                 actor_id: str = "", actor_name: str = ""):
        self.provider = provider
        self.model = model
        self.method = method
        self.path = path
        self.host = host
        self.component = component
        self.actor_id = actor_id
        self.actor_name = actor_name
        self.prompt_chars = prompt_chars
        self.messages_count = messages_count
        self.max_tokens = max_tokens
        self.start = 0.0

    def __enter__(self):
        self.start = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        latency_ms = (time.time() - self.start) * 1000
        if exc_type:
            log_llm_call(
                provider=self.provider, model=self.model,
                method=self.method, path=self.path, host=self.host,
                status="0", latency_ms=latency_ms,
                prompt_chars=self.prompt_chars, max_tokens=self.max_tokens,
                messages_count=self.messages_count, component=self.component,
                actor_id=self.actor_id, actor_name=self.actor_name,
                error=f"{exc_type.__name__}: {exc_val}",
            )
        return False  # 不吞异常

    def ok(self, response_chars: int = 0, status: str = "200",
           usage: Optional[Dict[str, Any]] = None):
        latency_ms = (time.time() - self.start) * 1000
        log_llm_call(
            provider=self.provider, model=self.model,
            method=self.method, path=self.path, host=self.host,
            status=status, latency_ms=latency_ms,
            prompt_chars=self.prompt_chars, response_chars=response_chars,
            max_tokens=self.max_tokens, messages_count=self.messages_count,
            component=self.component,
            actor_id=self.actor_id, actor_name=self.actor_name,
            usage=usage,
        )
