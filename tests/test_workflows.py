"""取证工作流：触发匹配、模板渲染、步骤串联、超时降级、报告渲染。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.config.settings import get_settings
from app.models.diagnosis import DiagnosisReport, EvidenceItem
from app.models.enums import Severity
from app.models.events import AnomalyEvent, TimeWindow
from app.workflows import engine as wf_engine
from app.workflows import loader as wf_loader
from app.workflows.models import Step, Trigger, Workflow
from app.workflows.providers import ProviderResult


def _anomaly(
    metric: str = "k8s-crashloop",
    target: str = "order-service-7d9f8c",
    severity: Severity = Severity.P0,
    source: str = "k8s",
) -> AnomalyEvent:
    now = datetime.now(UTC)
    return AnomalyEvent(
        source=source,
        cluster="prod-cluster",
        namespace="prod",
        target=target,
        metric=metric,
        rule_id=metric,
        rule_name="容器反复崩溃",
        severity=severity,
        value=6,
        threshold=1,
        window=TimeWindow(start=now - timedelta(minutes=5), end=now),
    )


@pytest.fixture
def wf_settings(tmp_path, monkeypatch):
    """把工作流文件指到临时目录，避免受仓库里的配置影响。"""

    def _write(body: str):
        path = tmp_path / "workflows.yaml"
        path.write_text(body, encoding="utf-8")
        wf_loader.clear_cache()
        settings = get_settings()
        monkeypatch.setattr(settings, "workflow_enabled", True)
        monkeypatch.setattr(settings, "workflow_config", str(path))
        monkeypatch.setattr(settings, "workflow_timeout", 5.0)
        monkeypatch.setattr(settings, "workflow_step_timeout", 1.0)
        return settings

    yield _write
    wf_loader.clear_cache()


# ---------------- 触发器 ----------------


def test_trigger_requires_all_filled_fields():
    trigger = Trigger(rules=["k8s-*"], severity=["P0"])
    assert trigger.matches(_anomaly(severity=Severity.P0))
    # 级别不符就不该触发
    assert not trigger.matches(_anomaly(severity=Severity.P2))


def test_trigger_glob_matches_rule_id_or_metric():
    assert Trigger(rules=["k8s_*"]).matches(_anomaly(metric="k8s_pod_restarts", source="metric"))
    assert Trigger(rules=["probe_ok"]).matches(_anomaly(metric="probe_ok"))
    assert not Trigger(rules=["k8s-*"]).matches(_anomaly(metric="cpu_usage_percent"))


def test_trigger_source_and_regex():
    assert Trigger(source=["metric"]).matches(_anomaly(metric="cpu_usage_percent", source="metric"))
    assert not Trigger(source=["metric"]).matches(_anomaly())
    assert Trigger(target_regex=r"-7d9f8c$").matches(_anomaly())
    # 非法正则不能炸，只是不匹配
    assert not Trigger(target_regex=r"([").matches(_anomaly())


def test_empty_trigger_never_matches():
    """没有条件的 workflow 不能对所有告警乱跑。"""
    workflow = Workflow(id="x", triggers=[Trigger()], steps=[])
    assert not workflow.matched_by(_anomaly())


def test_disabled_workflow_skipped():
    workflow = Workflow(
        id="x", enabled=False, triggers=[Trigger(rules=["k8s-*"])], steps=[]
    )
    assert not workflow.matched_by(_anomaly())


# ---------------- 加载 ----------------


def test_loader_parses_repo_config():
    settings = get_settings()
    workflows = wf_loader.load_workflows(settings)
    ids = {w.id for w in workflows}
    assert "k8s-crash-evidence" in ids
    crash = next(w for w in workflows if w.id == "k8s-crash-evidence")
    assert [s.name for s in crash.steps] == ["pod-detail", "crash-logs", "recent-release"]
    assert crash.steps[0].params["namespace"] == "{{ anomaly.namespace }}"


def test_loader_rejects_invalid_workflow(wf_settings):
    settings = wf_settings(
        """
workflows:
  - id: no-trigger
    steps: []
  - id: bad
    triggers:
      - rules: ["x"]
"""
    )
    # 没有触发器的被跳过；缺少 steps 的仍可加载（只是不取证）
    workflows = wf_loader.load_workflows(settings)
    assert [w.id for w in workflows] == ["bad"]


def test_loader_survives_broken_yaml(wf_settings):
    settings = wf_settings("this: [is: not: valid")
    assert wf_loader.load_workflows(settings) == []


# ---------------- 模板渲染 ----------------


def test_render_variables_and_types():
    scope = {"anomaly": {"target": "api-1", "value": 6}, "steps": {}}
    assert wf_engine._render("{{ anomaly.target }}", scope) == "api-1"
    # 整串只有一个变量时保留原始类型，别把数字变成字符串
    assert wf_engine._render("{{ anomaly.value }}", scope) == 6
    assert wf_engine._render("ns-{{ anomaly.target }}", scope) == "ns-api-1"
    assert wf_engine._render({"a": ["{{ anomaly.target }}"]}, scope) == {"a": ["api-1"]}
    # 引用不存在的字段得到空串，不抛异常
    assert wf_engine._render("{{ anomaly.nope }}", scope) == ""


# ---------------- 执行 ----------------


@pytest.mark.asyncio
async def test_steps_chain_and_reference_previous_output(wf_settings, monkeypatch):
    settings = wf_settings(
        """
workflows:
  - id: chain
    triggers:
      - rules: ["k8s-*"]
    steps:
      - name: first
        provider: fake.first
      - name: second
        provider: fake.second
        with:
          pod: "{{ steps.first.pod }}"
"""
    )
    seen: dict = {}

    async def first(params, _scope):
        return ProviderResult(ok=True, summary="拿到了 pod", detail={"pod": "api-1"})

    async def second(params, _scope):
        seen.update(params)
        return ProviderResult(ok=True, summary=f"收到 {params['pod']}", detail={})

    monkeypatch.setitem(wf_engine.REGISTRY, "fake.first", first)
    monkeypatch.setitem(wf_engine.REGISTRY, "fake.second", second)

    items = await wf_engine.collect_evidence(_anomaly(), settings)
    assert [i.step for i in items] == ["first", "second"]
    assert all(i.ok for i in items)
    assert items[0].workflow == "chain"
    assert seen["pod"] == "api-1"


@pytest.mark.asyncio
async def test_failure_is_recorded_not_raised(wf_settings, monkeypatch):
    settings = wf_settings(
        """
workflows:
  - id: broken
    triggers:
      - rules: ["k8s-*"]
    steps:
      - name: boom
        provider: fake.boom
"""
    )

    async def boom(params, _scope):
        raise RuntimeError("apiserver 挂了")

    monkeypatch.setitem(wf_engine.REGISTRY, "fake.boom", boom)
    items = await wf_engine.collect_evidence(_anomaly(), settings)
    assert len(items) == 1
    assert items[0].ok is False
    assert "apiserver 挂了" in items[0].error


@pytest.mark.asyncio
async def test_unknown_provider_reports_available_names(wf_settings):
    settings = wf_settings(
        """
workflows:
  - id: typo
    triggers:
      - rules: ["k8s-*"]
    steps:
      - name: oops
        provider: k8s.pdo
"""
    )
    items = await wf_engine.collect_evidence(_anomaly(), settings)
    assert items[0].ok is False
    assert "k8s.pod" in items[0].error


@pytest.mark.asyncio
async def test_step_timeout_does_not_block(wf_settings, monkeypatch):
    settings = wf_settings(
        """
workflows:
  - id: slow
    triggers:
      - rules: ["k8s-*"]
    steps:
      - name: slow-step
        provider: fake.slow
"""
    )

    async def slow(params, _scope):
        await asyncio.sleep(5)
        return ProviderResult(ok=True, summary="不该到这")

    monkeypatch.setitem(wf_engine.REGISTRY, "fake.slow", slow)
    items = await wf_engine.collect_evidence(_anomaly(), settings)
    assert items[0].ok is False
    assert "超时" in items[0].error


@pytest.mark.asyncio
async def test_continue_on_error_false_stops_workflow(wf_settings, monkeypatch):
    settings = wf_settings(
        """
workflows:
  - id: strict
    triggers:
      - rules: ["k8s-*"]
    steps:
      - name: first
        provider: fake.fail
        continue_on_error: false
      - name: second
        provider: fake.ok
"""
    )

    async def fail(params, _scope):
        raise RuntimeError("不行")

    async def ok(params, _scope):
        return ProviderResult(ok=True, summary="ok", detail={})

    monkeypatch.setitem(wf_engine.REGISTRY, "fake.fail", fail)
    monkeypatch.setitem(wf_engine.REGISTRY, "fake.ok", ok)
    items = await wf_engine.collect_evidence(_anomaly(), settings)
    assert [i.step for i in items] == ["first"]


@pytest.mark.asyncio
async def test_no_match_runs_nothing(wf_settings, monkeypatch):
    settings = wf_settings(
        """
workflows:
  - id: only-probe
    triggers:
      - rules: ["probe_*"]
    steps:
      - name: nope
        provider: fake.first
"""
    )
    monkeypatch.setitem(wf_engine.REGISTRY, "fake.first", lambda p, s: None)
    assert await wf_engine.collect_evidence(_anomaly(), settings) == []


@pytest.mark.asyncio
async def test_disabled_globally(wf_settings, monkeypatch):
    settings = wf_settings(
        """
workflows:
  - id: any
    triggers:
      - rules: ["k8s-*"]
    steps:
      - name: x
        provider: fake.first
"""
    )
    monkeypatch.setattr(settings, "workflow_enabled", False)
    assert await wf_engine.collect_evidence(_anomaly(), settings) == []


# ---------------- 汇总与渲染 ----------------


def test_summarize_evidence_lines():
    items = [
        EvidenceItem(step="pod", provider="k8s.pod", ok=True, summary="Running 但未就绪"),
        EvidenceItem(step="logs", provider="k8s.logs", ok=False, error="无权限"),
        EvidenceItem(step="empty", provider="k8s.pod", ok=True, summary=""),
    ]
    lines = wf_engine.summarize_evidence(items)
    assert "Running 但未就绪" in lines[0]
    assert "无权限" in lines[1]
    assert len(lines) == 2


def test_report_renders_evidence_section():
    from app.agents.report import render_markdown

    report = DiagnosisReport(
        target="order-service",
        evidence=[
            EvidenceItem(
                workflow="k8s-crash-evidence",
                step="crash-logs",
                provider="k8s.logs",
                ok=True,
                summary="取到上一个容器实例最近 80 行日志；日志命中「内存溢出」",
                detail={"preview": "java.lang.OutOfMemoryError: Java heap space"},
                elapsed_ms=48.2,
            )
        ],
    )
    markdown = render_markdown(report)
    assert "## 自动取证" in markdown
    assert "k8s.logs" in markdown
    assert "OutOfMemoryError" in markdown


def test_workflows_endpoint_lists_definitions():
    from fastapi.testclient import TestClient

    from app.main import app

    body = TestClient(app).get("/api/v1/workflows").json()
    assert body["enabled"] is True
    assert "k8s.pod" in body["providers"]
    ids = {item["id"] for item in body["items"]}
    assert {"k8s-crash-evidence", "k8s-workload-evidence"} <= ids
    crash = next(i for i in body["items"] if i["id"] == "k8s-crash-evidence")
    assert [s["provider"] for s in crash["steps"]] == ["k8s.pod", "k8s.logs", "k8s.rollout"]


@pytest.mark.asyncio
async def test_k8s_provider_reports_reason_without_cluster(monkeypatch):
    """没有集群时不能装作采集成功，要说清楚为什么不可用。"""
    from app.config.settings import get_settings
    from app.workflows.providers import ProviderError, k8s_pod

    settings = get_settings()
    monkeypatch.setattr(settings, "enable_k8s_adapter", False)
    monkeypatch.setattr(settings, "kubernetes_api_url", "")

    with pytest.raises(ProviderError) as exc:
        await k8s_pod({"namespace": "prod", "name": "api-1"}, {})
    assert "K8s 取证不可用" in str(exc.value)


@pytest.mark.asyncio
async def test_k8s_pod_requires_arguments():
    from app.workflows.providers import ProviderError, k8s_pod

    with pytest.raises(ProviderError) as exc:
        await k8s_pod({"namespace": "prod"}, {})
    assert "需要 namespace 与 name" in str(exc.value)


@pytest.mark.asyncio
async def test_http_get_rejects_non_http_url():
    from app.workflows.providers import ProviderError, http_get

    with pytest.raises(ProviderError):
        await http_get({"url": "file:///etc/passwd"}, {})


def test_pod_metrics_of_failed_request_is_evidence_not_exception():
    """http.get 探不通也要算一条证据（探不通本身就是结论）。"""
    import asyncio

    from app.workflows.providers import http_get

    result = asyncio.run(http_get({"url": "http://127.0.0.1:1/healthz", "timeout": 1}, {}))
    assert result.ok is True
    assert "失败" in result.summary or "返回" in result.summary


@pytest.mark.asyncio
async def test_k8s_provider_reports_reason_without_cluster(monkeypatch):
    """没有集群时不能装作采集成功，要说清楚为什么不可用。"""
    from app.config.settings import get_settings
    from app.workflows.providers import ProviderError, k8s_pod

    settings = get_settings()
    monkeypatch.setattr(settings, "enable_k8s_adapter", False)
    monkeypatch.setattr(settings, "kubernetes_api_url", "")

    with pytest.raises(ProviderError) as exc:
        await k8s_pod({"namespace": "prod", "name": "api-1"}, {})
    assert "K8s 取证不可用" in str(exc.value)


@pytest.mark.asyncio
async def test_k8s_pod_requires_arguments():
    from app.workflows.providers import ProviderError, k8s_pod

    with pytest.raises(ProviderError) as exc:
        await k8s_pod({"namespace": "prod"}, {})
    assert "需要 namespace 与 name" in str(exc.value)


@pytest.mark.asyncio
async def test_http_get_rejects_non_http_url():
    from app.workflows.providers import ProviderError, http_get

    with pytest.raises(ProviderError):
        await http_get({"url": "file:///etc/passwd"}, {})


def test_pod_metrics_of_failed_request_is_evidence_not_exception():
    """http.get 探不通也要算一条证据（探不通本身就是结论）。"""
    import asyncio

    from app.workflows.providers import http_get

    result = asyncio.run(http_get({"url": "http://127.0.0.1:1/healthz", "timeout": 1}, {}))
    assert result.ok is True
    assert "失败" in result.summary or "返回" in result.summary
