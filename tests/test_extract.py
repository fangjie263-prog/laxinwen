"""Offline test for article extraction from a real-shaped ECO article HTML."""

from laxinwen.extract import extract_article

ARTICLE_HTML = """<!DOCTYPE html>
<html lang="pt-PT">
<head>
  <meta charset="UTF-8">
  <link rel="canonical" href="https://eco.sapo.pt/2026/08/08/titulo-da-noticia/">
  <meta property="og:title" content="Título da Notícia">
  <meta property="og:image" content="https://eco.imgix.net/uploads/2026/06/foto.jpg">
  <meta property="article:published_time" content="2026-08-08T22:23:34Z">
  <script type="application/ld+json">
  {"@type":"NewsArticle","author":[{"@type":"Person","name":"Lusa"}]}
  </script>
</head>
<body>
  <nav><a href="/">Home</a> <a href="/economia">Economia</a></nav>
  <article>
    <header>
      <h1>Título da Notícia</h1>
      <span class="meta__info">Lusa</span>
    </header>
    <div class="entry__content">
      <p>Primeiro parágrafo da notícia com conteúdo relevante.</p>
      <p>Segundo parágrafo com mais detalhes sobre o assunto.</p>
      <p>Terceiro parágrafo concluindo a notícia.</p>
      <img src="https://eco.imgix.net/uploads/2026/06/foto2.jpg">
    </div>
    <aside class="related">Artigo recomendado aqui</aside>
  </article>
</body>
</html>
"""


def test_extract_article_fields():
    art = extract_article(
        "eco", "ECO – Economia Online",
        "https://eco.sapo.pt/2026/08/08/titulo-da-noticia/",
        ARTICLE_HTML,
    )
    assert art.title == "Título da Notícia"
    assert art.canonical_url == "https://eco.sapo.pt/2026/08/08/titulo-da-noticia/"
    assert art.published_at is not None
    assert art.published_at_iso() == "2026-08-08T22:23:34+00:00"
    assert "Lusa" in art.authors
    assert "Primeiro parágrafo" in art.body_text
    assert "relevante" in art.body_text
    assert "Terceiro parágrafo" in art.body_text
    assert art.language == "pt-PT"
    assert art.lead_image and art.lead_image.startswith("https://eco.imgix.net")
    assert any("foto2.jpg" in img for img in art.images)
    assert art.status == "ok"


def test_extract_article_fallback_body():
    html = ARTICLE_HTML.replace('<div class="entry__content">', '<div>').replace(
        '<p>Primeiro parágrafo da notícia com conteúdo relevante.</p>', ""
    ).replace('<p>Segundo parágrafo com mais detalhes sobre o assunto.</p>', "")
    art = extract_article(
        "eco", "ECO",
        "https://eco.sapo.pt/2026/08/08/titulo-da-noticia/",
        html,
    )
    # Title still extracted even if body extraction produced little.
    assert art.title == "Título da Notícia"


def test_extract_removes_site_chrome_lines():
    html = ARTICLE_HTML.replace(
        "<p>Terceiro parágrafo concluindo a notícia.</p>",
        "<p>Terceiro parágrafo concluindo a notícia.</p>"
        "<p>Escolha o ECO como fonte preferida no Google</p>",
    )
    art = extract_article(
        "eco", "ECO",
        "https://eco.sapo.pt/2026/08/08/titulo-da-noticia/",
        html,
        site_extract={"exclude_phrases": ["Escolha o ECO como fonte preferida no Google"]},
    )
    assert "Escolha o ECO" not in art.body_text
    assert "Terceiro parágrafo" in art.body_text
