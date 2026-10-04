"""文档导入、块补丁、摘要审核、版本化发布和回滚。"""
from __future__ import annotations

import difflib
import re
import secrets
import json

from . import knowledge
from .store import audit, db, many, now, one, pack


def import_document(release: str, name: str, content: str) -> dict:
    if not name.endswith((".md", ".txt")) or len(content) > 1_000_000:
        raise ValueError("UNSUPPORTED_DOCUMENT_TYPE_OR_SIZE")
    if release not in knowledge.RELEASES.values():
        raise ValueError("RELEASE_INVALID")
    original = content
    content = re.sub(r"(?is)<script\b[^>]*>.*?</script>", "", content)
    content_hash = knowledge.digest(original)
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        old = one(con, "SELECT id FROM source_documents WHERE release=? AND hash=?", (release, content_hash))
        if old:
            return {"source_id": old["id"], "created": False}
        source_id = "doc-" + secrets.token_hex(8)
        con.execute("INSERT INTO source_documents VALUES (?,?,?,?,?,?)", (source_id, release, name, content_hash, original, now()))
        sections = re.split(r"(?m)(?=^# )", content)
        revisions = []
        for i, section in enumerate(s for s in sections if s.strip()):
            title = section.splitlines()[0].lstrip("# ")[:150] or name
            revisions.append(knowledge.create_revision(source_id + "-" + str(i), release, title, section, con=con)["id"])
    return {"source_id": source_id, "created": True, "revisions": revisions, "hash": content_hash}


def blocks(content: str) -> list[dict]:
    chunks = re.split(r"\n\s*\n", content)
    counts = {}
    result = []
    for chunk in chunks:
        fingerprint = knowledge.digest(chunk)
        counts[fingerprint] = counts.get(fingerprint, 0) + 1
        result.append({"block_id": fingerprint[:16] + ":" + str(counts[fingerprint]), "hash": fingerprint, "markdown": chunk})
    return result


def create_patch(baseline_id: int, baseline_hash: str, operations: list[dict], evidence_ids: list[str]) -> dict:
    with db() as con:
        baseline = one(con, "SELECT * FROM manual_revisions WHERE id=?", (baseline_id,))
        if not baseline:
            raise LookupError("MANUAL_NOT_FOUND")
        if baseline["digest"] != baseline_hash:
            raise RuntimeError("BASELINE_HASH_CHANGED")
        for eid in evidence_ids:
            if not one(con, "SELECT id FROM evidence WHERE id=?", (eid,)):
                raise ValueError("PATCH_EVIDENCE_INVALID")
    content_blocks = blocks(baseline["content"])
    by_id = {b["block_id"]: b for b in content_blocks}
    seen = set()
    for op in operations:
        if set(op) - {"operation", "block_id", "expected_hash", "new_markdown"} or op.get("operation") not in ("replace", "insert_after", "delete"):
            raise ValueError("PATCH_OPERATION_INVALID")
        target = by_id.get(op.get("block_id"))
        if not target or target["hash"] != op.get("expected_hash") or target["block_id"] in seen:
            raise RuntimeError("PATCH_BLOCK_CONFLICT")
        seen.add(target["block_id"])
        if op["operation"] != "delete" and (not isinstance(op.get("new_markdown"), str) or len(op["new_markdown"]) > 10000):
            raise ValueError("PATCH_CONTENT_INVALID")
        target["operation"] = op
    output = []
    for block in content_blocks:
        op = block.get("operation", {})
        if op.get("operation") != "delete":
            output.append(op["new_markdown"] if op.get("operation") == "replace" else block["markdown"])
        if op.get("operation") == "insert_after":
            output.append(op["new_markdown"])
    proposed = "\n\n".join(output)
    patch_id = "patch-" + secrets.token_hex(8)
    with db() as con:
        con.execute("INSERT INTO manual_patches(id,baseline_id,proposed,digest,status,created_at,evidence_ids) VALUES (?,?,?,?,?,?,?)", (patch_id, baseline_id, proposed, knowledge.digest(proposed), "pending", now(), pack(evidence_ids)))
    return {"patch_id": patch_id, "baseline_id": baseline_id, "proposed": proposed,
            "digest": knowledge.digest(proposed), "diff": "\n".join(difflib.unified_diff(baseline["content"].splitlines(), proposed.splitlines(), fromfile="baseline", tofile="candidate", lineterm=""))}


def apply_patch(patch_id: str) -> dict:
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        patch = one(con, "SELECT * FROM manual_patches WHERE id=?", (patch_id,))
        if not patch:
            raise LookupError("PATCH_NOT_FOUND")
        if patch["status"] != "pending":
            raise RuntimeError("PATCH_ALREADY_APPLIED")
        baseline = one(con, "SELECT * FROM manual_revisions WHERE id=?", (patch["baseline_id"],))
        latest = one(con, "SELECT id FROM manual_revisions WHERE page_id=? AND release=? ORDER BY id DESC LIMIT 1", (baseline["page_id"], baseline["release"]))
        if latest["id"] != baseline["id"]:
            raise RuntimeError("BASELINE_CONFLICT")
        revision_id = con.execute("INSERT INTO manual_revisions(page_id,release,title,content,status,digest,created_at) VALUES (?,?,?,?,?,?,?) RETURNING id",
                          (baseline["page_id"], baseline["release"], baseline["title"], patch["proposed"], "draft", patch["digest"], now())).fetchone()["id"]
        con.execute("INSERT INTO manual_evidence SELECT ?,evidence_id FROM manual_evidence WHERE revision_id=?", (revision_id, baseline["id"]))
        con.executemany("INSERT INTO manual_evidence VALUES (?,?) ON CONFLICT DO NOTHING", [(revision_id, eid) for eid in json.loads(patch["evidence_ids"])])
        con.execute("UPDATE manual_patches SET status='applied' WHERE id=?", (patch_id,))
        return one(con, "SELECT * FROM manual_revisions WHERE id=?", (revision_id,))


def review(revision_id: int, digest: str, decision: str, reviewer: str, comment: str) -> dict:
    if decision not in ("approve", "reject"):
        raise ValueError("REVIEW_DECISION_INVALID")
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        row = one(con, "SELECT * FROM manual_revisions WHERE id=?", (revision_id,))
        if not row:
            raise LookupError("REVISION_NOT_FOUND")
        if row["digest"] != digest:
            raise RuntimeError("REVIEW_DIGEST_MISMATCH")
        status = "approved" if decision == "approve" else "rejected"
        if row["status"] not in ("draft", status):
            raise RuntimeError("REVIEW_STATE_CONFLICT")
        con.execute("UPDATE manual_revisions SET status=? WHERE id=?", (status, revision_id))
        audit(reviewer, "manual.review", str(revision_id), {"digest": digest, "decision": decision, "comment": comment}, con)
    return {**row, "status": status}


def history(release: str) -> list[dict]:
    with db() as con:
        rows = many(con, "SELECT * FROM knowledge_snapshots WHERE release=? ORDER BY id DESC", (release,))
        head = one(con, "SELECT snapshot_id FROM knowledge_heads WHERE release=?", (release,))
        for row in rows:
            members = many(con, "SELECT page_id,revision_id FROM knowledge_members WHERE snapshot_id=? ORDER BY page_id", (row["id"],))
            row.update(members=members, manifest_hash=knowledge.digest(pack(members)), current=head and row["id"] == head["snapshot_id"])
    return rows


def rollback(release: str, snapshot_id: int, expected_head: int, actor: str) -> dict:
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        target = one(con, "SELECT id FROM knowledge_snapshots WHERE id=? AND release=?", (snapshot_id, release))
        if not target:
            raise LookupError("KNOWLEDGE_SNAPSHOT_NOT_FOUND")
        updated = con.execute("UPDATE knowledge_heads SET snapshot_id=? WHERE release=? AND snapshot_id=?", (snapshot_id, release, expected_head))
        if updated.rowcount != 1:
            raise RuntimeError("PUBLICATION_CONFLICT")
        audit(actor, "knowledge.rollback", release, {"from": expected_head, "to": snapshot_id}, con)
    return {"release": release, "knowledge_snapshot_id": snapshot_id}
