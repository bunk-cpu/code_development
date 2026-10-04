"""测试用例来源、固定脚本候选和真实断言。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import secrets
import sqlite3

from .store import db, many, now, one, pack

CSV_FIELDS = ("source_id", "source_revision", "title", "tenant", "status", "expected_total")


def _hash(value: object) -> str:
    return hashlib.sha256(pack(value).encode()).hexdigest()


def add_case(source: str, source_id: str, source_revision: str, title: str,
             params: dict, expected: int | None, source_payload: dict | None = None,
             con: sqlite3.Connection | None = None) -> tuple[dict, bool]:
    if con is None:
        with db() as owned:
            return add_case(source, source_id, source_revision, title, params, expected, source_payload, owned)
    source_payload = source_payload or {}
    content_hash = _hash({"title": title, "params": params, "expected": expected, "source_payload": source_payload})
    existing = one(con, "SELECT * FROM test_cases WHERE source=? AND source_id=? AND source_revision=?",
                   (source, source_id, source_revision))
    if existing:
        if existing["hash"] != content_hash:
            raise RuntimeError("SOURCE_REVISION_CONFLICT")
        return existing, False
    case_id = "tc-" + secrets.token_hex(8)
    con.execute("INSERT INTO test_cases VALUES (?,?,?,?,?,?,?,?,?,?)",
                (case_id, source, source_id, source_revision, content_hash, title,
                 pack(params), expected, pack(source_payload), now()))
    return one(con, "SELECT * FROM test_cases WHERE id=?", (case_id,)), True


def import_csv(text: str) -> dict:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or any(field not in reader.fieldnames for field in CSV_FIELDS):
        raise ValueError("CSV 缺少必需列：" + ", ".join(CSV_FIELDS))
    rows = list(reader)
    validated = []
    for number, row in enumerate(rows, 2):
        try:
            if not all(row.get(field) for field in CSV_FIELDS):
                raise ValueError("存在空字段")
            if row["tenant"] not in ("tenant-a", "tenant-b") or row["status"] not in ("all", "pending", "completed"):
                raise ValueError("租户或状态无效")
            expected = int(row["expected_total"])
            if expected < 0:
                raise ValueError("预期数量不能为负")
            validated.append((row["source_id"], row["source_revision"], row["title"],
                              {"tenant": row["tenant"], "status": row["status"],
                               "date_from": "2026-09-01", "date_to": "2026-09-07"}, expected))
        except ValueError as exc:
            raise ValueError(f"IMPORT_ROW_INVALID: 第 {number} 行：{exc}") from exc
    created = 0
    with db() as con:
        for source_id, revision, title, params, expected in validated:
            created += add_case("csv", source_id, revision, title, params, expected, con=con)[1]
    return {"rows": len(validated), "created": created, "existing": len(validated) - created}


def list_cases() -> list[dict]:
    with db() as con:
        rows = many(con, "SELECT * FROM test_cases ORDER BY created_at,id")
    for row in rows:
        row["params"] = json.loads(row["params"])
        row["source_payload"] = json.loads(row["source_payload"])
    return rows


def create_candidate(case_id: str) -> dict:
    with db() as con:
        case = one(con, "SELECT * FROM test_cases WHERE id=?", (case_id,))
        if not case:
            raise LookupError("用例不存在")
        if case["expected"] is None:
            raise ValueError("旧系统自然语言预期不能作为本地确定性断言")
        script = {"template_id": "api.order_summary.read", "template_revision": 1,
                  "parameters": json.loads(case["params"]), "assertions": {"total": case["expected"]}}
        digest = _hash(script)
        existing = one(con, "SELECT * FROM script_candidates WHERE case_id=? AND digest=?", (case_id, digest))
        if existing:
            return {**existing, "script": script}
        candidate_id = "sc-" + secrets.token_hex(8)
        con.execute("INSERT INTO script_candidates VALUES (?,?,?,?,?)", (candidate_id, case_id, digest, 0, now()))
        return {**one(con, "SELECT * FROM script_candidates WHERE id=?", (candidate_id,)), "script": script}


def approve_candidate(candidate_id: str, digest: str) -> dict:
    with db() as con:
        candidate = one(con, "SELECT * FROM script_candidates WHERE id=?", (candidate_id,))
        if not candidate:
            raise LookupError("脚本候选不存在")
        if candidate["digest"] != digest:
            raise RuntimeError("SCRIPT_DIGEST_MISMATCH")
        con.execute("UPDATE script_candidates SET approved=1 WHERE id=?", (candidate_id,))
        return one(con, "SELECT * FROM script_candidates WHERE id=?", (candidate_id,))


def run_candidate(candidate_id: str, enqueue) -> dict:
    with db() as con:
        row = one(con, """SELECT s.*,c.expected,c.params FROM script_candidates s
          JOIN test_cases c ON c.id=s.case_id WHERE s.id=?""", (candidate_id,))
    if not row:
        raise LookupError("脚本候选不存在")
    if not row["approved"]:
        raise RuntimeError("SCRIPT_NOT_APPROVED")
    run_id = "tr-" + secrets.token_hex(8)
    with db() as con:
        job = enqueue("test_run", "internal", "tester", {"run_id": run_id}, con)
        con.execute("INSERT INTO test_runs VALUES (?,?,?,?,?,?,?,?)",
                    (run_id, row["case_id"], candidate_id, job["id"], "queued", row["expected"], None, now()))
    return {"run_id": run_id, "job_id": job["id"], "status": "queued"}


def get_run(run_id: str) -> dict | None:
    with db() as con:
        return one(con, "SELECT * FROM test_runs WHERE id=?", (run_id,))


def seed() -> None:
    data = "source_id,source_revision,title,tenant,status,expected_total\n" \
           "normal,1,A 已完成订单统计,tenant-a,completed,3\n" \
           "wrong,1,故意错误的期望,tenant-a,completed,99\n" \
           "other-tenant,1,B 待处理订单统计,tenant-b,pending,3\n"
    import_csv(data)
