"""任务事实表与独立 Worker；租约版本阻止过期 Worker 覆写结果。"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import time
import threading

import httpx

from .store import db, now, one, pack, DATABASE_URL, DATABASE_ERRORS


def enqueue(kind: str, scope: str, actor: str, payload: dict,
            con: sqlite3.Connection | None = None) -> dict:
    job = {"id": "job-" + secrets.token_hex(8), "kind": kind, "scope": scope,
           "actor": actor, "payload": pack(payload), "status": "queued", "created_at": now()}
    if con is None:
        with db() as owned:
            return enqueue(kind, scope, actor, payload, owned)
    con.execute("INSERT INTO jobs(id,kind,scope,actor,payload,status,created_at) VALUES (?,?,?,?,?,?,?)",
                (job["id"], job["kind"], job["scope"], job["actor"], job["payload"], job["status"], job["created_at"]))
    return job


def claim(owner: str, kinds: list[str] | None = None) -> dict | None:
    with db() as con:
        if not DATABASE_URL:
            con.execute("BEGIN IMMEDIATE")
        kind_sql = " AND kind IN (" + ",".join("?" for _ in kinds) + ")" if kinds else ""
        job = one(con, """SELECT * FROM jobs WHERE (status='queued'
          OR (status='running' AND lease_until<?))""" + kind_sql + " ORDER BY created_at,id LIMIT 1" + (" FOR UPDATE SKIP LOCKED" if DATABASE_URL else ""), (now(), *(kinds or [])))
        if not job:
            return None
        con.execute("""UPDATE jobs SET status='running',run_epoch=run_epoch+1,
          lease_owner=?,lease_until=? WHERE id=?""", (owner, now() + 30, job["id"]))
        return one(con, "SELECT * FROM jobs WHERE id=?", (job["id"],))


def _mock(params: dict) -> dict:
    base = os.getenv("MOCK_BUSINESS_BASE_URL", "http://127.0.0.1:9100").rstrip("/")
    with httpx.Client(timeout=5) as client:
        response = client.get(base + "/orders/summary", params=params,
                              headers={"X-Mock-Secret": os.getenv("MOCK_SECRET", "demo-mock-only")})
        response.raise_for_status()
        return response.json()


def assert_lease(job: dict, con=None):
    if con is None:
        with db() as owned:
            return assert_lease(job, owned)
    if not one(con, "SELECT id FROM jobs WHERE id=? AND status='running' AND run_epoch=? AND lease_owner=? AND lease_until>?", (job["id"], job["run_epoch"], job["lease_owner"], now())):
        raise ValueError("LEASE_LOST")


def perform(job: dict) -> dict:
    payload = json.loads(job["payload"])
    if job["kind"] == "source_index":
        from .source import index_fixture
        return index_fixture(payload["version"])
    if job["kind"] == "repository_index":
        from .java_source import index_repository
        return index_repository(payload["repository_id"], payload.get("ref", "HEAD"), job=job)
    if job["kind"] == "module_analysis":
        from .source_agent import run
        return run(job)
    if job["kind"] == "code_answer":
        from .code_qa import answer_job
        return answer_job(job)
    if job["kind"] == "legacy_test_run":
        from .legacy import perform_run
        return perform_run(job)
    if job["kind"] == "test_run":
        with db() as con:
            row = one(con, """SELECT r.id,r.expected,c.params FROM test_runs r
              JOIN test_cases c ON c.id=r.case_id WHERE r.id=?""", (payload["run_id"],))
        if not row:
            raise ValueError("测试任务缺少用例")
        params = json.loads(row["params"])
        actual = _mock(params)
        if actual.get("tenant") != params["tenant"] or actual.get("filter") != {k: params[k] for k in ("status", "date_from", "date_to")}:
            raise ValueError("TARGET_RESPONSE_SCOPE_MISMATCH")
        return {"run_id": row["id"], "expected": row["expected"], "actual": actual["total"],
                "assertion_status": "passed" if row["expected"] == actual["total"] else "failed"}
    if job["kind"] == "workflow_execute":
        with db() as con:
            row = one(con, """SELECT e.id,e.tenant,e.status,p.params,p.release,p.workflow_version,p.workflow_digest FROM executions e
              JOIN preparations p ON p.id=e.prepared_id WHERE e.id=?""", (payload["execution_id"],))
            current = one(con, "SELECT id FROM jobs WHERE id=? AND status='running' AND run_epoch=? AND lease_owner=? AND lease_until>?", (job["id"], job["run_epoch"], job["lease_owner"], now()))
            deployed = one(con, "SELECT release,flags FROM deployments WHERE tenant=?", (row["tenant"],)) if row else None
            version = one(con, "SELECT status,digest FROM workflow_versions WHERE version=?", (row["workflow_version"],)) if row else None
        if not row:
            raise ValueError("执行任务缺少预览")
        if not current or row["status"] == "cancelled":
            raise ValueError("EXECUTION_CANCELLED_OR_LEASE_LOST")
        if not version or version["status"] != "published" or version["digest"] != row["workflow_digest"] or (deployed and (deployed["release"] != row["release"] or not json.loads(deployed["flags"]).get("order.summary.read"))):
            raise ValueError("ACCESS_REVOKED_OR_VERSION_CHANGED")
        params = json.loads(row["params"])
        actual = _mock({"tenant": row["tenant"], **params})
        if actual.get("tenant") != row["tenant"] or actual.get("filter") != params or not isinstance(actual.get("total"), int) or actual["total"] < 0:
            raise ValueError("TARGET_RESPONSE_SCOPE_MISMATCH")
        return {"execution_id": row["id"], "status": "succeeded", "summary": actual}
    raise ValueError("未知任务类型")


def finish(job: dict, result: dict | None, error: str | None) -> bool:
    status = "failed" if error else "succeeded"
    if job["kind"] == "module_analysis" and result:
        status = {"WAITING_REVIEW": "waiting_review", "INCOMPLETE": "incomplete", "CANCELLED": "cancelled", "FAILED": "failed"}.get(result["status"], "succeeded")
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        updated = con.execute("""UPDATE jobs SET status=?,result=?,error=?,lease_owner=NULL,
          lease_until=NULL WHERE id=? AND status='running' AND run_epoch=?
          AND lease_owner=? AND lease_until>?""",
          (status, pack(result) if result else None, error, job["id"], job["run_epoch"], job["lease_owner"], now()))
        if updated.rowcount != 1:
            return False
        payload = json.loads(job["payload"])
        if job["kind"] == "repository_index" and result and payload.get("analyze_modules"):
            from . import java_source, source_agent
            baseline = result["report"].get("base_snapshot_id")
            modules = java_source.impact(baseline, result["snapshot_id"])["affected_modules"] if baseline else [m["id"] for m in result["manifest"]["modules"]]
            created = []
            for module in modules:
                if java_source.module_coverage(result["snapshot_id"], module)["source_files_total"] and not one(con, "SELECT id FROM agent_runs WHERE snapshot_id=? AND module=?", (result["snapshot_id"], module)):
                    created.append(source_agent.create(result["snapshot_id"], module, job["actor"], con))
            result["module_analysis_jobs"] = created
            con.execute("UPDATE jobs SET result=? WHERE id=?", (pack(result), job["id"]))
        elif job["kind"] == "test_run":
            con.execute("UPDATE test_runs SET status=?,actual=? WHERE id=?",
                        ((result or {}).get("assertion_status", "error"), (result or {}).get("actual"), payload["run_id"]))
        elif job["kind"] == "workflow_execute":
            con.execute("UPDATE executions SET status=?,result=? WHERE id=?",
                        (status, pack(result) if result else None, payload["execution_id"]))
        elif job["kind"] == "module_analysis":
            con.execute("UPDATE agent_runs SET status=?,stage=? WHERE id=?",
                        ((result or {}).get("status", "FAILED"), "review" if status == "waiting_review" else "finished", payload["run_id"]))
        elif job["kind"] == "code_answer":
            con.execute("UPDATE code_answers SET result=? WHERE id=?", (pack(result or {"status": "failed", "error": error}), payload["answer_id"]))
    return True


def process_once(owner: str = "demo-worker", kinds: list[str] | None = None) -> dict | None:
    job = claim(owner, kinds)
    if not job:
        return None
    stopped = threading.Event()
    def heartbeat():
        while not stopped.wait(5):
            try:
                with db() as con:
                    renewed = con.execute("""UPDATE jobs SET lease_until=? WHERE id=? AND status='running'
                      AND run_epoch=? AND lease_owner=? AND lease_until>?""",
                      (now() + 30, job["id"], job["run_epoch"], job["lease_owner"], now()))
                    if renewed.rowcount != 1:
                        break
            except DATABASE_ERRORS:
                continue
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        result = perform(job)
        error = None
    except (ValueError, LookupError, PermissionError, httpx.HTTPError) as exc:
        result = None
        error = str(exc)[:200]
    except Exception as exc:
        result = None
        error = type(exc).__name__
    finally:
        stopped.set()
        thread.join(timeout=6)
    accepted = finish(job, result, error)
    return {"job_id": job["id"], "accepted": accepted, "result": result, "error": error}


def get_job(job_id: str) -> dict | None:
    with db() as con:
        row = one(con, "SELECT * FROM jobs WHERE id=?", (job_id,))
    if row:
        row["payload"] = json.loads(row["payload"])
        row["result"] = json.loads(row["result"]) if row["result"] else None
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--kinds", help="逗号分隔的任务类型；分析、测试和客户 Runner 可独立部署")
    args = parser.parse_args()
    from .store import init
    init()
    while True:
        worked = process_once("worker-" + str(os.getpid()), args.kinds.split(",") if args.kinds else None)
        if args.once:
            print(worked or {"status": "idle"})
            break
        if not worked:
            time.sleep(0.5)


if __name__ == "__main__":
    main()
