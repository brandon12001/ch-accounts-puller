#!/usr/bin/env python3
"""Companies House bulk accounts puller + FX triage scanner (v2, OCR-capable)."""

import csv
import io
import os
import re
import sys
import time
from pathlib import Path

import requests

try:
    import pdfplumber
    HAS_PDF = True
except ImportError:
    HAS_PDF = False

try:
    from pdf2image import convert_from_path
    import pytesseract
    HAS_OCR = True
except ImportError:
    HAS_OCR = False

try:
    from bs4 import BeautifulSoup
    HAS_BS = True
except ImportError:
    HAS_BS = False

API_KEY = os.environ.get("CH_API_KEY", "PASTE_KEY_HERE")
BASE = "https://api.company-information.service.gov.uk"
DOC_BASE = "https://document-api.company-information.service.gov.uk"

OUT_DIR = Path("accounts")
OUT_DIR.mkdir(exist_ok=True)

MAX_OCR_PAGES = 35  # cap OCR effort per document

PATTERNS = [
    (r"forward (foreign )?(currency|exchange) contract", "FORWARD CONTRACTS - active hedger", 10),
    (r"committed to (pay|sell)\s*[£$€]?[\d,]+", "FORWARD COMMITMENT VALUE stated", 10),
    (r"hedg\w+", "Mentions hedging", 6),
    (r"foreign (currency|exchange) (risk|exposure)", "Names FX risk", 5),
    (r"exchange (rate )?(loss|gain|losses|gains)", "FX P&L line present", 5),
    (r"denominated in (a )?foreign currenc", "Foreign currency transactions", 4),
    (r"currency risk", "Currency risk section", 4),
    (r"import|export", "Imports/exports mentioned", 2),
    (r"minimal exposure to exchange", "DISQUALIFIER: says minimal exposure", -8),
    (r"invoice (discount|financ)|supplier (discount|financ)|debtor financ", "Invoice/supplier finance (cash tight)", 3),
    (r"directors?['\u2019]? loans? to the company|due to key management", "Directors lending in (cash tight)", 2),
]

session = requests.Session()
session.auth = (API_KEY, "")


def rate_limited_get(url, **kw):
    for attempt in range(4):
        try:
            r = session.get(url, timeout=60, **kw)
        except requests.RequestException as e:
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 429:
            time.sleep(60 * (attempt + 1))
            continue
        return r
    return r


def search_company(name):
    r = rate_limited_get(f"{BASE}/search/companies", params={"q": name, "items_per_page": 3})
    if r.status_code != 200:
        return None, f"search failed ({r.status_code})"
    items = r.json().get("items", [])
    if not items:
        return None, "no match"
    return items[0], None


def latest_accounts_filing(number):
    r = rate_limited_get(f"{BASE}/company/{number}/filing-history",
                         params={"category": "accounts", "items_per_page": 5})
    if r.status_code != 200:
        return None, f"filing history failed ({r.status_code})"
    for item in r.json().get("items", []):
        doc_meta = item.get("links", {}).get("document_metadata")
        if doc_meta:
            return item, None
    return None, "no accounts with document found"


def get_document(doc_metadata_url, dest_stem: Path):
    """Prefer xhtml (pure text). Fall back to PDF. Returns (kind, path_or_text, err)."""
    doc_id = doc_metadata_url.rstrip("/").split("/")[-1]
    meta = rate_limited_get(f"{DOC_BASE}/document/{doc_id}")
    formats = {}
    if meta.status_code == 200:
        formats = meta.json().get("resources", {})

    if "application/xhtml+xml" in formats:
        r = rate_limited_get(f"{DOC_BASE}/document/{doc_id}/content",
                             headers={"Accept": "application/xhtml+xml"},
                             allow_redirects=True)
        if r.status_code == 200:
            return "xhtml", r.text, None

    for attempt in range(2):
        r = rate_limited_get(f"{DOC_BASE}/document/{doc_id}/content",
                             headers={"Accept": "application/pdf"},
                             allow_redirects=True)
        if r.status_code == 200:
            dest = dest_stem.with_suffix(".pdf")
            dest.write_bytes(r.content)
            expected = int(r.headers.get("Content-Length", 0))
            if expected and dest.stat().st_size < expected:
                continue  # truncated, retry
            return "pdf", dest, None
    return None, None, f"document fetch failed ({r.status_code})"


def text_from_xhtml(xhtml: str) -> str:
    if HAS_BS:
        return BeautifulSoup(xhtml, "html.parser").get_text(" ")
    return re.sub(r"<[^>]+>", " ", xhtml)


def text_from_pdf(path: Path) -> tuple[str, str]:
    """Returns (text, method). Tries text layer, then OCR."""
    text = ""
    if HAS_PDF:
        try:
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    text += (page.extract_text() or "") + "\n"
        except Exception:
            text = ""
    if len(text.strip()) >= 200:
        return text, "text-layer"

    if HAS_OCR:
        try:
            images = convert_from_path(path, dpi=200, last_page=MAX_OCR_PAGES)
            ocr_text = ""
            for img in images:
                ocr_text += pytesseract.image_to_string(img) + "\n"
            if len(ocr_text.strip()) >= 200:
                return ocr_text, "ocr"
        except Exception as e:
            return "", f"ocr failed: {e}"
    return "", "no text"


def scan_text(text: str):
    lower = text.lower()
    score, findings, excerpts = 0, [], []
    for pattern, label, weight in PATTERNS:
        matches = list(re.finditer(pattern, lower))
        if matches:
            score += weight
            findings.append(f"{label} (x{len(matches)})")
            m = matches[0]
            start = max(0, m.start() - 120)
            end = min(len(text), m.end() + 160)
            excerpts.append(" ".join(text[start:end].split()))
    turnover = ""
    m = re.search(r"turnover[^\d£$€]{0,40}[£$€]?\s?([\d,]{6,})", lower)
    if m:
        turnover = m.group(1)
    return score, findings, excerpts, turnover


def main(input_csv):
    rows_out = []
    with open(input_csv, newline="", encoding="utf-8-sig") as f:
        inputs = list(csv.DictReader(f))

    print(f"Processing {len(inputs)} companies...\n")

    for i, row in enumerate(inputs, 1):
        row = {k.lower().strip(): (v or "").strip() for k, v in row.items()}
        name_in, number = row.get("name", ""), row.get("number", "")
        print(f"[{i}/{len(inputs)}] {name_in or number}")

        result = {"input_name": name_in, "company_number": number, "matched_name": "",
                  "status": "", "accounts_date": "", "accounts_type": "", "read_method": "",
                  "fx_score": "", "findings": "", "turnover_guess": "", "excerpts": "",
                  "pdf_path": "", "error": ""}
        try:
            if not number:
                match, err = search_company(name_in)
                if err:
                    result["error"] = err; rows_out.append(result); print(f"    SKIP: {err}"); continue
                number = match["company_number"]
                result.update(company_number=number, matched_name=match.get("title", ""),
                              status=match.get("company_status", ""))
                if result["status"] not in ("active", ""):
                    result["error"] = f"status: {result['status']}"
                    rows_out.append(result); print("    SKIP: not active"); continue

            filing, err = latest_accounts_filing(number)
            if err:
                result["error"] = err; rows_out.append(result); print(f"    SKIP: {err}"); continue
            result["accounts_date"] = filing.get("action_date", filing.get("date", ""))
            result["accounts_type"] = filing.get("description", "")

            safe = re.sub(r"[^A-Za-z0-9]+", "_", (result["matched_name"] or name_in or number))[:60]
            kind, payload, err = get_document(filing["links"]["document_metadata"], OUT_DIR / f"{safe}_{number}")
            if err:
                result["error"] = err; rows_out.append(result); print(f"    SKIP: {err}"); continue

            if kind == "xhtml":
                text = text_from_xhtml(payload)
                result["read_method"] = "xhtml"
            else:
                result["pdf_path"] = str(payload)
                text, method = text_from_pdf(payload)
                result["read_method"] = method

            if not text:
                result["error"] = "no readable text"
                rows_out.append(result); print("    SKIP: unreadable"); continue

            score, findings, excerpts, turnover = scan_text(text)
            result.update(fx_score=score, findings=" | ".join(findings),
                          excerpts=" || ".join(excerpts[:4]), turnover_guess=turnover)
            print(f"    OK: score {score} via {result['read_method']}")

        except Exception as e:
            result["error"] = f"unexpected: {e}"
            print(f"    ERROR: {e}")
        rows_out.append(result)
        time.sleep(0.6)

    rows_out.sort(key=lambda r: (isinstance(r["fx_score"], int), r["fx_score"] or 0), reverse=True)
    with open("triage_report.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
        w.writeheader(); w.writerows(rows_out)
    print(f"\nDone. {len(rows_out)} rows -> triage_report.csv")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__); sys.exit(1)
    if API_KEY == "PASTE_KEY_HERE":
        print("Set CH_API_KEY first"); sys.exit(1)
    main(sys.argv[1])
