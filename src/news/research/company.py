"""公司 / 股票代码识别与可扩展映射表。

核心约束（需求十七～二十、十九）：

- **股票代码必须单独保存为结构化元数据**，不能只依赖文件名；
- 目录格式固定为 ``Ticker_Company``（如 ``09696.HK_天齐锂业``）；
- 优先从文件名识别；文件名没有明确公司时，可回退到映射表别名匹配；
- **不确定就 Unknown，不猜**：``锂行业研究.pdf`` 不允许自动猜一个上市公司；
- 映射表是**数据文件**（``data/research/companies.json``），不写死在业务代码里。

匹配顺序（越靠前越可信）：

1. 文件名里的 ``ticker`` 形态串（``09696.HK`` / ``002466`` / ``NVDA``）；
2. 文件名里的公司名 / 别名（精确 → 紧凑 → 有限前缀）；
3. 映射表别名（同上）—— 只用于「文件名含公司名但没写代码」的情形；
4. 保持 Unknown。
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

# ticker 形态：
#   09696.HK / 9696.HK / 600519.SH / NVDA / KAP / BRK.B
_TICKER_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])("
    r"\d{4,6}\.(?:HK|SH|SZ|SS|BJ)"       # 港股 / A 股
    r"|\d{4,6}"                          # 纯数字代码
    r"|[A-Z]{1,5}(?:\.[A-Z])?"           # 美股 / 其它市场代码
    r")(?![A-Za-z0-9])"
)

# 出现在文件名里的市场后缀（用于归一化 ticker 大小写）
_MARKET_SUFFIXES = ("HK", "SH", "SZ", "SS", "BJ", "US", "NASDAQ", "NYSE")

# 明显不是股票代码的纯字母词（避免把 AI 名 / 研究词当 ticker）
_TICKER_STOPWORDS = {
    "PDF", "DOCX", "HTML", "HTM", "AI", "IPO", "ESG", "ROE", "PE", "PB",
    "DCF", "GPU", "CPU", "CEO", "CFO", "YOY", "QOQ", "USD", "CNY", "HKD",
    "AND", "THE", "FOR", "WITH", "PART", "NO", "OK", "VS", "ETF", "WACC",
    "IRR", "NPV", "EBIT", "EBITDA", "EPS", "BPS", "TAM", "SAM",
}


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
)


def parse_ticker(raw: str) -> str:
    """归一化 ticker 写法：``09696.hk`` → ``09696.HK``，``nvda`` → ``NVDA``。"""
    text = unicodedata.normalize("NFKC", str(raw or "")).strip()
    if not text:
        return ""
    text = text.replace("：", ":").replace("．", ".")
    if "." in text:
        head, _, tail = text.rpartition(".")
        suffix = tail.upper()
        if suffix in _MARKET_SUFFIXES:
            return f"{head.upper() if head.isalpha() else head}.{suffix}"
        return text.upper() if text.isalpha() else text
    return text.upper() if text.isalpha() else text


def looks_like_ticker(token: str) -> bool:
    """判断 token 是否像股票代码。"""
    if not token:
        return False
    if token.upper() in _TICKER_STOPWORDS:
        return False
    if re.fullmatch(r"\d{4,6}", token):
        return True
    if re.fullmatch(r"\d{4,6}\.[A-Za-z]{2}", token):
        return True
    if re.fullmatch(r"[A-Za-z]{1,5}(\.[A-Za-z])?", token):
        return True
    return False


@dataclass(frozen=True)
class CompanyMatch:
    """公司识别结果。"""

    ticker: str = UNKNOWN_TICKER
    company: str = UNKNOWN_COMPANY
    matched_by: str = "unknown"
    raw_ticker: str = ""

    @property
    def known(self) -> bool:
        return self.company != UNKNOWN_COMPANY or self.ticker != UNKNOWN_TICKER

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

    def detect(self, filename: str) -> CompanyMatch:
        """从文件名识别公司。

        ``filename`` 传原始文件名（含或不含扩展名均可）。
        """
        stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", str(filename or ""))
        if not stem.strip():
            return CompanyMatch()

        # 1) ticker 形态串（可能与公司名同时存在）
        for token in _TICKER_TOKEN_RE.findall(unicodedata.normalize("NFKC", stem)):
            if not looks_like_ticker(token):
                continue
            normalized = parse_ticker(token)
            record = self.by_ticker(normalized)
            if record is not None:
                return CompanyMatch(
                    ticker=record.ticker,
                    company=record.name,
                    matched_by="filename_ticker",
                    raw_ticker=normalized,
                )

        # 2) 公司名 / 别名（长别名优先，避免「天齐」抢在「天齐锂业」前）
        folded = _fold(stem)
        compact = _compact(stem)
        for alias, record in sorted(
            self._alias_index.items(), key=lambda item: -len(item[0])
        ):
            if len(alias) < 2:
                continue
            if alias in compact:
                ticker = self._ticker_for(record, compact=compact)
                return CompanyMatch(
                    ticker=ticker,
                    company=record.name,
                    matched_by="filename_name",
                )
            if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", folded):
                ticker = self._ticker_for(record, compact=compact)
                return CompanyMatch(
                    ticker=ticker,
                    company=record.name,
                    matched_by="filename_name",
                )

        # 3) 无法确定 —— 不猜
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


def company_directory(path: str | Path | None = None) -> CompanyDirectory:
    """便捷工厂：从文件路径载入映射表（缺省用内置默认）。"""
    return CompanyDirectory.from_file(path)


__all__ = [
    "CompanyDirectory",
    "CompanyMatch",
    "CompanyRecord",
    "DEFAULT_COMPANIES",
    "UNKNOWN_COMPANY",
    "UNKNOWN_TICKER",
    "company_directory",
    "looks_like_ticker",
    "parse_ticker",
]
