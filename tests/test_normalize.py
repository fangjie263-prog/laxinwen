"""Unit tests for URL canonicalization and title fingerprinting."""

import pytest

from laxinwen.normalize import canonicalize_url, title_fingerprint


@pytest.mark.parametrize(
    "raw,expected",
    [
        # fragment stripping
        (
            "https://eco.sapo.pt/2026/08/08/a/#comments",
            "https://eco.sapo.pt/2026/08/08/a/",
        ),
        # utm params stripped
        (
            "https://eco.sapo.pt/x/?utm_source=rss&utm_medium=feed&id=5",
            "https://eco.sapo.pt/x/?id=5",
        ),
        # fbclid / gclid stripped
        (
            "https://example.com/a?fbclid=abc&gclid=def&page=2",
            "https://example.com/a?page=2",
        ),
        # host lowercasing + default port removal
        (
            "HTTPS://ECO.Sapo.PT:443/2026/08/08/x/",
            "https://eco.sapo.pt/2026/08/08/x/",
        ),
        # trailing slash preserved
        ("https://eco.sapo.pt/2026/08/08/x", "https://eco.sapo.pt/2026/08/08/x"),
        # query params sorted deterministically
        (
            "https://example.com/a?z=1&a=2",
            "https://example.com/a?a=2&z=1",
        ),
        # empty
        ("", ""),
    ],
)
def test_canonicalize_url(raw, expected):
    assert canonicalize_url(raw) == expected


def test_canonicalize_url_dedup_collision():
    a = canonicalize_url("https://eco.sapo.pt/x/?utm_source=feed#top")
    b = canonicalize_url("https://eco.sapo.pt/x/")
    assert a == b


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Hello World!", "helloworld"),
        ("  Hello   World  ", "helloworld"),
        ("Économie & Finance", "économiefinance"),
        ("Reuters: Breaking News", "reutersbreakingnews"),
        ("", ""),
    ],
)
def test_title_fingerprint(title, expected):
    assert title_fingerprint(title) == expected


def test_title_fingerprint_strips_site_suffix():
    assert (
        title_fingerprint("Some headline - ECO", strip_site_suffix="ECO")
        == "someheadline"
    )


def test_title_fingerprint_unicode_nfkc():
    # fullwidth characters normalize via NFKC
    assert title_fingerprint("Ｈｅｌｌｏ") == "hello"
