"""Deterministic venue resolvers: Semantic Scholar, OpenReview, and DBLP.

LLM + web-search is intentionally a separate, later fallback (see resolve_llm stub)
so the cheap, zero-hallucination structured path runs first and handles the majority.
"""
from __future__ import annotations

import email.utils
import random
import re
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass

import requests

from . import config, rankings, util


@dataclass
class VenueHit:
    source: str                       # semantic_scholar | openreview | dblp | llm
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


_OPENREVIEW_SEARCH = "https://api2.openreview.net/notes/search"
_OPENREVIEW_MAX_RETRIES = 2
_OPENREVIEW_BACKOFF_FACTOR = 1.0
_OPENREVIEW_MAX_DELAY = 30.0
_OPENREVIEW_REJECT_STATE = re.compile(
    r"\b(?:submitted|submission|reject(?:ed|ion)?|withdrawn)\b"
    r"|\bunder[\s_-]+review\b|\bdesk[\s_-]+rejected\b",
    re.I,
)
_OPENREVIEW_PRESENTATION = re.compile(
    r"(?:\s*[-–—,:]\s*|\s+)(?:poster|spotlight|oral|regular)(?:\s+presentation)?\s*$",
    re.I,
)
_OPENREVIEW_PUNCTUATION = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u00a0": " ",
})


def _openreview_value(content: dict, key: str):
    """Return an API-v2 content value while tolerating legacy scalar fixtures."""
    value = content.get(key)
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def _openreview_rendered_title(value: str | None) -> str:
    """Remove TeX display wrappers while preserving the rendered title's case."""
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", value).translate(_OPENREVIEW_PUNCTUATION)
    value = value.replace(r"\(", "").replace(r"\)", "")
    value = value.replace(r"\[", "").replace(r"\]", "")
    value = value.replace("$", "")
    return re.sub(r"\s+", " ", value).strip()


def _openreview_norm_title(value: str | None) -> str:
    """Normalize display differences without erasing meaningful mathematical notation.

    arXiv commonly retains TeX math delimiters (``$f(x)$``), while OpenReview stores the
    rendered text (``f(x)``). The delimiters are formatting, so remove them, but preserve
    parentheses, operators, and the expression itself.
    """
    return _openreview_rendered_title(value).lower()


def _openreview_title_quality(query: str, candidate: str) -> int:
    q = _openreview_norm_title(query)
    c = _openreview_norm_title(candidate)
    if not q or not c:
        return 0
    if q == c:
        return 2
    # Some Zotero/arXiv records retain only the lead title while OpenReview appends a
    # subtitle. This is deterministic (a complete prefix at a colon boundary), not fuzzy.
    if c.startswith(q + ":") or q.startswith(c + ":"):
        return 1
    return 0


def _openreview_surname(name: str) -> str:
    # OpenReview/Zotero may represent the same person as "Given Family" or
    # "Family, Given". In the comma form, the surname is the leading segment.
    value = (name or "").split(",", 1)[0] if "," in (name or "") else (name or "")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c)).lower()
    parts = re.findall(r"[^\W\d_]+", value, re.UNICODE)
    while parts and parts[-1] in {"jr", "sr", "ii", "iii", "iv"}:
        parts.pop()
    return parts[-1] if parts else ""


def _openreview_author_match(expected: list[str], candidate: list[str]) -> tuple[bool, int, bool]:
    """Return (acceptable, surname overlap count, first-author surname match)."""
    exp = [x for name in expected if (x := _openreview_surname(name))]
    got = [x for name in candidate if (x := _openreview_surname(name))]
    if not exp or not got:
        return True, 0, False
    overlap = len(set(exp) & set(got))
    first = exp[0] == got[0]
    return first or overlap >= 2, overlap, first


def _openreview_year(content: dict, venue: str, venue_id: str) -> int | None:
    value = _openreview_value(content, "year")
    if str(value or "").isdigit():
        return int(value)
    match = re.search(r"\b(?:19|20)\d{2}\b", f"{venue} {venue_id}")
    return int(match.group()) if match else None


def _openreview_doi(content: dict) -> str | None:
    for key in ("doi", "DOI"):
        value = _openreview_value(content, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


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


class OpenReview:
    """Resolve a title against public, structured OpenReview forum notes."""

    def __init__(self, timeout=20, max_retries=_OPENREVIEW_MAX_RETRIES,
                 backoff_factor=_OPENREVIEW_BACKOFF_FACTOR,
                 backoff_max=_OPENREVIEW_MAX_DELAY, sleep=None, random_fn=None):
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.backoff_factor = max(0.0, float(backoff_factor))
        self.backoff_max = max(0.0, float(backoff_max))
        self.sleep = sleep or time.sleep
        self.random = random_fn or random.random
        self.s = requests.Session()

    def _retry_after(self, response) -> float | None:
        headers = getattr(response, "headers", None)
        raw = headers.get("Retry-After") if headers else None
        if raw is None:
            return None
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            try:
                retry_at = email.utils.parsedate_to_datetime(str(raw))
                seconds = retry_at.timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                return None
        # Retry-After is the server's explicit instruction; the local backoff cap only
        # applies when the header is absent (matching urllib3/OpenReview client behavior).
        return max(0.0, seconds)

    def _retry_delay(self, response, retry_number: int) -> float:
        retry_after = self._retry_after(response)
        if retry_after is not None:
            return retry_after
        delay = self.backoff_factor * (2 ** retry_number)
        delay += self.random() * self.backoff_factor
        return min(delay, self.backoff_max)

    def _search(self, term: str) -> list | None:
        for retry_number in range(self.max_retries + 1):
            try:
                response = self.s.get(_OPENREVIEW_SEARCH, params={
                    "term": term,
                    "content": "title",
                    "type": "exact",
                    "source": "forum",
                    "limit": 25,
                }, timeout=self.timeout)
                if response.status_code == 429:
                    if retry_number >= self.max_retries:
                        return None
                    self.sleep(self._retry_delay(response, retry_number))
                    continue
                response.raise_for_status()
                payload = response.json()
                break
            except (requests.RequestException, ValueError):
                return None
        else:  # pragma: no cover - loop always returns or breaks
            return None

        if not isinstance(payload, dict) or not isinstance(payload.get("notes"), list):
            return None
        return payload["notes"]

    def best_by_title(self, title: str, authors: list[str] | None = None,
                      year: int | None = None, arxiv_id: str | None = None) -> VenueHit | None:
        rendered = _openreview_rendered_title(title)
        notes = self._search(rendered or title)
        if not notes:
            return None

        candidates = []
        for note in notes:
            try:
                candidate = self._candidate(note, title, authors or [], year, arxiv_id)
            except (AttributeError, TypeError, ValueError):
                candidate = None
            if candidate:
                candidates.append(candidate)
        if not candidates:
            return None

        best_score = max(score for score, _ in candidates)
        best = [hit for score, hit in candidates if score == best_score]
        identities = {(h.venue_raw.lower(), h.year) for h in best if h.venue_raw}
        if len(identities) > 1:
            return None
        return sorted(best, key=lambda h: h.evidence_url or "")[0]

    @staticmethod
    def _candidate(note, title: str, authors: list[str], year: int | None,
                   arxiv_id: str | None) -> tuple[int, VenueHit] | None:
        if not isinstance(note, dict) or not isinstance(note.get("content"), dict):
            return None
        content = note["content"]
        candidate_title = _openreview_value(content, "title")
        if not isinstance(candidate_title, str):
            return None
        title_quality = _openreview_title_quality(title, candidate_title)
        if not title_quality:
            return None

        raw_authors = _openreview_value(content, "authors")
        candidate_authors = [x for x in raw_authors if isinstance(x, str)] \
            if isinstance(raw_authors, list) else []
        author_ok, overlap, first_author = _openreview_author_match(authors, candidate_authors)
        if not author_ok or (title_quality == 1 and (not authors or not candidate_authors)):
            return None

        if arxiv_id:
            for key in ("arxiv", "arxiv_id", "arxivId"):
                value = _openreview_value(content, key)
                if isinstance(value, str):
                    match = re.search(r"\d{4}\.\d{4,5}", value)
                    if match and match.group() != arxiv_id:
                        return None

        venue = _openreview_value(content, "venue")
        venue_id = _openreview_value(content, "venueid") or _openreview_value(content, "venue_id")
        if not isinstance(venue, str) or not isinstance(venue_id, str):
            return None
        venue = venue.strip()
        venue_id = venue_id.strip()
        decision = _openreview_value(content, "decision")
        status = _openreview_value(content, "status")
        state = " ".join(str(x or "") for x in (venue, venue_id, decision, status))
        if _OPENREVIEW_REJECT_STATE.search(state) or _is_nonvenue(venue):
            return None
        if re.fullmatch(r"(?:poster|spotlight|oral|regular)(?:\s+presentation)?", venue, re.I):
            return None

        provenance = " ".join(str(note.get(k) or "") for k in ("domain", "invitations", "signatures"))
        if re.search(r"(?:^|[./_\s-])dblp(?:[./_\s-]|$)", provenance, re.I):
            return None

        venue_raw = _OPENREVIEW_PRESENTATION.sub("", venue).strip()
        if not venue_raw or _is_nonvenue(venue_raw):
            return None
        row = rankings.lookup(venue_raw)
        venue_scope = f"{venue_id} {note.get('domain') or ''}".lower()
        is_workshop = "/workshop" in venue_scope or "workshop" in venue_raw.lower()
        is_main_conference = "/conference" in venue_scope and not is_workshop
        if "/conference" in venue_scope or "/workshop" in venue_scope:
            venue_type = "conference"
        elif row:
            venue_type = row["kind"]
        else:
            return None

        candidate_year = _openreview_year(content, venue_raw, venue_id)
        forum = note.get("forum") or note.get("id")
        if not isinstance(forum, str) or not forum:
            return None
        hit = VenueHit(
            source="openreview",
            venue_raw=venue_raw,
            year=candidate_year,
            venue_type=venue_type,
            external_doi=_openreview_doi(content),
            evidence_url="https://openreview.net/forum?id=" + urllib.parse.quote(forum),
        )
        score = title_quality * 100
        score += 20 if first_author else 0
        score += min(overlap, 5) * 2
        score += 5 if year is not None and candidate_year == year else 0
        score += 8 if is_main_conference else 0
        score += 4 if row and row["kind"] == "conference" else 0
        score -= 4 if is_workshop else 0
        return score, hit


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
