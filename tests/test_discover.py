"""Tests for RSS parsing and HTML list discovery (offline fixtures)."""

import feedparser
import pytest
from selectolax.parser import HTMLParser

from laxinwen.config import ListConfig, SiteConfig
from laxinwen.discover import Discoverer
from laxinwen.fetch import Fetcher

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel>
  <title>ECO</title>
  <link>https://eco.sapo.pt</link>
  <description>Economia Online</description>
  <item>
    <title>Primeira notícia</title>
    <link>https://eco.sapo.pt/2026/08/08/primeira/</link>
    <dc:creator><![CDATA[Lusa]]></dc:creator>
    <pubDate>Sat, 08 Aug 2026 22:23:34 +0000</pubDate>
  </item>
  <item>
    <title>Segunda notícia</title>
    <link>https://eco.sapo.pt/2026/08/08/segunda/?utm_source=rss</link>
    <dc:creator><![CDATA[Redação]]></dc:creator>
    <pubDate>Sat, 08 Aug 2026 21:00:00 +0000</pubDate>
  </item>
</channel>
</rss>
"""

HTML_LIST = """
<html><body>
<div class="grid-block">
  <article class="card-list card card--list">
    <a href="https://eco.sapo.pt/2026/08/08/noticia-a/"><h3 class="card__title">Notícia A</h3></a>
  </article>
  <article class="card-list card card--list">
    <a href="https://eco.sapo.pt/entrevista/nao-e-noticia/"><h3 class="card__title">Entrevista</h3></a>
  </article>
  <article class="card-list card card--list">
    <a href="https://eco.sapo.pt/2026/08/07/noticia-b/"><h3 class="card__title">Notícia B</h3></a>
  </article>
  <a href="/outra/coisa/">não é artigo</a>
</div>
</body></html>
"""


class FakeFetcher(Fetcher):
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    def fetch_text(self, url: str) -> str:
        if url not in self.pages:
            raise RuntimeError(f"unexpected URL: {url}")
        return self.pages[url]

    def close(self) -> None:
        pass


def test_feedparser_parses_rss():
    parsed = feedparser.parse(RSS_XML)
    assert not parsed.bozo
    assert len(parsed.entries) == 2
    e = parsed.entries[0]
    assert e.title == "Primeira notícia"
    assert e.link == "https://eco.sapo.pt/2026/08/08/primeira/"
    assert e.author == "Lusa"


def test_rss_discovery_and_canonicalization():
    site = SiteConfig(id="eco", name="ECO", rss="https://eco.sapo.pt/feed/")
    disc = Discoverer(FakeFetcher({"https://eco.sapo.pt/feed/": RSS_XML}))
    items = disc.discover(site, max_items=10)
    assert len(items) == 2
    urls = {it.url for it in items}
    # utm params were NOT stripped by discovery (stripped at storage time),
    # but both items are distinct articles so both remain.
    assert "https://eco.sapo.pt/2026/08/08/primeira/" in urls


def test_html_list_discovery_filters_non_articles():
    site = SiteConfig(
        id="eco",
        name="ECO",
        lists=[
            ListConfig(
                url="https://eco.sapo.pt/ultimas/",
                link_selector="article.card-list a h3.card__title",
                article_url_pattern=r"https://eco\.sapo\.pt/20\d{2}/\d{2}/\d{2}/[^/]+/",
            )
        ],
    )
    disc = Discoverer(FakeFetcher({"https://eco.sapo.pt/ultimas/": HTML_LIST}))
    items = disc.discover(site, max_items=10)
    urls = [it.url for it in items]
    assert "https://eco.sapo.pt/2026/08/08/noticia-a/" in urls
    assert "https://eco.sapo.pt/2026/08/07/noticia-b/" in urls
    # non-matching URLs (entrevista + plain link) filtered by pattern
    assert "https://eco.sapo.pt/entrevista/nao-e-noticia/" not in urls
    assert "/outra/coisa/" not in urls
    assert len(urls) == 2


def test_html_parser_selects_selector():
    tree = HTMLParser(HTML_LIST)
    nodes = tree.css("article.card-list a h3.card__title")
    assert len(nodes) == 3
