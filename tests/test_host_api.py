"""服务器状态接口：能读到 /proc 时返回指标，读不到时明确说明原因。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config.settings import get_settings
from app.main import app

client = TestClient(app)


def _make_proc(root):
    (root / "sys" / "fs").mkdir(parents=True)
    (root / "net").mkdir()
    (root / "stat").write_text("cpu  1 0 1 8 0 0 0 0 0 0\n", encoding="utf-8")
    (root / "loadavg").write_text("1.20 1.10 0.90 1/100 4242\n", encoding="utf-8")
    (root / "cpuinfo").write_text("processor\t: 0\n", encoding="utf-8")
    (root / "meminfo").write_text(
        "MemTotal: 1000000 kB\nMemAvailable: 400000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n",
        encoding="utf-8",
    )
    (root / "sys" / "fs" / "file-nr").write_text("100\t0\t10000\n", encoding="utf-8")
    (root / "net" / "tcp").write_text("  sl  local_address rem_address   st\n", encoding="utf-8")
    proc = root / "77"
    proc.mkdir()
    (proc / "stat").write_text("77 (app) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0\n", encoding="utf-8")


@pytest.fixture
def proc_settings(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    _make_proc(root)
    settings = get_settings()
    monkeypatch.setattr(settings, "server_enabled", True)
    monkeypatch.setattr(settings, "server_proc_root", str(root))
    monkeypatch.setattr(settings, "server_disk_paths", str(tmp_path))
    monkeypatch.setattr(settings, "server_sample_interval_ms", 50)
    return root


def test_host_endpoint_returns_metrics(proc_settings):
    resp = client.get("/api/v1/host")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["host"]
    metrics = {item["metric"] for item in body["items"]}
    assert "host_load1" in metrics
    assert "host_memory_used_percent" in metrics
    memory = next(i for i in body["items"] if i["metric"] == "host_memory_used_percent")
    assert memory["value"] == pytest.approx(60.0)
    assert memory["warn"] is False


def test_host_endpoint_reports_unavailable(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "server_enabled", True)
    monkeypatch.setattr(settings, "server_proc_root", str(tmp_path / "missing"))
    resp = client.get("/api/v1/host")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["items"] == []
    assert body["reason"]
