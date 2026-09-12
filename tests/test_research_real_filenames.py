"""真实验收测试：用 Issue 里给出的**真实文件名**端到端验证修复。

对应 Issue #1 第五阶段。这些用例全部使用真实文件名（含 GENIMI 拼写、
中文书名号 / 全角竖线、SK海力士、000660.KS 等），并**不依赖**任何
``companies.json`` 的存在，用来证明：

1. ticker 独立识别 —— ``000660.KS`` 即使映射表缺失也必须是 ``000660.KS``；
2. 公司识别优先级 ticker 标准映射 → aliases → 文件名公司名 → 内容 → Unknown；
3. ``GENIMI`` → ``Gemini``、``CLAUDE`` → ``Claude``；
4. 同一天两个公司分别从 A 开始编号；
5. 日期 source 正确（filename / metadata / modified），ctime 不作为来源；
6. 日志不重复（每条信息只输出一次）。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

import pytest

from news.research.ai_source import detect_ai_source
from news.research.company import (
    UNKNOWN_COMPANY,
    UNKNOWN_TICKER,
    CompanyDirectory,
)
from news.research.config import ResearchArchiveConfig
from news.research.dates import detect_date
from news.research.pipeline import run_research_archive
from news.research.sanitize import sanitize_filename
from news.research.scanner import ResearchArchiveScanner

from research_helpers import make_docx, make_html, make_pdf


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def config(tmp_path):
    cfg = ResearchArchiveConfig(
        inbox_dir=tmp_path / "inbox",
        archive_dir=tmp_path / "archive",
        failed_dir=tmp_path / "failed",
        db_path=tmp_path / "research.db",
        companies_file=None,  # 关键：不加载任何映射表文件
        dry_run=True,
    )
    cfg.ensure_dirs()
    return cfg


def _mk(config, name: str, kind: str = "pdf"):
    path = config.inbox_dir / name
    if kind == "pdf":
        return make_pdf(path)
    if kind == "docx":
        return make_docx(path)
    return make_html(path)


def _scan(config, **kwargs) -> dict[str, object]:
    scanner = ResearchArchiveScanner(config, read_content=True, **kwargs)
    return {candidate.path.name: candidate for candidate in scanner.scan()}


# ------------------------------------------------ 1. 真实文件名逐一验收

@pytest.mark.parametrize(
    ("filename", "ticker", "company", "ai"),
    [
        ("20260912 GENIMI 09696.HK 天齐锂业.pdf", "09696.HK", "天齐锂业", "Gemini"),
        ("20260912A CLAUDE 000660.KS SK海力士.pdf", "000660.KS", "SK海力士", "Claude"),
        ("20260912A GENIMI 000660.KS SK海力士.pdf", "000660.KS", "SK海力士", "Gemini"),
        ("20260912B GENIMI 000660.KS SK海力士.pdf", "000660.KS", "SK海力士", "Gemini"),
    ],
)
def test_real_pdf_filenames(config, filename, ticker, company, ai):
    """Issue 第五阶段 1～4 号真实文件（映射表缺失也必须正确）。"""
    _mk(config, filename)
    candidate = _scan(config)[filename]

    assert candidate.ticker == ticker
    assert candidate.company == company
    assert candidate.ai_source == ai
    # ticker 必须独立成立，不能被降级为 Unknown
    assert candidate.ticker != UNKNOWN_TICKER


def test_real_docx_with_brackets_and_pipe(config):
    """Issue 第五阶段 5 号真实文件：特殊字符清理 + ticker 保留。"""
    filename = "「天齐锂业｜09696.HK｜锂价周期研究」.docx"
    _mk(config, filename, "docx")
    candidate = _scan(config)[filename]

    assert candidate.ticker == "09696.HK"
    assert candidate.company == "天齐锂业"

    # 特殊字符清理正确：这些字符一个都不能残留
    for char in "「」｜《》【】（）[]：:/*?\"<>":
        assert char not in candidate.normalized_name, f"{char!r} 未被清理"
    # 干净文件名示例形态：YYYYMMDD序号_AI_代码_公司_主题.docx
    # 注意：该文件名的「」里没有日期，日期来自 mtime（本 Runner 时钟），
    # 因此这里只断言「8 位日期 + 序号」结构，不绑定某个具体日期。
    assert candidate.normalized_name.endswith(".docx")
    assert re.match(r"^\d{8}[A-Z]+_", candidate.normalized_name), candidate.normalized_name
    assert "_09696.HK_天齐锂业_" in candidate.normalized_name
    # ticker 里的点号必须保留（没有被 sanitize 破坏）
    assert "." in candidate.normalized_name


# --------------------------------------- 2. ticker 独立于 companies.json

@pytest.mark.parametrize(
    ("filename", "ticker"),
    [
        ("000660.KS SK海力士.pdf", "000660.KS"),
        ("09696.HK 天齐锂业.pdf", "09696.HK"),
        ("600519.SH 贵州茅台.pdf", "600519.SH"),
        ("NVDA 英伟达.pdf", "NVDA"),
        ("KAP Kazatomprom.pdf", "KAP"),
    ],
)
def test_ticker_is_independent_of_companies_json(filename, ticker):
    """需求：文件名的 ticker 必须独立识别，companies.json 缺失也不能变 Unknown。"""
    empty = CompanyDirectory(records=[])  # 空映射表 == 映射表缺失
    match = empty.detect(filename)
    assert match.ticker == ticker
    assert match.ticker != UNKNOWN_TICKER


def test_ticker_stands_alone_even_when_company_is_unknown():
    """映射表完全没有 000660.KS 时：ticker 有值、company 可为 Unknown。"""
    empty = CompanyDirectory(records=[])
    match = empty.detect("000660.KS 未知公司.pdf")
    assert match.ticker == "000660.KS"
    assert match.ticker_known is True


def test_unknown_mapping_still_yields_ticker_and_known_company_name(config):
    """没有映射表时，文件名里的公司名也要尽量保留（而不是丢成 Unknown）。"""
    _mk(config, "000660.KS SK海力士.pdf")
    candidate = _scan(config)["000660.KS SK海力士.pdf"]
    assert candidate.ticker == "000660.KS"
    assert candidate.company == "SK海力士"


# ----------------------------------------------- 3. AI 来源拼写归一

@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("20260912 GENIMI 09696.HK 天齐锂业.pdf", "Gemini"),
        ("20260912A GENIMI 000660.KS SK海力士.pdf", "Gemini"),
        ("20260912A CLAUDE 000660.KS SK海力士.pdf", "Claude"),
        ("20260912B CLAUDE 000660.KS SK海力士.pdf", "Claude"),
        ("20260912 CHATGPT 600519.SH 贵州茅台.pdf", "ChatGPT"),
    ],
)
def test_ai_source_spelling_normalisation(filename, expected):
    """``GENIMI`` → ``Gemini``、``CLAUDE`` → ``Claude``。"""
    assert detect_ai_source(filename).source == expected


# ------------------------------- 4. 同一天两个公司分别从 A 开始编号

def test_two_companies_on_same_day_each_start_at_a(config):
    """同一天：天齐锂业 A/B，SK海力士 A/B/C，各自独立编号。"""
    _mk(config, "20260912 GENIMI 09696.HK 天齐锂业.pdf")
    _mk(config, "20260912 CLAUDE 09696.HK 天齐锂业.pdf")
    _mk(config, "20260912A GENIMI 000660.KS SK海力士.pdf")
    _mk(config, "20260912A CLAUDE 000660.KS SK海力士.pdf")
    _mk(config, "20260912B GENIMI 000660.KS SK海力士.pdf")

    candidates = list(_scan(config).values())

    tianqi = sorted(c.letter for c in candidates if c.ticker == "09696.HK")
    hynix = sorted(c.letter for c in candidates if c.ticker == "000660.KS")

    assert tianqi == ["A", "B"], f"天齐锂业编号应独立从 A 开始：{tianqi}"
    assert hynix == ["A", "B", "C"], f"SK海力士编号应独立从 A 开始：{hynix}"

    # 目录也必须按 日期/代码_公司 分开
    for candidate in candidates:
        assert candidate.archive_dir.parent.name == "2026-09-12"
        assert candidate.company_dir in {"09696.HK_天齐锂业", "000660.KS_SK海力士"}

    # 编号前缀与日期一致
    for candidate in candidates:
        assert candidate.normalized_name.startswith(f"20260912{candidate.letter}_")


def test_same_company_second_file_gets_next_letter(config):
    """同一公司同一天：文件名里已写的 A/B 不能造成重复编号。"""
    _mk(config, "20260912A GENIMI 000660.KS SK海力士.pdf")
    _mk(config, "20260912A CLAUDE 000660.KS SK海力士.pdf")
    letters = sorted(c.letter for c in _scan(config).values())
    assert letters == ["A", "B"]


# ------------------------------------------------ 5. 日期 source 正确

def test_date_source_filename_wins(config):
    """文件名里有日期 → source=filename。"""
    _mk(config, "20260912 GENIMI 09696.HK 天齐锂业.pdf")
    candidate = _scan(config)["20260912 GENIMI 09696.HK 天齐锂业.pdf"]
    assert candidate.date == "2026-09-12"
    assert candidate.date_source == "filename"

    line = candidate.plan_line(1)
    assert "date=2026-09-12(source=filename)" in line


def test_date_source_metadata_when_filename_has_none(tmp_path):
    """文件名无日期 → 用内嵌元数据 → source=metadata。"""
    path = tmp_path / "metadata.pdf"
    make_pdf(path)
    match = detect_date(
        path,
        filename="天齐锂业 锂价研究.pdf",
        metadata={"creation_date": "20260904"},
    )
    assert match.matched_by == "metadata"
    assert match.date.isoformat() == "2026-09-04"
    assert f"date={match.date.isoformat()}(source={match.matched_by})" == (
        "date=2026-09-04(source=metadata)"
    )


def test_date_source_modified_when_no_filename_or_metadata(tmp_path):
    """文件名与元数据都没有日期 → mtime → source=modified。"""
    path = tmp_path / "报告.pdf"
    make_pdf(path)
    match = detect_date(path, filename="研究报告.pdf", metadata=None)
    assert match.matched_by == "modified"
    assert match.date.isoformat() == datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()


def test_ctime_is_never_used_as_date_source(tmp_path, monkeypatch):
    """ctime 明确不作为日期来源（即使它离得很近也不会被采用）。"""
    path = tmp_path / "无日期.pdf"
    make_pdf(path)

    real_stat = path.stat()

    class FakeStat:
        st_mtime = real_stat.st_mtime
        st_ctime = real_stat.st_mtime + 86400 * 365  # 差一年
        st_size = real_stat.st_size
        st_mode = real_stat.st_mode

    monkeypatch.setattr(type(path), "stat", lambda self, **kw: FakeStat(), raising=False)
    match = detect_date(path, filename="无日期.pdf", metadata=None)
    assert match.matched_by == "modified"
    assert match.date.isoformat() == datetime.fromtimestamp(real_stat.st_mtime).date().isoformat()
    assert match.date.isoformat() != datetime.fromtimestamp(FakeStat.st_ctime).date().isoformat()


# ------------------------------------------------ 6. 日志不重复

def test_logs_are_not_duplicated(config, caplog):
    """每条信息只输出一次：``发现文件：5`` 只能出现一次。"""
    for name in (
        "20260912 GENIMI 09696.HK 天齐锂业.pdf",
        "20260912A CLAUDE 000660.KS SK海力士.pdf",
        "20260912A GENIMI 000660.KS SK海力士.pdf",
        "20260912B GENIMI 000660.KS SK海力士.pdf",
    ):
        _mk(config, name)
    _mk(config, "「天齐锂业｜09696.HK｜锂价周期研究」.docx", "docx")

    seen: list[str] = []
    with caplog.at_level(logging.DEBUG):
        result = run_research_archive(config=config, on_message=seen.append)

    assert result.scanned == 5

    # 1) 回调里「发现文件：5」只能出现一次
    discovery = [line for line in seen if "发现文件" in line]
    assert len(discovery) == 1, f"发现文件 出现了 {len(discovery)} 次：{discovery}"
    assert "发现文件：5" in discovery[0]

    # 2) 整个回调消息列表不能有完全重复的行
    non_empty = [line for line in seen if line.strip()]
    duplicates = {line for line in non_empty if non_empty.count(line) > 1}
    assert not duplicates, f"重复日志：{duplicates}"

    # 3) logger 也不能与回调重复输出同样的内容
    logged = [record.getMessage() for record in caplog.records]
    for line in seen:
        if line.strip():
            assert line not in logged, f"logger 与 print 重复输出：{line!r}"


# ------------------------------------------------- 7. 综合验收

def test_all_real_files_end_to_end(config):
    """5 个真实文件一次性扫描：ticker / 公司 / AI / 日期 / 编号全部正确。"""
    expected = {
        "20260912 GENIMI 09696.HK 天齐锂业.pdf": ("09696.HK", "天齐锂业", "Gemini"),
        "20260912A CLAUDE 000660.KS SK海力士.pdf": ("000660.KS", "SK海力士", "Claude"),
        "20260912A GENIMI 000660.KS SK海力士.pdf": ("000660.KS", "SK海力士", "Gemini"),
        "20260912B GENIMI 000660.KS SK海力士.pdf": ("000660.KS", "SK海力士", "Gemini"),
        "「天齐锂业｜09696.HK｜锂价周期研究」.docx": ("09696.HK", "天齐锂业", None),
    }
    for name in expected:
        _mk(config, name, "docx" if name.endswith(".docx") else "pdf")

    candidates = _scan(config)
    assert set(candidates) == set(expected)

    for name, (ticker, company, ai) in expected.items():
        candidate = candidates[name]
        assert candidate.ticker == ticker, name
        assert candidate.company == company, name
        if ai is not None:
            assert candidate.ai_source == ai, name
        # 含日期的文件名 → filename；「」文件名本身没有日期 → mtime
        expected_source = "modified" if name.startswith("「") else "filename"
        assert candidate.date_source == expected_source, name
        if expected_source == "filename":
            assert candidate.date == "2026-09-12", name
        else:
            # mtime 由本 Runner 时钟决定，只校验格式与一致性
            assert re.match(r"^\d{4}-\d{2}-\d{2}$", candidate.date), name
            assert candidate.normalized_name.startswith(candidate.date_prefix), name
        assert candidate.ticker != UNKNOWN_TICKER, name
        assert candidate.company != UNKNOWN_COMPANY, name
        assert "Unknown_Unknown" not in str(candidate.archive_path), name


def test_filename_sanitize_keeps_ticker_dots():
    """sanitize_filename 必须保留点号，否则 ticker 会被破坏。"""
    assert sanitize_filename("09696.HK") == "09696.HK"
    assert sanitize_filename("000660.KS") == "000660.KS"
    assert sanitize_filename("600519.SH") == "600519.SH"
    assert sanitize_filename("「天齐锂业｜09696.HK｜锂价周期研究」") == (
        "天齐锂业_09696.HK_锂价周期研究"
    )
