"""Mock 控制面：故障注入 + 通知接收端（用于验收断言）。"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel

from app.mock import world

router = APIRouter(prefix="/_mock", tags=["mock"])

_sink: list[dict] = []
_lock = threading.Lock()


class FaultRequest(BaseModel):
    target: str
    metric: str = "cpu_usage_percent"
    severity: str = "P1"
    magnitude: float = 1.0
    note: str = ""


def get_sink_records() -> list[dict]:
    with _lock:
        return list(_sink)


def clear_sink() -> int:
    with _lock:
        count = len(_sink)
        _sink.clear()
    return count


@router.post("/fault", summary="注入 mock 故障")
async def inject_fault(payload: FaultRequest) -> dict:
    fault = world.inject_fault(
        target=payload.target,
        metric=payload.metric,
        severity=payload.severity,
        magnitude=payload.magnitude,
        note=payload.note,
    )
    return {
        "ok": True,
        "fault": {
            "target": fault.target,
            "metric": fault.metric,
            "severity": fault.severity,
            "injected_at": fault.injected_at.isoformat(),
        },
        "active": len(world.list_faults()),
    }


@router.post("/reset", summary="清除全部 mock 故障")
async def reset_faults() -> dict:
    return {"ok": True, "cleared": world.clear_faults()}


@router.get("/faults", summary="当前 mock 故障列表")
async def list_faults() -> dict:
    return {
        "items": [
            {
                "target": f.target,
                "metric": f.metric,
                "severity": f.severity,
                "note": f.note,
                "injected_at": f.injected_at.isoformat(),
            }
            for f in world.list_faults()
        ]
    }


@router.post("/sink/{channel}", summary="接收端：记录各渠道投递的通知")
async def sink(channel: str, request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - 非 JSON body 也记录下来
        body = {"raw": (await request.body()).decode("utf-8", "replace")}
    record = {
        "channel": channel,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "payload": body,
    }
    with _lock:
        _sink.append(record)
    return {"ok": True}


@router.get("/sink", summary="接收端：已记录的通知")
async def list_sink(channel: str | None = None) -> dict:
    items = get_sink_records()
    if channel:
        items = [i for i in items if i["channel"] == channel]
    return {"total": len(items), "items": items}


@router.delete("/sink", summary="接收端：清空记录")
async def clear_sink_records() -> dict:
    return {"ok": True, "cleared": clear_sink()}


@router.post("/echo", summary="接收端：通用回显（调试 Webhook 用）")
async def echo(payload: dict = Body(default={})) -> dict:
    with _lock:
        _sink.append(
            {
                "channel": "echo",
                "received_at": datetime.now(timezone.utc).isoformat(),
                "payload": payload,
            }
        )
    return {"ok": True, "received": payload}
