import pytest

from arxiv_marker import resolvers
from arxiv_marker.resolvers import DBLP, SemanticScholar, _is_nonvenue


class TestIsNonvenue:
    @pytest.mark.parametrize("v", [None, "", "arXiv", "arXiv.org", "CoRR", "corr", "preprint"])
    def test_nonvenue(self, v):
        assert _is_nonvenue(v)

    @pytest.mark.parametrize("v", ["NeurIPS", "Communications of the ACM"])
    def test_real_venue(self, v):
        assert not _is_nonvenue(v)


class TestSemanticScholarParsing:
    def test_batch_parsing(self, monkeypatch):
        s2 = SemanticScholar(api_key="k")
        rec = {
            "publicationVenue": {"name": "International Conference on Learning Representations",
                                 "type": "conference", "alternate_names": ["ICLR"], "issn": None},
            "externalIds": {"DOI": "10.48550/arXiv.2106.09685"},
            "venue": "ICLR 2021", "year": 2021,
            "citationCount": 19435, "influentialCitationCount": 100,
        }
        monkeypatch.setattr(s2, "_post_with_retry", lambda payload: [rec])
        hit = s2.batch_by_arxiv(["2106.09685"])["2106.09685"]
        assert hit.venue_raw == "International Conference on Learning Representations"
        assert hit.abbrev == "ICLR"
        assert hit.citation_count == 19435
        assert hit.influential_citations == 100
        assert hit.year == 2021

    def test_arxiv_only_record_has_no_venue(self, monkeypatch):
        s2 = SemanticScholar(api_key="k")
        rec = {"venue": "arXiv.org", "year": 2022, "citationCount": 5}
        monkeypatch.setattr(s2, "_post_with_retry", lambda payload: [rec])
        hit = s2.batch_by_arxiv(["2200.00001"])["2200.00001"]
        assert hit.venue_raw is None
        assert hit.citation_count == 5

    def test_empty_when_no_records(self, monkeypatch):
        s2 = SemanticScholar(api_key="k")
        monkeypatch.setattr(s2, "_post_with_retry", lambda payload: None)
        assert s2.batch_by_arxiv(["2106.09685"]) == {}

    def test_journal_venue_type_from_publication_types(self, monkeypatch):
        # Regression: TNNLS / Science Robotics — S2 returns publicationVenue with name + issn
        # but NO `type`, plus publicationTypes=["JournalArticle"]. Reading only pv.type left
        # venue_type=None, so the proposal defaulted these journals to conferencePaper.
        s2 = SemanticScholar(api_key="k")
        rec = {
            "publicationVenue": {  # note: no "type" key (exactly what S2 returns here)
                "name": "IEEE Transactions on Neural Networks and Learning Systems",
                "issn": "2162-237X"},
            "venue": "IEEE Transactions on Neural Networks and Learning Systems",
            "publicationTypes": ["JournalArticle"], "year": 2023, "citationCount": 143,
        }
        monkeypatch.setattr(s2, "_post_with_retry", lambda payload: [rec])
        assert s2.batch_by_arxiv(["2207.10422"])["2207.10422"].venue_type == "journal"

    def test_conference_venue_type_from_publication_types(self, monkeypatch):
        s2 = SemanticScholar(api_key="k")
        rec = {"publicationVenue": {"name": "Some Conf"}, "venue": "Some Conf",
               "publicationTypes": ["Conference"]}
        monkeypatch.setattr(s2, "_post_with_retry", lambda payload: [rec])
        assert s2.batch_by_arxiv(["x"])["x"].venue_type == "conference"

    def test_venue_type_falls_back_to_issn(self, monkeypatch):
        # no pv.type, no usable publicationTypes, but an ISSN -> journal
        s2 = SemanticScholar(api_key="k")
        rec = {"publicationVenue": {"name": "Science Robotics", "issn": "2470-9476"},
               "venue": "Science Robotics"}
        monkeypatch.setattr(s2, "_post_with_retry", lambda payload: [rec])
        assert s2.batch_by_arxiv(["y"])["y"].venue_type == "journal"

    def test_explicit_pv_type_wins_over_publication_types(self, monkeypatch):
        s2 = SemanticScholar(api_key="k")
        rec = {"publicationVenue": {"name": "X", "type": "conference"},
               "venue": "X", "publicationTypes": ["JournalArticle"]}
        monkeypatch.setattr(s2, "_post_with_retry", lambda payload: [rec])
        assert s2.batch_by_arxiv(["z"])["z"].venue_type == "conference"

    def test_venue_type_none_without_any_signal(self, monkeypatch):
        s2 = SemanticScholar(api_key="k")
        rec = {"publicationVenue": {"name": "Mystery Venue"}, "venue": "Mystery Venue"}
        monkeypatch.setattr(s2, "_post_with_retry", lambda payload: [rec])
        assert s2.batch_by_arxiv(["m"])["m"].venue_type is None


class TestSemanticScholarRetry:
    def test_retries_on_429_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(resolvers.time, "sleep", lambda *_: None)
        s2 = SemanticScholar(api_key="k")
        calls = {"n": 0}

        class _Resp:
            def __init__(self, status):
                self.status_code = status

            def raise_for_status(self):
                pass

            def json(self):
                return [{"venue": "NeurIPS"}]

        def fake_post(url, params=None, json=None, timeout=None):
            calls["n"] += 1
            return _Resp(429) if calls["n"] == 1 else _Resp(200)

        monkeypatch.setattr(s2.s, "post", fake_post)
        assert s2._post_with_retry({"ids": []}) == [{"venue": "NeurIPS"}]
        assert calls["n"] == 2


class TestDBLP:
    @staticmethod
    def _hit(title, venue, year="2014", typ="Conference and Workshop Papers", author="Goodfellow"):
        return {"info": {"title": title, "venue": venue, "year": year, "type": typ,
                         "authors": {"author": [{"text": f"Ian {author}"}]},
                         "url": "https://dblp.org/x", "doi": "10.1/x"}}

    def test_best_by_title_conference(self, monkeypatch):
        d = DBLP()
        monkeypatch.setattr(
            d, "_search", lambda q: [self._hit("Generative Adversarial Nets", "NeurIPS")])
        hit = d.best_by_title("Generative Adversarial Nets", "Goodfellow", 2014)
        assert hit.venue_raw == "NeurIPS"
        assert hit.venue_type == "conference"
        assert hit.year == 2014

    def test_skips_nonvenue(self, monkeypatch):
        d = DBLP()
        monkeypatch.setattr(
            d, "_search", lambda q: [self._hit("Generative Adversarial Nets", "CoRR")])
        assert d.best_by_title("Generative Adversarial Nets", "Goodfellow", 2014) is None

    def test_title_mismatch_returns_none(self, monkeypatch):
        d = DBLP()
        monkeypatch.setattr(d, "_search", lambda q: [self._hit("Totally Different Title", "NeurIPS")])
        assert d.best_by_title("Generative Adversarial Nets", "", None) is None

    def test_loose_match_accepted_with_author_and_year(self, monkeypatch):
        # ~0.5 jaccard title, accepted only because author lastname AND year agree
        d = DBLP()
        monkeypatch.setattr(d, "_search", lambda q: [
            self._hit("Deep Residual Learning", "CVPR", year="2016", author="He")])
        hit = d.best_by_title("Deep Residual Learning for Image Recognition", "He", 2016)
        assert hit is not None and hit.venue_raw == "CVPR"

    def test_loose_match_rejected_when_year_disagrees(self, monkeypatch):
        d = DBLP()
        monkeypatch.setattr(d, "_search", lambda q: [
            self._hit("Deep Residual Learning", "CVPR", year="1999", author="He")])
        assert d.best_by_title("Deep Residual Learning for Image Recognition", "He", 2016) is None
