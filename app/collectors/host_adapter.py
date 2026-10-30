"""主机状态采集：直读 /proc，零外部依赖、不执行任何命令。

为什么需要这一层
----------------
容器/应用层指标（cAdvisor、HTTP histogram）回答的是「这个 Pod / 这个接口怎么样」，
而管理员真正跑起来排查时第一眼看的是**服务器本体**：负载多少、IO 是否堆积、
文件描述符有没有打满、连接是不是爆了、有没有僵尸进程。

这一层之前完全缺失，导致「监测服务器状态」这件事实际上没有做。

设计取舍
--------
1. **只读 /proc**：不依赖 psutil、不执行 shell 命令，避免把只读采集变成可执行。
2. **proc_root 可配置**：容器里把宿主机 /proc 挂到 /host/proc 就能看宿主机；
   不挂则看容器自身，仍然是有意义的真实信号。
3. **maintains 内存滑动序列**：检测引擎的环比与 3σ 基线需要多个采样点，
   而每轮只采一个瞬时值，所以这里保留最近 MAX_HISTORY_POINTS 个点。
4. **mock 模式下依然采真实值**：主机状态不存在「假数据」的意义，
   否则又会变成「页面看着热闹、实际什么都不知道」。
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import sys
from datetime import datetime
from pathlib import Path

from app.collectors.base import BaseCollector
from app.models.enums import SourceType
from app.models.events import BaseEvent, DataSourceHealth, MetricEvent, MetricSample
from app.utils.clock import utc_now
from app.utils.logging import get_logger

logger = get_logger(__name__)

# 10s 检测周期下约 30 分钟历史，足够 3σ 基线与环比使用
MAX_HISTORY_POINTS = 180
HISTORY_TTL_SECONDS = 3600

# key = "{metric}|{target}|{mount}"，跨实例共享（采集器每轮都会重建）
_HISTORY: dict[str, list[tuple[datetime, float]]] = {}


def reset_history() -> None:
    """清空历史（测试用）。"""
    _HISTORY.clear()


def _append_history(key: str, ts: datetime, value: float) -> list[MetricSample]:
    series = _HISTORY.setdefault(key, [])
    series.append((ts, value))
    cutoff = ts.timestamp() - HISTORY_TTL_SECONDS
    if len(series) > MAX_HISTORY_POINTS or series[0][0].timestamp() < cutoff:
        series[:] = [p for p in series if p[0].timestamp() >= cutoff][-MAX_HISTORY_POINTS:]
    return [MetricSample(timestamp=t, value=v) for t, v in series]


def _clamp(value: float) -> float:
    return round(max(0.0, min(value, 100.0)), 2)


class HostCollector(BaseCollector):
    """服务器本体状态。target = 主机名，namespace 固定为 host。"""

    name = "host"
    source = SourceType.METRIC

    # ---------- 配置 ----------
    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "server_enabled", True))

    @property
    def proc_root(self) -> Path:
        return Path(getattr(self.settings, "server_proc_root", "/proc") or "/proc").expanduser()

    @property
    def sample_interval_ms(self) -> int:
        return int(getattr(self.settings, "server_sample_interval_ms", 300) or 300)

    @property
    def disk_paths(self) -> list[str]:
        raw = str(getattr(self.settings, "server_disk_paths", "/") or "/")
        return [p.strip() for p in raw.split(",") if p.strip()]

    @property
    def configured(self) -> bool:
        return self.enabled and (self.proc_root / "stat").is_file()

    @property
    def supported(self) -> bool:
        """/proc 是 Linux 专有，其他系统直接判定不适用。"""
        return sys.platform.startswith("linux")

    @property
    def unavailable_reason(self) -> str:
        """采集不到时的可读原因，接口与日志共用。"""
        if not self.enabled:
            return "服务器采集已在配置中关闭（server.enabled=false）"
        if not self.supported:
            return f"非 Linux 系统（{sys.platform}）没有 /proc，服务器采集不适用"
        return f"读不到 {self.proc_root}/stat，请检查 SERVER_PROC_ROOT 是否正确"

    @property
    def use_mock(self) -> bool:
        # 主机状态永远采真实值，不参与 mock
        return False

    # ---------- 主流程 ----------
    async def query(self, window, record_history: bool = True) -> list[BaseEvent]:
        """record_history=False 用于控制台展示，避免手动刷新污染检测基线。"""
        if not self.configured:
            return []
        self._record = record_history
        host = self._hostname()
        events: list[BaseEvent] = []

        cpu = await self._cpu_usage()
        if cpu is not None:
            usage, iowait = cpu
            events.append(self._event(host, "host_cpu_usage_percent", usage, "%", window))
            events.append(self._event(host, "host_cpu_iowait_percent", iowait, "%", window))

        load = self._loadavg()
        cores = self._cpu_cores()
        for idx, metric in enumerate(("host_load1", "host_load5", "host_load15")):
            if load is not None and len(load) > idx:
                events.append(self._event(host, metric, load[idx], "", window))
        if load is not None and cores:
            # 单看 load 没有意义，要看「每个核的排队长度」：>1 已经跑满，>2 明显排队
            events.append(
                self._event(host, "host_load_per_core", round(load[0] / cores, 2), "", window)
            )
        if cores:
            events.append(self._event(host, "host_cpu_cores", float(cores), "核", window))

        memory = self._memory()
        if memory is not None:
            used, swap_used = memory
            events.append(self._event(host, "host_memory_used_percent", used, "%", window))
            if swap_used is not None:
                events.append(self._event(host, "host_swap_used_percent", swap_used, "%", window))

        for path in self.disk_paths:
            usage = self._disk_usage(path)
            if usage is not None:
                events.append(
                    self._event(
                        host, "host_disk_used_percent", usage, "%", window, {"mount": path}
                    )
                )

        fd = self._fd_usage()
        if fd is not None:
            events.append(self._event(host, "host_fd_used_percent", fd, "%", window))

        tcp = self._tcp_established()
        if tcp is not None:
            events.append(self._event(host, "host_tcp_established", tcp, "个", window))

        proc = self._process_stats()
        if proc is not None:
            total, zombie = proc
            events.append(self._event(host, "host_process_count", total, "个", window))
            events.append(self._event(host, "host_zombie_processes", zombie, "个", window))

        return events

    async def health_check(self) -> DataSourceHealth:
        if not self.enabled:
            return DataSourceHealth(
                name=self.label, configured=False, reachable=False, detail="主机采集已关闭"
            )
        if not self.supported:
            # 非 Linux 根本没有 /proc，属于环境不适用，不算配置错误
            return DataSourceHealth(
                name=self.label,
                configured=False,
                reachable=False,
                detail=f"非 Linux 系统（{sys.platform}），无 /proc，服务器采集不适用",
            )
        if not self.configured:
            return DataSourceHealth(
                name=self.label,
                configured=True,
                reachable=False,
                detail=f"读不到 {self.proc_root}/stat，检查 SERVER_PROC_ROOT 是否正确",
                checked_at=utc_now(),
            )
        return DataSourceHealth(
            name=self.label,
            configured=True,
            reachable=True,
            detail=f"{self._hostname()} ← {self.proc_root}",
            checked_at=utc_now(),
        )

    # ---------- 采集实现 ----------
    def _hostname(self) -> str:
        try:
            return socket.gethostname() or "localhost"
        except OSError:  # pragma: no cover - 极端环境
            return "localhost"

    def _read(self, rel: str) -> str | None:
        try:
            return (self.proc_root / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None

    def _cpu_raw(self) -> tuple[float, float, float] | None:
        """返回 (总时间, idle+iowait, iowait)，单位 jiffies。"""
        raw = self._read("stat")
        if not raw:
            return None
        first = raw.splitlines()[0].split()
        if not first or not first[0].startswith("cpu"):
            return None
        try:
            values = [float(x) for x in first[1:]]
        except ValueError:
            return None
        if len(values) < 4:
            return None
        # user nice system idle iowait irq softirq steal —— 不含 guest，避免重复计算
        core = values[:8] if len(values) >= 8 else values
        total = sum(core)
        iowait = core[4] if len(core) > 4 else 0.0
        idle = core[3] + iowait
        return total, idle, iowait

    async def _cpu_usage(self) -> tuple[float, float] | None:
        """两次采样求差，得到真实的瞬时使用率；首轮无历史也能算。"""
        first = self._cpu_raw()
        if first is None:
            return None
        await asyncio.sleep(max(self.sample_interval_ms, 50) / 1000)
        second = self._cpu_raw()
        if second is None:
            return None
        delta_total = second[0] - first[0]
        if delta_total <= 0:
            return None
        usage = 100.0 * (1.0 - (second[1] - first[1]) / delta_total)
        iowait = 100.0 * (second[2] - first[2]) / delta_total
        return _clamp(usage), _clamp(iowait)

    def _loadavg(self) -> tuple[float, float, float] | None:
        raw = self._read("loadavg")
        if not raw:
            return None
        parts = raw.split()
        try:
            return float(parts[0]), float(parts[1]), float(parts[2])
        except (IndexError, ValueError):
            return None

    def _cpu_cores(self) -> int | None:
        """按 /proc/cpuinfo 的 processor 行计数；读不到再退回 os.cpu_count()。"""
        raw = self._read("cpuinfo")
        if raw:
            cores = sum(1 for line in raw.splitlines() if line.strip().startswith("processor"))
            if cores:
                return cores
        import os

        return os.cpu_count()

    def _memory(self) -> tuple[float, float | None] | None:
        raw = self._read("meminfo")
        if not raw:
            return None
        info: dict[str, float] = {}
        for line in raw.splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts:
                try:
                    info[key.strip()] = float(parts[0])
                except ValueError:
                    continue
        total = info.get("MemTotal", 0.0)
        if total <= 0:
            return None
        available = info.get("MemAvailable")
        if available is None:
            available = info.get("MemFree", 0.0) + info.get("Buffers", 0.0) + info.get("Cached", 0.0)
        used = _clamp(100.0 * (1.0 - available / total))

        swap_total = info.get("SwapTotal", 0.0)
        swap_used: float | None = None
        if swap_total > 0:
            swap_used = _clamp(100.0 * (swap_total - info.get("SwapFree", 0.0)) / swap_total)
        return used, swap_used

    @staticmethod
    def _disk_usage(path: str) -> float | None:
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            return None
        if usage.total <= 0:
            return None
        return _clamp(100.0 * usage.used / usage.total)

    def _fd_usage(self) -> float | None:
        raw = self._read("sys/fs/file-nr")
        if not raw:
            return None
        parts = raw.split()
        if len(parts) < 3:
            return None
        try:
            used = float(parts[0])
            limit = float(parts[2])
        except ValueError:
            return None
        if limit <= 0:
            return None
        return _clamp(100.0 * used / limit)

    def _tcp_established(self) -> int | None:
        total = 0
        seen = False
        for name in ("net/tcp", "net/tcp6"):
            raw = self._read(name)
            if not raw:
                continue
            seen = True
            for line in raw.splitlines()[1:]:
                cols = line.split()
                # sl local_address rem_address st ...
                if len(cols) > 3 and cols[3] == "01":
                    total += 1
        return total if seen else None

    def _process_stats(self) -> tuple[int, int] | None:
        try:
            entries = list(self.proc_root.iterdir())
        except OSError:
            return None
        total = 0
        zombie = 0
        scanned = False
        for entry in entries:
            if not entry.name.isdigit():
                continue
            scanned = True
            try:
                stat = (entry / "stat").read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            # 格式：pid (comm) state ...，comm 可能含空格与括号
            end = stat.rfind(")")
            if end < 0:
                continue
            fields = stat[end + 1 :].split()
            if not fields:
                continue
            total += 1
            if fields[0] == "Z":
                zombie += 1
        if not scanned:
            return None
        return total, zombie

    # ---------- 组装 ----------
    def _event(
        self,
        host: str,
        metric: str,
        value: float,
        unit: str,
        window,
        labels: dict[str, str] | None = None,
    ) -> MetricEvent:
        ts = utc_now()
        labels = labels or {}
        if not getattr(self, "_record", True):
            samples = [MetricSample(timestamp=ts, value=float(value))]
            key = None
        else:
            key = f"{metric}|{host}|{labels.get('mount', '')}"
            samples = _append_history(key, ts, float(value))
        return MetricEvent(
            cluster=self.cluster_name,
            namespace="host",
            target=host,
            metric=metric,
            unit=unit,
            labels=labels,
            value=float(value),
            samples=samples,
        )
