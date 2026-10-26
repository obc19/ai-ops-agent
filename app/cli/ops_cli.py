"""ops-cli：命令行触发诊断、查询告警、管理知识库。

设计为 HTTP 客户端，与 Web 控制台走同一套接口，避免逻辑分叉。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

DEFAULT_API = os.environ.get("AIOPS_API", "http://127.0.0.1:8080")
PREFIX = "/api/v1"


def _client(args) -> httpx.Client:
    headers = {}
    token = args.token or os.environ.get("AUTH_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(
        base_url=args.api.rstrip("/"),
        headers=headers,
        timeout=60,
        trust_env=False,
    )


def _print(data) -> None:
    if isinstance(data, str):
        print(data)
        return
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_health(args) -> int:
    with _client(args) as client:
        _print(client.get("/healthz").json())
    return 0


def cmd_overview(args) -> int:
    with _client(args) as client:
        data = client.get(f"{PREFIX}/overview").json()
    stats, mode = data["stats"], data["mode"]
    print(f"模式：mock={mode['mock']}｜LLM={'开' if mode['llm_enabled'] else '关'}"
          f"｜embedding={'开' if mode['embedding_enabled'] else '关'}"
          f"｜知识库={mode['knowledge_backend']}")
    print(f"告警：总计 {stats['alerts_total']}，活跃 {stats['alerts_active']}"
          f"｜诊断 {stats['diagnoses_total']}｜已确认经验 {stats['experiences_confirmed']}")
    print(f"分级：{data['severity_count']}")
    print(f"数据源：{[(s['name'], s['reachable']) for s in data['sources']]}")
    if data["mock_faults"]:
        print(f"当前 mock 故障：{data['mock_faults']}")
    return 0


def cmd_diagnose(args) -> int:
    payload = {
        "target": args.target,
        "metric": args.metric,
        "cluster": args.cluster,
        "namespace": args.namespace,
        "severity": args.severity,
        "notify": not args.no_notify,
        "window_minutes": args.window,
    }
    if args.value is not None:
        payload["value"] = args.value
    if args.threshold is not None:
        payload["threshold"] = args.threshold
    with _client(args) as client:
        data = client.post(f"{PREFIX}/diagnostics", json=payload).json()
    report, alert = data["report"], data["alert"]
    print(f"诊断 {report['id']}｜状态={report['status']}｜置信度={report['confidence']}"
          f"｜耗时={report['duration_ms'] / 1000:.2f}s")
    print(f"根因：{report['root_cause']}")
    if report["suggestion"]:
        print("建议：")
        for idx, step in enumerate(report["suggestion"], 1):
            print(f"  {idx}. {step}")
    print(f"告警：{alert['id']}（{alert['severity']}，{'新建' if data['alert_is_new'] else '已聚合'}）")
    for note in data["notifications"]:
        print(f"  渠道 {note['channel']}: {'成功' if note['ok'] else '失败'} - {note['detail']}")
    return 0


def cmd_detect(args) -> int:
    with _client(args) as client:
        data = client.post(f"{PREFIX}/diagnostics/detect", json={"notify": not args.no_notify}).json()
    print(f"本轮检出 {data['count']} 条异常")
    for item in data["items"]:
        print(f"  [{item['severity']}] {item['target']} → 报告 {item['report_id']}")
    return 0


def cmd_alerts(args) -> int:
    params = {"limit": args.limit}
    if args.severity:
        params["severity"] = args.severity
    if args.status:
        params["status"] = args.status
    with _client(args) as client:
        data = client.get(f"{PREFIX}/alerts", params=params).json()
    print(f"共 {data['total']} 条（分级 {data['severity_count']}）")
    for item in data["items"]:
        print(f"  {item['id']} [{item['severity']}/{item['status']}] "
              f"{item['cluster']}/{item['target']} {item['metric']} ×{item['count']}")
    return 0


def cmd_alert_action(args) -> int:
    body = {"note": args.note or "", "resolved_by": args.by or "operator"}
    if args.action == "confirm":
        body = {"confirmed_by": args.by or "operator", "note": args.note or ""}
    with _client(args) as client:
        resp = client.post(f"{PREFIX}/alerts/{args.id}/{args.action}", json=body)
    if resp.status_code >= 400:
        print(f"失败（HTTP {resp.status_code}）：{resp.json().get('detail')}")
        return 1
    data = resp.json()
    print(f"告警 {data['id']} 状态 → {data['status']}")
    for item in data.get("status_history", []):
        print(f"  {item['at']} {item['to_status']} by {item['actor']} {item['note']}")
    return 0


def cmd_clusters(args) -> int:
    with _client(args) as client:
        data = client.get(f"{PREFIX}/clusters/sources").json()
    print(f"共 {len(data)} 个集群")
    for item in data:
        print(f"  {item['name']}（ns={item['namespace']}，enabled={item['enabled']}）")
        for name, url in item["sources"].items():
            if name == "elasticsearch_index" or not url:
                continue
            print(f"    - {name}: {url}")
    return 0


def cmd_ingest(args) -> int:
    """把 Alertmanager 告警（文件或命令行参数）灌入诊断闭环。"""
    if args.file:
        payload = json.loads(open(args.file, encoding="utf-8").read())
    else:
        payload = {
            "version": "4",
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "fingerprint": "cli",
                    "labels": {
                        "alertname": args.metric,
                        "severity": args.severity.lower(),
                        "instance": args.target,
                        "cluster": args.cluster,
                        "namespace": args.namespace,
                    },
                    "annotations": {"summary": args.summary or f"{args.target} {args.metric}"},
                }
            ],
        }
    with _client(args) as client:
        resp = client.post(
            f"{PREFIX}/ingest/alertmanager",
            json=payload,
            params={"notify": not args.no_notify},
        )
    if resp.status_code >= 400:
        print(f"失败（HTTP {resp.status_code}）：{resp.json().get('detail')}")
        return 1
    data = resp.json()
    print(f"接收 {data['accepted']} 条，跳过 {data['skipped']} 条")
    for item in data["items"]:
        if item.get("error"):
            print(f"  失败 {item['target']}：{item['error']}")
        else:
            print(f"  [{item['severity']}] {item['target']} → 告警 {item['alert_id']}（{item['alert_status']}）")
    return 0


def cmd_report(args) -> int:
    with _client(args) as client:
        text = client.get(f"{PREFIX}/diagnostics/{args.id}/markdown").text
    print(text)
    return 0


def cmd_knowledge(args) -> int:
    with _client(args) as client:
        if args.action == "list":
            items = client.get(f"{PREFIX}/knowledge", params={"status": args.status}).json()
            print(f"共 {len(items)} 条经验")
            for exp in items:
                print(f"  {exp['id']} [{exp['status']}] {exp['title']} 确认人={exp['confirmed_by'] or '-'}")
        elif args.action == "confirm":
            body = {"confirmed_by": args.by or "operator", "force": args.force}
            resp = client.post(f"{PREFIX}/knowledge/{args.id}/confirm", json=body)
            if resp.status_code >= 400:
                print(f"失败（HTTP {resp.status_code}）：{resp.json().get('detail')}")
                return 1
            exp = resp.json()
            print(f"经验 {exp['id']} 已确认入库，向量来源={exp['embedding_source']}")
        elif args.action == "rollback":
            body = {"reason": args.reason or "manual rollback", "actor": args.by or "operator"}
            resp = client.post(f"{PREFIX}/knowledge/{args.id}/rollback", json=body)
            if resp.status_code >= 400:
                print(f"失败（HTTP {resp.status_code}）：{resp.json().get('detail')}")
                return 1
            print(f"经验 {resp.json()['id']} 已回滚")
        elif args.action == "stats":
            _print(client.get(f"{PREFIX}/knowledge/stats").json())
        elif args.action == "search":
            data = client.get(f"{PREFIX}/knowledge/search", params={"q": args.query}).json()
            for hit in data["hits"]:
                print(f"  [{hit['score']:.3f}] {hit['title']} — {hit['root_cause'][:60]}")
    return 0


def cmd_audit(args) -> int:
    with _client(args) as client:
        data = client.get(f"{PREFIX}/audit", params={"limit": args.limit, "egress_only": args.egress}).json()
    print(f"共 {data['total']} 条审计记录")
    for item in data["items"]:
        flag = "外发" if item["egress"] else "内部"
        print(f"  {item['ts']} [{flag}] {item['action']} actor={item['actor']} {item['target']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ops-cli", description="ai-ops-agent 命令行工具")
    parser.add_argument("--api", default=DEFAULT_API, help="服务地址，默认 %(default)s")
    parser.add_argument("--token", default="", help="AUTH_TOKEN（未配置鉴权时留空）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="健康检查").set_defaults(func=cmd_health)
    sub.add_parser("overview", help="全局总览").set_defaults(func=cmd_overview)

    p = sub.add_parser("diagnose", help="触发一次诊断")
    p.add_argument("--target", required=True)
    p.add_argument("--metric", default="cpu_usage_percent")
    p.add_argument("--cluster", default="prod-cluster")
    p.add_argument("--namespace", default="prod")
    p.add_argument("--severity", default="P1", choices=["P0", "P1", "P2"])
    p.add_argument("--value", type=float)
    p.add_argument("--threshold", type=float)
    p.add_argument("--window", type=int, default=5, help="时间窗口（分钟）")
    p.add_argument("--no-notify", action="store_true", help="不推送告警")
    p.set_defaults(func=cmd_diagnose)

    p = sub.add_parser("detect", help="立即执行一轮采集与检测")
    p.add_argument("--no-notify", action="store_true")
    p.set_defaults(func=cmd_detect)

    p = sub.add_parser("alerts", help="告警列表")
    p.add_argument("--severity", choices=["P0", "P1", "P2"])
    p.add_argument("--status", choices=["triggered", "notified", "confirmed", "resolved"])
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_alerts)

    for action in ("confirm", "resolve", "reopen"):
        p = sub.add_parser(f"alert-{action}", help=f"{action} 告警")
        p.add_argument("id")
        p.add_argument("--by", default="operator")
        p.add_argument("--note", default="")
        p.set_defaults(func=cmd_alert_action, action=action)

    sub.add_parser("clusters", help="已配置集群与数据源").set_defaults(func=cmd_clusters)

    p = sub.add_parser("ingest", help="把 Alertmanager 告警灌入诊断闭环")
    p.add_argument("--file", default="", help="Alertmanager webhook JSON 文件")
    p.add_argument("--target", default="unknown")
    p.add_argument("--metric", default="external_alert")
    p.add_argument("--cluster", default="prod-cluster")
    p.add_argument("--namespace", default="prod")
    p.add_argument("--severity", default="P1", choices=["P0", "P1", "P2"])
    p.add_argument("--summary", default="")
    p.add_argument("--no-notify", action="store_true")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("report", help="打印诊断报告 Markdown")
    p.add_argument("id")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("knowledge", help="知识库管理")
    p.add_argument("action", choices=["list", "confirm", "rollback", "stats", "search"])
    p.add_argument("id", nargs="?", default="")
    p.add_argument("--by", default="operator")
    p.add_argument("--status", default=None)
    p.add_argument("--reason", default="")
    p.add_argument("--query", default="")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_knowledge)

    p = sub.add_parser("audit", help="审计日志")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--egress", action="store_true", help="只看数据外发记录")
    p.set_defaults(func=cmd_audit)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except httpx.HTTPError as exc:
        print(f"请求失败：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
