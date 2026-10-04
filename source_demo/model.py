"""DeepSeek wire 协议；不把兼容协议或 JSON 合法误认成业务正确。"""
from __future__ import annotations

import hashlib
import json
import time
from urllib.parse import urlparse

import httpx

from .config import model_config
from .store import pack


class ModelError(RuntimeError):
    pass


def complete(messages: list[dict], *, tools: list | None = None,
             json_mode: bool = False, max_tokens: int = 4000, timeout: float | None = None) -> dict:
    cfg = model_config()
    if not cfg["api_key"]:
        raise ModelError("MODEL_NOT_CONFIGURED")
    url = cfg["base_url"].rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
        raise ModelError("MODEL_ENDPOINT_INVALID")
    body = {"model": cfg["model"], "messages": messages, "max_tokens": max_tokens,
            "stream": False, "thinking": {"type": "disabled"}}
    if tools:
        body.update(tools=tools, tool_choice="auto")
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    started = time.monotonic()
    for attempt in range(2):
        try:
            with httpx.Client(timeout=timeout or cfg["timeout"], follow_redirects=False) as client:
                response = client.post(url + "/chat/completions", json=body,
                                       headers={"Authorization": "Bearer " + cfg["api_key"]})
            if response.status_code in (429, 502, 503, 504) and attempt == 0:
                time.sleep(0.2)
                continue
            if response.status_code != 200:
                raise ModelError(f"MODEL_HTTP_{response.status_code}")
            data = response.json()
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ModelError("MODEL_OUTPUT_TRUNCATED")
            message = choice["message"]
            calls = message.get("tool_calls") or []
            if len(calls) > 16 or len({c["id"] for c in calls}) != len(calls):
                raise ModelError("MODEL_TOOL_PROTOCOL_INVALID")
            if not calls and not message.get("content"):
                raise ModelError("MODEL_EMPTY_OUTPUT")
            # 非思考模式，不持久化供应商的隐藏推理字段。
            message = {k: v for k, v in message.items() if k in ("role", "content", "tool_calls")}
            return {"message": message, "usage": data.get("usage", {}), "model": cfg["model"],
                    "request_id": data.get("id"), "request_hash": hashlib.sha256(pack(body).encode()).hexdigest(),
                    "latency_ms": round((time.monotonic() - started) * 1000)}
        except (httpx.HTTPError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ModelError("MODEL_PROVIDER_ERROR:" + type(exc).__name__) from None
    raise ModelError("MODEL_PROVIDER_ERROR")


def structured(system: str, data: dict, **kwargs) -> tuple[dict, dict]:
    result = complete([{"role": "system", "content": system + " 返回 JSON 对象。"},
                       {"role": "user", "content": pack(data)}], json_mode=True, **kwargs)
    try:
        parsed = json.loads(result["message"]["content"])
        if not isinstance(parsed, dict):
            raise ValueError()
    except (ValueError, TypeError):
        raise ModelError("MODEL_INVALID_JSON") from None
    return parsed, {k: v for k, v in result.items() if k != "message"}
