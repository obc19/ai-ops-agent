"""主动拨测：分段计时、状态判定、配置解析、不可达降级。"""

from __future__ import annotations

import http.server
import threading

import pytest
import yaml

from app.collectors.probe_adapter import ProbeCollector, load_probes, reset_history
from app.config.settings import Settings


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):  # noqa: N802 - 标准库命名
        body = b"ok"
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静默
        pass


@pytest.fixture
def local_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _write_probes(tmp_path, targets):
    path = tmp_path / "probes.yaml"
    path.write_text(yaml.safe_dump({"targets": targets}, allow_unicode=True), encoding="utf-8")
    return path


def _settings(tmp_path, cfg):
    return Settings(probe_enabled=True, probe_config=str(cfg), probe_timeout=3)


def test_load_probes_parses_targets(tmp_path):
    cfg = _write_probes(
        tmp_path,
        [{"name": "web", "url": "http://a.example/", "expect_status": 200}, {"url": "http://b/"}],
    )
    target_list = load_probes(_settings(tmp_path, cfg))
    assert [t.name for t in target_list] == ["web", "b"]  # 未给 name 时用 host
    assert target_list[0].expect_status == 200


async def test_empty_targets_disables_probe(tmp_path):
    path = tmp_path / "probes.yaml"
    path.write_text("targets: []\n", encoding="utf-8")
    collector = ProbeCollector(_settings(tmp_path, path))
    assert collector.targets == []
    assert collector.configured is False
    assert await collector.query(None) == []
    health = await collector.health_check()
    assert health.reachable is False


async def test_explicit_missing_config_falls_back_to_bundled(tmp_path):
    # 显式路径写错时回退到内置 probes.yaml（至少保留 self-console，能力不至于静默消失）
    collector = ProbeCollector(_settings(tmp_path, tmp_path / "nope.yaml"))
    assert collector.configured is True
    assert collector.targets


async def test_probe_measures_segments(tmp_path, local_server):
    cfg = _write_probes(
        tmp_path, [{"name": "local", "url": f"http://127.0.0.1:{local_server}/", "expect_status": 200}]
    )
    reset_history()
    events = await ProbeCollector(_settings(tmp_path, cfg)).query(None)
    got = {e.metric: e.value for e in events}

    assert got["probe_ok"] == 1.0
    assert got["probe_total_ms"] > 0
    assert got["probe_ttfb_ms"] >= 0
    # 直接是 IP，没有 DNS 环节
    assert got["probe_dns_ms"] == 0.0
    assert all(e.namespace == "probe" for e in events)
    assert all(e.target == "local" for e in events)


async def test_probe_flags_unreachable(tmp_path):
    # 1 端口基本不会有人监听，必然连接失败
    cfg = _write_probes(tmp_path, [{"name": "dead", "url": "http://127.0.0.1:1/"}])
    events = await ProbeCollector(_settings(tmp_path, cfg)).query(None)
    got = {e.metric: e.value for e in events}
    assert got["probe_ok"] == 0.0
    ok_event = next(e for e in events if e.metric == "probe_ok")
    assert ok_event.labels.get("error")


async def test_probe_expect_status_mismatch(tmp_path, local_server):
    cfg = _write_probes(
        tmp_path, [{"name": "strict", "url": f"http://127.0.0.1:{local_server}/", "expect_status": 201}]
    )
    events = await ProbeCollector(_settings(tmp_path, cfg)).query(None)
    got = {e.metric: e.value for e in events}
    assert got["probe_ok"] == 0.0


async def test_probe_history_accumulates(tmp_path, local_server):
    cfg = _write_probes(
        tmp_path, [{"name": "local", "url": f"http://127.0.0.1:{local_server}/"}]
    )
    reset_history()
    first = await ProbeCollector(_settings(tmp_path, cfg)).query(None)
    second = await ProbeCollector(_settings(tmp_path, cfg)).query(None)
    assert len(next(e for e in first if e.metric == "probe_total_ms").samples) == 1
    assert len(next(e for e in second if e.metric == "probe_total_ms").samples) == 2
