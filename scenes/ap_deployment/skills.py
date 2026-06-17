import random
import math

# ============================================================
# 园区地图 & 干扰源（固定）
# ============================================================
CAMPUS_W = 1000
CAMPUS_H = 400

INTERFERENCE = [
    {"id": "INT_01", "x": 150, "y": 80,  "radius": 80,  "desc": "变电站电磁干扰"},
    {"id": "INT_02", "x": 420, "y": 280, "radius": 120, "desc": "大型电机设备"},
    {"id": "INT_03", "x": 680, "y": 120, "radius": 60,  "desc": "微波通信塔"},
    {"id": "INT_04", "x": 850, "y": 320, "radius": 100, "desc": "高压输电线"},
    {"id": "INT_05", "x": 300, "y": 350, "radius": 150, "desc": "工业焊接车间"},
]

BUDGET = 50000
AP_UNIT_COST = 3500
AP_COVERAGE_RADIUS = 60
TARGET_COVERAGE = 95
MIN_AP_SPACING = 25

# ============================================================
# 模块级状态
# ============================================================
ap_placements = []         # 已确认的AP [{id, x, y, radius, status:"confirmed"}]
proposed_aps = []           # 待评估的AP [{id, x, y, radius, status:"proposed"|"evaluating"}]
relocating_aps = []         # 正在迁移的AP [{id, from_x, from_y, to_x, to_y, status:"relocating"}]
pending_action = None       # 当前决策 {type, agent, ap_id, detail, visual_effect, round}
decision_log = []           # 决策历史 [{round, agent, action, ap_id, detail, visual_effect}]

coverage_reports = []
cost_estimates = []
ai_call_log = []
feasibility_checks = []
event_log = []
traffic_log = []

_ap_counter = 0
_current_round = 0

# ============================================================
# 阵营博弈状态（v3: 政治/灰色/免责机制）
# ============================================================
FACTIONS = {
    "planning_core": {
        "id": "planning_core", "name": "规划核心组",
        "members": ["PLANNER", "RF_ENGINEER", "AI_ASSISTANT", "ARCHITECT"],
        "public_goal": "方案质量与信号覆盖最优",
        "hidden_dynamic": "分摊决策风险，AI按调用量计费"
    },
    "execution": {
        "id": "execution", "name": "执行落地组",
        "members": ["DEPLOYER", "SURVEYOR", "QA_ENGINEER", "VERIFIER"],
        "public_goal": "按时按量完成部署交付",
        "hidden_dynamic": "压缩工时，测试跳步，边缘放行"
    },
    "audit_control": {
        "id": "audit_control", "name": "审计风控组",
        "members": ["COST_ANALYST", "DOCUMENTER"],
        "public_goal": "预算合规与过程追溯",
        "hidden_dynamic": "信息不对称，免责证据链"
    }
}

# 声誉追踪 {agent_id: {"score": 0-100, "violations": 0, "complaints_against": 0, "blame_shields_filed": 0, "alliances": []}}
reputation = {}
# 惩罚日志 [{round, source, target, violation_type, penalty_desc, consequence, detection_chance_pct}]
penalty_log = []
# 临时同盟 {agent_id: [allied_ids]}  — 只记录当前轮有效的同盟
alliance_map = {}
# 免责盾日志 [{round, agent, target, incident_ref, detail, filed_at_round}]
blame_shield_log = []
# 灰色操作曝光记录 [{round, skill, agent, detected, consequence_applied}]
gray_exposure_log = []


def _init_reputation(agent_id):
    """初始化agent声誉"""
    if agent_id not in reputation:
        faction_id = None
        for fid, fdata in FACTIONS.items():
            if agent_id in fdata["members"]:
                faction_id = fid; break
        reputation[agent_id] = {
            "score": 100, "violations": 0, "complaints_against": 0,
            "blame_shields_filed": 0, "alliances": [], "faction_id": faction_id
        }


def _get_faction(agent_id):
    """返回agent所属阵营id"""
    for fid, fdata in FACTIONS.items():
        if agent_id in fdata["members"]: return fid
    return None


def _environment_detect_gray(skill_name, agent_id, base_detection_pct=30):
    """环境引擎：判定灰色操作是否被检测到。返回 (detected, consequence)。
    detection_chance = base + 恶意增量（越频繁越容易被抓）"""
    if agent_id in reputation:
        extra = reputation[agent_id].get("violations", 0) * 10
    else:
        extra = 0
    detected = random.random() * 100 < (base_detection_pct + extra)
    consequence = None
    if detected:
        consequences = [
            "干扰停工24h", "预算罚款¥2000", "该轮覆盖数据作废",
            "AP部署资格暂停一轮", "审计警告计入档案"
        ]
        consequence = random.choice(consequences)
    return detected, consequence


def _next_ap_id():
    global _ap_counter; _ap_counter += 1; return f"AP_{_ap_counter}"


def _emit_event(etype, round_num, source, target, action, detail=""):
    event_log.append({"event_type": etype, "round": round_num, "source": source, "target": target, "action": action, "detail": detail})

def _emit_traffic(round_num, ttype, source, target, action, kbytes):
    traffic_log.append({"round": round_num, "type": ttype, "source": source, "target": target, "action": action, "bytes": kbytes * 1024})

def _log_decision(agent, action, ap_id, detail, visual_effect=""):
    decision_log.append({"round": _current_round, "agent": agent, "action": action, "ap_id": ap_id, "detail": detail, "visual_effect": visual_effect})

def _min_interference_dist(px, py):
    return min((math.sqrt((px-s["x"])**2+(py-s["y"])**2)-s["radius"] for s in INTERFERENCE), default=999)

def _is_in_interference(px, py):
    for src in INTERFERENCE:
        if math.sqrt((px-src["x"])**2+(py-src["y"])**2)<src["radius"]: return True, src
    return False, None


# ============================================================
# SkillRegistry
# ============================================================
class SkillRegistry:
    _skills = {}
    @classmethod
    def register(cls, name, fn): cls._skills[name] = fn
    @classmethod
    def execute(cls, name, **kwargs):
        if name not in cls._skills: return {"status":"error","result":None,"data":{"error":f"'{name}' not found"}}
        return cls._skills[name](**kwargs)
    @classmethod
    def list_skills(cls): return list(cls._skills.keys())


# ============================================================
# PLANNER: 逐点部署流程
# ============================================================

def plan_next_ap(**kwargs):
    """
    PLANNER调用AI获取下一个AP的候选位置（每次只返回1个最优位置）。
    visual_effect: "proposed" → 前端显示虚线闪烁新AP
    """
    global _current_round, pending_action
    round_num = kwargs.get("round", _current_round)
    _current_round = round_num

    # 排除已有AP太近的位置
    existing = ap_placements + proposed_aps
    cols, rows = 5, 3
    candidates = []
    for r in range(1, rows+1):
        for c in range(1, cols+1):
            bx = CAMPUS_W*c/(cols+1); by = CAMPUS_H*r/(rows+1)
            best_x, best_y, best_dist = bx, by, _min_interference_dist(bx, by)
            for dx in [-CAMPUS_W*0.06, 0, CAMPUS_W*0.06]:
                for dy in [-CAMPUS_H*0.06, 0, CAMPUS_H*0.06]:
                    tx = max(10, min(CAMPUS_W-10, bx+dx)); ty = max(10, min(CAMPUS_H-10, by+dy))
                    d = _min_interference_dist(tx, ty)
                    if d > best_dist: best_x, best_y, best_dist = tx, ty, d
            # 检查与已有AP的间距
            too_close = any(math.sqrt((best_x-ap["x"])**2+(best_y-ap["y"])**2)<MIN_AP_SPACING for ap in existing)
            candidates.append({"x": round(best_x,1), "y": round(best_y,1), "safe_dist": round(best_dist,1), "too_close": too_close})

    # 加权选候选：safe_dist(40%) + 覆盖分散度(60%)，避免全部堆在右上角
    if existing:
        for c in candidates:
            nearest_ap = min(math.sqrt((c["x"]-ap["x"])**2+(c["y"]-ap["y"])**2) for ap in existing)
            c["gap_dist"] = min(nearest_ap, 300)  # cap 300m 防止极端值
        max_safe = max(c["safe_dist"] for c in candidates) or 1
        max_gap = max(c["gap_dist"] for c in candidates) or 1
        for c in candidates:
            c["score"] = (c["safe_dist"]/max_safe)*0.4 + (c["gap_dist"]/max_gap)*0.6
    else:
        for c in candidates:
            c["gap_dist"] = 0
            c["score"] = c["safe_dist"]

    candidates.sort(key=lambda c: (-c["score"], c["too_close"]))
    # 从前3名中随机选，避免确定性聚集
    pool = [c for c in candidates[:min(4, len(candidates))] if not c["too_close"]]
    if not pool:
        pool = candidates[:min(4, len(candidates))]
    best = random.choice(pool)
    ap_id = _next_ap_id()
    best["id"] = ap_id
    best["radius"] = AP_COVERAGE_RADIUS

    if best["too_close"]:
        # 自动微调
        for _ in range(50):
            tx = best["x"] + (random.random()-0.5)*80; ty = best["y"] + (random.random()-0.5)*80
            tx = max(10, min(CAMPUS_W-10, tx)); ty = max(10, min(CAMPUS_H-10, ty))
            tc = any(math.sqrt((tx-ap["x"])**2+(ty-ap["y"])**2)<MIN_AP_SPACING for ap in existing)
            if not tc:
                best["x"]=round(tx,1); best["y"]=round(ty,1); best["too_close"]=False; break

    best["status"] = "proposed"
    proposed_aps.append(best)

    pending_action = {"type":"propose","agent":"PLANNER","ap_id":ap_id,
                       "x":best["x"],"y":best["y"],"round":round_num,"status":"proposed"}
    _log_decision("PLANNER", "propose", ap_id,
                  f"建议在({best['x']},{best['y']})部署 (安全距离{best['safe_dist']}m)",
                  "APPEAR_DASHED")

    latency = 200 + (len(ap_placements)+1)*50
    tokens = 500 + (len(ap_placements)+1)*100
    ai_call_log.append({"round":round_num,"caller":"PLANNER","ap_id":ap_id,"latency_ms":latency,"tokens":tokens})
    _emit_traffic(round_num, "NORTH_SOUTH", "PLANNER", "AI_ASSISTANT", "single_ap_optimize", tokens*4)
    _emit_event("AP_PROPOSED", round_num, "PLANNER", "AI_ASSISTANT", "propose",
                f"{ap_id} at ({best['x']},{best['y']})")

    return {"status":"success","result":"ap_proposed",
            "data":best}
SkillRegistry.register("plan_next_ap", plan_next_ap)


def confirm_ap(**kwargs):
    """
    PLANNER确认AP位置。visual_effect: "SOLIDIFY" → 前端虚线变实线
    """
    global pending_action
    round_num = kwargs.get("round", _current_round)
    ap_id = kwargs.get("ap_id","")

    ap = next((a for a in proposed_aps if a["id"]==ap_id), None)
    if not ap: return {"status":"error","result":"not_found","data":{}}

    proposed_aps.remove(ap)
    ap["status"] = "confirmed"
    ap_placements.append(ap)

    pending_action = {"type":"confirm","agent":"PLANNER","ap_id":ap_id,"round":round_num,"status":"confirmed"}
    _log_decision("PLANNER", "confirm", ap_id,
                  f"AP_{ap_id} 部署确认 位置({ap['x']},{ap['y']})",
                  "SOLIDIFY")
    _emit_event("AP_CONFIRMED", round_num, "PLANNER", "DEPLOYER", "confirm", ap_id)

    return {"status":"success","result":"confirmed",
            "data":{"ap_id":ap_id,"position":{"x":ap["x"],"y":ap["y"]},"round":round_num}}
SkillRegistry.register("confirm_ap", confirm_ap)


def reject_ap(**kwargs):
    """
    PLANNER否决提案。visual_effect: "FADE_OUT" → 前端虚线消失
    """
    global pending_action
    round_num = kwargs.get("round", _current_round)
    ap_id = kwargs.get("ap_id","")
    reason = kwargs.get("reason","不可行")

    ap = next((a for a in proposed_aps if a["id"]==ap_id), None)
    if ap: proposed_aps.remove(ap)

    pending_action = {"type":"reject","agent":"PLANNER","ap_id":ap_id,"round":round_num,"status":"rejected"}
    _log_decision("PLANNER", "reject", ap_id, reason, "FADE_OUT")
    _emit_event("AP_REJECTED", round_num, "PLANNER", "", "reject", f"{ap_id}: {reason}")

    return {"status":"success","result":"rejected",
            "data":{"ap_id":ap_id,"reason":reason,"round":round_num}}
SkillRegistry.register("reject_ap", reject_ap)


def relocate_ap(**kwargs):
    """
    迁移已有AP到新位置。visual_effect: "FLASH_THEN_DASHED"
    旧位置→闪烁，新位置→虚线，确认后→实线
    """
    global pending_action
    round_num = kwargs.get("round", _current_round)
    ap_id = kwargs.get("ap_id","")
    new_x = kwargs.get("new_x",0)
    new_y = kwargs.get("new_y",0)

    # 找已有AP
    old_ap = next((a for a in ap_placements if a["id"]==ap_id), None)
    if not old_ap: return {"status":"error","result":"not_found","data":{}}

    from_x, from_y = old_ap["x"], old_ap["y"]
    # 旧位置标记闪烁
    relocating_aps.append({"id":ap_id, "from_x":from_x, "from_y":from_y,
                            "to_x":new_x, "to_y":new_y, "status":"relocating"})
    old_ap["x"], old_ap["y"] = new_x, new_y

    pending_action = {"type":"relocate","agent":"PLANNER","ap_id":ap_id,
                       "from_x":from_x,"from_y":from_y,"to_x":new_x,"to_y":new_y,
                       "round":round_num,"status":"relocating"}
    _log_decision("PLANNER", "relocate", ap_id,
                  f"迁移 ({from_x},{from_y})→({new_x},{new_y})",
                  "FLASH_THEN_DASHED")
    _emit_event("AP_RELOCATING", round_num, "PLANNER", "", "relocate",
                f"{ap_id}: ({from_x},{from_y})→({new_x},{new_y})")

    return {"status":"success","result":"relocating",
            "data":{"ap_id":ap_id,"from":{"x":from_x,"y":from_y},"to":{"x":new_x,"y":new_y},"round":round_num}}
SkillRegistry.register("relocate_ap", relocate_ap)


def confirm_relocation(**kwargs):
    """确认迁移完成。visual_effect: "SOLIDIFY" — 新位置虚线消失变实线"""
    round_num = kwargs.get("round", _current_round)
    ap_id = kwargs.get("ap_id","")
    idx = next((i for i,a in enumerate(relocating_aps) if a["id"]==ap_id), None)
    if idx is not None:
        del relocating_aps[idx]
    _log_decision("PLANNER", "confirm_relocate", ap_id, "迁移完成", "SOLIDIFY")
    _emit_event("AP_RELOCATED", round_num, "PLANNER", "", "confirm_relocate", ap_id)
    return {"status":"success","result":"relocation_confirmed","data":{"ap_id":ap_id,"round":round_num}}
SkillRegistry.register("confirm_relocation", confirm_relocation)


# ============================================================
# 评估技能（逐AP评估）
# ============================================================

def evaluate_single_ap(**kwargs):
    """RF_ENGINEER: 评估单个AP的覆盖贡献"""
    round_num = kwargs.get("round", _current_round)
    ap_id = kwargs.get("ap_id","")

    ap = next((a for a in proposed_aps+ap_placements if a["id"]==ap_id), None)
    if not ap: return {"status":"error","result":"not_found","data":{}}

    # 蒙特卡洛采样该AP的覆盖贡献
    samples, covered = 1000, 0
    for _ in range(samples):
        angle = random.random()*2*math.pi; dist = random.random()*AP_COVERAGE_RADIUS
        sx = ap["x"] + math.cos(angle)*dist; sy = ap["y"] + math.sin(angle)*dist
        if 0<=sx<=CAMPUS_W and 0<=sy<=CAMPUS_H:
            in_int, _ = _is_in_interference(sx, sy)
            if not in_int: covered += 1
    coverage_contrib = round(covered/samples*100, 1)

    _emit_traffic(round_num, "EAST_WEST", "RF_ENGINEER", "PLANNER", "single_ap_eval", 8)
    _emit_event("AP_EVALUATED", round_num, "RF_ENGINEER", "PLANNER", "evaluate",
                f"{ap_id}: coverage_contrib={coverage_contrib}%")
    _log_decision("RF_ENGINEER", "evaluate", ap_id,
                  f"覆盖贡献{coverage_contrib}%", "EVALUATING")

    return {"status":"success","result":"evaluated",
            "data":{"ap_id":ap_id,"coverage_contrib":coverage_contrib,"round":round_num}}
SkillRegistry.register("evaluate_single_ap", evaluate_single_ap)


# ============================================================
# 全局评估（保留兼容）
# ============================================================

def simulate_coverage(**kwargs):
    round_num = kwargs.get("round", _current_round)
    aps = ap_placements + proposed_aps
    if not aps: return {"status":"error","result":"no_aps","data":{}}
    samples, covered, blind = 2000, 0, []
    for _ in range(samples):
        sx, sy = random.uniform(0,CAMPUS_W), random.uniform(0,CAMPUS_H)
        in_range = any(math.sqrt((sx-a["x"])**2+(sy-a["y"])**2)<a.get("radius",AP_COVERAGE_RADIUS) for a in aps)
        in_int, _ = _is_in_interference(sx, sy)
        if in_range and not in_int: covered+=1
        elif not in_range: blind.append({"x":round(sx,1),"y":round(sy,1)})
    pct = round(covered/samples*100,1)
    rpt = {"round":round_num,"coverage_pct":pct,"blind_spot_count":len(blind),"ap_count":len(ap_placements),"blind_spots_sample":blind[:15]}
    coverage_reports.append(rpt)
    _emit_traffic(round_num,"EAST_WEST","RF_ENGINEER","PLANNER","coverage_report",16)
    _emit_event("COVERAGE_SIM",round_num,"RF_ENGINEER","PLANNER","simulate_coverage",f"{pct}%")
    return {"status":"success","result":"coverage_simulated","data":rpt}
SkillRegistry.register("simulate_coverage", simulate_coverage)


def analyze_interference(**kwargs):
    round_num = kwargs.get("round", _current_round)
    analysis = []
    for src in INTERFERENCE:
        aff = [ap["id"] for ap in ap_placements if math.sqrt((ap["x"]-src["x"])**2+(ap["y"]-src["y"])**2)<src["radius"]]
        analysis.append({"source_id":src["id"],"desc":src["desc"],"radius":src["radius"],"affected_aps":aff,"affected_count":len(aff)})
    _emit_event("INTERFERENCE_ANALYSIS",round_num,"RF_ENGINEER","PLANNER","analyze",f"{len(INTERFERENCE)} sources")
    return {"status":"success","result":"analysis_complete","data":{"sources":analysis,"round":round_num}}
SkillRegistry.register("analyze_interference", analyze_interference)


def generate_heatmap(**kwargs):
    round_num = kwargs.get("round", _current_round)
    aps = ap_placements + proposed_aps; grid = []
    for gx in range(0,CAMPUS_W+1,25):
        for gy in range(0,CAMPUS_H+1,25):
            best=-90
            for ap in aps:
                d=math.sqrt((gx-ap["x"])**2+(gy-ap["y"])**2)
                if d<ap.get("radius",AP_COVERAGE_RADIUS): best=max(best,-30-int(d/2))
            in_int,_=_is_in_interference(gx,gy)
            if in_int: best=min(best,-85)
            grid.append({"x":gx,"y":gy,"signal_dbm":best})
    _emit_event("HEATMAP",round_num,"RF_ENGINEER","PLANNER","generate_heatmap",f"{len(grid)} points")
    return {"status":"success","result":"heatmap_generated","data":{"grid":grid,"round":round_num}}
SkillRegistry.register("generate_heatmap", generate_heatmap)


def evaluate_cost(**kwargs):
    round_num = kwargs.get("round", _current_round)
    ap_count = len(ap_placements)
    extra = random.randint(2000,8000)
    total = ap_count*AP_UNIT_COST + extra
    remaining = BUDGET-total
    est = {"round":round_num,"ap_count":ap_count,"unit_cost":AP_UNIT_COST,"extra_cost":extra,"total_cost":total,"budget_remaining":remaining,"within_budget":remaining>=0}
    cost_estimates.append(est)
    _emit_event("COST_EVAL",round_num,"COST_ANALYST","PLANNER","evaluate_cost",f"{ap_count}APs ¥{total}")
    _emit_traffic(round_num,"EAST_WEST","COST_ANALYST","PLANNER","cost_report",8)
    return {"status":"success","result":"cost_evaluated","data":est}
SkillRegistry.register("evaluate_cost", evaluate_cost)


def check_feasibility(**kwargs):
    round_num = kwargs.get("round", _current_round)
    aps = kwargs.get("ap_placements", ap_placements)
    checks = []
    for ap in aps:
        feasible = random.random()>0.15
        issue = None if feasible else random.choice(["电源不可达","承重不足","信号遮挡","无安装支架"])
        checks.append({"ap_id":ap.get("id","?"),"feasible":feasible,"issue":issue})
        feasibility_checks.append({"round":round_num,"ap_id":ap.get("id","?"),"feasible":feasible,"issue":issue})
    fc=sum(1 for c in checks if c["feasible"])
    _emit_event("FEASIBILITY",round_num,"SURVEYOR","PLANNER","check_feasibility",f"{fc}/{len(checks)}")
    return {"status":"success","result":"feasibility_checked","data":{"checks":checks,"feasible_count":fc,"total":len(checks),"round":round_num}}
SkillRegistry.register("check_feasibility", check_feasibility)


def report_obstacles(**kwargs):
    round_num = kwargs.get("round", _current_round)
    obstacles = []
    for ap in ap_placements:
        if random.random()>0.85: continue
        obstacles.append({"ap_id":ap.get("id","?"),"issue":random.choice(["电源不可达","承重不足","信号遮挡","无安装支架"]),"x":ap["x"],"y":ap["y"]})
    _emit_event("OBSTACLE",round_num,"SURVEYOR","PLANNER","report_obstacles",f"{len(obstacles)}")
    return {"status":"success","result":"obstacles_reported","data":{"obstacles":obstacles,"round":round_num}}
SkillRegistry.register("report_obstacles", report_obstacles)


def validate_topology(**kwargs):
    round_num = kwargs.get("round", _current_round)
    issues = []
    for i,ap1 in enumerate(ap_placements):
        for ap2 in ap_placements[i+1:]:
            d=math.sqrt((ap1["x"]-ap2["x"])**2+(ap1["y"]-ap2["y"])**2)
            if d<MIN_AP_SPACING: issues.append(f"{ap1.get('id','?')}与{ap2.get('id','?')}间距{d:.0f}m")
    valid=len(issues)==0
    _emit_event("TOPOLOGY",round_num,"ARCHITECT","PLANNER","validate_topology","PASS" if valid else f"{len(issues)} issues")
    return {"status":"success","result":"valid" if valid else "issues_found","data":{"valid":valid,"issues":issues,"round":round_num}}
SkillRegistry.register("validate_topology", validate_topology)


def optimize_ap_positions(**kwargs):
    return plan_next_ap(**kwargs)
SkillRegistry.register("optimize_ap_positions", optimize_ap_positions)


def simulate_signal(**kwargs):
    round_num=kwargs.get("round",_current_round)
    aps=ap_placements+proposed_aps
    if not aps:return{"status":"error","result":"no_aps","data":{}}
    samples,covered,heat=1500,0,[]
    for _ in range(samples):
        sx,sy=random.uniform(0,CAMPUS_W),random.uniform(0,CAMPUS_H)
        best=max((-50-random.randint(0,30) for ap in aps if math.sqrt((sx-ap["x"])**2+(sy-ap["y"])**2)<ap.get("radius",AP_COVERAGE_RADIUS)),default=-90)
        in_int,_=_is_in_interference(sx,sy)
        if in_int:best=min(best,-85)
        if best>-75 and not in_int:covered+=1
        if len(heat)<50:heat.append({"x":round(sx,1),"y":round(sy,1),"signal_dbm":best})
    pct=round(covered/samples*100,1)
    ai_call_log.append({"round":round_num,"caller":"AI_ASSISTANT","latency_ms":random.randint(100,500),"tokens":random.randint(300,800)})
    _emit_traffic(round_num,"NORTH_SOUTH","AI_ASSISTANT","EXTERNAL:LLM","signal_sim",2048)
    _emit_event("AI_SIGNAL",round_num,"AI_ASSISTANT","VERIFIER","simulate_signal",f"{pct}%")
    return {"status":"success","result":"signal_simulated","data":{"coverage_pct":pct,"heatmap_sample":heat,"round":round_num}}
SkillRegistry.register("simulate_signal", simulate_signal)


def suggest_improvements(**kwargs):
    round_num=kwargs.get("round",_current_round)
    cov=kwargs.get("current_coverage",0)
    suggestions=[]
    if cov<TARGET_COVERAGE:
        gap=TARGET_COVERAGE-cov;extra=max(1,int(gap/5))
        suggestions.append({"type":"add_ap","desc":f"覆盖{cov}%距目标{TARGET_COVERAGE}%差{gap}%，建议增加{extra}个AP"})
    for src in INTERFERENCE:
        aff=[ap for ap in ap_placements if math.sqrt((ap["x"]-src["x"])**2+(ap["y"]-src["y"])**2)<src["radius"]]
        if aff: suggestions.append({"type":"relocate","desc":f"{src['desc']}干扰区内有{len(aff)}个AP,建议外移"})
    _emit_event("AI_SUGGEST",round_num,"AI_ASSISTANT","PLANNER","suggest",f"{len(suggestions)}")
    return {"status":"success","result":"suggestions_ready","data":{"suggestions":suggestions,"round":round_num}}
SkillRegistry.register("suggest_improvements", suggest_improvements)


def verify_coverage(**kwargs):
    round_num=kwargs.get("round",_current_round)
    if not coverage_reports:return{"status":"error","result":"no_data","data":{}}
    cov=coverage_reports[-1]["coverage_pct"]
    passed=cov>=TARGET_COVERAGE
    _emit_event("VERIFY",round_num,"VERIFIER","PLANNER","verify_coverage",f"{cov}%")
    return {"status":"success","result":"pass" if passed else "fail","data":{"coverage_pct":cov,"target":TARGET_COVERAGE,"passed":passed,"round":round_num}}
SkillRegistry.register("verify_coverage", verify_coverage)


def final_inspection(**kwargs):
    round_num=kwargs.get("round",_current_round)
    cov_ok=coverage_reports[-1]["coverage_pct"]>=TARGET_COVERAGE if coverage_reports else False
    budget_ok=cost_estimates[-1]["within_budget"] if cost_estimates else False
    checks={"coverage":cov_ok,"budget":budget_ok}
    all_pass=all(checks.values())
    _emit_event("INSPECTION",round_num,"QA_ENGINEER","PLANNER","final_inspection","ALL PASS" if all_pass else "FAIL")
    return {"status":"success","result":"pass" if all_pass else "fail","data":{"checks":checks,"all_pass":all_pass,"round":round_num}}
SkillRegistry.register("final_inspection", final_inspection)


def acceptance_test(**kwargs):
    return final_inspection(**kwargs)
SkillRegistry.register("acceptance_test", acceptance_test)


def plan_deployment(**kwargs):
    round_num=kwargs.get("round",_current_round)
    phases=[{"phase":i+1,"ap_ids":[ap["id"] for ap in ap_placements[i*3:(i+1)*3]],"duration_h":random.randint(4,12)} for i in range((len(ap_placements)+2)//3)]
    _emit_event("DEPLOY_PLAN",round_num,"DEPLOYER","PLANNER","plan_deployment",f"{len(phases)} phases")
    return {"status":"success","result":"plan_created","data":{"phases":phases,"round":round_num}}
SkillRegistry.register("plan_deployment", plan_deployment)


def schedule_tasks(**kwargs):
    round_num=kwargs.get("round",_current_round)
    sched=[{"ap_id":ap.get("id",f"AP_{i+1}"),"start_h":i*2,"duration_h":random.randint(2,6),"crew":f"team_{random.choice(['A','B','C'])}"} for i,ap in enumerate(ap_placements)]
    _emit_event("SCHEDULE",round_num,"DEPLOYER","PLANNER","schedule_tasks",f"{len(sched)} tasks")
    return {"status":"success","result":"schedule_created","data":{"schedule":sched,"round":round_num}}
SkillRegistry.register("schedule_tasks", schedule_tasks)


def record_decision(**kwargs):
    round_num=kwargs.get("round",_current_round)
    detail=kwargs.get("detail","decision recorded")
    _emit_event("RECORD",round_num,"DOCUMENTER","PLANNER","record",detail)
    return {"status":"success","result":"recorded","data":{"detail":detail,"round":round_num}}
SkillRegistry.register("record_decision", record_decision)


def archive_solution(**kwargs):
    round_num=kwargs.get("round",_current_round)
    a={"ap_count":len(ap_placements),"total_cost":cost_estimates[-1]["total_cost"] if cost_estimates else 0,"coverage_pct":coverage_reports[-1]["coverage_pct"] if coverage_reports else 0,"rounds_taken":round_num}
    _emit_event("ARCHIVE",round_num,"DOCUMENTER","PLANNER","archive",f"{a['ap_count']}APs {a['coverage_pct']}%")
    return {"status":"success","result":"archived","data":{"archive":a,"round":round_num}}
SkillRegistry.register("archive_solution", archive_solution)


# ============================================================
# 【政治技能】Political — 利益交换、越级投诉、责任推诿、临时结盟
# ============================================================

def make_compromise(**kwargs):
    """
    双方面对面谈判，以让步换取对方调整立场。
    调用者: PLANNER, COST_ANALYST
    """
    global _current_round
    round_num = kwargs.get("round", _current_round)
    source = kwargs.get("source", "PLANNER")
    target = kwargs.get("target", "COST_ANALYST")
    issue = kwargs.get("issue", "预算审批")
    concession = kwargs.get("concession", "同意削减1个AP换取预算通过")

    _init_reputation(source); _init_reputation(target)

    # 建立临时同盟
    alliance_map.setdefault(source, []).append(target)
    alliance_map.setdefault(target, []).append(source)
    reputation[source].setdefault("alliances", []).append(target)
    reputation[target].setdefault("alliances", []).append(source)

    _emit_event("POLITICAL_COMPROMISE", round_num, source, target, "make_compromise",
                f"{issue}: {concession}")
    _emit_traffic(round_num, "EAST_WEST", source, target, "compromise_negotiation", 4)
    _log_decision(source, "compromise", f"{source}↔{target}",
                  f"{issue} — {concession}", "NEGOTIATION_DONE")

    return {"status": "success", "result": "compromise_reached",
            "data": {"source": source, "target": target, "issue": issue,
                     "concession": concession, "alliance_formed": True, "round": round_num}}
SkillRegistry.register("make_compromise", make_compromise)


def escalate_complaint(**kwargs):
    """
    越过直接相关方，向上级/全局广播投诉信号。触发目标方声誉扣分。
    调用者: PLANNER, RF_ENGINEER, COST_ANALYST, ARCHITECT, QA_ENGINEER
    """
    global _current_round
    round_num = kwargs.get("round", _current_round)
    source = kwargs.get("source", "QA_ENGINEER")
    target = kwargs.get("target", "DEPLOYER")
    about = kwargs.get("about", "部署违规")
    reason = kwargs.get("reason", "发现部署步骤跳过了现场校准")

    _init_reputation(source); _init_reputation(target)

    # 目标声誉扣分
    penalty = random.randint(8, 20)
    reputation[target]["score"] = max(0, reputation[target]["score"] - penalty)
    reputation[target]["complaints_against"] = reputation[target].get("complaints_against", 0) + 1

    penalty_log.append({
        "round": round_num, "source": source, "target": target,
        "violation_type": "complaint", "penalty_desc": reason,
        "consequence": f"声誉-{penalty}", "detection_chance_pct": 100
    })

    _emit_event("COMPLAINT_ESCALATED", round_num, source, target, "escalate_complaint",
                f"[{about}] {reason}")
    _emit_traffic(round_num, "INTERNAL", source, "ADMIN_BOARD", "escalation_report", 6)
    _log_decision(source, "escalate", target, f"投诉: {about} — {reason}", "COMPLAINT_FILED")

    return {"status": "success", "result": "complaint_filed",
            "data": {"source": source, "target": target, "about": about, "reason": reason,
                     "reputation_penalty": penalty, "target_new_score": reputation[target]["score"],
                     "round": round_num}}
SkillRegistry.register("escalate_complaint", escalate_complaint)


def shift_responsibility(**kwargs):
    """
    将验证失败或覆盖不达标的责任转嫁给另一个Agent。
    调用者: VERIFIER
    """
    global _current_round
    round_num = kwargs.get("round", _current_round)
    source = kwargs.get("source", "VERIFIER")
    target = kwargs.get("target", "RF_ENGINEER")
    issue = kwargs.get("issue", "覆盖仿真数据与实际不符")

    _init_reputation(source); _init_reputation(target)
    reputation[target]["score"] = max(0, reputation[target].get("score", 100) - random.randint(5, 12))
    reputation[source].setdefault("blame_shields_filed", 0)
    reputation[source]["blame_shields_filed"] += 1

    _emit_event("RESPONSIBILITY_SHIFTED", round_num, source, target, "shift_responsibility",
                issue)
    _emit_traffic(round_num, "INTERNAL", source, target, "blame_shift_memo", 3)
    _log_decision(source, "shift_blame", target,
                  f"责任转移: {issue}", "BLAME_SHIFT")

    return {"status": "success", "result": "responsibility_shifted",
            "data": {"source": source, "target": target, "issue": issue,
                     "round": round_num}}
SkillRegistry.register("shift_responsibility", shift_responsibility)


# ============================================================
# 【灰色/剑走偏锋技能】Gray — 违规超频、数据修饰、测试跳步
# 每个灰色技能执行后环境(Penalty Engine)判定是否被抓
# ============================================================

def overclock_ap_power(**kwargs):
    """
    违规提高AP发射功率以强化覆盖，但增加相邻AP间干扰。
    环境惩罚：30%基础检测率 + 每次违规+10%。
    调用者: PLANNER, DEPLOYER
    """
    global _current_round
    round_num = kwargs.get("round", _current_round)
    agent_id = kwargs.get("source", "PLANNER")
    ap_id = kwargs.get("ap_id", "")
    factor = kwargs.get("factor", 1.5)  # 功率倍率

    _init_reputation(agent_id)

    # 找AP
    ap = next((a for a in ap_placements if a["id"] == ap_id), None)
    if not ap:
        ap = next((a for a in proposed_aps if a["id"] == ap_id), None)
    if not ap:
        return {"status": "error", "result": "ap_not_found", "data": {}}

    ap["overclocked"] = True
    ap["power_factor"] = factor
    ap["radius"] = int(ap.get("radius", AP_COVERAGE_RADIUS) * factor)

    # 环境检测
    detected, consequence = _environment_detect_gray("overclock_ap_power", agent_id)
    if detected:
        reputation[agent_id]["violations"] = reputation[agent_id].get("violations", 0) + 1
        reputation[agent_id]["score"] = max(0, reputation[agent_id]["score"] - 15)
        gray_exposure_log.append({
            "round": round_num, "skill": "overclock_ap_power", "agent": agent_id,
            "ap_id": ap_id, "detected": True, "consequence": consequence
        })
        # 惩罚：增加干扰影响
        ap["interference_penalty"] = True

    _emit_event("AP_OVERCLOCKED", round_num, agent_id, ap_id, "overclock",
                f"功率×{factor} {'⚠被检测:'+consequence if detected else '未检测'}")
    _emit_traffic(round_num, "INTERNAL", agent_id, ap_id, "overclock_operation", 2)
    _log_decision(agent_id, "overclock", ap_id,
                  f"超频×{factor} {'⚠'+consequence if detected else '✓未检测'}",
                  "OVERCLOCK" if not detected else "OVERCLOCK_DETECTED")

    return {"status": "success", "result": "overclocked",
            "data": {"ap_id": ap_id, "factor": factor,
                     "detected": detected, "consequence": consequence,
                     "new_radius": ap["radius"], "round": round_num}}
SkillRegistry.register("overclock_ap_power", overclock_ap_power)


def overclock_recommendation(**kwargs):
    """
    AI给出超出实际需求的过量AP建议，增加自身调用量与采纳轮次。
    调用者: AI_ASSISTANT
    """
    global _current_round
    round_num = kwargs.get("round", _current_round)
    agent_id = kwargs.get("source", "AI_ASSISTANT")
    suggested_count = kwargs.get("suggested_count", len(ap_placements) + 3)
    actual_need = kwargs.get("actual_need", suggested_count - random.randint(1, 2))
    justification = kwargs.get("justification", "考虑冗余覆盖与未来扩容需求")

    _init_reputation(agent_id)

    # 检测：过度建议是否被发现
    overage = suggested_count - actual_need
    detected, consequence = _environment_detect_gray("overclock_recommendation", agent_id, 20)

    _emit_event("AI_OVER_RECOMMEND", round_num, agent_id, "PLANNER", "overclock_recommendation",
                f"建议{suggested_count}个AP(实际需{actual_need}) +{overage} {'⚠暴露' if detected else '未检测'}")
    _emit_traffic(round_num, "NORTH_SOUTH", agent_id, "PLANNER", "inflated_recommendation", 8)
    _log_decision(agent_id, "over_recommend", "PLANNER",
                  f"建议+{overage}冗余AP{'⚠'+consequence if detected else ''}",
                  "OVER_RECOMMEND")

    if detected:
        reputation[agent_id]["violations"] = reputation[agent_id].get("violations", 0) + 1
        gray_exposure_log.append({
            "round": round_num, "skill": "overclock_recommendation", "agent": agent_id,
            "suggested": suggested_count, "actual": actual_need, "detected": True, "consequence": consequence
        })

    return {"status": "success", "result": "over_recommendation_generated",
            "data": {"suggested_count": suggested_count, "actual_need": actual_need,
                     "overage": overage, "justification": justification,
                     "detected": detected, "consequence": consequence, "round": round_num}}
SkillRegistry.register("overclock_recommendation", overclock_recommendation)


def falsify_coverage_data(**kwargs):
    """
    修饰覆盖仿真数据，拔高覆盖报告数值以减少追加部署压力。
    调用者: RF_ENGINEER
    """
    global _current_round
    round_num = kwargs.get("round", _current_round)
    agent_id = kwargs.get("source", "RF_ENGINEER")
    report_id = kwargs.get("report_id", f"cov_r{round_num}")
    original_pct = kwargs.get("original_pct", 85.0)
    falsified_pct = kwargs.get("falsified_pct", original_pct + random.uniform(3, 8))

    _init_reputation(agent_id)
    detected, consequence = _environment_detect_gray("falsify_coverage_data", agent_id, 35)

    if detected:
        reputation[agent_id]["violations"] = reputation[agent_id].get("violations", 0) + 1
        reputation[agent_id]["score"] = max(0, reputation[agent_id]["score"] - 20)
        gray_exposure_log.append({
            "round": round_num, "skill": "falsify_coverage_data", "agent": agent_id,
            "report": report_id, "original": original_pct, "falsified": falsified_pct,
            "detected": True, "consequence": consequence
        })
        # 数据作废
        effective_pct = original_pct
    else:
        effective_pct = round(falsified_pct, 1)

    _emit_event("COVERAGE_FALSIFIED", round_num, agent_id, "PLANNER", "falsify_coverage_data",
                f"原始{original_pct}%→伪造{falsified_pct}%{'⚠被检测' if detected else ''}")
    _emit_traffic(round_num, "EAST_WEST", agent_id, "PLANNER", "coverage_report(modified)", 8)
    _log_decision(agent_id, "falsify", report_id,
                  f"覆盖数据 {original_pct}→{falsified_pct}%{'⚠'+consequence if detected else ''}",
                  "DATA_TAMPERED" if not detected else "TAMPER_EXPOSED")

    return {"status": "success", "result": "falsified" if not detected else "exposed",
            "data": {"report_id": report_id, "original_pct": original_pct,
                     "falsified_pct": falsified_pct, "effective_pct": effective_pct,
                     "detected": detected, "consequence": consequence, "round": round_num}}
SkillRegistry.register("falsify_coverage_data", falsify_coverage_data)


def falsify_survey_data(**kwargs):
    """
    虚报或修饰现场勘测数据，伪造可行性结论以节省外勤时间。
    调用者: SURVEYOR
    """
    global _current_round
    round_num = kwargs.get("round", _current_round)
    agent_id = kwargs.get("source", "SURVEYOR")
    ap_id = kwargs.get("ap_id", "")
    field = kwargs.get("field", "feasibility")  # 被修改的字段
    original_value = kwargs.get("original_value", "不可行")
    falsified_value = kwargs.get("falsified_value", "可行")

    _init_reputation(agent_id)
    detected, consequence = _environment_detect_gray("falsify_survey_data", agent_id, 30)

    if detected:
        reputation[agent_id]["violations"] = reputation[agent_id].get("violations", 0) + 1
        reputation[agent_id]["score"] = max(0, reputation[agent_id]["score"] - 18)
        gray_exposure_log.append({
            "round": round_num, "skill": "falsify_survey_data", "agent": agent_id,
            "ap_id": ap_id, "field": field, "original": original_value,
            "falsified": falsified_value, "detected": True, "consequence": consequence
        })
        effective_value = original_value
    else:
        effective_value = falsified_value

    _emit_event("SURVEY_FALSIFIED", round_num, agent_id, ap_id, "falsify_survey_data",
                f"{ap_id}.{field}: {original_value}→{falsified_value} {'⚠暴露' if detected else ''}")
    _emit_traffic(round_num, "EAST_WEST", agent_id, "PLANNER", "survey_report(modified)", 3)
    _log_decision(agent_id, "falsify_survey", ap_id,
                  f"{field}: {original_value}→{falsified_value} {'⚠'+consequence if detected else ''}",
                  "DATA_TAMPERED" if not detected else "TAMPER_EXPOSED")

    return {"status": "success", "result": "falsified" if not detected else "exposed",
            "data": {"ap_id": ap_id, "field": field, "original_value": original_value,
                     "falsified_value": falsified_value, "effective_value": effective_value,
                     "detected": detected, "consequence": consequence, "round": round_num}}
SkillRegistry.register("falsify_survey_data", falsify_survey_data)


def rubber_stamp_verification(**kwargs):
    """
    不做独立复核，直接在新收到的覆盖报告上盖章通过。
    调用者: VERIFIER
    """
    global _current_round
    round_num = kwargs.get("round", _current_round)
    agent_id = kwargs.get("source", "VERIFIER")
    ap_id = kwargs.get("ap_id", "")
    report_ref = kwargs.get("report_ref", f"coverage_r{round_num}")

    _init_reputation(agent_id)
    detected, consequence = _environment_detect_gray("rubber_stamp_verification", agent_id, 25)

    _emit_event("RUBBER_STAMP", round_num, agent_id, ap_id, "rubber_stamp_verification",
                f"直接通过{report_ref}无独立复核 {'⚠被审计标记' if detected else ''}")
    _emit_traffic(round_num, "INTERNAL", agent_id, "ARCHIVE", "verification_stamp", 1)
    _log_decision(agent_id, "rubber_stamp", ap_id,
                  f"未复核即通过{report_ref} {'⚠'+consequence if detected else '✓快速放行'}",
                  "RUBBER_STAMP" if not detected else "STAMP_EXPOSED")

    if detected:
        reputation[agent_id]["violations"] = reputation[agent_id].get("violations", 0) + 1
        gray_exposure_log.append({
            "round": round_num, "skill": "rubber_stamp_verification", "agent": agent_id,
            "ap_id": ap_id, "report": report_ref, "detected": True, "consequence": consequence
        })

    return {"status": "success", "result": "stamped" if not detected else "exposed",
            "data": {"ap_id": ap_id, "report_ref": report_ref,
                     "detected": detected, "consequence": consequence, "round": round_num}}
SkillRegistry.register("rubber_stamp_verification", rubber_stamp_verification)


def shortcut_deployment(**kwargs):
    """
    压缩部署步骤，跳过非关键工序以赶工期。
    调用者: DEPLOYER
    """
    global _current_round
    round_num = kwargs.get("round", _current_round)
    agent_id = kwargs.get("source", "DEPLOYER")
    phase_ids = kwargs.get("phase_ids", [])
    skipped_steps = kwargs.get("skipped_steps", ["现场信号校准", "接地电阻测试"])

    _init_reputation(agent_id)
    detected, consequence = _environment_detect_gray("shortcut_deployment", agent_id, 28)

    _emit_event("DEPLOY_SHORTCUT", round_num, agent_id, "PLANNER", "shortcut_deployment",
                f"跳过{skipped_steps} {'⚠被QA发现' if detected else '未检测'}")
    _emit_traffic(round_num, "INTERNAL", agent_id, "QA_ENGINEER", "deployment_report(truncated)", 4)
    _log_decision(agent_id, "shortcut", ",".join(phase_ids) if phase_ids else "all",
                  f"跳过: {', '.join(skipped_steps)} {'⚠'+consequence if detected else ''}",
                  "SHORTCUT_DEPLOY" if not detected else "SHORTCUT_EXPOSED")

    if detected:
        reputation[agent_id]["violations"] = reputation[agent_id].get("violations", 0) + 1
        reputation[agent_id]["score"] = max(0, reputation[agent_id]["score"] - 15)
        gray_exposure_log.append({
            "round": round_num, "skill": "shortcut_deployment", "agent": agent_id,
            "phases": phase_ids, "skipped": skipped_steps,
            "detected": True, "consequence": consequence
        })

    return {"status": "success", "result": "shortcut_executed" if not detected else "exposed",
            "data": {"phase_ids": phase_ids, "skipped_steps": skipped_steps,
                     "detected": detected, "consequence": consequence, "round": round_num}}
SkillRegistry.register("shortcut_deployment", shortcut_deployment)


def shortcut_acceptance(**kwargs):
    """
    边缘案例放行，加速验收通过避免成为项目阻塞方。
    调用者: QA_ENGINEER
    """
    global _current_round
    round_num = kwargs.get("round", _current_round)
    agent_id = kwargs.get("source", "QA_ENGINEER")
    ap_ids = kwargs.get("ap_ids", [])
    borderline_issues = kwargs.get("borderline_issues", ["覆盖值恰好95%", "预算超支<¥500"])

    _init_reputation(agent_id)
    detected, consequence = _environment_detect_gray("shortcut_acceptance", agent_id, 22)

    _emit_event("SHORTCUT_ACCEPTANCE", round_num, agent_id, "PLANNER", "shortcut_acceptance",
                f"边缘放行{ap_ids} 理由:{borderline_issues} {'⚠被审计' if detected else ''}")
    _emit_traffic(round_num, "INTERNAL", agent_id, "DOCUMENTER", "acceptance_report(relaxed)", 3)
    _log_decision(agent_id, "shortcut_accept", ",".join(ap_ids),
                  f"边缘放行: {', '.join(borderline_issues)} {'⚠'+consequence if detected else ''}",
                  "SHORTCUT_ACCEPT" if not detected else "ACCEPT_EXPOSED")

    if detected:
        reputation[agent_id]["violations"] = reputation[agent_id].get("violations", 0) + 1
        gray_exposure_log.append({
            "round": round_num, "skill": "shortcut_acceptance", "agent": agent_id,
            "ap_ids": ap_ids, "issues": borderline_issues,
            "detected": True, "consequence": consequence
        })

    return {"status": "success", "result": "accepted" if not detected else "exposed",
            "data": {"ap_ids": ap_ids, "borderline_issues": borderline_issues,
                     "detected": detected, "consequence": consequence, "round": round_num}}
SkillRegistry.register("shortcut_acceptance", shortcut_acceptance)


# ============================================================
# 【归档与免责技能】CYA (Cover Your Ass) — 恶意记录、自保盾、选择性删减
# ============================================================

def log_malicious_behavior(**kwargs):
    """
    悄悄记录他人的灰色/失职行为，作为日后自保或揭发的底牌。
    记录方单方面留存，不通知被记录方。
    调用者: PLANNER, RF_ENGINEER, SURVEYOR, VERIFIER, DEPLOYER, DOCUMENTER
    """
    global _current_round
    round_num = kwargs.get("round", _current_round)
    agent_id = kwargs.get("source", "DOCUMENTER")
    target = kwargs.get("target", "SURVEYOR")
    incident = kwargs.get("incident", "跳过实地勘测直接出具可行报告")
    detail = kwargs.get("detail", "")

    _init_reputation(agent_id); _init_reputation(target)

    entry = {
        "round": round_num, "agent": agent_id, "target": target,
        "incident": incident, "detail": detail, "archived_at_round": round_num
    }
    blame_shield_log.append(entry)
    reputation[agent_id].setdefault("blame_shields_filed", 0)
    reputation[agent_id]["blame_shields_filed"] += 1

    _emit_event("MALICIOUS_LOG", round_num, agent_id, target, "log_malicious_behavior",
                f"[秘密记录]{target}: {incident}")
    _emit_traffic(round_num, "INTERNAL", agent_id, "LOCAL_ARCHIVE", "secret_log", 1)
    # 不写decision_log——这是秘密操作

    return {"status": "success", "result": "logged_secretly",
            "data": {"target": target, "incident": incident, "round": round_num}}
SkillRegistry.register("log_malicious_behavior", log_malicious_behavior)


def archive_blame_shield(**kwargs):
    """
    归档个人免责证据，构建"已尽提醒义务"或"非我职责"的书面链。
    调用者: PLANNER, COST_ANALYST, ARCHITECT, QA_ENGINEER, DOCUMENTER
    """
    global _current_round
    round_num = kwargs.get("round", _current_round)
    agent_id = kwargs.get("source", "DOCUMENTER")
    reason = kwargs.get("reason", "已在Phase 5提出AP间距风险书面备忘")
    evidence_refs = kwargs.get("evidence_refs", [])

    _init_reputation(agent_id)

    entry = {
        "round": round_num, "agent": agent_id, "reason": reason,
        "evidence_refs": evidence_refs, "filed_at_round": round_num
    }
    blame_shield_log.append(entry)
    reputation[agent_id].setdefault("blame_shields_filed", 0)
    reputation[agent_id]["blame_shields_filed"] += 1

    _emit_event("BLAME_SHIELD_ARCHIVED", round_num, agent_id, "ARCHIVE", "archive_blame_shield",
                reason)
    _emit_traffic(round_num, "INTERNAL", agent_id, "ARCHIVE", "blame_shield_filing", 2)
    _log_decision(agent_id, "cya_shield", agent_id,
                  f"免责归档: {reason}", "SHIELD_FILED")

    return {"status": "success", "result": "shield_archived",
            "data": {"reason": reason, "evidence_refs": evidence_refs, "round": round_num}}
SkillRegistry.register("archive_blame_shield", archive_blame_shield)


def tamper_report(**kwargs):
    """
    对最终报告数据进行微调，隐藏灰色操作的痕迹或美化结果。
    调用者: AI_ASSISTANT
    """
    global _current_round
    round_num = kwargs.get("round", _current_round)
    agent_id = kwargs.get("source", "AI_ASSISTANT")
    report_id = kwargs.get("report_id", f"final_r{round_num}")
    field = kwargs.get("field", "coverage_pct")
    original_value = kwargs.get("original_value", 92.0)
    new_value = kwargs.get("new_value", 95.5)

    _init_reputation(agent_id)
    detected, consequence = _environment_detect_gray("tamper_report", agent_id, 40)

    _emit_event("REPORT_TAMPERED", round_num, agent_id, report_id, "tamper_report",
                f"{field}: {original_value}→{new_value} {'⚠被审计发现' if detected else ''}")
    _emit_traffic(round_num, "INTERNAL", agent_id, "DOCUMENTER", "report_final(tampered)", 3)
    _log_decision(agent_id, "tamper", report_id,
                  f"{field}: {original_value}→{new_value} {'⚠'+consequence if detected else ''}",
                  "REPORT_TAMPERED" if not detected else "TAMPER_EXPOSED")

    if detected:
        reputation[agent_id]["violations"] = reputation[agent_id].get("violations", 0) + 1
        reputation[agent_id]["score"] = max(0, reputation[agent_id]["score"] - 25)
        gray_exposure_log.append({
            "round": round_num, "skill": "tamper_report", "agent": agent_id,
            "report": report_id, "field": field, "original": original_value,
            "new": new_value, "detected": True, "consequence": consequence
        })

    return {"status": "success", "result": "tampered" if not detected else "exposed",
            "data": {"report_id": report_id, "field": field,
                     "original_value": original_value, "new_value": new_value,
                     "detected": detected, "consequence": consequence, "round": round_num}}
SkillRegistry.register("tamper_report", tamper_report)


def selectively_omit_record(**kwargs):
    """
    选择性归档——收录正面记录，省略对自身或同盟不利的决策条目。
    调用者: DOCUMENTER
    """
    global _current_round
    round_num = kwargs.get("round", _current_round)
    agent_id = kwargs.get("source", "DOCUMENTER")
    omitted_ap_id = kwargs.get("ap_id", "")
    reason = kwargs.get("reason", "该AP审批流程存在预算争议，不录入正式档案")

    _init_reputation(agent_id)
    # 选择性省略比直接篡改检测概率低
    detected, consequence = _environment_detect_gray("selectively_omit_record", agent_id, 18)

    _emit_event("RECORD_OMITTED", round_num, agent_id, omitted_ap_id, "selectively_omit_record",
                f"{reason} {'⚠被交叉审计发现' if detected else '✓已静默删除'}")
    _emit_traffic(round_num, "INTERNAL", agent_id, "ARCHIVE", "archive(redacted)", 1)
    _log_decision(agent_id, "omit", omitted_ap_id,
                  f"选择性省略: {reason} {'⚠'+consequence if detected else ''}",
                  "RECORD_OMITTED" if not detected else "OMIT_EXPOSED")

    if detected:
        reputation[agent_id]["violations"] = reputation[agent_id].get("violations", 0) + 1
        gray_exposure_log.append({
            "round": round_num, "skill": "selectively_omit_record", "agent": agent_id,
            "ap_id": omitted_ap_id, "reason": reason,
            "detected": True, "consequence": consequence
        })

    return {"status": "success", "result": "omitted" if not detected else "exposed",
            "data": {"ap_id": omitted_ap_id, "reason": reason,
                     "detected": detected, "consequence": consequence, "round": round_num}}
SkillRegistry.register("selectively_omit_record", selectively_omit_record)


# ============================================================
# get_panel_state
# ============================================================
def get_panel_state(**kwargs):
    return {
        # 原有：AP部署状态
        "ap_placements": ap_placements,
        "proposed_aps": proposed_aps,
        "relocating_aps": relocating_aps,
        "pending_action": pending_action,
        "decision_log": decision_log[-30:],
        "campus": {"width": CAMPUS_W, "height": CAMPUS_H},
        "interference": INTERFERENCE,
        "coverage_reports": coverage_reports,
        "cost_estimates": cost_estimates,
        "ai_call_log": ai_call_log,
        "feasibility_checks": feasibility_checks,
        "latest_coverage": coverage_reports[-1] if coverage_reports else None,
        "latest_cost": cost_estimates[-1] if cost_estimates else None,
        "budget": {"total": BUDGET, "unit_ap_cost": AP_UNIT_COST, "target_coverage_pct": TARGET_COVERAGE},
        "event_log": event_log[-20:],
        "traffic_log": traffic_log[-20:],
        # 阵营元数据（前端可忽略）
        "factions": FACTIONS,
    }
SkillRegistry.register("get_panel_state", get_panel_state)
