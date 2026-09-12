# Huxiu formal integration

This change registers the real-page-validated Huxiu prototype as a normal
Laxinwen source without changing `news.model.Article`, SQLite schema, Reader,
Export, or Scheduler storage formats.

## Source wiring

| Huxiu | Laxinwen | Result |
|---|---|---|
| `sites/huxiu.yaml` article channel | `lists[0].url` / `HuxiuAdapter.discover()` | registered discovery source |
| canonical article URL | `Article.canonical_url` | mapped and URL-deduplicated |
| title | `Article.title` | mapped |
| author | `Article.authors` | mapped |
| published time | `Article.published_at` | timezone-aware UTC |
| article DOM | `Article.body_html`, `Article.body_text` | mapped |
| ordered article images | `Article.images`, `Article.lead_image` | mapped as absolute URLs |
| `section`, `tags`, `original_source`, `blocks` | `HuxiuExtractionResult.metadata` | prototype metadata only |

The fields that do not exist on the current Article model remain in the
extraction result metadata; no migration is required.

## Pipeline path

`Pipeline.run_site("huxiu")` loads the site YAML, resolves `HuxiuAdapter`,
discovers the server-rendered `/article/` cards, applies the existing storage
URL/title deduplication, fetches article pages with the existing HTTPX fetcher,
extracts with the Huxiu parser, and persists through the existing
`Storage.insert_article` / `update_article_body` methods. The configured
three-second article interval is applied by the existing pipeline hook.

The GUI source list and scheduler source validation include `huxiu`; selecting
`all` therefore includes it. Existing sources keep their previous adapters and
configuration.

## Verification

The checked-in real HTML fixtures cover 20 articles. The formal live run on
2026-09-12 used the current channel and three current article pages:

```text
discovered=3, deduplicated=3, fetched_ok=3,
extracted_ok=3, stored=3, failed=0
```

The detailed live result is recorded in `huxiu-recon/formal-e2e.json`.

The channel's observed `?page=2` and `?page=3` responses repeated the first
window, so pagination is intentionally not invented or enabled. Future work
should monitor for a documented stable cursor/load-more endpoint.
