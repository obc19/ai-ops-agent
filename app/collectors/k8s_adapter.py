"""Kubernetes 适配器：只用集群原生 API 采集，不依赖 Prometheus / ELK。

集群内（Deployment）部署时零配置：自动读取 ServiceAccount 挂载的
``/var/run/secrets/kubernetes.io/serviceaccount/{token,ca.crt}``，直连
``https://kubernetes.default.svc``。集群外运行时用环境变量显式配置。

采集四类（全部只读 GET）：

1. ``/api/v1/events``                        —— 警告事件（FailedScheduling/Unhealthy/BackOff…）
2. ``/api/v1/pods``                          —— Pod 相位、容器重启次数、waiting/terminated 的 reason
3. ``/api/v1/nodes``                         —— Ready / MemoryPressure / DiskPressure / PIDPressure
4. ``/apis/apps/v1/deployments``             —— 期望副本 vs 就绪副本（缺口）

外加一类**可选**：

5. ``/apis/metrics.k8s.io/v1beta1/{pods,nodes}`` —— metrics-server 提供的 CPU/内存用量。
   集群没装 metrics-server 时这组接口直接 404，这里降级跳过，不报错——
   「有事件、有重启次数、有就绪状态」本身已经能定位绝大多数故障。

刻意不做的：不 exec 进容器取日志（那需要写权限，违反平台只读原则），
不调 kubelet，不改任何对象。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from app.collectors.base import BaseCollector
from app.mock import world
from app.models.enums import SourceType
from app.models.events import BaseEvent, DataSourceHealth, K8sEvent, MetricEvent
from app.utils.logging import get_logger

logger = get_logger(__name__)

# 标准挂载路径，由 kubelet 注入
SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
IN_CLUSTER_API = "https://kubernetes.default.svc"

# metrics-server 的聚合 API 路径
METRICS_PODS = "/apis/metrics.k8s.io/v1beta1/pods"
METRICS_NODES = "/apis/metrics.k8s.io/v1beta1/nodes"

# 快照缓存：key = 集群名，value = (单调时钟, 事件, metrics 可用, 说明)
_SNAPSHOT_TTL = 15.0
_SNAPSHOTS: dict[str, tuple[float, list[BaseEvent], bool | None, str]] = {}

# 数量单位后缀 → 倍数（CPU 用 m 表示毫核，内存用 Ki/Mi/Gi）
_QUANTITY: dict[str, float] = {
    "": 1.0,
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "Ki": 1024.0,
    "Mi": 1024.0**2,
    "Gi": 1024.0**3,
    "Ti": 1024.0**4,
}


def parse_quantity(raw: str | int | float | None) -> float:
    """解析 K8s 数量表达式：``250m`` → 0.25，``512Mi`` → 536870912。"""
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text:
        return 0.0
    for suffix in ("Ki", "Mi", "Gi", "Ti", "n", "u", "m", "k", "M", "G", "T"):
        if text.endswith(suffix):
            try:
                return float(text[: -len(suffix)]) * _QUANTITY[suffix]
            except ValueError:
                return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _read_sa_file(name: str) -> str:
    """读取 SA 挂载文件；不存在或不可读时返回空串。"""
    candidate = SA_DIR / name
    try:
        return candidate.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def in_cluster() -> bool:
    """是否跑在 K8s 里（kubelet 会挂载 SA token）。"""
    return SA_DIR.joinpath("token").exists()


class K8sCollector(BaseCollector):
    name = "kubernetes"
    source = SourceType.K8S

    def __init__(self, settings=None, cluster=None) -> None:
        super().__init__(settings, cluster)
        # metrics-server 只探测失败一次就闭嘴，避免每轮刷日志
        self._metrics_available: bool | None = None
        self._metrics_note = ""

    # ---------- 连接参数 ----------
    @property
    def in_cluster(self) -> bool:
        return in_cluster()

    @property
    def explicit_url(self) -> str:
        """显式配置的 API 地址（集群外运行）。"""
        from_sources = self.sources.kubernetes_api_url if self.sources else ""
        return from_sources or self.settings.kubernetes_api_url or ""

    @property
    def base_url(self) -> str:
        if self.explicit_url:
            return self.explicit_url
        # 集群内未配地址时，直连内置的服务入口
        return IN_CLUSTER_API if self.in_cluster else ""

    @property
    def token(self) -> str:
        explicit = (
            (self.sources.kubernetes_token if self.sources else "")
            or self.settings.kubernetes_token
        )
        if explicit:
            return explicit
        if self.settings.kubernetes_token_file:
            try:
                return Path(self.settings.kubernetes_token_file).read_text(encoding="utf-8").strip()
            except OSError:
                logger.warning("KUBERNETES_TOKEN_FILE 不可读：%s", self.settings.kubernetes_token_file)
                return ""
        return _read_sa_file("token")

    @property
    def ca_file(self) -> str:
        """私有 CA 路径：显式配置优先，否则用 SA 挂载的集群 CA。"""
        explicit = self.settings.kubernetes_ca_file
        if explicit:
            return explicit
        candidate = SA_DIR / "ca.crt"
        return str(candidate) if candidate.exists() else ""

    @property
    def verify(self) -> bool | str:
        if not self.settings.tls_verify:
            return False
        return self.ca_file or True

    @property
    def namespaces(self) -> set[str]:
        """留空 = 全部命名空间；集群内多看几个 ns 才有意义。"""
        raw = getattr(self.settings, "kubernetes_namespaces", "") or ""
        return {item.strip() for item in raw.split(",") if item.strip()}

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    @property
    def enabled(self) -> bool:
        # 三种情况自动启用，避免「配好了却不生效」这种低级坑：
        # 1) 显式打开开关；2) 跑在集群里（读得到 SA token）；3) 显式配了 API 地址
        return bool(self.settings.enable_k8s_adapter) or self.in_cluster or bool(self.explicit_url)

    # ---------- 主入口 ----------
    async def query(self, window=None, force: bool = False) -> list[BaseEvent]:
        if not self.enabled:
            return []
        if self.use_mock:
            return self._mock(window)

        # 同一次诊断会采两轮（流水线一轮 + 编排一轮），一轮检测里还可能有多个异常群，
        # 每次都 list pods 会把 apiserver 打得很惨。这里用短 TTL 快照兜住，
        # 免得「监控自己成为故障源」。
        snap = _SNAPSHOTS.get(self.cluster_name)
        if snap and not force and time.monotonic() - snap[0] < _SNAPSHOT_TTL:
            self._metrics_available, self._metrics_note = snap[2], snap[3]
            return list(snap[1])

        events = await self._real(window)
        _SNAPSHOTS[self.cluster_name] = (
            time.monotonic(),
            events,
            self._metrics_available,
            self._metrics_note,
        )
        return events

    @staticmethod
    def clear_cache() -> None:
        """测试或需要立刻看到最新状态时清空快照。"""
        _SNAPSHOTS.clear()

    def _mock(self, window) -> list[K8sEvent]:
        if window is None:  # 控制台接口不带窗口，补一个默认的
            from datetime import timedelta

            from app.models.events import TimeWindow
            from app.utils.clock import utc_now

            now = utc_now()
            window = TimeWindow(start=now - timedelta(minutes=5), end=now)
        return [
            K8sEvent(
                cluster=world.cluster,
                namespace=world.namespace,
                target=row["target"],
                reason=row["reason"],
                kind=row["kind"],
                message=row["message"],
                count=row["count"],
            )
            for row in world.k8s_events(window)
        ]

    async def _real(self, window) -> list[BaseEvent]:
        headers = {"Authorization": f"Bearer {self.token}"}
        events: list[BaseEvent] = []
        async with self._client(self.base_url, verify=self.verify) as client:
            payloads = await asyncio.gather(
                self._get(client, "/api/v1/events", headers, {"fieldSelector": "type!=Normal"}),
                self._get(client, "/api/v1/pods", headers),
                self._get(client, "/api/v1/nodes", headers),
                self._get(client, "/apis/apps/v1/deployments", headers),
                self._get_metrics(client, METRICS_PODS, headers),
                self._get_metrics(client, METRICS_NODES, headers),
                return_exceptions=False,
            )
            raw_events, raw_pods, raw_nodes, raw_deploys, pod_metrics, node_metrics = payloads

        events += self._parse_events(raw_events)
        pod_events, pod_index = self._parse_pods(raw_pods)
        events += pod_events
        events += self._parse_nodes(raw_nodes)
        events += self._parse_deployments(raw_deploys)
        events += self._parse_pod_metrics(pod_metrics, pod_index)
        events += self._parse_node_metrics(node_metrics, raw_nodes)

        logger.info(
            "Kubernetes 采集：事件 %d / Pod 状态 %d / 指标 %d（metrics-server %s）",
            len(raw_events),
            len(pod_events),
            len(events) - len(raw_events) - len(pod_events),
            "可用" if self._metrics_available else f"不可用：{self._metrics_note}",
        )
        return events

    # ---------- HTTP ----------
    async def _get(
        self, client, path: str, headers: dict[str, str], params: dict | None = None
    ) -> dict:
        """单个资源的只读拉取；失败只记警告，不牵连其他资源。

        RBAC 通常是逐资源授权的，某个 list 被拒（403）不该让整轮采集失败。
        """
        try:
            resp = await client.get(path, params=params or {}, headers=headers)
            if resp.status_code in (401, 403):
                logger.warning("Kubernetes %s 无权限（HTTP %s），请检查 RBAC", path, resp.status_code)
                return {}
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - 单资源失败降级
            logger.warning("Kubernetes %s 拉取失败：%s", path, exc)
            return {}

    async def _get_metrics(self, client, path: str, headers: dict[str, str]) -> dict:
        """metrics-server 可能没装：404 / 503 视作「本集群没有」，静默降级。"""
        if self._metrics_available is False:
            return {}
        try:
            resp = await client.get(path, headers=headers)
        except Exception as exc:  # noqa: BLE001
            self._set_metrics(False, str(exc)[:120])
            return {}
        if resp.status_code in (404, 503):
            self._set_metrics(False, f"HTTP {resp.status_code}（未安装 metrics-server）")
            return {}
        if resp.status_code in (401, 403):
            self._set_metrics(False, f"HTTP {resp.status_code}（RBAC 未授权 metrics.k8s.io）")
            return {}
        if resp.status_code != 200:
            self._set_metrics(False, f"HTTP {resp.status_code}")
            return {}
        self._set_metrics(True, "")
        return resp.json()

    def _set_metrics(self, ok: bool, note: str) -> None:
        if self._metrics_available is ok:
            return
        self._metrics_available = ok
        self._metrics_note = note
        if ok:
            logger.info("metrics-server 可用，K8s 资源用量采集已开启")
        else:
            logger.info(
                "metrics-server 不可用（%s），资源用量降级跳过；"
                "事件/重启/就绪/副本缺口不受影响",
                note,
            )

    # ---------- 解析：Events ----------
    def _parse_events(self, payload: dict) -> list[K8sEvent]:
        out: list[K8sEvent] = []
        for item in payload.get("items", []):
            meta = item.get("metadata", {})
            namespace = meta.get("namespace", "")
            if not self._wanted(namespace):
                continue
            obj = item.get("involvedObject") or {}
            out.append(
                K8sEvent(
                    cluster=self.cluster_name,
                    namespace=namespace or "default",
                    target=obj.get("name", "unknown"),
                    reason=item.get("reason", ""),
                    kind=obj.get("kind", ""),
                    message=(item.get("message") or "")[:1000],
                    count=int(item.get("count", 1) or 1),
                )
            )
        return out

    # ---------- 解析：Pods ----------
    def _parse_pods(self, payload: dict) -> tuple[list[BaseEvent], dict[str, dict]]:
        """返回 (事件列表, pod 用量比对索引)。"""
        events: list[BaseEvent] = []
        index: dict[str, dict] = {}
        for item in payload.get("items", []):
            meta = item.get("metadata", {})
            name = meta.get("name", "")
            namespace = meta.get("namespace", "default")
            if not name or not self._wanted(namespace):
                continue
            status = item.get("status") or {}
            phase = status.get("phase", "")
            # 正在删除的 Pod 天然会短暂 not ready，别报
            if meta.get("deletionTimestamp"):
                continue

            statuses = (status.get("containerStatuses") or []) + (
                status.get("initContainerStatuses") or []
            )
            restarts = sum(int(c.get("restartCount", 0) or 0) for c in statuses)
            ready = bool(statuses) and phase == "Running" and all(
                c.get("ready") for c in statuses
            )

            # 只有「有问题」才产出指标，避免把一堆 0 灌进基线
            if restarts > 0:
                events.append(
                    MetricEvent(
                        cluster=self.cluster_name,
                        namespace=namespace,
                        target=name,
                        metric="k8s_pod_restarts",
                        value=float(restarts),
                        unit="次",
                        labels={"phase": phase},
                    )
                )
            if not ready and phase in ("Running", "Pending"):
                events.append(
                    MetricEvent(
                        cluster=self.cluster_name,
                        namespace=namespace,
                        target=name,
                        metric="k8s_pod_ready",
                        value=0.0,
                        unit="",
                        labels={"phase": phase},
                    )
                )

            # 容器级失败原因：waiting（还没起来）/ lastState.terminated（起来过又死了）
            for container in statuses:
                reason, message, count = self._container_failure(container, restarts)
                if not reason:
                    continue
                events.append(
                    K8sEvent(
                        cluster=self.cluster_name,
                        namespace=namespace,
                        target=name,
                        reason=reason,
                        kind="Pod",
                        message=message[:1000],
                        count=max(count, 1),
                    )
                )

            # Pending 的调度原因（FailedScheduling 事件有时已经过期，这里补一手）
            if phase == "Pending":
                for cond in status.get("conditions") or []:
                    if cond.get("type") == "PodScheduled" and cond.get("status") == "False":
                        events.append(
                            K8sEvent(
                                cluster=self.cluster_name,
                                namespace=namespace,
                                target=name,
                                reason=cond.get("reason") or "Unschedulable",
                                kind="Pod",
                                message=(cond.get("message") or "Pod 无法调度")[:1000],
                                count=1,
                            )
                        )

            index[f"{namespace}/{name}"] = {
                "labels": meta.get("labels") or {},
                "spec": item.get("spec") or {},
            }
        return events, index

    @staticmethod
    def _container_failure(container: dict, restarts: int) -> tuple[str, str, int]:
        """从容器状态里抽出「为什么不行」，优先报当前 waiting，其次看上一条终止原因。"""
        count = max(restarts, 1)
        state = container.get("state") or {}
        waiting = state.get("waiting")
        if waiting and waiting.get("reason"):
            return waiting["reason"], waiting.get("message") or "", count
        last = (container.get("lastState") or {}).get("terminated") or {}
        reason = last.get("reason") or ""
        # Completed 是正常退出（Job/CronJob 收尾），不算故障
        if reason and reason != "Completed":
            message = last.get("message") or f"容器退出码 {last.get('exitCode')}"
            return reason, message, count
        return "", "", 0

    # ---------- 解析：Nodes ----------
    def _parse_nodes(self, payload: dict) -> list[BaseEvent]:
        out: list[BaseEvent] = []
        for item in payload.get("items", []):
            name = (item.get("metadata") or {}).get("name", "")
            if not name:
                continue
            conds = {
                c.get("type"): c.get("status")
                for c in (item.get("status") or {}).get("conditions") or []
            }
            ready = 1.0 if conds.get("Ready") == "True" else 0.0
            out.append(
                MetricEvent(
                    cluster=self.cluster_name,
                    namespace="",
                    target=name,
                    metric="k8s_node_ready",
                    value=ready,
                    unit="",
                )
            )
            for ctype, metric in (
                ("MemoryPressure", "k8s_node_memory_pressure"),
                ("DiskPressure", "k8s_node_disk_pressure"),
                ("PIDPressure", "k8s_node_pid_pressure"),
            ):
                if conds.get(ctype) == "True":
                    out.append(
                        MetricEvent(
                            cluster=self.cluster_name,
                            namespace="",
                            target=name,
                            metric=metric,
                            value=1.0,
                            unit="",
                        )
                    )
            if ready == 0.0:
                out.append(
                    K8sEvent(
                        cluster=self.cluster_name,
                        namespace="",
                        target=name,
                        reason="NodeNotReady",
                        kind="Node",
                        message="节点 Ready 条件不为 True，该节点上的 Pod 大概率已被驱逐或不可达",
                        count=1,
                    )
                )
        return out

    # ---------- 解析：Deployments ----------
    def _parse_deployments(self, payload: dict) -> list[BaseEvent]:
        out: list[BaseEvent] = []
        for item in payload.get("items", []):
            meta = item.get("metadata") or {}
            name = meta.get("name", "")
            namespace = meta.get("namespace", "default")
            if not name or not self._wanted(namespace):
                continue
            spec = item.get("spec") or {}
            status = item.get("status") or {}
            desired = int(spec.get("replicas", 1) or 0)
            ready = int(status.get("readyReplicas", 0) or 0)
            gap = max(desired - ready, 0)
            if gap <= 0:
                continue
            out.append(
                MetricEvent(
                    cluster=self.cluster_name,
                    namespace=namespace,
                    target=name,
                    metric="k8s_deployment_ready_gap",
                    value=float(gap),
                    unit="副本",
                    labels={"desired": str(desired), "ready": str(ready)},
                )
            )
        return out

    # ---------- 解析：metrics-server ----------
    def _parse_pod_metrics(self, payload: dict, index: dict[str, dict]) -> list[BaseEvent]:
        out: list[BaseEvent] = []
        for item in payload.get("items", []):
            meta = item.get("metadata") or {}
            name = meta.get("name", "")
            namespace = meta.get("namespace", "default")
            key = f"{namespace}/{name}"
            if key not in index:
                continue
            limits = self._limits(index[key])
            cpu_used = mem_used = 0.0
            for container in item.get("containers") or []:
                usage = container.get("usage") or {}
                cpu_used += parse_quantity(usage.get("cpu"))
                mem_used += parse_quantity(usage.get("memory"))

            cpu_limit, mem_limit = limits
            if cpu_limit > 0:
                out.append(
                    MetricEvent(
                        cluster=self.cluster_name,
                        namespace=namespace,
                        target=name,
                        metric="k8s_pod_cpu_percent",
                        value=round(cpu_used / cpu_limit * 100, 2),
                        unit="%",
                    )
                )
            if mem_limit > 0:
                out.append(
                    MetricEvent(
                        cluster=self.cluster_name,
                        namespace=namespace,
                        target=name,
                        metric="k8s_pod_memory_percent",
                        value=round(mem_used / mem_limit * 100, 2),
                        unit="%",
                    )
                )
        return out

    @staticmethod
    def _limits(entry: dict) -> tuple[float, float]:
        """汇总 Pod 内所有容器的 limit；没设 limit 就返回 0，表示「无法算占比」。"""
        cpu = mem = 0.0
        for container in (entry.get("spec") or {}).get("containers") or []:
            limits = ((container.get("resources") or {}).get("limits")) or {}
            cpu += parse_quantity(limits.get("cpu"))
            mem += parse_quantity(limits.get("memory"))
        return cpu, mem

    def _parse_node_metrics(self, payload: dict, nodes_payload: dict) -> list[BaseEvent]:
        allocatable = {
            (item.get("metadata") or {}).get("name", ""): (item.get("status") or {}).get(
                "allocatable"
            )
            or {}
            for item in nodes_payload.get("items", [])
        }
        out: list[BaseEvent] = []
        for item in payload.get("items", []):
            name = (item.get("metadata") or {}).get("name", "")
            cap = allocatable.get(name) or {}
            cpu_cap = parse_quantity(cap.get("cpu"))
            mem_cap = parse_quantity(cap.get("memory"))
            usage = item.get("usage") or {}
            if cpu_cap > 0:
                out.append(
                    MetricEvent(
                        cluster=self.cluster_name,
                        namespace="",
                        target=name,
                        metric="k8s_node_cpu_percent",
                        value=round(parse_quantity(usage.get("cpu")) / cpu_cap * 100, 2),
                        unit="%",
                    )
                )
            if mem_cap > 0:
                out.append(
                    MetricEvent(
                        cluster=self.cluster_name,
                        namespace="",
                        target=name,
                        metric="k8s_node_memory_percent",
                        value=round(parse_quantity(usage.get("memory")) / mem_cap * 100, 2),
                        unit="%",
                    )
                )
        return out

    # ---------- 辅助 ----------
    def _wanted(self, namespace: str) -> bool:
        """namespaces 留空 = 全收；配置了就只收列出的（集群级资源 namespace 为空时放行）。"""
        wanted = self.namespaces
        if not wanted:
            return True
        return not namespace or namespace in wanted

    async def _probe(self) -> DataSourceHealth:  # pragma: no cover
        headers = {"Authorization": f"Bearer {self.token}"}
        async with self._client(self.base_url, verify=self.verify) as client:
            resp = await client.get("/version", headers=headers)
            # 顺手探一下 metrics-server，让健康页能直接看出「资源用量有没有」
            await self._get_metrics(client, METRICS_PODS, headers)
            detail = f"HTTP {resp.status_code}"
            detail += (
                "；metrics-server 可用"
                if self._metrics_available
                else f"；metrics-server 不可用（{self._metrics_note}）"
            )
            return DataSourceHealth(
                name=self.label,
                configured=True,
                reachable=resp.status_code == 200,
                detail=detail,
            )
