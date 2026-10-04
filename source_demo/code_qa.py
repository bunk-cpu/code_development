"""内部问答复用同快照事实；引用校验后持久化，不经过模块人审图。"""
from __future__ import annotations

import json
import re
import secrets

from . import agent_tools, java_source, model
from .jobs import enqueue
from .knowledge import redact
from .source_agent import SYSTEM, Claim, validate_draft
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from .store import db, many, now, one, pack

TERMS = {"订单": "order", "导出": "export", "统计": "summary", "权限": "authorize policy permission", "校验": "guard require validate", "入口": "controller mapping", "状态": "status", "租户": "tenant", "上限": "limit", "最大": "limit", "数量": "limit", "查询": "query find select", "规则": "guard require validate", "装配": "profile service"}


class AnswerDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: list[Claim] = Field(max_length=20)
    unknowns: list[str] = Field(max_length=20)


def retrieve(snapshot_id: str, question: str) -> dict:
    index = java_source.get_index(snapshot_id)
    terms = set(re.findall(r"[a-zA-Z_][\w.]{2,}", question.lower()))
    terms.update(word for key, values in TERMS.items() if key in question for word in values.split())
    symbols = java_source.symbols(snapshot_id)
    scored = [(sum((1 if t in {"order", "export"} else 4) for t in terms if t in (pack(s) + " " + s["file"]).lower()), s) for s in symbols]
    selected = [s for score, s in sorted(scored, key=lambda pair: (-pair[0], pair[1]["file"], pair[1]["start_line"])) if score][:8]
    by_id = {s["id"]: s for s in symbols}
    selected_ids = {s["id"] for s in selected}
    relations = sorted(java_source.relations(snapshot_id), key=lambda e: (e["kind"] not in {"calls_candidate", "guard", "overrides"}, e["id"]))
    edges, frontier, edge_ids = [], set(selected_ids), set()
    # ponytail: bounded three-hop graph retrieval; add graph-aware reranking for larger frozen evaluation domains.
    for _ in range(3):
        reached = set()
        for edge in relations:
            if edge["source_id"] not in frontier and not (edge["kind"] == "overrides" and edge.get("target_id") in frontier):
                continue
            if edge["id"] not in edge_ids and len(edges) < 60:
                edges.append(edge); edge_ids.add(edge["id"])
                reached |= {sid for sid in (edge["source_id"], edge.get("target_id")) if sid in by_id}
        frontier = set(sorted(reached - selected_ids)[:max(0, 30 - len(selected_ids))])
        selected_ids |= frontier
    selected = [by_id[sid] for sid in sorted(selected_ids)]
    evidence = {}
    all_modules = [m["id"] for m in index["manifest"]["modules"]]
    scope = {"repository_id": index["repository_id"], "snapshot_id": snapshot_id,
             "build_context_id": index["build_context_id"], "module_id": all_modules[0], "allowed_module_ids": all_modules}
    for eid in [s["evidence_id"] for s in selected] + [e["id"] for e in edges]:
        if ev := agent_tools.read_evidence(scope, eid):
            evidence[eid] = ev
    return {"scope": scope, "evidence": evidence, "relations": edges, "coverage": index["report"]}


def answer(snapshot_id: str, question: str, use_model: bool = True) -> dict:
    question = redact(question)
    packet = retrieve(snapshot_id, question)
    citations = [{"id": e["id"], "file": e["file"], "line": e["line"], "text": e["text"], "locator": e["locator"]} for e in packet["evidence"].values()]
    result = {"status": "partial" if citations else "insufficient_evidence", "snapshot_id": snapshot_id,
              "citations": citations, "claims": [], "unknowns": packet["coverage"]["known_dynamic_gaps"],
              "deployment_status": "not_verified", "coverage": packet["coverage"],
              "answer": "以下是固定版本的静态证据；实际部署、角色权限及动态派发尚未核验。" if citations else "当前固定版本未找到足够证据；不能据此断言功能不存在。"}
    if not citations or not use_model:
        return result
    try:
        aliases = {f"E{i:03d}": eid for i, eid in enumerate(sorted(packet["evidence"]), 1)}
        by_evidence = {eid: alias for alias, eid in aliases.items()}
        data = {"question": question, "scope": packet["scope"],
                "schema": AnswerDraft.model_json_schema(), "allowed_evidence_ids": list(aliases),
                "evidence": [{**{k: e[k] for k in ("kind", "file", "line", "content", "module")}, "id": by_evidence[e["id"]]} for e in packet["evidence"].values()], "coverage": packet["coverage"]}
        repair_used = False
        for attempt in range(2):
            try:
                raw, metadata = model.structured(SYSTEM + " 根据问题及 schema 生成最多8条直接相关的源码主张。只输出 claims 和 unknowns。claims 只写有源码证据的静态事实（basis=static）或明确推断（basis=inferred）；未核验部署、运行时派发、环境、租户授权等限制全部写入 unknowns，不写成 runtime 主张。每条 claim 至少引用1个 allowed_evidence_ids 中的真实ID。coverage/known_dynamic_gaps 等字段名是元数据，不是证据ID。kind/basis 必须使用 Schema 枚举。禁止把不确定性改写成肯定事实。", data)
            except model.ModelError as exc:
                if str(exc) != "MODEL_INVALID_JSON" or attempt:
                    raise
                data["repair_errors"] = ["上次响应不是合法 JSON 对象；仅返回符合给定 schema 的 JSON，不添加说明、代码围栏和额外字段。"]
                repair_used = True
                continue
            previous_raw = raw
            extras = set(raw) - {"claims", "unknowns"}
            if extras and not repair_used:
                # One bounded schema repair: discard provider metadata; claims and references are never invented or rewritten.
                raw = {k: v for k, v in raw.items() if k in {"claims", "unknowns"}}
                result["schema_repair"] = {"discarded_fields": sorted(extras), "claims_changed": False}
                repair_used = True
            raw = json.loads(pack(raw))
            for claim in raw.get("claims", []) if isinstance(raw.get("claims"), list) else []:
                if isinstance(claim, dict) and isinstance(claim.get("evidence_ids"), list):
                    claim["evidence_ids"] = [aliases.get(eid, eid) if isinstance(eid, str) else eid for eid in claim["evidence_ids"]]
            try:
                validated = AnswerDraft.model_validate(raw)
                raw = validated.model_dump()
                draft = {"module_id": packet["scope"]["module_id"], "snapshot_id": snapshot_id,
                         "build_context_id": packet["scope"]["build_context_id"], "summary_claim_ids": [],
                         "claims": raw["claims"], "feature_candidates": [], "open_questions": raw["unknowns"]}
                report = validate_draft(draft, packet["scope"], packet["evidence"])
            except ValidationError as exc:
                report = {"valid": False, "hard_errors": [], "errors": [str(e["loc"]) + ":" + e["type"] for e in exc.errors()]}
            result["validation"] = report
            if report["valid"]:
                break
            if attempt or repair_used or report["hard_errors"]:
                raise model.ModelError("ANSWER_EVIDENCE_VALIDATION_FAILED")
            data.update(previous_draft=previous_raw, repair_errors=report["errors"])
            repair_used = True
        metadata["evidence_alias_hash"] = java_source.digest(aliases)
        supported = [c["text"] for c in draft["claims"]]
        result.update(answer="\n".join(supported) if supported else result["answer"], claims=draft["claims"],
                      unknowns=result["unknowns"] + raw.get("unknowns", []), model=metadata, validation=report)
    except model.ModelError as exc:
        result.update(model_error=str(exc), degraded=True)
    return result


def record_sync(snapshot_id: str, question: str, actor: str, result: dict) -> dict:
    answer_id = "answer-" + secrets.token_hex(8)
    with db() as con:
        job = enqueue("code_answer", "internal", actor, {"answer_id": answer_id}, con)
        stored = {**result, "answer_id": answer_id, "job_id": job["id"]}
        con.execute("UPDATE jobs SET status='succeeded',result=? WHERE id=?", (pack(stored), job["id"]))
        con.execute("INSERT INTO code_answers VALUES (?,?,?,?,?,?,?,?,?)", (answer_id, actor, snapshot_id, redact(question), pack(stored), job["id"], "inline:" + answer_id, java_source.digest([snapshot_id, question]), now()))
    return stored


def create(snapshot_id: str, question: str, actor: str, key: str) -> dict:
    java_source.get_index(snapshot_id)
    if not key or len(key) > 100:
        raise ValueError("IDEMPOTENCY_KEY_REQUIRED")
    request_hash = java_source.digest([snapshot_id, question])
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        row = one(con, "SELECT * FROM code_answers WHERE actor=? AND idempotency_key=?", (actor, key))
        if row:
            if row["request_hash"] != request_hash:
                raise RuntimeError("IDEMPOTENCY_CONFLICT")
            return {"answer_id": row["id"], "job_id": row["job_id"], "status": "existing"}
        answer_id = "answer-" + secrets.token_hex(8)
        job = enqueue("code_answer", "internal", actor, {"answer_id": answer_id}, con)
        con.execute("INSERT INTO code_answers VALUES (?,?,?,?,?,?,?,?,?)",
                    (answer_id, actor, snapshot_id, redact(question), None, job["id"], key, request_hash, now()))
    return {"answer_id": answer_id, "job_id": job["id"], "status": "queued", "poll_url": "/internal/code-answers/" + answer_id}


def answer_job(job: dict) -> dict:
    payload = json.loads(job["payload"])
    with db() as con:
        row = one(con, "SELECT * FROM code_answers WHERE id=?", (payload["answer_id"],))
    from .platform import authorize_snapshot
    authorize_snapshot(row["snapshot_id"], job["actor"])
    return answer(row["snapshot_id"], row["question"])


def get(answer_id: str) -> dict:
    with db() as con:
        row = one(con, "SELECT * FROM code_answers WHERE id=?", (answer_id,))
    if not row:
        raise LookupError("ANSWER_NOT_FOUND")
    row["result"] = json.loads(row["result"]) if row["result"] else None
    row["status"] = row["result"]["status"] if row["result"] else "queued"
    return row
