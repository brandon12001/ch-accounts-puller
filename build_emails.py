#!/usr/bin/env python3
"""Contact list in, bespoke merge emails out.

Upload a CSV of contacts and this does the whole chain:

  1. matches each contact to the accounts already in the cache
  2. runs the triage on any company that has not been read yet
  3. writes one email per contact from that company's own filed figures
  4. exports a merge CSV for Word

It does not send. That is deliberate: sending from a script bypasses the
Salesforce BCC and the Outlook signature carrying the regulatory footer, both
of which have to be on every email. Word and Outlook do the sending.

Input needs a company column. Name and email columns are used when present:

    company,name,email
    Meadow Vale Foods Ltd,Nigel O'Donnell,nigelo@meadowvalefoods.co.uk

    python build_emails.py --contacts leads.csv --out merge.csv
    python build_emails.py --contacts leads.csv --no-triage   # cached only
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")

# The rules that took a fortnight of drafts to settle on. They go in the prompt
# rather than being applied afterwards, because a model that knows the
# constraints writes better prose than one that gets edited into shape.
SYSTEM = """You write cold emails for Brandon Ellis, a senior sales executive at
Lumon, an FX brokerage. He emails MDs and finance directors of UK businesses
that trade in foreign currency.

Write ONE email. Rules, all of them absolute:

- Open on a specific fact from the company's own filed accounts. Never open with
  "I am getting in touch about foreign currency" or any variation.
- Never sell on price, rates or being cheaper. Sell on strategy, margin
  certainty and structure.
- No flattery, no throat-clearing, no "I hope this finds you well".
- No em dashes anywhere. Use commas or full stops.
- No sign-off, no name, no regulatory footer. The email ends at the last line of
  body copy. Both are in his Outlook signature.
- British English.
- Four or five short paragraphs maximum. Shorter is better.
- Close with this conditional, adapted to their exposure: "If I could show you a
  way to protect against the downside on [their specific exposure], while still
  participating when the market moves in your favour, would that be worth a
  conversation?"
- Then a final line offering two named weekdays.

Tone: direct, informed, unhurried. He has read their accounts and is asking a
question about them, not pitching a product.

If they already hedge, do not pitch hedging. Ask about the instrument, the
horizon, or what sits outside their stated policy.
If they hold nothing, the exposure itself is the story.
If their policy uses discretionary language like "where appropriate", that gap
is the story.

Return JSON only, no other text:
{"subject": "...", "body": "..."}
The subject must be under nine words and must not contain the words FX, foreign
exchange, currency risk or hedging."""


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    s = re.sub(r"\b(ltd|limited|plc|llp|uk|holdings?|group|company|co|the|and|"
               r"international|intl|europe|european)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def call_claude(payload: str, retries: int = 3) -> dict | None:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return None
    body = json.dumps({
        "model": MODEL, "max_tokens": 900, "system": SYSTEM,
        "messages": [{"role": "user", "content": payload}],
    }).encode()
    req = urllib.request.Request(
        ANTHROPIC_URL, data=body,
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read())
            text = "".join(b.get("text", "") for b in data.get("content", []))
            text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
            return json.loads(text)
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(2 * (attempt + 1))
    return None


def brief_for(row: dict) -> str:
    """Everything the model needs about one company, and nothing else."""
    fields = [
        ("Company", row.get("company")),
        ("What they do", row.get("one_liner")),
        ("Turnover", row.get("turnover")),
        ("Currencies named", row.get("currencies_named")),
        ("Hedging instruments held", row.get("hedging_instruments")),
        ("Exchange gains or losses disclosed", row.get("fx_pnl_figures")),
        ("Export split", row.get("export_split")),
        ("Estimated FX volume", row.get("est_fx_volume")),
        ("Position", row.get("sophistication")),
        ("From their accounts", row.get("call_ammo")),
        ("Recent events", row.get("triggers")),
    ]
    out = []
    for label, v in fields:
        v = str(v or "").strip()
        if v and v.lower() not in ("not disclosed", "none", "no", "nan"):
            out.append(f"{label}: {v}")
    return "\n".join(out)


def confidence(row: dict) -> str:
    """Flag rows worth reading before they go out."""
    has_number = bool(re.search(r"\d", str(row.get("fx_pnl_figures", ""))
                                + str(row.get("hedging_instruments", ""))
                                + str(row.get("export_split", ""))))
    if not str(row.get("call_ammo", "")).strip():
        return "review - nothing from the accounts"
    if not str(row.get("turnover", "")).strip():
        return "review - no turnover"
    return "ok" if has_number else "review - no hard figure"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--contacts", required=True, type=Path)
    ap.add_argument("--cache", type=Path, default=Path("ch_results_cache.jsonl"))
    ap.add_argument("--out", type=Path, default=Path("merge.csv"))
    ap.add_argument("--no-triage", action="store_true",
                    help="skip companies not already in the cache")
    ap.add_argument("--priority", default="P1,P2,P3",
                    help="only write emails for these, or ALL")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).parent))
    import ch_cache
    try:
        import ch_classify
        HAS_CLASSIFY = True
    except ImportError:
        HAS_CLASSIFY = False

    # read contacts, tolerating whatever the export called its columns
    contacts = []
    with args.contacts.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            low = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            co = low.get("company") or low.get("company name") or low.get("account") or ""
            if not co:
                continue
            contacts.append({
                "company": co,
                "name": low.get("name") or low.get("contact") or low.get("contact name") or "",
                "email": low.get("email") or low.get("work email") or "",
                "title": low.get("title") or low.get("job title") or "",
                "phone": low.get("phone") or low.get("mobile") or "",
            })
    if args.limit:
        contacts = contacts[: args.limit]
    print(f"{len(contacts)} contacts", flush=True)

    cache = ch_cache.load_cache(args.cache)
    by_name = {}
    for rec in cache.values():
        k = norm(rec.get("company", ""))
        if k and (k not in by_name or str(rec.get("turnover", "")).strip()):
            by_name[k] = rec

    # triage anything not already read
    missing = [c for c in contacts if norm(c["company"]) not in by_name]
    if missing and not args.no_triage:
        print(f"{len(missing)} companies not yet read, triaging them now", flush=True)
        import ch_engine as eng
        for i, c in enumerate(missing, 1):
            try:
                res = eng.process_company(name=c["company"], do_brief=True)
            except Exception as exc:
                res = eng.blank_result(c["company"])
                res["error"] = f"crashed: {exc}"
            by_name[norm(c["company"])] = res
            print(f"  [{i}/{len(missing)}] {c['company'][:44]:46} "
                  f"{res.get('grade') or res.get('error','')[:30]}", flush=True)
    elif missing:
        print(f"{len(missing)} not in the cache, skipping (--no-triage)", flush=True)

    wanted = None if args.priority.upper() == "ALL" else tuple(
        p.strip() for p in args.priority.split(",") if p.strip())

    rows, skipped, failed = [], 0, 0
    for i, c in enumerate(contacts, 1):
        acct = by_name.get(norm(c["company"]))
        if not acct:
            skipped += 1
            continue
        pri = ch_classify.priority(acct) if HAS_CLASSIFY else ""
        if wanted and not str(pri).startswith(wanted):
            skipped += 1
            continue

        brief = brief_for(acct)
        if not brief.strip():
            skipped += 1
            continue

        first = c["name"].split()[0] if c["name"] else ""
        out = call_claude(brief)
        if not out or not out.get("body"):
            failed += 1
            continue

        body = re.sub(r"\u2014|\u2013", ",", out["body"]).strip()
        # The model is told not to sign off, but strip anything that slips
        # through. Repeat until nothing more comes off, since a sign-off is
        # usually two lines: the valediction and then the name.
        SIGNOFF = re.compile(
            r"\n\s*(best regards|kind regards|many thanks|best wishes|regards|"
            r"best|thanks|brandon[\w\s]*|lumon[\w\s]*)[,.]?\s*$", re.I)
        while True:
            trimmed = SIGNOFF.sub("", body).strip()
            if trimmed == body:
                break
            body = trimmed

        rows.append({
            "company": acct.get("company", c["company"]),
            "name": c["name"], "first_name": first, "email": c["email"],
            "title": c["title"], "phone": c["phone"],
            "greeting": f"Hi {first}," if first else "Hi,",
            "subject": out.get("subject", "").strip(),
            "body": body,
            "priority": pri,
            "turnover": acct.get("turnover", ""),
            "ch_number": acct.get("number", ""),
            "check": confidence(acct),
        })
        if i % 10 == 0:
            print(f"  written {len(rows)}/{i}", flush=True)
        time.sleep(0.4)

    if rows:
        with args.out.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    print(f"\n{len(rows)} emails written to {args.out}")
    print(f"  skipped (no accounts data or wrong priority): {skipped}")
    print(f"  failed (model error): {failed}")
    flagged = [r for r in rows if r["check"] != "ok"]
    if flagged:
        print(f"\n{len(flagged)} worth reading before sending:")
        for r in flagged[:12]:
            print(f"   {r['company'][:40]:42} {r['check']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
