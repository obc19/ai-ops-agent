"""主动拨测采集：平台自己去访问目标，而不是等别人埋点。

为什么需要
----------
此前所有信号都是「别人采好了我去读」：Prometheus 有指标才读得到，应用没埋点就是空的。
但「用户访问变慢」这件事，最直接的证据是**自己掐一次表**，并且要能说清慢在哪一段：
DNS 解析、TCP 建连、TLS 握手、还是服务端首字节。

分段指标
--------
- ``probe_dns_ms``     DNS 解析
- ``probe_connect_ms`` TCP 建连（已扣除 DNS）
- ``probe_tls_ms``     TLS 握手（http 为 0）
- ``probe_ttfb_ms``    发出请求到收到响应头，即服务端处理时间
- ``probe_total_ms``   端到端总耗时
- ``probe_ok``         状态码是否符合预期（1/0）

这样「慢了」能直接落到某一段上，而不是只会说「P99 高了」。

配置在 ``config/probes.yaml``（挂载目录优先），未配置 targets 时整个采集器自动跳过。
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

from app.collectors.base import BaseCollector
from app.models.enums import SourceType
from app.models.events import BaseEvent, DataSourceHealth, MetricEvent, MetricSample
from app.utils.clock import utc_now
from app.utils.logging import get_logger

logger = get_logger(__name__)

PROBE_FILENAME = "probes.yaml"
MAX_HISTORY_POINTS = 180


@dataclass
class ProbeTarget:
    name: str
    url: str
    expect_status: int = 200


def _history_key(name: str, metric: str) -> str:
    return f"probe:{name}:{metric}"


_HISTORY: dict[str, list[tuple[float, float]]] = {}


def reset_history() -> None:
    _HISTORY.clear()


def _append(key: str, ts: float, value: float) -> list[MetricSample]:
    series = _HISTORY.setdefault(key, [])
    series.append((ts, value))
    if len(series) > MAX_HISTORY_POINTS:
        del series[:-MAX_HISTORY_POINTS]
    from datetime import datetime, timezone

    return [MetricSample(timestamp=datetime.fromtimestamp(t, timezone.utc), value=v) for t, v in series]


def load_probes(settings) -> list[ProbeTarget]:
    """按 显式 probe_config → 配置目录 → 仓库 config/ 的顺序找 probes.yaml。"""
    candidates: list[Path] = []
    explicit = str(getattr(settings, "probe_config", "") or "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    try:
        candidates.append(settings.config_root / PROBE_FILENAME)
    except Exception:  # noqa: BLE001 - config_root 不可用时忽略
        pass
    from app.config.constants import PROJECT_ROOT

    candidates.append(PROJECT_ROOT / "config" / PROBE_FILENAME)

    for path in candidates:
        if not path.is_file():
            continue
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.error("拨测配置解析失败（%s）：%s", path, exc)
            return []
        targets: list[ProbeTarget] = []
        for item in payload.get("targets") or []:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            url = str(item["url"])
            name = str(item.get("name") or urlsplit(url).netloc or url)
            try:
                expect = int(item.get("expect_status", 200))
            except (TypeError, ValueError):
                expect = 200
            targets.append(ProbeTarget(name=name, url=url, expect_status=expect))
        logger.info("拨测目标 %d 个（来自 %s）", len(targets), path)
        return targets
    return []


class ProbeCollector(BaseCollector):
    """主动拨测。target = 配置里的 name，namespace 固定为 probe。"""

    name = "probe"
    source = SourceType.METRIC

    def __init__(self, settings=None, cluster=None) -> None:
        super().__init__(settings, cluster)
        self._targets: list[ProbeTarget] | None = None

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "probe_enabled", True))

    @property
    def targets(self) -> list[ProbeTarget]:
        if self._targets is None:
            self._targets = load_probes(self.settings) if self.enabled else []
        return self._targets

    @property
    def configured(self) -> bool:
        return bool(self.targets)

    @property
    def use_mock(self) -> bool:
        # 拨测是真实网络行为，mock 模式下也照打（否则「用户慢」永远验不出来）
        return False

    @property
    def timeout(self) -> float:
        return float(getattr(self.settings, "probe_timeout", 5) or 5)

    async def query(self, window, record_history: bool = True) -> list[BaseEvent]:
        if not self.targets:
            return []
        events: list[BaseEvent] = []
        for target in self.targets:
            try:
                events.extend(await self._probe_one(target, window, record_history))
            except Exception as exc:  # noqa: BLE001 - 单个目标失败不影响其他
                logger.warning("拨测 %s 失败：%s", target.name, exc)
        return events

    async def health_check(self) -> DataSourceHealth:
        if not self.enabled:
            return DataSourceHealth(
                name=self.label, configured=False, reachable=False, detail="拨测已关闭"
            )
        if not self.targets:
            return DataSourceHealth(
                name=self.label,
                configured=False,
                reachable=False,
                detail=f"未配置 {PROBE_FILENAME} 中的 targets",
            )
        return DataSourceHealth(
            name=self.label,
            configured=True,
            reachable=True,
            detail="、".join(t.name for t in self.targets[:5]),
            checked_at=utc_now(),
        )

    # ---------- 单目标拨测 ----------
    async def _probe_one(
        self, target: ProbeTarget, window, record_history: bool = True
    ) -> list[MetricEvent]:
        # 首次请求带有模块导入、TLS 上下文初始化等一次性开销，
        # 直接计入会稳定虚高 200~300ms 并误报；先打一次丢弃，只留稳定态。
        try:
            await self._measure(target)
        except Exception:  # noqa: BLE001 - 预热失败不影响正式测量
            pass
        metrics, labels = await self._measure(target)

        events: list[MetricEvent] = []
        for metric, value in metrics.items():
            unit = "ms" if metric.endswith("_ms") else ""
            if record_history:
                samples = _append(
                    _history_key(target.name, metric), utc_now().timestamp(), value
                )
            else:
                samples = [MetricSample(timestamp=utc_now(), value=value)]
            events.append(
                MetricEvent(
                    cluster=self.cluster_name,
                    namespace="probe",
                    target=target.name,
                    metric=metric,
                    unit=unit,
                    labels=labels,
                    value=value,
                    samples=samples,
                )
            )
        return events

    async def _measure(self, target: ProbeTarget) -> tuple[dict[str, float], dict[str, str]]:
        parts = urlsplit(target.url)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)

        start = time.perf_counter()
        dns_ms = self._resolve_ms(host, port)
        resolve_done = time.perf_counter()

        marks: dict[str, float] = {}

        def trace(event: str, info) -> None:  # noqa: ANN001 - httpx trace 签名
            marks.setdefault(event, time.perf_counter())

        # httpx 在异步接口下要求 trace 回调本身是协程
        async def async_trace(event: str, info) -> None:  # noqa: ANN001
            trace(event, info)

        status = 0
        error = ""
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            trust_env=False,
            verify=self.settings.tls_verify,
            headers={"User-Agent": "ai-ops-agent-probe/1.0", "Connection": "close"},
        ) as client:
            try:
                response = await client.get(target.url, extensions={"trace": async_trace})
                status = response.status_code
                await response.aread()
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}: {exc}"[:180]
        total_ms = (time.perf_counter() - start) * 1000

        def at(suffix: str) -> float | None:
            for name, ts in marks.items():
                if name.endswith(suffix):
                    return ts
            return None

        tcp_done = at("connect_tcp.complete")
        tls_done = at("start_tls.complete")
        headers_done = at("receive_response_headers.complete")

        connect_ms = max((tcp_done - resolve_done) * 1000, 0.0) if tcp_done else 0.0
        tls_ms = max((tls_done - tcp_done) * 1000, 0.0) if (tls_done and tcp_done) else 0.0
        ttfb_base = tls_done or tcp_done or resolve_done
        ttfb_ms = max((headers_done - ttfb_base) * 1000, 0.0) if headers_done else total_ms

        ok = bool(status) and (
            200 <= status < 300 if target.expect_status == 200 else status == target.expect_status
        )

        if error:
            # 请求根本没建立起来，分段耗时全是噪声；
            # 只报「不可用」一条，避免同一目标同时冒出 3 条 P0/P1
            metrics = {"probe_ok": 0.0}
        else:
            metrics = {
                "probe_total_ms": round(total_ms, 1),
                "probe_dns_ms": round(dns_ms, 2),
                "probe_connect_ms": round(connect_ms, 2),
                "probe_tls_ms": round(tls_ms, 2),
                "probe_ttfb_ms": round(ttfb_ms, 1),
                "probe_ok": 1.0 if ok else 0.0,
            }
        labels = {"url": target.url, "status": str(status)}
        if error:
            labels["error"] = error
        return metrics, labels

    @staticmethod
    def _resolve_ms(host: str, port: int) -> float:
        if not host:
            return 0.0
        # 已经是 IP 就没有 DNS 环节
        try:
            socket.inet_pton(socket.AF_INET, host)
            return 0.0
        except OSError:
            pass
        try:
            socket.inet_pton(socket.AF_INET6, host)
            return 0.0
        except OSError:
            pass
        started = time.perf_counter()
        try:
            socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except OSError:
            return 0.0
        return (time.perf_counter() - started) * 1000
