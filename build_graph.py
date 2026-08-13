#!/usr/bin/env python3
"""Find the people and firms that sit around your FX-qualified companies.

Calling 14 CFOs is 14 conversations. Getting to the one private equity firm
that owns all 14 is one. This builds that map from Companies House, using
structured endpoints rather than parsing accounts text:

  charges     who lends to them. A bank appearing across 30 qualified
              companies is a relationship worth having, and it also tells you
              which incumbent you are usually up against.
  PSC         who ultimately owns them. This is where private equity sponsors
              and family holding structures surface.
  officers    who sits on the board. A finance director on two boards in your
              universe is a warm route into both.

Output is two files. connectors.csv ranks every firm or person by how much
qualified turnover they touch. company_connections.csv is the raw edge list,
so you can look up one company and see everyone around it.

    python build_graph.py
    python build_graph.py --min-companies 3 --out-dir graph/
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

BASE = "https://api.company-information.service.gov.uk"

# Lenders and sponsors are the two connector types worth chasing. Trade
# creditors and individuals with a single charge are noise.
NOISE = re.compile(
    r"^(hm revenue|hmrc|the registrar|companies house|n/?a|unknown|none)\b", re.I
)
# A PSC that is an individual tells you about ownership but is rarely a route
# in. Corporate PSCs are the interesting ones: sponsors, holdcos, trade owners.
CORPORATE_PSC = ("corporate-entity-person-with-significant-control",
                 "legal-person-person-with-significant-control")


def _key() -> str:
    return os.environ.get("CH_API_KEY", "")


def get(path: str, session=None, **params):
    for attempt in range(3):
        try:
            r = (session or requests).get(
                f"{BASE}{path}", auth=(_key(), ""), params=params or None, timeout=25)
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code == 429:
            time.sleep(6)
            continue
        if r.status_code != 200:
            return {}
        return r.json()
    return {}


def tidy(name: str) -> str:
    """Normalise a firm name so the same lender is not counted five ways."""
    n = re.sub(r"\s+", " ", str(name or "")).strip().rstrip(".,")
    n = re.sub(r"\b(plc|limited|ltd|llp|uk|\(uk\)|bank|banking)\b\.?", "", n, flags=re.I)
    n = re.sub(r"\s+", " ", n).strip(" -,")
    return n.title() if n.isupper() else n


def lenders(number: str, session=None) -> list[str]:
    j = get(f"/company/{number}/charges", session, items_per_page=50)
    out = []
    for c in j.get("items", []) or []:
        if str(c.get("status", "")).startswith("satisfied"):
            continue
        for p in c.get("persons_entitled") or []:
            nm = tidy(p.get("name", ""))
            if nm and not NOISE.match(nm) and len(nm) > 3:
                out.append(nm)
    return out


def owners(number: str, session=None) -> list[tuple[str, str]]:
    j = get(f"/company/{number}/persons-with-significant-control", session,
            items_per_page=25)
    out = []
    for p in j.get("items", []) or []:
        if p.get("ceased_on"):
            continue
        kind = str(p.get("kind", ""))
        nm = tidy(p.get("name", ""))
        if not nm or NOISE.match(nm):
            continue
        out.append((nm, "owner (company)" if kind in CORPORATE_PSC else "owner (person)"))
    return out


def board(number: str, session=None) -> list[tuple[str, str]]:
    j = get(f"/company/{number}/officers", session, items_per_page=50)
    out = []
    for o in j.get("items", []) or []:
        if o.get("resigned_on"):
            continue
        nm = re.sub(r"\s+", " ", str(o.get("name", ""))).strip()
        if not nm:
            continue
        occ = str(o.get("occupation", "") or "")
        out.append((nm.title(), occ))
    return out


def money(v) -> float:
    try:
        return float(re.sub(r"[^0-9.]", "", str(v) or "0") or 0)
    except ValueError:
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, default=Path("ch_results_cache.jsonl"))
    ap.add_argument("--out-dir", type=Path, default=Path("."))
    ap.add_argument("--min-companies", type=int, default=2,
                    help="only report connectors touching at least this many")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-officers", action="store_true",
                    help="faster: lenders and owners only")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).parent))
    import ch_cache
    try:
        import ch_classify
        HAS_CLASSIFY = True
    except ImportError:
        HAS_CLASSIFY = False

    universe = {}
    for rec in ch_cache.load_cache(args.cache).values():
        num = str(rec.get("number", "")).strip()
        if not num or num in universe:
            continue
        if HAS_CLASSIFY and not str(ch_classify.priority(rec)).startswith(("P1", "P2", "P3")):
            continue
        universe[num] = rec

    items = list(universe.items())
    if args.limit:
        items = items[: args.limit]
    print(f"mapping {len(items)} qualified companies", flush=True)

    edges = []
    conn = defaultdict(lambda: {"companies": set(), "turnover": 0.0, "types": set()})

    with requests.Session() as sess:
        for i, (num, row) in enumerate(items, 1):
            co, t = row.get("company", ""), money(row.get("turnover"))

            def add(name, kind, detail=""):
                edges.append({"company": co, "number": num, "turnover": row.get("turnover", ""),
                              "connector": name, "type": kind, "detail": detail,
                              "priority": ch_classify.priority(row) if HAS_CLASSIFY else ""})
                c = conn[name]
                c["companies"].add(co)
                c["types"].add(kind)
                if co not in getattr(add, "_counted", set()):
                    pass
                c["turnover"] = c["turnover"]  # summed after dedupe below

            for nm in set(lenders(num, sess)):
                add(nm, "lender")
            for nm, kind in owners(num, sess):
                add(nm, kind)
            if not args.skip_officers:
                for nm, occ in board(num, sess):
                    add(nm, "director", occ)

            if i % 20 == 0:
                print(f"  {i}/{len(items)}, {len(conn)} connectors so far", flush=True)
            time.sleep(0.12)

    # sum turnover once per company per connector
    seen = defaultdict(set)
    tmap = {r["company"]: money(r["turnover"]) for r in edges}
    for e in edges:
        if e["company"] not in seen[e["connector"]]:
            seen[e["connector"]].add(e["company"])
            conn[e["connector"]]["turnover"] += tmap.get(e["company"], 0.0)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, d in conn.items():
        if len(d["companies"]) < args.min_companies:
            continue
        rows.append({
            "connector": name,
            "type": "/".join(sorted(d["types"])),
            "companies": len(d["companies"]),
            "combined_turnover": int(d["turnover"]),
            "examples": "; ".join(sorted(d["companies"])[:6]),
        })
    rows.sort(key=lambda r: (-r["companies"], -r["combined_turnover"]))

    cp = args.out_dir / "connectors.csv"
    with cp.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["connector", "type", "companies",
                                           "combined_turnover", "examples"])
        w.writeheader()
        w.writerows(rows)

    ep = args.out_dir / "company_connections.csv"
    with ep.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["company", "number", "turnover",
                                           "connector", "type", "detail", "priority"])
        w.writeheader()
        w.writerows(edges)

    print(f"\n{len(rows)} connectors touching {args.min_companies}+ companies -> {cp}")
    print(f"{len(edges)} connections -> {ep}\n")

    print("--- who to build a relationship with ---")
    for r in rows[:15]:
        print(f"  {r['companies']:3} companies  £{r['combined_turnover']/1e6:8.1f}m  "
              f"{r['type']:16} {r['connector'][:44]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
