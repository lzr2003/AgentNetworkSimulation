import random

# ============================================================
# 模块级业务状态 — 所有技能共享
# ============================================================
active_contracts = {}    # key: (source, target) -> contract dict
event_log = []           # 运行时事件流，每轮由技能自动追加
complaint_log = []       # 投诉记录
sanction_log = []        # 处罚记录
market_audit_log = []    # 市场审计记录

# 已知角色清单（由 _init_world 注入）
ALL_AGENCIES = set()
ALL_OPERATORS = set()


def _emit_event(action, source, target, visual_effect, reason, round_num=0):
    event = {
        "event_type": "BUSINESS_LINK_CHANGED",
        "round": round_num,
        "action": action,
        "source": source,
        "target": target,
        "visual_effect": visual_effect,
        "reason": reason,
    }
    event_log.append(event)
    return event


def _ensure_link(source, target, contract_value=0, round_num=0):
    """若 (source,target) 不在 active_contracts 中，创建 NEGOTIATING 连线"""
    key = (source, target)
    if key not in active_contracts:
        active_contracts[key] = {
            "source": source,
            "target": target,
            "contract_status": "NEGOTIATING",
            "contract_value": contract_value,
            "round_signed": -1,
            "breach_flash_round": -1,
        }
        _emit_event("CREATE", source, target, "APPEAR",
                     f"{source} 与 {target} 开始谈判", round_num)


def _init_world(agencies=None, operators=None, seed_links=None):
    """初始化世界：注册角色 + 种子连线。兼容 contract_value/value、contract_status/status 两种字段名。"""
    global ALL_AGENCIES, ALL_OPERATORS
    if agencies:
        ALL_AGENCIES = set(agencies)
    if operators:
        ALL_OPERATORS = set(operators)
    if seed_links:
        for link in seed_links:
            key = (link["source"], link["target"])
            if key not in active_contracts:
                active_contracts[key] = {
                    "source": link["source"],
                    "target": link["target"],
                    "contract_status": link.get("status") or link.get("contract_status", "NEGOTIATING"),
                    "contract_value": link.get("value") or link.get("contract_value", 0),
                    "round_signed": -1,
                    "breach_flash_round": -1,
                }


# ============================================================
# SkillRegistry
# ============================================================
class SkillRegistry:
    _skills = {}

    @classmethod
    def register(cls, name, fn):
        cls._skills[name] = fn

    @classmethod
    def execute(cls, name, **kwargs):
        if name not in cls._skills:
            return {"status": "error", "result": None, "data": {"error": f"Skill '{name}' not found"}}
        return cls._skills[name](**kwargs)

    @classmethod
    def list_skills(cls):
        return list(cls._skills.keys())


# ============================================================
# 通用：获取可谈判对象列表
# ============================================================
def list_candidates(**kwargs):
    """
    列出所有可谈判的对方角色。
    参数: agent_id(str), side(str: 'agency'|'operator')
    返回: 候选列表 [{id, name}]
    """
    agent_id = kwargs.get("agent_id", "")
    side = kwargs.get("side", "")
    if side == "agency":
        candidates = sorted(ALL_OPERATORS - {agent_id})
    else:
        candidates = sorted(ALL_AGENCIES - {agent_id})
    return {
        "status": "success",
        "result": candidates,
        "data": {"candidates": candidates, "count": len(candidates)},
    }
SkillRegistry.register("list_candidates", list_candidates)


# ============================================================
# 机构侧技能
# ============================================================

def evaluate_proposals(**kwargs):
    """
    货比三家：评估所有收到的（或可获取的）竞标方案。
    参数: agent_id(str), proposals(list[{target,contract_value,bandwidth,latency,price}]), round(int)
    返回: 排名列表、最佳选择、是否建议转投
    proposals 可为空列表（机构自主搜寻所有运营商的公开报价）
    """
    agent_id = kwargs.get("agent_id", "unknown")
    proposals = kwargs.get("proposals", [])
    current_round = kwargs.get("round", 0)

    # 若未传入方案，自动向所有运营商询价（模拟市场公开信息）
    if not proposals:
        for op in ALL_OPERATORS:
            proposals.append({
                "target": op,
                "contract_value": random.randint(200, 800),
                "bandwidth": random.randint(5, 50),
                "latency": random.randint(3, 20),
                "price": random.randint(200, 800),
            })

    # 找到当前签约方
    current_target = None
    current_value = 0
    for (src, tgt), c in active_contracts.items():
        if src == agent_id and c["contract_status"] == "SIGNED":
            current_target = tgt
            current_value = c["contract_value"]
            break

    # 对所有方案评分
    scored = []
    for prop in proposals:
        bw = prop.get("bandwidth", 5)
        lat = prop.get("latency", 15)
        price = prop.get("price", 500)
        score = bw * 10 + (20 - lat) * 5 - price * 0.01
        scored.append({**prop, "score": round(score, 1)})

    scored.sort(key=lambda x: x["score"], reverse=True)
    best = scored[0]

    # 决策
    if current_target is None:
        recommendation = "sign_first"
        reason = f"当前无签约方，建议与 {best['target']} (评分{best['score']}) 签约"
    elif best["target"] != current_target:
        threshold = current_value * 0.01 + 10
        if best["score"] > threshold:
            recommendation = "switch"
            reason = f"{best['target']}(评分{best['score']}) 优于当前 {current_target}(估值{current_value})，建议转投"
        else:
            recommendation = "stay"
            reason = f"当前 {current_target}(估值{current_value}) 仍最优，挑战者 {best['target']}(评分{best['score']}) 差距不足"
    else:
        recommendation = "stay"
        reason = f"当前签约方 {current_target} 仍为最优(评分{best['score']})"

    # 为所有被评估但未连线的运营商自动创建 NEGOTIATING 连线
    for prop in proposals:
        tgt = prop.get("target", "")
        if tgt and tgt != agent_id:
            _ensure_link(agent_id, tgt, prop.get("contract_value", 0), current_round)

    return {
        "status": "success",
        "result": recommendation,
        "data": {
            "agent_id": agent_id,
            "current_target": current_target,
            "current_value": current_value,
            "best_target": best["target"],
            "best_score": best["score"],
            "recommendation": recommendation,
            "reason": reason,
            "all_scores": scored[:5],
        }
    }
SkillRegistry.register("evaluate_proposals", evaluate_proposals)


def sign_contract(**kwargs):
    """
    签署合约。会先自动创建 NEGOTIATING 连线（如不存在）。
    参数: source(str), target(str), contract_value(float), round(int)
    """
    source = kwargs.get("source")
    target = kwargs.get("target")
    contract_value = kwargs.get("contract_value", 100)
    current_round = kwargs.get("round", 0)

    if not source or not target:
        return {"status": "error", "result": "missing_params", "data": {"error": "source 和 target 是必填参数"}}

    key = (source, target)
    if key in active_contracts and active_contracts[key]["contract_status"] == "SIGNED":
        return {"status": "error", "result": "already_signed", "data": {"error": f"{source} 与 {target} 已签约"}}

    # 确保连线存在
    _ensure_link(source, target, contract_value, current_round)

    # 如有其他 SIGNED 合约（同一机构），先自动触发 BREACH_FLASHING
    for (s, t), c in list(active_contracts.items()):
        if s == source and c["contract_status"] == "SIGNED" and t != target:
            c["contract_status"] = "BREACH_FLASHING"
            c["breach_flash_round"] = current_round
            _emit_event("BREAK", s, t, "FLASH_AND_DESTROY",
                         f"{s} 转投 {target}，与 {t} 解约", current_round)

    active_contracts[key]["contract_status"] = "SIGNED"
    active_contracts[key]["contract_value"] = contract_value
    active_contracts[key]["round_signed"] = current_round

    event = _emit_event("SIGN", source, target, "SOLIDIFY",
                         f"{source} 与 {target} 签约 (价值{contract_value})", current_round)

    return {
        "status": "success",
        "result": "contract_signed",
        "data": {
            "source": source,
            "target": target,
            "contract_status": "SIGNED",
            "contract_value": contract_value,
            "round": current_round,
            "visual_effect": "SOLIDIFY",
            "event": event,
        }
    }
SkillRegistry.register("sign_contract", sign_contract)


def terminate_contract_with_flash(**kwargs):
    """
    主动终止合同（违约解约）。
    参数: source(str), target(str), reason(str), round(int)
    效果: SIGNED/NEGOTIATING → BREACH_FLASHING
    """
    source = kwargs.get("source")
    target = kwargs.get("target")
    reason = kwargs.get("reason", "性价比不足")
    current_round = kwargs.get("round", 0)

    if not source or not target:
        return {"status": "error", "result": "missing_params", "data": {"error": "source 和 target 是必填参数"}}

    key = (source, target)
    if key not in active_contracts:
        return {"status": "error", "result": "not_found", "data": {"error": f"({source},{target}) 无业务关系"}}

    contract = active_contracts[key]
    if contract["contract_status"] not in ("SIGNED", "NEGOTIATING"):
        return {"status": "error", "result": "invalid_state", "data": {"error": f"状态为 {contract['contract_status']}"}}

    contract["contract_status"] = "BREACH_FLASHING"
    contract["breach_flash_round"] = current_round

    event = _emit_event("BREAK", source, target, "FLASH_AND_DESTROY", reason, current_round)

    return {
        "status": "success",
        "result": "breach_flashing",
        "data": {
            "source": source,
            "target": target,
            "contract_status": "BREACH_FLASHING",
            "reason": reason,
            "round": current_round,
            "visual_effect": "FLASH_AND_DESTROY",
            "event": event,
        }
    }
SkillRegistry.register("terminate_contract_with_flash", terminate_contract_with_flash)


def confirm_termination(**kwargs):
    """
    确认终止（动画完成后移除连线）。
    参数: source(str), target(str), round(int)
    """
    source = kwargs.get("source")
    target = kwargs.get("target")
    current_round = kwargs.get("round", 0)

    key = (source, target)
    if key not in active_contracts:
        return {"status": "error", "result": "not_found", "data": {}}
    if active_contracts[key]["contract_status"] != "BREACH_FLASHING":
        return {"status": "error", "result": "not_flashing", "data": {}}

    del active_contracts[key]
    _emit_event("TERMINATE", source, target, "FADE_OUT",
                 f"{source} 与 {target} 合同正式终止", current_round)

    return {
        "status": "success",
        "result": "contract_terminated",
        "data": {"source": source, "target": target, "contract_status": "TERMINATED", "round": current_round}
    }
SkillRegistry.register("confirm_termination", confirm_termination)


# ============================================================
# 运营商侧技能
# ============================================================

def submit_bidding_proposal(**kwargs):
    """
    向任意机构提交竞标方案。首次接触自动创建 NEGOTIATING 连线。
    参数: operator_id(str), target_agency(str), price(float), bandwidth(int), latency(int), round(int)
    """
    operator_id = kwargs.get("operator_id")
    target_agency = kwargs.get("target_agency")
    price = kwargs.get("price", random.randint(200, 800))
    bandwidth = kwargs.get("bandwidth", random.randint(5, 50))
    latency = kwargs.get("latency", random.randint(3, 15))
    current_round = kwargs.get("round", 0)

    if not operator_id or not target_agency:
        return {"status": "error", "result": "missing_params", "data": {"error": "operator_id 和 target_agency 是必填参数"}}

    # 检查目标机构是否已有签约
    competitor = None
    competitor_value = 0
    for (src, tgt), c in active_contracts.items():
        if src == target_agency and c["contract_status"] == "SIGNED":
            competitor = tgt
            competitor_value = c["contract_value"]
            break

    # 挖墙脚策略：压低报价
    strategy = "standard"
    if competitor and competitor != operator_id:
        price = price * random.uniform(0.70, 0.90)
        bandwidth = min(bandwidth + random.randint(2, 10), 50)
        strategy = "undercut"

    # 自动创建 NEGOTIATING 连线（自由市场核心）
    _ensure_link(target_agency, operator_id, price * 10, current_round)

    score = bandwidth * 10 + (20 - latency) * 5 - price * 0.01

    return {
        "status": "success",
        "result": "proposal_submitted",
        "data": {
            "operator_id": operator_id,
            "target_agency": target_agency,
            "price": round(price, 2),
            "bandwidth": bandwidth,
            "latency": latency,
            "score": round(score, 1),
            "strategy": strategy,
            "competitor": competitor,
        }
    }
SkillRegistry.register("submit_bidding_proposal", submit_bidding_proposal)


def process_breach_notification(**kwargs):
    """
    被违约后的决策：挽留/报复/举报。
    参数: operator_id(str), breaching_agency(str), reason(str), round(int)
    """
    operator_id = kwargs.get("operator_id")
    breaching_agency = kwargs.get("breaching_agency")
    reason = kwargs.get("reason", "unknown")
    current_round = kwargs.get("round", 0)

    if not operator_id or not breaching_agency:
        return {"status": "error", "result": "missing_params", "data": {"error": "operator_id 和 breaching_agency 是必填参数"}}

    contract_value = 0
    for (src, tgt), c in active_contracts.items():
        if src == breaching_agency and tgt == operator_id:
            contract_value = c.get("contract_value", 0)
            break

    if contract_value > 400:
        weights = {"retain": 0.5, "retaliate": 0.2, "report": 0.3}
    elif contract_value > 200:
        weights = {"retain": 0.3, "retaliate": 0.3, "report": 0.4}
    else:
        weights = {"retain": 0.1, "retaliate": 0.4, "report": 0.5}

    strategy = random.choices(list(weights.keys()), weights=list(weights.values()), k=1)[0]

    if strategy == "retain":
        discount = random.randint(10, 35)
        data = {"action": "offer_discount", "discount_percent": discount}
    elif strategy == "retaliate":
        data = {"action": "price_war", "message": f"对 {breaching_agency} 的新运营商发起价格战"}
    else:
        complaint_log.append({"round": current_round, "reporter": operator_id, "target": breaching_agency, "reason": reason})
        data = {"action": "report_to_regulator", "complaint_id": len(complaint_log) - 1}

    return {"status": "success", "result": strategy, "data": data}
SkillRegistry.register("process_breach_notification", process_breach_notification)


# ============================================================
# 监管侧技能
# ============================================================

def investigate_complaint(**kwargs):
    """
    调查投诉。参数: complaint_id(int), reporter(str), round(int)
    """
    complaint_id = kwargs.get("complaint_id")
    reporter = kwargs.get("reporter")
    current_round = kwargs.get("round", 0)

    if complaint_id is not None and 0 <= complaint_id < len(complaint_log):
        complaint = complaint_log[complaint_id]
    elif reporter:
        matches = [c for c in complaint_log if c["reporter"] == reporter]
        complaint = matches[-1] if matches else None
    else:
        complaint = complaint_log[-1] if complaint_log else None

    if complaint is None:
        return {"status": "error", "result": "no_complaint", "data": {"error": "无待调查投诉"}}

    evidence = []
    for (src, tgt), c in active_contracts.items():
        if src == complaint["target"]:
            if c["contract_status"] == "BREACH_FLASHING":
                evidence.append(f"({src},{tgt}) BREACH_FLASHING")
            elif c["contract_status"] == "SIGNED":
                evidence.append(f"({src},{tgt}) SIGNED(价值{c['contract_value']})")

    if any("BREACH_FLASHING" in e for e in evidence):
        conclusion = "confirmed"
        suggested_fine = random.randint(50, 200)
    elif len(evidence) >= 2:
        conclusion = "inconclusive"
        suggested_fine = 0
    else:
        conclusion = "vindicated"
        suggested_fine = 0

    return {
        "status": "success", "result": conclusion,
        "data": {"complaint": complaint, "evidence": evidence, "conclusion": conclusion, "suggested_fine": suggested_fine, "round": current_round}
    }
SkillRegistry.register("investigate_complaint", investigate_complaint)


def impose_sanction(**kwargs):
    """
    施加处罚。参数: target(str), fine(float), reason(str), round(int)
    """
    target = kwargs.get("target")
    fine = kwargs.get("fine", 100)
    reason = kwargs.get("reason", "违规")
    current_round = kwargs.get("round", 0)

    if not target or fine <= 0:
        return {"status": "error", "result": "invalid_params", "data": {}}

    sanction_log.append({"round": current_round, "target": target, "fine": fine, "reason": reason})
    return {"status": "success", "result": "sanction_imposed", "data": {"target": target, "fine": fine, "round": current_round}}
SkillRegistry.register("impose_sanction", impose_sanction)


def audit_market_share(**kwargs):
    """
    审计市场份额。参数: round(int)
    """
    current_round = kwargs.get("round", 0)
    op_contracts = {}
    total_value = 0
    for (src, tgt), c in active_contracts.items():
        if c["contract_status"] == "SIGNED":
            op_contracts[tgt] = op_contracts.get(tgt, 0) + 1
            total_value += c.get("contract_value", 0)

    total = sum(op_contracts.values()) or 1
    shares = {op: round(cnt / total * 100, 1) for op, cnt in op_contracts.items()}
    alerts = [f"{op} 份额 {shares[op]}% 超50%" for op, cnt in op_contracts.items() if cnt / total > 0.5]

    market_audit_log.append({"round": current_round, "shares": shares, "total_value": total_value, "alerts": alerts})
    return {
        "status": "success", "result": "audit_complete",
        "data": {"round": current_round, "market_shares": shares, "total_contracts": total, "total_value": total_value, "alerts": alerts, "antitrust_triggered": len(alerts) > 0}
    }
SkillRegistry.register("audit_market_share", audit_market_share)


# ============================================================
# 顾问侧技能
# ============================================================

def provide_evaluation_report(**kwargs):
    """
    为机构提供运营商评估报告（货比三家）。
    参数: client_id(str), operator_ids(list[str]), round(int)
    返回: 排名报告，含推荐签约方
    """
    client_id = kwargs.get("client_id")
    operator_ids = kwargs.get("operator_ids", list(ALL_OPERATORS))
    current_round = kwargs.get("round", 0)

    if not client_id:
        return {"status": "error", "result": "missing_params", "data": {"error": "client_id 是必填参数"}}

    # 为每个运营商生成评估数据
    evaluations = []
    for op in operator_ids:
        # 检查是否有竞争者已签约
        competitor = None
        for (src, tgt), c in active_contracts.items():
            if src == client_id and c["contract_status"] == "SIGNED":
                competitor = tgt
                break

        price = random.randint(200, 800)
        bw = random.randint(5, 50)
        lat = random.randint(3, 15)
        reliability = random.randint(70, 99)
        score = bw * 10 + (20 - lat) * 5 - price * 0.01 + reliability * 0.5

        evaluations.append({
            "operator": op,
            "price": price,
            "bandwidth": bw,
            "latency": lat,
            "reliability": reliability,
            "score": round(score, 1),
        })

        # 确保有谈判连线
        _ensure_link(client_id, op, price * 10, current_round)

    evaluations.sort(key=lambda x: x["score"], reverse=True)
    best = evaluations[0]

    return {
        "status": "success",
        "result": "report_ready",
        "data": {
            "client_id": client_id,
            "recommended": best["operator"],
            "best_score": best["score"],
            "rankings": evaluations,
            "round": current_round,
        }
    }
SkillRegistry.register("provide_evaluation_report", provide_evaluation_report)


def broker_deal(**kwargs):
    """
    撮合签约：顾问推动机构与推荐运营商签约，收取佣金。
    参数: client_id(str), operator_id(str), contract_value(float), round(int)
    效果: 调用 sign_contract，返回含佣金信息
    """
    client_id = kwargs.get("client_id")
    operator_id = kwargs.get("operator_id")
    contract_value = kwargs.get("contract_value", 100)
    current_round = kwargs.get("round", 0)

    if not client_id or not operator_id:
        return {"status": "error", "result": "missing_params", "data": {"error": "client_id 和 operator_id 是必填参数"}}

    # 委托给 sign_contract
    result = SkillRegistry.execute("sign_contract",
                                    source=client_id, target=operator_id,
                                    contract_value=contract_value, round=current_round)

    if result["status"] == "success":
        commission = contract_value * 0.05
        result["data"]["broker"] = "CONSULTANT"
        result["data"]["commission"] = commission

    return result
SkillRegistry.register("broker_deal", broker_deal)
