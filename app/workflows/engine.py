"""取证引擎：按触发器匹配工作流，逐步执行只读 Provider，产出可写进报告的证据。

三道护栏，都是为了避免「诊断本身变成故障源」：

1. 一次诊断最多跑 3 个工作流、每个最多 8 步；
2. 单步超时 + 总预算超时，超了直接放弃并如实记一条「取证超时」；
3. 任何一步失败都不抛出，转成 ``ok=False`` 的证据条目 —— 取证失败本身也是信息。
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from app.config.settings import Settings, get_settings
from app.models.diagnosis import EvidenceItem
from app.utils.logging import get_logger
from app.workflows.loader import load_workflows
from app.workflows.providers import REGISTRY, ProviderError

logger = get_logger(__name__)

MAX_WORKFLOWS = 3
MAX_STEPS = 8

_VAR = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def _lookup(path: str, scope: dict[str, Any]) -> Any:
    current: Any = scope
    for part in path.split("."):
        if not isinstance(current, dict):
            return ""
        current = current.get(part)
    return "" if current is None else current


def _render(value: Any, scope: dict[str, Any]) -> Any:
    """把 ``{{ anomaly.target }}`` 之类替换成实际值；整串只有一个变量时保留原始类型。"""
    if isinstance(value, str):
        whole = _VAR.fullmatch(value.strip())
        if whole:
            return _lookup(whole.group(1), scope)
        return _VAR.sub(lambda m: str(_lookup(m.group(1), scope)), value)
    if isinstance(value, dict):
        return {k: _render(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, scope) for v in value]
    return value


def _anomaly_scope(anomaly) -> dict[str, Any]:
    return {
        "anomaly": {
            "cluster": anomaly.cluster,
            "namespace": anomaly.namespace,
            "target": anomaly.target,
            "metric": anomaly.metric,
            "severity": anomaly.severity.value,
            "rule_id": anomaly.rule_id,
            "rule_name": anomaly.rule_name,
            "value": anomaly.value,
            "threshold": anomaly.threshold,
            "summary": anomaly.summary,
            "context": anomaly.context or {},
        }
    }


async def _run_step(step, scope: dict[str, Any], timeout: float) -> tuple[EvidenceItem, dict]:
    provider = REGISTRY.get(step.provider)
    started = time.monotonic()
    if provider is None:
        return (
            EvidenceItem(
                workflow="",
                step=step.name,
                provider=step.provider,
                ok=False,
                error=f"未知 Provider「{step.provider}」，可用：{'、'.join(sorted(REGISTRY))}",
            ),
            {},
        )

    params = _render(step.params_safe, scope)
    try:
        result = await asyncio.wait_for(provider(params, scope), timeout=timeout)
    except TimeoutError:
        return (
            EvidenceItem(
                workflow="",
                step=step.name,
                provider=step.provider,
                ok=False,
                error=f"取证超时（>{timeout:.0f}s）",
                elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            ),
            {},
        )
    except ProviderError as exc:
        return (
            EvidenceItem(
                workflow="",
                step=step.name,
                provider=step.provider,
                ok=False,
                error=str(exc)[:300],
                elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            ),
            {},
        )
    except Exception as exc:  # noqa: BLE001 - 取证失败不影响诊断
        logger.warning("取证步骤 %s 失败：%s", step.name, exc)
        return (
            EvidenceItem(
                workflow="",
                step=step.name,
                provider=step.provider,
                ok=False,
                error=f"{type(exc).__name__}: {exc}"[:300],
                elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            ),
            {},
        )

    return (
        EvidenceItem(
            workflow="",
            step=step.name,
            provider=step.provider,
            ok=result.ok,
            summary=result.summary,
            detail=result.detail,
            error=result.error,
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
        ),
        result.detail if result.ok else {},
    )


async def collect_evidence(anomaly, settings: Settings | None = None) -> list[EvidenceItem]:
    """按异常匹配工作流并取证。任何情况下都不抛异常。"""
    settings = settings or get_settings()
    if not settings.workflow_enabled:
        return []
    try:
        workflows = load_workflows(settings)
    except Exception as exc:  # noqa: BLE001
        logger.warning("加载取证工作流失败：%s", exc)
        return []

    matched = [w for w in workflows if w.matched_by(anomaly)][:MAX_WORKFLOWS]
    if not matched:
        return []

    deadline = time.monotonic() + settings.workflow_timeout
    scope: dict[str, Any] = _anomaly_scope(anomaly)
    scope["steps"] = {}

    items: list[EvidenceItem] = []
    for workflow in matched:
        for step in workflow.steps[:MAX_STEPS]:
            if time.monotonic() >= deadline:
                items.append(
                    EvidenceItem(
                        workflow=workflow.id,
                        step=step.name,
                        provider=step.provider,
                        ok=False,
                        error=f"整体取证预算耗尽（{settings.workflow_timeout:.0f}s），该步跳过",
                    )
                )
                break
            remaining = min(settings.workflow_step_timeout, deadline - time.monotonic())
            item, detail = await _run_step(step, scope, max(remaining, 0.5))
            item.workflow = workflow.id
            items.append(item)
            scope["steps"][step.name] = detail
            if not item.ok and not step.continue_on_error:
                break

    ok_count = sum(1 for item in items if item.ok)
    logger.info(
        "取证完成：%d 个工作流 / %d 步，成功 %d 步",
        len(matched),
        len(items),
        ok_count,
    )
    return items


def summarize_evidence(items: list[EvidenceItem]) -> list[str]:
    """压成几行自然语言，供规则模式的根因说明与 LLM prompt 使用。"""
    lines: list[str] = []
    for item in items:
        if item.ok and item.summary:
            lines.append(f"[{item.provider}] {item.step}：{item.summary}")
        elif not item.ok and item.error:
            lines.append(f"[{item.provider}] {item.step} 取证失败：{item.error}")
    return lines
