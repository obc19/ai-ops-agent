"""API 测试：健康检查、检测触发、告警与知识库流转。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_overview():
    resp = client.get("/api/v1/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert "stats" in body and "mode" in body
    assert body["mode"]["mock"] is True


def test_mock_fault_and_detect():
    assert client.post("/_mock/fault", json={"target": "order-service"}).status_code == 200
    resp = client.post("/api/v1/diagnostics/detect", json={"notify": False})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items, "注入故障后应检出异常"
    report_id = items[0]["report_id"]
    assert client.get(f"/api/v1/diagnostics/{report_id}").json()["id"] == report_id
    assert client.get(f"/api/v1/diagnostics/{report_id}/markdown").status_code == 200
    client.post("/_mock/reset")


def test_manual_diagnosis_and_alert_flow():
    resp = client.post(
        "/api/v1/diagnostics",
        json={"target": "manual-svc", "metric": "cpu_usage_percent", "severity": "P2", "notify": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    alert_id = body["alert"]["id"]
    assert client.get(f"/api/v1/alerts/{alert_id}").status_code == 200
    confirmed = client.post(
        f"/api/v1/alerts/{alert_id}/confirm", json={"confirmed_by": "tester"}
    )
    assert confirmed.json()["status"] == "confirmed"
    resolved = client.post(f"/api/v1/alerts/{alert_id}/resolve", json={"note": "已处理"})
    assert resolved.json()["status"] == "resolved"


def test_knowledge_flow_via_api():
    diagnosis = client.post(
        "/api/v1/diagnostics",
        json={"target": "knowledge-svc", "metric": "memory_usage_percent", "notify": False},
    ).json()
    report_id = diagnosis["report"]["id"]

    draft = client.post("/api/v1/knowledge", json={"diagnosis_id": report_id, "tags": ["memory"]})
    assert draft.status_code == 200
    exp_id = draft.json()["id"]

    confirmed = client.post(
        f"/api/v1/knowledge/{exp_id}/confirm", json={"confirmed_by": "tester"}
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"

    stats = client.get("/api/v1/knowledge/stats").json()
    assert stats["confirmed"] >= 1

    rolled = client.post(
        f"/api/v1/knowledge/{exp_id}/rollback", json={"reason": "测试回滚"}
    )
    assert rolled.json()["status"] == "rolled_back"


def test_audit_endpoint():
    resp = client.get("/api/v1/audit", params={"limit": 10})
    assert resp.status_code == 200
    assert "items" in resp.json()


def test_unauthorized_when_token_set(monkeypatch):
    from app.config.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "auth_token", "secret-token")
    assert client.get("/api/v1/overview").status_code == 401
    ok = client.get("/api/v1/overview", headers={"Authorization": "Bearer secret-token"})
    assert ok.status_code == 200
