"""Import into an empty PostgreSQL database; never clears existing data."""
import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from source_demo import store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    if not store.DATABASE_URL or not args.source.is_file():
        raise SystemExit("需要 PostgreSQL DATABASE_URL 和存在的 SQLite 来源")
    store.init()
    with sqlite3.connect(f"file:{args.source}?mode=ro", uri=True) as old, store.db() as new:
        old.row_factory = sqlite3.Row
        tables = [r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid")]
        allowed = {r["table_name"]: set() for r in store.many(new, "SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_type='BASE TABLE'")}
        for row in store.many(new, "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema=current_schema()"):
            if row["table_name"] in allowed:
                allowed[row["table_name"]].add(row["column_name"])
        if any(not re.fullmatch(r"[a-z][a-z0-9_]*", table) or table not in allowed for table in tables):
            raise SystemExit("来源含未知数据表，拒绝导入")
        if any(store.one(new, f'SELECT COUNT(*) AS n FROM "{table}"')["n"] for table in tables):
            raise SystemExit("目标已有数据，拒绝覆盖")
        for table in tables:
            rows = list(old.execute(f'SELECT * FROM "{table}"'))
            if rows:
                columns = rows[0].keys()
                if any(not re.fullmatch(r"[a-z][a-z0-9_]*", c) or c not in allowed[table] for c in columns):
                    raise SystemExit("来源含未知字段，拒绝导入")
                new.executemany(f'INSERT INTO "{table}" (' + ",".join('"' + c + '"' for c in columns) + ') VALUES (' + ",".join("?" for _ in columns) + ")", [tuple(r) for r in rows])
        for row in store.many(new, "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema=current_schema() AND column_default LIKE 'nextval(%'"):
            table, column = row["table_name"], row["column_name"]
            new.execute(f"SELECT setval(pg_get_serial_sequence(?,?), COALESCE(MAX(\"{column}\"),1), MAX(\"{column}\") IS NOT NULL) FROM \"{table}\"", (table, column))
    print("业务数据已原子导入 PostgreSQL；SQLite 原件及旧检查点保留供审计。")


if __name__ == "__main__":
    main()
