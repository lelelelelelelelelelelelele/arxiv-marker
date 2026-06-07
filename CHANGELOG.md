# Changelog

All notable changes are documented here. Format based on
[Keep a Changelog](https://keepachangelog.com/); versioning aims for
[SemVer](https://semver.org/).

## [Unreleased]

## [0.2.1] — 2026-06-07

### Fixed
- **Zotero 9 compatibility**: bumped `strict_max_version` to `99.99.99` and added
  `browser_specific_settings` so the plugin installs on Zotero 9.x.
- **Journal venue type detection**: the resolver now uses `publicationTypes` and `ISSN` to
  distinguish journals from conferences, instead of relying solely on `publicationVenue.type`
  which Semantic Scholar omits for some journals (e.g. TNNLS, Science Robotics). Previously
  these were misidentified as conferences and written into `proceedingsTitle`/`conferenceName`.

## [0.2.0] — 2026-06-04

### Added
- **Native Zotero 7/9 plugin** (`arxiv-marker.xpi`): select arXiv preprints, right-click →
  **Resolve venue**, review every proposed change in a dialog, and write back **directly to
  the local library** — no Python, no local server, no Web API key. The resolver is a faithful
  JS port of the Python pipeline, kept in lock-step by a live JS-vs-Python parity test on top
  of 123 deterministic unit tests.
- Plugin preferences pane (optional Semantic Scholar key, confidence threshold), bilingual
  (English / 中文).

### Changed
- **Renamed the project `zotero-marker` → `arxiv-marker`** — a tool name may not contain
  "Zotero" under the [Zotero trademark policy](https://www.zotero.org/support/terms/trademark).
  The Python import package is now `arxiv_marker`; write-back stamps `arxiv-marker: resolved
  <date>`. The idempotent `Extra` rewrite still recognizes the old `zotero-marker:` line and
  migrates it in place, so re-running on previously-marked items does not duplicate.

### Fixed
- The plugin now recognizes already-resolved items and proposes nothing for them on re-run
  (idempotent), and its preferences pane is bilingual.
- ICCV items got no easyScholar CCF tag. Semantic Scholar returns the venue as
  `IEEE International Conference on Computer Vision`, but easyScholar's ICCV (CCF A) entry
  has no `IEEE` prefix — so the venue string written didn't match and the tag stayed blank
  (venue + citation count were unaffected). Added an optional `write_as` column to
  `data/venue_rankings.csv` that pins the exact easyScholar-matching string to write,
  decoupled from the lookup aliases; ICCV now writes `International Conference on Computer
  Vision`. CVPR/ECCV were already correct (S2 returns their prefix-less names).

## [0.1.0] — 2026-05-31

### Added
- Deterministic venue resolver: Semantic Scholar (batch by arXiv id) → DBLP (by title)
  cascade, with an LLM fallback left as a stub.
- CORE/CCF ranking lookup table (`data/venue_rankings.csv`) with alias + substring matching.
- Write-back proposal: `preprint` → `journalArticle`/`conferencePaper` plus
  venue / DOI / ISSN / journalAbbreviation / publisher, preserving the arXiv id and
  citation count in `Extra` (idempotent; Citation-Tally display format).
- Manual overrides (`data/overrides.csv`) keyed by arXiv id; always win, clearly labelled.
- Three review surfaces: CSV, JSON, and a self-contained HTML console.
- Optional local web UI (`web` extra, FastAPI): review + write from the browser.
- CLI: `resolve`, `write`, `web`, and `--version`.
- Duplicate-arXiv detection surfaced in the resolve summary.
- Test suite (pytest, network mocked), ruff lint config, GitHub Actions CI (3.10–3.13),
  MIT license, uv packaging.

### Fixed
- `_confidence` now returns 0.95 when ≥2 sources actually **agree** on a venue. The previous
  set-based `len(canons) >= 2` check only fired when sources *disagreed*, so the documented
  high-confidence tier was effectively unreachable.
