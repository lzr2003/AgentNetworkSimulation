# 大规模仿真剧本 — 通用规范

> 适用于所有 `{name}_scale/` 场景。基于统计建模，不实例化个体 Agent。

## 文件结构

```
{name}_scale/
├── scale_config.json    ← Agent 分类 + 拓扑规则 + 流量参数
├── skills.py            ← 原子技能函数 + 全局状态 + 动态行为剖面
└── scale_spawner.py     ← (可选) 统计调度器
```

| 文件 | 与小规模对应 | 说明 |
|------|------------|------|
| `scale_config.json` | meta_and_roles + instances + network_topology | 三合一纯配置 |
| `skills.py` | 同源 skills.py | 原子技能 + `get_panel_state()` + `get_dynamic_behavior()` |
| `scale_spawner.py` | (无) | 独立于 server.py 的统计调度器 |

---

## 实现逻辑

### 平台启动

```
1. 加载 scale_config.json → agent_categories[10] / network_generation_rules[4] / ...
2. 按 spawn_count 分配 agent 数量 → 总计 ~1M
3. 按 persona_templates[].ratio 分配子人格
4. 按 network_generation_rules 生成拓扑边
5. 导入 skills.py
```

### 每轮循环

```
┌─ 轮次开始 ──────────────────────────────────────┐
│                                                   │
│  1. skills.get_dynamic_behavior(round, state)     │
│     → 读取当前 coverage_pct / budget / ap_count   │
│     → 判定阶段 (bootstrap→aggressive→refine→finalize) │
│     → 返回各分类的动态 actions_weight + traffic_mix │
│                                                   │
│  2. 按 actions_weight 抽样 → 每个 agent 本轮技能 │
│     scale_config 静态值作为冷启动基线              │
│     get_dynamic_behavior 返回值覆盖静态基线        │
│                                                   │
│  3. 批量执行技能                                  │
│     skills.py 原子函数更新全局状态                 │
│     (coverage_reports / cost_estimates / etc.)    │
│                                                   │
│  4. 按 traffic_mix + avg_payload_kb 生成流量统计  │
│                                                   │
└─────────────────────────────────────────────────┘
```

### 自演进闭环

```
scale_config.json (静态基线)
        ↓ 冷启动
get_dynamic_behavior() (状态感知)
        ↓ 返回动态权重
平台调度 agent 行为
        ↓ 执行技能
skills.py 全局状态变更
        ↓ 下一轮
get_dynamic_behavior() 读取新状态 → 自动切换阶段
```

阶段判定基于当前仿真状态，非固定轮次：

| 条件 | 阶段 | 行为倾向 |
|------|------|---------|
| `ap_count == 0` | `bootstrap` | AI 大批量生成 AP 位置 |
| `coverage < 60%` | `aggressive` | 全力部署新 AP |
| `60% ≤ coverage < 95%` | `refine` | 精细调整 + 迁移优化 |
| `coverage ≥ 95%` | `finalize` | 停止部署 → 验收归档 |
| `budget_remaining < 0` | `optimize_only` | 禁新增，只优化现有布局 |

---

## 数据字典

### scale_config.json

| 路径 | 说明 |
|------|------|
| `_description` / `version` / `target_scale` | 元信息 |
| `agent_categories[10]` | 分类定义 (见下表) |
| `network_generation_rules[4]` | 子网生成规则 (见下表) |
| `network_global_constraints` | 全局上限：`{max_total_edges, max_edges_per_agent, bandwidth_limits_gbps{EW/NS/INT}}` |
| `traffic_generation_rules` | 三类流量参数：`{description, generated_by[], avg_requests_per_agent_per_round, size_distribution{min/p50/p95/max_kb}}` |
| `mapping_rules.rules[]` | `{category_id, subnets[], roles[]}` — 分类→子网映射，`roles`: `hub`/`spoke`/`peer` |
| `scaling_parameters` | `{total_agents_target, llm_enabled_ratio, scale_factors{agent_count/edges/traffic}}` |

#### agent_categories[] — 分类对象

| 字段 | 类型 | 说明 |
|------|------|------|
| `category_id` | string | 分类 ID，对应 mapping_rules |
| `spawn_count` | `{base, scale_factor}` | 基准数量 × 缩放系数 |
| `model_backbone` | `{enabled, llm_ratio}` | LLM 启用率 (0.0–1.0) |
| `skills[]` | string[] | 技能池，与 skills.py 注册名一致 |
| `persona_templates[]` | `[{role, ratio, ...}]` | 子人格分布，ratio 合计 = 1.0 |
| `behavior_profile` | object | 静态行为基线 (动态调整前) |
| `.actions_per_10_rounds` | `{skill: [min,max], idle: [min,max]}` | 每10轮各动作执行次数范围 |
| `.traffic_mix` | `{EAST_WEST, NORTH_SOUTH, INTERNAL}` | 三类流量占比，合计 1.0 |
| `.avg_payload_kb` | `{EAST_WEST, NORTH_SOUTH, INTERNAL}` | 平均负载 (KB) |
| `topology_constraints` | `{max_peers_per_agent, preferred_connection_categories[], connection_affinity{cat:ratio}}` | 拓扑连接约束 |

#### network_generation_rules[] — 子网规则

| 字段 | 说明 |
|------|------|
| `sub_id` / `topology_type` / `description` | 子网 ID + `STAR`/`MESH` + 业务描述 |
| `generation_rule` | `STAR_HUB`(含 hub/spoke_categories) 或 `INTRA_CATEGORY_FULL_INTER_CATEGORY_SPARSE`(含 source/target_categories) |
| `edge_density` / `max_total_edges` | 边密度 + 硬上限 |
| `paradigm` / `channel_prefix` | `COLLABORATION`/`NEGOTIATION` + VLAN 前缀 |

### skills.py

| 元素 | 说明 |
|------|------|
| `SkillRegistry` | 技能注册中心：`register(name,fn)` / `execute(name,**kwargs)` / `list_skills()` |
| 技能函数 | 每函数接受 `**kwargs`，返回 `{status, result, data}`，内部操作模块级状态变量 |
| `get_panel_state()` | 返回当前全局状态快照，供前端消费 |
| `get_dynamic_behavior()` | **(自演进核心)** 平台每轮调用，基于状态返回动态行为剖面 |

### skills.py 全局状态

| 变量 | 结构 | 说明 |
|------|------|------|
| `ap_placements[]` | `{id, x, y, radius, cost, status}` | 已部署 AP |
| `coverage_reports[]` | `{round, coverage_pct, blind_spots, ap_count, total_cost}` | 覆盖报告 |
| `cost_estimates[]` | `{round, ap_count, unit_cost, total_cost, budget_remaining}` | 成本记录 |
| `ai_call_log[]` | `{round, caller, request_type, latency_ms, tokens}` | AI 调用日志 |
| `event_log[]` | `{event_type, round, source, target, action, detail}` | 事件日志 |
| `traffic_log[]` | `{round, type, source, target, action, bytes}` | 流量日志 |

**流量类型**: `EAST_WEST`(Agent间协作) / `NORTH_SOUTH`(外部API) / `INTERNAL`(内部管理)

---

## 场景间差异

| | ap_deployment_scale | tech_campus_scale | telecom_bidding_test_scale |
|---|---|---|---|
| 业务域 | AP 部署规划 | 科技园区协作 | 电信招投标 |
| 分类数 | 10 | 10 | 9 |
| 子网数 | 4 | 5 | 5 |
| LLM 分类 | ai_assistant(3K, 100%) | 同 | 同 |
| 自演进 | `get_dynamic_behavior` | 待实现 | 待实现 |
| spawner | 待补 | ✅ | ✅ |
| 业务拓扑 | 无 | 无 | ✅ (contract lifecycle) |
