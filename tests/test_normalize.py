"""URL 规范化与标题指纹测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.normalize import canonicalize_url, title_fingerprint  # noqa: E402


class TestCanonicalizeUrl:
    def test_remove_fragment(self):
        assert canonicalize_url("https://eco.sapo.pt/a/b/#comments") == (
            "https://eco.sapo.pt/a/b/"
        )

    def test_remove_tracking_params(self):
        url = (
            "https://eco.sapo.pt/2026/08/08/x/?utm_source=twitter"
            "&utm_medium=social&fbclid=abc&gclid=def&keep=1"
        )
        assert canonicalize_url(url) == "https://eco.sapo.pt/2026/08/08/x/?keep=1"

    def test_domain_lowercase(self):
        assert canonicalize_url("https://ECO.SAPO.PT/2026/08/08/X/") == (
            "https://eco.sapo.pt/2026/08/08/X/"
        )

    def test_default_port_removed(self):
        assert canonicalize_url("https://eco.sapo.pt:443/a/") == (
            "https://eco.sapo.pt/a/"
        )

    def test_trailing_slash_preserved(self):
        # 不主动加/删尾部斜杠，避免误判不同路径
        assert canonicalize_url("https://eco.sapo.pt/a") == "https://eco.sapo.pt/a"

    def test_empty(self):
        assert canonicalize_url("") == ""
        assert canonicalize_url("   ") == ""


class TestTitleFingerprint:
    def test_nfkc_and_case(self):
        # 全角／半角统一 + 大小写
        fp = title_fingerprint("ＣＡＲＮＥＩＲＯ concorda")
        assert fp == "carneiro concorda"

    def test_site_suffix_removed(self):
        assert title_fingerprint("Título da notícia – ECO") == "titulo da noticia"
        assert title_fingerprint("Título - ECO") == "titulo"

    def test_punctuation_removed(self):
        assert title_fingerprint("Título: “A & B”!") == "titulo a b"

    def test_whitespace_collapsed(self):
        assert title_fingerprint("  A   B  ") == "a b"

    def test_same_story_different_sites_kept(self):
        # 不同站点对同一故事的标题不同 → 指纹不同，不应误判重复
        a = title_fingerprint("Trump recorre ao Supremo", site_suffixes=[" - Reuters"])
        b = title_fingerprint("Trump recorre ao Supremo após bloqueio", site_suffixes=[" | BBC"])
        assert a != b

    def test_empty(self):
        assert title_fingerprint("") == ""
