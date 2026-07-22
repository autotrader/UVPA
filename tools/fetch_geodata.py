#!/usr/bin/env python3
"""Holt die Geodaten für den Karten-Tab — deterministisch, ohne LLM, ohne Fremd-Key.

Zwei Quellen, beide öffentlich:

  1. OpenStreetMap über die Overpass-API — Geometrie und Tempo-Klassen der
     Straßen sowie die Einrichtungen (Schulen, Kitas, soziale Einrichtungen,
     Spielplätze). Die Abfragen stehen kuratiert und kommentiert in
     geo/*.overpassql.  Lizenz: ODbL, © OpenStreetMap-Mitwirkende.

  2. Stadt Erlangen, Statistik und Stadtforschung — „Statistische Bezirke der
     Stadt Erlangen nach Straßenabschnitten" (Open Data, xlsx). Liefert das
     amtliche Straßenverzeichnis samt Zuordnung zum statistischen Bezirk.
     Lizenz: Datenlizenz Deutschland Namensnennung 2.0.

Das amtliche Verzeichnis ist die Namensautorität: Nur Straßennamen, die dort
stehen, werden später im Dokumentvolltext gesucht. Das hält Ortsfremdes
draußen (die Bounding-Box greift bewusst über die Stadtgrenze) und verhindert
Fehltreffer durch OSM-Namen, die zugleich Allerweltswörter sind.

Aufruf:  python tools/fetch_geodata.py [--out geo] [--skip-osm] [--skip-amt]
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import amtliche_geometrie as geom

# Overpass-Spiegel: der erste, der antwortet, gewinnt. Der Hauptserver
# quittiert Lastspitzen mit 504/429 — dann ist der nächste dran.
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
]

# „Statistische Bezirke der Stadt Erlangen nach Straßenabschnitten"
# Bei einer neuen Ausgabe hier den Dateinamen nachziehen (Übersicht:
# https://erlangen.de/aktuelles/opendata).
AMT_URL = ("https://erlangen.de/uwao-api/faila/files/bypath/Dokumente/Statistik/"
           "Statistik%20Open%20Data/bezirke_strassenabschnitte_2025.10.xlsx")

# Gebiete der Orts- und Stadtteilbeiräte (Esri-Shapefile, DHDN/Gauß-Krüger 4).
# Einzige amtliche Definition dieser Gebiete — die Stadt veröffentlicht sie
# nirgends in Textform, und die Satzung (140.00) nennt nur die Namen.
BEIRAT_URL = ("https://erlangen.de/uwao-api/faila/files/bypath/Dokumente/Statistik/"
              "Statistik%20Open%20Data/Elangen_2015_Vektorgeometrie_"
              "Stadtteilbeiratsgebiete.zip")

# Die Geometriedatei stammt von 2015 und trägt Arbeitsnamen: die
# Stadtteilbeiräte wurden erst am 27.07.2016 beschlossen und 2017
# konstituiert. Die Zuordnung auf die heutigen Namen nach § 1 der Satzung
# über Orts- und Stadtteilbeiräte.
#
# Die Nummerierung belegt die Übersetzung: der Plan, der seit 01.05.2026
# Bestandteil der Satzung ist (recht/orts-und-stadtteilbeiraete_…_plan_2026.pdf),
# führt dieselben Nummern wie die Geometriedatei von 2015 —
# 08 SB Innenstadt, 09 SB Alterlangen, 10 SB Ost, 11 SB Süd, 13 SB Büchenbach
# gegenüber 08 SB Zentrum/Nord, 09 SB Regnitz, 10 SB Ost, 11 SB Süd-Ost,
# 13 SB West. Die sieben Ortsbeiräte (01–07) tragen ohnehin unverändert
# ihre Namen.
#
# Einzige inhaltliche Änderung: die alte Nummer 12 „SB Tal/Anger/Bruck" ist
# entfallen und in 14 SB Anger und 15 SB Bruck geteilt. Eine getrennte
# Geometrie dafür veröffentlicht die Stadt nicht — der Plan ist eine
# Rasterkarte, und das Geodatenangebot führt die Beiratsgebiete weiterhin nur
# im Stand von 2015. Straßen in diesem Gebiet werden deshalb BEIDEN Beiräten
# zugeordnet: Wer nach Bruck filtert, bekommt das gemeinsame Gebiet Anger und
# Bruck und damit zu viel, aber nichts fehlt. Eine Grenze zu erfinden wäre die
# schlechtere Wahl. Sobald die Stadt getrennte Geometrie veröffentlicht, wird
# hier aus dem Paar ein einzelner Name je Gebiet.
BEIRAT_NAMEN = {
    "SB Zentrum/Nord": "Stadtteilbeirat Innenstadt",
    "SB Regnitz": "Stadtteilbeirat Alterlangen",
    "SB Ost": "Stadtteilbeirat Ost",
    "SB Süd-Ost": "Stadtteilbeirat Süd",
    "SB Südost": "Stadtteilbeirat Süd",
    "SB Tal/Anger/Bruck": ["Stadtteilbeirat Anger", "Stadtteilbeirat Bruck"],
    "SB West": "Stadtteilbeirat Büchenbach",
    "OB Eltersdorf": "Ortsbeirat Eltersdorf",
    "OB Frauenaurach": "Ortsbeirat Frauenaurach",
    "OB Dechsendorf": "Ortsbeirat Dechsendorf",
    "OB Hüttendorf": "Ortsbeirat Hüttendorf",
    "OB Kriegenbrunn": "Ortsbeirat Kriegenbrunn",
    "OB Tennenlohe": "Ortsbeirat Tennenlohe",
    "OB Kosbach/Häusling/Steudach": "Ortsbeirat Kosbach/Häusling/Steudach",
}

# Ab diesem Anteil gilt eine Straße als (auch) in einem Beirat liegend.
# Straßen auf einer Beiratsgrenze gehören zu beiden — die Kurt-Schumacher-
# Straße liegt zu 52 % in Süd und zu 48 % in Ost, und wer nach Ost filtert,
# will sie sehen.
BEIRAT_MIN_ANTEIL = 0.15

UA = "UVPA-Erlangen/1.0 (ehrenamtliches Dokumentenportal; +https://erlangen-kommunal.github.io/UVPA/)"

# Koordinaten auf 5 Nachkommastellen (~1 m). Genauer bringt für eine
# Übersichtskarte nichts, kostet aber ein Drittel der Dateigröße.
PRECISION = 5

XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def log(msg: str) -> None:
    print(msg, flush=True)


def http_get(url: str, timeout: int = 240) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ── OpenStreetMap ────────────────────────────────────────────────────────────

def overpass(query: str) -> dict:
    """Führt eine Overpass-Abfrage aus und probiert dabei die Spiegel durch."""
    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    last_err = None
    for mirror in OVERPASS_MIRRORS:
        for attempt in (1, 2):
            try:
                req = urllib.request.Request(
                    mirror, data=body,
                    headers={"User-Agent": UA,
                             "Content-Type": "application/x-www-form-urlencoded"})
                with urllib.request.urlopen(req, timeout=300) as r:
                    return json.loads(r.read().decode("utf-8"))
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
                last_err = e
                code = getattr(e, "code", None)
                log(f"  {mirror} → {code or e} (Versuch {attempt})")
                # 429/504 = überlastet; kurz warten und einmal nachfassen.
                if code in (429, 504) and attempt == 1:
                    time.sleep(20)
                else:
                    break
    raise RuntimeError(f"Kein Overpass-Spiegel erreichbar: {last_err}")


def road_class(tags: dict) -> str | None:
    """Tempo-Klasse eines Wegs — bestimmt Farbe und Legende auf der Karte."""
    if tags.get("highway") == "living_street":
        return "living"
    if tags.get("maxspeed") == "20":
        return "t20"
    if tags.get("maxspeed") == "30":
        return "t30"
    if "maxspeed:conditional" in tags:
        return "cond"
    return None


def fetch_roads(query: str) -> dict:
    data = overpass(query)
    features = []
    for el in data.get("elements", []):
        geom = el.get("geometry")
        if not geom:
            continue
        tags = el.get("tags", {})
        cls = road_class(tags)
        if cls is None:
            continue
        coords = [[round(p["lon"], PRECISION), round(p["lat"], PRECISION)] for p in geom]
        props = {"cls": cls}
        if tags.get("name"):
            props["name"] = tags["name"]
        # Die zeitliche Bedingung ist der eigentliche Informationsgehalt der
        # bedingten Begrenzungen ("30 @ (Mo-Fr 07:00-17:00)") — mitnehmen.
        if tags.get("maxspeed:conditional"):
            props["cond"] = tags["maxspeed:conditional"]
        features.append({"type": "Feature", "properties": props,
                         "geometry": {"type": "LineString", "coordinates": coords}})
    return {"type": "FeatureCollection", "features": features}


POI_KIND = [
    ("school", lambda t: t.get("amenity") == "school"),
    ("kindergarten", lambda t: t.get("amenity") == "kindergarten"),
    ("social", lambda t: t.get("amenity") == "social_facility"),
    ("playground", lambda t: t.get("leisure") == "playground"),
]


def fetch_pois(query: str) -> dict:
    data = overpass(query)
    features = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        kind = next((k for k, pred in POI_KIND if pred(tags)), None)
        if kind is None:
            continue
        center = el.get("center") or ({"lat": el.get("lat"), "lon": el.get("lon")})
        if center.get("lat") is None or center.get("lon") is None:
            continue
        props = {"kind": kind}
        if tags.get("name"):
            props["name"] = tags["name"]
        features.append({
            "type": "Feature", "properties": props,
            "geometry": {"type": "Point", "coordinates": [
                round(center["lon"], PRECISION), round(center["lat"], PRECISION)]},
        })
    return {"type": "FeatureCollection", "features": features}


# ── Amtliches Straßenverzeichnis (xlsx) ──────────────────────────────────────

def xlsx_rows(blob: bytes, sheet_name: str) -> list[dict[str, str]]:
    """Liest ein Arbeitsblatt als Liste von {Spaltenbuchstabe: Wert}.

    Bewusst ein Minimalparser statt openpyxl: die Datei ist simpel und der
    Wochen-Sync soll ohne zusätzliche pip-Abhängigkeit auskommen.
    """
    z = zipfile.ZipFile(io.BytesIO(blob))
    shared = [
        "".join(t.text or "" for t in si.iter(XLSX_NS + "t"))
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(XLSX_NS + "si")
    ]
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    rels = {r.get("Id"): r.get("Target")
            for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}

    target = None
    for sheet in wb.iter(XLSX_NS + "sheet"):
        if sheet.get("name") == sheet_name:
            target = rels[sheet.get(rel_ns + "id")]
            break
    if target is None:
        raise KeyError(f"Arbeitsblatt „{sheet_name}“ nicht in der Datei "
                       f"(vorhanden: {[s.get('name') for s in wb.iter(XLSX_NS + 'sheet')]})")

    path = "xl/" + target.lstrip("/").removeprefix("xl/")
    rows = []
    for row in ET.fromstring(z.read(path)).iter(XLSX_NS + "row"):
        cells = {}
        for c in row.findall(XLSX_NS + "c"):
            col = re.match(r"[A-Z]+", c.get("r")).group()
            v = c.find(XLSX_NS + "v")
            inline = c.find(XLSX_NS + "is")
            if c.get("t") == "s" and v is not None:
                val = shared[int(v.text)]
            elif inline is not None:
                val = "".join(t.text or "" for t in inline.iter(XLSX_NS + "t"))
            else:
                val = v.text if v is not None else ""
            cells[col] = (val or "").strip()
        rows.append(cells)
    return rows


def norm(s: str) -> str:
    """Vergleichsform eines Straßennamens (Unicode-Normalform, gestraffte Leerzeichen)."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", s)).strip()


def parse_amt(blob: bytes) -> dict:
    """xlsx → {name: {schluessel, bezirke:[{nr,name}], abschnitte:[…]}}."""
    rows = xlsx_rows(blob, "Nach Straßen")

    stand = ""
    for r in rows[:6]:
        m = re.search(r"Stand:\s*([\d.]+)", r.get("A", ""))
        if m:
            d, mo, y = m.group(1).rstrip(".").split(".")
            stand = f"{y}-{mo}-{d}"
            break

    streets: dict[str, dict] = {}
    bezirke: dict[str, str] = {}
    for r in rows:
        key, name, bez = r.get("A", ""), r.get("B", ""), r.get("K", "")
        # Datenzeilen tragen einen numerischen Straßenschlüssel; Titel-,
        # Kopf- und Leerzeilen fallen damit von selbst heraus.
        if not key.isdigit() or not name:
            continue
        name = norm(name)
        m = re.match(r"(\d+)\s+(.*)", bez)
        bez_nr, bez_name = (m.group(1), norm(m.group(2))) if m else ("", "")
        if bez_nr:
            bezirke[bez_nr] = bez_name

        entry = streets.setdefault(name, {"schluessel": key, "bezirke": [], "abschnitte": []})
        if bez_nr and bez_nr not in [b["nr"] for b in entry["bezirke"]]:
            entry["bezirke"].append({"nr": bez_nr, "name": bez_name})
        # Hausnummernbereiche: ungerade C–E, gerade G–I (D/F/H/J sind
        # Buchstabenzusätze wie „23B“). Nur mitschreiben, wenn belegt.
        abschnitt = {k: v for k, v in (
            ("u_von", r.get("C", "") + r.get("D", "")),
            ("u_bis", r.get("E", "") + r.get("F", "")),
            ("g_von", r.get("G", "") + r.get("H", "")),
            ("g_bis", r.get("I", "") + r.get("J", "")),
        ) if v}
        if abschnitt and bez_nr:
            abschnitt["bezirk"] = bez_nr
            entry["abschnitte"].append(abschnitt)

    return {
        "stand": stand,
        "quelle": "Stadt Erlangen, Statistik und Stadtforschung (Open Data)",
        "quelle_url": AMT_URL,
        "lizenz": "Datenlizenz Deutschland Namensnennung 2.0 (dl-de/by-2.0)",
        "bezirke": [{"nr": nr, "name": bezirke[nr]} for nr in sorted(bezirke)],
        "strassen": [dict(name=n, **v) for n, v in sorted(streets.items())],
    }


# ── Beiratsgebiete ───────────────────────────────────────────────────────────

# Alle benannten Straßen im Stadtgebiet. Diese Geometrie wird nur zur
# Zuordnung gebraucht und deshalb nicht abgelegt — im Repo landet allein das
# Ergebnis (Straße → Beirat).
NAMED_ROADS_QUERY = """
[out:json][timeout:180][bbox:49.52,10.92,49.65,11.10];
way["highway"]["name"];
out geom;
"""


def fetch_beiraete(out: Path) -> dict:
    """Beiratsgebiete umrechnen, als GeoJSON ablegen, Straßen zuordnen."""
    shapes, attrs = geom.load_zip(http_get(BEIRAT_URL))

    gebiete, features = [], []
    for rings_gk, attr in zip(shapes, attrs):
        roh = norm(attr.get("NAME", ""))
        namen = BEIRAT_NAMEN.get(roh, roh)
        if roh not in BEIRAT_NAMEN:
            log(f"  Warnung: unbekanntes Beiratsgebiet „{roh}“ — bleibt unbenannt.")
        # ein Gebiet kann für mehrere Beiräte stehen, solange keine getrennte
        # Geometrie vorliegt (derzeit Anger und Bruck)
        namen = [namen] if isinstance(namen, str) else list(namen)
        label = " / ".join(namen)
        rings = [[[round(v, PRECISION) for v in geom.gk4_to_wgs84(x, y)]
                  for x, y in ring] for ring in rings_gk]
        gebiete.append((namen, label, rings, geom.bbox(rings)))
        features.append({
            "type": "Feature",
            "properties": {"name": label, "beiraete": namen, "name_2015": roh,
                           "nummer": attr.get("NUMMER", "")},
            "geometry": {"type": "Polygon", "coordinates": rings},
        })
    write_json(out / "beiraete.geojson",
               {"type": "FeatureCollection", "features": features}, compact=True)

    def finde(lon: float, lat: float) -> str | None:
        for _namen, label, rings, (x0, y0, x1, y1) in gebiete:
            if x0 <= lon <= x1 and y0 <= lat <= y1 and geom.contains(rings, lon, lat):
                return label
        return None

    log("  Straßengeometrie für die Zuordnung …")
    roads = overpass(NAMED_ROADS_QUERY)
    treffer: dict[str, dict[str, int]] = {}
    for el in roads.get("elements", []):
        name = el.get("tags", {}).get("name")
        line = el.get("geometry")
        if not name or not line:
            continue
        # bis zu acht Stützpunkte je Weg: genug, um eine Straße zu erfassen,
        # die über eine Beiratsgrenze läuft, ohne jeden Knoten zu prüfen
        step = max(1, len(line) // 8)
        counts = treffer.setdefault(name, {})
        for p in line[::step]:
            b = finde(p["lon"], p["lat"])
            if b:
                counts[b] = counts.get(b, 0) + 1
    # Gebiets-Label → die Beiräte, für die es steht (meist genau einer)
    label_zu_beiraeten = {g[1]: g[0] for g in gebiete}
    return {"gebiete": sorted({n for g in gebiete for n in g[0]}),
            "treffer": treffer, "label_zu_beiraeten": label_zu_beiraeten}


def apply_beiraete(amt: dict, treffer: dict[str, dict[str, int]],
                   label_zu_beiraeten: dict[str, list[str]]) -> dict[str, int]:
    """Schreibt beirat/beiraete in jede Straße des amtlichen Verzeichnisses."""
    stats = {"eindeutig": 0, "mehrere": 0, "ohne": 0}
    for s in amt["strassen"]:
        counts = treffer.get(s["name"]) or {}
        total = sum(counts.values())
        if not total:
            stats["ohne"] += 1
            continue
        anteile = {b: n / total for b, n in counts.items()}
        top = max(anteile, key=anteile.get)
        s["beirat"] = top
        # Gebiete, in denen ein nennenswerter Teil der Straße liegt, auf die
        # zugehörigen Beiräte auflösen — ein Gebiet kann für zwei stehen.
        s["beiraete"] = [b for label, a in sorted(anteile.items(), key=lambda kv: -kv[1])
                         if a >= BEIRAT_MIN_ANTEIL
                         for b in label_zu_beiraeten.get(label, [label])]
        s["beirat_anteil"] = round(anteile[top], 3)
        # „eindeutig" heißt: die Straße liegt in genau einem Gebiet. Dass ein
        # Gebiet für zwei Beiräte steht, ist eine Lücke der Geodaten, kein
        # Grenzverlauf der Straße.
        stats["eindeutig" if len([a for a in anteile.values() if a >= BEIRAT_MIN_ANTEIL]) == 1
              else "mehrere"] += 1
    return stats


# ── Hauptlauf ────────────────────────────────────────────────────────────────

def write_json(path: Path, data, compact: bool) -> None:
    # GeoJSON kompakt (es wird nur maschinell gelesen und geht über die
    # Leitung), die kuratierten Verzeichnisse eingerückt und damit diffbar.
    text = json.dumps(data, ensure_ascii=False,
                      separators=(",", ":") if compact else None,
                      indent=None if compact else 2)
    path.write_text(text + ("" if compact else "\n"), encoding="utf-8")
    log(f"  → {path} ({path.stat().st_size / 1024:.0f} KB)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="geo", help="Zielverzeichnis (Vorgabe: geo)")
    ap.add_argument("--skip-osm", action="store_true", help="OpenStreetMap-Abruf auslassen")
    ap.add_argument("--skip-amt", action="store_true",
                    help="Amtliches Straßenverzeichnis nicht neu laden")
    ap.add_argument("--skip-beirat", action="store_true",
                    help="Beiratsgebiete und Straßenzuordnung auslassen")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    meta = {}
    if (out / "meta.json").exists():
        meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))

    if not args.skip_osm:
        log("OpenStreetMap: Tempo-30-Netz …")
        roads = fetch_roads((out / "tempo30.overpassql").read_text(encoding="utf-8"))
        write_json(out / "tempo30.geojson", roads, compact=True)

        log("OpenStreetMap: Einrichtungen …")
        pois = fetch_pois((out / "einrichtungen.overpassql").read_text(encoding="utf-8"))
        write_json(out / "einrichtungen.geojson", pois, compact=True)

        counts: dict[str, int] = {}
        for f in roads["features"]:
            counts[f["properties"]["cls"]] = counts.get(f["properties"]["cls"], 0) + 1
        for f in pois["features"]:
            k = f["properties"]["kind"]
            counts[k] = counts.get(k, 0) + 1
        meta["osm"] = {
            "abgerufen": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "quelle": "OpenStreetMap über Overpass-API",
            "lizenz": "ODbL, © OpenStreetMap-Mitwirkende",
            "wege": len(roads["features"]),
            "einrichtungen": len(pois["features"]),
            "klassen": counts,
        }
        log(f"  {len(roads['features'])} Wege, {len(pois['features'])} Einrichtungen: {counts}")

    if not args.skip_amt:
        log("Stadt Erlangen: Statistische Bezirke nach Straßenabschnitten …")
        amt = parse_amt(http_get(AMT_URL))

        if not args.skip_beirat:
            log("Stadt Erlangen: Gebiete der Orts- und Stadtteilbeiräte …")
            bei = fetch_beiraete(out)
            stats = apply_beiraete(amt, bei["treffer"], bei["label_zu_beiraeten"])
            amt["beiraete"] = bei["gebiete"]
            meta["beiraete"] = {
                "abgerufen": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "quelle": "Stadt Erlangen, Statistik und Stadtforschung (Open Data)",
                "quelle_url": BEIRAT_URL,
                "lizenz": "Datenlizenz Deutschland Namensnennung 2.0 (dl-de/by-2.0)",
                "stand_geometrie": "2015",
                "namen_nach": "Satzung über Orts- und Stadtteilbeiräte, Fassung ab 01.05.2026",
                "hinweis": ("Anger und Bruck sind seit 01.05.2026 getrennte Stadtteilbeiräte; "
                            "getrennte Geometrie liegt nicht vor, beide führen daher auf das "
                            "gemeinsame Gebiet."),
                "gebiete": len(bei["gebiete"]),
                "zuordnung": stats,
            }
            log(f"  {len(bei['gebiete'])} Gebiete · Straßen: {stats['eindeutig']} eindeutig, "
                f"{stats['mehrere']} über eine Grenze, {stats['ohne']} ohne Geometrie")

        write_json(out / "strassen.json", amt, compact=False)
        meta["amtlich"] = {
            "abgerufen": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "stand": amt["stand"],
            "quelle": amt["quelle"],
            "quelle_url": amt["quelle_url"],
            "lizenz": amt["lizenz"],
            "strassen": len(amt["strassen"]),
            "bezirke": len(amt["bezirke"]),
        }
        log(f"  {len(amt['strassen'])} Straßen, {len(amt['bezirke'])} statistische Bezirke "
            f"(Stand {amt['stand']})")

    write_json(out / "meta.json", meta, compact=False)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
