"""
Agent SDK — 对应架构文档 第三节：Agent SDK & 第六节：Agent管理层

统一屏蔽Agent差异，提供：
- Agent 基本属性（id, role, skills, tags, capability_scores）
- 任务收发
- 状态查询
- 工具调用
- Agent 注册与发现
"""

from typing import List, Optional, Dict, Any
from datetime import datetime
from dataclasses import dataclass
import uuid
import json


@dataclass
class Message:
    """Agent 间消息"""
    source: str
    target: str
    type: str = "task"
    payload: Dict[str, Any] = None
    message_id: str = ""

    def __post_init__(self):
        if self.message_id == "":
            self.message_id = str(uuid.uuid4())[:8]
        if self.payload is None:
            self.payload = {}


class Agent:
    """
    Agent 基类 — 仿真中的智能体

    对应架构文档第三节 Agent SDK:
    class Agent:
        def send_task(self, task): ...
        def get_status(self): ...
        def call_tool(self): ...
    """

    def __init__(
        self,
        agent_id: str = None,
        role: str = "generic",
        name: str = "",
        skills: List[str] = None,
        tags: List[str] = None,
        capability_scores: Dict[str, float] = None,
    ):
        self.agent_id = agent_id or f"agent-{str(uuid.uuid4())[:8]}"
        self.role = role
        self.name = name or self.agent_id
        self.skills = skills or []
        self.tags = tags or []
        self.capability_scores = capability_scores or {}
        self.status = "created"  # created, idle, running, paused, stopped, error
        self.container_id = f"docker-{self.agent_id}"
        self.event_bus = None  # 由仿真引擎注入
        self.comm = None       # 统一通信层 (LocalBus/RemoteBus)
        self.task_queue: List[Message] = []
        self.completed_tasks: List[Dict[str, Any]] = []
        self.pending_task_descs: List[str] = []  # task descriptions from scene script
        self.extra_meta: Dict[str, Any] = {}     # script_json metadata (identity, goals, etc.)
        self.inbox: List[Dict] = []           # LLM 收件箱
        self.brain = None                      # LLM 大脑
        self.has_brain = False                 # 是否启用 LLM
        self._created_at = datetime.now().isoformat(timespec="seconds")
        # 地图位置
        self.x: float = 0.0                    # 当前 X 坐标 (网格列)
        self.y: float = 0.0                    # 当前 Y 坐标 (网格行)
        self.speed: float = 1.0                # 移动速度 (格/tick)
        self._target_x: Optional[float] = None  # 移动目标 X
        self._target_y: Optional[float] = None  # 移动目标 Y

    # ── 任务收发 ────────────────────────────────

    def set_comm(self, comm, registry=None):
        """设置统一通信层和 Agent 注册表"""
        self.comm = comm
        self._registry = registry

    def send_task(self, task: str, target: "Agent" = None, **kwargs) -> Message:
        """
        发送任务给目标 Agent。优先使用统一通信层。
        """
        target_id = target.agent_id if target else self.agent_id
        msg = Message(
            source=self.agent_id,
            target=target_id,
            type="task",
            payload={"action": task, **kwargs},
        )

        # 统一通信层: LocalBus 直投 inbox, RemoteBus HTTP 转发
        if self.comm and target and target_id != self.agent_id:
            self.comm.send(self.agent_id, self.name, target_id, task)
        elif self.comm and not target:
            self.task_queue.append(msg)
        elif self.event_bus and target:
            self.event_bus.publish(msg)
        else:
            self.task_queue.append(msg)

        return msg

    def send_response(self, target: "Agent", result: Any, **kwargs) -> Message:
        """发送响应给目标 Agent"""
        msg = Message(
            source=self.agent_id,
            target=target.agent_id,
            type="response",
            payload={"result": result, **kwargs},
        )
        if self.comm and target:
            self.comm.send(self.agent_id, self.name, target.agent_id, str(result))
        elif self.event_bus:
            self.event_bus.publish(msg)
        return msg

    def receive_task(self, message: Message):
        """从事件总线接收消息"""
        self.task_queue.append(message)
        # 也加入收件箱（供 LLM brain 使用）
        self.inbox.append({
            "from": getattr(message, 'source', 'unknown'),
            "content": message.payload.get("action", message.payload.get("result", "")),
            "type": message.type,
        })
        if len(self.inbox) > 50:
            self.inbox.pop(0)

    # ── LLM Brain 集成 ───────────────────────────

    def equip_brain(self, goals: List[str] = None, config: Dict = None,
                     system_prompt: str = ""):
        """为 Agent 安装 LLM 大脑"""
        from .brain import create_brain
        self.brain = create_brain(role=self.role, name=self.name, goals=goals,
                                  system_prompt=system_prompt)
        if config:
            self.brain.config = config
        self.has_brain = True

    def decide(self, context: Dict = None) -> Any:
        """
        使用 Brain 做决策

        Returns:
            Action 对象，包含 type/target/content/reasoning
        """
        if not hasattr(self, 'brain') or not self.brain:
            return None
        # 构建已知 Agent 列表
        context = context or {}
        if "known_agents" not in context:
            from .agent import AgentRegistry
            all_agents = AgentRegistry.list_all()
            context["known_agents"] = [
                {"name": a.name, "agent_id": a.agent_id, "role": a.role}
                for a in all_agents if a.agent_id != self.agent_id
            ]
        return self.brain.decide(self.inbox, context)

    def act(self, action: Any, registry=None) -> List[Any]:
        """
        执行决策动作

        Args:
            action: Brain 返回的 Action
            registry: AgentRegistry（用于查找目标）

        Returns:
            执行结果列表
        """
        if action is None:
            return [{"status": "no_action"}]

        results = []

        if action.type == "send_message":
            self.status = "running"
            target_agent = None
            if registry:
                # 按名字或 ID 查找目标
                for a in registry.list_all():
                    if a.name == action.target or a.agent_id == action.target:
                        target_agent = a
                        break
            msg = Message(
                source=self.agent_id,
                target=target_agent.agent_id if target_agent else "broadcast",
                type="message",
                payload={"action": action.content, "from_name": self.name},
            )
            if self.event_bus and target_agent:
                self.event_bus.publish(msg)
                # 目标 Agent 自动收件
                target_agent.receive_task(msg)
            results.append({"status": "sent", "target": action.target, "content": action.content})
            self.status = "idle"

        elif action.type == "broadcast":
            self.status = "running"
            msg = Message(
                source=self.agent_id, target="broadcast", type="broadcast",
                payload={"action": action.content, "from_name": self.name},
            )
            if self.event_bus:
                self.event_bus.publish(msg)
            results.append({"status": "broadcast", "content": action.content})
            self.status = "idle"

        elif action.type in ("search", "analyze", "plan"):
            self.status = "running"
            # 使用 Tool 执行
            from .tool import ToolRegistry
            try:
                if action.type == "search":
                    result = ToolRegistry.execute("search", keyword=action.content)
                elif action.type == "analyze":
                    result = {"analysis": f"分析完成: {action.content}", "confidence": 0.85}
                else:
                    result = {"plan": f"方案: {action.content}"}
                results.append({"status": "completed", "action": action.type, "result": result})

                # 将结果加入自己的收件箱（自我反馈）
                self.inbox.append({
                    "from": "self",
                    "content": f"[{action.type}结果] {json.dumps(result, ensure_ascii=False)[:200]}",
                    "type": "self_feedback",
                })
            except Exception as e:
                results.append({"status": "error", "action": action.type, "error": str(e)})
            self.status = "idle"

        elif action.type == "wait":
            results.append({"status": "waiting"})

        elif action.type == "move_to":
            self.status = "running"
            tx = getattr(action, "target_x", -1)
            ty = getattr(action, "target_y", -1)
            if tx >= 0 and ty >= 0:
                self._target_x = float(tx)
                self._target_y = float(ty)
                results.append({"status": "moving", "target": [tx, ty], "from": [self.x, self.y]})
                # Publish movement event
                if self.event_bus:
                    msg = Message(
                        source=self.agent_id, target="map", type="movement",
                        payload={"target_x": tx, "target_y": ty, "from": [self.x, self.y]},
                    )
                    self.event_bus.publish(msg)
            else:
                results.append({"status": "invalid_move_target", "target": [tx, ty]})
            self.status = "idle"

        else:
            results.append({"status": "unknown_action", "type": action.type})

        return results

    # ── 任务执行 ────────────────────────────────

    def execute_task(self, message: Message) -> Dict[str, Any]:
        """
        执行单个任务（同步版本，由仿真引擎调度）

        子类可重写以定制行为。
        """
        self.status = "running"
        action = message.payload.get("action", "unknown")

        result = {
            "agent_id": self.agent_id,
            "agent_role": self.role,
            "task": action,
            "status": "completed",
            "output": f"[{self.name}] 执行任务完成: {action}",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }

        self.completed_tasks.append(result)
        self.status = "idle"
        return result

    # ── 状态查询 ────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """获取 Agent 状态（对应架构文档 Agent 注册信息）"""
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "role": self.role,
            "url": getattr(self, 'container_url', ''),
            "container_id": self.container_id,
            "status": self.status,
            "skills": self.skills,
            "tags": self.tags,
            "capability_scores": self.capability_scores,
            "pending_tasks": len(self.task_queue),
            "pending_task_descs": self.pending_task_descs,
            "extra_meta": self.extra_meta,
            "completed_tasks": len(self.completed_tasks),
            "created_at": self._created_at,
            "x": self.x,
            "y": self.y,
        }

    # ── 工具调用 ────────────────────────────────

    def call_tool(self, tool_name: str, **kwargs) -> Any:
        """调用已注册的工具"""
        from .tool import ToolRegistry
        return ToolRegistry.execute(tool_name, **kwargs)

    # ── 生命周期 ────────────────────────────────

    def start(self):
        """启动 Agent"""
        self.status = "idle"

    def pause(self):
        """暂停 Agent"""
        if self.status == "running":
            self.status = "paused"

    def resume(self):
        """恢复 Agent"""
        if self.status == "paused":
            self.status = "running"

    def stop(self):
        """停止 Agent"""
        self.status = "stopped"

    def error(self, reason: str = ""):
        """标记为错误状态"""
        self.status = "error"

    def __repr__(self):
        return f"Agent(id={self.agent_id}, role={self.role}, status={self.status})"


# ═══════════════════════════════════════════════════
# Agent 注册中心 — 对应架构文档 第六节：Agent管理层
# ═══════════════════════════════════════════════════

class AgentRegistry:
    """
    Agent 注册中心 — 管理所有 Agent 的生命周期与发现

    支持发现方式（对应架构文档 第六节）：
    - 按角色: find_agent(role="commander")
    - 按技能: find_agent(skill="planning")
    - 按标签: find_agent(tag="blue_force")
    - 按能力评分: find_best_agent(skill="analysis")
    """
    _instance: Optional["AgentRegistry"] = None
    _agents: Dict[str, Agent] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def register(cls, agent: Agent):
        """注册 Agent"""
        cls._agents[agent.agent_id] = agent

    @classmethod
    def unregister(cls, agent_id: str):
        """注销 Agent"""
        cls._agents.pop(agent_id, None)

    @classmethod
    def get(cls, agent_id: str) -> Optional[Agent]:
        """按 ID 获取 Agent"""
        return cls._agents.get(agent_id)

    @classmethod
    def find_agent(
        cls,
        role: str = None,
        skill: str = None,
        tag: str = None,
    ) -> List[Agent]:
        """
        Agent 发现 — 支持多条件筛选

        对应架构文档：
        - find_agent(role="commander")
        - find_agent(skill="planning")
        - find_agent(tag="blue_force")
        """
        results = []
        for agent in cls._agents.values():
            if role and agent.role != role:
                continue
            if skill and skill not in agent.skills:
                continue
            if tag and tag not in agent.tags:
                continue
            results.append(agent)
        return results

    @classmethod
    def find_best_agent(cls, skill: str) -> Optional[Agent]:
        """
        按能力评分找最优 Agent

        对应架构文档：find_best_agent(skill="analysis")
        返回指定 skill 评分最高的 Agent
        """
        # 优先查找 skills 列表中包含该 skill 的 agent
        candidates = [a for a in cls._agents.values() if skill in a.skills]
        if not candidates:
            # 回退：查找 capability_scores 中有该 skill 的
            candidates = [a for a in cls._agents.values() if skill in a.capability_scores]
        if not candidates:
            return None
        return max(candidates, key=lambda a: a.capability_scores.get(skill, 0))

    @classmethod
    def list_all(cls) -> List[Agent]:
        """列出所有 Agent"""
        return list(cls._agents.values())

    @classmethod
    def get_stats(cls) -> Dict[str, Any]:
        """获取 Agent 注册中心统计信息"""
        agents = cls.list_all()
        roles = {}
        statuses = {}
        for a in agents:
            roles[a.role] = roles.get(a.role, 0) + 1
            statuses[a.status] = statuses.get(a.status, 0) + 1
        return {
            "total_agents": len(agents),
            "by_role": roles,
            "by_status": statuses,
        }

    @classmethod
    def reset(cls):
        """重置注册中心（测试用）"""
        cls._agents.clear()
