"""扫描 / 识别 / 编号 / 计划测试（对应需求六～十一、二十一、三十七）。"""

from __future__ import annotations

from datetime import date

import pytest

from news.research.archive_store import ResearchArchiveStore
from news.research.config import SUPPORTED_EXTENSIONS, ResearchArchiveConfig
from news.research.dates import detect_date_from_filename
from news.research.scanner import (
    ResearchArchiveScanner,
    extract_topic,
)

from research_helpers import make_docx, make_html, make_pdf, write_bytes


@pytest.fixture()
def config(tmp_path):
    cfg = ResearchArchiveConfig(
        inbox_dir=tmp_path / "inbox",
        archive_dir=tmp_path / "archive",
        failed_dir=tmp_path / "failed",
        db_path=tmp_path / "research.db",
        companies_file=None,
        dry_run=True,
    )
    cfg.ensure_dirs()
    return cfg


@pytest.fixture()
def store(config):
    with ResearchArchiveStore(config.db_path) as instance:
        yield instance


def make_inbox(config, name: str, kind: str = "html", **kwargs):
    path = config.inbox_dir / name
    if kind == "html":
        return make_html(path, **kwargs)
    if kind == "docx":
        return make_docx(path, **kwargs)
    if kind == "pdf":
        return make_pdf(path, **kwargs)
    return write_bytes(path, kwargs.get("size", 100))


# ---------- 只处理最终成果 ----------

def test_only_supported_extensions_are_scanned(config, store):
    make_html(config.inbox_dir / "研究成果.html")
    make_html(config.inbox_dir / "旧页面.htm")
    make_docx(config.inbox_dir / "报告.docx")
    make_pdf(config.inbox_dir / "报告.pdf")
    (config.inbox_dir / "草稿.txt").write_text("draft", encoding="utf-8")
    (config.inbox_dir / "笔记.md").write_text("# note", encoding="utf-8")
    (config.inbox_dir / "截图.png").write_bytes(b"\x89PNG")
    scanner = ResearchArchiveScanner(config, store=store, read_content=False)
    names = [path.name for path in scanner.iter_inbox_files()]
    assert names == ["报告.docx", "报告.pdf", "研究成果.html", "旧页面.htm", "笔记.md", "草稿.txt"] or sorted(names) == sorted(
        ["报告.docx", "报告.pdf", "研究成果.html", "旧页面.htm", "笔记.md", "草稿.txt"]
    )
    assert all(path.suffix.lower() in SUPPORTED_EXTENSIONS for path in scanner.iter_inbox_files())


def test_temp_and_hidden_files_are_skipped(config, store):
    make_html(config.inbox_dir / "结果.html")
    (config.inbox_dir / "~$结果.docx").write_bytes(b"lock")
    (config.inbox_dir / ".hidden.html").write_text("<html></html>", encoding="utf-8")
    (config.inbox_dir / "下载中.html.part").write_bytes(b"partial")
    (config.inbox_dir / "崩溃恢复.html.crdownload").write_bytes(b"partial")
    scanner = ResearchArchiveScanner(config, store=store, read_content=False)
    assert [path.name for path in scanner.iter_inbox_files()] == ["结果.html"]


def test_recursive_is_opt_in(config, store):
    sub = config.inbox_dir / "sub"
    sub.mkdir()
    make_html(sub / "深层.html")
    assert ResearchArchiveScanner(config, store=store, read_content=False).iter_inbox_files() == []
    config.recursive = True
    assert len(ResearchArchiveScanner(config, store=store, read_content=False).iter_inbox_files()) == 1


# ---------- 编号：日期 + 公司 独立 ----------

def test_letters_restart_per_company_same_day(config, store):
    make_html(config.inbox_dir / "Claude 天齐锂业研究.html")
    make_html(config.inbox_dir / "ChatGPT 天齐锂业锂价分析.html")
    make_html(config.inbox_dir / "Gemini NVDA研究.html")
    make_html(config.inbox_dir / "Claude KAP成本曲线.html")
    scanner = ResearchArchiveScanner(config, store=store, read_content=False)
    candidates = scanner.scan()
    by_dir: dict[str, list[str]] = {}
    for candidate in candidates:
        by_dir.setdefault(candidate.company_dir, []).append(candidate.letter)
    assert sorted(by_dir["09696.HK_天齐锂业"]) == ["A", "B"]
    assert by_dir["NVDA_NVIDIA"] == ["A"]
    assert by_dir["KAP_Kazatomprom"] == ["A"]


def test_letters_continue_from_existing_archive_files(config, store):
    directory = config.archive_dir / date.today().isoformat() / "09696.HK_天齐锂业"
    directory.mkdir(parents=True)
    (directory / f"{date.today():%Y%m%d}A_Claude_09696.HK_天齐锂业_旧研究.pdf").write_bytes(b"%PDF")
    (directory / f"{date.today():%Y%m%d}B_Claude_09696.HK_天齐锂业_旧研究2.pdf").write_bytes(b"%PDF")
    make_html(config.inbox_dir / "ChatGPT 天齐锂业新研究.html")
    candidate = ResearchArchiveScanner(config, store=store, read_content=False).scan()[0]
    assert candidate.letter == "C"


def test_letter_sequence_beyond_z(store, tmp_path):
    from news.research.sanitize import index_to_letters

    assert index_to_letters(26) == "Z"
    assert index_to_letters(27) == "AA"
    assert index_to_letters(28) == "AB"


# ---------- 命名 ----------

def test_normalized_filename_shape(config, store):
    make_html(config.inbox_dir / "Claude 天齐锂业深度研究.html", title="深度投资研究")
    candidate = ResearchArchiveScanner(config, store=store, read_content=False).scan()[0]
    assert candidate.normalized_name.startswith(f"{date.today():%Y%m%d}A_Claude_09696.HK_天齐锂业_")
    assert candidate.normalized_name.endswith(".html")
    assert " " not in candidate.normalized_name
    assert candidate.archive_dir.name == "09696.HK_天齐锂业"
    assert candidate.archive_path.parent == candidate.archive_dir
    assert candidate.archive_dir.parent.name == date.today().isoformat()


def test_unknown_ai_and_company_are_marked(config, store):
    make_html(config.inbox_dir / "行业研究.html", body="普通研究正文", title="行业研究")
    candidate = ResearchArchiveScanner(config, store=store, read_content=False).scan()[0]
    assert candidate.ai_source == "Unknown"
    assert candidate.ticker == "Unknown"
    assert candidate.company == "Unknown"
    # 未识别身份时直接挂在日期目录，不生成 Unknown 公司目录。
    assert candidate.company_dir == candidate.date


def test_illegal_chars_in_filename_are_cleaned(config, store):
    # 注意：``/`` 在真实文件系统里是路径分隔符，无法出现在文件名中，
    # 因此这里用可写入的非法集合（: * ? " < > |）做端到端验证，
    # 全字符集由 tests/test_research_sanitize.py 覆盖。
    make_html(config.inbox_dir / 'Claude 天齐锂业 研究：2026*预测？"x".html')
    candidate = ResearchArchiveScanner(config, store=store, read_content=False).scan()[0]
    for char in ':*?"<>|':
        assert char not in candidate.normalized_name
    # 文件名里没有 ticker 时，公司名仍必须命中（别名层）
    assert candidate.ticker == "09696.HK"
    assert candidate.company == "天齐锂业"
    assert candidate.normalized_name.startswith(
        f"{date.today():%Y%m%d}A_Claude_09696.HK_天齐锂业_"
    )


# ---------- 主题提取 ----------

@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("20260912A_Claude_09696.HK_天齐锂业_锂价分析.pdf", "锂价分析"),
        ("Claude 天齐锂业深度投资研究.pdf", "深度投资研究"),
        ("Gemini KAP成本曲线.html", "成本曲线"),
    ],
)
def test_extract_topic(filename, expected):
    ticker = "09696.HK" if "09696" in filename else ("KAP" if "KAP" in filename else "")
    company = "天齐锂业" if "天齐锂业" in filename else ("Kazatomprom" if "KAP" in filename else "")
    ai_source = "Claude" if "Claude" in filename else ("Gemini" if "Gemini" in filename else "")
    assert extract_topic(filename, ai_source=ai_source, ticker=ticker, company=company) == expected


# ---------- 日期 ----------

@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("20260912 Claude 天齐锂业.pdf", date(2026, 9, 12)),
        ("2026-09-12 天齐.pdf", date(2026, 9, 12)),
        ("研究_2026_09_12.pdf", date(2026, 9, 12)),
        ("2026年09月12日 研究.pdf", date(2026, 9, 12)),
        ("没有日期.pdf", None),
    ],
)
def test_detect_date_from_filename(filename, expected):
    assert detect_date_from_filename(filename) == expected


def test_date_falls_back_to_file_times(config, store):
    make_html(config.inbox_dir / "没有日期.html")
    candidate = ResearchArchiveScanner(config, store=store, read_content=False).scan()[0]
    assert candidate.date == date.today().isoformat()


# ---------- 去重与计划 ----------

def test_duplicate_detection_uses_sha256(config, store):
    content = "<html><body>Claude 天齐锂业研究</body></html>"
    (config.inbox_dir / "Claude 天齐锂业研究.html").write_text(content, encoding="utf-8")
    scanner = ResearchArchiveScanner(config, store=store, read_content=False)
    first = scanner.scan()[0]
    # 写入数据库模拟「已处理」
    store.upsert(first.record(status="ARCHIVED"))
    # 同名内容不同文件名 → 仍然是重复
    (config.inbox_dir / "Claude 天齐锂业研究最终版.html").write_text(content, encoding="utf-8")
    candidates = scanner.scan()
    duplicates = [item for item in candidates if item.duplicate]
    assert len(duplicates) == 2, [item.path.name for item in candidates]
    assert {item.path.name for item in duplicates} == {
        "Claude 天齐锂业研究.html",          # 已入库的同内容文件
        "Claude 天齐锂业研究最终版.html",     # 同内容不同文件名 → 仍判重复
    }
    assert all(item.duplicate_of for item in duplicates)


def test_dry_run_plan_mentions_no_side_effects(config, store):
    make_html(config.inbox_dir / "Claude 天齐锂业研究.html")
    scanner = ResearchArchiveScanner(config, store=store, read_content=False)
    lines = scanner.format_plan(scanner.scan())
    text = "\n".join(lines)
    assert "DRY-RUN" in text
    assert "未移动 / 未重命名 / 未上传任何文件" in text
    assert "09696.HK_天齐锂业" in text


def test_max_files_limit(config, store):
    for index in range(5):
        make_html(config.inbox_dir / f"Claude 天齐锂业研究{index}.html")
    config.max_files = 2
    assert len(ResearchArchiveScanner(config, store=store, read_content=False).iter_inbox_files()) == 2


def test_empty_inbox_returns_no_candidates(config, store):
    scanner = ResearchArchiveScanner(config, store=store, read_content=False)
    assert scanner.scan() == []
    assert "没有需要处理的" in "\n".join(scanner.format_plan([]))


def test_missing_inbox_directory_is_tolerated(tmp_path, store):
    cfg = ResearchArchiveConfig(
        inbox_dir=tmp_path / "does-not-exist",
        archive_dir=tmp_path / "archive",
        failed_dir=tmp_path / "failed",
        db_path=tmp_path / "db.sqlite",
        companies_file=None,
    )
    scanner = ResearchArchiveScanner(cfg, store=store, read_content=False)
    assert scanner.iter_inbox_files() == []
