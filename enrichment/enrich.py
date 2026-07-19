#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM-Anreicherung der UVPA-Sitzungsdokumente (Phase 2).

Erzeugt pro Dokument eine Markdown-Datei unter enrichment/docs/<pfad>.md mit
YAML-Frontmatter (themen, modell, erstellt) und einer deutschen Kurz-
zusammenfassung. Die Themen kommen ausschließlich aus der kuratierten
Taxonomie in enrichment/themen.md (eine Quelle der Wahrheit).

Läuft inkrementell: Dokumente mit vorhandener .md-Datei werden übersprungen.
Nutzt die Message-Batches-API (50 % günstiger); das Modell ist claude-haiku-4-5
(vom Projektinhaber freigegebene Kostenbasis).

Aufruf:
    python enrichment/enrich.py [--limit N] [--dry-run]

Umgebung:
    ANTHROPIC_API_KEY  erforderlich (außer bei --dry-run)
"""

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS_OUT = REPO / "enrichment" / "docs"
THEMEN_MD = REPO / "enrichment" / "themen.md"
INDEX_JSON = REPO / "index.json"
STATE_FILE = REPO / "enrichment" / ".batch_state.json"

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 1200
BATCH_CHUNK = 2000          # Requests pro Batch
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


def build_request(job: dict, text: str, system_prompt: str, schema: dict) -> dict:
    tc = job["type_code"] if job["type_code"] in TYPE_INSTRUCTIONS else ""
    ctx = [f"Dokumenttyp-Anweisung: {TYPE_INSTRUCTIONS[tc]}",
           f"Sitzungsdatum: {job['date']}"]
    if job["top_title"]:
        ctx.append(f"Tagesordnungspunkt: {job['top_nr']} {job['top_title']}")
    ctx.append(f"Dokumenttitel: {job['title']}")
    user = "\n".join(ctx) + f"\n\n--- Dokumenttext (ggf. gekürzt) ---\n{text}"

    return {
        "custom_id": job["custom_id"],
        "params": {
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "system": system_prompt,
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
            "messages": [{"role": "user", "content": user}],
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max. Anzahl Dokumente")
    ap.add_argument("--dry-run", action="store_true", help="nur zählen/schätzen, keine API-Aufrufe")
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
    schema = {
        "type": "object",
        "properties": {
            "zusammenfassung": {"type": "string"},
            "themen": {"type": "array", "items": {"type": "string", "enum": themen}},
        },
        "required": ["zusammenfassung", "themen"],
        "additionalProperties": False,
    }

    print("Extrahiere PDF-Texte …")
    requests_list = []
    manifest = {}
    no_text = 0
    for i, job in enumerate(jobs):
        cap = TEXT_CAP_LONG if job["type_code"] in ("NI", "EI", "SU") else TEXT_CAP_DEFAULT
        text = extract_text(job["pdf"], cap)
        if not text:
            no_text += 1
            continue
        job["custom_id"] = "d" + hashlib.sha1(job["rel"].encode()).hexdigest()[:20]
        manifest[job["custom_id"]] = job
        requests_list.append(build_request(job, text, system_prompt, schema))
        if (i + 1) % 500 == 0:
            print(f"  … {i + 1}/{len(jobs)}")

    est_chars = sum(len(r["params"]["messages"][0]["content"]) for r in requests_list)
    est_in_tok = est_chars / 3.2 + len(requests_list) * 700   # grob: Text + Systemprompt
    est_cost = est_in_tok / 1e6 * 1.0 * 0.5 + len(requests_list) * 200 / 1e6 * 5.0 * 0.5
    print(f"{len(requests_list)} Anfragen ({no_text} ohne extrahierbaren Text übersprungen). "
          f"Geschätzte Batch-Kosten: ~{est_cost:.2f} USD ({MODEL}, 50% Batch-Rabatt).")

    if args.dry_run:
        print("Dry-Run — keine API-Aufrufe.")
        return

    import anthropic
    client = anthropic.Anthropic()

    batch_ids = []
    for start in range(0, len(requests_list), BATCH_CHUNK):
        chunk = requests_list[start:start + BATCH_CHUNK]
        batch = client.messages.batches.create(requests=chunk)
        batch_ids.append(batch.id)
        print(f"Batch {batch.id} eingereicht ({len(chunk)} Anfragen).")

    STATE_FILE.write_text(json.dumps({
        "batch_ids": batch_ids,
        "manifest": {cid: str(j["rel"]) for cid, j in manifest.items()},
    }, ensure_ascii=False), encoding="utf-8")

    # ── Polling + Ergebnisse schreiben ──────────────────────────────────────
    written, errored = 0, 0
    for batch_id in batch_ids:
        while True:
            b = client.messages.batches.retrieve(batch_id)
            if b.processing_status == "ended":
                break
            counts = b.request_counts
            print(f"  {batch_id}: {counts.processing} in Arbeit, "
                  f"{counts.succeeded} fertig … warte 60 s")
            time.sleep(60)

        for result in client.messages.batches.results(batch_id):
            job = manifest.get(result.custom_id)
            if job is None:
                continue
            if result.result.type != "succeeded":
                errored += 1
                continue
            msg = result.result.message
            raw = next((blk.text for blk in msg.content if blk.type == "text"), "")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                errored += 1
                continue
            out: Path = job["out"]
            out.parent.mkdir(parents=True, exist_ok=True)
            frontmatter = (
                "---\n"
                f"themen: {json.dumps(data.get('themen', []), ensure_ascii=False)}\n"
                f"modell: {MODEL}\n"
                f"erstellt: {date.today().isoformat()}\n"
                "---\n\n"
            )
            out.write_text(frontmatter + data.get("zusammenfassung", "").strip() + "\n",
                           encoding="utf-8")
            written += 1

    print(f"Fertig: {written} Zusammenfassungen geschrieben, {errored} Fehler "
          f"(werden beim nächsten Lauf erneut versucht).")


if __name__ == "__main__":
    main()
