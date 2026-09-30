# arxiv-marker (Zotero 7/9 plugin)

Native port of [arxiv-marker](https://github.com/lelelelelelelelelelelelele/arxiv-marker).
Resolves arXiv preprints to their **real CS/ML venue** (conference/journal) and **CCF/CORE
tier**, then writes the venue field **directly into your local library** — so easyScholar /
zotero-style light up. Deterministic cascade (Semantic Scholar batch → OpenReview → DBLP
residual), no LLM guessing. Runs entirely inside Zotero: **no Python, no local server, no
Web API key.**

## How it works

1. Right-click selected items → **Resolve venue with arxiv-marker** (also under **Tools**).
2. Each item's arXiv id → Semantic Scholar batch → OpenReview structured API → DBLP
   fallback when earlier sources give no recognized conference → CCF/CORE tier + citation
   count.
3. A **review dialog** lists every item; rows at/above your confidence threshold are
   pre-checked, lower-confidence rows are shown but left unchecked (honest abstention).
4. **Write selected** changes the item type (preprint → conferencePaper / journalArticle)
   and fills `proceedingsTitle`/`conferenceName` (or `publicationTitle`), `publisher`,
   real `DOI`/`ISSN`, and preserves the arXiv id + a `Citations: N (SemanticScholar)` line
   in **Extra** (Citation-Tally compatible). Already-resolved items propose nothing (idempotent).

## Layout

```
manifest.json            bootstrap (Zotero 7/9 bootstrapped plugin manifest)
bootstrap.js             registers chrome path, loads scripts, exposes Zotero.ZoteroMarker
prefs.js                 default prefs (optional S2 key, confidence threshold)
content/
  preferences.xhtml      prefs pane
  review.xhtml           review/confirm dialog (modal window)
  scripts/
    zm-data.js           AUTO-GENERATED venue/override tables (tools/gen-data.mjs)
    resolver.js          ported resolver (util+rankings+resolvers+pipeline+proposal)
    arxiv-marker.js     Zotero glue (menu, item I/O, HTTP adapter, write-back)
    review.js            review dialog logic
tools/
  gen-data.mjs           regenerate zm-data.js from ../../data/*.csv
  build-xpi.ps1          package build/arxiv-marker-<version>.xpi
test/
  unit.mjs               deterministic tests (1:1 port of the Python suite)
  parity.mjs             JS vs Python on live S2/OpenReview/DBLP — proves port fidelity
  dump_resolution.py     Python side of the parity check
  cases.json             shared real arXiv ids
```

The resolver (`resolver.js` + `zm-data.js`) is written so the **same files** run both in
Node (CommonJS, for tests) and in Zotero (classic subscripts in a shared scope). All network
I/O is injected via `request(method, url, {headers, body}) -> {status, data, headers}`: a
`fetch` adapter in tests, a `Zotero.HTTP.request` adapter in the plugin.

## Develop

```bash
node tools/gen-data.mjs        # rebuild embedded tables after editing data/*.csv
node test/unit.mjs             # deterministic unit tests (no network)
node test/parity.mjs           # live JS-vs-Python parity (needs network + the Python pkg)
powershell -ExecutionPolicy Bypass -File tools/build-xpi.ps1   # -> build/arxiv-marker-<version>.xpi
```

## Install (Zotero 7+ / 9)

1. `build/arxiv-marker-<version>.xpi`
2. Zotero → **Tools → Plugins** (gear/▾ menu) → **Install Plugin From File…** → pick the `.xpi`.
   (Or drag the `.xpi` onto the Zotero window.)
3. Restart Zotero if prompted.

## 60-second smoke test

1. Select one or more arXiv preprints (e.g. *Attention Is All You Need*, *LoRA*).
2. Right-click → **Resolve venue with arxiv-marker**.
3. The review dialog should show e.g. `NeurIPS / A*`, `ICLR / A*`, the type change, and the
   fields to write. Confirm a row or two with **Write selected**.
4. The item should flip to *Conference Paper*, with the venue + Extra filled.

If anything misbehaves, enable **Help → Debug Output Logging → Enable**, reproduce, then
**View Output** — the plugin logs under `arxiv-marker:` / `Zotero.debug`.
