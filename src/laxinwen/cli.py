"""Command-line interface for laxinwen.

Commands::

    laxinwen fetch [--site eco] [--limit N] [--db PATH]
    laxinwen list [--site eco] [--limit N] [--db PATH]
    laxinwen export --format jsonl|markdown [--db PATH] [--out PATH]
    laxinwen status [--db PATH]
    laxinwen sites
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import typer

from .config import DEFAULT_SITES_DIR, load_site, list_sites
from .exporters import export_jsonl, export_markdown
from .pipeline import run_pipeline
from .storage import Storage

app = typer.Typer(
    name="laxinwen",
    help="Personal financial news collection & research database.",
    no_args_is_help=True,
)
DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "laxinwen.db"
DEFAULT_EXPORT_DIR = Path(__file__).resolve().parent.parent.parent / "exports"


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@app.command()
def sites() -> None:
    """List all configured news sites."""
    for sid in list_sites():
        site = load_site(sid)
        sources = ", ".join(f"{k}={u}" for k, u in site.effective_sources()) or "(none)"
        typer.echo(f"{sid:12s} {site.name}  [{sources}]")


@app.command()
def fetch(
    site: str = typer.Option("eco", "--site", help="Site id to fetch"),
    limit: int = typer.Option(0, "--limit", help="Max articles to download (0 = all)"),
    max_items: int = typer.Option(50, "--max-items", help="Max URLs discovered"),
    db: Path = typer.Option(DEFAULT_DB, "--db", help="SQLite database path"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Discover, download, extract and store articles for a site."""
    _setup_logging(verbose)
    cfg = load_site(site)
    typer.echo(f"Fetching site: {cfg.name} ({cfg.id})")
    with Storage(db) as storage:
        report = run_pipeline(
            cfg,
            storage,
            max_items=max_items,
            max_download=limit if limit else None,
        )
        typer.echo(
            f"discovered={report.discovered} downloaded={report.downloaded} "
            f"extracted_ok={report.extracted_ok} inserted={report.inserted} "
            f"duplicates={report.duplicates} failed={report.failed}"
        )
        for err in report.errors[:20]:
            typer.echo(f"  ! {err}")
        typer.echo(f"DB now has {storage.count()} articles total.")


@app.command("list")
def list_cmd(
    site: str | None = typer.Option(None, "--site", help="Filter by site id"),
    limit: int = typer.Option(20, "--limit", help="Max rows"),
    db: Path = typer.Option(DEFAULT_DB, "--db", help="SQLite database path"),
) -> None:
    """List recent articles from the database."""
    with Storage(db) as storage:
        rows = storage.list_articles(limit=limit, source_id=site)
        for r in rows:
            authors = ", ".join(r["authors"]) or "-"
            typer.echo(
                f"[{r['id']}] {r['title']} | {authors} | "
                f"{r['published_at'] or 'n/a'} | {r['canonical_url']}"
            )
        typer.echo(f"-- {len(rows)} rows --")


@app.command()
def export(
    format: str = typer.Option("jsonl", "--format", help="jsonl | markdown"),
    site: str | None = typer.Option(None, "--site", help="Filter by site id"),
    limit: int = typer.Option(0, "--limit", help="Max rows (0 = all)"),
    db: Path = typer.Option(DEFAULT_DB, "--db", help="SQLite database path"),
    out: Path = typer.Option(None, "--out", help="Output path (jsonl) or dir (markdown)"),
) -> None:
    """Export articles to JSONL or Markdown (derived from SQLite)."""
    fmt = format.lower()
    if fmt not in ("jsonl", "markdown", "md"):
        raise typer.BadParameter("--format must be jsonl or markdown")
    with Storage(db) as storage:
        rows = storage.list_articles(limit=limit or 10_000, source_id=site)
    if fmt == "jsonl":
        path = out or (DEFAULT_EXPORT_DIR / "articles.jsonl")
        export_jsonl(rows, path)
        typer.echo(f"Exported {len(rows)} articles to {path}")
    else:
        directory = out or (DEFAULT_EXPORT_DIR / "markdown")
        written = export_markdown(rows, directory)
        typer.echo(f"Exported {len(written)} markdown files under {directory}")


@app.command()
def status(
    db: Path = typer.Option(DEFAULT_DB, "--db", help="SQLite database path"),
) -> None:
    """Show database and fetch status."""
    with Storage(db) as storage:
        typer.echo(json.dumps(storage.status(), ensure_ascii=False, indent=2))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
