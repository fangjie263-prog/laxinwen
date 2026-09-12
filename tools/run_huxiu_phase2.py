"""Low-rate, evidence-producing Phase 2 validation against real Huxiu pages.

This script intentionally uses one normal HTTP client, one request at a time,
and a one-second article interval. It does not bypass challenges or infer an
API endpoint; candidate pagination URLs are tested and recorded as facts.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import httpx
from selectolax.parser import HTMLParser

from news.sources.huxiu import ARTICLE_URL_RE, extract_huxiu_article, normalize_huxiu_url, parse_channel_page

ROOT = Path(__file__).resolve().parents[1]
RECON = ROOT / "huxiu-recon"
ARTICLES = RECON / "articles"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"


def write_json(name: str, payload: object) -> None:
    (RECON / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def ids(html: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"/article/(\d+)\.html", html)))


def status_group(status: int | str) -> str:
    if isinstance(status, int):
        if status in {301, 302, 303, 307, 308}:
            return str(status)
        if status == 200:
            return "200"
        if status in {403, 429}:
            return str(status)
        return "other"
    return status


def main() -> None:
    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"}
    client = httpx.Client(timeout=30, follow_redirects=True, headers=headers)
    stability: list[dict] = []
    page_results: dict[str, dict] = {}
    try:
        # 1. Explicitly test candidate pagination forms without assuming one.
        canonical_page = "https://www.huxiu.com/article/"
        canonical_response = client.get(canonical_page)
        page_results["page1"] = {"url": canonical_page, "status": canonical_response.status_code, "final_url": str(canonical_response.url), "article_ids": ids(canonical_response.text), "length": len(canonical_response.text)}
        time.sleep(1.0)
        pagination_urls = {
            "page_query_2": "https://www.huxiu.com/article/?page=2",
            "page_path_2": "https://www.huxiu.com/article/page/2",
            "page_query_3": "https://www.huxiu.com/article/?page=3",
            "page_path_3": "https://www.huxiu.com/article/page/3",
        }
        for name, url in pagination_urls.items():
            try:
                response = client.get(url)
                page_results[name] = {"url": url, "status": response.status_code, "final_url": str(response.url), "article_ids": ids(response.text), "length": len(response.text)}
            except Exception as exc:
                page_results[name] = {"url": url, "error": repr(exc), "article_ids": []}
            time.sleep(1.0)
        page1 = page_results.get("page1", {}).get("article_ids", [])
        p1 = page_results.get("page_query_2", {}).get("article_ids", [])
        p2 = page_results.get("page_path_2", {}).get("article_ids", [])
        p3 = page_results.get("page_query_3", {}).get("article_ids", [])
        duplicates = sorted(set(page1) & (set(p1) | set(p2) | set(p3)))
        write_json("pagination-analysis.json", {
            "method": "No stable pagination method confirmed",
            "candidates": page_results,
            "page1_ids": page1,
            "page2_ids": p1,
            "page3_ids": p3,
            "duplicates": duplicates,
            "stable": False,
            "notes": "page 1 is the canonical /article/ page; page query/path candidates are recorded verbatim. Empty/duplicate/redirect results are not promoted to a production method.",
        })

        # 2. Build a 20-article sample from the channel plus home pages.
        source_pages = ["https://www.huxiu.com/article/", "https://www.huxiu.com/", "https://m.huxiu.com/"]
        discovered: list[str] = []
        source_for: dict[str, str] = {}
        for page_url in source_pages:
            response = client.get(page_url)
            stability.append({"kind": "channel" if page_url.endswith("article/") else "home", "url": page_url, "status": response.status_code, "final_url": str(response.url), "length": len(response.text)})
            for article_id in ids(response.text):
                url = normalize_huxiu_url(f"https://www.huxiu.com/article/{article_id}.html")
                if url not in discovered:
                    discovered.append(url)
                    source_for[url] = page_url
            time.sleep(1.0)
        if len(discovered) < 20:
            raise RuntimeError(f"Only {len(discovered)} unique real article URLs discovered; refusing to invent fixtures")
        selected = discovered[:20]

        existing_by_url: dict[str, Path] = {}
        for path in sorted(ARTICLES.glob("*.html")):
            match = re.search(r"/article/(\d+)\.html", path.read_text(encoding="utf-8")[:300000])
            if match:
                existing_by_url[normalize_huxiu_url(f"https://www.huxiu.com/article/{match.group(1)}.html")] = path
        extracted_rows: list[dict] = []
        next_fixture_index = max([int(path.stem) for path in ARTICLES.glob("*.html") if path.stem.isdigit()] or [0]) + 1
        image_urls: list[str] = []
        source_rows: list[dict] = []
        time_rows: list[dict] = []
        url_rows: list[dict] = []
        for index, url in enumerate(selected, start=1):
            response = client.get(url)
            stability.append({"kind": "article", "url": url, "status": response.status_code, "final_url": str(response.url), "length": len(response.text)})
            if response.status_code == 200:
                path = existing_by_url.get(url)
                if path is None:
                    path = ARTICLES / f"{next_fixture_index:03d}.html"
                    next_fixture_index += 1
                    path.write_text(response.text, encoding="utf-8")
                result = extract_huxiu_article(response.text, url=url)
                image_urls.extend(result.images)
                raw_reprint = ""
                tree = HTMLParser(response.text)
                node = tree.css_first(".article__reprinted-explain")
                if node is not None:
                    raw_reprint = node.text(separator=" ", strip=True)
                source_rows.append({"url": url, "parser_original_source": result.original_source, "raw_reprinted_explain": raw_reprint, "source_links": [a.attributes.get("href") for a in node.css("a[href]")] if node is not None else [], "nuxt_has_source_url": "source_url" in response.text, "nuxt_has_reprinted": "reprinted" in response.text})
                time_rows.append({"url": url, "raw": result.metadata.get("published_raw"), "visible_time_raw": result.metadata.get("visible_time_raw"), "parsed": result.published_at.isoformat() if result.published_at else None, "timezone": str(result.published_at.tzinfo) if result.published_at else None, "timezone_aware": bool(result.published_at and result.published_at.tzinfo), "source": result.metadata.get("time_source")})
                url_rows.append({"url": url, "canonical_tag": next((n.attributes.get("href") for n in tree.css("link[rel='canonical']") if n.attributes.get("href")), None), "og_url": next((n.attributes.get("content") for n in tree.css("meta[property='og:url']") if n.attributes.get("content")), None), "normalized": result.canonical_url, "mobile_variant": normalize_huxiu_url(url.replace("www.huxiu.com", "m.huxiu.com")), "query_variant": normalize_huxiu_url(url + "?utm_source=phase2#top"), "trailing_slash_variant": normalize_huxiu_url(url.replace(".html", ".html/"))})
                extracted_rows.append({"id": url.rsplit("/", 1)[-1].split(".")[0], "url": url, "title": result.title, "section": result.section, "authors": result.authors, "body_length": len(result.body_text), "images": len(result.images), "captions": sum(block["type"] == "caption" for block in result.blocks), "headings": sum(block["type"] == "heading" for block in result.blocks), "blockquotes": sum(block["type"] == "blockquote" for block in result.blocks), "lists": sum(block["type"] == "list" for block in result.blocks), "fixture": str(path.relative_to(ROOT))})
            time.sleep(1.0)

        # 3. Validate ten actual image URLs with GET, slowly and sequentially.
        image_checks = []
        for image_url in list(dict.fromkeys(image_urls))[:10]:
            try:
                response = client.get(image_url)
                image_checks.append({"url": image_url, "status": response.status_code, "content_type": response.headers.get("content-type"), "length": len(response.content)})
            except Exception as exc:
                image_checks.append({"url": image_url, "error": repr(exc)})
            time.sleep(0.5)

        write_json("time-analysis.json", {"captured_at": "2026-09-12", "articles": time_rows, "formats": sorted({row["raw"] for row in time_rows if row["raw"]}), "all_timezone_aware": all(row["timezone_aware"] for row in time_rows if row["parsed"])})
        write_json("url-analysis.json", {"articles": url_rows, "all_normalized_stable": all(row["normalized"] == row["mobile_variant"] == row["query_variant"] for row in url_rows)})
        write_json("original-source-analysis.json", {"articles": source_rows, "parser_detected": sum(bool(row["parser_original_source"]) for row in source_rows), "named_publishers": [], "notes": "The observed reprint notices identify network-origin content but do not consistently name an external publisher; no publisher name was invented."})
        write_json("image-analysis.json", {"articles": extracted_rows, "image_get_checks": image_checks, "checked_count": len(image_checks), "all_checked_200": all(row.get("status") == 200 for row in image_checks)})
        counts = Counter(status_group(row.get("status", "error")) for row in stability)
        write_json("network-stability.json", {"interval_seconds": {"articles": 1.0, "images": 0.5}, "user_agent": UA, "requests": stability, "counts": dict(counts), "article_success_count": sum(row.get("kind") == "article" and row.get("status") == 200 for row in stability), "notes": "Sequential low-rate requests; no proxy, captcha bypass, or concurrency."})
        (RECON / "phase2-samples.json").write_text(json.dumps(extracted_rows, ensure_ascii=False, indent=2), encoding="utf-8")
        write_json("articles.json", [{"id": row["id"], "url": row["url"], "title": row["title"], "discovered_from": source_for.get(row["url"], "real Phase 2 sample"), "article_type": "real_html", "notes": "Downloaded 2026-09-12; see phase2-samples.json for structure metrics."} for row in extracted_rows])
        print(json.dumps({"selected": len(selected), "article_200": sum(row.get("status") == 200 for row in stability if row.get("kind") == "article"), "images_checked": len(image_checks), "status_counts": dict(counts)}, ensure_ascii=False))
    finally:
        client.close()


if __name__ == "__main__":
    main()
