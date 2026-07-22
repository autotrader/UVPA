"""Tagesordnungspunkte benachbarter Gremien holen.

Warum nur Tagesordnungspunkte und keine Dokumente: Der Bauausschuss allein
brächte grob 4.500 PDFs und würde das Repo etwa verdoppeln. Die TOP-Titel des
Ratsinformationssystems sind dagegen aussagekräftig genug („Masterplanung
Universitätsmedizin", „Denkmalschutz; hier: Photovoltaik-Anlagen"), und je
Sitzung genügt ein einziger Request. Für die Frage „was lief dazu woanders?"
reicht das.

Erfasst werden Titel, TOP-Nummer, Datum, Gremium, Beschlusstext, die
Vorlagennummer (`kvonr`, verbindet mit beratungsfolge.json) und ein Deeplink.

Routine-Tagesordnungspunkte („Anfragen", „Mitteilungen zur Kenntnis" …) werden
nicht weggeworfen, sondern als `routine: true` markiert — wegwerfen hieße, eine
Vollständigkeit zu behaupten, die die Daten nicht hergeben. Das Frontend blendet
sie standardmäßig aus.

Nur Standardbibliothek. Der Lauf ist inkrementell: bereits erfasste Sitzungen
werden übersprungen, solange nicht --force gesetzt ist. Vergangene Sitzungen
ändern sich nicht mehr.

Aufruf:
    python tools/fetch_gremien_tops.py
    python tools/fetch_gremien_tops.py --force
"""

from __future__ import annotations

import argparse
import html
import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_JSON = REPO / "gremien_tops.json"

BASE = "https://ratsinfo.erlangen.de"
UA = "SBR-Infoportal/1.0 (ehrenamtlich; Kontakt ueber github.com/Erlangen-Kommunal)"

# ── Projektspezifisch: welche Nachbargremien interessieren ───────────────────
# UVPA ist stadtweit für Umwelt, Verkehr und Planung zuständig. Fachlich am
# nächsten liegen der Bauausschuss (überschneidende Bauthemen) und der Stadtrat
# (entscheidet über das, was der Ausschuss empfiehlt).
GREMIEN = {
    1: "Stadtrat",
    8: "Bauausschuss / Werkausschuss für den Entwässerungsbetrieb",
}

# Wahlperiode 2020–2026: 72 Monate ab Mai 2020. Für spätere Perioden anpassen.
WP_START_JAHR, WP_START_MONAT, WP_MONATE = 2020, 5, 72

# Wiederkehrende Formalpunkte ohne eigenen Sachgehalt. Anker auf Zeilenanfang,
# damit „Anfragen zur Verkehrssituation …" nicht mitgefangen wird.
ROUTINE = [
    re.compile(p, re.I) for p in (
        r"^Anfragen\b",
        r"^Mitteilung(en)? zur Kenntnis\b",
        r"^Bericht aus (der )?nicht ?öffentlicher? Sitzung",
        r"^Bearbeitungsstand (der )?Fraktionsanträge",
        r"^Genehmigung der Niederschrift",
        r"^Niederschrift(en)? (der|über)",
        r"^Verschiedenes$",
        r"^Bekanntgaben?$",
        r"^Einwohnerfrage(n|stunde)",
        r"^Beschlussüberwachung",
        r"^Strategisches Management",
    )
]

# ── Ortsbezug ────────────────────────────────────────────────────────────────
# Das amtliche Straßenverzeichnis ist die Namensautorität (geo/strassen.json).
# Verglichen wird über Wort-n-Gramme, NICHT über Teilzeichenketten: sonst gilt
# „Schallershofer Straße" als Treffer für „Hofer Straße". Normalisiert werden
# Umlaute, Bindestriche und Leerzeichen, damit die OSM-Schreibweise
# („Adenauerring") auf die amtliche („Adenauer-Ring") trifft.
MAX_STRASSEN_WORTE = 4  # längster amtlicher Name: „An der Weißen Marter"


def norm_strasse(s: str) -> str:
    s = s.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"), ("-", ""), (" ", ""), (".", "")):
        s = s.replace(a, b)
    return s


def lade_strassen() -> dict[str, str]:
    p = REPO / "geo" / "strassen.json"
    if not p.exists():
        print("Hinweis: geo/strassen.json fehlt — kein Straßenbezug.")
        return {}
    d = json.loads(p.read_text(encoding="utf-8"))
    namen = [s["name"] if isinstance(s, dict) else s for s in d.get("strassen", [])]
    namen += [s.get("amtliche_schreibweise") for s in d.get("strassen", [])
              if isinstance(s, dict) and s.get("amtliche_schreibweise")]
    namen += d.get("alle_namen", [])
    return {norm_strasse(n): n for n in namen if n and len(n) > 4}


def strassen_in(titel: str, nmap: dict[str, str]) -> list[str]:
    woerter = re.findall(r"[A-Za-zÄÖÜäöüß.\-]+", titel)
    out = set()
    for i in range(len(woerter)):
        for k in range(1, MAX_STRASSEN_WORTE + 1):
            if i + k > len(woerter):
                break
            treffer = nmap.get(norm_strasse("".join(woerter[i:i + k])))
            if treffer:
                out.add(treffer)
    return sorted(out)


TOP_ROW_RE = re.compile(r'(?is)<tr[^>]*class="smc-t-r-l"[^>]*>(.*?)</tr>')
NUM_RE = re.compile(r'(?is)class="tofnum".*?<span[^>]*>(.*?)</span>')
TITLE_RE = re.compile(r'(?is)href="to0050\.asp\?__ktonr=(\d+)"[^>]*class="[^"]*smc_datatype_to[^"]*"[^>]*>(.*?)</a>')
VORLAGE_RE = re.compile(r'(?is)href="vo0050\.asp\?__kvonr=(\d+)"')
BESCHLUSS_RE = re.compile(r'(?is)smc_field_smcdv0_box\d+_beschluss[^>]*>(.*?)</p>')


def text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def hole(url: str, versuche: int = 3) -> str:
    letzter = None
    for n in range(versuche):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            letzter = e
            time.sleep(1.5 * (n + 1))
    raise RuntimeError(f"{url}: {letzter}")


def sitzungen(kgrnr: int) -> list[tuple[str, str]]:
    """(ksinr, ISO-Datum) aller Sitzungen des Gremiums in der Wahlperiode."""
    url = (f"{BASE}/si0046.asp?__cjahr={WP_START_JAHR}&__cmonat={WP_START_MONAT}"
           f"&__canz={WP_MONATE}&smccont=85&__osidat=d&__kgsgrnr={kgrnr}&__cselect=65536")
    seite = hole(url)
    out = []
    for row in TOP_ROW_RE.findall(seite):
        m = re.search(r"si0057\.asp\?__ksinr=(\d+)", row)
        d = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", row)
        if m and d:
            out.append((m.group(1), f"{d.group(3)}-{d.group(2)}-{d.group(1)}"))
    return list(dict.fromkeys(out))


def tops(ksinr: str) -> list[dict]:
    seite = hole(f"{BASE}/si0057.asp?__ksinr={ksinr}")
    out = []
    for row in TOP_ROW_RE.findall(seite):
        t = TITLE_RE.search(row)
        if not t:
            continue
        titel = text(t.group(2))
        if len(titel) < 4:
            continue
        nummer = NUM_RE.search(row)
        vorlage = VORLAGE_RE.search(row)
        beschluss = BESCHLUSS_RE.search(row)
        out.append({
            "ktonr": t.group(1),
            "top": text(nummer.group(1)) if nummer else "",
            "titel": titel,
            "kvonr": vorlage.group(1) if vorlage else "",
            "beschluss": text(beschluss.group(1)).removeprefix("Beschluss:").strip() if beschluss else "",
            "routine": any(r.search(titel) for r in ROUTINE),
        })
    # Ein TOP kann in der Tabelle mehrfach auftauchen (Unterpunkte je Vorlage).
    return list({t["ktonr"]: t for t in out}.values())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="auch bereits erfasste Sitzungen neu holen")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    bestand = {}
    if OUT_JSON.exists() and not args.force:
        alt = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        for t in alt.get("tops", []):
            bestand.setdefault(t["ksinr"], []).append(t)

    alle: list[dict] = []
    for kgrnr, name in GREMIEN.items():
        sitz = sitzungen(kgrnr)
        offen = [s for s in sitz if s[0] not in bestand]
        print(f"{name}: {len(sitz)} Sitzungen, {len(offen)} neu abzurufen", flush=True)

        for ksinr, _ in sitz:
            if ksinr in bestand:
                alle.extend(bestand[ksinr])

        if offen:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                for (ksinr, datum), ts in zip(offen, pool.map(lambda s: tops(s[0]), offen)):
                    for t in ts:
                        alle.append({
                            "gremium": name, "kgrnr": kgrnr, "datum": datum, "ksinr": ksinr,
                            **t,
                            "url": f"{BASE}/to0050.asp?__ktonr={t['ktonr']}",
                        })

    # Ortsbezug nachtragen (auch für Einträge aus dem Bestand, damit ein
    # aufgefrischtes Straßenverzeichnis überall durchschlägt).
    nmap = lade_strassen()
    for t in alle:
        t["strassen"] = strassen_in(t["titel"], nmap) if nmap else []

    alle.sort(key=lambda t: (t["datum"], t["gremium"], t["top"]), reverse=True)
    OUT_JSON.write_text(json.dumps({
        "stand": date.today().isoformat(),
        "wahlperiode": {"label": "2020 – 2026", "von": "2020-05-01", "bis": "2026-04-30"},
        "quelle": "Ratsinformationssystem der Stadt Erlangen (SessionNet)",
        "gremien": {str(k): v for k, v in GREMIEN.items()},
        "tops": alle,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    routine = sum(1 for t in alle if t["routine"])
    mit_vorlage = sum(1 for t in alle if t["kvonr"])
    print(f"\n{OUT_JSON.name}: {len(alle)} Tagesordnungspunkte "
          f"({routine} Routine, {len(alle) - routine} inhaltlich, {mit_vorlage} mit Vorlage).")


if __name__ == "__main__":
    main()
