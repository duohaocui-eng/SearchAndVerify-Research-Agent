import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture(autouse=True)
def _offline_by_default():
    """单元测试必须不依赖真实网络：确定性、可重复、CI 不受外部 API 抽风影响。

    SNV_TEST_OFFLINE 是 evidence_core.normalize / evidence_core.sources.build_registry
    读的开关：设了它，在线归一化与除 fixtures 外的所有真实数据源都会被强制禁用，
    与 configs/sources.yaml 里各源的 enabled 取值无关。
    生产运行（CLI / notebook）不设这个变量，行为不受影响。
    """
    old = os.environ.get("SNV_TEST_OFFLINE")
    os.environ["SNV_TEST_OFFLINE"] = "1"
    yield
    if old is None:
        os.environ.pop("SNV_TEST_OFFLINE", None)
    else:
        os.environ["SNV_TEST_OFFLINE"] = old
