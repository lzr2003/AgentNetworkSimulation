"""
Docker 容器运行时 — 管理 Agent 容器的生命周期

功能:
- 创建/启动/停止 Agent 容器
- 向容器发送指令
- 收集容器状态和日志
- 配合消息总线实现 Agent 间通信

依赖: docker SDK (pip install docker)
"""

import os
import json
import time
import requests
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field


@dataclass
class ContainerAgent:
    """运行在 Docker 容器中的 Agent"""
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
    """
    Agent 容器运行时管理器 — Docker 模式
    支持三种后端: brain (默认), openclaw, claude-code
    """

    BACKEND_CONFIG = {
        "brain":       {"image": "agent-network:latest",    "cmd": "python agent_server.py"},
        "openclaw":    {"image": "agent-network:openclaw",  "cmd": "python agent_server_openclaw.py"},
        "claude-code": {"image": "agent-network:claude",    "cmd": "python3 agent_server_claude.py"},
    }
    DEFAULT_BACKEND = "brain"
    NETWORK_NAME = "agent-net"
    SUBNET = "172.20.0.0/16"

    def __init__(self, message_bus_url: str = "http://host.docker.internal:9000"):
        self.message_bus_url = message_bus_url
        self.agents: Dict[str, ContainerAgent] = {}
        self._built_images: set = set()

        try:
            import docker
        except ImportError:
            raise RuntimeError("Docker SDK not installed. Run: pip install docker")

        for attempt in range(3):
            try:
                self.docker_client = docker.from_env()
                self.docker_client.ping()
                self._setup_network()
                print("[Runtime] Docker mode")
                return
            except Exception as e:
                if attempt < 2:
                    print(f"[Runtime] Docker probe {attempt+1}/3: {e}")
                    time.sleep(2)
        raise RuntimeError(
            "Docker Engine unavailable. Ensure Docker Desktop is running.\n"
            "Run 'docker ps' to verify. If WSL2 is blocked by IT policy,\n"
            "Docker Desktop cannot run on this machine."
        )

    def _setup_network(self):
        """创建 Docker 网络（如不存在）"""
        try:
            self.docker_client.networks.get(self.NETWORK_NAME)
            print(f"[Runtime] Using existing network: {self.NETWORK_NAME}")
        except Exception:
            ipam_pool = docker.types.IPAMPool(subnet=self.SUBNET)
            ipam_config = docker.types.IPAMConfig(pool_configs=[ipam_pool])
            self.docker_client.networks.create(
                self.NETWORK_NAME, driver="bridge", ipam=ipam_config,
            )
            print(f"[Runtime] Created network: {self.NETWORK_NAME} ({self.SUBNET})")

    def _ensure_image(self, backend: str):
        """检查镜像是否存在，不存在则自动构建"""
        if backend in self._built_images:
            return
        cfg = self.BACKEND_CONFIG.get(backend, self.BACKEND_CONFIG[self.DEFAULT_BACKEND])
        image_name = cfg["image"]

        try:
            self.docker_client.images.get(image_name)
            self._built_images.add(backend)
            return
        except Exception:
            pass

        # 查找 Dockerfile
        import pathlib
        root = pathlib.Path(__file__).parent.parent
        dockerfile_map = {
            "brain":       root / "Dockerfile",
            "openclaw":    root / "Dockerfile.openclaw",
            "claude-code": root / "Dockerfile.claude",
        }
        dockerfile_path = dockerfile_map.get(backend, root / "Dockerfile")
        if not dockerfile_path.exists():
            raise RuntimeError(f"Dockerfile not found at {dockerfile_path}")

        print(f"[Runtime] Building Docker image {image_name} ...")
        image, logs = self.docker_client.images.build(
            path=str(root),
            dockerfile=str(dockerfile_path),
            tag=image_name,
            rm=True,
        )
        for chunk in logs:
            if 'stream' in chunk:
                line = chunk['stream'].rstrip()
                if line:
                    print(f"  [build] {line}")
        print(f"[Runtime] Image {image_name} built successfully")
        self._built_images.add(backend)

    def create_agent(self, agent_id: str, role: str, name: str,
                     port: int = 0, llm_config: Dict = None,
                     extra_meta: Dict = None) -> ContainerAgent:
        """创建并启动一个 Agent 容器"""
        if port == 0:
            used_ports = {a.port for a in self.agents.values()}
            port = 8100 + len(self.agents)
            while port in used_ports:
                port += 1

        ca = ContainerAgent(
            agent_id=agent_id, name=name, role=role, port=port,
            container_name=f"agent-{agent_id}",
            url=f"http://localhost:{port}",
        )

        self._start_docker_container(ca, llm_config, extra_meta)
        self.agents[agent_id] = ca
        return ca

    def _start_docker_container(self, ca: ContainerAgent, llm_config: Dict = None,
                                 extra_meta: Dict = None):
        """启动 Docker 容器，根据 backend 选择镜像和命令"""
        backend = (extra_meta or {}).get("backend", self.DEFAULT_BACKEND)
        if backend not in self.BACKEND_CONFIG:
            backend = self.DEFAULT_BACKEND

        self._ensure_image(backend)
        cfg = self.BACKEND_CONFIG[backend]

        env = {
            "AGENT_ID": ca.agent_id,
            "AGENT_ROLE": ca.role,
            "AGENT_NAME": ca.name,
            "PORT": str(ca.port),
            "MESSAGE_BUS_URL": self.message_bus_url,
            "SERVER_URL": getattr(self, 'server_url', 'http://localhost:8000'),
        }
        # 传递 script_json 身份数据
        if extra_meta:
            env["AGENT_CORE_GOAL"] = extra_meta.get("core_goal", "")
            env["AGENT_HIDDEN_SECRET"] = extra_meta.get("hidden_secret", "")
            env["AGENT_ACTION_SPACE"] = json.dumps(extra_meta.get("action_space", []), ensure_ascii=False)
            env["AGENT_INITIAL_ASSETS"] = json.dumps(extra_meta.get("initial_assets", {}), ensure_ascii=False)
            env["AGENT_SYSTEM_PROMPT"] = extra_meta.get("background_rules", "")
            env["AGENT_INTERACTION_PARADIGM"] = extra_meta.get("interaction_paradigm", "")
            env["AGENT_PARADIGM_HINT"] = extra_meta.get("paradigm_hint", "")
        if llm_config:
            if llm_config.get("api_key"):
                env["LLM_API_KEY"] = llm_config["api_key"]
                env["LLM_MODEL"] = llm_config.get("model", "")
                env["LLM_PROVIDER"] = llm_config.get("provider", "auto")

        # Claude Code needs ANTHROPIC_API_KEY for the CLI
        if backend == "claude-code" and llm_config and llm_config.get("api_key"):
            env["ANTHROPIC_API_KEY"] = llm_config["api_key"]

        container = self.docker_client.containers.run(
            cfg["image"],
            name=ca.container_name,
            detach=True,
            command=cfg["cmd"],
            ports={f"{ca.port}/tcp": ca.port},
            environment=env,
            network=self.NETWORK_NAME,
        )
        ca.container_id = container.id

        # Read assigned IP from Docker network
        container.reload()
        nets = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        agent_net = nets.get(self.NETWORK_NAME, {})
        agent_ip = agent_net.get("IPAddress", "")
        if agent_ip:
            ca._ip = agent_ip
            ca.url = f"http://{agent_ip}:{ca.port}"
        else:
            # Fallback: use container name as hostname (Docker DNS)
            ca.url = f"http://{ca.container_name}:{ca.port}"

        ca.status = "starting"
        print(f"[Runtime] Started {ca.agent_id} ({backend}) @ IP {agent_ip}:{ca.port}")


    def _register_with_bus(self, ca: ContainerAgent):
        """向消息总线注册"""
        try:
            requests.post(f"{self.message_bus_url}/register",
                         params={"agent_id": ca.agent_id, "url": ca.url, "name": ca.name}, timeout=3)
        except Exception as e:
            print(f"[Runtime] Failed to register {ca.agent_id}: {e}")

    def stop_agent(self, agent_id: str):
        """停止 Agent"""
        ca = self.agents.get(agent_id)
        if not ca:
            return
        try:
            requests.post(f"{self.message_bus_url}/unregister", params={"agent_id": agent_id}, timeout=2)
        except Exception:
            pass
        if ca.container_id:
            try:
                container = self.docker_client.containers.get(ca.container_id)
                container.stop()
                container.remove()
            except Exception:
                pass
        ca.status = "stopped"

    def stop_all(self):
        for aid in list(self.agents.keys()):
            self.stop_agent(aid)

    def decide_all(self, context: Dict = None) -> List[Dict]:
        """并行触发所有 Agent 决策"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        results = []
        agents_list = list(self.agents.values())
        def _decide(ca):
            try:
                resp = requests.post(f"{ca.url}/decide",
                                    json={"context": context or {}}, timeout=60)
                return resp.json()
            except Exception as e:
                return {"agent_id": ca.agent_id, "error": str(e)}
        with ThreadPoolExecutor(max_workers=len(agents_list)) as pool:
            futures = {pool.submit(_decide, ca): ca for ca in agents_list}
            for f in as_completed(futures):
                results.append(f.result())
        return results

    def act_all(self) -> List[Dict]:
        """并行触发所有 Agent 执行决策"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        results = []
        agents_list = list(self.agents.values())
        def _act(ca):
            try:
                resp = requests.post(f"{ca.url}/act", timeout=60)
                return resp.json()
            except Exception as e:
                return {"agent_id": ca.agent_id, "error": str(e)}
        with ThreadPoolExecutor(max_workers=len(agents_list)) as pool:
            futures = {pool.submit(_act, ca): ca for ca in agents_list}
            for f in as_completed(futures):
                results.append(f.result())
        return results

    def run_round(self, context: Dict = None) -> Dict:
        """执行一轮：决策 → 执行 → 收集结果"""
        decisions = self.decide_all(context)
        actions = self.act_all()
        return {"decisions": decisions, "actions": actions}

    def get_all_status(self) -> List[Dict]:
        """获取所有 Agent 状态"""
        statuses = []
        for ca in self.agents.values():
            try:
                resp = requests.get(f"{ca.url}/status", timeout=3)
                statuses.append({**ca.to_dict(), **resp.json()})
            except Exception:
                statuses.append({**ca.to_dict(), "error": "unreachable"})
        return statuses


# 全局单例
_runtime: Optional[ContainerRuntime] = None


def get_runtime() -> ContainerRuntime:
    global _runtime
    if _runtime is None:
        bus_url = os.environ.get("MESSAGE_BUS_URL", "http://localhost:9000")
        _runtime = ContainerRuntime(message_bus_url=bus_url)
    return _runtime
