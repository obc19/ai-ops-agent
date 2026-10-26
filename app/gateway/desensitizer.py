"""脱敏引擎：正则规则识别 + 打码/替换/截断，附带二次校验。

安全策略：规则加载失败或处理异常一律 fail-closed（调用方据此拦截外发）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.utils.logging import get_logger

logger = get_logger(__name__)


class DesensitizerError(RuntimeError):
    """脱敏引擎自身故障，调用方必须按 fail-closed 处理。"""


@dataclass
class Rule:
    id: str
    name: str
    pattern: re.Pattern[str]
    action: str = "mask"
    replacement: str = ""
    keep_head: int = 2
    keep_tail: int = 2
    severity: str = "high"


@dataclass
class Finding:
    rule_id: str
    rule_name: str
    count: int
    action: str
    severity: str = "high"
    samples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "count": self.count,
            "action": self.action,
            "severity": self.severity,
            "samples": self.samples,
        }


@dataclass
class RedactResult:
    text: str
    findings: list[Finding]

    @property
    def sensitive(self) -> bool:
        return bool(self.findings)

    @property
    def summary(self) -> str:
        if not self.findings:
            return "未发现敏感字段"
        return "、".join(f"{f.rule_name}×{f.count}" for f in self.findings)


def _mask(value: str, head: int, tail: int) -> str:
    if len(value) <= head + tail:
        return "*" * len(value)
    middle = "*" * (len(value) - head - tail)
    return f"{value[:head]}{middle}{value[len(value) - tail:] if tail else ''}"


class Desensitizer:
    def __init__(self, rules_path: Path | None = None) -> None:
        self.rules_path = rules_path
        self._rules: list[Rule] = []
        self.load()

    # ---------- 规则加载 ----------
    def load(self) -> None:
        if self.rules_path is None:
            from app.config.settings import get_settings

            self.rules_path = get_settings().desensitize_rules_path
        try:
            raw = yaml.safe_load(Path(self.rules_path).read_text(encoding="utf-8")) or {}
            rules: list[Rule] = []
            for item in raw.get("rules", []):
                pattern = re.compile(str(item["pattern"]))
                rules.append(
                    Rule(
                        id=str(item["id"]),
                        name=str(item.get("name", item["id"])),
                        pattern=pattern,
                        action=str(item.get("action", "mask")),
                        replacement=str(item.get("replacement", "")),
                        keep_head=int(item.get("keep_head", 2)),
                        keep_tail=int(item.get("keep_tail", 2)),
                        severity=str(item.get("severity", "high")),
                    )
                )
            if not rules:
                raise DesensitizerError("脱敏规则为空")
            self._rules = rules
            logger.info("脱敏规则已加载：%d 条", len(rules))
        except (OSError, KeyError, yaml.YAMLError, re.error) as exc:
            self._rules = []
            raise DesensitizerError(f"脱敏规则加载失败：{exc}") from exc

    @property
    def loaded(self) -> bool:
        return bool(self._rules)

    # ---------- 核心能力 ----------
    def scan(self, text: str) -> list[Finding]:
        if not self.loaded:
            raise DesensitizerError("脱敏规则未加载")
        findings: list[Finding] = []
        for rule in self._rules:
            matches = list(rule.pattern.finditer(text))
            if not matches:
                continue
            samples = []
            for match in matches[:3]:
                value = match.group(0)
                samples.append(_mask(value, 2, 2))
            findings.append(
                Finding(
                    rule_id=rule.id,
                    rule_name=rule.name,
                    count=len(matches),
                    action=rule.action,
                    severity=rule.severity,
                    samples=samples,
                )
            )
        return findings

    def redact(self, text: str) -> RedactResult:
        """脱敏并做二次校验：若仍有命中则视为失败（fail-closed）。"""
        if not text:
            return RedactResult(text="", findings=[])
        findings = self.scan(text)

        redacted = text
        for rule in self._rules:
            redacted = self._apply(rule, redacted)

        leftover = self.scan(redacted)
        if leftover:
            names = "、".join(f.rule_name for f in leftover)
            raise DesensitizerError(f"脱敏后仍检出敏感内容（{names}），已拦截外发")

        return RedactResult(text=redacted, findings=findings)

    def _apply(self, rule: Rule, text: str) -> str:
        if rule.action == "replace":
            if rule.replacement:
                return rule.pattern.sub(rule.replacement, text)
            return rule.pattern.sub(f"<{rule.id.upper()}>", text)
        if rule.action == "truncate":
            return rule.pattern.sub(
                lambda m: m.group(0)[: rule.keep_head] + "…", text
            )
        # 默认 mask
        return rule.pattern.sub(
            lambda m: _mask(m.group(0), rule.keep_head, rule.keep_tail), text
        )


_desensitizer: Desensitizer | None = None


def get_desensitizer() -> Desensitizer:
    global _desensitizer
    if _desensitizer is None:
        _desensitizer = Desensitizer()
    return _desensitizer
