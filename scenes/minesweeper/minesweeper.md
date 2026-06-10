# 基于多Agent星状协同的分布式扫雷博弈仿真手册

## 1. 游戏机制 (Game Mechanics)

* **棋盘定义**：采用 **9x9** 的共享虚拟网格，全局随机分布 **10 枚地雷**。
* **星状协同机制**：
    * **指挥官 (CMDR_01)**：掌握全局“盲图”视角的决策权，但无法直接探测格子，依靠小兵上报的情报构建雷区图谱。
    * **探雷兵 (SOLDIER_01 ~ 09)**：不具备全局视角，只能听从指挥官的宏观指令移动到指定坐标进行局部探测。
* **小兵不死机制 (Non-lethal Recon)**：前线小兵若踩到地雷**不会被淘汰或清除**。系统会将该坐标标记为“已知雷区”并向指挥官返回雷区情报，小兵在下一轮继续保持活跃并参与探测。
* **时序同步与终止机制**：
    * **1个全局轮次 (Round)**：指挥官下达调度指令 $\rightarrow$ 9个小兵并发调用技能 $\rightarrow$ 系统结算棋盘状态。
    * **成功终止**：成功翻开除 10 枚地雷外的其余 **71 个安全网格**（胜率推进至 100%）。
    * **硬性熔断**：最大全局轮次上限为 **35 轮**。
    * **卡死收敛终止**：若连续 **3 轮**（`stalemate_rounds: 3`）全图探测进度（安全格翻开增量）为 0，系统判定仿真陷入死循环，自动切断并熔断。

---

## 2. 角色设置 (Role Configurations)

| 角色ID | 角色名称 | 基座模型 | 核心目标与行为模式 |
| :--- | :--- | :--- | :--- |
| **CMDR_01** | 扫雷行动最高指挥官 | `claudecode` | 战术大脑。整合多源异步情报，规避已知雷区，在每轮向 9 个小兵分发最优的移动探测坐标。 |
| **SOLDIER_01 ~ 09** | 探雷侦察兵 (共9人) | `openclaw` | 前线执行单元。严格响应指挥官的坐标指令，执行探测技能，实时上报踩雷事件或邻近雷数。 |

---

## 3. 数据字典 (Data Dictionary)

### 3.1 实体实例配置 (`instances_and_skills.json`)
* `container_instances`: `object`，所有运行的 Agent 容器实例集合。
    * `{Agent_ID}`: `object`，具体 Agent 实例。
        * `runtime_engine`: `string`，固定为 `"docker"`。
        * `docker_image`: `string`，运行时镜像（如 `"python:3.10-slim"`）。
        * `pip_packages`: `array[string]`，依赖包列表（如 `["numpy==1.24.3", "requests==2.31.0"]`）。
        * `skill_bindings`: `array[object]`，该实例绑定的技能域（**新嵌套结构**）。
            * `skill_name`: `string`，技能唯一标识符。
            * `endpoint`: `string`，技能的 API 路由网关。
            * `description`: `string`，技能的业务含义与调用约束。

### 3.2 技能输入与输出参数 (`skills.py`)

#### 技能一：`initialize_minesweeper_map` (地图初始化)
* **输入参数**：无
* **输出参数说明 (`data` 域)**：
    * `map_size`: `string`，棋盘规格（固定为 `"9x9"`）。
    * `total_mines`: `integer`，总雷数（固定为 `10`）。
    * `initial_safe_zone`: `object`，系统初始赠送的第一个安全格位置。包含 `x`, `y` 坐标及 `adjacent_mines`（周围雷数）。

#### 技能二：`move_and_reconnaissance` (移动与侦察)
* **输入参数**：
    * `soldier_id`: `string`，发起调用的侦察兵ID。
    * `x`: `integer`，探测目标列索引（`0 ~ 8`）。
    * `y`: `integer`，探测目标行索引（`0 ~ 8`）。
    * `current_round`: `integer`，当前全局仿真轮次，用于锁定时序防刷。
* **输出参数说明 (`data` 域)**：
    * `event`: `string`，事件标识。`"SAFE_RECON"` (安全区) 或 `"FOUND_MINE_REPORT"` (踩雷情报)。
    * `is_alive`: `boolean`，小兵是否存活。由于小兵不死机制，**恒为 `true`**。
    * `adjacent_mines`: `integer`，仅在 `SAFE_RECON` 时返回，周围 8 格的雷数（`0 ~ 8`）。
    * `game_over_trigger`: `boolean`，是否触发全图清空胜利。
    * `map_progress`: `string`，当前安全格翻开进度（格式如 `"15/71 safe cells revealed."`）。

#### 技能三：`query_game_status` (全局状态审计)
* **输入参数**：
    * `current_round`: `integer`，当前轮次。
* **输出参数说明 (`data` 域)**：
    * `game_state`: `string`，全局状态。`"RUNNING"` (运行中) 或 `"ALL_MAP_CLEARED_VICTORY"` (胜利)。
    * `cells_revealed`: `integer`，当前已翻开的安全格总数。
    * `completion_percentage`: `string`，整体仿真推进百分比。
    * `total_discovered_mines_count`: `integer`，当前已被小兵用身体探测出来的地雷总数。
    * `discovered_mines_coordinates`: `array[string]`，所有已知雷区的具体坐标列表。