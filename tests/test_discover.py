"""RSS 解析与文章发现测试（含离线 fixture 与在线测试）。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.discover import discover_from_rss, _parse_datetime  # noqa: E402
from news.model import Article  # noqa: E402

_RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>ECO</title>
  <link>https://eco.sapo.pt</link>
  <item>
    <title>Primeira notícia</title>
    <link>https://eco.sapo.pt/2026/08/08/primeira-noticia/?utm_source=x</link>
    <dc:creator><![CDATA[Lusa]]></dc:creator>
    <pubDate>Sat, 08 Aug 2026 22:23:34 +0000</pubDate>
    <description><![CDATA[<p>Resumo.</p>]]></description>
    <content:encoded><![CDATA[<p>Conteúdo completo.</p>]]></content:encoded>
  </item>
  <item>
    <title>Segunda notícia</title>
    <link>https://eco.sapo.pt/entrevista/segunda-noticia/</link>
    <author>Flávio Nunes</author>
    <pubDate>Sat, 08 Aug 2026 15:59:52 +0000</pubDate>
  </item>
</channel>
</rss>
"""


@pytest.fixture
def rss_file(tmp_path):
    p = tmp_path / "feed.xml"
    p.write_text(_RSS_SAMPLE, encoding="utf-8")
    return str(p)


class TestRssParsing:
    def test_parse_entries(self, rss_file):
        items = discover_from_rss(rss_file)
        assert len(items) == 2

    def test_title_and_url(self, rss_file):
        items = discover_from_rss(rss_file)
        assert items[0].title == "Primeira notícia"
        assert items[0].url == (
            "https://eco.sapo.pt/2026/08/08/primeira-noticia/?utm_source=x"
        )

    def test_authors(self, rss_file):
        items = discover_from_rss(rss_file)
        assert items[0].authors == ["Lusa"]
        assert items[1].authors == ["Flávio Nunes"]

    def test_published_date(self, rss_file):
        items = discover_from_rss(rss_file)
        assert items[0].published_at is not None
        assert items[0].published_at.hour == 22

    def test_content_html_preserved(self, rss_file):
        items = discover_from_rss(rss_file)
        assert items[0].content_html is not None
        assert "Conteúdo completo" in items[0].content_html

    def test_to_article_canonicalizes(self, rss_file):
        items = discover_from_rss(rss_file)
        art = items[0].to_article("eco", "ECO")
        assert isinstance(art, Article)
        assert art.canonical_url == "https://eco.sapo.pt/2026/08/08/primeira-noticia/"
        assert art.source_id == "eco"


class TestDatetimeParse:
    def test_parses_iso_with_offset(self):
        dt = _parse_datetime("2026-08-08T22:23:34+00:00", None)
        assert dt is not None and dt.hour == 22

    def test_parses_rfc822(self):
        dt = _parse_datetime("Sat, 08 Aug 2026 22:23:34 +0000", None)
        assert dt is not None and dt.hour == 22

    def test_parses_struct_time(self):
        import time as _time

        struct = _time.struct_time((2026, 8, 8, 10, 30, 0, 5, 220, 0))
        dt = _parse_datetime(None, struct)
        assert dt is not None and dt.hour == 10

    def test_none(self):
        assert _parse_datetime(None, None) is None


@pytest.mark.network
class TestLiveRss:
    def test_eco_feed(self):
        items = discover_from_rss("https://eco.sapo.pt/feed/")
        assert len(items) >= 10
        assert all(i.url for i in items)
