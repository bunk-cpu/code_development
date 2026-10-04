"""Native backups and read-only verification; credentials stay in child env."""
import argparse
import hashlib
import os
import sqlite3
import subprocess
from pathlib import Path
from . import store


def pg_env():
    values = store.psycopg.conninfo.conninfo_to_dict(store.DATABASE_URL)
    env = os.environ.copy()
    for key, variable in {"host": "PGHOST", "port": "PGPORT", "dbname": "PGDATABASE", "user": "PGUSER", "password": "PGPASSWORD", "sslmode": "PGSSLMODE", "options": "PGOPTIONS"}.items():
        if key in values:
            env[variable] = values[key]
    return env


def backup(destination: Path):
    if destination.exists():
        raise ValueError("BACKUP_ALREADY_EXISTS")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if store.DATABASE_URL:
        subprocess.run(["pg_dump", "--format=custom", "--file", str(destination)], env=pg_env(), check=True, capture_output=True)
    else:
        with sqlite3.connect(store.DB_PATH) as source, sqlite3.connect(destination) as target:
            source.backup(target)
        checkpoint = store.DB_PATH.with_suffix(".agent.sqlite3")
        if checkpoint.exists():
            with sqlite3.connect(checkpoint) as source, sqlite3.connect(destination.with_suffix(".agent.sqlite3")) as target:
                source.backup(target)
    destination.chmod(0o600)
    return {"file": str(destination), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}


def verify(source: Path):
    if source.read_bytes()[:5] == b"PGDMP":
        subprocess.run(["pg_restore", "--list", str(source)], check=True, capture_output=True)
    else:
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as con:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("BACKUP_CORRUPT")
    return {"verified": True, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("backup", "verify"))
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    print(backup(args.file) if args.action == "backup" else verify(args.file))


if __name__ == "__main__":
    main()
