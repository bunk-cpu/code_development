"""七个只读工具：授权范围绑定在服务端；分页、来源和上限共同验证。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import java_source
from .store import db, many, now, one, pack


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManifestArgs(Args):
    section: Literal["all", "build", "entries", "dependencies", "coverage"] = "all"


class SearchArgs(Args):
    query: str = Field(default="", max_length=200)
    fact_types: list[Literal["class", "method", "field", "record", "enum", "annotation", "config", "sql", "mapper"]] = Field(default_factory=list, max_length=9)
    match_mode: Literal["exact", "lexical"] = "lexical"
    cursor: str | None = Field(default=None, max_length=1000)
    limit: int = Field(default=20, ge=1, le=20)


class SymbolArgs(Args):
    symbol_id: str = Field(min_length=1, max_length=100)
    view: Literal["signature", "body", "context"] = "body"
    start_line: int | None = Field(default=None, ge=1)
    max_lines: int = Field(default=80, ge=1, le=120)


class TraceArgs(Args):
    symbol_id: str = Field(min_length=1, max_length=100)
    direction: Literal["outgoing", "incoming", "both"] = "outgoing"
    relation_types: list[Literal["calls_candidate", "imports", "guard", "implements", "inherits", "overrides"]] = Field(default_factory=lambda: ["calls_candidate", "overrides"], max_length=6)
    max_depth: int = Field(default=2, ge=1, le=3)
    cursor: str | None = Field(default=None, max_length=1000)
    limit: int = Field(default=30, ge=1, le=30)


class ContractArgs(Args):
    entry_id: str = Field(min_length=1, max_length=100)
    sections: list[Literal["contract", "permissions", "conditions", "runtime"]] = Field(default_factory=lambda: ["contract"], max_length=4)
    runtime_projection_id: str | None = Field(default=None, max_length=100)


class MaterialArgs(Args):
    symbol_id: str | None = Field(default=None, max_length=100)
    feature_id: str | None = Field(default=None, max_length=100)
    kinds: list[Literal["test", "manual"]] = Field(default_factory=lambda: ["test", "manual"], max_length=2)
    limit: int = Field(default=10, ge=1, le=10)


class ChangeArgs(Args):
    base_snapshot_id: str | None = Field(default=None, max_length=100)
    symbol_id: str | None = Field(default=None, max_length=100)
    limit: int = Field(default=10, ge=1, le=10)


ARGUMENT_MODELS = {
    "get_module_manifest": ManifestArgs, "search_code_facts": SearchArgs,
    "get_symbol_evidence": SymbolArgs, "trace_code_relations": TraceArgs,
    "get_contract_and_runtime": ContractArgs, "get_tests_and_manual": MaterialArgs,
    "get_git_change_context": ChangeArgs,
}
TOOL_SCHEMAS = [{"type": "function", "function": {"name": name,
                 "description": "只读固定快照、当前模块和获准邻接模块；" + name,
                 "parameters": args.model_json_schema()}} for name, args in ARGUMENT_MODELS.items()]


def scope_for(snapshot_id: str, module: str) -> dict:
    index = java_source.get_index(snapshot_id)
    if module not in {m["id"] for m in index["manifest"]["modules"]}:
        raise LookupError("MODULE_NOT_FOUND")
    all_symbols = java_source.symbols(snapshot_id)
    by_id = {s["id"]: s for s in all_symbols}
    related = {module}
    edges = java_source.relations(snapshot_id)
    for _ in range(3):
        related |= {by_id[e["target_id"]]["module"] for e in edges
                    if e.get("target_id") in by_id and by_id.get(e["source_id"], {}).get("module") in related}
    return {"repository_id": index["repository_id"], "snapshot_id": snapshot_id,
            "build_context_id": index["build_context_id"], "module_id": module,
            "allowed_module_ids": sorted({module} | related)}


def read_evidence(scope: dict, evidence_id: str) -> dict | None:
    with db() as con:
        row = one(con, "SELECT * FROM evidence WHERE id=? AND snapshot_id=?", (evidence_id, scope["snapshot_id"]))
        if not row or row["module"] not in scope["allowed_module_ids"]:
            return None
        relation = one(con, "SELECT data FROM code_relations WHERE id=?", (evidence_id,))
        material = one(con, "SELECT * FROM analysis_materials WHERE id=? AND snapshot_id=?", (evidence_id, scope["snapshot_id"]))
        source = one(con, "SELECT content,hash FROM source_files WHERE snapshot_id=? AND path=?", (scope["snapshot_id"], row["file"]))
    if material:
        if material["content"] != row["text"] or hashlib.sha256(row["text"].encode()).hexdigest() != material["payload_hash"]:
            raise ValueError("MATERIAL_HASH_MISMATCH")
    elif not source or hashlib.sha256(source["content"].encode()).hexdigest() != source["hash"]:
        raise ValueError("SOURCE_HASH_MISMATCH")
    if not relation and not material:
        lines = source["content"].splitlines()
        expected = "\n".join(lines[row["line"] - 1:row["line"] - 1 + len(row["text"].splitlines())])
        if row["text"] != expected:
            raise ValueError("EVIDENCE_HASH_MISMATCH")
    return {**row, "evidence_id": row["id"], "kind": material["kind"] if material else "relation" if relation else "source",
            "content_hash": hashlib.sha256(row["text"].encode()).hexdigest(), "content": row["text"],
            "repository_id": scope["repository_id"], "build_context_id": scope["build_context_id"],
            "locator": f"/internal/source/{scope['snapshot_id']}/evidence/{row['id']}",
            "hash_verified": True, "runtime_verified": False}


def page(items: list[dict], scope: dict, args: Args) -> tuple[list, str | None]:
    opts = args.model_dump(exclude={"cursor"})
    fingerprint = java_source.digest([scope, opts])
    key = os.getenv("CURSOR_SECRET", os.getenv("DEMO_PASSWORD", "demo-only")).encode()
    start = 0
    cursor = getattr(args, "cursor", None)
    if cursor:
        try:
            payload, signature = cursor.split(".")
            raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            if not hmac.compare_digest(signature, hmac.new(key, raw, hashlib.sha256).hexdigest()):
                raise ValueError()
            decoded = json.loads(raw)
            if decoded["scope"] != fingerprint or decoded["expires"] < now():
                raise ValueError()
            start = next(i + 1 for i, item in enumerate(items) if item["id"] == decoded["last"])
        except (ValueError, KeyError, StopIteration):
            raise ValueError("CURSOR_INVALID") from None
    selected = items[start:start + args.limit]
    if start + args.limit >= len(items):
        return selected, None
    raw = pack({"scope": fingerprint, "last": selected[-1]["id"], "expires": now() + 900}).encode()
    payload = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return selected, payload + "." + hmac.new(key, raw, hashlib.sha256).hexdigest()


def execute(scope: dict, tool: str, arguments: dict) -> dict:
    envelope = {"ok": True, "tool": tool, "scope": scope, "items": [], "evidence": [],
                "next_cursor": None, "truncated": False, "warnings": [], "error": None}
    try:
        if tool not in ARGUMENT_MODELS:
            raise ValueError("UNKNOWN_TOOL")
        args = ARGUMENT_MODELS[tool].model_validate(arguments)
        index = java_source.get_index(scope["snapshot_id"])
        if scope != scope_for(scope["snapshot_id"], scope["module_id"]):
            raise ValueError("SCOPE_CHANGED")
        symbols = [s for s in java_source.symbols(scope["snapshot_id"]) if s["module"] in scope["allowed_module_ids"]]
        by_id = {s["id"]: s for s in symbols}
        edges = [e for e in java_source.relations(scope["snapshot_id"]) if e["module"] in scope["allowed_module_ids"]]
        coverage = java_source.module_coverage(scope["snapshot_id"], scope["module_id"])
        envelope["coverage"] = coverage
        refs = []
        if tool == "get_module_manifest":
            module = next(m for m in index["manifest"]["modules"] if m["id"] == scope["module_id"])
            envelope["items"] = [{**module, "build_context_id": scope["build_context_id"], "coverage": coverage,
                                  "entries": [s["id"] for s in symbols if s["module"] == scope["module_id"] and s["framework"]["entries"]][:20]}]
        elif tool == "search_code_facts":
            terms = set(args.query.lower().split())
            matched = []
            for s in symbols:
                if args.fact_types and s["kind"] not in args.fact_types:
                    continue
                searchable = pack({k: s.get(k) for k in ("name", "key", "signature", "framework", "file")}).lower()
                hit = not terms or any(term in searchable for term in terms)
                if args.match_mode == "exact":
                    hit = args.query in (s["name"], s["id"], s["key"], s.get("signature"))
                if hit:
                    matched.append(s)
            envelope["items"], envelope["next_cursor"] = page(matched, scope, args)
            refs = [s["evidence_id"] for s in envelope["items"]]
        elif tool == "get_symbol_evidence":
            symbol = by_id.get(args.symbol_id)
            if not symbol:
                raise LookupError("NOT_FOUND")
            ev = read_evidence(scope, symbol["evidence_id"])
            start = args.start_line or symbol["start_line"]
            if not symbol["start_line"] <= start <= symbol["end_line"]:
                raise ValueError("SOURCE_RANGE_INVALID")
            with db() as con:
                source = one(con, "SELECT content FROM source_files WHERE snapshot_id=? AND path=?", (scope["snapshot_id"], symbol["file"]))
            end = min(symbol["end_line"], start + (1 if args.view == "signature" else args.max_lines) - 1)
            snippet = "\n".join(source["content"].splitlines()[start - 1:end])
            # The registered definition remains the stable citation; expanded ranges are immutable-source views.
            envelope["items"] = [{**symbol, "view_start": start, "view_end": end, "content": snippet}]
            envelope["evidence"] = [ev]
            envelope["truncated"] = end < symbol["end_line"]
        elif tool == "trace_code_relations":
            if args.symbol_id not in by_id:
                raise LookupError("NOT_FOUND")
            visited, frontier, found = {args.symbol_id}, {args.symbol_id}, {}
            for depth in range(args.max_depth):
                next_frontier = set()
                for e in edges:
                    if e["kind"] not in args.relation_types:
                        continue
                    out = args.direction in ("outgoing", "both") and e["source_id"] in frontier
                    inc = args.direction in ("incoming", "both") and e.get("target_id") in frontier
                    if out or inc:
                        found[e["id"]] = {**e, "depth": depth + 1}
                        node = e.get("target_id") if out else e["source_id"]
                        if node in by_id and node not in visited:
                            next_frontier.add(node)
                visited |= next_frontier
                frontier = next_frontier
                if not frontier:
                    break
            envelope["items"], envelope["next_cursor"] = page(sorted(found.values(), key=lambda e: (e["depth"], e["id"])), scope, args)
            refs = [e["id"] for e in envelope["items"]]
            envelope["depth_limit_reached"] = bool(frontier)
            envelope["unresolved_count"] = sum(e["resolution_status"] in {"UNRESOLVED", "AMBIGUOUS"} for e in found.values())
        elif tool == "get_contract_and_runtime":
            symbol = by_id.get(args.entry_id)
            if not symbol:
                raise LookupError("NOT_FOUND")
            envelope["items"] = [symbol["framework"]]
            refs = [symbol["evidence_id"]]
            envelope["warnings"] = ["NOT_AVAILABLE: 运行投影未提供，静态入口不能证明客户已启用"]
        elif tool == "get_tests_and_manual":
            if args.symbol_id and args.symbol_id not in by_id:
                raise LookupError("NOT_FOUND")
            with db() as con:
                materials = many(con, "SELECT id,module,kind,target_id,payload_hash,reviewer FROM analysis_materials WHERE snapshot_id=? ORDER BY id", (scope["snapshot_id"],))
            materials = [m for m in materials if m["module"] in scope["allowed_module_ids"] and m["kind"] in args.kinds]
            envelope["items"] = materials[:args.limit]
            envelope["truncated"] = len(materials) > args.limit
            refs = [m["id"] for m in envelope["items"]]
            envelope["warnings"] = ["HUMAN_DECLARED_MAPPING: 资产登记不自动证明同构建实测"] if refs else ["NOT_AVAILABLE: 尚无映射到本源码版本的测试执行/手册；不会用其他版本替代"]
            if refs and (args.symbol_id or args.feature_id):
                envelope["warnings"].append("MODULE_LEVEL_ONLY: 模块级资产关联不能证明指定符号/功能已被该资产覆盖")
        elif tool == "get_git_change_context":
            base = args.base_snapshot_id or coverage.get("base_snapshot_id")
            if base:
                result = java_source.impact(base, scope["snapshot_id"])
                changes = [c for c in result["changes"] if c["module"] in scope["allowed_module_ids"]]
                envelope["items"] = changes[:args.limit]
                envelope["truncated"] = len(changes) > args.limit
            else:
                envelope["warnings"] = ["NOT_AVAILABLE: 没有成功分析基线"]
        envelope["evidence"].extend(ev for ref in refs if (ev := read_evidence(scope, ref)))
        envelope["truncated"] = envelope["truncated"] or bool(envelope["next_cursor"])
    except (ValueError, LookupError, ValidationError) as exc:
        envelope.update(ok=False, error={"code": "NOT_FOUND" if isinstance(exc, LookupError) else "INVALID_ARGUMENT",
                                         "message": "参数或范围无效"}, items=[], evidence=[])
    return envelope
