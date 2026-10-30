"""服务器本体采集：/proc 解析、CPU 差分、历史序列、缺 /proc 时的降级。"""

from __future__ import annotations

import pytest

from app.collectors import host_adapter
from app.collectors.host_adapter import HostCollector, reset_history
from app.config.settings import Settings

STAT_A = "cpu  1000 50 800 7000 300 0 0 0 0 0\ncpu0 500 25 400 3500 150 0 0 0 0 0\n"
STAT_B = "cpu  1200 50 900 7400 400 0 0 0 0 0\ncpu0 600 25 450 3700 200 0 0 0 0 0\n"


def _make_proc(root):
    (root / "sys" / "fs").mkdir(parents=True)
    (root / "net").mkdir()
    (root / "stat").write_text(STAT_A, encoding="utf-8")
    (root / "loadavg").write_text("8.40 6.10 5.20 3/512 99887\n", encoding="utf-8")
    (root / "cpuinfo").write_text(
        "processor\t: 0\nmodel name\t: Xeon\n\nprocessor\t: 1\nmodel name\t: Xeon\n",
        encoding="utf-8",
    )
    (root / "meminfo").write_text(
        "MemTotal:        1000000 kB\n"
        "MemFree:           20000 kB\n"
        "MemAvailable:      50000 kB\n"
        "Buffers:            1000 kB\n"
        "Cached:            4000 kB\n"
        "SwapTotal:        200000 kB\n"
        "SwapFree:         100000 kB\n",
        encoding="utf-8",
    )
    (root / "sys" / "fs" / "file-nr").write_text("9000\t0\t10000\n", encoding="utf-8")
    (root / "net" / "tcp").write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid\n"
        "   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0\n"
        "   1: 0100007F:1F90 0100007F:D431 01 00000000:00000000 00:00000000 00000000     0\n"
        "   2: 0100007F:1F90 0100007F:D432 01 00000000:00000000 00:00000000 00000000     0\n",
        encoding="utf-8",
    )
    for pid, state in ((101, "S"), (102, "R"), (103, "Z")):
        proc = root / str(pid)
        proc.mkdir()
        (proc / "stat").write_text(
            f"{pid} (worker 1) {state} 1 1 1 0 -1 0 0 0 0 0 0 0 0 0\n", encoding="utf-8"
        )


def _settings(root, **overrides):
    base = dict(
        server_enabled=True,
        server_proc_root=str(root),
        server_disk_paths=str(root),
        server_sample_interval_ms=50,
    )
    base.update(overrides)
    return Settings(**base)


async def test_parses_host_metrics(tmp_path):
    _make_proc(tmp_path)
    reset_history()
    events = await HostCollector(_settings(tmp_path)).query(None)
    got = {e.metric: e.value for e in events}

    assert got["host_load1"] == pytest.approx(8.4)
    assert got["host_cpu_cores"] == pytest.approx(2.0)
    assert got["host_load_per_core"] == pytest.approx(4.2)
    # MemAvailable 50000 / 1000000 → 已用 95%
    assert got["host_memory_used_percent"] == pytest.approx(95.0)
    assert got["host_swap_used_percent"] == pytest.approx(50.0)
    assert got["host_fd_used_percent"] == pytest.approx(90.0)
    assert got["host_tcp_established"] == pytest.approx(2.0)
    assert got["host_process_count"] == pytest.approx(3.0)
    assert got["host_zombie_processes"] == pytest.approx(1.0)
    assert "host_disk_used_percent" in got

    load = next(e for e in events if e.metric == "host_load1")
    assert load.unit == ""
    assert load.namespace == "host"


async def test_cpu_usage_uses_delta(monkeypatch, tmp_path):
    _make_proc(tmp_path)
    collector = HostCollector(_settings(tmp_path))
    # 总时间 +500，其中 idle+iowait +350，iowait +50 → 使用率 30%，iowait 10%
    seq = iter([(10000.0, 7000.0, 200.0), (10500.0, 7350.0, 250.0)])
    monkeypatch.setattr(HostCollector, "_cpu_raw", lambda self: next(seq))

    usage, iowait = await collector._cpu_usage()
    assert usage == pytest.approx(30.0)
    assert iowait == pytest.approx(10.0)


async def test_history_accumulates_across_instances(tmp_path):
    _make_proc(tmp_path)
    reset_history()
    # 采集器每轮都会重建，历史必须跨实例累积，否则环比/基线永远只有一个点
    first = await HostCollector(_settings(tmp_path)).query(None)
    second = await HostCollector(_settings(tmp_path)).query(None)

    before = {e.metric: len(e.samples) for e in first}
    after = {e.metric: len(e.samples) for e in second}
    assert before["host_load1"] == 1
    assert after["host_load1"] == 2
    assert after["host_load5"] == 2


async def test_missing_proc_on_linux_reports_unreachable(tmp_path, monkeypatch):
    # 模拟「是 Linux 但 SERVER_PROC_ROOT 配错」：应提示为配置错误，而不是不适用
    monkeypatch.setattr(HostCollector, "supported", property(lambda self: True))
    collector = HostCollector(_settings(tmp_path / "not-exist"))
    assert collector.configured is False
    assert await collector.query(None) == []

    health = await collector.health_check()
    assert health.configured is True
    assert health.reachable is False
    assert "SERVER_PROC_ROOT" in health.detail


async def test_non_linux_is_not_applicable(tmp_path, monkeypatch):
    # Windows / macOS 没有 /proc，属于环境不适用，不该算成配置错误
    monkeypatch.setattr(HostCollector, "supported", property(lambda self: False))
    health = await HostCollector(_settings(tmp_path)).health_check()
    assert health.configured is False
    assert "非 Linux" in health.detail


async def test_disabled_returns_nothing(tmp_path):
    _make_proc(tmp_path)
    collector = HostCollector(_settings(tmp_path, server_enabled=False))
    assert collector.configured is False
    assert await collector.query(None) == []


def test_proc_root_from_string_settings():
    # 配置里写的是字符串路径，派生属性要能正确展开
    collector = HostCollector(Settings(server_proc_root="/host/proc", server_disk_paths="/a,/b"))
    assert str(collector.proc_root).endswith("host\\proc") or str(
        collector.proc_root
    ).endswith("host/proc")
    assert collector.disk_paths == ["/a", "/b"]
