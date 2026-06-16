"""
tech_campus_scale — 外部AI助手技能模块
供 scale_spawner.py 和 panel.html 使用
"""
import random
import time

# ============================================================
# 状态
# ============================================================
llm_call_log = []     # [{round, caller, skill, tokens, latency_ms, response_status}]
event_log = []
traffic_log = []


def _emit(r, t, s, d, a, kb):
    traffic_log.append({"round": r, "type": t, "source": s, "target": d, "action": a, "bytes": kb * 1024})


def _event(et, r, s, d, a, detail=""):
    event_log.append({"event_type": et, "round": r, "source": s, "target": d, "action": a, "detail": detail})


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
# AI_ASSISTANT 技能 — 被其他 Agent 调用
# ============================================================

def llm_code_generate(**kwargs):
    """
    AI代码生成。developer→AI_ASSISTANT→EXTERNAL:LLM (南北向)
    参数: caller(str), language(str), prompt(str), round(int)
    """
    caller = kwargs.get("caller", "unknown")
    language = kwargs.get("language", "python")
    prompt = kwargs.get("prompt", "")
    round_num = kwargs.get("round", 0)
    tokens = random.randint(100, 2000)
    latency = random.randint(150, 600)
    response_status = 200 if random.random() > 0.05 else 429

    _emit(round_num, "EAST_WEST", caller, "AI_ASSISTANT", "code_request", len(prompt) // 4 or 1)
    if response_status == 200:
        _emit(round_num, "NORTH_SOUTH", "AI_ASSISTANT", "EXTERNAL:LLM", "llm_inference", tokens * 4)
        _emit(round_num, "EAST_WEST", "AI_ASSISTANT", caller, "code_response", tokens // 10)
    llm_call_log.append({"round": round_num, "caller": caller, "skill": "llm_code_generate",
                          "tokens": tokens, "latency_ms": latency, "response_status": response_status})
    _event("LLM_CODE", round_num, caller, "AI_ASSISTANT", "code_generate",
           f"{language} {tokens}t {latency}ms {'OK' if response_status == 200 else 'RATE_LIMITED'}")

    return {
        "status": "success" if response_status == 200 else "error",
        "result": "code_generated" if response_status == 200 else "rate_limited",
        "data": {"caller": caller, "language": language, "tokens": tokens, "latency_ms": latency, "round": round_num}
    }
SkillRegistry.register("llm_code_generate", llm_code_generate)


def llm_document_assist(**kwargs):
    """AI文档辅助。PM/DOC_WRITER→AI_ASSISTANT→EXTERNAL:LLM"""
    caller = kwargs.get("caller", "unknown")
    doc_type = kwargs.get("doc_type", "api_doc")
    round_num = kwargs.get("round", 0)
    tokens = random.randint(200, 1500)
    latency = random.randint(100, 400)

    _emit(round_num, "EAST_WEST", caller, "AI_ASSISTANT", "doc_request", 2)
    _emit(round_num, "NORTH_SOUTH", "AI_ASSISTANT", "EXTERNAL:LLM", "llm_inference", tokens * 4)
    _emit(round_num, "EAST_WEST", "AI_ASSISTANT", caller, "doc_response", tokens // 8)
    llm_call_log.append({"round": round_num, "caller": caller, "skill": "llm_document_assist",
                          "tokens": tokens, "latency_ms": latency, "response_status": 200})
    _event("LLM_DOC", round_num, caller, "AI_ASSISTANT", "document_assist",
           f"{doc_type} {tokens}t {latency}ms")

    return {"status": "success", "result": "document_assisted",
            "data": {"caller": caller, "doc_type": doc_type, "tokens": tokens, "latency_ms": latency, "round": round_num}}
SkillRegistry.register("llm_document_assist", llm_document_assist)


def llm_model_inference(**kwargs):
    """AI模型推理。DEV_AI→AI_ASSISTANT→EXTERNAL:LLM"""
    caller = kwargs.get("caller", "unknown")
    model_name = kwargs.get("model_name", "default")
    round_num = kwargs.get("round", 0)
    tokens = random.randint(500, 5000)
    latency = random.randint(300, 2000)
    response_status = 200 if random.random() > 0.08 else 500

    _emit(round_num, "EAST_WEST", caller, "AI_ASSISTANT", "inference_request", 8)
    if response_status == 200:
        _emit(round_num, "NORTH_SOUTH", "AI_ASSISTANT", "EXTERNAL:LLM", "llm_inference", tokens * 4)
        _emit(round_num, "EAST_WEST", "AI_ASSISTANT", caller, "inference_response", tokens // 50)
    llm_call_log.append({"round": round_num, "caller": caller, "skill": "llm_model_inference",
                          "tokens": tokens, "latency_ms": latency, "response_status": response_status})
    _event("LLM_INFERENCE", round_num, caller, "AI_ASSISTANT", "model_inference",
           f"{model_name} {tokens}t {latency}ms {'OK' if response_status == 200 else 'FAIL'}")

    return {"status": "success" if response_status == 200 else "error",
            "result": "inference_complete" if response_status == 200 else "service_unavailable",
            "data": {"caller": caller, "model_name": model_name, "tokens": tokens, "latency_ms": latency, "round": round_num}}
SkillRegistry.register("llm_model_inference", llm_model_inference)


def llm_eda_simulation(**kwargs):
    """EDA云仿真。DEV_IC→AI_ASSISTANT→EXTERNAL:EDA"""
    caller = kwargs.get("caller", "unknown")
    design_name = kwargs.get("design_name", "default")
    round_num = kwargs.get("round", 0)
    latency = random.randint(2000, 8000)
    size_mb = random.randint(100, 2000)

    _emit(round_num, "EAST_WEST", caller, "AI_ASSISTANT", "eda_request", size_mb // 10)
    _emit(round_num, "NORTH_SOUTH", "AI_ASSISTANT", "EXTERNAL:EDA_CLOUD", "eda_simulation", size_mb * 1024)
    _emit(round_num, "EAST_WEST", "AI_ASSISTANT", caller, "eda_response", 64)
    llm_call_log.append({"round": round_num, "caller": caller, "skill": "llm_eda_simulation",
                          "tokens": 0, "latency_ms": latency, "response_status": 200, "size_mb": size_mb})
    _event("EDA_SIM", round_num, caller, "AI_ASSISTANT", "eda_simulation",
           f"{design_name} {size_mb}MB {latency}ms")

    return {"status": "success", "result": "simulation_complete",
            "data": {"caller": caller, "design_name": design_name, "size_mb": size_mb, "latency_ms": latency, "round": round_num}}
SkillRegistry.register("llm_eda_simulation", llm_eda_simulation)


# ============================================================
# get_panel_state
# ============================================================
def get_panel_state(**kwargs):
    return {
        "llm_call_log": llm_call_log,
        "event_log": event_log[-30:],
        "traffic_log": traffic_log[-30:],
        "summary": {
            "total_llm_calls": len(llm_call_log),
            "total_tokens": sum(c.get("tokens", 0) for c in llm_call_log),
            "avg_latency_ms": round(sum(c.get("latency_ms", 0) for c in llm_call_log) / max(len(llm_call_log), 1)),
            "success_rate": round(sum(1 for c in llm_call_log if c.get("response_status") == 200) / max(len(llm_call_log), 1) * 100, 1),
        },
    }
SkillRegistry.register("get_panel_state", get_panel_state)
