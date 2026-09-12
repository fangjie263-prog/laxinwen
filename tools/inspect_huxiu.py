"""Generate evidence-backed Huxiu recon and A/B/C benchmark reports.

Usage: ``python tools/inspect_huxiu.py`` from the repository root after the
real pages have been captured under ``huxiu-recon``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import trafilatura
from selectolax.parser import HTMLParser

from news.extract import extract_article
from news.sources.huxiu import extract_huxiu_article, parse_channel_page

ROOT = Path(__file__).resolve().parents[1]
RECON = ROOT / "huxiu-recon"
SAMPLE_URLS = {
    "001": "https://www.huxiu.com/article/4890703.html",
    "002": "https://www.huxiu.com/article/4890705.html",
    "003": "https://www.huxiu.com/article/4890702.html",
    "004": "https://www.huxiu.com/article/4890701.html",
    "005": "https://www.huxiu.com/article/4890711.html",
    "006": "https://www.huxiu.com/article/4890693.html",
    "007": "https://www.huxiu.com/article/4890638.html",
    "008": "https://www.huxiu.com/article/4890510.html",
}


def _stats(html: str) -> dict[str, int | bool]:
    tree = HTMLParser(html)
    return {
        "p": len(tree.css("p")),
        "div": len(tree.css("div")),
        "h2": len(tree.css("h2")),
        "h3": len(tree.css("h3")),
        "blockquote": len(tree.css("blockquote")),
        "ul": len(tree.css("ul")),
        "ol": len(tree.css("ol")),
        "figure": len(tree.css("figure")),
        "img": len(tree.css("img")),
        "figcaption": len(tree.css("figcaption")),
        "video": len(tree.css("video")),
        "iframe": len(tree.css("iframe")),
        "has_json_ld": bool(tree.css("script[type='application/ld+json']")),
        "has_nuxt_data": bool(tree.css("#__NUXT_DATA__")),
    }


def main() -> None:
    channel_html = (RECON / "article-channel.html").read_text(encoding="utf-8")
    items = parse_channel_page(channel_html)
    article_files = sorted((RECON / "articles").glob("*.html"))
    sample_rows = []
    benchmark_rows = []
    for path in article_files:
        html = path.read_text(encoding="utf-8")
        url = SAMPLE_URLS.get(path.stem, f"https://www.huxiu.com/article/{path.stem}.html")
        parsed = extract_huxiu_article(html, url=url)
        existing = extract_article(html, url=url)
        tra_text = trafilatura.extract(html, url=url, include_comments=False, include_tables=False, favor_recall=True) or ""
        tree = HTMLParser(html)
        pollution_terms = {
            "related_articles": bool(tree.css(".article__related-article-wrap")),
            "popular_articles": "热门文章" in parsed.body_text,
            "advertising": any(x in parsed.body_text for x in ("广告", "下载虎嗅APP")),
            "footer": any(x in parsed.body_text for x in ("Copyright", "版权所有")),
            "navigation": any(x in parsed.body_text for x in ("首页", "登录注册")),
            "comments": bool(tree.css(".article__comment")) and "评论" in parsed.body_text,
            "app_download": "虎嗅APP" in parsed.body_text,
        }
        sample_rows.append({
            "id": path.stem,
            "url": url,
            "title": parsed.title,
            "discovered_from": "https://www.huxiu.com/article/",
            "article_type": "original" if "原创" in html or "is_original\\\":true" in html else "unknown",
            "author": parsed.authors,
            "published_at": parsed.published_at.isoformat() if parsed.published_at else None,
            "section": parsed.section,
            "body_length": len(parsed.body_text),
            "paragraph_count": len([b for b in parsed.blocks if b["type"] == "paragraph"]),
            "image_count": len(parsed.images),
            "body_dom": _stats(html),
            "pollution_checks": pollution_terms,
            "notes": "真实页面，下载于 2026-09-12；类型/转载以页面字段为准，未猜测。",
        })
        benchmark_rows.append({
            "id": path.stem,
            "huxiu_parser": {"title": bool(parsed.title), "author": bool(parsed.authors), "published_at": bool(parsed.published_at), "body_length": len(parsed.body_text), "paragraph_count": len([b for b in parsed.blocks if b["type"] == "paragraph"]), "images": len(parsed.images), "captions": len([b for b in parsed.blocks if b["type"] == "caption"])},
            "trafilatura": {"title": bool(getattr(trafilatura.extract_metadata(html, default_url=url), "title", None)), "body_length": len(tra_text), "paragraph_count": len([x for x in tra_text.splitlines() if x.strip()])},
            "existing_extractor": {"title": bool(existing.title), "author": bool(existing.authors), "published_at": bool(existing.published_at), "body_length": len(existing.text), "images": len(existing.images)},
        })

    recon = {
        "captured_at": "2026-09-12",
        "access_constraints": {"desktop": {"status": 200}, "mobile": {"status": 200}, "article_channel": {"status": 200}, "article_samples": {"status": 200}},
        "discovery_candidates": [
            {"url": "https://www.huxiu.com/article/", "method": "SSR HTML cards", "evidence": ".article-item-wrap anchors and #__NUXT_DATA__ in saved HTML", "recommended": True},
            {"url": "https://www.huxiu.com/", "method": "SSR HTML plus embedded Nuxt state", "evidence": "article URLs and Nuxt state in saved desktop home", "recommended": False},
            {"url": "https://m.huxiu.com/", "method": "SSR HTML plus embedded Nuxt state", "evidence": "article URLs in saved mobile home", "recommended": False},
        ],
        "recommended_method": "GET /article/ and parse .article-item-wrap cards; use canonical article URL as the dedup key",
        "article_url_pattern": r"^https?://(?:www\\.|m\\.)?huxiu\\.com/article/\\d+\\.html$",
        "pagination": {"observed": False, "notes": "The captured channel page had 12 SSR cards and no visible page=2/load-more control; long-term pagination was not verified."},
        "embedded_json": ["#__NUXT_DATA__", "window.__NUXT__.config"],
        "api_candidates": ["API_MS_ARTICLE and API_ARTICLE hostnames are exposed in Nuxt config, but no endpoint path was inferred or called."],
        "notes": ["Desktop and mobile home were byte-different but both exposed article URLs.", "No CAPTCHA/login wall appeared in the captured pages.", "Only observed facts are recorded; API use remains unverified."],
    }
    (RECON / "discovery-analysis.json").write_text(json.dumps(recon, ensure_ascii=False, indent=2), encoding="utf-8")
    (RECON / "articles.json").write_text(json.dumps(sample_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (RECON / "benchmark.json").write_text(json.dumps(benchmark_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"discovered": len(items), "samples": len(sample_rows), "written": ["discovery-analysis.json", "articles.json", "benchmark.json"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
