"""Read-only historical identity audit and explicit repair plan."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .archive_store import ResearchArchiveStore
from .company import UNKNOWN_COMPANY, UNKNOWN_TICKER, CompanyDirectory, CompanyMatch, directory_name, parse_ticker
from .config import SUPPORTED_EXTENSIONS
from .document_text import read_document_text
from .ai_source import detect_ai_source
from .scanner import build_normalized_name, extract_topic

SAFE_STATUSES = {"SAFE", "CANONICALIZE_REQUIRED", "IDENTITY_REPAIR_AVAILABLE"}


@dataclass(frozen=True)
class AuditEntry:
    path: Path
    current_dir: str
    current_ticker: str
    current_company: str
    filename_ticker: str
    filename_company: str
    content_ticker: str
    content_company: str
    content_status: str
    identity_status: str
    canonical_ticker: str
    canonical_company: str
    suggested_dir: str
    suggested_filename: str
    repair_reason: str
    notion_page_id: str = ""

    @property
    def needs_review(self) -> bool:
        return self.identity_status == "REVIEW_REQUIRED"

    @property
    def target(self) -> Path:
        return self.path.parent.parent / self.suggested_dir / self.suggested_filename


def _current_identity(dirname: str) -> tuple[str, str]:
    raw = str(dirname or "").strip()
    if "_" in raw:
        ticker, company = raw.split("_", 1)
        return (parse_ticker(ticker) if ticker != UNKNOWN_TICKER else UNKNOWN_TICKER,
                company or UNKNOWN_COMPANY)
    if raw in {"", UNKNOWN_TICKER, UNKNOWN_COMPANY}:
        return UNKNOWN_TICKER, UNKNOWN_COMPANY
    return UNKNOWN_TICKER, raw


def _match_content(companies: CompanyDirectory, snippets: tuple[str, ...]) -> CompanyMatch:
    return companies._detect_one("\n".join(snippets)) if snippets else CompanyMatch()


def _date_prefix(path: Path) -> str:
    match = re.match(r"^(\d{8})[A-Z]+_", path.stem)
    if match:
        return match.group(1)
    return path.parent.parent.name.replace("-", "")


def _letter(path: Path) -> str:
    match = re.match(r"^\d{8}([A-Z]+)_", path.stem)
    return match.group(1) if match else "A"


def _suggested_filename(
    path: Path,
    *,
    companies: CompanyDirectory,
    canonical_ticker: str,
    canonical_company: str,
    filename_match: CompanyMatch,
    extra_aliases: tuple[str, ...] = (),
) -> str:
    ai = detect_ai_source(path.name).source
    record = companies.by_ticker(canonical_ticker)
    aliases = list(record.aliases) if record is not None else []
    aliases.extend(("Unknown", filename_match.company, canonical_company, path.parent.name, *extra_aliases))
    topic = extract_topic(
        path.name,
        ai_source=ai,
        ticker=canonical_ticker,
        company=canonical_company,
        aliases=aliases,
    )
    return build_normalized_name(
        date_prefix=_date_prefix(path),
        letter=_letter(path),
        ai_source=ai,
        ticker=canonical_ticker,
        company=canonical_company,
        topic=topic,
        extension=path.suffix.lower(),
    )


def manual_repair_entry(
    path: str | Path,
    *,
    ticker: str,
    company: str,
    companies: CompanyDirectory,
) -> AuditEntry:
    """Build a repair plan from an explicit human-confirmed identity.

    This intentionally does not inspect document content or override automatic
    conflict handling.  It is only an explicit operator instruction for one
    known file.
    """
    source = Path(path)
    record = companies.by_ticker(ticker)
    canonical_ticker = record.ticker if record is not None else ticker
    canonical_company = record.name if record is not None else company
    filename_match = CompanyMatch(ticker=canonical_ticker, company=canonical_company, matched_by="manual")
    suggested_dir = directory_name(canonical_ticker, canonical_company)
    suggested_filename = _suggested_filename(
        source,
        companies=companies,
        canonical_ticker=canonical_ticker,
        canonical_company=canonical_company,
        filename_match=filename_match,
        extra_aliases=(ticker, company, *source.parent.name.split("_")),
    )
    current_ticker, current_company = _current_identity(source.parent.name)
    return AuditEntry(
        path=source,
        current_dir=source.parent.name,
        current_ticker=current_ticker,
        current_company=current_company,
        filename_ticker=UNKNOWN_TICKER,
        filename_company=UNKNOWN_COMPANY,
        content_ticker=UNKNOWN_TICKER,
        content_company=UNKNOWN_COMPANY,
        content_status="MANUAL_CONFIRMATION",
        identity_status="MANUAL_CONFIRMED",
        canonical_ticker=canonical_ticker,
        canonical_company=canonical_company,
        suggested_dir=suggested_dir,
        suggested_filename=suggested_filename,
        repair_reason="MANUAL_CONFIRMATION",
    )


def _entry_for_file(path: Path, companies: CompanyDirectory, store: ResearchArchiveStore | None) -> AuditEntry:
    current_dir = path.parent.name
    current_ticker, current_company = _current_identity(current_dir)
    filename_match = companies._detect_one(path.name)
    document = read_document_text(path) if path.suffix.lower() in SUPPORTED_EXTENSIONS else None
    content_status = document.content_status if document is not None else "UNKNOWN"
    snippets = document.snippets if document is not None else ()
    content_match = _match_content(companies, snippets)
    resolved = companies.detect(path.name, content_text="\n".join(snippets), content_status=content_status)
    # A historical directory name is useful evidence when the file itself has
    # an opaque legacy name.  It can repair a ticker-format split, but an
    # Unknown/Unknown directory remains unresolved and therefore review-only.
    directory_match = companies._detect_one(current_dir)
    if not resolved.known and directory_match.known:
        resolved = directory_match
    notion_page_id = ""
    if store is not None:
        record = store.find_by_archive_path(path)
        if record is not None:
            notion_page_id = record.notion_page_id

    suggested_filename = path.name
    if resolved.identity_status == "CONFLICT":
        identity_status, reason = "REVIEW_REQUIRED", "IDENTITY_CONFLICT"
        canonical_ticker, canonical_company, suggested_dir = UNKNOWN_TICKER, UNKNOWN_COMPANY, "REVIEW_REQUIRED"
    elif not resolved.known:
        identity_status, reason = "REVIEW_REQUIRED", "IDENTITY_UNKNOWN"
        canonical_ticker, canonical_company, suggested_dir = UNKNOWN_TICKER, UNKNOWN_COMPANY, "REVIEW_REQUIRED"
    else:
        canonical_ticker, canonical_company = resolved.ticker, resolved.company
        suggested_dir = directory_name(canonical_ticker, canonical_company)
        suggested_filename = _suggested_filename(
            path,
            companies=companies,
            canonical_ticker=canonical_ticker,
            canonical_company=canonical_company,
            filename_match=filename_match,
        )
        if current_dir == suggested_dir:
            if path.name == suggested_filename:
                identity_status, reason = "SAFE", "ALREADY_CANONICAL"
            else:
                identity_status, reason = "CANONICALIZE_REQUIRED", "CANONICAL_FILENAME"
        elif current_ticker == UNKNOWN_TICKER or current_company == UNKNOWN_COMPANY:
            identity_status, reason = "IDENTITY_REPAIR_AVAILABLE", "IDENTITY_MAPPING"
        elif current_ticker != canonical_ticker:
            identity_status, reason = "CANONICALIZE_REQUIRED", "CANONICAL_TICKER"
        elif current_company != canonical_company:
            identity_status, reason = "CANONICALIZE_REQUIRED", "CANONICAL_COMPANY"
        else:
            identity_status, reason = "CANONICALIZE_REQUIRED", "CANONICAL_DIRECTORY"

    return AuditEntry(
        path=path, current_dir=current_dir, current_ticker=current_ticker, current_company=current_company,
        filename_ticker=filename_match.ticker, filename_company=filename_match.company,
        content_ticker=content_match.ticker, content_company=content_match.company,
        content_status=content_status, identity_status=identity_status,
        canonical_ticker=canonical_ticker, canonical_company=canonical_company,
        suggested_dir=suggested_dir, suggested_filename=suggested_filename,
        repair_reason=reason, notion_page_id=notion_page_id,
    )


def inspect_archive(archive_dir: str | Path, *, companies: CompanyDirectory | None = None,
                    store: ResearchArchiveStore | None = None) -> list[AuditEntry]:
    """Inspect every historical file without writing anything."""
    root = Path(archive_dir)
    companies = companies or CompanyDirectory()
    if not root.is_dir():
        return []
    files = sorted(
        (p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS),
        key=lambda p: str(p).casefold(),
    )
    return [_entry_for_file(path, companies, store) for path in files]


def format_audit(entries: list[AuditEntry], archive_dir: str | Path) -> list[str]:
    lines = [f"RESEARCH ARCHIVE AUDIT · {archive_dir}", f"FILES={len(entries)}"]
    for index, item in enumerate(entries, 1):
        lines.extend([
            f"[{index}] current_path={item.path}",
            f"  current_dir={item.current_dir} current_ticker={item.current_ticker} current_company={item.current_company}",
            f"  filename_ticker={item.filename_ticker} filename_company={item.filename_company}",
            f"  content_ticker={item.content_ticker} content_company={item.content_company} content_status={item.content_status}",
            f"  canonical_ticker={item.canonical_ticker} canonical_company={item.canonical_company}",
            f"  identity_status={item.identity_status} suggested_dir={item.suggested_dir}",
            f"  suggested_filename={item.suggested_filename} suggested_full_path={item.target}",
            f"  needs_review={'YES' if item.needs_review else 'NO'} historical_error={'YES' if item.current_dir != item.suggested_dir else 'NO'}",
            f"  notion_page_id={item.notion_page_id or '(not-in-state-db)'} notion_action=PRESERVE_NO_REUPLOAD",
        ])
    if not entries:
        lines.append("（没有发现归档文件）")
    return lines


def format_repair_plan(entries: list[AuditEntry]) -> list[str]:
    lines = ["REPAIR PLAN", f"FILES={len(entries)}", "NOTION=NO_UPLOAD_PRESERVE_EXISTING_PAGE_IDS"]
    for item in entries:
        if item.needs_review:
            lines.append(f"SKIPPED · REVIEW_REQUIRED · {item.path} reason={item.repair_reason}")
        elif item.current_dir == item.suggested_dir:
            lines.append(f"SAFE · NOOP · {item.path} reason={item.repair_reason}")
        else:
            lines.append(
                f"current_path={item.path}\n"
                f"suggested_directory={item.suggested_dir}\n"
                f"suggested_filename={item.suggested_filename}\n"
                f"suggested_full_path={item.target}\n"
                f"identity_status={item.identity_status} repair_reason={item.repair_reason}"
            )
    return lines


def repair_archive(archive_dir: str | Path, entries: list[AuditEntry], *,
                    store: ResearchArchiveStore | None = None, apply: bool = False) -> list[str]:
    """Apply only safe moves when explicitly requested; never upload or delete."""
    lines = format_repair_plan(entries)
    if not apply:
        return lines
    log_path = Path(archive_dir) / "research-identity-audit.log"
    for item in entries:
        if item.needs_review or item.current_dir == item.suggested_dir:
            continue
        target = item.target
        if target.exists():
            lines.append(f"SKIPPED · TARGET_EXISTS · {item.path} → {target}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(item.path), str(target))
        if store is not None:
            store.relocate(str(item.path), str(target), ticker=item.canonical_ticker, company=item.canonical_company)
        event = {"at": datetime.now(timezone.utc).isoformat(), "action": "MOVE",
                 "source": str(item.path), "target": str(target),
                 "identity_status": item.identity_status, "repair_reason": item.repair_reason,
                 "notion_page_id": item.notion_page_id}
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        lines.append(f"APPLIED · {item.path} → {target}")
    return lines
