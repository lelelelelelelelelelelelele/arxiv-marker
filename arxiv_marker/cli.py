"""Command-line interface.

  python run.py resolve [--limit N] [--items KEY,KEY] [--out DIR]   # dry-run, writes nothing to Zotero
  python run.py write   [--threshold 0.85] [--yes]                  # apply tags (additive)
"""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__, config
from .pipeline import duplicate_arxiv_groups, resolve_items
from .rankings import lookup as _rank  # noqa: F401  (ensures table loads early)
from .report import write_reports
from .resolvers import DBLP, OpenReview, SemanticScholar
from .zotero_api import ZoteroClient


def _gather(zot: ZoteroClient, items_filter: list[str] | None, limit: int | None) -> list[dict]:
    out = []
    for it in zot.iter_items(item_type="preprint", limit=100):
        if items_filter and it["key"] not in items_filter:
            continue
        # never touch a preprint that somehow already carries a real venue
        if (it["data"].get("publicationTitle") or "").strip():
            continue
        out.append(it)
        if limit and len(out) >= limit and not items_filter:
            break
    return out


def cmd_resolve(args) -> int:
    if not config.ZOTERO_LIBRARY_ID:
        print("! Set ZOTERO_LIBRARY_ID in .env to your Zotero user/library id "
              "(see https://www.zotero.org/settings/keys).", file=sys.stderr)
        return 2
    zot = ZoteroClient()
    if not zot.ping():
        print(f"! Cannot reach Zotero at {zot.base}. Is Zotero desktop running?", file=sys.stderr)
        return 2

    items_filter = [k.strip() for k in args.items.split(",")] if args.items else None
    items = _gather(zot, items_filter, args.limit)
    print(f"Candidates: {len(items)} preprint item(s)")

    try:  # collection labels are a nice-to-have for the review UIs, never fatal
        cmap = zot.get_collections()
    except Exception:  # noqa: BLE001
        cmap = {}
    if cmap:
        print(f"Collections: {len(cmap)} (items will be labelled by collection)")

    s2 = SemanticScholar()
    openreview = OpenReview()
    dblp = DBLP()
    print(f"Semantic Scholar key: {'yes' if s2.has_key else 'NO (using OpenReview + DBLP + rate-limited S2)'}")

    n = len(items)
    def progress(res):
        mark = "OK " if res.acceptance == "accepted" else "?? "
        v = res.canonical or "-"
        tier = res.core_tier or "-"
        print(f"  [{mark}] {v:<28} CORE:{tier:<3} conf={res.confidence:.2f}  "
              f"cite={res.citation_count if res.citation_count is not None else '-'}  "
              f"{res.title[:48]}")

    results = resolve_items(items, s2, dblp, progress=progress, collections_map=cmap,
                            openreview=openreview)
    csv_path, json_path, html_path = write_reports(results, args.out_path)

    accepted = [r for r in results if r.acceptance == "accepted"]
    print("\n--- summary ---")
    print(f"  resolved (venue found): {len(accepted)}/{n}")
    print(f"  unknown (no venue):     {n - len(accepted)}/{n}")
    by_tier: dict[str, int] = {}
    for r in accepted:
        by_tier[r.core_tier or "(unranked)"] = by_tier.get(r.core_tier or "(unranked)", 0) + 1
    for tier, c in sorted(by_tier.items()):
        print(f"    CORE {tier}: {c}")

    dups = duplicate_arxiv_groups(results)
    if dups:
        print(f"\n  ! {len(dups)} duplicate arXiv id(s) across items "
              "(same preprint added twice — consider merging in Zotero):")
        for aid, keys in list(dups.items())[:10]:
            print(f"      {aid}: {', '.join(keys)}")

    print(f"\n  CSV : {csv_path}")
    print(f"  JSON: {json_path}")
    print(f"  HTML: {html_path}  <- open this to review & pick what to write")
    print("  (dry-run: nothing written to Zotero.)")
    return 0


def cmd_write(args) -> int:
    try:
        with open(args.from_json, encoding="utf-8") as f:
            records = json.load(f)
    except FileNotFoundError:
        print(f"! {args.from_json} not found. Run `resolve` first.", file=sys.stderr)
        return 2

    # Writes MUST use the web API + key (the local API is read-only -> 501).
    if not config.ZOTERO_API_KEY:
        print("! Writing needs a Zotero API key. Put ZOTERO_API_KEY in .env "
              "(the local API at localhost is read-only).", file=sys.stderr)
        return 2
    if not config.ZOTERO_LIBRARY_ID:
        print("! Set ZOTERO_LIBRARY_ID in .env to your Zotero user/library id.", file=sys.stderr)
        return 2
    zot = ZoteroClient(base=config.ZOTERO_WRITE_BASE, api_key=config.ZOTERO_API_KEY)
    if not zot.ping():
        print(f"! Cannot reach Zotero web API at {zot.base} "
              "(check the key and that your library is synced to zotero.org).", file=sys.stderr)
        return 2

    items_filter = [k.strip() for k in args.items.split(",")] if args.items else None
    targets = [r for r in records
               if r.get("target_item_type") and r.get("fields")
               and float(r.get("confidence") or 0) >= args.threshold
               and (not items_filter or r.get("item_key") in items_filter)]
    print(f"{len(targets)} item(s) eligible (confidence >= {args.threshold}"
          + (", filtered to given keys" if items_filter else "") + ").")
    if not args.yes:
        print("DRY-RUN (pass --yes to actually write). Proposed changes:\n")

    written = failed = 0
    for r in targets:
        key = r["item_key"]
        print(f"  {key}  {r['current_item_type']} -> {r['target_item_type']}  [{r.get('canonical')}]")
        for k, v in r["fields"].items():
            print(f"        {k}: {str(v).replace(chr(10), ' / ')}")
        if not args.yes:
            continue
        try:
            # Guard on the RESOLVE-TIME version: if the item changed since `resolve`,
            # Zotero rejects with 412 so we never clobber the user's newer edits.
            zot.apply_changes(key, int(r.get("version") or 0), r["target_item_type"], r["fields"])
            written += 1
        except Exception as e:  # noqa: BLE001
            msg = ("item changed since `resolve` — re-run `resolve` first"
                   if "412" in str(e) else str(e))
            print(f"        ! write failed: {msg}")
            failed += 1

    if args.yes:
        print(f"\nWROTE {written} item(s); {failed} failed.")
    else:
        print(f"\nWOULD WRITE {len(targets)} item(s). Re-run with --yes (optionally --items KEY,KEY) to apply.")
    return 0


def cmd_web(args) -> int:
    from .web import main as web_main  # lazy: the web UI deps are optional
    web_main(host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="arxiv-marker",
                                description="Resolve arXiv preprints to venue + tier + citations.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("resolve", help="dry-run: resolve venues and write a CSV/JSON report")
    r.add_argument("--limit", type=int, default=None, help="max candidates (ignored with --items)")
    r.add_argument("--items", type=str, default=None, help="comma-separated item keys to target")
    r.add_argument("--out", dest="out_path", type=lambda s: __import__("pathlib").Path(s),
                   default=None, help="output dir (default ./out)")
    r.set_defaults(func=cmd_resolve)

    w = sub.add_parser("write", help="apply venue fields + itemType changes to Zotero")
    w.add_argument("--from-json", default=str(config.OUT_DIR / "resolutions.json"))
    w.add_argument("--threshold", type=float, default=0.85)
    w.add_argument("--items", type=str, default=None, help="comma-separated item keys (from the HTML console)")
    w.add_argument("--yes", action="store_true", help="actually write (otherwise dry-run)")
    w.set_defaults(func=cmd_write)

    wb = sub.add_parser("web", help="launch the local web UI (needs the 'web' extra)")
    wb.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    wb.add_argument("--port", type=int, default=8000, help="bind port (default 8000)")
    wb.set_defaults(func=cmd_web)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
