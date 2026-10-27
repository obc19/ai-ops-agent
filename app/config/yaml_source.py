"""YAML 配置文件：把「从仓库内文件取变量」改成「从挂载的配置文件取变量」。

优先级（高 → 低）：

1. ``Settings(...)`` 构造参数
2. 环境变量（``LLM_API_KEY`` 等，全大写字段名）
3. ``.env`` 文件
4. **YAML 配置文件**（本模块，默认 ``/etc/aiops/aiops.yaml``）
5. 代码内默认值

这样既能让用户 ``-v ./config:/etc/aiops:ro`` 一次性挂载全部配置，
又保留了「临时用环境变量覆盖某一项」的能力，老部署方式不会失效。

文件发现顺序：

1. 显式 ``CONFIG_FILE`` 环境变量（或 ``config_file`` 字段）
2. ``CONFIG_DIR``/aiops.yaml
3. ``/etc/aiops/aiops.yaml``（容器挂载约定路径）
4. ``<项目根>/config/aiops.yaml``（镜像内置默认 / 本地开发）
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic_settings import PydanticBaseSettingsSource

logger = logging.getLogger(__name__)

CONFIG_FILENAME = "aiops.yaml"
DEFAULT_CONFIG_DIR = "/etc/aiops"

# 分区里的短键 → Settings 字段名。
# 未列出的键按「原样即字段名」处理（runtime / sources / clusters / modules / auth / alerting）。
_SECTION_ALIASES: dict[tuple[str, str], str] = {
    ("llm", "base_url"): "llm_base_url",
    ("llm", "model"): "llm_model",
    ("llm", "api_key"): "llm_api_key",
    ("llm", "timeout"): "llm_timeout",
    ("embedding", "base_url"): "embedding_base_url",
    ("embedding", "model"): "embedding_model",
    ("embedding", "api_key"): "embedding_api_key",
    ("embedding", "dim"): "embedding_dim",
    ("knowledge", "backend"): "knowledge_backend",
    ("knowledge", "dedup_threshold"): "knowledge_dedup_threshold",
    ("knowledge", "top_k"): "knowledge_top_k",
    ("knowledge", "min_score"): "rag_min_score",
    ("detection", "interval"): "detect_interval",
    ("detection", "retention_days"): "retention_days",
    ("detection", "alert_group_window"): "alert_group_window",
    ("detection", "alert_cooldown"): "alert_cooldown",
    ("rules", "detection"): "detection_rules",
    ("rules", "desensitize"): "desensitize_rules",
    ("rules", "clusters"): "clusters_config",
    ("agent", "timeout"): "agent_timeout",
    ("agent", "diagnosis_budget"): "diagnosis_budget",
}


def _project_root() -> Path:
    from app.config.constants import PROJECT_ROOT

    return PROJECT_ROOT


def resolve_config_file(explicit: str | Path | None = None) -> Path | None:
    """按发现顺序返回第一个存在的配置文件；都没有则返回 None。"""
    candidates: list[Path] = []

    explicit = explicit or os.environ.get("CONFIG_FILE", "")
    if explicit:
        candidates.append(Path(explicit).expanduser())

    config_dir = os.environ.get("CONFIG_DIR", "")
    if config_dir:
        candidates.append(Path(config_dir).expanduser() / CONFIG_FILENAME)

    candidates.append(Path(DEFAULT_CONFIG_DIR) / CONFIG_FILENAME)
    candidates.append(_project_root() / "config" / CONFIG_FILENAME)

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_config_file(path: str | Path | None = None) -> dict[str, Any]:
    """读取并扁平化配置文件。读不到 / 解析失败都只告警，绝不阻断启动。"""
    target = Path(path) if path else resolve_config_file()
    if target is None or not target.is_file():
        return {}
    try:
        payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.error("配置文件解析失败，回退环境变量与默认值：%s（%s）", target, exc)
        return {}
    if not isinstance(payload, Mapping):
        logger.error("配置文件顶层必须是映射（key: value），实际是 %s", type(payload).__name__)
        return {}
    return flatten(payload)


def flatten(payload: Mapping[str, Any]) -> dict[str, Any]:
    """把分区的 YAML 拍平成 Settings 字段名。

    ``llm: {base_url: x}`` → ``llm_base_url: x``；
    ``runtime: {mock_mode: false}`` → ``mock_mode: false``；
    顶层标量（``mock_mode: false``）原样保留。
    """
    flat: dict[str, Any] = {}
    for section, body in payload.items():
        if isinstance(body, Mapping):
            for key, value in body.items():
                flat[_SECTION_ALIASES.get((str(section), str(key)), str(key))] = value
        else:
            flat[str(section)] = body
    return flat


class YamlConfigSource(PydanticBaseSettingsSource):
    """pydantic-settings 自定义源：把扁平化后的 YAML 作为一层配置。"""

    def __init__(self, settings_cls: type, data: Mapping[str, Any] | None = None) -> None:
        super().__init__(settings_cls)
        known = getattr(settings_cls, "model_fields", {})
        unknown = sorted(k for k in (data or {}) if k not in known)
        if unknown:
            # 不静默丢弃：字段名写错时用户最难排查
            logger.warning(
                "配置文件 %s 中存在无法识别的字段（已忽略）：%s",
                resolve_config_file(),
                ", ".join(unknown),
            )
        self._data: dict[str, Any] = dict(data or {})

    def get_field_value(self, field, field_name: str):  # noqa: ANN001 - 基类签名
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._data

    def __repr__(self) -> str:
        return f"YamlConfigSource(path={resolve_config_file()}, keys={len(self._data)})"
