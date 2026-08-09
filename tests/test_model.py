"""Tests for the Article model and its serialization."""

from datetime import datetime, timezone

from laxinwen.model import Article


def test_article_defaults():
    a = Article(source_id="eco", source_name="ECO", canonical_url="https://eco.sapo.pt/x/")
    assert a.status == "new"
    assert a.authors == []
    assert a.errors == []
    assert a.published_at is None


def test_published_at_iso_utc():
    a = Article(
        source_id="eco",
        source_name="ECO",
        canonical_url="https://eco.sapo.pt/x/",
        published_at=datetime(2026, 8, 8, 22, 23, 34, tzinfo=timezone.utc),
    )
    assert a.published_at_iso() == "2026-08-08T22:23:34+00:00"


def test_published_at_naive_becomes_utc():
    a = Article(
        source_id="eco",
        source_name="ECO",
        canonical_url="https://eco.sapo.pt/x/",
        published_at=datetime(2026, 8, 8, 22, 23, 34),
    )
    assert a.published_at_iso().endswith("+00:00")


def test_to_dict():
    a = Article(
        source_id="eco",
        source_name="ECO",
        canonical_url="https://eco.sapo.pt/x/",
        title="Título",
        authors=["Lusa"],
        body_text="Corpo.",
    )
    d = a.to_dict()
    assert d["title"] == "Título"
    assert d["authors"] == ["Lusa"]
    assert d["body_text"] == "Corpo."
    assert d["canonical_url"] == "https://eco.sapo.pt/x/"
