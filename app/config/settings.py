"""全局配置：配置文件（YAML）+ 环境变量双通道，改行为无需改代码或重建镜像。

优先级：构造参数 > 环境变量 > .env > YAML 配置文件 > 代码默认值。
YAML 是主力（可整目录挂载），环境变量用于临时覆盖单项。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config.yaml_source import (
    DEFAULT_CONFIG_DIR,
    YamlConfigSource,
    load_config_file,
    resolve_config_file,
)


class Settings(BaseSettings):
    """所有配置项均可由同名大写环境变量覆盖（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 配置文件本身 ----
    # 留空则按 CONFIG_FILE → CONFIG_DIR/aiops.yaml → /etc/aiops/aiops.yaml → <项目>/config/aiops.yaml 探测
    config_dir: str = ""        # 配置目录（挂载点），规则文件相对它解析
    config_file: str = ""       # 主配置文件路径，指定后优先于 config_dir

    # ---- 运行模式 ----
    app_name: str = "ai-ops-agent"
    version: str = "0.6.0"
    mock_mode: bool = True
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"
    data_dir: Path = Path("./data")

    # ---- 外部数据源 ----
    prometheus_url: str = ""
    elasticsearch_url: str = ""
    elasticsearch_index: str = "app-logs-*"
    elasticsearch_username: str = ""
    elasticsearch_password: str = ""
    jaeger_url: str = ""
    kubernetes_api_url: str = ""
    kubernetes_token: str = ""

    # ---- 集群内部署：SA 挂载目录留空则自动探测 ----
    # 容器跑在 K8s 里时无需配置地址/Token，直接读 ServiceAccount 挂载
    kubernetes_ca_file: str = ""
    kubernetes_token_file: str = ""
    # 只采这些命名空间（逗号分隔）；留空 = 全部命名空间
    kubernetes_namespaces: str = ""
    # 数据源为自签证书时置 false；仅在可信内网使用
    tls_verify: bool = True

    # ---- 多集群（留空则单集群，使用上面这份数据源地址）----
    clusters_config: str = ""       # 文件路径或内联 YAML
    default_cluster: str = "prod-cluster"
    default_namespace: str = "prod"

    # ---- 规则文件（留空则在配置目录里按默认文件名探测）----
    detection_rules: str = ""       # 默认 rules-detection.yaml
    desensitize_rules: str = ""     # 默认 rules-desensitize.yaml

    # ---- 可选模块 ----
    enable_trace_agent: bool = False
    enable_k8s_adapter: bool = False

    # ---- 服务器本体状态（本地直读 /proc，零外部依赖）----
    server_enabled: bool = True
    # 容器内看宿主机：把宿主机 /proc 挂到 /host/proc，再把这里改成 /host/proc
    server_proc_root: str = "/proc"
    # 磁盘使用率要看的路径，逗号分隔；容器内指容器自身，要看宿主机需挂载宿主目录
    server_disk_paths: str = "/"
    # 两次 CPU 采样的间隔（毫秒），用于算瞬时使用率
    server_sample_interval_ms: int = Field(default=300, ge=50, le=5000)

    # ---- 主动拨测（platform 自己去访问目标，回答「用户访问慢在哪一段」）----
    probe_enabled: bool = True
    probe_config: str = ""          # 留空则自动找 <配置目录>/probes.yaml
    probe_timeout: float = Field(default=5.0, gt=0)

    # ---- 服务拓扑（根因收敛的唯一依据：谁调谁）----
    topology_enabled: bool = True
    topology_refresh_seconds: int = Field(default=300, ge=0)
    # trace 推导不全时的兜底：手工声明 {caller: [callee, ...]}
    topology_dependencies: dict[str, Any] = Field(default_factory=dict)
    # 根因被 N 个下游连带时，把等级提升到群内最严重
    topology_escalate_by_impact: bool = True

    # ---- LLM ----
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_api_key: str = ""
    llm_timeout: float = 20.0

    # ---- Embedding ----
    embedding_base_url: str = ""
    embedding_model: str = ""
    embedding_api_key: str = ""
    embedding_dim: int = Field(default=8, ge=1)

    # ---- 知识库 ----
    knowledge_backend: Literal["simple", "chroma", "faiss"] = "simple"
    knowledge_dedup_threshold: float = Field(default=0.92, ge=0.0, le=1.0)
    knowledge_top_k: int = Field(default=3, ge=1)
    # 检索最低相似度：哈希兜底向量分值偏低，可接真实 embedding 后调高
    rag_min_score: float = Field(default=0.15, ge=0.0, le=1.0)

    # ---- 检测与告警 ----
    detect_interval: int = Field(default=10, ge=1)
    retention_days: int = Field(default=30, ge=1)
    alert_group_window: int = Field(default=300, ge=0)
    alert_cooldown: int = Field(default=300, ge=0)

    # ---- 鉴权（静态 Token + 三角色；三者都不配则本地体验模式全放行）----
    auth_token: str = ""          # admin
    operator_token: str = ""      # operator：可执行确认/关闭/触发诊断
    viewer_token: str = ""        # viewer：只读

    # ---- 告警渠道 ----
    alert_channels: str = "feishu,dingtalk,email,webhook"
    feishu_webhook: str = ""
    feishu_secret: str = ""      # 飞书机器人「加签」密钥（可选）
    dingtalk_webhook: str = ""
    dingtalk_secret: str = ""    # 钉钉机器人「加签」密钥（可选）
    alert_webhook_url: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    alert_email_from: str = ""
    alert_email_to: str = ""
    # mock 模式下把通知打到内置接收端（默认跟随运行端口），用于验收断言
    mock_sink_base: str = ""

    # ---- Agent 超时预算（保证端到端 ≤60s）----
    agent_timeout: float = Field(default=12.0, gt=0)
    diagnosis_budget: float = Field(default=45.0, gt=0)

    @field_validator("data_dir", mode="after")
    @classmethod
    def _ensure_data_dir(cls, value: Path) -> Path:
        value = value.expanduser()
        value.mkdir(parents=True, exist_ok=True)
        return value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """把 YAML 配置文件插到环境变量之后、默认值之前。"""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSource(settings_cls, load_config_file()),
            file_secret_settings,
        )

    # ---- 派生属性 ----
    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_token or self.operator_token or self.viewer_token)

    @property
    def config_root(self) -> Path:
        """配置目录：显式 CONFIG_DIR > 挂载约定目录 /etc/aiops > 配置文件所在目录 > 内置 config/。

        即使挂载目录里只放了规则文件、没放 aiops.yaml，规则也优先从挂载目录读。
        """
        if self.config_dir:
            return Path(self.config_dir).expanduser()
        mounted = Path(DEFAULT_CONFIG_DIR)
        if mounted.is_dir():
            return mounted
        found = resolve_config_file(self.config_file)
        if found is not None:
            return found.parent
        from app.config.constants import PROJECT_ROOT

        return PROJECT_ROOT / "config"

    def _resolve_rules(self, explicit: str, filename: str) -> Path:
        """规则文件解析：显式路径 → 挂载目录 → 镜像内置目录。"""
        candidates: list[Path] = []
        if explicit:
            raw = Path(explicit).expanduser()
            candidates.append(raw if raw.is_absolute() else self.config_root / raw)
        candidates.append(self.config_root / filename)
        from app.config.constants import PROJECT_ROOT

        bundled = PROJECT_ROOT / "config" / filename
        if bundled not in candidates:
            candidates.append(bundled)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return candidates[0]

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key and self.llm_base_url)

    @property
    def llm_provider(self) -> str:
        """只取 base_url 的 host，用于日志与页面展示，不外泄完整地址。"""
        from urllib.parse import urlparse

        return urlparse(self.llm_base_url).netloc or self.llm_base_url

    @property
    def llm_key_masked(self) -> str:
        """密钥打码：仅保留前 4 位与后 4 位，够用户确认「填的是哪把」。"""
        key = self.llm_api_key
        if not key:
            return ""
        if len(key) <= 10:
            return "*" * len(key)
        return f"{key[:4]}…{key[-4:]}"

    @property
    def embedding_enabled(self) -> bool:
        return bool(self.embedding_base_url and self.embedding_model)

    @property
    def sink_base(self) -> str:
        return self.mock_sink_base or f"http://127.0.0.1:{self.port}/_mock/sink"

    @property
    def desensitize_rules_path(self) -> Path:
        return self._resolve_rules(self.desensitize_rules, "rules-desensitize.yaml")

    @property
    def detection_rules_path(self) -> Path:
        return self._resolve_rules(self.detection_rules, "rules-detection.yaml")

    @property
    def clusters_file(self) -> Path | None:
        """未显式配置 clusters_config 时，自动使用配置目录下的 clusters.yaml（存在才算）。"""
        if self.clusters_config.strip():
            return None
        candidate = self.config_root / "clusters.yaml"
        return candidate if candidate.is_file() else None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "aiops.db"

    @property
    def vector_path(self) -> Path:
        return self.data_dir / "vectors.json"

    @property
    def audit_path(self) -> Path:
        return self.data_dir / "audit.jsonl"


_settings: Settings | None = None


def get_settings() -> Settings:
    """进程内单例，避免重复解析。"""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """测试用：清空单例。"""
    global _settings
    _settings = None
