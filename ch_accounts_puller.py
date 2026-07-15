#!/usr/bin/env python3
"""Companies House bulk accounts puller + FX triage + AI call briefs (v3.1, trimmed briefs)."""

import csv
import json
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

API_KEY = os.environ.get("CH_API_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GSHEET_CREDS = os.environ.get("GSHEET_CREDENTIALS", "")
GSHEET_ID = os.environ.get("GSHEET_ID", "")
BASE = "https://api.company-information.service.gov.uk"
DOC_BASE = "https://document-api.company-information.service.gov.uk"
CLAUDE_MODEL = "claude-haiku-4-5"

OUT_DIR = Path("accounts")
OUT_DIR.mkdir(exist_ok=True)
MAX_OCR_PAGES = 35

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
        except requests.RequestException:
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
    doc_id = doc_metadata_url.rstrip("/").split("/")[-1]
    meta = rate_limited_get(f"{DOC_BASE}/document/{doc_id}")
    formats = meta.json().get("resources", {}) if meta.status_code == 200 else {}

    if "application/xhtml+xml" in formats:
        r = rate_limited_get(f"{DOC_BASE}/document/{doc_id}/content",
                             headers={"Accept": "application/xhtml+xml"},
                             allow_redirects=True)
        if r.status_code == 200:
            r.encoding = "utf-8"
            return "xhtml", r.text, None

    for _ in range(2):
        r = rate_limited_get(f"{DOC_BASE}/document/{doc_id}/content",
                             headers={"Accept": "application/pdf"},
                             allow_redirects=True)
        if r.status_code == 200:
            dest = dest_stem.with_suffix(".pdf")
            dest.write_bytes(r.content)
            expected = int(r.headers.get("Content-Length", 0))
            if expected and dest.stat().st_size < expected:
                continue
            return "pdf", dest, None
    return None, None, f"document fetch failed ({r.status_code})"


def text_from_xhtml(xhtml: str) -> str:
    if HAS_BS:
        return BeautifulSoup(xhtml, "html.parser").get_text(" ")
    return re.sub(r"<[^>]+>", " ", xhtml)


def text_from_pdf(path: Path):
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
            ocr = "".join(pytesseract.image_to_string(img) + "\n" for img in images)
            if len(ocr.strip()) >= 200:
                return ocr, "ocr"
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
            excerpts.append(" ".join(text[max(0, m.start()-120):m.end()+160].split()))
    turnover = ""
    m = re.search(r"turnover[^\d£$€]{0,40}[£$€]?\s?([\d,]{6,})", lower)
    if m:
        turnover = m.group(1)
    return score, findings, excerpts, turnover


KEEP_WORDS = re.compile(
    r"currenc|foreign exchange|hedg|forward|derivativ|exchange rate|import|export|"
    r"overseas|international|turnover|revenue|principal activit|strategic|review of|"
    r"borrow|overdraft|invoice discount|loan|creditor|going concern|acqui|fire|"
    r"restructur|expansion|new (division|market|site|facility)|risk",
    re.I,
)


def trim_for_brief(text: str, head_chars: int = 6000, cap_chars: int = 28000) -> str:
    """Keep the opening pages plus only FX/finance-relevant paragraphs."""
    head = text[:head_chars]
    kept = []
    for para in re.split(r"\n\s*\n|(?<=\.)\s{2,}", text[head_chars:]):
        p = para.strip()
        if len(p) > 40 and KEEP_WORDS.search(p):
            kept.append(" ".join(p.split()))
    body = "\n".join(kept)
    return (head + "\n---\n" + body)[:cap_chars]


def ai_brief(company_name: str, accounts_text: str, score: int, findings: list):
    """Ask Claude for a call brief. Returns dict or None."""
    if not ANTHROPIC_KEY:
        return None
    prompt = f"""You are preparing a cold-call battle card for an FX brokerage salesperson at Lumon (UK, sells FX risk management: forwards, options, no-deposit facilities, dedicated dealers).

Below is extracted text from the latest filed statutory accounts for {company_name}. Regex pre-scan score: {score}. Signals found: {', '.join(findings) if findings else 'none'}.

Return ONLY a JSON object, no markdown, with exactly these keys:
- "one_liner": what the company does, one sentence, plain English
- "fx_summary": their FX exposure and how they currently manage it, 1-2 sentences, cite figures from the accounts where present
- "triggers": recent events from the business review worth referencing on a call (fires, acquisitions, new divisions, growth, restructuring, new markets), 1-2 sentences, or "none found"
- "sophistication": "hedger" (uses forwards/derivatives), "exposed-unhedged" (has FX, no hedging evident), "minimal" (little/no FX), or "unclear"
- "angle": the single best opening angle for the call in one sentence, matched to sophistication: hedgers get benchmarking/fixed-spread/margin-certainty language, never missed-upside talk; exposed-unhedged get margin-protection framing; cash-tight companies get no-deposit forwards. If sophistication is "minimal" or the accounts state minimal FX exposure, say exactly "DO NOT PURSUE - no meaningful FX exposure" instead of inventing an angle
- "red_flags": anything suggesting caution (insolvency risk, restricted sectors like firearms/cannabis/adult/radioactive, tiny scale), or "none"

ACCOUNTS TEXT:
{trim_for_brief(accounts_text)}"""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 800,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=120,
        )
        if r.status_code != 200:
            return {"error": f"api {r.status_code}: {r.text[:200]}"}
        blocks = r.json().get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": "unparseable AI response", "raw": text[:300]}
    except Exception as e:
        return {"error": f"api call failed: {e}"}


def push_to_gsheet(rows):
    """Write results to a new dated tab in the Google Sheet."""
    if not (GSHEET_CREDS and GSHEET_ID):
        print("Google Sheet push skipped (no credentials).")
        return
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(
            json.loads(GSHEET_CREDS),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(GSHEET_ID)
        tab_name = time.strftime("run_%d-%m-%Y_%H%M")
        headers = list(rows[0].keys())
        ws = sh.add_worksheet(title=tab_name, rows=len(rows) + 5, cols=len(headers))
        data = [headers] + [[str(r.get(h, "")) for h in headers] for r in rows]
        ws.update(data)
        ws.freeze(rows=1)
        print(f"Pushed {len(rows)} rows to Google Sheet tab '{tab_name}'")
    except Exception as e:
        print(f"Google Sheet push failed: {e}")


def main(input_csv):
    rows_out = []
    with open(input_csv, newline="", encoding="utf-8-sig") as f:
        inputs = list(csv.DictReader(f))
    print(f"Processing {len(inputs)} companies... (AI briefs: {'ON' if ANTHROPIC_KEY else 'OFF, no ANTHROPIC_API_KEY'})\n")

    for i, row in enumerate(inputs, 1):
        row = {k.lower().strip(): (v or "").strip() for k, v in row.items()}
        name_in, number = row.get("name", ""), row.get("number", "")
        print(f"[{i}/{len(inputs)}] {name_in or number}")

        result = {"company": name_in, "number": number, "matched_name": "", "score": "",
                  "sophistication": "", "one_liner": "", "fx_summary": "", "triggers": "",
                  "angle": "", "red_flags": "", "turnover": "", "accounts_date": "",
                  "accounts_type": "", "read_method": "", "findings": "", "excerpts": "",
                  "pdf_path": "", "error": ""}
        try:
            if not number:
                match, err = search_company(name_in)
                if err:
                    result["error"] = err; rows_out.append(result); print(f"    SKIP: {err}"); continue
                number = match["company_number"]
                result["number"] = number
                result["matched_name"] = match.get("title", "")
                if match.get("company_status") not in ("active", "", None):
                    result["error"] = f"status: {match.get('company_status')}"
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
                text, result["read_method"] = text_from_xhtml(payload), "xhtml"
            else:
                result["pdf_path"] = str(payload)
                text, result["read_method"] = text_from_pdf(payload)

            if not text:
                result["error"] = "no readable text"
                rows_out.append(result); print("    SKIP: unreadable"); continue

            score, findings, excerpts, turnover = scan_text(text)
            result.update(score=score, findings=" | ".join(findings),
                          excerpts=" || ".join(excerpts[:4]), turnover=turnover)

            brief = ai_brief(result["matched_name"] or name_in, text, score, findings)
            if brief:
                if "error" in brief:
                    result["error"] = f"brief: {brief['error']}"
                else:
                    for k in ("one_liner", "fx_summary", "triggers", "sophistication", "angle", "red_flags"):
                        result[k] = brief.get(k, "")
            print(f"    OK: score {score} via {result['read_method']}"
                  + (", brief done" if brief and "error" not in (brief or {}) else ""))

        except Exception as e:
            result["error"] = f"unexpected: {e}"
            print(f"    ERROR: {e}")
        rows_out.append(result)
        time.sleep(0.6)

    rows_out.sort(key=lambda r: (isinstance(r["score"], int), r["score"] or 0), reverse=True)
    with open("call_sheet.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
        w.writeheader(); w.writerows(rows_out)
    print(f"\nDone. {len(rows_out)} rows -> call_sheet.csv (sorted by score)")
    push_to_gsheet(rows_out)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__); sys.exit(1)
    if not API_KEY:
        print("Set CH_API_KEY first"); sys.exit(1)
    main(sys.argv[1])
