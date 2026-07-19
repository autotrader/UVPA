// UVPA Dokumentensuche — Frontend
// Kernaufgabe: Dokumente schnell finden (FTS/BM25 + Metadaten-Filter) und den
// extrahierten Volltext sofort im Browser anzeigen. Das Beziehungsnetzwerk
// (Cytoscape) ist die Kontext-Ansicht dazu. Bewusst ohne KI — nur SQL.
//
// Datenquellen: graph.db (unverschlüsselt von Pages), Original-PDFs direkt
// aus dem öffentlichen GitHub-Repo (jsDelivr-CDN). Das Passwort-Gate ist
// bewusst nur eine Nutzungshürde (Hash-Vergleich), kein Datenschutz — die
// Dokumente sind amtlich öffentlich.

import * as duckdb from "https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.33.1-dev57.0/+esm";

const $ = (id) => document.getElementById(id);
const status = (msg) => { $("statusbar").textContent = msg; };
const bootMsg = (msg) => { $("boot-msg").textContent = msg; };
const esc = (s) => String(s).replace(/'/g, "''");
const escHtml = (s) => String(s).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const escRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

const TYPE_NAMES = { EI: "Einladung", NI: "Niederschrift", SU: "Sitzungsunterlagen",
                     BL: "Beschluss", VO: "Beschlussvorlage", AN: "Anlage" };

// ── Zugangs-Gate (einfacher Frontend-Schutz, keine Verschlüsselung) ─────────
// Der Deploy-Workflow legt auth.json mit einem PBKDF2-Hash von SITE_PASSWORD
// ab. Fehlt auth.json (lokaler Dev-Modus), gibt es kein Gate.

async function pbkdf2Hex(password, saltHex, iterations) {
  const salt = Uint8Array.from(saltHex.match(/.{2}/g).map((b) => parseInt(b, 16)));
  const material = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt, iterations, hash: "SHA-256" }, material, 256);
  return [...new Uint8Array(bits)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function checkAuth() {
  let auth;
  try {
    const r = await fetch("auth.json");
    if (!r.ok) return;                       // Dev-Modus: kein Gate konfiguriert
    auth = await r.json();
  } catch { return; }
  if (sessionStorage.getItem("uvpa_auth") === auth.hash) return;

  $("boot").hidden = true;
  $("gate").hidden = false;
  $("gate-pw").focus();
  await new Promise((resolve) => {
    $("gate-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const hash = await pbkdf2Hex($("gate-pw").value, auth.salt, auth.iterations);
      if (hash === auth.hash) {
        sessionStorage.setItem("uvpa_auth", hash);
        $("gate").hidden = true;
        resolve();
      } else {
        $("gate-error").hidden = false;
        $("gate-pw").select();
      }
    });
  });
  $("boot").hidden = false;
}

async function loadDbBytes() {
  const r = await fetch("graph.db");
  if (!r.ok) throw new Error("graph.db nicht gefunden.");
  return new Uint8Array(await r.arrayBuffer());
}

// ── DuckDB-Wasm ──────────────────────────────────────────────────────────────

let conn;

async function initDb(bytes) {
  bootMsg("Starte DuckDB-Wasm …");
  const bundle = await duckdb.selectBundle(duckdb.getJsDelivrBundles());
  const workerUrl = URL.createObjectURL(new Blob(
    [`importScripts("${bundle.mainWorker}");`], { type: "text/javascript" }));
  const db = new duckdb.AsyncDuckDB(
    new duckdb.ConsoleLogger(duckdb.LogLevel.WARNING), new Worker(workerUrl));
  await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
  URL.revokeObjectURL(workerUrl);
  await db.registerFileBuffer("graph.db", bytes);
  await db.open({ path: "graph.db", query: { castBigIntToDouble: true } });
  conn = await db.connect();
  bootMsg("Lade Volltext-Index (FTS) …");
  await conn.query("LOAD fts");
}

async function q(sql) {
  const t = await conn.query(sql);
  return t.toArray().map((r) => r.toJSON());
}

// ── Suche + Ergebnisliste ────────────────────────────────────────────────────

let lastTerms = [];

function currentFilters() {
  return { year: $("f-year").value, type: $("f-type").value,
           ort: $("f-ort").value, sort: $("f-sort").value };
}

function filterConds(f) {
  const conds = [];
  if (f.year) conds.push(`year(n.date) = ${Number(f.year)}`);
  if (f.type === "AN") conds.push(`d.type_code = ''`);
  else if (f.type) conds.push(`d.type_code = '${esc(f.type)}'`);
  if (f.ort) conds.push(
    `EXISTS (SELECT 1 FROM edges e WHERE e.source = d.node_id AND e.target = 'o:${esc(f.ort)}')`);
  return conds;
}

async function runSearch() {
  const query = $("search-input").value.trim();
  const f = currentFilters();
  const conds = filterConds(f);
  lastTerms = query.split(/\s+/).map((w) => w.replace(/[^\p{L}\p{N}\/.\-]/gu, ""))
                   .filter((w) => w.length >= 3).slice(0, 8);

  // Pläne haben kein Sitzungsdatum/keinen Stadtteil-Bezug im Beziehungsnetz —
  // bei aktivem Jahres- oder Stadtteilfilter tauchen sie in der Trefferliste
  // konsequent nicht auf (Filter-Bedeutung sonst irreführend).
  const includePlans = (!f.type || f.type === "PLAN") && !f.year && !f.ort;
  const includeDocs = f.type !== "PLAN";
  let rows;
  if (query) {
    status(`Suche „${query}“ …`);
    conds.unshift("s.score IS NOT NULL");
    const order = f.sort === "date" ? "n.date DESC, s.score DESC" : "s.score DESC";
    rows = includeDocs ? await q(
      `SELECT d.id, 'doc' AS kind, d.title, d.type_code, d.node_id, d.pages,
              n.label AS top_label, n.date::VARCHAR AS date, n.vorlage_nr, s.score
       FROM (SELECT id, fts_main_documents.match_bm25(id, '${esc(query)}') AS score
             FROM documents) s
       JOIN documents d ON d.id = s.id
       JOIN nodes n ON n.id = d.node_id
       WHERE ${conds.join(" AND ")}
       ORDER BY ${order} LIMIT 50`) : [];

    // Snippets nur für die Trefferliste berechnen (nicht über alle 5000 Texte)
    if (rows.length && lastTerms.length) {
      const ids = rows.map((r) => `'${esc(r.id)}'`).join(",");
      const w = esc(lastTerms[0].toLowerCase());
      const snips = await q(
        `SELECT id, substr(text, greatest(position('${w}' IN lower(text)) - 90, 1), 240) AS snip
         FROM documents WHERE id IN (${ids}) AND text IS NOT NULL`);
      const byId = Object.fromEntries(snips.map((s) => [s.id, s.snip]));
      for (const r of rows) r.snippet = byId[r.id];
    }

    if (includePlans) {
      const planRows = await q(
        `SELECT id, 'planfile' AS kind, title, plan_id, top_label, themen, score FROM (
           SELECT pf.rowid::VARCHAR AS id, pf.titel AS title, pf.plan_id,
                  p.title AS top_label, p.themen,
                  fts_main_plan_files.match_bm25(pf.rowid, '${esc(query)}') AS score
           FROM plan_files pf JOIN plans p ON p.id = pf.plan_id
         ) WHERE score IS NOT NULL ORDER BY score DESC LIMIT 15`);
      rows = [...rows, ...planRows].sort((a, b) => b.score - a.score);
    }
    renderResults(rows, `${rows.length} Treffer`);
    status(`${rows.length} Treffer für „${query}“.`);
  } else if (f.type === "PLAN") {
    rows = (f.year || f.ort) ? [] : await q(
      `SELECT p.id, 'plan' AS kind, p.title AS top_label, p.themen, p.beschreibung
       FROM plans p ORDER BY p.title`);
    renderResults(rows, `${rows.length} Pläne & Konzepte`);
    status(f.year || f.ort
      ? "Pläne haben kein Sitzungsjahr/keinen Stadtteil-Bezug — Jahres-/Stadtteilfilter zurücksetzen, um sie zu sehen."
      : `${rows.length} Pläne & Konzepte angezeigt.`);
  } else {
    const where = conds.length ? `WHERE ${conds.join(" AND ")}` : "";
    rows = await q(
      `SELECT d.id, 'doc' AS kind, d.title, d.type_code, d.node_id, d.pages,
              n.label AS top_label, n.date::VARCHAR AS date, n.vorlage_nr
       FROM documents d JOIN nodes n ON n.id = d.node_id
       ${where} ORDER BY n.date DESC, d.title LIMIT 50`);
    renderResults(rows, conds.length ? `${rows.length} Dokumente (gefiltert)` : "Neueste Dokumente");
    status(`${rows.length} Dokumente angezeigt.`);
  }
}

function renderResults(rows, title) {
  $("results-title").textContent = title;
  $("results-list").innerHTML = rows.map((r) => {
    if (r.kind === "plan") return `
      <li data-kind="plan" data-plan="${escHtml(r.id)}">
        <div class="r-title"><span class="badge badge-plan">PLAN</span>${escHtml(r.top_label)}</div>
        <div class="r-meta">${r.themen ? escHtml(r.themen) : ""}</div>
      </li>`;
    if (r.kind === "planfile") return `
      <li data-kind="planfile" data-file="${escHtml(r.id)}" data-plan-title="${escHtml(r.top_label)}">
        <div class="r-title"><span class="badge badge-plan">PLAN</span>${escHtml(r.title)}</div>
        <div class="r-meta">${escHtml(r.top_label ?? "")}${r.themen ? " · " + escHtml(r.themen) : ""}${r.score != null ? ` · Score ${r.score.toFixed(2)}` : ""}</div>
      </li>`;
    return `
      <li data-kind="doc" data-doc="${escHtml(r.id)}">
        <div class="r-title"><span class="badge">${escHtml(r.type_code || "AN")}</span>${escHtml(r.title)}</div>
        <div class="r-meta">${r.date ?? ""}${r.vorlage_nr ? " · " + escHtml(r.vorlage_nr) : ""}
          · ${escHtml(shortLabel(r.top_label ?? "", 55))}${r.score != null ? ` · Score ${r.score.toFixed(2)}` : ""}</div>
        ${r.snippet ? `<div class="r-snippet">… ${highlight(escHtml(r.snippet))} …</div>` : ""}
      </li>`;
  }).join("");
  for (const li of $("results-list").querySelectorAll("li"))
    li.addEventListener("click", () => {
      $("results-list").querySelector("li.active")?.classList.remove("active");
      li.classList.add("active");
      if (li.dataset.kind === "plan") openPlan(li.dataset.plan);
      else if (li.dataset.kind === "planfile") openPlanFile(li.dataset.file, li.dataset.planTitle);
      else openDoc(li.dataset.doc);
    });
}

function shortLabel(s, max = 70) {
  return s.length > max ? s.slice(0, max - 1).trimEnd() + "…" : s;
}

function highlight(escapedHtml) {
  if (!lastTerms.length) return escapedHtml;
  const re = new RegExp(`(${lastTerms.map(escRe).join("|")})`, "giu");
  return escapedHtml.replace(re, "<mark>$1</mark>");
}

// ── Dokument-Leseansicht ─────────────────────────────────────────────────────

// Original-PDFs kommen direkt aus dem öffentlichen GitHub-Repo. Wir holen die
// Bytes selbst und zeigen sie als Blob mit PDF-Content-Type im nativen
// Browser-Viewer — so funktioniert es unabhängig davon, welche Header die
// Quelle sendet. Nach dem Umzug zur Organisation nur REPO anpassen.
const REPO = "autotrader/UVPA";
const PDF_SOURCES = [
  (p) => `https://cdn.jsdelivr.net/gh/${REPO}@main/${p}`,
  (p) => `https://raw.githubusercontent.com/${REPO}/main/${p}`,
];
let pdfBlobUrl = null;

function notice(msg) {
  const el = $("doc-notice");
  el.textContent = msg;
  el.hidden = !msg;
}

/** PDF-Bytes laden; null = Datei nicht im Repository (oder offline). */
async function fetchDocPdf(path) {
  const p = encodeURI(path);
  for (const src of PDF_SOURCES) {
    try {
      const r = await fetch(src(p));
      if (r.ok) return new Uint8Array(await r.arrayBuffer());
    } catch { /* Quelle nicht erreichbar — nächste probieren */ }
  }
  return null;
}

async function showDocPdf(d) {
  notice("");
  status("Lade PDF …");
  const bytes = await fetchDocPdf(d.path);
  if (!bytes) {
    notice("Dieses PDF liegt nicht im Repository (z. B. übergroße Sitzungsunterlagen) " +
           "oder es besteht keine Internetverbindung — bitte das Original über den RIS-Link laden.");
    status("PDF nicht verfügbar.");
    return;
  }
  if (pdfBlobUrl) URL.revokeObjectURL(pdfBlobUrl);
  pdfBlobUrl = URL.createObjectURL(new Blob([bytes], { type: "application/pdf" }));
  $("doc-body").innerHTML =
    `<iframe class="pdf-frame" src="${pdfBlobUrl}" title="PDF-Ansicht"></iframe>`;
  $("btn-pdf").classList.add("active");
  $("btn-text")?.classList.remove("active");
  status(`PDF angezeigt (${(bytes.length / 1048576).toFixed(1)} MB).`);
}

async function openDoc(id) {
  const [d] = await q(
    `SELECT d.id, d.title, d.type_code, d.node_id, d.path, d.url, d.pages, d.text,
            n.label AS top_label, n.date::VARCHAR AS date, n.vorlage_nr
     FROM documents d JOIN nodes n ON n.id = d.node_id
     WHERE d.id = '${esc(id)}'`);
  if (!d) return;
  activateTab("doc");

  const tc = d.type_code || "AN";
  const html = `<div class="doc-head">
    <h3><span class="badge">${escHtml(tc)}</span>${escHtml(d.title)}</h3>
    <p class="meta">${TYPE_NAMES[tc] ?? tc} · ${d.date ?? ""}${d.pages ? ` · ${d.pages} Seiten` : ""}</p>
    <p class="meta">${escHtml(d.top_label)}${d.vorlage_nr ? " · Vorlage " + escHtml(d.vorlage_nr) : ""}</p>
    <div class="doc-actions">
      <button id="btn-text" class="active" type="button">Text</button>
      <button id="btn-pdf" type="button">PDF</button>
      <a href="${escHtml(d.url)}" target="_blank" rel="noopener"
         title="${escHtml(d.path)}">⬇ Original-PDF (RIS)</a>
      <button id="btn-context" type="button">⛬ Kontext im Netzwerk</button>
    </div>
  </div>
  <div id="doc-notice" class="notice" hidden></div>
  <div id="doc-body"></div>`;

  $("doc-view").innerHTML = html;
  renderDocText(d);
  $("btn-context").addEventListener("click", () => focusNode(d.node_id));
  $("btn-pdf").addEventListener("click", () => showDocPdf(d));
  $("btn-text").addEventListener("click", () => {
    renderDocText(d);
    $("btn-text").classList.add("active");
    $("btn-pdf").classList.remove("active");
  });
  $("panel-doc").scrollTop = 0;
  document.querySelector("#doc-body .doc-page mark")
    ?.scrollIntoView({ block: "center" });
}

/** Externer Plan/Konzept (plaene/registry.json): Übersicht + Dateien + verknüpfte TOPs. */
async function openPlan(planId) {
  const [p] = await q(
    `SELECT id, title, beschreibung, quelle_url, themen FROM plans WHERE id = '${esc(planId)}'`);
  if (!p) return;
  activateTab("doc");

  const files = await q(
    `SELECT rowid::VARCHAR AS id, titel, path, quelle_url, pages, (text IS NOT NULL) AS has_text
     FROM plan_files WHERE plan_id = '${esc(planId)}' ORDER BY titel`);
  const related = await q(
    `SELECT n.label, n.date::VARCHAR AS date FROM edges e JOIN nodes n ON n.id = e.target
     WHERE e.source = 'p:${esc(planId)}' AND e.type = 'relates_to_plan' ORDER BY n.date DESC`);

  const html = `<div class="doc-head">
    <h3><span class="badge badge-plan">PLAN</span>${escHtml(p.title)}</h3>
    ${p.beschreibung ? `<p class="meta">${escHtml(p.beschreibung)}</p>` : ""}
    <p class="meta">${p.themen ? escHtml(p.themen) : ""}</p>
    <div class="doc-actions">
      ${p.quelle_url ? `<a href="${escHtml(p.quelle_url)}" target="_blank" rel="noopener">🔗 Quelle (Stadt Erlangen)</a>` : ""}
    </div>
  </div>
  <div id="doc-notice" class="notice" hidden></div>
  <div id="doc-body">
    ${files.length ? `<div class="doc-page"><p class="doc-page-nr">Dokumente</p><ul class="plan-files">` +
      files.map((f) => `<li>
          <a href="#" data-plan-file="${escHtml(f.id)}">${escHtml(f.titel)}</a>
          ${f.pages ? `<span class="meta"> · ${f.pages} S.</span>` : ""}
          ${f.quelle_url ? ` · <a href="${escHtml(f.quelle_url)}" target="_blank" rel="noopener">Original</a>` : ""}
        </li>`).join("") + `</ul></div>` : ""}
    ${related.length ? `<div class="doc-page"><p class="doc-page-nr">Verknüpfte Tagesordnungspunkte (${related.length})</p><ul class="plan-files">` +
      related.map((r) => `<li>${r.date ?? ""} — ${escHtml(shortLabel(r.label, 90))}</li>`).join("") + `</ul></div>` : ""}
  </div>`;

  $("doc-view").innerHTML = html;
  for (const a of $("doc-view").querySelectorAll("[data-plan-file]"))
    a.addEventListener("click", (ev) => { ev.preventDefault(); openPlanFile(a.dataset.planFile, p.title); });
}

async function openPlanFile(fileId, planTitle) {
  const [f] = await q(
    `SELECT titel, path, quelle_url, pages, text FROM plan_files WHERE rowid = ${Number(fileId)}`);
  if (!f) return;
  const html = `<div class="doc-head">
    <h3><span class="badge badge-plan">PLAN</span>${escHtml(f.titel)}</h3>
    <p class="meta">${escHtml(planTitle)}${f.pages ? ` · ${f.pages} Seiten` : ""}</p>
    <div class="doc-actions">
      <button id="btn-text" class="active" type="button">Text</button>
      ${f.path ? `<button id="btn-pdf" type="button">PDF</button>` : ""}
      ${f.quelle_url ? `<a href="${escHtml(f.quelle_url)}" target="_blank" rel="noopener">⬇ Original</a>` : ""}
    </div>
  </div>
  <div id="doc-notice" class="notice" hidden></div>
  <div id="doc-body"></div>`;
  $("doc-view").innerHTML = html;
  renderDocText(f);
  if (f.path) $("btn-pdf").addEventListener("click", () => showDocPdf(f));
  $("btn-text").addEventListener("click", () => {
    renderDocText(f);
    $("btn-text").classList.add("active");
    $("btn-pdf")?.classList.remove("active");
  });
  $("panel-doc").scrollTop = 0;
}

function renderDocText(d) {
  notice("");
  if (d.text) {
    const pages = d.text.split("\f");
    $("doc-body").innerHTML = pages.map((p, i) => `
      <div class="doc-page">
        ${pages.length > 1 ? `<p class="doc-page-nr">Seite ${i + 1} / ${pages.length}</p>` : ""}${highlight(escHtml(p.trim()))}
      </div>`).join("");
  } else {
    $("doc-body").innerHTML = `<p class="hint">Für dieses Dokument liegt kein
      extrahierter Text vor (vermutlich gescannt oder Datei nicht im Repository).
      Über den PDF-Button lässt sich das Original meist direkt anzeigen.</p>`;
  }
}

// ── Netzwerk-Ansicht (Kontext) ───────────────────────────────────────────────

const COLORS = { top: "#2a78d6", session: "#008300", ort: "#e87ba4", vorlage: "#eda100", bplan: "#1baf7a", plan: "#4a3aa7" };
const SHAPES = { top: "ellipse", session: "round-rectangle", ort: "triangle", vorlage: "diamond", bplan: "hexagon", plan: "star" };

let cy = null;

function initCy() {
  cy = cytoscape({
    container: $("cy"),
    wheelSensitivity: 0.25,
    style: [
      { selector: "node", style: {
          label: "data(short)",
          "font-size": 8, color: "#52514e",
          "text-wrap": "wrap", "text-max-width": 110,
          "text-valign": "bottom", "text-margin-y": 4,
          width: 18, height: 18,
          "background-color": (el) => COLORS[el.data("type")] ?? "#898781",
          shape: (el) => SHAPES[el.data("type")] ?? "ellipse",
          "border-width": 1, "border-color": "rgba(11,11,11,0.25)",
      }},
      { selector: 'node[type="session"]', style: { width: 26, height: 18 } },
      { selector: 'node[type="top"]', style: { width: 22, height: 22 } },
      { selector: "node:selected", style: { "border-width": 3, "border-color": "#0b0b0b" } },
      { selector: "edge", style: {
          "curve-style": "bezier", width: 1,
          "line-color": "#c3c2b7", opacity: 0.8,
      }},
      { selector: 'edge[type="mentions_ort"], edge[type="mentions_bplan"]', style: {
          "line-style": "dotted", opacity: 0.55 } },
      { selector: 'edge[type="references_vorlage"]', style: { "line-style": "dashed" } },
      { selector: 'edge[type="thread"]', style: {
          "line-color": "#eb6834", width: 3, opacity: 0.95 } },
      { selector: 'edge[type="relates_to_plan"]', style: {
          "line-color": "#4a3aa7", "line-style": "dashed", width: 2, opacity: 0.85 } },
    ],
  });
  // Klick im Graphen → Dokumente dieses Knotens in der Ergebnisliste
  cy.on("tap", "node", async (ev) => {
    const id = ev.target.id();
    if (ev.target.data("type") === "plan") {
      await expandNode(id);
      return openPlan(id.slice(2));
    }
    await expandNode(id);
    const rows = await q(
      `SELECT d.id, 'doc' AS kind, d.title, d.type_code, d.node_id, d.pages,
              n.label AS top_label, n.date::VARCHAR AS date, n.vorlage_nr
       FROM documents d JOIN nodes n ON n.id = d.node_id
       WHERE d.node_id = '${esc(id)}' ORDER BY d.title`);
    if (rows.length) {
      renderResults(rows, `Dokumente: ${shortLabel(ev.target.data("label"), 40)}`);
      status(`${rows.length} Dokument(e) zu „${shortLabel(ev.target.data("label"), 60)}“.`);
    } else {
      status(`„${shortLabel(ev.target.data("label"), 60)}“ — keine direkten Dokumente; Nachbarn geladen.`);
    }
  });
}

function addElements(nodeRows, edgeRows) {
  const els = [];
  for (const n of nodeRows) {
    if (cy.getElementById(n.id).length) continue;
    els.push({ group: "nodes", data: {
      id: n.id, type: n.type, label: n.label, short: shortLabel(n.label),
    }});
  }
  for (const e of edgeRows) {
    const eid = `${e.source}->${e.target}:${e.type}`;
    if (cy.getElementById(eid).length) continue;
    els.push({ group: "edges", data: {
      id: eid, source: e.source, target: e.target, type: e.type, weight: e.weight,
    }});
  }
  if (els.length) cy.add(els);
  cy.layout({ name: "cose", animate: false, padding: 30,
              nodeRepulsion: 40_000, idealEdgeLength: 60 }).run();
}

async function fetchNodesByIds(ids) {
  if (!ids.length) return [];
  const list = ids.map((i) => `'${esc(i)}'`).join(",");
  return q(`SELECT id, type, label FROM nodes WHERE id IN (${list})`);
}

/** Übersicht: alle roten Fäden (gleiche Vorlage über mehrere Sitzungen). */
async function showOverview() {
  const threads = await q("SELECT source, target, type, weight FROM edges WHERE type='thread'");
  const topIds = [...new Set(threads.flatMap((e) => [e.source, e.target]))];
  const sessEdges = topIds.length
    ? await q(`SELECT source, target, type, weight FROM edges
               WHERE type='in_session' AND source IN (${topIds.map((i) => `'${esc(i)}'`).join(",")})`)
    : [];
  const allIds = [...new Set([...topIds, ...sessEdges.map((e) => e.target)])];
  addElements(await fetchNodesByIds(allIds), [...threads, ...sessEdges]);
}

/** 1-Hop-Nachbarschaft eines Knotens nachladen + Kantenschluss. */
async function expandNode(id) {
  const neighborEdges = await q(
    `SELECT source, target, type, weight FROM edges
     WHERE source = '${esc(id)}' OR target = '${esc(id)}'
     ORDER BY weight DESC LIMIT 150`);
  const ids = [...new Set([id, ...neighborEdges.flatMap((e) => [e.source, e.target])])];
  addElements(await fetchNodesByIds(ids), neighborEdges);
  const visible = cy.nodes().map((n) => n.id());
  const list = visible.map((i) => `'${esc(i)}'`).join(",");
  addElements([], await q(
    `SELECT source, target, type, weight FROM edges
     WHERE source IN (${list}) AND target IN (${list})`));
}

/** Vom Dokument in den Netzwerk-Tab springen und den Knoten fokussieren. */
async function focusNode(nodeId) {
  activateTab("net");
  await expandNode(nodeId);
  const node = cy.getElementById(nodeId);
  cy.elements().unselect();
  node.select();
  cy.animate({ center: { eles: node }, zoom: 1.2, duration: 300 });
}

// ── Tabs ─────────────────────────────────────────────────────────────────────

function activateTab(which) {
  const isDoc = which === "doc";
  $("tab-doc").classList.toggle("active", isDoc);
  $("tab-net").classList.toggle("active", !isDoc);
  $("panel-doc").hidden = !isDoc;
  $("panel-net").hidden = isDoc;
  if (!isDoc) {
    if (!cy) { initCy(); showOverview(); }
    else cy.resize();
  }
}

// ── Filter füllen ────────────────────────────────────────────────────────────

async function populateFilters() {
  const years = await q(
    "SELECT DISTINCT year(date)::INT AS y FROM nodes WHERE type='session' ORDER BY y DESC");
  $("f-year").insertAdjacentHTML("beforeend",
    years.map((r) => `<option value="${r.y}">${r.y}</option>`).join(""));
  const orte = await q("SELECT label FROM nodes WHERE type='ort' ORDER BY label");
  $("f-ort").insertAdjacentHTML("beforeend",
    orte.map((r) => `<option value="${escHtml(r.label)}">${escHtml(r.label)}</option>`).join(""));
}

// ── Boot ─────────────────────────────────────────────────────────────────────

try {
  await checkAuth();
  bootMsg("Lade Datenbank …");
  const bytes = await loadDbBytes();
  await initDb(bytes);
  await populateFilters();

  $("search-form").addEventListener("submit", (ev) => { ev.preventDefault(); runSearch(); });
  for (const id of ["f-year", "f-type", "f-ort", "f-sort"])
    $(id).addEventListener("change", runSearch);
  $("tab-doc").addEventListener("click", () => activateTab("doc"));
  $("tab-net").addEventListener("click", () => activateTab("net"));

  await runSearch();   // Startansicht: neueste Dokumente
  $("boot").hidden = true;

  const [meta] = await q(
    `SELECT (SELECT count(*) FROM documents)::INT AS d,
            (SELECT count(*) FROM documents WHERE text IS NOT NULL)::INT AS t,
            (SELECT count(*) FROM nodes)::INT AS n`);
  status(`Bereit — ${meta.d} Dokumente (${meta.t} mit Volltext), ${meta.n} Knoten. ` +
         `Suche oben, Filter darunter.`);
} catch (err) {
  bootMsg(`Fehler: ${err.message}`);
  console.error(err);
}
