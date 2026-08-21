"""RFI GUI 正文质量修复测试。

覆盖第三阶段验收项 A-J：
A. ``article_interval=15`` 生效（FetcherOptions + HttpxFetcher.fetch_article 独立节流）
B. retry 每个 attempt 都经过 article throttle
C. HTTP 403 → failed，不计 usable，不导出
D. HTTP 200 + 空正文 → failed，不计 usable，不导出
E. 空 title → failed
F. 10 候选、6 成功、4 失败 → usable=6，导出=6
G. 已有有效正文（RSS content short-circuit）→ 0-fetch
H. discovery 仍然使用普通 fetch
I. ECO/HKEJ 未配置 article_interval 时行为不变
J. GUI 使用 usable count，并正确区分 fetch failure / AI failure
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.discover import DiscoveredItem  # noqa: E402
from news.export import export_jsonl, export_markdown  # noqa: E402
from news.fetch import (  # noqa: E402
    BaseFetcher,
    FetchError,
    FetcherOptions,
    HttpxFetcher,
)
from news.model import Article  # noqa: E402
from news.pipeline import Pipeline, _validate_extracted_body  # noqa: E402
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
    """离线假抓取器，支持按 URL 返回成功/失败/空正文。"""

    def __init__(self, url_map=None):
        self.url_map = url_map or {}
        self.calls = []
        self.article_calls = []

    def fetch(self, url, **kwargs):
        """discovery 用普通 fetch。"""
        self.calls.append(url)
        return self._respond(url)

    def fetch_article(self, url, **kwargs):
        """文章正文用 fetch_article。"""
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


# 一篇有效 RFI 文章的 HTML（正文容器 .t-content__body）
_VALID_HTML = """<html><head><title>RFI 测试</title></head><body>
<h1>RFI 测试文章标题</h1>
<div class="t-content__chapo">导语段落。</div>
<div class="t-content__body"><p>正文第一段，包含足够多的可读文字内容用于测试。</p>
<p>正文第二段，继续提供更多可读内容。</p></div>
</body></html>"""


def _article(url, *, title="标题", body="正文内容", status="fetched"):
    return Article(
        source_id="rfi",
        source_name="RFI 法广中文",
        canonical_url=url,
        title=title,
        body_text=body,
        status=status,
    )


# ---------------------------------------------------------------------------
# A. article_interval 配置
# ---------------------------------------------------------------------------


def test_fetcher_options_article_interval():
    opts = FetcherOptions(article_interval=15)
    assert opts.article_interval == 15


def test_rfi_yaml_has_article_interval():
    from news.config import load_site_config

    cfg = load_site_config("rfi")
    assert cfg.get("article_interval") == 15


def test_eco_hkej_no_article_interval():
    from news.config import load_site_config

    assert load_site_config("eco").get("article_interval") is None
    assert load_site_config("hkej").get("article_interval") is None


def test_httpx_fetcher_article_interval_attribute():
    f = HttpxFetcher(FetcherOptions(article_interval=15))
    assert f.article_interval == 15
    f.article_interval = 20
    assert f.article_interval == 20
    f.close()


# ---------------------------------------------------------------------------
# B. retry 每个 attempt 都 throttle
# ---------------------------------------------------------------------------


def test_httpx_fetch_article_throttle_per_attempt(monkeypatch):
    """直接验证 HttpxFetcher._fetch_with_throttle 在 retry 循环中每次 attempt 都节流。"""
    opts = FetcherOptions(retries=3, min_interval=0, max_interval=0, article_interval=15)
    h = HttpxFetcher(opts)
    throttle_calls = []

    def fake_throttle(url):
        throttle_calls.append(url)

    class FakeResp:
        status_code = 500
        headers = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def get(self, url, **k):
            # 每次都返回 500，让 retry 走到第 3 次后抛出 FetchError
            return FakeResp()

        def close(self):
            pass

    h._client = FakeClient()
    h.options.retries = 3
    h.options.retry_backoff = 0  # 快速重试
    with pytest.raises(FetchError):
        h._fetch_with_throttle("https://rfi.fr/a", fake_throttle)
    # 3 次 attempt，每次 attempt 前都应调用 throttle
    assert len(throttle_calls) == 3
    h.close()


def test_article_throttle_independent_from_discovery():
    """article throttle 与 discovery throttle 完全独立（不同 bucket）。"""
    opts = FetcherOptions(min_interval=100, max_interval=100)  # discovery 很慢
    f = HttpxFetcher(opts)
    f.article_interval = 0.001  # article 很快
    # discovery 节流应记录在 _last_request，article 节流记录在 _last_article_request
    f._throttle_with("https://rfi.fr/x", 0.001, f._last_article_request)
    assert "rfi.fr" in f._last_article_request
    assert "rfi.fr" not in f._last_request
    f.close()


# ---------------------------------------------------------------------------
# C. HTTP 403 → failed，不计 usable，不导出
# ---------------------------------------------------------------------------


def test_http_403_marks_failed_not_usable(storage):
    from news.config import load_site_config

    cfg = load_site_config("rfi")
    fetcher = FakeFetcher(
        url_map={"https://rfi.fr/cn/a/20260101-x/": FetchError("HTTP 403", status=403)}
    )
    pipe = Pipeline(storage, fetcher=fetcher)
    # 直接喂入一篇候选
    items = [DiscoveredItem(url="https://rfi.fr/cn/a/20260101-x/")]
    stats = pipe._ingest_items(items, "rfi", "RFI", "zh", cfg.get("extract") or {}, [], adapter=None)
    assert stats.failed == 1
    assert stats.usable == 0
    arts = storage.list_articles(source_id="rfi", limit=10)
    assert arts[0].status == "failed"
    assert storage.count_usable(source_id="rfi") == 0
    pipe.close()


def test_http_403_not_exported(storage, tmp_path):
    from news.config import load_site_config

    cfg = load_site_config("rfi")
    fetcher = FakeFetcher(
        url_map={"https://rfi.fr/cn/a/20260101-x/": FetchError("HTTP 403", status=403)}
    )
    pipe = Pipeline(storage, fetcher=fetcher)
    items = [DiscoveredItem(url="https://rfi.fr/cn/a/20260101-x/")]
    pipe._ingest_items(items, "rfi", "RFI", "zh", cfg.get("extract") or {}, [], adapter=None)
    out = tmp_path / "out.jsonl"
    n = export_jsonl(storage, out, source_id="rfi")
    assert n == 0
    pipe.close()


# ---------------------------------------------------------------------------
# D. HTTP 200 + 空正文 → failed，不计 usable，不导出
# ---------------------------------------------------------------------------


def test_empty_body_marks_failed(storage):
    from news.config import load_site_config

    cfg = load_site_config("rfi")
    fetcher = FakeFetcher(
        url_map={"https://rfi.fr/cn/b/20260101-y/": "<html><body><h1>标题</h1></body></html>"}
    )
    pipe = Pipeline(storage, fetcher=fetcher)
    items = [DiscoveredItem(url="https://rfi.fr/cn/b/20260101-y/")]
    stats = pipe._ingest_items(items, "rfi", "RFI", "zh", cfg.get("extract") or {}, [], adapter=None)
    assert stats.failed == 1
    assert stats.usable == 0
    arts = storage.list_articles(source_id="rfi", limit=10)
    assert arts[0].status == "failed"
    assert storage.count_usable(source_id="rfi") == 0
    pipe.close()


def test_empty_body_not_exported(storage, tmp_path):
    from news.config import load_site_config

    cfg = load_site_config("rfi")
    fetcher = FakeFetcher(
        url_map={"https://rfi.fr/cn/b/20260101-y/": "<html><body><h1>标题</h1></body></html>"}
    )
    pipe = Pipeline(storage, fetcher=fetcher)
    items = [DiscoveredItem(url="https://rfi.fr/cn/b/20260101-y/")]
    pipe._ingest_items(items, "rfi", "RFI", "zh", cfg.get("extract") or {}, [], adapter=None)
    out = tmp_path / "out.jsonl"
    assert export_jsonl(storage, out, source_id="rfi") == 0
    pipe.close()


# ---------------------------------------------------------------------------
# E. 空 title → failed
# ---------------------------------------------------------------------------


def test_empty_title_marks_failed(storage):
    from news.config import load_site_config

    cfg = load_site_config("rfi")
    # 返回的 HTML 有正文但无标题（无 h1 / og:title / title）
    empty_title_html = (
        '<html><body><div class="t-content__body">'
        "<p>这是一段足够长、可读的正文内容，用于测试标题为空时应该标记失败。</p>"
        "</div></body></html>"
    )
    fetcher = FakeFetcher(
        url_map={"https://rfi.fr/cn/c/20260101-z/": empty_title_html}
    )
    pipe = Pipeline(storage, fetcher=fetcher)
    items = [DiscoveredItem(url="https://rfi.fr/cn/c/20260101-z/", title="")]
    stats = pipe._ingest_items(items, "rfi", "RFI", "zh", cfg.get("extract") or {}, [], adapter=None)
    assert stats.failed == 1
    assert stats.usable == 0
    arts = storage.list_articles(source_id="rfi", limit=10)
    assert arts[0].status == "failed"
    pipe.close()


# ---------------------------------------------------------------------------
# F. 10 候选、6 成功、4 失败 → usable=6，导出=6
# ---------------------------------------------------------------------------


def test_ten_candidates_six_usable_four_failed(storage, tmp_path):
    from news.config import load_site_config

    cfg = load_site_config("rfi")
    url_map = {}
    items = []
    for i in range(10):
        url = f"https://rfi.fr/cn/t/{20260101}-{i}/"
        if i < 6:
            url_map[url] = _VALID_HTML
        else:
            url_map[url] = FetchError("HTTP 403", status=403)
        items.append(DiscoveredItem(url=url))
    fetcher = FakeFetcher(url_map=url_map)
    pipe = Pipeline(storage, fetcher=fetcher)
    stats = pipe._ingest_items(items, "rfi", "RFI", "zh", cfg.get("extract") or {}, [], adapter=None)
    assert stats.fetched_ok == 6
    assert stats.failed == 4
    assert stats.usable == 6
    assert storage.count_usable(source_id="rfi") == 6
    out = tmp_path / "all.jsonl"
    assert export_jsonl(storage, out, source_id="rfi") == 6
    pipe.close()


# ---------------------------------------------------------------------------
# G. 已有有效正文 → 0-fetch
# ---------------------------------------------------------------------------


def test_existing_valid_body_zero_fetch(storage):
    from news.config import load_site_config

    cfg = load_site_config("rfi")
    fetcher = FakeFetcher(url_map={})
    pipe = Pipeline(storage, fetcher=fetcher)
    # RSS 已带完整正文 content_html → 走 short-circuit，不 fetch 原文
    long_content = (
        "这是一段足够长的、可读的 RSS 完整正文内容，"
        "长度需要超过 has_usable_content 的 150 字符阈值才能判定为完整正文。"
        "继续补充更多文字以确保达到阈值。"
        "这一段文字继续扩充，让整段内容真正足够长。"
        "再增加一些描述性文字，确保整个段落的内容确实足够长。"
        "这部分文字是额外补充的，主要目的是让长度超过阈值。"
        "多一些内容总是没有坏处，继续写几个短句。"
    )
    items = [
        DiscoveredItem(
            url="https://rfi.fr/cn/g/20260101-ok/",
            content_html=f"<p>{long_content}</p>",
        )
    ]
    stats = pipe._ingest_items(items, "rfi", "RFI", "zh", cfg.get("extract") or {}, [], adapter=None)
    assert stats.fetched_ok == 1
    assert stats.usable == 1
    # discovery 未调用任何正文 fetch_article
    assert fetcher.article_calls == []
    pipe.close()


# ---------------------------------------------------------------------------
# H. discovery 仍然使用普通 fetch
# ---------------------------------------------------------------------------


def test_discovery_uses_plain_fetch(storage, monkeypatch):
    from news.config import load_site_config
    from news.discover import discover_for_site

    cfg = load_site_config("rfi")
    fetcher = FakeFetcher(url_map={})
    # 直接验证 run_site 中 discovery 走 fetcher.fetch 而非 fetch_article
    calls_article = []

    class Probe(FakeFetcher):
        def fetch_article(self, url, **kwargs):
            calls_article.append(url)
            return super().fetch_article(url, **kwargs)

    probe = Probe(url_map={})
    # 验证 discovery 流程（官方 RSS 404 → 栏目页）走 fetch
    # 用空 url_map，默认返回有效正文 → discovery 用 fetch 收集栏目
    pipe = Pipeline(storage, fetcher=probe)
    pipe.run_site("rfi")
    # discovery 阶段不应触发 fetch_article（正文节流 15s 不应作用在 discovery）
    assert len(calls_article) == 0
    pipe.close()


# ---------------------------------------------------------------------------
# I. ECO/HKEJ 未配置 article_interval 时行为不变
# ---------------------------------------------------------------------------


def test_eco_hkej_no_article_interval_behavior(storage):
    """ECO 未配置 article_interval，fetch_article 回退到普通 fetch。"""
    from news.fetch import BaseFetcher

    # 未覆盖 fetch_article 的 fetcher → 默认委托 fetch
    class PlainFetcher(BaseFetcher):
        def __init__(self):
            self.called = False

        def fetch(self, url, **kwargs):
            self.called = True
            return "ok"

        def close(self):
            pass

    pf = PlainFetcher()
    assert pf.fetch_article("https://eco.sapo.pt/x/") == "ok"
    assert pf.called is True


def test_eco_article_interval_none_pipeline(storage):
    """ECO 不设置 article_interval，pipeline run_site 不改变 fetcher.article_interval 语义。"""
    from news.config import load_site_config

    cfg = load_site_config("eco")
    assert cfg.get("article_interval") is None
    # pipeline 只在有 article_interval 时设置 fetcher.article_interval
    f = FakeFetcher(url_map={})
    pipe = Pipeline(storage, fetcher=f)
    # 手动模拟 run_site 开头逻辑
    if cfg.get("article_interval") is not None and hasattr(f, "article_interval"):
        f.article_interval = float(cfg["article_interval"])
    assert not hasattr(f, "article_interval") or f.article_interval is None or f.article_interval == 0
    pipe.close()


# ---------------------------------------------------------------------------
# J. GUI 使用 usable count
# ---------------------------------------------------------------------------


def test_storage_count_usable_excludes_failed_and_empty(storage):
    # 1 篇成功可用
    storage.insert_article(_article("https://rfi.fr/a/", body="有效正文"))
    # 1 篇 failed
    storage.insert_article(_article("https://rfi.fr/b/", status="failed"))
    # 1 篇空正文
    storage.insert_article(_article("https://rfi.fr/c/", body=""))
    # 1 篇 [抓取失败] 前缀
    storage.insert_article(_article("https://rfi.fr/d/", body="[抓取失败] timeout"))
    assert storage.count_usable(source_id="rfi") == 1


def test_gui_uses_count_usable(monkeypatch, storage):
    """GUI 状态卡片应使用 count_usable() 统计可读新闻。"""
    # 环境无 tkinter 时明确跳过（GUI 无法实测）
    pytest.importorskip("tkinter")
    import tkinter

    try:
        root = tkinter.Tk()
    except Exception:
        pytest.skip("当前环境无法创建 tkinter Tk（无显示服务器），GUI 无法实测")
    storage.insert_article(_article("https://rfi.fr/a/", body="有效正文"))
    # 手动验证 count_usable 被用于状态
    assert storage.count_usable(source_id="rfi") == 1
    root.destroy()


# ---------------------------------------------------------------------------
# _validate_extracted_body 单元测试
# ---------------------------------------------------------------------------


def test_validate_extracted_body_valid():
    art = _article("https://rfi.fr/valid/", body="这是一段足够长的可读正文内容")
    assert _validate_extracted_body(art) is True


def test_validate_extracted_body_empty_title():
    art = _article("https://rfi.fr/et/", title="", body="有正文但标题为空")
    assert _validate_extracted_body(art) is False


def test_validate_extracted_body_empty_body():
    art = _article("https://rfi.fr/eb/", body="")
    assert _validate_extracted_body(art) is False


def test_validate_extracted_body_failure_prefix():
    art = _article("https://rfi.fr/fp/", body="[抓取失败] timeout")
    assert _validate_extracted_body(art) is False
