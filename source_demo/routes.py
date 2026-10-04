"""新平台薄路由：范围和身份在这里固定，长任务交给 Worker。"""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
import asyncio
from pydantic import BaseModel, ConfigDict, Field

from . import agent_tools, code_qa, java_source, knowledge, legacy, manuals, platform, source_agent, workflow
from .api import actor, customer, editor, staff
from .jobs import enqueue, get_job
from .store import audit, db, many, now, one, pack

router = APIRouter()


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepositoryIn(Input):
    name: str = Field(min_length=1, max_length=150)
    root: str = Field(min_length=1, max_length=1000)
    classpath: list[str] = Field(default_factory=list, max_length=200)
    java_release: str = "17"


@router.get("/internal/repositories")
def repositories(user=Depends(staff)):
    result = []
    for row in java_source.repositories():
        try:
            platform.authorize(row["id"], user["name"])
            result.append(row)
        except PermissionError:
            continue
    return result


@router.post("/internal/repositories")
def register(body: RepositoryIn, user=Depends(editor)):
    result = java_source.register(**body.model_dump())
    audit(user["name"], "repository.register", result["id"], {"name": result["name"]})
    return result


class IndexIn(Input):
    ref: str = Field(default="HEAD", min_length=1, max_length=180)
    analyze_modules: bool = True


@router.post("/internal/repositories/{repository_id}/index", status_code=202)
def index_repository(repository_id: str, body: IndexIn, user=Depends(staff)):
    platform.authorize(repository_id, user["name"])
    return enqueue("repository_index", "internal", user["name"], {"repository_id": repository_id, "ref": body.ref, "analyze_modules": body.analyze_modules})


@router.get("/internal/source/{snapshot_id}/index")
def index_manifest(snapshot_id: str, user=Depends(staff)):
    platform.authorize_snapshot(snapshot_id, user["name"])
    return java_source.get_index(snapshot_id)


@router.get("/internal/source/{snapshot_id}/symbols")
def symbols(snapshot_id: str, module: str | None = None, user=Depends(staff)):
    platform.authorize_snapshot(snapshot_id, user["name"])
    return java_source.symbols(snapshot_id, module)


@router.get("/internal/source/{snapshot_id}/relations")
def relations(snapshot_id: str, user=Depends(staff)):
    platform.authorize_snapshot(snapshot_id, user["name"])
    return java_source.relations(snapshot_id)


class AgentIn(Input):
    snapshot_id: str
    module_id: str


class MaterialIn(Input):
    snapshot_id: str
    module_id: str
    kind: Literal["manual", "test", "legacy_test"]
    target_id: str = Field(min_length=1, max_length=150)


@router.post("/internal/source/material-bindings")
def bind_material(body: MaterialIn, user=Depends(editor)):
    platform.authorize_snapshot(body.snapshot_id, user["name"])
    return platform.bind_material(body.snapshot_id, body.module_id, body.kind, body.target_id, user["name"])


@router.post("/internal/source/analysis-runs", status_code=202)
def create_agent(body: AgentIn, user=Depends(staff)):
    platform.authorize_snapshot(body.snapshot_id, user["name"])
    return source_agent.create(body.snapshot_id, body.module_id, user["name"])


@router.get("/internal/source/analysis-runs")
def agents(user=Depends(staff)):
    with db() as con:
        rows = many(con, "SELECT id,snapshot_id,module,status,stage,job_id,bundle_hash,created_at FROM agent_runs ORDER BY created_at DESC LIMIT 100")
    result = []
    for row in rows:
        try:
            platform.authorize_snapshot(row["snapshot_id"], user["name"])
            result.append(row)
        except PermissionError:
            continue
    return result


@router.get("/internal/source/analysis-runs/{run_id}")
def agent_run(run_id: str, user=Depends(staff)):
    result = source_agent.get_run(run_id)
    platform.authorize_snapshot(result["snapshot_id"], user["name"])
    return result


class ReviewIn(Input):
    digest: str
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=2000)


@router.post("/internal/source/analysis-runs/{run_id}/review")
def review_agent(run_id: str, body: ReviewIn, user=Depends(editor)):
    platform.authorize_snapshot(source_agent.get_run(run_id)["snapshot_id"], user["name"])
    return source_agent.decide(run_id, body.digest, body.decision, user["name"], body.comment)


@router.post("/internal/source/analysis-runs/{run_id}/cancel")
def cancel_agent(run_id: str, user=Depends(staff)):
    platform.authorize_snapshot(source_agent.get_run(run_id)["snapshot_id"], user["name"])
    return source_agent.cancel(run_id)


class CodeAnswerIn(Input):
    snapshot_id: str
    question: str = Field(min_length=2, max_length=1000)


@router.post("/internal/code-answers", status_code=202)
def code_answer(body: CodeAnswerIn, idempotency_key: str = Header(default="", alias="Idempotency-Key"), user=Depends(staff)):
    platform.authorize_snapshot(body.snapshot_id, user["name"])
    return code_qa.create(body.snapshot_id, body.question, user["name"], idempotency_key)


@router.get("/internal/code-answers")
def code_answers(user=Depends(staff)):
    with db() as con:
        rows = many(con, "SELECT id,snapshot_id,question,job_id,created_at FROM code_answers WHERE actor=? ORDER BY created_at DESC LIMIT 100", (user["name"],))
    result = []
    for row in rows:
        try:
            platform.authorize_snapshot(row["snapshot_id"], user["name"])
            result.append(row)
        except PermissionError:
            continue
    return result


@router.get("/internal/code-answers/{answer_id}")
def code_answer_get(answer_id: str, user=Depends(staff)):
    result = code_qa.get(answer_id)
    platform.authorize_snapshot(result["snapshot_id"], user["name"])
    return result


class FeedbackIn(Input):
    kind: Literal["accurate", "incorrect", "insufficient_evidence", "version_mismatch"]
    comment: str = Field(default="", max_length=2000)


@router.post("/internal/code-answers/{answer_id}/feedback")
def code_feedback(answer_id: str, body: FeedbackIn, user=Depends(staff)):
    row = code_qa.get(answer_id)
    platform.authorize_snapshot(row["snapshot_id"], user["name"])
    if not row["result"]:
        raise RuntimeError("ANSWER_NOT_READY")
    with db() as con:
        con.execute("INSERT INTO answer_feedback(answer_id,actor,kind,comment,created_at) VALUES (?,?,?,?,?)", (answer_id, user["name"], body.kind, knowledge.redact(body.comment), now()))
    return {"ok": True}


@router.get("/internal/source/changes/compare")
def compare(base: str, target: str, user=Depends(staff)):
    platform.authorize_snapshot(base, user["name"]); platform.authorize_snapshot(target, user["name"])
    return java_source.impact(base, target)


@router.get("/internal/features")
def features(snapshot_id: str, user=Depends(staff)):
    platform.authorize_snapshot(snapshot_id, user["name"])
    return platform.feature_candidates(snapshot_id)


class FeatureIn(Input):
    run_id: str
    feature_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=150)
    claim_ids: list[str] = Field(min_length=1, max_length=12)


@router.post("/internal/features/confirm")
def confirm_feature(body: FeatureIn, user=Depends(editor)):
    platform.authorize_snapshot(source_agent.get_run(body.run_id)["snapshot_id"], user["name"])
    return platform.approve_feature(body.run_id, body.feature_id, body.title, body.claim_ids, user["name"])


class DocumentIn(Input):
    release: str
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=1000000)


class FeatureManualIn(Input):
    snapshot_id: str
    feature_id: str
    release: str
    page_id: str = Field(min_length=1, max_length=80)


@router.post("/internal/manuals/from-feature")
def manual_from_feature(body: FeatureManualIn, user=Depends(staff)):
    platform.authorize_snapshot(body.snapshot_id, user["name"])
    with db() as con:
        feature = one(con, "SELECT * FROM features WHERE id=? AND snapshot_id=?", (body.feature_id, body.snapshot_id))
    if not feature:
        raise LookupError("FEATURE_NOT_CONFIRMED")
    return knowledge.draft_from_evidence(body.page_id, body.release, feature["title"], body.snapshot_id, json.loads(feature["evidence_ids"])[:8])


@router.post("/internal/manuals/import")
def import_document(body: DocumentIn, user=Depends(staff)):
    return manuals.import_document(**body.model_dump())


@router.get("/internal/manuals/{revision_id}/blocks")
def blocks(revision_id: int, user=Depends(staff)):
    with db() as con:
        row = one(con, "SELECT * FROM manual_revisions WHERE id=?", (revision_id,))
    if not row:
        raise LookupError("REVISION_NOT_FOUND")
    return {"revision": row, "blocks": manuals.blocks(row["content"])}


class PatchIn(Input):
    baseline_id: int
    baseline_hash: str
    operations: list[dict] = Field(min_length=1, max_length=40)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)


@router.post("/internal/manuals/patches")
def create_patch(body: PatchIn, user=Depends(staff)):
    return manuals.create_patch(**body.model_dump())


@router.post("/internal/manuals/patches/{patch_id}/apply")
def apply_patch(patch_id: str, user=Depends(staff)):
    return manuals.apply_patch(patch_id)


@router.post("/internal/manuals/{revision_id}/review")
def review_manual(revision_id: int, body: ReviewIn, user=Depends(editor)):
    return manuals.review(revision_id, body.digest, body.decision, user["name"], body.comment)


@router.get("/internal/knowledge/{release}/history")
def knowledge_history(release: str, user=Depends(staff)):
    return manuals.history(release)


class RollbackIn(Input):
    snapshot_id: int
    expected_head: int


@router.post("/internal/knowledge/{release}/rollback")
def rollback(release: str, body: RollbackIn, user=Depends(editor)):
    return manuals.rollback(release, body.snapshot_id, body.expected_head, user["name"])


@router.get("/customer/help/search")
def help_search(q: str = "", user=Depends(customer)):
    return knowledge.search(user["tenant"], q)


@router.get("/customer/help/{page_id}")
def help_page(page_id: str, user=Depends(customer)):
    context = knowledge.projection(user["tenant"])
    snapshot, pages = context["snapshot_id"], context["pages"]
    row = next((p for p in pages if p["page_id"] == page_id), None)
    if not row:
        raise LookupError("HELP_NOT_FOUND")
    return {**row, "knowledge_snapshot_id": snapshot, "release": context["release"]}


@router.post("/customer/questions/{question_id}/feedback")
def customer_feedback(question_id: str, body: FeedbackIn, user=Depends(customer)):
    with db() as con:
        updated = con.execute("UPDATE questions SET feedback=? WHERE id=? AND tenant=? AND actor=?", (pack({"kind": body.kind, "comment": knowledge.redact(body.comment)}), question_id, user["tenant"], user["name"]))
        if not updated.rowcount:
            raise LookupError("QUESTION_NOT_FOUND")
    return {"ok": True}


@router.get("/internal/operations")
def operations(user=Depends(staff)):
    return platform.operations()


class OperationIn(Input):
    topic: str = Field(min_length=1, max_length=150)
    kind: str
    owner: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=2000)


@router.post("/internal/operations/tasks")
def create_operation(body: OperationIn, user=Depends(staff)):
    return platform.operation_task(**body.model_dump())


@router.post("/internal/operations/tasks/{task_id}/close")
def close_operation(task_id: str, user=Depends(staff)):
    with db() as con:
        con.execute("UPDATE operation_tasks SET status='closed' WHERE id=?", (task_id,))
    return {"ok": True}


@router.get("/internal/deployments")
def deployments(user=Depends(staff)):
    return [platform.deployment(t) for t in knowledge.RELEASES]


class DeploymentIn(Input):
    tenant: str
    release: str
    snapshot_id: str | None = None
    flags: dict[str, bool]
    expected_revision: int


@router.post("/internal/deployments")
def deploy(body: DeploymentIn, user=Depends(editor)):
    return platform.deploy(**body.model_dump(), actor=user["name"])


class AccessIn(Input):
    actor: Literal["analyst", "editor"]
    allowed: bool


@router.put("/internal/repositories/{repository_id}/access")
def access(repository_id: str, body: AccessIn, user=Depends(editor)):
    with db() as con:
        con.execute("INSERT INTO repository_acl VALUES (?,?,?) ON CONFLICT(repository_id,actor) DO UPDATE SET allowed=excluded.allowed", (repository_id, body.actor, body.allowed))
        audit(user["name"], "repository.access", repository_id, body.model_dump(), con)
    return {"ok": True}


class VisibilityIn(Input):
    tenant: str
    page_id: str
    allowed: bool


@router.put("/internal/manuals/visibility")
def visibility(body: VisibilityIn, user=Depends(editor)):
    if body.tenant not in knowledge.RELEASES:
        raise ValueError("TENANT_INVALID")
    with db() as con:
        con.execute("INSERT INTO manual_visibility VALUES (?,?,?) ON CONFLICT(tenant,page_id) DO UPDATE SET allowed=excluded.allowed", (body.tenant, body.page_id, body.allowed))
    return {"ok": True}


@router.get("/internal/workflow-versions")
def workflow_versions(user=Depends(staff)):
    with db() as con:
        rows = many(con, "SELECT * FROM workflow_versions ORDER BY created_at DESC")
    return [{**r, "spec": json.loads(r["spec"]), "validation": json.loads(r["validation"]) if r["validation"] else None} for r in rows]


@router.post("/internal/workflow-versions")
def create_workflow(spec: dict, user=Depends(staff)):
    return workflow.create_version(spec)


@router.post("/internal/workflow-versions/{version}/validate")
def validate_workflow(version: str, user=Depends(staff)):
    return workflow.validate_version(version)


class DigestIn(Input):
    digest: str


@router.post("/internal/workflow-versions/{version}/{action}")
def workflow_transition(version: str, action: str, body: DigestIn, user=Depends(editor)):
    result = workflow.transition(version, body.digest, action, user["name"])
    audit(user["name"], "workflow." + action, version, {"digest": body.digest})
    return result


@router.post("/customer/executions/{execution_id}/cancel")
def cancel_execution(execution_id: str, user=Depends(customer)):
    return workflow.cancel_execution(user["tenant"], user["name"], execution_id)


@router.get("/internal/legacy/status")
def legacy_status(user=Depends(staff)):
    return legacy.status()


@router.get("/internal/legacy/automation/attempts/{attempt_id}/report/{path:path}")
def legacy_report(attempt_id: int, path: str, user=Depends(staff)):
    content, content_type = legacy.report(attempt_id, path)
    return Response(content, media_type=content_type, headers={"Content-Disposition": "attachment", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox; default-src 'none'"})


@router.get("/internal/legacy/automation/batches")
def batches(project_id: int | None = None, user=Depends(staff)):
    return legacy.list_batches(project_id)


@router.get("/internal/legacy/automation/batches/{batch_id}")
def batch(batch_id: int, user=Depends(staff)):
    return legacy.batch(batch_id)


@router.get("/internal/legacy/projects/{project_id}/cases")
def legacy_cases(project_id: int, user=Depends(staff)):
    return legacy.list_cases(project_id)


@router.get("/internal/legacy/runs/{run_id}")
def legacy_run(run_id: int, user=Depends(staff)):
    return legacy.request("GET", f"/api/runs/{run_id}")


@router.post("/internal/legacy/runs/{run_id}/import")
def import_run(run_id: int, context: dict, user=Depends(staff)):
    return legacy.sync_run(run_id, context)


class LegacyRunIn(Input):
    case_ids: list[int] = Field(min_length=1, max_length=100)
    environment_id: int | None = None


@router.post("/internal/legacy/projects/{project_id}/runs", status_code=202)
def request_run(project_id: int, body: LegacyRunIn, idempotency_key: str = Header(default="", alias="Idempotency-Key"), user=Depends(editor)):
    return legacy.create_run(project_id, body.case_ids, body.environment_id, idempotency_key, user["name"])


@router.get("/internal/legacy/results/{result_id}/{kind}")
def artifact(result_id: int, kind: str, user=Depends(staff)):
    body, content_type, digest = legacy.artifact(result_id, kind)
    return Response(body, media_type=content_type, headers={"X-Content-SHA256": digest})


@router.post("/internal/legacy/automation/revisions/{revision_id}/approve")
def legacy_script_approve(revision_id: int, body: DigestIn, user=Depends(editor)):
    return legacy.approve_script(revision_id, body.digest, user["name"])


class AutomationActionIn(Input):
    target_id: int = Field(ge=1)
    action: Literal["start", "generate", "reexplore", "run", "retry"]
    digest: str = ""


@router.post("/internal/legacy/automation/actions", status_code=202)
def automation_action(body: AutomationActionIn, idempotency_key: str = Header(default="", alias="Idempotency-Key"), user=Depends(editor)):
    return legacy.create_automation_action(body.target_id, body.action, body.digest, idempotency_key, user["name"])


@router.get("/internal/legacy/imported-runs")
def imported_runs(user=Depends(staff)):
    with db() as con:
        rows = many(con, "SELECT * FROM legacy_runs ORDER BY created_at DESC LIMIT 100")
    return [{**r, "context": json.loads(r["context"]), "result": json.loads(r["result"]) if r["result"] else None} for r in rows]


@router.post("/internal/legacy/requests/{request_id}/reconcile")
def reconcile_request(request_id: str, user=Depends(staff)):
    return legacy.reconcile(request_id)


@router.post("/integrations/git/events", status_code=202)
async def git_events(request: Request, x_hub_signature_256: str = Header(default=""), x_event_id: str = Header(default="")):
    body = await request.body()
    if len(body) > 10000:
        raise ValueError("WEBHOOK_TOO_LARGE")
    return platform.git_event(body, x_hub_signature_256, x_event_id)


@router.get("/internal/metrics")
def metrics(user=Depends(staff)):
    return platform.metrics()


@router.get("/internal/audit")
def audits(user=Depends(editor)):
    with db() as con:
        rows = many(con, "SELECT * FROM audit_log ORDER BY id DESC LIMIT 200")
    return [{**r, "details": json.loads(r["details"])} for r in rows]


@router.get("/internal/jobs")
def jobs(user=Depends(staff)):
    with db() as con:
        return many(con, "SELECT id,kind,status,error,created_at FROM jobs WHERE scope='internal' AND actor=? ORDER BY created_at DESC LIMIT 100", (user["name"],))


@router.get("/internal/source/analysis-runs/{run_id}/events")
def agent_events(run_id: str, request: Request, user=Depends(staff)):
    row = source_agent.get_run(run_id)
    platform.authorize_snapshot(row["snapshot_id"], user["name"])
    async def events():
        while True:
            if await request.is_disconnected():
                break
            try:
                current = actor(request)
                if current["role"] not in ("staff", "editor"):
                    raise HTTPException(403, "员工权限不足")
                platform.authorize_snapshot(row["snapshot_id"], current["name"])
                result = source_agent.get_run(run_id)
                data = {"status": result["status"], "stage": result["stage"], "event_count": len(result["events"])}
                yield "event: progress\ndata: " + pack(data) + "\n\n"
                if result["status"] not in ("QUEUED", "RUNNING"):
                    break
            except (PermissionError, HTTPException):
                yield 'event: revoked\ndata: {"error":"access_revoked"}\n\n'
                break
            await asyncio.sleep(1)
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
