from arxiv_marker import rankings


def test_exact_alias():
    row = rankings.lookup("NeurIPS")
    assert row and row["canonical"] == "NeurIPS"
    assert row["core"] == "A*"
    assert row["kind"] == "conference"


def test_full_name_alias():
    assert rankings.lookup(
        "Advances in Neural Information Processing Systems")["canonical"] == "NeurIPS"


def test_case_insensitive():
    assert rankings.lookup("iclr")["canonical"] == "ICLR"


def test_substring_match():
    row = rankings.lookup(
        "Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern Recognition")
    assert row and row["canonical"] == "CVPR"


def test_journal_has_no_core_tier():
    row = rankings.lookup("Journal of Machine Learning Research")
    assert row["canonical"] == "JMLR"
    assert row["kind"] == "journal"
    assert row["core"] == ""


def test_unknown_returns_none():
    assert rankings.lookup("Some Made-Up Venue 2099") is None


def test_none_and_empty():
    assert rankings.lookup(None) is None
    assert rankings.lookup("") is None
    assert rankings.lookup("   ") is None


# --- regression guards: don't collapse lower-tier / compound venues onto the flagship ---

def test_rejects_workshop_track():
    assert rankings.lookup("NeurIPS Workshop on Deep Learning") is None
    assert rankings.lookup("ICML 2021 Workshop") is None


def test_rejects_findings_track():
    assert rankings.lookup(
        "Findings of the Association for Computational Linguistics: EMNLP 2023") is None


def test_rejects_compound_conference():
    # ICMLA is a different venue from ICML
    assert rankings.lookup("International Conference on Machine Learning and Applications") is None


def test_rejects_compound_journal():
    assert rankings.lookup("Nature Communications") is None
    assert rankings.lookup("Science Robotics") is None


def test_accepts_acronym_followed_by_year():
    assert rankings.lookup("NeurIPS 2021")["canonical"] == "NeurIPS"


def test_accepts_generic_venue_suffix():
    # "Symposium" is part of the official name, not a distinct venue
    assert rankings.lookup("USENIX Security Symposium")["canonical"] == "USENIX Security"


def test_naacl_full_name_does_not_collapse_to_acl():
    name = "Conference of the North American Chapter of the Association for Computational Linguistics: Human Language Technologies"
    for venue in (name, f"{name} 2024"):
        assert rankings.lookup(venue)["canonical"] == "NAACL"
        assert rankings.lookup(venue)["core"] == "A"


def test_write_as_column():
    # Optional column: the exact string to write when S2's name != easyScholar's match value.
    assert rankings.lookup("ICCV")["write_as"] == "International Conference on Computer Vision"
    assert rankings.lookup("ICLR")["write_as"] == "International Conference on Learning Representations"
    assert rankings.lookup("JMLR")["write_as"] == ""        # optional for journals


def test_conference_write_names_are_complete_and_unambiguous():
    for row in rankings._table():
        if row["kind"] != "conference":
            continue
        name = row["write_as"]
        assert len(name.split()) >= 2, row["canonical"]
        assert not any(c.isdigit() for c in name), name
        for variant in (name, f"{name} 2026", f"Proceedings of the {name}"):
            assert rankings.lookup(variant)["canonical"] == row["canonical"], variant
