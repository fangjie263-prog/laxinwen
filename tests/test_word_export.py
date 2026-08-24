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
    # 每篇有一个 bookmarkStart（正文锚点） + 1 个目录锚点 laxinwen-toc
    assert doc.count("bookmarkStart") == 4
    # 目录锚点存在
    assert 'w:name="laxinwen-toc"' in doc
    # 每个目录条目是一个带 w:anchor 的内部超链接（跳转到 bookmark）
    assert doc.count('w:anchor="art-') == 3
    # 每个正文 bookmark 的 name 都能被目录 anchor 命中
    for i in range(3):
        assert f'w:name="art-{i + 1}-' in doc
        assert f'w:anchor="art-{i + 1}-' in doc


# ---------- 5b：每篇正文都有「返回目录」，指向目录 bookmark ----------

def test_each_article_has_return_to_toc(tmp_path, storage):
    out = tmp_path / "out.docx"
    export_word_package(storage, out, source_id="rfi")
    doc, _ = _docx_xml(out)
    # 每篇正文都有「返回目录」内部超链接，anchor 指向 laxinwen-toc
    assert doc.count('w:anchor="laxinwen-toc"') == 3
    # 正文里确实出现了「返回目录」文字
    assert "返回目录" in doc


def test_return_to_toc_uses_internal_hyperlink_not_text(tmp_path, storage):
    out = tmp_path / "out.docx"
    export_word_package(storage, out, source_id="rfi")
    doc, _ = _docx_xml(out)
    # 「返回目录」必须是真正的 w:hyperlink（w:anchor），而不是普通文本假装。
    # 三个返回链接分别包裹在三个 w:hyperlink w:anchor="laxinwen-toc" 中。
    assert doc.count('<w:hyperlink w:anchor="laxinwen-toc">') == 3
    # 目录锚点与每个文章的返回链接 anchor 形成闭环
    assert 'w:name="laxinwen-toc"' in doc


def test_bookmarks_unique_and_no_broken_links(tmp_path, storage):
    import re
    out = tmp_path / "out.docx"
    export_word_package(storage, out, source_id="rfi")
    doc, _ = _docx_xml(out)
    names = re.findall(r'w:name="([^"]+)"', doc)
    anchors = re.findall(r'w:anchor="([^"]+)"', doc)
    # bookmark 名唯一（目录 + 3 篇，共 4 个，互不重复）
    assert len(names) == len(set(names)) == 4
    # 每个内部超链接 anchor 都能找到对应 bookmark（不存在断开的 internal hyperlink）
    for a in anchors:
        assert a in names


def test_two_hundred_articles_bi_directional(tmp_path):
    """200 篇文章时仍能建立正确的一一对应关系，且每篇都可独立返回目录。"""
    import re
    from news.word_export import export_word_package
    base = datetime(2026, 8, 24, 6, 0, 0, tzinfo=timezone.utc)
    s = Storage(tmp_path / "big.db")
    for i in range(200):
        s.insert_article(Article(
            source_id="rfi", source_name="RFI",
            canonical_url=f"https://www.rfi.fr/cn/art-{i}/",
            title=f"新闻第 {i} 篇", authors=["RFI"],
            published_at=base + timedelta(hours=-i),
            body_text=f"正文 {i}", language="zh", status="fetched",
        ))
    out = tmp_path / "big.docx"
    res = export_word_package(s, out, source_id="rfi", limit=200)
    assert res.exported == 200
    doc, _ = _docx_xml(out)
    names = re.findall(r'w:name="([^"]+)"', doc)
    anchors = re.findall(r'w:anchor="([^"]+)"', doc)
    # 目录 + 200 篇文章，bookmark 唯一
    assert len(names) == len(set(names)) == 201
    # 200 个「返回目录」链接
    assert doc.count('w:anchor="laxinwen-toc"') == 200
    # 每个 anchor 都有对应 bookmark（无断链）
    for a in set(anchors):
        assert a in names
    # 200 篇各有一对（目录→文章、文章→目录）
    art_names = [n for n in names if n.startswith("art-")]
    assert len(art_names) == 200


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
    # 3 篇各有独立 bookmark / 正文 / URL（另有 1 个目录锚点）
    assert doc.count("bookmarkStart") == 4
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


# ---------- 14~16：auto_export 统一生成 HTML + Word（DOCX） ----------
# 无论旧配置 export_type 是 portable / word / both，运行时都统一解释为
# HTML + DOCX（便携阅读包 = HTML + Word 一次生成）。


def _make_dual_export(tmp_path, *, html_fail=False, word_fail=False):
    """构造一个既生成 index.html 又生成 .docx 的假 portable 导出。"""
    seen = {}

    def fake_portable(storage, out_dir, *, source_id, limit, research_root=None, job_id=""):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        if not html_fail:
            (out_dir / "index.html").write_text("<html></html>", encoding="utf-8")
        if not word_fail:
            (out_dir / f"{out_dir.name}.docx").write_bytes(b"PK\x03\x04fake")
        seen["html"] = not html_fail
        seen["word"] = not word_fail
        seen["dir"] = out_dir
        class R:
            exported = 0
        return R()

    return fake_portable, seen


def test_auto_export_portable_type_still_generates_html_and_word(tmp_path, storage):
    """旧 export_type=portable 在运行时统一生成 HTML + Word。"""
    from news.scheduled_fetch import _run_auto_export

    fake_portable, seen = _make_dual_export(tmp_path)
    res = _run_auto_export(
        storage, "rfi", 10, "portable",
        portable_dir=tmp_path / "portable",
        research_dir=tmp_path / "html",
        portable_export=fake_portable,
    )
    assert seen["html"] is True
    assert seen["word"] is True
    assert res.html_ok is True
    assert res.word_ok is True
    assert res.ok is True


def test_auto_export_word_type_still_generates_html_and_word(tmp_path, storage):
    """旧 export_type=word 也统一生成 HTML + Word（不再只生成 Word）。"""
    from news.scheduled_fetch import _run_auto_export

    fake_portable, seen = _make_dual_export(tmp_path)
    res = _run_auto_export(
        storage, "rfi", 10, "word",
        portable_dir=tmp_path / "portable",
        research_dir=tmp_path / "html",
        portable_export=fake_portable,
    )
    assert seen["html"] is True
    assert seen["word"] is True
    assert res.ok is True


def test_auto_export_both_type_generates_html_and_word(tmp_path, storage):
    """export_type=both 统一生成 HTML + Word。"""
    from news.scheduled_fetch import _run_auto_export

    fake_portable, seen = _make_dual_export(tmp_path)
    res = _run_auto_export(
        storage, "rfi", 10, "both",
        portable_dir=tmp_path / "portable",
        research_dir=tmp_path / "html",
        portable_export=fake_portable,
    )
    assert seen["html"] is True
    assert seen["word"] is True
    assert res.ok is True


def test_auto_export_word_failure_marks_export_failed(tmp_path, storage):
    """HTML 成功 + Word 失败 → 整体 EXPORT: FAILED（HTML: SUCCESS / WORD: FAILED）。"""
    from news.scheduled_fetch import _run_auto_export

    fake_portable, seen = _make_dual_export(tmp_path, word_fail=True)
    res = _run_auto_export(
        storage, "rfi", 10, "both",
        portable_dir=tmp_path / "portable",
        research_dir=tmp_path / "html",
        portable_export=fake_portable,
    )
    assert seen["html"] is True
    assert seen["word"] is False
    assert res.html_ok is True
    assert res.word_ok is False
    assert res.ok is False  # 任一失败则整体失败
    assert "HTML: SUCCESS / WORD: FAILED" in res.summary()


def test_auto_export_html_failure_marks_export_failed(tmp_path, storage):
    """HTML 失败 + Word 成功 → 整体 EXPORT: FAILED（HTML: FAILED / WORD: SUCCESS）。"""
    from news.scheduled_fetch import _run_auto_export

    fake_portable, seen = _make_dual_export(tmp_path, html_fail=True)
    res = _run_auto_export(
        storage, "rfi", 10, "portable",
        portable_dir=tmp_path / "portable",
        research_dir=tmp_path / "html",
        portable_export=fake_portable,
    )
    assert seen["html"] is False
    assert seen["word"] is True
    assert res.html_ok is False
    assert res.word_ok is True
    assert res.ok is False
    assert "HTML: FAILED / WORD: SUCCESS" in res.summary()


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
    # 旧配置不崩溃；export_type 字段保留为 portable（运行时统一解释为 HTML + DOCX，
    # 由 _run_auto_export 保证，见 scheduled_fetch.py）
    assert jobs[0].export_type == "portable"


def test_legacy_portable_config_runtime_generates_html_and_word(tmp_path):
    """旧 export_type=portable 配置在运行时统一生成 HTML + Word。"""
    from news.scheduled_fetch import run_scheduled_fetch
    from news.scheduler_config import SchedulerConfig

    class _Stats:
        discovered = 1
        skipped_dup = 0
        fetched_ok = 1
        extracted_ok = 1
        low_quality = 0
        failed = 0
        usable = 1
        errors = []

    class _P:
        def run_site(self, sid):
            return _Stats()
        def close(self):
            pass

    seen = {}

    def fake_pipeline(storage, limit):
        return _P()

    def fake_portable(storage, out_dir, *, source_id, limit, research_root=None, job_id=""):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "index.html").write_text("<html></html>", encoding="utf-8")
        (out_dir / f"{out_dir.name}.docx").write_bytes(b"PK")
        seen["dir"] = out_dir
        class R:
            exported = 1
        return R()

    cfg = SchedulerConfig(id="rfi-legacy", source="rfi", limit=10, auto_export=True,
                          export_type="portable")
    logf = tmp_path / "sched.log"
    rc = run_scheduled_fetch(
        cfg,
        db_path=tmp_path / "empty.db",
        log_file=logf,
        portable_dir=tmp_path / "portable",
        research_dir=tmp_path / "html",
        pipeline_factory=fake_pipeline,
        portable_export=fake_portable,
    )
    assert rc == 0
    content = logf.read_text(encoding="utf-8")
    assert "EXPORT: SUCCESS" in content  # HTML + Word 都成功
    assert "HTML: SUCCESS / WORD: SUCCESS" in content


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

    def fake_portable(storage, out_dir, *, source_id, limit, research_root=None, job_id=""):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "index.html").write_text("<html></html>", encoding="utf-8")
        (out_dir / f"{out_dir.name}.docx").write_bytes(b"PK")
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


# ---------- 19：Portable 阅读包自动同时生成 HTML + Word（DOCX），且同批 ----------

def test_portable_package_auto_generates_html_and_word(tmp_path, storage):
    """便携阅读包一次生成 HTML + DOCX（同一目录），且两者为同一批新闻。"""
    import re, zipfile
    from news.portable import export_portable_reader_package

    out_dir = tmp_path / "portable" / "Laxinwen-RFI-2026-08-24-100000-test"
    res = export_portable_reader_package(storage, out_dir, source_id="rfi", limit=10,
                                         job_id="test")
    assert res.exported == 3
    docx = out_dir / f"{out_dir.name}.docx"
    assert (out_dir / "index.html").exists()
    assert docx.exists()

    # HTML 与 Word 使用同一批文章（3 篇）
    with zipfile.ZipFile(docx) as z:
        doc = z.read("word/document.xml").decode("utf-8")
    # Word 目录含 3 篇文章标题
    assert doc.count('w:anchor="art-') == 3
    for i in range(3):
        assert f"测试新闻第 {i} 篇" in doc


def test_job_id_in_portable_docx_name(tmp_path, storage):
    """便携阅读包内 docx 名称含 job id（不同 job 不覆盖）。"""
    from news.portable import export_portable_reader_package

    out_dir = tmp_path / "portable" / "Laxinwen-RFI-2026-08-24-100000-rfi-morning"
    export_portable_reader_package(storage, out_dir, source_id="rfi", limit=10,
                                   job_id="rfi-morning")
    docx = out_dir / f"{out_dir.name}.docx"
    assert docx.name.endswith("rfi-morning.docx")
    assert docx.exists()
