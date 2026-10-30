"""无集群验证取证工作流：起一个假 apiserver，跑一次真实诊断，断言证据进了报告。

    python tools/verify_workflows.py

价值在于「不需要真 K8s 也能回归」：Pod 状态解析、日志提取、ReplicaSet 反推发布、
报告渲染、以及 k8s 不可用时的降级文案，全都能在本地验完。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PORT = 8098

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="aiops-verify-")
os.environ["MOCK_MODE"] = "true"
os.environ["DETECT_INTERVAL"] = "3600"
os.environ["AUTH_TOKEN"] = ""
os.environ["KUBERNETES_API_URL"] = f"http://127.0.0.1:{PORT}"
os.environ["KUBERNETES_TOKEN"] = "fake-token"
os.environ["TLS_VERIFY"] = "false"
os.environ["LLM_API_KEY"] = ""

POD = {
    "metadata": {
        "name": "order-service-7d9f8c",
        "namespace": "prod",
        "ownerReferences": [{"kind": "ReplicaSet", "name": "order-service-7d9f8c"}],
    },
    "spec": {
        "nodeName": "node-1",
        "containers": [{"name": "app", "resources": {"limits": {"cpu": "1", "memory": "512Mi"}}}],
    },
    "status": {
        "phase": "Running",
        "podIP": "10.0.1.7",
        "startTime": "2026-10-30T02:00:00Z",
        "containerStatuses": [
            {
                "name": "app",
                "ready": False,
                "restartCount": 6,
                "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 5m0s"}},
                "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
            }
        ],
    },
}

REPLICASETS = [
    {
        "metadata": {
            "name": "order-service-7d9f8c",
            "namespace": "prod",
            "creationTimestamp": "2026-10-30T01:52:11Z",
            "ownerReferences": [{"kind": "Deployment", "name": "order-service"}],
        },
        "spec": {"template": {"spec": {"containers": [{"image": "registry/order:1.4.2"}]}}},
        "status": {"replicas": 3, "readyReplicas": 1},
    },
    {
        "metadata": {
            "name": "order-service-5b8c4d",
            "namespace": "prod",
            "creationTimestamp": "2026-10-20T09:00:00Z",
            "ownerReferences": [{"kind": "Deployment", "name": "order-service"}],
        },
        "spec": {"template": {"spec": {"containers": [{"image": "registry/order:1.4.1"}]}}},
        "status": {"replicas": 0, "readyReplicas": 0},
    },
]

LOGS = "\n".join(
    [
        "2026-10-30 10:01:02 INFO  starting order-service 1.4.2",
        "2026-10-30 10:01:05 WARN  connection pool exhausted, waiting for available connection",
        "2026-10-30 10:01:07 ERROR java.lang.OutOfMemoryError: Java heap space",
        "2026-10-30 10:01:07 ERROR     at com.example.OrderCache.warm(OrderCache.java:88)",
        "2026-10-30 10:01:08 FATAL shutting down after 3 retries",
    ]
)

NODE = {"metadata": {"name": "node-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # 静音
        pass

    def _send(self, payload, status=200, content_type="application/json") -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        routes = {
            "/api/v1/events": {"items": []},
            "/api/v1/pods": {"items": [POD]},
            "/api/v1/nodes": {"items": [NODE]},
            "/apis/apps/v1/deployments": {"items": []},
            f"/api/v1/namespaces/prod/pods/{POD['metadata']['name']}": POD,
            "/apis/apps/v1/namespaces/prod/replicasets": {"items": REPLICASETS},
            "/apis/apps/v1/namespaces/prod/replicasets/order-service-7d9f8c": REPLICASETS[0],
        }
        if path in routes:
            return self._send(routes[path])
        if path.endswith("/log"):
            return self._send(LOGS.encode(), content_type="text/plain")
        # metrics-server 未安装
        return self._send({"kind": "Status", "code": 404}, status=404)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    resp = client.post(
        "/api/v1/diagnostics",
        json={
            "target": "order-service-7d9f8c",
            "metric": "k8s_pod_restarts",
            "namespace": "prod",
            "severity": "P0",
            "value": 6,
            "threshold": 3,
            "rule_id": "k8s-pod-restart-critical",
            "notify": False,
        },
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()["report"]
    evidence = report["evidence"]
    by_step = {item["step"]: item for item in evidence}

    checks: list[tuple[str, bool, str]] = [
        ("触发了 3 个取证步骤", len(evidence) == 3, f"实际 {len(evidence)}"),
        ("全部采集成功", all(item["ok"] for item in evidence), str([i["error"] for i in evidence])),
        ("Pod 识别出 OOMKilled", "OOMKilled" in by_step["pod-detail"]["summary"], ""),
        ("Pod 识别出重启 6 次", "6 次" in by_step["pod-detail"]["summary"], ""),
        ("日志命中内存溢出", "内存溢出" in by_step["crash-logs"]["summary"], ""),
        ("日志片段含 OutOfMemoryError", "OutOfMemoryError" in str(by_step["crash-logs"]["detail"]), ""),
        ("反推出归属 Deployment", "order-service 的 ReplicaSet" in by_step["recent-release"]["summary"], ""),
        ("带出镜像版本", "1.4.2" in by_step["recent-release"]["summary"], ""),
        ("报告含自动取证章节", "## 自动取证" in report["markdown"], ""),
        ("证据进入根因依据", any("k8s.pod" in e for e in report["fragments"][-1]["evidence"]), ""),
    ]

    print("=" * 66)
    for label, ok, note in checks:
        print(f"{'PASS' if ok else 'FAIL'}  | {label:<24} | {note if not ok else ''}".rstrip(" |"))
    print("=" * 66)

    print("\n=== 自动取证 ===")
    for item in evidence:
        print(f"[{'OK' if item['ok'] else 'FAIL'}] {item['step']:<15} {item['summary'] or item['error']}")

    server.shutdown()
    failed = [c for c in checks if not c[1]]
    print(f"\n合计：通过 {len(checks) - len(failed)}｜失败 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
