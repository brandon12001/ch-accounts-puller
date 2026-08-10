#!/usr/bin/env python3
"""Find new companies from Companies House, then triage them.

Replaces buying lists. Companies House advanced search filters on SIC code,
location, status and incorporation date, free and unlimited, so the candidate
list is generated rather than purchased.

Anything already in the results cache is dropped before triage, so a repeat
search on the same vertical only ever surfaces companies not seen before.

    python run_discover.py --vertical "food wholesale" --max 300
    python run_discover.py --vertical "fabricated metal" --location Birmingham
    python run_discover.py --sic 46120,46720,46730 --max 200 --discover-only
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import ch_discover as disc

try:
    import ch_cache
    HAS_CACHE = True
except ImportError:
    HAS_CACHE = False

try:
    import ch_classify
    HAS_CLASSIFY = True
except ImportError:
    HAS_CLASSIFY = False


def main() -> int:
    ap = argparse.ArgumentParser(description="Discover then triage UK companies")
    ap.add_argument("--vertical", default="", help=f"one of: {', '.join(sorted(disc.VERTICALS))}")
    ap.add_argument("--sic", default="", help="comma separated SIC codes, instead of a vertical")
    ap.add_argument("--location", default="", help="town, city or region. Blank searches nationally")
    ap.add_argument("--max", type=int, default=300, help="how many companies to find")
    ap.add_argument("--min-age", type=int, default=8, help="skip companies incorporated more recently")
    ap.add_argument("--out-list", type=Path, default=Path("discovered.csv"))
    ap.add_argument("--out", type=Path, default=Path("call_sheet_discovered.csv"))
    ap.add_argument("--discover-only", action="store_true", help="build the list, skip the triage")
    ap.add_argument("--no-brief", action="store_true", help="skip the paid AI brief")
    ap.add_argument("--triage-limit", type=int, default=0, help="cap how many get triaged")
    args = ap.parse_args()

    sic = [c.strip() for c in args.sic.split(",") if c.strip()]
    if not sic and not args.vertical:
        raise SystemExit("give --vertical or --sic")

    label = args.vertical or f"SIC {','.join(sic)}"
    where = args.location or "nationally"
    print(f"searching Companies House: {label}, {where}, "
          f"active, incorporated before {args.min_age} years ago", flush=True)

    rows = disc.discover(
        vertical=args.vertical, sic_codes=sic or None, location=args.location,
        max_results=args.max, min_age_years=args.min_age,
    )
    print(f"found {len(rows)} companies", flush=True)
    if not rows:
        print("nothing returned. Check the location spelling, or widen the SIC codes.")
        return 0

    # Drop anything already processed, so repeat searches only surface new names.
    if HAS_CACHE:
        before = len(rows)
        rows = [r for r in rows
                if not ch_cache.cache_get(name=r["name"], number=r["number"])]
        if before - len(rows):
            print(f"dropping {before - len(rows)} already in the cache", flush=True)

    disc.write_run_list(rows, args.out_list)
    print(f"wrote {args.out_list} with {len(rows)} companies", flush=True)

    if args.discover_only:
        return 0

    # ---- triage ----
    import ch_engine as eng
    todo = rows[: args.triage_limit] if args.triage_limit else rows
    print(f"\ntriaging {len(todo)}, briefs {'off' if args.no_brief else 'on'}", flush=True)

    results, started = [], time.time()
    for i, row in enumerate(todo, 1):
        try:
            res = eng.process_company(
                name=row["name"], number=row["number"], do_brief=not args.no_brief,
            )
        except KeyboardInterrupt:
            print("\ninterrupted; completed companies are cached")
            break
        except Exception as exc:                  # one bad filing must not kill the run
            res = eng.blank_result(row["name"], row["number"])
            res["error"] = f"crashed: {exc}"
            if HAS_CACHE:
                ch_cache.cache_put(res)
        # carry the search fields through, the engine does not know about them
        for k in ("locality", "postcode", "sic_codes", "incorporated"):
            res.setdefault(k, "") or res.update({k: res.get(k) or row.get(k, "")})
        if HAS_CLASSIFY:
            res.update(ch_classify.classify(res))
        results.append(res)

        rate = (time.time() - started) / i
        note = res.get("priority") or res.get("grade") or (res.get("error", "")[:34] or "done")
        print(f"[{i}/{len(todo)}] {row['name'][:44]:46} {note:34} "
              f"eta {(len(todo) - i) * rate / 60:5.1f}m", flush=True)

    if results:
        cols: list[str] = []
        for r in results:
            for k in r:
                if k not in cols:
                    cols.append(k)
        with args.out.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(results)
        print(f"\nwrote {len(results)} rows to {args.out}")

    if HAS_CLASSIFY and results:
        from collections import Counter
        print()
        for pri, n in sorted(Counter(r.get("priority", "") for r in results).items()):
            print(f"  {n:4}  {pri}")
    if HAS_CACHE:
        print("cache now holds:", ch_cache.cache_stats())
    return 0


if __name__ == "__main__":
    sys.exit(main())
