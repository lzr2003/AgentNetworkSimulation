# Scenes — 剧本编译器

`scenario.py` 将自然语言想法编译为多 Agent 仿真剧本，输出 4 个文件。

## 用法

### 前置条件

```bash
pip install httpx
```

环境变量 `ANTHROPIC_API_KEY` 设置为 DeepSeek API Key（`sk-` 格式）。建议写入 `.env` 文件（已在 `.gitignore` 中）。

### 命令行

```bash
python scenes/scenario.py -i "<一句话想法>" -d <输出目录>
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-i` `--idea` | 剧本想法或背景描述 | 新能源项目并网审批示例 |
| `-d` `--dir` | 输出目录 | `./scenarios/energy_project_v1` |

### 示例

```bash
python scenes/scenario.py \
  -i "一个关于电信运营商市场竞争博弈及内部协作的剧本" \
  -d ./scenes/telecom_operator_v1
```

---

## 输出文件

每次运行在目标目录下生成 4 个文件：

| 文件 | 用途 |
|------|------|
| `meta_and_roles.json` | 角色、身份与目标定义 |
| `instances_and_skills.json` | 容器运行配置与技能挂载 |
| `network_topology.json` | 角色间拓扑网络与通信连线 |
| `skills.py` | 技能可执行 Python 代码 |

---

## 数据字典

### 1. meta_and_roles.json

#### `scenario_metadata`

| 字段 | 类型 | 说明 |
|------|------|------|
| `title` | string | 剧本名称 |
| `global_rules` | string | 仿真世界规则与约束（周期、资源上限、法规限制等） |

#### `roles`

Key 为角色 ID（如 `GOV_REG`），Value 对象：

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 角色名称 |
| `model_backbone` | enum | 底层模型基座：`openclaw` 或 `claudecode` |
| `identity` | string | 角色身份与组织背景 |
| `core_goal` | string | 量化目标（含时限或指标） |
| `primary_interaction_paradigm` | enum | 主导互动范式：`INTERNAL_COLLABORATION` / `EXTERNAL_NEGOTIATION` / `ZERO_SUM_GAME` |

---

### 2. instances_and_skills.json

#### `container_instances`

Key 为角色 ID（与 `meta_and_roles.json` 中一一对应），Value 对象：

| 字段 | 类型 | 说明 |
|------|------|------|
| `runtime_engine` | string | 运行引擎名称（如 `agent_runtime_v2`） |
| `docker_image` | string | 推荐 Docker 基础镜像 |
| `pip_packages` | string[] | 容器拉起时自动安装的 Python 包及版本号 |

#### `skill_bindings`（数组元素）

| 字段 | 类型 | 说明 |
|------|------|------|
| `skill_name` | string | 技能函数名（与 `skills.py` 中注册名一致） |
| `endpoint` | string | 技能对应的 HTTP API 端点 URL |
| `description` | string | 技能作用与触发逻辑 |

---

### 3. network_topology.json

#### 顶层

| 字段 | 类型 | 说明 |
|------|------|------|
| `global_topology_type` | enum | 宏观拓扑类型：`STAR` / `MESH` / `TREE` / `RING` / `HYBRID_MESH` |
| `sub_networks` | object[] | 子网络列表，每个子网独立定义拓扑与连线 |

#### `sub_networks` 元素

| 字段 | 类型 | 说明 |
|------|------|------|
| `sub_id` | string | 子网唯一标识 |
| `topology_type` | enum | 局部拓扑类型：`STAR` / `MESH` / `TREE` / `RING` |
| `description` | string | 该层网络的业务含义 |
| `nodes` | string[] | 该子网包含的角色 ID 数组 |
| `edges` | object[] | 节点间连线列表 |

#### `edges` 元素

| 字段 | 类型 | 说明 |
|------|------|------|
| `source` | string | 源角色 ID |
| `target` | string | 目标角色 ID |
| `paradigm` | enum | 连线互动类型：`COLLABORATION` / `NEGOTIATION` / `GAME` |
| `channel_id` | string | 通信通道标识（如 `vlan_reg_101`） |

---

### 4. skills.py

可独立运行的 Python 模块。

#### SkillRegistry 类

| 方法 | 说明 |
|------|------|
| `register(name, func)` | 注册技能函数 |
| `execute(name, **kwargs)` | 按名称调用技能，返回 `{"status", "result", "data"}` |
| `_skills` | 类变量 dict，存储所有已注册技能 |

#### 返回值结构

```json
{
  "status": "success | error",
  "result": "<可读结果>",
  "data": { "<具体数据>" }
}
```

#### 每个技能函数

- 接受 `**kwargs` 参数
- 内置参数校验、边界检查、仿真逻辑（状态追踪、随机事件）
- 文件末尾通过 `SkillRegistry.register()` 注册

#### 导入与调用

```python
from scenes.network_collaboration.skills import SkillRegistry

SkillRegistry.execute("allocate_budget", department="研发部", amount=50000)
# → {"status": "success", "result": "Budget allocated", "data": {...}}
```
