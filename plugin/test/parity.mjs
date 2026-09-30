// Parity test: run the JS resolver and the Python resolver on the SAME real arXiv IDs
// against the LIVE Semantic Scholar + OpenReview + DBLP APIs, then diff their outputs.
// "Logic" fields must match exactly; citation_count is a live-changing value passed
// straight through from S2, so a difference there is reported as drift, not a failure.
// Run: node test/parity.mjs   (from plugin/)
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { spawnSync } from "node:child_process";

const require = createRequire(import.meta.url);
const R = require("../content/scripts/resolver.js");
const here = dirname(fileURLToPath(import.meta.url));

const FIELD_KEYS = ["proceedingsTitle", "conferenceName", "publicationTitle", "journalAbbreviation", "ISSN", "DOI", "publisher"];
const LOGIC_KEYS = ["arxiv_id", "canonical", "kind", "core_tier", "year", "acceptance", "confidence", "target_item_type", "sources"];

async function nodeRequest(method, url, { headers, body } = {}) {
  const res = await fetch(url, { method, headers, body });
  let data = null;
  try { data = await res.json(); } catch { /* leave null */ }
  const retryAfter = res.headers.get("retry-after");
  return {
    status: res.status,
    data,
    headers: retryAfter == null ? {} : { "retry-after": retryAfter },
  };
}

function normalizeJs(res) {
  const fields = {};
  for (const k of FIELD_KEYS) if (k in res.fields) fields[k] = res.fields[k] ?? null;
  return {
    key: res.item_key, arxiv_id: res.arxiv_id, canonical: res.canonical, kind: res.kind,
    core_tier: res.core_tier, year: res.year, acceptance: res.acceptance, confidence: res.confidence,
    citation_count: res.citation_count, target_item_type: res.target_item_type,
    sources: res.sources, fields,
  };
}

function pickPython() {
  for (const cmd of ["python", "python3", "py"]) {
    const r = spawnSync(cmd, ["--version"], { encoding: "utf-8" });
    if (r.status === 0 || (r.stdout + r.stderr).toLowerCase().includes("python")) return cmd;
  }
  return null;
}

async function main() {
  const cases = JSON.parse(readFileSync(resolve(here, "cases.json"), "utf-8"));
  const items = cases.map((c) => ({
    key: c.key, version: 1,
    data: { title: c.title, archiveID: c.archiveID || "", date: c.date || "", creators: c.creators || [], itemType: "preprint", extra: "", tags: [] },
  }));

  console.log("running JS resolver against live S2 + OpenReview + DBLP …");
  const jsResRaw = await R.resolveItems(items, {
    request: nodeRequest,
    s2ApiKey: process.env.S2_API_KEY || null,
  });
  const jsRes = jsResRaw.map(normalizeJs);

  const py = pickPython();
  if (!py) {
    console.error("no python found; cannot run parity. JS results:");
    console.log(JSON.stringify(jsRes, null, 2));
    process.exit(2);
  }
  console.log(`running Python resolver (${py}) against live S2 + OpenReview + DBLP …`);
  const pyProc = spawnSync(py, [resolve(here, "dump_resolution.py")], { encoding: "utf-8", maxBuffer: 10 * 1024 * 1024 });
  if (pyProc.status !== 0) {
    console.error("python dumper failed:\n" + pyProc.stderr);
    process.exit(2);
  }
  const pyRes = JSON.parse(pyProc.stdout);

  const pyByKey = Object.fromEntries(pyRes.map((r) => [r.key, r]));
  let mismatches = 0;
  let drift = 0;
  console.log("\nkey      arxiv        canonical     tier  type             conf   cites(js/py)   verdict");
  console.log("-".repeat(96));
  for (const j of jsRes) {
    const p = pyByKey[j.key];
    const diffs = [];
    if (!p) {
      diffs.push("MISSING in python");
    } else {
      for (const k of LOGIC_KEYS) {
        if (JSON.stringify(j[k]) !== JSON.stringify(p[k])) diffs.push(`${k}: js=${JSON.stringify(j[k])} py=${JSON.stringify(p[k])}`);
      }
      for (const k of FIELD_KEYS) {
        if (JSON.stringify(j.fields[k] ?? null) !== JSON.stringify(p.fields[k] ?? null))
          diffs.push(`fields.${k}: js=${JSON.stringify(j.fields[k] ?? null)} py=${JSON.stringify(p.fields[k] ?? null)}`);
      }
    }
    const citeDrift = p && j.citation_count !== p.citation_count;
    if (citeDrift) drift++;
    const verdict = diffs.length ? "✗ MISMATCH" : citeDrift ? "~ cite-drift" : "✓ match";
    if (diffs.length) mismatches++;
    const cites = `${j.citation_count}/${p ? p.citation_count : "-"}`;
    console.log(
      `${j.key.padEnd(8)} ${(j.arxiv_id || "-").padEnd(12)} ${String(j.canonical).padEnd(13)} ${String(j.core_tier).padEnd(5)} ${String(j.target_item_type).padEnd(16)} ${String(j.confidence).padEnd(6)} ${cites.padEnd(14)} ${verdict}`
    );
    for (const d of diffs) console.log("           · " + d);
  }

  console.log("\n" + "=".repeat(96));
  console.log(`parity: ${jsRes.length - mismatches}/${jsRes.length} items match on logic fields; ${drift} live citation-count drift(s).`);
  if (mismatches) {
    console.log("RESULT: FAIL — JS and Python disagree on deterministic fields above.");
    process.exit(1);
  }
  console.log("RESULT: PASS — JS port is faithful to the Python resolver on live data.");
}

main().catch((e) => { console.error(e); process.exit(2); });
