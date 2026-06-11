import random

class SkillRegistry:
    """技能注册中心，管理所有可执行技能函数"""
    _skills = {}

    @classmethod
    def register(cls, func):
        cls._skills[func.__name__] = func
        return func

    @classmethod
    def execute(cls, skill_name, **kwargs):
        if skill_name not in cls._skills:
            return {"status": "error", "result": None, "data": {"error": f"Skill {skill_name} not found"}}
        return cls._skills[skill_name](**kwargs)

# 资源状态追踪
resource_state = {
    "bank_budget": 1000,  # 万元
    "gov_budget": 800,
    "uni_budget": 300,
    "ai_contracts": 0,
    "agent_a_revenue": 500,
    "agent_b_revenue": 500,
    "agent_c_revenue": 200,
    "outsourcer_contract": 400,
    "standard_progress": 0,  # 0-100
}

@SkillRegistry.register
def negotiate_contract(**kwargs):
    """谈判合同：根据双方立场达成协议，影响预算和收入"""
    actor = kwargs.get("actor", "unknown")
    target = kwargs.get("target", "unknown")
    offer = kwargs.get("offer", 0)
    # 简单随机博弈
    acceptance = random.random()
    if acceptance > 0.6:
        # 接受：更新资源
        if actor == "BANK_DC":
            resource_state["bank_budget"] -= offer
        elif actor == "GOV_INFRA":
            resource_state["gov_budget"] -= offer
        elif actor == "UNIV_NET":
            resource_state["uni_budget"] -= offer
        if target == "AI_PROVIDER":
            resource_state["ai_contracts"] += 1
        return {"status": "success", "result": "accepted", "data": {"actor": actor, "target": target, "offer": offer}}
    else:
        return {"status": "success", "result": "rejected", "data": {"actor": actor, "target": target, "offer": offer}}

@SkillRegistry.register
def evaluate_vendor(**kwargs):
    """评估供应商：返回供应商评分，影响后续决策"""
    vendor = kwargs.get("vendor", "unknown")
    score = random.randint(60, 100)
    return {"status": "success", "result": "evaluated", "data": {"vendor": vendor, "score": score}}

@SkillRegistry.register
def monitor_sla(**kwargs):
    """监控SLA：检查服务级别协议达标情况"""
    provider = kwargs.get("provider", "unknown")
    sla_met = random.random() > 0.2
    return {"status": "success", "result": "monitored", "data": {"provider": provider, "sla_met": sla_met}}

@SkillRegistry.register
def audit_compliance(**kwargs):
    """审计合规性：检查是否符合监管标准"""
    entity = kwargs.get("entity", "unknown")
    compliance = random.random() > 0.3
    return {"status": "success", "result": "audited", "data": {"entity": entity, "compliance": compliance}}

@SkillRegistry.register
def approve_budget(**kwargs):
    """批准预算：决定是否批准某项预算申请"""
    amount = kwargs.get("amount", 0)
    if resource_state["gov_budget"] >= amount:
        resource_state["gov_budget"] -= amount
        return {"status": "success", "result": "approved", "data": {"amount": amount}}
    else:
        return {"status": "success", "result": "rejected", "data": {"reason": "insufficient budget"}}

@SkillRegistry.register
def troubleshoot_network(**kwargs):
    """网络故障排查：尝试定位故障，返回定位时间"""
    device = kwargs.get("device", "unknown")
    time = random.randint(5, 60)  # 分钟
    # 如果有AI平台辅助，时间减少
    if kwargs.get("ai_assist", False):
        time = int(time * 0.5)
    return {"status": "success", "result": "troubleshooted", "data": {"device": device, "mtti": time}}

@SkillRegistry.register
def request_proposal(**kwargs):
    """请求方案：向服务商请求技术方案"""
    vendor = kwargs.get("vendor", "unknown")
    return {"status": "success", "result": "proposal_requested", "data": {"vendor": vendor}}

@SkillRegistry.register
def train_staff(**kwargs):
    """培训员工：提升内部技能，降低对外依赖"""
    staff_count = kwargs.get("staff_count", 10)
    skill_gain = random.randint(5, 15)
    return {"status": "success", "result": "trained", "data": {"staff_count": staff_count, "skill_gain": skill_gain}}

@SkillRegistry.register
def demo_platform(**kwargs):
    """演示平台：向客户展示AI平台功能"""
    client = kwargs.get("client", "unknown")
    impression = random.randint(60, 100)
    return {"status": "success", "result": "demoed", "data": {"client": client, "impression": impression}}

@SkillRegistry.register
def customize_interface(**kwargs):
    """定制接口：为特定客户定制南向接口"""
    client = kwargs.get("client", "unknown")
    effort = random.randint(1, 5)  # 人天
    return {"status": "success", "result": "customized", "data": {"client": client, "effort": effort}}

@SkillRegistry.register
def propose_integration(**kwargs):
    """提出集成方案：整合多方设备到统一平台"""
    target = kwargs.get("target", "unknown")
    feasibility = random.random()
    return {"status": "success", "result": "proposed", "data": {"target": target, "feasibility": feasibility}}

@SkillRegistry.register
def calculate_roi(**kwargs):
    """计算投资回报率：为决策提供数据"""
    investment = kwargs.get("investment", 100)
    roi = random.uniform(1.2, 2.0)
    return {"status": "success", "result": "calculated", "data": {"investment": investment, "roi": roi}}

@SkillRegistry.register
def protect_exclusive_access(**kwargs):
    """保护独家接入：阻止其他方直接管理设备"""
    device_type = kwargs.get("device_type", "unknown")
    success = random.random() > 0.4
    return {"status": "success", "result": "protected", "data": {"device_type": device_type, "success": success}}

@SkillRegistry.register
def negotiate_terms(**kwargs):
    """谈判条款：就合同条款进行讨价还价"""
    actor = kwargs.get("actor", "unknown")
    terms = kwargs.get("terms", {})
    acceptance = random.random()
    return {"status": "success", "result": "negotiated", "data": {"actor": actor, "terms": terms, "accepted": acceptance > 0.5}}

@SkillRegistry.register
def provide_support(**kwargs):
    """提供技术支持：响应客户故障"""
    client = kwargs.get("client", "unknown")
    response_time = random.randint(10, 120)  # 分钟
    return {"status": "success", "result": "supported", "data": {"client": client, "response_time": response_time}}

@SkillRegistry.register
def collaborate_integration(**kwargs):
    """协作集成：与AI平台合作开放接口"""
    partner = kwargs.get("partner", "unknown")
    integration_level = random.randint(50, 100)
    return {"status": "success", "result": "collaborated", "data": {"partner": partner, "integration_level": integration_level}}

@SkillRegistry.register
def share_data(**kwargs):
    """共享数据：提供设备运行数据给AI平台"""
    data_volume = kwargs.get("data_volume", 100)
    return {"status": "success", "result": "shared", "data": {"data_volume": data_volume}}

@SkillRegistry.register
def expand_service(**kwargs):
    """扩展服务：增加管理的设备数量"""
    additional_devices = random.randint(10, 50)
    resource_state["agent_c_revenue"] += additional_devices * 2
    return {"status": "success", "result": "expanded", "data": {"additional_devices": additional_devices}}

@SkillRegistry.register
def perform_maintenance(**kwargs):
    """执行维护：日常巡检和简单修复"""
    task = kwargs.get("task", "routine")
    success = random.random() > 0.1
    return {"status": "success", "result": "maintained", "data": {"task": task, "success": success}}

@SkillRegistry.register
def report_incident(**kwargs):
    """报告事件：向客户报告故障详情"""
    incident_id = kwargs.get("incident_id", "unknown")
    severity = random.choice(["low", "medium", "high"])
    return {"status": "success", "result": "reported", "data": {"incident_id": incident_id, "severity": severity}}

@SkillRegistry.register
def upskill_team(**kwargs):
    """提升团队技能：培训外包人员掌握新工具"""
    trained_count = random.randint(5, 20)
    return {"status": "success", "result": "upskilled", "data": {"trained_count": trained_count}}

@SkillRegistry.register
def draft_standard(**kwargs):
    """起草标准：制定统一南向接口标准草案"""
    progress = random.randint(10, 30)
    resource_state["standard_progress"] += progress
    if resource_state["standard_progress"] > 100:
        resource_state["standard_progress"] = 100
    return {"status": "success", "result": "drafted", "data": {"progress": resource_state["standard_progress"]}}

@SkillRegistry.register
def enforce_compliance(**kwargs):
    """强制合规：要求各方遵守标准"""
    entity = kwargs.get("entity", "unknown")
    compliance = random.random() > 0.3
    return {"status": "success", "result": "enforced", "data": {"entity": entity, "compliance": compliance}}

@SkillRegistry.register
def mediate_dispute(**kwargs):
    """调解争端：在各方之间斡旋"""
    parties = kwargs.get("parties", [])
    resolution = random.choice(["compromise", "stalemate", "agreement"])
    return {"status": "success", "result": "mediated", "data": {"parties": parties, "resolution": resolution}}

@SkillRegistry.register
def lobby_against_standard(**kwargs):
    """游说反对标准：阻止统一标准通过"""
    target = kwargs.get("target", "unknown")
    influence = random.randint(10, 30)
    resource_state["standard_progress"] -= influence
    if resource_state["standard_progress"] < 0:
        resource_state["standard_progress"] = 0
    return {"status": "success", "result": "lobbied", "data": {"target": target, "influence": influence}}

@SkillRegistry.register
def offer_incentive(**kwargs):
    """提供激励：给予客户优惠以维持私有协议"""
    client = kwargs.get("client", "unknown")
    discount = random.uniform(5, 20)
    return {"status": "success", "result": "offered", "data": {"client": client, "discount": discount}}

@SkillRegistry.register
def delay_cooperation(**kwargs):
    """延迟合作：拖延接口开放进度"""
    partner = kwargs.get("partner", "unknown")
    delay_days = random.randint(1, 10)
    return {"status": "success", "result": "delayed", "data": {"partner": partner, "delay_days": delay_days}}