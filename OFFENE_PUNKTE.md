# Offene Punkte — UVPA & SBR-Büchenbach

Übergabedokument für einen weiterarbeitenden Coding-Agent.
Erstellt 2026-07-22, **abgearbeitet am selben Tag**. Alles unten ist gegen Repo,
Live-Seite und Ratsinformationssystem geprüft, nicht aus Notizen übernommen.
Wo etwas nicht verifiziert werden konnte, steht das ausdrücklich dabei.

Was am 2026-07-22 erledigt wurde, steht in **Teil A** — mit den Erkenntnissen,
die dabei angefallen sind, weil sie beim Weiterarbeiten Zeit sparen.
**Teil B** listet, was noch offen ist.

---

## 0. Die zwei Projekte in drei Sätzen

| | UVPA | SBR-Büchenbach |
|---|---|---|
| Repo | `c:\Csharp\UVPA` → `Erlangen-Kommunal/UVPA` | `c:\Csharp\SBR-Buechenbach` → `Erlangen-Kommunal/SBR-Buechenbach` |
| Live | https://erlangen-kommunal.github.io/UVPA/ | https://erlangen-kommunal.github.io/SBR-Buechenbach/ |
| Inhalt | Umwelt-, Verkehrs- und Planungsausschuss, 69 Sitzungen ab 2020-01-21 | Stadtteilbeirat Büchenbach, 16 Sitzungen ab 2020-01-22 |
| Aufbau | `GraphBuilder/` (C#/net10, DuckDB + PdfPig) baut `graph.db` → statisches `web/`-SPA mit DuckDB-Wasm und FTS/BM25 (stemmer `german`) | identisch, flacherer Index |

Beide haben ein **Frontend-Passwort-Gate** (kein Datenschutz, nur Nutzungshürde):
`tools/make-auth.mjs` schreibt einen PBKDF2-Hash nach `_site/auth.json`. Fehlt die
Datei, entfällt das Gate — so läuft die lokale Entwicklung. Passwort in `.secrets`
(gitignored), gehört nie ins Repo.

Deployment: Push auf `main` → `deploy.yml`. Wochen-Sync: `sync.yml`, donnerstags
04:00 Europe/Berlin, deterministisch und ohne LLM.

---

# Teil A — erledigt am 2026-07-22

## A1. Die Fachbeiräte-Daten waren nie live (SBR)

**Das war der teuerste Fehler und er ist beim Bearbeiten selbst passiert.**

SBR hatte zwei Verzeichnisse mit gleich heißenden JSON-Dateien: `content/` im
Wurzelverzeichnis (kanonisch, wird per `cp -r content _site/content` ausgeliefert)
und `web/content/` — **gitignored**, nie getrackt, nur eine lokale Kopie.

Wer die Dev-Kopie bearbeitete, sah lokal alles funktionieren, Git meldete nichts,
und live änderte sich nie etwas. Genau so lag `content/fachbeiraete.json` seit dem
allerersten Commit unverändert bei 8 Einträgen, während die Dev-Kopie auf 32
Gremien mit Sitzungsdaten gewachsen war. Auch die committeten Ratsinfo-Buttons
rendeten live nie, weil `start_url`/`uebersicht_url`/`ausschuesse_url` in der
ausgelieferten Datei fehlten.

**Behoben:** Die 32-Gremien-Fassung ist kanonisch. `web/content` ist jetzt eine
**Junction auf `content/`** — beide Pfade zeigen auf dieselben Dateien, ein
Auseinanderlaufen ist damit ausgeschlossen. Nach einem frischen Clone einmal
`pwsh -File tools/link-content.ps1` aufrufen; das Skript bricht ab, wenn es in
`web/content` Stände findet, die in `content/` fehlen.

> **Regel fürs Weiterarbeiten:** Vor jeder Änderung an `content/*` prüfen, welchen
> Pfad man bearbeitet. Und: Wenn eine Änderung „lokal geht, live nicht", zuerst
> fragen, ob die Datei überhaupt ausgeliefert wird.

## A2. Sitzung 02.07.2026 fehlte im SBR-Index — Spaltenzahl ist nicht konstant

Der SBR-Scraper griff in der Sitzungsübersicht hart auf `row[2]` als
Dokumentenspalte zu. Bei **terminierten** Sitzungen schiebt SessionNet aber eine
zusätzliche Zelle `sitermin` ein, wodurch `sidocs` auf Index 3 rutscht. Ergebnis:
Die gemeinsame Sitzung aller Stadtteilbeiräte vom 02.07.2026 galt als Sitzung
ohne Dokumente und fehlte im Index — **ohne dass irgendetwas fehlschlug**.

**Behoben:** `_RowParser` merkt sich die CSS-Klasse jeder Zelle,
`docs_cell()` sucht die Spalte über `sidocs` mit der letzten Zelle als Rückfall.
Der Index umfasst jetzt 47 Dokumente in 16 Sitzungen.

> Nie über die Spaltenposition adressieren, immer über die Klasse.

## A3. Doppelte Niederschrift (SBR)

Das RIS listet die Niederschrift zur Sitzung 2020-10-20 zweimal unter
verschiedenen Namen — einmal sprechend, einmal mit interner RIS-Kennung, beide
Dateien byte-identisch. Beide Einträge haben eine eigene `doc_id`, die
vorhandene Prüfung griff also nicht; die Sitzung erschien doppelt und einmal
ohne Zusammenfassung.

**Behoben:** Der GraphBuilder entdoppelt über den SHA-256 des Dateiinhalts und
behält den Eintrag, an dem die Anreicherung hängt. Der Index wird bei jedem Sync
neu erzeugt, deshalb sitzt die Entdopplung im Builder und nicht in der Datei.
Jetzt 46 Dokumente, **alle 15 Niederschriften mit Zusammenfassung**.

## A4. Die 279 PDFs ohne Zusammenfassung — überwiegend Absicht, aber zwei Funde

Die Lücke ist erklärt: `enrichment/enrich.py` überspringt Dokumente ohne
extrahierbaren Text (`if not text: continue`). Von 313 Dateien ohne `.md` haben
**300 tatsächlich keinen Text** (gescannte Pläne, Karten, Freiflächenkonzepte) —
kein Versäumnis, sondern das dokumentierte Verhalten.

Zwei echte Funde blieben übrig:

- **5 Dateien sind gar keine PDFs.** Das RIS liefert Haushalts- und
  Abstimmungslisten mit der Endung `.pdf` aus, die in Wahrheit **XLSX** sind
  (ZIP-Signatur `PK\x03\x04`). PdfPig scheiterte daran stumm.
  **Behoben:** `GraphBuilder/SheetText.cs` erkennt die Magic Bytes und liest die
  Zellwerte; alle fünf sind jetzt im Volltext. Verifiziert, dass echte PDFs
  unverändert durchlaufen.
- **5 Dokumente mit Text, aber ohne `.md`** — eine echte, kleine Lücke, siehe B2.

## A5. Karte: BayernAtlas raus, amtliche WMS-Dienste rein

Wunsch war, den BayernAtlas nur noch für Flurstückkarten mit hochauflösendem
Luftbild zu einer Straße oder einem POI zu nutzen; Standardkarten über OSM.

Dabei kam heraus: `atlas.bayern.de` bleibt uneinbettbar (`X-Frame-Options: DENY`),
**seine Datengrundlage aber nicht.** Die Bayerische Vermessungsverwaltung
betreibt offene WMS-Dienste, beide **CC BY 4.0, kostenfrei, mit CORS-Header und
EPSG:3857** — also ohne Umprojektion Leaflet-tauglich:

| Zweck | Dienst | Layer |
|---|---|---|
| Luftbild 40 cm | `geoservices.bayern.de/od/wms/dop/v1/dop40` | `by_dop40c` (Farbe), `by_dop40g` (grau) |
| Flurstücke | `…/od/wms/alkis/v1/parzellarkarte` | `by_alkis_parzellarkarte_umr_gelb` |

**Umgesetzt in beiden Repos:** Luftbild als weitere Grundkarte, Flurstücke als
Overlay, OSM als Standard. Der allgemeine „Im BayernAtlas öffnen"-Button ist
weg; der Deeplink bleibt nur dort, wo er etwas kann, das die eingebettete Karte
nicht kann — Flurstücke auf Luftbild zu einer Straße/POI (Layer `luftbild_parz`,
Zoom 17). Der Flurstück-Umring ist für große Maßstäbe gezeichnet und bleibt
unter Zoom 16 leer, deshalb erscheint dann ein Hinweis statt einer wortlos
leeren Karte.

Mit Playwright live geprüft: alle WMS-Antworten HTTP 200, Flurstücke liegen
deckungsgleich auf dem Luftbild.

> Lizenz und Gebührenfreiheit stehen im GetCapabilities unter
> `AccessConstraints` und `Fees` — dort nachlesen, nicht anderswo.
> Der Namensraum ist `/od/wms/<thema>/v1/<sprechender-name>`; **geratene Pfade**
> (`alkis/v1/alkis`, `alkis/v1/flurstueck`) liefern 500, `dfk/v1/dfk` und
> `inspire/cp/v1/cp` liefern 404.

## A6. Beratungsfolge der Vorlagen (UVPA)

Der Index kannte je Tagesordnungspunkt die Vorlagennummer, aber nicht den
weiteren Weg durch die Gremien. Genau der ist für die Beiratsarbeit interessant:
ob der Stadtrat einer Ausschussempfehlung gefolgt ist, ob eine Sache noch
woanders liegt, wie oft sie vertagt wurde.

`tools/fetch_beratungsfolge.py` liest die Registerkarte „Beratungen" je Vorlage
(`vo0053.asp?__kvonr=<N>` — **nicht** `vo0050.asp`, dort steht sie nicht),
inkrementell und nur mit Standardbibliothek. Im Wochen-Sync mit
`continue-on-error` verdrahtet.

**Ergebnis:** 3.161 Beratungen zu 1.442 Vorlagen in 14 Gremien, 0 Fehler.
1.255 Vorlagen liefen durch mehr als ein Gremium, **224 durch den Stadtrat**.
Neue Tabelle `beratungen`; im Dokument erscheint ein einklappbarer Abschnitt,
aber erst ab zwei Stationen — eine einzelne sagt nichts aus.

## A7. Nahverkehrsplan 2025

Der Vermerk „noch nicht veröffentlicht" am NVP 2016 ist erledigt: Der
fortgeschriebene NVP wurde am **27.11.2025 vom Stadtrat beschlossen** und liegt
als PDF vor. Aufgenommen als eigener Registry-Eintrag `nvp-2025`; das 27,3-MB-
Original ist auf 4,9 MB komprimiert (214 Seiten, Text erhalten), weil der
Pre-Commit-Hook ab 12 MB blockt. Der NVP 2016 bleibt als Vergleichsgrundlage.

## A8. READMEs

Beide Repos haben jetzt eine README, die Zweck, Aufbau, lokalen Build,
Automatik und die Fallstricke beschreibt. SBR hatte gar keine, UVPA hatte zwei
Zeilen. Dazu `geo/DATENQUELLEN.md` mit den geprüften Datenquellen (siehe B4).

---

# Teil B — noch offen

## B1. Wochen-Sync: läuft er überhaupt?

In **keinem** der beiden Repos existiert je ein Commit von `github-actions[bot]`.
Für SBR ist das erklärbar — der Sync fand schlicht nie etwas Neues (bis auf die
Sitzung aus A2, die er wegen des Parserfehlers nicht sah). Für **UVPA ist es
das nicht**: dort fehlten vier Sitzungen aus 2020, die ein laufender Wochen-Sync
längst nachgezogen hätte.

**Nicht verifizierbar gewesen** — die `gh`-CLI fehlt auf dem Rechner, die
Workflow-Logs waren nicht einsehbar. Zu prüfen:

- Repository → Actions → **Workflow permissions auf „Read and write"** (ohne das
  scheitert der Auto-Commit)
- Läuft der Cron? GitHub deaktiviert Cron-Workflows in Repos ohne Aktivität nach
  60 Tagen automatisch.
- Die Logs der letzten Läufe ansehen — dort steht, woran es lag.

Die Pfadangaben in beiden Workflows sind korrekt (SBR nutzt
`working-directory: SBR`, UVPA hat das Skript im Wurzelverzeichnis).

## B2. Fünf Dokumente ohne Zusammenfassung (UVPA)

Übrig aus der Analyse in A4 — Dokumente **mit** Text, aber ohne `.md`:

| Zeichen | Datei |
|---|---|
| 20.000 | `2021-07-20/…StUB…/Anlage_3_Wettbewerbsbeitrag_3_Preis_StUB_Regnitzquerung.pdf` |
| 5.981 | `2022-05-17/…Erlangen-Suedost…/2022-04-06_Vorentwurf_mit_Leitungen_DINA4.pdf` |
| 5.633 | `2021-09-21/…Fuchsengarten…/Anlage_3_Handlungsempfehlungen_des_Blockkonzeptes.pdf` |
| 5.494 | `2022-05-17/…Erlangen-Suedost…/2022-04-06_Vorentwurf_DINA4.pdf` |
| 472 | `2023-03-14/…Entsiegelung…/Anlage-2_Entsiegelung-Baumstandorte_Amt-fuer-Sport-und-Gesun.pdf` |

Dazu kommen die neu synchronisierten Sitzungen (siehe B3), deren Dokumente
ebenfalls noch keine Zusammenfassung haben.

**Ablauf:** Die Zusammenfassungen entstehen **nicht** in der CI, sondern werden
lokal von einem KI-Agenten geschrieben — `enrichment/enrich.py` ist bewusst kein
lauffähiges Skript (`call_llm()` wirft `SystemExit`), sondern hält Taxonomie,
Prompts und Schema als Referenz. Muster: vorhandene Dateien unter
`enrichment/docs/`. Themen **ausschließlich** aus `enrichment/themen.md`, intern
mit `|` getrennt (ein Thema enthält selbst Kommas, Komma-Split zerlegt das
Filter-Dropdown).

## B3. UVPA-Sitzungsdaten committen

Der Sync hat die vier fehlenden Sitzungen aus 2020 sowie 2026-07-14 nachgezogen.
Vor dem Commit beachten: Der Pre-Commit-Hook blockt Dateien ab 12 MB
(`git config core.hooksPath .githooks`); mindestens eine neue Sitzungsunterlage
lag darüber und muss über `python uvp_agent.py --compress` klein werden.

Die vier **terminierten** Sitzungen ab 2026-09-22 fehlen weiterhin — zu Recht:
Sie haben im RIS noch keine Dokumente. Das ist keine Lücke.

## B4. Weitere Datenquellen — geprüft, aber nicht eingebaut

Vollständig in [`geo/DATENQUELLEN.md`](geo/DATENQUELLEN.md), inklusive der
gemessenen CORS-Header und der Sackgassen. Kurz:

- **OpenRailwayMap** (Kacheln + JSON-API, ODbL, CORS `*`) — naheliegend für
  Bahnthemen (StUB, Aurachtalbahn, Bahnübergänge). Die Overlay-Mechanik steht
  seit A5 bereit, es fehlt nur der Layer.
- **VGN Open Data** (`www.vgn.de/opendata/GTFS.zip`, 14,6 MB, kein Schlüssel) —
  **CC BY-SA 3.0 DE**, ausdrücklich nur *Soll*-Fahrplandaten. Beim Copyleft
  aufpassen, bevor man es mit anders lizenzierten Daten verknüpft.
- **Zensus 2022 Atlas** — kleinräumige Bevölkerungsdaten, aber **ohne CORS**:
  zur Bauzeit holen wie die übrigen Geodaten. Lizenz noch offen.
- **Verspätungsdaten ESTW/VAG:** keine offene Schnittstelle auffindbar; der VGN
  schließt Echtzeitdaten ausdrücklich aus. Direkt bei ESTW/VGN anfragen statt
  auf gut Glück zu implementieren.

## B5. Kleineres

| Repo | Punkt |
|---|---|
| beide | Anthropic-Key soll laut Projektentscheidung rotiert werden |
| SBR | Beiratsgebiet Anger/Bruck: seit 01.05.2026 zwei getrennte Beiräte, aber **keine getrennte Geometrie** — beide Namen zeigen auf dasselbe Gebiet, der Filter liefert dort eine Obermenge. **Grenze nicht schätzen**, bis die Stadt Geometrie liefert |
| UVPA | Die 5 XLSX-Dateien behalten ihre irreführende `.pdf`-Endung. Der Volltext stimmt jetzt, aber das Frontend bietet für sie weiterhin „PDF anzeigen" an, was fehlschlagen wird. Sauber wäre, den echten Typ beim Download zu erkennen und in `documents` zu führen |

---

## Fallen, die schon Zeit gekostet haben

- **Spaltenposition in SessionNet-Tabellen ist nicht stabil** (A2) — über die
  CSS-Klasse adressieren.
- **`atlas.bayern.de` sendet `X-Frame-Options: DENY`** — iframe ist unmöglich,
  nicht schwierig. Die WMS-Dienste dahinter sind der Weg (A5).
- **Overpass sendet `Access-Control-Allow-Origin: *` nur bei gesetztem
  `Origin`-Header.** `curl` ohne Origin führt in die Irre. Der Hauptserver
  quittiert Last mit 504/429, deshalb die Spiegel `kumi.systems`, `osm.ch`.
- **Stadtrecht A–Z auf `erlangen.de` ist clientseitig gerendert** — mit
  Playwright scrapen. Die Liste verlinkt nicht direkt auf PDFs, sondern auf
  `/aktuelles/<slug>`: zweistufig vorgehen.
- **Die beiden Erlanger Shapefiles nutzen verschiedene Zeichensätze** —
  dBASE-Sprachtreiberbyte 29 auswerten (`0x57`→cp1252, `0x10`→cp850), sonst wird
  „Hüttendorf" zu „H³ttendorf".
- **Kartenklick über `document_streets`, nicht über `mentions_strasse`-Kanten** —
  die hängen am TOP, und ein TOP hat oft ein Dutzend Anlagen, von denen nur eine
  die Straße nennt (Odenwaldallee: 200 statt korrekt 169 Treffer).
- **Playwright-Tests dürfen nicht auf `#boot[hidden]` warten** — `checkAuth()`
  versteckt `#boot` schon während des Passwort-Gates, der Test läuft dann los,
  bevor die DB geladen ist, und sieht leere Dropdowns. Stattdessen auf den
  `#statusbar`-Text warten. Playwright liegt nicht als Modul vor, sondern im
  npx-Cache (`~/AppData/Local/npm-cache/_npx/e41f203b7505f1fb/node_modules/playwright/index.mjs`).
- **Asset-Version bei jeder Frontend-Änderung hochzählen** — `APP_VERSION`,
  `CONTENT_VERSION` (nur SBR, bustet `content/*.json`) und die `?v=`-Parameter
  in `index.html` müssen synchron laufen.
- **`app.js` nicht per Skript komplett neu schreiben**, wenn sie parallel im
  Editor offen sein könnte. Ein dabei zerstörtes typografisches
  Anführungszeichen (`“` → `"`) hat den JS-String vorzeitig beendet und die
  ganze Datei unparsebar gemacht. Nach jeder Änderung `node --check` laufen
  lassen (Datei nach `.mjs` kopieren).
- **Im UVPA-Repo war ein Fremdakteur „Copilot" aktiv** und hat parallel gepusht.
  Vor jedem Push `git fetch`.
- **Python-Ausgabe in Hintergrundläufen puffern** — ein Sync-Lauf ohne `python -u`
  hat seine gesamte Download-Ausgabe verloren, obwohl er 57 Dateien geladen hat.
