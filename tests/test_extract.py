"""正文提取测试（Trafilatura 参数、杂讯清理）。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.extract import _apply_clean_patterns, extract_article  # noqa: E402

_HTML = """<!DOCTYPE html>
<html lang="pt">
<head>
  <title>Notícia Teste - ECO</title>
  <meta property="article:published_time" content="2026-08-08T10:00:00+00:00"/>
  <meta property="og:image" content="https://eco.imgix.net/uploads/x.jpg"/>
  <link rel="canonical" href="https://eco.sapo.pt/2026/08/08/noticia-teste/"/>
</head>
<body>
  <article>
    <h1>Notícia Teste</h1>
    <p class="author">Por <a>Maria Silva</a></p>
    <p>Primeiro parágrafo do corpo da notícia.</p>
    <p>Segundo parágrafo com mais conteúdo para o teste.</p>
    <p>Escolha o ECO como fonte preferida no Google</p>
  </article>
</body>
</html>
"""


class TestCleanPatterns:
    def test_remove_pattern_line(self):
        text = "linha 1\nEscolha o ECO como fonte preferida no Google\nlinha 3"
        out = _apply_clean_patterns(text, ["Escolha o ECO como fonte preferida no Google"])
        assert "Escolha o ECO" not in out
        assert "linha 1" in out and "linha 3" in out

    def test_invalid_pattern_ignored(self):
        text = "linha 1"
        assert _apply_clean_patterns(text, ["["]) == text  # 非法正则不报错

    def test_empty(self):
        assert _apply_clean_patterns("", ["x"]) == ""


class TestExtractArticle:
    def test_extract_basic(self):
        res = extract_article(_HTML, url="https://eco.sapo.pt/2026/08/08/noticia-teste/")
        assert "Notícia Teste" in res.title
        assert res.text
        assert "Primeiro parágrafo" in res.text
        assert res.published_at is not None
        assert res.published_at.hour == 10

    def test_extract_clean_patterns_from_site_config(self):
        site_cfg = {
            "favor_recall": True,
            "clean_patterns": ["Escolha o ECO como fonte preferida no Google", "Assine o ECO Premium.*"],
        }
        res = extract_article(_HTML, url="https://eco.sapo.pt/2026/08/08/noticia-teste/", site_extract=site_cfg)
        assert "Escolha o ECO" not in res.text
