import random
import time

# ============================================================
# 模块级状态 — 三类流量追踪
# ============================================================
traffic_log = []       # 流量事件 [{round, type, source, target, action, bytes}]
event_log = []         # 业务事件 [{event_type, round, source, target, action, detail}]

git_commits = []
model_submissions = []
design_submissions = []
documents = []
test_reports = []
external_api_calls = []
ci_pipelines = []


def _emit_traffic(round_num, traffic_type, source, target, action, bytes_est=0):
    event = {
        "round": round_num,
        "type": traffic_type,
        "source": source,
        "target": target,
        "action": action,
        "bytes": bytes_est,
    }
    traffic_log.append(event)
    return event


def _emit_event(event_type, round_num, source, target, action, detail=""):
    e = {"event_type": event_type, "round": round_num, "source": source, "target": target, "action": action, "detail": detail}
    event_log.append(e)
    return e


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
# 开发侧技能
# ============================================================

def submit_code(**kwargs):
    """
    提交代码到Git仓库，触发CI/CD。
    参数: developer(str), repo(str), files_changed(int), round(int)
    """
    developer = kwargs.get("developer", "unknown")
    repo = kwargs.get("repo", "main")
    files = kwargs.get("files_changed", random.randint(1, 5))
    current_round = kwargs.get("round", 0)

    commit_id = f"commit_{len(git_commits)+1}_{int(time.time()%100000)}"
    git_commits.append({"developer": developer, "repo": repo, "commit_id": commit_id, "files": files, "round": current_round})

    _emit_traffic(current_round, "EAST_WEST", developer, "REPO_ADMIN", "git_push", files * 2048)

    pipeline_id = f"ci_{len(ci_pipelines)+1}"
    ci_pipelines.append({"pipeline_id": pipeline_id, "triggered_by": commit_id, "status": "running", "round": current_round})
    _emit_traffic(current_round, "INTERNAL", "REPO_ADMIN", "CI_RUNNER", "trigger_pipeline", 512)

    _emit_event("CODE_SUBMITTED", current_round, developer, "REPO_ADMIN", "push", f"{commit_id} ({files} files)")

    return {
        "status": "success", "result": "code_submitted",
        "data": {"commit_id": commit_id, "files": files, "pipeline_id": pipeline_id, "round": current_round}
    }
SkillRegistry.register("submit_code", submit_code)


# ============================================================
# AI/IC 侧技能
# ============================================================

def submit_model(**kwargs):
    """
    提交训练好的模型文件。
    参数: developer(str), model_name(str), size_mb(float), round(int)
    """
    developer = kwargs.get("developer", "unknown")
    model_name = kwargs.get("model_name", "model_v1")
    size_mb = kwargs.get("size_mb", random.randint(50, 500))
    current_round = kwargs.get("round", 0)

    model_id = f"model_{len(model_submissions)+1}_{int(time.time()%100000)}"
    model_submissions.append({"developer": developer, "model_name": model_name, "model_id": model_id, "size_mb": size_mb, "round": current_round})

    _emit_traffic(current_round, "INTERNAL", developer, "REPO_ADMIN", "model_push", int(size_mb * 1_048_576))
    _emit_event("MODEL_SUBMITTED", current_round, developer, "REPO_ADMIN", "push", f"{model_id} ({size_mb}MB)")

    return {"status": "success", "result": "model_submitted", "data": {"model_id": model_id, "size_mb": size_mb, "round": current_round}}
SkillRegistry.register("submit_model", submit_model)


def submit_design(**kwargs):
    """
    提交芯片设计文件。
    参数: developer(str), design_name(str), size_mb(float), round(int)
    """
    developer = kwargs.get("developer", "unknown")
    design_name = kwargs.get("design_name", "design_v1")
    size_mb = kwargs.get("size_mb", random.randint(100, 2000))
    current_round = kwargs.get("round", 0)

    design_id = f"design_{len(design_submissions)+1}_{int(time.time()%100000)}"
    design_submissions.append({"developer": developer, "design_name": design_name, "design_id": design_id, "size_mb": size_mb, "round": current_round})

    _emit_traffic(current_round, "INTERNAL", developer, "REPO_ADMIN", "design_push", int(size_mb * 1_048_576))
    _emit_event("DESIGN_SUBMITTED", current_round, developer, "REPO_ADMIN", "push", f"{design_id} ({size_mb}MB)")

    return {"status": "success", "result": "design_submitted", "data": {"design_id": design_id, "size_mb": size_mb, "round": current_round}}
SkillRegistry.register("submit_design", submit_design)


def request_external_api(**kwargs):
    """
    请求外部API资源（LLM推理/EDA云仿真等）。
    参数: requester(str), api_name(str), payload_size(float,KB), round(int)
    流量: requester→external (南北向)
    """
    requester = kwargs.get("requester", "unknown")
    api_name = kwargs.get("api_name", "external_service")
    payload_size = kwargs.get("payload_size", random.randint(1, 100))
    current_round = kwargs.get("round", 0)

    # 每轮限制10次外部调用
    current_count = len([c for c in external_api_calls if c["round"] == current_round])
    if current_count >= 10:
        _emit_event("API_BLOCKED", current_round, requester, "EXTERNAL", "rate_limited", api_name)
        return {"status": "error", "result": "rate_limited", "data": {"api_name": api_name, "reason": "超过限流阈值"}}

    call_id = f"api_{len(external_api_calls)+1}_{int(time.time()%100000)}"
    latency_ms = random.randint(50, 500)

    _emit_traffic(current_round, "NORTH_SOUTH", requester, f"EXTERNAL:{api_name}", "api_request", int(payload_size * 1024))

    resp_size = payload_size * random.uniform(0.5, 2.0)
    _emit_traffic(current_round, "NORTH_SOUTH", f"EXTERNAL:{api_name}", requester, "api_response", int(resp_size * 1024))

    external_api_calls.append({"requester": requester, "api_name": api_name, "call_id": call_id, "payload_kb": payload_size, "round": current_round, "latency_ms": latency_ms})
    _emit_event("EXTERNAL_API_CALL", current_round, requester, "EXTERNAL", api_name, f"{call_id} ({payload_size}KB, {latency_ms}ms)")

    return {
        "status": "success", "result": "api_call_completed",
        "data": {"call_id": call_id, "api_name": api_name, "payload_kb": payload_size, "response_kb": round(resp_size, 1), "latency_ms": latency_ms, "round": current_round}
    }
SkillRegistry.register("request_external_api", request_external_api)


# ============================================================
# 架构师/PM/文档侧技能
# ============================================================

def review_document(**kwargs):
    """
    审查设计文档并通知相关方。
    参数: reviewer(str), doc_id(str), target_dev(str), round(int)
    """
    reviewer = kwargs.get("reviewer", "ARCHITECT")
    doc_id = kwargs.get("doc_id", f"doc_{len(documents)+1}")
    target_dev = kwargs.get("target_dev", "")
    current_round = kwargs.get("round", 0)

    decision = random.choice(["approved", "revision_required"])
    _emit_traffic(current_round, "EAST_WEST", reviewer, target_dev or "DEV_TEAM", "review_feedback", 4096)

    if decision == "revision_required" and target_dev:
        SkillRegistry.execute("notify_team", sender=reviewer, target=target_dev,
                              message=f"文档 {doc_id} 需修改", round=current_round)

    _emit_event("DOC_REVIEWED", current_round, reviewer, target_dev or "DEV_TEAM", decision, doc_id)

    return {"status": "success", "result": decision, "data": {"doc_id": doc_id, "decision": decision, "target_dev": target_dev, "round": current_round}}
SkillRegistry.register("review_document", review_document)


def write_document(**kwargs):
    """
    编写/协作编辑文档。
    参数: author(str), doc_type(str), title(str), round(int)
    """
    author = kwargs.get("author", "unknown")
    doc_type = kwargs.get("doc_type", "requirement")
    title = kwargs.get("title", "untitled")
    current_round = kwargs.get("round", 0)

    doc_id = f"doc_{len(documents)+1}_{int(time.time()%100000)}"
    size_kb = random.randint(10, 200)
    documents.append({"author": author, "type": doc_type, "doc_id": doc_id, "title": title, "size_kb": size_kb, "status": "draft", "round": current_round})

    _emit_traffic(current_round, "INTERNAL", author, "REPO_ADMIN", "doc_push", size_kb * 1024)
    _emit_event("DOC_CREATED", current_round, author, "REPO_ADMIN", doc_type, f"{doc_id}: {title}")

    return {"status": "success", "result": "document_created", "data": {"doc_id": doc_id, "title": title, "size_kb": size_kb, "round": current_round}}
SkillRegistry.register("write_document", write_document)


# ============================================================
# 通知/测试/CI 侧技能
# ============================================================

def notify_team(**kwargs):
    """
    发送通知。
    参数: sender(str), target(str), message(str), round(int)
    """
    sender = kwargs.get("sender", "unknown")
    target = kwargs.get("target", "unknown")
    message = kwargs.get("message", "")
    current_round = kwargs.get("round", 0)

    _emit_traffic(current_round, "EAST_WEST", sender, target, "notify", len(message.encode()) if message else 256)
    _emit_event("NOTIFY", current_round, sender, target, "notify", message[:80])

    return {"status": "success", "result": "notified", "data": {"sender": sender, "target": target, "round": current_round}}
SkillRegistry.register("notify_team", notify_team)


def run_test(**kwargs):
    """
    执行自动化测试。
    参数: tester(str), target(str), test_suite(str), round(int)
    """
    tester = kwargs.get("tester", "QA")
    target = kwargs.get("target", "DEV_FE")
    test_suite = kwargs.get("test_suite", "regression")
    current_round = kwargs.get("round", 0)

    test_id = f"test_{len(test_reports)+1}_{int(time.time()%100000)}"
    passed = random.random() > 0.3
    test_reports.append({"tester": tester, "target": target, "test_id": test_id, "passed": passed, "round": current_round})

    _emit_traffic(current_round, "EAST_WEST", tester, target, "test_report", 2048)
    _emit_event("TEST_COMPLETED", current_round, tester, target, "passed" if passed else "failed", test_id)

    if not passed:
        SkillRegistry.execute("notify_team", sender=tester, target=target, message=f"测试失败: {test_suite}", round=current_round)

    return {"status": "success", "result": "passed" if passed else "failed", "data": {"test_id": test_id, "passed": passed, "target": target, "round": current_round}}
SkillRegistry.register("run_test", run_test)


def handle_push(**kwargs):
    """
    处理推送，触发CI/CD流水线。
    参数: pusher(str), push_type(str: code|model|design|doc), artifact_id(str), round(int)
    """
    pusher = kwargs.get("pusher", "unknown")
    push_type = kwargs.get("push_type", "code")
    artifact_id = kwargs.get("artifact_id", "unknown")
    current_round = kwargs.get("round", 0)

    pipeline_id = f"ci_{len(ci_pipelines)+1}_{int(time.time()%100000)}"
    ci_pipelines.append({"pipeline_id": pipeline_id, "type": push_type, "triggered_by": pusher, "status": "running", "round": current_round})

    _emit_traffic(current_round, "INTERNAL", "REPO_ADMIN", "CI_RUNNER", "trigger_build", 4096)
    _emit_event("PUSH_HANDLED", current_round, "REPO_ADMIN", pusher, push_type, f"{artifact_id}->{pipeline_id}")

    build_result = random.choice(["success", "success", "success", "failed"])
    ci_pipelines[-1]["status"] = build_result
    _emit_traffic(current_round, "INTERNAL", "CI_RUNNER", "REPO_ADMIN", "build_result", 1024)

    if build_result == "success" and push_type in ("code", "model"):
        img_size_mb = random.randint(50, 500)
        _emit_traffic(current_round, "INTERNAL", "REPO_ADMIN", "REGISTRY", "image_push", int(img_size_mb * 1_048_576))
        _emit_event("IMAGE_PUSHED", current_round, "REPO_ADMIN", "REGISTRY", "push", f"{pipeline_id} ({img_size_mb}MB)")

    return {
        "status": "success", "result": build_result,
        "data": {"pipeline_id": pipeline_id, "push_type": push_type, "build_result": build_result, "round": current_round}
    }
SkillRegistry.register("handle_push", handle_push)


def trigger_ci_cd(**kwargs):
    """
    手动触发CI/CD流水线。
    参数: trigger_by(str), target_artifact(str), round(int)
    """
    trigger_by = kwargs.get("trigger_by", "unknown")
    target_artifact = kwargs.get("target_artifact", "latest")
    current_round = kwargs.get("round", 0)

    pipeline_id = f"ci_{len(ci_pipelines)+1}_{int(time.time()%100000)}"
    ci_pipelines.append({"pipeline_id": pipeline_id, "type": "manual", "triggered_by": trigger_by, "status": "running", "round": current_round})

    _emit_traffic(current_round, "INTERNAL", trigger_by, "CI_RUNNER", "manual_trigger", 512)
    _emit_event("CI_TRIGGERED", current_round, trigger_by, "CI_RUNNER", "trigger", pipeline_id)

    return {"status": "success", "result": "ci_triggered", "data": {"pipeline_id": pipeline_id, "round": current_round}}
SkillRegistry.register("trigger_ci_cd", trigger_ci_cd)
