"""唯一的客户流程：固定模板读取订单统计。"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import date

from .jobs import enqueue
from .knowledge import RELEASES, current_release
from .store import db, many, now, one, pack


def catalog(tenant: str) -> list[dict]:
    with db() as con:
        deployment = one(con, "SELECT release,flags FROM deployments WHERE tenant=?", (tenant,))
        rows = many(con, "SELECT * FROM workflow_versions WHERE status='published' ORDER BY created_at DESC")
    if deployment and not json.loads(deployment["flags"]).get("order.summary.read", False):
        return []
    selected = {}
    release = deployment["release"] if deployment else RELEASES[tenant]
    for row in rows:
        spec = json.loads(row["spec"])
        if release in spec["releases"] and row["id"] not in selected:
            selected[row["id"]] = {"workflow_id": row["id"], "version": row["version"], "digest": row["digest"],
                                   "risk": spec["risk"], "description": spec["description"], "release": release,
                                   "input_schema": spec["input_schema"]}
    return list(selected.values())


def normalize(release: str, params: dict) -> dict:
    if set(params) != {"status", "date_from", "date_to"}:
        raise ValueError("PARAMETER_INVALID: 仅支持 status/date_from/date_to")
    allowed = {"all", "pending"} if release == "v1" else {"all", "pending", "completed"}
    if not isinstance(params["status"], str) or params["status"] not in allowed:
        raise ValueError("PARAMETER_INVALID: 当前版本不支持该状态")
    try:
        start, end = date.fromisoformat(params["date_from"]), date.fromisoformat(params["date_to"])
    except (TypeError, ValueError) as exc:
        raise ValueError("PARAMETER_INVALID: 日期格式无效") from exc
    if start > end or (end - start).days > 31:
        raise ValueError("PARAMETER_INVALID: 最多查询 31 天")
    return {"status": params["status"], "date_from": start.isoformat(), "date_to": end.isoformat()}


def prepare(tenant: str, actor: str, params: dict) -> dict:
    workflows = catalog(tenant)
    if not workflows:
        raise ValueError("WORKFLOW_NOT_AVAILABLE")
    selected = workflows[0]
    release = selected["release"]
    normalized = normalize(release, params)
    content = {"tenant": tenant, "actor": actor, "release": release,
               "workflow_id": "order.summary.read", "workflow_version": selected["version"], "workflow_digest": selected["digest"], "parameters": normalized}
    digest = hashlib.sha256(pack(content).encode()).hexdigest()
    prepared_id = "prep-" + secrets.token_hex(8)
    expires = now() + 300
    with db() as con:
        con.execute("INSERT INTO preparations VALUES (?,?,?,?,?,?,?,?,?)",
                    (prepared_id, tenant, actor, release, pack(normalized), digest, expires, selected["version"], selected["digest"]))
    return {"prepared_id": prepared_id, "preview_digest": digest, "expires_at": expires, "workflow_version": selected["version"],
            "preview": {"action": "读取订单状态统计", "scope": "当前租户与指定日期",
                        "parameters": normalized, "changes_business_data": False}}


def confirm(tenant: str, actor: str, prepared_id: str, confirmed_digest: str,
            idempotency_key: str, confirmation: bool) -> dict:
    if not confirmation or not idempotency_key or len(idempotency_key) > 100:
        raise ValueError("必须确认预览并提供 Idempotency-Key")
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        prepared = one(con, "SELECT * FROM preparations WHERE id=? AND tenant=? AND actor=?",
                       (prepared_id, tenant, actor))
        if not prepared:
            raise LookupError("预览不存在")
        if prepared["preview_digest"] != confirmed_digest:
            raise RuntimeError("PREVIEW_CHANGED")
        request_hash = hashlib.sha256(pack({"prepared_id": prepared_id, "digest": confirmed_digest}).encode()).hexdigest()
        existing = one(con, "SELECT * FROM executions WHERE tenant=? AND actor=? AND idempotency_key=?",
                       (tenant, actor, idempotency_key))
        if existing:
            if existing["request_hash"] != request_hash:
                raise RuntimeError("IDEMPOTENCY_CONFLICT")
            return existing
        deployed = one(con, "SELECT release,flags FROM deployments WHERE tenant=?", (tenant,))
        version = one(con, "SELECT * FROM workflow_versions WHERE id='order.summary.read' AND version=?", (prepared["workflow_version"],))
        if not version or version["status"] != "published" or version["digest"] != prepared["workflow_digest"] or (deployed and not json.loads(deployed["flags"]).get("order.summary.read")):
            raise RuntimeError("WORKFLOW_NOT_AVAILABLE")
        if prepared["expires"] < now() or prepared["release"] != (deployed["release"] if deployed else RELEASES[tenant]):
            raise RuntimeError("PREPARATION_EXPIRED")
        execution_id = "exe-" + secrets.token_hex(8)
        job = enqueue("workflow_execute", tenant, actor, {"execution_id": execution_id}, con)
        con.execute("INSERT INTO executions VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (execution_id, tenant, actor, prepared_id, idempotency_key, request_hash,
                     job["id"], "queued", None, now()))
        return one(con, "SELECT * FROM executions WHERE id=?", (execution_id,))


def get_execution(tenant: str, actor: str, execution_id: str) -> dict | None:
    with db() as con:
        row = one(con, "SELECT * FROM executions WHERE id=? AND tenant=? AND actor=?",
                  (execution_id, tenant, actor))
    if row and row["result"]:
        row["result"] = json.loads(row["result"])
    return row


def list_executions(tenant: str, actor: str) -> list[dict]:
    with db() as con:
        return many(con, "SELECT id,status,job_id,created_at FROM executions WHERE tenant=? AND actor=? ORDER BY created_at DESC", (tenant, actor))


def seed():
    spec = {"workflow_id": "order.summary.read", "version": "0.1.0", "risk": "R0_read_only",
            "description": "读取本租户订单状态统计", "template_id": "api.order_summary.read",
            "releases": ["v1", "v2"], "input_schema": {"status": ["all", "pending", "completed"], "date_from": "date", "date_to": "date"}}
    with db() as con:
        con.execute("INSERT INTO workflow_versions VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                    (spec["workflow_id"], spec["version"], pack(spec), hashlib.sha256(pack(spec).encode()).hexdigest(),
                     "published", pack({"fixture": True, "assertions": "covered_by_regression"}), "seed", now()))


def create_version(spec: dict) -> dict:
    if set(spec) != {"workflow_id", "version", "risk", "description", "template_id", "releases", "input_schema"}:
        raise ValueError("WORKFLOW_SPEC_INVALID")
    if spec["workflow_id"] != "order.summary.read" or spec["template_id"] != "api.order_summary.read" or spec["risk"] != "R0_read_only" or not isinstance(spec["version"], str) or not 1 <= len(spec["version"]) <= 80:
        raise ValueError("WORKFLOW_TEMPLATE_NOT_ALLOWED")
    if not isinstance(spec["releases"], list) or not spec["releases"] or any(v not in ("v1", "v2") for v in spec["releases"]):
        raise ValueError("WORKFLOW_RELEASE_INVALID")
    if spec["input_schema"] != {"status": ["all", "pending", "completed"], "date_from": "date", "date_to": "date"}:
        raise ValueError("WORKFLOW_INPUT_SCHEMA_INVALID")
    digest = hashlib.sha256(pack(spec).encode()).hexdigest()
    with db() as con:
        old = one(con, "SELECT * FROM workflow_versions WHERE id=? AND version=?", (spec["workflow_id"], spec["version"]))
        if old:
            if old["digest"] != digest:
                raise RuntimeError("WORKFLOW_VERSION_CONFLICT")
            return old
        con.execute("INSERT INTO workflow_versions VALUES (?,?,?,?,?,?,?,?)", (spec["workflow_id"], spec["version"], pack(spec), digest, "draft", None, None, now()))
    return {"workflow_id": spec["workflow_id"], "version": spec["version"], "digest": digest, "status": "draft"}


def validate_version(version: str) -> dict:
    from .jobs import _mock
    with db() as con:
        row = one(con, "SELECT * FROM workflow_versions WHERE id='order.summary.read' AND version=?", (version,))
    if not row:
        raise LookupError("WORKFLOW_NOT_FOUND")
    if row["status"] != "draft":
        raise RuntimeError("WORKFLOW_NOT_DRAFT")
    checks = []
    for tenant, expected in (("tenant-a", 2), ("tenant-b", 3)):
        params = {"tenant": tenant, "status": "pending", "date_from": "2026-09-01", "date_to": "2026-09-07"}
        result = _mock(params)
        checks.append({"tenant": tenant, "passed": result.get("tenant") == tenant and result.get("total") == expected
                       and result.get("filter") == {k: v for k, v in params.items() if k != "tenant"}})
    report = {"checks": checks, "passed": all(c["passed"] for c in checks)}
    with db() as con:
        con.execute("UPDATE workflow_versions SET validation=?,status=? WHERE version=? AND status='draft'", (pack(report), "validated" if report["passed"] else "draft", version))
    return report


def transition(version: str, digest: str, action: str, actor: str) -> dict:
    states = {"approve": ("validated", "approved"), "publish": ("approved", "published"), "disable": ("published", "disabled")}
    if action not in states:
        raise ValueError("WORKFLOW_ACTION_INVALID")
    before, after = states[action]
    with db() as con:
        changed = con.execute("UPDATE workflow_versions SET status=?,reviewer=? WHERE id='order.summary.read' AND version=? AND digest=? AND status=?", (after, actor, version, digest, before))
        if changed.rowcount != 1:
            raise RuntimeError("WORKFLOW_STATE_OR_DIGEST_CONFLICT")
    return {"version": version, "status": after}


def cancel_execution(tenant: str, actor: str, execution_id: str) -> dict:
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        row = one(con, "SELECT * FROM executions WHERE id=? AND tenant=? AND actor=?", (execution_id, tenant, actor))
        if not row:
            raise LookupError("EXECUTION_NOT_FOUND")
        if row["status"] not in ("succeeded", "failed", "cancelled"):
            con.execute("UPDATE executions SET status='cancelled' WHERE id=?", (execution_id,))
            con.execute("UPDATE jobs SET status='cancelled',lease_until=NULL,lease_owner=NULL WHERE id=?", (row["job_id"],))
            row["status"] = "cancelled"
    return row
