"""常量：项目路径、分级映射、超时预算等。"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

API_PREFIX = "/api/v1"

# 告警等级由高到低
SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}

# 不参与抑制的等级
SEVERITY_NO_SUPPRESS = {"P0"}

DEFAULT_CHANNELS = ["feishu", "dingtalk", "email", "webhook"]

# 展示时区
DISPLAY_TZ = "Asia/Shanghai"

# 各来源默认严重级别（异常事件未显式指定时使用）
SOURCE_DEFAULT_SEVERITY = {
    "metric": "P1",
    "log": "P2",
    "trace": "P2",
    "k8s": "P1",
}
