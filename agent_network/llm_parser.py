"""
LLM 脚本解析器 — 用 AI 将自然语言剧本解析为结构化 Agent 配置

支持:
- Anthropic API (Claude)
- OpenAI-compatible API
- 自动检测可用的 API Key

解析流程:
自然语言 → LLM → JSON Scene Definition → Agent 创建 & 任务调度
"""

import json
import os
import re

from .config import DEFAULT_LLM_MODEL
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field, asdict


@dataclass
class AgentDef:
    """LLM 解析出的单个 Agent 定义"""
    agent_id: str
    role: str
    name: str
    skills: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    tasks: List[str] = field(default_factory=list)  # 该 agent 要执行的任务
    extra_meta: Dict[str, Any] = field(default_factory=dict)  # script_json 扩展字段


@dataclass
class SceneDefinition:
    """LLM 解析出的完整场景定义"""
    scene_name: str = ""
    description: str = ""
    agents: List[AgentDef] = field(default_factory=list)
    workflow: List[Dict[str, Any]] = field(default_factory=list)  # 任务执行顺序
    event_triggers: List[Dict[str, Any]] = field(default_factory=list)  # script_json 事件

    def to_workflow_steps(self):
        """
        将 workflow 字典列表转换为 WorkflowStep 对象列表

        支持两种格式:
        1. 新格式: {"step_id": "s1", "type": "task", "agent_id": "...", "depends_on": [...]}
        2. 旧格式: {"step": 1, "agent": "agent_id", "action": "..."} → 自动转换
        """
        from .workflow import WorkflowStep

        steps = []
        for i, wf in enumerate(self.workflow):
            # 检测旧格式: 有 "step" 字段但没有 "step_id"
            if "step" in wf and "step_id" not in wf:
                step_id = f"step-{wf['step']}"
                agent_id = wf.get("agent", "")
                action = wf.get("action", "")
                # 旧格式线性依赖（除了第一步，其他依赖前一步）
                deps = [f"step-{wf['step'] - 1}"] if wf["step"] > 1 else []
                wf = {
                    "step_id": step_id,
                    "type": "task",
                    "agent_id": agent_id,
                    "action": action,
                    "depends_on": deps,
                    "description": wf.get("description", f"Step {wf['step']}"),
                }

            step = WorkflowStep.from_dict(wf)
            steps.append(step)

        return steps


# Agent 角色模板库
ROLE_TEMPLATES = {
    "scout": {
        "skills": ["intelligence_collection", "reconnaissance"],
        "tags": ["blue_force", "recon"],
    },
    "commander": {
        "skills": ["strategy_planning", "command", "analysis"],
        "tags": ["blue_force", "command"],
    },
    "analyst": {
        "skills": ["data_analysis", "intelligence_collection"],
        "tags": ["blue_force", "analysis"],
    },
    "support": {
        "skills": ["logistics", "report_generation"],
        "tags": ["blue_force", "support"],
    },
    "observer": {
        "skills": ["reconnaissance", "monitoring"],
        "tags": ["blue_force", "observer"],
    },
    "generic": {
        "skills": ["intelligence_collection"],
        "tags": ["blue_force"],
    },
}

SCENE_TEMPLATES = {
    "battlefield": SceneDefinition(
        scene_name="战场推演",
        agents=[
            AgentDef("scout-001", "scout", "侦察兵", ["intelligence_collection", "reconnaissance"],
                     ["blue_force", "recon"], ["搜索敌军位置并分析地形"]),
            AgentDef("commander-001", "commander", "指挥官", ["strategy_planning", "command", "analysis"],
                     ["blue_force", "command"], ["接收情报", "制定攻击方案并下达指令"]),
        ],
    ),
    "fleet": SceneDefinition(
        scene_name="编队推演",
        agents=[
            AgentDef("scout-fleet-a", "scout", "侦察兵A", ["intelligence_collection", "reconnaissance"],
                     ["blue_force", "recon", "alpha_team"], ["搜索敌军雷达信号"]),
            AgentDef("scout-fleet-b", "scout", "侦察兵B", ["intelligence_collection", "reconnaissance"],
                     ["blue_force", "recon", "bravo_team"], ["收集目标区域地形数据"]),
            AgentDef("cmd-fleet", "commander", "指挥官", ["strategy_planning", "command", "analysis"],
                     ["blue_force", "command"], ["综合分析多路情报，制定联合作战方案"]),
        ],
    ),
}


def get_api_config() -> Dict[str, str]:
    """获取 LLM API 配置，优先级: 环境变量 > 配置文件"""
    config = {
        "provider": "auto",           # "anthropic" | "openai" | "auto"
        "api_key": "",
        "api_base": "",
        "model": "",
    }

    # Anthropic
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        config["api_key"] = anthropic_key
        config["provider"] = "anthropic"
        config["model"] = os.environ.get("ANTHROPIC_MODEL", DEFAULT_LLM_MODEL)

    # OpenAI (优先于 Anthropic，如果同时存在)
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if openai_key:
        config["api_key"] = openai_key
        config["provider"] = "openai"
        config["api_base"] = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")
        config["model"] = os.environ.get("OPENAI_MODEL", "gpt-4o")

    # 自定义 API Base
    custom_base = os.environ.get("LLM_API_BASE", "")
    if custom_base:
        config["api_base"] = custom_base
        config["provider"] = os.environ.get("LLM_PROVIDER", "openai")

    custom_key = os.environ.get("LLM_API_KEY", "")
    if custom_key:
        config["api_key"] = custom_key

    custom_model = os.environ.get("LLM_MODEL", "")
    if custom_model:
        config["model"] = custom_model

    return config


SYSTEM_PROMPT = """你是一个仿真剧本解析器。将用户的自然语言描述解析为 JSON 场景定义。

## 输出格式
严格按以下 JSON Schema 输出，不要输出其他内容:
```json
{
  "scene_name": "场景名称",
  "description": "场景背景与规则描述，会作为每个Agent的系统提示词",
  "agents": [
    {
      "agent_id": "唯一ID (如 trader-001)",
      "role": "角色类型 (scout/commander/analyst/support/observer/custom)",
      "name": "中文名称",
      "skills": ["技能1", "技能2"],
      "tags": ["标签1"],
      "tasks": ["要执行的任务描述"],
      "core_goal": "该角色要达成的核心目标",
      "hidden_secret": "该角色隐藏的秘密、弱点和不可告人的动机",
      "action_space": ["具体行动1","具体行动2","具体行动3"],
      "initial_assets": {"funding": 100, "influence": 50},
      "backend": "brain"
    }
  ],
  "workflow": [
    {
      "step_id": "step-1",
      "type": "task",
      "agent_id": "trader-001",
      "action": "搜索敌军位置",
      "depends_on": [],
      "description": "可选，步骤说明"
    },
    {
      "step_id": "step-2",
      "type": "task",
      "agent_id": "commander-001",
      "action": "分析情报并制定方案",
      "depends_on": ["step-1"],
      "description": "依赖侦察完成"
    }
  ]
}
```

## 工作流步骤类型
- task: 执行 Agent 任务（需 agent_id + action）
- wait: 等待 N 秒（需 params: {"seconds": N}）
- parallel: 并行块（需 sub_steps: [...多个步骤...]）
- condition: 条件分支（需 condition 表达式 + branches: {"true": [...], "false": [...]}）

## 依赖编排
- 使用 depends_on 字段定义步骤间依赖关系: ["前置步骤的step_id"]
- 无依赖的步骤会自动并行执行
- 例如：侦察完成 → 分析情报 → 制定方案 → 执行方案

## 角色类型
- scout: 侦察兵，负责情报收集、地形侦察、搜索
- commander: 指挥官，负责分析情报、制定方案、下达指令
- analyst: 分析员，负责数据分析、情报评估
- support: 支援兵，负责后勤、物资
- observer: 观察员，负责监视、预警

## 技能参考
- intelligence_collection: 情报收集
- reconnaissance: 侦察
- strategy_planning: 策略规划
- command: 指挥
- analysis: 分析
- monitoring: 监视
- logistics: 后勤
- report_generation: 报告生成
- data_analysis: 数据分析

## 规则
1. 根据用户描述的角色数量创建对应数量的 Agent
2. 为每个 Agent 分配合适的角色、技能和任务
3. 设计合理的工作流步骤依赖顺序
4. 可并行执行的步骤（无依赖关系的）放在同一层级
5. Agent 名称用中文
6. tags 至少包含一个分类标签
7. 步骤之间通过 depends_on 形成 DAG，不要让所有步骤串行
8. 如果用户提到"同时"、"并行"等关键词，使用 parallel 类型
9. core_goal 是每个角色的核心驱动力，必须具体明确
10. hidden_secret 是角色的隐藏秘密或弱点，用于增加戏剧张力和决策变数，若用户未提及则合理推断
11. action_space 是角色可以执行的5-8个具体行动，用动词短语表达
12. initial_assets 是角色的初始资源，若用户未提及数字则根据角色类型合理设定
13. backend 指定角色使用的AI后端: "brain"(默认推理), "claudecode"(Claude Code CLI), "openclaw"(Anthropic API)
"""


def parse_with_llm(script: str, config: Dict[str, str] = None) -> SceneDefinition:
    """
    使用 LLM 解析自然语言剧本

    Args:
        script: 用户的自然语言描述
        config: API 配置，如不提供则从环境变量读取

    Returns:
        SceneDefinition: 结构化的场景定义
    """
    if config is None:
        config = get_api_config()

    api_key = config.get("api_key", "")
    if not api_key:
        raise ValueError("未配置 LLM API Key。请设置 ANTHROPIC_API_KEY 或 OPENAI_API_KEY 环境变量")

    provider = config.get("provider", "auto")
    model = config.get("model", "")
    api_base = config.get("api_base", "")

    # 自动检测 provider
    if provider == "auto":
        # 根据 model 名检测 deepseek
        if "deepseek" in model.lower():
            provider = "deepseek"
        elif api_key.startswith("sk-ant-"):
            provider = "anthropic"
        elif api_key.startswith("sk-"):
            provider = "openai"

    # DeepSeek 使用 OpenAI 兼容 API
    if provider == "deepseek":
        if not api_base:
            api_base = "https://api.deepseek.com/v1"
        provider = "openai"  # 走 OpenAI 兼容路径

    print(f"[LLM Parser] 使用 {provider} API, model={model or 'default'}, base={api_base or 'default'}")

    if provider == "anthropic":
        return _parse_with_anthropic(script, api_key, model)
    else:
        return _parse_with_openai(script, api_key, model, api_base)


def _parse_with_anthropic(script: str, api_key: str, model: str = "") -> SceneDefinition:
    """使用 Anthropic Claude API 解析"""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    model = model or DEFAULT_LLM_MODEL

    message = client.messages.create(
        model=model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": script}],
    )

    response_text = message.content[0].text
    return _extract_json(response_text)


def _parse_with_openai(script: str, api_key: str, model: str = "", api_base: str = "") -> SceneDefinition:
    """使用 OpenAI-compatible API 解析"""
    import requests

    model = model or "gpt-4o"
    api_base = api_base or "https://api.openai.com/v1"
    url = f"{api_base.rstrip('/')}/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": script},
        ],
        "max_tokens": 1024,
        "temperature": 0.3,
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    response_text = data["choices"][0]["message"]["content"]
    return _extract_json(response_text)


def _extract_json(text: str) -> SceneDefinition:
    """从 LLM 响应中提取 JSON 并解析为 SceneDefinition"""
    # 尝试提取 ```json ... ``` 代码块
    json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if json_match:
        text = json_match.group(1)

    # 尝试找到 JSON 对象
    json_match = re.search(r'\{[\s\S]*"scene_name"[\s\S]*\}', text)
    if json_match:
        text = json_match.group(0)

    data = json.loads(text)

    agents = []
    for a in data.get("agents", []):
        # 填充默认值（从角色模板）
        role = a.get("role", "generic")
        template = ROLE_TEMPLATES.get(role, ROLE_TEMPLATES["generic"])

        agent = AgentDef(
            agent_id=a.get("agent_id", f"agent-{role}"),
            role=role,
            name=a.get("name", role),
            skills=a.get("skills", template["skills"]),
            tags=a.get("tags", template["tags"]),
            tasks=a.get("tasks", []),
            # LLM 解析的 extra_meta: 让 Agent 获得完整的上下文
            extra_meta={
                "identity": a.get("name", role),
                "core_goal": a.get("core_goal", ""),
                "hidden_secret": a.get("hidden_secret", ""),
                "action_space": a.get("action_space", a.get("skills", [])),
                "initial_assets": a.get("initial_assets", {}),
                "background_rules": data.get("description", ""),
                "backend": a.get("backend", "brain"),
            },
        )
        agents.append(agent)

    return SceneDefinition(
        scene_name=data.get("scene_name", "自定义场景"),
        description=data.get("description", ""),
        agents=agents,
        workflow=data.get("workflow", []),
    )


def parse_script(script: str, use_llm: bool = True, config: Dict[str, str] = None) -> SceneDefinition:
    """
    解析剧本 — 智能选择 LLM 或模板匹配

    Args:
        script: 自然语言剧本
        use_llm: 是否使用 LLM（False 时使用关键词模板匹配）
        config: API 配置

    Returns:
        SceneDefinition
    """
    if use_llm:
        try:
            api_config = config or get_api_config()
            if api_config.get("api_key"):
                return parse_with_llm(script, api_config)
        except Exception as e:
            print(f"[LLM Parser] LLM 解析失败，回退到模板匹配: {e}")

    # 回退：模板匹配
    return _template_match(script)


def _cn_num(ch: str) -> int:
    """中文数字转整数: 一→1, 两→2, 三→3, ..."""
    cn_map = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if ch in cn_map:
        return cn_map[ch]
    try:
        return int(ch)
    except ValueError:
        return 0


def _template_match(script: str) -> SceneDefinition:
    """关键词模板匹配（回退方案）"""
    s = script.lower()

    # 按角色检测数量: "3个侦察兵", "2名分析员", "5架无人机" 等
    scout_count = 0
    scout_match = re.search(r'(\d+)\s*(?:个|名|位|架|辆)?\s*侦察', script)
    if scout_match:
        scout_count = int(scout_match.group(1))
    else:
        # 检查 "两个侦察兵" 等中文数字
        cn_nums = {"两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8}
        for cn, n in cn_nums.items():
            if cn + "个侦察" in script or cn + "名侦察" in script:
                scout_count = n
                break

    analyst_count = 0
    analyst_match = re.search(r'(\d+|[一两三四五六七八九])\s*(?:个|名|位)?\s*分析', script)
    if analyst_match:
        raw = analyst_match.group(1)
        analyst_count = _cn_num(raw)

    support_count = 0
    support_match = re.search(r'(\d+|[一两三四五六七八九])\s*(?:个|名|位)?\s*(?:支援|后勤)', script)
    if support_match:
        raw = support_match.group(1)
        support_count = _cn_num(raw)

    # 通用数字检测（没有特定角色关联时）
    generic_nums = re.findall(r'(\d+)\s*(?:个|名|位|架|辆)', script)
    generic_count = int(generic_nums[0]) if generic_nums else 1

    # 是否多 Agent 场景
    multi_flags = [
        "2个" in s, "两个" in s, "3个" in s, "三个" in s,
        "多个" in s, "多路" in s, "编队" in s, "fleet" in s,
        "协同" in s, "联合" in s,
    ]
    total_detected = scout_count + analyst_count + support_count
    is_multi = any(multi_flags) or total_detected >= 2 or generic_count >= 2

    if is_multi:
        agents = []

        # 侦察兵
        if scout_count == 0 and total_detected == 0:
            # 没有明确角色关联，用通用数字
            scout_count = max(1, generic_count - 1)  # 至少留1个给指挥官
        elif scout_count == 0 and is_multi:
            scout_count = 1  # 默认至少1个侦察兵

        for i in range(scout_count):
            tasks = ["搜索敌军情报"] if i == 0 else (
                "收集雷达信号数据" if i == 1 else f"侦察区域-{chr(65+i)}"
            )
            agents.append(AgentDef(
                f"scout-{i+1:03d}", "scout", f"侦察兵-{chr(65+i)}",
                ["intelligence_collection", "reconnaissance"],
                ["blue_force", "recon", f"team_{chr(97+i)}"],
                [tasks],
            ))

        # 分析员
        for i in range(analyst_count):
            agents.append(AgentDef(
                f"analyst-{i+1:03d}", "analyst", f"分析员-{chr(65+i)}",
                ["data_analysis", "intelligence_collection"],
                ["blue_force", "analysis"],
                ["分析情报数据并生成报告"],
            ))

        # 支援兵
        for i in range(support_count):
            agents.append(AgentDef(
                f"support-{i+1:03d}", "support", f"支援兵-{chr(65+i)}",
                ["logistics", "report_generation"],
                ["blue_force", "support"],
                ["提供后勤支援和物资保障"],
            ))

        # 指挥官（至少1个）
        agents.append(AgentDef(
            "commander-001", "commander", "指挥官",
            ["strategy_planning", "command", "analysis"],
            ["blue_force", "command"],
            ["汇总情报并制定作战方案"],
        ))

        desc_parts = []
        if scout_count: desc_parts.append(f"{scout_count} 个侦察兵")
        if analyst_count: desc_parts.append(f"{analyst_count} 个分析员")
        if support_count: desc_parts.append(f"{support_count} 个支援兵")
        desc_parts.append("1 个指挥官")

        return SceneDefinition(
            scene_name="自定义编队推演",
            description=f"由 {' + '.join(desc_parts)} 组成的编队",
            agents=agents,
        )

    # 单 Agent 场景（无明确多Agent指示）→ 默认 1 scout + 1 commander
    return SceneDefinition(
        scene_name="自定义推演",
        agents=[
            AgentDef("scout-001", "scout", "侦察兵",
                     ["intelligence_collection", "reconnaissance"],
                     ["blue_force", "recon"],
                     ["搜索敌军位置并分析地形"]),
            AgentDef("commander-001", "commander", "指挥官",
                     ["strategy_planning", "command", "analysis"],
                     ["blue_force", "command"],
                     ["接收情报并制定方案"]),
        ],
    )
