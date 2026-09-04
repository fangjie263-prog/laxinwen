"""Source Adapter 注册表。

``sites/<id>.yaml`` 中 ``adapter: <name>`` 指定使用哪个 adapter；
``discover_for_site`` 通过 :func:`get_adapter` 按名称加载并实例化。

当前内置 adapter：

- ``hkej``：HKEJ 信報財經新聞 —— 复用 ResearchReader 已验证的列表页
  抓取（LINK_RE / 分页）与标题 fallback（h1 → og:title → title）、
  正文（article-content）解析逻辑。
- ``rfi``：RFI（法广中文）—— 官方 RSS → RSSHub 回退 + HTML fallback
  （``.t-content__chapo`` + ``.t-content__body`` 正文提取）。
- ``nytchinese``：纽约时报中文网——官方 RSS + 已验证 HTML parser。
"""

from __future__ import annotations

from typing import Optional

from ..config import load_site_config
from .base import SourceAdapter


def get_adapter(site_cfg: dict) -> Optional[SourceAdapter]:
    """根据站点配置返回对应的 SourceAdapter；未声明 adapter 时返回 None。

    - 站点配置含 ``adapter: hkej`` → 返回 HkejAdapter 实例；
    - 其他站点（ECO 等）没有 ``adapter`` 字段 → 返回 None，
      继续使用 discover.py 内建的通用发现逻辑，完全不受影响。
    """
    name = (site_cfg or {}).get("adapter")
    if not name:
        return None

    source_id = site_cfg.get("id", "")
    source_name = site_cfg.get("name", "") or source_id

    if name == "hkej":
        from .hkej import HkejAdapter

        return HkejAdapter(source_id, source_name)
    if name == "rfi":
        from .rfi import RfiAdapter

        return RfiAdapter(source_id, source_name, site_cfg=site_cfg)
    if name == "nytchinese":
        from .nytchinese import NytChineseAdapter

        rss_url = site_cfg.get("rss")
        if not rss_url:
            raise ValueError("NYT Chinese adapter 需要配置 rss")
        return NytChineseAdapter(source_id, source_name, rss_url=rss_url)
    raise ValueError(
        f"未知的 source adapter: {name!r}（站点 {source_id!r}）。"
        f"可用 adapter：hkej、rfi、nytchinese"
    )


def build_adapter(site_id: str) -> Optional[SourceAdapter]:
    """便捷入口：按站点 id 加载配置并返回 adapter（供 CLI/pipeline 使用）。"""
    cfg = load_site_config(site_id)
    return get_adapter(cfg)
