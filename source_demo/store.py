from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import config  # load local .env before resolving the database path
import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("DEMO_DB", "data/demo.sqlite3"))
DATABASE_URL = os.getenv("DATABASE_URL", "")
DATABASE_ERRORS = (sqlite3.Error, psycopg.Error)
if not DB_PATH.is_absolute():
    DB_PATH = ROOT / DB_PATH


def pack(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def now() -> float:
    return time.time()


class PostgresConnection:
    """Keep the same parameterized, fixed SQL for the two supported databases."""
    def __init__(self):
        self.connection = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)

    def execute(self, sql, args=()):
        if sql == "BEGIN IMMEDIATE":
            # ponytail: serialize state transitions; replace with per-object row locks when throughput requires it.
            return self.connection.execute("SELECT pg_advisory_xact_lock(724031)")
        return self.connection.execute(sql.replace("%", "%%").replace("?", "%s"), tuple(int(v) if type(v) is bool else v for v in args))

    def executemany(self, sql, args):
        return self.connection.cursor().executemany(sql.replace("%", "%%").replace("?", "%s"), [tuple(int(v) if type(v) is bool else v for v in row) for row in args])

    def executescript(self, sql):
        sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY").replace(" REAL", " DOUBLE PRECISION")
        for statement in sql.split(";"):
            if statement.strip():
                self.execute(statement)


@contextmanager
def db():
    if DATABASE_URL:
        if not DATABASE_URL.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL must use PostgreSQL")
        con = PostgresConnection()
        with con.connection:
            yield con
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def one(con: sqlite3.Connection, sql: str, args: tuple = ()) -> dict | None:
    row = con.execute(sql, args).fetchone()
    return dict(row) if row else None


def many(con: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(row) for row in con.execute(sql, args)]


def init() -> None:
    with db() as con:
        if not DATABASE_URL:
            con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
          token TEXT PRIMARY KEY, actor TEXT NOT NULL, expires REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS snapshots (
          id TEXT PRIMARY KEY, version TEXT NOT NULL UNIQUE, digest TEXT NOT NULL,
          changed_json TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evidence (
          id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
          module TEXT NOT NULL, file TEXT NOT NULL, line INTEGER NOT NULL, text TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS evidence_snapshot ON evidence(snapshot_id, module);
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL, actor TEXT NOT NULL,
          payload TEXT NOT NULL, status TEXT NOT NULL, run_epoch INTEGER NOT NULL DEFAULT 0,
          lease_owner TEXT, lease_until REAL, result TEXT, error TEXT, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS manual_revisions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, page_id TEXT NOT NULL, release TEXT NOT NULL,
          title TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
          digest TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS manual_page_release ON manual_revisions(page_id, release, id);
        CREATE TABLE IF NOT EXISTS manual_evidence (
          revision_id INTEGER NOT NULL REFERENCES manual_revisions(id),
          evidence_id TEXT NOT NULL REFERENCES evidence(id),
          PRIMARY KEY(revision_id,evidence_id)
        );
        CREATE TABLE IF NOT EXISTS knowledge_snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT, release TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS knowledge_members (
          snapshot_id INTEGER NOT NULL REFERENCES knowledge_snapshots(id),
          page_id TEXT NOT NULL, revision_id INTEGER NOT NULL REFERENCES manual_revisions(id),
          PRIMARY KEY(snapshot_id, page_id)
        );
        CREATE TABLE IF NOT EXISTS questions (
          id TEXT PRIMARY KEY, tenant TEXT NOT NULL, actor TEXT NOT NULL,
          redacted TEXT NOT NULL, status TEXT NOT NULL, topic TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS test_cases (
          id TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
          source_revision TEXT NOT NULL, hash TEXT NOT NULL, title TEXT NOT NULL,
          params TEXT NOT NULL, expected INTEGER, source_payload TEXT NOT NULL,
          created_at REAL NOT NULL,
          UNIQUE(source, source_id, source_revision)
        );
        CREATE TABLE IF NOT EXISTS script_candidates (
          id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES test_cases(id),
          digest TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS legacy_script_assets (
          id TEXT PRIMARY KEY, source_revision_id INTEGER NOT NULL UNIQUE,
          source_item_id INTEGER NOT NULL, revision INTEGER NOT NULL,
          content_sha256 TEXT NOT NULL, source_status TEXT NOT NULL,
          imported_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS test_runs (
          id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES test_cases(id),
          candidate_id TEXT NOT NULL REFERENCES script_candidates(id), job_id TEXT NOT NULL,
          status TEXT NOT NULL, expected INTEGER, actual INTEGER, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS preparations (
          id TEXT PRIMARY KEY, tenant TEXT NOT NULL, actor TEXT NOT NULL, release TEXT NOT NULL,
          params TEXT NOT NULL, preview_digest TEXT NOT NULL, expires REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS executions (
          id TEXT PRIMARY KEY, tenant TEXT NOT NULL, actor TEXT NOT NULL,
          prepared_id TEXT NOT NULL REFERENCES preparations(id),
          idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL, job_id TEXT NOT NULL,
          status TEXT NOT NULL, result TEXT, created_at REAL NOT NULL,
          UNIQUE(tenant, actor, idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS repositories (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, root TEXT NOT NULL,
          config TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS code_indexes (
          snapshot_id TEXT PRIMARY KEY REFERENCES snapshots(id), repository_id TEXT NOT NULL,
          commit_sha TEXT NOT NULL, build_context_id TEXT NOT NULL, manifest TEXT NOT NULL,
          status TEXT NOT NULL, report TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_files (
          snapshot_id TEXT NOT NULL REFERENCES snapshots(id), path TEXT NOT NULL,
          module TEXT NOT NULL, hash TEXT NOT NULL, content TEXT NOT NULL,
          PRIMARY KEY(snapshot_id,path)
        );
        CREATE TABLE IF NOT EXISTS code_symbols (
          id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
          module TEXT NOT NULL, file TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
          start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, data TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS symbol_scope ON code_symbols(snapshot_id,module,name);
        CREATE TABLE IF NOT EXISTS code_relations (
          id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
          source_id TEXT NOT NULL, target_id TEXT, kind TEXT NOT NULL, data TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS relation_scope ON code_relations(snapshot_id,source_id,target_id);
        CREATE TABLE IF NOT EXISTS agent_runs (
          id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
          snapshot_id TEXT NOT NULL REFERENCES snapshots(id), module TEXT NOT NULL,
          status TEXT NOT NULL, stage TEXT NOT NULL, budget TEXT NOT NULL,
          cancelled INTEGER NOT NULL DEFAULT 0, bundle TEXT, bundle_hash TEXT,
          decision TEXT, reviewer TEXT, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES agent_runs(id),
          stage TEXT NOT NULL, details TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS code_answers (
          id TEXT PRIMARY KEY, actor TEXT NOT NULL, snapshot_id TEXT NOT NULL,
          question TEXT NOT NULL, result TEXT, job_id TEXT NOT NULL,
          idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL, created_at REAL NOT NULL,
          UNIQUE(actor,idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS answer_feedback (
          id INTEGER PRIMARY KEY AUTOINCREMENT, answer_id TEXT NOT NULL,
          actor TEXT NOT NULL, kind TEXT NOT NULL, comment TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS features (
          id TEXT NOT NULL, snapshot_id TEXT NOT NULL, title TEXT NOT NULL,
          claims TEXT NOT NULL, evidence_ids TEXT NOT NULL, status TEXT NOT NULL,
          reviewer TEXT, created_at REAL NOT NULL, PRIMARY KEY(id,snapshot_id)
        );
        CREATE TABLE IF NOT EXISTS deployments (
          tenant TEXT PRIMARY KEY, release TEXT NOT NULL, snapshot_id TEXT,
          knowledge_snapshot_id INTEGER, flags TEXT NOT NULL, revision INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workflow_versions (
          id TEXT NOT NULL, version TEXT NOT NULL, spec TEXT NOT NULL,
          digest TEXT NOT NULL, status TEXT NOT NULL, validation TEXT,
          reviewer TEXT, created_at REAL NOT NULL, PRIMARY KEY(id,version)
        );
        CREATE TABLE IF NOT EXISTS knowledge_heads (
          release TEXT PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES knowledge_snapshots(id)
        );
        CREATE TABLE IF NOT EXISTS manual_patches (
          id TEXT PRIMARY KEY, baseline_id INTEGER NOT NULL REFERENCES manual_revisions(id),
          proposed TEXT NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS operation_tasks (
          id TEXT PRIMARY KEY, topic TEXT NOT NULL, kind TEXT NOT NULL,
          status TEXT NOT NULL, owner TEXT NOT NULL, note TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS legacy_runs (
          id TEXT PRIMARY KEY, source_run_id INTEGER, request_hash TEXT NOT NULL,
          idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
          context TEXT NOT NULL, result TEXT, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS git_events (
          id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, job_id TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL,
          action TEXT NOT NULL, resource TEXT NOT NULL, details TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_documents (
          id TEXT PRIMARY KEY, release TEXT NOT NULL, name TEXT NOT NULL,
          hash TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL,
          UNIQUE(release,hash)
        );
        CREATE TABLE IF NOT EXISTS repository_acl (
          repository_id TEXT NOT NULL, actor TEXT NOT NULL, allowed INTEGER NOT NULL,
          PRIMARY KEY(repository_id,actor)
        );
        CREATE TABLE IF NOT EXISTS manual_visibility (
          tenant TEXT NOT NULL, page_id TEXT NOT NULL, allowed INTEGER NOT NULL,
          PRIMARY KEY(tenant,page_id)
        );
        CREATE TABLE IF NOT EXISTS legacy_script_approvals (
          source_revision_id INTEGER PRIMARY KEY, digest TEXT NOT NULL,
          reviewer TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS analysis_materials (
          id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
          module TEXT NOT NULL, kind TEXT NOT NULL, target_id TEXT NOT NULL,
          payload_hash TEXT NOT NULL, content TEXT NOT NULL, reviewer TEXT NOT NULL,
          created_at REAL NOT NULL
        );
        """)
        for table, column, definition in (
            ("preparations", "workflow_version", "TEXT NOT NULL DEFAULT '0.1.0'"),
            ("preparations", "workflow_digest", "TEXT NOT NULL DEFAULT ''"),
            ("questions", "feedback", "TEXT"),
            ("manual_patches", "evidence_ids", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            columns = {row["column_name"] for row in con.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=?", (table,))} if DATABASE_URL else {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        con.execute("INSERT INTO knowledge_heads SELECT release,MAX(id) FROM knowledge_snapshots GROUP BY release ON CONFLICT DO NOTHING")


def audit(actor: str, action: str, resource: str, details: dict, con=None) -> None:
    if con is None:
        with db() as owned:
            return audit(actor, action, resource, details, owned)
    con.execute("INSERT INTO audit_log(actor,action,resource,details,created_at) VALUES (?,?,?,?,?)",
                (actor, action, resource, pack(details), now()))
