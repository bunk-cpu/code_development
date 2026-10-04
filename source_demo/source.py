"""固定许可样本的只读词法索引；没有 JDT 时明确报告部分覆盖。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .store import ROOT, db, many, now, one, pack

FIXTURE = ROOT / "fixtures"
VERSIONS = ("v1", "v2")


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def files(version: str) -> dict[str, str]:
    if version not in VERSIONS:
        raise ValueError("仅允许固定样本版本 v1/v2")
    return {p.name: p.read_text(encoding="utf-8") for p in sorted((FIXTURE / version).glob("*.java"))}


def index_fixture(version: str) -> dict:
    content = files(version)
    digest = sha(pack(content).encode())
    snapshot_id = f"fixture-{digest[:20]}"
    prior = files("v1") if version == "v2" else {}
    changed = [name for name in content if content[name] != prior.get(name)]
    with db() as con:
        existing = one(con, "SELECT id,digest FROM snapshots WHERE version=?", (version,))
        if existing and existing["digest"] != digest:
            raise RuntimeError("SOURCE_REVISION_CONFLICT: 固定样本版本内容已变化")
        con.execute(
            "INSERT INTO snapshots VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
            (snapshot_id, version, digest, pack(changed), now()),
        )
        for name, body in content.items():
            for number, raw in enumerate(body.splitlines(), 1):
                line = raw.strip()
                if not line:
                    continue
                evidence_id = "ev-" + sha(f"{digest}:{name}:{number}:{line}".encode())[:20]
                con.execute(
                    "INSERT INTO evidence VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                    (evidence_id, snapshot_id, name.removesuffix(".java"), name, number, line),
                )
    return {"snapshot_id": snapshot_id, "version": version, "digest": digest,
            "changed_files": changed, "coverage": "PARTIAL",
            "unknown": ["未运行 Java 编译/JDT；调用关系及动态绑定未验证"]}


def snapshots() -> list[dict]:
    with db() as con:
        rows = many(con, "SELECT * FROM snapshots ORDER BY created_at, version")
    for row in rows:
        row["changed_files"] = json.loads(row.pop("changed_json"))
        row["coverage"] = "PARTIAL"
        with db() as con:
            index = one(con, "SELECT repository_id,status,report FROM code_indexes WHERE snapshot_id=?", (row["id"],))
        if index:
            row.update(coverage=index["status"], repository_id=index["repository_id"], report=json.loads(index["report"]))
    return rows


def profile(snapshot_id: str, module: str) -> dict | None:
    with db() as con:
        semantic = one(con, "SELECT snapshot_id FROM code_indexes WHERE snapshot_id=?", (snapshot_id,))
        run = one(con, "SELECT id,status,bundle FROM agent_runs WHERE snapshot_id=? AND module=? AND bundle IS NOT NULL ORDER BY created_at DESC LIMIT 1", (snapshot_id, module))
    if semantic:
        from .java_source import module_coverage, symbols
        if run:
            return {"run_id": run["id"], "status": run["status"], **json.loads(run["bundle"])}
        return {"module": module, "snapshot_id": snapshot_id, "coverage": module_coverage(snapshot_id, module),
                "symbols": symbols(snapshot_id, module), "status": "NOT_ANALYZED"}
    with db() as con:
        snapshot = one(con, "SELECT id,version,digest FROM snapshots WHERE id=?", (snapshot_id,))
        if not snapshot:
            return None
        lines = many(con, "SELECT id,file,line,text FROM evidence WHERE snapshot_id=? AND module=? ORDER BY line", (snapshot_id, module))
    if not lines:
        return None
    claims = [line for line in lines if re.search(r"\b(class|public|throw)\b", line["text"]) and not line["text"].startswith("//")]
    return {"snapshot": snapshot, "module": module, "coverage": "PARTIAL", "claims": claims,
            "unknown": ["词法索引只证明这些行存在；无法证明运行时链路或功能已部署"]}


def ask(snapshot_id: str, question: str) -> dict | None:
    with db() as con:
        semantic = one(con, "SELECT snapshot_id FROM code_indexes WHERE snapshot_id=?", (snapshot_id,))
    if semantic:
        from .code_qa import answer
        return answer(snapshot_id, question)
    with db() as con:
        snapshot = one(con, "SELECT id,version,digest FROM snapshots WHERE id=?", (snapshot_id,))
        if not snapshot:
            return None
        lines = many(con, "SELECT id,module,file,line,text FROM evidence WHERE snapshot_id=?", (snapshot_id,))
    terms = set(re.findall(r"[A-Za-z_]{3,}", question.lower()))
    mapping = {"订单": "order", "统计": "summary", "权限": "policy", "校验": "require", "入口": "controller", "状态": "status", "租户": "tenant"}
    terms.update(value for key, value in mapping.items() if key in question)
    scored = []
    for line in lines:
        if line["text"].startswith("//"):
            continue
        searchable = (line["module"] + " " + line["text"]).lower()
        score = sum(term in searchable for term in terms)
        if score:
            scored.append((score, line))
    citations = [line for _, line in sorted(scored, key=lambda item: (-item[0], item[1]["file"], item[1]["line"]))[:8]]
    return {"status": "partial" if citations else "insufficient_evidence",
            "snapshot_id": snapshot_id,
            "answer": "找到以下固定快照中的源码行；调用链和部署状态尚未验证。" if citations else "未找到足够的源码依据。",
            "snapshot": snapshot, "citations": citations,
            "unknown": ["词法匹配不等于调用关系或运行时行为"]}


def evidence(snapshot_id: str, evidence_id: str) -> dict | None:
    with db() as con:
        return one(con, "SELECT * FROM evidence WHERE snapshot_id=? AND id=?", (snapshot_id, evidence_id))
