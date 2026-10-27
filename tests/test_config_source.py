"""配置文件（YAML）与挂载支持：扁平化、优先级、规则路径解析。"""

from __future__ import annotations

import logging

import yaml

from app.config.settings import Settings
from app.config.yaml_source import flatten, load_config_file, resolve_config_file


def test_flatten_maps_sections_to_fields():
    flat = flatten(
        {
            "runtime": {"mock_mode": False, "port": 9090},
            "llm": {"base_url": "http://x/v1", "api_key": "sk-1"},
            "knowledge": {"top_k": 5, "min_score": 0.3},
            "rules": {"detection": "my-rules.yaml"},
            "alerting": {"feishu_webhook": "http://hook"},
            "version": "9.9.9",  # 顶层标量原样保留
        }
    )
    assert flat["mock_mode"] is False
    assert flat["port"] == 9090
    assert flat["llm_base_url"] == "http://x/v1"
    assert flat["llm_api_key"] == "sk-1"
    assert flat["knowledge_top_k"] == 5
    assert flat["rag_min_score"] == 0.3
    assert flat["detection_rules"] == "my-rules.yaml"
    assert flat["feishu_webhook"] == "http://hook"
    assert flat["version"] == "9.9.9"


def test_mounted_config_dir_takes_effect(tmp_path, monkeypatch):
    (tmp_path / "aiops.yaml").write_text(
        yaml.safe_dump(
            {
                "runtime": {"port": 18080, "log_level": "DEBUG"},
                "llm": {"model": "mounted-model", "base_url": "http://mounted/v1"},
                "agent": {"timeout": 7},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))

    s = Settings()
    assert s.config_root == tmp_path
    assert s.port == 18080
    assert s.log_level == "DEBUG"
    assert s.llm_model == "mounted-model"
    assert s.agent_timeout == 7
    # conftest 里 MOCK_MODE=true 是环境变量，按优先级应当盖掉配置文件里的同名项
    assert s.mock_mode is True


def test_environment_overrides_config_file(tmp_path, monkeypatch):
    (tmp_path / "aiops.yaml").write_text(
        yaml.safe_dump({"llm": {"model": "from-file"}}), encoding="utf-8"
    )
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_MODEL", "from-env")

    assert Settings().llm_model == "from-env"


def test_rules_fall_back_to_bundled_copy(tmp_path, monkeypatch):
    """只挂主配置、没挂规则文件时，仍能用镜像内置的规则。"""
    (tmp_path / "aiops.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))

    s = Settings()
    assert s.detection_rules_path.is_file()
    assert s.detection_rules_path.parent.name == "config"
    assert s.detection_rules_path.name == "rules-detection.yaml"
    assert s.desensitize_rules_path.is_file()


def test_rules_follow_mounted_dir(tmp_path, monkeypatch):
    (tmp_path / "aiops.yaml").write_text("{}\n", encoding="utf-8")
    (tmp_path / "rules-detection.yaml").write_text("thresholds: []\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))

    assert Settings().detection_rules_path == tmp_path / "rules-detection.yaml"


def test_unknown_keys_are_warned(tmp_path, monkeypatch, caplog):
    (tmp_path / "aiops.yaml").write_text(
        yaml.safe_dump({"llm": {"modle": "typo"}}), encoding="utf-8"
    )
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))

    with caplog.at_level(logging.WARNING):
        Settings()
    assert any("无法识别" in record.getMessage() for record in caplog.records)


def test_resolve_config_file_prefers_explicit(tmp_path, monkeypatch):
    explicit = tmp_path / "custom.yaml"
    explicit.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_FILE", str(explicit))

    assert resolve_config_file() == explicit
    assert load_config_file() == {}


def test_missing_config_file_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_FILE", str(tmp_path / "nope.yaml"))
    monkeypatch.delenv("CONFIG_DIR", raising=False)

    assert resolve_config_file() is None or resolve_config_file().name == "aiops.yaml"
    s = Settings()  # 读不到配置也要正常启动，走默认值
    assert s.app_name == "ai-ops-agent"
