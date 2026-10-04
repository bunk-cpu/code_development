"""检查工具链、数据库和可选真实联通，不输出凭据。"""
import argparse
import shutil
import sys
from . import config, java_source, legacy, model, store


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="访问模型、TestPilot 和只读 Mock")
    args = parser.parse_args()
    store.init()
    checks = {"Python 3.12+": sys.version_info >= (3, 12),
              "Git / JDK / Maven": all(shutil.which(c) for c in ("git", "java", "mvn")),
              "JDT 可执行产物": java_source.JAR.is_file(), "数据库可读写": False,
              "前端两个独立构建": all((store.ROOT / "frontend/dist" / a / f"{a}.html").is_file() for a in ("internal", "customer")),
              "DeepSeek 配置": config.model_status()["configured"]}
    with store.db() as con:
        checks["数据库可读写"] = store.one(con, "SELECT 1 AS ok") == {"ok": 1}
    if args.live:
        try:
            result, _ = model.structured("返回 JSON 对象，字段 ok 固定为 true。", {"task": "connection_check"}, max_tokens=100)
            checks["DeepSeek 实际响应"] = result.get("ok") is True
        except model.ModelError:
            checks["DeepSeek 实际响应"] = False
        checks["TestPilot 独立认证"] = legacy.status()["status"] == "connected"
        try:
            from .jobs import _mock
            checks["只读业务 Mock"] = _mock({"tenant": "tenant-a", "status": "pending", "date_from": "2026-09-01", "date_to": "2026-09-07"})["total"] == 2
        except Exception:
            checks["只读业务 Mock"] = False
    for name, ok in checks.items():
        print(("OK  " if ok else "FAIL") + name)
    print("数据库:", "PostgreSQL" if store.DATABASE_URL else "SQLite 本机兼容模式")
    print("模型:", config.model_status()["model"], "（密钥不显示）")
    print("语义边界: JDT 声明/调用候选不证明实际 Spring 装配和生产部署")
    if not all(checks.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
