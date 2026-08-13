#!/usr/bin/env python3
"""Watch the qualified universe for Companies House events worth a call.

The triage tool answers "who has FX exposure". This answers "who has something
happening right now", which is the difference between a cold call and a reason
to ring today.

Everything here comes from the free Companies House API against companies
already in the results cache, so there is no new data source to buy and no
list to build.

Four event types, scored by how much they actually change a currency position:

  new officer        a new FD or CFO reviews arrangements in their first months.
                     Raaisha Janghir was the strongest lead the system produced
                     and that came from a job change, not from accounts.
  new charge         borrowing, usually to fund something. Names the lender too,
                     which feeds the connector map.
  accounts filed     fresh figures, so the company is worth re-triaging
  officer resigned   often the other half of a finance change

    python trigger_monitor.py --days 30
    python trigger_monitor.py --days 90 --min-score 8 --out triggers.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import re
import sys
import time
from pathlib import Path

import requests

BASE = "https://api.company-information.service.gov.uk"

FINANCE_ROLE = re.compile(
    r"finance|financial|treasur|chief financial|cfo\b|controller", re.I
)
MD_ROLE = re.compile(r"managing director|chief executive|\bceo\b", re.I)

# Score reflects how much the event changes a currency position, not how
# newsworthy it is. A new FD outranks a new charge because the FD reviews
# everything; a charge only signals borrowing.
SCORES = {
    "new finance officer": 9,
    "new director": 5,
    "new charge": 6,
    "accounts filed": 4,
    "officer resigned (finance)": 6,
    "officer resigned": 2,
}


def _key() -> str:
    return os.environ.get("CH_API_KEY", "")


def get(path: str, session=None, **params):
    for attempt in range(3):
        try:
            r = (session or requests).get(
                f"{BASE}{path}", auth=(_key(), ""), params=params or None, timeout=25
            )
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code == 429:                 # 600 requests per 5 minutes
            time.sleep(6)
            continue
        if r.status_code != 200:
            return {}
        return r.json()
    return {}


def _date(s: str):
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def officer_events(number: str, since: dt.date, session=None) -> list[dict]:
    j = get(f"/company/{number}/officers", session, items_per_page=50)
    out = []
    for o in j.get("items", []) or []:
        role = str(o.get("officer_role", ""))
        title = f"{o.get('name','')}"
        occ = str(o.get("occupation", "") or "")
        appointed, resigned = _date(o.get("appointed_on")), _date(o.get("resigned_on"))
        finance = bool(FINANCE_ROLE.search(occ) or FINANCE_ROLE.search(role))
        if appointed and appointed >= since:
            kind = "new finance officer" if finance else "new director"
            out.append({"type": kind, "date": appointed.isoformat(),
                        "detail": f"{title}"
                                  + (f", {occ}" if occ else "")
                                  + (f" ({role})" if role and role != "director" else "")})
        if resigned and resigned >= since:
            kind = "officer resigned (finance)" if finance else "officer resigned"
            out.append({"type": kind, "date": resigned.isoformat(),
                        "detail": f"{title} resigned" + (f", {occ}" if occ else "")})
    return out


def charge_events(number: str, since: dt.date, session=None) -> list[dict]:
    j = get(f"/company/{number}/charges", session, items_per_page=30)
    out = []
    for c in j.get("items", []) or []:
        created = _date(c.get("created_on") or c.get("delivered_on"))
        if not created or created < since:
            continue
        if str(c.get("status", "")).startswith("satisfied"):
            continue
        holders = ", ".join(
            str(p.get("name", "")) for p in (c.get("persons_entitled") or [])
        )
        out.append({"type": "new charge", "date": created.isoformat(),
                    "detail": f"charge registered{': ' + holders if holders else ''}",
                    "lender": holders})
    return out


def filing_events(number: str, since: dt.date, session=None) -> list[dict]:
    j = get(f"/company/{number}/filing-history", session,
            category="accounts", items_per_page=5)
    out = []
    for f in j.get("items", []) or []:
        d = _date(f.get("date"))
        if d and d >= since:
            out.append({"type": "accounts filed", "date": d.isoformat(),
                        "detail": str(f.get("description", "accounts filed"))[:90]})
    return out


def why_it_matters(ev: dict, row: dict) -> str:
    """One line to read before dialling. Not an essay."""
    cur = str(row.get("currencies_named", "")).strip()
    cur = "" if cur.lower() in ("", "not disclosed") else cur.split(";")[0].strip()
    pos = str(row.get("sophistication", "")).lower()
    t = ev["type"]
    if t == "new finance officer":
        return ("New finance appointment. First months in the seat are when banking and "
                "currency arrangements get reviewed, and there is no loyalty to the incumbent.")
    if t == "officer resigned (finance)":
        return "Finance departure, so the arrangements they set up are now unowned."
    if t == "new charge":
        base = "New borrowing registered"
        if ev.get("lender"):
            base += f" with {ev['lender'].split(',')[0]}"
        return base + ". Usually funds something, and funding usually changes the buying pattern."
    if t == "accounts filed":
        extra = f" Last position: {pos}" if pos else ""
        return f"Fresh accounts filed, so the figures are worth re-reading.{extra}"
    return "Board change, worth checking what sits behind it."


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, default=Path("ch_results_cache.jsonl"))
    ap.add_argument("--days", type=int, default=30, help="how far back to look")
    ap.add_argument("--min-score", type=int, default=4)
    ap.add_argument("--out", type=Path, default=Path("triggers.csv"))
    ap.add_argument("--limit", type=int, default=0, help="cap companies checked")
    ap.add_argument("--qualified-only", action="store_true", default=True)
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).parent))
    import ch_cache
    try:
        import ch_classify
        HAS_CLASSIFY = True
    except ImportError:
        HAS_CLASSIFY = False

    cache = ch_cache.load_cache(args.cache)
    universe = {}
    for rec in cache.values():
        num = str(rec.get("number", "")).strip()
        if not num or num in universe:
            continue
        if args.qualified_only and HAS_CLASSIFY:
            if not str(ch_classify.priority(rec)).startswith(("P1", "P2", "P3")):
                continue
            if not ch_classify.winnable(rec):
                continue
        universe[num] = rec

    names = list(universe.items())
    if args.limit:
        names = names[: args.limit]
    since = dt.date.today() - dt.timedelta(days=args.days)
    print(f"watching {len(names)} qualified companies for events since {since}", flush=True)

    triggers = []
    with requests.Session() as sess:
        for i, (num, row) in enumerate(names, 1):
            evs = (officer_events(num, since, sess)
                   + charge_events(num, since, sess)
                   + filing_events(num, since, sess))
            for ev in evs:
                score = SCORES.get(ev["type"], 1)
                # recency matters: something from this week beats last quarter
                age = (dt.date.today() - _date(ev["date"])).days
                recency = 3 if age <= 7 else 2 if age <= 21 else 1 if age <= 60 else 0
                if score + recency < args.min_score:
                    continue
                triggers.append({
                    "score": score + recency,
                    "company": row.get("company", ""),
                    "number": num,
                    "turnover": row.get("turnover", ""),
                    "trigger": ev["type"],
                    "date": ev["date"],
                    "days_ago": age,
                    "detail": ev["detail"],
                    "why": why_it_matters(ev, row),
                    "fx_position": row.get("sophistication", ""),
                    "currencies": row.get("currencies_named", ""),
                    "lender": ev.get("lender", ""),
                })
            if i % 25 == 0:
                print(f"  {i}/{len(names)} checked, {len(triggers)} triggers", flush=True)
            time.sleep(0.12)

    triggers.sort(key=lambda t: (-t["score"], t["days_ago"]))
    if triggers:
        with args.out.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(triggers[0].keys()))
            w.writeheader()
            w.writerows(triggers)
    print(f"\n{len(triggers)} triggers written to {args.out}\n")

    from collections import Counter
    for k, v in Counter(t["trigger"] for t in triggers).most_common():
        print(f"  {v:4}  {k}")

    print("\n--- today's A list ---")
    for t in triggers[:10]:
        to = f" £{float(re.sub(r'[^0-9.]','',t['turnover'] or '0') or 0)/1e6:.1f}m" if t["turnover"] else ""
        print(f"\n[{t['score']}] {t['company']}{to}  ({t['days_ago']}d ago)")
        print(f"     {t['trigger']}: {t['detail'][:90]}")
        print(f"     {t['why'][:150]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
