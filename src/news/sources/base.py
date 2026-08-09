"""Source Adapter 抽象基类。

laxinwen 的新闻发现机制从"单一 discover_for_site 内嵌逻辑"演进为
"站点配置 + 可选 Source Adapter"：

- 大多数简单站点（如 ECO）继续使用 discover.py 内置的通用发现逻辑
  （RSS → RSSHub → 栏目页 → load-more），无需 adapter；
- 需要站点特有解析逻辑的站点（如 HKEJ）通过 ``sites/<id>.yaml`` 中的
  ``adapter: <name>`` 声明，由 ``discover_for_site`` 按名称调度到
  对应的 adapter 实现。

Adapter 只负责"发现新闻 URL + 提取标题/时间/作者"，输出统一
``DiscoveredItem``，之后继续进入 laxinwen 现有 pipeline（去重 → 下载 →
正文提取 → SQLite）。这样 HKEJ 的成熟解析逻辑被封装在独立模块内，
不会污染通用 discover 逻辑，也不需要在 discover.py 里堆站点 if/else。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..discover import DiscoveredItem
from ..fetch import BaseFetcher


class SourceAdapter(ABC):
    """新闻源适配器：把站点特有的发现逻辑包装为统一接口。

    ``source_id`` 为站点配置的 ``id``（如 ``hkej``），
    ``source_name`` 为展示名（如 ``HKEJ 信報財經新聞``）。
    """

    def __init__(self, source_id: str, source_name: str) -> None:
        self.source_id = source_id
        self.source_name = source_name

    @abstractmethod
    def discover(self, *, fetcher: BaseFetcher, max_items: int) -> list[DiscoveredItem]:
        """发现新闻条目（未下载正文）。

        ``max_items`` 为本次发现窗口上限（对应 ``--limit`` 语义：
        最多返回最近 ``max_items`` 篇候选文章，供后续去重/下载）。
        """

    # ---------- 可选钩子 ----------

    def fetch_custom_headers(self) -> Optional[dict[str, str]]:
        """返回站点特有请求头（HKEJ 等对 UA/Referer 敏感）。

        若返回 dict，pipeline 创建 fetcher 时会合并进默认请求头；
        返回 None 表示使用默认请求头。
        """
        return None
