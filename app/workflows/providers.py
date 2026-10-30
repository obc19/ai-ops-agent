"""取证 Provider：全部是**只读 GET**，不 exec、不写、不改任何对象。

三个 K8s Provider 直接回答管理员最先会问的三件事：

- ``k8s.pod``      —— 这个 Pod 现在什么状态？重启了几次？上一次为什么死？资源 limit 是多少？
- ``k8s.logs``     —— 崩溃前打了什么日志？（``previous=true`` 取上一个容器实例）
- ``k8s.rollout``  —— 最近一次发布在多久之前？是不是刚发版就坏了？

再加一个 ``http.get`` 用于探第三方健康页/配置中心等只读接口。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.utils.logging import get_logger

logger = get_logger(__name__)

MAX_TEXT = 1200
MAX_LINES = 60


@dataclass
class ProviderResult:
    ok: bool
    summary: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class ProviderError(RuntimeError):
    """Provider 无法完成取证（未启用、参数缺失、目标不存在等）。"""


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + f"…（已截断，原长 {len(text)}）"


def _k8s_client():
    """复用 K8s 采集器的凭据与连接配置，避免两处各写一套。"""
    from app.collectors.k8s_adapter import K8sCollector

    collector = K8sCollector()
    if not collector.enabled:
        raise ProviderError(
            "K8s 取证不可用：集群内运行或配置 sources.kubernetes_api_url 后自动启用"
        )
    if not collector.configured:
        raise ProviderError("K8s 取证不可用：未配置 Kubernetes API 地址")
    return collector


async def _get_json(client, path: str, headers: dict, params: dict | None = None) -> dict:
    resp = await client.get(path, headers=headers, params=params or {})
    if resp.status_code == 404:
        raise ProviderError(f"对象不存在（HTTP 404）：{path}")
    if resp.status_code in (401, 403):
        raise ProviderError(f"无权限读取 {path}（HTTP {resp.status_code}），检查 RBAC")
    resp.raise_for_status()
    return resp.json()


# ---------------- Provider: k8s.pod ----------------


async def k8s_pod(params: dict[str, Any], _vars: dict) -> ProviderResult:
    namespace = params.get("namespace") or ""
    name = params.get("name") or ""
    if not namespace or not name:
        raise ProviderError("k8s.pod 需要 namespace 与 name")

    collector = _k8s_client()
    headers = {"Authorization": f"Bearer {collector.token}"}
    async with collector._client(collector.base_url, verify=collector.verify) as client:
        pod = await _get_json(
            client, f"/api/v1/namespaces/{namespace}/pods/{name}", headers
        )

    meta = pod.get("metadata") or {}
    spec = pod.get("spec") or {}
    status = pod.get("status") or {}
    statuses = (status.get("containerStatuses") or []) + (
        status.get("initContainerStatuses") or []
    )

    containers: list[dict] = []
    total_restarts = 0
    for item in statuses:
        state = item.get("state") or {}
        last = (item.get("lastState") or {}).get("terminated") or {}
        total_restarts += int(item.get("restartCount", 0) or 0)
        containers.append(
            {
                "name": item.get("name", ""),
                "ready": bool(item.get("ready")),
                "restarts": int(item.get("restartCount", 0) or 0),
                "state": (list(state.keys()) or ["unknown"])[0],
                "state_reason": (
                    (state.get("waiting") or {}).get("reason")
                    or (state.get("terminated") or {}).get("reason")
                    or ""
                ),
                "last_reason": last.get("reason", ""),
                "last_exit_code": last.get("exitCode"),
            }
        )

    owners = (meta.get("ownerReferences") or [{}])[0]
    limits = {}
    for container in spec.get("containers") or []:
        res = (container.get("resources") or {}).get("limits") or {}
        if res:
            limits[container.get("name", "")] = res

    detail = {
        "phase": status.get("phase", ""),
        "node": spec.get("nodeName", ""),
        "owner": f"{owners.get('kind', '').lower()}/{owners.get('name', '')}".strip("/"),
        "restarts": total_restarts,
        "ready": bool(status.get("containerStatuses"))
        and all(c["ready"] for c in containers),
        "limits": limits,
        "containers": containers,
        "pod_ip": status.get("podIP", ""),
        "start_time": status.get("startTime", ""),
    }

    bits = [f"Pod 处于 {detail['phase'] or '未知'}"]
    if detail["node"]:
        bits.append(f"调度在节点 {detail['node']}")
    if total_restarts:
        bits.append(f"累计重启 {total_restarts} 次")
    for container in containers:
        if container["last_reason"]:
            bits.append(
                f"容器 {container['name']} 上次因 {container['last_reason']}"
                f"（退出码 {container['last_exit_code']}）终止"
            )
        elif container["state_reason"]:
            bits.append(f"容器 {container['name']} 当前 {container['state_reason']}")
    if limits:
        bits.append(f"资源 limit：{limits}")
    return ProviderResult(ok=True, summary="；".join(bits), detail=detail)


# ---------------- Provider: k8s.logs ----------------


async def k8s_logs(params: dict[str, Any], _vars: dict) -> ProviderResult:
    namespace = params.get("namespace") or ""
    pod = params.get("pod") or params.get("name") or ""
    if not namespace or not pod:
        raise ProviderError("k8s.logs 需要 namespace 与 pod")

    tail = int(params.get("tail_lines") or 50)
    tail = max(1, min(tail, 500))
    previous = bool(params.get("previous", True))
    container = params.get("container") or ""

    collector = _k8s_client()
    headers = {"Authorization": f"Bearer {collector.token}"}
    query = {"tailLines": tail, "timestamps": "false"}
    if container:
        query["container"] = container
    if previous:
        query["previous"] = "true"

    async with collector._client(collector.base_url, verify=collector.verify) as client:
        path = f"/api/v1/namespaces/{namespace}/pods/{pod}/log"
        resp = await client.get(path, headers=headers, params=query)
        if resp.status_code == 400 and previous:
            # 首次启动的容器没有 previous 实例，退回当前实例，避免把「正常」报成失败
            query.pop("previous", None)
            resp = await client.get(path, headers=headers, params=query)
        if resp.status_code in (401, 403):
            raise ProviderError(f"无权限读取 Pod 日志（HTTP {resp.status_code}）")
        resp.raise_for_status()
        text = resp.text or ""

    lines = [line for line in text.splitlines() if line.strip()]
    tail_lines = lines[-MAX_LINES:]
    hint = ""
    lowered = text.lower()
    for keyword, label in (
        ("outofmemory", "内存溢出"),
        ("exception", "异常堆栈"),
        ("connection refused", "连接被拒"),
        ("timeout", "超时"),
        ("no space left", "磁盘写满"),
        ("permission denied", "权限不足"),
    ):
        if keyword in lowered:
            hint = f"；日志命中「{label}」"
            break

    mode = "上一个容器实例" if previous and "previous" in query else "当前容器实例"
    summary = (
        f"取到 {mode} 最近 {len(tail_lines)} 行日志{hint}"
        if tail_lines
        else f"{mode}没有输出日志（可能尚未写入或已轮转）"
    )
    detail = {
        "mode": mode,
        "line_count": len(lines),
        "tail": tail_lines,
        "preview": _clip("\n".join(tail_lines[-8:]), 600),
    }
    return ProviderResult(ok=True, summary=summary, detail=detail)


# ---------------- Provider: k8s.rollout ----------------


async def _resolve_workload(client, headers: dict, namespace: str, target: str) -> str:
    """把「Pod 名」还原成它所属的 Deployment。

    不能靠猜：``order-service-7d9f8c9b4-abcde`` 这种名字里既有 Deployment 名也有哈希，
    而 Deployment 名本身可能带横线（``order-service``）。所以老老实实查 ownerReferences：
    Pod → ReplicaSet → Deployment，查不到再退回「去掉最后一段哈希」。
    """
    if not target:
        return ""
    try:
        pod = await _get_json(client, f"/api/v1/namespaces/{namespace}/pods/{target}", headers)
    except ProviderError:
        return ""
    owners = (pod.get("metadata") or {}).get("ownerReferences") or []
    if not owners:
        return ""
    owner = owners[0]
    if owner.get("kind") == "Deployment":
        return owner.get("name", "")
    rs_name = owner.get("name", "")
    if not rs_name:
        return ""
    try:
        rs = await _get_json(
            client, f"/apis/apps/v1/namespaces/{namespace}/replicasets/{rs_name}", headers
        )
        rs_owners = (rs.get("metadata") or {}).get("ownerReferences") or []
        if rs_owners and rs_owners[0].get("kind") == "Deployment":
            return rs_owners[0].get("name", "")
    except ProviderError:
        pass
    # 最后兜底：ReplicaSet 名 = Deployment 名 + "-" + pod-template-hash
    return rs_name.rsplit("-", 1)[0] if "-" in rs_name else rs_name


async def k8s_rollout(params: dict[str, Any], _vars: dict) -> ProviderResult:
    """用 ReplicaSet 的创建时间判断「最近一次发布」，这是 K8s 里唯一可靠的发布痕迹。"""
    namespace = params.get("namespace") or ""
    if not namespace:
        raise ProviderError("k8s.rollout 需要 namespace")

    collector = _k8s_client()
    headers = {"Authorization": f"Bearer {collector.token}"}
    async with collector._client(collector.base_url, verify=collector.verify) as client:
        workload = params.get("workload") or ""
        if not workload and params.get("target"):
            workload = await _resolve_workload(client, headers, namespace, params["target"])
        payload = await _get_json(
            client, f"/apis/apps/v1/namespaces/{namespace}/replicasets", headers
        )

    rows: list[dict] = []
    for item in payload.get("items", []):
        meta = item.get("metadata") or {}
        owners = meta.get("ownerReferences") or [{}]
        owner = owners[0].get("name", "") or "(无 owner)"
        if workload and owner != workload:
            continue
        images = [
            c.get("image", "")
            for c in (item.get("spec") or {}).get("template", {}).get("spec", {}).get("containers", [])
        ]
        rows.append(
            {
                "replicaset": meta.get("name", ""),
                "deployment": owner,
                "created_at": meta.get("creationTimestamp", ""),
                "replicas": (item.get("status") or {}).get("replicas", 0),
                "ready": (item.get("status") or {}).get("readyReplicas", 0),
                "images": images,
            }
        )

    if not rows:
        scope = f"{namespace}/{workload}" if workload else f"命名空间 {namespace}"
        return ProviderResult(
            ok=True,
            summary=f"{scope} 下没有 ReplicaSet（不是 Deployment 管理的负载，或已全部删除）",
            detail={"replicasets": []},
        )

    rows.sort(key=lambda r: r["created_at"], reverse=True)
    latest = rows[0]
    prefix = "最近一次发布" if workload else f"命名空间 {namespace} 内最近一次发布"
    summary = (
        f"{prefix}：{latest['deployment']} 的 ReplicaSet {latest['replicaset']}"
        f" 创建于 {latest['created_at']}，镜像 {'、'.join(latest['images']) or '-'}"
        f"（就绪 {latest['ready']}/{latest['replicas']}）"
    )
    if len(rows) > 1:
        summary += f"；该负载共 {len(rows)} 个 ReplicaSet 历史版本"
    return ProviderResult(ok=True, summary=summary, detail={"latest": latest, "replicasets": rows[:10]})


# ---------------- Provider: http.get ----------------


async def http_get(params: dict[str, Any], _vars: dict) -> ProviderResult:
    """只读探测：给第三方健康页、配置中心、依赖服务用。"""
    import httpx

    url = params.get("url") or ""
    if not url.startswith(("http://", "https://")):
        raise ProviderError("http.get 需要 http(s) 地址")
    timeout = float(params.get("timeout") or 4.0)

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=True) as client:
            resp = await client.get(url)
    except Exception as exc:  # noqa: BLE001 - 探不通就是证据本身
        elapsed = round((time.monotonic() - started) * 1000, 1)
        return ProviderResult(
            ok=True,
            summary=f"请求 {url} 失败（{type(exc).__name__}），耗时 {elapsed}ms",
            detail={"url": url, "error": str(exc)[:300], "elapsed_ms": elapsed},
        )

    elapsed = round((time.monotonic() - started) * 1000, 1)
    body = resp.text or ""
    return ProviderResult(
        ok=True,
        summary=f"{url} 返回 HTTP {resp.status_code}，耗时 {elapsed}ms，响应 {len(body)} 字节",
        detail={
            "url": url,
            "status": resp.status_code,
            "elapsed_ms": elapsed,
            "body_preview": _clip(body, 500),
        },
    )


REGISTRY = {
    "k8s.pod": k8s_pod,
    "k8s.logs": k8s_logs,
    "k8s.rollout": k8s_rollout,
    "http.get": http_get,
}


def list_providers() -> list[str]:
    return sorted(REGISTRY)
