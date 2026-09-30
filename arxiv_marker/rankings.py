"""Venue normalization -> canonical name + CORE tier (deterministic lookup table).

The table in data/venue_rankings.csv is a STARTER set; edit it freely. The CORE tier
(A*/A/B/C) is used internally to disambiguate venues and score confidence — it is NOT
written to Zotero. CCF / 分区 / impact-factor display is left to the plugins (easyScholar
+ zotero-style), which read the venue field this tool writes.
"""
from __future__ import annotations

import csv
import re
from functools import lru_cache

from . import config


@lru_cache(maxsize=1)
def _table() -> list[dict]:
    rows: list[dict] = []
    path = config.DATA_DIR / "venue_rankings.csv"
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            aliases = [a.strip().lower()
                       for a in (r.get("aliases") or "").split("|") if a.strip()]
            aliases.append(r["canonical"].lower())
            rows.append({
                "canonical": r["canonical"],
                "kind": (r.get("kind") or "conference").strip(),
                "core": (r.get("core_tier") or "").strip(),
                "aliases": aliases,
                # Exact string to WRITE when S2's venue name differs from easyScholar's
                # match value (e.g. S2 'IEEE International Conference on Computer Vision'
                # vs easyScholar's prefix-less ICCV). Empty -> use the resolved name.
                "write_as": (r.get("write_as") or "").strip(),
            })
    return rows


# Track/qualifier words that denote a DIFFERENT (usually lower-tier) venue than the
# flagship whose name they contain — e.g. "NeurIPS Workshop", "Findings of ACL".
_DISQUALIFIERS = {"workshop", "workshops", "findings", "doctoral", "companion",
                  "tutorial", "tutorials", "demonstration", "demonstrations",
                  "poster", "abstracts", "satellite"}

# Generic venue-type words that may trail a matched alias WITHOUT changing the venue
# (e.g. "USENIX Security" + "Symposium"). A non-generic trailing word signals a different
# or compound venue ("Nature" + "Communications", "... Machine Learning" + "and Applications").
_GENERIC_SUFFIX = {"symposium", "conference", "conferences", "proceedings", "meeting", "congress"}


def _tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def _run_index(hay: list[str], needle: list[str]) -> int:
    """Index where `needle` occurs as a contiguous run in `hay`, else -1."""
    if not needle:
        return -1
    for i in range(len(hay) - len(needle) + 1):
        if hay[i:i + len(needle)] == needle:
            return i
    return -1


def lookup(venue_raw: str | None) -> dict | None:
    """Map a raw venue string to a ranking-table row, or None.

    Exact alias match wins. Otherwise prefer the longest matching whole-word alias
    (e.g. NAACL's full name contains ACL's shorter alias). An alias may match as a run,
    but only when (a) the string carries no workshop/findings-style qualifier and (b) no
    extra alphabetic word immediately follows the run — so 'Nature Communications',
    'International Conference on Machine Learning and Applications', and 'NeurIPS Workshop'
    do NOT collapse onto the flagship A* venue.
    """
    if not venue_raw:
        return None
    v = venue_raw.lower().strip()
    v_tokens = _tokens(v)
    if not v_tokens:
        return None
    blocked = any(d in v_tokens for d in _DISQUALIFIERS)
    substring_hit = None
    matched_tokens = 0
    for row in _table():
        for a in row["aliases"]:
            if v == a:
                return row
            if blocked:
                continue
            at = _tokens(a)
            if len(at) <= matched_tokens:
                continue
            idx = _run_index(v_tokens, at)
            if idx < 0:
                continue
            after = v_tokens[idx + len(at):]
            if after and not all(t.isdigit() or t in _GENERIC_SUFFIX for t in after):
                continue        # a non-generic trailing word => different/compound venue
            substring_hit = row
            matched_tokens = len(at)
    return substring_hit
