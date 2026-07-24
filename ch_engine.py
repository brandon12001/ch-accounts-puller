#!/usr/bin/env python3
"""
Companies House engine (v4) - importable module.

Same core as v3.1 (CH API -> accounts fetch -> xhtml/PDF/OCR -> FX regex scan
-> Haiku call brief) but refactored so every step is a callable function the
Streamlit app can use for single OR bulk runs. Adds smart match-and-skip so a
name search only proceeds when the top hit is confidently the right company.

Nothing about the fetch/OCR/scan/brief logic has changed in behaviour; it has
only been made importable and given a confidence gate on company matching.
"""
import json
import os
import re
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

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def _ch_key():
    return os.environ.get("CH_API_KEY", "")

def _anthropic_key():
    return os.environ.get("ANTHROPIC_API_KEY", "")

# Back-compat module-level values (read at import; prefer the functions above)
API_KEY = _ch_key()
ANTHROPIC_KEY = _anthropic_key()
BASE = "https://api.company-information.service.gov.uk"
DOC_BASE = "https://document-api.company-information.service.gov.uk"
CLAUDE_MODEL = "claude-haiku-4-5"
OUT_DIR = Path("accounts")
OUT_DIR.mkdir(exist_ok=True)
MAX_OCR_PAGES = 35

# FX scan patterns (unchanged from v3.1)
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
    # v4: overseas parent flag
    (r"(ultimate )?parent (company|undertaking).{0,60}(incorporated|registered) in (?!england|wales|scotland|the uk|united kingdom|northern ireland)", "OVERSEAS PARENT - FX may be group-level", 3),
    (r"consolidated (financial statements|accounts) of .{0,40}(gmbh|s\.?a\.?|b\.?v\.?|inc\.?|llc|ag|spa|s\.?r\.?l)", "Foreign parent consolidates - qualify who controls FX", 3),
]

session = requests.Session()
session.auth = (_ch_key(), "")


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
def rate_limited_get(url, **kw):
    session.auth = (_ch_key(), "")   # refresh in case secrets loaded after import
    r = None
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


# ---------------------------------------------------------------------------
# Smart match-and-skip
# ---------------------------------------------------------------------------
_SUFFIX_NOISE = re.compile(
    r"\b(the|ltd|limited|llp|plc|company|co|group|holdings?|uk|gb|"
    r"international|intl|services?|trading|"
    r"\(uk\)|\(gb\)|\(holdings?\))\b",
    re.I,
)

def normalise_name(name: str) -> str:
    """Lowercase, strip legal suffixes / common noise / punctuation, collapse spaces."""
    n = name.lower()
    n = n.replace("&", " and ")             # Food & Drinks == Food and Drinks
    n = re.sub(r"[^\w\s]", " ", n)          # drop punctuation
    n = _SUFFIX_NOISE.sub(" ", n)           # drop Ltd/Limited/Group/etc
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _collapsed(name: str) -> str:
    """Normalised name with ALL spaces removed: fever tree -> fevertree."""
    return normalise_name(name).replace(" ", "")


def _token_set(name: str) -> set:
    return set(normalise_name(name).split())


def match_confidence(query: str, candidate_title: str) -> tuple[str, float, str]:
    """
    Compare a search query to a CH candidate name.
    Returns (bucket, score 0-1, reason) where bucket is:
      'exact'  - normalised names identical -> auto-accept
      'strong' - candidate contains the full query phrase, or >=90% token overlap
      'weak'   - some overlap but not confident -> auto-accept ONLY if allowed
      'skip'   - clearly different -> do not pull accounts
    """
    q_norm = normalise_name(query)
    c_norm = normalise_name(candidate_title)
    if not q_norm or not c_norm:
        return "skip", 0.0, "empty after normalising"

    if q_norm == c_norm:
        return "exact", 1.0, "exact match after normalising suffixes"

    # Fever-Tree vs FEVERTREE: identical once all spaces/hyphens collapse
    if _collapsed(query) and _collapsed(query) == _collapsed(candidate_title):
        return "exact", 1.0, "exact match after collapsing spaces/hyphens"

    q_tokens, c_tokens = _token_set(query), _token_set(candidate_title)

    # candidate contains the whole query phrase as a suffix/prefix, e.g.
    # 'Eden Fine Wines Wholesale' extends the query -> strong.
    # But 'Burger King Foods' PREPENDS a brand word to 'King Foods' -> not safe.
    if q_norm in c_norm or c_norm in q_norm:
        longer, shorter = (c_norm, q_norm) if len(c_norm) >= len(q_norm) else (q_norm, c_norm)
        if longer.startswith(shorter):        # extra words come AFTER the query
            return "strong", 0.95, "candidate extends the query name"
        # extra words prepended: only trust it if every query token is present anyway
        if q_tokens and q_tokens <= c_tokens and longer.endswith(shorter):
            return "weak", 0.8, "query appears at end of a longer name - check"
    if not q_tokens:
        return "skip", 0.0, "no comparable tokens"
    overlap = len(q_tokens & c_tokens) / len(q_tokens)
    shared = ", ".join(sorted(q_tokens & c_tokens)) or "none"

    # A single shared common word ('Eden') is NOT enough
    if len(q_tokens & c_tokens) <= 1 and len(q_tokens) > 1:
        return "skip", overlap, f"only shares '{shared}' - likely different company"
    if overlap >= 0.9:
        return "strong", overlap, f"{int(overlap*100)}% token overlap"
    if overlap >= 0.6:
        return "weak", overlap, f"{int(overlap*100)}% token overlap ({shared})"
    return "skip", overlap, f"low overlap ({int(overlap*100)}%), shares: {shared}"


def search_candidates(name: str, limit: int = 5):
    """Return up to `limit` CH candidates with the detail needed to disambiguate."""
    r = rate_limited_get(f"{BASE}/search/companies",
                         params={"q": name, "items_per_page": limit})
    if r.status_code != 200:
        return [], f"search failed ({r.status_code})"
    out = []
    for item in r.json().get("items", []):
        addr = item.get("address", {}) or {}
        bucket, score, reason = match_confidence(name, item.get("title", ""))
        out.append({
            "title": item.get("title", ""),
            "number": item.get("company_number", ""),
            "status": item.get("company_status", ""),
            "incorporated": item.get("date_of_creation", ""),
            "address": ", ".join(filter(None, [
                addr.get("locality", ""), addr.get("postal_code", ""),
            ])),
            "sic": ", ".join(item.get("sic_codes", []) or []),
            "match_bucket": bucket,
            "match_score": round(score, 2),
            "match_reason": reason,
        })
    return out, None


def auto_pick(name: str, allow_weak: bool = False):
    """
    Pick the best confident candidate for a name, or return None with a reason.
    Used by bulk mode. Single mode shows the shortlist instead.
    """
    candidates, err = search_candidates(name, limit=5)
    if err:
        return None, candidates, err
    if not candidates:
        return None, [], "no CH match"

    # Prefer active companies when choosing the top confident hit
    ranked = sorted(
        candidates,
        key=lambda c: (
            {"exact": 3, "strong": 2, "weak": 1, "skip": 0}[c["match_bucket"]],
            1 if c["status"] == "active" else 0,
            c["match_score"],
        ),
        reverse=True,
    )
    best = ranked[0]
    if best["match_bucket"] in ("exact", "strong"):
        return best, candidates, None
    if best["match_bucket"] == "weak" and allow_weak:
        return best, candidates, None
    return None, candidates, f"no confident match (best: '{best['title']}' - {best['match_reason']})"


# ---------------------------------------------------------------------------
# Filing + document (unchanged logic from v3.1)
# ---------------------------------------------------------------------------
def latest_accounts_filing(number):
    r = rate_limited_get(f"{BASE}/company/{number}/filing-history",
                         params={"category": "accounts", "items_per_page": 5})
    if r.status_code != 200:
        return None, f"filing history failed ({r.status_code})"
    for item in r.json().get("items", []):
        if item.get("links", {}).get("document_metadata"):
            return item, None
    return None, "no accounts with document found"


def classify_accounts_type(filing_description: str, text: str) -> str:
    """
    Classify the filing as full / medium / small / micro / dormant.
    Uses the CH filing description first, then the document text.
    Small + micro filings legally omit the P&L, so briefs on them are thin.
    """
    desc = (filing_description or "").lower()
    lower = (text or "").lower()

    if "dormant" in desc or "dormant company" in lower:
        return "dormant"
    if "micro" in desc or "micro-entity" in lower or "micro entity" in lower:
        return "micro"
    # CH's "total exemption full/small accounts" = small-companies regime,
    # despite the word "full" - usually no P&L filed
    if "total exemption" in desc:
        return "small"
    if "small" in desc or re.search(r"small companies regime|provisions applicable to companies subject to the small", lower):
        return "small"
    if "medium" in desc or "medium-sized companies" in lower:
        return "medium"
    if "full" in desc or "group" in desc:
        return "full"
    # Heuristics from content: audited full accounts have these sections
    has_pl = bool(re.search(r"(income statement|profit and loss account|statement of comprehensive income)", lower))
    has_audit = "independent auditor" in lower
    if has_pl and has_audit:
        return "full"
    if has_pl:
        return "medium"
    if re.search(r"balance sheet", lower) and not has_pl:
        return "small"
    return "unknown"


def classify_accounts(description: str) -> str:
    """
    Map a CH filing description to a clean category.
    full / group / medium -> rich disclosure, worth full pipeline
    small                 -> usually abridged, little to read
    micro / dormant       -> nothing useful, skip before fetching
    NOTE: 'total-exemption-full' is a SMALL company (audit-exempt), not full accounts.
    """
    d = (description or "").lower()
    if "group" in d:
        return "group"
    if "micro" in d:
        return "micro"
    if "dormant" in d:
        return "dormant"
    if "medium" in d:
        return "medium"
    if "total-exemption" in d or "abridged" in d or "small" in d:
        return "small"
    if "full" in d:
        return "full"
    return "unknown"


def get_document(doc_metadata_url, dest_stem: Path):
    doc_id = doc_metadata_url.rstrip("/").split("/")[-1]
    meta = rate_limited_get(f"{DOC_BASE}/document/{doc_id}")
    formats = meta.json().get("resources", {}) if meta.status_code == 200 else {}
    if "application/xhtml+xml" in formats:
        r = rate_limited_get(f"{DOC_BASE}/document/{doc_id}/content",
                             headers={"Accept": "application/xhtml+xml"}, allow_redirects=True)
        if r.status_code == 200:
            r.encoding = "utf-8"
            return "xhtml", r.text, None
    r = None
    for _ in range(2):
        r = rate_limited_get(f"{DOC_BASE}/document/{doc_id}/content",
                             headers={"Accept": "application/pdf"}, allow_redirects=True)
        if r.status_code == 200:
            dest = dest_stem.with_suffix(".pdf")
            dest.write_bytes(r.content)
            expected = int(r.headers.get("Content-Length", 0))
            if expected and dest.stat().st_size < expected:
                continue
            return "pdf", dest, None
    return None, None, f"document fetch failed ({r.status_code if r else 'no response'})"


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


# ---------------------------------------------------------------------------
# FX scan + grading (v3.1 scan, v4 grade)
# ---------------------------------------------------------------------------
def scan_text(text: str):
    lower = text.lower()
    score, findings, excerpts = 0, [], []
    for pattern, label, weight in PATTERNS:
        matches = list(re.finditer(pattern, lower))
        if matches:
            score += weight
            findings.append(f"{label} (x{len(matches)})")
            m = matches[0]
            excerpts.append(" ".join(text[max(0, m.start() - 120):m.end() + 160].split()))
    turnover = ""
    m = re.search(r"turnover[^\d£$€]{0,40}[£$€]?\s?([\d,]{6,})", lower)
    if m:
        turnover = m.group(1)
    return score, findings, excerpts, turnover


def _parse_money(s: str) -> float:
    if not s:
        return 0.0
    digits = re.sub(r"[^\d]", "", str(s))
    return float(digits) if digits else 0.0


def fx_grade(score, turnover_str) -> str:
    """
    v4 est_fx_volume grade A-D: FX intensity (score) x size (turnover).
    A = strong FX signals + real size (est FX vol likely >= £2m). D = weak/small.
    """
    t = _parse_money(turnover_str)
    if score >= 10 and t >= 5_000_000:
        return "A"
    if score >= 6 and t >= 2_000_000:
        return "B"
    if score >= 4 or t >= 2_000_000:
        return "C"
    return "D"


# ---------------------------------------------------------------------------
# AI brief (unchanged prompt from v3.1, plus v4 turnover key)
# ---------------------------------------------------------------------------
KEEP_WORDS = re.compile(
    r"currenc|foreign exchange|hedg|forward|derivativ|exchange rate|import|export|"
    r"overseas|international|turnover|revenue|principal activit|strategic|review of|"
    r"borrow|overdraft|invoice discount|loan|creditor|going concern|acqui|fire|"
    r"restructur|expansion|new (division|market|site|facility)|risk|parent|subsidiar|"
    r"cost of sales|gross profit|operating profit|profit before|income statement|"
    r"statement of comprehensive|profit and loss",
    re.I,
)

def trim_for_brief(text: str, head_chars: int = 6000, cap_chars: int = 28000) -> str:
    head = text[:head_chars]
    kept = []
    for para in re.split(r"\n\s*\n|(?<=\.)\s{2,}", text[head_chars:]):
        p = para.strip()
        if len(p) > 40 and KEEP_WORDS.search(p):
            kept.append(" ".join(p.split()))
    return (head + "\n---\n" + "\n".join(kept))[:cap_chars]


def ai_brief(company_name: str, accounts_text: str, score: int, findings: list,
             accounts_category: str = "unknown"):
    if not _anthropic_key():
        return None
    prompt = f"""You are extracting hard evidence from UK statutory accounts for an FX brokerage salesperson at Lumon. Your job is EVIDENCE EXTRACTION, not summary. Never pad. If the accounts do not state something, write exactly "not disclosed".
Company: {company_name}. Accounts filing category: {accounts_category}. Regex pre-scan score: {score}. Signals: {', '.join(findings) if findings else 'none'}.
Return ONLY a JSON object, no markdown, with exactly these keys:
- "one_liner": what the company does, one sentence, plain English
- "turnover": most recent annual turnover/revenue/sales figure as a plain number, no symbols or commas. Check the income statement AND prior-year comparatives AND the strategic report. If abridged with no P&L, write "not disclosed - abridged"
- "fx_pnl_figures": every exchange gain/loss figure stated, with year and amount, e.g. "FY25 loss 93593; FY24 loss 227054", or "not disclosed"
- "currencies_named": every currency explicitly mentioned (USD, EUR, CHF, CNH etc) with context, e.g. "CHF - purchases from Swiss parent", or "not disclosed"
- "export_split": export vs home turnover split if the geographic analysis gives it, with figures, or "not disclosed"
- "hedging_instruments": what is actually HELD or USED - forwards, options, with commitment values where stated. Distinguish policy boilerplate ("policy permits hedging") from evidence of use. Or "none evident"
- "est_fx_volume": reasoned estimate of annual FX volume with reasoning shown, labelled EST, e.g. "EST 5-8m+: export 26.1m of 37.7m turnover". If nothing supports one, "cannot estimate - no FX data disclosed"
- "sophistication": "hedger" / "exposed-unhedged" / "minimal" / "unclear"
- "overseas_parent": "yes - [parent, country]" if non-UK owned/consolidated, else "no"
- "triggers": recent events worth referencing on a call (acquisitions, growth, new markets, restructuring), 1-2 sentences, or "none found"
- "call_ammo": the 2-3 hardest verifiable facts from these accounts, flat statements with figures, NO pitch language. e.g. "Exchange losses 93,593 FY25 and 227,054 FY24. No hedging instruments held. Export is 69% of 37.7m turnover."
- "red_flags": insolvency signals, restricted sectors (firearms/cannabis/adult/radioactive), going-concern doubts, or "none"
ACCOUNTS TEXT:
{trim_for_brief(accounts_text)}"""
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": _anthropic_key(), "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": 1100,
                  "messages": [{"role": "user", "content": prompt}]},
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


# ---------------------------------------------------------------------------
# Orchestration: run ONE company end to end
# ---------------------------------------------------------------------------
def blank_result(name="", number=""):
    return {"company": name, "number": number, "matched_name": "", "match_bucket": "",
            "match_reason": "", "score": "", "grade": "", "accounts_category": "",
            "sophistication": "", "overseas_parent": "", "one_liner": "",
            "fx_pnl_figures": "", "currencies_named": "", "export_split": "",
            "hedging_instruments": "", "est_fx_volume": "", "call_ammo": "",
            "triggers": "", "red_flags": "", "turnover": "", "accounts_date": "",
            "accounts_type": "", "read_method": "", "findings": "", "excerpts": "",
            "pdf_path": "", "error": ""}


def process_company(name="", number="", allow_weak=False, do_brief=True,
                    preselected=None):
    """
    Full pipeline for one company.
    - If `number` given: skip name matching, go straight to accounts.
    - If `preselected` (a candidate dict from single-search confirm) given: use it.
    - Else: auto_pick by name with the confidence gate.
    Returns a result dict.
    """
    result = blank_result(name, number)

    try:
        if not number:
            if preselected:
                chosen = preselected
            else:
                chosen, candidates, err = auto_pick(name, allow_weak=allow_weak)
                if err:
                    result["error"] = err
                    if candidates:
                        result["matched_name"] = candidates[0]["title"]
                        result["match_reason"] = candidates[0]["match_reason"]
                    return result
            number = chosen["number"]
            result["number"] = number
            result["matched_name"] = chosen["title"]
            result["match_bucket"] = chosen.get("match_bucket", "")
            result["match_reason"] = chosen.get("match_reason", "")
            if chosen.get("status") not in ("active", "", None):
                result["error"] = f"status: {chosen.get('status')}"
                return result

        filing, err = latest_accounts_filing(number)
        if err:
            result["error"] = err
            return result
        result["accounts_date"] = filing.get("action_date", filing.get("date", ""))
        result["accounts_type"] = filing.get("description", "")
        result["accounts_category"] = classify_accounts_type(result["accounts_type"], "")
        if result["accounts_category"] in ("micro", "dormant"):
            result["error"] = f"{result['accounts_category']} accounts - no useful disclosure, skipped"
            return result

        safe = re.sub(r"[^A-Za-z0-9]+", "_", (result["matched_name"] or name or number))[:60]
        kind, payload, err = get_document(filing["links"]["document_metadata"],
                                          OUT_DIR / f"{safe}_{number}")
        if err:
            result["error"] = err
            return result

        if kind == "xhtml":
            text, result["read_method"] = text_from_xhtml(payload), "xhtml"
        else:
            result["pdf_path"] = str(payload)
            text, result["read_method"] = text_from_pdf(payload)
        if not text:
            result["error"] = "no readable text"
            return result

        score, findings, excerpts, turnover = scan_text(text)
        result.update(score=score, findings=" | ".join(findings),
                      excerpts=" || ".join(excerpts[:4]), turnover=turnover)

        # Refine classification now the document text exists (CH descriptions are
        # often just "accounts made up to ..." with no size hint)
        if result["accounts_category"] in ("unknown", ""):
            result["accounts_category"] = classify_accounts_type(result["accounts_type"], text)
        if result["accounts_category"] in ("micro", "dormant"):
            result["error"] = f"{result['accounts_category']} accounts - no useful disclosure, skipped"
            return result

        if do_brief and result["accounts_category"] == "small":
            result["error"] = "small/abridged filing - little to read, brief skipped; qualify by phone"
        elif do_brief:
            brief = ai_brief(result["matched_name"] or name, text, score, findings,
                             accounts_category=result["accounts_category"])
            if brief:
                if "error" in brief:
                    result["error"] = f"brief: {brief['error']}"
                else:
                    for k in ("one_liner", "fx_pnl_figures", "currencies_named",
                              "export_split", "hedging_instruments", "est_fx_volume",
                              "call_ammo", "triggers", "sophistication",
                              "red_flags", "overseas_parent"):
                        result[k] = brief.get(k, "")
                    t = str(brief.get("turnover", ""))
                    if t and not t.startswith("not disclosed"):
                        result["turnover"] = t

        result["grade"] = fx_grade(score, result["turnover"])
    except Exception as e:
        result["error"] = f"unexpected: {e}"
    return result
