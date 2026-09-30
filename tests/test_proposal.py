import pytest

from arxiv_marker import proposal


class TestIsArxivDoi:
    def test_true(self):
        assert proposal.is_arxiv_doi("10.48550/arXiv.2106.09685")
        assert proposal.is_arxiv_doi("arXiv:1234.5678")

    def test_false(self):
        assert not proposal.is_arxiv_doi("10.1109/CVPR.2021.00123")
        assert not proposal.is_arxiv_doi(None)
        assert not proposal.is_arxiv_doi("")


class TestBuildConference:
    def test_basic_fields(self, make_resolution, make_hit):
        res = make_resolution(kind="conference", canonical="ICLR",
                              venue_raw="International Conference on Learning Representations",
                              citation_count=19435)
        hit = make_hit(external_doi="10.48550/arXiv.2106.09685", issn=None, abbrev=None)
        itype, fields = proposal.build(res, hit, "2106.09685", {"extra": "arXiv:2106.09685 [cs]"})
        assert itype == "conferencePaper"
        assert fields["proceedingsTitle"] == "International Conference on Learning Representations"
        assert fields["conferenceName"] == fields["proceedingsTitle"]
        assert "DOI" not in fields                                  # arXiv DOI is filtered out
        assert "Citations: 19435 (SemanticScholar)" in fields["extra"]
        assert "arXiv:2106.09685" in fields["extra"]
        assert "arxiv-marker: resolved" in fields["extra"]

    def test_publisher_filled_from_map(self, make_resolution, make_hit):
        res = make_resolution(kind="conference", canonical="CVPR",
                              venue_raw="Computer Vision and Pattern Recognition")
        _, fields = proposal.build(res, make_hit(), "2101.00001", {})
        assert fields["publisher"] == "IEEE"

    def test_real_doi_is_kept(self, make_resolution, make_hit):
        res = make_resolution(kind="conference", canonical="CVPR",
                              venue_raw="Computer Vision and Pattern Recognition")
        hit = make_hit(external_doi="10.1109/CVPR52688.2022.01042")
        _, fields = proposal.build(res, hit, "2101.00001", {})
        assert fields["DOI"] == "10.1109/CVPR52688.2022.01042"


class TestBuildJournal:
    def test_basic_fields(self, make_resolution, make_hit):
        res = make_resolution(
            kind="journal", canonical="TPAMI",
            venue_raw="IEEE Transactions on Pattern Analysis and Machine Intelligence",
            citation_count=2485)
        hit = make_hit(external_doi="10.1109/TPAMI.2021.123", issn="0162-8828", abbrev="TPAMI")
        itype, fields = proposal.build(res, hit, "2104.00001", {})
        assert itype == "journalArticle"
        assert fields["publicationTitle"].startswith("IEEE Transactions on Pattern Analysis")
        assert fields["journalAbbreviation"] == "TPAMI"
        assert fields["ISSN"] == "0162-8828"
        assert fields["DOI"] == "10.1109/TPAMI.2021.123"
        assert "proceedingsTitle" not in fields


class TestExtraIdempotency:
    def test_rewrites_not_duplicates(self, make_resolution, make_hit):
        prior = ("arXiv:2106.09685 [cs]\n"
                 "Citations: 50 (SemanticScholar) [2026-01-01]\n"
                 "zotero-marker: resolved 2026-01-01\n"   # legacy stamp from before the rename
                 "User note: keep me")
        res = make_resolution(kind="conference", canonical="ICLR",
                              venue_raw="International Conference on Learning Representations",
                              citation_count=100)
        _, fields = proposal.build(res, make_hit(), "2106.09685", {"extra": prior})
        extra = fields["extra"]
        assert extra.count("Citations:") == 1
        assert "Citations: 100 (SemanticScholar)" in extra
        assert "Citations: 50" not in extra
        assert extra.count("zotero-marker:") == 0       # legacy stamp migrated away on re-run
        assert extra.count("arxiv-marker:") == 1         # exactly one current stamp
        assert "User note: keep me" in extra            # untouched user content preserved
        assert extra.count("arXiv:2106.09685") == 1     # arXiv id not duplicated

    def test_removes_legacy_citation_format(self, make_resolution, make_hit):
        prior = "19435 citations (Semantic Scholar) [2026-05-30]\narXiv:2106.09685"
        res = make_resolution(kind="conference", canonical="ICLR",
                              venue_raw="International Conference on Learning Representations",
                              citation_count=20000)
        _, fields = proposal.build(res, make_hit(), "2106.09685", {"extra": prior})
        assert "19435 citations" not in fields["extra"]
        assert "Citations: 20000 (SemanticScholar)" in fields["extra"]

    def test_preserves_foreign_citation_line(self, make_resolution, make_hit):
        # A user's own citation line from another source (Citation Tally's default is
        # Crossref) must NOT be deleted — only our own SemanticScholar line is rewritten.
        prior = ("arXiv:2106.09685 [cs]\n"
                 "Citations: 99 (Crossref) [2025-01-01]\n"
                 "arxiv-marker: resolved 2025-01-01")
        res = make_resolution(kind="conference", canonical="ICLR",
                              venue_raw="International Conference on Learning Representations",
                              citation_count=100)
        _, fields = proposal.build(res, make_hit(), "2106.09685", {"extra": prior})
        extra = fields["extra"]
        assert "Citations: 99 (Crossref) [2025-01-01]" in extra      # preserved
        assert "Citations: 100 (SemanticScholar)" in extra           # ours added
        assert extra.count("arxiv-marker:") == 1                    # our stamp not duplicated


class TestFullName:
    def test_preserves_stopword_casing(self):
        # The configured full name preserves interior stopword casing.
        assert proposal._full_name("NeurIPS", None) == \
            "Advances in Neural Information Processing Systems"

    def test_multiword_raw_written_verbatim(self):
        assert proposal._full_name("ICLR", "International Conference on Learning Representations") \
            == "International Conference on Learning Representations"

    def test_write_as_pins_easyscholar_matching_name(self):
        # Regression: S2 returns ICCV as "IEEE International Conference on Computer Vision",
        # but easyScholar's ICCV (CCF A) entry has no "IEEE" prefix — so S2's string left
        # the CCF tag blank. `write_as` pins the prefix-less string easyScholar matches.
        assert proposal._full_name("ICCV", "IEEE International Conference on Computer Vision") \
            == "International Conference on Computer Vision"

    @pytest.mark.parametrize("raw", [
        "Some Unknown Conference 2026", "ICML 2026 Workshop",
        "Findings of the Association for Computational Linguistics: EMNLP 2023",
    ])
    def test_unmapped_venues_are_preserved(self, raw):
        assert proposal._full_name(raw, raw) == raw


class TestBuildSkips:
    def test_unknown_returns_empty(self, make_resolution):
        res = make_resolution(acceptance="unknown", canonical=None)
        assert proposal.build(res, None, None, {}) == (None, {})

    def test_accepted_but_no_canonical_returns_empty(self, make_resolution):
        res = make_resolution(acceptance="accepted", canonical=None)
        assert proposal.build(res, None, None, {}) == (None, {})


class TestBuildIdempotent:
    """Once an item IS the target type with its venue field written, re-resolving must
    propose nothing — otherwise a resolved paper is re-listed forever (the stale-cache bug)."""

    def test_already_converted_returns_empty(self, make_resolution, make_hit):
        res = make_resolution(kind="conference", canonical="ICLR",
                              venue_raw="International Conference on Learning Representations")
        data = {"itemType": "conferencePaper",
                "proceedingsTitle": "International Conference on Learning Representations",
                "extra": "arXiv:2106.09685"}
        assert proposal.build(res, make_hit(), "2106.09685", data) == (None, {})

    def test_still_preprint_proposes_change(self, make_resolution, make_hit):
        res = make_resolution(kind="conference", canonical="ICLR",
                              venue_raw="International Conference on Learning Representations")
        itype, fields = proposal.build(res, make_hit(), "2106.09685", {"itemType": "preprint"})
        assert itype == "conferencePaper" and fields["proceedingsTitle"]

    def test_right_type_but_venue_missing_still_proposes(self, make_resolution, make_hit):
        # user flipped the type by hand but never filled the venue field -> still write it
        res = make_resolution(kind="conference", canonical="ICLR",
                              venue_raw="International Conference on Learning Representations")
        data = {"itemType": "conferencePaper", "proceedingsTitle": ""}
        itype, fields = proposal.build(res, make_hit(), "2106.09685", data)
        assert itype == "conferencePaper" and fields["proceedingsTitle"]
