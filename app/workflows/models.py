"""取证工作流的声明式定义。

设计参考 keephq/keep 的 workflow（triggers → steps → actions），但**砍掉了 actions**：
这个平台只读、不执行修复，所以工作流唯一的产出是「证据」，用来喂给诊断和 LLM。
少一类能力，换来「不可能因为工作流配置错误而改坏生产」。
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Trigger(BaseModel):
    """触发条件：字段之间是 AND，字段内部的列表是 OR。留空字段不参与判断。"""

    # 匹配 anomaly.metric 或 rule_id，支持通配符：k8s-* / host_* / probe_ok
    rules: list[str] = Field(default_factory=list)
    severity: list[str] = Field(default_factory=list)
    source: list[str] = Field(default_factory=list)  # metric / log / trace / k8s
    target_regex: str = ""

    def is_empty(self) -> bool:
        return not (self.rules or self.severity or self.source or self.target_regex)

    def matches(self, anomaly) -> bool:
        if self.rules:
            names = {anomaly.metric or "", anomaly.rule_id or ""}
            if not any(
                any(fnmatch(name, pattern) for pattern in self.rules) for name in names if name
            ):
                return False
        if self.severity:
            if anomaly.severity.value not in self.severity:
                return False
        if self.source:
            if anomaly.source.value not in self.source:
                return False
        if self.target_regex:
            try:
                if not re.search(self.target_regex, anomaly.target or ""):
                    return False
            except re.error:
                return False
        return True


class Step(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    provider: str
    # YAML 里写 with:（和 Keep 一致），Python 侧用 params
    params: dict[str, Any] = Field(default_factory=dict, alias="with")
    continue_on_error: bool = True

    @property
    def params_safe(self) -> dict[str, Any]:
        return self.params or {}


class Workflow(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    enabled: bool = True
    triggers: list[Trigger] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)

    def matched_by(self, anomaly) -> bool:
        if not self.enabled:
            return False
        # 没有任何触发条件的 workflow 不参与匹配：宁可不动，也别让它对所有告警乱取证
        return any(t.matches(anomaly) for t in self.triggers if not t.is_empty())
