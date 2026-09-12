"""文件名清理 / 序号 / 规范命名测试（对应需求七、九、十、十一、二十七）。"""

from __future__ import annotations

import re

import pytest

from news.research.sanitize import (
    MAX_ARCHIVE_FILE_SIZE,
    index_to_letters,
    join_filename,
    normalize_extension,
    part_number,
    sanitize_filename,
    split_extension,
)

WINDOWS_ILLEGAL = '\\/:*?"<>|'


@pytest.mark.parametrize("char", list(WINDOWS_ILLEGAL))
def test_sanitize_never_emits_windows_illegal_chars(char):
    cleaned = sanitize_filename(f"研究{char}报告")
    assert not set(cleaned) & set(WINDOWS_ILLEGAL)
    assert cleaned  # 不允许生成空文件名


def test_sanitize_colon_and_slash_become_underscore():
    assert sanitize_filename("锂价供需：2026/2027预测？") == "锂价供需_2026_2027预测"


def test_sanitize_example_from_requirement():
    # 需求十一给出的例子
    assert sanitize_filename("Claude：天齐锂业/锂价分析？") == "Claude_天齐锂业_锂价分析"


def test_sanitize_collapses_underscores_and_trims():
    assert sanitize_filename("__a___b__") == "a_b"
    assert sanitize_filename("  a  b  ") == "a_b"


def test_sanitize_spaces_become_underscore_never_space():
    cleaned = sanitize_filename("Claude 天齐锂业 研究")
    assert " " not in cleaned
    assert cleaned == "Claude_天齐锂业_研究"


def test_sanitize_empty_uses_fallback():
    assert sanitize_filename("", fallback="Unknown") == "Unknown"
    assert sanitize_filename("***???", fallback="Unknown") == "Unknown"


def test_sanitize_windows_reserved_names():
    assert sanitize_filename("CON") == "CON_file"
    assert sanitize_filename("nul") == "nul_file"
    assert sanitize_filename("COM1") == "COM1_file"


def test_sanitize_length_is_bounded():
    cleaned = sanitize_filename("研" * 500)
    assert len(cleaned) <= 120


def test_split_and_normalize_extension():
    assert split_extension("a.PDF") == ("a", ".pdf")
    assert split_extension("无扩展名") == ("无扩展名", "")
    assert split_extension(".hidden") == (".hidden", "")
    assert normalize_extension("a.HTM") == ".html"
    assert normalize_extension("a.htm") == ".html"
    assert normalize_extension("a.Pdf") == ".pdf"


def test_join_filename_keeps_extension_and_strips_dots():
    assert join_filename("研究.报告.", ".pdf") == "研究.报告.pdf"
    assert join_filename("", ".docx") == "Untitled.docx"


@pytest.mark.parametrize(
    ("index", "expected"),
    [(1, "A"), (2, "B"), (26, "Z"), (27, "AA"), (28, "AB"), (52, "AZ"), (53, "BA"), (702, "ZZ")],
)
def test_index_to_letters(index, expected):
    assert index_to_letters(index) == expected


def test_index_to_letters_clamps_non_positive():
    assert index_to_letters(0) == "A"
    assert index_to_letters(-5) == "A"


def test_part_number_is_two_digits_for_sorting():
    assert part_number(1) == "Part01"
    assert part_number(9) == "Part09"
    assert part_number(10) == "Part10"
    assert part_number(100) == "Part100"


def test_max_archive_file_size_matches_requirement():
    assert MAX_ARCHIVE_FILE_SIZE == int(4.5 * 1024 * 1024)


def test_generated_name_has_no_space_separator():
    name = "_".join(sanitize_filename(p) for p in ("20260912A", "Claude", "09696.HK", "天齐锂业", "深度投资研究"))
    assert re.fullmatch(r"[^ ]+\.pdf", f"{name}.pdf")
    assert name == "20260912A_Claude_09696.HK_天齐锂业_深度投资研究"
