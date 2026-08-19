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

    rows, matched = [], 0
    for c in contacts:
        company = c.get("company", "")
        rec = idx.get(squash(company))
        if rec is None:
            rows.append({
                "name": c.get("name", ""), "job_title": c.get("job_title", ""),
                "company": company, "phone": c.get("phone", ""),
                "email": c.get("email", ""), "priority": "NOT TRIAGED",
                "business": "", "money": "", "happening": "",
                "currency": "not triaged, run the accounts first", "risk": "",
                "_t": 0,
            })
            continue
        matched += 1
        pri = ch_classify.priority(rec) if HAS_CLASSIFY else rec.get("priority", "")
        t = str(rec.get("turnover", "")).replace(",", "")
        rows.append({
            "name": c.get("name", ""),
            "job_title": c.get("job_title", ""),
            "company": company,
            "phone": c.get("phone", ""),
            "email": c.get("email", ""),
            "priority": pri,
            "business": clip(rec.get("one_liner", ""), 120),
            "money": money_line(rec),
            "happening": happening_line(rec),
            "currency": currency_line(rec),
            "risk": risk_line(rec),
            "_t": float(t) if t.replace(".", "").isdigit() else 0,
        })

    print(f"{matched}/{len(contacts)} matched to accounts", flush=True)

    order = {"P1": 0, "P2": 1, "P3": 2, "P4": 3, "NO": 4, "X ": 5}
    rows.sort(key=lambda r: (order.get(str(r["priority"])[:2], 9), -r["_t"]))
    for r in rows:
        r.pop("_t")

    cols = ["name", "job_title", "company", "phone", "email", "priority",
            "business", "money", "happening", "currency", "risk"]
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

    print(f"written to {out}")


if __name__ == "__main__":
    main()
