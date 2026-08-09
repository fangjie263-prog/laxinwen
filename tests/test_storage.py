"""Article 数据模型与 SQLite 存储测试。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.model import Article, utcnow  # noqa: E402
from news.normalize import fingerprint_sha256  # noqa: E402
from news.storage import Storage  # noqa: E402


def _article(url="https://eco.sapo.pt/2026/08/08/a/", title="Notícia A", body_text="corpo"):
    return Article(
        source_id="eco",
        source_name="ECO",
        canonical_url=url,
        title=title,
        authors=["Lusa"],
        published_at=utcnow(),
        body_text=body_text,
        language="pt-PT",
    )


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "test.db")
    yield s
    s.close()


class TestArticleModel:
    def test_to_dict_iso8601(self):
        art = _article()
        d = art.to_dict()
        assert "T" in d["published_at"] and d["published_at"].endswith("+00:00")

    def test_to_dict_json_serializable(self):
        import json

        art = _article()
        json.dumps(art.to_dict(), ensure_ascii=False)

    def test_default_utc_now(self):
        art = Article(source_id="s", source_name="S", canonical_url="u", title="t")
        assert art.discovered_at.tzinfo is not None


class TestStorage:
    def test_insert_and_get(self, storage):
        art = _article()
        rowid, inserted = storage.insert_article(art, title_fp="fp1")
        assert inserted is True
        got = storage.get_article(rowid)
        assert got is not None
        assert got.title == "Notícia A"
        assert got.authors == ["Lusa"]
        assert got.language == "pt-PT"

    def test_unique_url_constraint(self, storage):
        art1 = _article()
        art2 = _article(title="Outra notícia")  # 同 URL 不同标题
        id1, ins1 = storage.insert_article(art1, title_fp="fp1")
        id2, ins2 = storage.insert_article(art2, title_fp="fp2")
        assert ins1 is True and ins2 is False
        assert id1 == id2  # 返回已存在行 id
        assert storage.count() == 1

    def test_duplicate_different_urls_kept(self, storage):
        _, ins1 = storage.insert_article(
            _article(url="https://eco.sapo.pt/2026/08/08/a/"), title_fp="fp1"
        )
        _, ins2 = storage.insert_article(
            _article(url="https://eco.sapo.pt/2026/08/08/b/"), title_fp="fp2"
        )
        assert ins1 and ins2
        assert storage.count() == 2

    def test_update_body(self, storage):
        art = _article(body_text="")
        rowid, _ = storage.insert_article(art, title_fp="fp")
        storage.update_article_body(
            rowid,
            title="Título atualizado",
            authors=["A", "B"],
            body_text="corpo completo",
            body_html="<p>corpo</p>",
            images=["https://img/1.jpg"],
            lead_image="https://img/1.jpg",
            published_at=utcnow(),
            fetched_at=utcnow(),
            language="pt-PT",
            status="fetched",
        )
        got = storage.get_article(rowid)
        assert got.body_text == "corpo completo"
        assert got.authors == ["A", "B"]
        assert got.status == "fetched"

    def test_mark_failed(self, storage):
        art = _article(body_text="")
        rowid, _ = storage.insert_article(art, title_fp="fp")
        storage.mark_failed(rowid, error="timeout")
        got = storage.get_article(rowid)
        assert got.status == "failed"
        assert "timeout" in got.body_text

    def test_mark_failed_does_not_overwrite_body(self, storage):
        art = _article()  # 已有正文
        rowid, _ = storage.insert_article(art, title_fp="fp")
        storage.mark_failed(rowid, error="timeout")
        got = storage.get_article(rowid)
        assert got.status == "failed"
        assert got.body_text == "corpo"  # 不覆盖已有正文

    def test_title_fp_exists(self, storage):
        storage.insert_article(_article(), title_fp="sha-1")
        assert storage.title_fp_exists("eco", "sha-1") is True
        assert storage.title_fp_exists("eco", "sha-2") is False
        assert storage.title_fp_exists("outro-site", "sha-1") is False

    def test_count_by_status(self, storage):
        a1 = _article()
        a2 = _article(url="https://eco.sapo.pt/2026/08/08/b/")
        id1, _ = storage.insert_article(a1, title_fp="fp1")
        id2, _ = storage.insert_article(a2, title_fp="fp2")
        storage.mark_failed(id1, error="err")
        assert storage.count_by_status() == {"new": 1, "failed": 1}
        assert storage.count() == 2
