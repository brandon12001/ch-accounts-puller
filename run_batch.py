#!/usr/bin/env python3
"""Run a batch of companies with no browser involved.

Streamlit Community Cloud gives one throttled CPU and dies when the browser
session drops. Every batch so far has been killed partway: 552 stopped at 389,
135 at 29, 106 at 28. OCR is exactly the workload that triggers the throttle.

This runs the same engine from the command line, so it can be driven by a
GitHub Actions runner, a laptop, or anything else with a CPU and no session to
lose. Results are cached per company as they complete, so an interrupted run
picks up where it stopped.

    python run_batch.py --input ch_run_list.csv --out results.csv
    python run_batch.py --input list.csv --limit 50 --no-brief
    python run_batch.py --input list.csv --force        # ignore the cache
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import ch_cache
import ch_engine as eng

try:
    import ch_classify
    HAS_CLASSIFY = True
except ImportError:
    HAS_CLASSIFY = False


def read_input(path: Path) -> list[dict]:
    """Accept a bare name column, or name plus number if it came from ch_discover."""
    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = [f.strip().lower() for f in (reader.fieldnames or [])]
        if "name" not in fields:
            raise SystemExit(f"{path} needs a 'name' column, found: {reader.fieldnames}")
        for row in reader:
            clean = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            if clean.get("name"):
                rows.append({"name": clean["name"], "number": clean.get("number", "")})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch Companies House accounts triage")
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=Path("call_sheet.csv"))
    ap.add_argument("--limit", type=int, default=0, help="stop after N companies")
    ap.add_argument("--no-brief", action="store_true", help="skip the paid AI brief")
    ap.add_argument("--force", action="store_true", help="re-read cached companies")
    ap.add_argument("--pause", type=float, default=0.0,
                    help="seconds between companies; use ~4 on throttled hosts")
    args = ap.parse_args()

    rows = read_input(args.input)
    if not args.force:
        before = len(rows)
        rows = [r for r in rows
                if not ch_cache.cache_get(name=r["name"], number=r["number"])]
        skipped = before - len(rows)
        if skipped:
            print(f"skipping {skipped} already in cache")
    if args.limit:
        rows = rows[: args.limit]

    print(f"processing {len(rows)} companies, briefs {'off' if args.no_brief else 'on'}")
    results, started = [], time.time()

    for i, row in enumerate(rows, 1):
        label = row["name"][:44]
        try:
            res = eng.process_company(
                name=row["name"], number=row["number"],
                do_brief=not args.no_brief, force=args.force,
            )
        except KeyboardInterrupt:
            print("\ninterrupted; everything completed so far is cached")
            break
        except Exception as exc:                      # never let one company kill a batch
            res = eng.blank_result(row["name"], row["number"])
            res["error"] = f"crashed: {exc}"
            if hasattr(ch_cache, "cache_put"):
                ch_cache.cache_put(res)
        if HAS_CLASSIFY:
            res.update(ch_classify.classify(res))
        results.append(res)

        rate = (time.time() - started) / i
        left = (len(rows) - i) * rate
        note = res.get("priority") or res.get("grade") or res.get("error", "")[:30] or "done"
        print(f"[{i}/{len(rows)}] {label:46} {note:34} eta {left/60:5.1f}m", flush=True)

        if args.pause:
            time.sleep(args.pause)

    if results:
        cols = list(results[0].keys())
        for r in results:
            for k in r:
                if k not in cols:
                    cols.append(k)
        with args.out.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in results:
                w.writerow(r)
        print(f"\nwrote {len(results)} rows to {args.out}")

    if HAS_CLASSIFY and results:
        from collections import Counter
        for pri, n in sorted(Counter(r.get("priority", "") for r in results).items()):
            print(f"  {n:4}  {pri}")
    print("cache now holds:", ch_cache.cache_stats())
    return 0


if __name__ == "__main__":
    sys.exit(main())
