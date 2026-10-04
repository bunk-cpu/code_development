"""Create a disposable Git history; the tracked fixture and user repositories remain untouched."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent.parent


def prepare(destination: Path | None = None) -> dict:
    target = destination or ROOT / "data/java-fixture"
    if not (target / ".git").exists():
        shutil.copytree(ROOT / "fixtures/java-project", target, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("target"))
        def git(*args):
            return subprocess.run(["git", "-C", str(target), *args], check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-b", "main")
        git("config", "user.name", "Local fixture"); git("config", "user.email", "fixture@example.invalid")
        git("add", "."); git("commit", "-m", "base: export 1..5000 with tenant guard"); git("tag", "base")
        path = target / "common/src/main/java/demo/common/OrderPolicy.java"
        path.write_text(path.read_text().replace("limit > 5000", "limit > 1000"))
        moved = path.with_name("ExportPolicy.java")
        moved.write_text(path.read_text().replace("OrderPolicy", "ExportPolicy")); path.unlink()
        service = target / "orders/src/main/java/demo/orders/ExportService.java"
        service.write_text(service.read_text().replace("OrderPolicy", "ExportPolicy"))
        git("add", "."); git("commit", "-m", "change: export 1..1000 and policy rename"); git("tag", "change")
    return {"root": str(target), "base": "base", "change": "change"}


if __name__ == "__main__":
    print(prepare())
