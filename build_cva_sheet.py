"""
CVA-sourced prospecting.

The trigger here is financial distress, not FX disclosure. A company in a
Company Voluntary Arrangement is restructuring, which is exactly when a finance
team is under pressure to cut costs, tighten cash management and review
suppliers, and when banks often pull the forward facility Lumon can still
offer. So the CVA is the reason to call. The accounts are used only to rank
and to remove companies that structurally cannot have FX (domestic services,
dormant shells, charities, property vehicles, group treasury).

This mirrors customs mode: source is the eligibility signal, Companies House is
enrichment, and silence on FX cannot cut a company that fits the thesis.

Input: the *_no_fx.csv (and call sheet) a --cva discovery run produces, which
already carry the full triaged record per company.
Ranking:
    imports/manufactures/distributes + real FX figure  -> 1
    imports/manufactures/distributes, £1m+             -> 2
    plausible international buyer, any size             -> 3
    domestic service / dormant / charity / property    -> cut
    group treasury / listed                            -> cut
"""
import argparse, csv, json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_call_sheet as B

# SIC ranges that plausibly buy or sell across a border. Distress plus one of
# these is a callable lead even with a silent filing.
_TRADED_SIC = re.compile(
    r"^(0[1-3]|1[0-8]|20|21|22|23|24|25|26|27|28|29|30|31|32|"
    r"46|47[1-9]|49|50|51|52|77)", )

# clearly domestic or irrelevant: these get cut even in CVA mode
_DOMESTIC = re.compile(
    r"^(55|56|68|69|70|75|8[0-9]|9[0-9]|41|42|43|45[23]|"
    r"64|65|66|85|86|87|88|93|94|96|97|99)")

_DOMESTIC_WORDS = re.compile(
    r"\b(restaurant|caf[eé]|pub|bar|hotel|takeaway|care home|nursing|"
    r"letting|estate agent|property|lettings|recruitment agency|nursery|"
    r"gym|leisure centre|hairdress|beauty salon|dental|veterinary|"
    r"solicitor|accountant|consultancy|charity|community|church|"
    r"cleaning services|security services|landscaping|scaffolding)\b", re.I)

_INTL_WORDS = re.compile(
    r"\b(import|export|manufactur|wholesal|distribut|supplier|trading|"
    r"overseas|international|shipping|freight)\b", re.I)

_TREASURY = re.compile(
    r"group treasury|central treasury|treasury (is )?managed (centrally|by the (group|parent))"
    r"|managed at group level", re.I)
_LISTED = re.compile(r"\bplc\b|listed on|ftse|subsidiary of|part of the .+ group", re.I)


def rank_cva(rec):
    company = str(rec.get("company", ""))
    sic = str(rec.get("sic_codes", ""))
    blob = " ".join(str(rec.get(k, "")) for k in
                    ("one_liner", "call_ammo", "excerpts", "findings"))
    cat = str(rec.get("accounts_category", "")).lower()

    # structural cuts first
    if cat == "dormant":
        return 0, "in a CVA but dormant, nothing trading"
    if _TREASURY.search(blob):
        return 0, "in a CVA but FX is run at group level, no local decision"
    if _LISTED.search(company + " " + blob):
        return 0, "in a CVA but listed or a subsidiary, treasury likely central"
    if _DOMESTIC_WORDS.search(blob):
        return 0, "in a CVA but a domestic service business, no cross-border trade"
    codes = re.findall(r"\d{4,5}", sic)
    if codes and all(_DOMESTIC.match(c) and not _TRADED_SIC.match(c) for c in codes):
        return 0, "in a CVA but the SIC codes are all domestic services"

    # does anything say it trades across a border?
    traded_sic = any(_TRADED_SIC.match(c) for c in codes)
    intl = bool(_INTL_WORDS.search(blob))

    # turnover for ranking
    t = B.scale(rec.get("turnover", ""), B.filed_in_thousands(rec)) if rec.get("turnover") else 0
    t = t or 0

    base = "in a live CVA"
    if traded_sic or intl:
        if B._tier1_or_2(rec):
            return 1, base + ", buys or sells abroad, and the accounts disclose FX"
        if t >= 1_000_000:
            return 2, base + f", buys or sells abroad, £{t/1e6:.1f}m turnover"
        return 3, base + ", plausibly buys or sells abroad, qualify by phone"

    # no signal either way. Distress alone isn't enough without a trade angle,
    # so these sit at the bottom rather than being called.
    return 0, "in a CVA but nothing suggests cross-border trade"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--infile", required=True,
                    help="the *_no_fx.csv (or call sheet) from a --cva discovery run")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    rows_in = list(csv.DictReader(open(args.infile, encoding="utf-8-sig")))
    print(f"{len(rows_in)} CVA companies in", flush=True)

    out, cut = [], 0
    for rec in rows_in:
        rank, why = rank_cva(rec)
        if rank == 0:
            cut += 1
        t = B.scale(rec.get("turnover", ""), B.filed_in_thousands(rec)) if rec.get("turnover") else 0
        out.append({
            "call_rank": rank if rank else 4,
            "why_call": why,
            "company": rec.get("company", ""),
            "sic_codes": rec.get("sic_codes", ""),
            "business": B.clip(rec.get("one_liner", ""), 120),
            "money": (B.money_line(rec) if rec.get("turnover") else "no turnover on file"),
            "currency": B.currency_line(rec),
            "locality": rec.get("locality", ""),
            "_t": t or 0,
        })
    out.sort(key=lambda r: (r["call_rank"], -r["_t"]))
    for r in out:
        r.pop("_t")
    cols = ["call_rank", "why_call", "company", "sic_codes", "business",
            "money", "currency", "locality"]
    with open(args.out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader(); w.writerows(out)
    raw = Path(args.out).read_bytes().rstrip(b"\r\n")
    Path(args.out).write_bytes(raw)

    from collections import Counter
    ranks = Counter(r["call_rank"] for r in out)
    print("ranks:", dict(sorted(ranks.items())))
    print(f"callable (1-3): {sum(v for k,v in ranks.items() if k in (1,2,3))}, cut: {ranks.get(4,0)}")
    print(f"written to {args.out}")


if __name__ == "__main__":
    main()
