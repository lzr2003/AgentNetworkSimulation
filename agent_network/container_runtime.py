"""
Agent 容器运行时 — 预创建池 + 动态扩容

策略:
1. 运行场景时，优先从预创建池中分配空闲容器
2. 池中某类型容器耗尽 → 自动创建新容器
3. 容器不再销毁，持续复用
"""

import os
import json
import time
import requests
from typing import Dict, List, Any, Optional, Set
from dataclasses import dataclass, field


@dataclass
class ContainerAgent:
    agent_id: str
    name: str
    role: str
    container_id: str = ""
    container_name: str = ""
    port: int = 8000
    status: str = "created"
    url: str = ""

    def to_dict(self):
        return {
            "agent_id": self.agent_id, "name": self.name, "role": self.role,
            "container_id": self.container_id, "port": self.port,
            "status": self.status, "url": self.url,
        }


class ContainerRuntime:
    """Agent 容器管理 — 分配 + 动态创建"""

    BACKEND_CONFIG = {
        "brain":       {"image": "agentnetwork-ag-b1",   "cmd": "python agent_server.py",   "prefix": "ag-b"},
        "claude-code": {"image": "agentnetwork-ag-c1",   "cmd": "python3 agent_server.py",  "prefix": "ag-c"},
        "openclaw":    {"image": "agentnetwork-ag-o1",   "cmd": "python agent_server.py",   "prefix": "ag-o"},
    }
    DEFAULT_BACKEND = "brain"
    NETWORK_NAME = "an"
    INTERNAL_PORT = 8000

    def __init__(self, message_bus_url: str = "http://message-bus:9000"):
        self.message_bus_url = message_bus_url
        self.agents: Dict[str, ContainerAgent] = {}
        self._docker_client = None
        self._used_containers: Set[str] = set()  # 当前仿真已分配的容器名
        self._init_docker()

    def _init_docker(self):
        try:
            import docker
            for _ in range(2):
                try:
                    self._docker_client = docker.from_env()
                    self._docker_client.ping()
                    print("[Runtime] Docker OK")
                    return
                except Exception:
                    time.sleep(1)
        except ImportError:
            pass
        print("[Runtime] No Docker SDK, container discovery only")

    def _get_running_containers(self, backend: str) -> List[str]:
        """获取某类型的所有运行中容器名"""
        cfg = self.BACKEND_CONFIG.get(backend, self.BACKEND_CONFIG[self.DEFAULT_BACKEND])
        prefix = cfg["prefix"]
        containers = []
        if self._docker_client:
            try:
                for c in self._docker_client.containers.list():
                    if c.name.startswith(prefix):
                        containers.append(c.name)
            except Exception:
                pass
        return sorted(containers)

    def _get_or_create_container(self, backend: str) -> str:
        """获取空闲容器名，无则动态创建"""
        cfg = self.BACKEND_CONFIG.get(backend, self.BACKEND_CONFIG[self.DEFAULT_BACKEND])
        prefix = cfg["prefix"]

        # 1. 列出运行中容器
        running = self._get_running_containers(backend)

        # 2. 找第一个未分配的
        for name in running:
            if name not in self._used_containers:
                self._used_containers.add(name)
                print(f"[Runtime] Assign {name} (from pool)")
                return name

        # 3. 池耗尽 → 动态创建
        if self._docker_client:
            prefix = cfg["prefix"]
            auto_n = len([c for c in self._used_containers if c.startswith(prefix)]) + 10  # start from 10
            auto_name = f"{prefix}{auto_n}"
            try:
                # 清理同名旧容器
                try:
                    old = self._docker_client.containers.get(auto_name)
                    old.remove(force=True)
                except Exception:
                    pass
                img = cfg["image"]
                cmd = cfg["cmd"]
                env = {
                    "AGENT_ID": auto_name,
                    "AGENT_NAME": auto_name,
                    "AGENT_ROLE": backend,
                    "AGENT_BACKEND": backend,
                    "PORT": str(self.INTERNAL_PORT),
                    "MESSAGE_BUS_URL": self.message_bus_url,
                    "SERVER_URL": os.environ.get("SERVER_URL", "http://srv:8000"),
                    "LOG_TRAFFIC": os.environ.get("LOG_TRAFFIC", "1"),
                }
                for key in ("LLM_API_KEY", "LLM_MODEL", "LLM_API_BASE", "LLM_PROVIDER", "ANTHROPIC_API_KEY"):
                    if os.environ.get(key):
                        env[key] = os.environ[key]

                c = self._docker_client.containers.run(
                    img, name=auto_name, detach=True, command=cmd,
                    environment=env, network=self.NETWORK_NAME,
                )
                self._used_containers.add(auto_name)
                print(f"[Runtime] Created {auto_name} ({backend}) container={c.id[:12]}")
                return auto_name
            except Exception as e:
                print(f"[Runtime] Dynamic create failed for {backend}: {e}")
                # 不再回退复用已分配容器（会导致多 Agent 共享同一进程，状态覆盖）
                raise RuntimeError(
                    f"Pool exhausted for backend '{backend}': {len(running)} pool containers, "
                    f"{len([c for c in self._used_containers if c.startswith(prefix)])} already assigned. "
                    f"Dynamic creation failed: {e}"
                )

        # 4. 无 Docker SDK 且池已耗尽
        raise RuntimeError(
            f"No {backend} containers available and Docker SDK unavailable. "
            f"Pool size: {len(running)}, all {len(self._used_containers)} in use."
        )

    def assign_agent(self, agent_id: str, role: str, name: str, extra_meta: Dict = None) -> ContainerAgent:
        """从池中分配容器给场景 Agent"""
        backend = (extra_meta or {}).get("backend", self.DEFAULT_BACKEND)
        if backend not in self.BACKEND_CONFIG:
            backend = self.DEFAULT_BACKEND

        port = self.INTERNAL_PORT
        assign_error = None
        try:
            container_name = self._get_or_create_container(backend)
            url = f"http://{container_name}:{port}"
            status = "running"
        except RuntimeError as exc:
            container_name = ""
            url = ""
            status = "error"
            assign_error = str(exc)
            print(f"[Runtime] assign_agent failed for {agent_id} ({name}): {exc}")

        ca = ContainerAgent(
            agent_id=agent_id, name=name, role=role,
            container_name=container_name, port=port,
            url=url, status=status,
        )
        ca._extra_meta = extra_meta or {}
        ca._assign_error = assign_error
        self.agents[agent_id] = ca

        return ca

    def run_round(self, context: Dict = None) -> Dict:
        decisions = self.decide_all(context)
        actions = self.act_all()
        return {"decisions": decisions, "actions": actions}

    def decide_all(self, context: Dict = None) -> List[Dict]:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        results = []
        agents_list = list(self.agents.values())
        def _decide(ca):
            try:
                ctx = dict(context or {})
                ctx["agent_id"] = ca.agent_id
                ctx["agent_name"] = ca.name
                ctx["agent_role"] = ca.role
                # 注入场景技能列表
                em = getattr(ca, '_extra_meta', {}) or {}
                if em.get("skills_list"): ctx["skills_list"] = em["skills_list"]
                if em.get("core_goal"): ctx["core_goal"] = em["core_goal"]
                if em.get("hidden_secret"): ctx["hidden_secret"] = em["hidden_secret"]
                if em.get("action_space"): ctx["action_space"] = em["action_space"]
                if em.get("background_rules"): ctx["background_rules"] = em["background_rules"]
                resp = requests.post(f"{ca.url}/decide", json={"context": ctx}, timeout=60)
                return resp.json()
            except Exception as e:
                return {"agent_id": ca.agent_id, "error": str(e)}
        with ThreadPoolExecutor(max_workers=min(10, len(agents_list))) as pool:
            futures = {pool.submit(_decide, ca): ca for ca in agents_list}
            for f in as_completed(futures):
                results.append(f.result())
        return results

    def act_all(self) -> List[Dict]:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        results = []
        agents_list = list(self.agents.values())
        def _act(ca):
            try:
                resp = requests.post(f"{ca.url}/act", timeout=60)
                return resp.json()
            except Exception as e:
                return {"agent_id": ca.agent_id, "error": str(e)}
        with ThreadPoolExecutor(max_workers=min(10, len(agents_list))) as pool:
            futures = {pool.submit(_act, ca): ca for ca in agents_list}
            for f in as_completed(futures):
                results.append(f.result())
        return results

    def stop_all(self):
        self.agents.clear()

    def reset(self):
        self.stop_all()
        self._used_containers.clear()
        # 清理上一轮仿真动态创建的残留容器（避免复用旧镜像）
        self._cleanup_orphan_containers()

    def _cleanup_orphan_containers(self):
        """移除所有动态创建的 Agent 容器（前缀匹配但不在池容器列表中的）"""
        if not self._docker_client:
            return
        pool_names = set()
        for cfg in self.BACKEND_CONFIG.values():
            for i in range(1, 10):  # 池容器: ag-b1~9, ag-c1~9, ag-o1~9 (range(1,10) 保留1~9)
                pool_names.add(f"{cfg['prefix']}{i}")
        try:
            for c in self._docker_client.containers.list(all=True):
                for prefix in [cfg["prefix"] for cfg in self.BACKEND_CONFIG.values()]:
                    if c.name.startswith(prefix) and c.name not in pool_names:
                        try:
                            c.remove(force=True)
                        except Exception:
                            pass
                        break
        except Exception:
            pass


_runtime: Optional[ContainerRuntime] = None


def get_runtime() -> ContainerRuntime:
    global _runtime
    if _runtime is None:
        bus_url = os.environ.get("MESSAGE_BUS_URL", "http://message-bus:9000")
        _runtime = ContainerRuntime(message_bus_url=bus_url)
    return _runtime
