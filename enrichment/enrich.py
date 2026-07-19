#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM-Anreicherung der UVPA-Sitzungsdokumente (Phase 2) — Google Gemini.

Erzeugt pro Dokument eine Markdown-Datei unter enrichment/docs/<pfad>.md mit
YAML-Frontmatter (themen, modell, erstellt) und einer deutschen Kurz-
zusammenfassung. Die Themen kommen ausschließlich aus der kuratierten
Taxonomie in enrichment/themen.md (eine Quelle der Wahrheit).

Läuft inkrementell: Dokumente mit vorhandener .md-Datei werden übersprungen —
der wöchentliche Sync bezahlt nur neue Dokumente. Jede fertige Zusammenfassung
wird sofort geschrieben (absturzsicher, einfach neu starten).

Aufruf:
    python enrichment/enrich.py [--limit N] [--dry-run] [--model M] [--workers N]

Umgebung:
    GEMINI_API_KEY  erforderlich (außer bei --dry-run). Unter Windows wird
    zusätzlich die systemweite Umgebungsvariable (Machine) gelesen, falls die
    Shell sie nicht geerbt hat.
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS_OUT = REPO / "enrichment" / "docs"
THEMEN_MD = REPO / "enrichment" / "themen.md"
INDEX_JSON = REPO / "index.json"

DEFAULT_MODEL = "gemini-3.1-flash-lite"
MAX_OUTPUT_TOKENS = 1200
TEXT_CAP_DEFAULT = 12_000   # Zeichen Dokumenttext pro Anfrage
TEXT_CAP_LONG = 20_000      # für NI/EI/SU (decken ganze Sitzungen ab)

TYPE_INSTRUCTIONS = {
    "VO": "Beschlussvorlage/Mitteilung: Worum geht es, was wird beantragt oder "
          "vorgeschlagen, was soll entschieden werden?",
    "BL": "Beschluss/Beratungsergebnis: Was wurde entschieden oder zur "
          "Kenntnis genommen?",
    "NI": "Niederschrift einer ganzen Sitzung: Fasse die wichtigsten Beschlüsse "
          "und Diskussionspunkte über alle Tagesordnungspunkte zusammen "
          "(hier sind 4-6 Sätze erlaubt).",
    "EI": "Einladung mit Tagesordnung: Nenne die inhaltlich wichtigsten "
          "Tagesordnungspunkte der Sitzung.",
    "SU": "Sitzungsunterlagen-Paket: Nenne die inhaltlich wichtigsten "
          "Tagesordnungspunkte der Sitzung.",
    "":   "Anlage: Was für ein Dokument ist das (Plan, Karte, Bericht, "
          "Präsentation, Stellungnahme ...) und was zeigt bzw. enthält es?",
}


def get_api_key() -> str | None:
    """GEMINI_API_KEY aus der Umgebung; unter Windows auch aus der Machine-Ebene."""
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            ) as k:
                value, _ = winreg.QueryValueEx(k, "GEMINI_API_KEY")
                return value or None
        except OSError:
            return None
    return None


def load_themen() -> list[str]:
    """Sachthemen aus der kuratierten Tabelle in themen.md (Quelle der Wahrheit)."""
    themen = []
    in_sach = False
    for line in THEMEN_MD.read_text(encoding="utf-8").splitlines():
        if line.startswith("## Sachthemen"):
            in_sach = True
            continue
        if in_sach and line.startswith("## "):
            break
        m = re.match(r"^\|\s*\d+\s*\|\s*(.+?)\s*\|", line)
        if in_sach and m:
            themen.append(m.group(1))
    if len(themen) < 10:
        sys.exit(f"Fehler: nur {len(themen)} Themen aus {THEMEN_MD} geparst — Tabellenformat prüfen.")
    return themen


def collect_docs() -> list[dict]:
    """Alle Dokumente aus index.json mit vorhandener PDF, die noch keine .md haben."""
    sessions = json.loads(INDEX_JSON.read_text(encoding="utf-8"))
    jobs, skipped_done, skipped_missing = [], 0, 0

    def add(doc, folder, session, top=None):
        nonlocal skipped_done, skipped_missing
        rel = f"{folder}/{doc['filename']}"
        pdf = REPO / folder / doc["filename"]
        out = DOCS_OUT / f"{rel}.md"
        if out.exists():
            skipped_done += 1
            return
        if not pdf.exists():
            skipped_missing += 1
            return
        jobs.append({
            "rel": rel, "pdf": pdf, "out": out,
            "type_code": doc.get("type_code", ""),
            "title": doc.get("title", ""),
            "date": session["date"],
            "top_title": (top or {}).get("title", ""),
            "top_nr": (top or {}).get("top_nr", ""),
        })

    for s in sessions:
        for d in s["header_docs"]:
            add(d, s["folder"], s)
        for t in s["tops"]:
            for d in t["docs"]:
                add(d, t["folder"], s, t)

    print(f"Dokumente: {len(jobs)} offen, {skipped_done} bereits angereichert, "
          f"{skipped_missing} ohne PDF im Repo.")
    return jobs


def extract_text(pdf: Path, cap: int) -> str:
    import pypdf
    try:
        reader = pypdf.PdfReader(str(pdf))
        parts = []
        total = 0
        for page in reader.pages:
            t = page.extract_text() or ""
            parts.append(t)
            total += len(t)
            if total >= cap:
                break
        return "\n".join(parts)[:cap].strip()
    except Exception:
        return ""


def build_user_content(job: dict, text: str) -> str:
    tc = job["type_code"] if job["type_code"] in TYPE_INSTRUCTIONS else ""
    ctx = [f"Dokumenttyp-Anweisung: {TYPE_INSTRUCTIONS[tc]}",
           f"Sitzungsdatum: {job['date']}"]
    if job["top_title"]:
        ctx.append(f"Tagesordnungspunkt: {job['top_nr']} {job['top_title']}")
    ctx.append(f"Dokumenttitel: {job['title']}")
    return "\n".join(ctx) + f"\n\n--- Dokumenttext (ggf. gekürzt) ---\n{text}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max. Anzahl Dokumente")
    ap.add_argument("--dry-run", action="store_true", help="nur zählen/schätzen, keine API-Aufrufe")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--workers", type=int, default=6, help="parallele Anfragen")
    args = ap.parse_args()

    themen = load_themen()
    print(f"Taxonomie: {len(themen)} Themen aus {THEMEN_MD.name}.")

    jobs = collect_docs()
    if args.limit:
        jobs = jobs[: args.limit]
    if not jobs:
        print("Nichts zu tun.")
        return

    system_prompt = (
        "Du bereitest Dokumente des Umwelt-, Verkehrs- und Planungsausschusses (UVPA) "
        "der Stadt Erlangen für eine Dokumentensuche auf. Die Nutzer sind kommunal-"
        "politische Beiräte ohne IT-Kenntnisse.\n\n"
        "Liefere für das Dokument:\n"
        "1. zusammenfassung: 2-4 Sätze auf Deutsch (bei Niederschriften bis 6). "
        "Konkret und informativ: Worum geht es, was wird beantragt/beschlossen/mitgeteilt, "
        "welche Orte oder Projekte sind betroffen. Keine Floskeln wie "
        "\"Das Dokument behandelt ...\" — direkt zur Sache.\n"
        "2. themen: 0 bis 4 wirklich zutreffende Themen, ausschließlich aus dieser Liste:\n"
        + "\n".join(f"- {t}" for t in themen)
    )
    response_schema = {
        "type": "object",
        "properties": {
            "zusammenfassung": {"type": "string"},
            "themen": {"type": "array", "items": {"type": "string", "enum": themen}},
        },
        "required": ["zusammenfassung", "themen"],
    }

    print("Extrahiere PDF-Texte …")
    tasks = []
    no_text = 0
    for i, job in enumerate(jobs):
        cap = TEXT_CAP_LONG if job["type_code"] in ("NI", "EI", "SU") else TEXT_CAP_DEFAULT
        text = extract_text(job["pdf"], cap)
        if not text:
            no_text += 1
            continue
        tasks.append((job, build_user_content(job, text)))
        if (i + 1) % 500 == 0:
            print(f"  … {i + 1}/{len(jobs)}", flush=True)

    est_chars = sum(len(c) for _, c in tasks)
    est_cost = (est_chars / 4 + len(tasks) * 700) / 1e6 * 0.10 \
             + len(tasks) * 250 / 1e6 * 0.40
    print(f"{len(tasks)} Anfragen ({no_text} ohne extrahierbaren Text übersprungen). "
          f"Geschätzte Kosten: ~{est_cost:.2f} USD ({args.model}).")

    if args.dry_run:
        print("Dry-Run — keine API-Aufrufe.")
        return

    api_key = get_api_key()
    if not api_key:
        sys.exit("Fehler: GEMINI_API_KEY nicht gefunden (Umgebung + Machine-Ebene geprüft).")

    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types

    client = genai.Client(api_key=api_key)
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        response_schema=response_schema,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        temperature=0.2,
    )

    lock = threading.Lock()
    done = {"ok": 0, "err": 0}
    t0 = time.time()

    def process(job: dict, content: str) -> None:
        for attempt in range(6):
            try:
                resp = client.models.generate_content(
                    model=args.model, contents=content, config=config)
                data = json.loads(resp.text)
                break
            except (json.JSONDecodeError, TypeError):
                if attempt >= 2:
                    raise
                time.sleep(2)
            except genai_errors.APIError as e:
                if e.code in (429, 500, 503) and attempt < 5:
                    time.sleep(15 if e.code == 429 else 2 ** attempt)
                    continue
                raise

        out: Path = job["out"]
        out.parent.mkdir(parents=True, exist_ok=True)
        frontmatter = (
            "---\n"
            f"themen: {json.dumps(data.get('themen', []), ensure_ascii=False)}\n"
            f"modell: {args.model}\n"
            f"erstellt: {date.today().isoformat()}\n"
            "---\n\n"
        )
        out.write_text(frontmatter + data.get("zusammenfassung", "").strip() + "\n",
                       encoding="utf-8")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process, job, content): job for job, content in tasks}
        for fut in as_completed(futures):
            job = futures[fut]
            with lock:
                try:
                    fut.result()
                    done["ok"] += 1
                except Exception as exc:
                    done["err"] += 1
                    print(f"  FEHLER {job['rel']}: {type(exc).__name__}: {str(exc)[:120]}",
                          flush=True)
                n = done["ok"] + done["err"]
                if n % 100 == 0:
                    rate = n / max(time.time() - t0, 1)
                    eta = (len(tasks) - n) / max(rate, 0.01) / 60
                    print(f"  … {n}/{len(tasks)} ({rate:.1f}/s, Rest ~{eta:.0f} min)",
                          flush=True)

    print(f"Fertig: {done['ok']} Zusammenfassungen geschrieben, {done['err']} Fehler "
          f"(werden beim nächsten Lauf erneut versucht). "
          f"Dauer: {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
