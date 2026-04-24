"""
LegalQuery MY — ejudgment.kehakiman.gov.my Scraper
Scrapes Malaysian court judgments and saves to Supabase
Run manually: python3 scraper.py
Scheduled: runs daily at 2am via crontab
"""

import os
import re
import time
import hashlib
import logging
import requests
import io
from bs4 import BeautifulSoup
from datetime import datetime
from pdfminer.high_level import extract_text
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
SUPABASE_URL        = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")

BASE_URL   = "https://ejudgment.kehakiman.gov.my/ejudgmentweb"
SEARCH_URL = f"{BASE_URL}/SearchPage.aspx"
DELAY      = 2    # seconds between requests — be polite
MAX_PAGES  = 5    # pages per run (20 cases/page = 100 cases max)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ─────────────────────────────────────────
# SUPABASE
# ─────────────────────────────────────────
def supabase_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }

def case_exists(content_hash: str) -> bool:
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/cases",
            headers=supabase_headers(),
            params={"content_hash": f"eq.{content_hash}", "select": "id"},
            timeout=10,
        )
        return len(resp.json()) > 0
    except:
        return False

def save_to_supabase(record: dict) -> bool:
    try:
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/cases",
            headers={**supabase_headers(), "Prefer": "return=minimal"},
            json=record,
            timeout=15,
        )
        if resp.status_code in (200, 201):
            log.info("✓ Saved: %s", record.get("citation"))
            return True
        elif resp.status_code == 409:
            log.info("Duplicate skipped: %s", record.get("citation"))
            return False
        else:
            log.error("Save failed %d: %s", resp.status_code, resp.text[:300])
            return False
    except Exception as e:
        log.error("Supabase error: %s", e)
        return False

# ─────────────────────────────────────────
# PDF EXTRACTION
# ─────────────────────────────────────────
def extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        text = extract_text(io.BytesIO(pdf_bytes))
        if text and len(text.strip()) > 100:
            return text.strip()
    except Exception as e:
        log.warning("PDF extraction failed: %s", e)
    return ""

# ─────────────────────────────────────────
# METADATA EXTRACTION FROM TABLE ROW
# ─────────────────────────────────────────
def parse_date(date_str: str):
    for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y"]:
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%Y-%m-%d")
        except:
            pass
    return None

def detect_court_level(case_number: str) -> str:
    cn = case_number.upper()
    if any(x in cn for x in ["FC", "FEDERAL"]):
        return "federal"
    if any(x in cn for x in ["W-02", "W-01", "B-02", "K-02"]):
        return "appeal"
    if any(x in cn for x in ["WA", "MT", "BA", "KA"]):
        return "high"
    return "appeal"  # ejudgment mostly has appeal cases

def extract_row_metadata(cells: list) -> dict:
    """
    ejudgment table columns:
    0: Bil (number)
    1: Nombor Kes (case number) + court in brackets
    2: Pihak-Pihak (parties) — Perayu / Responden
    3: Kata Kunci (keywords)
    4: Tarikh Keputusan (decision date)
    5: Tarikh AP Dimuat Naik (upload date)
    6: Hakim/Majistret (judges)
    7: Dokumen (PDF links)
    """
    try:
        # Case number and court
        case_cell = cells[1].get_text(strip=True) if len(cells) > 1 else ""
        case_number = case_cell.split("(")[0].strip()
        court_in_bracket = ""
        m = re.search(r'\(([^)]+)\)', case_cell)
        if m:
            court_in_bracket = m.group(1)

        # Parties
        perayu = ""
        responden = ""
        if len(cells) > 2:
            party_html = cells[2]
            text = party_html.get_text(separator="\n", strip=True)
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            capture_perayu = False
            capture_responden = False
            for line in lines:
                if "PERAYU" in line.upper():
                    capture_perayu = True
                    capture_responden = False
                    continue
                elif "RESPONDEN" in line.upper() or "PENDAKWA" in line.upper():
                    capture_responden = True
                    capture_perayu = False
                    continue
                if capture_perayu and not perayu:
                    perayu = line
                elif capture_responden and not responden:
                    responden = line

        case_name = f"{perayu} v {responden}" if perayu and responden else case_number

        # Keywords
        keywords = cells[3].get_text(strip=True) if len(cells) > 3 else ""

        # Decision date
        date_str = cells[4].get_text(strip=True) if len(cells) > 4 else ""
        decision_date = parse_date(date_str)

        # Judges
        judges = []
        if len(cells) > 6:
            judge_text = cells[6].get_text(separator="\n", strip=True)
            for line in judge_text.split("\n"):
                line = line.strip()
                if line and len(line) > 5 and not line.upper().startswith("KORUM"):
                    judges.append(line)

        # PDF link
        pdf_url = None
        if len(cells) > 7:
            for a in cells[7].find_all("a", href=True):
                href = a["href"]
                link_text = a.get_text(strip=True).lower()
                if ".pdf" in href.lower() or "alasan" in link_text or "penghakiman" in link_text:
                    pdf_url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"
                    break

        return {
            "case_number": case_number,
            "case_name": case_name,
            "court_label": court_in_bracket,
            "court_level": detect_court_level(case_number),
            "keywords": keywords[:1000],
            "decision_date": decision_date,
            "judges": judges[:10],
            "pdf_url": pdf_url,
        }
    except Exception as e:
        log.error("Row parse error: %s", e)
        return {}

# ─────────────────────────────────────────
# SEARCH PAGE INTERACTION
# ─────────────────────────────────────────
def get_page(session: requests.Session, page: int = 1) -> list:
    """
    Fetch one page of results from ejudgment portal.
    Returns list of table rows.
    """
    try:
        # Load search page first to get ASP.NET state
        resp = session.get(SEARCH_URL, headers=HEADERS, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")

        def get_field(name):
            el = soup.find("input", {"id": name}) or soup.find("input", {"name": name})
            return el["value"] if el else ""

        viewstate       = get_field("__VIEWSTATE")
        viewstate_gen   = get_field("__VIEWSTATEGENERATOR")
        event_validation = get_field("__EVENTVALIDATION")

        if page == 1:
            # Submit search with no filters = all results
            form_data = {
                "__VIEWSTATE": viewstate,
                "__VIEWSTATEGENERATOR": viewstate_gen,
                "__EVENTVALIDATION": event_validation,
                "__EVENTTARGET": "",
                "__EVENTARGUMENT": "",
                "ctl00$ContentPlaceHolder1$btnCari": "Cari",
            }
        else:
            # Navigate to specific page
            form_data = {
                "__VIEWSTATE": viewstate,
                "__VIEWSTATEGENERATOR": viewstate_gen,
                "__EVENTVALIDATION": event_validation,
                "__EVENTTARGET": "ctl00$ContentPlaceHolder1$gvHasilCarian",
                "__EVENTARGUMENT": f"Page${page}",
            }

        time.sleep(DELAY)
        resp = session.post(
            SEARCH_URL,
            data=form_data,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        soup = BeautifulSoup(resp.text, "html.parser")

# Find results table — class is tblResult gridView
        table = None
        for t in soup.find_all("table"):
            classes = t.get("class", [])
            if "tblResult" in classes or "gridView" in classes:
                rows = t.find_all("tr", recursive=False)
                if len(rows) >= 1:
                    table = t
                    break
                    
        # Fallback — find table with most rows
        if not table:
            all_tables = soup.find_all("table")
            if all_tables:
                table = max(all_tables, key=lambda t: len(t.find_all("tr")))

        if not table:
            log.warning("Results table not found on page %d", page)
            log.debug("Page content preview: %s", resp.text[:500])
            return []

        data_rows = table.find_all("tr")[1:]  # skip header row
        log.info("Page %d: found %d rows", page, len(data_rows))
        return data_rows

    except Exception as e:
        log.error("Failed to get page %d: %s", page, e)
        return []

# ─────────────────────────────────────────
# PROCESS ONE ROW
# ─────────────────────────────────────────
def process_row(session: requests.Session, row) -> bool:
    cells = row.find_all("td")
    if len(cells) < 4:
        return False

    meta = extract_row_metadata(cells)
    if not meta or not meta.get("case_number"):
        return False

    # Generate hash for dedup
    hash_input = meta["case_number"] + (meta["decision_date"] or "")
    content_hash = hashlib.sha256(hash_input.encode()).hexdigest()

    # Download PDF if available
    full_text = ""
    if meta.get("pdf_url"):
        try:
            time.sleep(DELAY)
            pdf_resp = session.get(meta["pdf_url"], headers=HEADERS, timeout=30)
            if pdf_resp.status_code == 200 and len(pdf_resp.content) > 500:
                content_hash = hashlib.sha256(pdf_resp.content).hexdigest()
                if case_exists(content_hash):
                    log.info("Already exists: %s", meta["case_number"])
                    return False
                full_text = extract_pdf_text(pdf_resp.content)
                log.info("PDF text: %d chars", len(full_text))
        except Exception as e:
            log.warning("PDF error %s: %s", meta["case_number"], e)

    if not full_text and case_exists(content_hash):
        log.info("Already exists: %s", meta["case_number"])
        return False

    # Build database record
    record = {
        "source_url":    meta.get("pdf_url") or SEARCH_URL,
        "source":        "ekehakiman",
        "citation":      meta["case_number"],
        "case_name":     meta["case_name"],
        "court_level":   meta["court_level"],
        "decision_date": meta["decision_date"],
        "judges":        meta["judges"],
        "outcome":       "other",
        "subject_tags":  [],
        "full_text":     full_text[:50000] if full_text else meta.get("keywords", ""),
        "word_count":    len(full_text.split()) if full_text else 0,
        "content_hash":  content_hash,
        "is_published":  True,
    }

    return save_to_supabase(record)

# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
def run():
    log.info("=" * 60)
    log.info("LegalQuery Scraper starting — %s", datetime.utcnow().isoformat())
    log.info("Target: %s", SEARCH_URL)
    log.info("Max pages: %d", MAX_PAGES)
    log.info("=" * 60)

    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        log.error("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY in .env")
        return 0

    session = requests.Session()
    total_saved = 0
    total_attempted = 0

    for page in range(1, MAX_PAGES + 1):
        log.info("--- Page %d ---", page)
        rows = get_page(session, page)

        if not rows:
            log.info("No rows on page %d — stopping", page)
            break

        for row in rows:
            total_attempted += 1
            try:
                if process_row(session, row):
                    total_saved += 1
            except Exception as e:
                log.error("Row processing error: %s", e)
            time.sleep(DELAY)

        log.info("Page %d done. Total saved: %d/%d", page, total_saved, total_attempted)
        time.sleep(DELAY * 3)

    log.info("=" * 60)
    log.info("Scrape complete: %d saved out of %d attempted", total_saved, total_attempted)
    log.info("=" * 60)
    return total_saved

if __name__ == "__main__":
    run()
