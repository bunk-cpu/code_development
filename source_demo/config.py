"""配置只在服务端加载；复用现有 DeepSeek 配置，不复制密钥。"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)


def legacy_env() -> dict:
    path = Path(os.getenv("TESTPILOT_ENV_FILE", "/root/testpilot-plus/.env"))
    return dict(dotenv_values(path)) if path.is_file() else {}


def model_config() -> dict:
    old = legacy_env()
    return {
        "base_url": os.getenv("DEEPSEEK_BASE_URL") or old.get("GATEWAY_BASE_URL", "https://api.deepseek.com"),
        "api_key": os.getenv("DEEPSEEK_API_KEY") or old.get("GATEWAY_API_KEY", ""),
        "model": os.getenv("DEEPSEEK_MODEL") or old.get("GATEWAY_MODEL", "deepseek-chat"),
        "timeout": float(os.getenv("MODEL_TIMEOUT_S", "60")),
    }


def model_status() -> dict:
    cfg = model_config()
    return {"configured": bool(cfg["api_key"]), "model": cfg["model"],
            "source": "environment / testpilot env", "thinking": "disabled"}


def integration_credentials() -> tuple[str, str]:
    old = legacy_env()
    return (os.getenv("TESTPILOT_EMAIL") or old.get("ADMIN_EMAIL", ""),
            os.getenv("TESTPILOT_PASSWORD") or old.get("ADMIN_PASSWORD", ""))
