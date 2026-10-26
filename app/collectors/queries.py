"""查询模板集中管理：换环境只改这里，不动适配器代码。"""

from __future__ import annotations

METRIC_QUERIES: dict[str, dict[str, str]] = {
    "cpu_usage_percent": {
        "query": (
            '100 * avg by (namespace, pod) '
            '(rate(container_cpu_usage_seconds_total{container!=""}[2m]))'
        ),
        "unit": "%",
    },
    "memory_usage_percent": {
        "query": (
            '100 * avg by (namespace, pod) (container_memory_working_set_bytes) '
            '/ avg by (namespace, pod) (kube_pod_container_resource_limits{resource="memory"})'
        ),
        "unit": "%",
    },
    "http_latency_p99_ms": {
        "query": (
            'histogram_quantile(0.99, sum by (namespace, pod) '
            "(rate(http_request_duration_seconds_bucket[5m]))) * 1000"
        ),
        "unit": "ms",
    },
    "http_error_rate_percent": {
        "query": (
            '100 * sum by (namespace, pod) (rate(http_requests_total{code=~"5.."}[5m])) '
            "/ sum by (namespace, pod) (rate(http_requests_total[5m]))"
        ),
        "unit": "%",
    },
}

# 日志规则：命中即视为异常线索
LOG_PATTERNS: list[dict[str, str]] = [
    {"id": "log-oom", "name": "内存溢出", "pattern": "OutOfMemoryError|OOMKilled|heap space"},
    {"id": "log-timeout", "name": "上游超时", "pattern": "timeout|Timed out|deadline exceeded"},
    {"id": "log-conn", "name": "连接失败", "pattern": "connection refused|Connection reset"},
    {"id": "log-disk", "name": "磁盘不足", "pattern": "no space left on device"},
    {"id": "log-5xx", "name": "服务端错误", "pattern": "HTTP 5\\d\\d|Status 5\\d\\d"},
]
