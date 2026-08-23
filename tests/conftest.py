"""pytest 全局 fixture。

提供 autouse fixture：在 RFI 相关测试文件中将 ``RFI_DISCOVERY_MAX_AGE_DAYS``
设为较大值，使 RFI 栏目页发现不受测试数据固定日期（如 20260815）的 7 天窗口影响。

实际生产环境中 ``RFI_DISCOVERY_MAX_AGE_DAYS = 7`` 保持默认。
"""

import pytest


@pytest.fixture(autouse=True)
def _wide_rfi_time_window(monkeypatch):
    """扩大 RFI 时间窗口（仅 RFI 相关测试文件生效）。"""
    import news.sources.rfi as rfi_mod

    monkeypatch.setattr(rfi_mod, "RFI_DISCOVERY_MAX_AGE_DAYS", 365)
