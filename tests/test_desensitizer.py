"""脱敏引擎测试：命中、二次校验、fail-closed。"""

from __future__ import annotations

import pytest

from app.gateway.desensitizer import Desensitizer, DesensitizerError, get_desensitizer


def test_redact_sensitive_fields():
    d = get_desensitizer()
    text = (
        "连接 10.1.2.3:9200 失败 token=abcd1234efgh "
        "用户 alice@example.com 手机 13812345678 host order-service.prod"
    )
    result = d.redact(text)
    assert result.sensitive is True
    assert "10.1.2.3" not in result.text
    assert "abcd1234efgh" not in result.text
    assert "alice@example.com" not in result.text
    assert "13812345678" not in result.text
    assert "order-service.prod" not in result.text
    assert result.findings


def test_second_pass_detects_nothing():
    d = get_desensitizer()
    result = d.redact("ip 192.168.10.20 token=zzzz9999yyyy")
    assert d.scan(result.text) == []


def test_plain_text_untouched():
    d = get_desensitizer()
    text = "order-service CPU 使用率 94%，触发严重超限"
    result = d.redact(text)
    assert result.sensitive is False
    assert result.text == text


def test_bad_rules_fail_closed(tmp_path):
    bad = tmp_path / "rules.yaml"
    bad.write_text("rules: []", encoding="utf-8")
    with pytest.raises(DesensitizerError):
        Desensitizer(bad)


def test_missing_file_fail_closed(tmp_path):
    with pytest.raises(DesensitizerError):
        Desensitizer(tmp_path / "not-exists.yaml")
