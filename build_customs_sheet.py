"""
Customs-sourced prospecting.

The insight that makes this different from the normal call sheet: a company in
HMRC's UK Trade Info export is a proven importer or exporter. So Companies House
is no longer asked "does this business trade internationally" (customs already
answered that). It is asked "how big, how healthy, and is there extra FX
evidence". Silence in the accounts cannot cut a customs-verified importer; it
only means the ranking rests on the trade data alone.

Input: the UK Trade Info CSV with columns
    CompanyName, TradeTypeDescription, Month, Year, CommodityCode, Commodity
(extra columns are ignored). Multiple rows per company are expected, one per
month traded; the pipeline collapses them and counts the months.

Ranking, customs mode:
    verified + explicit FX evidence in the accounts        -> 1
    verified + repeated months (>=4) at £5m+               -> 1
    verified + £5m+                                        -> 2
    verified + traded but small or thin                    -> 3
    group treasury / listed parent / micro / dormant       -> cut
"""
import argparse, csv, json, re, sys, unicodedata
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_call_sheet as B          # reuse squash, lookup, money_line, etc.


def read_customs(path: str):
    """Collapse the UK Trade Info export to one record per company."""
    agg = defaultdict(lambda: {"months": set(), "trade": set(),
                               "commodities": set(), "year": ""})
    with open(path, encoding="utf-8-sig", newline="") as fh:
        r = csv.DictReader(fh)
        cols = {c.lower().strip(): c for c in (r.fieldnames or [])}
        name_c = cols.get("companyname") or cols.get("name")
        month_c = cols.get("month")
        trade_c = cols.get("tradetypedescription") or cols.get("tradetype")
        comm_c = cols.get("commodity") or cols.get("commoditydescription")
        year_c = cols.get("year")
        if not name_c:
            raise SystemExit("customs file needs a CompanyName column")
        for row in r:
            name = (row.get(name_c) or "").strip()
            if not name:
                continue
            a = agg[name]
            if month_c and row.get(month_c):
                a["months"].add(str(row[month_c]).strip())
            if trade_c and row.get(trade_c):
                a["trade"].add(row[trade_c].strip())
            if comm_c and row.get(comm_c):
                a["commodities"].add(row[comm_c].strip())
            if year_c and row.get(year_c):
                a["year"] = str(row[year_c]).strip()
    out = []
    for name, a in agg.items():
        out.append({
            "name": name,
            "months_traded": len(a["months"]),
            "trade_type": "; ".join(sorted(a["trade"]))[:60],
            "commodity": "; ".join(sorted(a["commodities"]))[:60],
            "year": a["year"],
        })
    return out


# reasons a customs-verified importer is still not worth a call.
# Central treasury is decisive at any size: a UK subsidiary whose FX is run by
# the parent cannot choose Lumon however large or small it is. Listed status or
# an explicit parent is enough on its own; no turnover threshold needed.
_CENTRAL_TREASURY = re.compile(
    r"group treasury|central treasury|treasury (is )?managed (centrally|by the (group|parent))"
    r"|hedg\w+ (is |are )?(managed|arranged|undertaken) (centrally|by the (group|parent))"
    r"|foreign exchange[^.]{0,40}(managed|handled)[^.]{0,20}(group|parent) level",
    re.I)
_LISTED_OR_SUB = re.compile(r"\bplc\b|listed on|ftse|subsidiary of|part of the .+ group", re.I)


def rank_customs(rec, customs):
    """rec may be None (never processed by CH). customs is always present."""
    months = customs["months_traded"]
    verified = f"customs verified importer, seen in {months} month{'s' if months!=1 else ''} of {customs['year'] or 'the data'}"

    # size and health come from the accounts if we have them
    t = 0.0
    if rec is not None:
        raw = B.scale(rec.get("turnover", ""), B.filed_in_thousands(rec)) if rec.get("turnover") else None
        t = raw or 0.0
        blob = " ".join(str(rec.get(k, "")) for k in ("one_liner", "call_ammo", "excerpts", "findings"))
        if _CENTRAL_TREASURY.search(blob):
            return 0, "customs verified but the accounts say FX is run at group level, no local decision"
        if _LISTED_OR_SUB.search(str(rec.get("company", "")) + " " + blob):
            return 0, "customs verified but listed or a subsidiary, treasury likely central"
        # explicit FX evidence lifts it to the top regardless of size
        if B._tier1_or_2(rec):
            return 1, verified + ", and the accounts disclose FX exposure"
        # micro or dormant: not worth the credit even though it traded
        cat = str(rec.get("accounts_category", "")).lower()
        if 0 < t < 1e6 and cat in ("micro", "dormant", "small") and months < 3:
            return 0, "customs verified but micro and infrequent, not worth the credit"

    # no FX note, but customs proves the trade. Rank on size and frequency.
    if t >= 5e6 and months >= 4:
        return 1, verified + f", £{t/1e6:.0f}m turnover, a regular importer, no hedging disclosed in the accounts"
    if t >= 5e6:
        return 2, verified + f", £{t/1e6:.0f}m turnover"
    if months >= 4:
        return 2, verified + ", trading most months"
    return 3, verified


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--customs", required=True, help="UK Trade Info export CSV")
    ap.add_argument("--cache", required=True)
    ap.add_argument("--contacts", default="", help="optional Lusha contacts CSV")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    customs = read_customs(args.customs)
    print(f"{len(customs)} companies in the customs file", flush=True)

    records = []
    with open(args.cache, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                records.append(json.loads(line))
    idx = B.build_index(records)

    contacts = {}
    if args.contacts:
        with open(args.contacts, encoding="utf-8-sig", newline="") as fh:
            for c in csv.DictReader(fh):
                contacts[B.squash(c.get("company", c.get("name", "")))] = c

    rows, cut = [], []
    cached = uncached = 0
    for cu in customs:
        rec, how = B.lookup(idx, cu["name"])
        if rec is not None:
            cached += 1
        else:
            uncached += 1
        rank, why = rank_customs(rec, cu)
        c = contacts.get(B.squash(cu["name"]), {})
        row = {
            "call_rank": rank if rank else 4,
            "why_call": why if rank else "cut: " + why,
            "company": cu["name"],
            "months_traded": cu["months_traded"],
            "trade_type": cu["trade_type"],
            "commodity": cu["commodity"],
            "name": c.get("name", ""), "email": c.get("email", ""),
            "phone": c.get("phone", ""),
            "money": (B.money_line(rec) if rec else "not in cache, not yet triaged"),
            "currency": (B.currency_line(rec) if rec else ""),
            "in_cache": "yes" if rec else "no",
            "_t": 0.0,
        }
        if rec:
            raw = B.scale(rec.get("turnover", ""), B.filed_in_thousands(rec)) if rec.get("turnover") else 0
            row["_t"] = raw or 0.0
        rows.append(row)
        if rank == 0:
            cut.append(cu["name"])

    rows.sort(key=lambda r: (r["call_rank"], -r["months_traded"], -r["_t"]))
    for r in rows:
        r.pop("_t")
    cols = ["call_rank", "why_call", "company", "months_traded", "trade_type",
            "commodity", "name", "email", "phone", "money", "currency", "in_cache"]
    with open(args.out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    raw = Path(args.out).read_bytes().rstrip(b"\r\n")
    Path(args.out).write_bytes(raw)

    from collections import Counter
    print(f"cached {cached}, needed CH {uncached}")
    print("ranks:", dict(sorted(Counter(r['call_rank'] for r in rows).items())))
    print(f"written to {args.out}")


if __name__ == "__main__":
    main()
