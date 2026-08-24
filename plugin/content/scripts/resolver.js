// arxiv-marker resolver — faithful JS port of the Python package
// (util + rankings + resolvers + pipeline + proposal). Pure logic; all network I/O is
// INJECTED via opts.request(method, url, {headers, body}) -> {status, data}. This lets the
// SAME file run in Node (tests, fetch adapter) and in Zotero (loadSubScript, Zotero.HTTP
// adapter). Top-level vars/functions intentionally live in the shared plugin scope under
// loadSubScript; the guarded module.exports at the bottom is for the Node test harness.

// --- data (embedded; see content/scripts/zm-data.js / tools/gen-data.mjs) -------------
var ZMData =
  typeof require !== "undefined" ? require("./zm-data.js") : { RANKINGS: ZM_RANKINGS, OVERRIDES: ZM_OVERRIDES };

// ====================================================================== util ==========
var _ARXIV_NEW = /(\d{4}\.\d{4,5})(v\d+)?/i;
var _ARXIV_FULL = /^(\d{4}\.\d{4,5})(v\d+)?$/i;
var _ARXIV_TAGGED = /arxiv[:.\s/]*?(\d{4}\.\d{4,5})/i;

function extractArxivId(data) {
  const archiveId = data.archiveID || "";
  let m = archiveId.match(_ARXIV_TAGGED) || archiveId.match(_ARXIV_NEW);
  if (m && (archiveId.toLowerCase().includes("arxiv") || _ARXIV_FULL.test(archiveId.trim() || "x"))) {
    return m[1];
  }
  const doi = data.DOI || "";
  m = doi.match(_ARXIV_TAGGED);
  if (m) return m[1];
  for (const field of ["url", "extra"]) {
    const val = data[field] || "";
    if (val.toLowerCase().includes("arxiv")) {
      m = val.match(_ARXIV_TAGGED) || val.match(_ARXIV_NEW);
      if (m) return m[1];
    }
  }
  return null;
}

function normTitle(s) {
  if (!s) return "";
  s = s.toLowerCase().replace(/[^a-z0-9 ]+/g, " ");
  return s.replace(/\s+/g, " ").trim();
}

function titleJaccard(a, b) {
  const na = normTitle(a);
  const nb = normTitle(b);
  if (!na || !nb) return 0.0;
  if (na === nb) return 1.0;
  const ta = new Set(na.split(" "));
  const tb = new Set(nb.split(" "));
  if (!ta.size || !tb.size) return 0.0;
  let inter = 0;
  for (const t of ta) if (tb.has(t)) inter++;
  const union = new Set([...ta, ...tb]).size;
  return inter / union;
}

function titleMatch(a, b, threshold = 0.85) {
  return titleJaccard(a, b) >= threshold;
}

function firstAuthorLastname(data) {
  for (const c of data.creators || []) {
    if (c.creatorType === "author" && c.lastName) return c.lastName;
  }
  return "";
}

// ================================================================== rankings ==========
const _DISQUALIFIERS = new Set([
  "workshop", "workshops", "findings", "doctoral", "companion", "tutorial", "tutorials",
  "demonstration", "demonstrations", "poster", "abstracts", "satellite",
]);
const _GENERIC_SUFFIX = new Set([
  "symposium", "conference", "conferences", "proceedings", "meeting", "congress",
]);

function _tokens(s) {
  return s.toLowerCase().match(/[a-z0-9]+/g) || [];
}

let _TABLE_CACHE = null;
function _table() {
  if (_TABLE_CACHE) return _TABLE_CACHE;
  _TABLE_CACHE = ZMData.RANKINGS.map((r) => {
    const aliases = (r.aliases || "")
      .split("|")
      .map((a) => a.trim().toLowerCase())
      .filter(Boolean);
    aliases.push(r.canonical.toLowerCase());
    return {
      canonical: r.canonical,
      kind: (r.kind || "conference").trim(),
      core: (r.core_tier || "").trim(),
      aliases,
      write_as: (r.write_as || "").trim(),
    };
  });
  return _TABLE_CACHE;
}

function _runIndex(hay, needle) {
  if (!needle.length) return -1;
  for (let i = 0; i <= hay.length - needle.length; i++) {
    let ok = true;
    for (let j = 0; j < needle.length; j++) {
      if (hay[i + j] !== needle[j]) {
        ok = false;
        break;
      }
    }
    if (ok) return i;
  }
  return -1;
}

function lookupRanking(venueRaw) {
  if (!venueRaw) return null;
  const v = venueRaw.toLowerCase().trim();
  const vTokens = _tokens(v);
  if (!vTokens.length) return null;
  const blocked = vTokens.some((t) => _DISQUALIFIERS.has(t));
  let substringHit = null;
  for (const row of _table()) {
    for (const a of row.aliases) {
      if (v === a) return row;
      if (blocked || substringHit !== null) continue;
      const at = _tokens(a);
      const idx = _runIndex(vTokens, at);
      if (idx < 0) continue;
      const after = vTokens.slice(idx + at.length);
      if (after.length && !after.every((t) => /^\d+$/.test(t) || _GENERIC_SUFFIX.has(t))) continue;
      substringHit = row;
    }
  }
  return substringHit;
}

// ================================================================= resolvers ==========
function isNonvenue(v) {
  if (!v) return true;
  const s = v.trim().toLowerCase();
  return s.includes("arxiv") || s === "corr" || s === "preprint" || s === "";
}

function _s2VenueType(pv, pubTypes) {
  // S2 often omits publicationVenue.type even when it clearly knows the paper is a
  // JournalArticle and gives the venue an ISSN (TNNLS / Science Robotics). Reading only
  // pv.type made those come back null and the proposal defaulted them to conferencePaper.
  // Fall back to the per-paper publicationTypes, then to the presence of an ISSN.
  if (pv.type) return pv.type;
  const types = pubTypes || [];
  if (types.includes("JournalArticle")) return "journal";
  if (types.includes("Conference")) return "conference";
  if (pv.issn) return "journal";
  return null;
}

function _authorsOf(info) {
  let a = (info.authors || {}).author;
  if (a && !Array.isArray(a)) a = [a];
  return (a || []).filter((x) => x && typeof x === "object").map((x) => x.text || "");
}

const _OPENREVIEW_SEARCH = "https://api2.openreview.net/notes/search";
const _OPENREVIEW_REJECT_STATE =
  /\b(?:submitted|submission|reject(?:ed|ion)?|withdrawn)\b|\bunder[\s_-]+review\b|\bdesk[\s_-]+rejected\b/i;
const _OPENREVIEW_PRESENTATION =
  /(?:\s*[-–—,:]\s*|\s+)(?:poster|spotlight|oral)(?:\s+presentation)?\s*$/i;

function openReviewValue(content, key) {
  const value = content[key];
  if (value && typeof value === "object" && !Array.isArray(value) && "value" in value) return value.value;
  return value;
}

function openReviewRenderedTitle(value) {
  if (!value) return "";
  value = String(value)
    .normalize("NFKC")
    .replace(/[‘’]/g, "'")
    .replace(/[“”]/g, '"')
    .replace(/[‐‑‒–—]/g, "-")
    .replace(/\u00a0/g, " ");
  for (const delimiter of ["\\(", "\\)", "\\[", "\\]"]) value = value.split(delimiter).join("");
  return value.replace(/\$/g, "").replace(/\s+/g, " ").trim();
}

function openReviewNormTitle(value) {
  return openReviewRenderedTitle(value).toLowerCase();
}

function openReviewTitleQuality(query, candidate) {
  const q = openReviewNormTitle(query);
  const c = openReviewNormTitle(candidate);
  if (!q || !c) return 0;
  if (q === c) return 2;
  if (c.startsWith(q + ":") || q.startsWith(c + ":")) return 1;
  return 0;
}

function openReviewSurname(name) {
  let value = String(name || "");
  if (value.includes(",")) value = value.split(",", 1)[0];
  value = value.normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase();
  const parts = value.match(/\p{L}+/gu) || [];
  while (parts.length && ["jr", "sr", "ii", "iii", "iv"].includes(parts[parts.length - 1])) parts.pop();
  return parts.length ? parts[parts.length - 1] : "";
}

function openReviewAuthorMatch(expected, candidate) {
  const exp = (expected || []).map(openReviewSurname).filter(Boolean);
  const got = (candidate || []).map(openReviewSurname).filter(Boolean);
  if (!exp.length || !got.length) return { ok: true, overlap: 0, first: false };
  const expSet = new Set(exp);
  const gotSet = new Set(got);
  let overlap = 0;
  for (const surname of expSet) if (gotSet.has(surname)) overlap++;
  const first = exp[0] === got[0];
  return { ok: first || overlap >= 2, overlap, first };
}

function openReviewYear(content, venue, venueId) {
  const value = openReviewValue(content, "year");
  if (/^\d+$/.test(String(value || ""))) return parseInt(value, 10);
  const match = `${venue} ${venueId}`.match(/\b(?:19|20)\d{2}\b/);
  return match ? parseInt(match[0], 10) : null;
}

function openReviewDoi(content) {
  for (const key of ["doi", "DOI"]) {
    const value = openReviewValue(content, key);
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

const _S2_FIELDS =
  "title,venue,publicationVenue,year,externalIds,publicationTypes,citationCount,influentialCitationCount";
const _S2_BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch";
const _OPENREVIEW_MAX_RETRIES = 2;
const _OPENREVIEW_BACKOFF_MS = 1000;
const _OPENREVIEW_MAX_DELAY_MS = 30000;

function _defaultSleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function makeS2(request, apiKey, sleep) {
  sleep = sleep || _defaultSleep;
  const baseHeaders = {};
  if (apiKey) baseHeaders["x-api-key"] = apiKey;

  async function postWithRetry(payload, tries = 6) {
    let delay = 3000;
    for (let i = 0; i < tries; i++) {
      try {
        const res = await request("POST", _S2_BATCH + "?fields=" + encodeURIComponent(_S2_FIELDS), {
          headers: { ...baseHeaders, "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (res.status === 429) {
          await sleep(delay);
          delay = Math.min(delay * 2, 30000);
          continue;
        }
        if (res.status < 200 || res.status >= 300) throw new Error("HTTP " + res.status);
        return res.data;
      } catch (e) {
        await sleep(delay);
        delay = Math.min(delay * 2, 30000);
      }
    }
    return null;
  }

  async function batchByArxiv(arxivIds) {
    const out = {};
    for (let i = 0; i < arxivIds.length; i += 100) {
      const chunk = arxivIds.slice(i, i + 100);
      const recs = await postWithRetry({ ids: chunk.map((a) => "ARXIV:" + a) });
      if (!recs) continue;
      for (let j = 0; j < chunk.length; j++) {
        const rec = recs[j];
        if (!rec) continue;
        const pv = rec.publicationVenue || {};
        const ext = rec.externalIds || {};
        const name = pv.name || rec.venue;
        const alts = pv.alternate_names || [];
        const abbrev =
          alts.find((a) => /[A-Z]/.test(a) && a === a.toUpperCase() && a.length >= 2 && a.length <= 8) || null;
        out[chunk[j]] = {
          source: "semantic_scholar",
          venue_raw: isNonvenue(name) ? null : name,
          year: rec.year ?? null,
          venue_type: _s2VenueType(pv, rec.publicationTypes),
          citation_count: rec.citationCount ?? null,
          influential_citations: rec.influentialCitationCount ?? null,
          external_doi: ext.DOI ?? null,
          dblp_key: ext.DBLP ?? null,
          evidence_url: "https://www.semanticscholar.org/arxiv/" + chunk[j],
          issn: pv.issn ?? null,
          abbrev,
        };
      }
    }
    return out;
  }

  return { batchByArxiv };
}

function makeOpenReview(
  request,
  sleep = _defaultSleep,
  randomFn = Math.random,
  maxRetries = _OPENREVIEW_MAX_RETRIES,
  backoffMs = _OPENREVIEW_BACKOFF_MS,
  maxDelayMs = _OPENREVIEW_MAX_DELAY_MS,
) {
  maxRetries = Math.max(0, Math.trunc(maxRetries));
  backoffMs = Math.max(0, Number(backoffMs));
  maxDelayMs = Math.max(0, Number(maxDelayMs));

  function header(res, name) {
    const headers = res && res.headers;
    if (!headers) return null;
    if (typeof headers.get === "function") return headers.get(name);
    const wanted = name.toLowerCase();
    for (const [key, value] of Object.entries(headers)) {
      if (key.toLowerCase() === wanted) return value;
    }
    return null;
  }

  function retryAfterMs(res) {
    const raw = header(res, "Retry-After");
    if (raw === null || raw === undefined) return null;
    const numeric = Number(raw);
    let delay;
    if (Number.isFinite(numeric)) delay = numeric * 1000;
    else {
      const retryAt = Date.parse(String(raw));
      if (!Number.isFinite(retryAt)) return null;
      delay = retryAt - Date.now();
    }
    // Retry-After is authoritative. maxDelayMs caps only our generated backoff.
    return Math.max(0, delay);
  }

  function retryDelayMs(res, retryNumber) {
    const retryAfter = retryAfterMs(res);
    if (retryAfter !== null) return retryAfter;
    const delay = backoffMs * (2 ** retryNumber) + randomFn() * backoffMs;
    return Math.min(delay, maxDelayMs);
  }

  function candidateFromNote(note, title, authors, year, arxivId) {
    if (!note || typeof note !== "object" || !note.content || typeof note.content !== "object") return null;
    const content = note.content;
    const candidateTitle = openReviewValue(content, "title");
    if (typeof candidateTitle !== "string") return null;
    const titleQuality = openReviewTitleQuality(title, candidateTitle);
    if (!titleQuality) return null;

    const rawAuthors = openReviewValue(content, "authors");
    const candidateAuthors = Array.isArray(rawAuthors) ? rawAuthors.filter((x) => typeof x === "string") : [];
    const authorMatch = openReviewAuthorMatch(authors, candidateAuthors);
    if (!authorMatch.ok || (titleQuality === 1 && (!(authors || []).length || !candidateAuthors.length))) return null;

    if (arxivId) {
      for (const key of ["arxiv", "arxiv_id", "arxivId"]) {
        const value = openReviewValue(content, key);
        if (typeof value === "string") {
          const match = value.match(/\d{4}\.\d{4,5}/);
          if (match && match[0] !== arxivId) return null;
        }
      }
    }

    const venueValue = openReviewValue(content, "venue");
    const venueIdValue = openReviewValue(content, "venueid") || openReviewValue(content, "venue_id");
    if (typeof venueValue !== "string" || typeof venueIdValue !== "string") return null;
    const venue = venueValue.trim();
    const venueId = venueIdValue.trim();
    const decision = openReviewValue(content, "decision");
    const status = openReviewValue(content, "status");
    if (_OPENREVIEW_REJECT_STATE.test(`${venue} ${venueId} ${decision || ""} ${status || ""}`) || isNonvenue(venue)) return null;
    if (/^(?:poster|spotlight|oral)(?:\s+presentation)?$/i.test(venue)) return null;

    const provenance = [note.domain, note.invitations, note.signatures].map((x) => String(x || "")).join(" ");
    if (/(?:^|[./_\s-])dblp(?:[./_\s-]|$)/i.test(provenance)) return null;

    const venueRaw = venue.replace(_OPENREVIEW_PRESENTATION, "").trim();
    if (!venueRaw || isNonvenue(venueRaw)) return null;
    const row = lookupRanking(venueRaw);
    const venueScope = `${venueId} ${note.domain || ""}`.toLowerCase();
    const isWorkshop = venueScope.includes("/workshop") || venueRaw.toLowerCase().includes("workshop");
    const isMainConference = venueScope.includes("/conference") && !isWorkshop;
    let venueType = null;
    if (venueScope.includes("/conference") || venueScope.includes("/workshop")) venueType = "conference";
    else if (row) venueType = row.kind;
    else return null;

    const candidateYear = openReviewYear(content, venueRaw, venueId);
    const forum = note.forum || note.id;
    if (typeof forum !== "string" || !forum) return null;
    const hit = {
      source: "openreview",
      venue_raw: venueRaw,
      year: candidateYear,
      venue_type: venueType,
      citation_count: null,
      influential_citations: null,
      external_doi: openReviewDoi(content),
      dblp_key: null,
      evidence_url: "https://openreview.net/forum?id=" + encodeURIComponent(forum),
      issn: null,
      abbrev: null,
    };
    let score = titleQuality * 100;
    if (authorMatch.first) score += 20;
    score += Math.min(authorMatch.overlap, 5) * 2;
    if (year !== null && year !== undefined && candidateYear === year) score += 5;
    if (isMainConference) score += 8;
    if (row && row.kind === "conference") score += 4;
    if (isWorkshop) score -= 4;
    return { score, hit };
  }

  async function search(term) {
    if (typeof request !== "function") return null;
    const params =
      "term=" + encodeURIComponent(term) + "&content=title&type=exact&source=forum&limit=25";
    let payload = null;
    for (let retryNumber = 0; retryNumber <= maxRetries; retryNumber++) {
      try {
        const res = await request("GET", _OPENREVIEW_SEARCH + "?" + params, {});
        if (res && res.status === 429) {
          if (retryNumber >= maxRetries) return null;
          await sleep(retryDelayMs(res, retryNumber));
          continue;
        }
        if (!res || res.status < 200 || res.status >= 300) return null;
        payload = res.data;
        break;
      } catch (e) {
        return null;
      }
    }
    if (!payload || typeof payload !== "object" || !Array.isArray(payload.notes)) return null;
    return payload.notes;
  }

  async function bestByTitle(title, authors = [], year = null, arxivId = null) {
    const rendered = openReviewRenderedTitle(title);
    const notes = await search(rendered || title);
    if (!notes || !notes.length) return null;
    const candidates = [];
    for (const note of notes) {
      try {
        const candidate = candidateFromNote(note, title, authors || [], year, arxivId);
        if (candidate) candidates.push(candidate);
      } catch (e) {
        // One malformed Note must not abort the item or block the DBLP fallback.
      }
    }
    if (!candidates.length) return null;
    const bestScore = Math.max(...candidates.map((x) => x.score));
    const best = candidates.filter((x) => x.score === bestScore).map((x) => x.hit);
    const identities = new Set(best.map((h) => `${h.venue_raw.toLowerCase()}\u0000${h.year}`));
    if (identities.size > 1) return null;
    best.sort((a, b) => {
      const au = a.evidence_url || "";
      const bu = b.evidence_url || "";
      return au < bu ? -1 : au > bu ? 1 : 0;
    });
    return best[0];
  }

  return { bestByTitle };
}

function makeDBLP(request) {
  async function search(query) {
    const url = "https://dblp.org/search/publ/api?q=" + encodeURIComponent(query) + "&format=json&h=10";
    try {
      const res = await request("GET", url, {});
      if (res.status < 200 || res.status >= 300) return null;
      const hits = (((res.data || {}).result || {}).hits || {}).hit || [];
      return hits;
    } catch (e) {
      return null;
    }
  }

  async function bestByTitle(title, authorLastname = "", year = null) {
    let hits = await search((title + " " + authorLastname).trim());
    if (!hits || !hits.length) hits = await search(title);
    hits = hits || [];
    const cands = [];
    for (const h of hits) {
      const info = h.info || {};
      const t = info.title || "";
      const jac = titleJaccard(title, t);
      const authors = _authorsOf(info);
      const ys = String(info.year ?? "");
      const y = /^\d+$/.test(ys) ? parseInt(ys, 10) : null;
      const authorOk =
        !!authorLastname && authors.some((a) => a.toLowerCase().includes(authorLastname.toLowerCase()));
      const yearOk = year !== null && y === year;
      if (!(jac >= 0.85 || (jac >= 0.5 && authorOk && yearOk))) continue;
      let venue = info.venue;
      if (Array.isArray(venue)) venue = venue.length ? venue[0] : null;
      if (isNonvenue(venue)) continue;
      const typ = info.type || "";
      const kind = typ.includes("Conference") ? "conference" : "journal";
      cands.push({ venue, y, kind, info });
    }
    if (!cands.length) return null;

    const score = (c) => {
      let s = 4;
      if (c.kind === "conference") s += 3;
      if (year !== null && c.y === year) s += 2;
      if (lookupRanking(c.venue)) s += 1;
      return s;
    };
    let best = cands[0];
    let bestScore = score(best);
    for (let i = 1; i < cands.length; i++) {
      const sc = score(cands[i]);
      if (sc > bestScore) {
        best = cands[i];
        bestScore = sc;
      }
    }
    return {
      source: "dblp",
      venue_raw: best.venue,
      year: best.y,
      venue_type: best.kind,
      external_doi: best.info.doi ?? null,
      evidence_url: best.info.url ?? null,
      citation_count: null,
      influential_citations: null,
      issn: null,
      abbrev: null,
      dblp_key: null,
    };
  }

  return { bestByTitle };
}

// ================================================================== pipeline ==========
const _CITE_BUCKETS = [10000, 5000, 1000, 500, 100, 50, 10];

function citeBucket(n) {
  if (n === null || n === undefined) return null;
  for (const b of _CITE_BUCKETS) if (n >= b) return `${b}+`;
  return "<10";
}

function itemYear(data) {
  const d = (data.date || "").trim();
  for (let i = 0; i < d.length - 3; i++) {
    const chunk = d.slice(i, i + 4);
    if (/^\d{4}$/.test(chunk) && (chunk.startsWith("19") || chunk.startsWith("20"))) return parseInt(chunk, 10);
  }
  return null;
}

function itemAuthors(data) {
  const authors = [];
  for (const creator of data.creators || []) {
    if (creator.creatorType !== "author") continue;
    if (creator.name) {
      authors.push(creator.name);
      continue;
    }
    const name = [creator.firstName, creator.lastName].filter(Boolean).join(" ");
    if (name) authors.push(name);
  }
  return authors;
}

function chooseVenue(hits) {
  const scored = [];
  for (const h of hits) {
    if (!h || !h.venue_raw) continue;
    const row = lookupRanking(h.venue_raw);
    let score = 0;
    if (row) {
      score += 3;
      if (row.kind === "conference") score += 2;
      if (row.core === "A*") score += 1;
    }
    scored.push({ score, h, row });
  }
  if (!scored.length) return [null, null];
  // stable sort: equal scores keep original (hit) order
  scored.sort((a, b) => b.score - a.score);
  return [scored[0].h, scored[0].row];
}

function confidence(hits, chosen, row) {
  if (!chosen) return 0.0;
  if (!row) return 0.6;
  let agree = 0;
  for (const h of hits) {
    if (!h || !h.venue_raw) continue;
    const r = lookupRanking(h.venue_raw);
    if (r && r.canonical === row.canonical) agree++;
  }
  return agree >= 2 ? 0.95 : 0.85;
}

function buildTags(res) {
  const tags = [];
  if (res.canonical) tags.push(`venue:${res.canonical}`);
  if (res.year) tags.push(`year:${res.year}`);
  if (res.core_tier) tags.push(`CORE:${res.core_tier}`);
  tags.push(`acceptance:${res.acceptance}`);
  const bucket = citeBucket(res.citation_count);
  if (bucket) tags.push(`cite:${bucket}`);
  return tags;
}

function duplicateArxivGroups(results) {
  const groups = {};
  for (const r of results) {
    if (r.arxiv_id) (groups[r.arxiv_id] = groups[r.arxiv_id] || []).push(r.item_key);
  }
  const out = {};
  for (const [aid, keys] of Object.entries(groups)) if (keys.length > 1) out[aid] = keys;
  return out;
}

function overridesGet(arxivId) {
  if (!arxivId) return null;
  return ZMData.OVERRIDES[arxivId.trim()] || null;
}

async function resolveItems(items, opts = {}) {
  const request = opts.request;
  const cmap = opts.collectionsMap || {};
  const today = opts.today || _todayStr();
  // Resolver options let tests inject fakes (mirrors the Python suite); production builds
  // all three structured clients from the injected request.
  const s2 = opts.s2 || makeS2(request, opts.s2ApiKey, opts.sleep);
  const openreview = opts.openreview !== undefined
    ? opts.openreview
    : typeof request === "function" ? makeOpenReview(request, opts.sleep) : null;
  const dblp = opts.dblp || makeDBLP(request);

  const arxivOf = {};
  for (const it of items) arxivOf[it.key] = extractArxivId(it.data);
  const ids = [...new Set(Object.values(arxivOf).filter(Boolean))].sort();
  const s2map = ids.length ? await s2.batchByArxiv(ids) : {};

  const results = [];
  // The loop awaits each item (max OpenReview concurrency: one); cache both hits and
  // misses so duplicate Zotero items do not repeat a lookup within this resolution run.
  const openreviewCache = new Map();
  for (const it of items) {
    const data = it.data;
    const key = it.key;
    const title = data.title || "";
    const aid = arxivOf[key];
    const s2hit = aid ? s2map[aid] || null : null;

    const hits = [s2hit].filter(Boolean);
    let needFallback = !s2hit || !s2hit.venue_raw;
    if (!needFallback) {
      const row = lookupRanking(s2hit.venue_raw);
      if (!(row && row.kind === "conference")) needFallback = true;
    }
    const yearHint = (s2hit && s2hit.year) || itemYear(data);
    let openreviewHit = null;
    if (needFallback && openreview) {
      const authors = itemAuthors(data);
      const cacheKey = aid
        ? `arxiv:${aid}`
        : JSON.stringify(["metadata", normTitle(title), authors, yearHint]);
      if (!openreviewCache.has(cacheKey)) {
        openreviewCache.set(
          cacheKey,
          await openreview.bestByTitle(title, authors, yearHint, aid),
        );
      }
      openreviewHit = openreviewCache.get(cacheKey);
      if (openreviewHit) hits.push(openreviewHit);
    }
    if (needFallback && !openreviewHit) {
      const dblpHit = await dblp.bestByTitle(title, firstAuthorLastname(data), yearHint);
      if (dblpHit) hits.push(dblpHit);
    }

    const [chosen, row] = chooseVenue(hits);
    const cites = s2hit ? s2hit.citation_count : null;
    const infl = s2hit ? s2hit.influential_citations : null;

    const res = {
      item_key: key,
      version: it.version || data.version || 0,
      title,
      arxiv_id: aid,
      venue_raw: chosen ? chosen.venue_raw : null,
      canonical: row ? row.canonical : chosen ? chosen.venue_raw : null,
      kind: row ? row.kind : chosen ? chosen.venue_type : null,
      year: chosen && chosen.year ? chosen.year : s2hit ? s2hit.year : null,
      core_tier: row && row.core ? row.core : null,
      acceptance: chosen ? "accepted" : "unknown",
      confidence: confidence(hits, chosen, row),
      citation_count: cites,
      influential_citations: infl,
      sources: hits.map((h) => h.source),
      evidence: hits.map((h) => `${h.source}: ${h.venue_raw || "-"} (${h.evidence_url || ""})`),
      suggested_tags: [],
      existing_tags: (data.tags || []).map((t) => t.tag || ""),
      collections: (data.collections || []).map((k) => cmap[k] || k),
      current_item_type: data.itemType || "preprint",
      target_item_type: null,
      fields: {},
    };
    res.suggested_tags = buildTags(res);

    const ov = overridesGet(aid);
    if (ov && ov.canonical) {
      const orow = lookupRanking(ov.canonical);
      res.canonical = orow ? orow.canonical : ov.canonical;
      res.venue_raw = ov.canonical;
      res.kind = orow ? orow.kind : res.kind;
      res.year = ov.year || res.year;
      res.core_tier = orow && orow.core ? orow.core : null;
      res.acceptance = "accepted";
      res.confidence = 1.0;
      res.sources = ["override", ...res.sources];
      res.evidence = [`override: ${ov.canonical}`, ...res.evidence];
      res.suggested_tags = buildTags(res);
    }

    const proposalHit = chosen && chosen.source === "openreview" && chosen.external_doi ? chosen : s2hit;
    const [itype, fields] = buildProposal(res, proposalHit, aid, data, today);
    res.target_item_type = itype;
    res.fields = fields;

    results.push(res);
    if (opts.onProgress) opts.onProgress(res);
  }
  return results;
}

// ================================================================== proposal ==========
const _TOOL_LINE =
  /^\s*(?:\d+\s+citations\s*\(semantic\s*scholar\)|citations:\s*\d+\s*\(semanticscholar\)|(?:zotero|arxiv)-marker:)/i;

const _PUBLISHER = {
  CVPR: "IEEE", ICCV: "IEEE", WACV: "IEEE", ICRA: "IEEE", IROS: "IEEE",
  ECCV: "Springer",
  ICML: "PMLR", AISTATS: "PMLR", COLT: "PMLR", UAI: "PMLR",
  ACL: "ACL", EMNLP: "ACL", NAACL: "ACL",
  KDD: "ACM", WWW: "ACM", SIGIR: "ACM", SIGGRAPH: "ACM", "ACM MM": "ACM",
  AAAI: "AAAI Press", IJCAI: "IJCAI", "USENIX Security": "USENIX",
};

const _TITLE_STOPWORDS = new Set(["a", "an", "the", "of", "for", "and", "in", "on", "to", "at", "via"]);

function isArxivDoi(doi) {
  return !!doi && doi.toLowerCase().includes("arxiv");
}

function smartTitle(s) {
  const words = s.split(/\s+/).filter(Boolean);
  return words
    .map((w, i) =>
      i && _TITLE_STOPWORDS.has(w.toLowerCase()) ? w.toLowerCase() : w.slice(0, 1).toUpperCase() + w.slice(1)
    )
    .join(" ");
}

function fullName(canonical, raw) {
  const row = lookupRanking(canonical || raw || "");
  if (row && row.write_as) return row.write_as;
  if (raw && raw.split(/\s+/).filter(Boolean).length >= 2) return raw;
  if (row) {
    const cands = [row.canonical, ...row.aliases];
    let longest = cands[0];
    for (const c of cands) if (c.length > longest.length) longest = c;
    if (longest.split(/\s+/).filter(Boolean).length >= 2) return smartTitle(longest);
  }
  return raw || canonical || "";
}

function _todayStr() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function buildProposal(res, s2hit, arxivId, data, today) {
  today = today || _todayStr();
  if (res.acceptance !== "accepted" || !res.canonical) return [null, {}];

  const name = fullName(res.canonical, res.venue_raw);
  let doi = null;
  let issn = null;
  let abbrev = res.canonical;
  if (s2hit) {
    if (s2hit.external_doi && !isArxivDoi(s2hit.external_doi)) doi = s2hit.external_doi;
    issn = s2hit.issn;
    abbrev = s2hit.abbrev || res.canonical;
  }

  const fields = {};
  let itype;
  if ((res.kind || "conference") === "journal") {
    itype = "journalArticle";
    fields.publicationTitle = name;
    if (abbrev) fields.journalAbbreviation = abbrev;
    if (issn) fields.ISSN = issn;
  } else {
    itype = "conferencePaper";
    fields.proceedingsTitle = name;
    fields.conferenceName = name;
    const pub = _PUBLISHER[res.canonical];
    if (pub) fields.publisher = pub;
  }
  if (doi) fields.DOI = doi;

  const extra = data.extra || "";
  const lines = extra ? extra.split(/\r\n|\r|\n/) : [];
  const kept = lines.filter((ln) => !_TOOL_LINE.test(ln));
  if (arxivId && !kept.some((ln) => ln.toLowerCase().includes("arxiv"))) kept.push(`arXiv:${arxivId}`);
  if (res.citation_count !== null && res.citation_count !== undefined) {
    kept.push(`Citations: ${res.citation_count} (SemanticScholar) [${today}]`);
  }
  kept.push(`arxiv-marker: resolved ${today}`);
  fields.extra = kept.join("\n").trim();

  const venueField = itype === "journalArticle" ? "publicationTitle" : "proceedingsTitle";
  if ((data.itemType || "") === itype && (data[venueField] || "") === (fields[venueField] || "")) {
    return [null, {}];
  }
  return [itype, fields];
}

// --- exports (Node test harness only; under loadSubScript these live in the scope) -----
var ZMResolver = {
  extractArxivId, normTitle, titleJaccard, titleMatch, firstAuthorLastname,
  lookupRanking, isNonvenue, openReviewNormTitle, openReviewTitleQuality,
  makeS2, makeOpenReview, makeDBLP,
  citeBucket, itemYear, itemAuthors, chooseVenue, confidence, buildTags, duplicateArxivGroups,
  resolveItems, overridesGet,
  isArxivDoi, smartTitle, fullName, buildProposal,
};
if (typeof module !== "undefined" && module.exports) {
  module.exports = ZMResolver;
}
