"""模型接入自检：配置状态、密钥打码、连通性探针。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config.settings import get_settings
from app.llm.client import LLMError
from app.main import app

client = TestClient(app)


def test_healthz_reports_llm_mode():
    body = client.get("/healthz").json()
    llm = body["llm"]
    assert set(llm) == {"configured", "model", "provider", "mode"}
    assert llm["mode"] == ("llm" if llm["configured"] else "rules")
    assert "api_key" not in str(body)  # 探针接口绝不回显密钥


def test_llm_status_masks_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_api_key", "sk-1234567890abcdef")

    body = client.get("/api/v1/system/llm").json()
    assert body["configured"] is True
    assert body["api_key_masked"] == "sk-1…cdef"
    assert "1234567890" not in str(body)
    assert body["hint"] == ""


def test_llm_test_fails_closed_when_unconfigured(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_api_key", "")

    body = client.post("/api/v1/system/llm-test").json()
    assert body["ok"] is False
    assert body["configured"] is False
    assert "LLM_API_KEY" in body["error"]


def test_llm_test_passes_through_when_configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_api_key", "sk-test-key-0001")

    async def fake_chat(messages, **kwargs):
        assert messages and kwargs["max_tokens"] == 16
        return "模型可用"

    monkeypatch.setattr("app.api.routes.system.chat_text", fake_chat)

    body = client.post("/api/v1/system/llm-test").json()
    assert body["ok"] is True
    assert body["reply"] == "模型可用"
    assert body["latency_ms"] >= 0
    assert body["error"] == ""


def test_llm_test_reports_upstream_error(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_api_key", "sk-test-key-0002")

    async def fake_chat(messages, **kwargs):
        raise LLMError("HTTP 401: invalid api key")

    monkeypatch.setattr("app.api.routes.system.chat_text", fake_chat)

    body = client.post("/api/v1/system/llm-test").json()
    assert body["ok"] is False
    assert "401" in body["error"]
