// Deterministic unit tests for the JS resolver — a 1:1 port of the Python test suite
// (tests/test_rankings.py, test_util.py, test_proposal.py, test_pipeline.py). No network.
// Run: node test/unit.mjs   (from plugin/)
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const R = require("../content/scripts/resolver.js");

let pass = 0;
let fail = 0;
const fails = [];
function ok(cond, msg) {
  if (cond) pass++;
  else {
    fail++;
    fails.push(msg);
  }
}
function eq(actual, expected, msg) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  ok(a === e, `${msg}  (got ${a}, want ${e})`);
}

// ----- helpers mirroring tests/conftest.py fixtures -----
function makeResolution(over = {}) {
  return {
    item_key: "K", version: 0, title: "T", arxiv_id: "2106.09685",
    venue_raw: null, canonical: null, kind: "conference", year: 2021,
    core_tier: null, acceptance: "accepted", confidence: 0.9,
    citation_count: null, influential_citations: null,
    sources: [], evidence: [], suggested_tags: [], existing_tags: [], collections: [],
    current_item_type: "preprint", target_item_type: null, fields: {},
    ...over,
  };
}
function makeHit(over = {}) {
  return {
    source: "semantic_scholar", venue_raw: "X", year: 2021, venue_type: "conference",
    citation_count: 100, influential_citations: 10, external_doi: null, dblp_key: null,
    evidence_url: "u", issn: null, abbrev: null,
    ...over,
  };
}
function makeItem(over = {}) {
  const { key = "K", title = "T", archiveID = "", creators = [], collections = [], ...rest } = over;
  return { key, version: 1, data: { title, archiveID, creators, collections, itemType: "preprint", ...rest } };
}

// ===================== rankings =====================
{
  const r = R.lookupRanking("NeurIPS");
  ok(r && r.canonical === "NeurIPS", "exact alias canonical");
  eq(r.core, "A*", "exact alias core");
  eq(r.kind, "conference", "exact alias kind");
  eq(R.lookupRanking("Advances in Neural Information Processing Systems").canonical, "NeurIPS", "full name alias");
  eq(R.lookupRanking("iclr").canonical, "ICLR", "case insensitive");
  eq(
    R.lookupRanking("Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern Recognition").canonical,
    "CVPR",
    "substring match CVPR"
  );
  const j = R.lookupRanking("Journal of Machine Learning Research");
  eq(j.canonical, "JMLR", "JMLR canonical");
  eq(j.kind, "journal", "JMLR kind");
  eq(j.core, "", "journal has no core");
  eq(R.lookupRanking("Some Made-Up Venue 2099"), null, "unknown -> null");
  eq(R.lookupRanking(null), null, "null -> null");
  eq(R.lookupRanking(""), null, "empty -> null");
  eq(R.lookupRanking("   "), null, "spaces -> null");
  eq(R.lookupRanking("NeurIPS Workshop on Deep Learning"), null, "rejects workshop track");
  eq(R.lookupRanking("ICML 2021 Workshop"), null, "rejects workshop 2");
  eq(R.lookupRanking("Findings of the Association for Computational Linguistics: EMNLP 2023"), null, "rejects findings");
  eq(R.lookupRanking("International Conference on Machine Learning and Applications"), null, "rejects compound conf (ICMLA)");
  eq(R.lookupRanking("Nature Communications"), null, "rejects compound journal Nature Communications");
  eq(R.lookupRanking("Science Robotics"), null, "rejects compound journal Science Robotics");
  eq(R.lookupRanking("NeurIPS 2021").canonical, "NeurIPS", "acronym + year");
  eq(R.lookupRanking("USENIX Security Symposium").canonical, "USENIX Security", "generic suffix Symposium");
  eq(R.lookupRanking("ICCV").write_as, "International Conference on Computer Vision", "write_as ICCV");
  eq(R.lookupRanking("ICLR").write_as, "", "write_as empty default");
}

// ===================== util =====================
{
  eq(R.extractArxivId({ archiveID: "arXiv:2106.09685" }), "2106.09685", "archiveID tagged");
  eq(R.extractArxivId({ archiveID: "2106.09685" }), "2106.09685", "archiveID bare");
  eq(R.extractArxivId({ archiveID: "arXiv:2106.09685v3" }), "2106.09685", "archiveID versioned");
  eq(R.extractArxivId({ DOI: "10.48550/arXiv.2302.03693" }), "2302.03693", "doi arxiv");
  eq(R.extractArxivId({ url: "http://arxiv.org/abs/2302.03693" }), "2302.03693", "url");
  eq(R.extractArxivId({ extra: "arXiv:2106.09685 [cs.LG]" }), "2106.09685", "extra");
  eq(R.extractArxivId({ DOI: "10.1109/CVPR.2021.00123", url: "https://x" }), null, "no arxiv -> null");
  eq(R.extractArxivId({}), null, "empty item -> null");

  eq(R.titleJaccard("Attention Is All You Need", "attention is all you need!"), 1.0, "jaccard identical after norm");
  eq(R.titleJaccard("", "x"), 0.0, "jaccard empty");
  eq(R.titleJaccard(null, "x"), 0.0, "jaccard null");
  const partial = R.titleJaccard("deep residual learning", "deep residual learning for image recognition");
  ok(partial > 0 && partial < 1, "jaccard partial in (0,1)");
  ok(R.titleMatch("Generative Adversarial Nets", "Generative Adversarial Nets"), "title match true");
  ok(!R.titleMatch("apples", "oranges entirely different fruit"), "title match false");
  eq(R.normTitle("  Hello,  World! "), "hello world", "norm title strips punct+space");

  eq(
    R.firstAuthorLastname({ creators: [{ creatorType: "editor", lastName: "Ed" }, { creatorType: "author", lastName: "Goodfellow" }] }),
    "Goodfellow",
    "first author skips non-author"
  );
  eq(R.firstAuthorLastname({ creators: [] }), "", "no author empty");
  eq(R.firstAuthorLastname({}), "", "no creators empty");
}

// ===================== proposal =====================
{
  ok(R.isArxivDoi("10.48550/arXiv.2106.09685"), "isArxivDoi true 1");
  ok(R.isArxivDoi("arXiv:1234.5678"), "isArxivDoi true 2");
  ok(!R.isArxivDoi("10.1109/CVPR.2021.00123"), "isArxivDoi false 1");
  ok(!R.isArxivDoi(null), "isArxivDoi null");
  ok(!R.isArxivDoi(""), "isArxivDoi empty");

  // conference basic
  {
    const res = makeResolution({ kind: "conference", canonical: "ICLR", venue_raw: "International Conference on Learning Representations", citation_count: 19435 });
    const hit = makeHit({ external_doi: "10.48550/arXiv.2106.09685", issn: null, abbrev: null });
    const [itype, fields] = R.buildProposal(res, hit, "2106.09685", { extra: "arXiv:2106.09685 [cs]" });
    eq(itype, "conferencePaper", "conf itype");
    eq(fields.proceedingsTitle, "International Conference on Learning Representations", "conf proceedingsTitle");
    eq(fields.conferenceName, fields.proceedingsTitle, "conf conferenceName==proceedingsTitle");
    ok(!("DOI" in fields), "arXiv DOI filtered out");
    ok(fields.extra.includes("Citations: 19435 (SemanticScholar)"), "conf citation line");
    ok(fields.extra.includes("arXiv:2106.09685"), "conf arxiv preserved");
    ok(fields.extra.includes("arxiv-marker: resolved"), "conf stamp");
  }
  // publisher from map
  {
    const res = makeResolution({ kind: "conference", canonical: "CVPR", venue_raw: "Computer Vision and Pattern Recognition" });
    const [, fields] = R.buildProposal(res, makeHit(), "2101.00001", {});
    eq(fields.publisher, "IEEE", "publisher CVPR -> IEEE");
  }
  // real doi kept
  {
    const res = makeResolution({ kind: "conference", canonical: "CVPR", venue_raw: "Computer Vision and Pattern Recognition" });
    const hit = makeHit({ external_doi: "10.1109/CVPR52688.2022.01042" });
    const [, fields] = R.buildProposal(res, hit, "2101.00001", {});
    eq(fields.DOI, "10.1109/CVPR52688.2022.01042", "real DOI kept");
  }
  // journal basic
  {
    const res = makeResolution({ kind: "journal", canonical: "TPAMI", venue_raw: "IEEE Transactions on Pattern Analysis and Machine Intelligence", citation_count: 2485 });
    const hit = makeHit({ external_doi: "10.1109/TPAMI.2021.123", issn: "0162-8828", abbrev: "TPAMI" });
    const [itype, fields] = R.buildProposal(res, hit, "2104.00001", {});
    eq(itype, "journalArticle", "journal itype");
    ok(fields.publicationTitle.startsWith("IEEE Transactions on Pattern Analysis"), "journal publicationTitle");
    eq(fields.journalAbbreviation, "TPAMI", "journal abbrev");
    eq(fields.ISSN, "0162-8828", "journal issn");
    eq(fields.DOI, "10.1109/TPAMI.2021.123", "journal doi");
    ok(!("proceedingsTitle" in fields), "journal has no proceedingsTitle");
  }
  // extra idempotency: rewrites not duplicates
  {
    const prior = "arXiv:2106.09685 [cs]\nCitations: 50 (SemanticScholar) [2026-01-01]\nzotero-marker: resolved 2026-01-01\nUser note: keep me";
    const res = makeResolution({ kind: "conference", canonical: "ICLR", venue_raw: "International Conference on Learning Representations", citation_count: 100 });
    const [, fields] = R.buildProposal(res, makeHit(), "2106.09685", { extra: prior });
    const extra = fields.extra;
    eq((extra.match(/Citations:/g) || []).length, 1, "one citation line");
    ok(extra.includes("Citations: 100 (SemanticScholar)"), "new citation");
    ok(!extra.includes("Citations: 50"), "old citation removed");
    eq((extra.match(/zotero-marker:/g) || []).length, 0, "legacy stamp migrated away");
    eq((extra.match(/arxiv-marker:/g) || []).length, 1, "one current stamp");
    ok(extra.includes("User note: keep me"), "user note preserved");
    eq((extra.match(/arXiv:2106\.09685/g) || []).length, 1, "arxiv not duplicated");
  }
  // removes legacy citation format
  {
    const prior = "19435 citations (Semantic Scholar) [2026-05-30]\narXiv:2106.09685";
    const res = makeResolution({ kind: "conference", canonical: "ICLR", venue_raw: "International Conference on Learning Representations", citation_count: 20000 });
    const [, fields] = R.buildProposal(res, makeHit(), "2106.09685", { extra: prior });
    ok(!fields.extra.includes("19435 citations"), "legacy removed");
    ok(fields.extra.includes("Citations: 20000 (SemanticScholar)"), "current added");
  }
  // preserves foreign citation line
  {
    const prior = "arXiv:2106.09685 [cs]\nCitations: 99 (Crossref) [2025-01-01]\narxiv-marker: resolved 2025-01-01";
    const res = makeResolution({ kind: "conference", canonical: "ICLR", venue_raw: "International Conference on Learning Representations", citation_count: 100 });
    const [, fields] = R.buildProposal(res, makeHit(), "2106.09685", { extra: prior });
    const extra = fields.extra;
    ok(extra.includes("Citations: 99 (Crossref) [2025-01-01]"), "foreign citation preserved");
    ok(extra.includes("Citations: 100 (SemanticScholar)"), "ours added");
    eq((extra.match(/arxiv-marker:/g) || []).length, 1, "stamp not duplicated");
  }
  // fullName
  eq(R.fullName("NeurIPS", null), "Advances in Neural Information Processing Systems", "fullName stopword casing");
  eq(R.fullName("ICLR", "International Conference on Learning Representations"), "International Conference on Learning Representations", "fullName multiword verbatim");
  eq(R.fullName("ICCV", "IEEE International Conference on Computer Vision"), "International Conference on Computer Vision", "fullName write_as pin");
  // build skips
  eq(R.buildProposal(makeResolution({ acceptance: "unknown", canonical: null }), null, null, {}), [null, {}], "skip unknown");
  eq(R.buildProposal(makeResolution({ acceptance: "accepted", canonical: null }), null, null, {}), [null, {}], "skip accepted no canonical");
  // idempotent
  {
    const res = makeResolution({ kind: "conference", canonical: "ICLR", venue_raw: "International Conference on Learning Representations" });
    const data = { itemType: "conferencePaper", proceedingsTitle: "International Conference on Learning Representations", extra: "arXiv:2106.09685" };
    eq(R.buildProposal(res, makeHit(), "2106.09685", data), [null, {}], "already converted -> empty");
    const [it1, f1] = R.buildProposal(res, makeHit(), "2106.09685", { itemType: "preprint" });
    ok(it1 === "conferencePaper" && f1.proceedingsTitle, "still preprint proposes");
    const [it2, f2] = R.buildProposal(res, makeHit(), "2106.09685", { itemType: "conferencePaper", proceedingsTitle: "" });
    ok(it2 === "conferencePaper" && f2.proceedingsTitle, "right type but venue missing proposes");
  }
}

// ===================== pipeline (sync helpers) =====================
{
  const buckets = [[null, null], [4, "<10"], [10, "10+"], [60, "50+"], [150, "100+"], [600, "500+"], [1200, "1000+"], [6000, "5000+"], [15000, "10000+"]];
  for (const [n, exp] of buckets) eq(R.citeBucket(n), exp, `citeBucket ${n}`);

  eq(R.itemYear({ date: "2021-10-16" }), 2021, "itemYear iso");
  eq(R.itemYear({ date: "2024" }), 2024, "itemYear year only");
  eq(R.itemYear({ date: "16/03/1999" }), 1999, "itemYear dmy");
  eq(R.itemYear({ date: "" }), null, "itemYear empty");
  eq(R.itemYear({}), null, "itemYear missing");

  {
    const j = { source: "semantic_scholar", venue_raw: "Communications of the ACM" };
    const c = { source: "dblp", venue_raw: "NeurIPS" };
    const [chosen, row] = R.chooseVenue([j, c]);
    eq(chosen.venue_raw, "NeurIPS", "chooseVenue prefers conference");
    eq(row.canonical, "NeurIPS", "chooseVenue row canonical");
  }
  {
    const h = { source: "dblp", venue_raw: "Some Workshop 2099" };
    const [chosen, row] = R.chooseVenue([h]);
    ok(chosen === h, "chooseVenue unknown still chosen");
    eq(row, null, "chooseVenue unknown row null");
  }
  eq(R.chooseVenue([]), [null, null], "chooseVenue empty");

  eq(R.confidence([], null, null), 0.0, "confidence no chosen");
  {
    const h = { source: "dblp", venue_raw: "Some Workshop 2099" };
    eq(R.confidence([h], h, null), 0.6, "confidence unknown venue string");
  }
  {
    const h = { source: "semantic_scholar", venue_raw: "NeurIPS" };
    eq(R.confidence([h], h, { canonical: "NeurIPS" }), 0.85, "confidence single source");
  }
  {
    const h1 = { source: "semantic_scholar", venue_raw: "NeurIPS" };
    const h2 = { source: "dblp", venue_raw: "Advances in Neural Information Processing Systems" };
    eq(R.confidence([h1, h2], h1, { canonical: "NeurIPS" }), 0.95, "confidence two sources agree");
  }

  {
    const tags = R.buildTags(makeResolution({ canonical: "ICLR", year: 2021, core_tier: "A*", acceptance: "accepted", citation_count: 12000 }));
    for (const t of ["venue:ICLR", "year:2021", "CORE:A*", "acceptance:accepted", "cite:10000+"]) ok(tags.includes(t), `buildTags has ${t}`);
  }

  {
    const a = makeResolution({ item_key: "A", arxiv_id: "2106.09685" });
    const b = makeResolution({ item_key: "B", arxiv_id: "2106.09685" });
    const c = makeResolution({ item_key: "C", arxiv_id: "2207.00001" });
    const d = makeResolution({ item_key: "D", arxiv_id: null });
    eq(R.duplicateArxivGroups([a, b, c, d]), { "2106.09685": ["A", "B"] }, "duplicate groups only repeated");
  }
}

// ===================== resolveItems (injected fake s2/dblp) =====================
function fakeS2(mapping) {
  return { async batchByArxiv(ids) { const o = {}; for (const [k, v] of Object.entries(mapping)) if (ids.includes(k)) o[k] = v; return o; } };
}
function fakeDBLP(hit = null) {
  const calls = [];
  return { calls, async bestByTitle(t, a, y) { calls.push([t, a, y]); return hit; } };
}
async function runResolveTests() {
  // journal republication prefers original conference
  {
    const item = makeItem({ key: "GAN", title: "Generative Adversarial Nets", archiveID: "arXiv:1406.2661", creators: [{ creatorType: "author", lastName: "Goodfellow" }] });
    const s2 = fakeS2({ "1406.2661": { source: "semantic_scholar", venue_raw: "Communications of the ACM", year: 2014, venue_type: "journal", citation_count: 60000, external_doi: "10.1145/3422622" } });
    const dblp = fakeDBLP({ source: "dblp", venue_raw: "NeurIPS", year: 2014, venue_type: "conference" });
    const [res] = await R.resolveItems([item], { s2, dblp });
    // NB: 1406.2661 has a manual override -> NeurIPS, confidence 1.0 (override wins)
    eq(res.canonical, "NeurIPS", "GAN canonical NeurIPS");
    eq(res.core_tier, "A*", "GAN core A*");
    eq(res.citation_count, 60000, "GAN citations from S2");
    eq(res.target_item_type, "conferencePaper", "GAN target conferencePaper");
    eq(res.confidence, 1.0, "GAN override confidence 1.0");
    ok(res.sources.includes("override"), "GAN override in sources");
  }
  // clean conference does not consult dblp
  {
    const item = makeItem({ key: "K", title: "Some ICLR Paper", archiveID: "arXiv:2106.00001" });
    const s2 = fakeS2({ "2106.00001": { source: "semantic_scholar", venue_raw: "International Conference on Learning Representations", year: 2021, venue_type: "conference", citation_count: 10 } });
    const dblp = fakeDBLP();
    const [res] = await R.resolveItems([item], { s2, dblp });
    eq(res.canonical, "ICLR", "clean conf canonical ICLR");
    eq(res.target_item_type, "conferencePaper", "clean conf target");
    eq(dblp.calls.length, 0, "clean conf no dblp call");
  }
  // unknown when no venue
  {
    const item = makeItem({ key: "U", title: "Unpublished Thing", archiveID: "arXiv:2200.00001" });
    const s2 = fakeS2({ "2200.00001": { source: "semantic_scholar", venue_raw: null, citation_count: 5 } });
    const [res] = await R.resolveItems([item], { s2, dblp: fakeDBLP(null) });
    eq(res.acceptance, "unknown", "unknown acceptance");
    eq(res.target_item_type, null, "unknown no target type");
    eq(res.citation_count, 5, "unknown still records citations");
  }
  // journal venue NOT in ranking table -> journalArticle (regression: TNNLS / Science Robotics)
  {
    const item = makeItem({ key: "J", title: "Differentiable Integrated Motion Planning", archiveID: "arXiv:2207.10422" });
    const s2 = fakeS2({ "2207.10422": { source: "semantic_scholar", venue_raw: "IEEE Transactions on Neural Networks and Learning Systems", year: 2023, venue_type: "journal", citation_count: 143, external_doi: "10.1109/TNNLS.2023.3283542", issn: "2162-237X" } });
    const [res] = await R.resolveItems([item], { s2, dblp: fakeDBLP(null) });
    eq(res.kind, "journal", "journal venue kind");
    eq(res.target_item_type, "journalArticle", "journal venue -> journalArticle");
    eq(res.fields.publicationTitle, "IEEE Transactions on Neural Networks and Learning Systems", "journal publicationTitle");
    ok(!("proceedingsTitle" in res.fields), "no proceedingsTitle for a journal");
    ok(!("conferenceName" in res.fields), "no conferenceName for a journal");
  }
  // venue_type derivation (regression: S2 omits publicationVenue.type for TNNLS / Science Robotics)
  {
    const get = async (rec) =>
      (await R.makeS2(async () => ({ status: 200, data: [rec] })).batchByArxiv(["x"]))["x"].venue_type;
    eq(await get({ publicationVenue: { name: "TNNLS", issn: "2162-237X" }, venue: "TNNLS", publicationTypes: ["JournalArticle"] }), "journal", "venue_type journal from publicationTypes");
    eq(await get({ publicationVenue: { name: "Conf" }, venue: "Conf", publicationTypes: ["Conference"] }), "conference", "venue_type conference from publicationTypes");
    eq(await get({ publicationVenue: { name: "Sci Robotics", issn: "2470-9476" }, venue: "Sci Robotics" }), "journal", "venue_type journal from issn");
    eq(await get({ publicationVenue: { name: "X", type: "conference" }, venue: "X", publicationTypes: ["JournalArticle"] }), "conference", "explicit pv.type wins");
    eq(await get({ publicationVenue: { name: "Mystery" }, venue: "Mystery" }), null, "no signal -> null venue_type");
  }
  // collections labelled from map
  {
    const item = makeItem({ key: "K", title: "X", archiveID: "arXiv:2106.00001", collections: ["AAA", "BBB"] });
    const s2 = fakeS2({ "2106.00001": { source: "semantic_scholar", venue_raw: "ICLR", year: 2021, venue_type: "conference", citation_count: 1 } });
    const [res] = await R.resolveItems([item], { s2, dblp: fakeDBLP(), collectionsMap: { AAA: "Foo", BBB: "Bar / Baz" } });
    eq(res.collections, ["Foo", "Bar / Baz"], "collections labelled");
  }
  // collections fall back to keys without map
  {
    const item = makeItem({ key: "K", title: "X", archiveID: "arXiv:2106.00002", collections: ["ZZZ"] });
    const s2 = fakeS2({ "2106.00002": { source: "semantic_scholar", venue_raw: null } });
    const [res] = await R.resolveItems([item], { s2, dblp: fakeDBLP() });
    eq(res.collections, ["ZZZ"], "collections fall back to keys");
  }
}

await runResolveTests();

console.log(`\nunit: ${pass} passed, ${fail} failed`);
if (fail) {
  console.log("\nFAILURES:");
  for (const f of fails) console.log("  ✗ " + f);
  process.exit(1);
}
