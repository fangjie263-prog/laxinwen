"""Read-only audit for historical Research Archive directory identities."""

from __future__ import annotations

from pathlib import Path

from .company import UNKNOWN_COMPANY, UNKNOWN_TICKER, CompanyDirectory, directory_name


def audit_archive(archive_dir: str | Path, companies: CompanyDirectory | None = None) -> list[str]:
    """Return a deterministic, non-mutating report of identity splits."""
    root = Path(archive_dir)
    companies = companies or CompanyDirectory()
    groups: dict[str, set[str]] = {}
    if not root.is_dir():
        return [f"归档目录不存在：{root}"]
    for date_dir in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name.casefold()):
        for raw_dir in sorted((p for p in date_dir.iterdir() if p.is_dir()), key=lambda p: p.name.casefold()):
            match = companies.detect(raw_dir.name)
            canonical = directory_name(match.ticker, match.company)
            if canonical == UNKNOWN_COMPANY:
                continue
            groups.setdefault(canonical, set()).add(raw_dir.name)
    lines: list[str] = []
    for canonical, names in sorted(groups.items()):
        if len(names) > 1 or canonical not in names:
            lines.append(f"identity split: {', '.join(sorted(names))}")
            lines.append(f"建议 canonical：{canonical}")
    return lines or ["未发现 identity split。"]
