from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict

from . import knowledge, legacy, source, testing, workflow, config, platform
from .jobs import enqueue, get_job
from .store import db, init, now, one

ACTORS = {
    "analyst": {"role": "staff"},
    "editor": {"role": "editor"},
    "customer-a": {"role": "customer", "tenant": "tenant-a"},
    "customer-b": {"role": "customer", "tenant": "tenant-b"},
}
FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "dist"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init()
    from .seed import seed
    seed()
    yield


app = FastAPI(title="源码知识与流程 Demo", lifespan=lifespan)
app.mount("/internal-static", StaticFiles(directory=FRONTEND / "internal", check_dir=False))
app.mount("/customer-static", StaticFiles(directory=FRONTEND / "customer", check_dir=False))


@app.exception_handler(ValueError)
async def value_error(_request: Request, exc: ValueError):
    return JSONResponse({"error": str(exc)}, status_code=400)


@app.exception_handler(RuntimeError)
async def runtime_error(_request: Request, exc: RuntimeError):
    return JSONResponse({"error": str(exc)}, status_code=409)


@app.exception_handler(LookupError)
async def lookup_error(_request: Request, exc: LookupError):
    return JSONResponse({"error": str(exc)}, status_code=404)


@app.exception_handler(httpx.HTTPError)
async def upstream_error(_request: Request, _exc: httpx.HTTPError):
    return JSONResponse({"error": "TestPilot 或只读目标服务暂不可用"}, status_code=503)


@app.exception_handler(PermissionError)
async def permission_error(_request: Request, exc: PermissionError):
    return JSONResponse({"error": str(exc)}, status_code=403)


@app.middleware("http")
async def same_origin(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        allowed = {f"{request.url.scheme}://{request.headers.get('host')}"}
        # Vite 开发服务器仅监听本机；代理会保留浏览器的 Origin。
        if request.client and request.client.host in ("127.0.0.1", "::1"):
            allowed.update(("http://127.0.0.1:5173", "http://127.0.0.1:5174"))
        if origin and origin not in allowed:
            return JSONResponse({"error": "跨站请求被拒绝"}, status_code=403)
    return await call_next(request)


def actor(request: Request) -> dict:
    token = request.cookies.get("demo_session")
    if not token:
        raise HTTPException(401, "请先登录")
    with db() as con:
        session = one(con, "SELECT actor FROM sessions WHERE token=? AND expires>?", (token, now()))
    if not session or session["actor"] not in ACTORS:
        raise HTTPException(401, "会话已失效")
    return {"name": session["actor"], **ACTORS[session["actor"]]}


def staff(user: dict = Depends(actor)) -> dict:
    if user["role"] not in ("staff", "editor"):
        raise HTTPException(403, "员工权限不足")
    return user


def editor(user: dict = Depends(actor)) -> dict:
    if user["role"] != "editor":
        raise HTTPException(403, "审批权限不足")
    return user


def customer(user: dict = Depends(actor)) -> dict:
    if user["role"] != "customer":
        raise HTTPException(403, "客户权限不足")
    return user


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginIn(Input):
    actor: str
    password: str


@app.post("/session")
def login(body: LoginIn, response: Response):
    if body.actor not in ACTORS or not secrets.compare_digest(body.password.encode(), os.getenv("DEMO_PASSWORD", "demo-only").encode()):
        raise HTTPException(401, "账号或口令错误")
    token = secrets.token_urlsafe(32)
    with db() as con:
        con.execute("INSERT INTO sessions VALUES (?,?,?)", (token, body.actor, now() + 3600))
    response.set_cookie("demo_session", token, httponly=True, samesite="strict", max_age=3600)
    return {"actor": body.actor, **ACTORS[body.actor]}


@app.delete("/session")
def logout(request: Request, response: Response):
    token = request.cookies.get("demo_session")
    with db() as con:
        con.execute("DELETE FROM sessions WHERE token=?", (token,))
    response.delete_cookie("demo_session")
    return {"ok": True}


@app.get("/session")
def current(user: dict = Depends(actor)):
    return user


@app.get("/health")
def health():
    from .java_source import JAR
    return {"status": "ok", "mode": "local", "model": config.model_status(), "source_parser": "eclipse-jdt" if JAR.is_file() else "not_ready"}


@app.get("/internal-ui")
def internal_ui():
    html = FRONTEND / "internal" / "internal.html"
    if not html.is_file():
        raise HTTPException(503, "请先在 frontend 执行 npm ci && npm run build")
    return FileResponse(html)


@app.get("/customer-ui")
def customer_ui():
    html = FRONTEND / "customer" / "customer.html"
    if not html.is_file():
        raise HTTPException(503, "请先在 frontend 执行 npm ci && npm run build")
    return FileResponse(html)


class VersionIn(Input):
    version: str


class QuestionIn(Input):
    question: str = Field(min_length=2, max_length=1000)


@app.get("/internal/source/snapshots")
def snapshots(user: dict = Depends(staff)):
    rows = []
    for row in source.snapshots():
        try:
            platform.authorize_snapshot(row["id"], user["name"])
            rows.append(row)
        except PermissionError:
            continue
    return rows


@app.post("/internal/source/index", status_code=202)
def index_source(body: VersionIn, user: dict = Depends(staff)):
    source.files(body.version)
    job = enqueue("source_index", "internal", user["name"], {"version": body.version})
    return {"job_id": job["id"], "status": "queued"}


@app.get("/internal/source/profiles/{snapshot_id}/{module}")
def source_profile(snapshot_id: str, module: str, _user: dict = Depends(staff)):
    platform.authorize_snapshot(snapshot_id, _user["name"])
    result = source.profile(snapshot_id, module)
    if not result:
        raise HTTPException(404, "画像不存在")
    return result


@app.post("/internal/source/{snapshot_id}/ask")
def source_ask(snapshot_id: str, body: QuestionIn, _user: dict = Depends(staff)):
    platform.authorize_snapshot(snapshot_id, _user["name"])
    result = source.ask(snapshot_id, body.question)
    platform.authorize_snapshot(snapshot_id, _user["name"])
    if not result:
        raise HTTPException(404, "快照不存在")
    if snapshot_id.startswith("idx-"):
        from .code_qa import record_sync
        result = record_sync(snapshot_id, body.question, _user["name"], result)
    return result


@app.get("/internal/source/{snapshot_id}/evidence/{evidence_id}")
def source_evidence(snapshot_id: str, evidence_id: str, _user: dict = Depends(staff)):
    platform.authorize_snapshot(snapshot_id, _user["name"])
    result = source.evidence(snapshot_id, evidence_id)
    if not result:
        raise HTTPException(404, "证据不存在")
    return result


@app.get("/internal/jobs/{job_id}")
def internal_job(job_id: str, _user: dict = Depends(staff)):
    job = get_job(job_id)
    if not job or job["scope"] != "internal":
        raise HTTPException(404, "任务不存在")
    snapshot_id = job["payload"].get("snapshot_id")
    if job["kind"] == "module_analysis":
        from .source_agent import get_run
        snapshot_id = get_run(job["payload"]["run_id"])["snapshot_id"]
    elif job["kind"] == "code_answer":
        from .code_qa import get
        snapshot_id = get(job["payload"]["answer_id"])["snapshot_id"]
    elif job["kind"] == "repository_index":
        platform.authorize(job["payload"]["repository_id"], _user["name"])
    if snapshot_id:
        platform.authorize_snapshot(snapshot_id, _user["name"])
    return job


class ManualIn(Input):
    page_id: str = Field(min_length=1, max_length=80)
    release: str
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=10000)
    baseline_id: int | None = None


@app.get("/internal/manuals")
def manual_revisions(release: str, _user: dict = Depends(staff)):
    return knowledge.revisions(release)


@app.post("/internal/manuals")
def manual_create(body: ManualIn, _user: dict = Depends(staff)):
    return knowledge.create_revision(body.page_id, body.release, body.title, body.content)


class EvidenceDraftIn(Input):
    page_id: str = Field(min_length=1, max_length=80)
    release: str
    title: str = Field(min_length=1, max_length=160)
    snapshot_id: str
    evidence_ids: list[str]


@app.post("/internal/manuals/from-evidence")
def manual_from_evidence(body: EvidenceDraftIn, _user: dict = Depends(staff)):
    platform.authorize_snapshot(body.snapshot_id, _user["name"])
    return knowledge.draft_from_evidence(body.page_id, body.release, body.title,
                                         body.snapshot_id, body.evidence_ids)


@app.put("/internal/manuals")
def manual_edit(body: ManualIn, _user: dict = Depends(staff)):
    if body.baseline_id is None:
        raise ValueError("需要 baseline_id")
    return knowledge.edit(body.page_id, body.release, body.baseline_id, body.title, body.content)


@app.post("/internal/manuals/{revision_id}/approve")
def manual_approve(revision_id: int, _user: dict = Depends(editor)):
    row = knowledge.approve(revision_id)
    if not row:
        raise HTTPException(404, "修订不存在")
    return row


@app.post("/internal/manuals/{revision_id}/reject")
def manual_reject(revision_id: int, _user: dict = Depends(editor)):
    row = knowledge.reject(revision_id)
    if not row:
        raise HTTPException(404, "修订不存在")
    return row


@app.post("/internal/knowledge/{release}/publish")
def knowledge_publish(release: str, _user: dict = Depends(editor)):
    return knowledge.publish(release)


@app.get("/internal/operations/topics")
def operation_topics(_user: dict = Depends(staff)):
    return knowledge.topics()


@app.get("/internal/legacy/projects")
def legacy_projects(_user: dict = Depends(staff)):
    return legacy.list_projects()


@app.get("/internal/legacy/projects/{project_id}/runs")
def legacy_runs(project_id: int, _user: dict = Depends(staff)):
    return legacy.list_runs(project_id)


@app.get("/internal/legacy/runs/{run_id}/results")
def legacy_results(run_id: int, _user: dict = Depends(staff)):
    return legacy.run_results(run_id)


@app.get("/internal/legacy/automation/items/{item_id}/revisions")
def legacy_script_revisions(item_id: int, _user: dict = Depends(staff)):
    return legacy.script_revisions(item_id)


@app.get("/internal/legacy/automation/revisions/{revision_id}")
def legacy_script_revision(revision_id: int, _user: dict = Depends(staff)):
    return legacy.script_revision(revision_id)


@app.post("/internal/legacy/automation/revisions/{revision_id}/import")
def legacy_script_import(revision_id: int, _user: dict = Depends(staff)):
    return legacy.import_script_revision(revision_id)


@app.get("/internal/legacy/automation/imported-scripts")
def legacy_imported_scripts(_user: dict = Depends(staff)):
    return legacy.imported_scripts()


@app.get("/internal/legacy/automation/items/{item_id}/evidence")
def legacy_item_evidence(item_id: int, _user: dict = Depends(staff)):
    return legacy.item_evidence(item_id)


@app.post("/internal/legacy/projects/{project_id}/import")
def legacy_import(project_id: int, _user: dict = Depends(staff)):
    return legacy.import_cases(project_id)


class CsvIn(Input):
    csv: str = Field(min_length=1, max_length=500000)


@app.get("/internal/test-cases")
def test_cases(_user: dict = Depends(staff)):
    return testing.list_cases()


@app.post("/internal/test-cases/import")
def test_import(body: CsvIn, _user: dict = Depends(staff)):
    return testing.import_csv(body.csv)


@app.post("/internal/test-cases/{case_id}/candidate")
def test_candidate(case_id: str, _user: dict = Depends(staff)):
    return testing.create_candidate(case_id)


class DigestIn(Input):
    digest: str


@app.post("/internal/test-candidates/{candidate_id}/approve")
def test_approve(candidate_id: str, body: DigestIn, _user: dict = Depends(editor)):
    return testing.approve_candidate(candidate_id, body.digest)


@app.post("/internal/test-candidates/{candidate_id}/run", status_code=202)
def test_run(candidate_id: str, _user: dict = Depends(staff)):
    return testing.run_candidate(candidate_id, enqueue)


@app.get("/internal/test-runs/{run_id}")
def test_run_get(run_id: str, _user: dict = Depends(staff)):
    result = testing.get_run(run_id)
    if not result:
        raise HTTPException(404, "运行不存在")
    return result


@app.get("/customer/help")
def help_pages(user: dict = Depends(customer)):
    context = knowledge.projection(user["tenant"])
    return {"release": context["release"], "knowledge_snapshot_id": context["snapshot_id"], "pages": context["pages"]}


@app.post("/customer/ask")
def customer_ask(body: QuestionIn, user: dict = Depends(customer)):
    return knowledge.answer(user["tenant"], user["name"], body.question)


@app.get("/customer/workflows")
def workflows(user: dict = Depends(customer)):
    return workflow.catalog(user["tenant"])


class PrepareIn(Input):
    parameters: dict


@app.post("/customer/workflows/order.summary.read/prepare")
def workflow_prepare(body: PrepareIn, user: dict = Depends(customer)):
    return workflow.prepare(user["tenant"], user["name"], body.parameters)


class ConfirmIn(Input):
    prepared_id: str
    confirmed_preview_digest: str
    confirmation: bool


@app.post("/customer/executions", status_code=202)
def workflow_confirm(body: ConfirmIn, idempotency_key: str = Header(default="", alias="Idempotency-Key"),
                     user: dict = Depends(customer)):
    return workflow.confirm(user["tenant"], user["name"], body.prepared_id,
                            body.confirmed_preview_digest, idempotency_key, body.confirmation)


@app.get("/customer/executions")
def executions(user: dict = Depends(customer)):
    return workflow.list_executions(user["tenant"], user["name"])


@app.get("/customer/executions/{execution_id}")
def execution(execution_id: str, user: dict = Depends(customer)):
    row = workflow.get_execution(user["tenant"], user["name"], execution_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    return row


from .routes import router
app.include_router(router)
