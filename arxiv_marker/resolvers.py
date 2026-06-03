"""Deterministic venue resolvers: Semantic Scholar (by arXiv id) + DBLP (by title).

LLM + web-search is intentionally a separate, later fallback (see resolve_llm stub)
so the cheap, zero-hallucination structured path runs first and handles the majority.
"""
from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass

import requests

from . import config, rankings, util


@dataclass
class VenueHit:
    source: str                       # "semantic_scholar" | "dblp" | "llm"
    venue_raw: str | None
    year: int | None = None
    venue_type: str | None = None     # conference | journal | ...
    citation_count: int | None = None
    influential_citations: int | None = None
    external_doi: str | None = None
    dblp_key: str | None = None
    evidence_url: str | None = None
    issn: str | None = None
    abbrev: str | None = None


def _is_nonvenue(v: str | None) -> bool:
    """True if the 'venue' is really just a preprint server (arXiv / CoRR) = not published."""
    if not v:
        return True
    s = v.strip().lower()
    return "arxiv" in s or s in {"corr", "preprint", ""}


def _s2_venue_type(pv: dict, pub_types: list | None) -> str | None:
    """Journal vs conference for an S2 record.

    S2 frequently omits publicationVenue.type even when it clearly knows the paper is a
    JournalArticle and gives the venue an ISSN (verified for TNNLS / Science Robotics). The
    resolver used to read only `pv.type`, so those typeless venues came back as None and the
    proposal defaulted them to a conferencePaper — converting real journal articles to
    conferencePaper and writing the journal name into proceedingsTitle/conferenceName. Fall
    back to the per-paper publicationTypes, then to the presence of an ISSN (journals have one).
    """
    t = pv.get("type")
    if t:
        return t
    types = pub_types or []
    if "JournalArticle" in types:
        return "journal"
    if "Conference" in types:
        return "conference"
    if pv.get("issn"):
        return "journal"
    return None


def _authors_of(info: dict) -> list[str]:
    a = (info.get("authors") or {}).get("author")
    if isinstance(a, dict):
        a = [a]
    return [x.get("text", "") for x in (a or []) if isinstance(x, dict)]


_S2_FIELDS = ("title,venue,publicationVenue,year,externalIds,"
              "publicationTypes,citationCount,influentialCitationCount")
_S2_BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch"


class SemanticScholar:
    def __init__(self, api_key=None, timeout=30):
        self.api_key = api_key if api_key is not None else config.SEMANTIC_SCHOLAR_KEY
        self.timeout = timeout
        self.s = requests.Session()
        if self.api_key:
            self.s.headers["x-api-key"] = self.api_key

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def batch_by_arxiv(self, arxiv_ids: list[str]) -> dict[str, VenueHit]:
        """Resolve arXiv ids -> {arxiv_id: VenueHit}. Chunks of 100, retry on 429."""
        out: dict[str, VenueHit] = {}
        for i in range(0, len(arxiv_ids), 100):
            chunk = arxiv_ids[i:i + 100]
            recs = self._post_with_retry({"ids": [f"ARXIV:{a}" for a in chunk]})
            if not recs:
                continue
            for aid, rec in zip(chunk, recs, strict=False):
                if not rec:
                    continue
                pv = rec.get("publicationVenue") or {}
                ext = rec.get("externalIds") or {}
                name = pv.get("name") or rec.get("venue")
                alts = pv.get("alternate_names") or []
                abbrev = next((a for a in alts if a.isupper() and 2 <= len(a) <= 8), None)
                out[aid] = VenueHit(
                    source="semantic_scholar",
                    venue_raw=(None if _is_nonvenue(name) else name),
                    year=rec.get("year"),
                    venue_type=_s2_venue_type(pv, rec.get("publicationTypes")),
                    citation_count=rec.get("citationCount"),
                    influential_citations=rec.get("influentialCitationCount"),
                    external_doi=ext.get("DOI"),
                    dblp_key=ext.get("DBLP"),
                    evidence_url=f"https://www.semanticscholar.org/arxiv/{aid}",
                    issn=pv.get("issn"),
                    abbrev=abbrev,
                )
        return out

    def _post_with_retry(self, payload, tries=6):
        delay = 3.0
        for _ in range(tries):
            try:
                r = self.s.post(_S2_BATCH, params={"fields": _S2_FIELDS},
                                json=payload, timeout=self.timeout)
                if r.status_code == 429:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                r.raise_for_status()
                return r.json()
            except requests.RequestException:
                time.sleep(delay)
                delay = min(delay * 2, 30)
        return None


class DBLP:
    def __init__(self, timeout=20):
        self.timeout = timeout
        self.s = requests.Session()

    def best_by_title(self, title: str, author_lastname: str = "",
                      year: int | None = None) -> VenueHit | None:
        """Title search disambiguated by author + year. Prefers the ORIGINAL peer-reviewed
        conference over a later journal republication (e.g. GANs: NeurIPS'14 over CACM'20)."""
        hits = self._search(f"{title} {author_lastname}".strip()) or self._search(title) or []
        cands = []
        for h in hits:
            info = h.get("info", {})
            t = info.get("title", "")
            jac = util.title_jaccard(title, t)
            authors = _authors_of(info)
            y = int(info["year"]) if str(info.get("year", "")).isdigit() else None
            author_ok = bool(author_lastname) and any(
                author_lastname.lower() in a.lower() for a in authors)
            year_ok = year is not None and y == year
            # strong title match, OR a looser match backed by author+year agreement
            if not (jac >= 0.85 or (jac >= 0.5 and author_ok and year_ok)):
                continue
            venue = info.get("venue")
            if isinstance(venue, list):
                venue = venue[0] if venue else None
            if _is_nonvenue(venue):       # CoRR / arXiv = not a real venue, skip
                continue
            typ = info.get("type", "") or ""
            kind = "conference" if "Conference" in typ else "journal"
            cands.append((venue, y, kind, info))
        if not cands:
            return None

        def score(c):
            venue, y, kind, _ = c
            s = 4
            if kind == "conference":
                s += 3
            if year is not None and y == year:
                s += 2
            if rankings.lookup(venue):
                s += 1
            return s

        venue, y, kind, info = max(cands, key=score)
        return VenueHit(source="dblp", venue_raw=venue, year=y, venue_type=kind,
                        external_doi=info.get("doi"), evidence_url=info.get("url"))

    def _search(self, query: str):
        url = ("https://dblp.org/search/publ/api?q="
               + urllib.parse.quote(query) + "&format=json&h=10")
        try:
            r = self.s.get(url, timeout=self.timeout)
            r.raise_for_status()
            return (r.json().get("result", {}).get("hits", {}) or {}).get("hit", []) or []
        except (requests.RequestException, ValueError):
            return None


def resolve_llm(title: str, **kwargs) -> VenueHit | None:
    """Placeholder for the LLM + web-search fallback (Tavily/Exa + Claude/GPT).

    Intentionally unimplemented in v1: the deterministic cascade above handles the
    majority for free with zero hallucination. Wire this in only for the residual
    that S2 and DBLP both miss, with FORCED abstention and snippet-constrained
    evidence URLs. Returns None for now (-> item stays 'unknown').
    """
    return None
