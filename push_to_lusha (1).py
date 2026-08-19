#!/usr/bin/env python3
"""Push the night's qualified companies into a fresh Lusha table.

The nightly run produces qualified companies but no contacts. This closes that
gap: it creates a dated table, finds the decision-makers at each company, and
writes them into it. In the morning the table is already populated, so the only
manual step left is selecting all and pushing to Salesforce.

A new table per run, named for the date and vertical, so nothing gets mixed up
and an old run can be reopened later.

The job title filter matters. Filtering on seniority instead returns HR
directors, IT directors and heads of marketing: a search on 34 companies came
back with 28 wrong contacts that had to be deleted by hand. Exact job titles
are the only filter that reliably returns finance and general management.

Usage:
    python push_to_lusha.py --sheet call_sheet_2026-08-20.csv \
        --vertical "fabricated metal"

Needs LUSHA_API_KEY in the environment.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.lusha.com"

# The only filter that reliably returns finance and general management.
TITLES = [
    "Managing Director",
    "Chief Executive Officer",
    "CEO",
    "Chief Financial Officer",
    "CFO",
    "Finance Director",
    "Financial Director",
    "Group Finance Director",
    "Director of Finance",
    "Financial Controller",
    "Owner",
    "Founder",
]

# Companies House returns names in a form Lusha often will not match. Strip the
# decoration before searching.
def tidy(name: str) -> str:
    n = str(name).split("|")[0].strip()
    for suffix in (" LIMITED", " Limited", " LTD", " Ltd", " ltd", " PLC", " Plc"):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
    return n.strip(" .,")


def call(path: str, payload: dict, key: str, method: str = "POST") -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode() if payload else None,
        headers={"api_key": key, "Content-Type": "application/json"},
        method=method,
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:300]
            if e.code == 429:          # rate limited, back off and retry
                wait = (attempt + 1) * 20
                print(f"  rate limited, waiting {wait}s", flush=True)
                time.sleep(wait)
                continue
            print(f"  HTTP {e.code}: {body}", flush=True)
            return {}
        except Exception as e:                       # noqa: BLE001
            print(f"  request failed: {e}", flush=True)
            time.sleep(5)
    return {}


def main() -> int:
    key = os.environ.get("LUSHA_API_KEY", "").strip()
    if not key:
        print("::error::LUSHA_API_KEY is not set")
        return 1

    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", required=True, help="call sheet or triage CSV")
    ap.add_argument("--vertical", default="")
    ap.add_argument("--min-rank", type=int, default=2,
                    help="only push companies at this call_rank or better")
    ap.add_argument("--batch", type=int, default=25,
                    help="companies per search request")
    args = ap.parse_args()

    with open(args.sheet, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    # Prefer call_rank where the sheet has it, fall back to priority.
    companies: list[str] = []
    for r in rows:
        rank = r.get("call_rank", "")
        pri = str(r.get("priority", ""))
        keep = False
        if rank:
            keep = str(rank).isdigit() and int(rank) <= args.min_rank
        else:
            keep = pri.startswith(("P1", "P2", "P3"))
        if keep:
            name = tidy(r.get("company") or r.get("name", ""))
            if name and name not in companies:
                companies.append(name)

    if not companies:
        print("nothing worth pushing in this sheet")
        return 0
    print(f"{len(companies)} companies to look up", flush=True)

    today = datetime.date.today().isoformat()
    label = f"{today} {args.vertical}".strip()
    created = call("/v2/tables", {
        "name": f"Leads {label}",
        "entityType": "contacts",
        "visibility": "private",
    }, key)
    table_id = (created.get("data") or {}).get("tableId", "")
    if not table_id:
        print("::error::could not create the table")
        return 1
    url = f"https://dashboard.lusha.com/agent/{table_id}"
    print(f"table created: {url}", flush=True)

    found = 0
    for i in range(0, len(companies), args.batch):
        chunk = companies[i : i + args.batch]
        print(f"  batch {i // args.batch + 1}: {len(chunk)} companies", flush=True)
        res = call("/prospecting/contact/search", {
            "filters": {
                "contacts": {
                    "include": {
                        "companyNames": chunk,
                        "jobTitlesExactMatch": TITLES,
                        "countries": ["GB"],
                    }
                }
            },
            "pages": {"page": 0, "size": 50},
            "tableId": table_id,
        }, key)
        n = len((res.get("data") or []))
        found += n
        print(f"    {n} contacts", flush=True)
        time.sleep(2)          # stay inside the per-minute limit

    print(f"\n{found} contacts written to the table")
    print(f"open: {url}")

    # leave the link where the workflow can pick it up for the email
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"table_url={url}\n")
            fh.write(f"contacts_found={found}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
