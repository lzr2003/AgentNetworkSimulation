import random
import json

class SkillRegistry:
    _skills = {}

    @classmethod
    def register(cls, name):
        def decorator(func):
            cls._skills[name] = func
            return func
        return decorator

    @classmethod
    def execute(cls, name, **kwargs):
        if name not in cls._skills:
            return {"status": "error", "result": None, "data": {"error": f"Skill {name} not found"}}
        return cls._skills[name](**kwargs)

# Global state for resources (simplified)
_state = {
    "budgets": {
        "NAN_WANG_SAFETY": 2000000,
        "SZ_AIRPORT_SECURITY": 5000000
    },
    "contracts": [],
    "patents": [],
    "projects": []
}

@SkillRegistry.register("negotiate_contract")
def negotiate_contract(agent_id, target_id, amount, terms, **kwargs):
    """
    谈判合同：模拟两个角色之间的合同谈判，根据随机因素决定是否达成。
    参数：agent_id (角色ID), target_id (目标角色ID), amount (合同金额), terms (合同条款字符串)
    返回：status (success/failure), result (是否达成), data (合同详情)
    """
    success_prob = random.uniform(0.5, 1.0)
    if random.random() < success_prob:
        contract = {"agent": agent_id, "target": target_id, "amount": amount, "terms": terms}
        _state["contracts"].append(contract)
        return {"status": "success", "result": True, "data": {"contract": contract}}
    else:
        return {"status": "failure", "result": False, "data": {"reason": "谈判破裂"}}

@SkillRegistry.register("evaluate_proposal")
def evaluate_proposal(proposal, criteria, **kwargs):
    """
    评估提案：根据给定的标准对提案进行评分。
    参数：proposal (提案字符串), criteria (评估标准列表)
    返回：status, result (评分), data (评分细节)
    """
    score = random.randint(60, 100)
    return {"status": "success", "result": score, "data": {"proposal": proposal, "criteria": criteria, "score": score}}

@SkillRegistry.register("manage_budget")
def manage_budget(agent_id, expense, **kwargs):
    """
    管理预算：从指定角色的预算中扣除费用，并检查是否超支。
    参数：agent_id (角色ID), expense (支出金额)
    返回：status, result (剩余预算), data (预算详情)
    """
    if agent_id not in _state["budgets"]:
        return {"status": "error", "result": None, "data": {"error": "Unknown agent"}}
    current = _state["budgets"][agent_id]
    if expense > current:
        return {"status": "failure", "result": None, "data": {"error": "预算不足", "current": current, "expense": expense}}
    _state["budgets"][agent_id] -= expense
    return {"status": "success", "result": _state["budgets"][agent_id], "data": {"agent": agent_id, "remaining": _state["budgets"][agent_id]}}

@SkillRegistry.register("develop_algorithm")
def develop_algorithm(research_focus, complexity, **kwargs):
    """
    开发算法：模拟算法研发过程，返回算法性能指标。
    参数：research_focus (研究方向), complexity (复杂度1-10)
    返回：status, result (算法ID), data (算法性能)
    """
    accuracy = min(1.0, 0.5 + complexity * 0.05 + random.uniform(-0.1, 0.1))
    algo_id = f"ALGO_{random.randint(1000,9999)}"
    return {"status": "success", "result": algo_id, "data": {"algorithm_id": algo_id, "accuracy": accuracy, "complexity": complexity}}

@SkillRegistry.register("license_patent")
def license_patent(patent_id, licensee, fee, **kwargs):
    """
    专利授权：将专利授权给被许可方，收取费用。
    参数：patent_id (专利ID), licensee (被许可方角色ID), fee (授权费用)
    返回：status, result (是否成功), data (授权详情)
    """
    _state["patents"].append({"patent_id": patent_id, "licensee": licensee, "fee": fee})
    return {"status": "success", "result": True, "data": {"patent_id": patent_id, "licensee": licensee, "fee": fee}}

@SkillRegistry.register("collaborate_research")
def collaborate_research(partners, topic, duration, **kwargs):
    """
    合作研究：多个角色共同开展研究项目。
    参数：partners (合作伙伴角色ID列表), topic (研究主题), duration (持续时间轮数)
    返回：status, result (项目ID), data (项目详情)
    """
    project_id = f"PROJ_{random.randint(1000,9999)}"
    _state["projects"].append({"project_id": project_id, "partners": partners, "topic": topic, "duration": duration})
    return {"status": "success", "result": project_id, "data": {"project_id": project_id, "partners": partners, "topic": topic}}

@SkillRegistry.register("produce_device")
def produce_device(device_type, quantity, unit_cost, **kwargs):
    """
    生产设备：模拟设备生产，返回总成本和交付时间。
    参数：device_type (设备类型), quantity (数量), unit_cost (单位成本)
    返回：status, result (总成本), data (交付详情)
    """
    total_cost = quantity * unit_cost
    delivery_time = random.randint(2, 6)
    return {"status": "success", "result": total_cost, "data": {"device_type": device_type, "quantity": quantity, "total_cost": total_cost, "delivery_time": delivery_time}}

@SkillRegistry.register("price_negotiation")
def price_negotiation(base_price, target_price, **kwargs):
    """
    价格谈判：模拟价格博弈，返回最终成交价。
    参数：base_price (初始报价), target_price (目标价格)
    返回：status, result (成交价), data (谈判过程)
    """
    final_price = random.uniform(target_price, base_price)
    return {"status": "success", "result": final_price, "data": {"base": base_price, "target": target_price, "final": final_price}}

@SkillRegistry.register("supply_chain_manage")
def supply_chain_manage(order_id, supplier, quantity, **kwargs):
    """
    供应链管理：管理订单和库存。
    参数：order_id (订单ID), supplier (供应商), quantity (数量)
    返回：status, result (订单状态), data (供应链详情)
    """
    status = random.choice(["processing", "shipped", "delivered"])
    return {"status": "success", "result": status, "data": {"order_id": order_id, "supplier": supplier, "quantity": quantity, "status": status}}

@SkillRegistry.register("integrate_system")
def integrate_system(components, requirements, **kwargs):
    """
    系统集成：将多个组件集成为完整系统。
    参数：components (组件列表), requirements (需求列表)
    返回：status, result (集成ID), data (集成结果)
    """
    integration_id = f"INT_{random.randint(1000,9999)}"
    success_prob = 0.8
    if random.random() < success_prob:
        return {"status": "success", "result": integration_id, "data": {"integration_id": integration_id, "components": components, "requirements": requirements, "success": True}}
    else:
        return {"status": "failure", "result": None, "data": {"error": "集成失败", "components": components}}

@SkillRegistry.register("manage_project")
def manage_project(project_id, tasks, **kwargs):
    """
    项目管理：管理项目进度和资源。
    参数：project_id (项目ID), tasks (任务列表)
    返回：status, result (完成百分比), data (项目状态)
    """
    progress = random.randint(0, 100)
    return {"status": "success", "result": progress, "data": {"project_id": project_id, "tasks": tasks, "progress": progress}}

@SkillRegistry.register("audit_compliance")
def audit_compliance(agent_id, standards, **kwargs):
    """
    合规审计：检查角色是否符合标准。
    参数：agent_id (角色ID), standards (标准列表)
    返回：status, result (合规分数), data (审计详情)
    """
    score = random.randint(70, 100)
    return {"status": "success", "result": score, "data": {"agent": agent_id, "standards": standards, "score": score}}

@SkillRegistry.register("set_standard")
def set_standard(standard_name, requirements, **kwargs):
    """
    制定标准：创建新的行业标准。
    参数：standard_name (标准名称), requirements (要求列表)
    返回：status, result (标准ID), data (标准详情)
    """
    std_id = f"STD_{random.randint(1000,9999)}"
    return {"status": "success", "result": std_id, "data": {"standard_id": std_id, "name": standard_name, "requirements": requirements}}

@SkillRegistry.register("approve_solution")
def approve_solution(solution_id, criteria, **kwargs):
    """
    批准方案：根据标准批准解决方案。
    参数：solution_id (方案ID), criteria (批准标准)
    返回：status, result (是否批准), data (批准详情)
    """
    approved = random.random() > 0.3
    return {"status": "success", "result": approved, "data": {"solution_id": solution_id, "criteria": criteria, "approved": approved}}
