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

import requests

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


# Companies House has no turnover filter, but the accounts type it publishes is a
# reliable proxy for size and costs one cheap API call with no document fetch.
#
# Every substantial find so far filed full, group or medium accounts. Every dud
# filed micro, small, abridged or "total exemption full", which despite the name
# means a small company claiming audit exemption. Screening on this before
# fetching anything avoids spending OCR time and Anthropic credits on companies
# that legally do not have to disclose enough to qualify them.
BIG_ENOUGH = {"full", "group", "medium"}
TOO_SMALL = {
    "micro-entity", "small", "dormant", "abridged", "total-exemption-full",
    "total-exemption-small", "unaudited-abridged", "filing-exemption-subsidiary",
    "audit-exemption-subsidiary", "initial", "no-accounts-type-available",
}


def accounts_size(number: str, session=None) -> tuple[str, bool]:
    """Return (accounts type, is it worth reading). One cheap profile call."""
    try:
        get = (session or requests).get
        import os
        r = get(f"https://api.company-information.service.gov.uk/company/{number}",
                auth=(os.environ.get("CH_API_KEY", ""), ""), timeout=20)
        if r.status_code != 200:
            return "", True                # cannot tell, so let it through
        j = r.json()
        if str(j.get("company_status", "")).lower() not in ("active", ""):
            return f"status: {j.get('company_status')}", False
        t = str(((j.get("accounts") or {}).get("last_accounts") or {}).get("type", "")).lower()
        if not t:
            return "", True
        if t in TOO_SMALL:
            return t, False
        return t, t in BIG_ENOUGH or True
    except Exception:
        return "", True                    # never let the screen kill a run


def main() -> int:
    ap = argparse.ArgumentParser(description="Discover then triage UK companies")
    ap.add_argument("--vertical", default="", help=f"one of: {', '.join(sorted(disc.VERTICALS))}")
    ap.add_argument("--sic", default="", help="comma separated SIC codes, instead of a vertical")
    ap.add_argument("--location", default="", help="town, city or region. Blank searches nationally")
    ap.add_argument("--max", type=int, default=300,
                    help="how many companies that PASS the size screen to find")
    ap.add_argument("--search-cap", type=int, default=0,
                    help="stop searching after this many raw candidates (0 = 12x max)")
    ap.add_argument("--min-age", type=int, default=8, help="skip companies incorporated more recently")
    ap.add_argument("--out-list", type=Path, default=Path("discovered.csv"))
    ap.add_argument("--out", type=Path, default=Path("call_sheet_discovered.csv"))
    ap.add_argument("--discover-only", action="store_true", help="build the list, skip the triage")
    ap.add_argument("--no-brief", action="store_true", help="skip the paid AI brief")
    ap.add_argument("--triage-limit", type=int, default=0, help="cap how many get triaged")
    ap.add_argument("--all-sizes", action="store_true",
                    help="do not screen out small and micro filers before triage")
    ap.add_argument("--keep-parents", action="store_true",
                    help="keep companies with a foreign or operating-group parent in the output")
    args = ap.parse_args()

    sic = [c.strip() for c in args.sic.split(",") if c.strip()]
    if not sic and not args.vertical:
        raise SystemExit("give --vertical or --sic")

    label = args.vertical or f"SIC {','.join(sic)}"
    where = args.location or "nationally"
    print(f"searching Companies House: {label}, {where}, "
          f"active, incorporated before {args.min_age} years ago", flush=True)

    # Most UK companies in any sector file small or micro accounts, so searching
    # for exactly `max` names and then screening leaves almost nothing. Instead
    # keep pulling pages until enough have passed the screen.
    want = args.max
    cap = args.search_cap or want * 12
    raw = disc.discover(
        vertical=args.vertical, sic_codes=sic or None, location=args.location,
        max_results=cap if not args.all_sizes else want, min_age_years=args.min_age,
    )
    print(f"found {len(raw)} candidates", flush=True)
    if not raw:
        print("nothing returned. Check the location spelling, or widen the SIC codes.")
        return 0
    rows = raw

    # Drop anything already processed, so repeat searches only surface new names.
    if HAS_CACHE:
        before = len(rows)
        rows = [r for r in rows
                if not ch_cache.cache_get(name=r["name"], number=r["number"])]
        if before - len(rows):
            print(f"dropping {before - len(rows)} already in the cache", flush=True)

    # Size screen. One profile call each, no documents, no credits.
    if not args.all_sizes:
        kept, dropped = [], {}
        with requests.Session() as sess:
            for i, r in enumerate(rows, 1):
                if len(kept) >= want:      # stop screening once we have enough
                    break
                if not r.get("number"):
                    kept.append(r)
                    continue
                t, ok = accounts_size(r["number"], sess)
                if ok:
                    r["accounts_type_hint"] = t
                    kept.append(r)
                else:
                    dropped[t or "unknown"] = dropped.get(t or "unknown", 0) + 1
                if i % 50 == 0:
                    print(f"  screened {i}/{len(rows)}, keeping {len(kept)}/{want}", flush=True)
                time.sleep(0.12)           # CH allows 600 requests per 5 minutes
        if dropped:
            detail = ", ".join(f"{v} {k}" for k, v in sorted(dropped.items(), key=lambda x: -x[1]))
            print(f"size screen dropped {sum(dropped.values())}: {detail}", flush=True)
        rows = kept
        print(f"{len(rows)} companies file accounts worth reading", flush=True)
        if not rows:
            print("nothing left after the size screen. Try a different location or vertical.")
            return 0

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
            res["parent_control"] = ch_classify.parent_control(res)
        results.append(res)

        rate = (time.time() - started) / i
        note = res.get("priority") or res.get("grade") or (res.get("error", "")[:34] or "done")
        print(f"[{i}/{len(todo)}] {row['name'][:44]:46} {note:34} "
              f"eta {(len(todo) - i) * rate / 60:5.1f}m", flush=True)

    # Drop companies where the decision sits with a foreign parent or a separate
    # operating group. A UK holding company that echoes the subsidiary's name is
    # kept, since those are structures rather than treasury functions.
    if HAS_CLASSIFY and not args.keep_parents:
        before = len(results)
        blocked = [r for r in results if not ch_classify.winnable(r)]
        results = [r for r in results if ch_classify.winnable(r)]
        if blocked:
            from collections import Counter
            mix = Counter(r.get("parent_control", "?") for r in blocked)
            print("\nparent screen dropped " + str(before - len(results)) + ": "
                  + ", ".join(f"{v} {k}" for k, v in mix.most_common()), flush=True)

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
