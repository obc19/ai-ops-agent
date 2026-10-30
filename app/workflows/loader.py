"""工作流定义加载：读 YAML、校验、按文件 mtime 缓存（改完重启或下一轮自然生效）。"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from app.config.settings import Settings, get_settings
from app.utils.logging import get_logger
from app.workflows.models import Workflow

logger = get_logger(__name__)

# (mtime, size) → 解析结果，避免每轮诊断都重新读盘
_CACHE: dict[str, tuple[tuple[float, int], list[Workflow]]] = {}


def load_workflows(settings: Settings | None = None) -> list[Workflow]:
    settings = settings or get_settings()
    path = Path(settings.workflow_path)
    if not path.is_file():
        logger.debug("未找到工作流定义文件：%s（取证工作流不启用）", path)
        return []

    try:
        stat = path.stat()
    except OSError:
        return []

    key = str(path)
    stamp = (stat.st_mtime, stat.st_size)
    cached = _CACHE.get(key)
    if cached and cached[0] == stamp:
        return cached[1]

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        # fail-open 到「不取证」：取证失败不该阻断诊断主流程
        logger.error("工作流定义解析失败，已跳过取证：%s", exc)
        _CACHE[key] = (stamp, [])
        return []

    items = raw.get("workflows") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        logger.warning("工作流文件缺少 workflows: 列表，已跳过")
        _CACHE[key] = (stamp, [])
        return []

    workflows: list[Workflow] = []
    for index, item in enumerate(items):
        try:
            workflow = Workflow.model_validate(item)
        except ValidationError as exc:
            logger.error("第 %d 个工作流定义非法，已跳过：%s", index + 1, exc.errors()[:2])
            continue
        if not workflow.triggers:
            logger.warning("工作流 %s 没有触发器，已跳过", workflow.id)
            continue
        workflows.append(workflow)

    logger.info(
        "取证工作流已加载：%d 个（%s）",
        len(workflows),
        "、".join(w.id for w in workflows) or "-",
    )
    _CACHE[key] = (stamp, workflows)
    return workflows


def clear_cache() -> None:
    _CACHE.clear()
