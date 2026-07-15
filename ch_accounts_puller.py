#!/usr/bin/env python3
"""
Companies House bulk accounts puller + FX triage scanner
========================================================
Feed it a CSV of company names (or company numbers), it will:
  1. Resolve each name to a company number via CH search
  2. Find the latest filed accounts (category 'accounts')
  3. Download the accounts PDF via the CH Document API
  4. Extract the text and scan for FX-relevant language
  5. Produce triage_report.csv ranking companies by FX signal

Usage:
  python ch_accounts_puller.py input.csv
  # input.csv needs a header row with either 'name' or 'number' column
"""

import csv
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
    print("WARNING: pdfplumber not installed, PDFs will download but no FX scan.")
    print("         pip install pdfplumber\n")

API_KEY = os.environ.get("CH_API_KEY", "PASTE_KEY_HERE")
BASE = "https://api.company-information.service.gov.uk"
DOC_BASE = "https://document-api.company-information.service.gov.uk"

OUT_DIR = Path("accounts")
OUT_DIR.mkdir(exist_ok=True)

# --- FX triage patterns: (regex, label, weight) --------------------------
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
    """CH allows 600 req/5min. Simple politeness delay + 429 backoff."""
    for attempt in range(4):
        r = session.get(url, timeout=30, **kw)
        if r.status_code == 429:
            wait = 60 * (attempt + 1)
            print(f"    rate limited, sleeping {wait}s...")
            time.sleep(wait)
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
    top = items[0]
    return top, None


def latest_accounts_filing(number):
    r = rate_limited_get(
        f"{BASE}/company/{number}/filing-history",
        params={"category": "accounts", "items_per_page": 5},
    )
    if r.status_code != 200:
        return None, f"filing history failed ({r.status_code})"
    for item in r.json().get("items", []):
        if "annotations" in item and not item.get("links"):
            continue
        doc_meta = item.get("links", {}).get("document_metadata")
        if doc_meta:
            return item, None
    return None, "no accounts with document found"


def download_pdf(doc_metadata_url, dest: Path):
    doc_id = doc_metadata_url.rstrip("/").split("/")[-1]
    r = rate_limited_get(
        f"{DOC_BASE}/document/{doc_id}/content",
        headers={"Accept": "application/pdf"},
        allow_redirects=True,
    )
    if r.status_code == 200 and r.headers.get("Content-Type", "").startswith("application/pdf"):
        dest.write_bytes(r.content)
        return True, None
    return False, f"document fetch failed ({r.status_code})"


def scan_pdf(path: Path):
    """Return (score, findings, excerpts, turnover_guess)."""
    if not HAS_PDF:
        return 0, [], [], ""
    text = ""
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text += (page.extract_text() or "") + "\n"
    except Exception as e:
        return 0, [f"pdf read error: {e}"], [], ""
    if len(text.strip()) < 200:
        return 0, ["scanned/image PDF, no text layer, read manually"], [], ""

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
            snippet = " ".join(text[start:end].split())
            excerpts.append(snippet)

    turnover = ""
    m = re.search(r"turnover[^\d£$€]{0,40}[£$€]?\s?([\d,]{6,})", lower)
    if m:
        turnover = m.group(1)
    return score, findings, excerpts, turnover


def main(input_csv):
    rows_out = []
    with open(input_csv, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        inputs = list(reader)

    print(f"Processing {len(inputs)} companies...\n")

    for i, row in enumerate(inputs, 1):
        row = {k.lower().strip(): (v or "").strip() for k, v in row.items()}
        name_in = row.get("name", "")
        number = row.get("number", "")
        label = name_in or number
        print(f"[{i}/{len(inputs)}] {label}")

        result = {
            "input_name": name_in, "company_number": number, "matched_name": "",
            "status": "", "accounts_date": "", "accounts_type": "",
            "fx_score": "", "findings": "", "turnover_guess": "",
            "excerpts": "", "pdf_path": "", "error": "",
        }

        try:
            if not number:
                match, err = search_company(name_in)
                if err:
                    result["error"] = err
                    rows_out.append(result); print(f"    SKIP: {err}"); continue
                number = match["company_number"]
                result["company_number"] = number
                result["matched_name"] = match.get("title", "")
                result["status"] = match.get("company_status", "")
                if result["status"] not in ("active", ""):
                    result["error"] = f"company status: {result['status']}"
                    rows_out.append(result); print(f"    SKIP: not active"); continue

            filing, err = latest_accounts_filing(number)
            if err:
                result["error"] = err
                rows_out.append(result); print(f"    SKIP: {err}"); continue

            result["accounts_date"] = filing.get("action_date", filing.get("date", ""))
            result["accounts_type"] = filing.get("description", "")

            safe = re.sub(r"[^A-Za-z0-9]+", "_", (result["matched_name"] or name_in or number))[:60]
            dest = OUT_DIR / f"{safe}_{number}.pdf"
            ok, err = download_pdf(filing["links"]["document_metadata"], dest)
            if not ok:
                result["error"] = err
                rows_out.append(result); print(f"    SKIP: {err}"); continue
            result["pdf_path"] = str(dest)

            score, findings, excerpts, turnover = scan_pdf(dest)
            result["fx_score"] = score
            result["findings"] = " | ".join(findings)
            result["excerpts"] = " || ".join(excerpts[:4])
            result["turnover_guess"] = turnover
            print(f"    OK: score {score}, {result['accounts_type']}")

        except Exception as e:
            result["error"] = f"unexpected: {e}"
            print(f"    ERROR: {e}")

        rows_out.append(result)
        time.sleep(0.6)  # politeness, stays well under rate limit

    rows_out.sort(key=lambda r: (isinstance(r["fx_score"], int), r["fx_score"] or 0), reverse=True)
    with open("triage_report.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
        writer.writeheader()
        writer.writerows(rows_out)

    print(f"\nDone. {len(rows_out)} rows -> triage_report.csv, PDFs in ./accounts/")
    print("Sort by fx_score: 15+ = pitch-ready hedger, 5-14 = worth a read, <0 = likely disqualify.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    if API_KEY == "PASTE_KEY_HERE":
        print("Set your API key first:  export CH_API_KEY=xxx")
        sys.exit(1)
    main(sys.argv[1])
