// arxiv-marker — Zotero glue. Runs inside Zotero (loaded via bootstrap loadSubScript).
// Uses ZMResolver (the ported resolver, shares this scope) + Zotero APIs to: read the
// selected items, resolve venues against live S2/OpenReview/DBLP, preview in a review dialog, and
// write the venue field back DIRECTLY to the local library (no Web API, no API key).

if (typeof Zotero === "undefined") {
  // resolver.js / zm-data.js may be loaded in Node for tests; this file is Zotero-only.
  throw new Error("arxiv-marker.js requires the Zotero environment");
}

const ZH = (Zotero.locale || "").toLowerCase().startsWith("zh");
const T = {
  menu: ZH ? "用 arxiv-marker 解析会议/期刊" : "Resolve venue with arxiv-marker",
  collMenu: ZH ? "用 arxiv-marker 解析此分类" : "Resolve this collection with arxiv-marker",
  noItems: ZH
    ? "没找到可处理的条目。选中条目、或右键左侧的某个分类。"
    : "No items to process. Select items, or right-click a collection.",
  resolving: ZH ? "arxiv-marker:解析中…" : "arxiv-marker: resolving…",
  resolvingN: (n) => (ZH ? `arxiv-marker:正在解析 ${n} 个条目…` : `arxiv-marker: resolving ${n} item(s)…`),
  failed: ZH ? "解析失败(看 Zotero 调试输出)。" : "Resolve failed (see Zotero debug output).",
  wroteN: (n) =>
    ZH
      ? `arxiv-marker:已写入 ${n} 个条目(可用「工具→撤销上次 arxiv-marker 写入」还原)。`
      : `arxiv-marker: wrote ${n} item(s). Undo via Tools menu.`,
  wroteNone: ZH ? "arxiv-marker:未写入任何条目。" : "arxiv-marker: nothing written.",
  undoMenu: ZH ? "撤销上次 arxiv-marker 写入" : "Undo last arxiv-marker write",
  noUndo: ZH ? "没有可撤销的写入记录。" : "Nothing to undo.",
  undoConfirm: (n, when) =>
    ZH
      ? `把 ${n} 个条目恢复到写入前(${when})的状态?\n这会还原类型和所有字段。`
      : `Restore ${n} item(s) to their pre-write state (${when})?\nThis reverts the item type and all fields.`,
  undoneN: (n) => (ZH ? `arxiv-marker:已恢复 ${n} 个条目。` : `arxiv-marker: restored ${n} item(s).`),
  prefLabel: "arxiv-marker",
};

// ----- item -> resolver data dict ---------------------------------------------------
function collectionPath(collectionID) {
  const parts = [];
  let c = Zotero.Collections.get(collectionID);
  let guard = 0;
  while (c && guard++ < 20) {
    parts.unshift(c.name);
    c = c.parentID ? Zotero.Collections.get(c.parentID) : null;
  }
  return parts.join(" / ");
}

function field(item, name) {
  try {
    return item.getField(name) || "";
  } catch (e) {
    return ""; // field not valid for this item type
  }
}

function itemToData(item) {
  const creators = item.getCreators().map((c) => ({
    creatorType: Zotero.CreatorTypes.getName(c.creatorTypeID),
    firstName: c.firstName || "",
    lastName: c.lastName || "",
  }));
  const tags = (item.getTags() || []).map((t) => ({ tag: t.tag }));
  const collections = (item.getCollections() || []).map((cid) => collectionPath(cid)).filter(Boolean);
  return {
    title: field(item, "title"),
    archiveID: field(item, "archiveID"),
    DOI: field(item, "DOI"),
    url: field(item, "url"),
    extra: field(item, "extra"),
    date: field(item, "date"),
    creators,
    tags,
    collections,
    itemType: Zotero.ItemTypes.getName(item.itemTypeID),
    // existing venue fields — needed so buildProposal's idempotency check can see that an
    // item is ALREADY resolved (else already-converted items get re-proposed every run).
    proceedingsTitle: field(item, "proceedingsTitle"),
    conferenceName: field(item, "conferenceName"),
    publicationTitle: field(item, "publicationTitle"),
    journalAbbreviation: field(item, "journalAbbreviation"),
    ISSN: field(item, "ISSN"),
    publisher: field(item, "publisher"),
  };
}

// ----- HTTP adapter: Zotero.HTTP.request -> {status, data} (never throws on 4xx/5xx) ---
async function zoteroRequest(method, url, opts = {}) {
  try {
    const xhr = await Zotero.HTTP.request(method, url, {
      headers: opts.headers || {},
      body: opts.body,
      responseType: "json",
      successCodes: false, // let the resolver see 429 etc. and apply its own retry/backoff
      timeout: 60000,
    });
    let data = xhr.response;
    if (data == null && xhr.responseText) {
      try {
        data = JSON.parse(xhr.responseText);
      } catch (e) {
        data = null;
      }
    }
    const retryAfter = typeof xhr.getResponseHeader === "function"
      ? xhr.getResponseHeader("Retry-After")
      : null;
    return {
      status: xhr.status,
      data,
      headers: retryAfter == null ? {} : { "retry-after": retryAfter },
    };
  } catch (e) {
    Zotero.debug("arxiv-marker request error: " + e);
    return { status: 0, data: null };
  }
}

// ----- write-back: change item type + set venue fields, save to LOCAL db --------------
async function applyResolution(item, res) {
  if (!res.target_item_type || !res.fields) return false;
  const targetTypeID = Zotero.ItemTypes.getID(res.target_item_type);
  if (targetTypeID && item.itemTypeID !== targetTypeID) {
    item.setType(targetTypeID); // Zotero drops fields not valid for the new type
  }
  for (const [name, value] of Object.entries(res.fields)) {
    if (value == null || value === "") continue;
    if (name === "extra") {
      item.setField("extra", value);
      continue;
    }
    const fieldID = Zotero.ItemFields.getID(name);
    if (fieldID && Zotero.ItemFields.isValidForType(fieldID, item.itemTypeID)) {
      try {
        item.setField(name, value);
      } catch (e) {
        Zotero.debug(`arxiv-marker: could not set ${name}: ${e}`);
      }
    }
  }
  await item.saveTx();
  return true;
}

// ----- undo safety net: snapshot full pre-write state, restore on demand --------------
function undoFilePath() {
  return PathUtils.join(Zotero.DataDirectory.dir, "arxiv-marker-undo.json");
}

// Save the FULL original state (toJSON) of every item about to be written. This is the
// hard backup: even if the undo command fails, the pre-write state is preserved on disk.
async function saveUndoSnapshot(items) {
  if (!items.length) return;
  const snapshot = {
    time: new Date().toISOString(),
    items: items.map((it) => ({ libraryID: it.libraryID, key: it.key, data: it.toJSON() })),
  };
  try {
    await Zotero.File.putContentsAsync(undoFilePath(), JSON.stringify(snapshot));
  } catch (e) {
    Zotero.debug("arxiv-marker: could not save undo snapshot: " + e);
  }
}

async function undoLast(window) {
  let snapshot;
  try {
    snapshot = JSON.parse(await Zotero.File.getContentsAsync(undoFilePath()));
  } catch (e) {
    notify(T.noUndo);
    return;
  }
  if (!snapshot || !snapshot.items || !snapshot.items.length) {
    notify(T.noUndo);
    return;
  }
  const when = snapshot.time ? snapshot.time.replace("T", " ").slice(0, 19) : "?";
  if (!Services.prompt.confirm(window, "arxiv-marker", T.undoConfirm(snapshot.items.length, when))) return;

  let restored = 0;
  for (const rec of snapshot.items) {
    try {
      const item = await Zotero.Items.getByLibraryAndKeyAsync(rec.libraryID, rec.key);
      if (!item) continue;
      const data = Object.assign({}, rec.data);
      delete data.version;
      delete data.dateModified;
      item.fromJSON(data); // restores itemType + every field to the snapshot
      await item.saveTx();
      restored++;
    } catch (e) {
      Zotero.debug("arxiv-marker: undo failed for " + rec.key + ": " + e);
    }
  }
  notify(T.undoneN(restored));
}

// Resolve the target Zotero.Items for a given scope:
//   "selected"   -> the items highlighted in the item list
//   "collection" -> every regular item in the collection selected on the left
//   "auto"       -> selected items if any, else the current collection's items
// So you can right-click a collection (or just hit the Tools menu with a collection open)
// without hand-picking each paper.
function getScopeItems(pane, scope) {
  if (!pane) return [];
  const isRegular = (it) => it.isRegularItem && it.isRegularItem();
  const collItems = () => {
    const c = pane.getSelectedCollection && pane.getSelectedCollection();
    return c ? c.getChildItems(false, false).filter(isRegular) : [];
  };
  if (scope === "collection") return collItems();
  const sel = pane.getSelectedItems().filter(isRegular);
  if (sel.length || scope === "selected") return sel;
  return collItems(); // "auto" fallback
}

// ----- main flow --------------------------------------------------------------------
async function run(window, scope = "auto") {
  const pane = Zotero.getActiveZoteroPane();
  const selected = getScopeItems(pane, scope);
  if (!selected.length) {
    notify(T.noItems);
    return;
  }

  const items = selected.map((it) => ({ key: it.key, version: it.version, data: itemToData(it) }));
  const itemByKey = new Map(selected.map((it) => [it.key, it]));

  const pw = new Zotero.ProgressWindow({ closeOnClick: false });
  pw.changeHeadline("arxiv-marker");
  const line = new pw.ItemProgress(null, T.resolvingN(items.length));
  pw.show();

  let resolutions;
  try {
    const s2ApiKey = (Zotero.Prefs.get("zoteromarker.s2ApiKey") || "").trim();
    resolutions = await ZMResolver.resolveItems(items, {
      request: zoteroRequest,
      s2ApiKey: s2ApiKey || null,
      sleep: (ms) => new Promise((r) => window.setTimeout(r, ms)),
    });
  } catch (e) {
    Zotero.debug("arxiv-marker resolve error: " + e + "\n" + (e && e.stack));
    line.setText(T.failed);
    line.setError();
    pw.startCloseTimer(6000);
    return;
  }
  pw.close();

  const minConfidence = parseFloat(Zotero.Prefs.get("zoteromarker.minConfidence") || "0.8");

  // review dialog (modal): user confirms which items to write
  const io = { resolutions, minConfidence, zh: ZH, accepted: false, selectedKeys: [] };
  window.openDialog(
    "chrome://zoteromarker/content/review.xhtml",
    "arxiv-marker-review",
    "chrome,dialog,centerscreen,resizable,modal",
    io
  );
  if (!io.accepted) return;

  const chosen = new Set(io.selectedKeys);
  const byKey = new Map(resolutions.map((r) => [r.item_key, r]));
  const toWrite = [...chosen]
    .map((k) => ({ key: k, res: byKey.get(k), item: itemByKey.get(k) }))
    .filter((x) => x.res && x.item && x.res.target_item_type);

  // snapshot BEFORE writing so this run can be undone
  await saveUndoSnapshot(toWrite.map((x) => x.item));

  let wrote = 0;
  for (const { key, res, item } of toWrite) {
    try {
      if (await applyResolution(item, res)) wrote++;
    } catch (e) {
      Zotero.debug(`arxiv-marker: write failed for ${key}: ${e}`);
    }
  }

  const done = new Zotero.ProgressWindow();
  done.changeHeadline("arxiv-marker");
  new done.ItemProgress(null, wrote ? T.wroteN(wrote) : T.wroteNone);
  done.show();
  done.startCloseTimer(4000);
}

function notify(text) {
  const pw = new Zotero.ProgressWindow();
  pw.changeHeadline("arxiv-marker");
  new pw.ItemProgress(null, text);
  pw.show();
  pw.startCloseTimer(4000);
}

// ----- menu wiring ------------------------------------------------------------------
const MENU_IDS = [
  "arxiv-marker-itemmenu",
  "arxiv-marker-collectionmenu",
  "arxiv-marker-toolsmenu",
  "arxiv-marker-undomenu",
];

function addToMenu(window, menuID, newID, label, handler) {
  const doc = window.document;
  const menupopup = doc.getElementById(menuID);
  if (!menupopup) return;
  if (doc.getElementById(newID)) return; // already added
  const mi = doc.createXULElement("menuitem");
  mi.id = newID;
  mi.setAttribute("label", label);
  mi.addEventListener("command", handler);
  menupopup.appendChild(mi);
}

function removeFromMenu(window) {
  const doc = window.document;
  for (const id of MENU_IDS) {
    const el = doc.getElementById(id);
    if (el) el.remove();
  }
}

// ----- preferences pane wiring (manual, no auto-binding magic) -----------------------
function onPrefsLoad({ window }) {
  const doc = window.document;
  const setText = (id, v) => {
    const el = doc.getElementById(id);
    if (el) el.textContent = v;
  };
  const setAttr = (id, a, v) => {
    const el = doc.getElementById(id);
    if (el) el.setAttribute(a, v);
  };
  // English is the xhtml default; localize to Chinese on zh locales (before values are set,
  // so the menulist shows the localized label for the current selection).
  if (ZH) {
    setText("zm-s2desc", "Semantic Scholar API key(可选)——留空即用免费公共端点。只有大批量解析撞到限流时才需要填。");
    setAttr("zm-s2label", "value", "S2 API key:");
    setAttr("zm-pref-s2key", "placeholder", "(留空 = 公共端点)");
    setText("zm-confdesc", "审核台里,置信达到此值的条目默认勾选;低于此值的仍会列出,但留给你决定。");
    setAttr("zm-conflabel", "value", "自动勾选 ≥");
    setAttr("zm-c06", "label", "0.60(含未识别的 venue 字符串)");
    setAttr("zm-c08", "label", "0.80(推荐)");
    setAttr("zm-c085", "label", "0.85(单一可信来源)");
    setAttr("zm-c095", "label", "0.95(两个来源一致)");
  }

  const key = doc.getElementById("zm-pref-s2key");
  if (key) {
    key.value = Zotero.Prefs.get("zoteromarker.s2ApiKey") || "";
    key.addEventListener("change", () => Zotero.Prefs.set("zoteromarker.s2ApiKey", key.value.trim()));
  }
  const conf = doc.getElementById("zm-pref-minconf");
  if (conf) {
    conf.value = String(Zotero.Prefs.get("zoteromarker.minConfidence") || "0.8");
    conf.addEventListener("command", () => Zotero.Prefs.set("zoteromarker.minConfidence", String(conf.value)));
  }
}

// ----- lifecycle hooks --------------------------------------------------------------
Zotero.ZoteroMarker = {
  rootURI: null,
  run,
  hooks: {
    async onStartup() {
      try {
        await Zotero.PreferencePanes.register({
          pluginID: "arxiv-marker@lele.dev",
          src: Zotero.ZoteroMarker.rootURI + "content/preferences.xhtml",
          label: T.prefLabel,
        });
      } catch (e) {
        Zotero.debug("arxiv-marker: prefpane register failed: " + e);
      }
      // wire menus into any already-open main windows
      const wins = Zotero.getMainWindows ? Zotero.getMainWindows() : [Zotero.getMainWindow && Zotero.getMainWindow()];
      for (const w of wins) if (w) Zotero.ZoteroMarker.hooks.onMainWindowLoad(w);
    },
    onMainWindowLoad(window) {
      // right-click selected items -> resolve just those
      addToMenu(window, "zotero-itemmenu", "arxiv-marker-itemmenu", T.menu, () => run(window, "selected"));
      // right-click a collection on the left -> resolve everything in it (no item selection needed)
      addToMenu(window, "zotero-collectionmenu", "arxiv-marker-collectionmenu", T.collMenu, () => run(window, "collection"));
      // Tools menu -> auto: selected items if any, else the current collection
      addToMenu(window, "menu_ToolsPopup", "arxiv-marker-toolsmenu", T.menu, () => run(window, "auto"));
      addToMenu(window, "menu_ToolsPopup", "arxiv-marker-undomenu", T.undoMenu, () => undoLast(window));
    },
    onMainWindowUnload(window) {
      removeFromMenu(window);
    },
    onShutdown() {
      const wins = Zotero.getMainWindows ? Zotero.getMainWindows() : [Zotero.getMainWindow && Zotero.getMainWindow()];
      for (const w of wins) if (w) removeFromMenu(w);
      delete Zotero.ZoteroMarker;
    },
    onPrefsLoad,
  },
};
