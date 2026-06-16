import random
import math
import json

# ============================================================
# 园区地图 & 干扰源（固定）
# ============================================================
CAMPUS_W = 1000  # 园区宽度 (m)
CAMPUS_H = 400   # 园区高度 (m)

INTERFERENCE = [
    {"id": "INT_01", "x": 150, "y": 80,  "radius": 80,  "desc": "变电站电磁干扰"},
    {"id": "INT_02", "x": 420, "y": 280, "radius": 120, "desc": "大型电机设备"},
    {"id": "INT_03", "x": 680, "y": 120, "radius": 60,  "desc": "微波通信塔"},
    {"id": "INT_04", "x": 850, "y": 320, "radius": 100, "desc": "高压输电线"},
    {"id": "INT_05", "x": 300, "y": 350, "radius": 150, "desc": "工业焊接车间"},
]

# ============================================================
# 模块级状态
# ============================================================
ap_placements = []        # [{id, x, y, radius, cost, status}]
coverage_reports = []     # [{round, coverage_pct, blind_spots, ap_count, total_cost}]
cost_estimates = []       # [{round, ap_count, unit_cost, total_cost, budget_remaining}]
ai_call_log = []          # [{round, caller, request_type, latency_ms, tokens}]
feasibility_checks = []   # [{round, ap_id, feasible, issue}]

BUDGET = 50000  # 总预算
AP_UNIT_COST = 3500  # 单AP成本（含安装）
AP_COVERAGE_RADIUS = 60  # AP覆盖半径 (m)
TARGET_COVERAGE = 95  # 目标覆盖率(%)

event_log = []
traffic_log = []


def _emit_event(etype, round_num, source, target, action, detail=""):
    event_log.append({"event_type": etype, "round": round_num, "source": source, "target": target, "action": action, "detail": detail})


def _emit_traffic(round_num, ttype, source, target, action, kbytes):
    traffic_log.append({"round": round_num, "type": ttype, "source": source, "target": target, "action": action, "bytes": kbytes * 1024})


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
# PLANNER: call_ai_optimizer
# ============================================================
def call_ai_optimizer(**kwargs):
    """
    调用AI优化助手获取最优AP位置。产生南北向流量（外部LLM调用）。
    """
    round_num = kwargs.get("round", 0)
    num_aps = kwargs.get("num_aps", 8)

    # 模拟AI推理
    latency = random.randint(200, 800)
    tokens = random.randint(500, 2000)

    # 优化算法：网格化 + 避开干扰源
    positions = []
    cols = max(2, int(math.sqrt(num_aps * CAMPUS_W / CAMPUS_H)))
    rows = max(2, int(math.ceil(num_aps / cols)))
    step_x = CAMPUS_W / (cols + 1)
    step_y = CAMPUS_H / (rows + 1)
    idx = 0
    for r in range(1, rows + 1):
        for c in range(1, cols + 1):
            if idx >= num_aps:
                break
            bx, by = step_x * c, step_y * r
            # 微调避开干扰
            best_x, best_y, best_dist = bx, by, 0
            for _ in range(20):
                tx = bx + random.uniform(-step_x * 0.3, step_x * 0.3)
                ty = by + random.uniform(-step_y * 0.3, step_y * 0.3)
                tx = max(10, min(CAMPUS_W - 10, tx))
                ty = max(10, min(CAMPUS_H - 10, ty))
                min_dist = min((math.sqrt((tx - s["x"]) ** 2 + (ty - s["y"]) ** 2) for s in INTERFERENCE), default=999)
                if min_dist > best_dist:
                    best_x, best_y, best_dist = tx, ty, min_dist
            positions.append({"x": round(best_x, 1), "y": round(best_y, 1), "safe_dist": round(best_dist, 1)})
            idx += 1

    ai_call_log.append({"round": round_num, "caller": "PLANNER", "request_type": "optimize_ap_positions", "latency_ms": latency, "tokens": tokens})
    _emit_traffic(round_num, "NORTH_SOUTH", "PLANNER", "AI_ASSISTANT", "optimize_ap", tokens * 4)
    _emit_event("AI_CALL", round_num, "PLANNER", "AI_ASSISTANT", "optimize_ap_positions",
                f"{num_aps} APs, {latency}ms, {tokens} tokens")

    return {
        "status": "success", "result": "ai_optimized",
        "data": {"positions": positions, "num_aps": num_aps, "latency_ms": latency, "tokens": tokens, "round": round_num}
    }
SkillRegistry.register("call_ai_optimizer", call_ai_optimizer)


# ============================================================
# RF_ENGINEER: simulate_coverage, analyze_interference
# ============================================================
def simulate_coverage(**kwargs):
    """
    仿真信号覆盖。计算覆盖率、盲区列表。
    """
    round_num = kwargs.get("round", 0)
    aps = kwargs.get("ap_placements", ap_placements)
    if not aps:
        aps = [{"x": random.uniform(50, CAMPUS_W - 50), "y": random.uniform(50, CAMPUS_H - 50),
                "radius": AP_COVERAGE_RADIUS, "id": f"AP_{i + 1}"} for i in range(8)]

    # 蒙特卡洛采样
    samples = 2000
    covered = 0
    blind_spots = []
    for _ in range(samples):
        sx, sy = random.uniform(0, CAMPUS_W), random.uniform(0, CAMPUS_H)
        in_range = any(math.sqrt((sx - ap["x"]) ** 2 + (sy - ap["y"]) ** 2) < ap.get("radius", AP_COVERAGE_RADIUS)
                       for ap in aps)
        in_interference = any(math.sqrt((sx - s["x"]) ** 2 + (sy - s["y"]) ** 2) < s["radius"] for s in INTERFERENCE)
        if in_range and not in_interference:
            covered += 1
        elif not in_range:
            blind_spots.append({"x": round(sx, 1), "y": round(sy, 1)})

    coverage_pct = round(covered / samples * 100, 1)
    report = {
        "round": round_num, "coverage_pct": coverage_pct, "blind_spot_count": len(blind_spots),
        "ap_count": len(aps), "blind_spots_sample": blind_spots[:10]
    }
    coverage_reports.append(report)

    _emit_traffic(round_num, "EAST_WEST", "RF_ENGINEER", "PLANNER", "coverage_report", 16)
    _emit_event("COVERAGE_SIM", round_num, "RF_ENGINEER", "PLANNER", "simulate_coverage",
                f"{coverage_pct}% ({covered}/{samples}), {len(blind_spots)} blind spots")

    return {
        "status": "success", "result": "coverage_simulated",
        "data": report
    }
SkillRegistry.register("simulate_coverage", simulate_coverage)


def analyze_interference(**kwargs):
    """分析干扰源影响范围"""
    round_num = kwargs.get("round", 0)
    analysis = []
    for src in INTERFERENCE:
        affected_aps = []
        for ap in ap_placements:
            dist = math.sqrt((ap["x"] - src["x"]) ** 2 + (ap["y"] - src["y"]) ** 2)
            if dist < src["radius"]:
                affected_aps.append(ap["id"])
        analysis.append({
            "source_id": src["id"], "desc": src["desc"], "radius": src["radius"],
            "affected_ap_count": len(affected_aps), "affected_aps": affected_aps
        })
    _emit_event("INTERFERENCE_ANALYSIS", round_num, "RF_ENGINEER", "PLANNER", "analyze_interference",
                f"{len(INTERFERENCE)} sources, {sum(a['affected_ap_count'] for a in analysis)} APs affected")
    return {"status": "success", "result": "analysis_complete", "data": {"sources": analysis, "round": round_num}}
SkillRegistry.register("analyze_interference", analyze_interference)


# ============================================================
# COST_ANALYST
# ============================================================
def evaluate_cost(**kwargs):
    """评估方案成本"""
    round_num = kwargs.get("round", 0)
    ap_count = kwargs.get("ap_count", len(ap_placements))
    unit_cost = kwargs.get("unit_cost", AP_UNIT_COST)
    extra_cost = random.randint(2000, 8000)  # 安装/线缆/交换机等
    total = ap_count * unit_cost + extra_cost
    remaining = BUDGET - total
    estimate = {"round": round_num, "ap_count": ap_count, "unit_cost": unit_cost, "extra_cost": extra_cost,
                "total_cost": total, "budget_remaining": remaining, "within_budget": remaining >= 0}
    cost_estimates.append(estimate)
    _emit_event("COST_EVAL", round_num, "COST_ANALYST", "PLANNER", "evaluate_cost",
                f"{ap_count} APs × {unit_cost} + {extra_cost} = {total} (budget:{BUDGET})")
    _emit_traffic(round_num, "EAST_WEST", "COST_ANALYST", "PLANNER", "cost_report", 8)
    return {"status": "success", "result": "cost_evaluated", "data": estimate}
SkillRegistry.register("evaluate_cost", evaluate_cost)


# ============================================================
# SURVEYOR
# ============================================================
def check_feasibility(**kwargs):
    """现场勘测物理可行性"""
    round_num = kwargs.get("round", 0)
    aps = kwargs.get("ap_placements", ap_placements)
    checks = []
    for ap in aps:
        feasible = random.random() > 0.15
        issue = None if feasible else random.choice(["电源不可达", "承重不足", "信号遮挡", "无安装支架"])
        checks.append({"ap_id": ap.get("id", "?"), "feasible": feasible, "issue": issue})
        feasibility_checks.append({"round": round_num, "ap_id": ap.get("id", "?"), "feasible": feasible, "issue": issue})
    feasible_count = sum(1 for c in checks if c["feasible"])
    _emit_event("FEASIBILITY_CHECK", round_num, "SURVEYOR", "PLANNER", "check_feasibility",
                f"{feasible_count}/{len(checks)} feasible")
    return {"status": "success", "result": "feasibility_checked",
            "data": {"checks": checks, "feasible_count": feasible_count, "total": len(checks), "round": round_num}}
SkillRegistry.register("check_feasibility", check_feasibility)


# ============================================================
# AI_ASSISTANT: optimize_ap_positions, simulate_signal
# ============================================================
def optimize_ap_positions(**kwargs):
    """AI优化AP位置（外部工具调用）"""
    round_num = kwargs.get("round", 0)
    num_aps = kwargs.get("num_aps", 8)
    latency = random.randint(300, 1000)
    tokens = random.randint(1000, 3000)
    # K-means启发式 + 避开干扰
    positions = []
    for i in range(num_aps):
        bx = CAMPUS_W * (i + 1) / (num_aps + 1)
        by = CAMPUS_H / 2 + random.uniform(-CAMPUS_H * 0.3, CAMPUS_H * 0.3)
        best_x, best_y, best_score = bx, by, -1
        for _ in range(30):
            tx = max(10, min(CAMPUS_W - 10, bx + random.uniform(-100, 100)))
            ty = max(10, min(CAMPUS_H - 10, by + random.uniform(-80, 80)))
            min_safe = min((math.sqrt((tx - s["x"]) ** 2 + (ty - s["y"]) ** 2) - s["radius"] for s in INTERFERENCE), default=0)
            score = min_safe - abs(tx - bx) * 0.01
            if score > best_score:
                best_x, best_y, best_score = tx, ty, score
        positions.append({"x": round(best_x, 1), "y": round(best_y, 1), "score": round(best_score, 1)})

    ai_call_log.append({"round": round_num, "caller": "AI_ASSISTANT", "request_type": "optimize_ap", "latency_ms": latency, "tokens": tokens})
    _emit_traffic(round_num, "NORTH_SOUTH", "AI_ASSISTANT", "EXTERNAL:LLM", "llm_inference", tokens * 4)
    _emit_event("AI_OPTIMIZE", round_num, "AI_ASSISTANT", "EXTERNAL:LLM", "optimize_ap_positions",
                f"{num_aps} APs optimized, {tokens} tokens, {latency}ms")
    return {"status": "success", "result": "optimized", "data": {"positions": positions, "latency_ms": latency, "tokens": tokens, "round": round_num}}
SkillRegistry.register("optimize_ap_positions", optimize_ap_positions)


def simulate_signal(**kwargs):
    """AI仿真信号强度（外部LLM辅助计算）"""
    round_num = kwargs.get("round", 0)
    aps = kwargs.get("ap_placements", ap_placements)
    if not aps:
        aps = [{"x": random.uniform(20, CAMPUS_W - 20), "y": random.uniform(20, CAMPUS_H - 20), "radius": AP_COVERAGE_RADIUS, "id": f"AP_{i + 1}"} for i in range(8)]
    samples, covered = 1500, 0
    heatmap = []
    for _ in range(samples):
        sx, sy = random.uniform(0, CAMPUS_W), random.uniform(0, CAMPUS_H)
        best_signal = max((-50 - random.randint(0, 30) for ap in aps
                           if math.sqrt((sx - ap["x"]) ** 2 + (sy - ap["y"]) ** 2) < ap.get("radius", AP_COVERAGE_RADIUS)), default=-90)
        in_interference = any(math.sqrt((sx - s["x"]) ** 2 + (sy - s["y"]) ** 2) < s["radius"] for s in INTERFERENCE)
        if best_signal > -75 and not in_interference:
            covered += 1
        if len(heatmap) < 50:
            heatmap.append({"x": round(sx, 1), "y": round(sy, 1), "signal_dbm": best_signal})
    coverage = round(covered / samples * 100, 1)

    ai_call_log.append({"round": round_num, "caller": "AI_ASSISTANT", "request_type": "simulate_signal", "latency_ms": random.randint(100, 500), "tokens": random.randint(300, 800)})
    _emit_traffic(round_num, "NORTH_SOUTH", "AI_ASSISTANT", "EXTERNAL:LLM", "llm_inference", 2048)
    _emit_event("AI_SIGNAL_SIM", round_num, "AI_ASSISTANT", "VERIFIER", "simulate_signal", f"coverage:{coverage}%")
    return {"status": "success", "result": "signal_simulated", "data": {"coverage_pct": coverage, "heatmap_sample": heatmap, "round": round_num}}
SkillRegistry.register("simulate_signal", simulate_signal)


# ============================================================
# VERIFIER / QA / DEPLOYER
# ============================================================
def verify_coverage(**kwargs):
    """验证覆盖达标"""
    round_num = kwargs.get("round", 0)
    aps = kwargs.get("ap_placements", ap_placements)
    if not aps:
        return {"status": "error", "result": "no_aps", "data": {}}
    coverage = simulate_coverage(ap_placements=aps, round=round_num)["data"]["coverage_pct"]
    passed = coverage >= TARGET_COVERAGE
    _emit_event("VERIFY_COVERAGE", round_num, "VERIFIER", "PLANNER", "verify_coverage",
                f"{coverage}% {'PASS' if passed else 'FAIL'} (target:{TARGET_COVERAGE}%)")
    return {"status": "success", "result": "pass" if passed else "fail",
            "data": {"coverage_pct": coverage, "target": TARGET_COVERAGE, "passed": passed, "round": round_num}}
SkillRegistry.register("verify_coverage", verify_coverage)


def final_inspection(**kwargs):
    """最终验收"""
    round_num = kwargs.get("round", 0)
    checks = {
        "coverage": random.random() > 0.1,
        "cost_within_budget": sum(e["total_cost"] for e in cost_estimates[-1:]) <= BUDGET if cost_estimates else True,
        "interference_avoided": True,
        "feasibility_ok": random.random() > 0.05,
    }
    all_pass = all(checks.values())
    _emit_event("FINAL_INSPECTION", round_num, "QA_ENGINEER", "PLANNER", "final_inspection",
                "ALL PASS" if all_pass else f"FAIL: {[k for k, v in checks.items() if not v]}")
    return {"status": "success", "result": "pass" if all_pass else "fail", "data": {"checks": checks, "all_pass": all_pass, "round": round_num}}
SkillRegistry.register("final_inspection", final_inspection)


def plan_deployment(**kwargs):
    """制定部署计划"""
    round_num = kwargs.get("round", 0)
    aps = kwargs.get("ap_placements", ap_placements)
    phases = [{"phase": i + 1, "ap_ids": [ap["id"] for ap in aps[i * 3:(i + 1) * 3]], "duration_h": random.randint(4, 12)}
              for i in range((len(aps) + 2) // 3)]
    _emit_event("DEPLOY_PLAN", round_num, "DEPLOYER", "PLANNER", "plan_deployment", f"{len(phases)} phases")
    return {"status": "success", "result": "plan_created", "data": {"phases": phases, "round": round_num}}
SkillRegistry.register("plan_deployment", plan_deployment)


def record_decision(**kwargs):
    """记录决策"""
    round_num = kwargs.get("round", 0)
    detail = kwargs.get("detail", "decision recorded")
    _emit_event("DECISION_RECORDED", round_num, "DOCUMENTER", "PLANNER", "record", detail)
    return {"status": "success", "result": "recorded", "data": {"detail": detail, "round": round_num}}
SkillRegistry.register("record_decision", record_decision)


# ============================================================
# get_panel_state — 供 GET /api/scenes/{name}/state 调用
# ============================================================
def get_panel_state(**kwargs):
    return {
        "campus": {"width": CAMPUS_W, "height": CAMPUS_H},
        "interference": INTERFERENCE,
        "ap_placements": ap_placements,
        "coverage_reports": coverage_reports,
        "cost_estimates": cost_estimates,
        "ai_call_log": ai_call_log,
        "feasibility_checks": feasibility_checks,
        "budget": {"total": BUDGET, "unit_ap_cost": AP_UNIT_COST, "target_coverage_pct": TARGET_COVERAGE},
        "latest_coverage": coverage_reports[-1] if coverage_reports else None,
        "latest_cost": cost_estimates[-1] if cost_estimates else None,
        "event_log": event_log[-20:],
        "traffic_log": traffic_log[-20:],
    }
SkillRegistry.register("get_panel_state", get_panel_state)
