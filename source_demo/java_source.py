"""不可变 Git/文件快照 → Maven 模块 → JDT 事实 → 版本化证据与影响。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

from .store import ROOT, db, many, now, one, pack

JAR = ROOT / "java-indexer/target/java-indexer-1.0.0.jar"
IGNORED = {".git", ".env", ".venv", "venv", "node_modules", "target", "build", "dist", ".idea", ".gradle"}
SUFFIXES = {".java", ".xml", ".properties", ".yaml", ".yml", ".sql", ".gradle", ".kts"}


def digest(value: object) -> str:
    return hashlib.sha256(pack(value).encode()).hexdigest()


def safe_path(path: str) -> bool:
    p = PurePosixPath(path)
    return (not p.is_absolute() and ".." not in p.parts and not IGNORED.intersection(p.parts)
            and not any(part.startswith(".env") for part in p.parts)
            and p.suffix in SUFFIXES and not re.search(r"(?i)(secret|credential|keystore|private.?key)", p.name))


def git(root: Path, *args: str) -> bytes:
    # Arguments never go through a shell; Git hooks/build scripts are not executed.
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError("GIT_OPERATION_FAILED")
    return result.stdout


def register(name: str, root: str, classpath: list[str] | None = None, java_release: str = "17") -> dict:
    path = Path(root).resolve()
    allowed = [Path(p).resolve() for p in os.getenv("SOURCE_ALLOWED_ROOTS", str(ROOT)).split(os.pathsep)]
    if not path.is_dir() or not any(path.is_relative_to(p) for p in allowed):
        raise ValueError("REPOSITORY_ROOT_NOT_ALLOWED: 配置 SOURCE_ALLOWED_ROOTS 后接入本机业务仓库")
    if java_release not in {"8", "11", "17", "21"}:
        raise ValueError("JAVA_RELEASE_INVALID")
    cp = []
    for item in classpath or []:
        candidate = Path(item).resolve()
        if not candidate.is_file() or candidate.suffix != ".jar" or not any(candidate.is_relative_to(p) for p in allowed):
            raise ValueError("CLASSPATH_NOT_ALLOWED")
        cp.append(str(candidate))
    repository_id = "repo-" + digest(str(path))[:16]
    cfg = {"classpath": sorted(cp), "java_release": java_release}
    with db() as con:
        existing = one(con, "SELECT * FROM repositories WHERE id=?", (repository_id,))
        if existing:
            con.execute("UPDATE repositories SET name=?,config=? WHERE id=?", (name, pack(cfg), repository_id))
        else:
            con.execute("INSERT INTO repositories VALUES (?,?,?,?,?)", (repository_id, name, str(path), pack(cfg), now()))
    return {"id": repository_id, "name": name, "root": str(path), "config": cfg}


def repositories() -> list[dict]:
    with db() as con:
        rows = many(con, "SELECT * FROM repositories ORDER BY name")
    return [{**r, "config": json.loads(r["config"])} for r in rows]


def load_tree(root: Path, ref: str) -> tuple[str, dict[str, str]]:
    content = {}
    total_bytes, max_files = 0, int(os.getenv("SOURCE_MAX_FILES", "20000"))
    def add(name, data):
        nonlocal total_bytes
        if len(data) > 1_000_000:
            raise ValueError("SOURCE_FILE_SIZE_BUDGET_EXCEEDED")
        total_bytes += len(data)
        if len(content) >= max_files or total_bytes > 100_000_000:
            raise ValueError("SOURCE_SIZE_BUDGET_EXCEEDED")
        content[name] = data.decode("utf-8", errors="replace")
    if (root / ".git").exists():
        if not re.fullmatch(r"[A-Za-z0-9_./-]{1,180}", ref) or ref.startswith("-"):
            raise ValueError("GIT_REF_INVALID")
        sha = git(root, "rev-parse", "--verify", ref + "^{commit}").decode().strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise ValueError("GIT_SHA_INVALID")
        entries = git(root, "ls-tree", "-rlz", sha).split(b"\0")
        for entry in entries:
            if not entry:
                continue
            meta, raw_path = entry.split(b"\t", 1)
            mode, kind, blob, size = meta.split()
            name = raw_path.decode("utf-8")
            if kind != b"blob" or mode not in (b"100644", b"100755") or not safe_path(name):
                continue
            if int(size) > 1_000_000:
                raise ValueError("SOURCE_FILE_SIZE_BUDGET_EXCEEDED")
            data = git(root, "cat-file", "blob", blob.decode())
            add(name, data)
    else:
        for base, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED and not (Path(base) / d).is_symlink())
            for name in sorted(names):
                path = Path(base) / name
                relative = path.relative_to(root).as_posix()
                if safe_path(relative) and not path.is_symlink():
                    if path.stat().st_size > 1_000_000:
                        raise ValueError("SOURCE_FILE_SIZE_BUDGET_EXCEEDED")
                    add(relative, path.read_bytes())
        sha = "tree-" + digest(content)
    # Never send configuration secret values to model/index evidence.
    for name in list(content):
        if not name.endswith(".java"):
            content[name] = re.sub(r"(?im)^([^\n]*(?:password|secret|api.?key|token)\s*[:=])[^\n]*", r"\1 [REDACTED]", content[name])
    return sha, dict(sorted(content.items()))


def inventory(content: dict, cfg: dict) -> dict:
    modules = []
    ns = {"m": "http://maven.apache.org/POM/4.0.0"}
    for name, body in content.items():
        if PurePosixPath(name).name != "pom.xml":
            continue
        try:
            node = ET.fromstring(body)
            namespaced = node.tag.startswith("{")
            def text(path, default=""):
                return node.findtext("/".join("m:" + p for p in path.split("/")) if namespaced else path, default, ns)
            prefix = str(PurePosixPath(name).parent)
            prefix = "" if prefix == "." else prefix + "/"
            dependencies = [{"group": d.findtext("m:groupId" if namespaced else "groupId", "", ns),
                             "artifact": d.findtext("m:artifactId" if namespaced else "artifactId", "", ns),
                             "version": d.findtext("m:version" if namespaced else "version", "", ns)}
                            for d in node.findall("m:dependencies/m:dependency" if namespaced else "dependencies/dependency", ns)]
            modules.append({"id": prefix.rstrip("/") or "root", "prefix": prefix, "pom": name,
                            "artifact": text("artifactId", "unknown"), "packaging": text("packaging", "jar"),
                            "declared_dependencies": dependencies, "build_model": "static_pom",
                            "source_roots": [prefix + "src/main/java", prefix + "src/test/java"],
                            "generated_sources": "not_executed", "profiles": [p.findtext("m:id" if namespaced else "id", "", ns) for p in node.findall("m:profiles/m:profile" if namespaced else "profiles/profile", ns)]})
        except ET.ParseError:
            modules.append({"id": str(PurePosixPath(name).parent), "prefix": "", "error": "INVALID_POM", "source_roots": []})
    if not modules:
        modules = [{"id": "root", "prefix": "", "artifact": "source-tree", "build_model": "no_build_file", "source_roots": []}]
    for module in modules:
        module["source_files"] = [name for name in content if name.endswith(".java") and module_for(name, modules) == module["id"]]
    return {"modules": modules, "java_release": cfg["java_release"], "classpath": cfg["classpath"],
            "build_executed": False, "limitations": ["静态 POM 不是 effective POM；项目构建、注解处理器及生成代码未执行",
                                                       "运行注册、Spring 装配、反射、AOP、动态 SQL 与实际部署需独立核验"]}


def module_for(name: str, modules: list[dict]) -> str:
    candidates = [m for m in modules if name.startswith(m.get("prefix", ""))]
    return max(candidates, key=lambda m: len(m.get("prefix", "")))["id"] if candidates else "root"


def parse_java(content: dict, manifest: dict, cfg: dict) -> dict:
    if not shutil.which("java") or not JAR.is_file():
        raise ValueError("JDT_NOT_READY: 在 java-indexer 执行 mvn package")
    with tempfile.TemporaryDirectory(prefix="source-index-") as temp:
        root = Path(temp)
        for name, body in content.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        java_files = [str(root / name) for name in content if name.endswith(".java")]
        roots = {str(root)}
        for name, body in content.items():
            if not name.endswith(".java"):
                continue
            package = re.search(r"\bpackage\s+([\w.]+)\s*;", body)
            path = (root / name).parent
            if package:
                for _ in package[1].split("."):
                    path = path.parent
                if path.is_relative_to(root):
                    roots.add(str(path))
        files_path, roots_path, cp_path = [root / ("index-" + n) for n in ("files", "roots", "classpath")]
        files_path.write_text("\n".join(java_files)); roots_path.write_text("\n".join(sorted(roots)))
        classpath = []
        for index, source in enumerate(cfg["classpath"]):
            target = root / "index-dependencies" / f"{index}.jar"
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, target)
            expected = cfg.get("classpath_hashes", [])[index] if cfg.get("classpath_hashes") else hashlib.sha256(Path(source).read_bytes()).hexdigest()
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise ValueError("CLASSPATH_CHANGED")
            classpath.append(str(target))
        cp_path.write_text("\n".join(classpath))
        proc = subprocess.run(["java", "-Xmx768m", "-jar", str(JAR), str(root), str(files_path), str(roots_path), str(cp_path), cfg["java_release"]],
                              capture_output=True, text=True, timeout=int(os.getenv("SOURCE_TIMEOUT_S", "180")))
        if proc.returncode:
            raise ValueError("JDT_FAILED:" + proc.stderr.splitlines()[-1][:200] if proc.stderr else "JDT_FAILED")
        return json.loads(proc.stdout)


def index_repository(repository_id: str, ref: str = "HEAD", job: dict | None = None) -> dict:
    def check(con):
        if job:
            from .jobs import assert_lease
            assert_lease(job, con)
            acl = one(con, "SELECT allowed FROM repository_acl WHERE repository_id=? AND actor=?", (repository_id, job["actor"]))
            if acl and not acl["allowed"]:
                raise PermissionError("REPOSITORY_ACCESS_DENIED")
    with db() as con:
        check(con)
        repository = one(con, "SELECT * FROM repositories WHERE id=?", (repository_id,))
    if not repository:
        raise LookupError("REPOSITORY_NOT_FOUND")
    root, cfg = Path(repository["root"]), json.loads(repository["config"])
    commit, content = load_tree(root, ref)
    cfg_fingerprint = {**cfg, "pipeline_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "classpath_hashes": [hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in cfg["classpath"]],
                       "analyzer_hash": hashlib.sha256(JAR.read_bytes()).hexdigest() if JAR.exists() else "not_ready",
                       "jdk": subprocess.run(["java", "-version"], capture_output=True, text=True).stderr if shutil.which("java") else "not_ready"}
    context_id = "ctx-" + digest(cfg_fingerprint)[:20]
    snapshot_id = "idx-" + digest([repository_id, commit, content, context_id])[:24]
    with db() as con:
        existing = one(con, "SELECT * FROM code_indexes WHERE snapshot_id=?", (snapshot_id,))
    if existing:
        return get_index(snapshot_id)
    manifest = inventory(content, cfg)
    parsed = parse_java(content, manifest, {**cfg, "classpath_hashes": cfg_fingerprint["classpath_hashes"]})
    keys = {}
    for symbol in parsed["symbols"]:
        symbol["module"] = module_for(symbol["file"], manifest["modules"])
        symbol["id"] = "sym-" + digest([snapshot_id, symbol["module"], symbol["file"], symbol["key"], symbol["start_line"]])[:24]
        keys.setdefault(symbol["key"], []).append(symbol)
    for edge in parsed["relations"]:
        sources = [s for s in keys.get(edge["source_key"], []) if s["file"] == edge["file"] and s["start_line"] <= edge["start_line"] <= s["end_line"]]
        targets = keys.get(edge.get("target_key"), [])
        edge.update(source_id=sources[0]["id"] if len(sources) == 1 else edge["source_key"],
                    target_id=targets[0]["id"] if len(targets) == 1 else None,
                    external_target=not targets, module=module_for(edge["file"], manifest["modules"]))
        if len(targets) > 1:
            # ponytail: retain ambiguity; isolated per-module binding is needed before selecting duplicate FQNs.
            edge.update(resolution_status="AMBIGUOUS", target_candidate_ids=[s["id"] for s in targets])
    with db() as con:
        previous = one(con, "SELECT snapshot_id FROM code_indexes WHERE repository_id=? ORDER BY created_at DESC LIMIT 1", (repository_id,))
        prior = {r["path"]: r["hash"] for r in many(con, "SELECT path,hash FROM source_files WHERE snapshot_id=?", (previous["snapshot_id"],))} if previous else {}
    hashes = {name: hashlib.sha256(body.encode()).hexdigest() for name, body in content.items()}
    changed = sorted(name for name in set(prior) | set(hashes) if prior.get(name) != hashes.get(name))
    calls = [e for e in parsed["relations"] if e["kind"] == "calls_candidate"]
    entries = []
    parents = {(s["key"], s["file"]): s for s in parsed["symbols"]}
    for symbol in parsed["symbols"]:
        mods = " ".join(symbol.get("modifiers", []))
        symbol["framework"] = framework_facts(mods, symbol)
        parent = parents.get((symbol.get("owner"), symbol["file"]), {})
        parent_mods = " ".join(parent.get("modifiers", []))
        if symbol["kind"] == "method":
            symbol["framework"]["class_annotations"] = parent_mods
            base_path = re.search(r'@RequestMapping\(\s*"([^"]*)"', parent_mods)
            routes = re.findall(r'@(Get|Post|Put|Delete|Patch|Request)Mapping\(\s*"([^"]*)"', mods)
            symbol["framework"]["entrypoints"] = [{"method": verb.upper(), "path": (base_path[1] if base_path else "") + path, "static_candidate": True} for verb, path in routes]
            symbol["framework"]["permissions"].extend(re.findall(r"@(?:PreAuthorize|Secured|RolesAllowed)\([^)]*\)", parent_mods))
            symbol["framework"]["conditions"].extend(re.findall(r"@(?:Profile|Conditional\w*|Value)\([^)]*\)", parent_mods))
        if symbol["kind"] == "method" and symbol["framework"]["entries"]:
            entries.append(symbol["id"])
    report = {"source_files_total": sum(n.endswith(".java") for n in content),
              "source_files_parsed": len({s["file"] for s in parsed["symbols"]}),
              "references_total": len(calls), "references_resolved": sum(e["resolution_status"] == "RESOLVED" for e in calls),
              "discovered_entries_total": len(entries), "entries_reviewed": 0, "pending_entry_ids": entries,
              "known_dynamic_gaps": manifest["limitations"], "runtime_projection_id": None,
              "ambiguous_relations": sum(e["resolution_status"] == "AMBIGUOUS" for e in parsed["relations"]),
              "problems": parsed["problems"], "analyzer": parsed["analyzer"], "base_snapshot_id": previous["snapshot_id"] if previous else None}
    status = "PARTIAL" if parsed["problems"] or report["ambiguous_relations"] else "READY"
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        check(con)
        version = repository_id + ":" + commit + ":" + context_id
        con.execute("INSERT INTO snapshots VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING", (snapshot_id, version, digest(content), pack(changed), now()))
        con.execute("INSERT INTO code_indexes VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                    (snapshot_id, repository_id, commit, context_id, pack(manifest), status, pack(report), now()))
        for name, body in content.items():
            con.execute("INSERT INTO source_files VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING", (snapshot_id, name, module_for(name, manifest["modules"]), hashes[name], body))
        for symbol in parsed["symbols"]:
            symbol_id, module = symbol["id"], symbol["module"]
            ev_id = "ev-" + digest([snapshot_id, symbol_id])[:24]
            symbol.update(id=symbol_id, module=module, evidence_id=ev_id)
            lines = content[symbol["file"]].splitlines()
            text = "\n".join(lines[symbol["start_line"] - 1:min(symbol["end_line"], symbol["start_line"] + 119)])
            con.execute("INSERT INTO code_symbols VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                        (symbol_id, snapshot_id, module, symbol["file"], symbol["name"], symbol["kind"], symbol["start_line"], symbol["end_line"], pack(symbol)))
            con.execute("INSERT INTO evidence VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING", (ev_id, snapshot_id, module, symbol["file"], symbol["start_line"], text))
        for edge in parsed["relations"]:
            source_id, target_id = edge["source_id"], edge["target_id"]
            relation_id = "rel-" + digest([snapshot_id, edge])[:24]
            edge["id"] = relation_id
            con.execute("INSERT INTO code_relations VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING", (relation_id, snapshot_id, source_id, target_id, edge["kind"], pack(edge)))
            con.execute("INSERT INTO evidence VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING", (relation_id, snapshot_id, edge["module"], edge["file"], edge["start_line"], pack(edge)))
        for name, body in content.items():
            if name.endswith(".java") or PurePosixPath(name).name == "pom.xml":
                continue
            module = module_for(name, manifest["modules"])
            kind = "sql" if name.endswith(".sql") else "mapper" if name.endswith(".xml") and "<mapper" in body else "config"
            symbol_id = "sym-" + digest([snapshot_id, name])[:24]
            ev_id = "ev-" + digest([snapshot_id, name])[:24]
            data = {"id": symbol_id, "key": name, "name": PurePosixPath(name).name, "kind": kind,
                    "module": module, "file": name, "start_line": 1, "end_line": len(body.splitlines()),
                    "evidence_id": ev_id, "binding_status": "SYNTAX", "framework": {"entries": [], "static_candidate": True}}
            if kind == "mapper":
                try:
                    mapper = ET.fromstring(body)
                    data["mapper_namespace"] = mapper.get("namespace")
                    data["statements"] = [{"kind": child.tag, "id": child.get("id"), "sql_template": " ".join(child.itertext())} for child in mapper]
                except ET.ParseError:
                    data["error"] = "INVALID_XML"
            con.execute("INSERT INTO code_symbols VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING", (symbol_id, snapshot_id, module, name, data["name"], kind, 1, data["end_line"], pack(data)))
            con.execute("INSERT INTO evidence VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING", (ev_id, snapshot_id, module, name, 1, "\n".join(body.splitlines()[:120])))
    return get_index(snapshot_id)


def framework_facts(modifiers: str, symbol: dict) -> dict:
    return {
        "entries": re.findall(r"@(GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping|KafkaListener|RabbitListener|Scheduled|EventListener)\b[^\n]*", modifiers),
        "annotations": modifiers,
        "permissions": re.findall(r"@(?:PreAuthorize|Secured|RolesAllowed)\([^)]*\)", modifiers),
        "conditions": re.findall(r"@(?:Profile|Conditional\w*|Value)\([^)]*\)", modifiers),
        "validation": re.findall(r"@(?:NotNull|NotBlank|Min|Max|Size|Valid|Pattern)\b[^\n]*", modifiers),
        "persistence": bool(re.search(r"@(Entity|Repository|Mapper|Select|Insert|Update|Delete)\b", modifiers)),
        "runtime_verified": False,
    }


def module_coverage(snapshot_id: str, module: str) -> dict:
    index = get_index(snapshot_id)
    manifest = next(m for m in index["manifest"]["modules"] if m["id"] == module)
    syms = symbols(snapshot_id, module)
    calls = [r for r in relations(snapshot_id) if r["module"] == module and r["kind"] == "calls_candidate"]
    entries = [s["id"] for s in syms if s["framework"].get("entries") and s["kind"] == "method"]
    return {**index["report"], "source_files_total": len(manifest["source_files"]),
            "source_files_parsed": len({s["file"] for s in syms if s["file"].endswith(".java")}),
            "references_total": len(calls), "references_resolved": sum(e["resolution_status"] == "RESOLVED" for e in calls),
            "discovered_entries_total": len(entries), "pending_entry_ids": entries,
            "problems": [p for p in index["report"]["problems"] if module_for(p["file"], index["manifest"]["modules"]) == module]}


def get_index(snapshot_id: str) -> dict:
    with db() as con:
        result = one(con, "SELECT * FROM code_indexes WHERE snapshot_id=?", (snapshot_id,))
    if not result:
        raise LookupError("INDEX_NOT_FOUND")
    for field in ("manifest", "report"):
        result[field] = json.loads(result[field])
    return result


def symbols(snapshot_id: str, module: str | None = None) -> list[dict]:
    with db() as con:
        rows = many(con, "SELECT data FROM code_symbols WHERE snapshot_id=?" + (" AND module=?" if module else "") + " ORDER BY file,start_line,id",
                    (snapshot_id, module) if module else (snapshot_id,))
    return [json.loads(r["data"]) for r in rows]


def relations(snapshot_id: str) -> list[dict]:
    with db() as con:
        return [json.loads(r["data"]) for r in many(con, "SELECT data FROM code_relations WHERE snapshot_id=? ORDER BY id", (snapshot_id,))]


def impact(base_snapshot_id: str, target_snapshot_id: str) -> dict:
    base, target = get_index(base_snapshot_id), get_index(target_snapshot_id)
    if base["repository_id"] != target["repository_id"]:
        raise ValueError("DIFF_REPOSITORY_MISMATCH")
    with db() as con:
        before = {r["path"]: r for r in many(con, "SELECT path,hash,module FROM source_files WHERE snapshot_id=?", (base_snapshot_id,))}
        after = {r["path"]: r for r in many(con, "SELECT path,hash,module FROM source_files WHERE snapshot_id=?", (target_snapshot_id,))}
        features = many(con, "SELECT * FROM features WHERE snapshot_id IN (?,?)", (base_snapshot_id, target_snapshot_id))
    changes = [{"path": name, "kind": "added" if name not in before else "deleted" if name not in after else "modified",
                "module": (after.get(name) or before[name])["module"]}
               for name in sorted(set(before) | set(after)) if before.get(name, {}).get("hash") != after.get(name, {}).get("hash")]
    deleted_by_hash = {before[c["path"]]["hash"]: c["path"] for c in changes if c["kind"] == "deleted"}
    for c in changes:
        if c["kind"] == "added" and after[c["path"]]["hash"] in deleted_by_hash:
            c.update(kind="renamed", old_path=deleted_by_hash[after[c["path"]]["hash"]])
    affected = {c["module"] for c in changes}
    modules_by_symbol = {s["id"]: s["module"] for sid in (base_snapshot_id, target_snapshot_id) for s in symbols(sid)}
    all_relations = relations(base_snapshot_id) + relations(target_snapshot_id)
    changed_context = base["build_context_id"] != target["build_context_id"] or any(PurePosixPath(c["path"]).name in {"pom.xml", "build.gradle", "settings.gradle", "build.gradle.kts"} for c in changes)
    # ponytail: partial bindings/new implementations trigger module-wide review; precise invalidation after a real-project benchmark.
    if changed_context or base["status"] == "PARTIAL" or target["status"] == "PARTIAL" or any(c["kind"] == "added" for c in changes):
        affected.update(m["id"] for m in target["manifest"]["modules"])
    while True:
        expanded = affected | {modules_by_symbol[e["source_id"]] for e in all_relations
                               if e.get("target_id") in modules_by_symbol and modules_by_symbol[e["target_id"]] in affected and e["source_id"] in modules_by_symbol}
        if expanded == affected:
            break
        affected = expanded
    return {"base_snapshot_id": base_snapshot_id, "target_snapshot_id": target_snapshot_id, "changes": changes,
            "affected_modules": sorted(affected), "build_context_changed": changed_context,
            "feature_review": [f["id"] for f in features], "coverage": target["status"],
            "reanalysis_required": bool(changes or changed_context), "unknowns": target["report"]["known_dynamic_gaps"]}
