"""
Agent LLM brain.

Each Agent owns a small decision loop:
observe inbox/context -> call LLM -> parse an Action.
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .scene_def import get_api_config


FACT_BOARD_SECTIONS = [
    ("task_progress", "Task Progress"),
    ("completed_actions", "Completed Actions"),
    ("pending_items", "Pending Items"),
    ("key_constraints", "Key Constraints"),
    ("recent_skill_results", "Recent Skill Results"),
]


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


class BoundedFactBoard:
    """Small rule-built fact board used to cap long raw histories."""

    def __init__(
        self,
        max_items: int = None,
        max_chars: int = None,
        item_max_chars: int = None,
    ):
        self.max_items = max(1, max_items if max_items is not None else _int_env("AGENT_FACT_BOARD_MAX_ITEMS", 40))
        self.max_chars = max(500, max_chars if max_chars is not None else _int_env("AGENT_FACT_BOARD_MAX_CHARS", 6000))
        self.item_max_chars = max(80, item_max_chars if item_max_chars is not None else _int_env("AGENT_FACT_ITEM_MAX_CHARS", 300))
        self.sections: Dict[str, List[str]] = {key: [] for key, _ in FACT_BOARD_SECTIONS}

    def clear(self):
        for items in self.sections.values():
            items.clear()

    def _stringify(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            return str(value)

    def _shorten(self, text: Any) -> str:
        text = re.sub(r"\s+", " ", self._stringify(text)).strip()
        if len(text) > self.item_max_chars:
            return text[: self.item_max_chars - 3].rstrip() + "..."
        return text

    def add(self, section: str, text: Any):
        if section not in self.sections:
            section = "task_progress"
        text = self._shorten(text)
        if not text:
            return
        items = self.sections[section]
        if text in items:
            items.remove(text)
        items.append(text)
        self._prune()

    def add_action(self, action: Any):
        if hasattr(action, "to_dict"):
            data = action.to_dict()
        elif isinstance(action, dict):
            data = action
        else:
            data = {}
        action_type = data.get("type", data.get("action", "wait"))
        target = data.get("target", "")
        content = data.get("content", "")
        reasoning = data.get("reasoning", "")
        if action_type in ("send_message", "broadcast"):
            self.add("completed_actions", f"{action_type} -> {target}: {content}")
        elif action_type == "execute_skill":
            skill = data.get("skill") or target
            params = data.get("params") or content
            self.add("pending_items", f"execute_skill {skill}: {params}")
        elif action_type in ("analyze", "plan"):
            self.add("task_progress", f"{action_type}: {content or reasoning}")
        elif action_type == "wait":
            self.add("pending_items", f"wait: {reasoning or content}")
        else:
            self.add("task_progress", f"{action_type} {target}: {content or reasoning}")

    def add_inbox_facts(self, inbox: List[Dict]):
        for msg in inbox[-5:]:
            if msg.get("type") != "system":
                continue
            content = msg.get("content", "")
            lower = content.lower()
            if "skill" in lower or "result" in lower or "执行结果" in content or "技能" in content:
                self.add("recent_skill_results", content)
            else:
                self.add("key_constraints", content)

    def add_skill_result(self, skill_name: str, result: Any):
        self.add("recent_skill_results", f"{skill_name}: {result}")

    def _total_items(self) -> int:
        return sum(len(items) for items in self.sections.values())

    def _render(self) -> str:
        parts = ["## Fact Board", "Stable bounded summary for long-running simulation context."]
        for key, title in FACT_BOARD_SECTIONS:
            items = self.sections.get(key, [])
            parts.append(f"\n### {title}")
            if items:
                parts.extend(f"- {item}" for item in items)
            else:
                parts.append("- (none)")
        return "\n".join(parts)

    def _drop_oldest(self) -> bool:
        for key, _ in FACT_BOARD_SECTIONS:
            if self.sections[key]:
                self.sections[key].pop(0)
                return True
        return False

    def _prune(self):
        while self._total_items() > self.max_items and self._drop_oldest():
            pass
        while len(self._render()) > self.max_chars and self._drop_oldest():
            pass

    def to_message_content(self) -> str:
        self._prune()
        return self._render()


@dataclass
class Action:
    """Action chosen by an Agent after one LLM decision."""

    type: str
    target: str = ""
    content: str = ""
    reasoning: str = ""
    raw_response: str = ""
    target_x: float = -1
    target_y: float = -1
    skill: str = ""
    params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        d = {
            "type": self.type,
            "target": self.target,
            "content": self.content,
            "reasoning": self.reasoning,
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
- send_message(target, content): 向 Agent 发送消息。target 填 agent_id，填 "0.0.0.0" 表示向全体 Agent 广播
- execute_skill(target, content): 执行可用技能
- analyze(data): 分析当前局势
- plan(objective): 制定行动计划
- wait: 等待更多信息或观察
行为准则：
- 始终围绕你的核心目标和场景任务行动
- 合理使用你的资源和影响力
- 根据局势变化灵活调整策略
- send_message 的 target 必须用 agent_id，不能用中文名"""


class Brain:
    """LLM-backed decision brain with cache-friendly message history."""

    def __init__(
        self,
        role: str,
        name: str,
        goals: List[str] = None,
        config: Dict = None,
        system_prompt: str = "",
    ):
        self.role = role
        self.name = name
        self.goals = goals or ["完成指派的任务"]
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self.config = config or get_api_config()
        self.message_history: List[Dict[str, str]] = []
        default_recent_turns = _int_env("AGENT_MESSAGE_HISTORY_TURNS", 2)
        self.recent_history_turns = max(0, _int_env("AGENT_RECENT_HISTORY_TURNS", default_recent_turns))
        self.history_turns = self.recent_history_turns
        self.fact_board = BoundedFactBoard()
        self.turn = 0

    def decide(self, inbox: List[Dict], context: Dict = None) -> Action:
        self.turn += 1
        api_key = self.config.get("api_key", "")
        if not api_key:
            return Action(type="wait", reasoning="no LLM API key configured")

        messages = self._build_messages(inbox, context)
        current_user = messages[-1]["content"] if messages else ""
        response_text = self._call_llm(messages, api_key)
        action = self._parse_response(response_text)

        self._update_fact_board(action, inbox, context)
        if current_user:
            self._append_history(current_user, response_text)

        action.raw_response = response_text
        return action

    def _format_known_agents(self, known: List[Dict]) -> str:
        lines = []
        for a in known:
            aid = a.get("agent_id", a.get("id", "?"))
            nm = a.get("name", aid)
            rl = a.get("role", "?")
            lines.append(f"  - agent_id={aid}  名称={nm}  角色={rl}")
        return "\n".join(lines) if lines else "  暂无"

    def _format_skills(self, skills: List[Dict]) -> str:
        if not skills:
            return ""
        lines = []
        for s in skills:
            sn = s.get("name", s.get("skill_name", "?"))
            sd = s.get("desc", s.get("description", ""))
            params = s.get("params", s.get("parameters", []))
            params_str = ", ".join(params) if params else ""
            lines.append(f"  - execute_skill: {sn}({params_str}) — {sd}")
        return "\n".join(lines)

    def _format_inbox(self, inbox: List[Dict]) -> str:
        direct_msgs, broadcast_msgs, system_msgs = [], [], []
        for msg in inbox[-15:]:
            mtype = msg.get("type", "direct")
            content = msg.get("content", "")
            line = f"  [{msg.get('from', '?')}]: {content}"
            if mtype == "system":
                system_msgs.append(f"  [系统]: {content}")
            elif mtype == "broadcast":
                broadcast_msgs.append(line)
            else:
                direct_msgs.append(line)

        parts = []
        if direct_msgs:
            parts.append("## 直接发给你的消息\n" + "\n".join(direct_msgs[-10:]))
        if broadcast_msgs:
            parts.append("## 广播消息\n" + "\n".join(broadcast_msgs[-5:]))
        if system_msgs:
            parts.append("## 系统通知 / 技能结果\n" + "\n".join(system_msgs[-3:]))
        if not parts:
            parts.append("（收件箱为空，请根据目标主动推进或等待更合适的时机）")
        if direct_msgs:
            parts.append(
                f"注意：你有 {len(direct_msgs)} 条未回复的直接消息。"
                "执行 skill 后结果会自动回复，不需要重复 send_message。"
            )
        return "\n\n".join(parts)

    def _build_system_message(self, context: Dict = None) -> str:
        """Stable prefix: global rules, tools, role and protocol."""
        context = context or {}
        known = context.get("known_agents", context.get("agents", []))
        goals_text = "\n".join(f"  {i + 1}. {g}" for i, g in enumerate(self.goals))
        skills_text = self._format_skills(context.get("skills_list", []))
        skill_block = (
            f"\n## 可用技能（使用 execute_skill 动作调用）\n{skills_text}\n"
            if skills_text
            else ""
        )
        return f"""{self.system_prompt}

## 你的身份
你的名字: {self.name}
你的角色: {self.role}

## 你的目标
{goals_text}

## 已知的其它 Agent（发消息时 target 必须用 agent_id）
{self._format_known_agents(known)}
{skill_block}
## 输出协议
只输出 JSON，不要输出其它内容：
```json
{{
  "reasoning": "一句话说明你的依据",
  "action": "send_message|execute_skill|analyze|plan|wait",
  "target": "目标 Agent 的 agent_id 或技能名",
  "content": "消息内容、技能参数或动作描述"
}}
```

行动指南：
- 收到任务指令时优先 execute_skill，技能结果会自动进入系统通知并回复发件人。
- send_message 的 target 必须用 agent_id，不能用中文名。
- 向全体 Agent 广播消息时，target 填 "0.0.0.0"。
- 消息内容要具体、有信息量。
- 用中文回复。"""

    def _build_user_message(self, inbox: List[Dict], context: Dict = None) -> str:
        """Dynamic suffix: round, inbox, events and skill results only."""
        context = context or {}
        return f"""## 当前动态状态
回合: {context.get("round", self.turn)}
总回合: {context.get("total_rounds", "未知")}
场景: {context.get("scene", "未知")}

{self._format_inbox(inbox)}

请基于上述最新动态状态做出本轮决策。"""

    def _build_messages(self, inbox: List[Dict], context: Dict = None) -> List[Dict[str, str]]:
        return [
            {"role": "system", "content": self._build_system_message(context)},
            {"role": "user", "content": self.fact_board.to_message_content()},
            *self.message_history,
            {"role": "user", "content": self._build_user_message(inbox, context)},
        ]

    def _build_prompt(self, inbox: List[Dict], context: Dict = None) -> str:
        """Compatibility helper for older callers/tests."""
        return "\n\n".join(m["content"] for m in self._build_messages(inbox, context))

    def _append_history(self, user_content: str, assistant_content: str):
        self.message_history.extend(
            [
                {"role": "user", "content": user_content},
                {"role": "assistant", "content": assistant_content},
            ]
        )
        keep = self.recent_history_turns * 2
        if keep <= 0:
            self.message_history = []
        elif len(self.message_history) > keep:
            self.message_history = self.message_history[-keep:]

    def _update_fact_board(self, action: Action, inbox: List[Dict], context: Dict = None):
        self.fact_board.add_inbox_facts(inbox or [])
        self.fact_board.add_action(action)

    def _messages_chars(self, messages: List[Dict[str, str]]) -> int:
        return sum(len(m.get("content", "")) for m in messages)

    def _call_llm(self, messages: List[Dict[str, str]], api_key: str) -> str:
        provider = self.config.get("provider", "auto")
        model = self.config.get("model", "")
        api_base = self.config.get("api_base", "")
        if api_key.startswith("sk-ant-") and provider != "openai":
            return self._call_anthropic(messages, api_key, model)
        return self._call_openai_compat(messages, api_key, model, api_base)

    def _call_anthropic(self, messages: List[Dict[str, str]], api_key: str, model: str = "") -> str:
        import anthropic
        from agent_network.llm_metrics import LLMCallTracker
        from .config import DEFAULT_LLM_MODEL

        model = model or DEFAULT_LLM_MODEL
        component = os.environ.get("EFFECTIVE_AGENT_ID") or os.environ.get("AGENT_ID", "brain")
        actor_id = os.environ.get("EFFECTIVE_AGENT_ID", "")
        actor_name = os.environ.get("EFFECTIVE_AGENT_NAME", "")
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        chat_messages = [m for m in messages if m.get("role") != "system"]

        with LLMCallTracker(
            provider="anthropic",
            model=model,
            method="POST",
            path="/v1/messages",
            host="api.anthropic.com",
            component=component,
            actor_id=actor_id,
            actor_name=actor_name,
            prompt_chars=self._messages_chars(messages),
            messages_count=len(messages),
            max_tokens=512,
        ) as tracker:
            client = anthropic.Anthropic(api_key=api_key)
            message = client.messages.create(
                model=model,
                max_tokens=512,
                system=system,
                messages=chat_messages,
            )
            text = message.content[0].text if message.content else ""
            usage = {}
            try:
                u = getattr(message, 'usage', None)
                if u:
                    usage = {
                        "input_tokens": getattr(u, 'input_tokens', 0),
                        "output_tokens": getattr(u, 'output_tokens', 0),
                        "cache_read_input_tokens": getattr(u, 'cache_read_input_tokens', None) or 0,
                        "cache_creation_input_tokens": getattr(u, 'cache_creation_input_tokens', None) or 0,
                    }
            except Exception:
                pass
            tracker.ok(response_chars=len(text), usage=usage)
        return text

    def _call_openai_compat(
        self,
        messages: List[Dict[str, str]],
        api_key: str,
        model: str = "",
        api_base: str = "",
    ) -> str:
        import httpx
        from agent_network.llm_metrics import LLMCallTracker
        from urllib.parse import urlparse

        model = model or "deepseek-chat"
        api_base = api_base or "https://api.deepseek.com/v1"
        url = f"{api_base.rstrip('/')}/chat/completions"
        parsed = urlparse(url)
        host = parsed.netloc
        provider = "deepseek" if "deepseek" in host else "openai"
        component = os.environ.get("EFFECTIVE_AGENT_ID") or os.environ.get("AGENT_ID", "brain")
        actor_id = os.environ.get("EFFECTIVE_AGENT_ID", "")
        actor_name = os.environ.get("EFFECTIVE_AGENT_NAME", "")

        with LLMCallTracker(
            provider=provider,
            model=model,
            method="POST",
            path="/v1/chat/completions",
            host=host,
            component=component,
            actor_id=actor_id,
            actor_name=actor_name,
            prompt_chars=self._messages_chars(messages),
            messages_count=len(messages),
            max_tokens=512,
        ) as tracker:
            resp = httpx.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": 512,
                    "temperature": 0.7,
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"].get("content", "")
            tracker.ok(
                response_chars=len(content or ""),
                status=str(resp.status_code),
                usage=data.get("usage", {}),
            )
            return content

    def _parse_response(self, text: str) -> Action:
        json_match = re.search(r'\{[\s\S]*"action"[\s\S]*\}', text or "")
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

        text_lower = (text or "").lower()
        if "发送" in (text or "") or "send" in text_lower:
            return Action(type="send_message", content=text or "", reasoning="从文本提取")
        if "搜索" in (text or "") or "search" in text_lower:
            return Action(type="search", content="目标区域", reasoning="从文本提取")
        if "等待" in (text or "") or "wait" in text_lower:
            return Action(type="wait", reasoning="从文本提取")
        return Action(type="wait", content="", reasoning=f"无法解析: {text}")


def create_brain(role: str, name: str, goals: List[str] = None, system_prompt: str = "") -> Brain:
    if goals is None:
        goals = ["完成指派任务"]
    return Brain(role=role, name=name, goals=goals, system_prompt=system_prompt)
