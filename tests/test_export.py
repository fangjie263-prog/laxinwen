"""导出测试（JSONL / Markdown）。"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.export import export_jsonl, export_markdown  # noqa: E402
from news.model import Article  # noqa: E402
from news.storage import Storage  # noqa: E402


@pytest.fixture
def storage(tmp_path):
    from datetime import datetime, timedelta, timezone

    s = Storage(tmp_path / "test.db")
    base = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
    for i, title in enumerate(["Primeira", "Segunda", "Terceira"]):
        s.insert_article(
            Article(
                source_id="eco",
                source_name="ECO",
                canonical_url=f"https://eco.sapo.pt/2026/08/0{8}/{i}/",
                title=title,
                authors=["Lusa"] if i % 2 == 0 else [],
                published_at=base + timedelta(hours=i),
                body_text=f"corpo {i}",
                language="pt-PT",
                status="fetched",
            ),
            title_fp=f"fp{i}",
        )
    yield s
    s.close()


class TestJsonlExport:
    def test_export_lines(self, storage, tmp_path):
        out = tmp_path / "news.jsonl"
        n = export_jsonl(storage, out)
        assert n == 3
        lines = out.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3
        first = json.loads(lines[0])
        assert first["title"] == "Terceira"  # 最新（时间倒序）在前
        assert first["source_id"] == "eco"
        assert first["published_at"].endswith("+00:00")

    def test_export_source_filter(self, storage, tmp_path):
        out = tmp_path / "news.jsonl"
        n = export_jsonl(storage, out, source_id="eco")
        assert n == 3
        n0 = export_jsonl(storage, out, source_id="nonexistent")
        assert n0 == 0


class TestMarkdownExport:
    def test_export_files(self, storage, tmp_path):
        outdir = tmp_path / "md"
        n = export_markdown(storage, outdir)
        assert n == 3
        files = sorted(outdir.rglob("*.md"))
        assert len(files) == 3
        # 目录结构 YYYY/MM/
        rels = {str(f.relative_to(outdir)) for f in files}
        assert all(len(Path(r).parts) == 3 for r in rels)  # YYYY/MM/xxx.md

    def test_frontmatter(self, storage, tmp_path):
        outdir = tmp_path / "md"
        export_markdown(storage, outdir)
        content = next(outdir.rglob("*.md")).read_text(encoding="utf-8")
        assert content.startswith("---")
        assert "title:" in content
        assert "source: ECO" in content
        assert "url: https://eco.sapo.pt/" in content
        assert "---" in content
