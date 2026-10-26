"""全局配置：全部通过环境变量注入，不改镜像即可调整行为。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """所有配置项均可由同名大写环境变量覆盖（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 运行模式 ----
    app_name: str = "ai-ops-agent"
    version: str = "0.1.0"
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

    # ---- 多集群（留空则单集群，使用上面这份数据源地址）----
    clusters_config: str = ""       # 文件路径或内联 YAML
    default_cluster: str = "prod-cluster"
    default_namespace: str = "prod"

    # ---- 可选模块 ----
    enable_trace_agent: bool = False
    enable_k8s_adapter: bool = False

    # ---- LLM ----
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_api_key: str = ""
    llm_timeout: float = 20.0

    # ---- 本地模型 ----
    local_model_url: str = ""
    local_model_name: str = "qwen2.5:7b"
    local_model_enabled: bool = False

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
    dingtalk_webhook: str = ""
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

    # ---- 派生属性 ----
    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_token or self.operator_token or self.viewer_token)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key and self.llm_base_url)

    @property
    def embedding_enabled(self) -> bool:
        return bool(self.embedding_base_url and self.embedding_model)

    @property
    def sink_base(self) -> str:
        return self.mock_sink_base or f"http://127.0.0.1:{self.port}/_mock/sink"

    @property
    def desensitize_rules_path(self) -> Path:
        from app.config.constants import PROJECT_ROOT

        return PROJECT_ROOT / "app" / "gateway" / "rules.yaml"

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
