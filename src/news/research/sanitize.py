"""文件名字段清理与规范命名。

Windows 文件名禁止 ``\\ / : * ? " < > |``，同时 Notion / HTML / URL / Python
都对空格敏感，因此：

- 所有**自动生成**的规范文件名只使用 ``_`` 作为字段分隔符，绝不使用空格；
- 非法字符按需求表替换：``\\ / : |`` → ``_``，``* ? " < >`` → 删除；
- 连续 ``_`` 合并、去掉首尾 ``_`` 与空格、避免空文件名、保留扩展名。

本模块是**纯函数层**，不读写文件、不依赖网络，便于单测覆盖所有非法字符。
"""

from __future__ import annotations

import unicodedata

# 统一大小限制（需求二十五）：单个上传文件 ≤ 4.5MiB。
# 与 notion_sync.notion_max_upload_bytes() 同源，保持一个常量，避免两处漂移。
MAX_ARCHIVE_FILE_SIZE = int(4.5 * 1024 * 1024)

# 非法字符 → 替换值（None 表示直接删除）
ILLEGAL_CHAR_REPLACEMENTS: dict[str, str | None] = {
    "\\": "_",
    "/": "_",
    ":": "_",
    "|": "_",
    "*": None,
    "?": None,
    '"': None,
    "<": None,
    ">": None,
}

# 除上表外还需要兜底处理的字符：控制字符 / 换行 / 制表符
_CONTROL_CHARS = {chr(code) for code in range(0x20)}
# Windows 保留文件名（不区分大小写），必须避免生成
RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# 归档文件名字段的最大长度（防止超长路径，同时保留可读性）
MAX_FIELD_LENGTH = 120
MAX_STEM_LENGTH = 180


# 全角 / Unicode 变体：Windows 允许但会污染文件名、URL 与 Notion 文本。
# 需求（第三阶段）明确要求清理的「中文/全角括注与分隔符」：
#   「」 《》 【】 （） [] ｜ | ： : / \ * ? " < >
# 全部替换为 ``_``，随后由「连续 _ 合并」收敛。
_UNICODE_ILLEGAL = {
    "：": "_",  # 全角冒号
    "？": "_",  # 全角问号
    "＊": "_",  # 全角星号
    "＂": "_",  # 全角引号
    "＜": "_",
    "＞": "_",
    "｜": "_",  # 全角竖线
    "∕": "_",
    "⁄": "_",
    "﹕": "_",
    "﹖": "_",
    # 中文书名号 / 引号 / 方括号 / 圆括号 / 全角方括号
    "「": "_",
    "」": "_",
    "『": "_",
    "』": "_",
    "《": "_",
    "》": "_",
    "〈": "_",
    "〉": "_",
    "【": "_",
    "】": "_",
    "〔": "_",
    "〕": "_",
    "［": "_",
    "］": "_",
    "｛": "_",
    "｝": "_",
    "（": "_",
    "）": "_",
    "“": "_",
    "”": "_",
    "‘": "_",
    "’": "_",
    "、": "_",
    "，": "_",
    "。": "_",
    "；": "_",
    "！": "_",
    "…": "_",
}

# ASCII 标点中「仅作分隔、必须清理」的字符（其余 ASCII 原样保留）
# 注意 ``.`` **必须保留**，否则 ``09696.HK`` / ``000660.KS`` 会被破坏。
_ASCII_SEPARATORS = frozenset(
    list("[](){},;!") + ["\\", "/", "|", ":", "<", ">", '"', "*", "?", "~",
    "`", "^", "&", "#", "$", "%", "@", "+", "="] + ["\t"]
)


def _is_illegal_for_windows(char: str) -> bool:
    """除 ASCII 非法字符外，是否还是需要清理的 Unicode 变体。"""
    if char in _UNICODE_ILLEGAL:
        return True
    if char in _ASCII_SEPARATORS:
        return True
    # 其它 Unicode 标点（Ps/Pe/Pf/Pi/Pd/Pc/Po）统一清理，
    # 但不包括 ``.`` ``-`` ``_`` 这些对 ticker 至关重要的字符。
    if char in {".", "-", "_", "+", "%"}:
        return False
    return unicodedata.category(char) in {"Ps", "Pe", "Pf", "Pi", "Pd", "Po", "Sm", "Sk", "So"}


def sanitize_filename(value: str, *, fallback: str = "Untitled") -> str:
    """把任意文本清洗成可安全用于 Windows 文件名的片段。

    规则（需求十一）：

    1. ``\\ / : |`` → ``_``；``* ? " < >`` → 删除；
    2. 其它控制字符删除；
    3. 连续多个 ``_`` 合并为一个；
    4. 删除首尾 ``_`` 与首尾空格；
    5. 结果为空时使用 ``fallback``；
    6. 命中 Windows 保留名时追加 ``_file``。

    注意：本函数**不**保留扩展名（调用方负责），因为它只处理单个字段。
    """
    if value is None:
        value = ""
    text = unicodedata.normalize("NFC", str(value))

    out: list[str] = []
    for char in text:
        if char in ILLEGAL_CHAR_REPLACEMENTS:
            replacement = ILLEGAL_CHAR_REPLACEMENTS[char]
            out.append("" if replacement is None else replacement)
        elif char in _CONTROL_CHARS:
            # 换行 / 制表符等：用下划线占位，避免把两个字段粘在一起
            out.append("_")
        elif char in _UNICODE_ILLEGAL or _is_illegal_for_windows(char):
            # 全角变体（：？＊＂＜＞｜）与 Windows 非法 ASCII 一样必须清理，
            # 否则在中文标题里会持续污染跨平台文件名 / URL / Notion 文本。
            out.append("_")
        else:
            out.append(char)
    text = "".join(out)

    # 空白（含全角空格）统一成单个下划线：自动生成名不得出现空格
    collapsed: list[str] = []
    prev_underscore = False
    for char in text:
        if char.isspace() or char == "_":
            if not prev_underscore:
                collapsed.append("_")
            prev_underscore = True
        else:
            collapsed.append(char)
            prev_underscore = False
    text = "".join(collapsed).strip("_").strip()

    if len(text) > MAX_FIELD_LENGTH:
        text = text[:MAX_FIELD_LENGTH].strip("_").strip()

    if not text:
        text = sanitize_filename_simple(fallback)
    if text.upper() in RESERVED_NAMES:
        text = f"{text}_file"
    return text


def sanitize_filename_simple(value: str) -> str:
    """``sanitize_filename`` 的无递归版本（供 fallback 使用）。"""
    text = "".join(
        "" if ILLEGAL_CHAR_REPLACEMENTS.get(char, "keep") is None
        else ("_" if char in ILLEGAL_CHAR_REPLACEMENTS else char)
        for char in str(value or "")
        if char not in _CONTROL_CHARS
    )
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_ ").strip() or "Untitled"


def sanitize_path_component(value: str, *, fallback: str = "Unknown") -> str:
    """清洗单个目录名（公司目录 / 日期目录），语义同 ``sanitize_filename``。"""
    return sanitize_filename(value, fallback=fallback)


def split_extension(filename: str) -> tuple[str, str]:
    """拆出 ``(stem, ext)``；``ext`` 含点号且已小写，无扩展名时为空串。"""
    name = str(filename or "")
    index = name.rfind(".")
    if index <= 0 or index == len(name) - 1:
        return name, ""
    return name[:index], name[index:].lower()


def normalize_extension(filename: str) -> str:
    """返回规范化扩展名（``.htm`` 统一成 ``.html``）。"""
    _, ext = split_extension(filename)
    return ".html" if ext == ".htm" else ext


def join_filename(stem: str, extension: str) -> str:
    """拼接 ``stem`` 与 ``extension``，保证总长不超过 ``MAX_STEM_LENGTH``。"""
    ext = extension if extension.startswith(".") or not extension else f".{extension}"
    cleaned_stem = str(stem).strip().strip(".").strip()
    if len(cleaned_stem) > MAX_STEM_LENGTH:
        cleaned_stem = cleaned_stem[:MAX_STEM_LENGTH].strip("_")
    if not cleaned_stem:
        cleaned_stem = "Untitled"
    return f"{cleaned_stem}{ext}"


# 序号规则：A..Z，然后 AA..AZ，BA..（需求七）

def index_to_letters(index: int) -> str:
    """把 1 基序号转成 ``A`` / ``Z`` / ``AA`` / ``AB`` 形式。

    ``index < 1`` 视为 1（不允许出现空序号）。
    """
    if index < 1:
        index = 1
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def part_number(index: int) -> str:
    """分片序号：``Part01`` / ``Part02`` ……（统一两位，保证机器排序）。"""
    return f"Part{max(1, int(index)):02d}"
