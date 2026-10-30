"""服务依赖拓扑接口：查看当前图、强制刷新。

这张图是「根因收敛」的依据，所以除了给人看，也把推导来源和
判定能力（是否有环、入口/末端）一并暴露出来，便于排查「为什么没收敛」。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import require_auth
from app.topology import get_topology
from app.utils.clock import iso_utc

router = APIRouter(tags=["topology"])


@router.get(
    "/topology",
    summary="服务依赖拓扑",
    dependencies=[Depends(require_auth)],
)
async def get_graph(refresh: bool = Query(False, description="忽略缓存，立即重新推导")) -> dict:
    service = get_topology()
    graph = await service.current(force=refresh)
    payload = graph.to_public()
    payload.update(
        {
            "enabled": service.settings.topology_enabled,
            "refresh_seconds": service.settings.topology_refresh_seconds,
            "escalate_by_impact": service.settings.topology_escalate_by_impact,
            "cached_at": iso_utc(graph.updated_at),
        }
    )
    if graph.is_empty():
        payload["hint"] = (
            "拓扑为空，异常不会被收敛。三种来源任选其一："
            "① 配 JAEGER_URL（平台从 span 父子关系自动推导）；"
            "② 在 config/aiops.yaml 的 topology.dependencies 里手工声明；"
            "③ mock 模式下会自动提供演示拓扑。"
        )
    return payload
