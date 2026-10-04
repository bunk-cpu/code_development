"""Provider faults and concurrent leases must fail safely on both database backends."""
import json
from concurrent.futures import ThreadPoolExecutor
import httpx
import pytest
from test_demo import users
from source_demo import jobs, knowledge, legacy, model, store


def test_provider_faults_and_tool_protocol(monkeypatch):
    monkeypatch.setattr(model, "model_config", lambda: {"base_url": "https://model.example.invalid", "api_key": "fixture-secret", "model": "fixture", "timeout": 1})
    client = httpx.Client
    replies = []
    def handle(request):
        assert request.headers["authorization"] == "Bearer fixture-secret"
        payload = json.loads(request.content)
        assert payload["thinking"]["type"] == "disabled"
        status, body = replies.pop(0)
        return httpx.Response(status, json=body)
    monkeypatch.setattr(model.httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
    good = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"ok":true}', "reasoning_content": "hidden"}}], "usage": {"total_tokens": 3}}
    replies.extend([(429, {}), (200, good)])
    result = model.complete([{"role": "user", "content": "JSON check"}], json_mode=True)
    assert not replies and "hidden" not in str(result) and result["usage"]["total_tokens"] == 3
    for status, body, expected in [(401, {"secret": "never_echo"}, "MODEL_HTTP_401"), (200, {"choices": []}, "MODEL_PROVIDER_ERROR:IndexError"), (200, {"choices": [{"finish_reason": "length"}]}, "MODEL_OUTPUT_TRUNCATED")]:
        replies.append((status, body))
        with pytest.raises(model.ModelError, match=expected) as failure:
            model.complete([])
        assert "never_echo" not in str(failure.value)
    replies.append((200, {"choices": [{"message": {"role": "assistant", "content": "[]"}}]}))
    with pytest.raises(model.ModelError, match="MODEL_INVALID_JSON"):
        model.structured("JSON", {})
    assert legacy.sanitized({"password": "private", "stderr": "[密码] 管理员: None → private\ntoken=private-value"}) == {"password": "[REDACTED]", "stderr": "[凭据日志已脱敏]\ntoken=[已脱敏]"}
    html = knowledge.render_content('<script>alert(1)</script>\n\n![tracking](https://example.invalid/img) [link](javascript:alert(1))')
    assert "<script" not in html and "<img" not in html and 'href="javascript:' not in html


def test_concurrent_claims_and_expired_worker_fence(users):
    queued = [jobs.enqueue("fixture", "internal", "analyst", {"n": n}) for n in range(12)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = list(pool.map(lambda n: jobs.claim("worker-" + str(n)), range(12)))
    assert {j["id"] for j in claimed} == {j["id"] for j in queued}
    original = claimed[0]
    with store.db() as con:
        con.execute("UPDATE jobs SET lease_until=? WHERE id=?", (store.now() - 1, original["id"]))
    recovered = jobs.claim("recovered")
    assert recovered["id"] == original["id"] and recovered["run_epoch"] == original["run_epoch"] + 1
    assert not jobs.finish(original, {"incorrect": True}, None)
    assert jobs.finish(recovered, {"correct": True}, None)
