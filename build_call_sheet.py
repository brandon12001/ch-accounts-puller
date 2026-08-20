#!/usr/bin/env python3
"""Build a call sheet worth reading.

The old version printed the FX evidence and, where the accounts said nothing
about currency, printed "qualify on the call". That is useless on a phone. Most
of these companies never mention currency in their accounts, but they do say
plenty about turnover direction, margin pressure, acquisitions, new facilities
and going concern. That is what an opening line is made of.

Every row now gets four things:

    business   what they actually do
    money      turnover, which way it is going, what happened to margin
    happening  acquisitions, new sites, ownership changes, contract wins
    currency   the FX evidence, or an honest note that there is none and what
               to ask instead

Usage:
    python build_call_sheet.py --contacts contacts.csv \
        --cache ch_results_cache.jsonl --out call_sheet.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from pathlib import Path

try:
    import ch_classify
    HAS_CLASSIFY = True
except ImportError:
    HAS_CLASSIFY = False


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

_NOISE = re.compile(
    r"\b(ltd|limited|plc|llp|uk|holdings?|group|company|co|the|and|"
    r"international|intl|europe|european)\b"
)


def squash(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    s = s.lower().split("|")[0]
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = _NOISE.sub(" ", s)
    return re.sub(r"\s+", "", s)


def build_index(records: list[dict]) -> dict[str, dict]:
    """Richest record wins where a company appears more than once."""
    idx: dict[str, dict] = {}
    for r in records:
        weight = len(str(r.get("call_ammo", ""))) + len(str(r.get("triggers", "")))
        for field in ("company", "matched_name"):
            k = squash(r.get(field, ""))
            if not k:
                continue
            cur = idx.get(k)
            if cur is None or weight > len(str(cur.get("call_ammo", ""))) + \
                                       len(str(cur.get("triggers", ""))):
                idx[k] = r
    return idx


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------

def money(v) -> str:
    v = str(v).replace(",", "").strip()
    if not v:
        return ""
    try:
        n = float(v)
    except ValueError:
        return v
    # accounts filed in thousands come through as a small number against a
    # company that is obviously not that small; leave those alone rather than
    # guessing, but flag anything suspiciously tiny
    if n >= 1e9:
        return f"£{n/1e9:.2f}bn"
    if n >= 1e6:
        return f"£{n/1e6:.1f}m"
    if n >= 1000:
        return f"£{n/1000:.0f}k"
    return f"£{n:,.0f}"


def clip(text: str, limit: int) -> str:
    """Cut at a sentence or clause boundary, never mid-word.

    A line that ends "...in respect of risks relating to" tells the reader
    nothing and looks broken on a printed call sheet.
    """
    text = re.sub(r"\s+", " ", str(text)).strip()
    if len(text) <= limit:
        return text
    window = text[:limit + 30]
    for sep in (". ", "; ", ", "):
        cut = window.rfind(sep, int(limit * 0.5))
        if cut > 0:
            return window[:cut].rstrip(" ,;.") + "..."
    cut = window.rfind(" ", int(limit * 0.6))
    return (window[:cut] if cut > 0 else text[:limit]).rstrip(" ,;.") + "..."


def _first(pattern: str, text: str, group: int = 1) -> str:
    m = re.search(pattern, text, re.I)
    return m.group(group).strip() if m else ""


def money_line(rec: dict) -> str:
    """Turnover, direction, and what happened to profit."""
    ammo = str(rec.get("call_ammo", ""))
    bits = []

    t = money(rec.get("turnover", ""))
    # the direction sentence can start with Turnover, Revenue, Sales, or the
    # verb first ("Revenue up 47.9% to..."), so try the whole clause
    blob = ammo + " " + str(rec.get("triggers", ""))
    m = re.search(r"(turnover|revenue|sales)[^.]{0,90}?"
                  r"\b(up|down|grew|increased|declined|fell|rose|surged|collapsed)\b"
                  r"[^.]{0,40}?([\d.]+\s?%)", blob, re.I)
    if m:
        verb = m.group(2).lower()
        pct = m.group(3).replace(" ", "")
        pn = re.match(r"([\d.]+)%", pct)
        if pn:
            pct = f"{round(float(pn.group(1)))}%"
        bits.append(f"turnover {t}, {verb} {pct}" if t else f"turnover {verb} {pct}")
    else:
        m = re.search(r"(turnover|revenue)[^.]{0,60}?\b(up|down|grew|increased|"
                      r"declined|fell|rose|flat)\b", blob, re.I)
        if m and t:
            bits.append(f"turnover {t}, {m.group(2).lower()}")
        elif t:
            # no direction stated, so derive it from any two turnover figures
            nums = re.findall(r"[£$\u20ac]\s?([\d,]+(?:\.\d+)?)\s?([mk])?",
                              re.sub(r"\s+", " ", blob)[:260])
            vals = []
            for raw, suf in nums[:4]:
                try:
                    v = float(raw.replace(",", ""))
                except ValueError:
                    continue
                v *= 1e6 if suf == "m" else 1e3 if suf == "k" else 1
                if v > 1e5:
                    vals.append(v)
            if len(vals) >= 2 and vals[0] != vals[1]:
                pct = (vals[0] - vals[1]) / vals[1] * 100
                if abs(pct) >= 2:
                    bits.append(f"turnover {t}, "
                                f"{'up' if pct > 0 else 'down'} {abs(pct):.0f}%")
                else:
                    bits.append(f"turnover {t}, broadly flat")
            else:
                bits.append(f"turnover {t}")

    # margin and profit movement carry more weight than the turnover headline
    margin = re.search(r"(gross |operating |net )?(profit |margin )[^.]{0,80}?"
                       r"(fell|declined|dropped|rose|improved|increased|up|down)"
                       r"[^.]{0,40}?([\d.]+%|£[\d,.]+[mk]?)", ammo, re.I)
    if margin:
        line = clip(margin.group(0), 90)
        # the brief writes raw integers: "Operating profit 222795 FY25 vs 1070916"
        line = re.sub(r"(?<![£$\u20ac\d.,])(\d{5,})(?![\d.,%])",
                      lambda m: money(m.group(1)), line)
        line = re.sub(r"([\d.]+)\s?%", lambda m: f"{round(float(m.group(1)))}%", line)
        bits.append(line)
    else:
        loss = _first(r"(operating loss[^.]{0,50}|loss before tax[^.]{0,50}|"
                      r"net liabilit\w+[^.]{0,50})", ammo)
        if loss:
            bits.append(clip(loss, 90))

    return "; ".join(bits)


def happening_line(rec: dict) -> str:
    """Acquisitions, new sites, ownership changes, anything with a date on it."""
    trig = str(rec.get("triggers", "")).strip()
    if trig and trig.lower() not in ("none found", "none", "not disclosed"):
        return clip(trig, 200)
    return ""


def risk_line(rec: dict) -> str:
    flags = str(rec.get("red_flags", "")).strip()
    if flags and flags.lower() not in ("none", "none found", "not disclosed"):
        return clip(flags, 180)
    return ""


_XBRL = re.compile(r"taxonomy|metadata|schema|xbrl", re.I)
_CCY = re.compile(r"\b(eur|usd|euro|dollar|yen|jpy|chf|aud|cad|nzd|zar|"
                  r"renminbi|rmb|cny|sek|nok|dkk|pln)\b", re.I)


def currency_line(rec: dict) -> str:
    """The FX evidence, or an honest note plus the question to ask."""
    bits = []

    pnl = str(rec.get("fx_pnl_figures", "")).strip()
    if pnl and pnl.lower() != "not disclosed":
        pairs = re.findall(r"(FY\d+)\s+(gain|loss|credit)\s+([\d,]+)", pnl, re.I)
        if pairs:
            bits.append(", ".join(f"{y} {k.lower()} {money(v)}" for y, k, v in pairs[:2]))
        else:
            bits.append(clip(pnl, 80))

    hedge = str(rec.get("hedging_instruments", "")).strip()
    hl = hedge.lower()

    # "Forward currency contracts held. At year end had contracts of £nil"
    # opens by saying held and then says the amount is nothing. Reading only
    # the first clause produced HOLDS against a company with no cover at all,
    # which is the opposite of the truth and the wrong pitch on the call.
    nil = re.search(r"(of|totalling|amounting to|value of)\s*[£$\u20ac]?\s*"
                    r"(nil|nought|zero|0)\b", hedge, re.I) or \
          re.search(r"[£$\u20ac]\s?nil", hedge, re.I)
    if nil:
        bits.append("POLICY ONLY: stated hedging policy but £nil held at year end")
        hedge = ""
        hl = ""
    # "none evident - accounts state the company does not hedge" starts with
    # none evident, so test the opening rather than the whole string
    denies = hl.startswith(("none", "not disclosed")) or "no hedging" in hl
    if hedge and not denies:
        bits.append("HOLDS: " + clip(hedge, 110))
    elif hedge and denies and len(hedge) > 20:
        # the model often explains why, and the explanation is the useful part
        why = re.sub(r"^none evident\s*[-–]\s*", "", hedge, flags=re.I)
        bits.append("NO COVER: " + clip(why, 110))
    elif bits:
        bits.append("nothing held against it")

    cur = str(rec.get("currencies_named", "")).strip()
    if cur and cur.lower() != "not disclosed" and not _XBRL.search(cur):
        found = {m.group(0).upper()[:3] for m in _CCY.finditer(cur)}
        found = {"EUR" if c in ("EUR", "EUR") else "USD" if c in ("USD", "DOL") else c
                 for c in found}
        if found:
            bits.append("trades in " + "/".join(sorted(found)))

    exp = str(rec.get("export_split", "")).strip()
    if exp and exp.lower() != "not disclosed" and re.search(r"\d", exp):
        bits.append("split: " + clip(exp, 70))

    if bits:
        return " | ".join(bits)

    # Nothing disclosed. Say so plainly and give a question worth asking,
    # picked from what the business actually does.
    what = (str(rec.get("one_liner", "")) + " " +
            str(rec.get("call_ammo", ""))).lower()
    if re.search(r"import|wholesal|distribut|merchant|stockist", what):
        ask = "ASK: where the stock comes from and who prices it"
    elif re.search(r"manufactur|engineer|fabricat|foundry|castings|precision", what):
        ask = "ASK: where the raw material and components are bought"
    elif re.search(r"export|overseas|international market", what):
        ask = "ASK: which markets and what currency they invoice in"
    elif re.search(r"haulage|logistics|freight|transport", what):
        ask = "ASK: whether fuel, tolls or European hauliers are paid in euros"
    elif re.search(r"food|produce|seafood|fish|meat|wine|drink", what):
        ask = "ASK: what proportion is bought from Europe"
    else:
        ask = "ASK: whether they buy or sell anything outside the UK"
    return "no currency disclosed in the accounts. " + ask


# --------------------------------------------------------------------------
# call order
#
# priority answers "do the accounts prove currency exposure". That is not the
# same question as "is this worth ringing". A £38m HGV parts distributor almost
# certainly buys from Europe; its accounts simply do not say so, because small
# and medium filings disclose almost nothing. So X means no evidence in the
# filing, not no exposure.
# --------------------------------------------------------------------------

# Sectors where the goods are near-certainly imported. Deliberately narrow.
# The earlier version matched on "food" and turnover, which flagged Tims Dairy
# (a UK yogurt maker buying British milk) and Unitas Wholesale (a buying group
# whose turnover is membership fees, not goods). Neither buys a thing abroad.
_IMPORTS_LIKELY = re.compile(
    r"import\w*|"
    r"wholesal\w*|distribut\w*|merchant|stockist|stockhold\w*|factor\b|"
    r"\b(steel|alloy|metal|aluminium|copper|timber|plywood|glass|resin|polymer|"
    r"plastic|chemical|pigment|solvent|component|bearing|fastener|semiconductor)\b|"
    r"\b(electronic|electrical) (component|equipment|distribut)|"
    r"seafood|fish merchant|fresh produce|fruit and veg|"
    r"wine|spirit|champagne|coffee|tea|cocoa|spice|"
    r"machinery (import|distribut|suppl)|"
    r"clothing|apparel|textile|footwear|giftware|toy\b|houseware|"
    # a machine shop buys steel, tooling and components, most of it priced
    # abroad even when the invoice arrives in sterling
    r"manufactur\w*|engineer\w*|fabricat\w*|machining|precision|foundry|"
    r"casting|pressing|moulding|extrusion|tooling|sub-?assembl\w+|"
    r"packaging|printing|coating|plating|treatment",
    re.I,
)

# Business models that do not buy goods at all, whatever sector words appear
# in the description. A buying group's turnover is membership income; a
# franchise network's is fees. Neither pays a supplier in euros.
_NOT_A_BUYER = re.compile(
    r"buying group|cooperative|co-operative|membership services|"
    r"supplier contributions|franchis\w+|"
    r"recruitment|consultanc\w+|advisor\w+|agency|agenc\w+|"
    r"software|saas|platform|marketplace|app\b|"
    r"insurance|broker(age)? services|financial services|fund manage|"
    r"holding company|investment (company|vehicle)|"
    r"training|education|care home|nursery|dental|veterinary|"
    r"haulage|logistics|freight forward|storage and warehous|"
    r"construction|housebuild|groundwork|civil engineering|scaffold|"
    r"estate agen|letting|property (develop|manage|investment)|"
    r"cleaning|security guard|facilities management|"
    r"pub\b|restaurant|takeaway|catering services|hospitality",
    re.I,
)

# Manufacturers whose raw material is domestic. A dairy buys British milk, a
# bakery buys British flour. Being large and in food proves nothing.
_DOMESTIC_INPUT = re.compile(
    r"dairy|yogurt|yoghurt|creamery|milk|"
    r"bakery|baker\b|bread|"
    r"abattoir|slaughter|butcher|meat process|poultry process|"
    r"brewery|brewer\b|cider|"
    r"quarry|aggregate|concrete|ready-?mix|"
    r"farm\w*|agricultur\w*|grower",
    re.I,
)


# One shared definition of currency evidence, from ch_classify. This used to be
# a second implementation here and the two disagreed: a company could pass the
# discovery gate and then be cut from the call sheet, or the reverse.
if HAS_CLASSIFY:
    has_hard_evidence = ch_classify.fx_evidence
else:
    def has_hard_evidence(rec: dict) -> str:      # type: ignore[misc]
        return ""


_MAKES_THINGS = re.compile(
    r"manufactur\w*|fabricat\w*|foundry|casting|pressing|machining|"
    r"engineer\w*|precision|tooling|moulding|extrusion",
    re.I,
)


def worth_calling(rec: dict, priority: str) -> tuple[int, str]:
    """Rank, or 0 meaning do not call and do not spend a Lusha credit.

    The point of the tool is to remove the guesswork, so a company only earns
    a place if the accounts show currency crossing a border or the business
    model makes it near-certain. Everything else is cut, not demoted.
    """
    p = str(priority)[:2]
    turnover = str(rec.get("turnover", "")).replace(",", "")
    try:
        t = float(turnover)
    except ValueError:
        t = 0.0

    what = " ".join(str(rec.get(k, "")) for k in
                    ("one_liner", "call_ammo", "sic_codes", "excerpts"))

    # Evidence in the accounts beats any sector reasoning.
    if p in ("P1", "P2"):
        return 1, "accounts show exposure, nothing held against it"
    if p == "P3":
        return 2, "holds instruments, so the exposure is real"

    # A thin filing that still discloses currency beats any sector reasoning.
    evidence = has_hard_evidence(rec)
    if evidence:
        return 2, evidence

    # No evidence. It has to be near-certain from the business model.
    # A manufacturer buys raw material, so it is never a "does not buy goods"
    # case however many service words appear in the description.
    if _NOT_A_BUYER.search(what) and not _MAKES_THINGS.search(what):
        return 0, "does not buy goods, no exposure to have"
    if _DOMESTIC_INPUT.search(what) and not re.search(r"import", what, re.I):
        return 0, "domestic raw material"
    if not _IMPORTS_LIKELY.search(what):
        return 0, "nothing in the accounts or the business model suggests FX"
    if t < 5e6:
        return 0, "sector fits but too small to be worth the credit"

    return 3, "no disclosure, but this sector buys abroad"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--contacts", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--extra-cache", nargs="*", default=[],
                    help="additional call sheet CSVs to fold in")
    args = ap.parse_args()

    records = []
    with open(args.cache, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                records.append(json.loads(line))
    for extra in args.extra_cache:
        with open(extra, encoding="utf-8-sig", newline="") as fh:
            records.extend(list(csv.DictReader(fh)))
    print(f"cache holds {len(records)} records", flush=True)

    idx = build_index(records)

    with open(args.contacts, encoding="utf-8-sig", newline="") as fh:
        contacts = list(csv.DictReader(fh))
    print(f"{len(contacts)} contacts", flush=True)

    rows, matched, cut = [], 0, []
    for c in contacts:
        company = c.get("company", "")
        rec = idx.get(squash(company))
        if rec is None:
            rows.append({
                "name": c.get("name", ""), "job_title": c.get("job_title", ""),
                "company": company, "phone": c.get("phone", ""),
                "email": c.get("email", ""), "priority": "NOT TRIAGED",
                "call_rank": 3, "why_call": "not triaged, qualify by phone",
                "business": "", "money": "", "happening": "",
                "currency": "not triaged, run the accounts first", "risk": "",
                "_t": 0,
            })
            continue
        matched += 1
        pri = ch_classify.priority(rec) if HAS_CLASSIFY else rec.get("priority", "")
        rank, why = worth_calling(rec, pri)
        if rank == 0:
            cut.append((company, why))
            continue
        t = str(rec.get("turnover", "")).replace(",", "")
        rows.append({
            "name": c.get("name", ""),
            "job_title": c.get("job_title", ""),
            "company": company,
            "phone": c.get("phone", ""),
            "email": c.get("email", ""),
            "priority": pri,
            "call_rank": rank,
            "why_call": why,
            "business": clip(rec.get("one_liner", ""), 120),
            "money": money_line(rec),
            "happening": happening_line(rec),
            "currency": currency_line(rec),
            "risk": risk_line(rec),
            "_t": float(t) if t.replace(".", "").isdigit() else 0,
        })

    print(f"{matched}/{len(contacts)} matched to accounts", flush=True)
    if cut:
        print(f"{len(cut)} cut as not worth a call or a Lusha credit:", flush=True)
        from collections import Counter
        for reason, n in Counter(w for _, w in cut).most_common():
            print(f"    {n:4}  {reason}", flush=True)

    # sort by whether it is worth ringing, then by size
    rows.sort(key=lambda r: (r["call_rank"], -r["_t"]))
    for r in rows:
        r.pop("_t")

    cols = ["call_rank", "why_call", "name", "job_title", "company", "phone",
            "email", "priority", "business", "money", "happening", "currency",
            "risk"]
    text = ""
    buf = []
    for r in rows:
        buf.append(r)
    out = Path(args.out)
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(buf)
    # strip the trailing newline: Word reads a blank final row as a record and
    # the merge macro dies with runtime error 5631
    raw = out.read_bytes().rstrip(b"\r\n")
    out.write_bytes(raw)

    if cut:
        # keep the discards visible: a wrong rule should be easy to spot
        cutfile = out.with_name(out.stem + "_cut.csv")
        with cutfile.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["company", "why_cut"])
            w.writerows(cut)
        print(f"cut list written to {cutfile}")

    print(f"written to {out}")


if __name__ == "__main__":
    main()
