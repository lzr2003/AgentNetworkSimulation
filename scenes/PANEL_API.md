# 场景 Panel 可用 API 速查

本文整理 `scenes/<scene_name>/panel.html` 中常用的前端接口。Panel 由主页面 iframe 加载，路径为：

```text
GET /api/scenes/{scene_name}/panel
```

所有接口建议使用相对路径，便于同源部署：

```js
const API = "/api";
```

## 推荐数据入口

### 1. 当前运行场景状态

```http
GET /api/scenes/state
```

用于“当前已 setup/launch 的场景”面板。适合嵌入主页面的场景面板，因为主页面切换场景后，后端会维护当前活动场景。

返回结构：

```json
{
  "scene": "tech_campus",
  "running": true,
  "round": 3,
  "max_rounds": 30,
  "agents": [
    {
      "agent_id": "DEV_FE",
      "name": "前端开发工程师",
      "role": "openclaw",
      "status": "thinking",
      "x": 120,
      "y": 80
    }
  ],
  "custom": {}
}
```

字段说明：

| 字段 | 含义 |
|------|------|
| `scene` | 当前活动场景名称 |
| `running` | 仿真是否运行中 |
| `round` | 当前轮次 |
| `max_rounds` | 当前场景最大轮次 |
| `agents` | 当前 AgentRegistry 中的 Agent 状态列表 |
| `custom` | 当前场景 `skills.py#get_panel_state()` 返回的自定义数据 |

建议：

- 动态面板优先轮询此接口。
- 轮询间隔建议 `500ms` 到 `1000ms`。
- 若 `custom` 为 `null`，面板应降级到只展示 `agents/round/running`。

### 3. 静态剧本配置

```http
GET /api/scenes/{scene_name}
```

读取场景文件夹中的基础 JSON 配置。

返回结构：

```json
{
  "name": "tech_campus",
  "title": "华为范式科技园区仿真",
  "format": "folder",
  "files": {
    "meta_and_roles": {},
    "instances_and_skills": {},
    "network_topology": {}
  }
}
```

注意：

- 当前接口只返回 `meta_and_roles.json`、`instances_and_skills.json`、`network_topology.json`。
- 如果面板需要 `business_topology.json` 等额外文件，当前不会由此接口返回，需要在面板中内置兜底数据，或后续扩展后端接口。

### 4. 场景列表

```http
GET /api/scenes
```

返回可用场景：

```json
{
  "scenes": [
    {"name": "tech_campus", "format": "folder"}
  ]
}
```

一般由主页面使用；场景 panel 通常不需要直接调用。

## 日志与网络数据

Panel 如果要展示日志、业务消息或网络层观测，可使用以下接口。

### 查询统一日志

```http
GET /api/logs?limit=100
GET /api/logs?layer=agent_application&limit=100
GET /api/logs?layer=agent_network&limit=100
GET /api/logs?agent_id=DEV_AI&limit=50
GET /api/logs?event=llm_api_packet&limit=50
```

常用过滤参数：

| 参数 | 含义 |
|------|------|
| `agent_id` | 过滤 `actor.id` |
| `level` | `INFO` / `WARN` / `ERROR` |
| `event` | 事件名，如 `agent_message`、`llm_api_packet` |
| `layer` | `agent_application` 或 `agent_network` |
| `category` | 日志分类 |
| `keyword` | 在摘要、payload、network 中搜索 |
| `limit` | 返回条数，最大 1000 |

### Agent 应用层业务消息

```http
GET /api/logs/messages?limit=100
```

只返回 `event == "agent_message"` 的业务通信，不包含 Docker HTTP 观测。

### Agent 网络层日志

```http
GET /api/logs/network?limit=100
```

返回 `layer == "agent_network"` 的日志，包括：

- `docker_http_inbound`
- `docker_http_outbound`
- `llm_api_packet`

适合网络态势、南北向/东西向流量统计类 panel。

### PacketRecorder 内存报文

```http
GET /api/packets?limit=100
GET /api/packets?agent_id=DEV_AI&direction=outbound
GET /api/packets/stats
```

这是内存中的模拟报文记录，不是 JSONL 日志文件。适合展示 Agent 间通信轨迹。

## 专用场景接口

### 扫雷棋盘

```http
GET /api/minesweeper/board
```

仅供扫雷类面板使用。

返回结构：

```json
{
  "board": [[null, 1, -1]],
  "soldiers": {
    "soldier_01": {"x": 1, "y": 2}
  },
  "discovered_mines": [{"x": 2, "y": 0}],
  "game_state": "RUNNING",
  "cells_revealed": 12,
  "completion_percentage": "18.2%"
}
```

## WebSocket

```js
const ws = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`);
ws.onopen = () => ws.send("all");
```

客户端可发送：

| 消息 | 返回内容 |
|------|----------|
| `status` | Agent 状态、统计、最近日志、关系 |
| `logs` | 最近日志与日志统计 |
| `packets` | 最近 PacketRecorder 报文和统计 |
| `all` | Agent、日志、报文统计、关系的组合快照 |

服务端也可能主动推送：

| `type` | 含义 |
|--------|------|
| `status` | 周期性状态快照 |
| `agent_status` | Agent 状态变化 |
| `agent_log` | Agent 决策/执行日志 |
| `logs` | 日志快照 |
| `packets` | 报文快照 |
| `all` | 综合快照 |

简单面板建议使用 HTTP 轮询；需要低延迟动画时再使用 WebSocket。

## Panel 编写建议

- 静态剧本信息使用 `/api/scenes/{scene_name}`。
- 当前运行态使用 `/api/scenes/state`。
- 场景私有运行态优先放在 `skills.py#get_panel_state()`，由 `/api/scenes/state.custom` 或 `/api/scenes/{scene_name}/state` 读取。
- 所有轮询都应有失败兜底，避免 iframe 空白。
- 动态轮询建议停止条件：
  - `running === false`
  - 或连续多次请求失败
  - 或场景结束状态已明确
- 面板不要调用会改变仿真状态的接口，除非明确设计为控制面板。

## 不建议在普通 Panel 中调用的接口

以下接口会改变系统状态，普通可视化面板应避免直接调用：

- `POST /api/simulations/setup`
- `POST /api/simulations/launch`
- `POST /api/simulations/run`
- `POST /api/simulations/stop`
- `POST /api/skills/execute`
- `POST /api/tools/execute`
- `POST /api/logs/ingest`

这些接口应由主控制台或明确的控制型面板调用。
