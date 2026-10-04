"""Start the three local processes; stop only children created by this launcher."""
import signal
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from source_demo.seed import seed
from source_demo.store import init


def main():
    for port in (8000, 9100):
        with socket.socket() as connection:
            connection.settimeout(0.5)
            if connection.connect_ex(("127.0.0.1", port)) == 0:
                raise SystemExit(f"端口 {port} 已被使用，请使用已启动实例或先停止自己的服务")
    init()
    seed()
    commands = [
        [sys.executable, "-m", "uvicorn", "source_demo.mock_business:app", "--host", "127.0.0.1", "--port", "9100"],
        [sys.executable, "-m", "uvicorn", "source_demo.api:app", "--host", "127.0.0.1", "--port", "8000"],
        [sys.executable, "-m", "source_demo.jobs"],
    ]
    processes = []
    def stop(*_args):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, stop)
    try:
        processes = [subprocess.Popen(command, cwd=ROOT) for command in commands]
        print("内部工作台 http://127.0.0.1:8000/internal-ui\n客户门户 http://127.0.0.1:8000/customer-ui", flush=True)
        while all(p.poll() is None for p in processes):
            try:
                processes[0].wait(timeout=1)
            except subprocess.TimeoutExpired:
                continue
        raise SystemExit("一个子进程已退出，请查看其错误输出")
    except KeyboardInterrupt:
        pass
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
