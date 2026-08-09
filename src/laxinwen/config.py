"""Site configuration loading.

Each site is described by a YAML file in ``sites/<id>.yaml`` so that adding a
simple RSS-based site does not require touching any Python code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_SITES_DIR = Path(__file__).resolve().parent.parent.parent / "sites"


@dataclass
class ListConfig:
    url: str
    link_selector: str | None = None
    article_url_pattern: str | None = None
    type: str = "html"  # "rss" | "html"
    max_items: int = 50


@dataclass
class SiteConfig:
    id: str
    name: str
    rss: str | None = None
    rsshub: str | None = None
    lists: list[ListConfig] = field(default_factory=list)
    language: str | None = None
    requires_js: bool = False
    extract: dict = field(default_factory=dict)
    url: str | None = None
    title_strip_suffix: str | None = None
    request_interval: float = 1.0

    @classmethod
    def from_dict(cls, data: dict) -> "SiteConfig":
        lists = [
            ListConfig(
                url=lc.get("url", ""),
                link_selector=lc.get("link_selector"),
                article_url_pattern=lc.get("article_url_pattern"),
                type=lc.get("type", "html"),
                max_items=int(lc.get("max_items", 50)),
            )
            for lc in data.get("lists", []) or []
        ]
        return cls(
            id=data["id"],
            name=data.get("name", data["id"]),
            rss=data.get("rss"),
            rsshub=data.get("rsshub"),
            lists=lists,
            language=data.get("language"),
            requires_js=bool(data.get("requires_js", False)),
            extract=data.get("extract", {}) or {},
            url=data.get("url"),
            title_strip_suffix=data.get("title_strip_suffix"),
            request_interval=float(data.get("request_interval", 1.0)),
        )

    def effective_sources(self) -> list[tuple[str, str]]:
        """Return ordered discovery sources: (kind, url)."""
        sources: list[tuple[str, str]] = []
        if self.rss:
            sources.append(("rss", self.rss))
        if self.rsshub:
            sources.append(("rsshub", self.rsshub))
        for lc in self.lists:
            sources.append(("html", lc.url))
        return sources


def load_site(site_id: str, sites_dir: str | Path | None = None) -> SiteConfig:
    """Load a single site configuration by id."""
    root = Path(sites_dir) if sites_dir else DEFAULT_SITES_DIR
    path = root / f"{site_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"No site config found: {path} (available: {list(root.glob('*.yaml'))})"
        )
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return SiteConfig.from_dict(data)


def list_sites(sites_dir: str | Path | None = None) -> list[str]:
    root = Path(sites_dir) if sites_dir else DEFAULT_SITES_DIR
    return sorted(p.stem for p in root.glob("*.yaml"))
