# -*- coding: utf-8 -*-
"""
自动化剧本生成 Skill 核心驱动脚本 (多文件/复杂范式/复合拓扑升级版)
模型配置: 依托于平台/聚合托管的 DeepSeek-V4 (Anthropic SDK 兼容端点)
"""

import os
import json
import shutil

# =====================================================================
# 1. 升级版系统核心提示词 (注入给大模型的 SYSTEM 角色)
# =====================================================================
SYSTEM_PROMPT = """
你是一个顶级的"多Agent分布式网络仿真"剧本编译器。你的任务是根据用户的输入想法，自动生成一个高度复杂的仿真剧本。该剧本包含10个左右的角色节点。

【核心铁律】
1. 绝对禁止自然语言废话：所有输出必须符合即插即用的槽位设计，严格遵循后续给出的 JSON 结构。严禁在 JSON 代码块之外输出任何解释性文字。
2. 互动范式多样化（重点）：不要仅限制于单一博弈。你必须将互动场景细化并混合。互动连线和角色行为必须明确区分：
   - INTERNAL_COLLABORATION (机构/公司内部行政与技术协助)
   - EXTERNAL_NEGOTIATION (外部多方利益拉扯与商务谈判)
   - ZERO_SUM_GAME (纯粹零和博弈、资源抢夺或生存对抗)
3. 丰富多元的复合拓扑网络：严禁生成单一的环状结构（RING）。你需要根据剧本的业务逻辑，在以下标准拓扑中进行组合或嵌套选择：
   - STAR (中心化，如一CEO多下属，或一核心平台多供应商)
   - TREE (层级制，如监管局->大集团->子公司)
   - MESH (稠密网状，错综复杂的外部自由谈判桌)
   - RING (环状传递)
   你必须在剧本中声明总体拓扑类型（global_topology_type），并通过子网（sub_networks）的形式，把角色分入不同的拓扑层。
4. 异构模型底层：根据模型的特长和角色特征，每个角色必须明确它运行时依赖的底层基座模型（model_backbone），必须在 ['openclaw', 'claudecode'] 中二选一。例如内部协助侧、技术蓝图侧节点可倾向于 openclaw，涉及深度工程、对赌合同决策侧可倾向于 claudecode。
5. 技能绑定：每个角色在 instances_and_skills 中通过 skills 字段（字符串列表）挂载技能。技能名必须与 skills_code 中定义的函数名完全一致。技能数量按角色复杂度分配（核心角色2-4个，辅助角色1-2个）。
6. 技能可执行代码落地（重点）：你必须为 instances_and_skills 中出现的每一个 skill_name 生成对应的 Python 函数实现，放入 skills_code 字段。代码要求：
   - 是一个完整可独立运行的 Python 模块字符串（含 SkillRegistry 注册中心类 + 所有技能函数）
   - 每个技能函数接受 **kwargs 参数，返回 dict 结果（至少包含 status/result/data 字段）
   - 函数内部必须有意义的仿真逻辑：资源状态追踪（用模块级 dict 存储）、随机事件模拟（用 random 模块）、数值计算、边界校验
   - 函数命名用英文 snake_case，docstring 用中文写清楚用途
   - 字符串内缩进使用 4 空格，确保从 JSON 解析出来后是合法 Python 代码
7. 动机可量化收敛：角色的核心目标必须具体且带有时限或量化指标（例如：预算结余>50%），严禁日常社交闲聊。
8. 终止条件嵌入：scenario_metadata 必须包含 max_rounds（硬上限，3-30）和 stalemate_rounds（僵局检测阈值，2-10）。max_rounds 根据角色数量和剧本复杂度合理设定（10 角色左右建议 8-12 轮），stalemate_rounds 建议设为 3-5 轮。
9. 业务拓扑双轨制（核心）：通信信道层（network_topology）与业务合约层（business_topology）严格分离。
   - 通信信道层：固定不变，定义 Agent 间底层网络通道。
   - 业务合约层：仅定义初始种子连线 links（全部 NEGOTIATING），代表开局时已知的谈判意向。
     任何机构可以与任何运营商自由谈判（不限于 seeds），由技能在运行时动态创建新连线：
     * submit_bidding_proposal：运营商向机构报价时，若二者无连线则自动创建 NEGOTIATING 连线
     * evaluate_proposals：机构评估方案时，若与某运营商无连线则自动创建 NEGOTIATING 连线
     * sign_contract：将 NEGOTIATING → SIGNED；若机构已有其他 SIGNED 合约，自动触发旧连线 BREACH_FLASHING
     合同状态机：NEGOTIATING → SIGNED → BREACH_FLASHING → TERMINATED（移除）
     所有业务事件由 skills.py 运行时通过 event_log 动态产出，严禁写入静态 JSON。
10. 合约技能细化（核心）：角色技能必须包含招投标与合约操作，与 business_topology 状态机联动：
    - 机构侧必备：evaluate_proposals（评估多方报价，对比 contract_value 决定转投）、terminate_contract_with_flash（触发 BREACH_FLASHING → TERMINATED）、sign_contract（创建 SIGNED 连线）
    - 运营商侧必备：submit_bidding_proposal（提交竞标方案，包含报价/带宽/算力等参数）、process_breach_notification（接收被违约通知，触发挽留或报复策略）
    每个技能在 skills_code 中必须有完整实现，函数内部用模块级字典 active_contracts 追踪业务连线状态。
"""

# =====================================================================
# 2. 全新多文件图纸 JSON Schema 强约束结构
# =====================================================================
RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "multi_file_agent_simulation_schema",
        "strict": True, 
        "schema": {
            "type": "object",
            "properties": {
                # 模块一：角色、背景与范式声明 (对应 meta_and_roles.json)
                "meta_and_roles": {
                    "type": "object",
                    "properties": {
                        "scenario_metadata": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "description": "剧本名称"},
                                "global_rules": {"type": "string", "description": "物理世界规则与仿真时限约束"},
                                "max_rounds": {"type": "integer", "description": "硬上限：最大仿真轮数，到达后强制终止", "minimum": 3, "maximum": 30},
                                "stalemate_rounds": {"type": "integer", "description": "僵局检测：连续无消息轮数阈值，达到后判定僵局终止", "minimum": 2, "maximum": 10}
                            },
                            "required": ["title", "global_rules", "max_rounds", "stalemate_rounds"],
                            "additionalProperties": False
                        },
                        "roles": {
                            "type": "object",
                            "description": "剧本中包含的10个左右角色，Key为角色的标准ID（如 DEV_A1_CEO）",
                            "additionalProperties": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string", "description": "角色人类可读名称"},
                                    "model_backbone": {"type": "string", "enum": ["openclaw", "claudecode"], "description": "指定的底层模型基座"},
                                    "identity": {"type": "string", "description": "角色的身份特征与组织背景说明"},
                                    "core_goal": {"type": "string", "description": "量化的具体利益导向或终极指标"},
                                    "primary_interaction_paradigm": {"type": "string", "enum": ["INTERNAL_COLLABORATION", "EXTERNAL_NEGOTIATION", "ZERO_SUM_GAME"], "description": "角色主导的互动范式"}
                                },
                                "required": ["name", "model_backbone", "identity", "core_goal", "primary_interaction_paradigm"],
                                "additionalProperties": False
                            }
                        }
                    },
                    "required": ["scenario_metadata", "roles"],
                    "additionalProperties": False
                },
                # 模块二：角色技能绑定 (对应 instances_and_skills.json)
                "instances_and_skills": {
                    "type": "object",
                    "properties": {
                        "container_instances": {
                            "type": "object",
                            "description": "角色技能配置，Key必须与模块一中的角色ID严格一一对应",
                            "additionalProperties": {
                                "type": "object",
                                "properties": {
                                    "skills": {
                                        "type": "array",
                                        "description": "该角色绑定的技能名称列表，必须与 skills.py 中的函数名一一对应",
                                        "items": {"type": "string"}
                                    }
                                },
                                "required": ["skills"],
                                "additionalProperties": False
                            }
                        }
                    },
                    "required": ["container_instances"],
                    "additionalProperties": False
                },
                # 模块三：丰富拓扑结构关系网络 (对应 network_topology.json)
                "network_topology": {
                    "type": "object",
                    "properties": {
                        "global_topology_type": {"type": "string", "enum": ["STAR", "MESH", "TREE", "RING", "HYBRID_MESH"], "description": "全局宏观拓扑结构类型"},
                        "sub_networks": {
                            "type": "array",
                            "description": "划分的复合拓扑子网络列表，前端根据此配置铺设网络和UI样式",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "sub_id": {"type": "string", "description": "子网唯一ID"},
                                    "topology_type": {"type": "string", "enum": ["STAR", "MESH", "TREE", "RING"], "description": "此局部子网的拓扑类型"},
                                    "description": {"type": "string", "description": "该层网络的业务关联或物理含义"},
                                    "nodes": {
                                        "type": "array",
                                        "description": "包含在此子网中的角色ID数组",
                                        "items": {"type": "string"}
                                    },
                                    "edges": {
                                        "type": "array",
                                        "description": "节点间的连线拓扑",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "source": {"type": "string", "description": "源角色ID"},
                                                "target": {"type": "string", "description": "目标角色ID"},
                                                "paradigm": {"type": "string", "enum": ["COLLABORATION", "NEGOTIATION", "GAME"], "description": "连线代表的互动本质类型"},
                                                "channel_id": {"type": "string", "description": "物理或网络虚拟通道名称，如 vlan_bridge_102"}
                                            },
                                            "required": ["source", "target", "paradigm", "channel_id"],
                                            "additionalProperties": False
                                        }
                                    }
                                },
                                "required": ["sub_id", "topology_type", "description", "nodes", "edges"],
                                "additionalProperties": False
                            }
                        }
                    },
                    "required": ["global_topology_type", "sub_networks"],
                    "additionalProperties": False
                },
                # 模块四：技能可执行代码 (对应 skills.py)
                "skills_code": {
                    "type": "string",
                    "description": "完整的 Python 模块代码字符串，包含 SkillRegistry 注册中心类 + 所有技能的函数实现。技能必须包含机构侧（evaluate_proposals、terminate_contract_with_flash、sign_contract）和运营商侧（submit_bidding_proposal、process_breach_notification），每个函数内部追踪模块级业务状态字典 active_contracts。所有函数接受 **kwargs 返回 dict(status/result/data)。代码缩进用 4 空格，可直接 import 运行。"
                },
                # 模块五：业务合约种子拓扑 (对应 business_topology.json)
                "business_topology": {
                    "type": "object",
                    "properties": {
                        "links": {
                            "type": "array",
                            "description": "业务合约种子连线。仅为开局时的已知谈判意向，全部 NEGOTIATING。任何机构可与任何运营商自由谈判（不限种子），由技能 submit_bidding_proposal/evaluate_proposals 自动创建新连线。签约/违约/终止由 Agent 运行时自行决策。",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "source": {"type": "string", "description": "机构角色ID"},
                                    "target": {"type": "string", "description": "运营商角色ID"},
                                    "status": {"type": "string", "enum": ["NEGOTIATING"], "description": "初始状态固定为 NEGOTIATING"},
                                    "value": {"type": "number", "description": "预估合同标的金额"},
                                    "desc": {"type": "string", "description": "业务关系说明"}
                                },
                                "required": ["source", "target", "status", "value"],
                                "additionalProperties": False
                            }
                        }
                    },
                    "required": ["links"],
                    "additionalProperties": False
                }
            },
            "required": ["meta_and_roles", "instances_and_skills", "network_topology", "skills_code", "business_topology"],
            "additionalProperties": False
        }
    }
}

# =====================================================================
# 3. 核心接口调用与多文件自动拆分分发逻辑
# =====================================================================
def generate_and_dispatch_scenarios(user_idea: str, output_directory: str) -> None:
    """
    根据粗糙想法，调用 DeepSeek-V4 生成全套混合剧本数据，
    并在目标目录下自动生成独立的五个剧本文件：
    1. meta_and_roles.json
    2. instances_and_skills.json
    3. network_topology.json (通信信道层)
    4. skills.py
    5. business_topology.json (业务合约层 + event_stream)
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("未设置 ANTHROPIC_API_KEY 环境变量。")

    # 使用 DeepSeek OpenAI 兼容端点 (Bearer Token 认证，适配 sk- 格式 Key)
    import httpx
    api_base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    model_name = "deepseek-chat"

    # 注入 JSON Schema 约束
    schema_json = RESPONSE_SCHEMA["json_schema"]["schema"]
    schema_instruction = (
        f"\n你必须严格遵循以下全局 JSON Schema 输出，"
        f"不得附加任何自然语言包裹：\n"
        f"{json.dumps(schema_json, ensure_ascii=False, indent=2)}\n"
    )
    system_prompt = SYSTEM_PROMPT + schema_instruction

    print(">> 正在发起大模型调用，编译剧本网络...")
    import time as _time
    last_error = None
    for attempt in range(1, 4):
        try:
            resp = httpx.post(
                f"{api_base.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"请基于以下原始灵感编译一套包含丰富拓扑、多模型异构、复杂互动范式（协作/谈判/博弈）的中文复合剧本：{user_idea}"}
                    ],
                    "max_tokens": 16384,
                    "temperature": 0.7,
                    "response_format": {"type": "json_object"},
                },
                timeout=300.0,
            )
            resp.raise_for_status()
            data = resp.json()
            final_text = data["choices"][0]["message"]["content"]
            break
        except Exception as e:
            last_error = e
            if attempt < 3:
                print(f"   [重试 {attempt}/3] 调用失败: {e}，等待 {attempt*5}s 后重试...")
                _time.sleep(attempt * 5)
    else:
        raise RuntimeError(f"API 调用三次均失败，最后错误: {last_error}")

    # 安全解析响应文本，剔除 Markdown 包裹
    # final_text 已在上面从 OpenAI 兼容响应中获取
    final_text = final_text.strip()
    if final_text.startswith("```json"):
        final_text = final_text[7:]
    if final_text.startswith("```"):
        final_text = final_text[3:]
    if final_text.endswith("```"):
        final_text = final_text[:-3]
    final_text = final_text.strip()

    # 载入统一的大 JSON 字典
    full_blueprint = json.loads(final_text)

    # 自动化多文件分发写入控制器
    os.makedirs(output_directory, exist_ok=True)
    print(f">> 大模型返回数据解析成功。启动多文件（Multi-file Bundle）分发落盘至: {output_directory}")

    # 文件 1 分发：角色背景与互动范式定义
    file1_path = os.path.join(output_directory, "meta_and_roles.json")
    with open(file1_path, "w", encoding="utf-8") as f1:
        json.dump(full_blueprint["meta_and_roles"], f1, ensure_ascii=False, indent=2)
    print(f"   [落盘成功] -> {file1_path}")

    # 文件 2 分发：运行实例与挂载的工具包/环境依赖包
    file2_path = os.path.join(output_directory, "instances_and_skills.json")
    with open(file2_path, "w", encoding="utf-8") as f2:
        json.dump(full_blueprint["instances_and_skills"], f2, ensure_ascii=False, indent=2)
    print(f"   [落盘成功] -> {file2_path}")

    # 文件 3 分发：丰富子网拓扑结构网络
    file3_path = os.path.join(output_directory, "network_topology.json")
    with open(file3_path, "w", encoding="utf-8") as f3:
        json.dump(full_blueprint["network_topology"], f3, ensure_ascii=False, indent=2)
    print(f"   [落盘成功] -> {file3_path}")

    # 文件 4 分发：技能可执行代码
    skills_code = full_blueprint.get("skills_code", "")
    file4_path = os.path.join(output_directory, "skills.py")
    with open(file4_path, "w", encoding="utf-8") as f4:
        f4.write(skills_code)
    print(f"   [落盘成功] -> {file4_path}")

    # 文件 5 分发：业务合约拓扑 + 事件流
    file5_path = os.path.join(output_directory, "business_topology.json")
    with open(file5_path, "w", encoding="utf-8") as f5:
        json.dump(full_blueprint["business_topology"], f5, ensure_ascii=False, indent=2)
    print(f"   [落盘成功] -> {file5_path}")

    # =====================================================================
    # 5. 生成合并版目录 {output_dir}_merged/
    # =====================================================================
    merged_dir = output_directory.rstrip("/\\") + "_merged"
    os.makedirs(merged_dir, exist_ok=True)

    # 5a. 复制 skills.py（business_topology 已在合并 JSON 中，不单独复制）
    import shutil
    merged_skills = os.path.join(merged_dir, "skills.py")
    shutil.copyfile(file4_path, merged_skills)
    print(f"\n>> 生成合并版: {merged_dir}")
    print(f"   [落盘成功] -> {merged_skills}")

    # 5b. 构建合并 JSON
    meta_and_roles = full_blueprint["meta_and_roles"]
    containers = full_blueprint["instances_and_skills"]["container_instances"]
    topo = full_blueprint["network_topology"]
    biz_topo = full_blueprint.get("business_topology", {})
    roles = meta_and_roles["roles"]

    # 从 edges 构建双向 peers
    peer_map = {rid: set() for rid in roles}
    for subnet in topo.get("sub_networks", []):
        for edge in subnet.get("edges", []):
            src, dst = edge["source"], edge["target"]
            if src in roles:
                peer_map[src].add(dst)
            if dst in roles:
                peer_map[dst].add(src)

    # 合并 peers + skills 到每个 role
    merged_roles = {}
    for rid, role_data in roles.items():
        merged_roles[rid] = dict(role_data)
        merged_roles[rid]["peers"] = sorted(peer_map[rid])

        # 兼容 skill_bindings（旧格式）和 skills（新格式）
        ci = containers.get(rid, {})
        if "skills" in ci and isinstance(ci["skills"], list):
            s = ci["skills"]
            merged_roles[rid]["skills"] = [x["skill_name"] if isinstance(x, dict) else x for x in s]
        else:
            bindings = ci.get("skill_bindings", [])
            merged_roles[rid]["skills"] = [s["skill_name"] for s in bindings]

    # 目录名作为 merged 文件名
    merged_name = os.path.basename(output_directory.rstrip("/\\")) + "_merged.json"
    merged_json = {
        "scenario_metadata": meta_and_roles["scenario_metadata"],
        "global_topology_type": topo["global_topology_type"],
        "roles": merged_roles,
        "business_topology": biz_topo,
    }
    merged_json_path = os.path.join(merged_dir, merged_name)
    with open(merged_json_path, "w", encoding="utf-8") as f:
        json.dump(merged_json, f, ensure_ascii=False, indent=2)
    print(f"   [落盘成功] -> {merged_json_path}")

    # 快速结构验证打印
    print("\n======== 剧本包自动化编译完成，下游就绪 ========")
    total_roles = len(roles)
    total_edges = sum(len(peer_map[rid]) for rid in roles) // 2  # 双向去重
    total_skills = sum(len(merged_roles[rid]["skills"]) for rid in roles)
    total_biz_links = len(biz_topo.get("links", []))
    print(f"1. 声明节点总数: {total_roles}")
    print(f"2. 通信信道连线: {total_edges} (固定)")
    print(f"3. 宏观拓扑结构: {topo['global_topology_type']}")
    print(f"4. 包含子网络数: {len(topo['sub_networks'])}")
    print(f"5. 技能实现总数: {total_skills} (已写入 skills.py)")
    print(f"6. 业务合约初始连线: {total_biz_links} (全部 NEGOTIATING，运行时演进)")
    print(f"7. 合并版: {merged_dir}/")

# =====================================================================
# 4. 本地独立测试入口
# =====================================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="多文件/多范式 Agent 仿真网络沙盒编译器")
    parser.add_argument("--idea", "-i", type=str, 
                        default="一个大型新能源项目的并网审批。包含公司内部技术侧配合、与外部设备供应商的价格谈判、以及与其它开发商就电网有限配额的零和博弈。",
                        help="剧本概念的想法输入")
    parser.add_argument("--dir", "-d", type=str, default="./scenarios/energy_project_v1",
                        help="多文件包输出的目标目录夹")
    args = parser.parse_args()

    try:
        generate_and_dispatch_scenarios(args.idea, args.dir)
    except Exception as e:
        print(f"剧本包编译失败，错误详情: {e}")