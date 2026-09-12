"""公司 / 股票代码识别与可扩展映射表。

核心约束（需求十七～二十、十九）：

- **股票代码必须单独保存为结构化元数据**，不能只依赖文件名；
- 目录格式固定为 ``Ticker_Company``（如 ``09696.HK_天齐锂业``）；
- 优先从文件名识别；文件名没有明确公司时，可回退到映射表别名匹配；
- **不确定就 Unknown，不猜**：``锂行业研究.pdf`` 不允许自动猜一个上市公司；
- 映射表是**数据文件**（``data/research/companies.json``），不写死在业务代码里。

匹配顺序（越靠前越可信，需求十七～二十、十九）：

1. **ticker 独立识别**（``09696.HK`` / ``000660.KS`` / ``600519.SH`` / ``NVDA`` / ``KAP``）；
2. ticker → 映射表标准记录（拿到规范 ``company_name``）；
3. 映射表 ``aliases`` 命中（文件名含公司名 / 别名但没写代码）；
4. 文件名里的公司名称（映射表里没有的未知公司，仍然保留名字）；
5. 文档内容（可选，由调用方提供）；
6. 保持 ``Unknown``。

**关键约束**：ticker 的识别**完全不依赖** ``companies.json``。即使映射表缺失
``000660.KS``，文件名 ``000660.KS SK海力士.pdf`` 也必须得到
``ticker=000660.KS``，绝不能退化成 ``ticker=Unknown``。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

UNKNOWN_TICKER = "Unknown"
UNKNOWN_COMPANY = "Unknown"

# ticker 形态（顺序敏感：带交易所后缀的最先匹配）：
#   09696.HK / 9696.HK / 600519.SH / 600519.SS / 000660.KS / 2330.TW
#   BRK.B / NVDA / KAP
# 关键：后缀是「通用两位交易所代码」而不是白名单，否则 000660.KS 会漏识别。
_TICKER_RE = re.compile(
    r"(?<![A-Za-z0-9])("
    r"\d{4,6}(?:\.[A-Za-z]{1,3})?"      # 数字代码，可带 .HK/.KS/.TW/... 后缀
    r"|[A-Za-z]{1,5}(?:\.[A-Za-z]{1,3})?"  # 字母代码，可带 .US/.IL/... 后缀
    r")(?![A-Za-z0-9])"
)

# 出现在文件名里的市场后缀（用于归一化 ticker 大小写）——
# 这是「已知市场」清单，仅用于大写化，未列出的后缀同样接受并原样保留。
_MARKET_SUFFIXES = (
    "HK", "SH", "SZ", "SS", "BJ", "US", "KS", "KQ", "TW", "T", "L", "IL",
    "TO", "V", "AX", "NZ", "DE", "F", "PA", "AS", "SW", "MI", "MC", "ST",
    "CO", "OL", "HE", "LS", "IR", "SI", "KL", "BK", "NS", "BO", "TA", "SA",
    "MX", "JO", "SR", "QA", "IS", "NASDAQ", "NYSE", "AMEX", "LSE", "TSX",
    "OTC", "HKEX", "SSE", "SZSE",
)

# ticker 前缀 / 整体命中这些词时不算 ticker（避免 AI 名 / 序号 / 研究词误判）
_TICKER_STOPWORD_EXACT = {
    "PDF", "DOCX", "DOC", "HTML", "HTM", "TXT", "XLSX", "PPTX", "CSV",
    "AI", "IPO", "ESG", "ROE", "ROA", "PE", "PB", "PS", "EV", "DCF",
    "GPU", "CPU", "CEO", "CFO", "CTO", "COO", "YOY", "QOQ", "MOM", "USD",
    "CNY", "RMB", "HKD", "EUR", "JPY", "AND", "THE", "FOR", "WITH",
    "PART", "NO", "OK", "VS", "ETF", "WACC", "IRR", "NPV", "EBIT",
    "EBITDA", "EPS", "BPS", "TAM", "SAM", "SOTP", "NAV", "AUM", "FCF",
    "GM", "NM", "OP", "TP", "BUY", "SELL", "HOLD", "NEW", "OLD",
    "GENIMI", "GEMINI", "CLAUDE", "CHATGPT", "GPT", "BARD", "COPILOT",
    "DEEPSEEK", "GROK", "KIMI", "QWEN", "LLM", "RESEARCH", "REPORT",
    "FINAL", "DRAFT", "COPY", "TEMP", "TEST", "DATA", "SAMPLE",
}

# 年份 / 日期形态：绝不当成 ticker（2026 / 20260912 / 2026-09 / 2026.09）
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_DATE8_RE = re.compile(r"^(?:19|20)\d{6}$")


def _is_date_like(token: str) -> bool:
    """判断 token 是否是年份 / 日期形态（这些不允许作为 ticker）。"""
    raw = str(token or "").strip()
    if _YEAR_RE.fullmatch(raw) or _DATE8_RE.fullmatch(raw):
        return False if raw.endswith(("HK", "SH")) else True
    return False


# 作为「独立 ticker」可接受的最短字母长度（1 位仅限大写在文件名里独立成词，
# 例如 KAP；2 位以上放宽）。避免把 "a"/"of" 这类小写词当代码。
_MIN_ALPHA_TICKER_LENGTH = 2

# 「像公司名的 CJK 片段」过滤词（避免把研究标题当成公司名）
_NAME_NOISE = {
    "研究", "研究报告", "报告", "分析", "深度", "深度研究", "行业研究",
    "调研", "周报", "月报", "年报", "季报", "点评", "跟踪", "更新",
    "摘要", "正文", "目录", "附件", "草稿", "终稿", "定稿", "最终版",
    "未知", "无", "其他", "其它", "文档", "文件", "资料",
}
_NAME_SUFFIX_NOISE = ("研究报告", "深度研究", "行业研究", "研究", "报告", "分析", "系列", "跟踪")
# 以「行业 / 板块 / 主题」结尾的片段是研究主题，不是公司名
_NAME_INDUSTRY_SUFFIX = ("行业", "板块", "产业", "赛道", "主题", "概念", "指数", "市场")

def _fold(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[\s_\-–—·、,，.。;；:：/\\|+*~()\[\]{}<>«»\"'“”‘’]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _compact(value: str) -> str:
    return _fold(value).replace(" ", "")


@dataclass(frozen=True)
class CompanyRecord:
    """一家公司的映射记录。"""

    ticker: str
    name: str
    aliases: tuple[str, ...] = ()
    market: str = ""

    @property
    def directory_name(self) -> str:
        """归档目录名：``Ticker_Company``。"""
        return f"{self.ticker}_{self.name}"

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "company_name": self.name,
            "aliases": list(self.aliases),
            "market": self.market,
        }


DEFAULT_COMPANIES: tuple[dict, ...] = (
    {
        "ticker": "09696.HK",
        "company_name": "天齐锂业",
        "aliases": ["Tianqi Lithium", "天齐", "Tianqi", "TianqiLithium", "09696", "9696.HK"],
        "market": "HK",
    },
    {
        "ticker": "002466",
        "company_name": "天齐锂业",
        "aliases": ["Tianqi Lithium", "天齐", "Tianqi"],
        "market": "SZ",
    },
    {
        "ticker": "600519",
        "company_name": "贵州茅台",
        "aliases": ["Kweichow Moutai", "茅台", "Moutai", "Guizhou Moutai"],
        "market": "SH",
    },
    {
        "ticker": "NVDA",
        "company_name": "NVIDIA",
        "aliases": ["英伟达", "Nvidia", "NVIDIA Corporation", "NVDA.US"],
        "market": "US",
    },
    {
        "ticker": "KAP",
        "company_name": "Kazatomprom",
        "aliases": ["哈萨克原子能", "Kazatomprom JSC", "KAP.IL", "Kazakhstan"],
        "market": "LSE",
    },
    {
        "ticker": "000660.KS",
        "company_name": "SK海力士",
        "aliases": ["SK Hynix", "SK海力士", "Hynix", "海力士", "000660", "SK하이닉스"],
        "market": "KRX",
    },
)


def parse_ticker(raw: str) -> str:
    """归一化 ticker 写法。

    - ``09696.hk`` → ``09696.HK``；``000660.ks`` → ``000660.KS``
    - ``nvda`` → ``NVDA``
    - **前缀数字保持原样**（``09696.HK`` 不允许丢掉前导 0，否则目录名会漂移）
    """
    text = unicodedata.normalize("NFKC", str(raw or "")).strip()
    if not text:
        return ""
    text = text.replace("：", ":").replace("．", ".").strip(" ")
    # 去掉首尾可能粘连的标点（文件名里常见：「09696.HK」）
    text = text.strip("_-–—·、,，。;；:：/\\|+*~()[]{}<>«»\"'“”‘’")
    if not text:
        return ""
    if "." in text:
        head, _, tail = text.rpartition(".")
        suffix = tail.upper()
        # 数字代码：保留前导 0；字母代码：大写
        normalized_head = head.upper() if head.isalpha() else head
        if tail and re.fullmatch(r"[A-Za-z]{1,3}", tail):
            return f"{normalized_head}.{suffix}"
        return normalized_head.upper() if normalized_head.isalpha() else normalized_head
    return text.upper() if text.isalpha() else text


def looks_like_ticker(token: str) -> bool:
    """判断 token 是否像股票代码（**不依赖映射表**）。

    接受：

    - ``09696.HK`` / ``000660.KS`` / ``600519.SH`` / ``2330.TW``（数字 + 交易所后缀）
    - ``09696`` / ``000660``（纯数字）
    - ``NVDA`` / ``KAP``（字母，需独立成词且不在停用词表）
    """
    if not token:
        return False
    raw = unicodedata.normalize("NFKC", str(token)).strip()
    if not raw:
        return False

    # 1) 数字 + 交易所后缀：000660.KS / 09696.HK / 600519.SH
    match = re.fullmatch(r"(\d{4,6})\.([A-Za-z]{1,3})", raw)
    if match:
        return True

    # 2) 纯数字代码（排除年份：2026 不是代码）
    if re.fullmatch(r"\d{4,6}", raw):
        return not _is_date_like(raw)

    # 3) 字母代码（可带单个字母类后缀，如 BRK.B）
    upper = raw.upper()
    if upper in _TICKER_STOPWORD_EXACT:
        return False
    match = re.fullmatch(r"([A-Za-z]{1,5})(?:\.([A-Za-z]{1,3}))?", raw)
    if not match:
        return False
    # 全大写 token 的前缀命中也算停用词：NOISETEST → NOISE 前缀
    for length in range(2, len(upper) + 1):
        if upper[:length] in _TICKER_STOPWORD_EXACT:
            return False
    if not match:
        return False
    head, suffix = match.group(1), match.group(2)
    if suffix and suffix.upper() in _TICKER_STOPWORD_EXACT:
        return False
    if raw.isupper() and len(head) >= 1:
        # 全大写：1 位也接受（K / F 这类真实代码），但要求独立成词（由调用方保证）
        return True
    # 小写 / 混合大小写字母词（file / research / Final）一律不作为 ticker 信号：
    # 真实代码在文件名里总是全大写（NVDA / KAP / 600519.SH）。
    return False


@dataclass(frozen=True)
class CompanyMatch:
    """公司识别结果。

    ``ticker`` 与 ``company`` **互相独立**：

    - 只要能识别出 ticker，``ticker`` 一定有值（即使映射表里没有该代码）；
    - ``company`` 拿不到时才是 ``Unknown``，不影响 ``ticker``。
    """

    ticker: str = UNKNOWN_TICKER
    company: str = UNKNOWN_COMPANY
    matched_by: str = "unknown"
    raw_ticker: str = ""

    @property
    def known(self) -> bool:
        return self.company != UNKNOWN_COMPANY or self.ticker != UNKNOWN_TICKER

    @property
    def ticker_known(self) -> bool:
        return bool(self.ticker) and self.ticker != UNKNOWN_TICKER

    @property
    def directory_name(self) -> str:
        return f"{self.ticker or UNKNOWN_TICKER}_{self.company or UNKNOWN_COMPANY}"


class CompanyDirectory:
    """公司映射表（可扩展；可从 JSON 文件覆盖内置默认）。"""

    def __init__(self, records: Iterable[CompanyRecord] | None = None):
        self._records: list[CompanyRecord] = list(records or self._default_records())
        # 别名 → 记录（精确归一化）
        self._alias_index: dict[str, CompanyRecord] = {}
        # ticker 归一化 → 记录
        self._ticker_index: dict[str, CompanyRecord] = {}
        self._rebuild_index()

    @staticmethod
    def _default_records() -> list[CompanyRecord]:
        records: list[CompanyRecord] = []
        for item in DEFAULT_COMPANIES:
            records.append(
                CompanyRecord(
                    ticker=parse_ticker(item["ticker"]),
                    name=str(item["company_name"]),
                    aliases=tuple(str(alias) for alias in item.get("aliases", ())),
                    market=str(item.get("market", "")),
                )
            )
        return records

    @classmethod
    def from_file(cls, path: str | Path | None) -> "CompanyDirectory":
        """从 JSON 载入映射；文件缺失 / 损坏时退回内置默认（不抛异常）。"""
        if not path:
            return cls()
        file_path = Path(path)
        if not file_path.is_file():
            logger.debug("公司映射文件不存在，使用内置默认：%s", file_path)
            return cls()
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("公司映射文件无法解析（使用内置默认）：%s（%s）", file_path, exc)
            return cls()
        items = raw.get("companies") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            logger.warning("公司映射文件结构不正确（使用内置默认）：%s", file_path)
            return cls()
        records: list[CompanyRecord] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            ticker = parse_ticker(str(item.get("ticker", "")))
            name = str(item.get("company_name") or item.get("name") or "").strip()
            if not ticker and not name:
                continue
            records.append(
                CompanyRecord(
                    ticker=ticker or UNKNOWN_TICKER,
                    name=name or UNKNOWN_COMPANY,
                    aliases=tuple(
                        str(alias) for alias in (item.get("aliases") or ()) if str(alias).strip()
                    ),
                    market=str(item.get("market", "")),
                )
            )
        return cls(records) if records else cls()

    def _rebuild_index(self) -> None:
        self._alias_index.clear()
        self._ticker_index.clear()
        for record in self._records:
            if record.ticker and record.ticker != UNKNOWN_TICKER:
                self._ticker_index.setdefault(_fold(record.ticker), record)
                self._ticker_index.setdefault(_compact(record.ticker), record)
                # 数字代码允许「去前导零」写法（09696 ↔ 9696）
                digits = re.match(r"^0*(\d{3,6})", record.ticker)
                if digits:
                    self._ticker_index.setdefault(digits.group(1), record)
            for alias in (record.name, *record.aliases):
                folded = _fold(alias)
                if len(folded) < 2:
                    continue
                self._alias_index.setdefault(folded, record)
                self._alias_index.setdefault(_compact(alias), record)

    @property
    def records(self) -> list[CompanyRecord]:
        return list(self._records)

    def by_ticker(self, ticker: str) -> Optional[CompanyRecord]:
        if not ticker:
            return None
        return self._ticker_index.get(_fold(parse_ticker(ticker))) or self._ticker_index.get(
            _compact(parse_ticker(ticker))
        )

    # ---------- ticker（独立于映射表） ----------

    @staticmethod
    def extract_tickers(text: str) -> list[str]:
        """从任意文本里抽出所有 **ticker 形态** 的串（与映射表无关）。

        ``000660.KS SK海力士.pdf`` → ``["000660.KS"]``
        """
        normalized = unicodedata.normalize("NFKC", str(text or ""))
        found: list[str] = []
        for raw in _TICKER_RE.findall(normalized):
            if not looks_like_ticker(raw):
                continue
            # 日期 / 年份不是 ticker：2026、20260912、2026-09
            if _is_date_like(raw):
                continue
            ticker = parse_ticker(raw)
            if ticker and ticker not in found:
                found.append(ticker)
        return found

    def detect_ticker(self, filename: str) -> tuple[str, str]:
        """**独立**识别 ticker，返回 ``(ticker, matched_by)``。

        不查映射表，因此 ``companies.json`` 缺失映射时依然能拿到
        ``000660.KS``。数字 + 交易所后缀的代码优先级最高（最可信）。
        """
        stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", str(filename or ""))
        if not stem.strip():
            return "", ""
        candidates = self.extract_tickers(stem)
        if not candidates:
            return "", ""
        # 优先「数字 + 交易所后缀」形态，其次纯数字，最后字母代码
        for priority in (
            r"^\d{4,6}\.[A-Za-z]{1,3}$",
            r"^\d{4,6}$",
            r"^[A-Za-z]{1,5}\.[A-Za-z]{1,3}$",
            r"^[A-Za-z]{1,5}$",
        ):
            for candidate in candidates:
                if re.fullmatch(priority, candidate):
                    return candidate, "filename_ticker"
        return candidates[0], "filename_ticker"

    # ---------- 公司名（映射表 aliases / 文件名公司名称） ----------

    def _match_alias(self, text: str) -> Optional[tuple[CompanyRecord, str]]:
        """用映射表 ``aliases`` 匹配公司名；返回 ``(记录, 命中词)``。"""
        folded = _fold(text)
        compact = _compact(text)
        for alias, record in sorted(self._alias_index.items(), key=lambda item: -len(item[0])):
            if len(alias) < 2:
                continue
            if alias in compact:
                return record, alias
            if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", folded):
                return record, alias
        return None

    # CJK 片段（可带紧邻的 ASCII 品牌前缀，如 SK海力士 / ST华微）
    _HAN_SEGMENT_RE = re.compile(
        r"(?<![A-Za-z0-9])([A-Z]{1,4})?"
        r"([\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]{2,20})"
    )

    @classmethod
    def _han_name(cls, text: str) -> str:
        """从文本里抽出「像公司名的中文 / 日文 / 韩文片段」并保留品牌前缀。

        仅作为映射表缺失时的兜底：``SK海力士`` / ``天齐锂业`` 会被保留下来，
        而不是变成 ``Unknown``。
        """
        cleaned = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", str(text or ""))
        cleaned = unicodedata.normalize("NFKC", cleaned)
        candidates: list[str] = []
        for prefix, segment in cls._HAN_SEGMENT_RE.findall(cleaned):
            # 去掉尾部噪音词（如「研究」「分析」）
            trimmed = segment
            for noise in _NAME_SUFFIX_NOISE:
                if trimmed.endswith(noise) and len(trimmed) > len(noise) + 1:
                    trimmed = trimmed[: -len(noise)]
            if len(trimmed) < 2:
                continue
            if trimmed.casefold() in _NAME_NOISE:
                continue
            # 「XX行业 / XX产业 / XX指数」是研究主题，不是公司名 —— 不猜
            if trimmed.endswith(_NAME_INDUSTRY_SUFFIX):
                continue
            if any(noise in trimmed for noise in ("研究", "报告", "分析", "深度")):
                continue
            candidates.append(f"{prefix}{trimmed}" if prefix else trimmed)
        if not candidates:
            return ""
        # 最长的最可能是公司名
        return max(candidates, key=len)

    # ---------- 统一入口 ----------

    def detect(self, filename: str, *, content_text: str = "") -> CompanyMatch:
        """按需求优先级识别 ticker / 公司。

        1. **独立 ticker 识别**（不依赖映射表）
        2. ticker → 映射表标准记录
        3. 映射表 ``aliases``
        4. 文件名公司名称（映射表没有也保留）
        5. 文档内容
        6. ``Unknown``
        """
        text = str(filename or "")
        stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", text)
        if not stem.strip():
            return CompanyMatch()

        # 1) ticker 独立识别（强信号：数字代码 / 带交易所后缀）
        ticker, ticker_by = self.detect_ticker(text)
        # 弱信号（纯字母短词）只有在映射表认识时才采用，避免 NOISE/FINAL 误判
        if not ticker:
            for candidate in self.extract_tickers(stem):
                if self.by_ticker(candidate) is not None:
                    ticker, ticker_by = candidate, "filename_ticker"
                    break

        # 2) ticker → 标准映射
        record = self.by_ticker(ticker) if ticker else None
        if record is not None:
            return CompanyMatch(
                ticker=record.ticker,
                company=record.name,
                matched_by="ticker_mapping",
                raw_ticker=ticker,
            )

        # 3) 映射表 aliases（文件名里没有可用 ticker 时）
        alias_hit = self._match_alias(stem)
        if alias_hit is not None:
            hit_record, _ = alias_hit
            resolved = ticker or self._ticker_for(hit_record, compact=_compact(stem))
            return CompanyMatch(
                ticker=resolved or UNKNOWN_TICKER,
                company=hit_record.name,
                matched_by="filename_alias" if not ticker else "filename_alias+filer_ticker",
                raw_ticker=ticker,
            )

        # 4) 文件名里的公司名称（映射表未收录也不丢）
        name = self._han_name(stem)
        if name:
            return CompanyMatch(
                ticker=ticker or UNKNOWN_TICKER,
                company=name,
                matched_by="filename_company",
                raw_ticker=ticker,
            )

        # 5) 文档内容
        if content_text:
            content_hit = self._match_alias(content_text)
            if content_hit is not None:
                hit_record, _ = content_hit
                return CompanyMatch(
                    ticker=ticker or hit_record.ticker or UNKNOWN_TICKER,
                    company=hit_record.name,
                    matched_by="content",
                    raw_ticker=ticker,
                )

        # 6) Unknown（ticker 仍可独立保留）
        if ticker:
            return CompanyMatch(
                ticker=ticker,
                company=UNKNOWN_COMPANY,
                matched_by=ticker_by,
                raw_ticker=ticker,
            )
        return CompanyMatch()

    def _ticker_for(self, record: CompanyRecord, *, compact: str) -> str:
        """公司名命中时，尽量同时拿到 ticker（同一家公司可能有多个市场代码）。"""
        same_name = [
            item for item in self._records if item.name == record.name and item.ticker
        ]
        if not same_name:
            return record.ticker
        # 若文件名里出现了同一公司的其它市场代码，优先采用它
        for item in same_name:
            for key in (_fold(item.ticker), _compact(item.ticker)):
                if key and key in compact:
                    return item.ticker
        return record.ticker or UNKNOWN_TICKER

    # ---------- aliases 数据扩展（映射表可继续追加） ----------

    def with_aliases(self, extra: Iterable[tuple[str, str, tuple[str, ...]]]) -> "CompanyDirectory":
        """返回追加了 ``(ticker, name, aliases)`` 的新映射表（不修改自身）。"""
        records = list(self._records)
        index = {record.ticker: record for record in records if record.ticker}
        for ticker, name, aliases in extra:
            normalized = parse_ticker(ticker)
            existing = index.get(normalized)
            if existing is not None:
                merged = tuple(dict.fromkeys((*existing.aliases, *aliases)))
                records[records.index(existing)] = CompanyRecord(
                    ticker=existing.ticker, name=name or existing.name,
                    aliases=merged, market=existing.market,
                )
                continue
            record = CompanyRecord(ticker=normalized, name=name, aliases=tuple(aliases))
            records.append(record)
            index[normalized] = record
        return CompanyDirectory(records)


def company_directory(path: str | Path | None = None) -> CompanyDirectory:
    """便捷工厂：从文件路径载入映射表（缺省用内置默认）。"""
    return CompanyDirectory.from_file(path)


def detect_ticker(filename: str) -> str:
    """模块级便捷函数：**独立**识别 ticker（不依赖任何映射表）。

    ``000660.KS SK海力士.pdf`` → ``000660.KS``，映射表缺失也照样识别。

    只有「带交易所后缀」或「纯数字代码」这一类**强信号**才返回（这覆盖了
    需求里点名的全部形态）；纯字母短词（``NOISE`` / ``FINAL``）不在此列，
    它们只在配合映射表 / 公司名时才被采用，避免误判。
    """
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", str(filename or ""))
    candidates = CompanyDirectory.extract_tickers(stem)
    for priority in (r"^\d{4,6}\.[A-Za-z]{1,3}$", r"^\d{4,6}$"):
        for candidate in candidates:
            if re.fullmatch(priority, candidate):
                return candidate
    return ""


__all__ = [
    "CompanyDirectory",
    "CompanyMatch",
    "CompanyRecord",
    "DEFAULT_COMPANIES",
    "UNKNOWN_COMPANY",
    "UNKNOWN_TICKER",
    "company_directory",
    "detect_ticker",
    "looks_like_ticker",
    "parse_ticker",
]
