"""端到端验收自证脚本：一条命令跑完需求文档第 6 章的 8 条验收标准。

用法：
    python scripts/demo.py            # 自动起一个 mock LLM + 本地服务，跑完整闭环
    python scripts/demo.py --verbose  # 打印每步细节

退出码：全部通过（或按环境跳过）为 0，存在失败为 1。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

# ---------------------------------------------------------------- mock LLM 服务
class MockLLM:
    """同时提供 chat 与 embedding 的 OpenAI 兼容服务，并记录全部外发请求。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("content-length", 0))
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    body = {"_raw": raw.decode("utf-8", "replace")}
                with outer._lock:
                    outer.requests.append({"path": self.path, "body": body})

                if self.path.endswith("/embeddings"):
                    inputs = body.get("input") or []
                    if isinstance(inputs, str):
                        inputs = [inputs]
                    payload = {
                        "object": "list",
                        "data": [
                            {"index": i, "embedding": _fake_vector(text)} for i, text in enumerate(inputs)
                        ],
                        "model": body.get("model", "demo-embed"),
                    }
                else:
                    payload = {
                        "id": "chatcmpl-demo",
                        "object": "chat.completion",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": json.dumps(
                                        {
                                            "root_cause": "【LLM】资源配额不足叠加线程池耗尽，导致请求排队并触发限流",
                                            "suggestion": ["扩容副本并上调 CPU limit", "排查阻塞调用与死循环"],
                                            "confidence": 0.86,
                                        },
                                        ensure_ascii=False,
                                    ),
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "model": body.get("model", "demo-chat"),
                    }
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):  # 静音
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def payloads(self) -> list[str]:
        with self._lock:
            return [json.dumps(item, ensure_ascii=False) for item in self.requests]

    def close(self) -> None:
        self.server.shutdown()


def _fake_vector(text: str, dim: int = 32) -> list[float]:
    import hashlib

    vector = [0.0] * dim
    for token in str(text).split() or [""]:
        digest = hashlib.md5(token.encode("utf-8")).digest()
        vector[digest[0] % dim] += 1.0
    norm = sum(v * v for v in vector) ** 0.5 or 1.0
    return [round(v / norm, 6) for v in vector]


# ---------------------------------------------------------------- 工具
def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Results:
    def __init__(self, verbose: bool = False) -> None:
        self.items: list[tuple[str, str, str]] = []
        self.verbose = verbose

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.items.append((name, status, detail))
        if self.verbose:
            print(f"  [{status}] {name} — {detail}")

    def check(self, name: str, ok: bool, detail: str = "", skip: bool = False) -> None:
        self.add(name, "SKIP" if skip else ("PASS" if ok else "FAIL"), detail)

    def report(self) -> int:
        width = max((len(n) for n, _, _ in self.items), default=10)
        print()
        print("=" * (width + 30))
        passed = failed = skipped = 0
        for name, status, detail in self.items:
            mark = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP"}[status]
            print(f"{mark:5} | {name:<{width}} | {detail[:90]}")
            passed += status == "PASS"
            failed += status == "FAIL"
            skipped += status == "SKIP"
        print("=" * (width + 30))
        print(f"合计：通过 {passed}｜失败 {failed}｜跳过 {skipped}")
        return 1 if failed else 0


def wait_http(client: httpx.Client, path: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if client.get(path).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.4)
    return False


# ---------------------------------------------------------------- 主流程
def run(verbose: bool = False) -> int:
    results = Results(verbose)
    llm = MockLLM()
    api_port = free_port()
    data_dir = Path(tempfile.mkdtemp(prefix="aiops-demo-"))
    base = f"http://127.0.0.1:{api_port}"

    env = {
        **os.environ,
        "MOCK_MODE": "true",
        "PORT": str(api_port),
        "DATA_DIR": str(data_dir),
        "DETECT_INTERVAL": "3600",
        "AUTH_TOKEN": "",
        "LOG_LEVEL": "WARNING",
        "LLM_BASE_URL": llm.base_url,
        "LLM_MODEL": "demo-chat",
        "LLM_API_KEY": "demo-key",
        "EMBEDDING_BASE_URL": llm.base_url,
        "EMBEDDING_MODEL": "demo-embed",
        "EMBEDDING_API_KEY": "demo-key",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }

    print(f"启动服务：{base}（数据目录 {data_dir}）")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(api_port),
         "--log-level", "warning"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL if not verbose else None,
        stderr=subprocess.DEVNULL if not verbose else None,
    )
    client = httpx.Client(base_url=base, timeout=90, trust_env=False)

    try:
        if not wait_http(client, "/healthz"):
            results.check("服务启动", False, "健康检查超时")
            return results.report()
        results.check("服务启动", True, client.get("/healthz").json()["version"])

        # ---- 验收 1：数据源可接入可查询 ----
        overview = client.get("/api/v1/overview").json()
        sources = overview["sources"]
        results.check(
            "1 数据源可接入可查询",
            bool(sources) and all(s["reachable"] for s in sources),
            f"{[(s['name'], s['reachable']) for s in sources]}",
        )

        # ---- 验收 2：模拟故障 60s 内诊断 + 分级告警 ----
        client.post("/_mock/fault", json={"target": "order-service", "metric": "cpu_usage_percent"})
        started = time.perf_counter()
        detect = client.post("/api/v1/diagnostics/detect", json={"notify": True}).json()
        elapsed = time.perf_counter() - started
        items = detect.get("items") or []
        report_ok = bool(items)
        alert_sev = items[0]["severity"] if items else "-"
        results.check(
            "2 60s 内诊断并分级告警",
            report_ok and elapsed <= 60,
            f"检出 {len(items)} 条，级别 {alert_sev}，耗时 {elapsed:.2f}s",
        )

        # ---- 验收 5：脱敏后外发不含敏感值 ----
        sensitive_summary = "连接 10.1.2.3:9200 失败 token=abcd1234efgh 用户 alice@example.com"
        client.post(
            "/api/v1/diagnostics",
            json={
                "target": "order-service",
                "metric": "cpu_usage_percent",
                "summary": sensitive_summary,
                "notify": False,
            },
        )
        egress = client.get("/api/v1/audit", params={"egress_only": True}).json()
        payloads = "\n".join(llm.payloads())
        leaked = any(
            token in payloads for token in ("10.1.2.3", "abcd1234efgh", "alice@example.com")
        )
        results.check(
            "5 脱敏后外发不含敏感值",
            (not leaked) and egress["total"] > 0,
            f"外发记录 {egress['total']} 条，泄漏={leaked}",
        )

        # ---- 验收 7：告警渠道送达 ----
        sink = client.get("/_mock/sink").json()
        channels = sorted({item["channel"] for item in sink["items"]})
        results.check(
            "7 告警渠道成功送达",
            len(channels) >= 1,
            f"送达渠道 {channels}，共 {sink['total']} 条",
        )

        # ---- 验收 6：经验入库并被 RAG 命中 ----
        reports = client.get("/api/v1/diagnostics", params={"limit": 20}).json()
        target_report = next((r for r in reports if r["target"] == "order-service"), reports[0])
        draft = client.post(
            "/api/v1/knowledge", json={"diagnosis_id": target_report["id"], "tags": ["cpu"]}
        ).json()
        confirmed = client.post(
            f"/api/v1/knowledge/{draft['id']}/confirm", json={"confirmed_by": "demo"}
        )
        confirm_ok = confirmed.status_code == 200 and confirmed.json()["status"] == "confirmed"

        client.post("/_mock/reset")
        client.post("/_mock/fault", json={"target": "order-service", "metric": "cpu_usage_percent"})
        second = client.post("/api/v1/diagnostics/detect", json={"notify": False}).json()
        rag_hits = []
        if second.get("items"):
            detail = client.get(f"/api/v1/diagnostics/{second['items'][0]['report_id']}").json()
            rag_hits = detail.get("rag_hits") or []
        results.check(
            "6 经验入库并被 RAG 命中",
            confirm_ok and bool(rag_hits),
            f"入库={confirm_ok}，命中 {len(rag_hits)} 条"
            + (f"，最高 {rag_hits[0]['score']:.2f}" if rag_hits else ""),
        )

        # ---- 验收 3/4：容器化与镜像指标（需 Docker）----
        docker = shutil.which("docker")
        if docker:
            try:
                result = subprocess.run(
                    [docker, "compose", "up", "-d", "--build"],
                    cwd=str(ROOT / "deploy"),
                    capture_output=True,
                    text=True,
                    timeout=900,
                )
                up_ok = result.returncode == 0
                results.check("3 docker compose 一键启动", up_ok, result.stderr[-120:] if not up_ok else "ok")
                if up_ok:
                    size = subprocess.run(
                        [docker, "image", "inspect", "ghcr.io/obc19/ai-ops-agent:latest",
                         "--format", "{{.Size}}"],
                        capture_output=True, text=True,
                    )
                    mb = int(size.stdout.strip() or 0) / 1024 / 1024 if size.stdout.strip() else 0
                    results.check("4 镜像体积 ≤300MB", 0 < mb <= 300, f"{mb:.1f} MB")
                    subprocess.run([docker, "compose", "down"], cwd=str(ROOT / "deploy"),
                                   capture_output=True, text=True)
            except subprocess.TimeoutExpired:
                results.check("3 docker compose 一键启动", False, "构建超时")
        else:
            results.check("3 docker compose 一键启动", False, "本机未安装 Docker", skip=True)
            results.check("4 镜像体积 ≤300MB", False, "本机未安装 Docker", skip=True)

        # ---- 验收 8：开源仓库要素 ----
        required = [
            "LICENSE", "README.md", "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md",
            ".github/workflows/ci.yml", ".github/workflows/release.yml",
        ]
        missing = [name for name in required if not (ROOT / name).exists()]
        results.check(
            "8 开源仓库要素齐备",
            not missing,
            "齐全" if not missing else f"缺少 {missing}",
        )

    finally:
        client.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        llm.close()
        shutil.rmtree(data_dir, ignore_errors=True)

    return results.report()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ai-ops-agent 端到端验收脚本")
    parser.add_argument("--verbose", action="store_true", help="输出详细过程与子进程日志")
    args = parser.parse_args()
    raise SystemExit(run(verbose=args.verbose))
