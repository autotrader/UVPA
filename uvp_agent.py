#!/usr/bin/env python3
"""
UVP Document Agent — Claude AI agent for the Umwelt-, Verkehrs- und Planungsausschuss
(Werkausschuss EB77) of the City of Erlangen.

Documents are indexed per session and per Tagesordnungspunkt (TOP), then stored in:
  {session_date}/
    EI_Einladung.pdf
    NI_Niederschrift.pdf
    TOP_Oe05_771-031-2025_EB77-Wirtschaftsplan/
      BL_Beschluss.pdf
      VO_Beschlussvorlage.pdf
      WiPlan_2026.pdf

Usage:
    python uvp_agent.py [--refresh]
    python uvp_agent.py --compress   # shrink already-downloaded PDFs >=10MB in place

Environment:
    ANTHROPIC_API_KEY  required

Optional:
    pip install pypdf     enables PDF text extraction
    pip install pymupdf   enables automatic compression of PDFs >=10MB (see COMPRESS_MAX_BYTES);
                           newly downloaded oversized files are compressed automatically, since
                           the pre-commit hook (.githooks/pre-commit) rejects anything >=10MB
"""

import argparse
import html as html_lib
import json
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

import requests

# ── Configuration ─────────────────────────────────────────────────────────────

BASE_URL = "https://ratsinfo.erlangen.de"
COMMITTEE_NUM = 15  # Umwelt-, Verkehrs- und Planungsausschuss / Werkausschuss EB77
COMMITTEE_NAME = "Umwelt-, Verkehrs- und Planungsausschuss"
DOWNLOAD_DIR = Path(__file__).parent
INDEX_FILE = DOWNLOAD_DIR / "index.json"
SCRAPE_YEARS = range(2020, 2027)
MODEL = "claude-opus-4-8"
MAX_TOKENS = 4096

# Kept under the pre-commit hook's 10 MiB cutoff (.githooks/pre-commit) with headroom.
COMPRESS_MAX_BYTES = 9 * 1024 * 1024
COMPRESS_ATTEMPTS = [  # (max image dimension px, JPEG quality) — escalating aggressiveness
    (1600, 65),
    (1200, 55),
    (900, 40),
    (700, 30),
]

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

SYSTEM_PROMPT = """\
You are a helpful assistant for the Umwelt-, Verkehrs- und Planungsausschuss
(Environment, Traffic and Planning Committee / Werkausschuss EB77) of the City of
Erlangen, Germany.

You can access official committee documents from the city's Ratsinformationssystem.
Documents are organized by session date and Tagesordnungspunkt (agenda item / TOP).
Each TOP may have a Vorlage (proposal) number like "611/251/2025".

Document types:
- EI  Einladung: Invitation / agenda before the meeting
- NI  Niederschrift: Official minutes recording decisions
- SU  Sitzungsunterlagen: Combined session document package
- BL  Beschluss: Decision / resolution text
- VO  Vorlage / Beschlussvorlage: Proposal document
- (no code)  Anlage: Appendix / supporting document

Use your tools to search, download, and read documents when answering questions.
Respond in the same language the user uses (German or English).\
"""

# ── Helpers ───────────────────────────────────────────────────────────────────

def _decode(s: str) -> str:
    """HTML-unescape and collapse whitespace."""
    return html_lib.unescape(re.sub(r"\s+", " ", s)).strip()


def _sanitize(name: str, max_len: int = 60) -> str:
    """Convert a title to a safe filename fragment."""
    for src, dst in [
        ("ä", "ae"), ("ö", "oe"), ("ü", "ue"),
        ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss"),
    ]:
        name = name.replace(src, dst)
    name = re.sub(r"[^\w\s\-_]", "", name)
    name = re.sub(r"[\s_]+", "_", name).strip("_")
    return name[:max_len].rstrip("_")


def _iso_date(text: str) -> str:
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else "unknown"


def _top_nr_safe(top_nr: str) -> str:
    """'Ö 5' → 'Oe05', 'Ö 8.1' → 'Oe08-1'"""
    m = re.match(r"[ÖO]\s*(\d+)(?:[.\-](\d+))?", top_nr.strip())
    if not m:
        return _sanitize(top_nr)[:10]
    main = int(m.group(1))
    return f"Oe{main:02d}-{m.group(2)}" if m.group(2) else f"Oe{main:02d}"


def _vorlage_safe(v: str) -> str:
    return v.replace("/", "-")


def _unique_filename(filename: str, used: set) -> str:
    """Append _2, _3 … if filename already taken in this folder."""
    if filename not in used:
        used.add(filename)
        return filename
    stem, ext = (filename.rsplit(".", 1) if "." in filename else (filename, ""))
    i = 2
    while True:
        candidate = f"{stem}_{i}.{ext}" if ext else f"{stem}_{i}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        i += 1


# ── HTML Row Parser (for si0046 year listing) ─────────────────────────────────

class _RowParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list = []
        self._row: list = []
        self._cell: list = []
        self._in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._in_cell, self._cell = True, []
        elif tag == "a" and self._in_cell:
            href = dict(attrs).get("href", "")
            if href:
                self._cell.append(("link_start", href))

    def handle_data(self, data):
        if self._in_cell:
            self._cell.append(("text", data))

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self._in_cell = False
            self._row.append(self._cell)
        elif tag == "tr" and self._row:
            self.rows.append(self._row)
        elif tag == "a" and self._in_cell:
            self._cell.append(("link_end", ""))


# ── Session Page Parser (si0057.asp) ──────────────────────────────────────────

def _extract_top_docs(sidocs_html: str) -> list[dict]:
    """Extract document entries from one TOP's sidocs cell."""
    docs = []
    used_filenames: set = set()

    for div in re.split(r'<div\s+id="smcy\d+"', sidocs_html)[1:]:
        m_type = re.search(r'smc-doc-dakurz[^>]*>([A-Z0-9]+)<', div)
        type_code = m_type.group(1).strip() if m_type else ""

        m_fid = re.search(r'getfile\.asp\?id=(\d+)', div)
        if not m_fid:
            continue
        fid = m_fid.group(1)

        m_t = re.search(
            r'class="smce-a-u smc-text-block[^"]*"[^>]*>\s*([^<]+?)\s*</a>', div
        )
        title = _decode(m_t.group(1)) if m_t else f"doc_{fid}"

        safe = _sanitize(title)
        prefix = f"{type_code}_" if type_code else ""
        base_filename = f"{prefix}{safe}.pdf"
        filename = _unique_filename(base_filename, used_filenames)

        docs.append({
            "file_id": fid,
            "title": title,
            "type_code": type_code,
            "filename": filename,
            "href": f"getfile.asp?id={fid}&type=do",
        })

    return docs


def _parse_session_page(html: str, date: str) -> dict:
    """Parse a si0057.asp session page into header_docs and tops."""
    tbody_pos = html.find("<tbody>")

    # ── Header documents (before the TOP table) ───────────────────────────────
    header_html = html[:tbody_pos] if tbody_pos != -1 else html
    header_docs = []
    seen_fids: set = set()
    used_filenames: set = set()

    for href, fid, raw_title in re.findall(
        r'href="(getfile\.asp\?id=(\d+)[^"]+)"[^>]*>\s*([^<]+?)\s*</a>',
        header_html,
    ):
        if fid in seen_fids:
            continue
        seen_fids.add(fid)
        title = _decode(raw_title)
        tl = title.lower()
        if "einladung" in tl:
            tc = "EI"
        elif "niederschrift" in tl:
            tc = "NI"
        elif "sitzung" in tl:
            tc = "SU"
        else:
            tc = ""
        safe = _sanitize(title)
        base_fn = (f"{tc}_" if tc else "") + safe + ".pdf"
        filename = _unique_filename(base_fn, used_filenames)
        header_docs.append({
            "file_id": fid,
            "title": title,
            "type_code": tc,
            "filename": filename,
            "href": href,
        })

    if tbody_pos == -1:
        return {"header_docs": header_docs, "tops": []}

    # ── TOP rows ──────────────────────────────────────────────────────────────
    tbody = html[tbody_pos:]
    rows = re.split(r"<tr\b[^>]*>", tbody)
    tops = []

    for row in rows[1:]:
        if re.search(r'class="totrenn"', row):
            continue

        # TOP number badge
        m_num = re.search(
            r'class="tofnum"[^>]*>.*?<span[^>]*>(.*?)</span>', row, re.S
        )
        if not m_num:
            continue
        top_nr_raw = _decode(m_num.group(1)).strip()
        if not re.match(r"[ÖO]", top_nr_raw):
            continue

        # TOP title — link text may contain <br />, so use .*? with re.S
        m_title = re.search(
            r'href="to0050\.asp\?__ktonr=(\d+)"[^>]*>(.*?)</a>', row, re.S
        )
        if m_title:
            ktonr = m_title.group(1)
            top_title = _decode(re.sub(r"<[^>]+>", " ", m_title.group(2)))
        else:
            # Some sessions render TOP titles without a ktonr link
            m_div = re.search(
                r'smc-card-header-title-simple[^>]*>(.*?)</div>', row, re.S
            )
            if not m_div:
                continue
            ktonr = ""
            top_title = _decode(re.sub(r"<[^>]+>", " ", m_div.group(1)))

        # Vorlage
        m_vo = re.search(r'href="vo0050\.asp\?__kvonr=(\d+)"[^>]*>([^<]+)</a>', row)
        kvonr = m_vo.group(1) if m_vo else ""
        vorlage_nr = _decode(m_vo.group(2)) if m_vo else ""

        # Build TOP subfolder name
        nr_safe = _top_nr_safe(top_nr_raw)
        vo_safe = _vorlage_safe(vorlage_nr) if vorlage_nr else ""
        t_safe = _sanitize(top_title, 45)
        parts = ["TOP", nr_safe] + ([vo_safe] if vo_safe else []) + [t_safe]
        top_folder = f"{date}/" + "_".join(parts)

        # Documents
        m_sidocs = re.search(r'class="[^"]*sidocs[^"]*"[^>]*>(.*?)(?:</td>|$)', row, re.S)
        docs = _extract_top_docs(m_sidocs.group(1)) if m_sidocs else []

        tops.append({
            "top_nr": top_nr_raw,
            "top_nr_safe": nr_safe,
            "title": top_title,
            "ktonr": ktonr,
            "vorlage_nr": vorlage_nr,
            "kvonr": kvonr,
            "folder": top_folder,
            "docs": docs,
        })

    return {"header_docs": header_docs, "tops": tops}


# ── Scraping ──────────────────────────────────────────────────────────────────

def _scrape_year_sessions(
    http: requests.Session, year: int
) -> list[tuple[str, str]]:
    """Return [(date_iso, ksinr), …] for all sessions in a given year."""
    url = (
        f"{BASE_URL}/si0046.asp?__cjahr={year}&__cmonat=1&__canz=12"
        f"&smccont=85&__osidat=d&__kgsgrnr={COMMITTEE_NUM}&__cselect=65536"
    )
    try:
        r = http.get(url, headers=HTTP_HEADERS, timeout=30)
    except requests.RequestException:
        return []
    if r.status_code != 200:
        return []
    r.encoding = "iso-8859-1"

    parser = _RowParser()
    parser.feed(r.text)

    sessions = []
    for row in parser.rows:
        ksinr = None
        for cell in row:
            for kind, val in cell:
                if kind == "link_start" and "si0057.asp" in val:
                    m = re.search(r"__ksinr=(\d+)", val)
                    if m:
                        ksinr = m.group(1)
                        break
        if not ksinr:
            continue
        date_text = "".join(val for kind, val in row[0] if kind == "text")
        sessions.append((_iso_date(date_text), ksinr))

    return sessions


def _build_index(http: requests.Session) -> list[dict]:
    http.get(f"{BASE_URL}/info.asp", headers=HTTP_HEADERS)

    # Stage 1: collect (date, ksinr) across all years
    all_sessions: dict[str, str] = {}  # ksinr → date (deduplicated)
    for year in SCRAPE_YEARS:
        year_sessions = _scrape_year_sessions(http, year)
        for date, ksinr in year_sessions:
            if ksinr not in all_sessions:
                all_sessions[ksinr] = date
        print(f"  {year}: {len(year_sessions)} sessions")

    # Stage 2: scrape each session page
    sessions = []
    items = sorted(all_sessions.items(), key=lambda x: x[1])
    total = len(items)
    for i, (ksinr, date) in enumerate(items, 1):
        print(f"  [{i}/{total}] Parsing session {date} (ksinr={ksinr}) …", flush=True)
        url = f"{BASE_URL}/si0057.asp?__ksinr={ksinr}"
        try:
            r = http.get(url, headers=HTTP_HEADERS, timeout=30)
            r.encoding = "iso-8859-1"
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue

        parsed = _parse_session_page(r.text, date)
        sessions.append({
            "session_id": ksinr,
            "date": date,
            "folder": date,
            "header_docs": parsed["header_docs"],
            "tops": parsed["tops"],
        })

    return sessions


# ── Index Management ──────────────────────────────────────────────────────────

def _update_downloaded_flags(sessions: list[dict]) -> None:
    for s in sessions:
        session_dir = DOWNLOAD_DIR / s["folder"]
        for doc in s["header_docs"]:
            doc["downloaded"] = (session_dir / doc["filename"]).exists()
        for top in s["tops"]:
            top_dir = DOWNLOAD_DIR / top["folder"]
            for doc in top["docs"]:
                doc["downloaded"] = (top_dir / doc["filename"]).exists()


def load_index(http: requests.Session, force: bool = False) -> list[dict]:
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    if not force and INDEX_FILE.exists():
        with open(INDEX_FILE, encoding="utf-8") as f:
            sessions = json.load(f)
    else:
        print(f"Scraping document index for {COMMITTEE_NAME} …")
        sessions = _build_index(http)
        with open(INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(sessions, f, ensure_ascii=False, indent=2)
        print(f"Index saved: {len(sessions)} sessions.")

    _update_downloaded_flags(sessions)
    return sessions


# ── Download Helpers ──────────────────────────────────────────────────────────

def _compress_pdf(path: Path) -> bool:
    """Recompress embedded raster images in place (downsample + re-encode as JPEG)
    until the PDF is under COMPRESS_MAX_BYTES. Returns True only if that goal was
    reached — the file is still rewritten in place to the smallest attempt found
    even when every attempt falls short, so callers must re-check the file size
    to detect a "shrank but still oversized" outcome.

    Vector/text content is untouched; only oversized raster images (maps, scans) lose
    resolution. No-op (returns False) if pymupdf isn't installed or nothing helps.
    """
    try:
        import fitz
    except ImportError:
        return False

    original_size = path.stat().st_size
    if original_size < COMPRESS_MAX_BYTES:
        return False

    tmp = path.with_suffix(path.suffix + ".tmp")
    best_size = original_size
    for max_dim, quality in COMPRESS_ATTEMPTS:
        try:
            doc = fitz.open(path)
            seen: set = set()
            for page in doc:
                for img in page.get_images(full=True):
                    xref = img[0]
                    if xref in seen:
                        continue
                    seen.add(xref)
                    try:
                        pix = fitz.Pixmap(doc, xref)
                        if pix.colorspace is None:
                            continue  # stencil/mask, leave alone
                        if pix.colorspace.n >= 4:
                            pix = fitz.Pixmap(fitz.csRGB, pix)
                        if pix.alpha:
                            pix = fitz.Pixmap(pix, 0)
                        w, h = pix.width, pix.height
                        if max(w, h) > max_dim:
                            scale = max_dim / max(w, h)
                            pix = fitz.Pixmap(pix, int(w * scale), int(h * scale), None)
                        jpg = pix.tobytes("jpeg", jpg_quality=quality)
                        page.replace_image(xref, stream=jpg)
                    except Exception:
                        continue
            doc.save(tmp, garbage=4, deflate=True, clean=True)
            doc.close()
        except Exception:
            tmp.unlink(missing_ok=True)
            return False

        tmp_size = tmp.stat().st_size
        if tmp_size < best_size:
            tmp.replace(path)
            best_size = tmp_size
        else:
            tmp.unlink(missing_ok=True)
        if best_size < COMPRESS_MAX_BYTES:
            return True

    return False  # shrank (maybe) but never got under the cap even at the most aggressive setting


def _download_one(doc: dict, target_dir: Path, http: requests.Session) -> str:
    path = target_dir / doc["filename"]
    if path.exists():
        doc["downloaded"] = True
        return f"Already: {path.name}"
    url = f"{BASE_URL}/{doc['href']}"
    try:
        r = http.get(url, headers=HTTP_HEADERS, stream=True, timeout=60)
        if r.status_code == 200:
            with open(path, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
            doc["downloaded"] = True
            note = ""
            if path.suffix.lower() == ".pdf" and path.stat().st_size >= COMPRESS_MAX_BYTES:
                if _compress_pdf(path):
                    note = f", komprimiert -> {path.stat().st_size:,} B"
                else:
                    note = " [WARNUNG: weiterhin >=10MB, wird vom Pre-Commit-Hook geblockt]"
            return f"OK: {path.name} ({path.stat().st_size:,} B{note})"
        return f"HTTP {r.status_code}: {path.name}"
    except Exception as exc:
        return f"Error: {exc}: {path.name}"


def _find_file(file_id: str, sessions: list[dict]) -> tuple:
    """Return (doc_dict, parent_Path) or (None, None)."""
    for s in sessions:
        session_dir = DOWNLOAD_DIR / s["folder"]
        for doc in s["header_docs"]:
            if doc["file_id"] == file_id:
                return doc, session_dir
        for top in s["tops"]:
            top_dir = DOWNLOAD_DIR / top["folder"]
            for doc in top["docs"]:
                if doc["file_id"] == file_id:
                    return doc, top_dir
    return None, None


# ── Tool Implementations ──────────────────────────────────────────────────────

def _tl_list_sessions(year: int, sessions: list[dict]) -> str:
    filtered = [s for s in sessions if not year or s["date"].startswith(str(year))]
    if not filtered:
        return f"Keine Sitzungen gefunden{f' für {year}' if year else ''}."

    lines = ["Sessions (newest first):"]
    for s in sorted(filtered, key=lambda x: x["date"], reverse=True):
        n_tops = len(s["tops"])
        total = len(s["header_docs"]) + sum(len(t["docs"]) for t in s["tops"])
        dl = (
            sum(1 for d in s["header_docs"] if d.get("downloaded"))
            + sum(1 for t in s["tops"] for d in t["docs"] if d.get("downloaded"))
        )
        lines.append(
            f"  {s['date']} [ksinr:{s['session_id']}] — "
            f"{n_tops} TOPs, {total} Dokumente [{dl} heruntergeladen]"
        )
    return "\n".join(lines)


def _tl_search_documents(
    query: str, year: int, vorlage_nr: str, doc_type: str, sessions: list[dict]
) -> str:
    hits = []
    ql = query.lower()
    for s in sessions:
        if year and not s["date"].startswith(str(year)):
            continue
        for doc in s["header_docs"]:
            if vorlage_nr:
                continue
            if doc_type and doc["type_code"] != doc_type:
                continue
            if ql and ql not in doc["title"].lower():
                continue
            hits.append({**doc, "date": s["date"], "top_nr": "", "vorlage_nr": ""})
        for top in s["tops"]:
            if vorlage_nr and vorlage_nr not in top["vorlage_nr"]:
                continue
            for doc in top["docs"]:
                if doc_type and doc["type_code"] != doc_type:
                    continue
                if ql and ql not in doc["title"].lower() and ql not in top["title"].lower():
                    continue
                hits.append({
                    **doc,
                    "date": s["date"],
                    "top_nr": top["top_nr"],
                    "vorlage_nr": top["vorlage_nr"],
                })

    if not hits:
        return "Keine Dokumente gefunden."

    hits.sort(key=lambda h: h["date"], reverse=True)
    shown = hits[:30]
    lines = [
        f"{len(hits)} Dokument(e) gefunden"
        + (" (erste 30):" if len(hits) > 30 else ":"),
        "",
    ]
    for h in shown:
        status = "✓" if h.get("downloaded") else "○"
        top_info = f" | TOP {h['top_nr']}" if h["top_nr"] else ""
        vo_info = f" | Vorlage {h['vorlage_nr']}" if h["vorlage_nr"] else ""
        tc = h["type_code"] or "  "
        lines.append(
            f"  {status} [ID:{h['file_id']}] {h['date']}{top_info}{vo_info}"
            f" | [{tc}] {h['title']}"
        )
    return "\n".join(lines)


def _tl_download_session(
    session_ref: str, sessions: list[dict], http: requests.Session
) -> str:
    session = next(
        (s for s in sessions
         if s["session_id"] == session_ref or s["date"] == session_ref),
        None,
    )
    if not session:
        return f"Keine Sitzung mit ID oder Datum '{session_ref}' gefunden."

    session_dir = DOWNLOAD_DIR / session["folder"]
    session_dir.mkdir(exist_ok=True)

    results = []
    for doc in session["header_docs"]:
        results.append(_download_one(doc, session_dir, http))
    for top in session["tops"]:
        if not top["docs"]:
            continue
        top_dir = DOWNLOAD_DIR / top["folder"]
        top_dir.mkdir(parents=True, exist_ok=True)
        for doc in top["docs"]:
            results.append(_download_one(doc, top_dir, http))

    n_ok = sum(1 for r in results if r.startswith(("OK:", "Already:")))
    n_fail = len(results) - n_ok
    summary = f"Sitzung {session['date']}: {n_ok} OK, {n_fail} Fehler."
    return summary + "\n" + "\n".join(results)


def _tl_download_top(
    ktonr: str, sessions: list[dict], http: requests.Session
) -> str:
    for s in sessions:
        for top in s["tops"]:
            if top["ktonr"] != ktonr:
                continue
            if not top["docs"]:
                return f"TOP {top['top_nr']} hat keine Dokumente."
            top_dir = DOWNLOAD_DIR / top["folder"]
            top_dir.mkdir(parents=True, exist_ok=True)
            results = [_download_one(doc, top_dir, http) for doc in top["docs"]]
            n_ok = sum(1 for r in results if r.startswith(("OK:", "Already:")))
            header = (
                f"TOP {top['top_nr']} – {top['title'][:60]}\n"
                f"Vorlage: {top['vorlage_nr'] or '—'} | {n_ok}/{len(results)} Dateien"
            )
            return header + "\n" + "\n".join(results)
    return f"Kein TOP mit ktonr='{ktonr}' im Index."


def _tl_download_file(
    file_id: str, sessions: list[dict], http: requests.Session
) -> str:
    doc, parent_dir = _find_file(file_id, sessions)
    if not doc:
        return f"Datei-ID '{file_id}' nicht im Index."
    parent_dir.mkdir(parents=True, exist_ok=True)
    return _download_one(doc, parent_dir, http)


def _tl_read(file_id: str, sessions: list[dict]) -> str:
    doc, parent_dir = _find_file(file_id, sessions)
    if not doc:
        return f"Datei-ID '{file_id}' nicht im Index."
    path = parent_dir / doc["filename"]
    if not path.exists():
        return (
            f"'{doc['filename']}' noch nicht heruntergeladen. "
            f"Zuerst download_document mit file_id='{file_id}' ausführen."
        )
    try:
        import pypdf
    except ImportError:
        return "PDF-Textextraktion benötigt pypdf: pip install pypdf"
    try:
        reader = pypdf.PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        text = "\n\n".join(t for t in pages if t.strip())
        if not text.strip():
            return f"Kein Text aus '{doc['filename']}' extrahierbar (ggf. gescannt)."
        words = text.split()
        if len(words) > 4000:
            text = " ".join(words[:4000]) + f"\n\n[Gekürzt — {len(words):,} Wörter gesamt]"
        tc = f"[{doc['type_code']}] " if doc["type_code"] else ""
        header = f"=== {tc}{doc['title']} | {path.parent.name} ==="
        return f"{header}\n\n{text}"
    except Exception as exc:
        return f"Fehler beim Lesen des PDFs: {exc}"


def _tl_refresh(http: requests.Session) -> tuple:
    sessions = load_index(http, force=True)
    return f"Index aktualisiert: {len(sessions)} Sitzungen.", sessions


# ── Tool Schemas ──────────────────────────────────────────────────────────────

TOOLS: list[dict] = [
    {
        "name": "list_sessions",
        "description": (
            f"List {COMMITTEE_NAME} sessions with date, TOP count, and download status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "year": {
                    "type": "integer",
                    "description": "Filter by year, e.g. 2024. Omit or 0 for all years.",
                }
            },
        },
    },
    {
        "name": "search_documents",
        "description": (
            "Search documents by keyword, year, Vorlage number, or document type. "
            "Returns file_id, date, TOP number, Vorlage number, and title."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword to match in document or TOP titles (case-insensitive).",
                },
                "year": {
                    "type": "integer",
                    "description": "Filter by year, e.g. 2024. Omit or 0 for all years.",
                },
                "vorlage_nr": {
                    "type": "string",
                    "description": "Filter by Vorlage number, e.g. '611/251/2025' or partial '611/251'.",
                },
                "doc_type": {
                    "type": "string",
                    "description": "Filter by type code: BL, VO, EI, NI, SU.",
                },
            },
        },
    },
    {
        "name": "download_session",
        "description": (
            "Download all documents for a session into dated folder with TOP subfolders. "
            "Pass the session date (YYYY-MM-DD) or ksinr ID."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_ref": {
                    "type": "string",
                    "description": "Session date 'YYYY-MM-DD' or ksinr ID from list_sessions.",
                }
            },
            "required": ["session_ref"],
        },
    },
    {
        "name": "download_top",
        "description": "Download all documents for one Tagesordnungspunkt by its ktonr.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ktonr": {
                    "type": "string",
                    "description": "ktonr from search_documents or list_sessions results.",
                }
            },
            "required": ["ktonr"],
        },
    },
    {
        "name": "download_document",
        "description": "Download a single document by its file_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "file_id from search_documents results.",
                }
            },
            "required": ["file_id"],
        },
    },
    {
        "name": "read_document",
        "description": (
            "Extract and return text from a downloaded PDF (up to ~4 000 words). "
            "Use download_document first if not yet downloaded."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "file_id of the document to read.",
                }
            },
            "required": ["file_id"],
        },
    },
    {
        "name": "refresh_index",
        "description": "Re-scrape the Ratsinformationsystem to update the document index.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def _call_tool(
    name: str, inputs: dict, sessions: list[dict], http: requests.Session
) -> tuple:
    if name == "list_sessions":
        return _tl_list_sessions(inputs.get("year", 0), sessions), sessions
    if name == "search_documents":
        return (
            _tl_search_documents(
                inputs.get("query", ""),
                inputs.get("year", 0),
                inputs.get("vorlage_nr", ""),
                inputs.get("doc_type", ""),
                sessions,
            ),
            sessions,
        )
    if name == "download_session":
        return _tl_download_session(inputs["session_ref"], sessions, http), sessions
    if name == "download_top":
        return _tl_download_top(inputs["ktonr"], sessions, http), sessions
    if name == "download_document":
        return _tl_download_file(inputs["file_id"], sessions, http), sessions
    if name == "read_document":
        return _tl_read(inputs["file_id"], sessions), sessions
    if name == "refresh_index":
        return _tl_refresh(http)
    return f"Unknown tool: {name}", sessions


# ── Agent Loop ────────────────────────────────────────────────────────────────

def run(force_refresh: bool = False) -> None:
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("Error: ANTHROPIC_API_KEY environment variable is not set.")

    client = anthropic.Anthropic(api_key=api_key)
    http = requests.Session()

    print(f"UVP Document Agent — {COMMITTEE_NAME}, Erlangen")
    print("=" * 60)
    sessions = load_index(http, force=force_refresh)
    total_docs = sum(
        len(s["header_docs"]) + sum(len(t["docs"]) for t in s["tops"])
        for s in sessions
    )
    downloaded = sum(
        sum(1 for d in s["header_docs"] if d.get("downloaded"))
        + sum(1 for t in s["tops"] for d in t["docs"] if d.get("downloaded"))
        for s in sessions
    )
    print(
        f"Ready — {len(sessions)} sessions, {total_docs} documents "
        f"[{downloaded} downloaded]."
    )
    print('Type "exit" to quit.\n')

    messages: list[dict] = []

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAuf Wiedersehen!")
            return
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit", "q", "bye", "tschüss"}:
            print("Auf Wiedersehen!")
            return

        messages.append({"role": "user", "content": user_input})

        while True:
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            text = "\n".join(b.text for b in response.content if b.type == "text")
            if text:
                print(f"Assistant: {text}")

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                arg_str = ", ".join(
                    f"{k}={v!r}"
                    for k, v in block.input.items()
                    if v not in (None, "", 0)
                )
                print(f"  → {block.name}({arg_str})")
                result, sessions = _call_tool(block.name, block.input, sessions, http)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            messages.append({"role": "user", "content": tool_results})

        print()


COMPRESS_TIMEOUT_SECS = 120  # a malformed source PDF can make MuPDF spin near-forever


def compress_existing() -> None:
    """Scan DOWNLOAD_DIR for already-downloaded PDFs >=COMPRESS_MAX_BYTES and shrink them
    in place. Each file is compressed in its own subprocess with a hard timeout, so one
    malformed PDF (MuPDF can hang indefinitely trying to repair a broken xref) can't stall
    the whole batch.
    """
    try:
        import fitz  # noqa: F401
    except ImportError:
        sys.exit("Error: pymupdf ist nicht installiert. 'pip install pymupdf' ausführen.")
    import subprocess

    candidates = sorted(
        p for p in DOWNLOAD_DIR.rglob("*.pdf")
        if p.stat().st_size >= COMPRESS_MAX_BYTES
    )
    if not candidates:
        print("Keine Dateien >=10MB gefunden.")
        return

    print(f"{len(candidates)} Datei(en) >=10MB gefunden.\n")
    n_ok, n_fail, n_timeout = 0, 0, 0
    for i, path in enumerate(candidates, 1):
        before = path.stat().st_size
        rel = path.relative_to(DOWNLOAD_DIR)
        try:
            r = subprocess.run(
                [sys.executable, __file__, "--compress-one", str(path)],
                capture_output=True, text=True, timeout=COMPRESS_TIMEOUT_SECS,
            )
            ok = r.returncode == 0
        except subprocess.TimeoutExpired:
            ok = False
            print(f"[{i}/{len(candidates)}] TIMEOUT {rel}: >={COMPRESS_TIMEOUT_SECS}s, vermutlich beschädigte PDF — übersprungen")
            n_timeout += 1
            continue

        after = path.stat().st_size
        if ok:
            print(f"[{i}/{len(candidates)}] OK  {rel}: {before:,} -> {after:,} B")
            n_ok += 1
        else:
            status = "unverändert" if after == before else f"{before:,} -> {after:,} B (weiterhin >=10MB)"
            print(f"[{i}/{len(candidates)}] FAIL {rel}: {status}")
            n_fail += 1
    print(f"\nFertig: {n_ok} komprimiert, {n_fail} weiterhin >=10MB, {n_timeout} übersprungen (Timeout).")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"Claude AI agent for {COMMITTEE_NAME} document retrieval"
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force re-scrape the document index from ratsinfo.erlangen.de",
    )
    parser.add_argument(
        "--compress",
        action="store_true",
        help="Shrink already-downloaded PDFs >=10MB in place, then exit (no chat session).",
    )
    parser.add_argument(
        "--compress-one",
        metavar="PATH",
        help=argparse.SUPPRESS,  # internal: single-file worker used by --compress via subprocess
    )
    main.__doc__ = None
    args = parser.parse_args()
    if args.compress_one:
        ok = _compress_pdf(Path(args.compress_one))
        sys.exit(0 if ok else 1)
    if args.compress:
        compress_existing()
        return
    run(force_refresh=args.refresh)


if __name__ == "__main__":
    main()
