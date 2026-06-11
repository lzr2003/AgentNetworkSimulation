"""
Agent LLM 大脑 — 让每个 Agent 拥有独立决策能力

观察 → 推理 → 决策 → 行动

每个 Agent 有:
- 角色 persona（system prompt）
- 独立收件箱
- LLM 驱动的决策循环
- 可用的工具/动作集

支持后端: Anthropic / OpenAI / DeepSeek（复用 llm_parser 的配置）
"""

import json
import re
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
from .llm_parser import get_api_config


@dataclass
class Action:
    """Agent 决策后的动作"""
    type: str  # "send_message" | "search" | "analyze" | "wait" | "plan" | "move_to" | "execute_skill"
    target: str = ""       # 消息目标 agent_id 或技能名
    content: str = ""      # 消息/动作内容
    reasoning: str = ""    # Agent 的推理过程
    raw_response: str = "" # LLM 原始响应
    target_x: float = -1   # move_to 目标 X
    target_y: float = -1   # move_to 目标 Y
    skill: str = ""        # execute_skill 的技能名
    params: Dict[str, Any] = field(default_factory=dict)  # execute_skill 的参数

    def to_dict(self):
        d = {
            "type": self.type, "target": self.target,
            "content": self.content, "reasoning": self.reasoning,
        }
        if self.type == "move_to":
            d["target_x"] = self.target_x
            d["target_y"] = self.target_y
        if self.type == "execute_skill":
            d["skill"] = self.skill or self.target
            d["params"] = self.params
        return d


DEFAULT_SYSTEM_PROMPT = """根据你的身份、目标和可用行动，在仿真场景中做出合理决策。

可用动作：
- send_message(target, content): 向 Agent 发送消息。target 填 agent_id（如 "ceo"），填 "0.0.0.0" 表示向全体 Agent 广播
- analyze(data): 分析当前局势
- plan(objective): 制定行动计划
- wait: 等待更多信息或观望

行为准则：
- 始终围绕你的核心目标和秘密行动
- 合理使用你的资产和影响力
- 与相关方建立联系、谈判或竞争
- 根据局势变化灵活调整策略
- send_message 的 target 必须用 agent_id（如 role_c），不能用中文名"""


class Brain:
    """
    Agent 的 LLM 大脑

    每轮决策:
    1. 收集上下文（角色、目标、当前状态）
    2. 收集观察（收件箱消息）
    3. 构造 prompt → 调用 LLM
    4. 解析响应 → Action
    """

    def __init__(self, role: str, name: str, goals: List[str] = None, config: Dict = None,
                 system_prompt: str = ""):
        self.role = role
        self.name = name
        self.goals = goals or ["完成指派的任务"]
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self.config = config or get_api_config()
        self.memory: List[str] = []  # 短期记忆（最近几轮的事件）
        self.turn = 0

    def decide(self, inbox: List[Dict], context: Dict = None) -> Action:
        """
        给定当前状态，做出决策

        Args:
            inbox: 收件箱消息列表 [{"from": "agent_name", "content": "..."}]
            context: 环境上下文 {"round": N, "known_agents": [...], "world_state": "..."}

        Returns:
            Action 决策动作
        """
        self.turn += 1
        api_key = self.config.get("api_key", "")
        if not api_key:
            return Action(type="wait", target="", content="", reasoning="no LLM API key configured")

        prompt = self._build_prompt(inbox, context)
        response_text = self._call_llm(prompt, api_key)
        action = self._parse_response(response_text)

        # 存入记忆
        self.memory.append(f"[Round {self.turn}] Decided: {action.type} → {action.content}")
        if len(self.memory) > 20:
            self.memory.pop(0)

        action.raw_response = response_text
        return action

    def _build_prompt(self, inbox: List[Dict], context: Dict = None) -> str:
        """构建发给 LLM 的 prompt"""
        context = context or {}
        system = self.system_prompt

        # 已知的其他 Agent
        known = context.get("known_agents", [])
        known_lines = []
        for a in known:
            aid = a.get('agent_id', '?')
            nm = a.get('name', aid)
            rl = a.get('role', '?')
            known_lines.append(f"  - agent_id={aid}  名称={nm}  角色={rl}")
        known_list = "\n".join(known_lines) if known_lines else "  暂无"

        # ── 收件箱分类 ──
        direct_msgs = []
        broadcast_msgs = []
        system_msgs = []
        for msg in inbox[-15:]:  # 最近15条
            mtype = msg.get('type', 'direct')
            if mtype == 'system':
                system_msgs.append(f"  ⚡ [系统]: {msg.get('content', '')}")
            elif mtype == 'broadcast':
                broadcast_msgs.append(f"  [{msg.get('from', '?')}]: {msg.get('content', '')}")
            else:
                direct_msgs.append(f"  🔴 [{msg.get('from', '?')}]: {msg.get('content', '')}")

        direct_count = len(direct_msgs)
        pending_count = direct_count  # 简化：所有未回复的直接消息

        inbox_text = ""
        if direct_msgs:
            inbox_text += "## 📬 直接发给你的消息\n"
            inbox_text += "\n".join(direct_msgs[-10:]) + "\n\n"
        if broadcast_msgs:
            inbox_text += "## 📢 广播消息\n"
            inbox_text += "\n".join(broadcast_msgs[-5:]) + "\n\n"
        if system_msgs:
            inbox_text += "## ⚡ 系统通知\n"
            inbox_text += "\n".join(system_msgs[-3:]) + "\n\n"
        if not inbox_text:
            inbox_text = "（收件箱为空 — 主动发起对话或分析局势）\n"

        # 记忆
        memory_text = "\n".join(f"  {m}" for m in self.memory[-6:]) if self.memory else "  （开始）"

        # 目标
        goals_text = "\n".join(f"  {i+1}. {g}" for i, g in enumerate(self.goals))

        # 可用技能
        skills = context.get("skills_list", [])
        skills_text = ""
        if skills:
            skills_lines = []
            for s in skills:
                sn = s.get('name', s.get('skill_name', '?'))
                sd = s.get('desc', s.get('description', ''))
                params = s.get('params', s.get('parameters', []))
                params_str = ", ".join(params) if params else ""
                skills_lines.append(f"  - execute_skill: {sn}({params_str}) — {sd}")
            skills_text = "\n".join(skills_lines)

        # 待回复警告
        pending_warning = ""
        if pending_count > 0:
            pending_warning = f"\n⚠️ 你有 {pending_count} 条未回复的直接消息。执行任务型 skill 后结果会自动回复，不需要单独 send_message。"

        prompt = f"""{system}

## 当前状态
回合: {self.turn}
你的名字: {self.name}
你的角色: {self.role}

## 你的目标
{goals_text}

## 已知的其它 Agent（发消息时 target 必须用 agent_id）
{known_list}
"""
        if skills_text:
            prompt += f"""## 可用技能（使用 execute_skill 动作调用）
{skills_text}

"""
        prompt += f"""## 最近的记忆
{memory_text}

{inbox_text}{pending_warning}

## 指令
基于以上信息，决定你这一轮要做什么。用以下 JSON 格式回复（只输出 JSON，不要其他内容）:

```json
{{
  "reasoning": "你的推理过程（一句话）",
  "action": "send_message|execute_skill|analyze|plan|wait",
  "target": "目标 Agent 的 agent_id（send_message 时必填）或技能名（execute_skill 时填技能名）",
  "content": "消息内容、技能参数或动作描述"
}}
```

行动指南:
- 这是第{self.turn}轮，必须立即行动！如果收到任务指令（如 move），直接用 execute_skill 执行，结果会自动回复发件人
- target 必须填 agent_id（如 ceo、cto），不能填中文名
- 向全体 Agent 广播消息时，target 填 "0.0.0.0"
- 消息内容要具体、有信息量，至少50字"""
        if skills_text:
            prompt += "\n- 你有可用技能！合理使用 execute_skill 来完成任务（如 allocate_budget、audit_expense 等）"
        prompt += "\n- 用中文回复"""
        return prompt
        return prompt

    def _call_llm(self, prompt: str, api_key: str) -> str:
        """调用 LLM API"""
        provider = self.config.get("provider", "auto")
        model = self.config.get("model", "")
        api_base = self.config.get("api_base", "")

        # 复用 llm_parser 的 API 调用逻辑
        if api_key.startswith("sk-ant-") and provider != "openai":
            return self._call_anthropic(prompt, api_key, model)
        else:
            return self._call_openai_compat(prompt, api_key, model, api_base)

    def _call_anthropic(self, prompt: str, api_key: str, model: str = "") -> str:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        from .config import DEFAULT_LLM_MODEL
        model = model or DEFAULT_LLM_MODEL
        # Split system and user
        parts = prompt.split("## 当前状态")
        system = parts[0].strip() if len(parts) > 1 else prompt
        user = prompt if len(parts) <= 1 else "## 当前状态" + parts[1]

        message = client.messages.create(
            model=model, max_tokens=512, system=system,
            messages=[{"role": "user", "content": user}],
        )
        return message.content[0].text

    def _call_openai_compat(self, prompt: str, api_key: str, model: str = "", api_base: str = "") -> str:
        import httpx
        model = model or "deepseek-chat"
        api_base = api_base or "https://api.deepseek.com/v1"
        url = f"{api_base.rstrip('/')}/chat/completions"

        try:
            resp = httpx.post(url, headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }, json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 512, "temperature": 0.7,
            }, timeout=30.0)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except httpx.HTTPError as e:
            raise RuntimeError(f"LLM API call failed: {e}") from e

    def _parse_response(self, text: str) -> Action:
        """从 LLM 响应中解析 Action"""
        # 提取 JSON
        json_match = re.search(r'\{[\s\S]*"action"[\s\S]*\}', text)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
                return Action(
                    type=data.get("action", "wait"),
                    target=data.get("target", ""),
                    content=data.get("content", ""),
                    reasoning=data.get("reasoning", ""),
                )
            except json.JSONDecodeError:
                pass

        # 回退：从文本中猜测意图
        text_lower = text.lower()
        if "发送" in text or "send" in text_lower:
            return Action(type="send_message", content=text, reasoning="从文本提取")
        if "搜索" in text or "search" in text_lower:
            return Action(type="search", content="目标区域", reasoning="从文本提取")
        if "等待" in text or "wait" in text_lower:
            return Action(type="wait", reasoning="从文本提取")

        return Action(type="wait", content="", reasoning=f"无法解析: {text}")

def create_brain(role: str, name: str, goals: List[str] = None,
                  system_prompt: str = "") -> Brain:
    """工厂函数：创建 Brain"""
    if goals is None:
        goals = ["完成指派任务"]
    return Brain(role=role, name=name, goals=goals, system_prompt=system_prompt)
