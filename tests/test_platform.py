"""跨服务场景回归：真实 JDT，离线模型协议，审核/租约/权限负例。"""
from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
from pathlib import Path

import pytest

from test_demo import users
from scripts.prepare_java_fixture import prepare
from source_demo import agent_tools, code_qa, java_source, jobs, knowledge, manuals, model, platform, source_agent, store, workflow


@pytest.fixture
def indexed(users, tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_ALLOWED_ROOTS", str(tmp_path))
    fixture = prepare(tmp_path / "repository", git_history=True)
    repo = java_source.register("multi-module", fixture["root"])
    base = java_source.index_repository(repo["id"], "base")
    change = java_source.index_repository(repo["id"], "change")
    return repo, base, change


def test_java_fixture_can_be_plain_directory_and_preserves_edits(users, tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_ALLOWED_ROOTS", str(tmp_path))
    fixture = prepare(tmp_path / "sources", git_history=False)
    root = Path(fixture["root"])
    assert not (root / ".git").exists()
    request = root / "api/src/main/java/demo/api/ExportRequest.java"
    request.write_text(request.read_text() + "\n// local edit\n")
    assert prepare(root, git_history=False) == fixture and request.read_text().endswith("// local edit\n")
    assert not (root / ".git").exists()
    repo = java_source.register("plain-sources", fixture["root"])
    index = java_source.index_repository(repo["id"])
    assert index["commit_sha"].startswith("tree-")
    assert index["report"]["source_files_total"] == 7


@pytest.fixture
def fake_model(monkeypatch):
    calls = []
    def complete(messages, *, tools=None, json_mode=False, **kwargs):
        calls.append(messages)
        if tools:
            if not any(m["role"] == "tool" for m in messages):
                message = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "search-1", "type": "function", "function": {"name": "search_code_facts", "arguments": '{"query":"","limit":20}'}},
                    {"id": "invalid-1", "type": "function", "function": {"name": "read_secret", "arguments": '{"path":"/root/testpilot-plus/.env"}'}},
                ]}
            else:
                assert {m["tool_call_id"] for m in messages if m["role"] == "tool"} >= {"search-1", "invalid-1"}
                message = {"role": "assistant", "content": "证据足够形成有限静态画像"}
        else:
            data = json.loads(messages[-1]["content"])
            ev = next(e for e in data["evidence"] if e["kind"] == "source")
            draft = {"module_id": data["scope"]["module_id"], "snapshot_id": data["scope"]["snapshot_id"],
                     "build_context_id": data["scope"]["build_context_id"], "summary_claim_ids": ["entry-1"],
                     "claims": [{"claim_id": "entry-1", "text": "固定源码中存在该模块的定义；运行配置尚未核验。", "kind": "technical_role", "basis": "static", "evidence_ids": [ev["id"]], "conditions": ["固定源码快照"], "unknown_reason": None}],
                     "feature_candidates": [{"feature_id_candidate": "order.export", "title": "订单导出", "claim_ids": ["entry-1"]}],
                     "open_questions": ["实际部署和 Spring 装配未核验"]}
            message = {"role": "assistant", "content": json.dumps(draft)}
        return {"message": message, "usage": {"total_tokens": 100}, "model": "fixture", "request_hash": "test"}
    monkeypatch.setattr(model, "complete", complete)
    return calls


def test_java_symbols_relations_resources_and_change(indexed, tmp_path, monkeypatch):
    repo, base, change = indexed
    assert base["report"]["source_files_total"] == 7
    assert base["status"] == "PARTIAL"  # No classpath: framework types are honestly unresolved.
    assert "MissingAuditClient" in str(base["report"]["problems"])
    symbols = java_source.symbols(base["snapshot_id"])
    overloaded = [s for s in symbols if s["name"] == "create" and "ExportService.java" in s["file"] and s["kind"] == "method"]
    assert len(overloaded) == 2 and len({s["id"] for s in overloaded}) == 2
    assert {"sql", "config", "mapper"} <= {s["kind"] for s in symbols}
    edges = java_source.relations(base["snapshot_id"])
    assert any(e["kind"] == "overrides" and e["target_id"] for e in edges)
    assert any(e["kind"] == "guard" and "5000" in e["condition"] for e in edges)
    assert all(not e.get("runtime_verified") for e in edges)
    assert java_source.index_repository(repo["id"], "base")["snapshot_id"] == base["snapshot_id"]
    impact = java_source.impact(base["snapshot_id"], change["snapshot_id"])
    assert {"common", "orders", "api"} <= set(impact["affected_modules"])
    assert any("ExportPolicy.java" in c["path"] for c in impact["changes"])
    assert "should-never-reach-the-model" not in json.dumps(java_source.load_tree(Path(repo["root"]), "base")[1])
    manifest = java_source.inventory({"pom.xml": '<project xmlns="http://maven.apache.org/POM/4.0.0"><artifactId>x</artifactId><profiles><profile><id>prod</id></profile></profiles></project>'}, {"java_release": "17", "classpath": []})
    assert manifest["modules"][0]["profiles"] == ["prod"]
    large = tmp_path / "large"
    large.mkdir(); (large / "TooLarge.java").write_bytes(b"x" * 1_000_001)
    with pytest.raises(ValueError, match="SOURCE_FILE_SIZE_BUDGET_EXCEEDED"):
        java_source.load_tree(large, "HEAD")
    monkeypatch.setenv("SOURCE_MAX_FILES", "1")
    with pytest.raises(ValueError, match="SOURCE_SIZE_BUDGET_EXCEEDED"):
        java_source.load_tree(Path(repo["root"]), "base")


def test_duplicate_java_definitions_and_revoked_index_worker(indexed, monkeypatch):
    repo, _, _ = indexed
    root = Path(repo["root"])
    original = root / "common/src/main/java/demo/common/ExportPolicy.java"
    duplicate = root / "orders/src/main/java/demo/common/ExportPolicy.java"
    duplicate.parent.mkdir(parents=True)
    duplicate.write_text(original.read_text())
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "duplicate definition"], check=True, capture_output=True)
    index = java_source.index_repository(repo["id"])
    definitions = [s for s in java_source.symbols(index["snapshot_id"]) if s["kind"] == "class" and s["name"] == "ExportPolicy"]
    assert len(definitions) == 2 and len({s["id"] for s in definitions}) == 2
    assert index["status"] == "PARTIAL" and any(s["binding_status"] == "UNRESOLVED" for s in definitions)
    ambiguous = [e for e in java_source.relations(index["snapshot_id"]) if e["resolution_status"] == "AMBIGUOUS"]
    assert all(not e["target_id"] and not e["external_target"] and len(e["target_candidate_ids"]) == 2 for e in ambiguous)
    unresolved_ids = {s["id"] for s in definitions if s["binding_status"] == "UNRESOLVED"}
    assert not any(e["resolution_status"] == "RESOLVED" and e["target_id"] in unresolved_ids for e in java_source.relations(index["snapshot_id"]))
    job = jobs.enqueue("repository_index", "internal", "analyst", {"repository_id": repo["id"]})
    worker = jobs.claim("index-worker", ["repository_index"])
    assert worker["id"] == job["id"]
    with store.db() as con:
        con.execute("INSERT INTO repository_acl VALUES (?,?,?)", (repo["id"], "analyst", False))
    monkeypatch.setattr(java_source, "parse_java", lambda *args: pytest.fail("revoked worker parsed source"))
    with pytest.raises(PermissionError, match="REPOSITORY_ACCESS_DENIED"):
        java_source.index_repository(repo["id"], job=worker)
    with store.db() as con:
        con.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (job["id"],))
    with pytest.raises(ValueError, match="LEASE_LOST"):
        java_source.index_repository(repo["id"], job=worker)


def test_tools_scope_pagination_and_fake_evidence(indexed, monkeypatch):
    _, base, change = indexed
    scope = agent_tools.scope_for(base["snapshot_id"], "api")
    result = agent_tools.execute(scope, "search_code_facts", {"limit": 1})
    assert result["ok"] and result["truncated"] and result["next_cursor"]
    second = agent_tools.execute(scope, "search_code_facts", {"limit": 1, "cursor": result["next_cursor"]})
    assert second["items"][0]["id"] != result["items"][0]["id"]
    wrong_scope = agent_tools.scope_for(change["snapshot_id"], "api")
    assert not agent_tools.execute(wrong_scope, "search_code_facts", {"limit": 1, "cursor": result["next_cursor"]})["ok"]
    assert not agent_tools.execute(scope, "get_symbol_evidence", {"symbol_id": "fake", "path": "/root/.env"})["ok"]
    assert not agent_tools.execute(scope, "shell", {"command": "cat .env"})["ok"]
    fake = {"module_id": "api", "snapshot_id": base["snapshot_id"], "build_context_id": scope["build_context_id"],
            "summary_claim_ids": ["c"], "claims": [{"claim_id": "c", "text": "伪造", "kind": "rule", "basis": "static", "evidence_ids": ["fake"]}]}
    assert source_agent.validate_draft(fake, scope, {})["hard_errors"]
    def response(system, data):
        return {"claims": [{"claim_id": "c1", "text": "固定版本的静态引用示例", "kind": "technical_role", "basis": "static", "evidence_ids": [data["allowed_evidence_ids"][0]]}], "unknowns": ["运行未验证"], "coverage": "provider metadata"}, {"model": "fixture"}
    monkeypatch.setattr(model, "structured", response)
    answer = code_qa.answer(base["snapshot_id"], "订单导出最大允许数量")
    assert answer["validation"]["valid"] and answer["schema_repair"]["discarded_fields"] == ["coverage"]
    assert answer["claims"][0]["evidence_ids"][0] in {c["id"] for c in answer["citations"]}
    monkeypatch.setattr(model, "structured", lambda *args: ({"claims": [{"claim_id": "c1", "text": "伪造来源", "kind": "rule", "basis": "static", "evidence_ids": ["E999"]}], "unknowns": []}, {}))
    rejected = code_qa.answer(base["snapshot_id"], "订单导出最大允许数量")
    assert rejected["degraded"] and not rejected["claims"] and rejected["validation"]["hard_errors"]


def test_agent_checkpoint_approval_recovery_and_no_customer_publish(indexed, users, fake_model):
    _, base, _ = indexed
    before = users["customer-a"].get("/customer/help").json()["knowledge_snapshot_id"]
    created = users["analyst"].post("/internal/source/analysis-runs", json={"snapshot_id": base["snapshot_id"], "module_id": "api"}).json()
    result = jobs.process_once()
    assert result["result"]["status"] == "WAITING_REVIEW", result
    detail = users["analyst"].get("/internal/source/analysis-runs/" + created["run_id"]).json()
    assert detail["bundle"]["validation"]["semantic_support"] == "requires_human_review"
    assert detail["budget"]["deadline"] == 0
    assert any(e["details"].get("tool") == "read_secret" and not e["details"]["ok"] for e in detail["events"])
    body = {"digest": detail["bundle_hash"], "decision": "approve", "comment": "已核对静态主张"}
    path = f"/internal/source/analysis-runs/{created['run_id']}/review"
    assert users["analyst"].post(path, json=body).status_code == 403
    assert users["editor"].post(path, json={**body, "digest": "bad"}).status_code == 409
    assert users["editor"].post(path, json=body).status_code == 200
    assert users["editor"].post(path, json=body).json()["idempotent"]
    assert jobs.process_once("restarted-worker")["result"]["profile_status"] == "APPROVED"
    assert users["customer-a"].get("/customer/help").json()["knowledge_snapshot_id"] == before
    response = users["editor"].post("/internal/features/confirm", json={"run_id": created["run_id"], "feature_id": "order.export", "title": "导出", "claim_ids": ["entry-1"]})
    assert response.status_code == 200 and not response.json()["customer_published"]
    with store.db() as con:
        manual = store.one(con, "SELECT id FROM manual_revisions WHERE status='approved' ORDER BY id LIMIT 1")
    mapping = users["editor"].post("/internal/source/material-bindings", json={"snapshot_id": base["snapshot_id"], "module_id": "api", "kind": "manual", "target_id": str(manual["id"])}).json()
    materials = agent_tools.execute(agent_tools.scope_for(base["snapshot_id"], "api"), "get_tests_and_manual", {})
    assert materials["evidence"][0]["id"] == mapping["evidence_id"] and not materials["evidence"][0]["runtime_verified"]


def test_agent_revocation_cancel_and_budget(indexed, users, fake_model):
    repo, base, _ = indexed
    run = source_agent.create(base["snapshot_id"], "api", "analyst")
    users["editor"].put(f"/internal/repositories/{repo['id']}/access", json={"actor": "analyst", "allowed": False})
    result = jobs.process_once()
    assert result["result"]["stop_reason"] == "ACCESS_DENIED"
    assert users["analyst"].get(f"/internal/source/{base['snapshot_id']}/symbols").status_code == 403
    users["editor"].put(f"/internal/repositories/{repo['id']}/access", json={"actor": "analyst", "allowed": True})
    cancelled = source_agent.create(base["snapshot_id"], "api", "analyst")
    assert source_agent.cancel(cancelled["run_id"])["status"] == "CANCELLED"
    assert jobs.process_once() is None
    limited = source_agent.create(base["snapshot_id"], "api", "analyst")
    with store.db() as con:
        row = store.one(con, "SELECT budget FROM agent_runs WHERE id=?", (limited["run_id"],))
        budget = json.loads(row["budget"]); budget["max_tokens"] = 100
        con.execute("UPDATE agent_runs SET budget=? WHERE id=?", (store.pack(budget), limited["run_id"]))
    assert jobs.process_once()["result"]["status"] == "INCOMPLETE"
    assert not fake_model


def test_manual_import_patch_conflict_review_and_rollback(users):
    analyst, editor, customer = users["analyst"], users["editor"], users["customer-a"]
    imported = analyst.post("/internal/manuals/import", json={"release": "v1", "name": "legacy.md", "content": "# 示例帮助\n\n旧规则"}).json()
    assert not analyst.post("/internal/manuals/import", json={"release": "v1", "name": "legacy.md", "content": "# 示例帮助\n\n旧规则"}).json()["created"]
    baseline = analyst.get(f"/internal/manuals/{imported['revisions'][0]}/blocks").json()
    block = baseline["blocks"][-1]
    patch = analyst.post("/internal/manuals/patches", json={"baseline_id": baseline["revision"]["id"], "baseline_hash": baseline["revision"]["digest"],
                          "operations": [{"operation": "replace", "block_id": block["block_id"], "expected_hash": block["hash"], "new_markdown": "新规则"}]}).json()
    assert "-旧规则" in patch["diff"] and "+新规则" in patch["diff"]
    revision = analyst.post(f"/internal/manuals/patches/{patch['patch_id']}/apply").json()
    assert analyst.post(f"/internal/manuals/patches/{patch['patch_id']}/apply").status_code == 409
    assert editor.post(f"/internal/manuals/{revision['id']}/review", json={"digest": "wrong", "decision": "approve"}).status_code == 409
    assert editor.post(f"/internal/manuals/{revision['id']}/review", json={"digest": revision["digest"], "decision": "approve"}).status_code == 200
    old_head = customer.get("/customer/help").json()["knowledge_snapshot_id"]
    new_head = editor.post("/internal/knowledge/v1/publish").json()["knowledge_snapshot_id"]
    assert len(customer.get("/customer/help").json()["pages"]) == 3
    assert editor.post("/internal/knowledge/v1/rollback", json={"snapshot_id": old_head, "expected_head": new_head}).status_code == 200
    assert "新规则" not in str(customer.get("/customer/help").json())
    editor.put("/internal/manuals/visibility", json={"tenant": "tenant-a", "page_id": "order-summary", "allowed": False})
    assert customer.get("/customer/help/order-summary").status_code == 404
    assert customer.post("/customer/ask", json={"question": "订单统计支持哪些状态"}).json()["status"] == "insufficient_evidence"


def test_workflow_versions_revocation_and_cancellation(users):
    analyst, editor, customer = users["analyst"], users["editor"], users["customer-a"]
    spec = {"workflow_id": "order.summary.read", "version": "0.2.0", "risk": "R0_read_only", "description": "订单统计", "template_id": "api.order_summary.read", "releases": ["v1", "v2"], "input_schema": {"status": ["all", "pending", "completed"], "date_from": "date", "date_to": "date"}}
    row = analyst.post("/internal/workflow-versions", json=spec).json()
    assert editor.post("/internal/workflow-versions/0.2.0/publish", json={"digest": row["digest"]}).status_code == 409
    assert analyst.post("/internal/workflow-versions/0.2.0/validate").json()["passed"]
    assert editor.post("/internal/workflow-versions/0.2.0/approve", json={"digest": row["digest"]}).status_code == 200
    assert editor.post("/internal/workflow-versions/0.2.0/publish", json={"digest": row["digest"]}).status_code == 200
    parameters = {"status": "pending", "date_from": "2026-09-01", "date_to": "2026-09-07"}
    preparation = customer.post("/customer/workflows/order.summary.read/prepare", json={"parameters": parameters}).json()
    editor.post("/internal/deployments", json={"tenant": "tenant-a", "release": "v1", "flags": {"order.summary.read": False}, "expected_revision": 1})
    confirm = {"prepared_id": preparation["prepared_id"], "confirmed_preview_digest": preparation["preview_digest"], "confirmation": True}
    assert customer.post("/customer/executions", json=confirm, headers={"Idempotency-Key": "revoked"}).status_code == 409
    assert not customer.get("/customer/workflows").json()
    editor.post("/internal/deployments", json={"tenant": "tenant-a", "release": "v1", "flags": {"order.summary.read": True}, "expected_revision": 2})
    execution = customer.post("/customer/executions", json=confirm, headers={"Idempotency-Key": "cancel"}).json()
    assert customer.post(f"/customer/executions/{execution['id']}/cancel").json()["status"] == "cancelled"
    assert jobs.process_once() is None


def test_git_signature_idempotency_and_feedback(indexed, users, monkeypatch):
    repo, _, change = indexed
    monkeypatch.setenv("GIT_WEBHOOK_SECRET", "test-signing-secret")
    payload = store.pack({"repository_id": repo["id"], "commit_sha": change["commit_sha"]}).encode()
    signature = "sha256=" + hmac.new(b"test-signing-secret", payload, hashlib.sha256).hexdigest()
    client = users["analyst"]
    assert client.post("/integrations/git/events", content=payload, headers={"X-Event-ID": "event-1"}).status_code == 403
    headers = {"X-Event-ID": "event-1", "X-Hub-Signature-256": signature}
    first = client.post("/integrations/git/events", content=payload, headers=headers).json()
    assert client.post("/integrations/git/events", content=payload, headers=headers).json()["job_id"] == first["job_id"]
    assert jobs.process_once()["accepted"]
    answer = users["customer-a"].post("/customer/ask", json={"question": "订单统计规则是什么"}).json()
    body = {"kind": "incorrect", "comment": "token=private-value"}
    assert users["customer-b"].post(f"/customer/questions/{answer['question_id']}/feedback", json=body).status_code == 404
    assert users["customer-a"].post(f"/customer/questions/{answer['question_id']}/feedback", json=body).status_code == 200
    assert "private-value" not in str(client.get("/internal/operations").json())
