"""TestPilot 受控 API 适配器：只读取资产，不触碰旧数据库或浏览器凭据。"""

from __future__ import annotations

import os
import re
import json
import hashlib
import secrets
from contextlib import contextmanager

import httpx

from .testing import add_case
from .store import db
from .store import many, now, one, pack, audit
from .config import integration_credentials
from .knowledge import redact


def sanitized(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if re.search(r"(?i)password|passwd|api[_-]?key|authorization|cookie|credential|^token$", key) else sanitized(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitized(item) for item in value]
    return redact(value) if isinstance(value, str) else value


@contextmanager
def client():
    base = os.getenv("TESTPILOT_BASE_URL", "http://127.0.0.1:8099").rstrip("/")
    with httpx.Client(base_url=base, timeout=8, follow_redirects=False) as http:
        config = http.get("/api/config")
        config.raise_for_status()
        if config.json().get("auth_enabled"):
            email, password = integration_credentials()
            if not email or not password:
                raise ValueError("TestPilot 已开启认证，请通过环境变量配置集成账号")
            login = http.post("/api/auth/login", json={"email": email, "password": password})
            login.raise_for_status()
        yield http


def list_projects() -> list[dict]:
    with client() as http:
        response = http.get("/api/projects")
        response.raise_for_status()
        return sanitized(response.json())


def list_cases(project_id: int) -> list[dict]:
    with client() as http:
        response = http.get(f"/api/projects/{project_id}/testcases")
        response.raise_for_status()
        return sanitized(response.json())


def list_runs(project_id: int) -> list[dict]:
    with client() as http:
        response = http.get(f"/api/projects/{project_id}/runs")
        response.raise_for_status()
        return sanitized(response.json())


def run_results(run_id: int) -> list[dict]:
    with client() as http:
        response = http.get(f"/api/runs/{run_id}/results")
        response.raise_for_status()
        return sanitized(response.json())


def script_revisions(item_id: int) -> list[dict]:
    with client() as http:
        response = http.get(f"/api/automation/items/{item_id}/revisions")
        response.raise_for_status()
        return sanitized(response.json())


def script_revision(revision_id: int) -> dict:
    with client() as http:
        response = http.get(f"/api/automation/revisions/{revision_id}")
        response.raise_for_status()
        return sanitized(response.json())


def item_evidence(item_id: int) -> dict:
    with client() as http:
        response = http.get(f"/api/automation/items/{item_id}/evidence")
        response.raise_for_status()
        return sanitized(response.json())


def import_script_revision(revision_id: int) -> dict:
    revision = script_revision(revision_id)
    if revision.get("id") != revision_id or not re.fullmatch(r"[0-9a-fA-F]{64}", revision.get("content_sha256") or ""):
        raise ValueError("旧系统脚本修订缺少稳定 ID 或内容摘要")
    with db() as con:
        existing = one(con, "SELECT * FROM legacy_script_assets WHERE source_revision_id=?", (revision_id,))
        if existing:
            if existing["content_sha256"] != revision["content_sha256"]:
                raise RuntimeError("SOURCE_REVISION_CONFLICT")
            return {**existing, "created": False}
        asset_id = "legacy-script-" + str(revision_id)
        con.execute("INSERT INTO legacy_script_assets VALUES (?,?,?,?,?,?,?)",
                    (asset_id, revision_id, revision["batch_item_id"], revision["revision"],
                     revision["content_sha256"], revision["status"], now()))
        return {**one(con, "SELECT * FROM legacy_script_assets WHERE id=?", (asset_id,)), "created": True}


def imported_scripts() -> list[dict]:
    with db() as con:
        return many(con, "SELECT * FROM legacy_script_assets ORDER BY imported_at DESC")


def import_cases(project_id: int) -> dict:
    cases = list_cases(project_id)
    created = 0
    with db() as con:
        for case in cases:
            payload = {key: case.get(key) for key in ("case_key", "name", "module", "preconditions", "steps", "prompt", "expected")}
            _, is_new = add_case("testpilot-plus", f"{project_id}:{case['id']}",
                                 case.get("updated_at") or "unknown", case.get("name") or case.get("case_key") or str(case["id"]),
                                 {}, None, payload, con)
            created += is_new
    return {"project_id": project_id, "source": "testpilot-plus", "rows": len(cases),
            "created": created, "existing": len(cases) - created,
            "note": "旧系统自然语言预期仅作来源记录，未认定为本地实测断言"}


def request(method: str, path: str, payload=None):
    with client() as http:
        response = http.request(method, path, json=payload)
        response.raise_for_status()
        return sanitized(response.json())


def status() -> dict:
    try:
        projects = list_projects()
        return {"status": "connected", "projects": len(projects), "authentication": "independent_session",
                "run_enabled": os.getenv("TESTPILOT_ALLOW_RUN", "false").lower() == "true"}
    except (httpx.HTTPError, ValueError):
        return {"status": "unavailable", "run_enabled": False}


def list_batches(project_id: int | None = None) -> list[dict]:
    return request("GET", f"/api/projects/{project_id}/automation/batches" if project_id else "/api/automation/batches")


def batch(batch_id: int) -> dict:
    return request("GET", f"/api/automation/batches/{batch_id}")


def sync_run(run_id: int, context: dict) -> dict:
    run = request("GET", f"/api/runs/{run_id}")
    results = run_results(run_id)
    payload = {"run": run, "results": results, "source_system": "testpilot-plus", "context": context,
               "assertion_status": "unverified_legacy_judgement",
               "note": "Browser Agent/judge 的 passed 不替代同构建业务断言，环境与角色单独留档"}
    asset_id = "legacy-run-" + str(run_id)
    request_hash = hashlib.sha256(pack([run_id, context]).encode()).hexdigest()
    with db() as con:
        old = one(con, "SELECT * FROM legacy_runs WHERE id=?", (asset_id,))
        if old and old["request_hash"] != request_hash:
            raise RuntimeError("LEGACY_RUN_CONTEXT_CONFLICT")
        con.execute("INSERT INTO legacy_runs VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,result=excluded.result",
                    (asset_id, run_id, request_hash, "import:" + str(run_id), run["status"], pack(context), pack(payload), now()))
    return {"id": asset_id, "source_run_id": run_id, "status": run["status"], "results": len(results), "payload_hash": hashlib.sha256(pack(payload).encode()).hexdigest()}


def create_run(project_id: int, case_ids: list[int], environment_id: int | None, key: str, actor: str) -> dict:
    if os.getenv("TESTPILOT_ALLOW_RUN", "false").lower() != "true":
        raise ValueError("TESTPILOT_RUN_DISABLED")
    if not key or len(key) > 100 or not case_ids or len(case_ids) > 100 or any(type(i) is not int or i <= 0 for i in case_ids):
        raise ValueError("TESTPILOT_RUN_ARGUMENT_INVALID")
    from .jobs import enqueue
    context = {"project_id": project_id, "case_ids": sorted(set(case_ids)), "environment_id": environment_id}
    hash_value = hashlib.sha256(pack(context).encode()).hexdigest()
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        scoped_key = actor + ":" + key
        old = one(con, "SELECT * FROM legacy_runs WHERE idempotency_key=?", (scoped_key,))
        if old:
            if old["request_hash"] != hash_value:
                raise RuntimeError("IDEMPOTENCY_CONFLICT")
            return old
        local_id = "legacy-request-" + secrets.token_hex(8)
        job = enqueue("legacy_test_run", "internal", actor, {"request_id": local_id}, con)
        con.execute("INSERT INTO legacy_runs VALUES (?,?,?,?,?,?,?,?)", (local_id, None, hash_value, scoped_key, "queued", pack(context), pack({"job_id": job["id"]}), now()))
        audit(actor, "testpilot.request_run", local_id, context, con)
    return {"id": local_id, "job_id": job["id"], "status": "queued"}


def perform_run(job: dict) -> dict:
    if os.getenv("TESTPILOT_ALLOW_RUN", "false").lower() != "true":
        raise ValueError("TESTPILOT_RUN_DISABLED")
    request_id = json.loads(job["payload"])["request_id"]
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        current = one(con, "SELECT * FROM jobs WHERE id=? AND status='running' AND run_epoch=? AND lease_owner=? AND lease_until>?", (job["id"], job["run_epoch"], job["lease_owner"], now()))
        if not current:
            raise ValueError("LEASE_LOST")
        row = one(con, "SELECT * FROM legacy_runs WHERE id=?", (request_id,))
        if row["source_run_id"]:
            return {"request_id": request_id, "source_run_id": row["source_run_id"]}
        if row["status"] != "queued":
            raise ValueError("TARGET_STATE_UNKNOWN: 旧服务无幂等创建接口，需核对后再操作")
        con.execute("UPDATE legacy_runs SET status='submitting' WHERE id=?", (request_id,))
    context = json.loads(row["context"])
    correlation = None
    try:
        if context.get("kind") == "dtm_action":
            if context["action"] in ("run", "retry"):
                revision = script_revision(context["target_id"])
                if revision["content_sha256"] != context["digest"]:
                    raise ValueError("SCRIPT_DIGEST_CHANGED")
                with db() as con:
                    approved = one(con, "SELECT digest FROM legacy_script_approvals WHERE source_revision_id=?", (context["target_id"],))
                if not approved or approved["digest"] != context["digest"]:
                    raise ValueError("LEGACY_SCRIPT_NOT_APPROVED")
                correlation = {"prior_attempt_ids": [a["id"] for a in revision.get("attempts", [])]}
                with db() as con:
                    from .jobs import assert_lease
                    assert_lease(job, con)
                    con.execute("UPDATE legacy_runs SET result=? WHERE id=?", (pack({"_adapter": correlation}), request_id))
            result = request("POST", context["path"], {})
            if correlation is not None:
                result["_adapter"] = correlation
            source_id = context["target_id"]
        else:
            result = request("POST", f"/api/projects/{context['project_id']}/runs",
                             {"name": "source-platform:" + request_id, "case_ids": context["case_ids"],
                              "environment_id": context["environment_id"], "concurrency": 1})
            source_id = result["id"]
    except httpx.HTTPStatusError as exc:
        rejected = exc.response.status_code < 500
        with db() as con:
            con.execute("UPDATE legacy_runs SET status=? WHERE id=?", ("rejected" if rejected else "unknown", request_id))
        raise ValueError(("UPSTREAM_REJECTED: HTTP " + str(exc.response.status_code)) if rejected else "TARGET_STATE_UNKNOWN: 不自动重发测试创建请求") from None
    except ValueError:
        with db() as con:
            con.execute("UPDATE legacy_runs SET status='rejected' WHERE id=?", (request_id,))
        raise
    except httpx.HTTPError:
        with db() as con:
            con.execute("UPDATE legacy_runs SET status='unknown' WHERE id=?", (request_id,))
        raise ValueError("TARGET_STATE_UNKNOWN: 不自动重发测试创建请求") from None
    with db() as con:
        con.execute("UPDATE legacy_runs SET status=?,source_run_id=?,result=? WHERE id=?", (result.get("status", "submitted"), source_id, pack(result), request_id))
    return {"request_id": request_id, "source_id": source_id, "status": result.get("status", "submitted")}


def report(attempt_id: int, path: str) -> tuple[bytes, str]:
    from pathlib import PurePosixPath
    candidate = PurePosixPath(path)
    if attempt_id <= 0 or candidate.is_absolute() or ".." in candidate.parts or not re.fullmatch(r"[A-Za-z0-9_./-]{1,250}", path):
        raise ValueError("REPORT_PATH_INVALID")
    with client() as http:
        response = http.get(f"/api/automation/attempts/{attempt_id}/report/{path}")
        response.raise_for_status()
        if len(response.content) > 20_000_000:
            raise ValueError("REPORT_TOO_LARGE")
        return response.content, response.headers.get("content-type", "application/octet-stream")


def approve_script(revision_id: int, digest: str, actor: str) -> dict:
    revision = script_revision(revision_id)
    if revision["content_sha256"] != digest:
        raise RuntimeError("SCRIPT_DIGEST_MISMATCH")
    import_script_revision(revision_id)
    with db() as con:
        con.execute("INSERT INTO legacy_script_approvals VALUES (?,?,?,?) ON CONFLICT(source_revision_id) DO UPDATE SET digest=excluded.digest,reviewer=excluded.reviewer,created_at=excluded.created_at",
                    (revision_id, digest, actor, now()))
        audit(actor, "testpilot.script_approve", str(revision_id), {"digest": digest}, con)
    return {"source_revision_id": revision_id, "approved_digest": digest}


def reconcile(request_id: str) -> dict:
    with db() as con:
        row = one(con, "SELECT * FROM legacy_runs WHERE id=?", (request_id,))
    if not row:
        raise LookupError("LEGACY_REQUEST_NOT_FOUND")
    context = json.loads(row["context"])
    if context.get("kind") == "dtm_action":
        target = context["target_id"]
        result = script_revision(target) if context["action"] in ("run", "retry") else batch(target) if context["action"] == "start" else {"revisions": script_revisions(target)}
        status_value = result.get("status", "observed")
        if context["action"] in ("run", "retry"):
            correlation = json.loads(row["result"] or "{}").get("_adapter", {})
            candidates = []
            if correlation.get("attempt_id"):
                candidates = [a for a in result.get("attempts", []) if a["id"] == correlation["attempt_id"]]
            elif "prior_attempt_ids" in correlation:
                candidates = [a for a in result.get("attempts", []) if a["id"] not in correlation["prior_attempt_ids"]]
            if len(candidates) == 1:
                attempt = candidates[0]
                correlation["attempt_id"] = attempt["id"]
                correlation["basis"] = "human_declared" if correlation.get("validation_human_correlation") else "unique_observed_attempt"
                status_value = "verified" if attempt["status"] == "passed" else attempt["status"]
                result["matched_attempt"] = attempt
            else:
                status_value = "unknown"
                result["correlation_note"] = "尚无唯一的执行尝试关联；不能用修订的最新状态证明本次请求成功"
            result["_adapter"] = correlation
    else:
        source_id = row["source_run_id"]
        if not source_id and context.get("project_id"):
            matches = [r for r in list_runs(context["project_id"]) if r.get("name") == "source-platform:" + request_id]
            if len(matches) == 1:
                source_id = matches[0]["id"]
        if not source_id:
            return {"id": request_id, "status": "unknown", "note": "未确认目标运行；继续人工对账，不自动重发"}
        result = {"run": request("GET", f"/api/runs/{source_id}"), "results": run_results(source_id)}
        status_value = result["run"]["status"]
        row["source_run_id"] = source_id
    result.update(source_system="testpilot-plus", assertion_status="unverified_legacy_judgement")
    with db() as con:
        con.execute("UPDATE legacy_runs SET status=?,source_run_id=?,result=? WHERE id=?", (status_value, row["source_run_id"], pack(result), request_id))
    return {"id": request_id, "status": status_value, "result": result}


def create_automation_action(target_id: int, action: str, digest: str, key: str, actor: str):
    if os.getenv("TESTPILOT_ALLOW_RUN", "false").lower() != "true":
        raise ValueError("TESTPILOT_RUN_DISABLED")
    paths = {"start": f"/api/automation/batches/{target_id}/start", "generate": f"/api/automation/items/{target_id}/generate",
             "reexplore": f"/api/automation/items/{target_id}/reexplore", "run": f"/api/automation/revisions/{target_id}/run", "retry": f"/api/automation/revisions/{target_id}/retry"}
    if action not in paths or target_id <= 0 or not key or len(key) > 100:
        raise ValueError("AUTOMATION_ACTION_INVALID")
    if action in ("run", "retry"):
        with db() as con:
            row = one(con, "SELECT digest FROM legacy_script_approvals WHERE source_revision_id=?", (target_id,))
        if not row or row["digest"] != digest:
            raise RuntimeError("LEGACY_SCRIPT_NOT_APPROVED")
    from .jobs import enqueue
    context = {"kind": "dtm_action", "action": action, "target_id": target_id, "digest": digest, "path": paths[action]}
    request_hash = hashlib.sha256(pack(context).encode()).hexdigest()
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        scoped_key = actor + ":dtm:" + key
        old = one(con, "SELECT * FROM legacy_runs WHERE idempotency_key=?", (scoped_key,))
        if old:
            if old["request_hash"] != request_hash:
                raise RuntimeError("IDEMPOTENCY_CONFLICT")
            return old
        request_id = "dtm-request-" + secrets.token_hex(8)
        job = enqueue("legacy_test_run", "internal", actor, {"request_id": request_id}, con)
        con.execute("INSERT INTO legacy_runs VALUES (?,?,?,?,?,?,?,?)", (request_id, None, request_hash, scoped_key, "queued", pack(context), pack({"job_id": job["id"]}), now()))
        audit(actor, "testpilot." + action, request_id, context, con)
    return {"id": request_id, "job_id": job["id"], "status": "queued"}


def artifact(result_id: int, kind: str) -> tuple[bytes, str, str]:
    if kind not in ("video", "trace"):
        raise ValueError("ARTIFACT_KIND_INVALID")
    with client() as http:
        # Only upstream-owned relative artifacts. Never forward credentials to an arbitrary redirect host.
        response = http.get(f"/api/results/{result_id}/{kind}")
        if response.status_code in (302, 307):
            location = response.headers.get("location", "")
            if not location.startswith("/artifacts/") or ".." in location or "\\" in location:
                raise ValueError("EXTERNAL_ARTIFACT_REQUIRES_APPROVED_STORAGE")
            response = http.get(location)
        response.raise_for_status()
        if len(response.content) > 20_000_000:
            raise ValueError("ARTIFACT_SIZE_EXCEEDED")
        return response.content, response.headers.get("content-type", "application/octet-stream"), hashlib.sha256(response.content).hexdigest()
