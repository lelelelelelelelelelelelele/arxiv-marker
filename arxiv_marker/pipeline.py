"""Orchestration: Zotero candidates -> resolved venue/tier/citations -> Resolution records."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import overrides, proposal, rankings, util
from .resolvers import DBLP, OpenReview, SemanticScholar, VenueHit


@dataclass
class Resolution:
    item_key: str
    version: int
    title: str
    arxiv_id: str | None
    venue_raw: str | None
    canonical: str | None
    kind: str | None
    year: int | None
    core_tier: str | None
    acceptance: str               # accepted | unknown
    confidence: float
    citation_count: int | None
    influential_citations: int | None
    sources: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    suggested_tags: list[str] = field(default_factory=list)
    existing_tags: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)   # Zotero collection display paths
    # write-back proposal (field-writing strategy)
    current_item_type: str = "preprint"
    target_item_type: str | None = None
    fields: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


_CITE_BUCKETS = [10000, 5000, 1000, 500, 100, 50, 10]


def _cite_bucket(n: int | None) -> str | None:
    if n is None:
        return None
    for b in _CITE_BUCKETS:
        if n >= b:
            return f"{b}+"
    return "<10"


def _item_year(data: dict) -> int | None:
    d = (data.get("date") or "").strip()
    for i in range(len(d) - 3):
        chunk = d[i:i + 4]
        if chunk.isdigit() and chunk.startswith(("19", "20")):
            return int(chunk)
    return None


def _item_authors(data: dict) -> list[str]:
    authors = []
    for creator in data.get("creators", []) or []:
        if creator.get("creatorType") != "author":
            continue
        if creator.get("name"):
            authors.append(creator["name"])
            continue
        name = " ".join(x for x in (creator.get("firstName"), creator.get("lastName")) if x)
        if name:
            authors.append(name)
    return authors


def _choose_venue(hits: list[VenueHit]):
    """Pick the best venue hit. Prefer a hit that maps to a ranking-table CONFERENCE
    (handles e.g. GANs: S2='Communications of the ACM' journal vs DBLP='NeurIPS' conf)."""
    scored = []
    for h in hits:
        if not h or not h.venue_raw:
            continue
        row = rankings.lookup(h.venue_raw)
        score = 0
        if row:
            score += 3
            if row["kind"] == "conference":
                score += 2
            if row.get("core") == "A*":
                score += 1
        scored.append((score, h, row))
    if not scored:
        return None, None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1], scored[0][2]


def _confidence(hits: list[VenueHit], chosen: VenueHit | None, row: dict | None) -> float:
    if not chosen:
        return 0.0
    if not row:
        return 0.6           # a venue string, but not in the ranking table
    # how many INDEPENDENT sources map to the chosen CANONICAL venue (i.e. agree)?
    agree = sum(1 for h in hits
                if h and h.venue_raw
                and (r := rankings.lookup(h.venue_raw)) and r["canonical"] == row["canonical"])
    return 0.95 if agree >= 2 else 0.85   # >=2 agreeing sources vs single recognized source


def build_tags(res: Resolution) -> list[str]:
    tags: list[str] = []
    if res.canonical:
        tags.append(f"venue:{res.canonical}")
    if res.year:
        tags.append(f"year:{res.year}")
    if res.core_tier:
        tags.append(f"CORE:{res.core_tier}")
    tags.append(f"acceptance:{res.acceptance}")
    bucket = _cite_bucket(res.citation_count)
    if bucket:
        tags.append(f"cite:{bucket}")
    return tags


def duplicate_arxiv_groups(results: list[Resolution]) -> dict[str, list[str]]:
    """arXiv ids that appear on more than one item -> the item keys sharing them.

    Surfaces duplicate library entries (the same preprint added twice) so the user
    can merge them in Zotero instead of writing the same venue to both.
    """
    groups: dict[str, list[str]] = {}
    for r in results:
        if r.arxiv_id:
            groups.setdefault(r.arxiv_id, []).append(r.item_key)
    return {aid: keys for aid, keys in groups.items() if len(keys) > 1}


def resolve_items(items: list[dict], s2: SemanticScholar, dblp: DBLP,
                  progress=None, collections_map: dict | None = None,
                  openreview: OpenReview | None = None) -> list[Resolution]:
    """Resolve raw Zotero items via S2 -> OpenReview -> DBLP.

    collections_map (key -> display path, from ZoteroClient.get_collections) labels each
    item with its Zotero collection(s) so the review UIs can filter by collection.
    """
    cmap = collections_map or {}
    # 1) extract arxiv ids and batch-resolve via Semantic Scholar
    arxiv_of = {it["key"]: util.extract_arxiv_id(it["data"]) for it in items}
    ids = sorted({a for a in arxiv_of.values() if a})
    s2_map = s2.batch_by_arxiv(ids) if ids else {}

    results: list[Resolution] = []
    # Resolution is intentionally sequential (at most one OpenReview request in flight),
    # and this run-local cache prevents duplicate lookups for duplicate Zotero items.
    openreview_cache: dict[tuple, VenueHit | None] = {}
    for it in items:
        data = it["data"]
        key = it["key"]
        title = data.get("title", "") or ""
        aid = arxiv_of[key]
        s2_hit = s2_map.get(aid) if aid else None

        hits: list[VenueHit] = [h for h in [s2_hit] if h]
        # 2) Structured fallbacks when S2 gave no venue, OR gave a venue that isn't a
        #    recognized conference (catches journal republications, e.g. GANs->CACM
        #    where the original is NeurIPS'14). Deterministic-first, still cheap.
        need_fallback = (not s2_hit) or (not s2_hit.venue_raw)
        if not need_fallback:
            row = rankings.lookup(s2_hit.venue_raw)
            if not (row and row["kind"] == "conference"):
                need_fallback = True
        year_hint = (s2_hit.year if s2_hit else None) or _item_year(data)
        openreview_hit = None
        if need_fallback and openreview:
            authors = _item_authors(data)
            cache_key = (("arxiv", aid) if aid else
                         ("metadata", util.norm_title(title), tuple(authors), year_hint))
            if cache_key not in openreview_cache:
                openreview_cache[cache_key] = openreview.best_by_title(
                    title, authors, year_hint, aid)
            openreview_hit = openreview_cache[cache_key]
            if openreview_hit:
                hits.append(openreview_hit)
        if need_fallback and not openreview_hit:
            dblp_hit = dblp.best_by_title(
                title, util.first_author_lastname(data), year_hint)
            if dblp_hit:
                hits.append(dblp_hit)

        chosen, row = _choose_venue(hits)
        # citations always come from S2 regardless of which venue we display
        cites = s2_hit.citation_count if s2_hit else None
        infl = s2_hit.influential_citations if s2_hit else None

        res = Resolution(
            item_key=key,
            version=it.get("version") or data.get("version", 0),
            title=title,
            arxiv_id=aid,
            venue_raw=chosen.venue_raw if chosen else None,
            canonical=row["canonical"] if row else (chosen.venue_raw if chosen else None),
            kind=row["kind"] if row else (chosen.venue_type if chosen else None),
            year=(chosen.year if chosen and chosen.year else (s2_hit.year if s2_hit else None)),
            core_tier=row["core"] if row and row.get("core") else None,
            acceptance=("accepted" if chosen else "unknown"),
            confidence=_confidence(hits, chosen, row),
            citation_count=cites,
            influential_citations=infl,
            sources=[h.source for h in hits],
            evidence=[f"{h.source}: {h.venue_raw or '-'} ({h.evidence_url or ''})"
                      for h in hits],
            existing_tags=[t.get("tag", "") for t in data.get("tags", []) or []],
            collections=[cmap.get(k, k) for k in data.get("collections", []) or []],
            current_item_type=data.get("itemType", "preprint"),
        )
        res.suggested_tags = build_tags(res)

        # manual override wins (republication quirks, etc.) — transparently labelled
        ov = overrides.get(aid)
        if ov and ov.get("canonical"):
            row = rankings.lookup(ov["canonical"])
            res.canonical = row["canonical"] if row else ov["canonical"]
            res.venue_raw = ov["canonical"]
            res.kind = row["kind"] if row else res.kind
            res.year = ov.get("year") or res.year
            res.core_tier = row["core"] if row and row.get("core") else None
            res.acceptance = "accepted"
            res.confidence = 1.0
            res.sources = ["override", *res.sources]
            res.evidence = [f"override: {ov['canonical']}", *res.evidence]
            res.suggested_tags = build_tags(res)

        # field-writing proposal: itemType change + venue metadata fields
        proposal_hit = (chosen if chosen and chosen.source == "openreview"
                        and chosen.external_doi else s2_hit)
        res.target_item_type, res.fields = proposal.build(res, proposal_hit, aid, data)

        results.append(res)
        if progress:
            progress(res)
    return results
