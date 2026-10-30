"""服务器实时状态：控制台首屏「服务器」与「拨测」卡片的数据源。

这里刻意**不写历史**：控制台刷新频率不可控，若混进检测基线会造成采样间隔不均。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import require_auth
from app.collectors.host_adapter import HostCollector
from app.collectors.probe_adapter import ProbeCollector
from app.models.events import MetricEvent

router = APIRouter(tags=["host"])

_HOST_ITEMS: list[tuple[str, str]] = [
    ("host_load1", "1 分钟负载"),
    ("host_load_per_core", "每核负载"),
    ("host_cpu_usage_percent", "CPU 使用率"),
    ("host_cpu_iowait_percent", "IO 等待"),
    ("host_memory_used_percent", "内存使用率"),
    ("host_swap_used_percent", "Swap"),
    ("host_disk_used_percent", "磁盘使用率"),
    ("host_fd_used_percent", "文件描述符"),
    ("host_tcp_established", "已建立连接"),
    ("host_process_count", "进程数"),
    ("host_zombie_processes", "僵尸进程"),
]

# 超过即标红的警戒线，与 rules-detection.yaml 的第一档阈值对齐
_WARN: dict[str, float] = {
    "host_load_per_core": 2,
    "host_cpu_usage_percent": 85,
    "host_cpu_iowait_percent": 20,
    "host_memory_used_percent": 90,
    "host_swap_used_percent": 30,
    "host_disk_used_percent": 85,
    "host_fd_used_percent": 80,
}


@router.get("/host", summary="服务器实时状态")
async def host_status(actor: str = Depends(require_auth)) -> dict:
    collector = HostCollector()
    if not collector.configured:
        return {
            "available": False,
            "reason": collector.unavailable_reason,
            "host": "",
            "items": [],
            "probes": [],
        }

    events = await collector.query(None, record_history=False)
    latest: dict[str, MetricEvent] = {}
    for event in events:
        latest.setdefault(event.metric, event)

    items: list[dict] = []
    for metric, label in _HOST_ITEMS:
        event = latest.get(metric)
        if event is None:
            continue
        threshold = _WARN.get(metric)
        items.append(
            {
                "metric": metric,
                "label": label,
                "unit": event.unit,
                "value": event.value,
                "warn": bool(
                    threshold is not None and event.value is not None and event.value >= threshold
                ),
            }
        )

    return {
        "available": True,
        "host": events[0].target if events else "",
        "proc_root": str(collector.proc_root),
        "items": items,
        "probes": await _probe_items(),
    }


async def _probe_items() -> list[dict]:
    collector = ProbeCollector()
    if not collector.targets:
        return []
    events = await collector.query(None, record_history=False)
    grouped: dict[str, dict] = {}
    for event in events:
        row = grouped.setdefault(
            event.target,
            {
                "name": event.target,
                "url": event.labels.get("url", ""),
                "ok": None,
                "status": event.labels.get("status", ""),
                "error": event.labels.get("error", ""),
                "total_ms": None,
                "ttfb_ms": None,
                "dns_ms": None,
                "connect_ms": None,
                "tls_ms": None,
            },
        )
        if event.metric == "probe_ok":
            row["ok"] = event.value == 1.0
        else:
            key = event.metric.removeprefix("probe_")
            if key in row:
                row[key] = event.value
    return list(grouped.values())
