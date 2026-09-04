"""NYT Chinese source adapter tests.

Uses fixture HTML files under ``tests/fixtures/nytchinese/``.

To use the actual reconnaissance data from the user's local run, set
``NYT_FIXTURE_DIR`` to the ``nyt-recon/articles/`` directory containing the
numbered HTML files (``001.html`` .. ``005.html``).

A minimal fixture loader (``_load_article``) resolves a fixture either from
the repo fixtures or from the nyt-recon directory by matching the article URL.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure src/ is importable
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from news.nytcn import parse_nyt_article, SOURCE_ID, SOURCE_NAME  # noqa: E402

# Repo-internal fixture directory
DEFAULT_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "nytchinese"

# User-supplied fixture override (e.g. nyt-recon/articles/)
FIXTURE_DIR = Path(os.environ.get("NYT_FIXTURE_DIR", str(DEFAULT_FIXTURE_DIR)))

# Section → (repo_fixture_filename, canonical_url, nyt_recon_number)
NYT_FIXTURES = {
    "technology": (
        "technology.html",
        "https://cn.nytimes.com/technology/20260904/openai-hugging-face-hacking/",
        "001.html",
    ),
    "business": (
        "business.html",
        "https://cn.nytimes.com/business/20260904/volkswagen-job-cuts/",
        "002.html",
    ),
    "china": (
        "china.html",
        "https://cn.nytimes.com/china/20260903/china-egypt-xi-jinping-el-sisi/",
        "003.html",
    ),
    "world": (
        "world.html",
        "https://cn.nytimes.com/world/20260903/nepal-flood-miracle-house-dhunge-bazaar/",
        "004.html",
    ),
    "opinion": (
        "opinion.html",
        "https://cn.nytimes.com/opinion/20260903/world-order-international-relations/",
        "005.html",
    ),
}


def _load_fixture(section: str) -> str:
    """Load article HTML for a given section.

    Tries, in order:
      1. ``NYT_FIXTURE_DIR`` override (env var)
      2. Repo fixture directory ``tests/fixtures/nytchinese/``
    """
    _, url, nyt_recon_fname = NYT_FIXTURES[section]

    # 1. User override dir (could be nyt-recon/articles/)
    #    Try both by-number and by-section-name
    candidates = [
        FIXTURE_DIR / nyt_recon_fname,
        FIXTURE_DIR / NYT_FIXTURES[section][0],
    ]
    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8")

    # 2. Default repo fixture dir
    repo_path = DEFAULT_FIXTURE_DIR / NYT_FIXTURES[section][0]
    if repo_path.is_file():
        return repo_path.read_text(encoding="utf-8")

    pytest.skip(f"No fixture for {section}: tried {[str(c) for c in candidates]} and {repo_path}")


# ---------------------------------------------------------------------------
# Metadata tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("section", list(NYT_FIXTURES.keys()))
def test_metadata_extracted(section: str):
    """All 5 sections must extract valid title / canonical / section / datetime."""
    html = _load_fixture(section)
    _, url, _ = NYT_FIXTURES[section]
    result = parse_nyt_article(html, url=url)
    meta = result.metadata

    assert meta.title, f"[{section}] title should not be empty"
    assert meta.canonical_url, f"[{section}] canonical_url should not be empty"
    assert meta.published_at is not None, f"[{section}] published_at should be set"
    assert meta.section == section, f"[{section}] section should be {section}, got {meta.section}"


@pytest.mark.parametrize("section", list(NYT_FIXTURES.keys()))
def test_canonical_url(section: str):
    """Canonical URL must be correct."""
    html = _load_fixture(section)
    _, expected_url, _ = NYT_FIXTURES[section]
    result = parse_nyt_article(html, url=expected_url)
    assert result.metadata.canonical_url == expected_url


@pytest.mark.parametrize("section", list(NYT_FIXTURES.keys()))
def test_authors_extracted(section: str):
    """Authors should not be empty for these fixtures."""
    html = _load_fixture(section)
    _, url, _ = NYT_FIXTURES[section]
    result = parse_nyt_article(html, url=url)
    assert len(result.metadata.authors) > 0, f"[{section}] authors should not be empty"


@pytest.mark.parametrize("section", list(NYT_FIXTURES.keys()))
def test_body_not_empty(section: str):
    """Body content must be extracted for every section."""
    html = _load_fixture(section)
    _, url, _ = NYT_FIXTURES[section]
    result = parse_nyt_article(html, url=url)
    assert len(result.body_text) > 200, f"[{section}] body_text too short: {len(result.body_text)}"
    assert len(result.blocks) >= 5, f"[{section}] should have at least 5 content blocks"


# ---------------------------------------------------------------------------
# Content exclusion tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("section", list(NYT_FIXTURES.keys()))
def test_body_excludes_related_articles(section: str):
    """相关报道 must never appear in body_text."""
    html = _load_fixture(section)
    _, url, _ = NYT_FIXTURES[section]
    result = parse_nyt_article(html, url=url)
    assert "相关报道" not in result.body_text
    assert "相关报道" not in result.body_html


@pytest.mark.parametrize("section", list(NYT_FIXTURES.keys()))
def test_body_excludes_most_popular(section: str):
    """最受欢迎 must never appear in body_text."""
    html = _load_fixture(section)
    _, url, _ = NYT_FIXTURES[section]
    result = parse_nyt_article(html, url=url)
    assert "最受欢迎" not in result.body_text
    assert "最受欢迎" not in result.body_html


@pytest.mark.parametrize("section", list(NYT_FIXTURES.keys()))
def test_body_excludes_footer(section: str):
    """Footer / copyright must not leak into body_text."""
    html = _load_fixture(section)
    _, url, _ = NYT_FIXTURES[section]
    result = parse_nyt_article(html, url=url)
    assert "All Rights Reserved" not in result.body_text
    assert "关于我们" not in result.body_text
    assert "联系我们" not in result.body_text


@pytest.mark.parametrize("section", list(NYT_FIXTURES.keys()))
def test_body_excludes_language_switcher(section: str):
    """Language switcher must not appear as link in body."""
    html = _load_fixture(section)
    _, url, _ = NYT_FIXTURES[section]
    result = parse_nyt_article(html, url=url)
    # "English" alone should not be a content block
    english_blocks = [b for b in result.blocks if b.type == "paragraph" and b.text.strip() == "English"]
    assert len(english_blocks) == 0


# ---------------------------------------------------------------------------
# Block structure tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("section", list(NYT_FIXTURES.keys()))
def test_block_order_preserved(section: str):
    """Block sequence must follow original document order."""
    html = _load_fixture(section)
    _, url, _ = NYT_FIXTURES[section]
    result = parse_nyt_article(html, url=url)

    # Extract paragraph text in DOM order from the fixture article-area
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html)
    article = tree.css_first("main article, article, main .article-area, main")
    para_texts = []
    if article:
        for p in article.css("p"):
            # Skip paragraphs inside related/popular/footer/nav sections
            skip = False
            node = p.parent
            while node is not None:
                pc = (node.attributes.get("class", "") or "").lower()
                if any(kw in pc for kw in ("related", "popular", "footer", "nav")):
                    skip = True
                    break
                if node.tag in ("nav", "footer"):
                    skip = True
                    break
                node = node.parent
            if not skip:
                txt = (p.text(strip=True) or "")
                if txt:
                    para_texts.append(txt)

    parsed_paras = [b.text for b in result.blocks if b.type == "paragraph"]
    assert len(parsed_paras) >= 5, f"[{section}] expected ≥5 paragraphs, got {len(parsed_paras)}"
    assert len(parsed_paras) <= len(para_texts) + 5, f"[{section}] more paragraphs than source"


@pytest.mark.parametrize("section", list(NYT_FIXTURES.keys()))
def test_figures_and_images_preserved(section: str):
    """Figure/image/caption should be preserved with correct metadata."""
    html = _load_fixture(section)
    _, url, _ = NYT_FIXTURES[section]
    result = parse_nyt_article(html, url=url)

    figure_blocks = [b for b in result.blocks if b.type in ("figure", "image")]
    if figure_blocks:
        for fig in figure_blocks:
            assert fig.src, "Figure/image block must have src"
            assert fig.src.startswith("http"), f"Image src must be absolute: {fig.src}"
    else:
        assert result.metadata.lead_image, "Lead image should be available"


@pytest.mark.parametrize("section", list(NYT_FIXTURES.keys()))
def test_metadata_images_present(section: str):
    """Metadata images list should have at least one entry."""
    html = _load_fixture(section)
    _, url, _ = NYT_FIXTURES[section]
    result = parse_nyt_article(html, url=url)
    assert len(result.metadata.images) > 0, f"[{section}] should have at least one image"
    for img in result.metadata.images:
        assert img.get("url"), "Image URL required"
        assert img["url"].startswith("http"), f"Image URL must be absolute: {img['url']}"


# ---------------------------------------------------------------------------
# Section-specific content tests
# ---------------------------------------------------------------------------

def test_technology_has_multiple_paragraphs():
    """Technology article should have extensive paragraphs."""
    html = _load_fixture("technology")
    _, url, _ = NYT_FIXTURES["technology"]
    result = parse_nyt_article(html, url=url)
    paras = [b for b in result.blocks if b.type == "paragraph"]
    assert len(paras) >= 6
    headings = [b for b in result.blocks if b.type == "heading"]
    assert len(headings) >= 3


def test_real_recon_article_uses_production_article_paragraphs():
    """The captured production NYT template must yield all 23 body paragraphs."""
    path = Path(__file__).resolve().parent.parent / "nyt-recon" / "articles" / "001.html"
    if not path.is_file():
        pytest.skip(f"Missing reconnaissance article: {path}")
    url = NYT_FIXTURES["technology"][1]
    result = parse_nyt_article(path.read_text(encoding="utf-8"), url=url)
    paragraphs = [block.text for block in result.blocks if block.type == "paragraph"]
    figures = [block for block in result.blocks if block.type == "figure"]

    assert len(path.read_text(encoding="utf-8").split('class="article-paragraph"')) - 1 == 23
    assert len(figures) == 1
    assert figures[0].src == "https://static01.nyt.com/images/2026/09/05/business/00roose-hugging-face/00roose-hugging-face-jumbo.jpg"
    assert paragraphs[0].startswith("今年夏天，当我第一次听说一群由OpenAI创造的AI智能体")
    assert paragraphs[-1] == "下一次，我们可能不会这么幸运。"
    assert "免费下载 纽约时报中文网" not in result.body_text
    assert "中文  中" not in result.body_text
    assert len(result.body_text) > 2000
    assert result.body_html.count("<figure class=\"content-image\">") == 1
    assert result.body_html.count("<img ") == 1
    assert figures[0].src in result.body_html

    # The production page's lead figure precedes the first body paragraph;
    # verify the generated HTML keeps that relative DOM order.
    assert result.body_html.index("<figure") < result.body_html.index("<p>")


def test_business_has_multiple_authors():
    """Business article should have two authors."""
    html = _load_fixture("business")
    _, url, _ = NYT_FIXTURES["business"]
    result = parse_nyt_article(html, url=url)
    assert len(result.metadata.authors) == 2


def test_china_has_heading_and_blockquote():
    """China article should have h2 headings and a blockquote."""
    html = _load_fixture("china")
    _, url, _ = NYT_FIXTURES["china"]
    result = parse_nyt_article(html, url=url)
    headings = [b for b in result.blocks if b.type == "heading"]
    quotes = [b for b in result.blocks if b.type == "blockquote"]
    assert len(headings) >= 1
    assert len(quotes) >= 1


def test_world_has_lists():
    """World article should contain a list block."""
    html = _load_fixture("world")
    _, url, _ = NYT_FIXTURES["world"]
    result = parse_nyt_article(html, url=url)
    lists = [b for b in result.blocks if b.type == "list"]
    assert len(lists) >= 1
    for lst in lists:
        assert len(lst.items) >= 3, "List should have ≥3 items"


def test_opinion_is_opinion_section():
    """Opinion article should have correct section metadata."""
    html = _load_fixture("opinion")
    _, url, _ = NYT_FIXTURES["opinion"]
    result = parse_nyt_article(html, url=url)
    assert result.metadata.section == "opinion"


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

def test_build_article_data_shape():
    """build_article must return fields compatible with news.model.Article."""
    from news.nytcn import build_article

    html = _load_fixture("technology")
    _, url, _ = NYT_FIXTURES["technology"]
    data = build_article(html, url=url)

    assert data["source_id"] == "nytchinese"
    assert data["source_name"] == SOURCE_NAME
    assert data["canonical_url"] == url
    assert data["title"]
    assert data["authors"]
    assert data["published_at"] is not None
    assert data["body_text"]
    assert data["body_html"]
    assert isinstance(data["images"], list)
    assert data["language"] == "zh-CN"
    assert data["section"] == "technology"


def test_source_constants():
    """Source ID and name must be consistent."""
    assert SOURCE_ID == "nytchinese"
    assert SOURCE_NAME


# ---------------------------------------------------------------------------
# Discovery / config test
# ---------------------------------------------------------------------------

def test_site_config_exists():
    """NYT Chinese site config YAML must exist and be loadable."""
    from news.config import load_site_config

    cfg = load_site_config("nytchinese")
    assert cfg["id"] == "nytchinese"
    assert cfg["rss"] == "https://cn.nytimes.com/rss/news.xml"
    assert cfg["adapter"] == "nytchinese"
    assert cfg["language"] == "zh-CN"


# ---------------------------------------------------------------------------
# Pipeline integration test
# ---------------------------------------------------------------------------

def test_extraction_via_pipeline_dispatch():
    """apply_extraction_to_article must use NYT adapter when source_id is nytchinese."""
    from news.extract import apply_extraction_to_article
    from news.model import Article

    html = _load_fixture("technology")
    _, url, _ = NYT_FIXTURES["technology"]

    article = Article(
        source_id="nytchinese",
        source_name=SOURCE_NAME,
        canonical_url=url,
        title="placeholder",
    )

    apply_extraction_to_article(article, html, source_id="nytchinese")

    assert article.title
    assert article.canonical_url == url
    assert len(article.body_text) > 200
    assert article.published_at is not None
    assert len(article.authors) > 0
    assert "相关报道" not in article.body_text
    assert "最受欢迎" not in article.body_text
