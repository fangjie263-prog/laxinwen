"""站点配置加载。

每个网站一个 YAML 文件（sites/<id>.yaml），核心代码不硬编码任何站点 URL /
selector / pattern。增加一个简单 RSS 网站只需新增一个 YAML 文件。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

# 默认站点配置目录：优先环境变量 NEWS_SITES_DIR，其次项目根 sites/
_SITES_DIR = Path(
    __import__("os").environ.get("NEWS_SITES_DIR", str(Path(__file__).resolve().parents[2] / "sites"))
)

# 内置兜底目录：包内 sites（当项目外使用时报错前可回退）
_PKG_SITES_DIR = Path(__file__).resolve().parent / "sites"

DEFAULT_CONFIG: dict[str, Any] = {
    "id": "",
    "name": "",
    "rss": None,           # 官方 RSS/Atom URL
    "rsshub": None,        # RSSHub route（如 https://rsshub.app/...）
    "lists": [],           # 栏目页列表
    "article_url_pattern": None,  # 文章 URL 正则（供栏目页链接过滤）
    "load_more": None,    # “加载更多”分页接口配置（如 ECO admin-ajax load-more）
    "sections": [],       # 站点专用栏目 discovery（由 source adapter 解释）
    "allow_summary_as_content": True,  # RSS summary 是否可作为正文候选
    "requires_js": False,  # 是否需要 JS 渲染
    "extract": {},         # Trafilatura 等提取参数
    "language": "",
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """浅合并站点配置到默认值。"""
    merged = dict(base)
    for k, v in override.items():
        merged[k] = v
    return merged


def load_site_config(site_id: str, sites_dir: str | Path | None = None) -> dict[str, Any]:
    """加载单个站点配置。找不到站点文件时抛出 FileNotFoundError。"""
    d = Path(sites_dir) if sites_dir else _SITES_DIR
    candidates = [d / f"{site_id}.yaml", d / f"{site_id}.yml", _PKG_SITES_DIR / f"{site_id}.yaml"]
    for cand in candidates:
        if cand.is_file():
            raw = yaml.safe_load(cand.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raise ValueError(f"站点配置格式错误（应为 mapping）：{cand}")
            cfg = _merge(DEFAULT_CONFIG, raw)
            cfg["_path"] = str(cand)
            return cfg
    raise FileNotFoundError(
        f"未找到站点配置：{site_id}（查找目录：{d}）"
    )


def list_available_sites(sites_dir: str | Path | None = None) -> list[str]:
    """列出所有可用站点 id（按配置文件名）。"""
    d = Path(sites_dir) if sites_dir else _SITES_DIR
    ids: list[str] = []
    for suffix in ("*.yaml", "*.yml"):
        for f in sorted(d.glob(suffix)):
            ids.append(f.stem)
    return ids


def resolve_config_source(cfg: dict[str, Any]) -> str:
    """返回站点配置中“发现机制”的实际来源描述，供日志/状态展示。"""
    if cfg.get("rss"):
        return f"rss:{cfg['rss']}"
    if cfg.get("rsshub"):
        return f"rsshub:{cfg['rsshub']}"
    if cfg.get("lists"):
        return f"list:{cfg['lists'][0].get('url', '')}"
    return "none"
