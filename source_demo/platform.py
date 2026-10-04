"""功能登记、部署、运营、评测与签名事件；业务状态由应用决定。"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets

from . import java_source, knowledge
from .jobs import enqueue
from .store import audit, db, many, now, one, pack


def authorize(repository_id: str, actor: str):
    with db() as con:
        acl = one(con, "SELECT allowed FROM repository_acl WHERE repository_id=? AND actor=?", (repository_id, actor))
    if acl and not acl["allowed"]:
        raise PermissionError("REPOSITORY_ACCESS_DENIED")


def authorize_snapshot(snapshot_id: str, actor: str):
    with db() as con:
        index = one(con, "SELECT repository_id FROM code_indexes WHERE snapshot_id=?", (snapshot_id,))
    if index:
        authorize(index["repository_id"], actor)


def seed():
    with db() as con:
        for tenant, release in knowledge.RELEASES.items():
            con.execute("INSERT INTO deployments VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING", (tenant, release, None, None, pack({"order.summary.read": True}), 1))


def deployment(tenant: str) -> dict:
    with db() as con:
        row = one(con, "SELECT * FROM deployments WHERE tenant=?", (tenant,))
    if not row:
        raise LookupError("DEPLOYMENT_NOT_FOUND")
    row["flags"] = json.loads(row["flags"])
    return row


def deploy(tenant: str, release: str, snapshot_id: str | None, flags: dict, expected_revision: int, actor: str) -> dict:
    if tenant not in knowledge.RELEASES or release not in ("v1", "v2") or set(flags) - {"order.summary.read"} or any(type(v) is not bool for v in flags.values()):
        raise ValueError("DEPLOYMENT_INVALID")
    if snapshot_id:
        java_source.get_index(snapshot_id)
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        updated = con.execute("UPDATE deployments SET release=?,snapshot_id=?,flags=?,revision=revision+1 WHERE tenant=? AND revision=?", (release, snapshot_id, pack(flags), tenant, expected_revision))
        if updated.rowcount != 1:
            raise RuntimeError("DEPLOYMENT_CONFLICT")
        audit(actor, "deployment.register", tenant, {"release": release, "snapshot_id": snapshot_id, "flags": flags}, con)
    return deployment(tenant)


def feature_candidates(snapshot_id: str) -> list[dict]:
    with db() as con:
        runs = many(con, "SELECT id,bundle,status FROM agent_runs WHERE snapshot_id=? AND bundle IS NOT NULL", (snapshot_id,))
        features = many(con, "SELECT * FROM features WHERE snapshot_id=?", (snapshot_id,))
    candidates = []
    for run in runs:
        bundle = json.loads(run["bundle"])
        draft = bundle["draft"]
        for feature in draft["feature_candidates"]:
            candidates.append({**feature, "run_id": run["id"], "profile_status": run["status"],
                               "claims": [c for c in draft["claims"] if c["claim_id"] in feature["claim_ids"]]})
    return {"candidates": candidates, "features": [{**f, "claims": json.loads(f["claims"]), "evidence_ids": json.loads(f["evidence_ids"])} for f in features]}


def approve_feature(run_id: str, feature_id: str, title: str, claim_ids: list[str], actor: str):
    if not feature_id or len(feature_id) > 100 or not title or len(title) > 150 or not claim_ids:
        raise ValueError("FEATURE_INVALID")
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        run = one(con, "SELECT * FROM agent_runs WHERE id=?", (run_id,))
        if not run or run["status"] != "COMPLETED" or not run["decision"] or json.loads(run["decision"])["decision"] != "approve":
            raise RuntimeError("PROFILE_NOT_APPROVED")
        bundle = json.loads(run["bundle"])
        by_id = {c["claim_id"]: c for c in bundle["draft"]["claims"]}
        if any(cid not in by_id or by_id[cid]["basis"] != "static" for cid in claim_ids):
            raise ValueError("FEATURE_CLAIMS_NOT_STATIC")
        claims = [by_id[cid] for cid in claim_ids]
        eids = sorted({eid for c in claims for eid in c["evidence_ids"]})
        con.execute("INSERT INTO features VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id,snapshot_id) DO UPDATE SET title=excluded.title,claims=excluded.claims,evidence_ids=excluded.evidence_ids,reviewer=excluded.reviewer",
                    (feature_id, run["snapshot_id"], title, pack(claims), pack(eids), "confirmed_internal", actor, now()))
        audit(actor, "feature.confirm", feature_id, {"run_id": run_id, "snapshot_id": run["snapshot_id"]}, con)
    return {"id": feature_id, "snapshot_id": run["snapshot_id"], "status": "confirmed_internal", "customer_published": False}


def operations() -> dict:
    with db() as con:
        questions = many(con, "SELECT id,redacted,status,topic,feedback,created_at FROM questions ORDER BY created_at DESC LIMIT 200")
        tasks = many(con, "SELECT * FROM operation_tasks ORDER BY created_at DESC")
    return {"questions": questions, "topics": knowledge.topics(), "tasks": tasks,
            "workflow_candidates": [{"topic": t["topic"], "count": t["count"], "risk": "requires_review"} for t in knowledge.topics() if t["count"] >= 3]}


def bind_material(snapshot_id: str, module: str, kind: str, target_id: str, actor: str) -> dict:
    index = java_source.get_index(snapshot_id)
    if module not in {m["id"] for m in index["manifest"]["modules"]} or kind not in ("manual", "test", "legacy_test"):
        raise ValueError("MATERIAL_SCOPE_INVALID")
    with db() as con:
        if kind == "manual":
            target = one(con, "SELECT id,release,title,content,digest,status FROM manual_revisions WHERE id=?", (target_id,))
        elif kind == "test":
            target = one(con, "SELECT * FROM test_runs WHERE id=?", (target_id,))
        else:
            target = one(con, "SELECT id,source_run_id,status,context,result FROM legacy_runs WHERE id=?", (target_id,))
        if not target:
            raise LookupError("MATERIAL_TARGET_NOT_FOUND")
        if kind == "manual" and target["status"] not in ("approved", "published"):
            raise RuntimeError("MATERIAL_NOT_APPROVED")
        if kind != "manual" and target["status"] in ("queued", "running", "submitting", "unknown"):
            raise RuntimeError("MATERIAL_NOT_FINISHED")
        content = pack({"target": target, "source_kind": kind, "commit_sha": index["commit_sha"], "mapping_status": "human_declared", "runtime_verified": False,
                        "note": "仅证明此资产已由审核者映射；同构建、环境、角色和业务实测仍须独立核验"})
        if len(content) > 50000:
            raise ValueError("MATERIAL_SIZE_EXCEEDED")
        content = knowledge.redact(content)
        payload_hash = hashlib.sha256(content.encode()).hexdigest()
        eid = "material-" + java_source.digest([snapshot_id, module, kind, target_id, payload_hash])[:24]
        evidence_kind = "manual" if kind == "manual" else "test"
        con.execute("INSERT INTO analysis_materials VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING", (eid, snapshot_id, module, evidence_kind, target_id, payload_hash, content, actor, now()))
        con.execute("INSERT INTO evidence VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING", (eid, snapshot_id, module, evidence_kind + ":" + target_id, 1, content))
        audit(actor, "source.bind_material", eid, {"kind": kind, "target_id": target_id, "snapshot_id": snapshot_id}, con)
    return {"evidence_id": eid, "mapping_status": "human_declared", "runtime_verified": False}


def operation_task(topic: str, kind: str, owner: str, note: str) -> dict:
    if kind not in ("knowledge_gap", "incorrect_answer", "workflow_candidate"):
        raise ValueError("TASK_KIND_INVALID")
    task_id = "task-" + secrets.token_hex(8)
    with db() as con:
        con.execute("INSERT INTO operation_tasks VALUES (?,?,?,?,?,?,?)", (task_id, topic, kind, "open", owner, knowledge.redact(note), now()))
    return {"id": task_id, "status": "open"}


def git_event(body: bytes, signature: str, event_id: str) -> dict:
    secret = os.getenv("GIT_WEBHOOK_SECRET", "")
    if not secret or not hmac.compare_digest(signature, "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()):
        raise PermissionError("WEBHOOK_SIGNATURE_INVALID")
    if not event_id or len(event_id) > 150:
        raise ValueError("EVENT_ID_INVALID")
    payload = json.loads(body)
    if set(payload) != {"repository_id", "commit_sha"} or not __import__("re").fullmatch(r"[0-9a-f]{40,64}", payload["commit_sha"]):
        raise ValueError("GIT_EVENT_INVALID")
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        old = one(con, "SELECT * FROM git_events WHERE id=?", (event_id,))
        hash_value = hashlib.sha256(body).hexdigest()
        if old:
            if old["payload_hash"] != hash_value:
                raise RuntimeError("EVENT_ID_CONFLICT")
            return {"job_id": old["job_id"], "idempotent": True}
        if not one(con, "SELECT id FROM repositories WHERE id=?", (payload["repository_id"],)):
            raise LookupError("REPOSITORY_NOT_FOUND")
        job = enqueue("repository_index", "internal", "analyst", {"repository_id": payload["repository_id"], "ref": payload["commit_sha"], "analyze_modules": True}, con)
        con.execute("INSERT INTO git_events VALUES (?,?,?,?)", (event_id, hash_value, job["id"], now()))
    return {"job_id": job["id"], "status": "queued"}


def metrics() -> dict:
    with db() as con:
        output = {"jobs": many(con, "SELECT kind,status,COUNT(*) AS count FROM jobs GROUP BY kind,status"),
                "agent_runs": many(con, "SELECT status,COUNT(*) AS count FROM agent_runs GROUP BY status"),
                "source_indexes": many(con, "SELECT status,COUNT(*) AS count FROM code_indexes GROUP BY status"),
                "questions": many(con, "SELECT status,COUNT(*) AS count FROM questions GROUP BY status"),
                "audit_events": one(con, "SELECT COUNT(*) AS count FROM audit_log")["count"]}
        # ponytail: bounded operational sample; export counters to a monitoring backend for lifetime billing and retention.
        calls = [json.loads(r["details"]) for r in many(con, "SELECT details FROM agent_events WHERE stage='model_response' ORDER BY id DESC LIMIT 10000")]
        for row in many(con, "SELECT result FROM code_answers WHERE result IS NOT NULL ORDER BY created_at DESC LIMIT 10000"):
            metadata = json.loads(row["result"]).get("model")
            if isinstance(metadata, dict):
                calls.append(metadata)
        tokens = [c.get("usage", {}).get("total_tokens") for c in calls]
        latencies = sorted(c["latency_ms"] for c in calls if isinstance(c.get("latency_ms"), (int, float)))
        output["model_sample"] = {"calls": len(calls), "known_tokens": sum(t for t in tokens if type(t) is int), "unknown_usage_calls": sum(type(t) is not int for t in tokens), "latency_p95_ms": latencies[int((len(latencies) - 1) * .95)] if latencies else None, "sample_limit_per_path": 10000}
    return output
