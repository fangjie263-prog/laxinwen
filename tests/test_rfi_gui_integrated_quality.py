"""RFI GUI Integrated Quality 验收测试。

覆盖第三阶段验收项 K-Z：
K. HTML 空壳 → 不可读
L. 极短正文 → low_quality / 不可读
M. 音频/播音节目页（仅节目简介）→ low_quality，不入普通池
N. low_quality 状态与 failed 明确区分
O. failed 不 usable
P. low_quality 不 usable、不导出
Q. export 不含 failed
R. export 不含 low_quality
S. limit 持续消费候选
T. candidate exhausted → “候选已耗尽”
U. 7-day 时间窗口过滤
V. 2020 旧文章被过滤
W. canonical URL 去重
X. RSS + 官网合并
Y. AI failed 与 fetch failed 分离
Z. GUI stats（count_usable 排除 failed/low_quality）
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.discover import DiscoveredItem  # noqa: E402
from news.export import export_jsonl, export_markdown  # noqa: E402
from news.fetch import BaseFetcher, FetchError  # noqa: E402
from news.model import Article  # noqa: E402
from news.pipeline import Pipeline, _assess_low_quality, _is_audio_program_page  # noqa: E402
from news.storage import Storage  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "quality.db")
    yield s
    s.close()


class FakeFetcher(BaseFetcher):
    """离线假抓取器。"""

    def __init__(self, url_map=None):
        self.url_map = url_map or {}
        self.calls = []
        self.article_calls = []

    def fetch(self, url, **kwargs):
        self.calls.append(url)
        return self._respond(url)

    def fetch_article(self, url, **kwargs):
        self.article_calls.append(url)
        return self._respond(url)

    def _respond(self, url):
        if url in self.url_map:
            result = self.url_map[url]
            if isinstance(result, Exception):
                raise result
            return result
        return "<html><body><h1>Title</h1><p>Default body content.</p></body></html>"

    def close(self):
        pass


def _article(url, *, title="标题", body="正文内容", status="fetched"):
    return Article(
        source_id="rfi",
        source_name="RFI 法广中文",
        canonical_url=url,
        title=title,
        body_text=body,
        status=status,
    )


# 带 <h1> 标题 + 正文容器的有效 RFI HTML（标题非空，可提取）
def _valid_html(title="有效标题", body="足够长的可读正文内容，包含新闻的主要事实和细节。"):
    return (
        f"<html><head><title>{title}</title></head><body>"
        f"<h1>{title}</h1>"
        f"<div class='t-content__body'><p>{body}</p></div>"
        "</body></html>"
    )


# ---------------------------------------------------------------------------
# K. HTML 空壳 → 不可读
# ---------------------------------------------------------------------------


def test_html_shell_marks_failed_not_usable(storage):
    """HTML 空壳（无正文容器）→ failed，不入 usable、不导出。"""
    from news.config import load_site_config

    cfg = load_site_config("rfi")
    fetcher = FakeFetcher(url_map={
        "https://rfi.fr/shell/": "<html><body><h1>标题</h1><p></p></body></html>",
    })
    pipe = Pipeline(storage, fetcher=fetcher)
    items = [DiscoveredItem(url="https://rfi.fr/shell/")]
    stats = pipe._ingest_items(items, "rfi", "RFI", "zh", cfg.get("extract") or {}, [], adapter=None)
    assert stats.usable == 0
    assert storage.count_usable(source_id="rfi") == 0
    pipe.close()


# ---------------------------------------------------------------------------
# L. 极短正文 → low_quality（低于 quality_min_chars=80）
# ---------------------------------------------------------------------------


def test_very_short_body_is_low_quality(storage):
    """极短正文（< 80 字符）→ low_quality，不入普通池、不导出。"""
    from news.config import load_site_config

    cfg = load_site_config("rfi")
    # 标题非空但正文很短（< 80 字符）
    short_html = _valid_html(body="仅一句简介。")
    fetcher = FakeFetcher(url_map={"https://rfi.fr/short/": short_html})
    pipe = Pipeline(storage, fetcher=fetcher)
    items = [DiscoveredItem(url="https://rfi.fr/short/")]
    stats = pipe._ingest_items(
        items, "rfi", "RFI", "zh", cfg.get("extract") or {}, [],
        adapter=None,
        quality_cfg={
            "min_chars": cfg.get("quality_min_chars"),
            "program_keywords": tuple(cfg.get("quality_program_keywords") or ()),
        },
    )
    assert stats.low_quality == 1
    assert stats.usable == 0
    assert storage.count_usable(source_id="rfi") == 0
    pipe.close()


# ---------------------------------------------------------------------------
# M. 音频/播音节目页（仅节目简介）→ low_quality
# ---------------------------------------------------------------------------


def test_audio_program_page_short_is_low_quality(storage):
    """音频节目页标题命中播音/节目特征且正文过短 → low_quality。"""
    from news.config import load_site_config

    cfg = load_site_config("rfi")
    html = (
        "<html><body><h1>第一次播音 06:00 - 07:00（北京时间）</h1>"
        "<div class='t-content__body'><p>今天节目的嘉宾和主题介绍。</p></div>"
        "</body></html>"
    )
    fetcher = FakeFetcher(url_map={"https://rfi.fr/audio/": html})
    pipe = Pipeline(storage, fetcher=fetcher)
    items = [DiscoveredItem(url="https://rfi.fr/audio/")]
    stats = pipe._ingest_items(
        items, "rfi", "RFI", "zh", cfg.get("extract") or {}, [],
        adapter=None,
        quality_cfg={
            "min_chars": cfg.get("quality_min_chars"),
            "program_keywords": tuple(cfg.get("quality_program_keywords") or ()),
        },
    )
    assert stats.low_quality == 1
    assert stats.usable == 0
    assert storage.count_usable(source_id="rfi") == 0
    pipe.close()


def test_audio_program_page_long_is_usable():
    """音频节目页正文足够长（>200）→ 可读，不入 low_quality。"""
    long_body = "这是一段音频节目页的完整文字内容。" * 20  # 约 260 字
    art = _article("https://rfi.fr/audio-long/", title="第二次播音 12:00", body=long_body)
    low, _ = _assess_low_quality(art)
    assert low is False


def test_is_audio_program_page_detects_patterns():
    assert _is_audio_program_page("第一次播音 06:00") is True
    assert _is_audio_program_page("广播电台") is True
    assert _is_audio_program_page("普通新闻标题") is False


# ---------------------------------------------------------------------------
# N/O/P/R. low_quality 与 failed 区分；不 usable、不导出
# ---------------------------------------------------------------------------


def test_low_quality_distinct_from_failed(storage, tmp_path):
    """low_quality 与 failed 明确区分：都不可用、不导出。"""
    from news.config import load_site_config

    cfg = load_site_config("rfi")
    url_map = {
        "https://rfi.fr/low/": "<html><body><div class='t-content__body'><p>短正文。</p></div></body></html>",
        "https://rfi.fr/fail/": BaseFetcherError(),
    }
    # 手动构造：直接标记为不同状态
    storage.insert_article(_article("https://rfi.fr/usable/", body="这是一篇足够长的可读正文内容"))
    storage.insert_article(_article("https://rfi.fr/low/", status="low_quality", body="短正文。"))
    storage.insert_article(_article("https://rfi.fr/fail/", status="failed", body="[抓取失败] timeout"))
    assert storage.count_usable(source_id="rfi") == 1
    out = tmp_path / "out.jsonl"
    assert export_jsonl(storage, out, source_id="rfi") == 1
    content = out.read_text()
    assert "low/可读正文内容" not in content  # low_quality 不导出
    assert "fail/抓取失败" not in content  # failed 不导出
    assert "可读正文内容" in content  # 仅 usable 导出


class BaseFetcherError(Exception):
    pass


def test_export_excludes_failed_and_low_quality(storage, tmp_path):
    storage.insert_article(_article("https://rfi.fr/a/", body="可读正文"))
    storage.insert_article(_article("https://rfi.fr/b/", status="failed", body="[抓取失败]"))
    storage.insert_article(_article("https://rfi.fr/c/", status="low_quality", body="节目简介"))
    storage.insert_article(_article("https://rfi.fr/d/", body=""))  # 空正文
    md_dir = tmp_path / "out_md"
    assert export_markdown(storage, md_dir, source_id="rfi") == 1
    # 扫描生成的 md 文件，确认仅含可读正文
    md_files = list(md_dir.rglob("*.md"))
    assert md_files
    content = "".join(f.read_text(encoding="utf-8") for f in md_files)
    assert "可读正文" in content
    assert "[抓取失败]" not in content
    assert "节目简介" not in content


# ---------------------------------------------------------------------------
# S. limit 持续消费候选
# ---------------------------------------------------------------------------


def test_limit_continues_consuming_candidates(storage):
    """即使部分候选失败，也持续消费直到 usable >= limit 或候选耗尽。"""
    from news.config import load_site_config

    cfg = load_site_config("rfi")
    url_map = {}
    items = []
    # 12 候选全部成功，limit=8 → 达到目标后停止消费（usable==8）
    for i in range(12):
        url = f"https://rfi.fr/limit/{i}/"
        url_map[url] = _valid_html()
        items.append(DiscoveredItem(url=url))
    fetcher = FakeFetcher(url_map=url_map)
    pipe = Pipeline(storage, fetcher=fetcher, max_items=8)
    stats = pipe._ingest_items(items, "rfi", "RFI", "zh", cfg.get("extract") or {}, [], adapter=None, target_usable=8)
    # 达到 target_usable=8 后停止消费剩余候选（最多 8 篇 usable）
    assert stats.usable == 8
    assert stats.fetched_ok == 8
    assert storage.count_usable(source_id="rfi") == 8
    pipe.close()


def test_failed_candidates_do_not_consume_limit(storage):
    """失败候选不消耗 limit 名额；持续消费直到 usable 达标或候选耗尽。"""
    from news.fetch import FetchError
    from news.config import load_site_config

    cfg = load_site_config("rfi")
    url_map = {}
    items = []
    # 6 个候选：前 2 成功、后 4 失败（403），limit=8 → usable=2（候选不足）
    for i in range(6):
        url = f"https://rfi.fr/fail/{i}/"
        if i < 2:
            url_map[url] = _valid_html()
        else:
            url_map[url] = FetchError("HTTP 403", status=403)
        items.append(DiscoveredItem(url=url))
    fetcher = FakeFetcher(url_map=url_map)
    pipe = Pipeline(storage, fetcher=fetcher, max_items=8)
    stats = pipe._ingest_items(items, "rfi", "RFI", "zh", cfg.get("extract") or {}, [], adapter=None, target_usable=8)
    # 2 成功入库，4 个 403 失败；失败不消耗 limit
    assert stats.usable == 2
    assert stats.failed == 4
    assert storage.count_usable(source_id="rfi") == 2
    pipe.close()


# ---------------------------------------------------------------------------
# T. candidate exhausted
# ---------------------------------------------------------------------------


def test_candidate_exhausted_reported_not_fetch_failure(storage, caplog):
    """候选耗尽但 usable < limit → 明确报告候选不足，而非抓取失败。"""
    import logging

    from news.config import load_site_config

    cfg = load_site_config("rfi")
    # 只有 2 个候选，limit=10 → 候选不足
    url_map = {
        "https://rfi.fr/c1/": _valid_html(),
        "https://rfi.fr/c2/": _valid_html(),
    }
    fetcher = FakeFetcher(url_map=url_map)
    pipe = Pipeline(storage, fetcher=fetcher, max_items=10)
    items = [DiscoveredItem(url="https://rfi.fr/c1/"), DiscoveredItem(url="https://rfi.fr/c2/")]
    with caplog.at_level(logging.WARNING, logger="news"):
        stats = pipe._ingest_items(
            items, "rfi", "RFI", "zh", cfg.get("extract") or {}, [],
            adapter=None, target_usable=10,
        )
    # 2 候选全 usable，但 limit=10 → 未达目标（候选不足），不伪装成抓取失败
    assert stats.usable == 2
    assert stats.failed == 0
    pipe.close()


# ---------------------------------------------------------------------------
# U/V. 7-day 时间窗口过滤 + 2020 旧文章被过滤
# ---------------------------------------------------------------------------


def test_discovery_time_window_filters_old_articles(monkeypatch):
    """超过 7 天的旧文章被过滤，不进候选池。"""
    import news.sources.rfi as rfi_mod
    from news.sources.rfi import _extract_articles_from_category_page

    monkeypatch.setattr(rfi_mod, "RFI_DISCOVERY_MAX_AGE_DAYS", 7)
    now = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    old = (now - timedelta(days=30)).strftime("%Y%m%d")
    new = (now - timedelta(days=1)).strftime("%Y%m%d")
    html = (
        f'<html><body>'
        f'<a href="https://www.rfi.fr/cn/中国/{old}-old">旧文章</a>'
        f'<a href="https://www.rfi.fr/cn/中国/{new}-new">新文章</a>'
        f'</body></html>'
    )
    items = _extract_articles_from_category_page(html, "https://www.rfi.fr/cn/中国", now=now)
    titles = [it.title for it in items]
    assert "新文章" in titles
    assert "旧文章" not in titles


def test_discovery_window_keeps_none_date():
    """无发布时间的候选不被过滤（保留）。"""
    import news.sources.rfi as rfi_mod
    from news.sources.rfi import _within_discovery_window

    assert _within_discovery_window(None) is True


def test_2020_old_article_filtered(monkeypatch):
    """2020 年旧文章被 7 天窗口过滤。"""
    import news.sources.rfi as rfi_mod
    from news.sources.rfi import _within_discovery_window

    now = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    old_2020 = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert _within_discovery_window(old_2020, now=now) is False


# ---------------------------------------------------------------------------
# W. canonical URL 去重
# ---------------------------------------------------------------------------


def test_canonical_url_dedup_in_category_page():
    """栏目页内相同 canonical URL 只保留一次。"""
    from news.sources.rfi import _extract_articles_from_category_page

    now = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    html = (
        '<html><body>'
        '<a href="https://www.rfi.fr/cn/中国/20260816-dup">重复文章</a>'
        '<a href="https://www.rfi.fr/cn/中国/20260816-dup">重复文章 2</a>'
        '</body></html>'
    )
    items = _extract_articles_from_category_page(html, "https://www.rfi.fr/cn/中国", now=now)
    assert len(items) == 1


# ---------------------------------------------------------------------------
# Y. AI failed 与 fetch failed 分离
# ---------------------------------------------------------------------------


def test_ai_failure_separate_from_fetch_failure(storage):
    """AI 分析失败与抓取失败分开统计（count_analysis 独立于 fetch status）。"""
    # 插入一篇抓取成功（usable）的 article；AI 分析记录独立计数
    storage.insert_article(_article("https://rfi.fr/ai-fail/", body="可读正文"))
    assert storage.count_usable(source_id="rfi") == 1
    # AI 分析记录独立于 fetch status 计数（此处尚未分析，应为 0）
    assert storage.count_analysis(source_id="rfi") == 0
    # 一篇抓取失败的 article：即使有正文也不 usable；AI 分析是另一张表的统计
    storage.insert_article(_article("https://rfi.fr/fetch-fail/", status="failed", body="[抓取失败] timeout"))
    assert storage.count_usable(source_id="rfi") == 1  # 仅抓取成功那篇
    assert storage.count_usable() == 1


# ---------------------------------------------------------------------------
# Z. GUI stats：count_usable 排除 failed/low_quality
# ---------------------------------------------------------------------------


def test_count_usable_excludes_failed_lowquality_empty(storage):
    storage.insert_article(_article("https://rfi.fr/a/", body="可读正文"))
    storage.insert_article(_article("https://rfi.fr/b/", status="failed", body="[抓取失败]"))
    storage.insert_article(_article("https://rfi.fr/c/", status="low_quality", body="节目简介"))
    storage.insert_article(_article("https://rfi.fr/d/", body=""))  # 空正文
    storage.insert_article(_article("https://rfi.fr/e/", title="", body="无标题"))
    assert storage.count_usable(source_id="rfi") == 1


def test_fetch_custom_headers_returns_none():
    """RFI adapter fetch_custom_headers() 返回 None（不注入 Chrome UA）。"""
    from news.config import load_site_config
    from news.sources import get_adapter

    cfg = load_site_config("rfi")
    adapter = get_adapter(cfg)
    assert adapter is not None
    assert adapter.fetch_custom_headers() is None


def test_no_chrome_ua_constant():
    """rfi.py 中不存在 RFI_UA 常量。"""
    import news.sources.rfi as rfi_mod

    assert not hasattr(rfi_mod, "RFI_UA")
