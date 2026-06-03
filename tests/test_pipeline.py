import pytest

from arxiv_marker import pipeline
from arxiv_marker.pipeline import (
    _choose_venue,
    _cite_bucket,
    _confidence,
    _item_year,
    build_tags,
    duplicate_arxiv_groups,
    resolve_items,
)
from arxiv_marker.resolvers import VenueHit


class TestCiteBucket:
    @pytest.mark.parametrize("n,expected", [
        (None, None), (4, "<10"), (10, "10+"), (60, "50+"), (150, "100+"),
        (600, "500+"), (1200, "1000+"), (6000, "5000+"), (15000, "10000+"),
    ])
    def test_buckets(self, n, expected):
        assert _cite_bucket(n) == expected


class TestItemYear:
    def test_iso(self):
        assert _item_year({"date": "2021-10-16"}) == 2021

    def test_year_only(self):
        assert _item_year({"date": "2024"}) == 2024

    def test_day_month_year(self):
        assert _item_year({"date": "16/03/1999"}) == 1999

    def test_missing(self):
        assert _item_year({"date": ""}) is None
        assert _item_year({}) is None


class TestChooseVenue:
    def test_prefers_conference_over_journal(self):
        j = VenueHit(source="semantic_scholar", venue_raw="Communications of the ACM")
        c = VenueHit(source="dblp", venue_raw="NeurIPS")
        chosen, row = _choose_venue([j, c])
        assert chosen.venue_raw == "NeurIPS"
        assert row["canonical"] == "NeurIPS"

    def test_unknown_venue_still_chosen_with_no_row(self):
        h = VenueHit(source="dblp", venue_raw="Some Workshop 2099")
        chosen, row = _choose_venue([h])
        assert chosen is h
        assert row is None

    def test_empty(self):
        assert _choose_venue([]) == (None, None)


class TestConfidence:
    def test_no_chosen_is_zero(self):
        assert _confidence([], None, None) == 0.0

    def test_unknown_venue_string(self):
        h = VenueHit(source="dblp", venue_raw="Some Workshop 2099")
        assert _confidence([h], h, None) == 0.6

    def test_single_known_source(self):
        h = VenueHit(source="semantic_scholar", venue_raw="NeurIPS")
        assert _confidence([h], h, {"canonical": "NeurIPS"}) == 0.85

    def test_two_sources_agree(self):
        h1 = VenueHit(source="semantic_scholar", venue_raw="NeurIPS")
        h2 = VenueHit(source="dblp", venue_raw="Advances in Neural Information Processing Systems")
        assert _confidence([h1, h2], h1, {"canonical": "NeurIPS"}) == 0.95


class TestBuildTags:
    def test_tags(self, make_resolution):
        res = make_resolution(canonical="ICLR", year=2021, core_tier="A*",
                              acceptance="accepted", citation_count=12000)
        tags = build_tags(res)
        assert {"venue:ICLR", "year:2021", "CORE:A*",
                "acceptance:accepted", "cite:10000+"} <= set(tags)


class TestDuplicateArxiv:
    def test_groups_only_repeated_ids(self, make_resolution):
        a = make_resolution(item_key="A", arxiv_id="2106.09685")
        b = make_resolution(item_key="B", arxiv_id="2106.09685")
        c = make_resolution(item_key="C", arxiv_id="2207.00001")
        d = make_resolution(item_key="D", arxiv_id=None)
        assert duplicate_arxiv_groups([a, b, c, d]) == {"2106.09685": ["A", "B"]}


class _FakeS2:
    def __init__(self, mapping):
        self.mapping = mapping

    def batch_by_arxiv(self, ids):
        return {k: v for k, v in self.mapping.items() if k in ids}


class _FakeDBLP:
    def __init__(self, hit=None):
        self.hit = hit
        self.calls = []

    def best_by_title(self, title, author, year):
        self.calls.append((title, author, year))
        return self.hit


class TestResolveItems:
    def test_journal_republication_prefers_original_conference(self, make_item):
        item = make_item(key="GAN", title="Generative Adversarial Nets",
                         archiveID="arXiv:1406.2661",
                         creators=[{"creatorType": "author", "lastName": "Goodfellow"}])
        s2 = _FakeS2({"1406.2661": VenueHit(
            source="semantic_scholar", venue_raw="Communications of the ACM", year=2014,
            venue_type="journal", citation_count=60000, external_doi="10.1145/3422622")})
        dblp = _FakeDBLP(VenueHit(source="dblp", venue_raw="NeurIPS", year=2014,
                                  venue_type="conference"))
        [res] = resolve_items([item], s2, dblp)
        assert res.canonical == "NeurIPS"
        assert res.core_tier == "A*"
        assert res.citation_count == 60000          # citations always come from S2
        assert res.target_item_type == "conferencePaper"
        assert res.confidence == 0.85               # only DBLP maps to NeurIPS

    def test_clean_conference_does_not_consult_dblp(self, make_item):
        item = make_item(key="K", title="Some ICLR Paper", archiveID="arXiv:2106.00001")
        s2 = _FakeS2({"2106.00001": VenueHit(
            source="semantic_scholar",
            venue_raw="International Conference on Learning Representations",
            year=2021, venue_type="conference", citation_count=10)})
        dblp = _FakeDBLP()
        [res] = resolve_items([item], s2, dblp)
        assert res.canonical == "ICLR"
        assert res.target_item_type == "conferencePaper"
        assert dblp.calls == []

    def test_journal_venue_not_in_table_becomes_journal_article(self, make_item):
        # Regression: a journal venue NOT in the ranking table (TNNLS / Science Robotics) must
        # convert to journalArticle with publicationTitle — NOT default to a conferencePaper
        # with the journal name written into proceedingsTitle/conferenceName.
        item = make_item(key="J", title="Differentiable Integrated Motion Planning",
                         archiveID="arXiv:2207.10422")
        s2 = _FakeS2({"2207.10422": VenueHit(
            source="semantic_scholar",
            venue_raw="IEEE Transactions on Neural Networks and Learning Systems",
            year=2023, venue_type="journal", citation_count=143,
            external_doi="10.1109/TNNLS.2023.3283542", issn="2162-237X")})
        [res] = resolve_items([item], s2, _FakeDBLP(None))
        assert res.kind == "journal"
        assert res.target_item_type == "journalArticle"
        assert res.fields["publicationTitle"] == \
            "IEEE Transactions on Neural Networks and Learning Systems"
        assert res.fields["ISSN"] == "2162-237X"
        assert "proceedingsTitle" not in res.fields
        assert "conferenceName" not in res.fields

    def test_unknown_when_no_venue(self, make_item):
        item = make_item(key="U", title="Unpublished Thing", archiveID="arXiv:2200.00001")
        s2 = _FakeS2({"2200.00001": VenueHit(source="semantic_scholar", venue_raw=None,
                                             citation_count=5)})
        [res] = resolve_items([item], s2, _FakeDBLP(None))
        assert res.acceptance == "unknown"
        assert res.target_item_type is None
        assert res.citation_count == 5             # citation count still recorded

    def test_manual_override_wins(self, make_item, monkeypatch):
        item = make_item(key="O", title="Whatever", archiveID="arXiv:2300.00001")
        s2 = _FakeS2({"2300.00001": VenueHit(source="semantic_scholar", venue_raw=None)})
        monkeypatch.setattr(
            pipeline.overrides, "get",
            lambda aid: {"canonical": "ICML", "year": 2015} if aid == "2300.00001" else None)
        [res] = resolve_items([item], s2, _FakeDBLP(None))
        assert res.canonical == "ICML"
        assert res.confidence == 1.0
        assert "override" in res.sources
        assert res.target_item_type == "conferencePaper"
        # the venue string actually written (what easyScholar reads) must be correctly cased
        assert res.fields["proceedingsTitle"] == "International Conference on Machine Learning"
        assert res.fields["conferenceName"] == res.fields["proceedingsTitle"]

    def test_labels_collections_from_map(self, make_item):
        item = make_item(key="K", title="X", archiveID="arXiv:2106.00001",
                         collections=["AAA", "BBB"])
        s2 = _FakeS2({"2106.00001": VenueHit(
            source="semantic_scholar", venue_raw="ICLR", year=2021,
            venue_type="conference", citation_count=1)})
        [res] = resolve_items([item], s2, _FakeDBLP(),
                              collections_map={"AAA": "Foo", "BBB": "Bar / Baz"})
        assert res.collections == ["Foo", "Bar / Baz"]

    def test_collections_fall_back_to_keys_without_map(self, make_item):
        item = make_item(key="K", title="X", archiveID="arXiv:2106.00002",
                         collections=["ZZZ"])
        s2 = _FakeS2({"2106.00002": VenueHit(source="semantic_scholar", venue_raw=None)})
        [res] = resolve_items([item], s2, _FakeDBLP())
        assert res.collections == ["ZZZ"]
