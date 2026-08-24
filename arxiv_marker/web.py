"""Optional local web UI (needs the `web` extra: fastapi + uvicorn).

A thin browser front-end over the same resolve/write pipeline: review the proposed
changes in a table, then write the ones you tick — without leaving the browser.

Safety: binds to 127.0.0.1 only; the /api/write endpoint requires ZOTERO_API_KEY,
an explicit click + confirm, and only writes rows at/above the confidence bar.
Launch with `python run.py web` (or `uv run python run.py web`).
"""
# NB: no `from __future__ import annotations` here — FastAPI must see the real
# (non-stringized) Pydantic model annotation on the closure-local request body,
# otherwise it can't resolve it and treats the body as a query param.
import json

from . import config
from .pipeline import resolve_items
from .report import write_reports
from .resolvers import DBLP, OpenReview, SemanticScholar
from .zotero_api import ZoteroClient


def _load_records() -> list[dict]:
    p = config.OUT_DIR / "resolutions.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return []


def _save_records(records: list[dict]) -> None:
    """Persist the cache back — same shape/format as report.write_reports' JSON, so a
    later browser refresh (which re-reads this file) sees the applied changes."""
    p = config.OUT_DIR / "resolutions.json"
    p.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def create_app():
    """Build the FastAPI app. Imports fastapi lazily so the core CLI needs no web deps."""
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import HTMLResponse
        from pydantic import BaseModel
    except ModuleNotFoundError as e:  # pragma: no cover - exercised via the CLI message
        raise SystemExit(
            "The web UI needs the 'web' extra. Install it with:  uv sync --extra web"
        ) from e

    app = FastAPI(title="arxiv-marker", docs_url=None, redoc_url=None)

    class WriteReq(BaseModel):
        keys: list[str]
        threshold: float = 0.85

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _PAGE

    @app.get("/api/resolutions")
    def api_resolutions() -> list[dict]:
        """The most recent dry-run results (from out/resolutions.json)."""
        return _load_records()

    @app.post("/api/resolve")
    def api_resolve() -> list[dict]:
        """Re-run the read-only resolve cascade against the live Zotero library."""
        from .cli import _gather  # local import avoids any import-order coupling

        zot = ZoteroClient()
        if not zot.ping():
            raise HTTPException(503, "Zotero desktop not reachable at localhost:23119.")
        items = _gather(zot, None, None)
        try:  # collection labels are optional — never fail the resolve over them
            cmap = zot.get_collections()
        except Exception:  # noqa: BLE001
            cmap = {}
        results = resolve_items(items, SemanticScholar(), DBLP(), collections_map=cmap,
                                openreview=OpenReview())
        write_reports(results)
        return [r.to_dict() for r in results]

    @app.post("/api/write")
    def api_write(req: WriteReq) -> dict:
        """Apply the proposed field changes for the picked keys (the only mutating call)."""
        if not config.ZOTERO_API_KEY:
            raise HTTPException(400, "Writing needs ZOTERO_API_KEY in .env (the local API is read-only).")
        cache = _load_records()
        records = {r["item_key"]: r for r in cache}
        zot = ZoteroClient(base=config.ZOTERO_WRITE_BASE, api_key=config.ZOTERO_API_KEY)
        results = []
        wrote = False
        for key in req.keys:
            r = records.get(key)
            if not r or not r.get("target_item_type") or not r.get("fields"):
                results.append({"key": key, "ok": False, "error": "no proposal / not eligible"})
                continue
            if float(r.get("confidence") or 0) < req.threshold:
                results.append({"key": key, "ok": False, "error": "below confidence threshold"})
                continue
            try:
                # guard on the resolve-time version (see the CLI note); 412 if it changed
                resp = zot.apply_changes(key, int(r.get("version") or 0), r["target_item_type"], r["fields"])
                # Mark the cached row as applied so a browser refresh (which re-reads this
                # file) doesn't resurrect it as an unwritten preprint. The item now IS the
                # target type: clear the proposal and advance to the new server version
                # (Zotero returns it in Last-Modified-Version on a successful PATCH).
                new_v = int((getattr(resp, "headers", {}) or {}).get("Last-Modified-Version") or 0)
                r["current_item_type"] = r["target_item_type"]
                r["version"] = new_v or int(r.get("version") or 0)
                r["target_item_type"] = None
                r["fields"] = {}
                wrote = True
                results.append({"key": key, "ok": True, "venue": r.get("canonical")})
            except Exception as e:  # noqa: BLE001 - report per-item, never abort the batch
                msg = "item changed since resolve — re-resolve" if "412" in str(e) else str(e)
                results.append({"key": key, "ok": False, "error": msg})
        if wrote:  # persist so the applied changes survive a page refresh
            _save_records(cache)
        return {"results": results}

    return app


def main(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    print(f"arxiv-marker web UI → http://{host}:{port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


# --------------------------------------------------------------------------- front-end
# "Highlighter Desk": the tool is named *marker*, so the signature motif is a
# highlighter swipe. Dark warm-ink lab theme; the Write button IS the highlighter
# (chartreuse fill); rows that will be written look literally "marked".

_FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    'family=Fraunces:ital,opsz,wght@0,9..144,500;0,9..144,600;1,9..144,500&'
    'family=Hanken+Grotesk:wght@400;500;600;700&'
    'family=IBM+Plex+Mono:wght@400;500;600&display=swap">'
)

_CSS_WEB = r"""
:root{
  --bg:#0b0d0c;--bg2:#0f1211;--panel:#131715;--panel2:#181d1a;
  --line:#242b27;--line2:#323a35;
  --fg:#eef1ea;--fg2:#c2c9bf;--mut:#7b847b;
  --mark:#c9f24a;--mark-dim:#aad23c;--mark-soft:rgba(201,242,74,.13);
  --violet:#b896ff;--blue:#76b6ff;--warn:#e9bb4f;--danger:#ff6f66;
  --mono:"IBM Plex Mono",ui-monospace,"SFMono-Regular",Consolas,monospace;
  --sans:"Hanken Grotesk",ui-sans-serif,system-ui,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
  --serif:"Fraunces",Georgia,"Times New Roman",serif;
  --hh:152px;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 var(--sans);letter-spacing:.005em;min-height:100vh;
  background-image:radial-gradient(1100px 460px at 12% -8%,rgba(201,242,74,.06),transparent 60%),
    radial-gradient(900px 520px at 100% 0%,rgba(118,182,255,.045),transparent 55%);background-attachment:fixed}
body::before{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.04;
  background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='100' height='100'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}
a{color:var(--fg);text-decoration:none}
code{font-family:var(--mono);font-size:.84em;background:var(--panel2);border:1px solid var(--line);border-radius:5px;padding:1px 5px;color:var(--fg2)}

/* ---- header ---- */
header{position:sticky;top:0;z-index:20;padding:18px 26px 14px;border-bottom:1px solid var(--line);
  backdrop-filter:blur(12px);background:linear-gradient(180deg,rgba(11,13,12,.94),rgba(11,13,12,.80))}
.brand-row{display:flex;align-items:baseline;gap:18px;flex-wrap:wrap}
.brand{font-family:var(--serif);font-weight:600;font-size:28px;line-height:1;letter-spacing:-.012em;display:inline-flex;align-items:baseline}
.brand .dot{color:var(--mark);padding:0 .05em}
.brand .m{position:relative;z-index:0}
.brand .m .swipe{position:absolute;left:-.05em;right:-.09em;bottom:.06em;height:.4em;z-index:-1;border-radius:2px;
  background:var(--mark);opacity:.85;transform:skewX(-12deg);transform-origin:left;animation:swipe .6s .15s both cubic-bezier(.2,.85,.2,1)}
@keyframes swipe{from{transform:skewX(-12deg) scaleX(0)}to{transform:skewX(-12deg) scaleX(1)}}
.tagline{color:var(--mut);font-size:13px}.tagline b{color:var(--fg2);font-weight:600}

/* ---- stat chips (double as filters) ---- */
.stats{display:flex;gap:10px;margin:16px 0 2px;flex-wrap:wrap}
.chip{appearance:none;cursor:pointer;display:flex;flex-direction:column;gap:2px;align-items:flex-start;color:var(--fg);
  background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:8px 15px;min-width:76px;
  transition:transform .14s,border-color .14s,background .14s}
.chip:hover{transform:translateY(-1px);border-color:var(--line2)}
.chip .chip-n{font-family:var(--serif);font-weight:600;font-size:21px;line-height:1}
.chip .chip-l{font:600 10.5px var(--mono);letter-spacing:.09em;text-transform:uppercase;color:var(--mut)}
.chip.on{border-color:var(--fg2)}.chip.on .chip-l{color:var(--fg2)}
.chip-mark .chip-n{color:var(--mark)}
.chip-mark.on{border-color:var(--mark);background:var(--mark-soft)}

/* ---- toolbar ---- */
.toolbar{display:flex;gap:12px;align-items:center;margin-top:14px;flex-wrap:wrap}
.search{position:relative;flex:1;min-width:210px;max-width:430px}
.search input{width:100%;background:var(--panel);border:1px solid var(--line);color:var(--fg);border-radius:10px;padding:9px 34px 9px 33px;font:14px var(--sans)}
.search input::-webkit-search-cancel-button{filter:grayscale(1) opacity(.5)}
.search input:focus{outline:none;border-color:var(--mark);box-shadow:0 0 0 3px var(--mark-soft)}
.search .mag{position:absolute;left:11px;top:50%;transform:translateY(-50%);color:var(--mut);font-size:15px}
.search kbd{position:absolute;right:9px;top:50%;transform:translateY(-50%);font:11px var(--mono);color:var(--mut);border:1px solid var(--line);border-radius:4px;padding:1px 5px}
.thr{display:flex;align-items:center;gap:9px;color:var(--mut);font-size:12.5px;white-space:nowrap}
.thr b{font-family:var(--mono);color:var(--mark);font-size:13px;min-width:34px;text-align:right}
input[type=range]{accent-color:var(--mark);width:120px;cursor:pointer}
.coll{max-width:280px;background:var(--panel);border:1px solid var(--line);color:var(--fg);border-radius:10px;padding:9px 30px 9px 12px;font:13px var(--sans);cursor:pointer;
  appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' fill='none' stroke='%237b847b' stroke-width='1.6'%3E%3Cpath d='M1 1l4 4 4-4'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 12px center}
.coll:focus{outline:none;border-color:var(--mark);box-shadow:0 0 0 3px var(--mark-soft)}
.coll option{background:var(--panel);color:var(--fg)}
.spacer{flex:1 1 auto}
.lnk{background:none;border:0;color:var(--mut);cursor:pointer;font:13px var(--sans);text-decoration:underline;text-underline-offset:3px;text-decoration-color:var(--line2)}
.lnk:hover{color:var(--fg2)}
.btn{cursor:pointer;font:600 13.5px var(--sans);border-radius:10px;padding:9px 16px;border:1px solid var(--line2);background:var(--panel);color:var(--fg);transition:border-color .14s,background .14s,transform .1s}
.btn:hover:not(:disabled){border-color:var(--fg2)}
.btn:active:not(:disabled){transform:translateY(1px)}
.btn:disabled{opacity:.42;cursor:default}
.btn.mark{background:var(--mark);color:#0b0d0c;border-color:var(--mark);box-shadow:0 8px 22px -10px rgba(201,242,74,.55)}
.btn.mark:hover:not(:disabled){background:#d7ff5e;border-color:#d7ff5e}
.btn.busy{position:relative;color:transparent!important}
.btn.busy::after{content:"";position:absolute;inset:0;margin:auto;width:15px;height:15px;border:2px solid currentColor;border-top-color:transparent;border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
#status{font-size:12.5px;color:var(--mut);max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#status.err{color:var(--danger)}

/* ---- table ---- */
.wrap{padding:6px 16px 48px;position:relative;z-index:1;animation:fade .45s ease both}
@keyframes fade{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
table{width:100%;border-collapse:separate;border-spacing:0}
thead th{position:sticky;top:var(--hh);z-index:6;background:var(--bg);font:600 10.5px var(--mono);letter-spacing:.08em;
  text-transform:uppercase;color:var(--mut);text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);white-space:nowrap;user-select:none}
th[data-col]{cursor:pointer}th[data-col]:hover{color:var(--fg2)}
th.asc::after{content:" ↑";color:var(--mark)}th.desc::after{content:" ↓";color:var(--mark)}
td{padding:11px 12px;border-bottom:1px solid var(--line);vertical-align:top}
tr.row{transition:background .12s}
tr.row:hover td{background:rgba(255,255,255,.018)}
tr.row.will{box-shadow:inset 2px 0 0 rgba(201,242,74,.30)}
tr.row.dim td{opacity:.5}
tr.row.sel{box-shadow:inset 3px 0 0 var(--mark)}
tr.row.sel td{background:linear-gradient(90deg,var(--mark-soft),transparent 46%)}
.mut{color:var(--mut)}
.c-check{width:36px}.c-check input{width:16px;height:16px;accent-color:var(--mark);cursor:pointer;margin-top:2px}
.c-check input:disabled{cursor:default;opacity:.4}
.c-title{max-width:330px}
.title{font-weight:600;line-height:1.34;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.sub{font:11.5px var(--mono);color:var(--mut);margin-top:3px;display:flex;flex-wrap:wrap;gap:4px 10px;align-items:center}
.ax{color:var(--mut)}.ax:hover{color:var(--mark)}
.cfold{color:var(--fg2);max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cfold::before{content:"▸ ";color:var(--mark-dim)}
.c-chg{font-size:12px}
.chg{display:inline-flex;align-items:center;gap:6px;font:12px var(--mono);white-space:nowrap}
.chg .from{color:var(--mut)}.chg .ar{color:var(--mark)}
.chg .to{color:var(--fg);background:var(--mark-soft);border:1px solid rgba(201,242,74,.26);border-radius:5px;padding:1px 6px}
.c-venue{max-width:200px}.ven{font-weight:500}
.tier{font:700 12px var(--mono);border-radius:6px;padding:2px 8px;display:inline-block}
.t-As{color:var(--violet);background:rgba(184,150,255,.12);border:1px solid rgba(184,150,255,.32)}
.t-A{color:var(--blue);background:rgba(118,182,255,.10);border:1px solid rgba(118,182,255,.28)}
.t-B,.t-C{color:var(--fg2);background:var(--panel2);border:1px solid var(--line)}
.c-cites{font-family:var(--mono);white-space:nowrap}
.cn{font-size:13.5px}.ci{display:block;font-size:10.5px;color:var(--mut);margin-top:2px}
.c-conf{width:98px}
.meter{height:5px;border-radius:3px;background:var(--panel2);overflow:hidden;margin-bottom:4px}
.meter span{display:block;height:100%;border-radius:3px}
.meter.ok span{background:var(--mark)}.meter.warn span{background:var(--warn)}.meter.zero span{background:var(--line2)}
.cval{font:600 12px var(--mono)}.cval.ok{color:var(--mark)}.cval.warn{color:var(--warn)}.cval.zero{color:var(--mut)}
.c-fields{max-width:430px;cursor:pointer}
.fwrap{display:flex;flex-wrap:wrap;gap:4px}
.fchip{font:11px var(--mono);background:var(--panel2);border:1px solid var(--line);border-radius:5px;padding:1px 6px;max-width:182px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--mut)}
.fchip b{color:var(--fg2);font-weight:600;margin-right:5px}
.exp{margin-top:7px;background:none;border:1px dashed var(--line2);color:var(--mut);border-radius:6px;padding:2px 9px;cursor:pointer;font:11px var(--mono)}
.exp:hover{color:var(--mark);border-color:var(--mark)}
.c-stat{width:96px}
.stat{font:600 11.5px var(--sans);white-space:nowrap}.st-ok{color:var(--mark)}.st-err{color:var(--danger)}

/* ---- detail drawer ---- */
tr.detail td{background:var(--bg2)}
.drawer{padding:6px 2px 14px}
.dsec{min-width:0}
.dsec h5{margin:0 0 8px;font:600 10.5px var(--mono);letter-spacing:.09em;text-transform:uppercase;color:var(--mut)}
.kvs{display:grid;grid-template-columns:max-content 1fr;gap:5px 18px;font:12px var(--mono)}
.kvs .k{color:var(--mark-dim)}.kvs .v{color:var(--fg2);word-break:break-word}
.kvs .v .ln{display:block}
.drow{display:flex;gap:40px;flex-wrap:wrap;margin-top:16px}
.src{font:11px var(--mono);background:var(--panel2);border:1px solid var(--line);border-radius:5px;padding:1px 8px;margin-right:6px;color:var(--fg2)}
.evs a{color:var(--blue);margin-right:14px;font-size:12.5px}.evs a:hover{color:var(--mark)}
.tg{font:11px var(--mono);border-radius:5px;padding:1px 8px;margin:0 6px 6px 0;display:inline-block}
.tg.add{color:var(--mark);background:var(--mark-soft);border:1px solid rgba(201,242,74,.26)}
.tg.have{color:var(--mut);background:var(--panel2);border:1px solid var(--line)}

/* ---- empty state ---- */
.empty{padding:64px 20px;text-align:center}
.empty-mark{width:64px;height:14px;background:var(--mark);opacity:.85;transform:skewX(-12deg);border-radius:3px;margin:0 auto 22px}
.empty-t{font-family:var(--serif);font-size:21px;margin-bottom:8px}
.empty-s{color:var(--mut);font-size:13.5px}

footer{position:relative;z-index:1;padding:18px 26px 42px;color:var(--mut);font-size:12px;border-top:1px solid var(--line);max-width:1000px}
footer b{color:var(--fg2)}

@media (max-width:860px){.c-cites,.c-fields{display:none}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

_JS = r"""
const $=(s,r=document)=>r.querySelector(s);
const $$=(s,r=document)=>[...r.querySelectorAll(s)];
let RECS=[],THRESH=0.85,FILTER='all',QUERY='',COLL='',SORT={col:'_default',dir:-1};
const SEL=new Set(),OPEN=new Set();

const fmtN=n=>n==null?'—':(''+n).replace(/\B(?=(\d{3})+(?!\d))/g,',');
const esc=s=>{const d=document.createElement('div');d.textContent=(s==null?'':s);return d.innerHTML;};
const proposable=r=>!!(r.target_item_type&&r.fields&&Object.keys(r.fields).length);
const eligible=r=>proposable(r)&&r.acceptance==='accepted'&&(r.confidence||0)>=THRESH;

function setStatus(t,err){const s=$('#status');s.textContent=t||'';s.classList.toggle('err',!!err);}
function setBusy(id,on){const b=$('#'+id);b.disabled=on;b.classList.toggle('busy',on);}
function syncHead(){document.documentElement.style.setProperty('--hh',$('header').offsetHeight+'px');}

function tierBadge(t){return t?`<span class="tier t-${t==='A*'?'As':esc(t)}">${esc(t)}</span>`:'<span class="mut">—</span>';}
function confMeter(c){const p=Math.round(c*100),k=c>=THRESH?'ok':(c>0?'warn':'zero');
  return `<div class="meter ${k}"><span style="width:${p}%"></span></div><span class="cval ${k}">${c.toFixed(2)}</span>`;}
function fieldChips(r){const f=r.fields||{},ks=Object.keys(f);if(!ks.length)return '<span class="mut">—</span>';
  return ks.map(k=>{const v=(''+f[k]).replace(/\n/g,' · ');return `<span class="fchip" title="${esc(k+' = '+f[k])}"><b>${esc(k)}</b>${esc(v)}</span>`;}).join('');}
function evLinks(r){return (r.evidence||[]).map(e=>{const m=e.match(/https?:\/\/\S+/);const src=esc((e.split(':')[0]||'src').trim());
  return m?`<a href="${esc(m[0].replace(/[)\s]+$/,''))}" target="_blank" rel="noopener">${src} ↗</a>`:`<span class="mut">${esc(e)}</span>`;}).join('')||'<span class="mut">—</span>';}

function matchFilter(r){return FILTER==='accepted'?r.acceptance==='accepted'
  :FILTER==='unknown'?r.acceptance!=='accepted'
  :FILTER==='astar'?r.core_tier==='A*'
  :FILTER==='will'?eligible(r):true;}
function matchQuery(r){return !QUERY||[r.title,r.canonical,r.venue_raw,r.arxiv_id].join(' ').toLowerCase().includes(QUERY);}
function matchColl(r){return !COLL||(r.collections||[]).includes(COLL);}
function sortVal(r,c){return c==='title'?(r.title||'').toLowerCase()
  :c==='change'?(r.target_item_type?1:0)
  :c==='venue'?(r.canonical||'').toLowerCase()
  :c==='core'?({'A*':4,A:3,B:2,C:1}[r.core_tier]||0)
  :c==='cites'?(r.citation_count||0)
  :c==='conf'?(r.confidence||0):0;}
function ordered(list){
  if(SORT.col==='_default')return list.slice().sort((a,b)=>{
    const ea=eligible(a)?0:1,eb=eligible(b)?0:1;if(ea!==eb)return ea-eb;
    if((b.confidence||0)!==(a.confidence||0))return (b.confidence||0)-(a.confidence||0);
    return (b.citation_count||0)-(a.citation_count||0);});
  const {col,dir}=SORT;return list.slice().sort((a,b)=>{const x=sortVal(a,col),y=sortVal(b,col);
    return typeof x==='number'?(x-y)*dir:(''+x).localeCompare(''+y)*dir;});
}

function renderStats(){
  const base=RECS.filter(matchColl);  // counts reflect the chosen collection scope
  const acc=base.filter(r=>r.acceptance==='accepted').length,astar=base.filter(r=>r.core_tier==='A*').length,will=base.filter(eligible).length;
  const chips=[['all','all',base.length],['accepted','resolved',acc],['unknown','unknown',base.length-acc],['astar','core a*',astar],['will','will write',will]];
  $('#stats').innerHTML=chips.map(([f,l,n])=>`<button class="chip ${FILTER===f?'on':''} ${f==='will'?'chip-mark':''}" data-f="${f}"><span class="chip-n">${n}</span><span class="chip-l">${l}</span></button>`).join('');
  $$('#stats .chip').forEach(c=>c.onclick=()=>{FILTER=c.dataset.f;renderStats();renderRows();});
  syncHead();
}

function renderColls(){
  const counts={};RECS.forEach(r=>(r.collections||[]).forEach(c=>counts[c]=(counts[c]||0)+1));
  const names=Object.keys(counts).sort((a,b)=>a.localeCompare(b));
  const sel=$('#coll');sel.style.display=names.length?'':'none';sel.innerHTML='';
  const mk=(v,label)=>{const o=document.createElement('option');o.value=v;o.textContent=label;return o;};
  sel.appendChild(mk('',`All collections (${RECS.length})`));
  names.forEach(n=>sel.appendChild(mk(n,`${n} (${counts[n]})`)));
  if(![...sel.options].some(o=>o.value===COLL))COLL='';
  sel.value=COLL;
}

function detailRow(r){
  const tr=document.createElement('tr');tr.className='detail';const f=r.fields||{};
  const kvs=Object.keys(f).map(k=>{const v=''+f[k];const val=k==='extra'?v.split('\n').map(l=>`<span class="ln">${esc(l)}</span>`).join(''):esc(v);
    return `<span class="k">${esc(k)}</span><span class="v">${val}</span>`;}).join('')||'<span class="mut">—</span>';
  const srcs=(r.sources||[]).map(s=>`<span class="src">${esc(s)}</span>`).join('')||'<span class="mut">—</span>';
  const tags=(r.suggested_tags||[]).map(t=>{const have=(r.existing_tags||[]).includes(t);return `<span class="tg ${have?'have':'add'}">${have?'':'+ '}${esc(t)}</span>`;}).join('')||'<span class="mut">—</span>';
  const colls=(r.collections||[]).map(c=>`<span class="src">${esc(c)}</span>`).join('')||'<span class="mut">—</span>';
  tr.innerHTML=`<td></td><td colspan="8"><div class="drawer">
    <div class="dsec"><h5>Fields to write</h5><div class="kvs">${kvs}</div></div>
    <div class="drow"><div class="dsec"><h5>Zotero collections</h5>${colls}</div>
      <div class="dsec"><h5>Sources</h5>${srcs}</div>
      <div class="dsec"><h5>Evidence</h5><div class="evs">${evLinks(r)}</div></div>
      <div class="dsec"><h5>Tags to add</h5>${tags}</div></div></div></td>`;
  return tr;
}

function rowEl(r){
  const tr=document.createElement('tr');
  tr.className='row'+(eligible(r)?' will':'')+(r.acceptance!=='accepted'?' dim':'')+(SEL.has(r.item_key)?' sel':'');
  tr.dataset.key=r.item_key;
  const prop=proposable(r);
  const chg=r.target_item_type?`<span class="chg"><span class="from">${esc(r.current_item_type)}</span><span class="ar">→</span><span class="to">${esc(r.target_item_type)}</span></span>`:'<span class="mut">no change</span>';
  const cites=r.citation_count==null?'<span class="mut">—</span>':`<span class="cn">${fmtN(r.citation_count)}</span>${r.influential_citations?`<span class="ci">${fmtN(r.influential_citations)} infl</span>`:''}`;
  const ax=r.arxiv_id?`<a class="ax" href="https://arxiv.org/abs/${esc(r.arxiv_id)}" target="_blank" rel="noopener">${esc(r.arxiv_id)} ↗</a>`:'';
  const cl=r.collections&&r.collections.length?`<span class="cfold" title="${esc(r.collections.join(' · '))}">${esc(r.collections[0])}${r.collections.length>1?` +${r.collections.length-1}`:''}</span>`:'';
  const sub=[r.year,r.kind].filter(Boolean).join(' · ');
  tr.innerHTML=`<td class="c-check"><input type="checkbox" ${SEL.has(r.item_key)?'checked':''} ${prop?'':'disabled'} aria-label="select row"></td>`
    +`<td class="c-title"><div class="title" title="${esc(r.title)}">${esc(r.title)}</div><div class="sub">${ax}${cl}</div></td>`
    +`<td class="c-chg">${chg}</td>`
    +`<td class="c-venue"><div class="ven">${esc(r.canonical||'—')}</div><div class="sub mut">${esc(sub)}</div></td>`
    +`<td class="c-core">${tierBadge(r.core_tier)}</td>`
    +`<td class="c-cites">${cites}</td>`
    +`<td class="c-conf">${confMeter(r.confidence||0)}</td>`
    +`<td class="c-fields"><div class="fwrap">${fieldChips(r)}</div>${prop?`<button class="exp" aria-label="toggle details">${OPEN.has(r.item_key)?'– less':'+ more'}</button>`:''}</td>`
    +`<td class="c-stat"><span class="stat"></span></td>`;
  const cb=tr.querySelector('input');
  if(cb)cb.onchange=()=>{cb.checked?SEL.add(r.item_key):SEL.delete(r.item_key);tr.classList.toggle('sel',cb.checked);updateBar();};
  if(prop)tr.querySelector('.c-fields').onclick=e=>{if(e.target.closest('a'))return;OPEN.has(r.item_key)?OPEN.delete(r.item_key):OPEN.add(r.item_key);renderRows();};
  return tr;
}

function emptyState(msg){return `<tr><td colspan="9"><div class="empty"><div class="empty-mark"></div><div class="empty-t">${esc(msg)}</div><div class="empty-s">Run <code>python run.py</code> to resolve your library, or click <b>Re-resolve from Zotero</b>.</div></div></td></tr>`;}

function renderRows(){
  const tb=$('#tbody');tb.innerHTML='';
  if(!RECS.length){tb.innerHTML=emptyState('no resolutions loaded yet');updateBar();return;}
  const list=ordered(RECS.filter(r=>matchFilter(r)&&matchQuery(r)&&matchColl(r)));
  if(!list.length){tb.innerHTML=emptyState('nothing matches this filter');updateBar();return;}
  for(const r of list){tb.appendChild(rowEl(r));if(OPEN.has(r.item_key))tb.appendChild(detailRow(r));}
  updateBar();
}

function visEligible(){return RECS.filter(r=>matchFilter(r)&&matchQuery(r)&&matchColl(r)&&eligible(r));}
function updateBar(){
  const n=SEL.size;$('#wn').textContent=n?` (${n})`:'';$('#write').disabled=!n;
  const list=visEligible(),all=list.length>0&&list.every(r=>SEL.has(r.item_key));
  $('#selall').textContent=all?'clear selection':'select all';
}

function initSel(){SEL.clear();OPEN.clear();RECS.filter(eligible).forEach(r=>SEL.add(r.item_key));}
async function load(){try{RECS=await (await fetch('/api/resolutions')).json();initSel();renderColls();renderStats();renderRows();
  setStatus(RECS.length?`${RECS.length} items · dry-run until you Write`:'');}catch(e){setStatus('load failed: '+e.message,true);}}

$('#q').oninput=e=>{QUERY=e.target.value.trim().toLowerCase();renderRows();};
$('#coll').onchange=e=>{COLL=e.target.value;renderStats();renderRows();};
$('#thr').oninput=e=>{THRESH=+e.target.value;$('#thrv').textContent=THRESH.toFixed(2);
  SEL.clear();RECS.filter(eligible).forEach(r=>SEL.add(r.item_key));renderStats();renderRows();};
$('#selall').onclick=()=>{const list=visEligible(),all=list.length>0&&list.every(r=>SEL.has(r.item_key));
  list.forEach(r=>all?SEL.delete(r.item_key):SEL.add(r.item_key));renderRows();};
$$('th[data-col]').forEach(th=>th.onclick=()=>{const c=th.dataset.col;
  if(SORT.col===c)SORT.dir*=-1;else SORT={col:c,dir:(c==='title'||c==='venue')?1:-1};
  $$('th[data-col]').forEach(h=>h.classList.remove('asc','desc'));th.classList.add(SORT.dir>0?'asc':'desc');renderRows();});

$('#resolve').onclick=async()=>{setBusy('resolve',true);setStatus('resolving… querying Semantic Scholar / DBLP');
  try{const r=await fetch('/api/resolve',{method:'POST'});const d=await r.json();if(!r.ok)throw new Error(d.detail||r.status);
    RECS=d;initSel();renderColls();renderStats();renderRows();setStatus(`resolved ${RECS.length} items`);}
  catch(e){setStatus('resolve failed: '+e.message,true);}finally{setBusy('resolve',false);}};

$('#write').onclick=async()=>{const keys=[...SEL];if(!keys.length){setStatus('nothing selected');return;}
  if(!confirm(`Write venue metadata to ${keys.length} item(s) in Zotero?\nThis changes their itemType + fields (confidence ≥ ${THRESH.toFixed(2)}).`))return;
  setBusy('write',true);setStatus(`writing ${keys.length}…`);
  try{const r=await fetch('/api/write',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({keys,threshold:THRESH})});
    const d=await r.json();if(!r.ok)throw new Error(d.detail||r.status);let ok=0;
    for(const res of d.results){const tr=$(`#tbody tr.row[data-key="${res.key}"]`);
      if(tr){const c=tr.querySelector('.stat');c.textContent=res.ok?'✓ written':('✗ '+res.error);c.className='stat '+(res.ok?'st-ok':'st-err');}
      if(res.ok){ok++;SEL.delete(res.key);if(tr){const cb=tr.querySelector('input');if(cb){cb.checked=false;cb.disabled=true;}tr.classList.remove('sel','will');}}}
    updateBar();setStatus(`wrote ${ok}/${d.results.length}`,ok<d.results.length);}
  catch(e){setStatus('write failed: '+e.message,true);}finally{setBusy('write',false);}};

addEventListener('keydown',e=>{if(e.key==='/'&&document.activeElement!==$('#q')){e.preventDefault();$('#q').focus();}});
addEventListener('resize',syncHead);
syncHead();load();
"""

_PAGE = ("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>arxiv-marker</title>
__FONTS__<style>__CSS__</style></head><body>
<header>
  <div class="brand-row">
    <div class="brand" aria-label="arxiv-marker">arxiv<span class="dot">·</span><span class="m">marker<span class="swipe"></span></span></div>
    <div class="tagline">arXiv → venue · CORE tier · citations — <b>review, then mark your library</b></div>
  </div>
  <div class="stats" id="stats"></div>
  <div class="toolbar">
    <div class="search"><span class="mag">⌕</span>
      <input id="q" type="search" placeholder="search title, venue, arXiv id…" autocomplete="off"><kbd>/</kbd></div>
    <select id="coll" class="coll" aria-label="filter by Zotero collection"><option value="">All collections</option></select>
    <div class="thr">confidence ≥ <input id="thr" type="range" min="0.5" max="1" step="0.05" value="0.85"><b id="thrv">0.85</b></div>
    <span class="spacer"></span>
    <button class="lnk" id="selall">select all</button>
    <button class="btn" id="resolve">Re-resolve from Zotero</button>
    <button class="btn mark" id="write" disabled>Write selected<span id="wn"></span> →</button>
    <span id="status"></span>
  </div>
</header>
<div class="wrap"><table>
<thead><tr>
  <th class="c-check"></th>
  <th data-col="title">Title / arXiv</th>
  <th data-col="change">Change</th>
  <th data-col="venue">Venue</th>
  <th data-col="core">CORE</th>
  <th data-col="cites">Cites</th>
  <th data-col="conf">Conf.</th>
  <th>Fields to write</th>
  <th class="c-stat">Status</th>
</tr></thead>
<tbody id="tbody"></tbody>
</table></div>
<footer>Reads the local Zotero API (read-only); writes go through the Zotero Web API and need
<code>ZOTERO_API_KEY</code>. Only selected rows at/above the confidence bar are written — itemType + venue fields.
Bound to <b>127.0.0.1</b>. Dry-run until you click <b>Write</b>.</footer>
<script>__JS__</script></body></html>"""
         .replace("__FONTS__", _FONTS).replace("__CSS__", _CSS_WEB).replace("__JS__", _JS))
