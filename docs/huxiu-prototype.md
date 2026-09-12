# Huxiu Prototype

Captured and validated against real Huxiu pages on 2026-09-12. The raw HTML is
under `huxiu-recon/`; the copied fixtures under `tests/fixtures/huxiu/` retain
the original page bytes. No production site YAML, database schema, GUI,
scheduler, or existing source was changed.

## Discovery report

The recommended candidate is `https://www.huxiu.com/article/`. The saved page
contains twelve server-rendered `.article-item-wrap` cards, real
`/article/<id>.html` links, visible card titles/authors, and `#__NUXT_DATA__`.
The desktop and mobile home pages also contain article links and Nuxt state, but
they mix articles with videos, discussions, and recommendations. The channel
page is therefore the cleaner prototype source. Discovery normalizes both
`www.huxiu.com` and `m.huxiu.com` to the desktop canonical article URL and
removes tracking parameters through the existing canonicalizer.

No `page=2`, load-more control, or stable pagination endpoint was observed in
the captured channel HTML. Long-term pagination is **not verified**. Nuxt
configuration exposes API hostnames, but no endpoint path was inferred or
called; API use is therefore only a lead, not a recommendation.

## Article structure report

Observed detail-page selectors:

| Huxiu field | Real evidence | Prototype handling |
|---|---|---|
| title | `.article__title`, fallback `og:title`/`h1` | mapped |
| author | `.article-author-info .author-info__username` | mapped |
| published | `.article__time`; Nuxt `dateline` confirms Unix timestamp | aware UTC datetime |
| section | `.article-type-channel` | metadata-only |
| tags | present in embedded Nuxt devalue state as `tag_name` entries | metadata flag; no guessed names |
| canonical | `og:url` / article URL | mapped |
| body | `.article__content` | Huxiu-specific DOM parser |
| image | content `img[src]`, `data-src`, or observed `_src` | absolute URL, ordered block |
| caption | `.text-img-note`, `figcaption` | ordered caption block |
| original source | `.article__reprinted-explain` | metadata-only; observed pages can say network source without naming a publisher |

The parser removes scripts/styles and known related/comment/user/reprint
containers before walking paragraphs, headings, lists, blockquotes, figures,
images, and videos. The resulting `blocks` preserve document order. It does
not treat maximum character count as proof of quality; the tests assert minimum
length plus pollution boundaries.

## Benchmark

`huxiu-recon/benchmark.json` compares the Huxiu parser, Trafilatura, and the
existing `news.extract.extract_article` on all eight real detail pages. The
Huxiu parser consistently supplies the observed title/author/time fields and
the site content container, while Trafilatura and the existing generic path are
useful fallbacks but do not preserve Huxiu's ordered image/caption blocks.
Exact per-article measurements are kept in JSON so they can be reviewed rather
than summarized by a single body-length score.

## Field mapping

`HuxiuExtractionResult.to_article()` fills only fields present in the existing
`news.model.Article`: `source_id`, `source_name`, `canonical_url`, `title`,
`body_text`, `body_html`, `authors`, `published_at`, `images`, `lead_image`,
and `language`. `section`, `tags`, `original_source`, `updated_at`, and
`blocks` stay in the prototype result's `metadata`; no Article/schema migration
was made.

## End-to-end and limitations

The independent adapter exposes `discover()`, `extract_article()`, and the
result-to-Article mapping. The saved real channel plus eight saved real article
pages provide an offline replay of discovery → fetch result → extraction →
normalization. A live HTTP run is performed by the final report separately;
pagination stability, rate-limit behavior over long runs, and a named external
publisher sample remain **unverified** in this prototype.

Recommendation: **YES, conditionally** for a future adapter. Use HTML channel
discovery plus canonical URL deduplication, the Huxiu-specific extractor for
body fidelity, and keep Article unchanged. Add a production adapter only after
live pagination/rate-limit monitoring and a confirmed reprinted-article sample.
