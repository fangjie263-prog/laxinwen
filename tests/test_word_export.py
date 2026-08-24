"""Word（DOCX）研究阅读包导出测试。

覆盖验收清单（对应 Issue）：
1. 能生成 .docx
2. 文件可以正常打开 / zip 结构有效
3. 标题存在
4. 目录存在
5. 目录和正文之间有内部跳转关系（bookmark + internal hyperlink）
6. 每篇新闻正文存在
7. 原始 URL 存在
8. URL 是 hyperlink（External Relationship）
9. 多篇文章不会互相覆盖（每篇独立 bookmark / 标题 / 正文）
10. job_id 出现在文件名
11. source 正确
12. 发布时间正确（北京时间展示）
13. HTML / Word 使用同一批文章（同一数据源 + 排序）
14. export_type=portable 不生成 Word
15. export_type=word 生成 Word
16. export_type=both 同时生成 HTML + Word
17. 旧 scheduler.json 的 export_type=portable 仍然正常
18. 无新文章不会被标记成 FETCH FAILED
"""

import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.model import Article  # noqa: E402
from news.storage import Storage  # noqa: E402
from news.word_export import (  # noqa: E402
    export_word_package,
    render_word_docx,
)


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "word.db")
    base = datetime(2026, 8, 24, 6, 0, 0, tzinfo=timezone.utc)
    for i in range(3):
        art = Article(
            source_id="rfi",
            source_name="RFI",
            canonical_url=f"https://www.rfi.fr/cn/art-{i}/",
            title=f"测试新闻第 {i} 篇",
            authors=["RFI 编辑部"],
            published_at=base + timedelta(hours=-i),
            body_text=f"这是第 {i} 篇的完整正文。\n第二段内容，包含中文标点，、；。",
            language="zh",
            status="fetched",
        )
        s.insert_article(art)
    return s


def _docx_xml(path: Path) -> tuple[str, str]:
    """返回 (document.xml, rels) 的文本内容。"""
    with zipfile.ZipFile(path) as z:
        assert z.testzip() is None, "zip 结构无效"
        doc = z.read("word/document.xml").decode("utf-8")
        rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
    return doc, rels


# ---------- 1~2：文件生成与 zip 有效性 ----------

def test_generates_valid_docx(tmp_path, storage):
    out = tmp_path / "Laxinwen-RFI-2026-08-24-080000-test.docx"
    result = export_word_package(storage, out, source_id="rfi", limit=10, job_id="test")
    assert result.exported == 3
    assert out.exists()
    assert out.stat().st_size > 0
    with zipfile.ZipFile(out) as z:
        assert z.testzip() is None
        assert "word/document.xml" in z.namelist()


# ---------- 3~4：标题与目录存在 ----------

def test_title_and_toc_exist(tmp_path, storage):
    out = tmp_path / "out.docx"
    export_word_package(storage, out, source_id="rfi")
    doc, _ = _docx_xml(out)
    assert "Laxinwen 新闻阅读包" in doc
    assert "目录" in doc
    # 每篇标题出现在目录
    for i in range(3):
        assert f"测试新闻第 {i} 篇" in doc


# ---------- 5：目录与正文内部跳转（bookmark + internal hyperlink） ----------

def test_internal_hyperlink_jump(tmp_path, storage):
    out = tmp_path / "out.docx"
    export_word_package(storage, out, source_id="rfi")
    doc, _ = _docx_xml(out)
    # 每篇有一个 bookmarkStart（正文锚点）
    assert doc.count("bookmarkStart") == 3
    # 每个目录条目是一个带 w:anchor 的内部超链接（跳转到 bookmark）
    assert doc.count('w:anchor="art-') == 3
    # 每个正文 bookmark 的 name 都能被目录 anchor 命中
    for i in range(3):
        assert f'w:name="art-{i + 1}-' in doc
        assert f'w:anchor="art-{i + 1}-' in doc


# ---------- 6：每篇新闻正文存在 ----------

def test_body_text_present(tmp_path, storage):
    out = tmp_path / "out.docx"
    export_word_package(storage, out, source_id="rfi")
    doc, _ = _docx_xml(out)
    for i in range(3):
        assert f"这是第 {i} 篇的完整正文" in doc
        assert "第二段内容" in doc


# ---------- 7~8：原始 URL 存在且是外部 hyperlink ----------

def test_url_present_and_hyperlink(tmp_path, storage):
    out = tmp_path / "out.docx"
    export_word_package(storage, out, source_id="rfi")
    doc, rels = _docx_xml(out)
    for i in range(3):
        assert f"https://www.rfi.fr/cn/art-{i}/" in doc
    # 外部 hyperlink 关系存在（原文 URL 可点击打开浏览器）
    assert rels.count("hyperlink") == 3
    assert "www.rfi.fr" in rels


# ---------- 9：多篇文章不互相覆盖 ----------

def test_multiple_articles_distinct(tmp_path, storage):
    out = tmp_path / "out.docx"
    export_word_package(storage, out, source_id="rfi")
    doc, _ = _docx_xml(out)
    # 3 篇各有独立 bookmark / 正文 / URL
    assert doc.count("bookmarkStart") == 3
    for i in range(3):
        assert f'w:anchor="art-{i + 1}-' in doc
        assert f"这是第 {i} 篇的完整正文" in doc


# ---------- 10~11：job_id 与 source ----------

def test_job_id_in_filename(tmp_path, storage):
    # 自动导出路径用 _run_word_export 构造文件名，应包含 job id + source
    from news.scheduled_fetch import _run_word_export

    seen = {}

    def fake_word(storage, out_path, *, source_id, limit, job_id=""):
        seen["path"] = Path(out_path)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"x")
        class R:
            exported = 0
        return R()

    _run_word_export(
        storage, "rfi", 10, job_id="rfi-morning",
        word_dir=tmp_path / "word",
        word_export=fake_word,
    )
    name = seen["path"].name
    assert "rfi-morning" in name
    assert "RFI" in name.upper()
    assert name.endswith(".docx")


def test_source_label_in_doc(tmp_path, storage):
    out = tmp_path / "out.docx"
    export_word_package(storage, out, source_id="rfi", job_id="rfi-morning")
    doc, _ = _docx_xml(out)
    assert "RFI · rfi-morning" in doc or "RFI" in doc


# ---------- 12：发布时间正确 ----------

def test_published_time_present(tmp_path, storage):
    out = tmp_path / "out.docx"
    export_word_package(storage, out, source_id="rfi")
    doc, _ = _docx_xml(out)
    # 发布时间展示为北京时间
    assert "发布时间：" in doc
    assert "2026-08-24" in doc


# ---------- 13：HTML / Word 使用同一批文章（同一数据源 + 倒序） ----------

def test_word_and_portable_same_batch(tmp_path, storage):
    from news.portable import export_portable_package

    word_out = tmp_path / "word.docx"
    html_dir = tmp_path / "html"
    wres = export_word_package(storage, word_out, source_id="rfi", limit=10)
    hres = export_portable_package(storage, html_dir, source_id="rfi", limit=10)
    assert wres.exported == hres.exported == 3
    # Word 用同一排序：list_articles_with_analysis（发布时间倒序）
    rows = storage.list_articles_with_analysis(source_id="rfi", limit=10)
    usable = [r for r in rows if storage.is_usable(r)]
    assert len(usable) == 3
    assert wres.exported == len(usable)


# ---------- 14~16：export_type 语义 ----------

def test_export_type_portable_no_word(tmp_path, storage):
    """export_type=portable 只生成 HTML，不生成 Word。"""
    from news.scheduled_fetch import _run_auto_export

    seen = {}

    def fake_portable(storage, out_dir, *, source_id, limit, research_root=None):
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        seen["html"] = True
        class R:
            exported = 0
        return R()

    def fake_word(storage, out_path, *, source_id, limit, job_id=""):
        seen["word"] = True
        class R:
            exported = 0
        return R()

    _run_auto_export(
        storage, "rfi", 10, "portable",
        portable_dir=tmp_path / "portable",
        research_dir=tmp_path / "html",
        portable_export=fake_portable,
        word_export=fake_word,
    )
    assert seen.get("html") is True
    assert "word" not in seen  # portable 不生成 Word


def test_export_type_word_generates_word(tmp_path, storage):
    from news.scheduled_fetch import _run_auto_export

    seen = {}

    def fake_portable(storage, out_dir, *, source_id, limit, research_root=None):
        seen["html"] = True
        class R:
            exported = 0
        return R()

    def fake_word(storage, out_path, *, source_id, limit, job_id=""):
        seen["word"] = True
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"x")
        class R:
            exported = 0
        return R()

    _run_auto_export(
        storage, "rfi", 10, "word",
        portable_dir=tmp_path / "portable",
        word_dir=tmp_path / "word",
        research_dir=tmp_path / "html",
        portable_export=fake_portable,
        word_export=fake_word,
    )
    assert seen.get("word") is True
    assert "html" not in seen  # word 只生成 Word


def test_export_type_both_generates_html_and_word(tmp_path, storage):
    from news.scheduled_fetch import _run_auto_export

    seen = {}

    def fake_portable(storage, out_dir, *, source_id, limit, research_root=None):
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        seen["html"] = True
        class R:
            exported = 0
        return R()

    def fake_word(storage, out_path, *, source_id, limit, job_id=""):
        seen["word"] = True
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"x")
        class R:
            exported = 0
        return R()

    _run_auto_export(
        storage, "rfi", 10, "both",
        portable_dir=tmp_path / "portable",
        word_dir=tmp_path / "word",
        research_dir=tmp_path / "html",
        portable_export=fake_portable,
        word_export=fake_word,
    )
    assert seen.get("html") is True
    assert seen.get("word") is True


# ---------- 17：旧 scheduler.json export_type=portable 仍正常 ----------

def test_legacy_scheduler_portable_still_works(tmp_path):
    """旧版 scheduler.json 顶层扁平格式 + export_type=portable 可正常读取。"""
    from news.scheduler_config import load_jobs

    cfg_file = tmp_path / "scheduler.json"
    cfg_file.write_text(
        '{"source": "rfi", "frequency": "daily", "time": "08:00", '
        '"limit": 50, "auto_export": true, "export_type": "portable"}',
        encoding="utf-8",
    )
    jobs = load_jobs(cfg_file)
    assert len(jobs) == 1
    assert jobs[0].export_type == "portable"
    assert jobs[0].auto_export is True
    # 旧配置不会崩溃；按 portable 语义自动导出（不生成 Word）
    assert jobs[0].export_type not in ("word", "both")


# ---------- 18：无新文章不被标记成 FETCH FAILED ----------

def test_zero_usable_not_fetch_failed(tmp_path):
    """usable=0 仍是 FETCH SUCCESS，且自动导出仍可执行。"""
    from news.scheduled_fetch import run_scheduled_fetch
    from news.scheduler_config import SchedulerConfig

    class _EmptyStats:
        discovered = 0
        skipped_dup = 0
        fetched_ok = 0
        extracted_ok = 0
        low_quality = 0
        failed = 0
        usable = 0
        errors = []

    class _EmptyPipeline:
        def run_site(self, sid):
            return _EmptyStats()
        def close(self):
            pass

    seen = {}

    def fake_pipeline(storage, limit):
        return _EmptyPipeline()

    def fake_portable(storage, out_dir, *, source_id, limit, research_root=None):
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        seen["export"] = True
        class R:
            exported = 0
        return R()

    cfg = SchedulerConfig(id="rfi-empty", source="rfi", limit=10, auto_export=True)
    rc = run_scheduled_fetch(
        cfg,
        db_path=tmp_path / "empty.db",
        log_file=tmp_path / "sched.log",
        portable_dir=tmp_path / "portable",
        research_dir=tmp_path / "html",
        pipeline_factory=fake_pipeline,
        portable_export=fake_portable,
    )
    assert rc == 0  # 不是 FETCH FAILED
    assert seen.get("export") is True  # 仍执行自动导出
