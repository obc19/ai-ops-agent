"""测试夹具：隔离数据目录，强制 mock 模式。"""

from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="aiops-test-")
os.environ["DATA_DIR"] = _TMP
os.environ["MOCK_MODE"] = "true"
os.environ["AUTH_TOKEN"] = ""
os.environ["DETECT_INTERVAL"] = "3600"
os.environ["ALERT_CHANNELS"] = "feishu,dingtalk,email,webhook"

import pytest  # noqa: E402

from app.config.settings import get_settings  # noqa: E402


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture(autouse=True)
def _clear_mock_faults():
    from app.collectors.k8s_adapter import K8sCollector
    from app.mock import world

    world.clear_faults()
    K8sCollector.clear_cache()
    yield
    world.clear_faults()
    K8sCollector.clear_cache()
