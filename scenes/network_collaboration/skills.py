import random
import json

class SkillRegistry:
    _skills = {}

    @classmethod
    def register(cls, name, func):
        cls._skills[name] = func

    @classmethod
    def execute(cls, name, **kwargs):
        if name not in cls._skills:
            return {"status": "error", "result": None, "data": {"error": f"Skill {name} not found"}}
        return cls._skills[name](**kwargs)

def allocate_budget(**kwargs):
    """
    分配预算给各部门。
    参数：department (str), amount (float)
    返回：分配后的预算状态
    """
    if 'department' not in kwargs or 'amount' not in kwargs:
        return {"status": "error", "result": None, "data": {"error": "Missing parameters"}}
    dept = kwargs['department']
    amount = kwargs['amount']
    if amount < 0:
        return {"status": "error", "result": None, "data": {"error": "Negative amount"}}
    if not hasattr(allocate_budget, 'budget_pool'):
        allocate_budget.budget_pool = 1000000.0
    if amount > allocate_budget.budget_pool:
        return {"status": "error", "result": None, "data": {"error": "Insufficient budget"}}
    allocate_budget.budget_pool -= amount
    if not hasattr(allocate_budget, 'allocations'):
        allocate_budget.allocations = {}
    allocate_budget.allocations[dept] = allocate_budget.allocations.get(dept, 0) + amount
    return {"status": "success", "result": "Budget allocated", "data": {"department": dept, "amount": amount, "remaining_pool": allocate_budget.budget_pool}}
SkillRegistry.register('allocate_budget', allocate_budget)

def approve_strategy(**kwargs):
    """
    批准或驳回战略提案。
    参数：proposal (str), risk_score (float)
    返回：批准状态
    """
    if 'proposal' not in kwargs or 'risk_score' not in kwargs:
        return {"status": "error", "result": None, "data": {"error": "Missing parameters"}}
    risk = kwargs['risk_score']
    if risk < 0 or risk > 1:
        return {"status": "error", "result": None, "data": {"error": "Risk score out of range"}}
    approved = risk < 0.5
    return {"status": "success", "result": "Approved" if approved else "Rejected", "data": {"proposal": kwargs['proposal'], "risk_score": risk, "approved": approved}}
SkillRegistry.register('approve_strategy', approve_strategy)

def assign_technical_task(**kwargs):
    """
    分配技术任务给开发组。
    参数：task (str), developer (str), complexity (int)
    返回：任务分配结果
    """
    if 'task' not in kwargs or 'developer' not in kwargs or 'complexity' not in kwargs:
        return {"status": "error", "result": None, "data": {"error": "Missing parameters"}}
    complexity = kwargs['complexity']
    if complexity < 1 or complexity > 10:
        return {"status": "error", "result": None, "data": {"error": "Complexity out of range"}}
    if not hasattr(assign_technical_task, 'task_queue'):
        assign_technical_task.task_queue = []
    assign_technical_task.task_queue.append(kwargs['task'])
    return {"status": "success", "result": "Task assigned", "data": {"task": kwargs['task'], "developer": kwargs['developer'], "complexity": complexity, "queue_length": len(assign_technical_task.task_queue)}}
SkillRegistry.register('assign_technical_task', assign_technical_task)

def evaluate_tech_debt(**kwargs):
    """
    评估当前技术债务。
    参数：无
    返回：技术债务评分（0-100）
    """
    debt = random.randint(20, 80)
    return {"status": "success", "result": debt, "data": {"tech_debt_score": debt}}
SkillRegistry.register('evaluate_tech_debt', evaluate_tech_debt)

def calculate_budget(**kwargs):
    """
    计算预算使用情况。
    参数：department (str)
    返回：预算使用率
    """
    if 'department' not in kwargs:
        return {"status": "error", "result": None, "data": {"error": "Missing department"}}
    if not hasattr(calculate_budget, 'budget_usage'):
        calculate_budget.budget_usage = {}
    usage = calculate_budget.budget_usage.get(kwargs['department'], random.uniform(0.5, 0.9))
    return {"status": "success", "result": usage, "data": {"department": kwargs['department'], "usage_rate": usage}}
SkillRegistry.register('calculate_budget', calculate_budget)

def audit_expense(**kwargs):
    """
    审计部门支出。
    参数：department (str)
    返回：审计结果（合规/违规）
    """
    if 'department' not in kwargs:
        return {"status": "error", "result": None, "data": {"error": "Missing department"}}
    compliant = random.random() > 0.2
    return {"status": "success", "result": "Compliant" if compliant else "Violation", "data": {"department": kwargs['department'], "compliant": compliant}}
SkillRegistry.register('audit_expense', audit_expense)

def plan_sprint(**kwargs):
    """
    规划迭代任务。
    参数：sprint_id (str), tasks (list)
    返回：规划结果
    """
    if 'sprint_id' not in kwargs or 'tasks' not in kwargs:
        return {"status": "error", "result": None, "data": {"error": "Missing parameters"}}
    if not isinstance(kwargs['tasks'], list):
        return {"status": "error", "result": None, "data": {"error": "Tasks must be a list"}}
    return {"status": "success", "result": "Sprint planned", "data": {"sprint_id": kwargs['sprint_id'], "task_count": len(kwargs['tasks'])}}
SkillRegistry.register('plan_sprint', plan_sprint)

def track_progress(**kwargs):
    """
    跟踪项目进度。
    参数：project (str)
    返回：完成百分比
    """
    if 'project' not in kwargs:
        return {"status": "error", "result": None, "data": {"error": "Missing project"}}
    progress = random.randint(0, 100)
    return {"status": "success", "result": progress, "data": {"project": kwargs['project'], "progress": progress}}
SkillRegistry.register('track_progress', track_progress)

def code_review(**kwargs):
    """
    代码审查。
    参数：code_snippet (str), reviewer (str)
    返回：审查结果（通过/需修改）
    """
    if 'code_snippet' not in kwargs or 'reviewer' not in kwargs:
        return {"status": "error", "result": None, "data": {"error": "Missing parameters"}}
    passed = random.random() > 0.3
    return {"status": "success", "result": "Passed" if passed else "Needs revision", "data": {"reviewer": kwargs['reviewer'], "passed": passed}}
SkillRegistry.register('code_review', code_review)

def estimate_effort(**kwargs):
    """
    估算开发工作量。
    参数：task (str)
    返回：人天估算
    """
    if 'task' not in kwargs:
        return {"status": "error", "result": None, "data": {"error": "Missing task"}}
    effort = random.randint(1, 20)
    return {"status": "success", "result": effort, "data": {"task": kwargs['task'], "effort_days": effort}}
SkillRegistry.register('estimate_effort', estimate_effort)

def negotiate_contract(**kwargs):
    """
    与客户谈判合同。
    参数：client (str), initial_offer (float)
    返回：谈判结果（接受/拒绝/还价）
    """
    if 'client' not in kwargs or 'initial_offer' not in kwargs:
        return {"status": "error", "result": None, "data": {"error": "Missing parameters"}}
    acceptance_prob = min(1.0, kwargs['initial_offer'] / 100000)
    if random.random() < acceptance_prob:
        return {"status": "success", "result": "Accepted", "data": {"client": kwargs['client'], "final_price": kwargs['initial_offer']}}
    else:
        counter = kwargs['initial_offer'] * random.uniform(0.8, 0.95)
        return {"status": "success", "result": "Counteroffer", "data": {"client": kwargs['client'], "counter_offer": counter}}
SkillRegistry.register('negotiate_contract', negotiate_contract)

def generate_lead(**kwargs):
    """
    生成潜在客户列表。
    参数：industry (str)
    返回：客户列表
    """
    if 'industry' not in kwargs:
        return {"status": "error", "result": None, "data": {"error": "Missing industry"}}
    leads = [f"Client_{i}" for i in range(random.randint(1, 5))]
    return {"status": "success", "result": leads, "data": {"industry": kwargs['industry'], "lead_count": len(leads)}}
SkillRegistry.register('generate_lead', generate_lead)

def offer_pricing(**kwargs):
    """
    提供定价方案。
    参数：service (str), volume (int)
    返回：报价
    """
    if 'service' not in kwargs or 'volume' not in kwargs:
        return {"status": "error", "result": None, "data": {"error": "Missing parameters"}}
    price = 1000 * kwargs['volume'] * random.uniform(0.9, 1.1)
    return {"status": "success", "result": price, "data": {"service": kwargs['service'], "volume": kwargs['volume'], "price": price}}
SkillRegistry.register('offer_pricing', offer_pricing)

def adjust_terms(**kwargs):
    """
    调整合同条款。
    参数：contract_id (str), new_terms (dict)
    返回：调整结果
    """
    if 'contract_id' not in kwargs or 'new_terms' not in kwargs:
        return {"status": "error", "result": None, "data": {"error": "Missing parameters"}}
    return {"status": "success", "result": "Terms adjusted", "data": {"contract_id": kwargs['contract_id'], "new_terms": kwargs['new_terms']}}
SkillRegistry.register('adjust_terms', adjust_terms)

def propose_service(**kwargs):
    """
    提出服务方案。
    参数：client (str), service (str)
    返回：方案详情
    """
    if 'client' not in kwargs or 'service' not in kwargs:
        return {"status": "error", "result": None, "data": {"error": "Missing parameters"}}
    cost = random.randint(5000, 20000)
    return {"status": "success", "result": "Proposal created", "data": {"client": kwargs['client'], "service": kwargs['service'], "cost": cost}}
SkillRegistry.register('propose_service', propose_service)

def renegotiate(**kwargs):
    """
    重新谈判条款。
    参数：contract_id (str), desired_change (str)
    返回：谈判结果
    """
    if 'contract_id' not in kwargs or 'desired_change' not in kwargs:
        return {"status": "error", "result": None, "data": {"error": "Missing parameters"}}
    success = random.random() > 0.5
    return {"status": "success", "result": "Accepted" if success else "Rejected", "data": {"contract_id": kwargs['contract_id'], "desired_change": kwargs['desired_change']}}
SkillRegistry.register('renegotiate', renegotiate)

def poach_customer(**kwargs):
    """
    挖走客户。
    参数：customer (str), incentive (float)
    返回：是否成功
    """
    if 'customer' not in kwargs or 'incentive' not in kwargs:
        return {"status": "error", "result": None, "data": {"error": "Missing parameters"}}
    success = kwargs['incentive'] > random.uniform(1000, 5000)
    return {"status": "success", "result": "Poached" if success else "Failed", "data": {"customer": kwargs['customer'], "incentive": kwargs['incentive']}}
SkillRegistry.register('poach_customer', poach_customer)

def price_war(**kwargs):
    """
    发起价格战。
    参数：product (str), discount (float)
    返回：市场反应
    """
    if 'product' not in kwargs or 'discount' not in kwargs:
        return {"status": "error", "result": None, "data": {"error": "Missing parameters"}}
    if kwargs['discount'] < 0 or kwargs['discount'] > 1:
        return {"status": "error", "result": None, "data": {"error": "Invalid discount"}}
    market_share_change = random.uniform(-0.05, 0.1)
    return {"status": "success", "result": "Price war initiated", "data": {"product": kwargs['product'], "discount": kwargs['discount'], "market_share_change": market_share_change}}
SkillRegistry.register('price_war', price_war)

def inspect_compliance(**kwargs):
    """
    检查合规性。
    参数：company (str)
    返回：合规状态
    """
    if 'company' not in kwargs:
        return {"status": "error", "result": None, "data": {"error": "Missing company"}}
    compliant = random.random() > 0.3
    return {"status": "success", "result": "Compliant" if compliant else "Non-compliant", "data": {"company": kwargs['company'], "compliant": compliant}}
SkillRegistry.register('inspect_compliance', inspect_compliance)

def impose_fine(**kwargs):
    """
    施加罚款。
    参数：company (str), violation (str)
    返回：罚款金额
    """
    if 'company' not in kwargs or 'violation' not in kwargs:
        return {"status": "error", "result": None, "data": {"error": "Missing parameters"}}
    fine = random.randint(1000, 10000)
    return {"status": "success", "result": fine, "data": {"company": kwargs['company'], "violation": kwargs['violation'], "fine": fine}}
SkillRegistry.register('impose_fine', impose_fine)
