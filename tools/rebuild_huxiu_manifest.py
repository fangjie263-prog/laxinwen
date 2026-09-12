"""Rebuild the real-sample manifest from the completed Phase 2 evidence."""

from __future__ import annotations

import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
recon = root / "huxiu-recon"
samples = json.loads((recon / "phase2-samples.json").read_text(encoding="utf-8"))
manifest = [
    {
        "id": row["id"],
        "url": row["url"],
        "title": row["title"],
        "discovered_from": "real Phase 2 channel/home discovery",
        "article_type": "real_html",
        "notes": "Downloaded 2026-09-12; see phase2-samples.json for structure metrics.",
    }
    for row in samples[:20]
]
(recon / "articles.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"wrote {len(manifest)} real article records")
