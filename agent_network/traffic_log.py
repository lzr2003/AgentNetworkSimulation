"""
Docker 流量监管 — 记录容器间 HTTP 流量的完整正文和元数据，写入 global.jsonl。

不做 MITM 解密，外部 HTTPS 只记录元数据。

用法（入站）:
  from agent_network.traffic_log import TrafficMiddleware
  app.add_middleware(TrafficMiddleware, component="ag-c1", server_url="http://srv:8000")

用法（出站）:
  from agent_network.traffic_log import traffic_post_json
  traffic_post_json("http://bus:9000/relay", {...}, component="ag-c1",
                     server_url="http://srv:8000", direction="outbound",
                     target_path="/relay", target_method="POST")
"""

import json
import time
import os
from datetime import datetime
from typing import Optional, Dict, Any, Tuple
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# ── 敏感字段脱敏 ──

SENSITIVE_KEYS = {
    "authorization", "x-api-key", "api-key", "api_key", "token",
    "password", "secret", "x-auth-token", "cookie",
    "set-cookie", "anthropic-api-key", "llm-api-key",
    "key",  # 泛化：JSON 中的 "key" 字段可能含凭证
}

EXCLUDED_PATHS = {
    "/api/logs/ingest",  # 避免递归
    "/api/logs/agent",    # 日志上报自身不计入流量
    "/api/minesweeper",   # UI 轮询，非容器间通信
    "/ws",                # WebSocket 不记录
    "/static",            # 静态资源
    "/health",            # 健康检查
}

MAX_BODY_BYTES = 64 * 1024  # 单条正文最大 64KB


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _sanitize_headers(headers: dict) -> dict:
    """脱敏敏感 header 字段"""
    result = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl in SENSITIVE_KEYS:
            result[k] = "***REDACTED***"
        else:
            result[k] = v
    return result


def _sanitize_body(obj: Any, depth: int = 0) -> Any:
    """递归脱敏 JSON body 中的敏感字段（api_key, token, password, secret 等）"""
    if depth > 10:
        return obj
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            kl = k.lower()
            if kl in SENSITIVE_KEYS or any(s in kl for s in ("secret", "password", "token")):
                result[k] = "***REDACTED***"
            elif isinstance(v, (dict, list)):
                result[k] = _sanitize_body(v, depth + 1)
            else:
                result[k] = v
        return result
    if isinstance(obj, list):
        return [_sanitize_body(item, depth + 1) for item in obj]
    return obj


def _should_skip(path: str) -> bool:
    """排除递归路径和静态资源"""
    for ex in EXCLUDED_PATHS:
        if path.startswith(ex):
            return True
    return False


def _parse_body(body: bytes, content_type: str) -> Any:
    """尝试解析 body 为 JSON，失败则返回截断字符串"""
    if not body:
        return ""
    if len(body) > MAX_BODY_BYTES:
        body = body[:MAX_BODY_BYTES]
    ct_lower = (content_type or "").lower()
    if "application/json" in ct_lower:
        try:
            return json.loads(body.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    try:
        text = body.decode("utf-8", errors="replace")
        return text[:MAX_BODY_BYTES]
    except Exception:
        return f"<binary {len(body)} bytes>"


# ═══════════════════════════════════════════
# 入站 middleware
# ═══════════════════════════════════════════

class TrafficMiddleware(BaseHTTPMiddleware):
    """
    捕获入站 HTTP 流量。每个请求/响应对写入一条 v2 schema 日志到 global.jsonl。
    """

    def __init__(self, app, component: str = "unknown", server_url: str = ""):
        super().__init__(app)
        self.component = component
        self.server_url = server_url
        self._logged_ingest = False

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if _should_skip(path):
            return await call_next(request)

        start = time.time()
        req_body = await request.body()

        # 读取请求体替身以便后续消费
        async def receive():
            return {"type": "http.request", "body": req_body}

        request._receive = receive

        response: Response = await call_next(request)
        latency_ms = (time.time() - start) * 1000

        # 异步写入（fire-and-forget，延迟≤1ms）
        import threading
        threading.Thread(
            target=_send_traffic_record,
            args=(self.server_url, self.component, "inbound",
                  request.method, path, response.status_code, latency_ms,
                  req_body, response.body if hasattr(response, 'body') else b"",
                  dict(request.headers), dict(response.headers)),
            daemon=True,
        ).start()
        return response


def _send_traffic_record(server_url: str, component: str, direction: str,
                          method: str, path: str, status_code: int,
                          latency_ms: float, req_body: bytes, resp_body: bytes,
                          req_headers: dict, resp_headers: dict):
    """通过网络发给 srv 的 /api/logs/ingest（不直接调 logger 避免依赖）"""
    content_type = resp_headers.get("content-type", "")
    req_ct = req_headers.get("content-type", "")
    parsed_req = _parse_body(req_body, req_ct)
    parsed_resp = _parse_body(resp_body, content_type)
    # 递归脱敏 JSON body
    if isinstance(parsed_req, dict):
        parsed_req = _sanitize_body(parsed_req)
    if isinstance(parsed_resp, dict):
        parsed_resp = _sanitize_body(parsed_resp)
    record = {
        "timestamp": _now_iso(),
        "level": ("ERROR" if status_code >= 500 else "INFO"),
        "source": "bus" if "bus" in component else "agent" if "ag-" in component else "backend",
        "component": component,
        "category": "communication",
        "event": f"docker_http_{direction}",
        "actor": {},
        "target": {},
        "action": {"name": method, "status": str(status_code)},
        "message": f"{direction.upper()} {method} {path} → {status_code} {latency_ms:.1f}ms",
        "payload": {
            "request": {
                "method": method, "path": path,
                "headers": _sanitize_headers(req_headers),
                "body": parsed_req,
            },
            "response": {
                "status": status_code,
                "headers": _sanitize_headers(resp_headers),
                "body": parsed_resp,
            },
        },
        "network": {
            "direction": direction,
            "latency_ms": round(latency_ms, 1),
            "request_bytes": len(req_body),
            "response_bytes": len(resp_body),
            "component": component,
        },
        "trace": {},
    }
    try:
        import requests as _r
        _r.post(f"{server_url}/api/logs/ingest", json=record, timeout=1)
    except Exception:
        pass


# ═══════════════════════════════════════════
# 出站包装
# ═══════════════════════════════════════════

def traffic_post_json(url: str, json_data: dict, *,
                      component: str = "unknown",
                      server_url: str = "",
                      timeout: float = 10,
                      **kwargs) -> Tuple[bool, Optional[dict]]:
    """
    发送 JSON POST 请求，同时记录出站流量日志。

    返回 (success, response_json_or_None)。
    """
    import requests as _r
    start = time.time()
    resp = None
    error = None
    try:
        resp = _r.post(url, json=json_data, timeout=timeout, **kwargs)
        resp_json = resp.json() if resp.ok else None
    except Exception as e:
        error = str(e)
        resp_json = None

    latency_ms = (time.time() - start) * 1000
    status_code = resp.status_code if resp is not None else 0
    resp_body = resp.content if resp is not None else b""
    resp_headers = dict(resp.headers) if resp is not None else {}
    resp_text = ""
    if resp_body:
        try:
            resp_text = resp_body.decode("utf-8", errors="replace")[:2000]
        except Exception:
            resp_text = f"<{len(resp_body)} bytes>"

    # 从 URL 提取路径
    from urllib.parse import urlparse
    parsed = urlparse(url)
    path = parsed.path or "/"

    record = {
        "timestamp": _now_iso(),
        "level": "ERROR" if error or status_code >= 500 else "INFO",
        "source": "bus" if "bus" in component else "agent" if "ag-" in component else "backend",
        "component": component,
        "category": "communication",
        "event": "docker_http_outbound",
        "actor": {},
        "target": {"host": parsed.netloc},
        "action": {"name": "POST", "status": f"error:{error}" if error else str(status_code)},
        "message": f"OUT POST {path} → {status_code} {latency_ms:.1f}ms{f' ERROR:{error}' if error else ''}",
        "payload": {
            "request": {
                "method": "POST", "path": path, "url": url,
                "body": _sanitize_body(json_data) if isinstance(json_data, dict) else json_data,
            },
            "response": {
                "status": status_code,
                "headers": _sanitize_headers(resp_headers),
                "body": resp_text[:2000] if resp_text else "",
                "error": error,
            },
        },
        "network": {
            "direction": "outbound",
            "latency_ms": round(latency_ms, 1),
            "request_bytes": len(json.dumps(json_data, ensure_ascii=False).encode()) if isinstance(json_data, dict) else 0,
            "response_bytes": len(resp_body),
            "target_host": parsed.netloc,
        },
        "trace": {},
    }
    # 异步发送
    if server_url:
        import threading
        def _send():
            try:
                _r.post(f"{server_url}/api/logs/ingest", json=record, timeout=1)
            except Exception:
                pass
        threading.Thread(target=_send, daemon=True).start()

    return (resp is not None and resp.ok), resp_json


def traffic_requests_post(url: str, *, component: str = "unknown",
                          server_url: str = "", json_data: dict = None,
                          timeout: float = 10, **kwargs):
    """
    requests.post 的包装器，自动记录出站流量日志。
    用法与 requests.post 相同，但 json= 参数改为 json_data=。

    返回 requests.Response 对象（成功）或 None（异常）。
    """
    if not traffic_enabled() or not server_url:
        try:
            return requests_post_fallback(url, json=json_data, timeout=timeout, **kwargs)
        except Exception:
            return None
    ok, resp_json = traffic_post_json(url, json_data,
                                      component=component, server_url=server_url,
                                      timeout=timeout, **kwargs)
    if ok:
        # 构造一个模拟 Response 对象
        class _FakeResp:
            def __init__(self, data, status):
                self._json = data
                self.status_code = status
                self.ok = True
            def json(self): return self._json
        return _FakeResp(resp_json, 200)
    return None


def requests_post_fallback(url: str, **kwargs):
    """普通 requests.post，不做流量记录"""
    import requests as _r
    return _r.post(url, **kwargs)


# ── 环境感知 ──
def traffic_enabled() -> bool:
    return os.environ.get("LOG_TRAFFIC", "0") == "1"
