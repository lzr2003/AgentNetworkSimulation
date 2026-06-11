import random

class SkillRegistry:
    """注册中心，管理所有技能函数。"""
    _skills = {}

    @classmethod
    def register(cls, name):
        def decorator(func):
            cls._skills[name] = func
            return func
        return decorator

    @classmethod
    def get(cls, name):
        return cls._skills.get(name)

    @classmethod
    def list_skills(cls):
        return list(cls._skills.keys())

# 全局状态追踪
_state = {
    'thu_budget': 200.0,  # 万元
    'zzu_budget': 150.0,
    'rfid_orders': 0.0,
    'wifi_orders': 0.0,
    'asset_orders': 0.0,
    'deployment_progress': 0.0,  # 0-100%
    'accuracy': 0.0,
    'compliance_passed': False,
    'round': 0
}

@SkillRegistry.register('evaluate_vendor_proposals')
def evaluate_vendor_proposals(**kwargs):
    """评估供应商方案，返回评分。"""
    proposals = kwargs.get('proposals', [])
    scores = []
    for p in proposals:
        cost = p.get('cost', 100)
        tech = random.uniform(0.5, 1.0)
        score = tech * 100 - cost * 0.1
        scores.append(score)
    return {'status': 'success', 'result': max(scores) if scores else 0, 'data': {'scores': scores}}

@SkillRegistry.register('allocate_budget')
def allocate_budget(**kwargs):
    """分配预算，返回剩余预算。"""
    amount = kwargs.get('amount', 0)
    entity = kwargs.get('entity', 'thu')
    if entity == 'thu':
        if _state['thu_budget'] >= amount:
            _state['thu_budget'] -= amount
            return {'status': 'success', 'result': _state['thu_budget'], 'data': {'allocated': amount}}
        else:
            return {'status': 'fail', 'result': _state['thu_budget'], 'data': {'error': 'Insufficient budget'}}
    else:
        if _state['zzu_budget'] >= amount:
            _state['zzu_budget'] -= amount
            return {'status': 'success', 'result': _state['zzu_budget'], 'data': {'allocated': amount}}
        else:
            return {'status': 'fail', 'result': _state['zzu_budget'], 'data': {'error': 'Insufficient budget'}}

@SkillRegistry.register('coordinate_deployment')
def coordinate_deployment(**kwargs):
    """协调部署进度，返回进度百分比。"""
    progress_increment = kwargs.get('increment', random.uniform(5, 15))
    _state['deployment_progress'] = min(100, _state['deployment_progress'] + progress_increment)
    return {'status': 'success', 'result': _state['deployment_progress'], 'data': {'increment': progress_increment}}

@SkillRegistry.register('report_progress')
def report_progress(**kwargs):
    """报告当前进度。"""
    return {'status': 'success', 'result': _state['deployment_progress'], 'data': {'accuracy': _state['accuracy']}}

@SkillRegistry.register('propose_rfid_solution')
def propose_rfid_solution(**kwargs):
    """提出RFID解决方案报价。"""
    base_cost = random.uniform(80, 120)
    return {'status': 'success', 'result': base_cost, 'data': {'solution': 'RFID', 'cost': base_cost}}

@SkillRegistry.register('propose_wifi_solution')
def propose_wifi_solution(**kwargs):
    """提出WiFi解决方案报价。"""
    base_cost = random.uniform(60, 100)
    return {'status': 'success', 'result': base_cost, 'data': {'solution': 'WiFi', 'cost': base_cost}}

@SkillRegistry.register('negotiate_price')
def negotiate_price(**kwargs):
    """谈判价格，返回最终价格。"""
    asking = kwargs.get('asking', 100)
    discount = random.uniform(0.05, 0.15)
    final = asking * (1 - discount)
    return {'status': 'success', 'result': final, 'data': {'original': asking, 'discount': discount}}

@SkillRegistry.register('provide_tech_support')
def provide_tech_support(**kwargs):
    """提供技术支持，提升准确率。"""
    accuracy_boost = random.uniform(0.01, 0.05)
    _state['accuracy'] = min(1.0, _state['accuracy'] + accuracy_boost)
    return {'status': 'success', 'result': _state['accuracy'], 'data': {'boost': accuracy_boost}}

@SkillRegistry.register('propose_integration_plan')
def propose_integration_plan(**kwargs):
    """提出系统集成方案报价。"""
    cost = random.uniform(30, 60)
    return {'status': 'success', 'result': cost, 'data': {'plan': 'integration', 'cost': cost}}

@SkillRegistry.register('customize_software')
def customize_software(**kwargs):
    """定制软件，提升兼容性。"""
    compat = random.uniform(0.8, 1.0)
    return {'status': 'success', 'result': compat, 'data': {'compatibility': compat}}

@SkillRegistry.register('assess_network_impact')
def assess_network_impact(**kwargs):
    """评估网络影响，返回风险等级。"""
    risk = random.choice(['low', 'medium', 'high'])
    return {'status': 'success', 'result': risk, 'data': {'impact': risk}}

@SkillRegistry.register('deploy_infrastructure')
def deploy_infrastructure(**kwargs):
    """部署基础设施，返回部署状态。"""
    success = random.random() > 0.1
    return {'status': 'success' if success else 'fail', 'result': success, 'data': {'deployed': success}}

@SkillRegistry.register('monitor_performance')
def monitor_performance(**kwargs):
    """监控性能，返回性能指标。"""
    latency = random.uniform(10, 100)
    return {'status': 'success', 'result': latency, 'data': {'latency_ms': latency}}

@SkillRegistry.register('approve_payment')
def approve_payment(**kwargs):
    """审批付款，返回审批结果。"""
    amount = kwargs.get('amount', 0)
    approved = random.random() > 0.2
    return {'status': 'success', 'result': approved, 'data': {'amount': amount, 'approved': approved}}

@SkillRegistry.register('audit_expenditure')
def audit_expenditure(**kwargs):
    """审计支出，返回审计报告。"""
    total = kwargs.get('total', 0)
    compliant = total <= _state['thu_budget'] + _state['zzu_budget']
    return {'status': 'success', 'result': compliant, 'data': {'total': total, 'compliant': compliant}}

@SkillRegistry.register('negotiate_payment_terms')
def negotiate_payment_terms(**kwargs):
    """谈判支付条款，返回分期方案。"""
    installments = random.randint(2, 4)
    return {'status': 'success', 'result': installments, 'data': {'installments': installments}}

@SkillRegistry.register('review_compliance')
def review_compliance(**kwargs):
    """审查合规性，返回是否通过。"""
    passed = random.random() > 0.3
    _state['compliance_passed'] = passed
    return {'status': 'success', 'result': passed, 'data': {'compliance': passed}}

@SkillRegistry.register('issue_certification')
def issue_certification(**kwargs):
    """颁发认证，返回认证编号。"""
    cert_id = f"CERT-{random.randint(1000,9999)}"
    return {'status': 'success', 'result': cert_id, 'data': {'cert_id': cert_id}}

@SkillRegistry.register('audit_data_security')
def audit_data_security(**kwargs):
    """审计数据安全，返回安全评分。"""
    score = random.uniform(70, 100)
    return {'status': 'success', 'result': score, 'data': {'security_score': score}}