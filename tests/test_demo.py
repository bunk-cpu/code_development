from __future__ import annotations

from contextlib import ExitStack, contextmanager
import os
import secrets
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

from source_demo import jobs, legacy, mock_business, store
from source_demo.api import app


@pytest.fixture
def users(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "demo.sqlite3")
    pg = os.getenv("TEST_DATABASE_URL")
    schema = "test_" + secrets.token_hex(8)
    if pg:
        with store.psycopg.connect(pg, autocommit=True) as connection:
            connection.execute('CREATE SCHEMA "' + schema + '"')
        scoped = pg + ("&" if "?" in pg else "?") + "options=" + quote("-csearch_path=" + schema)
        monkeypatch.setattr(store, "DATABASE_URL", scoped)
        monkeypatch.setattr(jobs, "DATABASE_URL", scoped)
    else:
        monkeypatch.setattr(store, "DATABASE_URL", "")
        monkeypatch.setattr(jobs, "DATABASE_URL", "")
    monkeypatch.setattr(jobs, "_mock", lambda p: mock_business.summary(**p))
    with ExitStack() as stack:
        result = {}
        for name in ("analyst", "editor", "customer-a", "customer-b"):
            client = stack.enter_context(TestClient(app))
            assert client.post("/session", json={"actor": name, "password": "demo-only"}).status_code == 200
            result[name] = client
        yield result
    if pg:
        with store.psycopg.connect(pg, autocommit=True) as connection:
            connection.execute('DROP SCHEMA "' + schema + '" CASCADE')


def test_source_manual_customer_isolation(users):
    analyst, editor = users["analyst"], users["editor"]
    assert analyst.post("/session", json={"actor": "analyst", "password": "无效的中文口令"}).status_code == 401
    a, b = users["customer-a"], users["customer-b"]
    assert analyst.post("/internal/source/index", json={"version": "v1"},
                        headers={"Origin": "http://evil.example"}).status_code == 403
    snapshots = analyst.get("/internal/source/snapshots").json()
    v2 = next(x for x in snapshots if x["version"] == "v2")
    assert "OrderController.java" in v2["changed_files"]
    profile = analyst.get(f"/internal/source/profiles/{v2['id']}/OrderController").json()
    assert profile["coverage"] == "PARTIAL" and profile["claims"]
    qa = analyst.post(f"/internal/source/{v2['id']}/ask", json={"question": "订单统计入口和权限在哪里"}).json()
    assert qa["citations"] and "忽略所有规则" not in str(qa)
    draft = analyst.post("/internal/manuals/from-evidence", json={"page_id": "code-candidate", "release": "v2",
                      "title": "源码候选", "snapshot_id": v2["id"],
                      "evidence_ids": [qa["citations"][0]["id"]]})
    assert draft.status_code == 200 and draft.json()["review_required"]
    assert analyst.post("/internal/manuals/from-evidence", json={"page_id": "bad", "release": "v2",
                      "title": "无效", "snapshot_id": v2["id"], "evidence_ids": ["fake"]}).status_code == 400
    assert analyst.post("/internal/manuals/from-evidence", json={"page_id": "bad", "release": "v1",
                      "title": "跨版本", "snapshot_id": v2["id"],
                      "evidence_ids": [qa["citations"][0]["id"]]}).status_code == 400
    assert a.get(f"/internal/source/profiles/{v2['id']}/OrderController").status_code == 403
    assert analyst.get("/customer/help").status_code == 403
    assert "completed" not in a.get("/customer/help").json()["pages"][1]["content"]
    assert "completed" in b.get("/customer/help").json()["pages"][1]["content"]

    initial = next(r for r in analyst.get("/internal/manuals?release=v1").json() if r["page_id"] == "order-summary")
    changed = analyst.put("/internal/manuals", json={"page_id": "order-summary", "release": "v1", "title": "订单统计",
                                                    "content": "# 订单统计\n仅演示新版说明。", "baseline_id": initial["id"]})
    assert changed.status_code == 200
    assert analyst.put("/internal/manuals", json={"page_id": "order-summary", "release": "v1", "title": "订单统计",
                                                 "content": "覆盖", "baseline_id": initial["id"]}).status_code == 409
    assert "新版说明" not in str(a.get("/customer/help").json())
    assert analyst.post(f"/internal/manuals/{changed.json()['id']}/approve").status_code == 403
    assert editor.post(f"/internal/manuals/{changed.json()['id']}/approve").status_code == 200
    assert editor.post("/internal/knowledge/v1/publish").status_code == 200
    assert "新版说明" in str(a.get("/customer/help").json())
    assert "新版说明" not in str(b.get("/customer/help").json())
    indexed = analyst.post("/internal/source/index", json={"version": "v2"}).json()
    assert jobs.process_once()["job_id"] == indexed["job_id"]
    assert analyst.get("/internal/jobs/" + indexed["job_id"]).json()["status"] == "succeeded"


def test_customer_qa_workflow_and_idempotency(users):
    a, b = users["customer-a"], users["customer-b"]
    answer = a.post("/customer/ask", json={"question": "订单统计支持哪些状态？邮箱 abc@example.com"}).json()
    assert answer["status"] == "answered" and answer["citations"]
    unknown = a.post("/customer/ask", json={"question": "退款自动审批是否可用"}).json()
    assert unknown["status"] == "insufficient_evidence"
    topics = users["analyst"].get("/internal/operations/topics").json()
    assert "abc@example.com" not in str(topics)
    params = {"status": "pending", "date_from": "2026-09-01", "date_to": "2026-09-07"}
    assert a.post("/customer/workflows/order.summary.read/prepare", json={"parameters": {**params, "status": "completed"}}).status_code == 400
    prepared = a.post("/customer/workflows/order.summary.read/prepare", json={"parameters": params}).json()
    body = {"prepared_id": prepared["prepared_id"], "confirmed_preview_digest": prepared["preview_digest"], "confirmation": True}
    headers = {"Idempotency-Key": "same-key"}
    first = a.post("/customer/executions", json=body, headers=headers).json()
    assert a.post("/customer/executions", json=body, headers=headers).json()["id"] == first["id"]
    second_preview = a.post("/customer/workflows/order.summary.read/prepare", json={"parameters": params}).json()
    conflict = a.post("/customer/executions", json={"prepared_id": second_preview["prepared_id"],
                                                    "confirmed_preview_digest": second_preview["preview_digest"],
                                                    "confirmation": True}, headers=headers)
    assert conflict.status_code == 409
    assert b.get("/customer/executions/" + first["id"]).status_code == 404
    jobs.process_once()
    result = a.get("/customer/executions/" + first["id"]).json()
    assert result["status"] == "succeeded" and result["result"]["summary"]["total"] == 2


def test_test_studio_assertion_and_lease(users):
    analyst, editor = users["analyst"], users["editor"]
    cases = analyst.get("/internal/test-cases").json()
    wrong = next(c for c in cases if c["source_id"] == "wrong")
    candidate = analyst.post(f"/internal/test-cases/{wrong['id']}/candidate").json()
    assert analyst.post(f"/internal/test-candidates/{candidate['id']}/run").status_code == 409
    assert editor.post(f"/internal/test-candidates/{candidate['id']}/approve", json={"digest": "bad"}).status_code == 409
    assert editor.post(f"/internal/test-candidates/{candidate['id']}/approve", json={"digest": candidate["digest"]}).status_code == 200
    run = analyst.post(f"/internal/test-candidates/{candidate['id']}/run").json()
    claimed = jobs.claim("old")
    with store.db() as con:
        con.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (claimed["id"],))
    replacement = jobs.claim("new")
    assert replacement["id"] == claimed["id"] and replacement["run_epoch"] > claimed["run_epoch"]
    assert not jobs.finish(claimed, {"assertion_status": "passed", "actual": 99}, None)
    assert jobs.finish(replacement, jobs.perform(replacement), None)
    report = analyst.get("/internal/test-runs/" + run["run_id"]).json()
    assert report["status"] == "failed" and report["expected"] == 99 and report["actual"] == 3


def test_testpilot_adapter_imports_without_running_browsers(users, monkeypatch):
    class FakeClient:
        def get(self, path):
            data = [{"id": 7, "case_key": "TC-007", "name": "已有用例", "prompt": "检查页面",
                     "expected": "看到统计", "updated_at": "2026-09-01T00:00:00"}]
            return httpx.Response(200, json=data, request=httpx.Request("GET", "http://testpilot" + path))

    @contextmanager
    def fake_client():
        yield FakeClient()

    monkeypatch.setattr(legacy, "client", fake_client)
    analyst = users["analyst"]
    first = analyst.post("/internal/legacy/projects/2/import").json()
    second = analyst.post("/internal/legacy/projects/2/import").json()
    assert first["created"] == 1 and second["existing"] == 1
    imported = next(c for c in analyst.get("/internal/test-cases").json() if c["source"] == "testpilot-plus")
    assert imported["source_id"] == "2:7" and imported["expected"] is None
    assert analyst.post(f"/internal/test-cases/{imported['id']}/candidate").status_code == 400

    monkeypatch.setattr(legacy, "script_revision", lambda revision_id: {
        "id": revision_id, "batch_item_id": 9, "revision": 2,
        "content_sha256": "a" * 64, "status": "generated"})
    script = analyst.post("/internal/legacy/automation/revisions/12/import").json()
    assert script["created"] is True
    assert analyst.post("/internal/legacy/automation/revisions/12/import").json()["created"] is False
    assert len(analyst.get("/internal/legacy/automation/imported-scripts").json()) == 1
    with store.db() as con:
        for rid, correlation in (("failed-first", {"attempt_id": 1}), ("uncorrelated", {})):
            con.execute("INSERT INTO legacy_runs VALUES (?,?,?,?,?,?,?,?)", (rid, 12, rid, rid, "running", store.pack({"kind": "dtm_action", "action": "run", "target_id": 12}), store.pack({"_adapter": correlation}), store.now()))
    monkeypatch.setattr(legacy, "script_revision", lambda revision_id: {"id": revision_id, "status": "verified", "attempts": [{"id": 2, "status": "passed"}, {"id": 1, "status": "failed"}]})
    assert legacy.reconcile("failed-first")["status"] == "failed"
    assert legacy.reconcile("uncorrelated")["status"] == "unknown"
