"""扫描 Inbox → 识别 → 去重 → 生成归档计划（不移动任何文件）。

这一层是纯「只读 + 计算」：

- 扫描指定目录（默认不递归，避免误吃掉 Inbox 子目录里的草稿）；
- 过滤：只保留 ``.pdf/.docx/.html/.htm``；
- 跳过临时文件（``~$`` / ``.tmp`` / ``.part`` / ``.crdownload`` / 隐藏文件）；
- 计算 SHA-256 并查库去重；
- 识别日期 / 公司 / AI 来源 / 主题；
- 生成规范文件名与目标路径。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .ai_source import UNKNOWN_AI_SOURCE, detect_ai_source
from .archive_store import ResearchArchiveStore, ResearchFileRecord, sha256_file
from .company import UNKNOWN_COMPANY, UNKNOWN_TICKER, CompanyDirectory
from .company import directory_name as company_directory_name
from .config import IGNORED_EXTENSIONS, SUPPORTED_EXTENSIONS, ResearchArchiveConfig
from .dates import detect_date
from .document_text import read_document_text
from .sanitize import (
    index_to_letters,
    join_filename,
    normalize_extension,
    sanitize_filename,
    split_extension,
)

logger = logging.getLogger(__name__)

# 需要跳过的文件前缀 / 后缀（临时文件、Office 锁文件）
_TEMP_PREFIXES = ("~$", ".~", "._")
_TEMP_SUFFIXES = (".tmp", ".part", ".crdownload", ".download", ".swp", ".swx")

# 主题清洗：去掉与 AI 来源 / 公司 / 代码重复的片段
_TOPIC_SEPARATORS = ("_", "-", "–", "—", "·", "|", "/", "\\", ":", "：")
_TOPIC_NOISE_WORDS = {
    "研究", "研究报告", "报告", "分析", "深度", "深度研究", "行业研究", "调研",
    "research", "report", "analysis", "final", "终稿", "最终版", "定稿", "草稿",
}


@dataclass
class ResearchCandidate:
    """一个待处理的 Inbox 文件及其识别结果。"""

    path: Path
    sha256: str
    size: int
    extension: str
    date: str
    date_prefix: str
    ticker: str
    company: str
    ai_source: str
    ai_matched_by: str
    date_source: str
    topic: str
    letter: str
    normalized_name: str
    archive_dir: Path
    archive_path: Path
    duplicate: bool = False
    duplicate_of: Optional[str] = None
    warnings: list[str] = field(default_factory=list)

    @property
    def company_dir(self) -> str:
        return self.archive_dir.name

    def record(self, *, status: str) -> ResearchFileRecord:
        return ResearchFileRecord(
            sha256=self.sha256,
            original_filename=self.path.name,
            normalized_filename=self.normalized_name,
            original_path=str(self.path),
            archive_path=str(self.archive_path),
            date=self.date,
            ticker=self.ticker,
            company=self.company,
            ai_source=self.ai_source,
            ai_matched_by=self.ai_matched_by,
            file_type=self.extension.lstrip("."),
            file_size=self.size,
            status=status,
            error="; ".join(self.warnings),
        )

    def plan_line(self, index: int) -> str:
        flag = "重复" if self.duplicate else "新增"
        return (
            f"{index:>3}. [{flag}] {self.path.name}\n"
            f"      sha256={self.sha256[:16]}… size={self.size}\n"
            f"      date={self.date}(source={self.date_source}) ticker={self.ticker} "
            f"company={self.company} ai={self.ai_source}({self.ai_matched_by})\n"
            f"      → {self.archive_path}"
        )


def _is_temp_file(path: Path) -> bool:
    name = path.name
    if name.startswith(".") or name.startswith(_TEMP_PREFIXES):
        return True
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in _TEMP_SUFFIXES)


def strip_known_parts(value: str) -> str:
    """去掉扩展名与已识别的 AI 来源 / 公司 / 代码片段，得到「主题」。"""
    stem, _ = split_extension(value)
    text = stem
    # 去掉日期串
    text = re.sub(r"(?<!\d)20\d{6}(?!\d)", " ", text)
    text = re.sub(r"20\d{2}[-_.年/]\d{1,2}[-_.月/]\d{1,2}日?", " ", text)
    # 去掉规范文件名里的「日期 + 独立序号」（如 20260912A）
    text = re.sub(r"\b\d{8}[A-Z]+\b", " ", text)
    text = re.sub(r"\b\d{8}\b", " ", text)
    for separator in _TOPIC_SEPARATORS:
        text = text.replace(separator, " ")
    return re.sub(r"\s+", " ", text).strip()


def extract_topic(
    filename: str,
    *,
    ai_source: str = "",
    ticker: str = "",
    company: str = "",
    aliases: Iterable[str] = (),
) -> str:
    """从文件名提取研究主题（去掉 AI / 公司 / 代码 / 日期 / 噪音词）。

    例如 ``20260912A_Claude_09696.HK_天齐锂业_锂价分析.pdf`` → ``锂价分析``。
    """
    text = strip_known_parts(filename)
    removable = [ai_source, ticker, company, *(aliases or ())]
    for item in removable:
        if not item:
            continue
        for token in (str(item), str(item).replace(".", " "), str(item).split(".")[0]):
            token = token.strip()
            if not token:
                continue
            text = re.sub(re.escape(token), " ", text, flags=re.IGNORECASE)
    tokens = [token for token in text.split(" ") if token.strip()]
    # 去掉「孤立的 1-2 位大写序号」（规范文件名残留的 A / AB）
    tokens = [token for token in tokens if not re.fullmatch(r"[A-Z]{1,2}", token)]
    kept = [token for token in tokens if token.casefold() not in _TOPIC_NOISE_WORDS]
    # 全部是噪音词时保留原 token（例如文件名只有「深度研究」）
    topic = " ".join(kept or tokens).strip()
    topic = sanitize_filename(topic, fallback="研究")
    if not topic or topic.casefold() in _TOPIC_NOISE_WORDS:
        topic = "研究"
    return topic


def build_normalized_name(
    *, date_prefix: str, letter: str, ai_source: str, ticker: str,
    company: str, topic: str, extension: str,
) -> str:
    """``YYYYMMDD序号_AI来源_股票代码_公司名称_研究主题.扩展名``（需求九）。

    不生成无意义的 ``Unknown`` 占位（需求五）：

    - 公司已识别但 ticker 未识别 → ``20260831A_Claude_哈尔滨电气_研究.doc``
      （不再出现 ``Unknown_哈尔滨电气``）
    - ticker / 公司都未识别 → 对应字段整体省略，不会出现 ``Unknown_Unknown``
    - AI 来源仍保留 ``Unknown``（Notion 侧需要按 AI Source = Unknown 检索）
    """
    parts = [f"{date_prefix}{letter}", ai_source]
    ticker_known = bool(ticker) and ticker != UNKNOWN_TICKER
    company_known = bool(company) and company != UNKNOWN_COMPANY
    if ticker_known:
        parts.append(ticker)
    if company_known:
        parts.append(company)
    parts.append(topic)
    stem = "_".join(
        sanitize_filename(part, fallback=UNKNOWN_AI_SOURCE) for part in parts
    )
    stem = re.sub(r"_{2,}", "_", stem).strip("_")
    return join_filename(stem, extension)


class ResearchArchiveScanner:
    """扫描 + 识别 + 去重 + 计划。"""

    def __init__(
        self,
        config: ResearchArchiveConfig,
        *,
        store: Optional[ResearchArchiveStore] = None,
        companies: Optional[CompanyDirectory] = None,
        read_content: bool = True,
    ):
        self.config = config
        self.store = store
        self.companies = companies or CompanyDirectory.from_file(config.companies_file)
        self.read_content = read_content
        # 同一批扫描内已分配的序号（避免 scan() 期间重复分配同一个字母）
        self._pending_letters: dict[tuple[str, str], set[str]] = {}

    # ---------- 扫描 ----------

    def iter_inbox_files(self) -> list[Path]:
        """列出 Inbox 中需要处理的文件（排序稳定，便于复现）。"""
        root = self.config.inbox_dir
        if not root.is_dir():
            return []
        iterator = root.rglob("*") if self.config.recursive else root.glob("*")
        files: list[Path] = []
        for path in iterator:
            if not path.is_file():
                continue
            if _is_temp_file(path):
                logger.debug("跳过临时文件：%s", path)
                continue
            extension = path.suffix.lower()
            if extension in IGNORED_EXTENSIONS:
                logger.debug("跳过不支持的类型：%s", path)
                continue
            if extension not in SUPPORTED_EXTENSIONS:
                logger.debug("跳过未支持扩展名：%s", path)
                continue
            files.append(path)
        files.sort(key=lambda item: (item.name.casefold(), str(item)))
        if self.config.max_files and self.config.max_files > 0:
            files = files[: self.config.max_files]
        return files

    # ---------- 识别 ----------

    def analyze(self, path: Path) -> ResearchCandidate:
        """对单个文件做完整识别（不移动、不写入）。"""
        warnings: list[str] = []
        extension = normalize_extension(path.name)
        size = path.stat().st_size
        digest = sha256_file(path)

        content_texts: tuple[str, ...] = ()
        document = None
        if self.read_content:
            document = read_document_text(path)
            if document.error:
                warnings.append(document.error)
            content_texts = document.snippets

        # 日期固定顺序：filename → metadata → mtime → Unknown（ctime 不参与）
        date_match = detect_date(
            path,
            metadata=(document.metadata if document is not None else None),
        )
        if date_match.matched_by == "unknown":
            warnings.append("无法从文件名/元数据/mtime 识别日期，已标记 source=unknown")

        # 公司识别优先级：ticker 独立识别 → 映射表 → aliases → 文件名公司名 → 内容
        company_match = self.companies.detect(
            path.name,
            content_text="\n".join(content_texts) if content_texts else "",
        )
        ai_match = detect_ai_source(path.name, content_texts=content_texts)

        aliases: list[str] = []
        if company_match.known:
            record = self.companies.by_ticker(company_match.ticker)
            if record is not None:
                aliases = [record.name, *record.aliases]
            else:
                aliases = [company_match.company]

        topic = extract_topic(
            path.name,
            ai_source=ai_match.source,
            ticker=company_match.ticker if company_match.known else "",
            company=company_match.company if company_match.known else "",
            aliases=aliases,
        )

        ticker = company_match.ticker or UNKNOWN_TICKER
        company = company_match.company or UNKNOWN_COMPANY
        ai_source = ai_match.source or UNKNOWN_AI_SOURCE

        # 目录名统一由 company.directory_name 决定（需求五）：
        # 公司已识别但 ticker 未识别时 → ``盐湖股份``，绝不生成 ``Unknown_盐湖股份``。
        company_dir = sanitize_filename(
            company_directory_name(company_match.ticker, company_match.company),
            fallback=UNKNOWN_COMPANY,
        )
        date_dir = date_match.directory
        archive_dir = self.config.archive_dir / date_dir / company_dir

        letter = self._next_letter(date=date_dir, company_dir=company_dir)
        normalized = build_normalized_name(
            date_prefix=date_match.prefix, letter=letter, ai_source=ai_source,
            ticker=ticker, company=company, topic=topic, extension=extension,
        )
        archive_path = archive_dir / normalized

        duplicate = False
        duplicate_of: Optional[str] = None
        if self.store is not None:
            existing = self.store.find_by_sha256(digest)
            if existing is not None:
                duplicate = True
                duplicate_of = existing.archive_path or existing.normalized_filename

        return ResearchCandidate(
            path=path, sha256=digest, size=size, extension=extension,
            date=date_dir, date_prefix=date_match.prefix,
            date_source=date_match.matched_by, ticker=ticker,
            company=company, ai_source=ai_source, ai_matched_by=ai_match.matched_by,
            topic=topic, letter=letter, normalized_name=normalized,
            archive_dir=archive_dir, archive_path=archive_path,
            duplicate=duplicate, duplicate_of=duplicate_of, warnings=warnings,
        )

    def _next_letter(self, *, date: str, company_dir: str) -> str:
        """``日期 + 公司`` 目录内独立编号：A、B、…、Z、AA、AB（需求七、八）。"""
        used: set[str] = set()
        used |= self._pending_letters.get((date, company_dir), set())
        if self.store is not None:
            used |= self.store.used_letters(date=date, company_dir=company_dir)
        # 兼顾「档案目录已存在但数据库可能被重建」的情况
        directory = self.config.archive_dir / date / company_dir
        if directory.is_dir():
            for item in directory.iterdir():
                match = re.match(r"^\d{8}([A-Z]+)_", item.name)
                if match:
                    used.add(match.group(1))
        index = 1
        while index_to_letters(index) in used:
            index += 1
        letter = index_to_letters(index)
        self._pending_letters.setdefault((date, company_dir), set()).add(letter)
        return letter

    def scan(self) -> list[ResearchCandidate]:
        """扫描并识别全部候选文件。"""
        return [self.analyze(path) for path in self.iter_inbox_files()]

    # ---------- 计划输出 ----------

    def format_plan(self, candidates: list[ResearchCandidate]) -> list[str]:
        """生成 dry-run 计划文本（只输出，不执行任何写操作）。"""
        lines: list[str] = []
        new_count = sum(1 for item in candidates if not item.duplicate)
        dup_count = len(candidates) - new_count
        lines.append(
            f"扫描目录：{self.config.inbox_dir}\n"
            f"归档目录：{self.config.archive_dir}\n"
            f"状态库：{self.config.db_path}\n"
            f"模式：{'DRY-RUN（只扫描，不移动/不上传）' if self.config.dry_run else 'APPLY'}\n"
            f"发现文件：{len(candidates)}（新增 {new_count}，重复 {dup_count}）"
        )
        if not candidates:
            lines.append("（没有需要处理的 .pdf / .docx / .html 文件）")
            return lines
        lines.append("")
        for index, candidate in enumerate(candidates, start=1):
            lines.append(candidate.plan_line(index))
        lines.append("")
        lines.append("提示：本次为 dry-run，未移动 / 未重命名 / 未上传任何文件。")
        return lines
