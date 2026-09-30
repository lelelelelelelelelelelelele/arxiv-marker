"""Run the PRODUCTION Python resolver on test/cases.json and print normalized JSON.

Bypasses pytest's _isolate_overrides fixture, so the real data/overrides.csv applies —
the same as the JS production path. Used by parity.mjs to diff the two implementations.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent  # plugin/test -> plugin -> repo root
sys.path.insert(0, str(REPO_ROOT))

from arxiv_marker.pipeline import resolve_items  # noqa: E402
from arxiv_marker.resolvers import DBLP, OpenReview, SemanticScholar  # noqa: E402

FIELD_KEYS = ["proceedingsTitle", "conferenceName", "publicationTitle",
              "journalAbbreviation", "ISSN", "DOI", "publisher"]


def normalize(res) -> dict:
    return {
        "key": res.item_key,
        "arxiv_id": res.arxiv_id,
        "canonical": res.canonical,
        "kind": res.kind,
        "core_tier": res.core_tier,
        "year": res.year,
        "acceptance": res.acceptance,
        "confidence": res.confidence,
        "citation_count": res.citation_count,
        "target_item_type": res.target_item_type,
        "sources": list(res.sources),
        "fields": {k: res.fields.get(k) for k in FIELD_KEYS if k in res.fields},
    }


def main():
    cases = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
    items = [{"key": c["key"], "version": 1, "data": {
        "title": c["title"], "archiveID": c.get("archiveID", ""),
        "date": c.get("date", ""), "creators": c.get("creators", []),
        "itemType": "preprint", "extra": "", "tags": [],
    }} for c in cases]
    results = resolve_items(items, SemanticScholar(), DBLP(), openreview=OpenReview())
    print(json.dumps([normalize(r) for r in results], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
