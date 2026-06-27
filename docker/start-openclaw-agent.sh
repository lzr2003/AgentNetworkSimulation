#!/usr/bin/env sh
set -eu

: "${OPENCLAW_START_GATEWAY:=1}"
: "${OPENCLAW_GATEWAY_HOST:=127.0.0.1}"
: "${OPENCLAW_GATEWAY_PORT:=18789}"
: "${OPENCLAW_GATEWAY_WS_URL:=ws://${OPENCLAW_GATEWAY_HOST}:${OPENCLAW_GATEWAY_PORT}/gateway}"
: "${OPENCLAW_GATEWAY_READY_TIMEOUT:=60}"
: "${AGENT_STRICT_BACKEND_SDK:=1}"

export OPENCLAW_GATEWAY_WS_URL

gateway_pid=""

echo "[openclaw-agent] AGENT_STRICT_BACKEND_SDK=${AGENT_STRICT_BACKEND_SDK}"
echo "[openclaw-agent] OPENCLAW_GATEWAY_WS_URL=${OPENCLAW_GATEWAY_WS_URL}"

fail() {
    echo "[openclaw-agent] ERROR: $*" >&2
    exit 1
}

validate_openclaw_sdk() {
    if [ "${MOCK_LLM:-0}" = "1" ]; then
        echo "[openclaw-agent] MOCK_LLM=1; skipping strict openclaw-sdk import check."
        return 0
    fi

    python3 - <<'PY'
import sys

try:
    from openclaw_sdk import OpenClawClient  # noqa: F401
except Exception as exc:
    print(
        "[openclaw-agent] ERROR: openclaw-sdk is not importable. "
        "Install vendor/python/openclaw_sdk-*.whl and rebuild the image. "
        f"Original error: {exc}",
        file=sys.stderr,
    )
    sys.exit(1)

print("[openclaw-agent] openclaw-sdk import ok")
PY
}

wait_for_gateway_port() {
    python3 - <<'PY'
import os
import socket
import sys
import time

host = os.environ.get("OPENCLAW_GATEWAY_HOST", "127.0.0.1")
port = int(os.environ.get("OPENCLAW_GATEWAY_PORT", "18789"))
timeout = int(os.environ.get("OPENCLAW_GATEWAY_READY_TIMEOUT", "60"))
deadline = time.time() + timeout

while time.time() < deadline:
    try:
        with socket.create_connection((host, port), timeout=1):
            print("[openclaw-agent] gateway port is open")
            sys.exit(0)
    except OSError:
        time.sleep(1)

print(f"[openclaw-agent] gateway did not become ready at {host}:{port}", file=sys.stderr)
sys.exit(1)
PY
}

term_children() {
    if [ -n "${gateway_pid:-}" ]; then
        kill "${gateway_pid}" 2>/dev/null || true
    fi
}
trap term_children INT TERM EXIT

validate_openclaw_sdk

if [ "${OPENCLAW_START_GATEWAY}" = "1" ]; then
    gateway_cmd=""
    if [ -n "${OPENCLAW_GATEWAY_CMD:-}" ]; then
        gateway_cmd="${OPENCLAW_GATEWAY_CMD}"
    elif command -v openclaw >/dev/null 2>&1; then
        gateway_cmd="openclaw gateway --host ${OPENCLAW_GATEWAY_HOST} --port ${OPENCLAW_GATEWAY_PORT}"
    fi

    if [ -z "${gateway_cmd}" ]; then
        fail "OPENCLAW_START_GATEWAY=1 but no OpenCLAW gateway command was found. Install the gateway runtime into this image or set OPENCLAW_GATEWAY_CMD."
    fi

    echo "[openclaw-agent] starting OpenCLAW gateway: ${gateway_cmd}"
    sh -c "${gateway_cmd}" &
    gateway_pid="$!"

    echo "[openclaw-agent] waiting for gateway on ${OPENCLAW_GATEWAY_HOST}:${OPENCLAW_GATEWAY_PORT} ..."
    if ! wait_for_gateway_port; then
        term_children
        fail "OpenCLAW gateway did not become ready before timeout."
    fi
else
    echo "[openclaw-agent] OPENCLAW_START_GATEWAY=0; using configured gateway URL only."
fi

echo "[openclaw-agent] starting AgentNetwork server..."
exec python3 services/agent_server.py
