import pytest
import requests

from arxiv_marker import resolvers
from arxiv_marker.resolvers import (
    DBLP,
    OpenReview,
    SemanticScholar,
    _is_nonvenue,
    _openreview_norm_title,
)


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


class TestOpenReview:
    @staticmethod
    def _note(title, venue="ICML 2026 spotlight", venue_id="ICML.cc/2026/Conference",
              authors=None, forum="forum-id", **content):
        values = {
            "title": {"value": title},
            "authors": {"value": authors or ["Ada Lovelace", "Grace Hopper"]},
            "venue": {"value": venue},
            "venueid": {"value": venue_id},
            **{k: {"value": v} for k, v in content.items()},
        }
        return {"id": forum, "forum": forum, "domain": venue_id, "content": values}

    @staticmethod
    def _response(payload, status=200, invalid_json=False, headers=None):
        response_headers = headers or {}

        class _Response:
            status_code = status
            headers = response_headers

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise requests.HTTPError(str(self.status_code))

            def json(self):
                if invalid_json:
                    raise ValueError("invalid JSON")
                return payload

        return _Response()

    def _resolve(self, monkeypatch, notes, title, authors, year, arxiv_id):
        resolver = OpenReview()
        monkeypatch.setattr(
            resolver.s, "get", lambda *args, **kwargs: self._response({"notes": notes}))
        return resolver.best_by_title(title, authors, year, arxiv_id)

    def test_math_tex_delimiters_normalize_without_erasing_expression(self):
        arxiv = "From $f(x)$ and $g(x)$ to $f(g(x))$: LLMs Learn New Skills"
        rendered = "From f(x) and g(x) to f(g(x)): LLMs Learn New Skills"
        assert _openreview_norm_title(arxiv) == _openreview_norm_title(rendered)
        assert "f(g(x))" in _openreview_norm_title(arxiv)

    def test_searches_tex_title_once_without_display_delimiters(self, monkeypatch):
        resolver = OpenReview()
        title = "From $f(x)$ and $g(x)$ to $f(g(x))$: LLMs Learn New Skills"
        note = self._note(
            "From f(x) and g(x) to f(g(x)): LLMs Learn New Skills",
            "ICLR 2026 Poster", "ICLR.cc/2026/Conference", ["Lifan Yuan"])
        terms = []

        def get(*args, **kwargs):
            terms.append(kwargs["params"]["term"])
            return self._response({"notes": [note]})

        monkeypatch.setattr(resolver.s, "get", get)
        hit = resolver.best_by_title(title, ["Lifan Yuan"], 2026, "2509.25123")
        assert hit.venue_raw == "ICLR 2026"
        assert len(terms) == 1 and "$" not in terms[0]

    def test_ragen_prefers_main_conference_over_workshop(self, monkeypatch):
        title = "RAGEN-2: Reasoning Collapse in Agentic RL"
        authors = ["Zihan Wang", "Chi Gui", "Xing Jin"]
        notes = [
            self._note(title, "ICML 2026 RLxF Workshop", "ICML.cc/2026/Workshop/RLxF",
                       authors, "workshop"),
            self._note(title, "ICML 2026 spotlight", "ICML.cc/2026/Conference",
                       authors, "01caH9oj7C"),
        ]
        hit = self._resolve(monkeypatch, notes, title, authors, 2026, "2604.06268")
        assert hit.source == "openreview"
        assert hit.venue_raw == "ICML 2026"
        assert hit.year == 2026
        assert hit.venue_type == "conference"
        assert hit.evidence_url.endswith("01caH9oj7C")

    def test_flexibility_trap_resolves_icml(self, monkeypatch):
        title = ("The Flexibility Trap: Rethinking the Value of Arbitrary Order in "
                 "Diffusion Language Models")
        note = self._note(title, authors=["Zanlin Ni", "Shenzhi Wang"],
                          forum="kpgURPRMGf", doi="10.1234/flex")
        hit = self._resolve(monkeypatch, [note], title, ["Zanlin Ni"], 2026, "2601.15165")
        assert hit.venue_raw == "ICML 2026"
        assert hit.external_doi == "10.1234/flex"

    def test_short_tex_title_matches_colon_subtitle_with_author(self, monkeypatch):
        query = "From $f(x)$ and $g(x)$ to $f(g(x))$"
        full = ("From f(x) and g(x) to f(g(x)): LLMs Learn New Skills in RL by "
                "Composing Old Ones")
        note = self._note(full, "ICLR 2026 Poster", "ICLR.cc/2026/Conference",
                          ["Lifan Yuan", "Weize Chen"], "jt7oCtYqHE")
        hit = self._resolve(monkeypatch, [note], query, ["Lifan Yuan"], 2025, "2509.25123")
        assert hit.venue_raw == "ICLR 2026"
        assert hit.evidence_url.endswith("jt7oCtYqHE")

    @pytest.mark.parametrize("venue,venue_id", [
        ("Submitted to ICLR 2026", "ICLR.cc/2026/Conference/Submission"),
        ("Under Review", "ICML.cc/2026/Conference/Submission"),
        ("ICLR 2026 Rejected Submission", "ICLR.cc/2026/Conference/Rejected_Submission"),
        ("Withdrawn Submission", "ICLR.cc/2026/Conference/Withdrawn_Submission"),
        ("Desk Rejected", "ICLR.cc/2026/Conference/Desk_Rejected_Submission"),
        ("Poster", "ICLR.cc/2026/Conference"),
    ])
    def test_rejects_non_published_states(self, monkeypatch, venue, venue_id):
        title = "Exact Title"
        note = self._note(title, venue, venue_id, ["Ada Lovelace"])
        assert self._resolve(monkeypatch, [note], title, ["Ada Lovelace"], 2026, None) is None

    @pytest.mark.parametrize("field,value", [
        ("decision", "Reject"),
        ("status", "Under Review"),
    ])
    def test_rejects_non_published_structured_status(
            self, monkeypatch, field, value):
        note = self._note("Exact Title", authors=["Ada Lovelace"], **{field: value})
        assert self._resolve(monkeypatch, [note], "Exact Title",
                             ["Ada Lovelace"], 2026, None) is None

    def test_rejects_corr_and_dblp_mirrors(self, monkeypatch):
        title = "Exact Title"
        corr = self._note(title, "CoRR", "arXiv/2601.00001", ["Ada Lovelace"], "corr")
        dblp = self._note(title, authors=["Ada Lovelace"], forum="dblp")
        dblp["domain"] = "dblp.org/rec/conf/icml"
        assert self._resolve(monkeypatch, [corr, dblp], title,
                             ["Ada Lovelace"], 2026, None) is None

    def test_rejects_exact_title_author_collision(self, monkeypatch):
        note = self._note("Same Title", authors=["Different Person"])
        assert self._resolve(monkeypatch, [note], "Same Title",
                             ["Ada Lovelace"], 2026, None) is None
        shared_nonfirst = self._note(
            "Same Title", authors=["Different Person", "Ada Lovelace"])
        assert self._resolve(monkeypatch, [shared_nonfirst], "Same Title",
                             ["Ada Lovelace"], 2026, None) is None

    def test_accepts_comma_separated_author_name(self, monkeypatch):
        note = self._note("Same Title", authors=["Lovelace, Ada"])
        hit = self._resolve(monkeypatch, [note], "Same Title",
                            ["Ada Lovelace"], 2026, None)
        assert hit is not None and hit.venue_raw == "ICML 2026"

    def test_abstains_when_equally_strong_venues_conflict(self, monkeypatch):
        title = "Same Title"
        notes = [
            self._note(title, "ICML 2026 Oral", "ICML.cc/2026/Conference",
                       ["Ada Lovelace"], "icml"),
            self._note(title, "ICLR 2026 Oral", "ICLR.cc/2026/Conference",
                       ["Ada Lovelace"], "iclr"),
        ]
        assert self._resolve(monkeypatch, notes, title,
                             ["Ada Lovelace"], 2026, None) is None

    def test_retries_429_with_exponential_backoff_and_jitter(self, monkeypatch):
        delays = []
        calls = []
        resolver = OpenReview(sleep=delays.append, random_fn=lambda: 0.5)

        def throttled(*args, **kwargs):
            calls.append(1)
            return self._response({}, status=429)

        monkeypatch.setattr(resolver.s, "get", throttled)
        assert resolver.best_by_title("T") is None
        assert len(calls) == 3
        assert delays == [1.5, 2.5]

    def test_respects_retry_after_then_succeeds(self, monkeypatch):
        delays = []
        note = self._note("Exact Title", authors=["Ada Lovelace"])
        responses = iter([
            self._response({}, status=429, headers={"Retry-After": "3"}),
            self._response({"notes": [note]}),
        ])
        resolver = OpenReview(sleep=delays.append, random_fn=lambda: 0.5)
        monkeypatch.setattr(resolver.s, "get", lambda *a, **k: next(responses))
        hit = resolver.best_by_title("Exact Title", ["Ada Lovelace"], 2026)
        assert hit is not None and hit.venue_raw == "ICML 2026"
        assert delays == [3.0]

    def test_retry_after_is_not_capped_by_local_backoff(self, monkeypatch):
        delays = []
        resolver = OpenReview(max_retries=1, sleep=delays.append)
        monkeypatch.setattr(
            resolver.s, "get",
            lambda *a, **k: self._response(
                {}, status=429, headers={"Retry-After": "999"}))
        assert resolver.best_by_title("T") is None
        assert delays == [999.0]

    def test_non_429_http_failure_is_not_retried(self, monkeypatch):
        calls = []
        resolver = OpenReview(sleep=lambda _: None)

        def failure(*args, **kwargs):
            calls.append(1)
            return self._response({}, status=500)

        monkeypatch.setattr(resolver.s, "get", failure)
        assert resolver.best_by_title("T") is None
        assert len(calls) == 1

    def test_timeout_returns_none(self, monkeypatch):
        resolver = OpenReview()

        def timeout(*args, **kwargs):
            raise requests.Timeout

        monkeypatch.setattr(resolver.s, "get", timeout)
        assert resolver.best_by_title("T") is None

    @pytest.mark.parametrize("payload,invalid", [
        ({}, True),
        ({"unexpected": []}, False),
        ({"notes": []}, False),
        ({"notes": [{"content": {}}]}, False),
    ])
    def test_malformed_or_empty_payload_returns_none(self, monkeypatch, payload, invalid):
        resolver = OpenReview()
        monkeypatch.setattr(
            resolver.s, "get", lambda *a, **k: self._response(payload, invalid_json=invalid))
        assert resolver.best_by_title("T") is None


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
