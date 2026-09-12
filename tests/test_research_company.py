"""公司 / 股票代码识别测试（对应需求十七～二十）。"""

from __future__ import annotations

import json

import pytest

from news.research.company import (
    UNKNOWN_COMPANY,
    UNKNOWN_TICKER,
    CompanyDirectory,
    CompanyRecord,
    looks_like_ticker,
    parse_ticker,
)


@pytest.fixture()
def directory() -> CompanyDirectory:
    return CompanyDirectory()


@pytest.mark.parametrize(
    ("filename", "ticker", "company"),
    [
        ("Claude 09696.HK 天齐锂业研究.pdf", "09696.HK", "天齐锂业"),
        ("09696.HK 天齐锂业深度研究.pdf", "09696.HK", "天齐锂业"),
        ("002466 天齐锂业.pdf", "002466", "天齐锂业"),
        ("600519 贵州茅台研究.docx", "600519", "贵州茅台"),
        ("NVDA NVIDIA GPU产业链研究.pdf", "NVDA", "NVIDIA"),
        ("KAP Kazatomprom 铀矿成本曲线.pdf", "KAP", "Kazatomprom"),
    ],
)
def test_ticker_from_filename(directory, filename, ticker, company):
    match = directory.detect(filename)
    assert match.ticker == ticker
    assert match.company == company
    # 阶段二：ticker 由「独立识别」得到，再经映射表拿到规范公司名
    assert match.matched_by in {"filename_ticker", "ticker_mapping"}


@pytest.mark.parametrize(
    ("filename", "ticker", "company"),
    [
        ("天齐锂业研究.pdf", "09696.HK", "天齐锂业"),
        ("Tianqi Lithium report.pdf", "09696.HK", "天齐锂业"),
        ("英伟达研究.pdf", "NVDA", "NVIDIA"),
        ("贵州茅台 估值分析.docx", "600519", "贵州茅台"),
    ],
)
def test_company_name_from_filename(directory, filename, ticker, company):
    match = directory.detect(filename)
    assert match.ticker == ticker
    assert match.company == company
    # 阶段二优先级：ticker 标准映射 → 映射表 aliases → 文件名公司名称
    assert match.matched_by in {"filename_alias", "filename_name", "filename_ticker"}


@pytest.mark.parametrize("filename", ["锂行业研究.pdf", "GPU研究.html", "研究报告.pdf"])
def test_no_company_is_unknown_not_guessed(directory, filename):
    """需求十九：没有明确公司时不允许自动猜一个上市公司。"""
    match = directory.detect(filename)
    assert match.ticker == UNKNOWN_TICKER
    assert match.company == UNKNOWN_COMPANY
    assert not match.known
    # 需求五：两者都 Unknown 时目录名就是 Unknown，不生成 Unknown_Unknown 占位
    assert match.directory_name == "Unknown"


def test_directory_name_format(directory):
    assert directory.detect("NVDA NVIDIA.pdf").directory_name == "NVDA_NVIDIA"
    assert directory.detect("09696.HK 天齐锂业.pdf").directory_name == "09696.HK_天齐锂业"
    assert directory.detect("KAP Kazatomprom.pdf").directory_name == "KAP_Kazatomprom"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("09696.hk", "09696.HK"),
        ("9696.HK", "9696.HK"),
        ("nvda", "NVDA"),
        ("600519.sh", "600519.SH"),
        ("", ""),
    ],
)
def test_parse_ticker(raw, expected):
    assert parse_ticker(raw) == expected


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("09696.HK", True), ("600519", True), ("NVDA", True),
        ("PDF", False), ("AI", False), ("研究报告", False), ("OO", True),
    ],
)
def test_looks_like_ticker(token, expected):
    assert looks_like_ticker(token) is expected


def test_mapping_is_data_driven_from_json(tmp_path):
    """映射表放 JSON，不需要改业务代码即可扩展（需求二十）。"""
    path = tmp_path / "companies.json"
    path.write_text(
        json.dumps(
            {
                "companies": [
                    {
                        "ticker": "TSLA",
                        "company_name": "特斯拉",
                        "aliases": ["Tesla", "TSLA.US"],
                        "market": "US",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    directory = CompanyDirectory.from_file(path)
    match = directory.detect("Claude TSLA 特斯拉研究.pdf")
    assert match.ticker == "TSLA"
    assert match.company == "特斯拉"
    assert match.directory_name == "TSLA_特斯拉"
    # 未在文件里声明的公司不再命中「公司映射」（确认没有被内置默认污染）；
    # 但 ticker 必须仍然被**独立识别**出来（阶段二需求）
    unmapped = directory.detect("NVDA NVIDIA.pdf")
    assert unmapped.ticker == "NVDA"
    assert unmapped.company == UNKNOWN_COMPANY


def test_missing_or_broken_mapping_file_falls_back_to_defaults(tmp_path):
    assert CompanyDirectory.from_file(tmp_path / "missing.json").detect("NVDA NVIDIA.pdf").ticker == "NVDA"
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    assert CompanyDirectory.from_file(broken).detect("NVDA NVIDIA.pdf").ticker == "NVDA"


def test_record_directory_name_joins_with_underscore():
    record = CompanyRecord(ticker="NVDA", name="NVIDIA", aliases=("英伟达",), market="US")
    assert record.directory_name == "NVDA_NVIDIA"
    assert " " not in record.directory_name
